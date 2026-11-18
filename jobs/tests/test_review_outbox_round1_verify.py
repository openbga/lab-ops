"""Independent verification of the round-one scheduler fixes. All records are synthetic."""

import os
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import OperationalError, connection, connections, transaction
from django.test import Client
from django.urls import reverse

from accounts.models import User
from clients.models import ClientSite, PortalAccess
from clients.services import set_portal_access
from core import demo
from desk import notifications, services
from desk.forms import assign_choices
from desk.jobs import close_resolved_tickets
from desk.models import Status
from desk.tests.test_auto_close import resolve, set_period
from desk.tests.test_contract import new_request
from jobs import outbox, runner
from jobs.actor import system_account
from jobs.models import Notice, NoticeStatus
from jobs.services import update_settings
from service_levels import jobs as clock_job
from service_levels.models import RequestClock, ServiceTarget
from teams import services as teams

from . import test_review_round1_fixes as cases

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("locmem")]
frozen = cases.frozen
portal_ticket = cases.portal_ticket
team_ticket = cases.team_ticket
waiting_notice_for_client = cases.waiting_notice_for_client


@pytest.fixture(autouse=True)
def review_database(db):
    assert connection.vendor == "postgresql"
    if db_name := os.getenv("TEST_DATABASE_NAME"):
        assert connection.settings_dict["NAME"] == db_name


def writer_waits(write):
    """Exercise the normal writer up to commit, then discard every change."""

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


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("new_member", [False, True], ids=["promote-peer", "add-first-lead"])
def test_member_handoff_holds_the_decision_that_the_team_has_no_lead(
    new_member, agent, category, accessioning, join, make_user, frozen
):
    supervisor = make_user("verify_supervisor", "supervisor")
    peer = make_user("verify_peer", "technical")
    ServiceTarget.objects.create(name="Verify target", resolution_minutes=60)
    join(agent, accessioning)
    membership = None if new_member else join(peer, accessioning)
    ticket = new_request(agent, category, team=accessioning)
    frozen(frozen.now + timedelta(hours=2))
    with patch.object(outbox, "send_now", return_value={}):
        clock_job.notice_breaches()
    row = Notice.objects.get(topic=notifications.BREACH_TOPIC, recipient=agent)
    assert row.data["role"] == "member"

    def appoint_lead():
        if new_member:
            teams.add_member(actor=supervisor, team=accessioning, user=peer, is_lead=True)
        else:
            teams.set_lead(actor=supervisor, membership=membership, is_lead=True)
        assert agent.pk not in {person.pk for person, _ in notifications.breach_recipients(ticket)}

    with transaction.atomic():
        assert notifications.prepare_breach_notice(row) is not None
        assert not cases.can_lock(ticket)
        assert writer_waits(appoint_lead), "The routing change finished before handoff"
    assert not mail.outbox


@pytest.mark.django_db(transaction=True)
def test_newer_busy_retry_survives_older_recovery(waiting_notice_for_client, clinic_portal):
    row = waiting_notice_for_client
    first = row.next_attempt_at
    later = first + timedelta(hours=2)
    database = connections["default"]
    rollback = database.rollback
    other = []

    def after_rollback():
        rollback()
        other.append(cases.independent(lambda: outbox.attempt(row.pk, now=later)))

    with (
        cases.held_by_another_session(clinic_portal),
        patch.object(database, "rollback", after_rollback),
        patch.object(
            outbox,
            "notice_handler",
            side_effect=[
                RuntimeError("Synthetic preparation error"),
                notifications.prepare_request_notice,
            ],
        ),
    ):
        outbox.attempt(row.pk, now=first)

    row.refresh_from_db()
    assert other == ["busy"]
    assert (row.attempts, row.last_attempt_at, row.next_attempt_at) == (
        0,
        None,
        later + outbox.BUSY_DELAY,
    )


@pytest.mark.django_db(transaction=True)
def test_breach_recording_leaves_a_busy_assignee_for_the_next_run(agent, team_ticket):
    system_account()  # isolate the breach transaction from first-use account creation
    outcomes = []
    release = services.release_assignments

    def before_release(**kwargs):
        # The account update has executed. Its normal next step needs the assigned ticket.
        # Let the sweep finish its bounded probe before that step, keeping the waits one-way.
        try:
            outcome = cases.independent(clock_job.notice_breaches)
        except OperationalError as exc:
            if exc.__cause__.sqlstate != "55P03":
                raise
            outcome = {"commit_lock_timeout": 1}
        outcomes.append(outcome)
        return release(**kwargs)

    with transaction.atomic(), patch.object(services, "release_assignments", before_release):
        agent.email = "changed-review-address@example.invalid"
        agent.is_active = False
        agent.save()
        assert agent.released_assignments == 1
        transaction.set_rollback(True)
    assert not RequestClock.objects.get(request=team_ticket).resolution_breach_noticed_at
    assert clock_job.notice_breaches()["notices"] == 1
    assert outcomes == [{"busy": 1, "breaches": 0, "notices": 0}]


def test_restarted_breach_with_same_sweep_time_is_a_distinct_occurrence(agent, team_ticket, frozen):
    sweep_time = frozen.now
    assert clock_job.notice_breaches(now=sweep_time)["notices"] == 1
    old = Notice.objects.get(topic=notifications.BREACH_TOPIC)
    cases.change_status(agent, team_ticket, Status.RESOLVED)
    assert notifications.prepare_breach_notice(old) is None
    cases.change_status(agent, team_ticket, Status.OPEN)
    assert clock_job.notice_breaches(now=sweep_time)["notices"] == 1
    current = Notice.objects.exclude(pk=old.pk).get(topic=notifications.BREACH_TOPIC)
    assert notifications.prepare_breach_notice(current) is not None
    assert notifications.prepare_breach_notice(old) is None


def test_withdrawn_notice_does_not_break_a_streak_of_smtp_refusals(agent, make_user, portal_ticket):
    supervisor = make_user("verify_supervisor", "supervisor")
    replies = [cases.reply(agent, portal_ticket) for _ in range(6)]
    portal_ticket.refresh_from_db()
    services.withdraw_reply(
        actor=supervisor,
        entry=replies[2],
        reason="Synthetic correction",
        expected_revision=portal_ticket.revision,
    )
    with patch.object(mail.EmailMessage, "send", side_effect=ConnectionRefusedError) as send:
        counts = outbox.deliver_due()
    assert counts["not_sent"] == 1
    assert send.call_count == outbox.STOP_AFTER_FAILURES
    assert counts["left_for_next_run"] == 2


@pytest.mark.django_db(transaction=True)
def test_slow_successful_deliveries_share_the_after_commit_time_budget(
    agent, category, clinic, clinic_contact, make_user, settings, frozen
):
    for name in ("verify_one", "verify_two", "verify_three"):
        person = make_user(name, client=clinic)
        PortalAccess.objects.create(
            user=person, client=clinic, contact=clinic_contact, whole_organization=True
        )
    ticket = new_request(agent, category, client=clinic, contact=clinic_contact)
    settings.EMAIL_TIMEOUT = 10
    start = frozen.now

    def slow_accept(message):
        frozen(frozen.now + timedelta(seconds=9))
        return 1

    with patch.object(mail.EmailMessage, "send", slow_accept):
        cases.reply(agent, ticket)
    assert Notice.objects.count() == 3
    # Allow one extra in-flight send when the budget is measured between deliveries.
    assert frozen.now - start < timedelta(seconds=2 * settings.EMAIL_TIMEOUT)


def test_opt_out_from_an_existing_session_persists_after_another_session_opted_in(agent):
    first, second = Client(), Client()
    for client in (first, second):
        client.force_login(agent)
    url = reverse("email-notices")
    assert first.post(url, {}).status_code == 302
    assert not notifications.wants_notices(agent)
    assert second.post(url, {"enabled": "on"}).status_code == 302
    assert notifications.wants_notices(agent)
    page = first.get(url)
    assert page.status_code == 200 and page.context["form"]["enabled"].value()
    assert first.post(url, {}).status_code == 302
    assert not notifications.wants_notices(agent)


def test_reserved_account_keeps_a_non_deliverable_address():
    actor = system_account()
    candidate = cases.account_form(actor, email="review-actor@example.org")
    if candidate.is_valid():
        candidate.save()
    actor.refresh_from_db()
    assert not actor.email or actor.email.lower().endswith(".invalid")


@pytest.mark.parametrize("change", ["remove_staff", "nonstaff_other_owner"])
def test_account_form_preserves_a_human_who_can_open_account_admin(change):
    owner = User.objects.create_superuser("verify_owner", "verify_owner@example.invalid", "test")
    if change == "remove_staff":
        form = cases.account_form(owner, is_staff=False)
    else:
        User.objects.create_user(
            "verify_other",
            "verify_other@example.invalid",
            is_active=True,
            is_staff=False,
            is_superuser=True,
        )
        form = cases.account_form(owner, is_active=False)
    if form.is_valid():
        form.save()
    assert User.objects.filter(is_active=True, is_staff=True, is_superuser=True).exists()


def test_jobs_page_hides_address_in_a_full_name(client, agent, portal_ticket, make_user):
    admin = make_user("verify_admin", "admin")
    recipient = portal_ticket.created_by
    recipient.first_name = recipient.email
    recipient.save(update_fields=["first_name"])
    cases.reply(agent, portal_ticket)
    Notice.objects.filter(recipient=recipient).update(status=NoticeStatus.FAILED)
    client.force_login(admin)
    response = client.get(reverse("jobs:overview"))
    page = response.content.decode()
    assert response.status_code == 200 and portal_ticket.number in page
    assert recipient.email not in page


def test_run_honors_names_from_a_single_pass_iterable():
    results = runner.run(only=iter(["send_notices", "send_notices"]))
    assert [result.name for result in results] == ["send_notices"]


def test_settings_service_returns_field_validation_for_oversized_digit_strings(make_user):
    admin = make_user("verify_admin", "admin")
    with pytest.raises(ValidationError) as refused:
        update_settings(actor=admin, expected_revision=1, close_resolved_after_days="9" * 5000)
    assert "close_resolved_after_days" in refused.value.error_dict


@pytest.mark.django_db(transaction=True)
def test_busy_recipient_releases_the_ticket_before_rescheduling(
    clinic_portal, portal_ticket, waiting_notice_for_client
):
    with cases.held_by_another_session(clinic_portal):
        assert cases.independent(lambda: outbox.attempt(waiting_notice_for_client.pk)) == "busy"
        assert cases.can_lock(portal_ticket)
    waiting_notice_for_client.refresh_from_db()
    assert waiting_notice_for_client.attempts == 0 and not mail.outbox


@pytest.mark.parametrize("create_actor", [False, True], ids=["fresh-database", "actor-exists"])
def test_assignment_picker_retains_humans_and_team_queue(create_actor, accessioning, agent, join):
    join(agent, accessioning)
    if create_actor:
        system_account()
    values = dict(dict(assign_choices())[accessioning.name])
    assert {f"{accessioning.pk}:", f"{accessioning.pk}:{agent.pk}"} == set(values)


@pytest.mark.django_db(transaction=True)
def test_prepared_client_reply_serializes_with_supported_withdrawal(
    waiting_notice_for_client, portal_ticket, make_user
):
    supervisor = make_user("verify_supervisor", "supervisor")
    row = waiting_notice_for_client
    entry = portal_ticket.entries.get(author__groups__name="client_services")
    portal_ticket.refresh_from_db()
    with transaction.atomic():
        assert notifications.prepare_request_notice(row) is not None
        assert writer_waits(
            lambda: services.withdraw_reply(
                actor=supervisor,
                entry=entry,
                expected_revision=portal_ticket.revision,
                reason="Synthetic correction",
            )
        )


def test_automatic_close_keeps_its_timeline_attribution_after_an_account_edit(
    client, agent, category
):
    ticket = resolve(agent, new_request(agent, category), days_ago=3)
    set_period(2)
    assert close_resolved_tickets()["closed"] == 1
    form = cases.account_form(system_account(), first_name="Synthetic operator")
    if form.is_valid():
        form.save()
    client.force_login(agent)
    page = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    assert page.status_code == 200
    event = next(
        line
        for line in page.content.decode().splitlines()
        if "closed automatically 2 days" in line.lower()
    )
    assert "Scheduled jobs" in event


@pytest.mark.parametrize("entry_point", ["picker", "service"])
def test_former_staff_member_is_no_longer_assignable(
    agent, category, accessioning, join, make_user, entry_point
):
    former_staff = make_user("verify_former", "technical")
    join(former_staff, accessioning)
    former_staff.groups.clear()
    if entry_point == "picker":
        choices = dict(dict(assign_choices())[accessioning.name])
        assert f"{accessioning.pk}:{former_staff.pk}" not in choices
    else:
        ticket = new_request(agent, category, team=accessioning)
        with pytest.raises(ValidationError):
            services.assign(
                actor=agent,
                request=ticket,
                team=accessioning,
                assignee=former_staff,
                expected_revision=ticket.revision,
            )


@pytest.mark.django_db(transaction=True)
def test_send_and_maintenance_serialize_before_transport(waiting_notice_for_client):
    guard_waited = []

    def transport(message):
        # Probe only the initial guard. No reset is run and no tables are erased.
        guard_waited.append(writer_waits(demo.lock_application_tables))
        return 1

    with patch.object(mail.EmailMessage, "send", transport):
        assert outbox.attempt(waiting_notice_for_client.pk) == "sent"
    assert guard_waited == [True], "The maintenance guard entered during mail handoff"


@pytest.mark.django_db(transaction=True)
def test_supported_site_grant_writer_waits_at_client_handoff(
    agent, clinic, clinic_portal, clinic_contact, category
):
    sites = [ClientSite.objects.create(client=clinic, code=n, name=n) for n in ("East", "West")]
    access = set_portal_access(
        actor=agent,
        user_id=clinic_portal.pk,
        expected_client_id=clinic.pk,
        contact=clinic_contact,
        whole_organization=False,
        sites=sites,
    )
    ticket = new_request(agent, category, client=clinic, contact=clinic_contact, site=sites[0])
    with patch.object(outbox, "send_now", return_value={}):
        cases.reply(agent, ticket)
    row = Notice.objects.get(recipient=clinic_portal)
    with transaction.atomic():
        assert notifications.prepare_request_notice(row) is not None
        assert writer_waits(
            lambda: set_portal_access(
                actor=agent,
                user_id=clinic_portal.pk,
                expected_client_id=clinic.pk,
                expected_revision=access.revision,
                contact=clinic_contact,
                whole_organization=False,
                sites=[sites[1]],
                reason="Synthetic site change",
            )
        )
