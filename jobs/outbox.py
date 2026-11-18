"""The notification outbox: email notices recorded with the change that raised them, sent after it
commits, and retried by the scheduled job until they are sent, fail for good or no longer apply.

Recording: a hook calls `record()` inside the change's transaction, after the change's own
writes, and `send_after_commit()` with the notices it recorded. Nothing is sent before the change
commits, and a change that rolls back leaves no notice.

Sending: right after the change commits, each of its notices gets a first attempt, so a
deployment without a scheduler still sends promptly. The scheduled job `send_notices`
(`deliver_due`) retries each waiting notice whose time has come.

Each attempt runs in its own transaction with the notice locked; a second sender skips a notice
that is locked. In order:

1. Email off (Django's dummy backend): Not sent, reason "email is off". It is never retried, so
   turning email on later does not send old notices.
2. The topic's handler (jobs.registry.NOTICE_TOPICS) checks, from the record and the recipient as
   they are at this moment, that the notice still applies, and words it from its allow-list.
   No longer applies: Not sent, reason "no longer applies".
3. Send. Accepted by the mail backend: Sent. Refused or unreachable: the attempt is counted and
   the next one waits RETRY_DELAYS; after MAX_ATTEMPTS attempts, Failed.

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

from .models import Notice, NoticeStatus, NotSentReason
from .registry import notice_handler

logger = logging.getLogger("lab_ops.notifications")

DUMMY_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
# Wait after the first, second ... seventh failed attempt; the eighth failure is final.
RETRY_DELAYS = tuple(timedelta(minutes=m) for m in (5, 15, 30, 60, 120, 240, 480))
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
# Most notices one run of the job sends; the rest wait for the next run.
SEND_BATCH = 500


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
    """Record a waiting notice in the current transaction (the change that raises it)."""
    if not connection.in_atomic_block:
        raise RuntimeError("Record a notice in the transaction of the change that raises it.")
    if not events:
        raise ValueError("A notice needs at least one event.")
    return Notice.objects.create(
        recipient=recipient,
        topic=topic,
        object_id=object_id,
        reference=reference,
        events=sorted(set(events)),
        data=data or {},
        next_attempt_at=timezone.now(),
    )


def send_after_commit(notice_ids):
    """Give these notices their first attempt once the current transaction commits. The change
    has committed by then: nothing that goes wrong while sending reaches the person who made it,
    and a notice left waiting is the scheduled job's to retry."""
    ids = list(notice_ids)
    if ids:
        transaction.on_commit(lambda: send_now(ids))


def send_now(notice_ids):
    """One attempt for each notice that is waiting; returns the outcomes counted. Never raises."""
    counts = Counter()
    for pk in notice_ids:
        try:
            counts[attempt(pk)] += 1
        except Exception as exc:
            counts["error"] += 1
            logger.warning(
                "Notice left for the scheduled job: notice=%s error=%s", pk, type(exc).__name__
            )
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
    for pk in list(due):
        counts[attempt(pk, now=now)] += 1
    counts.pop("skipped", None)
    return dict(counts)


def attempt(pk, now=None):
    """Try to send notice `pk` once. Returns "sent", "retrying", "failed", "not_sent" or
    "skipped" (locked by another sender, no longer waiting, or not due yet)."""
    now = now or timezone.now()
    try:
        with transaction.atomic():
            notice = (
                Notice.objects.select_for_update(skip_locked=True)
                .filter(pk=pk, status=NoticeStatus.WAITING, next_attempt_at__lte=now)
                .first()
            )
            if notice is None:
                return "skipped"
            if not mail_enabled():
                return _not_sent(notice, NotSentReason.MAIL_OFF, now)
            prepared = notice_handler(notice.topic)(notice)
            if prepared is None:
                return _not_sent(notice, NotSentReason.NO_LONGER_APPLIES, now)
            try:
                EmailMessage(prepared.subject, prepared.body, to=[prepared.to]).send()
            except Exception as exc:  # a delivery problem: counted and retried
                return _failed_attempt(notice, exc, now)
            notice.status = NoticeStatus.SENT
            notice.attempts += 1
            notice.last_attempt_at = notice.sent_at = now
            notice.last_error = ""
            notice.save(update_fields=_ATTEMPT_FIELDS + ["sent_at"])
            return "sent"
    except Exception as exc:  # the handler or the database failed: the attempt rolled back
        with transaction.atomic():
            notice = (
                Notice.objects.select_for_update(skip_locked=True)
                .filter(pk=pk, status=NoticeStatus.WAITING)
                .first()
            )
            if notice is None:
                return "skipped"
            return _failed_attempt(notice, exc, now)


_ATTEMPT_FIELDS = ["status", "attempts", "last_attempt_at", "last_error", "next_attempt_at"]


def _not_sent(notice, reason, now):
    notice.status = NoticeStatus.NOT_SENT
    notice.not_sent_reason = reason
    notice.last_attempt_at = now
    notice.save(update_fields=["status", "not_sent_reason", "last_attempt_at"])
    return "not_sent"


def _failed_attempt(notice, exc, now):
    notice.attempts += 1
    notice.last_attempt_at = now
    notice.last_error = type(exc).__name__[:80]
    final = notice.attempts >= MAX_ATTEMPTS
    if final:
        notice.status = NoticeStatus.FAILED
    else:
        notice.next_attempt_at = now + RETRY_DELAYS[notice.attempts - 1]
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
