"""Service target breach notices (service_levels.jobs.notice_breaches): who is told, once per
clock run, without touching the ticket, and checked again at sending. Synthetic data."""

from datetime import date, datetime, timedelta

import pytest
from django.core import mail
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from core.models import AuditEvent, Lab, UserPreference
from desk import notifications
from desk.models import ServiceRequest, Status
from desk.services import add_entry, change_status
from jobs import outbox
from jobs.actor import system_account
from jobs.models import Notice, NoticeStatus, NotSentReason
from service_levels.jobs import notice_breaches
from service_levels.models import BusinessCalendar, Holiday, RequestClock, ServiceTarget
from taxonomy.models import RequestCategory
from teams.models import TeamMembership

from .conftest import NY

pytestmark = pytest.mark.django_db

WEEKDAYS = {d: [["08:00", "17:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}
LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
FIRST = "first_response_breached"
RESOLUTION = "resolution_breached"


@pytest.fixture(autouse=True)
def mail_on(settings):
    settings.EMAIL_BACKEND = LOCMEM
    settings.PUBLIC_URL = "https://lab-ops.example.invalid"
    Lab.objects.update_or_create(pk=1, defaults={"name": "Example Reference Laboratory"})


@pytest.fixture
def target(db):
    calendar = BusinessCalendar.objects.create(name="Office hours", hours=WEEKDAYS)
    Holiday.objects.create(calendar=calendar, date=date(2026, 11, 26), name="Example holiday")
    return ServiceTarget.objects.create(
        name="Everything", first_response_minutes=60, resolution_minutes=9 * 60, calendar=calendar
    )


@pytest.fixture
def crew(make_user, team):
    """Staff of the default team: an assignee, a lead and a plain member."""
    people = {
        "assignee": make_user("assignee", "technical"),
        "lead": make_user("team_lead", "technical"),
        "member": make_user("plain_member", "technical"),
    }
    TeamMembership.objects.create(team=team, user=people["assignee"])
    TeamMembership.objects.create(team=team, user=people["lead"], is_lead=True)
    TeamMembership.objects.create(team=team, user=people["member"])
    return people


def clock_of(request):
    return RequestClock.objects.get(request=request)


def breach_notices(event=None):
    notices = Notice.objects.filter(topic="desk.breach")
    return notices.filter(events=[event]) if event else notices


def who(event=None):
    return {n.recipient.username: n.data["role"] for n in breach_notices(event)}


@pytest.fixture
def late(frozen):
    """Move to 10:00 on the first Monday: past the first-response time, before the
    resolution time."""
    return frozen(datetime(2026, 11, 16, 10, tzinfo=NY))


@pytest.fixture
def breached(target, new_request, crew, frozen):
    """A request assigned to `assignee`, created at 08:00."""
    return new_request(assignee=crew["assignee"])


# Who is told ---------------------------------------------------------------------------------


def test_the_assignee_and_the_teams_leads_are_told_once_each(breached, crew, late):
    assert notice_breaches() == {"breaches": 1, "notices": 2}
    assert who() == {"assignee": "assignee", "team_lead": "lead"}
    for notice in breach_notices():
        assert notice.events == [FIRST]
        assert notice.object_id == breached.pk and notice.reference == breached.number
        assert notice.data == {
            "clock": "first_response",
            "role": notice.data["role"],
            # The clock run it belongs to: the breach mark set with it.
            "noticed_at": clock_of(breached).first_response_breach_noticed_at.isoformat(),
        }
        assert notice.status == NoticeStatus.WAITING and notice.attempts == 0


def test_a_person_who_is_assignee_and_lead_is_told_once(target, new_request, crew, frozen):
    TeamMembership.objects.filter(user=crew["assignee"]).update(is_lead=True)
    new_request(assignee=crew["assignee"])
    frozen(datetime(2026, 11, 16, 10, tzinfo=NY))
    notice_breaches()
    assert who() == {"assignee": "assignee", "team_lead": "lead"}
    assert breach_notices().filter(recipient=crew["assignee"]).count() == 1


def test_without_an_assignee_the_leads_are_told(target, new_request, crew, make_user, team, frozen):
    second = make_user("second_lead", "technical")
    TeamMembership.objects.create(team=team, user=second, is_lead=True)
    new_request()
    frozen(datetime(2026, 11, 16, 10, tzinfo=NY))
    notice_breaches()
    assert who() == {"team_lead": "lead", "second_lead": "lead"}


def test_with_no_assignee_and_no_leads_every_member_is_told(target, new_request, crew, frozen):
    TeamMembership.objects.filter(is_lead=True).update(is_lead=False)
    new_request()
    frozen(datetime(2026, 11, 16, 10, tzinfo=NY))
    notice_breaches()
    assert who() == {
        "assignee": "member",
        "team_lead": "member",
        "plain_member": "member",
    }


def test_staff_who_turned_notices_off_get_none(breached, crew, late):
    UserPreference.objects.create(user=crew["lead"], key="notifications.email", value=False)
    notice_breaches()
    assert who() == {"assignee": "assignee"}


def test_inactive_staff_get_none(breached, crew, late):
    type(crew["lead"]).objects.filter(pk=crew["lead"].pk).update(is_active=False)
    notice_breaches()
    assert who() == {"assignee": "assignee"}


def test_staff_who_cannot_read_a_restricted_ticket_get_none(
    target, new_request, crew, make_user, team, group, frozen
):
    restricted = RequestCategory.objects.create(
        key="privacy",
        name="Privacy request",
        parent=group,
        is_restricted=True,
        default_team=team,
    )
    request = new_request(category_=restricted)
    outsider = make_user("outsider", "technical")  # not in the team, not a supervisor
    ServiceRequest.objects.filter(pk=request.pk).update(assignee=outsider)
    frozen(datetime(2026, 11, 16, 10, tzinfo=NY))
    notice_breaches()
    assert who() == {"team_lead": "lead"}


def test_a_request_with_no_one_to_tell_is_still_marked_noticed(target, new_request, team, frozen):
    new_request()
    frozen(datetime(2026, 11, 16, 10, tzinfo=NY))
    assert notice_breaches() == {"breaches": 1, "notices": 0}
    assert clock_of(ServiceRequest.objects.get()).first_response_breach_noticed_at is not None


# Once, without changing the ticket -------------------------------------------------------------


def test_the_clock_is_marked_audited_and_not_noticed_again(breached, crew, late):
    robot_before = AuditEvent.objects.filter(action="jobs.system_account_created").count()
    assert notice_breaches() == {"breaches": 1, "notices": 2}
    clock = clock_of(breached)
    assert clock.first_response_breach_noticed_at == late
    assert clock.resolution_breach_noticed_at is None
    robot = system_account()
    event = AuditEvent.objects.get(action="clock.breach_noticed")
    assert event.actor_id == robot.pk
    assert event.object_type == "service_levels.requestclock"
    assert event.object_id == str(clock.pk)
    assert event.after["clock"] == "first_response" and event.after["notices"] == 2
    assert event.after["request_id"] == breached.pk
    assert AuditEvent.objects.filter(action="jobs.system_account_created").count() == (
        robot_before + 1
    )
    notices, events = Notice.objects.count(), AuditEvent.objects.count()
    assert notice_breaches() == {"breaches": 0, "notices": 0}
    assert Notice.objects.count() == notices and AuditEvent.objects.count() == events


def test_the_ticket_does_not_change(breached, crew, late):
    before = ServiceRequest.objects.values().get(pk=breached.pk)
    entries = breached.entries.count()
    notice_breaches()
    assert ServiceRequest.objects.values().get(pk=breached.pk) == before
    assert breached.entries.count() == entries


def test_each_clock_is_noticed_in_its_own_time(breached, crew, frozen):
    frozen(datetime(2026, 11, 16, 10, tzinfo=NY))
    notice_breaches()
    frozen(datetime(2026, 11, 16, 18, tzinfo=NY))
    assert notice_breaches() == {"breaches": 1, "notices": 2}
    assert who(FIRST) == who(RESOLUTION) == {"assignee": "assignee", "team_lead": "lead"}
    clock = clock_of(breached)
    assert clock.first_response_breach_noticed_at < clock.resolution_breach_noticed_at


def test_a_clock_not_yet_due_is_not_noticed(breached, crew, frozen):
    frozen(datetime(2026, 11, 16, 8, 59, tzinfo=NY))
    assert notice_breaches() == {"breaches": 0, "notices": 0}
    assert not Notice.objects.filter(topic="desk.breach").exists()


# Clocks that do not count -----------------------------------------------------------------------


def test_a_paused_resolution_clock_is_not_breached(target, new_request, crew, set_status, frozen):
    request = new_request(channel="phone", assignee=crew["assignee"])  # first response met
    frozen(datetime(2026, 11, 16, 9, tzinfo=NY))
    set_status(request, "waiting_client")
    assert clock_of(request).paused_since is not None
    frozen(datetime(2026, 11, 16, 18, tzinfo=NY))
    assert notice_breaches() == {"breaches": 0, "notices": 0}
    assert clock_of(request).resolution_breach_noticed_at is None
    set_status(request, "open")  # resumed: runs again, due later than it was
    frozen(datetime(2026, 11, 20, 18, tzinfo=NY))
    assert notice_breaches()["breaches"] == 1
    assert who(RESOLUTION)


@pytest.mark.parametrize("status", ["resolved", "closed"])
def test_resolved_and_closed_requests_are_skipped(
    target, new_request, crew, set_status, frozen, status
):
    request = new_request(assignee=crew["assignee"])
    frozen(datetime(2026, 11, 16, 8, 30, tzinfo=NY))
    set_status(request, "resolved")
    if status == "closed":
        set_status(request, "closed")
    # Even a clock that looks running (as after an import) is not noticed for a stopped request.
    RequestClock.objects.filter(request=request).update(
        first_response_met_at=None, resolution_met_at=None
    )
    frozen(datetime(2026, 11, 20, 18, tzinfo=NY))
    assert notice_breaches() == {"breaches": 0, "notices": 0}
    assert not Notice.objects.filter(topic="desk.breach").exists()


def test_resolving_clears_the_mark_so_a_reopened_ticket_is_noticed_again(
    target, new_request, crew, agent, frozen
):
    request = new_request(channel="phone", assignee=crew["assignee"])
    frozen(datetime(2026, 11, 16, 18, tzinfo=NY))
    assert notice_breaches()["breaches"] == 1
    assert clock_of(request).resolution_breach_noticed_at is not None
    assert breach_notices(RESOLUTION).count() == 2

    request.refresh_from_db()
    change_status(
        actor=agent,
        request=request,
        expected_revision=request.revision,
        status=Status.RESOLVED,
        summary="Fixed.",
    )
    assert clock_of(request).resolution_breach_noticed_at is None
    frozen(datetime(2026, 11, 17, 9, tzinfo=NY))
    request.refresh_from_db()
    change_status(
        actor=agent,
        request=request,
        expected_revision=request.revision,
        status=Status.OPEN,
        reason="It came back.",
    )
    assert clock_of(request).resolution_breach_noticed_at is None
    # Its resolution time had passed before it was resolved; the hour before opening counted,
    # so it is due at the reopening itself (still on time then) and past it a minute later.
    assert clock_of(request).resolution_due_at == datetime(2026, 11, 17, 9, tzinfo=NY)
    assert notice_breaches() == {"breaches": 0, "notices": 0}
    frozen(datetime(2026, 11, 17, 9, 1, tzinfo=NY))
    assert notice_breaches() == {"breaches": 1, "notices": 2}
    assert breach_notices(RESOLUTION).count() == 4


# At sending --------------------------------------------------------------------------------------


@pytest.fixture
def recorded(breached, crew, late, django_capture_on_commit_callbacks):
    """The breach notices of `breached`, recorded and not yet attempted."""
    with django_capture_on_commit_callbacks(execute=False):
        notice_breaches()
    mail.outbox.clear()
    return {n.recipient.username: n for n in breach_notices()}


def send(notice):
    outbox.send_now([notice.pk])
    notice.refresh_from_db()
    return notice


def test_notices_are_sent_after_commit_with_one_attempt(
    breached, crew, late, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        notice_breaches()
    assert sorted(m.to[0] for m in mail.outbox) == sorted(
        [crew["assignee"].email, crew["lead"].email]
    )
    assert {n.status for n in breach_notices()} == {NoticeStatus.SENT}
    assert {n.attempts for n in breach_notices()} == {1}


def test_a_still_valid_notice_is_sent(recorded):
    assert send(recorded["assignee"]).status == NoticeStatus.SENT
    assert len(mail.outbox) == 1
    assert notifications.PHRASES[FIRST] in mail.outbox[0].body


def test_a_met_clock_means_no_notice_is_sent(recorded, breached, agent, late):
    breached.refresh_from_db()
    add_entry(
        actor=agent,
        request=breached,
        kind="reply",
        body="Answer",
        expected_revision=breached.revision,
    )
    assert clock_of(breached).first_response_met_at is not None
    mail.outbox.clear()
    for notice in recorded.values():
        send(notice)
        assert (notice.status, notice.not_sent_reason) == (
            NoticeStatus.NOT_SENT,
            NotSentReason.NO_LONGER_APPLIES,
        )
    assert mail.outbox == []


def test_a_new_assignee_means_the_old_assignee_is_not_told(recorded, breached, crew):
    ServiceRequest.objects.filter(pk=breached.pk).update(assignee=crew["member"])
    notice = send(recorded["assignee"])
    assert (notice.status, notice.not_sent_reason) == (
        NoticeStatus.NOT_SENT,
        NotSentReason.NO_LONGER_APPLIES,
    )
    assert mail.outbox == []
    assert send(recorded["team_lead"]).status == NoticeStatus.SENT  # others still apply


def test_a_lead_who_left_the_team_is_not_told(recorded, crew):
    TeamMembership.objects.filter(user=crew["lead"]).delete()
    notice = send(recorded["team_lead"])
    assert (notice.status, notice.not_sent_reason) == (
        NoticeStatus.NOT_SENT,
        NotSentReason.NO_LONGER_APPLIES,
    )
    assert mail.outbox == []


def test_a_lead_who_lost_the_lead_role_is_not_told(recorded, crew):
    TeamMembership.objects.filter(user=crew["lead"]).update(is_lead=False)
    assert send(recorded["team_lead"]).status == NoticeStatus.NOT_SENT


def test_a_ticket_resolved_before_sending_is_not_told(recorded, breached, agent):
    breached.refresh_from_db()
    change_status(
        actor=agent,
        request=breached,
        expected_revision=breached.revision,
        status=Status.RESOLVED,
        summary="Fixed.",
    )
    mail.outbox.clear()
    assert send(recorded["assignee"]).status == NoticeStatus.NOT_SENT
    assert mail.outbox == []


def test_staff_who_turn_notices_off_before_sending_are_not_told(recorded, crew):
    UserPreference.objects.create(user=crew["lead"], key="notifications.email", value=False)
    assert send(recorded["team_lead"]).status == NoticeStatus.NOT_SENT


def test_breach_emails_carry_only_the_number_phrase_lab_name_and_link(
    target,
    new_request,
    team,
    make_user,
    client_org,
    category,
    group,
    frozen,
    django_capture_on_commit_callbacks,
):
    sentinel = "SENTINEL{}X"
    client_org.name = sentinel.format("ORG")
    client_org.save()
    team.name = sentinel.format("TEAM")
    team.save()
    group.name = sentinel.format("GROUP")
    group.save()
    category.name = sentinel.format("CATEGORY")
    category.save()
    person = make_user(
        "told_person", "technical", first_name=sentinel.format("FIRST"), last_name="Persson"
    )
    TeamMembership.objects.create(team=team, user=person)
    frozen(datetime(2026, 11, 16, 8, tzinfo=NY))
    request = new_request(
        assignee=person,
        client=client_org,
        reported_by=sentinel.format("REPORTER"),
        client_reference=sentinel.format("REF"),
    )
    ServiceRequest.objects.filter(pk=request.pk).update(subject=sentinel.format("SUBJECT"))
    frozen(datetime(2026, 11, 16, 10, tzinfo=NY))
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        notice_breaches()
    sent = [m for m in mail.outbox if m.to == [person.email]]
    assert len(sent) == 1
    text = sent[0].message().as_string()
    assert request.number in sent[0].subject
    assert notifications.PHRASES[FIRST] in sent[0].body
    assert "Example Reference Laboratory" in sent[0].body
    assert "https://lab-ops.example.invalid/accounts/login/?next=" in sent[0].body
    assert "SENTINEL" not in text
    assert "Persson" not in sent[0].body and "Persson" not in sent[0].subject
    notice = breach_notices().get(recipient=person)
    assert "SENTINEL" not in f"{notice.events}{notice.data}{notice.reference}{notice.last_error}"


# Migration 0003 ----------------------------------------------------------------------------------


@pytest.fixture
def at_migration(transactional_db):
    """Migrate service_levels to a named migration; always return to the latest afterwards."""
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()

    def go(name):
        executor = MigrationExecutor(connection)
        executor.migrate([("service_levels", name)])

    yield go
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(latest)


def test_migration_marks_breaches_that_already_happened(
    at_migration, target, new_request, crew, set_status, frozen
):
    from django.utils import timezone

    now = timezone.now()
    frozen(now)
    past, future = now - timedelta(hours=2), now + timedelta(hours=5)

    def make(**clock):
        request = new_request(assignee=crew["assignee"])
        RequestClock.objects.filter(request=request).update(**clock)
        return request

    both = make(first_response_due_at=past, resolution_due_at=past)
    not_due = make(first_response_due_at=future, resolution_due_at=future)
    met = make(first_response_due_at=past, first_response_met_at=past, resolution_due_at=future)
    paused = make(first_response_due_at=future, resolution_due_at=past, paused_since=past)
    stopped = new_request(assignee=crew["assignee"])
    set_status(stopped, "closed")
    RequestClock.objects.filter(request=stopped).update(
        first_response_due_at=past,
        first_response_met_at=None,
        resolution_due_at=past,
        resolution_met_at=None,
    )

    at_migration("0002_ticket_terminology")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=%s",
            ["service_levels_requestclock"],
        )
        assert "first_response_breach_noticed_at" not in {row[0] for row in cursor.fetchall()}
    at_migration("0003_breach_notices")

    def marks(request):
        clock = clock_of(request)
        return (
            clock.first_response_breach_noticed_at is not None,
            clock.resolution_breach_noticed_at is not None,
        )

    assert marks(both) == (True, True)
    assert marks(not_due) == (False, False)
    assert marks(met) == (False, False)
    assert marks(paused) == (False, False)
    assert marks(stopped) == (False, False)
    # The first run of the job after installing has nothing to say about the old breaches.
    assert notice_breaches(now=now) == {"breaches": 0, "notices": 0}
    assert not Notice.objects.filter(topic="desk.breach").exists()
