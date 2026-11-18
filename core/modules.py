"""Optional modules: parts of the application a laboratory switches on (docs/product.md
"Optional modules").

A module that is off shows no page, menu entry or link, and its addresses answer 404. Its
records are kept, unchanged, and reappear when it is switched on again. The desk works with
every module off. The admin role switches a module, with a reason; the change is audited.

A write in a module takes `lock_enabled(key)`, a shared row lock on the module's switch, so
switching the module off waits for writes already under way and a write that starts after it
finds the module off.
"""

from functools import wraps

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.handlers.exception import response_for_exception
from django.db import connection, transaction
from django.http import Http404
from django.utils.cache import add_never_cache_headers

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


class ModuleOff(Http404):
    """The module is off: its addresses do not exist. A view that turns a 404 of something else
    (a ticket out of reach) into a message on its form lets this one through."""


def require_enabled(key):
    """404 for a page of a module that is off: its pages do not exist while it is off."""
    if not is_enabled(key):
        raise ModuleOff("This module is not in use.")


def module_view(key):
    """Decorator for every view of a module: while the module is off it does not exist, for
    every audience (404 before sign-in or role checks), and nothing it answers is stored by the
    browser or a proxy, its refusals included (record text and typed input)."""

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            try:
                require_enabled(key)
                response = view(request, *args, **kwargs)
            except (Http404, PermissionDenied) as exc:
                response = response_for_exception(request, exc)
            add_never_cache_headers(response)
            return response

        wrapped.module_key = key
        return wrapped

    return decorator


def states_for(request):
    """{key: whether it is on} for every module, read once per request and kept on it: the menu
    and every page part of one response show the same reading of the switches."""
    cached = getattr(request, "_vr_module_states", None)
    if cached is None:
        rows = dict(LabModule.objects.filter(key__in=MODULES).values_list("key", "is_enabled"))
        cached = {key: bool(rows.get(key)) for key in MODULES}
        if request is not None:
            request._vr_module_states = cached
    return cached


def enabled_for(request, key):
    """Whether `key` is on, in this request's one reading of the switches (states_for)."""
    _known(key)
    return states_for(request)[key]


class ModuleAddressMiddleware:
    """Placed before CsrfViewMiddleware (config/settings.py). For a view of an optional module
    (core.modules.module_view): while the module is off the address is Not found for every method,
    before the CSRF check or any sign-in and role check, the same answer as an unknown address;
    and every response of a module address, refusals of any middleware included, is marked never
    to be stored."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if _module_of(request) is not None and not getattr(request, "_vr_module_off", False):
            add_never_cache_headers(response)
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        key = getattr(view_func, "module_key", None)
        if key is not None and not enabled_for(request, key):
            # Exactly the answer to an unknown address: the same page and headers.
            request._vr_module_off = True
            return response_for_exception(request, Http404("No such page."))
        return None


def _module_of(request):
    match = getattr(request, "resolver_match", None)
    return getattr(getattr(match, "func", None), "module_key", None) if match else None


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
        raise ModuleOff("This module is not in use.")


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
    # Who may switch it is decided again from the account as it stands once the switch is locked:
    # a role removed or an account deactivated while the request waited counts.
    current = get_user_model().objects.filter(pk=actor.pk).first()
    if current is None:
        raise PermissionDenied("Your role does not permit this action.")
    require_role(current, *MODULE_ROLES)
    actor = current
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
