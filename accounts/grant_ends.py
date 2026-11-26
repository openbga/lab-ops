"""End dates of access grants: a capability given directly to an account (accounts.models.
CapabilityEnd) and a client portal account's portal access (clients.models.PortalAccess.ends_at).

A person chooses the last day the grant holds; the grant gives nothing from the start of the next
day in the laboratory's time zone (settings.TIME_ZONE, as set when the day is saved). What is
stored is that moment (`ends_at`), so a later change of the time zone does not move an end
already set; the day shown is read back from it in the zone set now.

Every check compares `ends_at` with the database's clock at the start of the transaction that
checks (transaction_timestamp(), TransactionTime), in the statement that reads the grant: every
server process uses the same clock, and every check made within one change or one page reading
agrees on whether a grant has ended. A change that checked before the end and commits after it is
a change of a holder: an end is a moment, not a change of the grant, so nothing waits for it. A
statement run on its own (outside a transaction) is its own transaction.

Nothing removes an ended grant: it stays on the account, giving nothing, until someone removes it
or sets a later day (which gives it again). Setting or moving an end is an access change: it takes
the account's access gate (accounts.acting.close_access) like a removal.
"""

from datetime import UTC, datetime, time, timedelta

from django.db.models import BooleanField, Case, DateTimeField, Func, Q, Value, When
from django.utils import timezone


class TransactionTime(Func):
    """The database's clock at the start of the current transaction (PostgreSQL's
    transaction_timestamp(), which now() also names)."""

    template = "transaction_timestamp()"
    output_field = DateTimeField()

    def __init__(self):
        super().__init__()


def ended(field="ends_at"):
    """A condition on `field`: the end has passed by the database's clock."""
    return Q(**{f"{field}__lte": TransactionTime()})


def in_force(field="ends_at"):
    """A condition on `field`: no end, or an end still to come by the database's clock."""
    return Q(**{f"{field}__isnull": True}) | Q(**{f"{field}__gt": TransactionTime()})


def has_ended(field="ends_at"):
    """An annotation: True when `field` holds an end that has passed by the database's clock,
    False otherwise (no end included). For pages that show an end, so they say what the checks
    decide."""
    return Case(
        When(ended(field), then=Value(True)), default=Value(False), output_field=BooleanField()
    )


def zone():
    return timezone.get_default_timezone()


def end_of(last_day):
    """The moment a grant whose last day is `last_day` (a date) ends: the start of the next day in
    the laboratory's time zone."""
    if last_day is None:
        return None
    return datetime.combine(last_day + timedelta(days=1), time.min, tzinfo=zone())


def last_day_of(ends_at):
    """The last day of a grant ending at `ends_at` (an aware moment), in the laboratory's time
    zone: the day of the moment just before it."""
    if ends_at is None:
        return None
    return timezone.localtime(ends_at - timedelta(microseconds=1), zone()).date()


def today():
    """Today in the laboratory's time zone, by the application's clock (for a form's own checks;
    the end itself is checked by the database's clock)."""
    return timezone.localdate(timezone.now(), zone())


PAST_DAY = "Choose today or a later day. To end it now, remove it instead."


def check_new_day(last_day, stored_last_day):
    """The error for a last day chosen in the past, or None. A day kept as stored, even one already
    past, is not a new choice and is never refused."""
    if last_day is None or last_day == stored_last_day:
        return None
    if last_day < today():
        return PAST_DAY
    return None


def iso(ends_at):
    """The audited and compared form of an end: its moment in UTC, ISO 8601, or None. One form
    whichever zone the moment came in, so the same moment always reads the same."""
    return ends_at.astimezone(UTC).isoformat() if ends_at is not None else None


# ------------------------------------------------------------------------------ whatever saves them


def _on_end_saved(sender, instance, raw=False, **kwargs):
    """An end set or moved: it can take access away, so it takes the account's access gate
    (accounts.acting.close_access), as a removal does."""
    if raw:
        return
    from .acting import close_access

    close_access([instance.user_id])


def _on_direct_grants(sender, instance, action, reverse, model, pk_set, **kwargs):
    """An end goes with its grant: removed with it, and a grant given anew starts without one."""
    from .models import CapabilityEnd

    if action not in ("post_add", "post_remove", "post_clear"):
        return
    ends = CapabilityEnd.objects.all()
    if not reverse:  # account.user_permissions
        ends = ends.filter(user_id=instance.pk)
        if action != "post_clear":
            ends = ends.filter(permission_id__in=pk_set or ())
    else:  # permission.user_set
        ends = ends.filter(permission_id=instance.pk)
        if action != "post_clear":
            ends = ends.filter(user_id__in=pk_set or ())
    ends.delete()


def connect():
    from django.db.models.signals import m2m_changed, pre_save

    from .models import CapabilityEnd, User

    pre_save.connect(_on_end_saved, sender=CapabilityEnd, dispatch_uid="grant-end-gate")
    m2m_changed.connect(
        _on_direct_grants, sender=User.user_permissions.through, dispatch_uid="grant-end-goes"
    )
