"""Business hours in the audit review workspace: which records fall outside the opening hours of one
of the laboratory's business calendars (service_levels.models.BusinessCalendar), the calendars its
service clocks count. A calendar's opening hours are its weekly hours in the deployment's time
zone, less its holidays, which are closed all day, as the service clocks count them
(service_levels.business_time.open_intervals). A record at the moment a calendar opens is inside
its hours; one at the moment it closes is outside.

The hours are read as the calendar stands when the page is drawn: a calendar edited since, or a
holiday added, changes which earlier records count as outside its hours.
"""

from datetime import date, timedelta

from django.db.models import BooleanField, F, Func
from django.db.models.expressions import RawSQL

from service_levels.business_time import open_intervals
from service_levels.models import BusinessCalendar

# At most this many days, the first and last included, are judged against a calendar at once.
MOST_DAYS = 366


def calendars():
    """The calendars a reviewer can judge by: the active ones, by name."""
    return BusinessCalendar.objects.filter(is_active=True).order_by("name", "pk")


def open_hours(calendar, first, last):
    """The opening hours of `calendar` on the local days `first` to `last`, as the text of a
    PostgreSQL tstzmultirange. The day before `first` is included for an opening that runs past
    midnight. A day whose opening a datetime cannot hold (around year 1 and year 9999 in some time
    zones) is left out, as closed; every other day keeps its hours."""
    value = calendar.as_calendar()
    day = first - timedelta(days=1) if first > date.min else first
    ranges = []
    while True:
        try:
            for begin, end in open_intervals(value, day, day):
                ranges.append(f'["{begin.isoformat()}","{end.isoformat()}")')
        except OverflowError:  # an opening of the first or last days a datetime can hold
            pass
        if day >= last or day == date.max:
            break
        day += timedelta(days=1)
    return "{" + ",".join(ranges) + "}"


class Outside(Func):
    """True where the moment in the column `field` falls outside `hours` (open_hours)."""

    output_field = BooleanField()
    template = "NOT (%(expressions)s)"
    arg_joiner = " <@ "

    def __init__(self, field, hours):
        super().__init__(F(field), RawSQL("%s::tstzmultirange", (hours,)))
