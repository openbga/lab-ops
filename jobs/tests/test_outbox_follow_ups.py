"""Neighbours of the follow-up findings C01-C07 of the outbox review (see
test_review_outbox_round1_confirm.py for the reviewer's own cases). All data is synthetic."""

import re
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, transaction
from django.urls import reverse

from accounts import roles
from accounts.admin import AccountChangeForm
from desk import notifications, services
from desk.models import Status
from desk.tests.test_contract import new_request
from jobs import outbox
from jobs.actor import locked_settings, system_account
from jobs.models import Notice, NoticeStatus, NotSentReason
from jobs.services import update_settings
from service_levels import jobs as clock_job
from service_levels.models import RequestClock, ServiceTarget
from teams import services as team_services

from . import test_review_outbox_round1_verify as prior
from . import test_review_round1_fixes as cases

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("locmem", "review_database"),
]
review_database = prior.review_database
frozen = cases.frozen
team_ticket = cases.team_ticket


class Abandon(Exception):
    """Raised to roll a transaction back."""


@pytest.fixture
def portal_tickets(clinic_portal, category):
    return [
        new_request(clinic_portal, category, channel="portal", first_entry_kind="reply")
        for _ in range(3)
    ]


@pytest.fixture
def waiting(agent, portal_tickets):
    """Three recorded, committed, unattempted notices."""
    with patch.object(outbox, "send_now", return_value={}):
        for ticket in portal_tickets:
            cases.reply(agent, ticket)
    rows = list(Notice.objects.order_by("pk"))
    assert len(rows) == 3
    assert all(row.status == NoticeStatus.WAITING and row.attempts == 0 for row in rows)
    return rows


def states(*rows):
    out = []
    for row in rows:
        row.refresh_from_db()
        out.append((row.status, row.attempts))
    return out


SENT, WAITING = NoticeStatus.SENT, NoticeStatus.WAITING
HELP = "Nobody signs in as this account: it has no password, and none can be set."


# C01: one budget per transaction -----------------------------------------------------------------


def test_first_refusal_in_one_transaction_stops_all_three_tickets(agent, portal_tickets):
    """Three tickets replied to in one transaction share one budget: one refusal stops the rest."""
    with patch.object(mail.EmailMessage, "send", side_effect=ConnectionRefusedError) as transport:
        with transaction.atomic():
            for ticket in portal_tickets:
                cases.reply(agent, ticket)
    attempts = list(Notice.objects.order_by("pk").values_list("attempts", flat=True))
    assert (transport.call_count, attempts) == (1, [1, 0, 0])


def test_refusal_in_one_transaction_does_not_stop_the_next_transaction(agent, portal_tickets):
    """The budget is not shared across separate transactions: each makes its first attempt."""
    with patch.object(mail.EmailMessage, "send", side_effect=ConnectionRefusedError) as transport:
        cases.reply(agent, portal_tickets[0])
        cases.reply(agent, portal_tickets[1])
    attempts = list(Notice.objects.order_by("pk").values_list("attempts", flat=True))
    assert (transport.call_count, attempts) == (2, [1, 1])


def test_two_replies_on_one_ticket_in_one_transaction_make_one_notice(agent, portal_tickets):
    """Two hook calls for one ticket in one transaction make one notice with both entries."""
    ticket = portal_tickets[0]
    with transaction.atomic():
        # The service works on its own locked copy of the ticket, so its hook never sees the
        # caller's instance: deliver the real hook explicitly on one instance.
        with patch.object(notifications, "entry_added"):
            first = cases.reply(agent, ticket)
            second = cases.reply(agent, ticket)
        notifications.entry_added(ticket, first)
        notifications.entry_added(ticket, second)
    notice = Notice.objects.get()
    assert notice.status == SENT
    assert sorted(notice.data["entries"]["reply"]) == sorted([first.pk, second.pk])
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_each_service_call_is_its_own_change_inside_a_test_case(
    agent, portal_tickets, django_capture_on_commit_callbacks
):
    """Inside a non-transactional test every service call is a transaction: two notices."""
    ticket = portal_tickets[0]
    with django_capture_on_commit_callbacks(execute=True):
        cases.reply(agent, ticket)
        cases.reply(agent, ticket)
    assert [n.status for n in Notice.objects.order_by("pk")] == [SENT, SENT]
    assert len(mail.outbox) == 2


# C02: a batch follows savepoints -----------------------------------------------------------------


def start_batch(agent, ticket):
    """Reply without the service's hook; return the entry for the test to deliver itself."""
    with patch.object(notifications, "entry_added"):
        return cases.reply(agent, ticket)


def extra_entry(agent, ticket):
    return start_batch(agent, ticket)


def test_rolled_back_entry_leaves_only_committed_entries_on_the_notice(agent, portal_tickets):
    """An entry added to a recipient's notice in a rolled-back savepoint is not on the sent one."""
    ticket = portal_tickets[0]
    with transaction.atomic():
        kept = start_batch(agent, ticket)
        dropped = extra_entry(agent, ticket)
        notifications.entry_added(ticket, kept)
        with transaction.atomic():
            notifications.entry_added(ticket, dropped)
            transaction.set_rollback(True)
    notice = Notice.objects.get()
    assert notice.status == SENT
    assert notice.data["entries"] == {"reply": [kept.pk]}
    assert notice.events == ["reply"]


def test_rolled_back_event_leaves_only_committed_events_on_the_notice(agent, portal_tickets):
    """An event added to a notice in a rolled-back savepoint is not on the sent notice."""
    ticket = portal_tickets[0]
    with transaction.atomic():
        entry = start_batch(agent, ticket)
        notifications.entry_added(ticket, entry)
        with transaction.atomic():
            notifications.status_changed(
                ticket, old=Status.OPEN, new=Status.WAITING_CLIENT, actor=agent
            )
            transaction.set_rollback(True)
    notice = Notice.objects.get()
    assert notice.status == SENT
    assert notice.events == ["reply"]


def test_recipient_recorded_in_rolled_back_savepoint_can_be_recorded_again(
    agent, portal_tickets, make_user, join
):
    """A new recipient's notice rolled back inside a batch is recorded afresh and then sent."""
    ticket = portal_tickets[0]
    person = make_user("follow_up_technician", "technical")
    join(person, ticket.team)
    with patch.object(notifications, "assigned"):
        services.assign(
            actor=agent,
            request=ticket,
            expected_revision=ticket.revision,
            team=ticket.team,
            assignee=person,
        )
    ticket.refresh_from_db()
    with transaction.atomic():
        entry = start_batch(agent, ticket)
        notifications.entry_added(ticket, entry)
        with transaction.atomic():
            notifications.assigned(ticket, assignee=person, actor=agent)
            assert Notice.objects.filter(recipient=person).exists()
            transaction.set_rollback(True)
        assert not Notice.objects.filter(recipient=person).exists()
        notifications.assigned(ticket, assignee=person, actor=agent)

    staff = Notice.objects.get(recipient=person)
    assert staff.status == SENT
    assert Notice.objects.exclude(recipient=person).get().status == SENT
    assert len(mail.outbox) == 2


def test_next_transaction_after_a_full_rollback_records_and_sends_afresh(agent, portal_tickets):
    """A batch from a transaction that rolled back is not reused by the next change."""
    ticket = portal_tickets[0]
    with pytest.raises(Abandon), transaction.atomic():
        cases.reply(agent, ticket)
        assert Notice.objects.count() == 1
        raise Abandon
    assert Notice.objects.count() == 0 and len(mail.outbox) == 0
    entry = cases.reply(agent, ticket)
    notice = Notice.objects.get()
    assert notice.status == SENT
    assert notice.data["entries"] == {"reply": [entry.pk]}
    assert len(mail.outbox) == 1


# C03: dispatches follow savepoints ---------------------------------------------------------------


def test_released_savepoint_is_sent_and_nested_rolled_back_one_is_not(waiting):
    """Notices of the outer and a released savepoint go out; a rolled-back nested one waits."""
    one, two, three = waiting
    with transaction.atomic():
        outbox.send_after_commit([one.pk])
        with transaction.atomic():
            outbox.send_after_commit([two.pk])
            with transaction.atomic():
                outbox.send_after_commit([three.pk])
                transaction.set_rollback(True)
    assert states(one, two, three) == [(SENT, 1), (SENT, 1), (WAITING, 0)]
    assert len(mail.outbox) == 2


def test_released_savepoint_dispatch_is_dropped_when_the_outer_transaction_rolls_back(waiting):
    """Nothing is sent when the outer transaction rolls back, whatever savepoints were released."""
    one, two, _three = waiting
    with pytest.raises(Abandon), transaction.atomic():
        outbox.send_after_commit([one.pk])
        with transaction.atomic():
            outbox.send_after_commit([two.pk])
        raise Abandon
    assert states(one, two) == [(WAITING, 0), (WAITING, 0)]
    assert len(mail.outbox) == 0


# C04: sending outside a transaction --------------------------------------------------------------


def test_send_after_commit_with_no_transaction_sends_at_once(waiting):
    """With no transaction open the notice is committed already, so it is sent immediately."""
    row = waiting[0]
    outbox.send_after_commit([row.pk])
    assert states(row) == [(SENT, 1)]
    assert len(mail.outbox) == 1


def test_send_after_commit_from_a_robust_commit_callback_sends(waiting):
    """A robust commit callback that asks for a first attempt gets it at once."""
    row = waiting[0]
    with transaction.atomic():
        transaction.on_commit(lambda: outbox.send_after_commit([row.pk]), robust=True)
    assert states(row) == [(SENT, 1)]
    assert len(mail.outbox) == 1


def test_breach_recorder_still_sends_after_commit(team_ticket):
    """The breach sweep records its notice in its transaction and sends it after commit."""
    assert clock_job.notice_breaches()["notices"] == 1
    assert Notice.objects.get(object_id=team_ticket.pk).status == SENT
    assert len(mail.outbox) == 1


# C05: roles are read and held in one statement ---------------------------------------------------


def granting_after_reading(person, group_name, granted):
    """held_roles that reads the real roles, then grants `group_name` from another session."""
    real = roles.held_roles
    group = Group.objects.get_or_create(name=group_name)[0]

    def grant():
        try:
            person.groups.add(group)
            granted.append(True)
        except OperationalError as exc:
            if exc.__cause__.sqlstate != "55P03":
                raise

    def wrapper(user, *args, **kwargs):
        result = real(user, *args, **kwargs)
        if user.pk == person.pk:
            cases.independent(grant)
        return result

    return wrapper


def test_assignment_decides_from_the_reading_not_a_role_granted_after_it(
    agent, category, accessioning, join, make_user
):
    """An assignee without a staff role at the reading is refused, even if one is granted next."""
    person = make_user("follow_up_nobody")
    join(person, accessioning)
    ticket = new_request(agent, category, team=accessioning)
    granted = []
    wrapper = granting_after_reading(person, "technical", granted)
    with patch.object(roles, "held_roles", wrapper), pytest.raises(ValidationError) as caught:
        services.assign(
            actor=agent,
            request=ticket,
            expected_revision=ticket.revision,
            team=accessioning,
            assignee=person,
        )
    assert "assignee" in caught.value.message_dict
    assert granted == [True]


def test_assignment_holds_the_assignees_staff_role_until_it_commits(
    agent, category, accessioning, join, make_user
):
    """Removing the assignee's staff role waits while the assignment is open."""
    person = make_user("follow_up_holder", "technical")
    join(person, accessioning)
    ticket = new_request(agent, category, team=accessioning)
    with transaction.atomic():
        services.assign(
            actor=agent,
            request=ticket,
            expected_revision=ticket.revision,
            team=accessioning,
            assignee=person,
        )
        assert prior.writer_waits(lambda: person.groups.clear())


def test_settings_update_decides_admin_from_the_reading_not_a_later_grant(make_user):
    """An actor whose admin role is granted after the reading is refused."""
    actor = make_user("follow_up_not_admin", "technical")
    granted = []
    wrapper = granting_after_reading(actor, "admin", granted)
    # The first check (require_role) is not the subject: let it pass to reach the held reading.
    with (
        patch("jobs.services.require_role"),
        patch("jobs.services.held_roles", wrapper),
        pytest.raises(PermissionDenied),
    ):
        with transaction.atomic():
            row = locked_settings()
            update_settings(
                actor=actor, expected_revision=row.revision, close_resolved_after_days=9
            )
    assert granted == [True]


def test_settings_update_holds_the_admins_role_until_it_commits(make_user):
    """Removing an administrator's admin membership waits while their update is open."""
    admin = make_user("follow_up_admin", "admin")
    with transaction.atomic():
        row = locked_settings()
        assert update_settings(
            actor=admin, expected_revision=row.revision, close_resolved_after_days=11
        )
        assert prior.writer_waits(lambda: admin.groups.clear())


def test_staff_notice_holds_roles_and_team_membership_but_not_other_assignments(
    agent, category, accessioning, join, make_user
):
    """While a staff notice is handed to the mail server, its roles and team are held."""
    person = make_user("follow_up_assignee", "technical")
    supervisor = make_user("follow_up_supervisor", "supervisor")
    newcomer = make_user("follow_up_newcomer", "technical")
    colleague = make_user("follow_up_colleague", "technical")
    for member in (person, colleague):
        join(member, accessioning)
    ticket = new_request(agent, category, team=accessioning)
    other = new_request(agent, category, team=accessioning)
    waited = []

    def drop_role():
        person.groups.clear()

    def add_member():
        team_services.add_member(actor=supervisor, team=accessioning, user=newcomer)

    def assign_other():
        services.assign(
            actor=agent,
            request=other,
            expected_revision=other.revision,
            team=accessioning,
            assignee=colleague,
        )

    def deliver(message):
        waited.append(
            [prior.writer_waits(probe) for probe in (drop_role, add_member, assign_other)]
        )
        return 1

    with patch.object(mail.EmailMessage, "send", deliver):
        services.assign(
            actor=agent,
            request=ticket,
            expected_revision=ticket.revision,
            team=accessioning,
            assignee=person,
        )
    assert waited == [[True, True, False]]
    assert Notice.objects.get(recipient=person).status == SENT


def test_staff_notice_without_a_role_at_the_reading_is_not_sent_if_one_is_granted_later(
    agent, category, accessioning, join, make_user
):
    """A role granted while the notice is decided does not make a roleless recipient eligible."""
    person = make_user("follow_up_lapsed", "technical")
    join(person, accessioning)
    ticket = new_request(agent, category, team=accessioning)
    with patch.object(outbox, "send_now", return_value={}):
        services.assign(
            actor=agent,
            request=ticket,
            expected_revision=ticket.revision,
            team=accessioning,
            assignee=person,
        )
    notice = Notice.objects.get(recipient=person)
    person.groups.clear()
    granted = []
    wrapper = granting_after_reading(person, "technical", granted)
    with patch.object(roles, "held_roles", wrapper):
        outbox.attempt(notice.pk)
    notice.refresh_from_db()
    assert granted == [True]
    assert (notice.status, notice.not_sent_reason) == (
        NoticeStatus.NOT_SENT,
        NotSentReason.NO_LONGER_APPLIES,
    )
    assert len(mail.outbox) == 0


# C06: the breach sweep starts below every key ----------------------------------------------------


def test_breach_sweep_pages_through_nonpositive_and_positive_ids_once(
    agent, category, accessioning, join, frozen, monkeypatch
):
    """With one clock per page, ids -1, 0 and a positive one are each noticed once in total."""
    ServiceTarget.objects.create(name="Follow-up target", resolution_minutes=60)
    join(agent, accessioning)
    tickets = [new_request(agent, category, team=accessioning, assignee=agent) for _ in range(3)]
    frozen(frozen.now + timedelta(hours=2))
    for ticket, explicit_id in zip(tickets[:2], (-1, 0), strict=True):
        assert RequestClock.objects.filter(request=ticket).update(id=explicit_id) == 1
    monkeypatch.setattr(clock_job, "BREACH_BATCH", 1)

    first = clock_job.notice_breaches()
    assert (first["breaches"], first["notices"]) == (3, 3)
    assert Notice.objects.count() == 3
    assert sorted(Notice.objects.values_list("object_id", flat=True)) == sorted(
        t.pk for t in tickets
    )

    second = clock_job.notice_breaches()
    assert (second["breaches"], second["notices"]) == (0, 0)
    assert Notice.objects.count() == 3


# C07: the reserved account's page ----------------------------------------------------------------


def reserved_page(client, make_user):
    client.force_login(make_user("follow_up_owner", is_superuser=True, is_staff=True))
    response = client.get(reverse("admin:accounts_user_change", args=[system_account().pk]))
    assert response.status_code == 200
    return response.content.decode()


def test_reserved_account_page_has_no_link_to_a_password_form(client, make_user):
    """The jobs' account page links to no password form and offers no password action."""
    html = reserved_page(client, make_user)
    assert not re.search(r'href="[^"]*password/?"', html)
    assert ">Set password</a>" not in html and ">Reset password</a>" not in html
    assert "No password set." in html


def test_reserved_account_form_carries_the_help_text(make_user):
    """The jobs' account's password field says nobody signs in as it and none can be set."""
    form = AccountChangeForm(instance=system_account())
    assert form.fields["password"].help_text == HELP


def test_reserved_account_page_shows_the_help_text(client, make_user):
    """The jobs' account page shows why it has no password."""
    assert HELP in reserved_page(client, make_user)


def test_ordinary_staff_account_still_offers_its_password_link(client, make_user):
    """An owner still reaches an ordinary staff account's password form from its page."""
    client.force_login(make_user("follow_up_owner2", is_superuser=True, is_staff=True))
    member = make_user("follow_up_staff", "technical", is_staff=True)
    response = client.get(reverse("admin:accounts_user_change", args=[member.pk]))
    assert response.status_code == 200
    html = response.content.decode()
    assert re.search(r'href="[^"]*password/?"', html)
    assert ">Reset password</a>" in html or ">Set password</a>" in html
    password_url = reverse("admin:auth_user_password_change", args=[member.pk])
    assert client.get(password_url).status_code == 200
