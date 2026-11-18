"""Run the scheduled jobs once (`manage.py run_scheduled_jobs`, docs/operations.md).

One run at a time: a run takes a PostgreSQL advisory lock for its whole length, and a run started
while another holds it does nothing. Each job then works in short transactions of its own and
locks each record it changes, skipping one that someone is changing (the next run takes it), so a
run never holds up the people working.

Jobs run in jobs.registry.JOBS order. A job that fails is logged with its error's class and
frames (never its message, which can quote stored values), recorded in its JobState, and the
remaining jobs still run.
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
    jobs = [JOBS_BY_NAME[name] for name in only] if only else list(JOBS)
    if not _try_lock():
        raise Busy
    try:
        return [_run_one(job, now) for job in jobs]
    finally:
        _unlock()


def _state(name):
    state, _ = JobState.objects.select_for_update().get_or_create(name=name)
    return state


def _run_one(job, now):
    started = timezone.now()
    with transaction.atomic():
        state = _state(job.name)
        state.last_started_at = started
        state.last_outcome = JobOutcome.RUNNING
        state.save(update_fields=["last_started_at", "last_outcome"])
    try:
        counts = job.function(now=now or timezone.now()) or {}
    except Exception as exc:
        error = type(exc).__name__[:80]
        logger.error(
            "Scheduled job failed: job=%s error=%s\n%s",
            job.name,
            error,
            "".join(traceback.format_tb(exc.__traceback__)),
        )
        result = Result(job.name, JobOutcome.FAILED, error=error)
    else:
        result = Result(
            job.name, JobOutcome.SUCCEEDED, counts={k: int(v) for k, v in counts.items()}
        )
    finished = timezone.now()
    with transaction.atomic():
        state = _state(job.name)
        state.last_finished_at = finished
        state.last_outcome = result.outcome
        state.last_error = result.error
        state.last_counts = result.counts
        if result.outcome == JobOutcome.SUCCEEDED:
            state.last_succeeded_at = finished
            state.consecutive_failures = 0
        else:
            state.consecutive_failures += 1
        state.save()
    if result.outcome == JobOutcome.SUCCEEDED:
        logger.info("Scheduled job done: job=%s %s", job.name, counts_text(result.counts))
    return result


def counts_text(counts):
    return " ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "nothing to do"
