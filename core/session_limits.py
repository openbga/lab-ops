"""The laboratory's session limits: after how long without activity a session is signed out, and
how long a session may last at most, separately for staff and for client portal accounts
(accounts.sessions applies them on every request).

Each value is set within fixed bounds: 5 to 60 minutes without activity and 1 to 24 hours at
most. The starting values, 30 minutes and 12 hours, apply to an audience whose limits were never
saved. The bounds keep both within NIST SP 800-63B-4's recommendation for AAL2 (§2.2.3: an
inactivity timeout of no more than 1 hour and an overall timeout of no more than 24 hours). That
is a recommendation, not a rule: the HIPAA Security Rule's automatic logoff (45 CFR
§164.312(a)(2)(iii), addressable) sets no period, and choosing one is the laboratory's decision.

The admin role changes them on the Security settings page, with a reason. A save holds the acting
account (accounts.acting), takes the limits' lock, checks the revision the form showed and the
role as the account is held, and records one audit event with the values before and after.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction

from accounts.acting import hold_actor
from accounts.roles import has_role

from .audit import record_event
from .models import AuditEvent, SessionLimit

ROLES = ("admin",)
ACTION = "lab.session_limits_changed"
FIELDS = ("inactivity_minutes", "maximum_hours")
DEFAULTS = {"inactivity_minutes": 30, "maximum_hours": 12}
BOUNDS = {"inactivity_minutes": (5, 60), "maximum_hours": (1, 24)}
# The advisory lock (PostgreSQL's two-key form; see accounts.acting and core.demo for the others)
# that one save of the limits holds at a time, the first save of an audience included.
LIMITS_LOCK = (19558, 0)


def can_manage(user):
    return has_role(user, *ROLES)


def limits():
    """The staff limits and the client portal limits, in that order. An audience never saved is
    an unsaved SessionLimit with the starting values and revision 0."""
    rows = {row.audience: row for row in SessionLimit.objects.all()}
    return [
        rows.get(audience) or SessionLimit(audience=audience, **DEFAULTS)
        for audience in SessionLimit.Audience.values
    ]


def values(limit):
    return {name: getattr(limit, name) for name in FIELDS}


def _check_bounds(new):
    errors = {}
    for name, (low, high) in BOUNDS.items():
        value = new.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            unit = "minutes" if name == "inactivity_minutes" else "hours"
            errors[name] = f"Choose from {low} to {high} {unit}."
    if errors:
        raise ValidationError(errors)


@transaction.atomic
def save_limits(*, actor, audience, new, expected_revision, reason):
    """Set one audience's limits, with a reason. Returns (limit, changed).

    `expected_revision` is the limits' revision the form showed (0 for limits never saved): a
    change saved in between refuses this one. Sessions already open follow the new limits from
    their next request: one idle longer than a shortened limit is then signed out.
    """
    if audience not in SessionLimit.Audience.values:
        raise ValueError(f"Unknown audience {audience!r}.")
    current = hold_actor(actor)  # before any other lock (accounts.acting)
    if current is None or not can_manage(current):
        raise PermissionDenied("Your role does not permit this action.")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required."})
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", list(LIMITS_LOCK))
    limit = SessionLimit.objects.select_for_update().filter(audience=audience).first()
    if limit is None:
        limit = SessionLimit(audience=audience, **DEFAULTS)
    if expected_revision is None or limit.revision != expected_revision:
        raise ValidationError(
            f"The {limit.get_audience_display().lower()} session limits were changed after you "
            "opened this form. Your input has been kept; reload and compare the current limits "
            "before saving again."
        )
    _check_bounds(new)
    before = values(limit)
    after = {name: new[name] for name in FIELDS}
    if after == before:
        return limit, False
    for name, value in after.items():
        setattr(limit, name, value)
    limit.revision += 1
    limit.save()
    record_event(
        actor=current,
        action=ACTION,
        obj=limit,
        before={"audience": audience, **before},
        after={"audience": audience, **after},
        reason=reason,
    )
    return limit, True


def history(limit=20):
    """The newest changes to the session limits, newest first."""
    return (
        AuditEvent.objects.filter(action=ACTION)
        .select_related("actor")
        .order_by("-occurred_at")[:limit]
    )
