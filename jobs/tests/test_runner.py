"""The job runner and `manage.py run_scheduled_jobs`: order, recorded state, failures, the
advisory lock and `--job`. Synthetic data."""

import io
import logging

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.utils import timezone

from jobs import runner
from jobs.models import JobOutcome, JobState
from jobs.registry import JOBS

pytestmark = pytest.mark.django_db

NAMES = [job.name for job in JOBS]
# Kept out of the raise statement: the log carries source lines of frames, never the message.
MESSAGE = "secret " + "value for someone@example.invalid"


@pytest.fixture(autouse=True)
def mail_off(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"


@pytest.fixture
def calls(monkeypatch):
    """Replace each job's function with one that records its call and returns counts."""
    seen = []

    def fake(name):
        def function(now=None):
            seen.append((name, now))
            return {"things": 2}

        return function

    monkeypatch.setattr("desk.jobs.close_resolved_tickets", fake("close_resolved_tickets"))
    monkeypatch.setattr("service_levels.jobs.notice_breaches", fake("service_target_breaches"))
    monkeypatch.setattr("jobs.outbox.deliver_due", fake("send_notices"))
    return seen


def test_jobs_are_registered_in_run_order():
    assert NAMES == ["close_resolved_tickets", "service_target_breaches", "send_notices"]


def test_run_runs_every_job_in_order_and_records_its_state(calls):
    before = timezone.now()
    results = runner.run()
    assert [name for name, _ in calls] == NAMES
    assert all(now is not None for _, now in calls)
    assert [r.name for r in results] == NAMES
    assert all(r.outcome == JobOutcome.SUCCEEDED and r.counts == {"things": 2} for r in results)
    states = {s.name: s for s in JobState.objects.all()}
    assert set(states) == set(NAMES)
    for state in states.values():
        assert before <= state.last_started_at <= state.last_finished_at
        assert state.last_succeeded_at == state.last_finished_at
        assert state.last_outcome == JobOutcome.SUCCEEDED
        assert state.last_counts == {"things": 2}
        assert state.last_error == ""
        assert state.consecutive_failures == 0


def test_a_fixed_time_is_what_each_job_sees(calls):
    moment = timezone.now()
    runner.run(now=moment)
    assert {now for _, now in calls} == {moment}


def test_the_real_jobs_run_and_reset_the_failure_count():
    JobState.objects.create(
        name="send_notices",
        last_outcome=JobOutcome.FAILED,
        last_error="OperationalError",
        consecutive_failures=3,
    )
    results = runner.run()
    assert [r.outcome for r in results] == [JobOutcome.SUCCEEDED] * 3
    state = JobState.objects.get(name="send_notices")
    assert state.consecutive_failures == 0
    assert state.last_error == ""
    assert state.last_outcome == JobOutcome.SUCCEEDED
    assert state.last_succeeded_at is not None
    assert set(state.last_counts) == {"sent", "retrying", "failed", "not_sent"}


@pytest.fixture
def broken(monkeypatch):
    def fail(now=None):
        raise ValueError(MESSAGE)

    monkeypatch.setattr("desk.jobs.close_resolved_tickets", fail)


def test_a_failing_job_is_recorded_and_the_others_still_run(calls, broken):
    first, *others = runner.run()
    assert (first.name, first.outcome, first.error) == (
        "close_resolved_tickets",
        JobOutcome.FAILED,
        "ValueError",
    )
    assert [r.outcome for r in others] == [JobOutcome.SUCCEEDED] * 2
    assert [name for name, _ in calls] == NAMES[1:]
    state = JobState.objects.get(name="close_resolved_tickets")
    assert state.last_outcome == JobOutcome.FAILED
    assert state.last_error == "ValueError"
    assert state.last_succeeded_at is None
    assert state.consecutive_failures == 1
    runner.run()
    state.refresh_from_db()
    assert state.consecutive_failures == 2
    assert JobState.objects.get(name="send_notices").consecutive_failures == 0


def test_a_success_after_failures_resets_the_count(calls, broken, monkeypatch):
    runner.run()
    runner.run()
    monkeypatch.setattr("desk.jobs.close_resolved_tickets", lambda now=None: {"closed": 0})
    runner.run()
    state = JobState.objects.get(name="close_resolved_tickets")
    assert state.consecutive_failures == 0
    assert state.last_outcome == JobOutcome.SUCCEEDED
    assert state.last_error == ""


def test_the_log_names_the_class_but_not_the_message(calls, broken, caplog):
    with caplog.at_level(logging.INFO, logger="lab_ops.jobs"):
        runner.run()
    assert "job=close_resolved_tickets" in caplog.text and "ValueError" in caplog.text
    assert "secret value" not in caplog.text
    assert "example.invalid" not in caplog.text


def test_the_command_fails_naming_the_failed_job(calls, broken, caplog):
    out = io.StringIO()
    with caplog.at_level(logging.INFO, logger="lab_ops.jobs"):
        with pytest.raises(CommandError) as error:
            call_command("run_scheduled_jobs", stdout=out)
    assert "close_resolved_tickets" in str(error.value)
    assert "service_target_breaches" not in str(error.value)
    assert "secret value" not in str(error.value) + out.getvalue() + caplog.text
    assert "close_resolved_tickets: failed (ValueError)" in out.getvalue()
    assert [name for name, _ in calls] == NAMES[1:]  # the rest still ran


def test_the_command_reports_each_job(calls):
    out = io.StringIO()
    call_command("run_scheduled_jobs", stdout=out)
    for name in NAMES:
        assert f"{name}: things=2" in out.getvalue()


def test_job_option_runs_only_the_named_job(calls):
    call_command("run_scheduled_jobs", "--job", "send_notices", stdout=io.StringIO())
    assert [name for name, _ in calls] == ["send_notices"]
    assert list(JobState.objects.values_list("name", flat=True)) == ["send_notices"]


def test_job_option_may_repeat_and_refuses_unknown_names(calls):
    call_command(
        "run_scheduled_jobs",
        "--job",
        "send_notices",
        "--job",
        "close_resolved_tickets",
        stdout=io.StringIO(),
    )
    assert sorted(name for name, _ in calls) == ["close_resolved_tickets", "send_notices"]
    with pytest.raises(CommandError):
        call_command("run_scheduled_jobs", "--job", "nonsense", stdout=io.StringIO())


@pytest.fixture
def other_session_holds_the_lock(transactional_db):
    connection = connections.create_connection("default")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [runner.LOCK_KEY])
        assert cursor.fetchone()[0] is True

    def release():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [runner.LOCK_KEY])

    yield release
    connection.close()


def test_a_run_started_while_another_holds_the_lock_does_nothing(
    other_session_holds_the_lock, calls
):
    with pytest.raises(runner.Busy):
        runner.run()
    assert calls == []
    assert not JobState.objects.exists()


def test_the_command_says_nothing_was_done_and_exits_normally(other_session_holds_the_lock, calls):
    out = io.StringIO()
    call_command("run_scheduled_jobs", stdout=out)  # no CommandError
    assert "nothing was done" in out.getvalue()
    assert calls == []
    assert not JobState.objects.exists()


def test_the_lock_is_released_after_a_run_and_after_a_failure(transactional_db, calls, broken):
    runner.run()
    runner.run()  # would raise Busy if the first run kept the lock
    assert [name for name, _ in calls] == NAMES[1:] * 2


def test_a_run_goes_ahead_once_the_other_session_lets_go(other_session_holds_the_lock, calls):
    with pytest.raises(runner.Busy):
        runner.run()
    other_session_holds_the_lock()
    assert len(runner.run()) == 3
