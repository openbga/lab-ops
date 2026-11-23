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
from django.db import transaction
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
# overview, adding an authenticator app (with the re-authentication that asks for first, by
# password or, for an account without one, by a recovery code) and the recovery codes pages the
# overview offers for codes the account already has;
# signing in and out; the session status, Stay signed in and keeping what was typed
# (accounts.sessions); the laboratory's logo; and the health checks.
ALLOWED_VIEWS = frozenset(
    {
        ENROLMENT_ROUTE,
        "mfa_index",
        "mfa_activate_totp",
        "mfa_view_recovery_codes",
        "mfa_download_recovery_codes",
        "mfa_generate_recovery_codes",
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


def waiting(state):
    """Whether the signed-in session `state` (accounts.sessions.SessionState) must add a second
    factor before anything else: the rule covered its account at sign-in (the row's flag), the
    account has no second factor of a type this deployment offers, and the rule as it stands still
    covers the active account. One statement, so the three are read together."""
    if state is None:
        return False
    from django.db import connection

    from core import second_factor

    t = second_factor._tables()
    sessions = connection.ops.quote_name(AccountSession._meta.db_table)
    source, source_params = second_factor._rule_source(None)
    condition, params = second_factor._covered_sql()
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT EXISTS (SELECT 1 FROM {sessions} s JOIN {t['user']} u ON u.id = s.user_id, "
            f"{source} r WHERE s.id = %s AND s.user_id = %s AND s.second_factor_required "
            f"AND u.is_active AND NOT EXISTS (SELECT 1 FROM {t['authenticator']} a "
            f"WHERE a.user_id = u.id AND a.type = ANY(%s)) AND {condition})",
            [
                *source_params,
                state.id,
                state.user_id,
                second_factor.second_factor_types(),
                *params,
            ],
        )
        return bool(cursor.fetchone()[0])


def pending(request):
    """Whether this request's session must add a second factor before anything else (waiting())."""
    return waiting(getattr(request, "account_session", None))


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
            # The first page opened in the window is the one Continue goes back to, an allowed one
            # too (the setup page itself remembers the start page when nothing is remembered).
            if (
                match.view_name != ENROLMENT_ROUTE
                and request.method == "GET"
                and _is_navigation(request)
            ):
                _remember(request)
            return None
        return _refusal(request)


def _take_next(request):
    """The page the sign-in was going to, taken from the session by a GET the browser opens in the
    window (a HEAD, a script's request, a dialog or a partial update leaves it), if it is on this
    site."""
    if request.method == "GET" and _is_navigation(request):
        path = request.session.pop(NEXT_KEY, None)
    else:
        path = request.session.get(NEXT_KEY)
    if isinstance(path, str) and path.startswith("/") and not path.startswith("//"):
        if url_has_allowed_host_and_scheme(path, allowed_hosts={request.get_host()}):
            return path
    return None


def _codes(user):
    """The account's recovery codes, for the setup page's steps: "unused", "used up", or ""
    when it has none."""
    from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes

    record = Authenticator.objects.filter(
        user_id=user.pk, type=Authenticator.Type.RECOVERY_CODES
    ).first()
    if record is None:
        return ""
    return "unused" if RecoveryCodes(record).get_unused_codes() else "used up"


@transaction.non_atomic_requests  # one_reading opens its own transaction (ATOMIC_REQUESTS)
@require_safe
@login_required
def enrolment_page(request):
    """Set up a second sign-in factor: why the laboratory requires one of this account, and the way
    to add an authenticator app. Once the session is no longer waiting (an authenticator added, or
    the rule no longer covers the account), Continue: it goes on to the page the sign-in was going
    to, or the start page, and forgets it. Whether the session waits, why, and the account's
    recovery codes are read in one snapshot, so the page describes one state."""
    from core.concurrency import one_reading
    from core.second_factor import reasons

    user = request.user
    with one_reading():
        why = reasons(user) if pending(request) else []
        codes = _codes(user) if why else ""
    # The shell (core.context_processors) follows this reading, not the middleware's earlier one.
    request.vr_second_factor_pending = bool(why)
    if not why:
        return redirect(_take_next(request) or "dashboard")
    if request.method == "GET" and _is_navigation(request):
        _remember(request, reverse("dashboard"))
    return render(
        request,
        "accounts/second_factor_required.html",
        {
            "page_title": "Set up a second sign-in factor",
            "reasons": why,
            "locked": user.credentials_locked,
            "codes": codes,
        },
    )
