"""Business-time arithmetic for service clocks.

Pure functions over a `Calendar` value: no database, no settings. A calendar is a set of weekly
opening intervals in the laboratory's local wall-clock time, less whole-day holidays. A clock
without a calendar (`calendar=None`) runs around the clock.

Opening hours are minutes from local midnight. An interval may end at 24:00 (1440), so a night
shift is written as two intervals: Monday 22:00-24:00 and Tuesday 00:00-06:00, and time runs
on without a break across midnight. A holiday closes every interval that starts on that date.

Every boundary is turned into an absolute instant through `zoneinfo`, so a day with a daylight
saving change is 23 or 25 hours long, and business seconds are real elapsed seconds. A local time
that does not exist (the hour skipped in spring) is read with the offset in force before the
change, as `zoneinfo` does with `fold=0`; an interval that collapses to nothing is skipped.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

MINUTES_PER_DAY = 24 * 60
# Weekday keys of the stored hours, in `date.weekday()` order.
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_LABELS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# How far ahead `add_business_seconds` looks for opening hours before giving up.
SEARCH_DAYS = 3 * 366
_TIME = re.compile(r"^([01]\d|2[0-4]):([0-5]\d)$")

Interval = tuple[int, int]


class HoursError(ValueError):
    """Opening hours that cannot be used: bad time, end not after start, or overlap."""


def parse_time_of_day(text: str) -> int:
    """'HH:MM' to minutes from midnight; '24:00' is allowed as an end of day."""
    match = _TIME.match(text.strip()) if isinstance(text, str) else None
    if not match:
        raise HoursError(f"“{text}” is not a time written as HH:MM.")
    minutes = int(match.group(1)) * 60 + int(match.group(2))
    if minutes > MINUTES_PER_DAY:
        raise HoursError(f"“{text}” is later than 24:00.")
    return minutes


def format_time_of_day(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def check_day(intervals: Iterable[Interval]) -> tuple[Interval, ...]:
    """Validate one day's intervals: start before end, within the day, in order, no overlap.

    Intervals may touch (08:00-12:00 then 12:00-17:00)."""
    result = tuple((int(start), int(end)) for start, end in intervals)
    previous_end = None
    for start, end in result:
        if not 0 <= start < MINUTES_PER_DAY or not 0 < end <= MINUTES_PER_DAY:
            raise HoursError("Opening hours must lie between 00:00 and 24:00.")
        if end <= start:
            raise HoursError(
                f"{format_time_of_day(start)}-{format_time_of_day(end)} ends before it starts. "
                "Split a night shift at midnight: 22:00-24:00, then 00:00-06:00 the next day."
            )
        if previous_end is not None and start < previous_end:
            raise HoursError("Opening intervals must be in time order and must not overlap.")
        previous_end = end
    return result


def parse_hours(data: Mapping[str, Iterable]) -> tuple[tuple[Interval, ...], ...]:
    """Stored JSON hours ({"mon": [["07:00", "19:00"]], ...}) to seven validated days.

    A missing weekday is closed. Raises HoursError naming the day."""
    unknown = set(data) - set(WEEKDAYS)
    if unknown:
        raise HoursError(f"Unknown weekday key: {', '.join(sorted(unknown))}.")
    days = []
    for key, label in zip(WEEKDAYS, WEEKDAY_LABELS, strict=True):
        try:
            pairs = [
                (parse_time_of_day(start), parse_time_of_day(end))
                for start, end in data.get(key, ())
            ]
            days.append(check_day(pairs))
        except (HoursError, TypeError, ValueError) as error:
            raise HoursError(f"{label}: {error}") from error
    return tuple(days)


def hours_to_json(days: Iterable[Iterable[Interval]]) -> dict[str, list[list[str]]]:
    """Seven days of intervals to the stored JSON form (closed days omitted)."""
    return {
        key: [[format_time_of_day(start), format_time_of_day(end)] for start, end in intervals]
        for key, intervals in zip(WEEKDAYS, days, strict=True)
        if intervals
    }


@dataclass(frozen=True)
class Calendar:
    """Weekly opening hours (Monday first) in `tz`, less `holidays`."""

    tz: ZoneInfo
    days: tuple[tuple[Interval, ...], ...]
    holidays: frozenset[date] = field(default_factory=frozenset)

    def __post_init__(self):
        if len(self.days) != 7:
            raise HoursError("A calendar needs seven days of opening hours.")

    @classmethod
    def from_hours(cls, hours: Mapping, tz: ZoneInfo | str, holidays: Iterable[date] = ()):
        zone = ZoneInfo(tz) if isinstance(tz, str) else tz
        return cls(zone, parse_hours(hours), frozenset(holidays))

    @property
    def is_open_some_day(self) -> bool:
        return any(self.days)


def _instant(day: date, minute: int, tz: ZoneInfo) -> datetime:
    if minute >= MINUTES_PER_DAY:
        day, minute = day + timedelta(days=1), minute - MINUTES_PER_DAY
    local = datetime(day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=tz)
    return local.astimezone(UTC)


def open_intervals(
    calendar: Calendar, first: date, last: date
) -> Iterator[tuple[datetime, datetime]]:
    """Opening intervals as UTC instants for local dates first..last inclusive, in order."""
    day = first
    while day <= last:
        if day not in calendar.holidays:
            for start, end in calendar.days[day.weekday()]:
                begin, finish = _instant(day, start, calendar.tz), _instant(day, end, calendar.tz)
                if finish > begin:
                    yield begin, finish
        day += timedelta(days=1)


def _require_aware(*moments: datetime) -> None:
    for moment in moments:
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("Business-time arithmetic needs time-zone-aware datetimes.")


def add_business_seconds(
    start: datetime, seconds: int, calendar: Calendar | None
) -> datetime | None:
    """The instant at which `seconds` of open time have passed since `start`.

    Without a calendar this is plain elapsed time. A due time that lands exactly on closing time
    stays there (17:00), not the next opening. Returns None when the calendar has no open time
    within SEARCH_DAYS (for example a calendar with no hours at all). The result is in UTC."""
    _require_aware(start)
    if seconds < 0:
        raise ValueError("A clock cannot run backwards.")
    start = start.astimezone(UTC)
    if calendar is None or seconds == 0:
        return start + timedelta(seconds=seconds)
    remaining = timedelta(seconds=seconds)
    first = start.astimezone(calendar.tz).date() - timedelta(days=1)
    for begin, finish in open_intervals(calendar, first, first + timedelta(days=SEARCH_DAYS)):
        if finish <= start:
            continue
        begin = max(begin, start)
        if remaining <= finish - begin:
            return begin + remaining
        remaining -= finish - begin
    return None


def business_seconds_between(start: datetime, end: datetime, calendar: Calendar | None) -> int:
    """Whole seconds of open time from `start` to `end` (0 when end is not after start)."""
    _require_aware(start, end)
    # Same-tzinfo subtraction is wall-clock arithmetic in Python; work in UTC.
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if end <= start:
        return 0
    if calendar is None:
        return int((end - start).total_seconds())
    first = start.astimezone(calendar.tz).date() - timedelta(days=1)
    last = end.astimezone(calendar.tz).date()
    total = timedelta(0)
    for begin, finish in open_intervals(calendar, first, last):
        low, high = max(begin, start), min(finish, end)
        if high > low:
            total += high - low
    return int(total.total_seconds())
