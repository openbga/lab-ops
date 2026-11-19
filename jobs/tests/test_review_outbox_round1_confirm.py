"""Third independent check of the scheduled-jobs fixes. All records are synthetic."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import OperationalError, transaction
from django.urls import reverse
from django.utils import timezone

from accounts import roles
from accounts.models import User
from desk import notifications, services
from desk.tests.test_contract import new_request
from jobs import outbox
from jobs.actor import system_account
from jobs.models import Notice, NoticeStatus
from service_levels import jobs as clock_job
from service_levels.models import RequestClock

from . import test_review_outbox_round1_verify as prior
from . import test_review_round1_fixes as cases

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("locmem", "review_database"),
]
review_database = prior.review_database
team_ticket = cases.team_ticket
frozen = cases.frozen


@pytest.fixture
def portal_pair(clinic_portal, category):
    return [
        new_request(clinic_portal, category, channel="portal", first_entry_kind="reply")
        for _ in range(2)
    ]


@pytest.fixture
def waiting_pair(agent, portal_pair):
    # Model a caller scheduling already-recorded work. Suppress only the setup dispatch.
    with patch.object(outbox, "send_now", return_value={}):
        for ticket in portal_pair:
            cases.reply(agent, ticket)
    rows = list(Notice.objects.order_by("pk"))
    assert len(rows) == 2
    assert all(row.status == NoticeStatus.WAITING and row.attempts == 0 for row in rows)
    return rows


def test_sibling_reply_services_share_the_first_refusal(agent, portal_pair):
    with patch.object(mail.EmailMessage, "send", side_effect=ConnectionRefusedError) as transport:
        with transaction.atomic():
            for ticket in portal_pair:
                cases.reply(agent, ticket)
            assert transport.call_count == 0

    attempts = list(Notice.objects.order_by("pk").values_list("attempts", flat=True))
    assert (transport.call_count, attempts) == (1, [1, 0])


def test_sibling_reply_services_share_one_timeout(agent, portal_pair, settings, frozen):
    settings.EMAIL_TIMEOUT = 10

    def slow_delivery(message):
        frozen(frozen.now + timedelta(seconds=settings.EMAIL_TIMEOUT))
        return 1

    with patch.object(mail.EmailMessage, "send", slow_delivery), transaction.atomic():
        for ticket in portal_pair:
            cases.reply(agent, ticket)

    statuses = list(Notice.objects.order_by("pk").values_list("status", flat=True))
    assert statuses == [NoticeStatus.SENT, NoticeStatus.WAITING]


def test_rolled_back_dispatch_leaves_its_committed_notice_waiting(waiting_pair):
    outer, inner = waiting_pair
    with transaction.atomic():
        outbox.send_after_commit([outer.pk])
        with transaction.atomic():
            outbox.send_after_commit([inner.pk])
            transaction.set_rollback(True)

    outer.refresh_from_db()
    inner.refresh_from_db()
    assert (outer.status, outer.attempts) == (NoticeStatus.SENT, 1)
    assert (inner.status, inner.attempts, len(mail.outbox)) == (NoticeStatus.WAITING, 0, 1)


def test_reply_hook_can_start_again_on_same_ticket_after_savepoint_rollback(agent, portal_pair):
    first, second = portal_pair
    with transaction.atomic():
        # Deliver the service's real hooks explicitly to reuse the same ticket instance.
        with patch.object(notifications, "entry_added"):
            first_entry = cases.reply(agent, first)
            second_entry = cases.reply(agent, second)
        notifications.entry_added(first, first_entry)
        with transaction.atomic():
            notifications.entry_added(second, second_entry)
            transaction.set_rollback(True)
        assert not Notice.objects.filter(object_id=second.pk).exists()
        notifications.entry_added(second, second_entry)

    sent = Notice.objects.filter(status=NoticeStatus.SENT).values_list("object_id", flat=True)
    assert sorted(sent) == sorted([first.pk, second.pk])
    assert len(mail.outbox) == 2


def test_dispatch_registered_at_commit_attempts_an_already_committed_notice(waiting_pair):
    row, _other = waiting_pair
    with transaction.atomic():
        transaction.on_commit(lambda: outbox.send_after_commit([row.pk]))

    row.refresh_from_db()
    assert (row.status, row.attempts, len(mail.outbox)) == (NoticeStatus.SENT, 1, 1)


@pytest.mark.parametrize("explicit_id", [0, -1])
def test_breach_sweep_includes_clocks_restored_with_explicit_ids(team_ticket, explicit_id):
    assert RequestClock.objects.filter(request=team_ticket).update(id=explicit_id) == 1
    due = RequestClock.objects.filter(clock_job.breached_filter(timezone.now()))
    assert due.get().pk == explicit_id

    counts = clock_job.notice_breaches()
    clock = RequestClock.objects.get(pk=explicit_id)
    assert (counts["breaches"], counts["notices"]) == (1, 1)
    assert clock.resolution_breach_noticed_at is not None
    assert Notice.objects.get(object_id=team_ticket.pk).status == NoticeStatus.SENT


def test_view_only_jobs_account_offers_no_set_password_action(client, make_user):
    owner = make_user("confirm_owner", is_superuser=True, is_staff=True)
    client.force_login(owner)
    actor = system_account()
    response = client.get(reverse("admin:accounts_user_change", args=[actor.pk]))
    assert response.status_code == 200
    assert not response.context["has_change_permission"]
    # The normal password widget describes an unusable password to the operator.
    html = response.content.decode()
    assert "No password set." in html
    password_url = reverse("admin:auth_user_password_change", args=[actor.pk])
    assert client.get(password_url).status_code == 403
    assert ">Set password</a>" not in html


def test_assignment_holds_its_qualifying_role_when_granted_during_validation(
    agent, category, accessioning, join, make_user
):
    """A role grant is legitimate. If assignment accepts it, that grant must be held."""
    person = make_user("confirm_new_technician")
    group = Group.objects.get_or_create(name="technical")[0]
    join(person, accessioning)
    ticket = new_request(agent, category, team=accessioning)
    real_has_role = roles.has_role

    def check(user, *allowed):
        if user.pk == person.pk:
            # A validator that serializes grants can decline on the earlier reading.
            cases.independent(grant_role)
        return real_has_role(user, *allowed)

    def grant_role():
        try:
            person.groups.add(group)
        except OperationalError as exc:
            if exc.__cause__.sqlstate != "55P03":
                raise

    with transaction.atomic(), patch.object(roles, "has_role", side_effect=check):
        try:
            services.assign(
                actor=agent,
                request=ticket,
                expected_revision=ticket.revision,
                team=accessioning,
                assignee=person,
            )
        except ValidationError as exc:
            assert "assignee" in exc.message_dict
            return
        # Probe the one qualifying link using a read-only row lock. The second connection
        # rolls back without changing the role or assignment.
        membership = User.groups.through.objects.filter(user=person, group=group)
        assert membership.exists()
        assert prior.writer_waits(lambda: membership.select_for_update().get())


def test_unrelated_assignment_can_finish_during_breach_recording_and_delivery(
    agent, team_ticket, category, accessioning, join, make_user
):
    """Check both uses of key-share mode against the ordinary desk assignment service."""
    colleague = make_user("confirm_colleague", "technical")
    join(colleague, accessioning)
    other_ticket = new_request(agent, category, team=accessioning)
    audience = notifications.breach_recipients
    waited = []

    def assign_other_ticket():
        services.assign(
            actor=agent,
            request=other_ticket,
            expected_revision=other_ticket.revision,
            team=accessioning,
            assignee=colleague,
        )

    def choose_recipients(request):
        result = audience(request)
        waited.append(("record", prior.writer_waits(assign_other_ticket)))
        return result

    def deliver(message):
        waited.append(("deliver", prior.writer_waits(assign_other_ticket)))
        return 1

    with (
        patch.object(notifications, "breach_recipients", choose_recipients),
        patch.object(mail.EmailMessage, "send", deliver),
    ):
        assert clock_job.notice_breaches()["notices"] == 1
    assert waited == [("record", False), ("deliver", False)]
