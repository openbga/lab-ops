"""Clock display: durations, the per-clock and per-request state labels.

{% load clocks %}
  {{ target.resolution_minutes|duration }}       "4 h", "30 min", "1 h 30 min"
  {% clock_state request %}                      {"state", "label", "tone"} for a request
"""

from django import template
from django.utils import timezone

from service_levels.rules import (
    STATE_LABELS,
    STATE_TONES,
    first_response_state,
    request_state,
    resolution_state,
)

register = template.Library()


@register.filter
def duration(minutes):
    if minutes in (None, ""):
        return ""
    minutes = int(minutes)
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest} min"
    return f"{hours} h" if not rest else f"{hours} h {rest} min"


def _describe(state):
    return {"state": state, "label": STATE_LABELS[state], "tone": STATE_TONES[state]}


@register.simple_tag
def request_clock_state(request, now=None):
    """The request-level state. Uses the SQL annotation when the list added it."""
    state = getattr(request, "clock_state", None)
    if state is None:
        clock = getattr(request, "clock", None) if _has_clock(request) else None
        state = request_state(clock, now or timezone.now())
    return _describe(state)


@register.simple_tag
def clock_parts(clock, now=None):
    """[{"name", "state", "label", "tone", "due_at", "met_at", "minutes"}] for both clocks."""
    now = now or timezone.now()
    return [
        {
            "name": "First response",
            **_describe(first_response_state(clock, now)),
            "due_at": clock.first_response_due_at,
            "met_at": clock.first_response_met_at,
            "minutes": clock.first_response_minutes,
        },
        {
            "name": "Resolution",
            **_describe(resolution_state(clock, now)),
            "due_at": clock.resolution_due_at,
            "met_at": clock.resolution_met_at,
            "minutes": clock.resolution_minutes,
            "paused_since": clock.paused_since,
        },
    ]


def _has_clock(request):
    from django.core.exceptions import ObjectDoesNotExist

    try:
        request.clock  # noqa: B018
    except ObjectDoesNotExist:
        return False
    return True
