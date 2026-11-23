"""A second sign-in factor the laboratory requires (core.second_factor), applied at sign-in.

When an account signs in, its session row records whether the laboratory's rule covered the
account at that moment (AccountSession.second_factor_required, set by
accounts.sessions.start_session). While the account of such a session has no second factor, the
session is waiting for one: EnrolmentMiddleware lets its requests reach only the pages that set one
up (ALLOWED_VIEWS), and answers every other request by sending the browser to Set up a second
sign-in factor (the setup page). A page opened with GET is remembered, once, as the page the
sign-in was going to; the setup page offers it again (Continue) once the factor is added.

Each request decides from the database as it stands then, in one statement: the row's flag and
whether the account has an authenticator. So adding an authenticator lets the next request through,
and removing the account's only one (which a person may always do: NIST SP 800-63B-4 §4.5 asks
that an authenticator be invalidated at its holder's request) sends the same session back to the
setup page at its next request. A session waiting for a factor whose account the rule no longer
covers (a role removed, the rule changed) goes on: the rule is read again before refusing.

A rule saved while a session is open does not affect that session: it applies from the account's
next sign-in. A superuser can end a person's sessions from the admin console to apply it at once.

A shared demonstration account (credentials_locked) cannot add a second factor (accounts.
middleware): the setup page says so and offers only Sign out.
"""

from allauth.mfa.models import Authenticator
from django.contrib.auth.decorators import login_required
from django.db.models import Exists, OuterRef
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_safe

from .models import AccountSession

ENROLMENT_ROUTE = "account-second-factor"
# The page the sign-in was going to, kept in the browser's session while it waits for a factor.
NEXT_KEY = "_second_factor_next"
# What a session waiting for a second factor may reach: the setup page; the account's two-factor
# overview and adding an authenticator app (with the re-authentication that asks for first);
# signing in and out; the session status, Stay signed in and keeping what was typed
# (accounts.sessions); the laboratory's logo; and the health checks.
ALLOWED_VIEWS = frozenset(
    {
        ENROLMENT_ROUTE,
        "mfa_index",
        "mfa_activate_totp",
        "account_reauthenticate",
        "account_login",
        "account_logout",
        "account-session",
        "account-session-continue",
        "account-session-keep",
        "lab-logo-image",
        "health",
        "ready",
    }
)


def second_factor_types():
    from core.second_factor import second_factor_types as types

    return types()


def waiting(state):
    """Whether the signed-in session `state` (accounts.sessions.SessionState) was held to the rule
    at sign-in and its account has no second factor now: one statement."""
    if state is None:
        return False
    factor = Authenticator.objects.filter(
        user_id=OuterRef("user_id"), type__in=second_factor_types()
    )
    return (
        AccountSession.objects.filter(
            pk=state.id, user_id=state.user_id, second_factor_required=True
        )
        .exclude(Exists(factor))
        .exists()
    )


def pending(request):
    """Whether this request's session must add a second factor before anything else: waiting for
    one (waiting()) and its account still covered by the rule as it stands (core.second_factor)."""
    from core.second_factor import requires

    state = getattr(request, "account_session", None)
    if state is None or not waiting(state):
        return False
    return requires(request.user)


def _is_navigation(request):
    from .sessions import _is_navigation as is_navigation

    return is_navigation(request)


def _remember(request):
    """Keep the page a GET navigation was opening, once, for Continue on the setup page (checked
    again when it is taken, _take_next)."""
    if NEXT_KEY not in request.session:
        request.session[NEXT_KEY] = request.get_full_path()


def _refusal(request):
    """Send the browser to the setup page: a page opened in the window is redirected (and
    remembered when opened with GET); the shared form dialog is redirected too, which it turns into
    loading the setup page in the whole window (core.form_dialog); a partial update is told to load
    it in the whole window; a script's request is refused with where to go."""
    target = reverse(ENROLMENT_ROUTE)
    if _is_navigation(request) or request.headers.get("X-VR-Form") == "1":
        if request.method in ("GET", "HEAD") and _is_navigation(request):
            _remember(request)
        response = HttpResponseRedirect(target)
    elif request.headers.get("HX-Request"):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = target
    else:
        response = JsonResponse({"second_factor_required": True, "location": target}, status=403)
    add_never_cache_headers(response)
    return response


class EnrolmentMiddleware:
    """After SessionLimitMiddleware, which decides whether the session may go on at all and sets
    request.account_session: a session waiting for a second factor reaches only ALLOWED_VIEWS."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.vr_second_factor_pending = False
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not pending(request):
            return None
        request.vr_second_factor_pending = True
        match = request.resolver_match
        if match is not None and match.view_name in ALLOWED_VIEWS:
            return None
        return _refusal(request)


def _take_next(request):
    """The page the sign-in was going to, taken from the session (once), if it is on this site."""
    path = request.session.pop(NEXT_KEY, None)
    if isinstance(path, str) and path.startswith("/") and not path.startswith("//"):
        if url_has_allowed_host_and_scheme(path, allowed_hosts={request.get_host()}):
            return path
    return None


@require_safe
@login_required
def enrolment_page(request):
    """Set up a second sign-in factor: why the laboratory requires one of this account, and the way
    to add an authenticator app. Once the session is no longer waiting (an authenticator added, or
    the rule no longer covers the account), Continue: it goes on to the page the sign-in was going
    to, or the start page."""
    from core.second_factor import reasons

    if not getattr(request, "vr_second_factor_pending", False):
        return redirect(_take_next(request) or "dashboard")
    user = request.user
    return render(
        request,
        "accounts/second_factor_required.html",
        {
            "page_title": "Set up a second sign-in factor",
            "reasons": reasons(user),
            "locked": user.credentials_locked,
        },
    )
