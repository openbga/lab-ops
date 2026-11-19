"""The notification outbox: email notices recorded with the change that raised them, sent after it
commits, and retried by the scheduled job until they are sent, fail for good or no longer apply.

Recording: a hook calls `record()` inside the change's transaction, after the change's own
writes. Nothing is sent before the change commits, and a change that rolls back leaves no
notice. With email off (Django's dummy backend) when the change is saved, the notice is recorded
as Not sent, reason "email is off", and never sent later, even if email is turned on before
anything tried to send it.

Sending: right after the change commits, its notices get a first attempt, so a deployment
without a scheduler still sends promptly. The first attempts of one transaction share one wait for
the mail server (EMAIL_TIMEOUT): they stop at the first notice the server does not take, or once
that wait has been spent, so a slow or unreachable server delays the person's save by about one
wait, not one per recipient or per ticket of a bulk action; the rest wait for the scheduled job.
A notice recorded in a savepoint that rolls back is gone, and so is its first attempt. The job
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


class _Budget:
    """One wait for the mail server (EMAIL_TIMEOUT), shared by the first attempts of every notice
    one transaction recorded: the first attempt always starts, and no further one starts after an
    attempt the server did not take or once the wait has been spent."""

    def __init__(self):
        self.started = None
        self.failed = False

    def allows_another(self):
        now = timezone.now()
        if self.started is None:
            self.started = now
            return True
        wait = timedelta(seconds=settings.EMAIL_TIMEOUT or 0)
        return not self.failed and now - self.started < wait


class _Dispatch:
    """Notices recorded at one savepoint level of a transaction, given their first attempt once
    it commits. It is registered with transaction.on_commit at that level, so a savepoint that
    rolls back drops it together with its notices, and one that is released keeps it for the
    commit. Every dispatch of one transaction (see _scope) shares one _Budget."""

    def __init__(self, scope, budget):
        self.scope = scope
        self.budget = budget
        self.ids = []
        self.done = False

    def __call__(self):
        self.done = True
        if self.ids:
            send_now(self.ids, budget=self.budget)


def _scope(connection_):
    """The transaction a change runs in, as the outbox counts it: its outermost atomic block,
    leaving out blocks a test case opened (Django's TestCase runs each test inside a transaction
    that never commits, so each change made inside one counts as a transaction of its own).
    "transaction" when that block began the transaction, else the id of its savepoint."""
    for index, block in enumerate(connection_.atomic_blocks):
        if not getattr(block, "_from_testcase", False):
            if index == 0:
                return "transaction"
            return connection_.savepoint_ids[index - 1] or f"block-{index}-{id(block)}"
    return None


def dispatch():
    """The dispatch of the current savepoint level of the open transaction, registered the first
    time it is asked for. It shares the budget of the transaction's other dispatches."""
    connection_ = transaction.get_connection()
    if not connection_.in_atomic_block:
        raise RuntimeError("Dispatch notices from the transaction that records them.")
    scope = _scope(connection_)
    level = set(connection_.savepoint_ids)
    budget = None
    for savepoints, callback, *_rest in connection_.run_on_commit:
        if isinstance(callback, _Dispatch) and callback.scope == scope and not callback.done:
            if savepoints == level:
                return callback
            budget = callback.budget
    created = _Dispatch(scope, budget or _Budget())
    transaction.on_commit(created)
    return created


def is_pending(dispatched):
    """Whether `dispatched` still runs when the transaction now open commits: it is registered in
    this transaction (as the outbox counts it), has not run, and no rollback has dropped it."""
    connection_ = transaction.get_connection()
    if dispatched is None or dispatched.done or not connection_.in_atomic_block:
        return False
    if dispatched.scope != _scope(connection_):
        return False
    return any(callback is dispatched for _sp, callback, *_rest in connection_.run_on_commit)


def send_after_commit(notice_ids):
    """Give these notices their first attempt once the current transaction commits, sharing one
    wait for the mail server with every other notice it recorded; at once when no transaction is
    open (they are committed already, for example when called from a commit callback). The
    change has committed by then: nothing that goes wrong while sending reaches the person who
    made it, and a notice left waiting is the scheduled job's."""
    ids = list(notice_ids)
    if not ids:
        return
    if not transaction.get_connection().in_atomic_block:
        send_now(ids)
        return
    dispatch().ids.extend(ids)


def send_now(notice_ids, budget=None):
    """A first attempt for each waiting notice, stopping at the first one the mail server does
    not take, and once one wait for the mail server (EMAIL_TIMEOUT) has been spent: the rest wait
    for the scheduled job. `budget` is the wait when it is shared with other notices of the same
    transaction. Returns the outcomes counted. Never raises."""
    counts = Counter()
    try:
        notice_ids = list(notice_ids)
    except Exception as exc:
        logger.warning("Notices left for the scheduled job: error=%s", type(exc).__name__)
        return {"error": 1}
    budget = budget or _Budget()
    for index, pk in enumerate(notice_ids):
        if not budget.allows_another():
            if not budget.failed:
                counts["left_for_next_run"] += len(notice_ids) - index
            break
        try:
            outcome = attempt(pk)
        except Exception as exc:
            counts["error"] += 1
            budget.failed = True
            logger.warning(
                "Notice left for the scheduled job: notice=%s error=%s", pk, type(exc).__name__
            )
            break
        counts[outcome] += 1
        if outcome in FAILED_OUTCOMES:
            budget.failed = True
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
        # Only a send the mail server took ends a streak of failures; a notice that no longer
        # applies, or a busy one, never reached it.
        if outcome in FAILED_OUTCOMES:
            failures_in_a_row += 1
        elif outcome == "sent":
            failures_in_a_row = 0
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
    seen = None  # (attempts, last attempt, next attempt) as this attempt found them
    try:
        with transaction.atomic():
            _reserve_outbox()
            notice = (
                Notice.objects.select_for_update(skip_locked=True)
                .filter(pk=pk, status=NoticeStatus.WAITING, next_attempt_at__lte=now)
                .first()
            )
            if notice is None:
                return "skipped"
            seen = (notice.attempts, notice.last_attempt_at, notice.next_attempt_at)
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
            _reserve_outbox()
            # Only the notice as this attempt found it: another sender may have attempted or
            # deferred it since this one let go of it, and its later outcome stands.
            attempts, last, next_at = seen
            found = Notice.objects.select_for_update(skip_locked=True).filter(
                pk=pk, status=NoticeStatus.WAITING, attempts=attempts, next_attempt_at=next_at
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


def _reserve_outbox():
    """Bound this transaction's lock waits (LOCK_WAIT) and take the write lock it needs on the
    outbox to record an outcome first: a whole-database maintenance lock
    (core.demo.lock_application_tables) then waits for the attempt instead of the two waiting on
    each other."""
    with connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL lock_timeout = '{LOCK_WAIT}'")
        cursor.execute(
            f"LOCK TABLE {connection.ops.quote_name(Notice._meta.db_table)} IN ROW EXCLUSIVE MODE"
        )


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
