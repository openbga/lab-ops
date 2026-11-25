"""The filters of the audit review workspace's lists (audit_review.sources.FILTERS) and of its
People tab (audit_review.people)."""

from datetime import date, timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.utils import timezone

from clients.models import Client
from core.forms import StyledForm
from core.ids import whole_number
from core.lookups import RemoteSelect
from desk.models import ServiceRequest

from . import hours
from .models import Source
from .sources import FILTERS, action_choices

ACTION_LABELS = {
    Source.CHANGES: ("Action", "Any action"),
    Source.ACCESS: ("What", "Any kind of read"),
    Source.SIGN_INS: ("Event", "Any event"),
    Source.USES: ("What", "Any use"),
}
# The filters shown in the toolbar's row; the rest are behind More filters.
INLINE = ("person", "organization", "action")


class PersonField(forms.ModelChoiceField):
    def label_from_instance(self, account):
        name = account.get_full_name()
        return f"{name} ({account.username})" if name else account.username


def _days(start, end):
    """How many days `start` to `end` covers, both included."""
    return (end - start).days + 1


def _days_before(day, days):
    try:
        return day - timedelta(days=days)
    except OverflowError:  # before the first day a date can hold
        return date.min


class ReviewFilter(StyledForm, forms.Form):
    person = PersonField(
        required=False,
        queryset=get_user_model().objects.all(),
        widget=RemoteSelect("review-accounts"),
        empty_label="Anyone",
    )
    organization = forms.ModelChoiceField(
        required=False,
        queryset=Client.objects.all(),
        widget=RemoteSelect("clients"),
        label="Client organization",
        empty_label="Any organization",
    )
    action = forms.ChoiceField(required=False)
    ticket = forms.CharField(
        required=False,
        max_length=24,
        label="Ticket number",
        help_text="As shown on the ticket, such as REQ-000123.",
    )
    date_from = forms.DateField(
        required=False,
        label="From",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    date_to = forms.DateField(
        required=False,
        label="To",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    outside_hours = forms.ModelChoiceField(
        required=False,
        queryset=hours.calendars(),
        label="Outside the hours of",
        empty_label="Any time",
        help_text="A business calendar. Needs From and To.",
    )

    def __init__(self, source, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.source = source
        for name in list(self.fields):
            if name not in FILTERS[source]:
                del self.fields[name]
        label, empty = ACTION_LABELS[source]
        self.fields["action"].label = label
        self.fields["action"].choices = [("", empty), *action_choices(source)]

    def clean_ticket(self):
        raw = self.cleaned_data.get("ticket", "").strip()
        if not raw:
            return None
        number = whole_number(raw.upper().removeprefix("REQ-").lstrip("0"))
        ticket = ServiceRequest.objects.filter(pk=number).first() if number else None
        if ticket is None:
            raise forms.ValidationError("No ticket has this number.")
        return ticket

    def clean(self):
        data = super().clean()
        start, end = data.get("date_from"), data.get("date_to")
        if start and end and start > end:
            self.add_error("date_to", "End date must be on or after the start date.")
        if data.get("outside_hours") is not None:
            if not start or not end:
                self.add_error(
                    "outside_hours",
                    "Set From and To to list what happened outside business hours.",
                )
            elif start <= end and _days(start, end) > hours.MOST_DAYS:
                self.add_error(
                    "date_to",
                    f"Business hours are checked over at most {hours.MOST_DAYS} days. "
                    "Narrow the dates.",
                )
        return data

    def recorded(self):
        """The accepted filters as a use records them: ids, codes and dates as strings, never
        text as typed. Only for a valid form."""
        recorded = {}
        for name, value in self.cleaned_data.items():
            if value in (None, ""):
                continue
            recorded[name] = str(value.pk) if hasattr(value, "pk") else str(value)
        return recorded

    def inline(self):
        return [name for name in INLINE if name in self.fields]

    def more(self):
        return [name for name in self.fields if name not in INLINE]


class CalendarField(forms.ChoiceField):
    """The People tab's business calendar: the first active calendar is offered as the empty
    choice, and its id, which a count's address names, is accepted too."""

    accepted = frozenset()

    def valid_value(self, value):
        return str(value) in self.accepted or super().valid_value(value)


# The People tab's period when From or To is left empty: the last 30 days, up to today.
DEFAULT_DAYS = 30
OUTSIDE = "outside"
NOT_JUDGED = "none"


class PeopleFilter(StyledForm, forms.Form):
    """The People tab's filters (audit_review.people). A period of at most hours.MOST_DAYS days:
    empty To is today and empty From is 30 days up to To, both in the deployment's time zone. The
    business calendar outside whose hours activity is counted: empty is the first active one by
    name, "none" judges no hours. Defaults are not filters: they make no chip, and the page names
    them (effective)."""

    date_from = forms.DateField(
        required=False,
        label="From",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    date_to = forms.DateField(
        required=False,
        label="To",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    activity = forms.ChoiceField(
        required=False,
        label="Activity",
        choices=[("", "All activity"), (OUTSIDE, "Only outside business hours")],
    )
    person = PersonField(
        required=False,
        queryset=get_user_model().objects.all(),
        widget=RemoteSelect("review-accounts"),
        empty_label="Anyone",
    )
    organization = forms.ModelChoiceField(
        required=False,
        queryset=Client.objects.all(),
        widget=RemoteSelect("clients"),
        label="Client organization",
        empty_label="Any organization",
        help_text="Its client portal accounts.",
    )
    calendar = CalendarField(required=False, label="Business hours")

    INLINE = ("date_from", "date_to", "activity")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calendars = {str(calendar.pk): calendar for calendar in hours.calendars()}
        choices = [(pk, calendar.name) for pk, calendar in self.calendars.items()]
        if choices:
            # The first calendar is the default: chosen, it sends nothing and makes no chip.
            self.fields["calendar"].choices = [("", choices[0][1]), *choices[1:]]
            self.fields["calendar"].choices.append((NOT_JUDGED, "Not judged"))
            self.fields["calendar"].accepted = frozenset(self.calendars)
            self.fields["calendar"].help_text = "Activity outside its opening hours is counted."
        else:
            del self.fields["calendar"]
            del self.fields["activity"]
        self.period = None

    def chip_value(self, value):
        """A chip's text for the calendar chosen (the first one has no choice of its own)."""
        calendar = self.calendars.get(value)
        return calendar.name if calendar else "Not judged"

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        today = timezone.localdate()
        start, end = data.get("date_from"), data.get("date_to")
        if end is None:
            end = max(today, start) if start else today
        if start is None:
            start = _days_before(end, DEFAULT_DAYS - 1)
        if start > end:
            self.add_error("date_to", "End date must be on or after the start date.")
        elif _days(start, end) > hours.MOST_DAYS:
            self.add_error(
                "date_to", f"A summary covers at most {hours.MOST_DAYS} days. Narrow the dates."
            )
        elif data.get("activity") == OUTSIDE and self.chosen_calendar() is None:
            self.add_error("activity", "Choose the business hours to count activity outside.")
        else:
            self.period = (start, end)
        return data

    def chosen_calendar(self):
        """The business calendar chosen, or the default, or None (not judged, or none active)."""
        if "calendar" not in self.fields:
            return None
        value = self.cleaned_data.get("calendar", "")
        if value == NOT_JUDGED:
            return None
        if value == "":
            return next(iter(self.calendars.values()), None)
        return self.calendars.get(value)

    def effective(self):
        """What the summary is drawn with, defaults included. Only for a valid form."""
        start, end = self.period
        return {
            "date_from": start,
            "date_to": end,
            "person": self.cleaned_data.get("person"),
            "organization": self.cleaned_data.get("organization"),
            "calendar": self.chosen_calendar(),
            "outside_only": self.cleaned_data.get("activity") == OUTSIDE,
        }

    def recorded(self):
        """What a use of the People tab records: the period, person, organization, calendar and
        activity as the summary applied them, defaults included, as ids, codes and dates."""
        effective = self.effective()
        recorded = {
            "date_from": effective["date_from"].isoformat(),
            "date_to": effective["date_to"].isoformat(),
        }
        for name in ("person", "organization"):
            if effective[name] is not None:
                recorded[name] = str(effective[name].pk)
        calendar = effective["calendar"]
        recorded["calendar"] = str(calendar.pk) if calendar else NOT_JUDGED
        if effective["outside_only"]:
            recorded["activity"] = OUTSIDE
        return recorded

    def inline(self):
        return [name for name in self.INLINE if name in self.fields]

    def more(self):
        return [name for name in self.fields if name not in self.INLINE]
