"""Demonstration seed for review records (core.seeding): a review interval of 31 days and one review
of last month's activity, signed by the demonstration's reviewer (desk.story.REVIEW_HOLDER, who
holds "Can review access and audit records"), with a note added a few days later. Written through
the audited services at moments in the past (nonconformance.seed.happened_at); the seed signs
without a password, which only a person signing on the form is asked for.

For the Access review section (audit_review.access_report): an unused account period of 90 days,
and one active staff account that has not signed in for longer (UNUSED_USERNAME, created months ago
with the Technical staff role, recorded as the account administration records a new account, less
its email address: accounts.setup.seed_snapshot). It has no password, so nobody can sign in as it,
and it is not added where an account already has its name or address. And one signed access review
of the staff accounts (audit_review.access_reviews), signed by the same reviewer three days ago:
every grant kept but the unused account's Technical staff role, decided for removal and still held.
Synthetic data only."""

from datetime import datetime, time, timedelta

from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import User
from accounts.setup import seed_snapshot
from core.audit import record_event
from desk.story import REVIEW_HOLDER
from nonconformance.seed import happened_at

from . import access_report, access_reviews, reviews
from .models import (
    AccessReview,
    AccessScope,
    Decision,
    GrantKind,
    Review,
    ReviewSchedule,
    UnusedPeriod,
)

INTERVAL_DAYS = 31
UNUSED_DAYS = 90
UNUSED_USERNAME = "former_technician"
UNUSED_EMAIL = "former.technician@example.invalid"


def seed_setup(ctx):
    """Nothing to set up: the reviewer gets the capability with the desk's records (desk.story),
    so the interval is set with the records."""


def _interval(reviewer):
    """Set the interval once. A laboratory that has set one since keeps its choice."""
    if ReviewSchedule.objects.exists():
        return
    reviews.set_interval(
        actor=reviewer,
        interval_days=INTERVAL_DAYS,
        expected_revision=0,
        reason="Demonstration: the example laboratory reviews its activity records each month.",
    )


def _local(day, hour):
    return timezone.make_aware(datetime.combine(day, time(hour)))


def _unused(reviewer):
    """The unused account period, once (a laboratory that has set one since keeps its choice), and
    the account the Unused accounts tab lists, once."""
    if not UnusedPeriod.objects.exists():
        access_report.set_period(
            actor=reviewer,
            days=UNUSED_DAYS,
            expected_revision=0,
            reason="Demonstration: accounts nobody has used for about three months are reviewed.",
        )
    # An account already using its name or its address is left as it is, and none is added. One
    # saved with the same name or address while the seed runs makes the account's insert fail: the
    # example is then left out (the insert's savepoint rolled back), and the seed goes on; any other
    # failure, a refused insert for another reason included, stops the seed. Known limit: one saved
    # meanwhile under the same address in other letter case is not seen (the address is unique as
    # typed), as for the other demonstration accounts.
    taken = Q(username__iexact=UNUSED_USERNAME) | Q(email__iexact=UNUSED_EMAIL)
    if User.objects.filter(taken).exists():
        return
    # Recorded as given by the demonstration's administrator: without one, no example is added.
    admin = User.objects.filter(username="demo_admin").first()
    if admin is None:
        return
    now = timezone.now()
    created, signed_in = now - timedelta(days=200), now - timedelta(days=120)
    with happened_at(created), transaction.atomic():
        account = User(
            username=UNUSED_USERNAME,
            email=UNUSED_EMAIL,
            first_name="Pat",
            last_name="Example",
            date_joined=created,
            last_login=signed_in,
        )
        account.set_unusable_password()
        try:
            with transaction.atomic():
                account.save()
        except IntegrityError as refused:
            if _constraint(refused) not in TAKEN:
                raise
            return
        account.groups.add(Group.objects.get(name="technical"))
        record_event(
            actor=admin,
            action="account.created",
            obj=account,
            after=seed_snapshot(account),
            reason="Demonstration seed: a technician who no longer signs in",
        )


# The constraints that refuse an account whose username or address is taken (accounts.User).
TAKEN = ("accounts_user_username_key", "accounts_user_email_key")


def _constraint(error):
    diagnostics = getattr(error.__cause__, "diag", None)
    return getattr(diagnostics, "constraint_name", None)


def _example_role(grants):
    """The keys of the grants the example review decides to remove: the Technical staff role of
    the unused example account (UNUSED_USERNAME, without a usable password and not signed in for
    UNUSED_DAYS), when it is the account the seed adds; none for an account of that name that is
    someone else's."""
    account = User.objects.filter(username=UNUSED_USERNAME).first()
    if account is None or account.has_usable_password():
        return set()
    if (account.last_login or account.date_joined) > access_report.cutoff(UNUSED_DAYS):
        return set()
    technical = Group.objects.filter(name="technical").values_list("pk", flat=True).first()
    return {
        grant.key
        for grant in grants
        if grant.account_id == account.pk
        and grant.kind == GrantKind.GROUP
        and grant.code == str(technical)
    }


def _access_review(reviewer):
    """One signed access review of the staff accounts, once: every grant kept, but the unused
    example account's Technical staff role (_example_role), decided for removal. The grants are
    read as they stand and signed through the service, as if three days ago."""
    if AccessReview.objects.exists():
        return
    signed = timezone.now() - timedelta(days=3)
    with transaction.atomic(), happened_at(signed):
        current = access_reviews.reading(AccessScope.STAFF)
        if current.too_many or not current.grants:
            return
        unused = _example_role(current.grants)
        decisions = {
            grant.key: Decision.REMOVE if grant.key in unused else Decision.KEEP
            for grant in current.grants
        }
        if unused:
            findings = (
                f"{UNUSED_USERNAME} has not signed in for months: the technician left the "
                "laboratory. Every other account's roles and capabilities match the person's "
                "current job."
            )
            follow_up = "Superuser to remove the role and deactivate the account this week."
        else:
            findings = "Every account's roles and capabilities match the person's current job."
            follow_up = ""
        access_reviews.sign_access_review(
            actor=reviewer,
            scope=AccessScope.STAFF,
            decisions=decisions,
            expected=current.fingerprint,
            findings=findings,
            follow_up=follow_up,
        )


def seed_records(ctx):
    """One signed review of the previous calendar month, and a note on what its follow-up came
    to, once; the unused account period and an unused account; one signed access review."""
    reviewer = User.objects.get(username=REVIEW_HOLDER)
    _interval(reviewer)
    _unused(reviewer)
    _access_review(reviewer)
    if Review.objects.exists():
        return
    now = timezone.now()
    today = timezone.localdate(now)
    last_day = today.replace(day=1) - timedelta(days=1)
    first_day = last_day.replace(day=1)
    # Signed on the third of this month, or an hour ago early in a month.
    signed = min(_local(today.replace(day=1) + timedelta(days=2), 10), now - timedelta(hours=1))
    with transaction.atomic(), happened_at(signed):
        review = reviews.sign_review(
            actor=reviewer,
            period_from=first_day,
            period_to=last_day,
            examined=["changes", "access", "sign_ins", "people"],
            outcome=Review.Outcome.FOLLOW_UP,
            findings="One client portal account had five failed sign-ins in a row on one "
            "evening, with no sign-in after them. Every other count matched normal working "
            "hours and the on-call rota.",
            follow_up="Client services to ask the organization whether the person still needs "
            "the account.",
        )
    noted = min(signed + timedelta(days=2), now - timedelta(minutes=30))
    with transaction.atomic(), happened_at(noted):
        reviews.add_note(
            actor=reviewer,
            review=review,
            text="The organization confirmed the person has left. Their portal access was ended "
            "the same day.",
            expected_revision=1,
        )
