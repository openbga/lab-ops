"""Request clocks through the desk services and hooks (S3)."""

from datetime import date, datetime, timedelta

import pytest

from desk import models as desk_models
from desk.services import add_entry
from service_levels import rules
from service_levels.models import BusinessCalendar, Holiday, RequestClock, ServiceTarget
from service_levels.rules import request_state

from .conftest import NY

H = timedelta(hours=1)
WEEKDAYS = {d: [["08:00", "17:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}


@pytest.fixture
def office(db):
    calendar = BusinessCalendar.objects.create(name="Office hours", hours=WEEKDAYS)
    Holiday.objects.create(calendar=calendar, date=date(2026, 11, 26), name="Example holiday")
    return calendar


@pytest.fixture
def target(office):
    return ServiceTarget.objects.create(
        name="Everything", first_response_minutes=60, resolution_minutes=9 * 60, calendar=office
    )


def clock_of(request):
    return RequestClock.objects.get(request=request)


def test_rule_codes_match_the_desk_contract():
    assert rules.ANSWERED_AT_INTAKE_CHANNELS == {str(c) for c in desk_models.ANSWERED_AT_INTAKE}
    assert rules.REACHED_OUTCOMES == {str(o) for o in desk_models.REACHED_OUTCOMES}
    assert set(rules.STATES) == set(
        __import__("service_levels.selectors", fromlist=["CLOCK_STATES"]).CLOCK_STATES
    )


def test_no_target_means_no_clock(frozen, new_request):
    request = new_request()
    assert not RequestClock.objects.filter(request=request).exists()
    assert request_state(None, frozen.now) == "none"


def test_due_times_at_creation(frozen, target, new_request):
    request = new_request()
    clock = clock_of(request)
    assert clock.target == target
    assert clock.first_response_due_at == datetime(2026, 11, 16, 9, tzinfo=NY)
    assert clock.resolution_due_at == datetime(2026, 11, 16, 17, tzinfo=NY)
    assert clock.calendar == target.calendar


def test_holiday_of_the_calendar_moves_the_due_time(frozen, target, new_request):
    frozen(datetime(2026, 11, 25, 16, tzinfo=NY))
    request = new_request()
    assert clock_of(request).resolution_due_at == datetime(2026, 11, 27, 16, tzinfo=NY)


def test_most_specific_target_applies(frozen, target, category, new_request):
    specific = ServiceTarget.objects.create(
        name="Urgent missing results", category=category, priority="urgent", resolution_minutes=30
    )
    assert clock_of(new_request(priority="urgent")).target == specific
    assert clock_of(new_request(priority="normal")).target == target


@pytest.mark.parametrize("channel", ["phone", "in_person"])
def test_call_or_walk_in_meets_the_first_response_at_creation(frozen, target, new_request, channel):
    clock = clock_of(new_request(channel=channel))
    assert clock.first_response_met_at == frozen.now
    assert not clock.first_response_missed


def test_first_entry_is_not_a_response(frozen, target, agent, category, team):
    from desk.services import create_request

    request = create_request(
        actor=agent,
        category=category,
        subject="Example",
        first_entry_kind="reply",
        first_entry_body="The client's words, typed by staff.",
        channel="email",
    )
    assert clock_of(request).first_response_met_at is None


def _entry(request, actor, kind, **extra):
    request.refresh_from_db()
    return add_entry(
        actor=actor,
        request=request,
        kind=kind,
        body="Synthetic entry.",
        expected_revision=request.revision,
        **extra,
    )


def test_staff_reply_meets_the_first_response_and_notes_do_not(frozen, target, agent, new_request):
    request = new_request()
    frozen(frozen.now + timedelta(minutes=20))
    _entry(request, agent, "note")
    assert clock_of(request).first_response_met_at is None
    frozen(frozen.now + timedelta(minutes=20))
    _entry(request, agent, "reply")
    clock = clock_of(request)
    assert clock.first_response_met_at == frozen.now
    assert not clock.first_response_missed


@pytest.mark.parametrize(
    ("outcome", "meets"),
    [
        ("spoke_with", True),
        ("message_delivered", True),
        ("no_answer", False),
        ("message_left", False),
    ],
)
def test_logged_contact_meets_only_when_it_reached_the_client(
    frozen, target, agent, new_request, outcome, meets
):
    request = new_request()
    frozen(frozen.now + 2 * H)
    contact = {
        "direction": "to_client",
        "channel": "phone",
        "with_whom": "Office manager",
        "outcome": outcome,
    }
    _entry(request, agent, "contact", contact=contact)
    clock = clock_of(request)
    assert (clock.first_response_met_at is not None) is meets
    if meets:
        assert clock.first_response_missed  # two hours on a one-hour target


def test_client_reply_is_not_a_response(frozen, target, portal_user, client_org, new_request):
    from service_levels import clocks

    request = new_request(client=client_org)
    frozen(frozen.now + timedelta(minutes=10))
    reply = desk_models.RequestEntry(
        request=request, kind="reply", author=portal_user, body="Synthetic.", created_at=frozen.now
    )
    clocks.entry_added(request, reply)  # what the hook does for a portal reply
    assert clock_of(request).first_response_met_at is None


def test_waiting_on_client_pauses_and_resumes(frozen, target, agent, new_request, set_status):
    request = new_request()
    frozen(frozen.now + H)
    request = set_status(request, "waiting_client")
    assert clock_of(request).paused_since == frozen.now
    frozen(datetime(2026, 11, 17, 10, tzinfo=NY))
    set_status(request, "open")
    clock = clock_of(request)
    assert clock.paused_since is None
    assert clock.paused_seconds == 10 * 3600  # Monday 09:00-17:00 and Tuesday 08:00-10:00
    assert clock.resolution_due_at == datetime(2026, 11, 18, 9, tzinfo=NY)


def test_composer_status_change_pauses_through_add_entry(frozen, target, agent, new_request):
    request = new_request()
    frozen(frozen.now + H)
    _entry(request, agent, "reply", new_status="waiting_client")
    assert clock_of(request).paused_since == frozen.now


@pytest.mark.parametrize(("pauses", "expected"), [(True, True), (False, False)])
def test_third_party_pause_follows_the_target(
    frozen, target, new_request, set_status, pauses, expected
):
    target.pause_on_third_party = pauses
    target.save()
    request = new_request()
    set_status(request, "waiting_third_party")
    assert (clock_of(request).paused_since is not None) is expected


def test_resolved_time_is_excluded_after_reopening(frozen, target, new_request, set_status):
    request = new_request()
    frozen(frozen.now + 2 * H)
    request = set_status(request, "resolved")
    clock = clock_of(request)
    assert clock.resolution_met_at == frozen.now and not clock.resolution_missed
    frozen(datetime(2026, 11, 17, 8, tzinfo=NY))
    set_status(request, "open")
    clock = clock_of(request)
    assert clock.resolution_met_at is None
    assert clock.paused_seconds == 7 * 3600
    assert clock.resolution_due_at == datetime(2026, 11, 17, 15, tzinfo=NY)


def test_missed_sticks(frozen, target, new_request, set_status, change_property):
    request = new_request()
    frozen(frozen.now + 10 * H)  # 18:00, past the 17:00 resolution due time
    request = set_status(request, "resolved")
    assert clock_of(request).resolution_missed
    frozen(frozen.now + H)
    request = set_status(request, "open")
    request = change_property(request, priority="low")
    assert clock_of(request).resolution_missed
    assert request_state(clock_of(request), frozen.now) in ("overdue", "missed")


def test_recalculation_on_priority_and_category_never_moves_a_met_clock(
    frozen, target, category, other_category, agent, new_request, change_property
):
    quick = ServiceTarget.objects.create(
        name="High", priority="high", first_response_minutes=15, resolution_minutes=120
    )
    request = new_request()
    frozen(frozen.now + timedelta(minutes=30))
    _entry(request, agent, "reply")
    met = clock_of(request).first_response_met_at
    request = change_property(request, priority="high")
    clock = clock_of(request)
    assert clock.target == quick
    assert clock.first_response_met_at == met and clock.first_response_minutes == 60
    assert clock.resolution_due_at == datetime(2026, 11, 16, 10, tzinfo=NY)  # around the clock
    special = ServiceTarget.objects.create(
        name="Questions", category=other_category, resolution_minutes=240
    )
    request = change_property(request, category=other_category)
    assert clock_of(request).target == special


def test_target_appearing_later_starts_a_clock_on_recalculation(
    frozen, new_request, change_property
):
    request = new_request()
    ServiceTarget.objects.create(name="High", priority="high", resolution_minutes=120)
    frozen(frozen.now + H)
    request = change_property(request, priority="high")
    assert clock_of(request).resolution_due_at == datetime(2026, 11, 16, 10, tzinfo=NY)


def test_editing_a_target_does_not_move_running_clocks(frozen, target, new_request):
    request = new_request()
    target.resolution_minutes = 60
    target.save()
    assert clock_of(request).resolution_minutes == 9 * 60
