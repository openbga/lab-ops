"""Neighbours of the findings of the round 1 verification (outbox-round-1-verification.md): the
cases next to each reported one, for the fixes in aee80e3. The reviewer's own cases are in
test_review_outbox_round1_verify.py. All data is synthetic."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from django.contrib.auth.models import Group
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import OperationalError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from clients.models import PortalAccess
from desk import notifications, services
from desk.forms import assign_choices
from desk.models import Status
from desk.tests.test_contract import new_request
from jobs import outbox, runner
from jobs.actor import system_account
from jobs.models import JobSettings, Notice, NoticeStatus
from jobs.services import update_settings
from jobs.views import recipient_label
from service_levels import jobs as clock_job
from service_levels.models import RequestClock, ServiceTarget
from teams import services as teams

from . import test_review_round1_fixes as cases

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("locmem")]

frozen = cases.frozen
portal_ticket = cases.portal_ticket
team_ticket = cases.team_ticket
waiting_notice_for_client = cases.waiting_notice_for_client
admin = cases.admin
fake_jobs = cases.fake_jobs
six_waiting = cases.six_waiting


def writer_waits(write):
    """Run the normal writer on another session up to its commit, then discard its changes.
    True when it had to wait for a lock held by the notice being sent."""

    def probe():
        try:
            with transaction.atomic():
                write()
                connection.check_constraints()
                transaction.set_rollback(True)
        except OperationalError as exc:
            if exc.__cause__.sqlstate != "55P03":
                raise
            return True
        return False

    return cases.independent(probe)


@pytest.fixture
def sender_in_transport(monkeypatch):
    """Call `watch(*writers)`; each message handed to the backend then records whether each
    writer had to wait. Returns the list of observations."""
    observations, writers = [], []

    def transport(message):
        observations.append([writer_waits(write) for write in writers])
        return 1

    monkeypatch.setattr(mail.EmailMessage, "send", transport)

    def watch(*functions):
        writers.extend(functions)
        return observations

    return watch


def human_manager(make_user):
    return make_user("team_manager", "supervisor")


# V01: the team's own rows are held while a notice is prepared -----------------------------------


@pytest.mark.django_db(transaction=True)
def test_changing_leads_or_members_of_the_team_waits_while_a_breach_notice_is_prepared(
    agent, make_user, category, accessioning, join, frozen, sender_in_transport
):
    ServiceTarget.objects.create(name="Review target", resolution_minutes=60)
    manager = human_manager(make_user)
    lead = make_user("review_lead", "technical")
    newcomer = make_user("review_newcomer", "technical")
    join(agent, accessioning)
    lead_membership = join(lead, accessioning, lead=True)
    agent_membership = TeamMembershipOf(agent, accessioning)
    new_request(agent, category, team=accessioning, assignee=agent)
    frozen(frozen.now + timedelta(hours=2))
    system_account()
    observed = sender_in_transport(
        lambda: teams.set_lead(actor=manager, membership=lead_membership, is_lead=False),
        lambda: teams.remove_member(actor=manager, membership=lead_membership),
        lambda: teams.remove_member(actor=manager, membership=agent_membership),
        lambda: teams.add_member(actor=manager, team=accessioning, user=newcomer),
    )
    assert clock_job.notice_breaches()["notices"] == 2
    assert observed == [[True, True, True, True]] * 2
    assert {n.status for n in Notice.objects.filter(topic=notifications.BREACH_TOPIC)} == {
        NoticeStatus.SENT
    }


def TeamMembershipOf(user, team):
    from teams.models import TeamMembership

    return TeamMembership.objects.get(user=user, team=team)


@pytest.mark.django_db(transaction=True)
def test_a_client_notice_leaves_the_tickets_team_and_its_memberships_free(
    agent, make_user, join, portal_ticket, sender_in_transport
):
    # A client's notice does not rest on the team (final check F03): team changes go on.
    manager = human_manager(make_user)
    newcomer = make_user("review_newcomer", "technical")
    colleague = make_user("review_colleague", "technical")
    membership = join(colleague, portal_ticket.team, lead=False)
    observed = sender_in_transport(
        lambda: teams.add_member(actor=manager, team=portal_ticket.team, user=newcomer),
        lambda: teams.set_lead(actor=manager, membership=membership, is_lead=True),
    )
    cases.reply(agent, portal_ticket)
    assert observed == [[False, False]]


# V02: the email-notices page compares with what is stored ---------------------------------------


def test_opting_back_in_from_a_session_whose_copy_says_opted_out_persists(agent):
    first, second = Client(), Client()
    for client in (first, second):
        client.force_login(agent)
    url = reverse("email-notices")
    assert first.post(url, {}).status_code == 302  # opted out: first session's copy says so
    assert second.post(url, {"enabled": "on"}).status_code == 302
    assert second.post(url, {}).status_code == 302  # opted out again, elsewhere
    assert not notifications.wants_notices(agent)
    assert first.post(url, {"enabled": "on"}).status_code == 302
    assert notifications.wants_notices(agent)


def test_a_page_opened_after_another_sessions_change_shows_the_stored_value(agent):
    first, second = Client(), Client()
    for client in (first, second):
        client.force_login(agent)
    url = reverse("email-notices")
    assert first.get(url).context["form"]["enabled"].value() is True
    second.post(url, {})
    assert not notifications.wants_notices(agent)
    assert first.get(url).context["form"]["enabled"].value() is False
    second.post(url, {"enabled": "on"})
    assert first.get(url).context["form"]["enabled"].value() is True


# V03, V12 and V13: busy attempts, streaks and the time budget -----------------------------------


def scripted_attempt(monkeypatch, outcomes):
    calls = []
    steps = iter(outcomes)

    def attempt(pk, now=None):
        calls.append(pk)
        return next(steps)

    monkeypatch.setattr(outbox, "attempt", attempt)
    return calls


def test_a_busy_attempt_neither_ends_nor_extends_a_streak_of_failures(monkeypatch, six_waiting):
    calls = scripted_attempt(
        monkeypatch, ["retrying", "retrying", "busy", "retrying", "sent", "sent"]
    )
    result = outbox.deliver_due()
    assert len(calls) == 4  # three failures, the busy one between them not counted or resetting
    assert result["retrying"] == 3 and result["busy"] == 1
    assert result["left_for_next_run"] == 6  # the stub left every row waiting


def test_busy_attempts_alone_never_stop_a_sweep(monkeypatch, six_waiting):
    calls = scripted_attempt(monkeypatch, ["busy"] * 6)
    result = outbox.deliver_due()
    assert len(calls) == 6 and result["busy"] == 6
    assert "left_for_next_run" not in result


def test_a_notice_that_no_longer_applies_does_not_end_a_streak(monkeypatch, six_waiting):
    calls = scripted_attempt(monkeypatch, ["retrying", "not_sent", "retrying", "failed"])
    outbox.deliver_due()
    assert len(calls) == 4


@pytest.fixture
def three_notices(agent, category, clinic, clinic_contact, make_user, monkeypatch):
    for name in ("verify_one", "verify_two", "verify_three"):
        person = make_user(name, client=clinic)
        PortalAccess.objects.create(
            user=person, client=clinic, contact=clinic_contact, whole_organization=True
        )
    ticket = new_request(agent, category, client=clinic, contact=clinic_contact)
    cases.reply(agent, ticket)
    ids = list(Notice.objects.order_by("pk").values_list("pk", flat=True))
    assert len(ids) == 3
    return ids


def test_fast_sends_all_go_out_within_the_time_budget(settings, three_notices):
    settings.EMAIL_TIMEOUT = 10
    assert outbox.send_now(three_notices) == {"sent": 3}
    assert len(mail.outbox) == 3


def test_a_zero_timeout_still_attempts_the_first_notice(settings, three_notices):
    settings.EMAIL_TIMEOUT = 0
    result = outbox.send_now(three_notices)
    assert result == {"sent": 1, "left_for_next_run": 2}
    assert Notice.objects.filter(status=NoticeStatus.SENT).count() == 1
    assert Notice.objects.filter(status=NoticeStatus.WAITING, attempts=0).count() == 2
    # What was left is the scheduled job's.
    assert outbox.deliver_due()["sent"] == 2


def test_a_slow_first_send_leaves_the_rest_for_the_job(settings, frozen, three_notices):
    settings.EMAIL_TIMEOUT = 10

    def slow(message):
        frozen(frozen.now + timedelta(seconds=11))
        return 1

    from unittest.mock import patch

    with patch.object(mail.EmailMessage, "send", slow):
        assert outbox.send_now(three_notices) == {"sent": 1, "left_for_next_run": 2}


# V04: the breach job and the people it tells ----------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_with_no_account_locked_the_breach_job_records_in_one_run(agent, team_ticket):
    system_account()
    assert clock_job.notice_breaches() == {"breaches": 1, "notices": 1}


@pytest.mark.django_db(transaction=True)
def test_a_locked_lead_account_makes_the_request_busy_until_the_next_run(
    agent, make_user, accessioning, join, team_ticket
):
    lead = make_user("review_lead", "technical")
    join(lead, accessioning, lead=True)
    system_account()
    with cases.held_by_another_session(lead):
        outcome = cases.independent(clock_job.notice_breaches)
    assert outcome == {"breaches": 0, "notices": 0, "busy": 1}
    clock = RequestClock.objects.get(request=team_ticket)
    assert clock.resolution_breach_noticed_at is None and clock.resolution_breach_run is None
    assert not Notice.objects.filter(topic=notifications.BREACH_TOPIC).exists()
    assert clock_job.notice_breaches() == {"breaches": 1, "notices": 2}
    clock.refresh_from_db()
    assert clock.resolution_breach_noticed_at is not None


# V05: senders on different notices do not wait for each other -----------------------------------


@pytest.mark.django_db(transaction=True)
def test_two_senders_on_different_notices_both_send(monkeypatch, agent, portal_ticket):
    monkeypatch.setattr(outbox, "send_now", lambda ids, budget=None: {})
    cases.reply(agent, portal_ticket)
    cases.reply(agent, portal_ticket)
    first, second = Notice.objects.order_by("pk").values_list("pk", flat=True)
    meeting = Barrier(2)

    def both_in_transport(message):
        meeting.wait(timeout=10)  # broken if one sender is held up behind the other
        return 1

    monkeypatch.setattr(mail.EmailMessage, "send", both_in_transport)
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [
            pool.submit(cases._worker, lambda pk=pk: outbox.attempt(pk)) for pk in (first, second)
        ]
        outcomes = [call.result(timeout=30) for call in calls]
    assert outcomes == ["sent", "sent"]


# V06: the owner guard ---------------------------------------------------------------------------


def test_demoting_a_superuser_is_allowed_while_another_active_staff_superuser_exists():
    first = User.objects.create_superuser("verify_first", "first@example.invalid", "test")
    User.objects.create_superuser("verify_second", "second@example.invalid", "test")
    assert cases.account_form(first, is_superuser=False).is_valid()
    # A fresh reading, as each page is drawn: validating the first form set its copy's flags.
    assert cases.account_form(User.objects.get(pk=first.pk), is_staff=False).is_valid()


def test_the_only_active_staff_superuser_cannot_lose_staff_status():
    owner = User.objects.create_superuser("verify_owner", "owner@example.invalid", "test")
    User.objects.create_user(  # a superuser who cannot open the admin does not count
        "verify_nonstaff", "nonstaff@example.invalid", is_superuser=True, is_staff=False
    )
    for change in ({"is_staff": False}, {"is_superuser": False}, {"is_active": False}):
        form = cases.account_form(User.objects.get(pk=owner.pk), **change)
        assert not form.is_valid(), change
        assert "staff status" in str(form.errors)


def test_a_nonstaff_superuser_may_be_changed_freely():
    User.objects.create_superuser("verify_owner", "owner@example.invalid", "test")
    other = User.objects.create_user(
        "verify_nonstaff", "nonstaff@example.invalid", is_superuser=True, is_staff=False
    )
    assert cases.account_form(other, is_superuser=False).is_valid()


# V07: who the assignment picker offers ----------------------------------------------------------


def offered_ids(team):
    ids = set()
    for _label, group in assign_choices():
        if not isinstance(group, (list, tuple)):
            continue
        for value, _text in group:
            team_id, _, user_id = value.partition(":")
            if user_id and int(team_id) == team.pk:
                ids.add(int(user_id))
    return ids


def test_a_member_who_regains_a_role_is_offered_again(make_user, accessioning, join):
    member = make_user("review_member", "technical")
    join(member, accessioning)
    assert member.pk in offered_ids(accessioning)
    member.groups.clear()
    assert member.pk not in offered_ids(accessioning)
    member.groups.add(Group.objects.get_or_create(name="technical")[0])
    assert member.pk in offered_ids(accessioning)


def test_a_member_with_only_the_admin_role_is_offered_and_assignable(
    agent, make_user, accessioning, join, category
):
    boss = make_user("review_boss", "admin")
    join(boss, accessioning)
    join(agent, accessioning)
    assert boss.pk in offered_ids(accessioning)
    ticket = new_request(agent, category, team=accessioning)
    services.assign(
        actor=agent,
        request=ticket,
        team=accessioning,
        assignee=boss,
        expected_revision=ticket.revision,
    )
    ticket.refresh_from_db()
    assert ticket.assignee_id == boss.pk


def test_a_member_with_two_roles_is_offered_once(make_user, accessioning, join):
    member = make_user("review_member", "technical", "supervisor")
    join(member, accessioning)
    shown = [
        value
        for _label, group in assign_choices()
        if isinstance(group, (list, tuple))
        for value, _ in group
        if value == f"{accessioning.pk}:{member.pk}"
    ]
    assert len(shown) == 1


# V08 and V09: the jobs' account is not edited ---------------------------------------------------


def test_submitting_the_jobs_account_form_unchanged_is_valid_and_saves_nothing():
    user = system_account()
    before = User.objects.values().get(pk=user.pk)
    form = cases.account_form(user)
    assert form.is_valid(), form.errors
    form.save()
    assert User.objects.values().get(pk=user.pk) == before


@pytest.mark.parametrize(
    "change",
    [
        {"email": "someone@example.org"},
        {"email": "scheduled-jobs@lab-ops.invalid.example"},
        {"username": "renamed-jobs"},
    ],
    ids=["address", "lookalike-address", "username"],
)
def test_changing_the_jobs_accounts_address_or_username_is_refused(change):
    user = system_account()
    before = User.objects.values().get(pk=user.pk)
    form = cases.account_form(user, **change)
    assert not form.is_valid()
    assert User.objects.values().get(pk=user.pk) == before


# V10: who the page names ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("username", "first", "last", "expected"),
    [
        ("plain.person", "Pat", "Example", "Pat Example"),
        ("plain.person", "write to", "pat@example.invalid", "plain.person"),
        ("plain.person", "pat@example.invalid", "", "plain.person"),
        ("pat@example.invalid", "Pat", "Example", "Pat Example"),
        ("pat@example.invalid", "pat@example.invalid", "", None),
    ],
    ids=["name", "address-in-name", "address-as-name", "address-username", "both"],
)
def test_recipient_label_shows_neither_a_name_nor_a_username_holding_an_address(
    username, first, last, expected
):
    user = User.objects.create_user(username, "c@example.invalid", first_name=first, last_name=last)
    label = recipient_label(user)
    assert label == (expected or f"Account {user.pk}")
    assert "@" not in label


# V11: each run of a clock has an identifier -----------------------------------------------------


def test_a_recalculation_clears_the_run_identifier_with_the_mark(agent, team_ticket, frozen):
    ServiceTarget.objects.create(name="Longer", priority="high", resolution_minutes=240)
    assert clock_job.notice_breaches()["breaches"] == 1
    clock = RequestClock.objects.get(request=team_ticket)
    assert clock.resolution_breach_run is not None
    notice = Notice.objects.get(topic=notifications.BREACH_TOPIC)
    assert notice.data["run"] == str(clock.resolution_breach_run)
    team_ticket.refresh_from_db()
    services.update_properties(
        actor=agent,
        request=team_ticket,
        category=team_ticket.category,
        priority="high",
        expected_revision=team_ticket.revision,
    )
    clock.refresh_from_db()
    assert clock.resolution_breach_noticed_at is None and clock.resolution_breach_run is None
    assert notifications.prepare_breach_notice(notice) is None


def test_a_pause_and_resume_keeps_the_run_identifier(agent, team_ticket, frozen):
    assert clock_job.notice_breaches()["breaches"] == 1
    run = RequestClock.objects.get(request=team_ticket).resolution_breach_run
    assert run is not None
    cases.change_status(agent, team_ticket, Status.WAITING_CLIENT)
    frozen(frozen.now + timedelta(minutes=10))
    cases.change_status(agent, team_ticket, Status.OPEN)
    assert RequestClock.objects.get(request=team_ticket).resolution_breach_run == run


def test_resolving_clears_the_run_identifier_and_a_new_breach_gets_a_new_one(
    agent, team_ticket, frozen
):
    assert clock_job.notice_breaches()["breaches"] == 1
    first = RequestClock.objects.get(request=team_ticket).resolution_breach_run
    cases.change_status(agent, team_ticket, Status.RESOLVED)
    assert RequestClock.objects.get(request=team_ticket).resolution_breach_run is None
    cases.change_status(agent, team_ticket, Status.OPEN)
    assert clock_job.notice_breaches()["breaches"] == 1
    second = RequestClock.objects.get(request=team_ticket).resolution_breach_run
    assert second is not None and second != first


@pytest.fixture
def at_migration(transactional_db):
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()

    def go(name):
        MigrationExecutor(connection).migrate([("service_levels", name)])

    yield go
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(latest)


def test_the_migration_marks_old_breaches_without_a_run_identifier(
    at_migration, agent, category, accessioning, join, frozen
):
    ServiceTarget.objects.create(name="Both", first_response_minutes=60, resolution_minutes=120)
    join(agent, accessioning)
    ticket = new_request(agent, category, team=accessioning, assignee=agent, channel="email")
    now = timezone.now()
    RequestClock.objects.filter(request=ticket).update(
        first_response_due_at=now - timedelta(hours=2), resolution_due_at=now - timedelta(hours=1)
    )
    at_migration("0002_ticket_terminology")
    at_migration("0003_breach_notices")
    clock = RequestClock.objects.get(request=ticket)
    assert clock.first_response_breach_noticed_at is not None
    assert clock.resolution_breach_noticed_at is not None
    assert clock.first_response_breach_run is None and clock.resolution_breach_run is None
    # An old mark without a run is not a run any waiting notice can belong to.
    assert clock_job.notice_breaches(now=now) == {"breaches": 0, "notices": 0}


# V14: which jobs a run takes --------------------------------------------------------------------


def test_no_choice_runs_every_job(fake_jobs):
    results = runner.run(only=None)
    assert [r.name for r in results] == [job.name for job in runner.JOBS]
    assert fake_jobs == [job.name for job in runner.JOBS]


@pytest.mark.parametrize("only", [(), []], ids=["tuple", "list"])
def test_an_empty_choice_runs_no_job(fake_jobs, only):
    # A selection runs exactly what it names (final check F11).
    assert runner.run(only=only) == []
    assert fake_jobs == []


def test_a_choice_given_as_a_generator_is_read_once_and_runs_in_registry_order(fake_jobs):
    chosen = (name for name in ("send_notices", "close_resolved_tickets", "send_notices"))
    results = runner.run(only=chosen)
    assert [r.name for r in results] == ["close_resolved_tickets", "send_notices"]


# V15: days given as digits ----------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0365", "00007", "000000000000000000000001", " 0365 "])
def test_leading_zeros_do_not_change_the_number(admin, value):
    update_settings(actor=admin, expected_revision=1, close_resolved_after_days=value)
    assert JobSettings.objects.get(pk=1).close_resolved_after_days == int(value)


@pytest.mark.parametrize("value", ["366", "0000366", "00", "0", "9" * 500])
def test_digit_strings_out_of_range_are_refused_with_a_field_message(admin, value):
    with pytest.raises(ValidationError) as error:
        update_settings(actor=admin, expected_revision=1, close_resolved_after_days=value)
    assert "close_resolved_after_days" in error.value.message_dict
    assert not JobSettings.objects.filter(close_resolved_after_days__isnull=False).exists()
