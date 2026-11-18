"""Display helpers for request pages: the status and priority tones of includes/status.html."""

from django import template

register = template.Library()

STATUS_TONES = {
    "open": "info",
    "waiting_client": "hold",
    "waiting_third_party": "hold",
    "resolved": "success",
    "closed": "neutral",
}
PRIORITY_TONES = {"urgent": "danger", "high": "warning", "normal": "neutral", "low": "neutral"}


@register.filter
def status_tone(status):
    return STATUS_TONES.get(status, "neutral")


@register.filter
def priority_tone(priority):
    return PRIORITY_TONES.get(priority, "neutral")


@register.filter
def person(user):
    if not user:
        return ""
    return user.get_full_name() or user.get_username()
