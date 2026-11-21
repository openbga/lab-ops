"""Sign-out after a period without activity and after the longest session the laboratory allows.

Each sign-in is an AccountSession row, created when the account signs in (Django's
user_logged_in, sent once every sign-in step has passed) and carried in the browser's session
by its id. SessionLimitMiddleware decides on every request to a signed-in session, before any
view or other middleware acts for the account:

- the row is missing (a session from before this release, or one whose row was erased), belongs
  to another account, or has ended (signed out, deactivated, a new password): sign out;
- no activity for the inactivity limit, or the longest session reached since sign-in: end the row
  with that reason (and the moment the limit passed) and sign out;
- otherwise the request may go on. Once its address resolves to a view and its CSRF check has
  passed, it counts as activity: one UPDATE moves the row's last activity to the database's
  clock at that moment, never back, only while the row is open and inside both limits, so a row
  ended or expired in between is not brought back. A row another transaction holds at that
  moment is not waited for: that is another request of the session, or a change or an ending
  under way, which decide for themselves.

The limits are the laboratory's (core.session_limits), one pair for staff and one for client
portal accounts, chosen by what the account is when the request arrives, and read with the row
in one statement. Times are the database's, so every server process measures the same way.

Activity is a request the person's browser makes: opening a page, a save, a search, a dialog
or Stay signed in. Reading or typing on a page sends nothing, so it does not count; neither do an
address that does not exist, a request its CSRF check refuses, or the status address the page
asks how long remains before it warns.

A change that holds its acting account (accounts.acting.hold_actor) also holds the request's
session row, FOR SHARE, and is refused when the row has ended: ending a session (Sign out, a new
password, a deactivation) waits for such a change under way and refuses one started afterwards.

What a request does once its session ends depends on what it is. A page opened with GET goes on
as a signed-out visitor, so a page that needs a sign-in sends them to the sign-in page, which
returns to that page afterwards. Anything else (a form sent, a dialog or a partial update) is
answered at once, with the sign-in page set to return to the page the person was on; a dialog or
a partial update is told to load that page in the whole window. A form, a dialog or a partial
update that needs a sign-in from a browser that is not signed in for any other reason (Django
refused its session, it signed out in another tab) is answered the same way.

Every answer to a signed-in session, or to one that ended or signed in during the request, is
marked not to be stored (no-store) by NoStoreMiddleware, the outermost layer, so the browser keeps
no copy of a page to show after sign-out, whichever layer drew the answer (a redirect added on
the way out, an error page). A response may opt out with `response.vr_cacheable = True` (the
laboratory's logo, which is not a page).
"""

import contextvars
import dataclasses
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote, urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import HASH_SESSION_KEY, get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.db.models.functions import Now
from django.dispatch import receiver
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import resolve_url
from django.urls import NoReverseMatch, reverse
from django.utils.cache import add_never_cache_headers
from django.utils.crypto import constant_time_compare
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


class SessionEnded(PermissionDenied):
    """A change refused because the session it was sent from ended after the request began."""

    def __init__(self):
        super().__init__("Your session has ended. Sign in again to continue.")


# The acting session of this request, (account id, session id): for a password change made by
# the account itself (accounts.models.User.save keeps that session and ends the others) and for
# changes that hold their acting account (hold_acting_session).
_current = contextvars.ContextVar("account_session", default=None)
# The session a password change of this request kept, (account id, session id): it stays open
# only if the request also kept Django's session valid (_settle_kept_session).
_kept = contextvars.ContextVar("account_session_kept", default=None)


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
    """Record activity, only while the row is open and inside both limits, measured by the
    database's clock when the row is updated (clock_timestamp(), not when the statement began),
    and never move the last activity back. Returns True when recorded, None when another
    transaction holds the row right now (another request of this session recording its own
    activity, a change under way holding it, or an ending about to commit): this request does not
    wait for it and goes on as admitted, and a change it makes is decided under that hold
    (hold_acting_session). False when the row no longer qualifies (ended or expired since the
    reading)."""
    sessions, _ = _tables()
    with connection.cursor() as cursor:
        cursor.execute(
            f"WITH locked AS (SELECT id FROM {sessions} WHERE id = %s AND user_id = %s "
            f"FOR NO KEY UPDATE SKIP LOCKED), "
            f"recorded AS (UPDATE {sessions} s "
            f"SET last_active_at = GREATEST(s.last_active_at, clock_timestamp()) FROM locked "
            f"WHERE s.id = locked.id AND s.ended_at IS NULL "
            f"AND s.last_active_at > clock_timestamp() - %s "
            f"AND s.started_at > clock_timestamp() - %s RETURNING s.id) "
            f"SELECT (SELECT count(*) FROM locked), (SELECT count(*) FROM recorded)",
            [state.id, state.user_id, state.inactivity, state.maximum],
        )
        locked, recorded = cursor.fetchone()
    if not locked:
        return None
    return recorded == 1


def _limits_for(user):
    """(inactivity, longest) for the account's audience as it is now."""
    from core.models import SessionLimit
    from core.session_limits import DEFAULTS

    row = SessionLimit.objects.filter(audience=audience(user)).values_list(
        "inactivity_minutes", "maximum_hours"
    )
    minutes, hours = row.first() or (DEFAULTS["inactivity_minutes"], DEFAULTS["maximum_hours"])
    return timedelta(minutes=minutes), timedelta(hours=hours)


def close_expired(user):
    """End the account's open rows that have passed a limit, as the limits stand now, with the
    limit passed first and the moment it passed. A browser that closed or was abandoned leaves
    its row open until then. Returns how many ended."""
    inactivity, maximum = _limits_for(user)
    sessions, _ = _tables()
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {sessions} SET "
            f"ending = CASE WHEN started_at + %s <= last_active_at + %s "
            f"THEN %s ELSE %s END, "
            f"ended_at = LEAST(started_at + %s, last_active_at + %s) "
            f"WHERE user_id = %s AND ended_at IS NULL "
            f"AND (started_at + %s <= clock_timestamp() "
            f"OR last_active_at + %s <= clock_timestamp())",
            [
                maximum,
                inactivity,
                Ending.MAXIMUM,
                Ending.INACTIVITY,
                maximum,
                inactivity,
                user.pk,
                maximum,
                inactivity,
            ],
        )
        return cursor.rowcount


def end_sessions(user, ending, *, keep=None, session_id=None):
    """End the account's open sessions (or the one given) with this reason, except `keep`. Their
    browsers are signed out at their next request. Rows already past a limit end first, as
    expired (close_expired), so they are not counted as ended by this. A change under way that
    holds one of the rows (hold_acting_session) is waited for. Returns how many ended."""
    if session_id is None:
        close_expired(user)
    query = AccountSession.objects.filter(user_id=user.pk, ended_at__isnull=True)
    if session_id is not None:
        query = query.filter(pk=session_id)
    if keep is not None:
        query = query.exclude(pk=keep)
    ended = query.update(ended_at=Now(), ending=ending)
    if ended and ending in (Ending.DEACTIVATED, Ending.NEW_PASSWORD):
        _log_after_commit(ending, user, ended)
    return ended


def hold_acting_session(user_pk):
    """For a change that holds its acting account (accounts.acting.hold_actor): when this request
    acts for `user_pk` through a signed-in session, hold that session's row FOR SHARE until the
    transaction ends and say whether it is still open. Ending the session waits for the change,
    and hold_actor refuses a change started after it ended (SessionEnded). True when the request
    has no session of that account (a job, a command, a test calling a service)."""
    current = _current.get()
    if current is None or current[0] != user_pk:
        return True
    sessions, _ = _tables()
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT ended_at IS NULL FROM {sessions} WHERE id = %s AND user_id = %s FOR SHARE",
            [current[1], user_pk],
        )
        row = cursor.fetchone()
    return bool(row and row[0])


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
    a reset by email from a browser not signed in to it), so that every session of the account
    ends. A kept session stays open only if the request keeps Django's session valid too
    (_settle_kept_session): a reset by email signed in to the same account does not."""
    current = _current.get()
    if current is not None and current[0] == user.pk:
        _kept.set(current)
        return current[1]
    return None


def _settle_kept_session(request):
    """After the view: a session kept by a password change stays open only when the request left
    Django's session carrying the account's new session hash (update_session_auth_hash, as the
    account pages do); otherwise its browser is signed out at its next request, so its row ends
    now with the others."""
    kept = _kept.get()
    if kept is None:
        return
    user_pk, session_id = kept
    user = get_user_model().objects.filter(pk=user_pk).first()
    stored = request.session.get(HASH_SESSION_KEY) if hasattr(request, "session") else None
    if user is not None and stored and constant_time_compare(stored, user.get_session_auth_hash()):
        return
    if user is not None:
        with transaction.atomic():
            end_sessions(user, Ending.NEW_PASSWORD, session_id=session_id)


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
        close_expired(user)  # sessions abandoned since, past a limit
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


def _sign_out(request, ending, limits=None, *, ended=0):
    """End the browser's session without the sign-out signal (that is for Sign out), keep a
    notice for the sign-in page, and let the request go on as a signed-out visitor. The sign-in
    log names the limit only when this request ended the row (`ended`): two requests of one
    session that find it expired together record it once."""
    from core.security_settings import duration_text

    user = request.user
    request.session.flush()
    request.user = AnonymousUser()
    request.account_session = None
    _current.set(None)
    if ending in ENDED_MESSAGES and limits is not None:
        if ended:
            logger.info(
                "signout.%s user=%s sessions=%s",
                ending,
                _username(user),
                ended,
                extra={"request": request},
            )
        text = ENDED_MESSAGES[ending].format(
            inactivity=duration_text(limits[0].total_seconds()),
            maximum=duration_text(limits[1].total_seconds()),
        )
    else:
        text = ENDED_MESSAGE
    messages.info(request, text, fail_silently=True)


def _end_expired(state, ending):
    """End this row for the limit it was found past when the request's reading was taken, at the
    moment that limit passed by that reading; only while it is still open. Returns 1 when this
    request ended it, 0 when another had (two tabs of one session finding it expired together)."""
    passed = (
        state.started_at + state.maximum
        if ending == Ending.MAXIMUM
        else state.last_active_at + state.inactivity
    )
    return AccountSession.objects.filter(pk=state.id, ended_at__isnull=True).update(
        ended_at=passed, ending=ending
    )


def _expired(row):
    """The limit a row read by _read has passed, or None."""
    _, started_at, last_active_at, _ended, now, minutes, hours = row
    if now - started_at >= timedelta(hours=hours):
        return Ending.MAXIMUM
    if now - last_active_at >= timedelta(minutes=minutes):
        return Ending.INACTIVITY
    return None


def check(request):
    """Decide whether the signed-in session of this request may go on. Returns its state when it
    may, or None after signing it out. Records nothing: the activity is recorded once the request
    reaches a view (SessionLimitMiddleware.process_view)."""
    user = request.user
    session_id = _valid_id(request.session.get(SESSION_KEY))
    row = _read(session_id, user) if session_id else None
    if row is None or row[0] != user.pk or row[3] is not None:
        _sign_out(request, None)
        return None
    _, started_at, last_active_at, _ended, now, minutes, hours = row
    limits = (timedelta(minutes=minutes), timedelta(hours=hours))
    state = SessionState(session_id, user.pk, started_at, last_active_at, now, *limits)
    ending = _expired(row)
    if ending is not None:
        _sign_out(request, ending, limits, ended=_end_expired(state, ending))
        return None
    return state


def check_again(request, state):
    """The activity was not recorded: the row ended, or reached a limit, after it was read.
    Decide again from the row as it is now, and sign out."""
    row = _read(state.id, request.user)
    ending, ended, limits = None, 0, (state.inactivity, state.maximum)
    if row is not None and row[3] is None:
        _, started_at, last_active_at, _ended, now, minutes, hours = row
        limits = (timedelta(minutes=minutes), timedelta(hours=hours))
        ending = _expired(row)
        if ending is not None:
            fresh = SessionState(state.id, state.user_id, started_at, last_active_at, now, *limits)
            ended = _end_expired(fresh, ending)
    _sign_out(request, ending, limits, ended=ended)


def _session_endpoints():
    """The status and Stay signed in addresses; none under a URL configuration without them."""
    try:
        return {_route_path(STATUS_ROUTE), _route_path(CONTINUE_ROUTE)}
    except NoReverseMatch:
        return set()


def _is_activity(request):
    """Whether a request that reached its view counts as activity: every one but the status
    question, and Stay signed in only as the POST it is."""
    match = request.resolver_match
    name = match.view_name if match is not None else ""
    if name == STATUS_ROUTE:
        return False
    return name != CONTINUE_ROUTE or request.method == "POST"


def _goes_on(request):
    """After its session ended: a page opened with GET, or a session question, goes on as a
    signed-out visitor; anything else is answered at once (_refusal)."""
    if request.path_info in _session_endpoints():
        return True
    return request.method in ("GET", "HEAD") and _is_navigation(request)


def _refusal(request):
    """The answer to a request whose session ended, other than a page opened with GET."""
    sign_in = sign_in_url(_page_on_screen(request))
    if _is_navigation(request):
        response = HttpResponseRedirect(sign_in)
    else:
        response = JsonResponse({"signed_in": False, "sign_in": sign_in}, status=401)
        response["HX-Redirect"] = sign_in
    add_never_cache_headers(response)
    return response


def _asks_to_sign_in(response):
    """Whether the response sends the browser to sign in in order to continue (a sign-in address
    carrying `next`, as login_required gives), directly or through HTMX."""
    try:
        sign_in = resolve_url(settings.LOGIN_URL)
    except NoReverseMatch:
        return False
    for header in ("Location", "HX-Redirect"):
        target = urlsplit(response.get(header, "") or "")
        if target.path == sign_in and not target.netloc and "next=" in target.query:
            return True
    return False


def _has_session_cookie(request):
    return settings.SESSION_COOKIE_NAME in request.COOKIES


class SessionLimitMiddleware:
    """After the session, authentication and message middleware, before anything that acts for
    the account (accounts.sessions)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        signed_in = _has_session_cookie(request) and request.user.is_authenticated
        request.vr_signed_in = signed_in
        request.account_session = None
        signed_in_now = signed_in
        current, kept = _current.set(None), _kept.set(None)
        try:
            response = None
            if signed_in:
                state = check(request)
                if state is not None:
                    request.account_session = state
                    _current.set((state.user_id, state.id))
                elif not _goes_on(request):
                    response = _refusal(request)
            if response is None:
                response = self.get_response(request)
            _settle_kept_session(request)
            user = getattr(request, "user", None)
            signed_in_now = user is not None and user.is_authenticated
            if (
                not signed_in_now
                and response.status_code in (204, 301, 302, 303, 307, 308)
                and _asks_to_sign_in(response)
                and not _goes_on(request)
            ):
                # Not signed in for another reason (Django refused the session, Sign out in
                # another tab, never signed in): the same answer as for a session that ended,
                # with the cookies the answer replaced set.
                cookies = response.cookies
                response = _refusal(request)
                response.cookies = cookies
        finally:
            _current.reset(current)
            _kept.reset(kept)
        if signed_in_now:
            request.vr_signed_in = True
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        """Record the activity of a request that reached its view (its CSRF check passed)."""
        state = getattr(request, "account_session", None)
        if state is None or not _is_activity(request):
            return None
        recorded = _touch(state)
        if recorded is None:
            return None  # the row is held elsewhere at this moment: go on as admitted
        if recorded:
            # As recorded, to the reading's precision: the page plans from this.
            request.account_session = dataclasses.replace(state, last_active_at=state.now)
            return None
        check_again(request, state)
        return None if _goes_on(request) else _refusal(request)


class NoStoreMiddleware:
    """The outermost layer: marks every answer to a signed-in session (request.vr_signed_in, set
    by SessionLimitMiddleware) not to be stored, after every other layer has drawn or replaced
    it."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if getattr(request, "vr_signed_in", False) and not getattr(response, "vr_cacheable", False):
            add_never_cache_headers(response)
        return response
