"""Run the scheduled jobs once (`manage.py run_scheduled_jobs`, docs/operations.md).

One run at a time: a run takes a PostgreSQL advisory lock for its whole length, and a run started
while another holds it does nothing. Each job then works in short transactions of its own and
locks each record it changes, skipping one that someone is changing (the next run takes it), so a
run never holds up the people working.

Jobs run in jobs.registry.JOBS order, each once. A job that fails, or whose state cannot be
recorded, is logged with its error's class and frames (never its message, which can quote stored
values), recorded in its JobState where possible, and the remaining jobs still run.
"""

import logging
import traceback
from dataclasses import dataclass, field

from django.db import connection, transaction
from django.utils import timezone

from .models import JobOutcome, JobState
from .registry import JOBS, JOBS_BY_NAME

logger = logging.getLogger("lab_ops.jobs")

# pg_try_advisory_lock key held by a run: "LabOpsJb" in ASCII.
LOCK_KEY = 0x4C61624F70734A62


@dataclass
class Result:
    name: str
    outcome: str
    counts: dict = field(default_factory=dict)
    error: str = ""


class Busy(Exception):
    """Another run holds the lock."""


def _try_lock():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_KEY])
        return cursor.fetchone()[0]


def _unlock():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_KEY])


def run(*, only=None, now=None):
    """Run every job, or those named in `only`, and return their Results. Raises Busy when
    another run is in progress. `now` fixes the time each job sees (tests)."""
    if only:
        unknown = set(only) - set(JOBS_BY_NAME)
        if unknown:
            raise ValueError(f"Unknown scheduled jobs: {', '.join(sorted(unknown))}.")
    # Registry order, each job once, whatever order or repetition `only` names them in.
    jobs = [job for job in JOBS if not only or job.name in set(only)]
    if not _try_lock():
        raise Busy
    try:
        return [_run_one(job, now) for job in jobs]
    finally:
        _unlock()


# How long recording a job's state waits for its row before the job counts as failed.
STATE_LOCK_WAIT = "5s"


def _record_state(name, update):
    """Apply `update(state)` to the job's JobState row in a short transaction of its own."""
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL lock_timeout = '{STATE_LOCK_WAIT}'")
        state, _ = JobState.objects.select_for_update().get_or_create(name=name)
        update(state)
        state.save()


def _log_failure(message, job, exc):
    logger.error(
        "%s: job=%s error=%s\n%s",
        message,
        job.name,
        type(exc).__name__,
        "".join(traceback.format_tb(exc.__traceback__)),
    )


def _run_one(job, now):
    """Run one job and record its state. Anything that goes wrong (the job, or recording its
    state) fails this job only: it is logged and returned, and the next job still runs."""
    started = timezone.now()

    def starting(state):
        state.last_started_at = started
        state.last_outcome = JobOutcome.RUNNING

    try:
        _record_state(job.name, starting)
    except Exception as exc:
        _log_failure("Scheduled job not run: its state could not be recorded", job, exc)
        return Result(job.name, JobOutcome.FAILED, error=type(exc).__name__[:80])
    try:
        counts = job.function(now=now or timezone.now()) or {}
    except Exception as exc:
        _log_failure("Scheduled job failed", job, exc)
        result = Result(job.name, JobOutcome.FAILED, error=type(exc).__name__[:80])
    else:
        result = Result(
            job.name, JobOutcome.SUCCEEDED, counts={k: int(v) for k, v in counts.items()}
        )
    finished = timezone.now()

    def finishing(state):
        state.last_finished_at = finished
        state.last_outcome = result.outcome
        state.last_error = result.error
        state.last_counts = result.counts
        if result.outcome == JobOutcome.SUCCEEDED:
            state.last_succeeded_at = finished
            state.consecutive_failures = 0
        else:
            state.consecutive_failures += 1

    try:
        _record_state(job.name, finishing)
    except Exception as exc:
        _log_failure("Scheduled job ran but its state could not be recorded", job, exc)
        return Result(
            job.name, JobOutcome.FAILED, counts=result.counts, error=type(exc).__name__[:80]
        )
    if result.outcome == JobOutcome.SUCCEEDED:
        logger.info("Scheduled job done: job=%s %s", job.name, counts_text(result.counts))
    return result


def counts_text(counts):
    return " ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "nothing to do"
