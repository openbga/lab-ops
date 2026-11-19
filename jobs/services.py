"""Changes to the scheduled job settings: one service, for administrators."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from accounts.roles import held_roles, require_role
from core.audit import record_event

from .actor import locked_settings
from .locks import share_lock
from .models import CLOSE_AFTER_DAYS_MAX

STALE = "These settings changed after you opened them. Reload and check them before saving again."


def _days(value):
    """A whole number of days from 1 to CLOSE_AFTER_DAYS_MAX, or None. Accepts an int or the
    digits of one (the form's text); never a fraction or a yes/no value."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        days = None
    elif isinstance(value, int):
        days = value
    elif isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        digits = value.strip().lstrip("0") or "0"
        # More digits than the largest period has is out of range, without converting it.
        days = (
            int(digits)
            if len(digits) <= len(str(CLOSE_AFTER_DAYS_MAX))
            else CLOSE_AFTER_DAYS_MAX + 1
        )
    else:
        days = None
    if days is None:
        raise ValidationError({"close_resolved_after_days": "Enter a whole number of days."})
    if not 1 <= days <= CLOSE_AFTER_DAYS_MAX:
        raise ValidationError(
            {
                "close_resolved_after_days": "Enter a number of days from 1 to "
                f"{CLOSE_AFTER_DAYS_MAX}, or leave it empty."
            }
        )
    return days


@transaction.atomic
def update_settings(*, actor, expected_revision, close_resolved_after_days):
    """Set how many days after resolution the scheduled job closes a ticket (None: never).
    Administrators only; refused when the settings changed since the form was opened. Returns
    whether anything changed; a change is audited and advances the revision."""
    from accounts.models import User

    require_role(actor, "admin")
    days = _days(close_resolved_after_days)
    row = locked_settings()
    # The account and its roles as they are now, held until this change commits: a
    # deactivation or a removed role either came first and refuses this change, or waits for it,
    # and the roles are decided from the reading that holds them.
    share_lock(User, [actor.pk])
    current = User.objects.filter(pk=actor.pk, is_active=True, client__isnull=True).first()
    if current is None or "admin" not in held_roles(current):
        raise PermissionDenied("Your role does not permit this action.")
    if expected_revision is None or row.revision != expected_revision:
        raise ValidationError(STALE)
    if days == row.close_resolved_after_days:
        return False
    before = {"close_resolved_after_days": row.close_resolved_after_days}
    row.close_resolved_after_days = days
    row.revision += 1
    row.save(update_fields=["close_resolved_after_days", "revision"])
    record_event(
        actor=current,
        action="jobs.settings_changed",
        obj=row,
        before=before,
        after={"close_resolved_after_days": days},
    )
    return True
