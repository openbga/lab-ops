"""Review records: the laboratory's record that its activity records were reviewed, by whom, over
which period, what was examined and found, and the reviewer's sign-off (docs/product.md "Audit
review"; 45 CFR §164.308(a)(1)(ii)(D) and §164.316(b)).

Who. Holders of "Can review access and audit records" with a laboratory role (audit_review.
permissions) record and sign a review, add a note to one, and set how often reviews are due. Each
reads every review. Nobody changes or removes a signed review or a note: the database refuses it
(migration 0003).

Signing. A review is written signed, in one step: the reviewer enters their password again on the
form at each signing (docs/roadmap.md, open decision 3), which the form checks before anything is
locked (audit_review.review_forms.ReviewForm.check_password; a password not accepted is written to
the sign-in history as a failed re-authentication and counts toward the failed sign-in limit). The
service then holds the acting account (accounts.acting), decides from the account as it is held
that it still may review and still has the password that was checked, and writes the review with
the sign-off: the signer's name, username and roles as they stand at that moment, the meaning
(Reviewed), the time by the application's clock, and a SHA-256 checksum of exactly what was signed
(signed_content), with its audit event, in one transaction. Signings take a lock in turn before
they are numbered (SIGNING_LOCK), so numbers follow the order they commit in. The seed signs its
synthetic example through the same service without a password.

Notes. A correction, or what a follow-up came to, is added as a note: who, when, the text. Notes are
not part of what was signed. A note posts the review's revision as its page showed it (1 and one
more for each note); one added meanwhile refuses it, with the typing kept.

When reviews are due. The laboratory sets an interval in days, or none (no default ships). The
next review is due that many days after the newest review was signed, by the deployment's calendar.
The Reviews tab also shows the days after the last day any review covered and the gaps between the
periods reviews covered (status()).
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import Count, Max
from django.http import Http404
from django.utils import timezone

from accounts.acting import hold_actor
from accounts.roles import ROLE_LABELS, ROLES, effective_roles
from core.audit import record_event
from core.models import AuditEvent

from . import hours
from .models import (
    EXAMINED,
    NUMBER_PREFIX,
    Meaning,
    Outcome,
    Review,
    ReviewNote,
    ReviewSchedule,
)
from .permissions import may_review

SIGNED = "audit_review.signed"
NOTE_ADDED = "audit_review.note_added"
INTERVAL_CHANGED = "audit_review.interval_changed"
# A review covers at most this many days, the first and last included.
MOST_DAYS = hours.MOST_DAYS
TEXT_LIMIT = 4000
OTHER_LIMIT = 2000
MOST_INTERVAL = 366
# The advisory lock (PostgreSQL's two-key form) a save of the interval takes. First keys 19553 to
# 19562 name other locks (accounts.acting, core.reveal_confirmation and their neighbours).
SCHEDULE_LOCK = (19563, 0)
# The advisory lock every signing takes, until it commits, before it is given its number: numbers
# then follow the order signings commit in, so a list drawn as of the newest number it holds
# (review_views._upto) never later gains a smaller one.
SIGNING_LOCK = (19564, 0)
REFUSED = "This needs an explicit permission: Can review access and audit records."
PASSWORD_CHANGED = (
    "Your password changed while this page was open. Enter your password again to sign this review."
)
STALE_NOTE = (
    "A note was added to this review after you opened it. Your note has been kept; read the "
    "latest notes, then add it again."
)
STALE_INTERVAL = (
    "The review interval was changed after you opened this form. Your input has been kept; "
    "reload and compare the current setting before saving again."
)
# How many gaps the Reviews tab lists, newest first.
GAPS_SHOWN = 10


# ------------------------------------------------------------------------------ what is signed


def signed_content(review):
    """Exactly what a sign-off signs, as the bytes its checksum is taken of: the review's number,
    its period, what was examined, its outcome, findings and follow-up, and the sign-off itself
    (who by id, name, username and roles, the meaning and the moment), as JSON with sorted keys and
    no spaces. Notes added later are not part of it."""
    content = {
        "record": "audit review",
        "number": review.number,
        "period_from": review.period_from.isoformat(),
        "period_to": review.period_to.isoformat(),
        "examined": list(review.examined),
        "examined_other": review.examined_other,
        "outcome": review.outcome,
        "findings": review.findings,
        "follow_up": review.follow_up,
        "signer": {
            "id": review.signed_by_id,
            "name": review.signer_name,
            "username": review.signer_username,
            "roles": review.signer_roles,
        },
        "meaning": review.meaning,
        "signed_at": review.signed_at.astimezone(UTC).isoformat(timespec="microseconds"),
    }
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def checksum(review):
    return hashlib.sha256(signed_content(review)).hexdigest()


def intact(review):
    """Whether the review as stored still matches the checksum taken when it was signed."""
    return checksum(review) == review.checksum


def roles_label(account):
    """The account's roles as a sign-off records them: "Supervisor, Technical staff". The admin
    role is named on its own (the roles its work includes are not repeated)."""
    held = effective_roles(account)
    if "admin" in held:
        return ROLE_LABELS["admin"]
    return ", ".join(ROLE_LABELS[role] for role in ROLES if role in held)


# ------------------------------------------------------------------------------ checks


def _text(value, field_name, label, limit, required=False):
    value = (value or "").strip()
    if required and not value:
        raise ValidationError({field_name: f"{label} is required."})
    if len(value) > limit:
        raise ValidationError({field_name: f"{label} takes at most {limit:,} characters."})
    return value


def check_period(period_from, period_to, today=None):
    """The period's errors by field: both days given, From on or before To, To not after today,
    and at most MOST_DAYS days."""
    today = today or timezone.localdate()
    errors = {}
    if not isinstance(period_from, date):
        errors["period_from"] = "Enter the first day the review covers."
    if not isinstance(period_to, date):
        errors["period_to"] = "Enter the last day the review covers."
    if errors:
        return errors
    if period_to > today:
        errors["period_to"] = "A review covers days that have begun: choose today or earlier."
    elif period_from > period_to:
        errors["period_to"] = "The last day must be on or after the first."
    elif (period_to - period_from).days + 1 > MOST_DAYS:
        errors["period_to"] = f"A review covers at most {MOST_DAYS} days. Narrow the period."
    return errors


def _examined(examined, other):
    codes = [source.value for source in EXAMINED]
    chosen = list(dict.fromkeys(str(code) for code in examined or ()))
    unknown = [code for code in chosen if code not in codes]
    if unknown:
        raise ValidationError({"examined": "Choose from the lists offered."})
    other = _text(other, "examined_other", "Other records examined", OTHER_LIMIT)
    if not chosen and not other:
        raise ValidationError(
            {"examined": "Choose at least one list, or say what else was examined."}
        )
    return [code for code in codes if code in chosen], other


def _reviewer(actor):
    """The acting account as held for this change (hold_actor, called first), if it may review."""
    current = hold_actor(actor)
    if current is None or not may_review(current):
        raise PermissionDenied(REFUSED)
    return current


def _name(account):
    return account.get_full_name() or account.username


# ------------------------------------------------------------------------------ changes


def _next_id():
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(pg_get_serial_sequence(%s, 'id'))", [Review._meta.db_table])
        return cursor.fetchone()[0]


@transaction.atomic
def sign_review(
    *,
    actor,
    period_from,
    period_to,
    examined,
    examined_other="",
    outcome,
    findings="",
    follow_up="",
    request_ref="",
    password_hash=None,
):
    """Write a review signed by `actor` as reviewed (see the module docstring). The caller checked
    the password entered at signing; `password_hash` is the stored hash that check accepted
    (accounts.confirmation.password_checked), and the account as held must still have it: a
    password changed after the check, in another tab, refuses the signing. Returns the review."""
    signer = _reviewer(actor)  # first: before anything else this change locks
    if password_hash is not None and signer.password != password_hash:
        raise ValidationError({"confirm_password": PASSWORD_CHANGED})
    errors = check_period(period_from, period_to)
    if errors:
        raise ValidationError(errors)
    examined, examined_other = _examined(examined, examined_other)
    if outcome not in Outcome.values:
        raise ValidationError({"outcome": "Choose the outcome."})
    findings = _text(
        findings, "findings", "Findings", TEXT_LIMIT, required=outcome == Outcome.FOLLOW_UP
    )
    follow_up = _text(follow_up, "follow_up", "Follow-up", TEXT_LIMIT)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", list(SIGNING_LOCK))
    pk = _next_id()
    review = Review(
        id=pk,
        number=f"{NUMBER_PREFIX}{pk:06d}",
        period_from=period_from,
        period_to=period_to,
        examined=examined,
        examined_other=examined_other,
        outcome=outcome,
        findings=findings,
        follow_up=follow_up,
        signed_by_id=signer.pk,
        signer_name=_name(signer),
        signer_username=signer.username,
        signer_roles=roles_label(signer),
        meaning=Meaning.REVIEWED,
        signed_at=timezone.now(),
        request_ref=(request_ref or "")[:32],
    )
    review.checksum = checksum(review)
    review.save(force_insert=True)
    record_event(actor=signer, action=SIGNED, obj=review, after=_snapshot(review))
    return review


def _snapshot(review):
    """What a review's audit event holds: what was signed, its checksum, nothing of the request."""
    return {
        "number": review.number,
        "period_from": review.period_from.isoformat(),
        "period_to": review.period_to.isoformat(),
        "examined": list(review.examined),
        "examined_other": review.examined_other,
        "outcome": review.outcome,
        "findings": review.findings,
        "follow_up": review.follow_up,
        "signer_name": review.signer_name,
        "signer_username": review.signer_username,
        "signer_roles": review.signer_roles,
        "meaning": review.meaning,
        "signed_at": review.signed_at.isoformat(),
        "checksum": review.checksum,
    }


def revision_of(review, notes=None):
    """The revision a page shows of `review`: 1, and one more for each note it has."""
    count = notes if notes is not None else ReviewNote.objects.filter(review=review).count()
    return 1 + count


@transaction.atomic
def add_note(*, actor, review, text, expected_revision, request_ref=""):
    """Add a note to a signed review. `expected_revision` is the revision the page showed
    (revision_of): a note added since refuses this one."""
    author = _reviewer(actor)
    locked = Review.objects.select_for_update().filter(pk=getattr(review, "pk", None)).first()
    if locked is None:
        raise Http404("No such review.")
    text = _text(text, "text", "A note", TEXT_LIMIT, required=True)
    if expected_revision is None or revision_of(locked) != expected_revision:
        raise ValidationError(STALE_NOTE)
    note = ReviewNote.objects.create(
        review=locked,
        author_id=author.pk,
        author_name=_name(author),
        text=text,
        added_at=timezone.now(),
        request_ref=(request_ref or "")[:32],
    )
    record_event(
        actor=author,
        action=NOTE_ADDED,
        obj=locked,
        after={"number": locked.number, "note": note.pk, "text": text},
    )
    return note


def schedule():
    """The interval as it stands: the saved row, or an unsaved one with none set (revision 0)."""
    return ReviewSchedule.objects.filter(pk=1).first() or ReviewSchedule(pk=1)


@transaction.atomic
def set_interval(*, actor, interval_days, expected_revision, reason):
    """Set how often reviews are due, in days, or None for no interval, with a reason. Returns
    (schedule, changed). `expected_revision` is the revision the form showed (0 for an interval
    never saved): a change saved in between refuses this one."""
    current = _reviewer(actor)
    reason = _text(reason, "reason", "A reason", TEXT_LIMIT, required=True)
    if interval_days is not None and (
        not isinstance(interval_days, int)
        or isinstance(interval_days, bool)
        or not 1 <= interval_days <= MOST_INTERVAL
    ):
        raise ValidationError({"interval_days": f"Choose from 1 to {MOST_INTERVAL} days."})
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", list(SCHEDULE_LOCK))
    stored = ReviewSchedule.objects.select_for_update().filter(pk=1).first()
    if stored is None:
        stored = ReviewSchedule(pk=1)
    if expected_revision is None or stored.revision != expected_revision:
        raise ValidationError(STALE_INTERVAL)
    if stored.interval_days == interval_days:
        return stored, False
    before = {"interval_days": stored.interval_days}
    stored.interval_days = interval_days
    stored.revision += 1
    stored.save()
    record_event(
        actor=current,
        action=INTERVAL_CHANGED,
        obj=stored,
        before=before,
        after={"interval_days": interval_days},
        reason=reason,
    )
    return stored, True


def interval_history(limit=10):
    """The newest changes to the interval, newest first."""
    return (
        AuditEvent.objects.filter(action=INTERVAL_CHANGED)
        .select_related("actor")
        .order_by("-occurred_at")[:limit]
    )


# ------------------------------------------------------------------------------ status


@dataclass
class Status:
    """Where the laboratory's reviews stand, for the Reviews tab (status())."""

    interval: int | None
    today: date
    # The review signed most recently (the highest number: SIGNING_LOCK), or None.
    last: Review | None = None
    # The day the next review is due: the last one's signing day plus the interval.
    due: date | None = None
    # The last day any review covers, or None.
    covered_to: date | None = None
    # (first, last) runs of days between the first day any review covers and covered_to that no
    # review covers, newest first, at most GAPS_SHOWN of them, and how many there are in all.
    gaps: list = field(default_factory=list)
    gap_count: int = 0
    reviews: int = 0

    @property
    def days_left(self):
        return None if self.due is None else (self.due - self.today).days

    @property
    def state(self):
        """ "unset" (no interval), "none" (no review yet), "overdue", "today" or "due"."""
        if self.interval is None:
            return "unset"
        if self.last is None:
            return "none"
        left = self.days_left
        return "overdue" if left < 0 else "today" if left == 0 else "due"

    @property
    def unreviewed_from(self):
        """The first day after the last day any review covers, when that day has begun."""
        if self.covered_to is None or self.covered_to >= self.today:
            return None
        return self.covered_to + timedelta(days=1)


def gaps(periods):
    """The runs of days, (first, last), that none of `periods` ((first, last) pairs) covers,
    between the first day of the earliest and the latest last day, oldest first."""
    found = []
    reached = None
    for start, end in sorted(periods):
        if reached is not None and start > reached + timedelta(days=1):
            found.append((reached + timedelta(days=1), start - timedelta(days=1)))
        if reached is None or end > reached:
            reached = end
    return found


def status(today=None):
    """Where reviews stand now (Status), read in the caller's reading."""
    today = today or timezone.localdate()
    interval = schedule().interval_days
    last = Review.objects.order_by("-id").first()  # numbers follow the order of signing
    summary = Review.objects.aggregate(count=Count("id"), covered_to=Max("period_to"))
    holes = gaps(Review.objects.values_list("period_from", "period_to"))
    found = Status(
        interval=interval,
        today=today,
        last=last,
        covered_to=summary["covered_to"],
        gaps=list(reversed(holes))[:GAPS_SHOWN],
        gap_count=len(holes),
        reviews=summary["count"],
    )
    if last is not None and interval is not None:
        found.due = timezone.localtime(last.signed_at).date() + timedelta(days=interval)
    return found


def suggested_period(state, today=None):
    """The period a new review's form starts with: from the day after the last day any review
    covers (30 days ago when none does, today when that day is today) to yesterday, or to its
    first day when that is today; never more than MOST_DAYS days."""
    today = today or timezone.localdate()
    covered_to = state.covered_to if state is not None else None
    if covered_to is None:
        start = today - timedelta(days=30)
    elif covered_to >= today:
        start = today
    else:
        start = covered_to + timedelta(days=1)
    end = max(start, today - timedelta(days=1))
    start = max(start, end - timedelta(days=MOST_DAYS - 1))
    return start, end
