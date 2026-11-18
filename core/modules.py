"""Optional modules: parts of the application a laboratory switches on (docs/product.md
"Optional modules").

A module that is off shows no page, menu entry or link, and its addresses answer 404. Its
records are kept, unchanged, and reappear when it is switched on again. The desk works with
every module off. The admin role switches a module, with a reason; the change is audited.

A write in a module takes `lock_enabled(key)`, a shared row lock on the module's switch, so
switching the module off waits for writes already under way and a write that starts after it
finds the module off.
"""

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.http import Http404

from accounts.roles import has_role, require_role

from .audit import record_event
from .models import LabModule

# key: (name, what it adds). Order is the order the laboratory profile lists them.
MODULES = {
    "nonconformance": (
        "Nonconformance and corrective action",
        "Records of problems found in the laboratory's work, with containment, the decision on "
        "reported results, root cause, actions with owners and due dates, and an effectiveness "
        "check.",
    ),
}
MODULE_ROLES = ("admin",)


def _known(key):
    if key not in MODULES:
        raise ValueError(f"Unknown module {key!r}.")


def is_enabled(key):
    _known(key)
    return LabModule.objects.filter(key=key, is_enabled=True).exists()


def require_enabled(key):
    """404 for a page of a module that is off: its pages do not exist while it is off."""
    if not is_enabled(key):
        raise Http404("This module is not in use.")


def lock_enabled(key):
    """In a write's transaction: refuse the write unless the module is on, and hold a shared lock
    on its switch until the transaction ends (FOR SHARE: writes do not wait for each other)."""
    _known(key)
    if not connection.in_atomic_block:
        raise RuntimeError("lock_enabled() needs a transaction.")
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT is_enabled FROM {LabModule._meta.db_table} WHERE key = %s FOR SHARE", [key]
        )
        row = cursor.fetchone()
    if not (row and row[0]):
        raise Http404("This module is not in use.")


def can_manage(user):
    return has_role(user, *MODULE_ROLES)


def states():
    """Every module with its name, description, whether it is on and its revision (the stale-edit
    check of the switch form; 0 before its first switch)."""
    rows = {row.key: row for row in LabModule.objects.filter(key__in=MODULES)}
    result = []
    for key, (name, description) in MODULES.items():
        row = rows.get(key)
        result.append(
            {
                "key": key,
                "name": name,
                "description": description,
                "is_enabled": bool(row and row.is_enabled),
                "revision": row.revision if row else 0,
            }
        )
    return result


@transaction.atomic
def set_enabled(*, actor, key, enabled, expected_revision, reason):
    """Switch a module on or off, with a reason. `expected_revision` is the revision the form
    showed; a switch in between refuses the change."""
    require_role(actor, *MODULE_ROLES)
    _known(key)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required."})
    module, _ = LabModule.objects.select_for_update().get_or_create(key=key)
    if expected_revision is None or module.revision != expected_revision:
        raise ValidationError(
            "This module was switched after you opened the page. Reload it and check its state."
        )
    if module.is_enabled == bool(enabled):
        raise ValidationError("Nothing to change.")
    module.is_enabled = bool(enabled)
    module.revision += 1
    module.save()
    record_event(
        actor=actor,
        action="module.enabled" if enabled else "module.disabled",
        obj=module,
        before={"key": key, "is_enabled": not module.is_enabled},
        after={"key": key, "is_enabled": module.is_enabled},
        reason=reason,
    )
    return module
