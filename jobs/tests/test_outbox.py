"""The notification outbox: recording with the change, the first attempt after commit, applying
rules again at sending, retries, failures and what a notice row may hold. Synthetic data."""

import json
import logging
import threading
from datetime import timedelta

import pytest
from django.core import mail
from django.core.mail import EmailMessage
from django.db import connection, transaction
from django.test import override_settings
from django.utils import timezone

from accounts.models import User
from clients.models import PortalAccess
from desk import notifications
from desk.models import EntryKind, ServiceRequest, Status
from desk.services import add_entry, assign, create_request, withdraw_reply
from jobs import outbox
from jobs.models import Notice, NoticeStatus, NotSentReason
from teams.models import Team

from .conftest import DUMMY

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("locmem")]


@pytest.fixture
def capture(django_capture_on_commit_callbacks):
    return django_capture_on_commit_callbacks


@pytest.fixture
def run(capture):
    """Run `fn` in a transaction, then its on-commit callbacks, as a committed change would."""

    def go(fn):
        with capture(execute=True):
            with transaction.atomic():
                return fn()

    return go


@pytest.fixture
def record_only(capture):
    """Run `fn` in a transaction and leave its on-commit callbacks unrun; returns (result,
    callbacks)."""

    def go(fn):
        with capture(execute=False) as callbacks:
            with transaction.atomic():
                result = fn()
        return result, callbacks

    return go


@pytest.fixture
def portal_account(make_user, clinic):
    user = make_user("clinic_raiser", client=clinic)
    PortalAccess.objects.create(user=user, client_id=user.client_id, whole_organization=True)
    return user


@pytest.fixture
def portal_request(portal_account, agent, category, clinic):
    """A request the portal account raised, so staff replies tell it."""
    request = create_request(
        actor=agent,
        category=category,
        subject="Logged",
        first_entry_kind=EntryKind.NOTE,
        first_entry_body="Internal start.",
        channel="phone",
        client=clinic,
    )
    ServiceRequest.objects.filter(pk=request.pk).update(created_by=portal_account, channel="portal")
    request.refresh_from_db()
    mail.outbox.clear()
    return request


def reply(actor, request, body="Answer", **extra):
    request.refresh_from_db()
    return add_entry(
        actor=actor,
        request=request,
        kind=extra.pop("kind", EntryKind.REPLY),
        body=body,
        expected_revision=request.revision,
        **extra,
    )


def only_notice():
    return Notice.objects.get()


# Recording and the first attempt ----------------------------------------------------------------


def test_a_change_records_a_waiting_notice_then_sends_it_after_commit(
    record_only, agent, portal_account, portal_request
):
    _, callbacks = record_only(lambda: reply(agent, portal_request))
    notice = only_notice()
    assert notice.recipient_id == portal_account.pk
    assert notice.status == NoticeStatus.WAITING and notice.attempts == 0
    assert notice.reference == portal_request.number
    assert notice.events == ["reply"] and notice.topic == "desk.request"
    assert mail.outbox == []
    for callback in callbacks:
        callback()
    notice.refresh_from_db()
    assert notice.status == NoticeStatus.SENT
    assert notice.attempts == 1 and notice.sent_at is not None
    assert notice.last_attempt_at == notice.sent_at
    assert notice.last_error == "" and notice.not_sent_reason == ""
    assert [m.to for m in mail.outbox] == [[portal_account.email]]


def test_a_rolled_back_change_leaves_no_notice(capture, agent, portal_request):
    class Abort(Exception):
        pass

    with capture(execute=True), pytest.raises(Abort), transaction.atomic():
        reply(agent, portal_request)
        assert Notice.objects.count() == 1  # recorded in the change's own transaction
        raise Abort
    assert Notice.objects.count() == 0
    assert mail.outbox == []


def test_two_hooks_of_one_change_give_one_notice_and_one_email(run, agent, portal_request):
    run(lambda: reply(agent, portal_request, new_status=Status.WAITING_CLIENT))
    notice = only_notice()
    assert notice.events == ["reply", "waiting_client"]
    assert notice.status == NoticeStatus.SENT
    assert len(mail.outbox) == 1
    assert notifications.PHRASES["waiting_client"] in mail.outbox[0].body
    assert notifications.PHRASES["reply"] not in mail.outbox[0].body


def test_a_notice_needs_an_event(portal_account):
    with pytest.raises(ValueError):
        outbox.record(
            recipient=portal_account, topic="desk.request", object_id=1, reference="R", events=[]
        )


# Email off ---------------------------------------------------------------------------------------


@override_settings(EMAIL_BACKEND=DUMMY)
def test_email_off_marks_the_notice_not_sent_and_it_is_never_retried(
    settings, run, agent, portal_request
):
    run(lambda: reply(agent, portal_request))
    notice = only_notice()
    assert notice.status == NoticeStatus.NOT_SENT
    assert notice.not_sent_reason == NotSentReason.MAIL_OFF
    assert notice.sent_at is None and notice.attempts == 0
    assert outbox.deliver_due(now=timezone.now() + timedelta(days=30)) == {
        "sent": 0,
        "retrying": 0,
        "failed": 0,
        "not_sent": 0,
    }
    # Turning email on later does not send what was held back.
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    outbox.deliver_due(now=timezone.now() + timedelta(days=30))
    notice.refresh_from_db()
    assert notice.status == NoticeStatus.NOT_SENT
    assert mail.outbox == []


# No longer applies at sending --------------------------------------------------------------------


def _deactivate_recipient(ctx):
    User.objects.filter(pk=ctx["portal"].pk).update(is_active=False)


def _withdraw_reply(ctx):
    lead = ctx["make_user"]("withdrawing_lead", "supervisor")
    ctx["request"].refresh_from_db()
    withdraw_reply(
        actor=lead,
        entry=ctx["entry"],
        reason="Sent too early.",
        expected_revision=ctx["request"].revision,
    )


def _recipient_left_email(ctx):
    User.objects.filter(pk=ctx["portal"].pk).update(email="")


@pytest.mark.parametrize(
    "change",
    [_deactivate_recipient, _withdraw_reply, _recipient_left_email],
    ids=["deactivated", "reply-withdrawn", "address-removed"],
)
def test_a_client_notice_that_no_longer_applies_is_not_sent(
    change, record_only, make_user, agent, portal_account, portal_request
):
    entry, _ = record_only(lambda: reply(agent, portal_request))
    notice = only_notice()
    change(
        {
            "portal": portal_account,
            "make_user": make_user,
            "request": portal_request,
            "entry": entry,
        }
    )
    assert outbox.send_now([notice.pk]) == {"not_sent": 1}
    notice.refresh_from_db()
    assert notice.status == NoticeStatus.NOT_SENT
    assert notice.not_sent_reason == NotSentReason.NO_LONGER_APPLIES
    assert notice.attempts == 0 and notice.sent_at is None
    assert mail.outbox == []


def test_a_notice_for_a_contact_account_whose_portal_access_was_removed_is_not_sent(
    record_only, make_user, agent, category, clinic, clinic_contact
):
    linked = make_user("linked", client=clinic)
    access = PortalAccess.objects.create(
        user=linked, client_id=linked.client_id, whole_organization=True, contact=clinic_contact
    )
    request = create_request(
        actor=agent,
        category=category,
        subject="Logged",
        first_entry_kind=EntryKind.NOTE,
        first_entry_body="Internal start.",
        channel="phone",
        client=clinic,
        contact=clinic_contact,
    )
    record_only(lambda: reply(agent, request))
    notice = only_notice()
    assert notice.recipient_id == linked.pk
    access.delete()
    outbox.send_now([notice.pk])
    notice.refresh_from_db()
    assert (notice.status, notice.not_sent_reason) == (
        NoticeStatus.NOT_SENT,
        NotSentReason.NO_LONGER_APPLIES,
    )
    assert mail.outbox == []


def test_an_assignment_notice_for_someone_who_is_no_longer_the_assignee_is_not_sent(
    record_only, make_user, join, agent, category, clinic
):
    colleague = make_user("colleague", "technical")
    other = make_user("other_colleague", "technical")
    team = Team.objects.create(slug="t", name="Team T")
    for person in (agent, colleague, other):
        join(person, team)
    request, _ = record_only(
        lambda: create_request(
            actor=agent,
            category=category,
            subject="Logged",
            first_entry_kind=EntryKind.NOTE,
            first_entry_body="Internal start.",
            channel="phone",
            client=clinic,
            team=team,
            assignee=colleague,
        )
    )
    notice = only_notice()
    assert notice.recipient_id == colleague.pk and notice.events == ["assigned"]
    assign(
        actor=agent, request=request, expected_revision=request.revision, team=team, assignee=other
    )
    outbox.send_now([notice.pk])
    notice.refresh_from_db()
    assert (notice.status, notice.not_sent_reason) == (
        NoticeStatus.NOT_SENT,
        NotSentReason.NO_LONGER_APPLIES,
    )
    assert mail.outbox == []


# Failures and retries ------------------------------------------------------------


@pytest.fixture
def waiting_notice(record_only, agent, portal_request):
    record_only(lambda: reply(agent, portal_request))
    return only_notice()


def fail_sending(monkeypatch, message="mail server down for someone@example.invalid"):
    def broken(self, *args, **kwargs):
        raise ConnectionRefusedError(message)

    monkeypatch.setattr("django.core.mail.EmailMessage.send", broken)


def test_a_failed_send_waits_five_minutes_and_keeps_only_the_error_class(
    monkeypatch, caplog, waiting_notice, portal_request
):
    fail_sending(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="lab_ops.notifications"):
        assert outbox.send_now([waiting_notice.pk]) == {"retrying": 1}
    waiting_notice.refresh_from_db()
    assert waiting_notice.status == NoticeStatus.WAITING
    assert waiting_notice.attempts == 1
    assert waiting_notice.last_error == "ConnectionRefusedError"
    assert waiting_notice.next_attempt_at == waiting_notice.last_attempt_at + timedelta(minutes=5)
    assert waiting_notice.sent_at is None
    assert "Notice not sent" in caplog.text
    assert f"notice={waiting_notice.pk}" in caplog.text
    assert portal_request.number in caplog.text
    assert "example.invalid" not in caplog.text and "@" not in caplog.text
    assert "mail server down" not in caplog.text


def test_deliver_due_waits_for_the_next_time_then_retries(monkeypatch, waiting_notice):
    fail_sending(monkeypatch)
    outbox.send_now([waiting_notice.pk])
    waiting_notice.refresh_from_db()
    due = waiting_notice.next_attempt_at
    monkeypatch.undo()
    assert outbox.deliver_due(now=due - timedelta(seconds=1)) == {
        "sent": 0,
        "retrying": 0,
        "failed": 0,
        "not_sent": 0,
    }
    waiting_notice.refresh_from_db()
    assert waiting_notice.attempts == 1 and mail.outbox == []
    assert outbox.deliver_due(now=due)["sent"] == 1
    waiting_notice.refresh_from_db()
    assert waiting_notice.status == NoticeStatus.SENT
    assert waiting_notice.attempts == 2
    assert waiting_notice.last_attempt_at == due and waiting_notice.sent_at == due
    assert waiting_notice.last_error == ""
    assert len(mail.outbox) == 1


def test_retry_delays_follow_the_schedule_and_the_last_failure_is_final(
    monkeypatch, waiting_notice
):
    assert outbox.MAX_ATTEMPTS == len(outbox.RETRY_DELAYS) + 1
    fail_sending(monkeypatch)
    outbox.send_now([waiting_notice.pk])
    for number in range(1, outbox.MAX_ATTEMPTS):
        waiting_notice.refresh_from_db()
        assert waiting_notice.status == NoticeStatus.WAITING
        assert waiting_notice.attempts == number
        assert (
            waiting_notice.next_attempt_at - waiting_notice.last_attempt_at
            == (outbox.RETRY_DELAYS[number - 1])
        )
        result = outbox.deliver_due(now=waiting_notice.next_attempt_at)
        assert result["retrying" if number + 1 < outbox.MAX_ATTEMPTS else "failed"] == 1
    waiting_notice.refresh_from_db()
    assert waiting_notice.status == NoticeStatus.FAILED
    assert waiting_notice.attempts == outbox.MAX_ATTEMPTS
    assert waiting_notice.last_error == "ConnectionRefusedError"
    # A failed notice is left alone.
    far = timezone.now() + timedelta(days=365)
    assert outbox.deliver_due(now=far)["failed"] == 0
    waiting_notice.refresh_from_db()
    assert waiting_notice.attempts == outbox.MAX_ATTEMPTS


def test_a_success_after_some_failures_is_sent(monkeypatch, waiting_notice):
    real_send = EmailMessage.send
    failures = []

    def flaky(self, *args, **kwargs):
        if len(failures) < 2:
            failures.append(1)
            raise TimeoutError("slow server")
        return real_send(self, *args, **kwargs)

    monkeypatch.setattr("django.core.mail.EmailMessage.send", flaky)
    outbox.send_now([waiting_notice.pk])
    for _ in range(2):
        waiting_notice.refresh_from_db()
        outbox.deliver_due(now=waiting_notice.next_attempt_at)
    waiting_notice.refresh_from_db()
    assert waiting_notice.status == NoticeStatus.SENT
    assert waiting_notice.attempts == 3
    assert waiting_notice.last_error == ""
    assert waiting_notice.sent_at is not None
    assert len(mail.outbox) == 1


def test_a_notice_is_attempted_at_most_once_per_call_when_not_waiting(waiting_notice):
    assert outbox.attempt(waiting_notice.pk) == "sent"
    assert outbox.attempt(waiting_notice.pk) == "skipped"
    assert len(mail.outbox) == 1


def test_a_notice_not_yet_due_is_skipped(waiting_notice):
    Notice.objects.filter(pk=waiting_notice.pk).update(
        next_attempt_at=timezone.now() + timedelta(hours=1)
    )
    assert outbox.attempt(waiting_notice.pk) == "skipped"
    assert mail.outbox == []


def test_a_handler_that_raises_counts_a_failed_attempt_and_does_not_raise(
    monkeypatch, caplog, waiting_notice
):
    message = "handler " + "broke for someone@example.invalid"

    def handler(topic):
        def prepare(notice):
            raise RuntimeError(message)

        return prepare

    monkeypatch.setattr("jobs.outbox.notice_handler", handler)
    with caplog.at_level(logging.WARNING, logger="lab_ops.notifications"):
        result = outbox.send_now([waiting_notice.pk])  # does not raise
    assert result == {"retrying": 1}
    waiting_notice.refresh_from_db()
    assert waiting_notice.status == NoticeStatus.WAITING
    assert waiting_notice.attempts == 1
    assert waiting_notice.last_error == "RuntimeError"
    assert waiting_notice.next_attempt_at == waiting_notice.last_attempt_at + timedelta(minutes=5)
    assert "handler broke" not in caplog.text and "example.invalid" not in caplog.text
    assert mail.outbox == []


# Locking -----------------------------------------------------------------------------------------


@pytest.fixture
def committed_notice(transactional_db, make_user):
    user = make_user("recipient", "technical")
    with transaction.atomic():
        return outbox.record(
            recipient=user,
            topic="desk.request",
            object_id=1,
            reference="REQ-000001",
            events=["assigned"],
        )


def test_a_notice_locked_by_another_transaction_is_skipped(committed_notice):
    locked, release, done = threading.Event(), threading.Event(), threading.Event()

    def holder():
        try:
            with transaction.atomic():
                Notice.objects.select_for_update().get(pk=committed_notice.pk)
                locked.set()
                release.wait(timeout=20)
        finally:
            connection.close()
            done.set()

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert locked.wait(timeout=20)
        assert outbox.attempt(committed_notice.pk) == "skipped"
        assert mail.outbox == []
    finally:
        release.set()
        thread.join(timeout=20)
    assert done.is_set()
    Notice.objects.get(pk=committed_notice.pk, status=NoticeStatus.WAITING, attempts=0)


# What a notice row holds ------------------------------------------------------------


def test_notice_rows_never_hold_ticket_text(
    monkeypatch, run, make_user, join, agent, category, clinic, portal_account
):
    sentinel = "SENTINELTICKETTEXTX"
    colleague = make_user("colleague", "technical")
    team = Team.objects.create(slug="t", name="Team T")
    join(colleague, team)
    request = run(
        lambda: create_request(
            actor=agent,
            category=category,
            subject=f"{sentinel} subject",
            first_entry_kind=EntryKind.NOTE,
            first_entry_body=f"{sentinel} body",
            channel="phone",
            client=clinic,
            team=team,
            assignee=colleague,
            reported_by=f"{sentinel} reporter",
            client_reference=f"{sentinel} ref",
        )
    )
    ServiceRequest.objects.filter(pk=request.pk).update(created_by=portal_account, channel="portal")
    run(lambda: reply(agent, request, body=f"{sentinel} reply", new_status=Status.WAITING_CLIENT))
    run(lambda: reply(portal_account, request, body=f"{sentinel} client reply"))
    fail_sending(monkeypatch, message=f"{sentinel} in a server message")
    run(lambda: reply(agent, request, body=f"{sentinel} second reply"))
    notices = list(Notice.objects.all())
    assert {n.status for n in notices} >= {NoticeStatus.SENT, NoticeStatus.WAITING}
    assert len(notices) >= 3
    for notice in notices:
        for name in ("events", "data", "reference", "last_error", "topic", "not_sent_reason"):
            assert sentinel not in json.dumps(getattr(notice, name)), (notice.pk, name)
