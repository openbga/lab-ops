"""Optional modules: parts of the application a laboratory switches on (docs/product.md
"Optional modules").

A module that is off shows no page, menu entry or link, and its addresses answer 404. Its
records are kept, unchanged, and reappear when it is switched on again. The desk works with
every module off. The admin role switches a module, with a reason; the change is audited.

A write in a module takes `lock_enabled(key)`, a shared row lock on the module's switch, so
switching the module off waits for writes already under way and a write that starts after it
finds the module off.
"""

import contextvars
import types
from functools import wraps
from importlib import import_module

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.handlers.exception import response_for_exception
from django.db import connection, transaction
from django.http import Http404
from django.urls import Resolver404, resolve
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


# The URL namespace under which each module's addresses are included (config/urls.py).
MODULE_NAMESPACES = {"nonconformance": "nonconformance"}

# This request's one reading of the switches ({key: on}), set by ModuleAddressMiddleware or the
# first states_for() of the request, so the menu, the pages and the selectors of one response
# agree. Writes do not use it: they lock the switch (lock_enabled).
_reading = contextvars.ContextVar("vr_module_reading", default=None)


def _read_states():
    rows = dict(LabModule.objects.filter(key__in=MODULES).values_list("key", "is_enabled"))
    return {key: bool(rows.get(key)) for key in MODULES}


def is_enabled(key):
    """Whether `key` is on: in this request's reading when there is one, else read now."""
    _known(key)
    reading = _reading.get()
    if reading is not None:
        return reading[key]
    return LabModule.objects.filter(key=key, is_enabled=True).exists()


class ModuleOff(Http404):
    """The module is off: its addresses do not exist. A view that turns a 404 of something else
    (a ticket out of reach) into a message on its form lets this one through."""


def require_enabled(key):
    """404 for a page of a module that is off: its pages do not exist while it is off."""
    if not is_enabled(key):
        raise ModuleOff("This module is not in use.")


def _found_off(request, key):
    """A write found the module off (lock_enabled): the rest of this response says so too."""
    reading = _reading.get()
    if reading is not None:
        reading[key] = False
    states = getattr(request, "_vr_module_states", None)
    if states is not None:
        states[key] = False


def module_view(key):
    """Decorator for every view of a module: while the module is off it does not exist, for
    every audience, and nothing it answers is stored by the browser or a proxy, its refusals
    included (record text and typed input)."""

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            try:
                require_enabled(key)
                response = view(request, *args, **kwargs)
            except (Http404, PermissionDenied) as exc:
                if isinstance(exc, ModuleOff):
                    # Found off during the request: answered like an unknown address (no route,
                    # so no page help or form of the module), and the rest of the response agrees.
                    _found_off(request, key)
                    request.resolver_match = None
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
        cached = _reading.get()
        if cached is None:
            cached = _read_states()
        if request is not None:
            request._vr_module_states = cached
    return cached


def enabled_for(request, key):
    """Whether `key` is on, in this request's one reading of the switches (states_for)."""
    _known(key)
    return states_for(request)[key]


def _module_key_for(path, root=None):
    """The module whose view `path` (or `path` with a slash) is, from the full address list of
    `root`; no database read."""
    for candidate in (path, path if path.endswith("/") else path + "/"):
        try:
            match = resolve(candidate, root)
        except Resolver404:
            continue
        return getattr(match.func, "module_key", None)
    return None


_URLCONFS = {}


def _urlconf_without(disabled, root=None):
    """The address list of `root` (the configured one by default) without the addresses of the
    modules in `disabled`, cached per root and set (its error handlers are that root's)."""
    root = root or settings.ROOT_URLCONF
    key = (root, frozenset(disabled))
    if key not in _URLCONFS:
        base = import_module(root) if isinstance(root, str) else root
        namespaces = {MODULE_NAMESPACES[module] for module in key[1]}
        urlconf = types.ModuleType(f"{base.__name__}__without_{'_'.join(sorted(key[1]))}")
        urlconf.urlpatterns = [
            pattern
            for pattern in base.urlpatterns
            if getattr(pattern, "namespace", None) not in namespaces
        ]
        for handler in ("handler400", "handler403", "handler404", "handler500"):
            if hasattr(base, handler):
                setattr(urlconf, handler, getattr(base, handler))
        _URLCONFS[key] = urlconf
    return _URLCONFS[key]


class ModuleAddressMiddleware:
    """Before SecurityMiddleware, CommonMiddleware and the CSRF check (config/settings.py). A
    request to an address of a module reads the switches once for the whole response. While the
    module is off, the request is resolved against the address list without it, so the address
    does not exist: for every method the answer is exactly that of an unknown address (no slash
    redirect, no CSRF step, no page help). While it is on, every response of the address, a
    redirect or refusal by any later middleware included, is marked never to be stored. Other
    requests read nothing here."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        root = getattr(request, "urlconf", None) or settings.ROOT_URLCONF
        key = _module_key_for(request.path_info, root)
        if key is None:
            return self.get_response(request)
        states = states_for(request)
        token = _reading.set(states)
        try:
            disabled = [module for module, on in states.items() if not on]
            if not states[key]:
                request.urlconf = _urlconf_without(disabled, root)
                return self.get_response(request)
            response = self.get_response(request)
        finally:
            _reading.reset(token)
        if states.get(key):
            add_never_cache_headers(response)
        return response


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
