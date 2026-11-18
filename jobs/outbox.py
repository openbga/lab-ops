"""The notification outbox: email notices recorded with the change that raised them, sent after it
commits, and retried by the scheduled job until they are sent, fail for good or no longer apply.

Recording: a hook calls `record()` inside the change's transaction, after the change's own
writes. Nothing is sent before the change commits, and a change that rolls back leaves no
notice. With email off (Django's dummy backend) when the change is saved, the notice is recorded
as Not sent, reason "email is off", and never sent later, even if email is turned on before
anything tried to send it.

Sending: right after the change commits, its notices get a first attempt, so a deployment
without a scheduler still sends promptly. That attempt stops at the first notice the mail server
does not take, so a slow or unreachable server delays the person's save by one wait for it
(EMAIL_TIMEOUT), not one per recipient; the rest wait for the scheduled job. The job
`send_notices` (`deliver_due`) retries each waiting notice whose time has come, and stops its
sweep after STOP_AFTER_FAILURES failures in a row.

Each attempt runs in its own transaction with the notice locked; a second sender skips a notice
that is locked. In order:

1. Email off: Not sent, reason "email is off".
2. The topic's handler (jobs.registry.NOTICE_TOPICS) takes shared locks on the rows its decision
   rests on (the record, the recipient's account and what gives them access) and holds them until
   the notice is sent, so a change to any of them waits until then and the notice goes out only
   on the reading it was decided from. From that reading it checks the notice still applies and
   words it from its allow-list. No longer applies: Not sent, reason "no longer applies". A row
   someone is changing at that moment: the attempt is not counted and is tried again a minute
   later (BUSY_DELAY).
3. Send. Taken by the mail backend: Sent. Refused, unreachable, or a backend that reports it sent
   nothing: the attempt is counted and the next one waits RETRY_DELAYS from the failure; after
   MAX_ATTEMPTS attempts, Failed.

A notice row and the log carry ids, codes, the record's number and the class of an error, never
an address, a name, ticket content or a mail server's message.

Delivery is at least once: if the process stops after the mail server accepted a notice and
before its row is marked sent, the next attempt sends it again.
"""

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import connection, transaction
from django.utils import timezone

from .locks import is_lock_unavailable
from .models import Notice, NoticeStatus, NotSentReason
from .registry import notice_handler

logger = logging.getLogger("lab_ops.notifications")

DUMMY_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
# Wait after the first, second ... seventh failed attempt; the eighth failure is final.
RETRY_DELAYS = tuple(timedelta(minutes=m) for m in (5, 15, 30, 60, 120, 240, 480))
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
# When a row the decision rests on is being changed: try again this much later, uncounted.
BUSY_DELAY = timedelta(minutes=1)
# How long an attempt waits for the record's row before it is left for later.
LOCK_WAIT = "2s"
# Most notices one run of the job sends; the rest wait for the next run.
SEND_BATCH = 500
# A sweep stops after this many failed sends in a row (the mail server is down).
STOP_AFTER_FAILURES = 3
FAILED_OUTCOMES = frozenset({"retrying", "failed"})


class NotAccepted(Exception):
    """The mail backend returned without sending the message."""


@dataclass(frozen=True)
class Prepared:
    """A notice worded for sending: one address, one subject, one plain-text body."""

    to: str
    subject: str
    body: str
    event: str


def mail_enabled():
    return settings.EMAIL_BACKEND != DUMMY_BACKEND


def record(*, recipient, topic, object_id, reference, events, data=None):
    """Record a notice in the current transaction (the change that raises it): waiting, or Not
    sent when email is off."""
    if not connection.in_atomic_block:
        raise RuntimeError("Record a notice in the transaction of the change that raises it.")
    if not events:
        raise ValueError("A notice needs at least one event.")
    now = timezone.now()
    off = not mail_enabled()
    return Notice.objects.create(
        recipient=recipient,
        topic=topic,
        object_id=object_id,
        reference=reference,
        events=sorted(set(events)),
        data=data or {},
        next_attempt_at=now,
        status=NoticeStatus.NOT_SENT if off else NoticeStatus.WAITING,
        not_sent_reason=NotSentReason.MAIL_OFF if off else "",
        last_attempt_at=now if off else None,
    )


def send_after_commit(notice_ids):
    """Give these notices their first attempt once the current transaction commits. The change
    has committed by then: nothing that goes wrong while sending reaches the person who made it,
    and a notice left waiting is the scheduled job's to retry."""
    ids = list(notice_ids)
    if ids:
        transaction.on_commit(lambda: send_now(ids))


def send_now(notice_ids):
    """A first attempt for each waiting notice, stopping at the first one the mail server does
    not take (the rest wait for the scheduled job). Returns the outcomes counted. Never raises."""
    counts = Counter()
    for pk in notice_ids:
        try:
            outcome = attempt(pk)
        except Exception as exc:
            counts["error"] += 1
            logger.warning(
                "Notice left for the scheduled job: notice=%s error=%s", pk, type(exc).__name__
            )
            break
        counts[outcome] += 1
        if outcome in FAILED_OUTCOMES:
            break
    return dict(counts)


def deliver_due(now=None):
    """The `send_notices` job: an attempt for each waiting notice whose time has come."""
    now = now or timezone.now()
    due = (
        Notice.objects.filter(status=NoticeStatus.WAITING, next_attempt_at__lte=now)
        .order_by("next_attempt_at", "pk")
        .values_list("pk", flat=True)[:SEND_BATCH]
    )
    counts = Counter({"sent": 0, "retrying": 0, "failed": 0, "not_sent": 0})
    failures_in_a_row = 0
    for pk in list(due):
        outcome = attempt(pk, now=now)
        counts[outcome] += 1
        failures_in_a_row = failures_in_a_row + 1 if outcome in FAILED_OUTCOMES else 0
        if failures_in_a_row >= STOP_AFTER_FAILURES:
            counts["left_for_next_run"] = Notice.objects.filter(
                status=NoticeStatus.WAITING, next_attempt_at__lte=now
            ).count()
            break
    counts.pop("skipped", None)
    return dict(counts)


def _clock(now):
    """The time of this moment of an attempt: never earlier than the `now` it was given (a
    fixed time in tests), and later when sending took time."""
    current = timezone.now()
    return current if current > now else now


def attempt(pk, now=None):
    """Try to send notice `pk` once. Returns "sent", "retrying", "failed", "not_sent", "busy"
    (a row the decision rests on is being changed; tried again a minute later, uncounted) or
    "skipped" (locked by another sender, no longer waiting, or not due yet)."""
    now = now or timezone.now()
    seen = None  # (attempts, last attempt) as this attempt found them
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(f"SET LOCAL lock_timeout = '{LOCK_WAIT}'")
            notice = (
                Notice.objects.select_for_update(skip_locked=True)
                .filter(pk=pk, status=NoticeStatus.WAITING, next_attempt_at__lte=now)
                .first()
            )
            if notice is None:
                return "skipped"
            seen = (notice.attempts, notice.last_attempt_at)
            if not mail_enabled():
                return _not_sent(notice, NotSentReason.MAIL_OFF, now)
            # Takes the shared locks and decides from that reading; they hold until commit.
            prepared = notice_handler(notice.topic)(notice)
            if prepared is None:
                return _not_sent(notice, NotSentReason.NO_LONGER_APPLIES, now)
            try:
                sent = EmailMessage(prepared.subject, prepared.body, to=[prepared.to]).send()
                if not sent:
                    raise NotAccepted
            except Exception as exc:  # a delivery problem: counted and retried
                return _failed_attempt(notice, exc, _clock(now))
            notice.status = NoticeStatus.SENT
            notice.attempts += 1
            notice.last_attempt_at = notice.sent_at = _clock(now)
            notice.last_error = ""
            notice.save(update_fields=_ATTEMPT_FIELDS + ["sent_at"])
            return "sent"
    except Exception as exc:  # the handler or the database failed: the attempt rolled back
        if seen is None:
            raise
        with transaction.atomic():
            # Only the notice as this attempt found it: another sender may have attempted it
            # since this one let go of it, and its later outcome stands.
            attempts, last = seen
            found = Notice.objects.select_for_update(skip_locked=True).filter(
                pk=pk, status=NoticeStatus.WAITING, attempts=attempts
            )
            if last is None:
                found = found.filter(last_attempt_at__isnull=True)
            else:
                found = found.filter(last_attempt_at=last)
            notice = found.first()
            if notice is None:
                return "skipped"
            if is_lock_unavailable(exc):
                notice.next_attempt_at = _clock(now) + BUSY_DELAY
                notice.save(update_fields=["next_attempt_at"])
                return "busy"
            return _failed_attempt(notice, exc, _clock(now))


_ATTEMPT_FIELDS = ["status", "attempts", "last_attempt_at", "last_error", "next_attempt_at"]


def _not_sent(notice, reason, now):
    notice.status = NoticeStatus.NOT_SENT
    notice.not_sent_reason = reason
    notice.last_attempt_at = now
    notice.save(update_fields=["status", "not_sent_reason", "last_attempt_at"])
    return "not_sent"


def _failed_attempt(notice, exc, failed_at):
    notice.attempts += 1
    notice.last_attempt_at = failed_at
    notice.last_error = type(exc).__name__[:80]
    final = notice.attempts >= MAX_ATTEMPTS
    if final:
        notice.status = NoticeStatus.FAILED
    else:
        notice.next_attempt_at = failed_at + RETRY_DELAYS[notice.attempts - 1]
    notice.save(update_fields=_ATTEMPT_FIELDS)
    logger.warning(
        "Notice not sent: notice=%s request=%s recipient=%s events=%s attempt=%s error=%s %s",
        notice.pk,
        notice.reference,
        notice.recipient_id,
        ",".join(notice.events),
        notice.attempts,
        notice.last_error,
        "final" if final else f"retry_at={notice.next_attempt_at.isoformat()}",
    )
    return "failed" if final else "retrying"
