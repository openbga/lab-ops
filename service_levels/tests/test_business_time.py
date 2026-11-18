"""Business-time arithmetic (S3): pure, no database."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from service_levels.business_time import (
    Calendar,
    HoursError,
    add_business_seconds,
    business_seconds_between,
    hours_to_json,
    parse_hours,
)

NY = ZoneInfo("America/New_York")
HOUR = 3600
WEEKDAYS_8_TO_17 = {day: [["08:00", "17:00"]] for day in ("mon", "tue", "wed", "thu", "fri")}
EVERY_DAY = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def at(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=NY)


def office(holidays=()):
    return Calendar.from_hours(WEEKDAYS_8_TO_17, NY, holidays)


def test_around_the_clock_is_elapsed_time_across_daylight_saving():
    # 2026-03-08 has 23 hours in New York: 24 elapsed hours end at 13:00 local.
    assert add_business_seconds(at(2026, 3, 7, 12), 24 * HOUR, None) == at(2026, 3, 8, 13)
    assert business_seconds_between(at(2026, 3, 7, 12), at(2026, 3, 8, 13), None) == 24 * HOUR


@pytest.mark.parametrize(
    ("start", "hours", "due"),
    [
        (at(2026, 11, 16, 16), 2, at(2026, 11, 17, 9)),  # runs over closing into next morning
        (at(2026, 11, 16, 6), 1, at(2026, 11, 16, 9)),  # starts before opening
        (at(2026, 11, 16, 8), 9, at(2026, 11, 16, 17)),  # lands exactly on closing time
        (at(2026, 11, 16, 17), 1, at(2026, 11, 17, 9)),  # starts at closing time
        (at(2026, 11, 20, 16), 2, at(2026, 11, 23, 9)),  # Friday afternoon to Monday
        (at(2026, 11, 21, 10), 1, at(2026, 11, 23, 9)),  # starts on a Saturday
        (at(2026, 11, 16, 8), 3 * 9, at(2026, 11, 18, 17)),  # three business days
    ],
)
def test_weekday_office_hours(start, hours, due):
    calendar = office()
    assert add_business_seconds(start, hours * HOUR, calendar) == due
    assert business_seconds_between(start, due, calendar) == hours * HOUR


def test_holiday_closes_the_whole_day():
    calendar = office(holidays=[date(2026, 11, 26)])
    assert add_business_seconds(at(2026, 11, 25, 16), 2 * HOUR, calendar) == at(2026, 11, 27, 9)
    assert business_seconds_between(at(2026, 11, 25), at(2026, 11, 28), calendar) == 18 * HOUR


def test_holidays_belong_to_their_calendar():
    plain, closed = office(), office(holidays=[date(2026, 11, 26)])
    start = at(2026, 11, 26, 8)
    assert add_business_seconds(start, HOUR, plain) == at(2026, 11, 26, 9)
    assert add_business_seconds(start, HOUR, closed) == at(2026, 11, 27, 9)


def test_overnight_hours_run_on_across_midnight():
    hours = {day: [["22:00", "24:00"]] for day in ("mon", "tue", "wed", "thu", "fri")}
    for day in ("tue", "wed", "thu", "fri", "sat"):
        hours.setdefault(day, []).insert(0, ["00:00", "06:00"])
    calendar = Calendar.from_hours(hours, NY)
    assert add_business_seconds(at(2026, 11, 16, 23), 2 * HOUR, calendar) == at(2026, 11, 17, 1)
    # Saturday 05:00 + 2 h: one hour Saturday morning, the next from Monday 22:00.
    assert add_business_seconds(at(2026, 11, 21, 5), 2 * HOUR, calendar) == at(2026, 11, 23, 23)
    assert business_seconds_between(at(2026, 11, 16, 21), at(2026, 11, 17, 7), calendar) == 8 * HOUR


def test_overnight_holiday_closes_only_the_intervals_starting_that_date():
    hours = {"mon": [["22:00", "24:00"]], "tue": [["00:00", "06:00"]]}
    calendar = Calendar.from_hours(hours, NY, [date(2026, 11, 17)])
    # Monday's 22:00-24:00 still counts; Tuesday 00:00-06:00 is closed.
    assert business_seconds_between(at(2026, 11, 16), at(2026, 11, 18), calendar) == 2 * HOUR


def test_around_the_clock_calendar_counts_the_long_and_short_days():
    calendar = Calendar.from_hours({day: [["00:00", "24:00"]] for day in EVERY_DAY}, NY)
    # 2026-11-01 is 25 hours long, 2026-03-08 is 23.
    assert business_seconds_between(at(2026, 11, 1), at(2026, 11, 2), calendar) == 25 * HOUR
    assert business_seconds_between(at(2026, 3, 8), at(2026, 3, 9), calendar) == 23 * HOUR
    assert add_business_seconds(at(2026, 10, 31, 12), 24 * HOUR, calendar) == at(2026, 11, 1, 11)


def test_business_hours_on_the_daylight_saving_weekend():
    calendar = Calendar.from_hours({day: [["08:00", "17:00"]] for day in EVERY_DAY}, NY)
    due = add_business_seconds(at(2026, 3, 7, 16), 2 * HOUR, calendar)
    assert due == at(2026, 3, 8, 9)
    assert due - at(2026, 3, 7, 16) == timedelta(hours=16)  # one wall-clock hour skipped
    due = add_business_seconds(at(2026, 10, 31, 16), 2 * HOUR, calendar)
    assert due == at(2026, 11, 1, 9)
    assert due - at(2026, 10, 31, 16) == timedelta(hours=18)  # one wall-clock hour repeated


def test_interval_inside_the_skipped_hour_counts_nothing():
    calendar = Calendar.from_hours({"sun": [["02:00", "03:00"]]}, NY)
    assert business_seconds_between(at(2026, 3, 8), at(2026, 3, 9), calendar) == 0
    assert business_seconds_between(at(2026, 3, 15), at(2026, 3, 16), calendar) == HOUR


def test_repeated_hour_counts_twice():
    calendar = Calendar.from_hours({"sun": [["01:00", "02:00"]]}, NY)
    # 01:00 EDT to 02:00 EST is two real hours on the night the clocks go back.
    assert business_seconds_between(at(2026, 11, 1), at(2026, 11, 2), calendar) == 2 * HOUR


@pytest.mark.parametrize("seconds", [1, 59, HOUR, 9 * HOUR, 9 * HOUR + 1, 40 * HOUR, 500 * HOUR])
@pytest.mark.parametrize(
    "start",
    [at(2026, 3, 6, 15, 7), at(2026, 10, 30, 12), at(2026, 11, 20, 23, 59), at(2026, 12, 31, 8)],
)
def test_between_inverts_add(start, seconds):
    calendar = office(holidays=[date(2026, 12, 25), date(2027, 1, 1)])
    due = add_business_seconds(start, seconds, calendar)
    assert business_seconds_between(start, due, calendar) == seconds


def test_result_is_utc_and_zero_is_the_start():
    start = at(2026, 11, 21, 10)
    assert add_business_seconds(start, 0, office()) == start
    assert add_business_seconds(start, HOUR, office()).tzinfo is UTC


def test_calendar_without_hours_never_reaches_a_due_time():
    calendar = Calendar.from_hours({}, NY)
    assert not calendar.is_open_some_day
    assert add_business_seconds(at(2026, 11, 16), HOUR, calendar) is None
    assert business_seconds_between(at(2026, 11, 16), at(2026, 11, 20), calendar) == 0


def test_naive_datetimes_and_negative_durations_are_refused():
    with pytest.raises(ValueError):
        add_business_seconds(datetime(2026, 11, 16, 9), HOUR, office())
    with pytest.raises(ValueError):
        add_business_seconds(at(2026, 11, 16, 9), -1, office())
    assert business_seconds_between(at(2026, 11, 17), at(2026, 11, 16), office()) == 0


def test_hours_round_trip_and_allow_touching_intervals_and_midnight_end():
    data = {"mon": [["08:00", "12:00"], ["12:00", "17:00"]], "sat": [["20:00", "24:00"]]}
    assert hours_to_json(parse_hours(data)) == data


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"mon": [["09:00", "08:00"]]}, "Monday: 09:00-08:00 ends before it starts"),
        ({"tue": [["08:00", "12:00"], ["11:00", "17:00"]]}, "Tuesday: Opening intervals must be"),
        ({"wed": [["12:00", "17:00"], ["08:00", "11:00"]]}, "Wednesday: Opening intervals must be"),
        ({"thu": [["8am", "17:00"]]}, "Thursday: “8am” is not a time"),
        ({"fri": [["08:00", "24:30"]]}, "Friday: “24:30” is later than 24:00"),
        ({"fri": [["24:00", "24:00"]]}, "Friday: Opening hours must lie"),
        ({"sun": [["08:00"]]}, "Sunday:"),
        ({"monday": [["08:00", "17:00"]]}, "Unknown weekday key: monday"),
    ],
)
def test_invalid_hours_are_refused_with_the_day(data, message):
    with pytest.raises(HoursError) as error:
        parse_hours(data)
    assert str(error.value).startswith(message)
