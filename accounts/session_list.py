"""Where an account is signed in, and ending its sessions (accounts.sessions holds the rows).

- open_sessions(user): the account's own sessions that are open and inside both limits as they
  stand, by the database's clock, most recent activity first. A row a browser abandoned stays
  open in the table until something closes it (accounts.sessions.close_expired); it is not listed
  once it has passed a limit.
- sign_out_other / sign_out_others: the person ends one of their other sessions, or every other
  one open when the change is saved, from Where you are signed in. The session the request comes
  from is never ended here: Sign out ends it.
- end_account_sessions: a superuser ends every open session of an account from the account
  administration, with a reason (their own current session excepted, when the account is theirs).

A browser whose session ended is signed out at its next request, with a notice saying how
(accounts.sessions.ENDED_BY_MESSAGES). Each ending runs in one transaction that:

1. takes the session-ending lock of the account whose sessions it ends and of the acting account,
   in key order, so that two sessions ending each other take turns: the second then finds its
   own session ended and is refused, instead of each holding its own session's row and waiting
   for the other's (a deadlock);
2. holds the acting account (accounts.acting.hold_actor), which holds the session the request
   acts through FOR SHARE and refuses a change from one that has ended or passed a limit
   (SessionEnded), and decides from the account as held (active; a superuser for the account
   administration; not a shared demonstration account for the person's own page);
3. locks the rows asked for, in id order, waiting for a change under way that holds one of them
   (it finishes first; one started from that session afterwards is refused), then, in a new
   statement, so that what committed during the wait counts, ends each: as expired, at the moment
   its limit passed, when it has passed one by then, otherwise as asked
   (accounts.sessions.end_session_rows);
4. records one audit event, `account.sessions_ended`, on the account whose sessions ended, with
   how they ended and the sessions' ids, never their browser or address, and the reason given by
   the administrator.

What the page showed is not what is ended: Sign out of all other sessions ends every other
session open when it is saved, any signed in after the page was opened included, and says how
many signed in after the page's reading, by their sign-in time against the database's clock when
the page read them.
"""

import re
import uuid
from dataclasses import dataclass

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction

from .acting import INT4, hold_actor
from .models import AccountSession, User
from .sessions import _limits_sql, _tables, end_session_ids, end_session_rows

Ending = AccountSession.Ending
ACTION = "account.sessions_ended"
# The advisory lock namespace of the session-ending locks (PostgreSQL's two-key form; see
# accounts.acting and core.session_limits for the others): the second key is the account.
SESSION_ENDINGS = 19559

LOCKED_REFUSAL = (
    "This is a shared demonstration account. Its other sessions belong to other visitors, so they "
    "are not listed and cannot be signed out here."
)
NOT_OPEN = "That session had already ended, so nothing changed."


@dataclass(frozen=True)
class OpenSession:
    id: str
    started_at: object
    last_active_at: object
    user_agent: str
    address: str | None

    @property
    def browser(self):
        return browser_label(self.user_agent)


def _lock_endings(*account_ids):
    keys = sorted({pk % INT4 for pk in account_ids if pk is not None})
    with connection.cursor() as cursor:
        for key in keys:
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [SESSION_ENDINGS, key])


def reading(user):
    """(sessions, now): the account's sessions open and inside both limits as they stand, by the
    database's clock at this statement, most recent activity first (then the latest sign-in), and
    that clock, which tells a later change what signed in after this reading."""
    sessions, _ = _tables()
    inactivity, longest, inactivity_params, longest_params = _limits_sql(user)
    with connection.cursor() as cursor:
        cursor.execute(
            f"WITH c AS (SELECT clock_timestamp() AS t) "
            f"SELECT c.t, s.id, s.started_at, s.last_active_at, s.user_agent, s.address "
            f"FROM c LEFT JOIN {sessions} s ON s.user_id = %s AND s.ended_at IS NULL "
            f"AND s.last_active_at > c.t - {inactivity} AND s.started_at > c.t - {longest} "
            f"ORDER BY s.last_active_at DESC, s.started_at DESC, s.id",
            [user.pk, *inactivity_params, *longest_params],
        )
        rows = cursor.fetchall()
    now = rows[0][0]
    return [
        OpenSession(
            str(pk), started_at, last_active_at, agent or "", str(address) if address else None
        )
        for _now, pk, started_at, last_active_at, agent, address in rows
        if pk is not None
    ], now


def open_sessions(user):
    """The account's sessions open and inside both limits as they stand (reading)."""
    return reading(user)[0]


def open_count(user):
    """How many sessions of the account are open and inside both limits, as open_sessions."""
    return len(open_sessions(user))


def _valid_id(value):
    try:
        return str(uuid.UUID(str(value)))
    except TypeError, ValueError, AttributeError:
        return None


def _held_person(actor, current):
    """The acting account as held, for its own sessions: active, signed in through `current`, and
    not a shared demonstration account."""
    held = hold_actor(actor)
    if held is None or not held.is_active or current is None:
        raise PermissionDenied("You can no longer do this.")
    if held.credentials_locked:
        raise PermissionDenied(LOCKED_REFUSAL)
    return held


def _record(actor, account, ending, ids, reason):
    from core.audit import record_event

    record_event(
        actor=actor,
        action=ACTION,
        obj=account,
        after={"ending": ending, "sessions": ids, "count": len(ids)},
        reason=reason,
    )


def sign_out_other(*, actor, current, session_id):
    """End one of the person's other sessions. `current` is the session the request acts
    through. Returns the ids ended: none when the session is not an open one of theirs (ended,
    past a limit, another account's or no session at all, which look the same)."""
    target = _valid_id(session_id)
    current = _valid_id(current)
    if target is not None and target == current:
        raise ValidationError(
            "This is the session you are using now. Use Sign out in your account menu to end it."
        )
    with transaction.atomic():
        _lock_endings(getattr(actor, "pk", None))
        held = _held_person(actor, current)
        if target is None:
            return []
        ids = end_session_ids(held, Ending.SIGNED_OUT_ELSEWHERE, session_id=target, keep=current)
        if ids:
            _record(held, held, Ending.SIGNED_OUT_ELSEWHERE, ids, "Signed out from another session")
        return ids


@dataclass(frozen=True)
class Ended:
    """What Sign out of all other sessions ended: the ids, and how many of them signed in after
    the page's reading (None when the page's reading is not known)."""

    ids: list
    later: int | None


def sign_out_others(*, actor, current, drawn_at=None):
    """End every other open session of the person, those signed in after the page was opened
    included. `drawn_at` is the database's clock when the page read the sessions (reading):
    sessions that signed in after it are counted as later. A session the page did not list
    because it had passed a limit, and that a lengthened limit has made open again, ends too but
    is not counted as later: it signed in before."""
    current = _valid_id(current)
    with transaction.atomic():
        _lock_endings(getattr(actor, "pk", None))
        held = _held_person(actor, current)
        rows = end_session_rows(held, Ending.SIGNED_OUT_ELSEWHERE, keep=current)
        ids = [pk for pk, _started in rows]
        if ids:
            _record(held, held, Ending.SIGNED_OUT_ELSEWHERE, ids, "Signed out from another session")
        later = None if drawn_at is None else sum(started > drawn_at for _pk, started in rows)
        return Ended(ids, later)


def can_end_sessions(user):
    """Whether the account may end other accounts' sessions: the account administration's owner
    (an active superuser with staff status, who opens it)."""
    return bool(user and user.is_active and user.is_superuser and user.is_staff)


def end_account_sessions(*, actor, account_id, reason, current=None):
    """A superuser ends every open session of the account, with a reason. On their own account,
    the session they act through (`current`) stays open. Returns the ids ended."""
    from jobs.actor import is_system_account

    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required."})
    with transaction.atomic():
        _lock_endings(getattr(actor, "pk", None), account_id)
        held = hold_actor(actor)
        if held is None or not can_end_sessions(held):
            raise PermissionDenied("Only a superuser can end an account's sessions.")
        account = User.objects.filter(pk=account_id).first()
        if account is None:
            raise User.DoesNotExist
        if is_system_account(account):
            raise PermissionDenied("Nobody signs in as the scheduled jobs' account.")
        keep = _valid_id(current) if account.pk == held.pk else None
        ids = end_session_ids(account, Ending.ADMINISTRATOR, keep=keep)
        if ids:
            _record(held, account, Ending.ADMINISTRATOR, ids, reason)
        return ids


# The browser's own description of itself (the User-Agent header), in a few words. Tokens are
# tried in order: several browsers name the others too (Chrome's string says Safari; Edge's says
# Chrome and Safari).
_BROWSERS = (
    (re.compile(r"\bEdg(e|A|iOS)?/"), "Edge"),
    (re.compile(r"\b(OPR|Opera)/"), "Opera"),
    (re.compile(r"\bSamsungBrowser/"), "Samsung Internet"),
    (re.compile(r"\b(Firefox|FxiOS)/"), "Firefox"),
    (re.compile(r"\b(HeadlessChrome|Chrome|CriOS|Chromium)/"), "Chrome"),
    (re.compile(r"\bVersion/[\d.]+.*\bSafari/"), "Safari"),
)
_SYSTEMS = (
    (re.compile(r"\biPhone\b"), "iPhone"),
    (re.compile(r"\biPad\b"), "iPad"),
    (re.compile(r"\bAndroid\b"), "Android"),
    (re.compile(r"\bCrOS\b"), "ChromeOS"),
    (re.compile(r"\bWindows\b"), "Windows"),
    (re.compile(r"\b(Macintosh|Mac OS X)\b"), "macOS"),
    (re.compile(r"\bLinux\b"), "Linux"),
)


def browser_label(user_agent):
    """ "Firefox on Windows", "Safari on iPhone" and the like, from what the browser reported;
    "Unknown browser" when it reported nothing recognisable."""
    agent = user_agent or ""
    browser = next((name for pattern, name in _BROWSERS if pattern.search(agent)), None)
    system = next((name for pattern, name in _SYSTEMS if pattern.search(agent)), None)
    if browser and system:
        return f"{browser} on {system}"
    if browser or system:
        return browser or f"Browser on {system}"
    return "Unknown browser"
