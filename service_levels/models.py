"""Service targets, business calendars and the clock each request carries.

docs/product.md "Service clocks". The arithmetic and rules are pure functions in
`business_time.py` and `rules.py`; `clocks.py` applies them to these rows.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from taxonomy.choices import Priority

from .business_time import WEEKDAY_LABELS, Calendar, HoursError, format_time_of_day, parse_hours


class BusinessCalendar(models.Model):
    name = models.CharField(max_length=120, unique=True)
    hours = models.JSONField(
        "weekly opening hours",
        default=dict,
        blank=True,
        help_text="Weekday to a list of [start, end] local times; a missing weekday is closed.",
    )
    is_active = models.BooleanField("active", default=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["name", "pk"]
        verbose_name = "business calendar"

    def __str__(self):
        return self.name

    def clean(self):
        try:
            days = parse_hours(self.hours or {})
        except HoursError as error:
            raise ValidationError(str(error)) from error
        if not any(days):
            raise ValidationError("Give at least one opening interval.")

    @property
    def days(self):
        return parse_hours(self.hours or {})

    def weekly_hours(self):
        """[(weekday label, 'HH:MM-HH:MM, ...' or '')] for display, Monday first."""
        days = self.days
        return [
            (
                label,
                ", ".join(f"{format_time_of_day(s)}-{format_time_of_day(e)}" for s, e in days[i]),
            )
            for i, label in enumerate(WEEKDAY_LABELS)
        ]

    def as_calendar(self, holidays=None):
        """The pure Calendar value in the deployment time zone, with this calendar's holidays."""
        if holidays is None:
            holidays = self.holidays.values_list("date", flat=True)
        return Calendar(_zone(), self.days, frozenset(holidays))


def _zone():
    from zoneinfo import ZoneInfo

    return ZoneInfo(settings.TIME_ZONE)


class Holiday(models.Model):
    calendar = models.ForeignKey(
        BusinessCalendar, on_delete=models.CASCADE, related_name="holidays"
    )
    date = models.DateField()
    name = models.CharField(max_length=120)

    class Meta:
        ordering = ["date", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["calendar", "date"],
                name="holiday_one_per_calendar_date",
                violation_error_message="This calendar already has a holiday on that date.",
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.date:%Y-%m-%d})"


class ServiceTarget(models.Model):
    name = models.CharField(
        max_length=120, help_text="What staff see, for example “Urgent tickets”."
    )
    category = models.ForeignKey(
        "taxonomy.RequestCategory",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="service_targets",
        help_text="A category or a whole group. Empty: any category.",
    )
    priority = models.CharField(
        max_length=10, choices=Priority.choices, blank=True, help_text="Empty: any priority."
    )
    first_response_minutes = models.PositiveIntegerField(
        "first response time (minutes)", null=True, blank=True
    )
    resolution_minutes = models.PositiveIntegerField(
        "resolution time (minutes)", null=True, blank=True
    )
    calendar = models.ForeignKey(
        BusinessCalendar,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="targets",
        help_text="Count only opening hours. Empty: around the clock.",
    )
    pause_on_third_party = models.BooleanField(
        "pause while waiting on a third party", default=False
    )
    is_active = models.BooleanField("active", default=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["pk"]
        verbose_name = "service target"
        constraints = [
            models.CheckConstraint(
                condition=Q(first_response_minutes__isnull=False)
                | Q(resolution_minutes__isnull=False),
                name="service_target_has_a_time",
                violation_error_message="Give a first-response time, a resolution time, or both.",
            ),
            models.CheckConstraint(
                condition=(Q(first_response_minutes__isnull=True) | Q(first_response_minutes__gt=0))
                & (Q(resolution_minutes__isnull=True) | Q(resolution_minutes__gt=0)),
                name="service_target_positive_times",
                violation_error_message="Times must be longer than zero.",
            ),
            models.UniqueConstraint(
                "category",
                "priority",
                condition=Q(is_active=True),
                nulls_distinct=False,
                name="service_target_one_active_per_scope",
                violation_error_message=(
                    "An active target already covers this category and priority. "
                    "Change that one, or deactivate it first."
                ),
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def category_label(self):
        if self.category_id is None:
            return "Any category"
        category = self.category
        return f"{category.name} (whole group)" if category.parent_id is None else category.name

    @property
    def priority_label(self):
        return self.get_priority_display() if self.priority else "Any priority"


class RequestClock(models.Model):
    """One per request with a target. Copies the target's values at calculation, so a later
    change to the target does not move a running clock until the request's category or priority
    changes."""

    request = models.OneToOneField(
        "desk.ServiceRequest", on_delete=models.CASCADE, related_name="clock"
    )
    target = models.ForeignKey(
        ServiceTarget, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    first_response_minutes = models.PositiveIntegerField(null=True, blank=True)
    resolution_minutes = models.PositiveIntegerField(null=True, blank=True)
    calendar = models.ForeignKey(
        BusinessCalendar, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    pause_on_third_party = models.BooleanField(default=False)
    first_response_due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    first_response_met_at = models.DateTimeField(null=True, blank=True)
    first_response_missed = models.BooleanField(default=False)
    resolution_due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    resolution_met_at = models.DateTimeField(null=True, blank=True)
    resolution_missed = models.BooleanField(default=False)
    paused_since = models.DateTimeField(null=True, blank=True)
    paused_seconds = models.PositiveBigIntegerField(
        default=0, help_text="Business seconds paused or resolved before a reopening."
    )
    calculated_at = models.DateTimeField(null=True, blank=True)
    # When the scheduled job (service_levels.jobs) recorded the breach notices of each clock:
    # one notice per clock run. Resolving clears the resolution clock's, so a reopened request
    # that passes its target again is noticed again.
    first_response_breach_noticed_at = models.DateTimeField(null=True, blank=True)
    resolution_breach_noticed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "ticket clock"

    def __str__(self):
        return f"Clock of ticket {self.request_id}"
