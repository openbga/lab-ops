"""Neighbours of the findings of the independent review, round 1 (outbox-round-1.md): the cases
next to each reported one, for the fixes. The reviewer's own cases are in
test_review_outbox_round1.py. All data is synthetic."""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, connection, connections, transaction
from django.forms import modelform_factory
from django.urls import reverse
from django.utils import timezone

from accounts.admin import AccountChangeForm
from accounts.models import User
from clients.models import PortalAccess
from desk import jobs as close_job
from desk import notifications, services
from desk.models import ComplaintDecision, Status
from desk.tests.test_auto_close import resolve, set_period
from desk.tests.test_contract import new_request
from jobs import outbox, runner
from jobs.actor import system_account
from jobs.models import JobSettings, JobState, Notice, NoticeStatus, NotSentReason
from jobs.services import update_settings
from jobs.views import recipient_label
from service_levels import jobs as clock_job
from service_levels.models import RequestClock, ServiceTarget
from service_levels.tests import conftest as clock_fixtures
from teams.models import TeamMembership, ensure_default_team

from .conftest import DUMMY, LOCMEM

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("locmem")]
frozen = clock_fixtures.frozen


# Helpers ----------------------------------------------------------------------------------------


def _worker(work):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '450ms'")
        return work()
    finally:
        connections.close_all()


def independent(work):
    """Run one operation on a separate PostgreSQL session with a bounded lock wait."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_worker, work).result(timeout=15)


def can_lock(row):
    """Take and release a writer-compatible probe on `row` from another session."""

    def probe():
        try:
            with transaction.atomic():
                type(row).objects.select_for_update(no_key=True).get(pk=row.pk)
        except OperationalError as exc:
            if exc.__cause__.sqlstate != "55P03":
                raise
            return False
        return True

    return independent(probe)


@contextmanager
def held_by_another_session(row):
    """Another session holds `row` FOR UPDATE until the block ends."""
    locked, release = threading.Event(), threading.Event()

    def hold():
        try:
            with transaction.atomic():
                type(row).objects.select_for_update().get(pk=row.pk)
                locked.set()
                release.wait(timeout=30)
        finally:
            connections.close_all()

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert locked.wait(timeout=20)
        yield
    finally:
        release.set()
        thread.join(timeout=20)


def change_status(user, ticket, status):
    ticket.refresh_from_db()
    return services.change_status(
        actor=user,
        request=ticket,
        expected_revision=ticket.revision,
        status=status,
        summary="Synthetic resolution",
        reason="Synthetic status change",
    )


def reply(user, ticket, **kwargs):
    ticket.refresh_from_db()
    return services.add_entry(
        actor=user,
        request=ticket,
        kind="reply",
        body="Synthetic reply",
        expected_revision=ticket.revision,
        **kwargs,
    )


def refusing_mail(monkeypatch, error=ConnectionRefusedError):
    def broken(self, *args, **kwargs):
        raise error

    monkeypatch.setattr("django.core.mail.EmailMessage.send", broken)


@pytest.fixture
def portal_ticket(clinic_portal, category):
    return new_request(clinic_portal, category, channel="portal", first_entry_kind="reply")


@pytest.fixture
def team_ticket(agent, category, accessioning, join, frozen):
    """A ticket assigned to `agent` (in the team), past a one-hour resolution target."""
    ServiceTarget.objects.create(name="Review target", resolution_minutes=60)
    join(agent, accessioning)
    ticket = new_request(agent, category, team=accessioning, assignee=agent)
    frozen(frozen.now + timedelta(hours=2))
    return ticket


# R1-01: what is held while the mail backend takes the message -----------------------------------


@pytest.fixture
def at_handoff(monkeypatch):
    """Record, for each message handed to the backend, what `probes()` finds lockable. Returns
    (observations, set_probes)."""
    observations, probes = [], []

    def transport(message):
        observations.append([can_lock(row) for row in probes[0]()])
        return 1

    monkeypatch.setattr(mail.EmailMessage, "send", transport)

    def watch(make_rows):
        probes.append(make_rows)
        return observations

    return watch


@pytest.mark.django_db(transaction=True)
def test_a_breach_notice_holds_the_recipients_membership_and_the_tickets_category(
    agent, team_ticket, accessioning, at_handoff
):
    membership = TeamMembership.objects.get(team=accessioning, user=agent)
    observed = at_handoff(lambda: [membership, team_ticket.category])
    assert clock_job.notice_breaches()["notices"] == 1
    assert observed == [[False, False]]
    assert Notice.objects.get(topic=notifications.BREACH_TOPIC).status == NoticeStatus.SENT


@pytest.mark.django_db(transaction=True)
def test_a_portal_notice_holds_the_recipients_portal_grant(
    agent, portal_ticket, clinic_portal, at_handoff
):
    grant = PortalAccess.objects.create(
        user=clinic_portal, client_id=clinic_portal.client_id, whole_organization=True
    )
    observed = at_handoff(lambda: [grant])
    reply(agent, portal_ticket)
    assert observed == [[False]]


@pytest.mark.django_db(transaction=True)
def test_a_staff_assignment_notice_holds_the_ticket_and_the_assignee(
    agent, make_user, join, portal_ticket, at_handoff
):
    assignee = make_user("review_assignee", "technical")
    join(assignee, portal_ticket.team)
    observed = at_handoff(lambda: [portal_ticket, assignee])
    services.assign(
        actor=agent,
        request=portal_ticket,
        team=portal_ticket.team,
        assignee=assignee,
        expected_revision=portal_ticket.revision,
    )
    assert observed == [[False, False]]


@pytest.fixture
def waiting_notice_for_client(monkeypatch, agent, portal_ticket):
    """A notice recorded and committed whose first attempt did not run."""
    monkeypatch.setattr(outbox, "send_now", lambda ids: {})
    reply(agent, portal_ticket)
    return Notice.objects.get()


@pytest.mark.django_db(transaction=True)
def test_a_ticket_being_changed_gives_busy_then_the_next_attempt_sends(
    waiting_notice_for_client, portal_ticket
):
    notice = waiting_notice_for_client
    started = timezone.now()
    with held_by_another_session(portal_ticket):
        assert outbox.attempt(notice.pk) == "busy"
    notice.refresh_from_db()
    assert notice.status == NoticeStatus.WAITING
    assert notice.attempts == 0 and notice.last_error == "" and notice.last_attempt_at is None
    assert started + timedelta(seconds=60) <= notice.next_attempt_at
    assert notice.next_attempt_at <= started + timedelta(seconds=60 + 30)
    assert mail.outbox == []
    assert outbox.attempt(notice.pk) == "skipped"  # not due until the minute has passed
    assert outbox.attempt(notice.pk, now=notice.next_attempt_at) == "sent"
    notice.refresh_from_db()
    assert (notice.status, notice.attempts) == (NoticeStatus.SENT, 1)
    assert len(mail.outbox) == 1


def test_opting_out_before_an_attempt_means_the_notice_is_not_sent(
    client, agent, make_user, join, portal_ticket
):
    assignee = make_user("review_assignee", "technical")
    join(assignee, portal_ticket.team)
    services.assign(
        actor=agent,
        request=portal_ticket,
        team=portal_ticket.team,
        assignee=assignee,
        expected_revision=portal_ticket.revision,
    )
    notice = Notice.objects.get(recipient=assignee)
    client.force_login(assignee)
    response = client.post(reverse("email-notices"), {})  # an unticked box posts nothing
    assert response.status_code == 302
    assert outbox.attempt(notice.pk) == "not_sent"
    notice.refresh_from_db()
    assert notice.not_sent_reason == NotSentReason.NO_LONGER_APPLIES
    assert mail.outbox == []


# R1-02: the administrator's own standing at the save --------------------------------------------


@pytest.fixture
def admin(make_user):
    return make_user("job_admin", "admin")


def test_an_admin_whose_admin_group_was_removed_before_the_save_is_refused(admin):
    User.groups.through.objects.filter(user=admin).delete()  # committed before the save
    with pytest.raises(PermissionDenied):
        update_settings(actor=admin, expected_revision=1, close_resolved_after_days=7)
    assert not JobSettings.objects.filter(close_resolved_after_days__isnull=False).exists()


def test_a_second_role_does_not_matter_to_an_admin(admin):
    admin.groups.add(Group.objects.get_or_create(name="supervisor")[0])
    assert update_settings(actor=admin, expected_revision=1, close_resolved_after_days=7)


@pytest.mark.django_db(transaction=True)
def test_granting_another_role_during_the_save_is_not_blocked(monkeypatch, admin):
    extra = Group.objects.get_or_create(name="supervisor")[0]
    original = JobSettings.save
    outcomes = []

    def save(row, *args, **kwargs):
        if row.close_resolved_after_days is not None:
            outcomes.append(independent(lambda: admin.groups.add(extra) or "granted"))
        return original(row, *args, **kwargs)

    monkeypatch.setattr(JobSettings, "save", save)
    assert update_settings(actor=admin, expected_revision=1, close_resolved_after_days=7)
    assert outcomes == ["granted"]
    assert admin.groups.filter(name="supervisor").exists()


# R1-03 and R1-04: the jobs' account stays a non-person ------------------------------------------


def account_form(user, **changes):
    form_class = modelform_factory(
        User,
        form=AccountChangeForm,
        fields=("username", "email", "first_name", "is_active", "is_staff", "is_superuser"),
    )
    data = {
        "username": user.username,
        "email": user.email,
        "first_name": user.first_name,
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        **changes,
    }
    return form_class(data, instance=user)


@pytest.mark.parametrize("flag", ["is_superuser", "is_staff", "is_active"])
def test_the_admin_form_refuses_a_flag_on_the_jobs_account(flag):
    form = account_form(system_account(), **{flag: True})
    assert not form.is_valid()


def test_the_admin_form_refuses_a_permission_on_the_jobs_account():
    from django.contrib.auth.models import Permission

    form_class = modelform_factory(
        User, form=AccountChangeForm, fields=("username", "email", "user_permissions")
    )
    user = system_account()
    permission = Permission.objects.first()
    form = form_class(
        {"username": user.username, "email": user.email, "user_permissions": [permission.pk]},
        instance=user,
    )
    assert not form.is_valid()


def test_the_admin_form_refuses_even_a_name_change_to_the_jobs_account():
    # Its name labels every change the jobs made (verification V08): it is not edited at all.
    user = system_account()
    form = account_form(user, first_name="Renamed")
    assert not form.is_valid()
    user.refresh_from_db()
    assert user.first_name == "Scheduled" and user.is_active is False


@pytest.mark.parametrize("through", ["assign", "create"])
def test_the_jobs_account_cannot_be_given_work_even_with_a_role_and_a_membership(
    agent, accessioning, join, category, through
):
    robot = system_account()
    User.objects.filter(pk=robot.pk).update(is_active=True)
    robot.refresh_from_db()
    robot.groups.add(Group.objects.get_or_create(name="technical")[0])
    join(robot, accessioning)
    join(agent, accessioning)
    with pytest.raises(ValidationError):
        if through == "assign":
            ticket = new_request(agent, category, team=accessioning)
            services.assign(
                actor=agent,
                request=ticket,
                team=accessioning,
                assignee=robot,
                expected_revision=ticket.revision,
            )
        else:
            new_request(agent, category, team=accessioning, assignee=robot)


def test_the_assignment_choices_leave_the_jobs_account_out(accessioning, join):
    from desk.forms import assign_choices

    robot = system_account()
    User.objects.filter(pk=robot.pk).update(is_active=True)
    join(robot, accessioning)
    offered = {
        value
        for _label, group in assign_choices()
        if isinstance(group, (list, tuple))
        for value, _ in group
    }
    assert not any(value.endswith(f":{robot.pk}") for value in offered if value)


@pytest.fixture
def last_owner_setup():
    owner = User.objects.create_superuser("review_owner", "owner@example.invalid", "test-only")
    robot = system_account()
    User.objects.filter(pk=robot.pk).update(is_active=True, is_staff=True, is_superuser=True)
    return owner


def test_the_last_human_superuser_cannot_be_removed_when_only_the_jobs_account_remains(
    last_owner_setup,
):
    form = account_form(last_owner_setup, is_active=False, is_superuser=True)
    assert not form.is_valid()
    assert "Keep at least one active superuser with staff status" in str(form.errors)


def test_a_human_superuser_can_be_removed_when_another_human_remains(last_owner_setup):
    User.objects.create_superuser("review_second", "second@example.invalid", "test-only")
    form = account_form(last_owner_setup, is_active=False, is_superuser=True)
    assert form.is_valid(), form.errors


# R1-05: who a notice was for --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("username", "first", "last", "expected"),
    [
        ("plain.person", "Pat", "Example", "Pat Example"),
        ("plain.person", "", "", "plain.person"),
        ("person@example.invalid", "", "", None),
        ("person@example.invalid", "Pat", "Example", "Pat Example"),
    ],
    ids=["full-name", "username", "address-shaped", "address-shaped-with-name"],
)
def test_recipient_label_never_shows_an_address(username, first, last, expected):
    user = User.objects.create_user(
        username, "contact@example.invalid", first_name=first, last_name=last
    )
    label = recipient_label(user)
    assert label == (expected or f"Account {user.pk}")
    assert "@" not in label


# R1-06 and R1-07: email off at record, and a backend that takes nothing -------------------------


@pytest.fixture
def mail_off(settings):
    settings.EMAIL_BACKEND = DUMMY


def test_email_off_at_record_time_is_not_sent_with_no_attempt(mail_off, agent, portal_ticket):
    reply(agent, portal_ticket)
    notice = Notice.objects.get()
    assert (notice.status, notice.not_sent_reason) == (
        NoticeStatus.NOT_SENT,
        NotSentReason.MAIL_OFF,
    )
    assert notice.attempts == 0 and notice.sent_at is None


def test_events_merged_into_it_later_keep_it_not_sent(mail_off, agent, portal_ticket):
    reply(agent, portal_ticket, new_status=Status.WAITING_CLIENT)
    notice = Notice.objects.get()
    assert notice.events == ["reply", "waiting_client"]
    assert (notice.status, notice.not_sent_reason) == (
        NoticeStatus.NOT_SENT,
        NotSentReason.MAIL_OFF,
    )


def test_turning_email_on_later_sends_nothing_held_back(mail_off, settings, agent, portal_ticket):
    reply(agent, portal_ticket)
    settings.EMAIL_BACKEND = LOCMEM
    result = outbox.deliver_due(now=timezone.now() + timedelta(days=30))
    assert result == {"sent": 0, "retrying": 0, "failed": 0, "not_sent": 0}
    assert Notice.objects.get().status == NoticeStatus.NOT_SENT
    assert mail.outbox == []


def test_a_backend_that_keeps_taking_nothing_ends_failed(monkeypatch, agent, portal_ticket):
    reply(agent, portal_ticket)
    notice = Notice.objects.get()
    monkeypatch.setattr(
        "django.core.mail.backends.locmem.EmailBackend.send_messages", lambda self, messages: 0
    )
    now = timezone.now()
    for number in range(1, outbox.MAX_ATTEMPTS + 1):
        outcome = outbox.attempt(notice.pk, now=now)
        notice.refresh_from_db()
        assert notice.attempts == number and notice.last_error == "NotAccepted"
        assert outcome == ("failed" if number == outbox.MAX_ATTEMPTS else "retrying")
        now = notice.next_attempt_at
    assert notice.status == NoticeStatus.FAILED and notice.sent_at is None
    assert mail.outbox == []


# R1-08: status events apply only while the status holds -----------------------------------------


def test_a_waiting_notice_still_sends_while_the_ticket_waits_on_the_client(
    agent, portal_ticket, clinic_portal
):
    change_status(agent, portal_ticket, Status.WAITING_CLIENT)
    notice = Notice.objects.get(recipient=clinic_portal)
    assert outbox.attempt(notice.pk) == "sent"
    assert notifications.PHRASES["waiting_client"] in mail.outbox[0].body


def test_a_resolved_notice_is_dropped_when_staff_reopen_the_ticket(
    agent, portal_ticket, clinic_portal
):
    change_status(agent, portal_ticket, Status.RESOLVED)
    notice = Notice.objects.get(recipient=clinic_portal)
    change_status(agent, portal_ticket, Status.OPEN)
    assert outbox.attempt(notice.pk) == "not_sent"
    notice.refresh_from_db()
    assert notice.not_sent_reason == NotSentReason.NO_LONGER_APPLIES
    assert mail.outbox == []


def test_a_resolved_notice_still_sends_while_the_ticket_is_resolved(
    agent, portal_ticket, clinic_portal
):
    change_status(agent, portal_ticket, Status.RESOLVED)
    notice = Notice.objects.get(recipient=clinic_portal)
    assert outbox.attempt(notice.pk) == "sent"
    assert notifications.PHRASES["resolved"] in mail.outbox[0].body


# R1-09, R1-10 and R1-11: who is still responsible, and which run of the clock -------------------


def test_a_member_notice_still_applies_while_there_is_no_assignee_and_no_lead(
    agent, category, accessioning, join, frozen
):
    ServiceTarget.objects.create(name="Review target", resolution_minutes=60)
    join(agent, accessioning)
    new_request(agent, category, team=accessioning)
    frozen(frozen.now + timedelta(hours=2))
    assert clock_job.notice_breaches()["notices"] == 1
    notice = Notice.objects.get(topic=notifications.BREACH_TOPIC)
    assert notice.data["role"] == "member"
    assert notifications.prepare_breach_notice(notice) is not None


def test_a_lead_who_becomes_the_assignee_is_still_told(
    agent, make_user, category, accessioning, join, frozen
):
    ServiceTarget.objects.create(name="Review target", resolution_minutes=60)
    lead = make_user("review_lead", "technical")
    join(agent, accessioning)
    join(lead, accessioning, lead=True)
    ticket = new_request(agent, category, team=accessioning, assignee=agent)
    frozen(frozen.now + timedelta(hours=2))
    assert clock_job.notice_breaches()["notices"] == 2
    lead_notice = Notice.objects.get(topic=notifications.BREACH_TOPIC, recipient=lead)
    agent_notice = Notice.objects.get(topic=notifications.BREACH_TOPIC, recipient=agent)
    assert (lead_notice.data["role"], agent_notice.data["role"]) == ("lead", "assignee")
    services.assign(
        actor=agent,
        request=ticket,
        team=accessioning,
        assignee=lead,
        expected_revision=ticket.revision,
    )
    assert notifications.prepare_breach_notice(lead_notice) is not None
    assert notifications.prepare_breach_notice(agent_notice) is None  # no longer responsible


def test_a_pause_and_resume_keeps_the_mark_and_the_notice_applies_again(agent, team_ticket, frozen):
    assert clock_job.notice_breaches()["breaches"] == 1
    notice = Notice.objects.get(topic=notifications.BREACH_TOPIC)
    mark = RequestClock.objects.get(request=team_ticket).resolution_breach_noticed_at
    assert mark is not None
    change_status(agent, team_ticket, Status.WAITING_CLIENT)
    assert RequestClock.objects.get(request=team_ticket).paused_since is not None
    assert notifications.prepare_breach_notice(notice) is None  # paused: not running
    frozen(frozen.now + timedelta(minutes=10))
    change_status(agent, team_ticket, Status.OPEN)
    clock = RequestClock.objects.get(request=team_ticket)
    assert clock.paused_since is None and clock.resolution_breach_noticed_at == mark
    assert clock_job.notice_breaches()["breaches"] == 0  # not noticed a second time
    assert notifications.prepare_breach_notice(notice) is not None


def test_a_recalculated_first_response_time_clears_its_mark_and_a_later_crossing_is_noticed(
    agent, category, accessioning, join, frozen
):
    ServiceTarget.objects.create(name="First", first_response_minutes=60)
    ServiceTarget.objects.create(name="Longer", priority="high", first_response_minutes=240)
    join(agent, accessioning)
    ticket = new_request(agent, category, team=accessioning, assignee=agent, channel="email")
    frozen(frozen.now + timedelta(hours=2))
    assert clock_job.notice_breaches()["breaches"] == 1
    old = Notice.objects.get(topic=notifications.BREACH_TOPIC)
    ticket.refresh_from_db()
    services.update_properties(
        actor=agent,
        request=ticket,
        category=ticket.category,
        priority="high",
        expected_revision=ticket.revision,
    )
    clock = RequestClock.objects.get(request=ticket)
    assert clock.first_response_due_at > frozen.now
    assert clock.first_response_breach_noticed_at is None
    assert clock_job.notice_breaches()["breaches"] == 0
    assert notifications.prepare_breach_notice(old) is None  # an earlier run of the clock
    frozen(clock.first_response_due_at + timedelta(minutes=1))
    assert clock_job.notice_breaches() == {"breaches": 1, "notices": 1}
    new = Notice.objects.filter(topic=notifications.BREACH_TOPIC).exclude(pk=old.pk).get()
    assert notifications.prepare_breach_notice(new) is not None


# R1-12 and R1-13: the close job and the breach job meet busy or blocked tickets -----------------


def test_a_reviewed_complaint_whose_review_no_longer_counts_does_not_stop_the_others(
    agent, make_user, category, join, monkeypatch
):
    monkeypatch.setattr(close_job, "CLOSE_BATCH", 1)
    join(agent, ensure_default_team())
    lead = make_user("first_lead", "supervisor")
    join(lead, ensure_default_team())
    blocked = new_request(agent, category, assignee=agent)
    services.set_complaint(
        actor=agent, request=blocked, expected_revision=1, is_complaint=True, reason="Late twice"
    )
    blocked.refresh_from_db()
    services.decide_complaint(
        actor=agent,
        request=blocked,
        expected_revision=blocked.revision,
        decision=ComplaintDecision.INVESTIGATE,
        rationale="Repeated delay.",
    )
    blocked.refresh_from_db()
    services.review_complaint(
        actor=lead, request=blocked, expected_revision=blocked.revision, note="Checked."
    )
    resolve(agent, blocked, days_ago=20)
    # The reviewer is then given the ticket: the review no longer counts.
    services.assign(
        actor=agent,
        request=blocked,
        team=blocked.team,
        assignee=lead,
        expected_revision=blocked.revision,
    )
    eligible = resolve(agent, new_request(agent, category), days_ago=10)
    set_period(7)
    counts = close_job.close_resolved_tickets()
    assert counts == {"closed": 1, "kept_open": 1}
    blocked.refresh_from_db()
    eligible.refresh_from_db()
    assert (blocked.status, eligible.status) == (Status.RESOLVED, Status.CLOSED)


@pytest.mark.django_db(transaction=True)
def test_a_ticket_locked_by_another_session_is_busy_and_noticed_by_the_next_run(team_ticket):
    with held_by_another_session(team_ticket):
        assert clock_job.notice_breaches() == {"breaches": 0, "notices": 0, "busy": 1}
    assert not Notice.objects.filter(topic=notifications.BREACH_TOPIC).exists()
    assert clock_job.notice_breaches() == {"breaches": 1, "notices": 1}


# R1-14 and R1-19: the runner --------------------------------------------------------------------


@pytest.fixture
def fake_jobs(monkeypatch, settings):
    settings.EMAIL_BACKEND = DUMMY
    seen = []

    def fake(name):
        def function(now=None):
            seen.append(name)
            return {"things": 1}

        return function

    monkeypatch.setattr("desk.jobs.close_resolved_tickets", fake("close_resolved_tickets"))
    monkeypatch.setattr("service_levels.jobs.notice_breaches", fake("service_target_breaches"))
    monkeypatch.setattr("jobs.outbox.deliver_due", fake("send_notices"))
    return seen


def test_a_failure_writing_the_finish_state_fails_that_job_only(monkeypatch, fake_jobs):
    original = runner._record_state

    def record(name, update):
        if name == "close_resolved_tickets" and update.__name__ == "finishing":
            raise OperationalError("synthetic database failure")
        return original(name, update)

    monkeypatch.setattr(runner, "_record_state", record)
    first, *others = runner.run()
    assert (first.name, first.outcome, first.error) == (
        "close_resolved_tickets",
        "failed",
        "OperationalError",
    )
    assert [r.outcome for r in others] == ["succeeded", "succeeded"]
    assert fake_jobs == ["close_resolved_tickets", "service_target_breaches", "send_notices"]
    done = JobState.objects.filter(last_outcome="succeeded").values_list("name", flat=True)
    assert set(done) == {"service_target_breaches", "send_notices"}


def test_an_unknown_job_name_raises_before_anything_runs(fake_jobs):
    with pytest.raises(ValueError):
        runner.run(only=["nope"])
    with pytest.raises(ValueError):
        runner.run(only=["send_notices", "nope"])
    assert fake_jobs == [] and not JobState.objects.exists()
    assert len(runner.run(only=["send_notices"])) == 1  # and the lock was not left held


# R1-17: strictly after the due time -------------------------------------------------------------


def test_a_first_response_exactly_at_its_due_time_is_not_breached_and_a_second_later_is(
    agent, category, accessioning, join, frozen
):
    ServiceTarget.objects.create(name="First", first_response_minutes=60)
    join(agent, accessioning)
    ticket = new_request(agent, category, team=accessioning, assignee=agent, channel="email")
    due = RequestClock.objects.get(request=ticket).first_response_due_at
    frozen(due)
    assert clock_job.notice_breaches() == {"breaches": 0, "notices": 0}
    frozen(due + timedelta(seconds=1))
    assert clock_job.notice_breaches() == {"breaches": 1, "notices": 1}


# R1-18: the days the settings service takes -----------------------------------------------------


@pytest.mark.parametrize("value", ["7", " 7 ", 7])
def test_days_as_a_number_or_its_digits_are_accepted(admin, value):
    update_settings(actor=admin, expected_revision=1, close_resolved_after_days=value)
    assert JobSettings.objects.get(pk=1).close_resolved_after_days == 7


@pytest.mark.parametrize("value", [7.0, "7.0", "-1", Decimal("7"), "7 days"])
def test_fractions_signs_and_other_shapes_are_refused(admin, value):
    with pytest.raises(ValidationError):
        update_settings(actor=admin, expected_revision=1, close_resolved_after_days=value)
    assert JobSettings.objects.filter(pk=1, close_resolved_after_days__isnull=False).count() == 0


def test_an_empty_string_clears_the_period(admin):
    update_settings(actor=admin, expected_revision=1, close_resolved_after_days=7)
    assert update_settings(actor=admin, expected_revision=2, close_resolved_after_days="")
    assert JobSettings.objects.get(pk=1).close_resolved_after_days is None


# R1-20: a sweep stops when the mail server is down ----------------------------------------------


@pytest.fixture
def six_waiting(agent, portal_ticket):
    for _ in range(6):
        reply(agent, portal_ticket)
    assert Notice.objects.filter(status=NoticeStatus.WAITING).count() == 6


def scripted_send(monkeypatch, script):
    """A send that follows `script` (True: accepted, False: refused), one step per call."""
    real_send = mail.EmailMessage.send
    steps = iter(script)

    def send(self, *args, **kwargs):
        if not next(steps):
            raise ConnectionRefusedError
        return real_send(self, *args, **kwargs)

    monkeypatch.setattr("django.core.mail.EmailMessage.send", send)


def test_a_sweep_stops_after_consecutive_failures_and_counts_the_rest(monkeypatch, six_waiting):
    refusing_mail(monkeypatch)
    result = outbox.deliver_due()
    assert result["retrying"] == outbox.STOP_AFTER_FAILURES == 3
    assert result["left_for_next_run"] == 3
    assert Notice.objects.filter(attempts=1).count() == 3
    assert Notice.objects.filter(attempts=0, status=NoticeStatus.WAITING).count() == 3


def test_a_success_between_failures_resets_the_run(monkeypatch, six_waiting):
    scripted_send(monkeypatch, [False, False, True, False, False, True])
    result = outbox.deliver_due()
    assert result == {"sent": 2, "retrying": 4, "failed": 0, "not_sent": 0}
    assert "left_for_next_run" not in result
    assert not Notice.objects.filter(attempts=0).exists()


def test_the_first_attempt_after_a_change_stops_at_the_first_refused_send(monkeypatch, six_waiting):
    ids = list(Notice.objects.order_by("pk").values_list("pk", flat=True))
    refusing_mail(monkeypatch)
    assert outbox.send_now(ids) == {"retrying": 1}
    assert Notice.objects.filter(attempts=1).count() == 1
    assert Notice.objects.filter(attempts=0).count() == 5


def test_only_ascii_digits_count_as_days(make_user):
    from jobs.services import update_settings as save

    admin = make_user("ascii_days_admin", "admin")
    with pytest.raises(ValidationError):
        save(actor=admin, expected_revision=1, close_resolved_after_days="٧")
