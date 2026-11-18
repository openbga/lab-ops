"""Service clock rules as pure functions.

`docs/product.md` §Service clocks says what a clock does; this module is the one place that
says how. The functions read and change any object with the `RequestClock` attributes (the model
instance in production, a plain namespace in tests) and never touch the database. The SQL state
annotation in `selectors.py` mirrors `request_state` and is tested against it.

Two clocks per request:

- First response starts at creation and is met by the first staff reply, a logged contact that
  reached the client, or creation itself for a call or walk-in. It does not pause. Resolving the
  request stops it (met at that moment; late means missed), because a resolved request owes no
  further first response.
- Resolution stops at Resolved and resumes on reopening; the time spent resolved is added to the
  paused time, so it does not count. It pauses while waiting on the client, and while waiting on
  a third party when the target says so. Its due time is always
  `created + target + paused time`, counted on the clock's calendar.

A clock that has been missed stays missed: the flag is set when a clock is met late, and whenever
a service touches a clock that is already past due (before a pause or a recalculation moves its
due time).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .business_time import Calendar, add_business_seconds, business_seconds_between

# Display order of the states, also the filter choices of request lists.
STATES = ("overdue", "due_soon", "on_track", "paused", "missed", "met", "none")
STATE_LABELS = {
    "overdue": "Overdue",
    "due_soon": "Due soon",
    "on_track": "On track",
    "paused": "Paused",
    "missed": "Missed",
    "met": "Met",
    "none": "No target",
}
STATE_TONES = {
    "overdue": "danger",
    "due_soon": "warning",
    "on_track": "success",
    "paused": "hold",
    "missed": "danger",
    "met": "success",
    "none": "neutral",
}
# When a request's two clocks disagree, the one still running decides, worst first.
RUNNING_RANK = {"overdue": 0, "missed": 1, "due_soon": 2, "paused": 3, "on_track": 4}

STOPPED_STATUSES = frozenset({"resolved", "closed"})
WAITING_CLIENT = "waiting_client"
WAITING_THIRD_PARTY = "waiting_third_party"
# A staff-logged request from these channels was answered while it was logged.
ANSWERED_AT_INTAKE_CHANNELS = frozenset({"phone", "in_person"})
# Logged-contact outcomes that reached the client and so count as a first response.
REACHED_OUTCOMES = frozenset({"spoke_with", "message_delivered"})

DUE_SOON_FLOOR = timedelta(hours=1)


def _late(moment: datetime, due_at: datetime) -> bool:
    """True when `moment` is after `due_at`. Compared in UTC: Python compares two datetimes
    that share one tzinfo by their wall-clock fields."""
    return moment.astimezone(UTC) > due_at.astimezone(UTC)


def due_soon_threshold(minutes: int) -> timedelta:
    """A fifth of the target, or one hour, whichever is longer (wall-clock time left)."""
    return max(timedelta(seconds=minutes * 12), DUE_SOON_FLOOR)


def clock_state(
    *,
    due_at: datetime | None,
    met_at: datetime | None,
    missed: bool,
    minutes: int | None,
    now: datetime,
    paused: bool = False,
) -> str:
    """The state of one clock (first response or resolution)."""
    if due_at is None:
        return "none"
    if met_at is not None:
        return "missed" if missed else "met"
    if paused:
        return "missed" if missed else "paused"
    if _late(now, due_at):
        return "overdue"
    if missed:
        return "missed"
    if due_at.astimezone(UTC) - now.astimezone(UTC) < due_soon_threshold(minutes or 0):
        return "due_soon"
    return "on_track"


def first_response_state(clock, now: datetime) -> str:
    if clock is None:
        return "none"
    return clock_state(
        due_at=clock.first_response_due_at,
        met_at=clock.first_response_met_at,
        missed=clock.first_response_missed,
        minutes=clock.first_response_minutes,
        now=now,
    )


def resolution_state(clock, now: datetime) -> str:
    if clock is None:
        return "none"
    return clock_state(
        due_at=clock.resolution_due_at,
        met_at=clock.resolution_met_at,
        missed=clock.resolution_missed,
        minutes=clock.resolution_minutes,
        now=now,
        paused=clock.paused_since is not None,
    )


def combine_states(clocks: Iterable[tuple[str, bool]]) -> str:
    """One state for a request from its clocks' (state, still running) pairs.

    A clock still running (not met) decides, worst first: overdue, missed, due soon, paused, on
    track. When every clock has been met, one met late makes the request missed, else met. No
    clock: none."""
    clocks = [(state, running) for state, running in clocks if state != "none"]
    running = [state for state, still_running in clocks if still_running]
    if running:
        return min(running, key=RUNNING_RANK.__getitem__)
    if any(state == "missed" for state, _ in clocks):
        return "missed"
    return "met" if clocks else "none"


def request_state(clock, now: datetime) -> str:
    """The request-level state lists show and filter on."""
    if clock is None:
        return "none"
    return combine_states(
        (
            (first_response_state(clock, now), clock.first_response_met_at is None),
            (resolution_state(clock, now), clock.resolution_met_at is None),
        )
    )


# Changes ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetValues:
    """What a clock copies from the applicable service target."""

    target_id: int | None
    first_response_minutes: int | None
    resolution_minutes: int | None
    calendar_id: int | None
    pause_on_third_party: bool


def desired_phase(status: str, pause_on_third_party: bool) -> str:
    """'running', 'paused' or 'stopped' for the resolution clock in this request status."""
    if status in STOPPED_STATUSES:
        return "stopped"
    if status == WAITING_CLIENT or (status == WAITING_THIRD_PARTY and pause_on_third_party):
        return "paused"
    return "running"


def current_phase(clock) -> str:
    if clock.resolution_met_at is not None:
        return "stopped"
    return "paused" if clock.paused_since is not None else "running"


def _resolution_due(clock, start: datetime, calendar: Calendar | None) -> datetime | None:
    if not clock.resolution_minutes:
        return None
    return add_business_seconds(
        start, clock.resolution_minutes * 60 + (clock.paused_seconds or 0), calendar
    )


def _mark_missed_if_past_due(clock, now: datetime) -> None:
    if (
        clock.first_response_met_at is None
        and clock.first_response_due_at is not None
        and _late(now, clock.first_response_due_at)
    ):
        clock.first_response_missed = True
    if (
        clock.resolution_met_at is None
        and clock.paused_since is None
        and clock.resolution_due_at is not None
        and _late(now, clock.resolution_due_at)
    ):
        clock.resolution_missed = True


def apply_target(
    clock, target: TargetValues | None, *, start: datetime, now: datetime, calendar: Calendar | None
) -> None:
    """Set due times from `target` (None: no target applies). Never moves a met clock.

    Used at creation and when the category or priority changes. A clock that was already past
    due is marked missed before its due time moves."""
    _mark_missed_if_past_due(clock, now)
    empty = target or TargetValues(None, None, None, None, False)
    if clock.first_response_met_at is None:
        clock.first_response_minutes = empty.first_response_minutes
        clock.first_response_due_at = (
            add_business_seconds(start, empty.first_response_minutes * 60, calendar)
            if empty.first_response_minutes
            else None
        )
    if clock.resolution_met_at is None:
        clock.target_id = empty.target_id
        clock.calendar_id = empty.calendar_id
        clock.pause_on_third_party = empty.pause_on_third_party
        clock.resolution_minutes = empty.resolution_minutes
        clock.resolution_due_at = _resolution_due(clock, start, calendar)
    clock.calculated_at = now


def meet_first_response(clock, at: datetime) -> bool:
    """Meet the first-response clock at `at` if it is running. True when it changed."""
    if clock.first_response_due_at is None or clock.first_response_met_at is not None:
        return False
    clock.first_response_met_at = at
    if _late(at, clock.first_response_due_at):
        clock.first_response_missed = True
    return True


def entry_meets_first_response(
    *, kind: str, author_is_staff: bool, contact_outcome: str = ""
) -> bool:
    """A staff reply, or a logged contact that reached the client. Notes and events never do."""
    if kind == "reply":
        return author_is_staff
    if kind == "contact":
        return contact_outcome in REACHED_OUTCOMES
    return False


def sync_status(
    clock, status: str, *, start: datetime, now: datetime, calendar: Calendar | None
) -> None:
    """Move the resolution clock to the phase the request status asks for, at `now`.

    Works from the clock's own record of its phase, so it needs no previous status."""
    if clock.resolution_minutes is None and clock.resolution_due_at is None:
        target_phase = "stopped" if status in STOPPED_STATUSES else "running"
    else:
        target_phase = desired_phase(status, clock.pause_on_third_party)
    phase = current_phase(clock)
    if phase != target_phase:
        if phase == "paused":
            clock.paused_seconds = (clock.paused_seconds or 0) + business_seconds_between(
                clock.paused_since, now, calendar
            )
            clock.paused_since = None
            clock.resolution_due_at = _resolution_due(clock, start, calendar)
        elif phase == "stopped":
            # Reopened: the time spent resolved does not count.
            clock.paused_seconds = (clock.paused_seconds or 0) + business_seconds_between(
                clock.resolution_met_at, now, calendar
            )
            clock.resolution_met_at = None
            clock.resolution_due_at = _resolution_due(clock, start, calendar)
        if target_phase == "paused":
            _mark_missed_if_past_due(clock, now)
            clock.paused_since = now
        elif target_phase == "stopped" and clock.resolution_due_at is not None:
            clock.resolution_met_at = now
            if _late(now, clock.resolution_due_at):
                clock.resolution_missed = True
            # This run of the clock is over: a reopened request that passes its target again
            # gets a new breach notice (service_levels.jobs).
            clock.resolution_breach_noticed_at = None
    if status in STOPPED_STATUSES:
        meet_first_response(clock, now)


# Target precedence -------------------------------------------------------------------------

PRECEDENCE = (
    "category and priority",
    "category, any priority",
    "group and priority",
    "group, any priority",
    "any category, priority",
    "any category, any priority",
)


def target_rank(
    target_category_id: int | None,
    target_priority: str,
    *,
    category_id: int,
    group_id: int | None,
    priority: str,
) -> int | None:
    """Position of a target in the precedence order for a request, or None if it does not apply.

    0 is most specific. `target_priority` '' means any priority; `target_category_id` None means
    any category. A target set on a group applies to every category in it."""
    if target_priority not in ("", priority):
        return None
    any_priority = target_priority == ""
    if target_category_id is None:
        return 5 if any_priority else 4
    if target_category_id == category_id:
        return 1 if any_priority else 0
    if group_id is not None and target_category_id == group_id:
        return 3 if any_priority else 2
    return None


def select_target(targets: Iterable, *, category_id: int, group_id: int | None, priority: str):
    """The most specific of `targets` (objects with `category_id` and `priority`), or None."""
    best, best_rank = None, None
    for target in targets:
        rank = target_rank(
            target.category_id,
            target.priority,
            category_id=category_id,
            group_id=group_id,
            priority=priority,
        )
        if rank is not None and (best_rank is None or rank < best_rank):
            best, best_rank = target, rank
    return best
