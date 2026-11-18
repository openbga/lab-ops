"""Display helpers for nonconformance pages: the tones of includes/status.html for a record's
status, its severity and an action's status (the one place this module maps states to tones)."""

from django import template

register = template.Library()

STATUS_TONES = {
    "open": "info",
    "investigating": "progress",
    "corrective_action": "progress",
    "verification": "progress",
    "closed": "neutral",
}
SEVERITY_TONES = {"minor": "neutral", "major": "warning", "critical": "danger"}
ACTION_TONES = {"open": "info", "done": "success", "cancelled": "neutral"}


@register.filter
def nc_status_tone(status):
    return STATUS_TONES.get(status, "neutral")


@register.filter
def severity_tone(severity):
    return SEVERITY_TONES.get(severity, "neutral")


@register.filter
def action_tone(status):
    return ACTION_TONES.get(status, "neutral")
