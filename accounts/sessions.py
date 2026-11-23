"""Sign-out after a period without activity and after the longest session the laboratory allows.

Each sign-in is an AccountSession row, created when the account signs in (Django's
user_logged_in, sent once every sign-in step has passed) and carried in the browser's session
by its id. SessionLimitMiddleware decides on every request to a signed-in session, before any
view or other middleware acts for the account:

- the row is missing (a session from before this release, or one whose row was erased), belongs
  to another account, or has ended (signed out, deactivated, a new password): sign out;
- no activity for the inactivity limit, or the longest session reached since sign-in: lock the
  row and read it again; if it had still passed a limit as of this request, by its values and the
  limits as they stand, end it with the limit passed first and the moment it passed, and sign
  out; if it had been extended or a limit lengthened meanwhile, go on;
- otherwise the request may go on. Once its address resolves to a view and its CSRF check has
  passed, it counts as activity: one UPDATE moves the row's last activity to the database's
  clock at that moment, never back, only while the row is open and inside both limits, so a row
  ended or expired in between is not brought back. A row another transaction holds at that
  moment is not waited for (another request of the session, or a change or an ending under way,
  which decide for themselves), except by Stay signed in, whose only purpose is this activity.

The limits are the laboratory's (core.session_limits), one pair for staff and one for client
portal accounts, chosen by what the account is when the request arrives, and read with the row
in one statement. Times are the database's, so every server process measures the same way.

Activity is a request the person's browser makes: opening a page, a save, a search, a dialog
or Stay signed in. Reading or typing on a page sends nothing, so it does not count; neither do an
address that does not exist, a request its CSRF check refuses, or the status address the page
asks how long remains before it warns.

A change that holds its acting account (accounts.acting.hold_actor) also holds the request's
session row, FOR SHARE, and is refused (SessionEnded, answered like a session found ended) when
the row has ended or passed a limit: ending a session (Sign out, a new password, a deactivation)
waits for such a change under way and refuses one started afterwards.

What a request does once its session ends depends on what it is. A page opened with GET goes on
as a signed-out visitor, so a page that needs a sign-in sends them to the sign-in page, which
returns to that page afterwards. Anything else (a form sent, a dialog or a partial update) is
answered at once, with the sign-in page set to return to the page the person was on; a dialog or
a partial update is told to load that page in the whole window. A form, a dialog or a partial
update that a view sends to sign in and back to its own address (login_required), from a browser
that is not signed in for any other reason (Django refused its session, it signed out in another
tab), is answered the same way.

Every answer to a signed-in session, or to one that ended or signed in during the request, is
marked not to be stored (no-store) by NoStoreMiddleware, the outermost layer, so the browser keeps
no copy of a page to show after sign-out, whichever layer drew the answer (a redirect added on
the way out, an error page). A response may opt out with `response.vr_cacheable = True` (the
laboratory's logo, which is not a page).
"""

import contextlib
import contextvars
import dataclasses
import ipaddress
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import parse_qs, quote, urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import HASH_SESSION_KEY, get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
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
# Where the page sends what was typed once its session has ended (accounts.kept): not activity,
# and answered whether or not the browser is still signed in.
KEEP_ROUTE = "account-session-keep"
Ending = AccountSession.Ending


ENDED_MESSAGES = {
    Ending.INACTIVITY: "You were signed out after {inactivity} without activity. Sign in again "
    "to continue.",
    Ending.MAXIMUM: "Your session reached its longest length of {maximum}. Sign in again to "
    "continue.",
}
ENDED_MESSAGE = "You were signed out. Sign in again to continue."
# Endings by a person, named in the notice whatever limits are in force.
ENDED_BY_MESSAGES = {
    Ending.SIGNED_OUT_ELSEWHERE: "You were signed out from another session of your account. Sign "
    "in again to continue.",
    Ending.ADMINISTRATOR: "An administrator ended your session. Sign in again to continue.",
    Ending.PASSWORD_COMPROMISED: "An administrator marked your password as compromised, so it no "
    "longer signs you in on its own. If your account has an authenticator app, sign in with your "
    "password and a code to choose a new password. Otherwise use Forgot your password? on the "
    "sign-in page, or ask the administrator to set a new one.",
}
# The same where the deployment sends no email, so Forgot your password? cannot help.
COMPROMISED_WITHOUT_MAIL = (
    "An administrator marked your password as compromised, so it no longer signs you in on its "
    "own. If your account has an authenticator app, sign in with your password and a code to "
    "choose a new password. Otherwise ask the administrator to set a new one."
)
# Endings the sign-in log records once the change that ended them commits (_log_after_commit).
LOGGED_ENDINGS = frozenset(
    {
        Ending.DEACTIVATED,
        Ending.NEW_PASSWORD,
        Ending.SIGNED_OUT_ELSEWHERE,
        Ending.ADMINISTRATOR,
        Ending.PASSWORD_COMPROMISED,
    }
)
# What a session row keeps of the browser's own description of itself (the User-Agent header).
USER_AGENT_LENGTH = AccountSession._meta.get_field("user_agent").max_length


class SessionEnded(Exception):  # noqa: N818 - named for what happened
    """A change refused because the session it was sent from ended or passed a limit after the
    request began. Not a PermissionDenied: a view that answers a refused permission itself must
    not answer this one, which SessionLimitMiddleware.process_exception answers by signing the
    browser out."""

    def __init__(self):
        super().__init__("Your session has ended. Sign in again to continue.")


# The acting session of this request, (account id, session id): for a password change made by
# the account itself (accounts.models.User.save keeps that session and ends the others) and for
# changes that hold their acting account (hold_acting_session).
_current = contextvars.ContextVar("account_session", default=None)
# The session a password change of this request kept, (account id, session id): it stays open
# only if the request also kept Django's session valid (_settle_kept_session).
_kept = contextvars.ContextVar("account_session_kept", default=None)
# The view this request reached (its resolved name), for kept_on_password_change and for Stay
# signed in's activity, which waits for its row.
_view = contextvars.ContextVar("account_session_view", default=None)
# The signed-in account of this request, so that its audience needs no further reading.
_account_var = contextvars.ContextVar("account_session_account", default=None)


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


def _limits_sql(user):
    """SQL for the account's limits as they stand when the statement runs (the defaults where
    none is set), with its parameters: (inactivity interval, longest interval, params)."""
    from core.session_limits import DEFAULTS

    _, limits = _tables()
    kind = audience(user)
    inactivity = (
        f"make_interval(mins => COALESCE((SELECT inactivity_minutes FROM {limits} "
        f"WHERE audience = %s), %s))"
    )
    longest = (
        f"make_interval(hours => COALESCE((SELECT maximum_hours FROM {limits} "
        f"WHERE audience = %s), %s))"
    )
    return (
        inactivity,
        longest,
        [kind, DEFAULTS["inactivity_minutes"]],
        [kind, DEFAULTS["maximum_hours"]],
    )


def _account(user_or_pk):
    """The account itself, given it or its id (for its audience)."""
    if hasattr(user_or_pk, "client_id"):
        return user_or_pk
    acting = _account_var.get()
    if acting is not None and acting.pk == user_or_pk:
        return acting
    return get_user_model().objects.only("pk", "client_id").get(pk=user_or_pk)


def _touch(state):
    """Record activity, only while the row is open and inside both limits as they stand, measured
    by the database's clock when the row is updated (clock_timestamp(), not when the statement
    began), and never move the last activity back.

    Returns the state as recorded (the new last activity, the limits and the clock the statement
    used) when recorded, and False when the row is missing or no longer qualifies (ended or
    expired since the reading). Returns None when another transaction holds the row at this
    moment (another request of this session recording its own activity, a change under way
    holding it, or an ending about to commit): this request does not wait for it and goes on as
    admitted; a change it makes is decided under its own hold (hold_acting_session). Stay signed
    in, whose only purpose is this activity, waits for the row instead."""
    sessions, _ = _tables()
    inactivity, longest, inactivity_params, longest_params = _limits_sql(_account(state.user_id))
    waits = _view.get() == CONTINUE_ROUTE
    skip = "" if waits else " SKIP LOCKED"
    # One statement unless it waits: then the wait and the activity share a transaction.
    with transaction.atomic() if waits else contextlib.nullcontext(), connection.cursor() as cursor:
        if waits:
            # The wait first, in its own statement; the statement below then reads the limits
            # and the row as they stand once the row is held (what committed meanwhile counts).
            cursor.execute(
                f"SELECT id FROM {sessions} WHERE id = %s AND user_id = %s FOR NO KEY UPDATE",
                [state.id, state.user_id],
            )
        cursor.execute(
            f"WITH target AS (SELECT id FROM {sessions} WHERE id = %s AND user_id = %s), "
            f"locked AS (SELECT id FROM {sessions} WHERE id = %s AND user_id = %s "
            f"FOR NO KEY UPDATE{skip}), "
            f"recorded AS (UPDATE {sessions} s "
            f"SET last_active_at = GREATEST(s.last_active_at, clock_timestamp()) FROM locked "
            f"WHERE s.id = locked.id AND s.ended_at IS NULL "
            f"AND s.last_active_at > clock_timestamp() - {inactivity} "
            f"AND s.started_at > clock_timestamp() - {longest} "
            f"RETURNING s.started_at, s.last_active_at, {inactivity}, {longest}, "
            f"clock_timestamp()) "
            f"SELECT (SELECT count(*) FROM target), (SELECT count(*) FROM locked), "
            f"r.* FROM (SELECT 1) one LEFT JOIN recorded r ON true",
            [
                state.id,
                state.user_id,
                state.id,
                state.user_id,
                *inactivity_params,
                *longest_params,
                *inactivity_params,
                *longest_params,
            ],
        )
        found, locked, started_at, last_active_at, inactivity_limit, longest_limit, now = (
            cursor.fetchone()
        )
    if not found:
        return False
    if not locked:
        return None
    if started_at is None:
        return False
    return SessionState(
        state.id, state.user_id, started_at, last_active_at, now, inactivity_limit, longest_limit
    )


class Settled(tuple):
    """What _settle_expiry found: unpacks to (outcome, detail); `limits` are (inactivity,
    longest) as read under the row's lock."""

    def __new__(cls, outcome, detail, limits):
        settled = super().__new__(cls, (outcome, detail))
        settled.limits = limits
        return settled


def _recorded_limits(ended_at, started_at, last_active_at):
    """(inactivity, longest) as a row's recorded end shows them: an expired row ended at the
    moment its limit passed, so the limit is that moment less the activity or the sign-in."""
    return ended_at - last_active_at, ended_at - started_at


def _ended_as_recorded(session_id):
    """(ending, limits) of an ended row as it records them, or (None, None)."""
    row = (
        AccountSession.objects.filter(pk=session_id, ended_at__isnull=False)
        .values_list("ending", "ended_at", "started_at", "last_active_at")
        .first()
    )
    if row is None:
        return None, None
    ending, ended_at, started_at, last_active_at = row
    return ending, _recorded_limits(ended_at, started_at, last_active_at)


def _settle_expiry(state):
    """For a row a request found expired: lock it (waiting for another request ending or
    extending it), then read it, the limits and the clock again in a new statement, so that what
    committed during the wait counts. End it if it has passed a limit, compared at the later of
    the request's reading and now, with the limit passed first and the moment it passed.

    Returns Settled("expired", ending) when this request ended it; Settled("ended", ending) when
    it had ended already (the ending recorded, or None for a missing row); Settled("open", state)
    with the row as it stands when it has not expired after all (extended, or a limit lengthened,
    since the reading). Each carries the limits read under the lock (Settled.limits)."""
    sessions, _ = _tables()
    inactivity, longest, inactivity_params, longest_params = _limits_sql(_account(state.user_id))
    limits = (state.inactivity, state.maximum)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            f"SELECT id FROM {sessions} WHERE id = %s AND user_id = %s FOR NO KEY UPDATE",
            [state.id, state.user_id],
        )
        if cursor.fetchone() is None:
            return Settled("ended", None, limits)
        cursor.execute(
            f"SELECT ended_at, ending, started_at, last_active_at, {inactivity}, {longest}, "
            f"clock_timestamp() FROM {sessions} WHERE id = %s",
            [*inactivity_params, *longest_params, state.id],
        )
        ended_at, recorded, started_at, last_active_at, inactivity_limit, longest_limit, now = (
            cursor.fetchone()
        )
        limits = (inactivity_limit, longest_limit)
        if ended_at is not None:
            # As the row records it: the limits that ended it, whatever they are now.
            return Settled(
                "ended", recorded, _recorded_limits(ended_at, started_at, last_active_at)
            )
        at = max(state.now, now)
        longest_at = started_at + longest_limit
        inactivity_at = last_active_at + inactivity_limit
        if longest_at > at and inactivity_at > at:
            fresh = SessionState(state.id, state.user_id, started_at, last_active_at, at, *limits)
            return Settled("open", fresh, limits)
        ending = Ending.MAXIMUM if longest_at <= inactivity_at else Ending.INACTIVITY
        AccountSession.objects.filter(pk=state.id).update(
            ended_at=min(longest_at, inactivity_at), ending=ending
        )
    return Settled("expired", ending, limits)


def _end_expired(state, ending=None):
    """End a row found expired (_settle_expiry). Returns 1 when this request ended it, else 0."""
    return 1 if _settle_expiry(state)[0] == "expired" else 0


def _lock_open(user, *, session_id=None, keep=None, everything=False, expired=False):
    """Lock the account's open rows asked for: the one given, every one when `everything`, and
    those past a limit by this reading when `expired`; never `keep`. All in one statement, in id
    order, FOR NO KEY UPDATE, waiting for a change under way that holds one. Returns their ids.
    Every ending locks its rows this way, once, so two endings never wait for each other in a
    cycle; the acting session a change holds first (hold_acting_session) is the exception, which
    the session-ending lock covers (accounts.session_list, accounts.models.User._end_sessions)."""
    sessions, _ = _tables()
    conditions, params = ["user_id = %s", "ended_at IS NULL"], [user.pk]
    if keep is not None:
        conditions.append("id <> %s::uuid")
        params.append(str(keep))
    wanted = []
    if everything:
        wanted.append("true")
    if session_id is not None:
        wanted.append("id = %s::uuid")
        params.append(str(session_id))
    if expired:
        inactivity, longest, inactivity_params, longest_params = _limits_sql(user)
        wanted.append(
            f"started_at + {longest} <= clock_timestamp() "
            f"OR last_active_at + {inactivity} <= clock_timestamp()"
        )
        params += [*longest_params, *inactivity_params]
    if not wanted:
        return []
    conditions.append("(" + " OR ".join(f"({part})" for part in wanted) + ")")
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT id FROM {sessions} WHERE {' AND '.join(conditions)} "
            f"ORDER BY id FOR NO KEY UPDATE",
            params,
        )
        return [str(row[0]) for row in cursor.fetchall()]


def _end_locked(user, ids, ending, *, expired_only=False):
    """End rows this transaction holds (_lock_open), in a new statement, so that what committed
    while it waited for them counts: each row still open that has passed a limit, by its values
    and the limits as they stand, at the database's clock now, ends as expired, with the limit
    passed first and the moment it passed; any other ends with `ending` (none when
    `expired_only`), at that clock and never before its last activity. Returns (id, ending,
    started_at) of each row ended."""
    if not ids:
        return []
    sessions, _ = _tables()
    inactivity, longest, inactivity_params, longest_params = _limits_sql(user)
    passed = "(s.started_at + lim.l <= lim.t OR s.last_active_at + lim.i <= lim.t)"
    with connection.cursor() as cursor:
        cursor.execute(
            f"WITH lim AS (SELECT {inactivity} AS i, {longest} AS l, clock_timestamp() AS t) "
            f"UPDATE {sessions} s SET "
            f"ending = CASE WHEN {passed} THEN CASE WHEN s.started_at + lim.l <= "
            f"s.last_active_at + lim.i THEN %s ELSE %s END ELSE %s END, "
            f"ended_at = CASE WHEN {passed} "
            f"THEN LEAST(s.started_at + lim.l, s.last_active_at + lim.i) "
            f"ELSE GREATEST(s.last_active_at, lim.t) END "
            f"FROM lim WHERE s.id = ANY(%s::uuid[]) AND s.ended_at IS NULL"
            f"{f' AND {passed}' if expired_only else ''} "
            f"RETURNING s.id, s.ending, s.started_at",
            [
                *inactivity_params,
                *longest_params,
                Ending.MAXIMUM,
                Ending.INACTIVITY,
                ending or Ending.INACTIVITY,
                ids,
            ],
        )
        return [(str(pk), recorded, started_at) for pk, recorded, started_at in cursor.fetchall()]


def _log_expired(user, ended, manual=None):
    """Rows ended as expired (not with `manual`), logged by their limit once this commits."""
    endings = [recorded for _pk, recorded, _started in ended if recorded != manual]
    for recorded in sorted(set(endings)):
        _log_after_commit(recorded, user, endings.count(recorded))


def close_expired(user):
    """End the account's open rows that have passed a limit, with the limit passed first and the
    moment it passed: the rows past a limit by a first reading are locked (waiting for a change
    under way that holds one), then each is ended only if, read again with the limits as they
    stand, it still has (_end_locked). One transaction, so the rows stay locked between the two. A
    browser that closed or was abandoned leaves its row open until then. Each one ended is
    recorded in the sign-in log once this commits. Returns how many ended."""
    with transaction.atomic(savepoint=False):
        ended = _end_locked(user, _lock_open(user, expired=True), "", expired_only=True)
    _log_expired(user, ended)
    return len(ended)


def end_sessions(user, ending, *, keep=None, session_id=None):
    """End the account's open sessions (or the one given) with this reason, except `keep`. Their
    browsers are signed out at their next request. A row that has passed a limit when it is
    ended, read after any wait for it, ends as expired instead and is not counted as ended by
    this. A change under way that holds one of the rows (hold_acting_session) is waited for.
    Returns how many ended."""
    return len(end_session_rows(user, ending, keep=keep, session_id=session_id))


def end_session_ids(user, ending, *, keep=None, session_id=None, cleanup=False):
    """end_sessions, returning the ids of the rows this ended with `ending`, sorted."""
    rows = end_session_rows(user, ending, keep=keep, session_id=session_id, cleanup=cleanup)
    return [pk for pk, _started in rows]


def end_session_rows(user, ending, *, keep=None, session_id=None, cleanup=False):
    """end_sessions, returning (id, started_at) of each row this ended with `ending`, by id. The
    rows are locked first, all at once in id order, and decided after the wait (_end_locked), in
    one transaction. With `cleanup` and one session given, the account's other rows past a limit
    are locked with it and ended as expired (close_expired), so that the two are not locked out of
    order."""
    everything = session_id is None
    if not everything:
        session_id = _valid_id(session_id)  # one spelling: the database returns the dashed form
        if session_id is None:
            return []
    with transaction.atomic(savepoint=False):
        locked = _lock_open(
            user,
            session_id=session_id,
            keep=keep,
            everything=everything,
            expired=cleanup and not everything,
        )
        targets = locked if everything else [pk for pk in locked if pk == str(session_id)]
        ended = _end_locked(user, targets, ending)
        others = [pk for pk in locked if pk not in targets]
        ended += _end_locked(user, others, "", expired_only=True)
    _log_expired(user, ended, manual=ending)
    rows = sorted((pk, started) for pk, recorded, started in ended if recorded == ending)
    if rows and ending in LOGGED_ENDINGS:
        _log_after_commit(ending, user, len(rows))
    return rows


def hold_acting_session(user):  # the account or its id
    """For a change that holds its acting account (accounts.acting.hold_actor, with the account as
    held): when this request acts for that account through a signed-in session, hold the
    session's row FOR SHARE until the transaction ends and say whether the session may still act:
    open, and inside both limits as they stand, by the database's clock now. Ending the session
    waits for the change, and hold_actor refuses a change from a session that has ended or expired
    (SessionEnded). True when the request has no session of that account (a job, a command, a
    test calling a service)."""
    current = _current.get()
    user_pk = getattr(user, "pk", user)
    if current is None or current[0] != user_pk:
        return True
    sessions, _ = _tables()
    inactivity, longest, inactivity_params, longest_params = _limits_sql(_account(user))
    with connection.cursor() as cursor:
        # The hold first; then, in a new statement, the row, the limits and the clock as they
        # stand once it is held (what committed while it waited included).
        cursor.execute(
            f"SELECT id FROM {sessions} WHERE id = %s AND user_id = %s FOR SHARE",
            [current[1], user_pk],
        )
        if cursor.fetchone() is None:
            return False
        cursor.execute(
            f"SELECT ended_at IS NULL "
            f"AND last_active_at > clock_timestamp() - {inactivity} "
            f"AND started_at > clock_timestamp() - {longest} "
            f"FROM {sessions} WHERE id = %s",
            [*inactivity_params, *longest_params, current[1]],
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


# The page where an account's password is set without its sessions being kept: a reset by
# email, even from a browser signed in to the account. Elsewhere the account's own session is
# kept when it changes its own password, and checked again after the view
# (_settle_kept_session), since a page may still not keep Django's session.
RESETTING_VIEWS = frozenset({"account_reset_password_from_key"})


def kept_on_password_change(user):
    """The session to keep when this account's password changes: the one making the change, when
    the account changes its own password other than by a reset (RESETTING_VIEWS). None otherwise
    (a password set for another account, a reset by email), so that every session of the account
    ends in the same transaction as the password. A kept session stays open only if the request
    keeps Django's session valid too (_settle_kept_session)."""
    current = _current.get()
    if current is not None and current[0] == user.pk and _view.get() not in RESETTING_VIEWS:
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


def reported_by(request):
    """(browser, address) as the request reports them, for its session row: the browser's
    User-Agent header, printable characters only, cut to USER_AGENT_LENGTH ("" without one), and
    the client address by the deployment's trusted proxy settings (ALLAUTH_TRUSTED_PROXY_COUNT or
    ALLAUTH_TRUSTED_CLIENT_IP_HEADER, as the sign-in limits read it), or None when there is none
    or it is not an address. Both are what the browser and the proxies say, not proof of where
    the person is."""
    from allauth.account.adapter import get_adapter

    agent = "".join(
        character
        for character in str(request.META.get("HTTP_USER_AGENT", ""))
        if character.isprintable()
    ).strip()[:USER_AGENT_LENGTH]
    try:
        address = ipaddress.ip_address(get_adapter(request).get_client_ip(request))
    except PermissionDenied, ImproperlyConfigured, TypeError, ValueError:
        return agent, None
    if getattr(address, "scope_id", None):
        # An IPv6 zone ("%eth0") names the server's own interface, and the column refuses it.
        address = ipaddress.ip_address(str(address).split("%", 1)[0])
    return agent, address.compressed


@receiver(user_logged_in, dispatch_uid="lab_ops_account_session_start")
def start_session(sender, request, user, **kwargs):
    from django.contrib.auth.models import update_last_login

    from .compromised import refused_at_sign_in
    from .session_list import lock_endings

    session = getattr(request, "session", None)
    agent, address = reported_by(request) if session is not None else ("", None)
    with transaction.atomic():
        # The account's session-ending lock first, as the person's own endings, a new password
        # saved alone and marking a password as compromised take it, so they take turns; then the
        # sign-in's time, which writes the account's row (Django's own receiver, disconnected in
        # accounts.apps so that its hold on the row comes after this lock, also with a transaction
        # around the request).
        lock_endings(user.pk)
        # The account's password is marked as compromised and this sign-in proved nothing more
        # than the password (accounts.compromised): its session starts ended, so the browser is
        # signed out at its next request, and the sign-in is not recorded as the last one.
        refused = refused_at_sign_in(request, user)
        if not refused:
            update_last_login(sender, user)
        if session is None:
            return
        previous = _valid_id(session.get(SESSION_KEY))
        if previous is not None:
            # Signed in again over an open session in the same browser (Django keeps the
            # session's data when the account is the same one), with the sessions abandoned
            # since and past a limit, locked together in id order.
            end_session_rows(user, Ending.SIGNED_IN_AGAIN, session_id=previous, cleanup=True)
        else:
            close_expired(user)  # sessions abandoned since, past a limit
        # Whether the laboratory's rule requires a second factor of the account as it signs in
        # (accounts.enrolment holds the session to it while the account has none).
        from core.second_factor import requires

        ended = {"ended_at": Now(), "ending": Ending.PASSWORD_COMPROMISED} if refused else {}
        record = AccountSession.objects.create(
            user=user,
            user_agent=agent,
            address=address,
            second_factor_required=requires(user),
            **ended,
        )
    if refused:
        _log_after_commit(Ending.PASSWORD_COMPROMISED, user, 1)
        request.vr_sign_in_refused = True  # accounts.adapter: not announced as signed in
    session[SESSION_KEY] = str(record.pk)
    # A new sign-in remembers its own page for Continue after a second factor is added
    # (accounts.enrolment), not one remembered for an earlier sign-in in this browser.
    from .enrolment import NEXT_KEY

    session.pop(NEXT_KEY, None)


@receiver(user_logged_out, dispatch_uid="lab_ops_account_session_end")
def end_signed_out_session(sender, request, user, **kwargs):
    from .kept import discard_all

    session = getattr(request, "session", None)
    session_id = _valid_id(session.get(SESSION_KEY)) if session is not None else None
    if user is not None and session_id is not None:
        end_sessions(user, Ending.SIGNED_OUT, session_id=session_id)
    if user is not None:
        # Signing out leaves nothing typed behind: every copy kept for the account goes, and the
        # moment is recorded, so a copy sent later from an ended session finds that Sign out.
        discard_all(user.pk)


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
    # Whose session this request ended (a Sign out arriving just after the limit, _refusal).
    request.vr_ended_account = user.pk
    request.session.flush()
    request.user = AnonymousUser()
    request.account_session = None
    _current.set(None)
    if ending == Ending.PASSWORD_COMPROMISED:
        from jobs.outbox import mail_enabled

        text = ENDED_BY_MESSAGES[ending] if mail_enabled() else COMPROMISED_WITHOUT_MAIL
    elif ending in ENDED_BY_MESSAGES:
        text = ENDED_BY_MESSAGES[ending]
    elif ending in ENDED_MESSAGES and limits is not None:
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


def _expired(row):
    """The limit a row read by _read had passed when it was read, or None. When both had, the one
    that passed first (the longest session on a tie), as close_expired and _end_expired decide."""
    _, started_at, last_active_at, _ended, now, minutes, hours = row
    longest_at = started_at + timedelta(hours=hours)
    inactivity_at = last_active_at + timedelta(minutes=minutes)
    if longest_at <= now and longest_at <= inactivity_at:
        return Ending.MAXIMUM
    if inactivity_at <= now:
        return Ending.INACTIVITY
    return None


def _decide(request):
    """Whether the signed-in session of this request may go on, from a reading of its row and its
    limits. Returns its state when it may, or None after signing it out. A row found expired is
    ended only if, locked and read again, it still had expired (_settle_expiry); if it had been
    extended or a limit lengthened meanwhile, the request goes on from that reading."""
    user = request.user
    session_id = _valid_id(request.session.get(SESSION_KEY))
    row = _read(session_id, user) if session_id else None
    if row is None or row[0] != user.pk:
        _sign_out(request, None)
        return None
    if row[3] is not None:
        # Ended already: the notice names the limit the row records, if a limit ended it.
        _sign_out(request, *_ended_as_recorded(session_id))
        return None
    _, started_at, last_active_at, _ended, now, minutes, hours = row
    limits = (timedelta(minutes=minutes), timedelta(hours=hours))
    state = SessionState(session_id, user.pk, started_at, last_active_at, now, *limits)
    if _expired(row) is None:
        return state
    settled = _settle_expiry(state)
    (outcome, detail), limits = settled, settled.limits
    if outcome == "open":
        return detail
    # The notice names the ending the row records (the limit and its value as read under the
    # lock), or none when it ended otherwise (Sign out in another tab, say).
    _sign_out(request, detail, limits, ended=1 if outcome == "expired" else 0)
    return None


def check(request):
    """Decide whether the signed-in session of this request may go on (_decide). Records nothing:
    the activity is recorded once the request reaches a view (SessionLimitMiddleware.process_view).
    """
    return _decide(request)


def _session_endpoints():
    """The status, Stay signed in and keep addresses; none under a URL configuration without
    them."""
    try:
        return {_route_path(STATUS_ROUTE), _route_path(CONTINUE_ROUTE), _route_path(KEEP_ROUTE)}
    except NoReverseMatch:
        return set()


def _is_activity(request):
    """Whether a request that reached its view counts as activity: every one but the status
    question and keeping what was typed, and Stay signed in only as the POST it is."""
    match = request.resolver_match
    name = match.view_name if match is not None else ""
    if name in (STATUS_ROUTE, KEEP_ROUTE):
        return False
    return name != CONTINUE_ROUTE or request.method == "POST"


def _goes_on(request):
    """After its session ended: a page opened with GET, or a session question, goes on as a
    signed-out visitor; anything else is answered at once (_refusal)."""
    if request.path_info in _session_endpoints():
        return True
    return request.method in ("GET", "HEAD") and _is_navigation(request)


def _is_sign_out(request):
    """Whether the request is Sign out (the application's or the admin console's)."""
    paths = set()
    for name in ("account_logout", "admin:logout"):
        try:
            paths.add(_route_path(name))
        except NoReverseMatch:
            pass
    return request.method == "POST" and request.path_info in paths


def _csrf_probe(request):
    """Stands in for a view in _csrf_passes."""


def _csrf_passes(request):
    """Whether the request passes Django's CSRF check (as the view it was refused before would
    have checked it)."""
    from django.middleware.csrf import CsrfViewMiddleware

    return CsrfViewMiddleware(_csrf_probe).process_view(request, _csrf_probe, (), {}) is None


def _keep_refused(request):
    """For a form or Sign out refused because the browser's session ended: a form posted with
    its keep token (accounts.kept) is kept, if its session ended by a limit; Sign out from a
    session this request found past its limit still discards what was kept for the account."""
    from .kept import discard_all, keep_posted, notify

    account = getattr(request, "vr_ended_account", None)
    if _is_sign_out(request):
        # As the Sign out view itself would, only for a request from the application's own page.
        if account is not None and _csrf_passes(request):
            discard_all(account)
        return False
    if keep_posted(request):
        notify(request)
        return True
    return False


def _refusal(request):
    """The answer to a request whose session ended, other than a page opened with GET, after
    keeping what its form posted (_keep_refused). Signing in again returns to the page on screen,
    or, when what the form posted was kept and it was sent from the shared form dialog or from a
    page the browser does not name, to the address it was posted to, where it is given back (a
    form is posted to its page's address; the reply addresses send a GET on to their ticket)."""
    held = _keep_refused(request)
    back = _page_on_screen(request)
    if held and (request.headers.get("X-VR-Form") or back is None):
        back = request.get_full_path()
    sign_in = sign_in_url(back)
    if _is_navigation(request):
        response = HttpResponseRedirect(sign_in)
    else:
        # `kept`: what this form posted is the copy held (static/js/session.js lets its page go);
        # not when a copy of other text kept before for the session and form stands instead.
        kept_now = held and getattr(request, "vr_kept_own", False)
        response = JsonResponse(
            {"signed_in": False, "sign_in": sign_in, "kept": kept_now}, status=401
        )
        response["HX-Redirect"] = sign_in
    add_never_cache_headers(response)
    return response


def _sign_in_paths():
    """The addresses that send a browser to sign in: the application's sign-in page and the admin
    console's, which forwards to it with the same `next`."""
    paths = set()
    for name in (settings.LOGIN_URL, "admin:login"):
        try:
            paths.add(resolve_url(name))
        except NoReverseMatch:
            pass
    return paths


def _asks_to_sign_in(request, response):
    """Whether the response sends the browser to sign in and then back to this very request's
    address (as login_required and the admin console do), directly or through HTMX. A sign-in
    that returns elsewhere is left as it is."""
    paths = _sign_in_paths()
    for header in ("Location", "HX-Redirect"):
        target = urlsplit(response.get(header, "") or "")
        if target.path not in paths or target.netloc:
            continue
        back = parse_qs(target.query).get("next", [])
        if back and back[0] == request.get_full_path():
            return True
    return False


def _has_session_cookie(request):
    return settings.SESSION_COOKIE_NAME in request.COOKIES


class SessionLimitMiddleware:
    """After the session, authentication and message middleware, so that it decides before
    anything acts for the account; after the locked-credentials check too, so that a page that
    check refuses before its view is not activity (process_view runs in this order)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        signed_in = _has_session_cookie(request) and request.user.is_authenticated
        request.vr_signed_in = signed_in
        request.account_session = None
        signed_in_now = signed_in
        tokens = (
            _current.set(None),
            _kept.set(None),
            _view.set(None),
            _account_var.set(request.user if signed_in else None),
        )
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
                and _asks_to_sign_in(request, response)
                and not _goes_on(request)
            ):
                if _page_on_screen(request):
                    # Not signed in for another reason (Django refused the session, Sign out in
                    # another tab, never signed in), and the page on screen is known: the same
                    # answer as for a session that ended, with the cookies the answer replaced
                    # set.
                    cookies = response.cookies
                    response = _refusal(request)
                    cookies.update(response.cookies)
                    response.cookies = cookies
                else:
                    # The page on screen is not known (no referrer): the view's own answer, after
                    # keeping what the form posted all the same.
                    _keep_refused(request)
        finally:
            _current.reset(tokens[0])
            _kept.reset(tokens[1])
            _view.reset(tokens[2])
            _account_var.reset(tokens[3])
        if signed_in_now:
            request.vr_signed_in = True
        # The browser's keep cookie, when a page drew a form that keeps typing or a copy was kept
        # (accounts.kept).
        from .kept import remember_browser

        remember_browser(request, response)
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        """Record the activity of a request that reached its view (its CSRF check passed, no
        earlier check answered instead)."""
        match = request.resolver_match
        _view.set(match.view_name if match is not None else None)
        state = getattr(request, "account_session", None)
        if state is None or not _is_activity(request):
            return None
        for _attempt in range(2):
            recorded = _touch(state)
            if recorded is None:
                return None  # the row is held elsewhere at this moment: go on as admitted
            if recorded:
                # As recorded: the page plans from this (Stay signed in answers with it).
                request.account_session = (
                    recorded
                    if isinstance(recorded, SessionState)
                    else dataclasses.replace(state, last_active_at=state.now)
                )
                return None
            # Not recorded: ended or expired since the reading, or a limit changed. Decide again.
            state = _decide(request)
            if state is None:
                return None if _goes_on(request) else _refusal(request)
            request.account_session = state
        # Twice found live and twice not recorded (limits changing as it is read): the last
        # decision stands, and the request goes on without its activity.
        return None

    def process_exception(self, request, exception):
        """A change refused because its session ended or passed a limit after the request began
        (SessionEnded): a row found past a limit is ended and recorded as a request would
        (_settle_expiry), and the browser signed out, with the sign-in page set to return to the
        page on screen (to the page itself for a page opened with GET), as for a session found
        ended when the request arrived. If the row turns out to be live after all (a limit
        lengthened since the change was refused), it is ended as the change found it
        (CHANGE_REFUSED) and the browser signed out all the same, so the browser and the row
        agree."""
        if not isinstance(exception, SessionEnded):
            return None
        user = getattr(request, "user", None)
        state = getattr(request, "account_session", None)
        if user is not None and user.is_authenticated:
            ending, limits, ended = None, None, 0
            if state is not None:
                settled = _settle_expiry(state)
                (outcome, detail), limits = settled, settled.limits
                if outcome != "open":
                    ending, ended = detail, 1 if outcome == "expired" else 0
                elif not end_sessions(user, Ending.CHANGE_REFUSED, session_id=state.id):
                    # Ended meanwhile by another request (a limit, as cleanup found it): the
                    # notice names what the row records.
                    ending, limits = _ended_as_recorded(state.id)
            _sign_out(request, ending, limits, ended=ended)
        if request.method in ("GET", "HEAD") and _is_navigation(request):
            response = HttpResponseRedirect(sign_in_url(request.get_full_path()))
            add_never_cache_headers(response)
            return response
        return _refusal(request)


class NoStoreMiddleware:
    """The outermost layer: marks every answer to a signed-in session (request.vr_signed_in, set
    by SessionLimitMiddleware), and every answer to a request carrying a session cookie that an
    earlier layer drew before that was known (a canonical-host redirect), not to be stored, after
    every other layer has drawn or replaced it. Static files served as such keep their caching."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from whitenoise.middleware import WhiteNoiseFileResponse

        response = self.get_response(request)
        if getattr(response, "vr_cacheable", False) or (
            isinstance(response, WhiteNoiseFileResponse) and response.status_code in (200, 206, 304)
        ):
            return response
        decided = hasattr(request, "vr_signed_in")
        if (decided and request.vr_signed_in) or (not decided and _has_session_cookie(request)):
            add_never_cache_headers(response)
        return response
