"""Demonstration seed for business calendars and service targets (core.seeding).

The targets are demonstration values, labelled as such: the product ships no service level.
Clocks need no seed: each request seeded afterwards gets one from these targets.
"""

from datetime import timedelta

from django.utils import timezone

from accounts.models import User
from taxonomy.models import RequestCategory

from .models import BusinessCalendar, ServiceTarget
from .services import add_holiday, create_calendar, create_target

CALENDAR = "Laboratory hours"
HOURS = {
    **{day: [["07:00", "19:00"]] for day in ("mon", "tue", "wed", "thu", "fri")},
    "sat": [["08:00", "12:00"]],
}
PICKUP_HOURS = 24  # pickups, around the clock (below)
BUSINESS_DAY = 12 * 60  # one weekday of the calendar above, in minutes
# (priority, first response minutes, resolution minutes)
TARGETS = (
    ("urgent", 30, 4 * 60),
    ("high", 60, BUSINESS_DAY),
    ("normal", 4 * 60, 3 * BUSINESS_DAY),
    ("low", BUSINESS_DAY, 5 * BUSINESS_DAY),
)


def _upcoming_weekdays(count, start_in_days):
    day = timezone.localdate() + timedelta(days=start_in_days)
    found = []
    while len(found) < count:
        if day.weekday() < 5:
            found.append(day)
            day += timedelta(days=17)
        day += timedelta(days=1)
    return found


def seed_setup(ctx):
    if BusinessCalendar.objects.filter(name=CALENDAR).exists():
        return
    admin = User.objects.get(username="demo_admin")
    calendar = create_calendar(actor=admin, name=CALENDAR, hours=HOURS, reason="Demonstration seed")
    for number, day in enumerate(_upcoming_weekdays(2, 9), start=1):
        add_holiday(
            actor=admin, calendar_id=calendar.pk, date=day, name=f"Example holiday {number}"
        )
    if ServiceTarget.objects.exists():
        return
    # Courier pickups are worked around the clock: a target without a calendar, so the story
    # (desk/story.py) can show pickups due soon whatever time of day or week the reset runs.
    pickup = RequestCategory.objects.filter(key="pickup").first()
    if pickup is not None:
        create_target(
            actor=admin,
            name="Demonstration: pickups, around the clock",
            category=pickup,
            first_response_minutes=60,
            resolution_minutes=PICKUP_HOURS * 60,
            reason="Demonstration seed",
        )
    for priority, first, resolution in TARGETS:
        create_target(
            actor=admin,
            name=f"Demonstration: {priority} priority",
            priority=priority,
            first_response_minutes=first,
            resolution_minutes=resolution,
            calendar=calendar,
            pause_on_third_party=priority in ("normal", "low"),
            reason="Demonstration seed",
        )


def seed_records(ctx):
    """Nothing: clocks follow the requests the desk seeds."""
