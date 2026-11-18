"""Second independent verification of the scheduler fixes. All data is synthetic."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest.mock import patch
from uuid import UUID

import pytest
from django.core import mail
from django.db import OperationalError, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.urls import reverse

from accounts.models import User
from core import demo, preferences
from core.models import UserPreference
from desk import notifications, services
from desk.forms import assign_choices
from desk.models import Status
from desk.tests.test_contract import new_request
from desk.tests.test_portal_scope_and_dispatch import admin_post
from jobs import outbox, runner
from jobs.actor import system_account
from jobs.models import Notice, NoticeStatus
from service_levels import jobs as clock_job
from service_levels.models import RequestClock, ServiceTarget
from teams.services import add_member

from . import test_review_outbox_round1_verify as prior
from . import test_review_round1_fixes as cases

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("locmem", "review_database")]
review_database = prior.review_database
frozen = cases.frozen
portal_ticket = cases.portal_ticket
waiting_notice_for_client = cases.waiting_notice_for_client
fake_jobs = cases.fake_jobs


@pytest.mark.django_db(transaction=True)
def test_two_owner_edits_preserve_access_to_account_administration(make_user):
    """Exercise two normal admin saves. Each connection discards its own edit."""

    def owner(name):
        user = make_user(name, is_staff=True, is_superuser=True)
        http = Client()
        http.force_login(user)
        return user, http

    first, first_http = owner("final_first_owner")
    second, second_http = owner("final_second_owner")

    def second_edit():
        try:
            with transaction.atomic():
                admin_post(second_http, second, is_staff="")
                changed = not User.objects.get(pk=second.pk).is_staff
                connection.check_constraints()
                transaction.set_rollback(True)
                return changed
        except OperationalError as exc:
            if exc.__cause__.sqlstate != "55P03":
                raise
            return False

    with transaction.atomic():
        assert admin_post(first_http, first, is_staff="").status_code == 302
        assert not User.objects.get(pk=first.pk).is_staff
        second_also_saved = cases.independent(second_edit)
        transaction.set_rollback(True)
    assert User.objects.filter(is_active=True, is_superuser=True, is_staff=True).count() == 2
    assert not second_also_saved, "Both owners' departures were accepted"


def test_reserved_account_has_no_editable_password_form(client, make_user):
    owner = make_user("final_owner", is_staff=True, is_superuser=True)
    actor = system_account()
    client.force_login(owner)
    response = client.get(reverse("admin:auth_user_password_change", args=[actor.pk]))
    editable = (
        response.status_code == 200
        and response.context.get("has_change_permission")
        and any(
            name in response.context["form"].fields
            and not response.context["form"].fields[name].disabled
            for name in ("password1", "password2")
        )
    )
    assert not editable, "Account administration offers Set password for the reserved account"


def test_person_at_preference_limit_can_still_turn_off_ticket_mail(client, agent):
    UserPreference.objects.bulk_create(
        [
            UserPreference(
                user=agent, key=f"grid:synthetic-{i}:v1", value={"order": [], "hidden": []}
            )
            for i in range(preferences.MAX_PREFERENCES)
        ]
    )
    client.force_login(agent)
    client.raise_request_exception = False
    response = client.post(reverse("email-notices"), {})
    assert (response.status_code, notifications.wants_notices(agent)) == (302, False)


@pytest.mark.django_db(transaction=True)
def test_portal_reply_sends_while_an_unrelated_staff_membership_is_edited(
    portal_ticket, agent, join, make_user
):
    peer = make_user("final_peer", "technical")
    unrelated = join(peer, portal_ticket.team)
    with cases.held_by_another_session(unrelated):
        cases.reply(agent, portal_ticket)
    row = Notice.objects.get(recipient_id=portal_ticket.created_by_id)
    assert (row.status, row.attempts, len(mail.outbox)) == (NoticeStatus.SENT, 1, 1)


@pytest.mark.django_db(transaction=True)
def test_breach_record_and_first_delivery_agree_about_the_first_team_lead(
    agent, category, accessioning, join, make_user, frozen, monkeypatch
):
    supervisor = make_user("final_supervisor", "supervisor")
    lead = make_user("final_lead", "technical")
    ServiceTarget.objects.create(name="Synthetic target", resolution_minutes=15)
    join(agent, accessioning)
    ticket = new_request(agent, category, team=accessioning)
    frozen(frozen.now + timedelta(hours=1))
    read_recipients = notifications.breach_recipients
    writer_committed = []

    def appoint():
        try:
            add_member(actor=supervisor, team=accessioning, user=lead, is_lead=True)
        except OperationalError as exc:
            if exc.__cause__.sqlstate != "55P03":
                raise
            return False
        return True

    def read_then_appoint(request):
        audience = read_recipients(request)
        if not writer_committed:
            writer_committed.append(cases.independent(appoint))
        return audience

    monkeypatch.setattr(notifications, "breach_recipients", read_then_appoint)
    outcome = clock_job.notice_breaches()
    if not outcome["breaches"]:
        # Deferring a concurrent change is also correct.
        assert outcome["busy"] == 1
        outcome = clock_job.notice_breaches()
    assert outcome["breaches"] == 1
    assert clock_job.notice_breaches()["breaches"] == 0
    expected = lead if writer_committed == [True] else agent
    deliveries = list(
        Notice.objects.filter(object_id=ticket.pk).values_list("recipient_id", "status")
    )
    assert (deliveries, [message.to for message in mail.outbox]) == (
        [(expected.pk, NoticeStatus.SENT)],
        [[expected.email]],
    )


@pytest.mark.django_db(transaction=True)
def test_busy_recipient_at_front_does_not_starve_an_unrelated_team(
    agent, category, accessioning, quality, join, make_user, frozen, monkeypatch
):
    other = make_user("final_other_team", "technical")
    join(agent, accessioning)
    join(other, quality)
    ServiceTarget.objects.create(name="Synthetic target", resolution_minutes=15)
    first = new_request(agent, category, team=accessioning, assignee=agent)
    next_ticket = new_request(other, category, team=quality, assignee=other)
    frozen(frozen.now + timedelta(hours=1))
    monkeypatch.setattr(clock_job, "BREACH_BATCH", 1)
    with cases.held_by_another_session(agent):
        clock_job.notice_breaches()
        clock_job.notice_breaches()
    assert not RequestClock.objects.get(request=first).resolution_breach_noticed_at
    assert RequestClock.objects.get(request=next_ticket).resolution_breach_noticed_at is not None


@pytest.mark.django_db(transaction=True)
def test_outbox_recovery_can_record_its_outcome_during_maintenance(waiting_notice_for_client):
    row = waiting_notice_for_client
    record_failure = outbox._failed_attempt
    recovering, proceed = Event(), Event()

    def at_recovery_write(*args):
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '450ms'")
        recovering.set()
        assert proceed.wait(timeout=10)
        return record_failure(*args)

    def deliver():
        try:
            return outbox.attempt(row.pk)
        except OperationalError as exc:
            if exc.__cause__.sqlstate != "55P03":
                raise
            return "status write timed out"
        finally:
            connections.close_all()

    with (
        patch.object(
            outbox, "notice_handler", side_effect=ValueError("Synthetic preparation error")
        ),
        patch.object(outbox, "_failed_attempt", at_recovery_write),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        worker = pool.submit(deliver)
        try:
            assert recovering.wait(timeout=10)
            # Acquire only the maintenance guard. The worker may complete or hit the short
            # lock timeout. No table contents are modified by maintenance.
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '450ms'")
                demo.lock_application_tables()
                proceed.set()
                worker.result(timeout=10)
        except OperationalError as exc:
            if exc.__cause__.sqlstate != "55P03":
                raise
        finally:
            proceed.set()
        outcome = worker.result(timeout=10)
    row.refresh_from_db()
    assert (outcome, row.attempts, row.last_error) == ("retrying", 1, "ValueError")


@pytest.mark.django_db(transaction=True)
def test_one_bulk_edit_uses_one_synchronous_mail_budget(
    agent, category, accessioning, join, make_user, frozen, settings
):
    assignee = make_user("final_assignee", "technical")
    join(assignee, accessioning)
    tickets = [new_request(agent, category, team=accessioning) for _ in range(4)]
    settings.EMAIL_TIMEOUT = 10
    start = frozen.now

    def slow_success(message):
        frozen(frozen.now + timedelta(seconds=9))
        return 1

    with patch.object(mail.EmailMessage, "send", slow_success):
        assert services.bulk_assign(
            actor=agent,
            selected={ticket.pk: ticket.revision for ticket in tickets},
            team=accessioning,
            assignee=assignee,
        ) == len(tickets)
    assert Notice.objects.count() == len(tickets)
    assert frozen.now - start < timedelta(seconds=2 * settings.EMAIL_TIMEOUT)


def test_send_now_finishes_a_single_pass_input_when_the_budget_is_used(
    agent, portal_ticket, frozen, settings
):
    for _ in range(3):
        cases.reply(agent, portal_ticket)
    ids = Notice.objects.order_by("pk").values_list("pk", flat=True)
    settings.EMAIL_TIMEOUT = 10

    def one_wait(message):
        frozen(frozen.now + timedelta(seconds=settings.EMAIL_TIMEOUT))
        return 1

    with patch.object(mail.EmailMessage, "send", one_wait):
        counts = outbox.send_now(iter(ids))
    assert counts["sent"] == 1
    assert Notice.objects.filter(status=NoticeStatus.WAITING).count() == 2


def test_empty_single_pass_job_selection_cannot_start_unselected_jobs(fake_jobs):
    results = runner.run(only=iter(()))
    assert (results, fake_jobs) == ([], [])


def test_meeting_first_response_ends_its_breach_run(agent, category, accessioning, join, frozen):
    ServiceTarget.objects.create(name="Synthetic target", first_response_minutes=15)
    join(agent, accessioning)
    ticket = new_request(agent, category, team=accessioning, assignee=agent, channel="email")
    frozen(frozen.now + timedelta(hours=1))
    assert clock_job.notice_breaches()["notices"] == 1
    row = Notice.objects.get(topic=notifications.BREACH_TOPIC)
    clock = RequestClock.objects.get(request=ticket)
    assert isinstance(clock.first_response_breach_run, UUID)
    cases.reply(agent, ticket)
    clock.refresh_from_db()
    assert clock.first_response_met_at is not None
    assert clock.first_response_missed and notifications.prepare_breach_notice(row) is None
    assert (clock.first_response_breach_noticed_at, clock.first_response_breach_run) == (None, None)


@pytest.mark.django_db(transaction=True)
def test_migrated_suppression_and_subsequent_live_run(agent, category, accessioning, join, frozen):
    ServiceTarget.objects.create(
        name="Synthetic target", first_response_minutes=15, resolution_minutes=30
    )
    join(agent, accessioning)
    ticket = new_request(agent, category, team=accessioning, assignee=agent, channel="email")
    frozen(frozen.now + timedelta(hours=1))
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        MigrationExecutor(connection).migrate([("service_levels", "0002_ticket_terminology")])
        with connection.cursor() as cursor:
            fields = connection.introspection.get_table_description(
                cursor, RequestClock._meta.db_table
            )
        assert not any(field.name.endswith("_breach_run") for field in fields)
    finally:
        MigrationExecutor(connection).migrate(latest)
    clock = RequestClock.objects.get(request=ticket)
    assert clock.first_response_breach_noticed_at and clock.resolution_breach_noticed_at
    assert clock_job.notice_breaches()["notices"] == 0 and not Notice.objects.exists()
    cases.change_status(agent, ticket, Status.RESOLVED)
    cases.change_status(agent, ticket, Status.OPEN)
    assert clock_job.notice_breaches()["notices"] == 1
    clock.refresh_from_db()
    row = Notice.objects.get()
    assert isinstance(clock.resolution_breach_run, UUID)
    assert (row.data["run"], row.status) == (str(clock.resolution_breach_run), NoticeStatus.SENT)


def test_picker_lists_a_person_with_two_valid_roles_once(accessioning, join, make_user):
    person = make_user("final_two_roles", "technical", "supervisor")
    join(person, accessioning)
    choices = dict(assign_choices())[accessioning.name]
    assert [key for key, _label in choices].count(f"{accessioning.pk}:{person.pk}") == 1


@pytest.mark.django_db(transaction=True)
def test_portal_handoff_allows_assignment_of_a_different_ticket_on_the_team(
    waiting_notice_for_client, agent, portal_ticket, category, make_user, join
):
    other_ticket = new_request(agent, category, team=portal_ticket.team)
    assignee = make_user("final_independent_assignee", "technical")
    join(assignee, portal_ticket.team)
    writer_waited = []

    def handoff(message):
        writer_waited.append(
            prior.writer_waits(
                lambda: services.assign(
                    actor=agent,
                    request=other_ticket,
                    team=portal_ticket.team,
                    assignee=assignee,
                    expected_revision=other_ticket.revision,
                )
            )
        )
        return 1

    with patch.object(mail.EmailMessage, "send", handoff):
        assert outbox.attempt(waiting_notice_for_client.pk) == "sent"
    assert writer_waited == [False], "An unrelated ticket's assignment waited for portal mail"


@pytest.mark.django_db(transaction=True)
def test_assignment_holds_the_role_used_to_validate_the_new_assignee(
    agent, category, accessioning, join, make_user
):
    person = make_user("final_staff_assignee", "technical")
    role = person.groups.get()
    join(person, accessioning)
    ticket = new_request(agent, category, team=accessioning)
    owner = make_user("final_role_admin", is_staff=True, is_superuser=True)
    http = Client()
    http.force_login(owner)
    with transaction.atomic():
        services.assign(
            actor=agent,
            request=ticket,
            team=accessioning,
            assignee=person,
            expected_revision=ticket.revision,
        )
        # A full account edit waits on the account. A role-only writer can legitimately use
        # the Django group relation directly. Both probes roll back.
        assert prior.writer_waits(lambda: admin_post(http, person, groups=[]))
        assert prior.writer_waits(lambda: person.groups.remove(role))
    assert person.groups.filter(pk=role.pk).exists()


def test_recipient_fallback_when_both_display_fields_contain_addresses(make_user):
    from jobs.views import recipient_label

    user = make_user("final_label")
    user.first_name = "Mailbox"
    user.last_name = "name@example.invalid"
    user.username = "login@example.invalid"
    assert recipient_label(user) == f"Account {user.pk}"


def test_long_zero_padding_preserves_an_in_range_settings_value(make_user):
    from jobs.models import JobSettings
    from jobs.services import update_settings

    admin = make_user("final_days_admin", "admin")
    assert update_settings(
        actor=admin, expected_revision=1, close_resolved_after_days="0" * 5000 + "365"
    )
    assert JobSettings.objects.get().close_resolved_after_days == 365


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("held", ["recipient", "notice"])
def test_contention_between_smtp_failures_preserves_the_streak(
    held, agent, portal_ticket, make_user, clinic, category
):
    other = make_user("final_second_portal", client=clinic)
    separate = new_request(other, category, channel="portal", first_entry_kind="reply")
    with patch.object(outbox, "send_now", return_value={}):
        for ticket in (
            portal_ticket,
            portal_ticket,
            separate,
            portal_ticket,
            portal_ticket,
            portal_ticket,
        ):
            cases.reply(agent, ticket)
    row = Notice.objects.get(recipient=other)
    busy = other if held == "recipient" else row
    with (
        cases.held_by_another_session(busy),
        patch.object(mail.EmailMessage, "send", side_effect=ConnectionRefusedError) as transport,
    ):
        result = outbox.deliver_due()
    assert result["retrying"] == transport.call_count == 3
    row.refresh_from_db()
    assert row.attempts == 0 and row.status == NoticeStatus.WAITING
