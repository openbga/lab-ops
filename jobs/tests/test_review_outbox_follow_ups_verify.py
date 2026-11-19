"""Verification of the outbox follow-ups round 1 fixes (R01, R02) at f6ad30a, and the cases next
to them. All records are synthetic.

Each test states intended behaviour. A failing test is a finding in
.local/review-history/outbox-follow-ups-round-1-verification.md; a passing one is a checked case.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction

from accounts.models import User
from desk import notifications, services
from desk.tests.test_contract import new_request
from jobs import locks, outbox
from jobs.models import JobSettings, Notice, NoticeStatus
from jobs.services import update_settings
from service_levels import jobs as clock_job
from teams import services as teams
from teams.models import Team, TeamMembership, ensure_default_team

from . import test_review_outbox_round1_verify as prior
from . import test_review_round1_fixes as cases

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("locmem", "review_database"),
]
review_database = prior.review_database
frozen = cases.frozen
team_ticket = cases.team_ticket


# Helpers ----------------------------------------------------------------------------------------


@contextmanager
def held_open(work):
    """Run `work()` in a transaction on another connection and keep that transaction open (its
    locks held) until the block ends; it then commits. Yields what `work` returned."""
    ready, release, box = threading.Event(), threading.Event(), {}

    def run():
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '5s'")
                box["result"] = work()
                ready.set()
                release.wait(timeout=30)
        except Exception as exc:  # reported to the test below
            box["error"] = exc
            ready.set()
        finally:
            connections.close_all()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert ready.wait(timeout=20)
        if "error" in box:
            raise box["error"]
        yield box.get("result")
    finally:
        release.set()
        thread.join(timeout=20)


def _waiting_locks():
    with connection.cursor() as cursor:
        # pg_stat_activity is read once per transaction: without this, a session that connected
        # after the first reading in this transaction is never seen.
        cursor.execute("SELECT pg_stat_clear_snapshot()")
        cursor.execute(
            "SELECT count(*) FROM pg_locks WHERE NOT granted AND pid IN "
            "(SELECT pid FROM pg_stat_activity WHERE datname = current_database())"
        )
        return cursor.fetchone()[0]


def _until_someone_waits(seconds=8):
    deadline = time.monotonic() + seconds
    while _waiting_locks() == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    return _waiting_locks() > 0


def _background(work):
    """Start `work()` on another connection (lock waits up to 8 s); returns its future."""

    def run():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '8s'")
            return work()
        finally:
            connections.close_all()

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(run)
    pool.shutdown(wait=False)
    return future


@pytest.fixture
def admin(make_user):
    return make_user("verify_team_admin", "admin")


@pytest.fixture
def supervisor(make_user):
    return make_user("verify_team_supervisor", "supervisor")


def edit_team(admin, team, **changes):
    """teams.update_team as the team page saves it."""
    changes = changes or {"description": "Synthetic description"}
    return teams.update_team(
        actor=admin,
        team_id=team.pk,
        expected_revision=Team.objects.get(pk=team.pk).revision,
        reason="Synthetic edit",
        **changes,
    )


def _assign(agent, ticket, team, person):
    ticket.refresh_from_db()
    return services.assign(
        actor=agent, request=ticket, expected_revision=ticket.revision, team=team, assignee=person
    )


@pytest.fixture
def technician(accessioning, join, make_user):
    person = make_user("verify_technician", "technical")
    join(person, accessioning)
    return person


# R01: a team edit and what holds a team against new members ------------------------------------


def test_a_staff_notice_is_sent_while_the_real_team_edit_is_open(
    agent, category, accessioning, technician, admin
):
    ticket = new_request(agent, category, team=accessioning)
    with patch.object(outbox, "send_now", return_value={}):
        _assign(agent, ticket, accessioning, technician)
    row = Notice.objects.get(recipient=technician)
    with held_open(lambda: edit_team(admin, accessioning, name="Accessioning and intake")):
        assert outbox.attempt(row.pk) == "sent"


def test_breach_recording_during_a_team_edit_records_and_sends(team_ticket, accessioning, admin):
    with held_open(lambda: edit_team(admin, accessioning)):
        counts = clock_job.notice_breaches()
    assert (counts["notices"], counts.get("busy", 0)) == (1, 0)
    assert Notice.objects.get(topic=notifications.BREACH_TOPIC).status == NoticeStatus.SENT


def test_a_team_edit_does_not_wait_for_breach_recording(team_ticket, accessioning, admin):
    audience = notifications.breach_recipients
    waited = []

    def choose(request):
        result = audience(request)
        waited.append(prior.writer_waits(lambda: edit_team(admin, accessioning)))
        return result

    with patch.object(notifications, "breach_recipients", choose):
        assert clock_job.notice_breaches()["notices"] == 1
    assert waited == [False]


def test_a_breach_notice_is_sent_while_its_team_is_edited(team_ticket, accessioning, admin):
    with patch.object(outbox, "send_now", return_value={}):
        clock_job.notice_breaches()
    row = Notice.objects.get(topic=notifications.BREACH_TOPIC)
    with held_open(lambda: edit_team(admin, accessioning)):
        assert outbox.attempt(row.pk) == "sent"


@pytest.mark.parametrize("first", ["edit", "new-member"])
def test_a_team_edit_and_a_new_member_still_take_turns(
    first, accessioning, admin, supervisor, make_user
):
    newcomer = make_user("verify_newcomer", "technical")

    def add():
        return teams.add_member(actor=supervisor, team=accessioning, user=newcomer)

    def edit():
        return edit_team(admin, accessioning)

    holder, other = (edit, add) if first == "edit" else (add, edit)
    with held_open(holder):
        assert prior.writer_waits(other)


def test_a_breach_notice_still_holds_its_team_against_a_new_member(
    team_ticket, accessioning, supervisor, make_user
):
    with patch.object(outbox, "send_now", return_value={}):
        clock_job.notice_breaches()
    row = Notice.objects.get(topic=notifications.BREACH_TOPIC)
    newcomer = make_user("verify_breach_newcomer", "technical")
    with transaction.atomic():
        assert notifications.prepare_breach_notice(row) is not None
        assert prior.writer_waits(
            lambda: teams.add_member(actor=supervisor, team=accessioning, user=newcomer)
        )


@pytest.mark.parametrize("first", ["assignment", "deactivation"])
def test_team_deactivation_and_assignment_into_it_take_turns(
    first, agent, category, accessioning, technician, admin
):
    ticket = new_request(agent, category, team=accessioning)
    if first == "assignment":
        with transaction.atomic():
            _assign(agent, ticket, accessioning, technician)
            assert prior.writer_waits(lambda: edit_team(admin, accessioning, is_active=False))
        return
    with held_open(lambda: edit_team(admin, accessioning, is_active=False)):
        assert prior.writer_waits(lambda: _assign(agent, ticket, accessioning, technician))
    with pytest.raises(ValidationError) as refused:
        _assign(agent, ticket, accessioning, technician)
    assert "team" in refused.value.message_dict


def test_ticket_creation_into_a_team_being_deactivated_waits_then_is_refused(
    agent, category, accessioning, admin
):
    with held_open(lambda: edit_team(admin, accessioning, is_active=False)):
        assert prior.writer_waits(lambda: new_request(agent, category, team=accessioning))
    with pytest.raises(ValidationError) as refused:
        new_request(agent, category, team=accessioning)
    assert "team" in refused.value.message_dict


@pytest.mark.parametrize("move", ["create_team", "update_team"])
def test_a_ticket_created_while_the_default_team_moves_goes_to_the_new_default(
    move, agent, category, accessioning, admin
):
    """desk.services._team_and_assignee: "from current state: ... else the default team". A
    creation that waited for the move decides on what the move committed."""
    old_default = ensure_default_team()
    assert category.default_team_id is None

    def move_default():
        if move == "create_team":
            return teams.create_team(actor=admin, name="Verify new default", is_default=True).pk
        edit_team(admin, accessioning, is_default=True)
        return accessioning.pk

    with held_open(move_default) as new_default_id:
        created = _background(lambda: new_request(agent, category).team_id)
        assert _until_someone_waits(), "the creation did not wait for the move"
    team_id = created.result(timeout=20)
    assert Team.objects.get(is_default=True).pk == new_default_id != old_default.pk
    assert team_id == new_default_id, "the ticket went to the team that is no longer the default"


# R02: the lock mode at each held_roles call site -------------------------------------------------


def test_each_role_reading_holds_the_roles_in_its_documented_mode(
    agent, category, accessioning, technician, make_user, join
):
    real = locks.hold_group_names
    calls = []

    def record(user_id, **kwargs):
        calls.append((user_id, kwargs.get("mode", "share"), kwargs.get("nowait", False)))
        return real(user_id, **kwargs)

    admin_actor = make_user("verify_settings_admin", "admin")
    ticket = new_request(agent, category, team=accessioning)
    with patch.object(locks, "hold_group_names", record):
        _assign(agent, ticket, accessioning, technician)  # assignment, then its first attempt
        update_settings(actor=admin_actor, expected_revision=1, close_resolved_after_days=5)
    assert calls == [
        (agent.pk, "key share", False),  # accounts.acting.hold_actor: the acting account
        (technician.pk, "key share", False),  # desk.services._current_assignee
        (technician.pk, "key share", True),  # desk.notifications._hold (first attempt)
        (admin_actor.pk, "share", False),  # jobs.services.update_settings
    ]
    assert Notice.objects.get(recipient=technician).status == NoticeStatus.SENT


def test_the_settings_write_does_not_hold_up_a_notice_to_its_administrator(
    agent, category, accessioning, join, make_user, monkeypatch
):
    """The settings write holds the administrator's roles FOR SHARE; a notice being decided for
    the same person reads them FOR KEY SHARE, which goes along with it."""
    admin_actor = make_user("verify_assigned_admin", "admin")
    join(admin_actor, accessioning)
    ticket = new_request(agent, category, team=accessioning)
    with patch.object(outbox, "send_now", return_value={}):
        _assign(agent, ticket, accessioning, admin_actor)
    row = Notice.objects.get(recipient=admin_actor)
    original = JobSettings.save
    outcomes = []

    def save(settings_row, *args, **kwargs):
        if settings_row.close_resolved_after_days is not None:
            outcomes.append(cases.independent(lambda: outbox.attempt(row.pk)))
        return original(settings_row, *args, **kwargs)

    monkeypatch.setattr(JobSettings, "save", save)
    assert update_settings(actor=admin_actor, expected_revision=1, close_resolved_after_days=6)
    assert outcomes == ["sent"]


def test_an_assignment_holds_its_assignees_roles_against_removal_not_against_a_grant(
    agent, category, accessioning, technician
):
    supervisor_group = Group.objects.get_or_create(name="supervisor")[0]
    technical = Group.objects.get(name="technical")
    ticket = new_request(agent, category, team=accessioning)
    with transaction.atomic():
        _assign(agent, ticket, accessioning, technician)
        assert prior.writer_waits(lambda: technician.groups.remove(technical))
        assert not prior.writer_waits(lambda: technician.groups.add(supervisor_group))


def test_the_settings_write_holds_its_roles_against_removal_not_against_a_grant(
    make_user, monkeypatch
):
    admin_actor = make_user("verify_grant_admin", "admin")
    admin_group = Group.objects.get(name="admin")
    extra = Group.objects.get_or_create(name="supervisor")[0]
    original = JobSettings.save
    observed = []

    def save(settings_row, *args, **kwargs):
        if settings_row.close_resolved_after_days is not None:
            observed.append(
                (
                    prior.writer_waits(lambda: admin_actor.groups.remove(admin_group)),
                    prior.writer_waits(lambda: admin_actor.groups.add(extra)),
                )
            )
        return original(settings_row, *args, **kwargs)

    monkeypatch.setattr(JobSettings, "save", save)
    assert update_settings(actor=admin_actor, expected_revision=1, close_resolved_after_days=4)
    assert observed == [(True, False)]


def test_a_staff_notice_holds_its_recipients_lead_flag_as_documented(staff_and_lead):
    """Documented: the team is held against members and leads changing. The recipient's own
    membership is held (key share), so changing their lead flag (FOR UPDATE) waits."""
    row, own, supervisor_actor = staff_and_lead
    with transaction.atomic():
        assert notifications.prepare_request_notice(row) is not None
        assert prior.writer_waits(
            lambda: teams.set_lead(actor=supervisor_actor, membership=own, is_lead=True)
        )


@pytest.fixture
def staff_and_lead(agent, category, accessioning, technician, supervisor):
    ticket = new_request(agent, category, team=accessioning)
    with patch.object(outbox, "send_now", return_value={}):
        _assign(agent, ticket, accessioning, technician)
    row = Notice.objects.get(recipient=technician)
    own = TeamMembership.objects.get(team=accessioning, user=technician)
    return row, own, supervisor


def test_a_staff_notice_is_busy_while_its_recipients_role_is_being_removed(
    agent, category, accessioning, technician
):
    ticket = new_request(agent, category, team=accessioning)
    with patch.object(outbox, "send_now", return_value={}):
        _assign(agent, ticket, accessioning, technician)
    row = Notice.objects.get(recipient=technician)
    technical = Group.objects.get(name="technical")
    started = row.next_attempt_at

    def remove():
        User.groups.through.objects.filter(user=technician, group=technical).delete()

    with held_open(remove):
        assert outbox.attempt(row.pk) == "busy"
    row.refresh_from_db()
    assert row.next_attempt_at >= started + timedelta(seconds=55)
    assert not mail.outbox
