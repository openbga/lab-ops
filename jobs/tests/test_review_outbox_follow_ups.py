"""Independent review of the outbox follow-ups (C01-C07 fixes). All records are synthetic.

Each test states intended behaviour; findings R01, R02 and R04 are resolved. Adopted from the
review with two changes: the deadlock case R03 (predating this change) is left out and kept as the
acceptance case of its roadmap item, and the team-edit hold follows update_team's lock mode.
"""

import logging
import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import connections, transaction
from django.urls import reverse

from accounts.models import User
from core.models import AuditEvent
from desk import notifications, services
from desk.models import Status
from desk.tests.test_contract import new_request, selection
from jobs import locks, outbox
from jobs.actor import system_account
from jobs.models import JobSettings, Notice, NoticeStatus, NotSentReason
from jobs.services import update_settings
from teams import services as teams
from teams.models import Team, TeamMembership

from . import test_review_outbox_round1_verify as prior
from . import test_review_round1_fixes as cases

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("locmem", "review_database"),
]
review_database = prior.review_database
frozen = cases.frozen
team_ticket = cases.team_ticket
PASSWORD = "Synthetic-review-pass-4821!"


# Fixtures ---------------------------------------------------------------------------------------


@pytest.fixture
def portal_three(clinic_portal, category):
    return [
        new_request(clinic_portal, category, channel="portal", first_entry_kind="reply")
        for _ in range(3)
    ]


@pytest.fixture
def portal_four(clinic_portal, category):
    return [
        new_request(clinic_portal, category, channel="portal", first_entry_kind="reply")
        for _ in range(4)
    ]


@pytest.fixture
def technical():
    return Group.objects.get_or_create(name="technical")[0]


def _assigned_notice(agent, ticket, team, person):
    """Assign `ticket` to `person` with the first attempt suppressed; the committed notice."""
    ticket.refresh_from_db()
    with patch.object(outbox, "send_now", return_value={}):
        services.assign(
            actor=agent,
            request=ticket,
            expected_revision=ticket.revision,
            team=team,
            assignee=person,
        )
    row = Notice.objects.get(recipient=person, topic=notifications.TOPIC)
    assert (row.status, row.attempts) == (NoticeStatus.WAITING, 0)
    return row


@pytest.fixture
def staff_notice(agent, category, accessioning, join, make_user):
    """A waiting assignment notice for a technician of Accessioning."""
    person = make_user("followup_technician", "technical")
    join(person, accessioning)
    ticket = new_request(agent, category, team=accessioning)
    row = _assigned_notice(agent, ticket, accessioning, person)
    return SimpleNamespace(row=row, ticket=ticket, person=person)


@pytest.fixture
def default_team_notice(agent, category, join, make_user):
    """A waiting assignment notice on a ticket of the default team (its category names none)."""
    person = make_user("followup_default_member", "technical")
    ticket = new_request(agent, category)
    team = Team.objects.get(pk=ticket.team_id)
    assert team.is_default
    join(person, team)
    row = _assigned_notice(agent, ticket, team, person)
    return SimpleNamespace(row=row, ticket=ticket, person=person, team=team)


def _refused(message):
    raise ConnectionRefusedError


# 1. Savepoints and commit callbacks --------------------------------------------------------------


def test_three_savepoint_levels_share_one_refusal_and_drop_rolled_back_notices(agent, portal_four):
    kept_a, dropped_b, dropped_c, kept_d = portal_four
    with patch.object(mail.EmailMessage, "send", side_effect=ConnectionRefusedError) as sent:
        with transaction.atomic():
            cases.reply(agent, kept_a)
            with transaction.atomic():
                cases.reply(agent, dropped_b)
                with transaction.atomic():
                    cases.reply(agent, dropped_c)
                transaction.set_rollback(True)
            cases.reply(agent, kept_d)
            assert sent.call_count == 0
    assert sent.call_count == 1
    rows = Notice.objects.order_by("pk")
    assert [row.object_id for row in rows] == [kept_a.pk, kept_d.pk]
    assert [row.attempts for row in rows] == [1, 0]


def test_three_savepoint_levels_send_only_what_committed(agent, portal_four):
    kept_a, dropped_b, kept_c, kept_d = portal_four
    with transaction.atomic():
        cases.reply(agent, kept_a)
        with transaction.atomic():
            cases.reply(agent, dropped_b)
            transaction.set_rollback(True)
        with transaction.atomic():
            with transaction.atomic():
                cases.reply(agent, kept_c)
        cases.reply(agent, kept_d)
    sent = sorted(
        Notice.objects.filter(status=NoticeStatus.SENT).values_list("object_id", flat=True)
    )
    assert sent == sorted([kept_a.pk, kept_c.pk, kept_d.pk])
    assert not Notice.objects.filter(object_id=dropped_b.pk).exists()
    assert len(mail.outbox) == 3


def test_a_batch_begun_in_a_released_savepoint_merges_and_shares_the_budget(
    agent, portal_three, clinic_portal
):
    """Hooks run twice on one ticket instance in one transaction: one notice per person."""
    ticket, other, _unused = portal_three
    with patch.object(notifications, "entry_added"), patch.object(notifications, "status_changed"):
        entry = cases.reply(agent, ticket)
        other_entry = cases.reply(agent, other)
    ticket.refresh_from_db()
    ticket.status = Status.WAITING_CLIENT
    ticket.save(update_fields=["status"])
    with patch.object(mail.EmailMessage, "send", side_effect=ConnectionRefusedError) as sent:
        with transaction.atomic():
            with transaction.atomic():
                notifications.entry_added(ticket, entry)
            notifications.status_changed(
                ticket, old=Status.OPEN, new=Status.WAITING_CLIENT, actor=agent
            )
            notifications.entry_added(other, other_entry)
    assert sent.call_count == 1
    row = Notice.objects.get(object_id=ticket.pk)
    assert row.events == ["reply", "waiting_client"]
    assert Notice.objects.get(object_id=other.pk).attempts == 0


def test_an_addition_rolled_back_in_a_savepoint_is_recorded_again_at_the_outer_level(
    agent, portal_three
):
    ticket = portal_three[0]
    with patch.object(notifications, "entry_added"):
        first = cases.reply(agent, ticket)
        second = cases.reply(agent, ticket)
    ticket.refresh_from_db()
    with transaction.atomic():
        notifications.entry_added(ticket, first)  # the batch begins here, at the outer level
        with transaction.atomic():
            with transaction.atomic():
                notifications.entry_added(ticket, second)
            transaction.set_rollback(True)
        row = Notice.objects.get(object_id=ticket.pk)
        assert row.data["entries"] == {"reply": [first.pk]}
        notifications.entry_added(ticket, second)
    row = Notice.objects.get(object_id=ticket.pk)
    assert row.data["entries"] == {"reply": [first.pk, second.pk]}
    assert row.status == NoticeStatus.SENT


def test_hooks_on_one_instance_in_two_transactions_make_two_notices(agent, portal_three):
    ticket = portal_three[0]
    with patch.object(notifications, "entry_added"):
        first = cases.reply(agent, ticket)
        second = cases.reply(agent, ticket)
    ticket.refresh_from_db()
    with transaction.atomic():
        notifications.entry_added(ticket, first)
    with transaction.atomic():
        notifications.entry_added(ticket, second)
    rows = list(Notice.objects.filter(object_id=ticket.pk).order_by("pk"))
    assert [row.data["entries"]["reply"] for row in rows] == [[first.pk], [second.pk]]
    assert {row.status for row in rows} == {NoticeStatus.SENT}


def test_a_hook_after_a_rolled_back_transaction_starts_a_new_batch(agent, portal_three):
    ticket = portal_three[0]
    with patch.object(notifications, "entry_added"):
        entry = cases.reply(agent, ticket)
    ticket.refresh_from_db()
    with transaction.atomic():
        notifications.entry_added(ticket, entry)
        transaction.set_rollback(True)
    assert not Notice.objects.exists()
    with transaction.atomic():
        notifications.entry_added(ticket, entry)
    row = Notice.objects.get()
    assert (row.status, len(mail.outbox)) == (NoticeStatus.SENT, 1)


@pytest.mark.parametrize("mode", ["refused", "slow"])
def test_bulk_status_change_shares_one_budget(
    mode, agent, portal_three, clinic_portal, frozen, settings
):
    settings.EMAIL_TIMEOUT = 10
    calls = []

    def transport(message):
        calls.append(message.to)
        if mode == "refused":
            raise ConnectionRefusedError
        frozen(frozen.now + timedelta(seconds=settings.EMAIL_TIMEOUT))
        return 1

    with patch.object(mail.EmailMessage, "send", transport):
        changed = services.bulk_status(
            actor=agent, selected=selection(portal_three), status=Status.WAITING_CLIENT
        )
    assert changed == 3
    assert Notice.objects.count() == 3
    assert len(calls) == 1


def test_two_transactions_never_share_one_budget(agent, portal_three):
    first, second, _ = portal_three
    with patch.object(mail.EmailMessage, "send", side_effect=ConnectionRefusedError) as sent:
        with transaction.atomic():
            cases.reply(agent, first)
        with transaction.atomic():
            cases.reply(agent, second)
    assert sent.call_count == 2
    assert list(Notice.objects.order_by("pk").values_list("attempts", flat=True)) == [1, 1]


@pytest.mark.django_db(transaction=False)
@pytest.mark.parametrize("wrapped", [False, True], ids=["two-changes", "one-test-transaction"])
def test_the_testcase_rule_counts_changes_as_production_does(
    wrapped, agent, portal_three, django_capture_on_commit_callbacks
):
    """Inside Django's TestCase each service call counts as its own transaction (as it is in
    production), and a block the test opens itself counts as one."""
    first, second, _ = portal_three
    with (
        patch.object(mail.EmailMessage, "send", side_effect=ConnectionRefusedError) as sent,
        django_capture_on_commit_callbacks(execute=True),
    ):
        if wrapped:
            with transaction.atomic():
                cases.reply(agent, first)
                cases.reply(agent, second)
        else:
            cases.reply(agent, first)
            cases.reply(agent, second)
    assert sent.call_count == (1 if wrapped else 2)


def _lock_unavailable():
    from django.db import OperationalError

    class Cause(Exception):
        sqlstate = locks.LOCK_NOT_AVAILABLE

    try:
        raise Cause
    except Cause as cause:
        raise OperationalError("lock not available") from cause


def test_a_breach_recording_that_rolls_back_after_dispatch_sends_nothing(agent, team_ticket):
    """The recorder's rollback takes its notices and their first attempt with it."""
    from service_levels import jobs as clock_job

    real = outbox.send_after_commit

    def dispatch_then_fail(ids):
        real(ids)
        _lock_unavailable()  # as a NOWAIT lock would, after the dispatch was registered

    with patch.object(outbox, "send_after_commit", dispatch_then_fail):
        counts = clock_job.notice_breaches()
    assert counts.get("busy") == 1 and counts["notices"] == 0
    assert not Notice.objects.exists() and not mail.outbox


def test_send_after_commit_in_autocommit_sends_at_once(agent, portal_three):
    ticket = portal_three[0]
    with patch.object(outbox, "send_now", return_value={}):
        cases.reply(agent, ticket)
    row = Notice.objects.get()
    outbox.send_after_commit([row.pk])
    row.refresh_from_db()
    assert (row.status, len(mail.outbox)) == (NoticeStatus.SENT, 1)


def test_send_after_commit_from_inside_an_attempt_runs_after_that_attempt(agent, portal_three):
    first_ticket, second_ticket, _ = portal_three
    with patch.object(outbox, "send_now", return_value={}):
        cases.reply(agent, first_ticket)
        cases.reply(agent, second_ticket)
    first, second = Notice.objects.order_by("pk")
    real = notifications.prepare_request_notice
    seen = []

    def handler(notice):
        if notice.pk == first.pk:
            outbox.send_after_commit([second.pk])
            seen.append(Notice.objects.get(pk=second.pk).status)  # not yet attempted
        return real(notice)

    with patch.object(outbox, "notice_handler", return_value=handler):
        assert outbox.attempt(first.pk) == "sent"
    second.refresh_from_db()
    assert seen == [NoticeStatus.WAITING]
    assert (second.status, len(mail.outbox)) == (NoticeStatus.SENT, 2)


def test_an_error_while_dispatching_never_reaches_the_save(agent, portal_three, caplog):
    ticket = portal_three[0]
    with (
        patch.object(outbox, "attempt", side_effect=RuntimeError("synthetic secret text")),
        caplog.at_level(logging.WARNING, logger="lab_ops.notifications"),
    ):
        entry = cases.reply(agent, ticket)
    assert entry.pk
    row = Notice.objects.get()
    assert (row.status, row.attempts) == (NoticeStatus.WAITING, 0)
    assert "synthetic secret text" not in caplog.text
    assert "RuntimeError" in caplog.text


# 2. Recipients at each attempt, from the held reading of roles ------------------------------------


@pytest.mark.parametrize(
    "roles, outcome",
    [
        (("technical",), "not_sent"),
        (("supervisor",), "sent"),
        (("admin",), "sent"),
        ((), "not_sent"),
    ],
    ids=["technical", "supervisor", "admin", "no-role"],
)
def test_a_restricted_ticket_notice_follows_the_held_roles_and_membership(
    roles, outcome, privacy_category, quality, join, make_user
):
    supervisor = make_user("followup_supervisor", "supervisor")
    person = make_user("followup_restricted", "technical", *roles)
    membership = join(person, quality)
    ticket = new_request(supervisor, privacy_category)
    assert ticket.team_id == quality.pk
    row = _assigned_notice(supervisor, ticket, quality, person)
    # Membership ended outside the services (no release) and only the listed roles remain.
    TeamMembership.objects.filter(pk=membership.pk).delete()
    User.groups.through.objects.filter(user=person).exclude(group__name__in=roles).delete()
    assert outbox.attempt(row.pk) == outcome
    assert len(mail.outbox) == (1 if outcome == "sent" else 0)


def test_a_role_granted_after_the_attempts_reading_does_not_count(staff_notice, technical):
    person = staff_notice.person
    person.groups.clear()  # committed: no staff role now
    real = locks.hold_group_names
    granted = []

    def reading_then_grant(user_id, **kwargs):
        names = real(user_id, **kwargs)
        if user_id == person.pk and not granted:
            granted.append(cases.independent(lambda: person.groups.add(technical) or "granted"))
        return names

    with patch.object(locks, "hold_group_names", reading_then_grant):
        assert outbox.attempt(staff_notice.row.pk) == "not_sent"
    assert granted == ["granted"]
    staff_notice.row.refresh_from_db()
    assert staff_notice.row.not_sent_reason == NotSentReason.NO_LONGER_APPLIES
    assert not mail.outbox


def test_removing_the_role_waits_for_a_staff_notice_being_sent(staff_notice, technical):
    person = staff_notice.person
    waited = []

    def transport(message):
        waited.append(prior.writer_waits(lambda: person.groups.remove(technical)))
        return 1

    with patch.object(mail.EmailMessage, "send", transport):
        assert outbox.attempt(staff_notice.row.pk) == "sent"
    assert waited == [True]


@pytest.mark.parametrize("who", ["deactivated", "opted_out", "jobs_account"])
def test_staff_notice_is_not_sent_to_people_who_may_not_receive_it(
    who, staff_notice, agent, accessioning
):
    from core.models import UserPreference

    person = staff_notice.person
    row = staff_notice.row
    if who == "deactivated":
        User.objects.filter(pk=person.pk).update(is_active=False)
    elif who == "opted_out":
        UserPreference.objects.create(user=person, key=notifications.OPT_OUT_KEY, value=False)
    else:
        actor = system_account()
        actor.groups.add(Group.objects.get(name="technical"))
        User.objects.filter(pk=actor.pk).update(is_active=True)
        row = Notice.objects.create(
            recipient=actor,
            topic=notifications.TOPIC,
            object_id=staff_notice.ticket.pk,
            reference=staff_notice.ticket.number,
            events=["assigned"],
            data={"portal": False, "entries": {}},
            next_attempt_at=row.next_attempt_at,
        )
        services.ServiceRequest.objects.filter(pk=staff_notice.ticket.pk).update(assignee=actor)
    assert outbox.attempt(row.pk) == "not_sent"
    assert not mail.outbox


# 3. A decision and its write ----------------------------------------------------------------------


@pytest.fixture
def roleless_member(accessioning, join, make_user):
    person = make_user("followup_new_technician")
    join(person, accessioning)
    return person


def _assign(agent, ticket, team, person):
    ticket.refresh_from_db()
    return services.assign(
        actor=agent,
        request=ticket,
        expected_revision=ticket.revision,
        team=team,
        assignee=person,
    )


def test_assignment_refuses_a_role_granted_after_its_reading(
    agent, category, accessioning, roleless_member, technical
):
    ticket = new_request(agent, category, team=accessioning)
    real = locks.hold_group_names
    granted = []

    def reading_then_grant(user_id, **kwargs):
        names = real(user_id, **kwargs)
        if user_id == roleless_member.pk and not granted:
            granted.append(
                cases.independent(lambda: roleless_member.groups.add(technical) or "granted")
            )
        return names

    with patch.object(locks, "hold_group_names", reading_then_grant):
        with pytest.raises(ValidationError) as refused:
            _assign(agent, ticket, accessioning, roleless_member)
    assert "assignee" in refused.value.message_dict
    assert granted == ["granted"]
    ticket.refresh_from_db()
    assert ticket.assignee_id is None


def test_assignment_holds_a_role_granted_before_its_reading(
    agent, category, accessioning, roleless_member, technical
):
    ticket = new_request(agent, category, team=accessioning)
    real = locks.hold_group_names
    granted = []

    def grant_then_reading(user_id, **kwargs):
        if user_id == roleless_member.pk and not granted:
            granted.append(
                cases.independent(lambda: roleless_member.groups.add(technical) or "granted")
            )
        return real(user_id, **kwargs)

    with patch.object(locks, "hold_group_names", grant_then_reading), transaction.atomic():
        _assign(agent, ticket, accessioning, roleless_member)
        assert prior.writer_waits(lambda: roleless_member.groups.remove(technical))
    assert granted == ["granted"]


def test_assignment_holds_a_role_against_removal_after_its_reading(
    agent, category, accessioning, join, make_user, technical
):
    person = make_user("followup_held_technician", "technical")
    join(person, accessioning)
    ticket = new_request(agent, category, team=accessioning)
    real = locks.hold_group_names
    waited = []

    def reading_then_remove(user_id, **kwargs):
        names = real(user_id, **kwargs)
        if user_id == person.pk and not waited:
            waited.append(prior.writer_waits(lambda: person.groups.remove(technical)))
        return names

    with patch.object(locks, "hold_group_names", reading_then_remove):
        _assign(agent, ticket, accessioning, person)
    assert waited == [True]
    ticket.refresh_from_db()
    assert ticket.assignee_id == person.pk


@pytest.mark.parametrize("change", ["remove", "repoint"])
def test_settings_write_holds_the_admin_role_against_removal(change, monkeypatch, make_user):
    admin = make_user("followup_admin", "admin")
    group = Group.objects.get(name="admin")
    other = Group.objects.get_or_create(name="technical")[0]
    membership = User.groups.through.objects.filter(user=admin, group=group)
    original = JobSettings.save
    waited = []

    def write():
        if change == "remove":
            admin.groups.remove(group)
        else:  # the same row made to point at another role
            membership.update(group=other)

    def save(row, *args, **kwargs):
        if row.close_resolved_after_days is not None:
            waited.append(prior.writer_waits(write))
        return original(row, *args, **kwargs)

    monkeypatch.setattr(JobSettings, "save", save)
    assert update_settings(actor=admin, expected_revision=1, close_resolved_after_days=7)
    assert waited == [True]


def test_a_staff_notice_lets_an_assignment_and_portal_mail_in_its_team_go_on(
    staff_notice, agent, category, accessioning, join, make_user, clinic_portal
):
    colleague = make_user("followup_colleague", "technical")
    join(colleague, accessioning)
    other = new_request(agent, category, team=accessioning)
    portal_other = new_request(clinic_portal, category, channel="portal", first_entry_kind="reply")
    services.ServiceRequest.objects.filter(pk=portal_other.pk).update(team=accessioning)
    with transaction.atomic():
        assert notifications.prepare_request_notice(staff_notice.row) is not None
        assert not prior.writer_waits(lambda: _assign(agent, other, accessioning, colleague))
        assert not prior.writer_waits(lambda: cases.reply(agent, portal_other))


def test_a_staff_notice_holds_its_team_against_a_new_member(staff_notice, accessioning, make_user):
    supervisor = make_user("followup_supervisor", "supervisor")
    newcomer = make_user("followup_newcomer", "technical")
    with transaction.atomic():
        assert notifications.prepare_request_notice(staff_notice.row) is not None
        assert prior.writer_waits(
            lambda: teams.add_member(actor=supervisor, team=accessioning, user=newcomer)
        )


def test_a_staff_notice_holds_its_recipients_membership_but_not_other_leads(
    staff_notice, accessioning, join, make_user
):
    supervisor = make_user("followup_lead_supervisor", "supervisor")
    colleague = make_user("followup_lead_colleague", "technical")
    other = join(colleague, accessioning)
    own = TeamMembership.objects.get(team=accessioning, user=staff_notice.person)
    with transaction.atomic():
        assert notifications.prepare_request_notice(staff_notice.row) is not None
        assert prior.writer_waits(lambda: teams.remove_member(actor=supervisor, membership=own))
        assert not prior.writer_waits(
            lambda: teams.set_lead(actor=supervisor, membership=other, is_lead=True)
        )


@pytest.mark.parametrize("edit", ["description", "new-default-team"])
def test_a_staff_notice_does_not_hold_up_editing_its_team(edit, default_team_notice, make_user):
    """Documented: the team is held only against members and leads changing."""
    admin = make_user("followup_team_admin", "admin")
    team = default_team_notice.team

    def write():
        if edit == "description":
            teams.update_team(
                actor=admin,
                team_id=team.pk,
                expected_revision=Team.objects.get(pk=team.pk).revision,
                reason="Synthetic edit",
                description="Synthetic description",
            )
        else:
            teams.create_team(actor=admin, name="Synthetic new default", is_default=True)

    with transaction.atomic():
        assert notifications.prepare_request_notice(default_team_notice.row) is not None
        assert not prior.writer_waits(write), "the team edit waited for the notice being sent"


def test_a_team_being_edited_does_not_make_a_staff_notice_busy(staff_notice, accessioning):
    """A team edit holds the team as update_team does (FOR NO KEY UPDATE: no key changes); the
    notice's decision does not rest on the team's name, description, active flag or default flag.
    Adapted on adoption: the review held the team FOR UPDATE, as update_team did then."""
    locked, release = threading.Event(), threading.Event()

    def edit_in_progress():
        try:
            with transaction.atomic():
                Team.objects.select_for_update(no_key=True).get(pk=accessioning.pk)
                locked.set()
                release.wait(timeout=30)
        finally:
            connections.close_all()

    thread = threading.Thread(target=edit_in_progress)
    thread.start()
    try:
        assert locked.wait(timeout=20)
        assert outbox.attempt(staff_notice.row.pk) == "sent"
    finally:
        release.set()
        thread.join(timeout=20)


def test_a_member_being_added_makes_a_staff_notice_wait(staff_notice, accessioning):
    """Adding a member locks the team FOR UPDATE (teams.services.add_member): the notice, which
    holds the team against members changing, is left for a minute later."""
    with cases.held_by_another_session(accessioning):
        assert outbox.attempt(staff_notice.row.pk) == "busy"


# 4. Account administration pages -----------------------------------------------------------------


@pytest.fixture
def owner(make_user):
    return make_user("followup_owner", is_superuser=True, is_staff=True)


def test_an_ordinary_account_page_still_offers_its_password_action(client, owner, make_user):
    person = make_user("followup_person", "technical")
    client.force_login(owner)
    html = client.get(reverse("admin:accounts_user_change", args=[person.pk])).content.decode()
    assert ">Reset password</a>" in html
    page = reverse("admin:auth_user_password_change", args=[person.pk])
    assert client.get(page).status_code == 200
    response = client.post(
        page, {"usable_password": "true", "password1": PASSWORD, "password2": PASSWORD}
    )
    assert response.status_code == 302
    person.refresh_from_db()
    assert person.check_password(PASSWORD)
    event = AuditEvent.objects.get(action="account.password_changed")
    assert PASSWORD not in str(event.__dict__)


def test_the_jobs_account_page_shows_status_without_the_password_form(client, owner):
    actor = system_account()
    client.force_login(owner)
    response = client.get(reverse("admin:accounts_user_change", args=[actor.pk]))
    html = response.content.decode()
    assert response.status_code == 200
    assert "No password set." in html
    assert "../password/" not in html and ">Set password<" not in html
    page = reverse("admin:auth_user_password_change", args=[actor.pk])
    before = User.objects.get(pk=actor.pk).password
    assert client.get(page).status_code == 403
    response = client.post(page, {"password1": PASSWORD, "password2": PASSWORD})
    assert response.status_code == 403
    assert User.objects.get(pk=actor.pk).password == before
    assert not AuditEvent.objects.filter(action="account.password_changed").exists()


def test_the_jobs_account_page_says_why_no_password_can_be_set(client, owner):
    """accounts/admin.py AccountChangeForm sets this help text for the reserved account."""
    actor = system_account()
    client.force_login(owner)
    response = client.get(reverse("admin:accounts_user_change", args=[actor.pk]))
    assert "Nobody signs in as this account" in response.content.decode()


def test_a_superuser_without_staff_status_cannot_open_the_jobs_account(client, make_user):
    actor = system_account()
    superuser = make_user("followup_superuser", is_superuser=True, is_staff=False)
    client.force_login(superuser)
    for name in ("admin:accounts_user_change", "admin:auth_user_password_change"):
        response = client.get(reverse(name, args=[actor.pk]))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("admin:login"))
    response = client.post(
        reverse("admin:auth_user_password_change", args=[actor.pk]),
        {"password1": PASSWORD, "password2": PASSWORD},
    )
    assert response.status_code == 302
    assert not User.objects.get(pk=actor.pk).has_usable_password()


# 5. Sensitive data -------------------------------------------------------------------------------


def test_a_refused_first_attempt_keeps_addresses_out_of_rows_and_logs(
    agent, portal_three, clinic_portal, caplog
):
    with (
        patch.object(mail.EmailMessage, "send", side_effect=ConnectionRefusedError("smtp says x")),
        caplog.at_level(logging.INFO),
    ):
        services.bulk_status(
            actor=agent, selected=selection(portal_three), status=Status.WAITING_CLIENT
        )
    for row in Notice.objects.all():
        assert set(row.data) == {"portal", "entries"}
        assert clinic_portal.email not in str(row.__dict__)
    assert clinic_portal.email not in caplog.text
    assert "smtp says x" not in caplog.text
