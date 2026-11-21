"""Sign-out after a period without activity and after the longest session the laboratory allows.

Each sign-in is an AccountSession row, created when the account signs in (Django's
user_logged_in, sent once every sign-in step has passed) and carried in the browser's session
by its id. SessionLimitMiddleware decides on every request to a signed-in session, before any
view or other middleware acts for the account:

- the row is missing (a session from before this release, or one whose row was erased), belongs
  to another account, or has ended (signed out, deactivated, a new password): sign out;
- no activity for the inactivity limit, or the longest session reached since sign-in: end the row
  with that reason and sign out;
- otherwise the request counts as activity: one UPDATE moves the row's last activity to now,
  under the same conditions, so a row ended or expired in between is not brought back.

The limits are the laboratory's (core.session_limits), one pair for staff and one for client
portal accounts, chosen by what the account is when the request arrives, and read with the row
in one statement. Times are the database's, so every server process measures the same way.

Activity is a request the person's browser makes: opening a page, a save, a search, a dialog
or Stay signed in. Reading or typing on a page sends nothing, so it does not count. The page
asks how long remains (the status address, which is not activity) before it warns.

What a request does once its session ends depends on what it is. A page opened with GET goes on
as a signed-out visitor, so a page that needs a sign-in sends them to the sign-in page, which
returns to that page afterwards. Anything else (a form sent, a dialog or a partial update) is
answered at once, with the sign-in page set to return to the page the person was on; a dialog or
a partial update is told to load that page in the whole window.

Every answer to a signed-in session, or to one that ended during the request, is marked not to
be stored (no-store), so the browser keeps no copy of a page to show after sign-out. A response
may opt out with `response.vr_cacheable = True` (the laboratory's logo, which is not a page).
"""

import contextvars
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote, urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db import connection, transaction
from django.db.models.functions import Now
from django.dispatch import receiver
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import resolve_url
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.utils.http import url_has_allowed_host_and_scheme

from .models import AccountSession

logger = logging.getLogger("lab_ops.signin")

# The browser session's key for its AccountSession id.
SESSION_KEY = "_account_session"
# The page warns this long before the inactivity sign-out, and offers Stay signed in. NIST SP
# 800-63B-4's usability guidance suggests about two minutes (a recommendation).
WARNING_SECONDS = 120
# And this long before the longest session ends, so that work can be saved; nothing extends it.
LONGEST_WARNING_SECONDS = 300
# The status address (session_views.session_status) is not activity: the page asks it before
# warning, and asking must not keep an unattended session open.
STATUS_ROUTE = "account-session"
CONTINUE_ROUTE = "account-session-continue"
Ending = AccountSession.Ending

ENDED_MESSAGES = {
    Ending.INACTIVITY: "You were signed out after {inactivity} without activity. Sign in again "
    "to continue.",
    Ending.MAXIMUM: "Your session reached its longest length of {maximum}. Sign in again to "
    "continue.",
}
ENDED_MESSAGE = "You were signed out. Sign in again to continue."

# The acting session of this request, for a password change made by the account itself
# (accounts.models.User.save keeps that session and ends the others).
_current = contextvars.ContextVar("account_session", default=None)


@dataclass(frozen=True)
class SessionState:
    """A signed-in session as one request found it, its times read from the database."""

    id: str
    user_id: int
    started_at: object
    last_active_at: object
    now: object
    inactivity: timedelta
    maximum: timedelta

    @property
    def inactivity_remaining(self):
        """Whole seconds left before the inactivity sign-out, as of `now`."""
        return max(0, int((self.last_active_at + self.inactivity - self.now).total_seconds()))

    @property
    def maximum_remaining(self):
        """Whole seconds left before the longest session ends, as of `now`."""
        return max(0, int((self.started_at + self.maximum - self.now).total_seconds()))

    def as_json(self):
        return {
            "signed_in": True,
            "inactivity_seconds": self.inactivity_remaining,
            "maximum_seconds": self.maximum_remaining,
            "inactivity_limit_seconds": int(self.inactivity.total_seconds()),
            "warning_seconds": WARNING_SECONDS,
            "longest_warning_seconds": LONGEST_WARNING_SECONDS,
        }


def audience(user):
    """Which limits apply to the account as it is now: a client portal account's or staff's."""
    from core.models import SessionLimit

    return (
        SessionLimit.Audience.PORTAL if user.client_id is not None else SessionLimit.Audience.STAFF
    )


def _tables():
    from core.models import SessionLimit

    quote_name = connection.ops.quote_name
    return quote_name(AccountSession._meta.db_table), quote_name(SessionLimit._meta.db_table)


def _read(session_id, user):
    """(row, limits) for this session in one statement: the row's account, times and ending, the
    database's now, and the limits for the account's audience (the defaults where none is set).
    None when there is no such row."""
    from core.session_limits import DEFAULTS

    sessions, limits = _tables()
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT s.user_id, s.started_at, s.last_active_at, s.ended_at, now(), "
            f"COALESCE(l.inactivity_minutes, %s), COALESCE(l.maximum_hours, %s) "
            f"FROM {sessions} s LEFT JOIN {limits} l ON l.audience = %s WHERE s.id = %s",
            [
                DEFAULTS["inactivity_minutes"],
                DEFAULTS["maximum_hours"],
                audience(user),
                session_id,
            ],
        )
        return cursor.fetchone()


def _touch(state):
    """Record activity now, only while the row is open and inside both limits as the database
    measures them at this moment. False when it no longer is (ended or expired in between)."""
    sessions, _ = _tables()
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {sessions} SET last_active_at = now() "
            f"WHERE id = %s AND user_id = %s AND ended_at IS NULL "
            f"AND last_active_at > now() - %s AND started_at > now() - %s",
            [state.id, state.user_id, state.inactivity, state.maximum],
        )
        return cursor.rowcount == 1


def end_sessions(user, ending, *, keep=None, session_id=None):
    """End the account's open sessions (or the one given) with this reason, except `keep`. Their
    browsers are signed out at their next request. Returns how many ended."""
    query = AccountSession.objects.filter(user_id=user.pk, ended_at__isnull=True)
    if session_id is not None:
        query = query.filter(pk=session_id)
    if keep is not None:
        query = query.exclude(pk=keep)
    ended = query.update(ended_at=Now(), ending=ending)
    if ended and ending in (Ending.DEACTIVATED, Ending.NEW_PASSWORD):
        _log_after_commit(ending, user, ended)
    return ended


def _username(user):
    return quote(user.get_username(), safe="@.+-_")


def _log_after_commit(ending, user, count):
    """Sessions ended by a change of the account, logged once the change has committed (a change
    rolled back ended nothing)."""
    username = _username(user)

    def log():
        logger.info("signout.%s user=%s sessions=%s", ending, username, count)

    transaction.on_commit(log)


def kept_on_password_change(user):
    """The session to keep when this account's password changes: the one making the change, when
    the account changes its own password. None otherwise (a password set for another account, or
    a reset by email), so that every session of the account ends."""
    current = _current.get()
    if current is not None and current[0] == user.pk:
        return current[1]
    return None


@receiver(user_logged_in, dispatch_uid="lab_ops_account_session_start")
def start_session(sender, request, user, **kwargs):
    session = getattr(request, "session", None)
    if session is None:
        return
    with transaction.atomic():
        previous = _valid_id(session.get(SESSION_KEY))
        if previous is not None:
            # Signed in again over an open session in the same browser (Django keeps the
            # session's data when the account is the same one).
            end_sessions(user, Ending.SIGNED_IN_AGAIN, session_id=previous)
        record = AccountSession.objects.create(user=user)
    session[SESSION_KEY] = str(record.pk)


@receiver(user_logged_out, dispatch_uid="lab_ops_account_session_end")
def end_signed_out_session(sender, request, user, **kwargs):
    session = getattr(request, "session", None)
    session_id = _valid_id(session.get(SESSION_KEY)) if session is not None else None
    if user is not None and session_id is not None:
        end_sessions(user, Ending.SIGNED_OUT, session_id=session_id)


def _valid_id(value):
    try:
        return str(uuid.UUID(str(value)))
    except TypeError, ValueError, AttributeError:
        return None


def _route_path(name):
    return reverse(name)


def sign_in_url(next_page):
    url = resolve_url(settings.LOGIN_URL)
    if next_page:
        url += "?next=" + quote(next_page, safe="/")
    return url


def _page_on_screen(request):
    """The page the person was on when a form, a dialog or a partial update was sent, from the
    address the browser reports (HTMX's current address, else the referrer): its path and query
    on this host, or None."""
    for header in ("HX-Current-URL", "Referer"):
        value = request.headers.get(header, "")
        if value and url_has_allowed_host_and_scheme(
            value, {request.get_host()}, require_https=request.is_secure()
        ):
            parts = urlsplit(value)
            if parts.path.startswith("/"):
                return parts.path + (f"?{parts.query}" if parts.query else "")
    return None


def _is_navigation(request):
    """A page the browser opens in the window, as opposed to a dialog, a partial update or a
    script's request."""
    if request.headers.get("HX-Request") or request.headers.get("X-VR-Form"):
        return False
    return request.headers.get("Sec-Fetch-Dest", "document") in ("document", "iframe", "")


def _sign_out(request, ending, limits=None):
    """End the browser's session without the sign-out signal (that is for Sign out), keep a
    notice for the sign-in page, and let the request go on as a signed-out visitor."""
    from core.security_settings import duration_text

    user = request.user
    request.session.flush()
    request.user = AnonymousUser()
    if ending in ENDED_MESSAGES and limits is not None:
        logger.info(
            "signout.%s user=%s sessions=1", ending, _username(user), extra={"request": request}
        )
        text = ENDED_MESSAGES[ending].format(
            inactivity=duration_text(limits[0].total_seconds()),
            maximum=duration_text(limits[1].total_seconds()),
        )
    else:
        text = ENDED_MESSAGE
    messages.info(request, text, fail_silently=True)


def check(request):
    """Decide whether the signed-in session of this request may go on. Returns the state when it
    may (and records the activity), or None after signing it out."""
    user = request.user
    session_id = _valid_id(request.session.get(SESSION_KEY))
    row = _read(session_id, user) if session_id else None
    if row is None or row[0] != user.pk or row[3] is not None:
        _sign_out(request, None)
        return None
    _, started_at, last_active_at, _ended, now, minutes, hours = row
    state = SessionState(
        session_id,
        user.pk,
        started_at,
        last_active_at,
        now,
        timedelta(minutes=minutes),
        timedelta(hours=hours),
    )
    limits = (state.inactivity, state.maximum)
    ending = None
    if now - started_at >= state.maximum:
        ending = Ending.MAXIMUM
    elif now - last_active_at >= state.inactivity:
        ending = Ending.INACTIVITY
    if ending is None and request.path_info != _route_path(STATUS_ROUTE):
        if not _touch(state):
            return check_again(request, state)
        state = SessionState(
            state.id, state.user_id, started_at, now, now, state.inactivity, state.maximum
        )
    if ending is not None:
        end_sessions(user, ending, session_id=session_id)
        _sign_out(request, ending, limits)
        return None
    return state


def check_again(request, state):
    """The activity was not recorded: the row ended, or reached a limit, after it was read.
    Decide again from the row as it is now."""
    row = _read(state.id, request.user)
    ending = None
    if row is not None and row[3] is None:
        _, started_at, last_active_at, _ended, now, minutes, hours = row
        if now - started_at >= timedelta(hours=hours):
            ending = Ending.MAXIMUM
        elif now - last_active_at >= timedelta(minutes=minutes):
            ending = Ending.INACTIVITY
        if ending is not None:
            end_sessions(request.user, ending, session_id=state.id)
    _sign_out(request, ending, (state.inactivity, state.maximum))
    return None


def _session_endpoints():
    return {_route_path(STATUS_ROUTE), _route_path(CONTINUE_ROUTE)}


def _refusal(request):
    """The answer to a request whose session just ended, other than a page opened with GET."""
    sign_in = sign_in_url(_page_on_screen(request))
    if _is_navigation(request):
        return HttpResponseRedirect(sign_in)
    response = JsonResponse({"signed_in": False, "sign_in": sign_in}, status=401)
    response["HX-Redirect"] = sign_in
    return response


def _has_session_cookie(request):
    return settings.SESSION_COOKIE_NAME in request.COOKIES


class SessionLimitMiddleware:
    """After the session, authentication and message middleware, before anything that acts for
    the account (accounts.sessions)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        signed_in = _has_session_cookie(request) and request.user.is_authenticated
        token = _current.set(None)
        try:
            request.account_session = None
            response = None
            if signed_in:
                state = check(request)
                if state is not None:
                    request.account_session = state
                    _current.set((state.user_id, state.id))
                elif request.path_info not in _session_endpoints() and (
                    request.method not in ("GET", "HEAD") or not _is_navigation(request)
                ):
                    response = _refusal(request)
            if response is None:
                response = self.get_response(request)
        finally:
            _current.reset(token)
        if (signed_in or _signed_in_now(request)) and not getattr(response, "vr_cacheable", False):
            add_never_cache_headers(response)
        return response


def _signed_in_now(request):
    """Signed in during this request (the sign-in page's answer)."""
    user = getattr(request, "user", None)
    return user is not None and user.is_authenticated
