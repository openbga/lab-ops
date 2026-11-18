"""Clock state in SQL, for request lists, filters and dashboard counts.

Mirrors `rules.request_state` exactly; `tests/test_states.py` checks the two agree over a table
of clocks. Works on any queryset of desk.ServiceRequest (the clock is `request.clock`, absent
when no target applies).
"""

from datetime import timedelta

from django.db.models import (
    Case,
    CharField,
    DateTimeField,
    DurationField,
    ExpressionWrapper,
    F,
    Q,
    Value,
    When,
)
from django.db.models.functions import Greatest, Least
from django.utils import timezone

from .rules import DUE_SOON_FLOOR, STATES

# Per-clock codes. A running clock (not met) and a met one are told apart, as rules.combine_states
# does: a clock met late is "missed" history, a running clock already past a miss is "missed_run".
_RUNNING_ORDER = ("overdue", "missed_run", "due_soon", "paused", "on_track")


def _clock_code(prefix, now, *, pausable):
    met, missed = f"clock__{prefix}_met_at", f"clock__{prefix}_missed"
    threshold = Greatest(
        ExpressionWrapper(
            F(f"clock__{prefix}_minutes") * Value(timedelta(seconds=12)),
            output_field=DurationField(),
        ),
        Value(DUE_SOON_FLOOR),
        output_field=DurationField(),
    )
    soon = ExpressionWrapper(Value(now) + threshold, output_field=DateTimeField())
    whens = [
        When(**{f"clock__{prefix}_due_at__isnull": True}, then=Value("none")),
        When(Q(**{f"{met}__isnull": False, missed: True}), then=Value("missed")),
        When(Q(**{f"{met}__isnull": False}), then=Value("met")),
    ]
    if pausable:
        whens += [
            When(Q(clock__paused_since__isnull=False, **{missed: True}), then=Value("missed_run")),
            When(clock__paused_since__isnull=False, then=Value("paused")),
        ]
    whens += [
        When(Q(**{f"clock__{prefix}_due_at__lt": Value(now)}), then=Value("overdue")),
        When(Q(**{missed: True}), then=Value("missed_run")),
        When(Q(**{f"clock__{prefix}_due_at__lt": soon}), then=Value("due_soon")),
    ]
    return Case(*whens, default=Value("on_track"), output_field=CharField())


def annotate_clock_state(queryset, now=None):
    """Annotate `clock_state` (one of rules.STATES) and `clock_due_at` (the earliest due time of
    a clock still running and not paused; null otherwise), computed in SQL."""
    now = now or timezone.now()
    queryset = queryset.annotate(
        _clock_first=_clock_code("first_response", now, pausable=False),
        _clock_resolution=_clock_code("resolution", now, pausable=True),
    )
    either = lambda code: Q(_clock_first=code) | Q(_clock_resolution=code)  # noqa: E731
    state = Case(
        *(
            When(either(code), then=Value("missed" if code == "missed_run" else code))
            for code in _RUNNING_ORDER
        ),
        When(either("missed"), then=Value("missed")),
        When(either("met"), then=Value("met")),
        default=Value("none"),
        output_field=CharField(),
    )
    first_due = Case(
        When(clock__first_response_met_at__isnull=True, then=F("clock__first_response_due_at")),
        output_field=DateTimeField(),
    )
    resolution_due = Case(
        When(
            clock__resolution_met_at__isnull=True,
            clock__paused_since__isnull=True,
            then=F("clock__resolution_due_at"),
        ),
        output_field=DateTimeField(),
    )
    return queryset.annotate(
        clock_state=state,
        clock_due_at=Least(first_due, resolution_due, output_field=DateTimeField()),
    )


def filter_clock_state(queryset, state, now=None):
    """`queryset` narrowed to requests whose clock is in `state` (one of rules.STATES)."""
    if state not in STATES:
        raise ValueError(f"Unknown clock state {state!r}.")
    return annotate_clock_state(queryset, now).filter(clock_state=state)
