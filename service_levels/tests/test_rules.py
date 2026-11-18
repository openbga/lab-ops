"""Clock rules and target precedence (S2, S3): pure, no database."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from service_levels.business_time import Calendar
from service_levels.rules import (
    TargetValues,
    apply_target,
    clock_state,
    combine_states,
    due_soon_threshold,
    entry_meets_first_response,
    meet_first_response,
    request_state,
    select_target,
    sync_status,
)

NY = ZoneInfo("America/New_York")
H = timedelta(hours=1)
OFFICE = Calendar.from_hours(
    {d: [["08:00", "17:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}, NY
)
START = datetime(2026, 11, 16, 8, tzinfo=NY)  # Monday 08:00


def new_clock():
    return SimpleNamespace(
        target_id=None,
        first_response_minutes=None,
        resolution_minutes=None,
        calendar_id=None,
        pause_on_third_party=False,
        first_response_due_at=None,
        first_response_met_at=None,
        first_response_missed=False,
        resolution_due_at=None,
        resolution_met_at=None,
        resolution_missed=False,
        paused_since=None,
        paused_seconds=0,
        calculated_at=None,
    )


def target(first=60, resolution=9 * 60, third_party=False, target_id=1):
    return TargetValues(target_id, first, resolution, None, third_party)


def started(first=60, resolution=9 * 60, third_party=False, calendar=OFFICE):
    clock = new_clock()
    apply_target(
        clock, target(first, resolution, third_party), start=START, now=START, calendar=calendar
    )
    return clock


# States -------------------------------------------------------------------------------------


def test_due_soon_threshold_is_a_fifth_or_one_hour():
    assert due_soon_threshold(60) == H
    assert due_soon_threshold(300) == H
    assert due_soon_threshold(600) == 2 * H
    assert due_soon_threshold(3 * 24 * 60) == timedelta(hours=14, minutes=24)


NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("due", "met", "missed", "minutes", "paused", "expected"),
    [
        (None, None, False, None, False, "none"),
        (NOW + 3 * H, None, False, 60, False, "on_track"),
        (NOW + 59 * timedelta(minutes=1), None, False, 60, False, "due_soon"),
        (NOW + H, None, False, 60, False, "on_track"),  # exactly the threshold left
        (NOW + 3 * H, None, False, 600, False, "on_track"),  # a fifth of 10 h is 2 h
        (NOW + 2 * H - timedelta(seconds=1), None, False, 600, False, "due_soon"),
        (NOW, None, False, 60, False, "due_soon"),  # due this instant: not yet overdue
        (NOW - timedelta(seconds=1), None, False, 60, False, "overdue"),
        (NOW - H, None, True, 60, False, "overdue"),
        (NOW + 5 * H, None, True, 60, False, "missed"),  # due moved later after a miss
        (NOW - H, None, False, 60, True, "paused"),
        (NOW - H, None, True, 60, True, "missed"),
        (NOW + H, NOW, False, 60, False, "met"),
        (NOW - H, NOW, True, 60, False, "missed"),
    ],
)
def test_clock_state_table(due, met, missed, minutes, paused, expected):
    state = clock_state(
        due_at=due, met_at=met, missed=missed, minutes=minutes, now=NOW, paused=paused
    )
    assert state == expected


def test_clock_state_compares_real_time_not_wall_clock():
    # Both in New York, across the November change: due 01:30 EST is after 01:45 EDT.
    due = datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=NY)
    now = datetime(2026, 11, 1, 1, 45, fold=0, tzinfo=NY)
    assert clock_state(due_at=due, met_at=None, missed=False, minutes=600, now=now) == "due_soon"


RUN, MET = True, False


@pytest.mark.parametrize(
    ("clocks", "expected"),
    [
        ((("none", RUN), ("none", RUN)), "none"),
        ((("met", MET), ("none", RUN)), "met"),
        ((("met", MET), ("met", MET)), "met"),
        ((("missed", MET), ("met", MET)), "missed"),
        ((("met", MET), ("on_track", RUN)), "on_track"),
        ((("missed", MET), ("on_track", RUN)), "on_track"),  # a late first response is history
        ((("missed", RUN), ("on_track", RUN)), "missed"),  # a running clock already missed
        ((("on_track", RUN), ("paused", RUN)), "paused"),
        ((("due_soon", RUN), ("paused", RUN)), "due_soon"),
        ((("overdue", RUN), ("due_soon", RUN)), "overdue"),
        ((("on_track", RUN), ("overdue", RUN)), "overdue"),
        ((("met", MET), ("missed", MET)), "missed"),
        ((("missed", MET), ("missed", RUN)), "missed"),
    ],
)
def test_request_state_combines_running_clocks_first(clocks, expected):
    assert combine_states(clocks) == expected


# Due times -------------------------------------------------------------------------------


def test_due_times_at_creation_use_the_calendar():
    clock = started(first=60, resolution=2 * 9 * 60)
    assert clock.first_response_due_at == datetime(2026, 11, 16, 9, tzinfo=NY)
    assert clock.resolution_due_at == datetime(2026, 11, 17, 17, tzinfo=NY)
    assert clock.target_id == 1
    assert request_state(clock, START) == "on_track"


def test_target_with_only_a_resolution_time_has_no_first_response_clock():
    clock = started(first=None, resolution=60)
    assert clock.first_response_due_at is None
    assert request_state(clock, START + 30 * timedelta(minutes=1)) == "due_soon"


def test_recalculation_moves_running_clocks_but_never_a_met_one():
    clock = started(first=60, resolution=9 * 60)
    meet_first_response(clock, START + 30 * timedelta(minutes=1))
    met_due = clock.first_response_due_at
    apply_target(
        clock, target(first=15, resolution=4 * 60), start=START, now=START + H, calendar=OFFICE
    )
    assert clock.first_response_due_at == met_due
    assert clock.first_response_minutes == 60
    assert clock.resolution_due_at == datetime(2026, 11, 16, 12, tzinfo=NY)
    assert clock.resolution_minutes == 4 * 60


def test_recalculation_after_a_miss_keeps_the_miss():
    clock = started(first=60)
    later = START + 2 * H
    apply_target(clock, target(first=4 * 60), start=START, now=later, calendar=OFFICE)
    assert clock.first_response_due_at == datetime(2026, 11, 16, 12, tzinfo=NY)
    assert clock.first_response_missed
    assert (
        clock_state(
            due_at=clock.first_response_due_at,
            met_at=None,
            missed=True,
            minutes=240,
            now=later,
        )
        == "missed"
    )


def test_no_target_clears_running_clocks():
    clock = started()
    apply_target(clock, None, start=START, now=START, calendar=None)
    assert clock.first_response_due_at is None and clock.resolution_due_at is None
    assert request_state(clock, START) == "none"


# First response ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "staff", "outcome", "meets"),
    [
        ("reply", True, "", True),
        ("reply", False, "", False),  # a client reply is not a response
        ("note", True, "", False),
        ("event", True, "", False),
        ("contact", True, "spoke_with", True),
        ("contact", True, "message_delivered", True),
        ("contact", True, "no_answer", False),
        ("contact", True, "left_message", False),
    ],
)
def test_what_meets_the_first_response(kind, staff, outcome, meets):
    assert (
        entry_meets_first_response(kind=kind, author_is_staff=staff, contact_outcome=outcome)
        is meets
    )


def test_first_response_met_late_is_missed_and_stays_met():
    clock = started(first=60)
    assert meet_first_response(clock, START + 2 * H)
    assert clock.first_response_missed
    assert not meet_first_response(clock, START + 3 * H)
    assert clock.first_response_met_at == START + 2 * H


# Pauses, resolution and reopening --------------------------------------------------------


def test_waiting_on_client_pauses_the_resolution_clock_only():
    clock = started(first=60, resolution=9 * 60)
    sync_status(clock, "waiting_client", start=START, now=START + H, calendar=OFFICE)
    assert clock.paused_since == START + H
    assert request_state(clock, START + 2 * H) == "overdue"  # first response still runs
    meet_first_response(clock, START + 30 * timedelta(minutes=1))
    assert request_state(clock, START + 5 * H) == "paused"
    # Resumes Tuesday 10:00: Monday 09:00-17:00 and Tuesday 08:00-10:00 do not count.
    sync_status(
        clock, "open", start=START, now=datetime(2026, 11, 17, 10, tzinfo=NY), calendar=OFFICE
    )
    assert clock.paused_since is None
    assert clock.paused_seconds == 10 * 3600
    assert clock.resolution_due_at == datetime(2026, 11, 18, 9, tzinfo=NY)


@pytest.mark.parametrize(("third_party", "paused"), [(True, True), (False, False)])
def test_waiting_on_third_party_pauses_only_when_the_target_says_so(third_party, paused):
    clock = started(third_party=third_party)
    sync_status(clock, "waiting_third_party", start=START, now=START + H, calendar=OFFICE)
    assert (clock.paused_since is not None) is paused


def test_moving_between_waiting_statuses_ends_the_pause_when_the_rule_differs():
    clock = started(third_party=False)
    sync_status(clock, "waiting_client", start=START, now=START + H, calendar=OFFICE)
    sync_status(clock, "waiting_third_party", start=START, now=START + 3 * H, calendar=OFFICE)
    assert clock.paused_since is None
    assert clock.paused_seconds == 2 * 3600


def test_resolving_meets_both_clocks_and_reopening_excludes_the_resolved_time():
    clock = started(first=60, resolution=9 * 60)
    sync_status(clock, "resolved", start=START, now=START + 2 * H, calendar=OFFICE)
    assert clock.resolution_met_at == START + 2 * H and not clock.resolution_missed
    assert clock.first_response_met_at == START + 2 * H and clock.first_response_missed
    assert request_state(clock, START + 3 * H) == "missed"
    sync_status(clock, "closed", start=START, now=START + 3 * H, calendar=OFFICE)
    assert clock.resolution_met_at == START + 2 * H  # closing a resolved request changes nothing
    # Reopened Tuesday 08:00: Monday 10:00-17:00 resolved does not count.
    sync_status(
        clock, "open", start=START, now=datetime(2026, 11, 17, 8, tzinfo=NY), calendar=OFFICE
    )
    assert clock.resolution_met_at is None
    assert clock.paused_seconds == 7 * 3600
    assert clock.resolution_due_at == datetime(2026, 11, 17, 15, tzinfo=NY)
    assert clock.first_response_met_at == START + 2 * H  # reopening does not reopen the response


def test_resolving_while_paused_counts_the_pause_first():
    clock = started(resolution=2 * 60)
    sync_status(clock, "waiting_client", start=START, now=START + H, calendar=OFFICE)
    sync_status(clock, "resolved", start=START, now=START + 4 * H, calendar=OFFICE)
    assert clock.paused_seconds == 3 * 3600
    assert clock.resolution_due_at == START + 5 * H
    assert clock.resolution_met_at == START + 4 * H and not clock.resolution_missed


def test_reopening_into_a_waiting_status_pauses_again():
    clock = started()
    sync_status(clock, "resolved", start=START, now=START + H, calendar=OFFICE)
    sync_status(clock, "waiting_client", start=START, now=START + 2 * H, calendar=OFFICE)
    assert clock.resolution_met_at is None and clock.paused_since == START + 2 * H
    assert clock.paused_seconds == 3600


def test_missed_resolution_sticks_through_reopening():
    clock = started(resolution=60)
    sync_status(clock, "resolved", start=START, now=START + 2 * H, calendar=OFFICE)
    assert clock.resolution_missed
    sync_status(clock, "open", start=START, now=START + 3 * H, calendar=OFFICE)
    assert clock.resolution_missed
    assert request_state(clock, START + 3 * H) == "overdue"


def test_pausing_past_due_records_the_miss():
    clock = started(first=None, resolution=60)
    sync_status(clock, "waiting_client", start=START, now=START + 2 * H, calendar=OFFICE)
    assert clock.resolution_missed
    assert request_state(clock, START + 2 * H) == "missed"


def test_around_the_clock_pause():
    clock = started(first=None, resolution=60, calendar=None)
    sync_status(
        clock, "waiting_client", start=START, now=START + timedelta(minutes=30), calendar=None
    )
    sync_status(clock, "open", start=START, now=START + 10 * H, calendar=None)
    assert clock.resolution_due_at == START + 10 * H + timedelta(minutes=30)


def test_no_resolution_target_still_stops_the_first_response_on_resolve():
    clock = started(first=60, resolution=None)
    sync_status(
        clock, "waiting_client", start=START, now=START + timedelta(minutes=10), calendar=OFFICE
    )
    assert clock.paused_since is None
    sync_status(clock, "resolved", start=START, now=START + timedelta(minutes=20), calendar=OFFICE)
    assert clock.first_response_met_at == START + timedelta(minutes=20)
    assert request_state(clock, START + H) == "met"


# Precedence --------------------------------------------------------------------------------

CATEGORY, GROUP, OTHER = 11, 1, 99
LEVELS = [
    ("category and priority", CATEGORY, "high"),
    ("category, any priority", CATEGORY, ""),
    ("group and priority", GROUP, "high"),
    ("group, any priority", GROUP, ""),
    ("any category, priority", None, "high"),
    ("any category, any priority", None, ""),
]


def candidates(levels):
    return [SimpleNamespace(name=n, category_id=c, priority=p) for n, c, p in levels]


@pytest.mark.parametrize("drop", range(len(LEVELS)))
def test_precedence_over_all_six_levels(drop):
    available = candidates(LEVELS[drop:])
    chosen = select_target(available, category_id=CATEGORY, group_id=GROUP, priority="high")
    assert chosen.name == LEVELS[drop][0]
    # Order of the candidate list does not matter.
    chosen = select_target(
        reversed(available), category_id=CATEGORY, group_id=GROUP, priority="high"
    )
    assert chosen.name == LEVELS[drop][0]


def test_targets_for_other_categories_and_priorities_never_apply():
    others = candidates([("other category", OTHER, "high"), ("other priority", CATEGORY, "low")])
    assert select_target(others, category_id=CATEGORY, group_id=GROUP, priority="high") is None
    assert select_target([], category_id=CATEGORY, group_id=None, priority="high") is None
