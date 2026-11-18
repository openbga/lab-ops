"""Forms for business calendars, holidays and service targets (S1, S2)."""

import re

from django import forms

from core.forms import REASON_HELP, ActiveFilter, GroupedForm, StyledForm
from core.ids import whole_number
from taxonomy.choices import Priority
from taxonomy.models import RequestCategory

from .business_time import (
    MINUTES_PER_DAY,
    WEEKDAY_LABELS,
    WEEKDAYS,
    HoursError,
    check_day,
    format_time_of_day,
    hours_to_json,
    parse_hours,
)
from .models import BusinessCalendar, Holiday, ServiceTarget

HOURS_HELP = (
    "For example 07:00-19:00, or 7-19. Separate intervals with commas. Leave empty when closed. "
    "Split a night shift at midnight: 22:00-24:00 here, 00:00-06:00 on the next day."
)
_TIME = re.compile(r"^(\d{1,2})(?::?(\d{2}))?$")
_SEPARATOR = re.compile(r"\s*(?:-|–|—|\bto\b)\s*")


def _minutes(text):
    match = _TIME.match(text.strip())
    if not match:
        raise HoursError(f"“{text.strip()}” is not a time. Use HH:MM, 24-hour.")
    hours, minutes = int(match.group(1)), int(match.group(2) or 0)
    if minutes > 59 or hours * 60 + minutes > MINUTES_PER_DAY:
        raise HoursError(f"“{text.strip()}” is not a time between 00:00 and 24:00.")
    return hours * 60 + minutes


def parse_day_text(text):
    """'07:00-12:00, 13:00-17:00' (also '7-12, 13-17', '0700-1200') to validated intervals."""
    intervals = []
    for part in filter(None, (piece.strip() for piece in (text or "").split(","))):
        bounds = _SEPARATOR.split(part)
        if len(bounds) != 2:
            raise HoursError(f"Write “{part}” as start-end, for example 08:00-17:00.")
        intervals.append((_minutes(bounds[0]), _minutes(bounds[1])))
    return check_day(intervals)


def day_text(intervals):
    return ", ".join(f"{format_time_of_day(s)}-{format_time_of_day(e)}" for s, e in intervals)


class CalendarForm(GroupedForm, StyledForm, forms.ModelForm):
    groups = (
        ("Calendar", (("name", "col-12"),)),
        ("Weekly opening hours", tuple((f"hours_{key}", "col-md-6") for key in WEEKDAYS)),
        ("Status", (("is_active", "col-12"),)),
        ("Change record", (("reason", "col-12"),)),
    )

    class Meta:
        model = BusinessCalendar
        fields = ["name"]
        labels = {"name": "Calendar name"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        days = parse_hours(self.instance.hours or {}) if self.instance.pk else [()] * 7
        for index, (key, label) in enumerate(zip(WEEKDAYS, WEEKDAY_LABELS, strict=True)):
            self.fields[f"hours_{key}"] = forms.CharField(
                label=label,
                required=False,
                max_length=200,
                initial=day_text(days[index]),
                help_text=HOURS_HELP if index == 0 else "",
                widget=forms.TextInput(
                    attrs={
                        "class": "form-control",
                        "autocomplete": "off",
                        "placeholder": "Closed",
                        "spellcheck": "false",
                    }
                ),
            )

    def clean(self):
        data = super().clean()
        days = []
        for key in WEEKDAYS:
            try:
                days.append(parse_day_text(data.get(f"hours_{key}", "")))
            except HoursError as error:
                self.add_error(f"hours_{key}", str(error))
                days.append(())
        if not self.errors and not any(days):
            raise forms.ValidationError("Give at least one opening interval.")
        data["hours"] = hours_to_json(days)
        self.instance.hours = data["hours"]
        return data

    def service_data(self):
        data = {"name": self.cleaned_data["name"], "hours": self.cleaned_data["hours"]}
        if "is_active" in self.cleaned_data:
            data["is_active"] = self.cleaned_data["is_active"]
        return data


class CalendarEditForm(CalendarForm):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    is_active = forms.BooleanField(
        label="Active",
        required=False,
        help_text="An inactive calendar is not offered for new targets.",
    )
    reason = forms.CharField(
        label="Reason for change", help_text=REASON_HELP, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["is_active"].initial = self.instance.is_active


class HolidayForm(StyledForm, forms.ModelForm):
    class Meta:
        model = Holiday
        fields = ["date", "name"]
        labels = {"name": "Holiday name"}
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}


class HolidayRemoveForm(StyledForm, forms.Form):
    reason = forms.CharField(
        label="Reason for removal", help_text=REASON_HELP, widget=forms.Textarea(attrs={"rows": 2})
    )


DURATION_UNITS = (("minutes", "minutes"), ("hours", "hours"))
UNIT_MINUTES = {"minutes": 1, "hours": 60}


class TargetForm(GroupedForm, StyledForm, forms.ModelForm):
    first_response_time = forms.CharField(
        label="First-response time",
        required=False,
        widget=forms.TextInput(attrs={"inputmode": "numeric"}),
    )
    first_response_unit = forms.ChoiceField(label="Unit", choices=DURATION_UNITS, initial="hours")
    resolution_time = forms.CharField(
        label="Resolution time",
        required=False,
        widget=forms.TextInput(attrs={"inputmode": "numeric"}),
    )
    resolution_unit = forms.ChoiceField(label="Unit", choices=DURATION_UNITS, initial="hours")
    groups = (
        ("Target", (("name", "col-12"),)),
        ("Applies to", (("category", "col-md-8"), ("priority", "col-md-4"))),
        (
            "Times",
            (
                ("first_response_time", "col-8 col-md-4"),
                ("first_response_unit", "col-4 col-md-2"),
                ("resolution_time", "col-8 col-md-4"),
                ("resolution_unit", "col-4 col-md-2"),
                ("calendar", "col-12"),
                ("pause_on_third_party", "col-12"),
            ),
        ),
        ("Status", (("is_active", "col-12"),)),
        ("Change record", (("reason", "col-12"),)),
    )

    class Meta:
        model = ServiceTarget
        fields = ["name", "category", "priority", "calendar", "pause_on_third_party"]
        labels = {"name": "Target name", "category": "Category or group"}
        help_texts = {
            "category": "A group covers every category in it. Empty: any category.",
            "calendar": "Times count only the calendar's opening hours. Empty: around the clock.",
            "pause_on_third_party": "The resolution clock always pauses while waiting on the "
            "client.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = self.instance
        categories = RequestCategory.objects.filter(is_active=True)
        calendars = BusinessCalendar.objects.filter(is_active=True)
        if instance.pk:
            categories |= RequestCategory.objects.filter(pk=instance.category_id)
            calendars |= BusinessCalendar.objects.filter(pk=instance.calendar_id)
        self.fields["category"].queryset = categories.select_related("parent").order_by(
            "parent__position", "position", "name"
        )
        self.fields["category"].label_from_instance = lambda c: (
            f"{c.name} (whole group)" if c.parent_id is None else f"{c.parent.name}: {c.name}"
        )
        self.fields["category"].empty_label = "Any category"
        self.fields["priority"].choices = [("", "Any priority"), *Priority.choices]
        self.fields["calendar"].queryset = calendars.order_by("name")
        self.fields["calendar"].empty_label = "Around the clock"
        for prefix in ("first_response", "resolution"):
            minutes = getattr(instance, f"{prefix}_minutes")
            if minutes:
                hours = minutes % 60 == 0
                self.initial[f"{prefix}_time"] = str(minutes // 60 if hours else minutes)
                self.initial[f"{prefix}_unit"] = "hours" if hours else "minutes"

    def _duration(self, prefix):
        text = (self.cleaned_data.get(f"{prefix}_time") or "").strip()
        if not text:
            return None
        if not whole_number(text):
            self.add_error(f"{prefix}_time", "Enter a whole number greater than zero.")
            return None
        return int(text) * UNIT_MINUTES[self.cleaned_data.get(f"{prefix}_unit") or "hours"]

    def clean(self):
        data = super().clean()
        data["first_response_minutes"] = self._duration("first_response")
        data["resolution_minutes"] = self._duration("resolution")
        if (
            not self.errors
            and data["first_response_minutes"] is None
            and data["resolution_minutes"] is None
        ):
            raise forms.ValidationError("Give a first-response time, a resolution time, or both.")
        return data

    def service_data(self):
        data = {
            name: self.cleaned_data[name]
            for name in (*self.Meta.fields, "first_response_minutes", "resolution_minutes")
        }
        if "is_active" in self.cleaned_data:
            data["is_active"] = self.cleaned_data["is_active"]
        return data


class TargetEditForm(TargetForm):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    is_active = forms.BooleanField(label="Active", required=False)
    reason = forms.CharField(
        label="Reason for change", help_text=REASON_HELP, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["is_active"].initial = self.instance.is_active
        self.fields["is_active"].help_text = (
            "Tickets keep the times they were given; new and recalculated tickets use the "
            "next most specific target."
        )


class TargetFilter(ActiveFilter):
    pass
