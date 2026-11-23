"""A second sign-in factor the laboratory requires (core.second_factor), applied at sign-in.

When an account signs in, its session row records whether the laboratory's rule covered the
account at that moment (AccountSession.second_factor_required, set by
accounts.sessions.start_session). While the account of such a session has no second factor, the
session is waiting for one: EnrolmentMiddleware lets its requests reach only the pages that set one
up (ALLOWED_VIEWS), and answers every other request by sending the browser to Set up a second
sign-in factor (the setup page). The first page opened with GET is remembered as the page the
sign-in was going to (the start page when the setup page itself was opened first); once the factor
is added, Continue goes there and forgets it. Each sign-in starts without one. The browser's
session is written as a whole, so two tabs choosing Continue at the same moment can both go there.

Each request decides from the database as it stands then, in one statement: the row's flag and
whether the account has an authenticator. So adding an authenticator lets the next request through,
and removing the account's only one (which a person may always do: NIST SP 800-63B-4 §4.5 asks
that an authenticator be invalidated at its holder's request) sends the same session back to the
setup page at its next request, as it does every other session of the account the rule covered at
sign-in. A session waiting for a factor whose account the rule no longer covers (a role removed,
the rule changed) goes on while that lasts: the rule and the account are read again, in one
statement, before refusing.

A session the rule did not cover at sign-in is not held, whatever is saved later: the rule applies
to it from the account's next sign-in. A superuser can end a person's sessions from the admin
console to apply it at once.

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
# overview and adding an authenticator app (with the re-authentication that asks for first, by
# password or, for an account without one, by a recovery code);
# signing in and out; the session status, Stay signed in and keeping what was typed
# (accounts.sessions); the laboratory's logo; and the health checks.
ALLOWED_VIEWS = frozenset(
    {
        ENROLMENT_ROUTE,
        "mfa_index",
        "mfa_activate_totp",
        "account_reauthenticate",
        "mfa_reauthenticate",
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


def _remember(request, path=None):
    """Keep the first page a GET navigation was opening, for Continue on the setup page (checked
    again when it is taken, _take_next)."""
    if NEXT_KEY not in request.session:
        request.session[NEXT_KEY] = path or request.get_full_path()


def _refusal(request):
    """Send the browser to the setup page: a page opened in the window is redirected (and
    remembered when opened with GET); the shared form dialog is redirected too, which it turns into
    loading the setup page in the whole window (core.form_dialog); a partial update is told to load
    it in the whole window; a script's request is refused with where to go."""
    target = reverse(ENROLMENT_ROUTE)
    if _is_navigation(request) or request.headers.get("X-VR-Form") == "1":
        if request.method == "GET" and _is_navigation(request):
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
        # Decided once a view is reached (process_view); None until then, so that a page drawn
        # without one (Not found) decides for its shell itself (core.context_processors).
        request.vr_second_factor_pending = None
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        request.vr_second_factor_pending = pending(request)
        if not request.vr_second_factor_pending:
            return None
        match = request.resolver_match
        if match is not None and match.view_name in ALLOWED_VIEWS:
            return None
        return _refusal(request)


def _take_next(request):
    """The page the sign-in was going to, taken from the session by a GET (a HEAD leaves it), if
    it is on this site."""
    if request.method == "GET":
        path = request.session.pop(NEXT_KEY, None)
    else:
        path = request.session.get(NEXT_KEY)
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
    to, or the start page, and forgets it."""
    from core.second_factor import reasons

    user = request.user
    # Why, read in one statement with the rule and the account as they stand now: when nothing
    # covers the account any more (the rule changed since this request was let through), the
    # session is no longer waiting, and the page goes on as for one that is not.
    why = reasons(user) if getattr(request, "vr_second_factor_pending", False) else []
    if not why:
        return redirect(_take_next(request) or "dashboard")
    if request.method == "GET":
        _remember(request, reverse("dashboard"))
    return render(
        request,
        "accounts/second_factor_required.html",
        {
            "page_title": "Set up a second sign-in factor",
            "reasons": why,
            "locked": user.credentials_locked,
            "has_recovery_codes": Authenticator.objects.filter(
                user_id=user.pk, type=Authenticator.Type.RECOVERY_CODES
            ).exists(),
        },
    )
