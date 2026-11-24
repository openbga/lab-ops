"""Each account's sign-in history, kept in the database (SignInEvent rows), and the page where a
person reads their own.

What is recorded, for the account it concerns:

- a sign-in (the session starts), with what this sign-in proved (sign_in_factor, read when
  allauth completes it, accounts.adapter.ClosedSignupAdapter.login), and a sign-out (the session
  ends), with why: written by the database itself when a session row starts or ends (accounts
  migration 0009), with the browser and address the session reported at sign-in;
- a sign-in refused as it starts, after its password or emailed code was accepted: the password
  was marked as compromised and nothing more was proved (accounts.compromised), or the account is
  deactivated, also when it was deactivated while the sign-in was under way (the session then
  starts ended, accounts.sessions.start_session; without a browser session, no session starts
  and the refusal is written here); recorded for the account the password or code was accepted
  for;
- a failed attempt: a password, an authenticator or recovery code, or an emailed code that was
  submitted and not accepted at sign-in; a password or code not accepted on a re-authentication
  page, and the current password not accepted on Change password (also the admin console's), for
  the signed-in account;
- a refusal after too many failed attempts (the failed sign-in limit), before the password or the
  code was checked: at most one entry a minute for each account, step and address
  (LOCKOUT_EVERY; concurrent refusals for one account take turns), since the second step has no
  request limit of its own.

A failed password or a refusal by the limit at the password step of a sign-in is recorded only
when the attempt names exactly one account (by username or email address, in any letter case,
as the sign-in log matches it, accounts.signin_log): one for a name or an address that matches no
account, or more than one, names nobody and stays in the sign-in log. A code's failure belongs to
the account already identified at that step. No entry holds a password, a code, or what was typed as
the username or email address. The browser and the address are what the request reported
(accounts.sessions.reported_by), not proof of anything.

Not recorded: an attempt the request limit stopped before the form was read, an emailed code form
sent without a code, a code entered after its emailed sign-in expired, a request for an emailed
code, and attempts that a process of the earlier release handles while a deployment replaces it
(the database still records the sessions such a process starts and ends).

Every account, staff or client portal, reads its own entries of the last RECENT_DAYS, the most
recent RECENT_LIMIT, on Sign-in history. A shared demonstration account (credentials_locked) is
used by other visitors too: its entries are listed, but the browser and address only for the
session the page is read from. Whether the account is shared is read in the same statement as its
entries. Staff who hold the capability to review access and audit records read every account's
entries in the audit review workspace (audit_review.sources); no other page shows them.
"""

import ipaddress
from dataclasses import dataclass

from django.db import connection, transaction

from .models import AccountSession, SignInEvent, SignInFactor

Kind = SignInEvent.Kind
Ending = AccountSession.Ending
RECENT_DAYS = 90
RECENT_LIMIT = 100
# A refusal by the failed sign-in limit is recorded at most once in this many seconds for each
# account, step and address.
LOCKOUT_EVERY = 60
# The advisory lock namespace (PostgreSQL's two-key form; see accounts.acting for the others) that
# writing a refusal by the limit takes for its account, so that two at once take turns.
SIGN_IN_LOCKOUTS = 19561

# What a step of a sign-in proved, as allauth records it in the browser's session.
_FIRST = {"password": "password", "code": "code"}
_SECOND = {"totp": "totp", "recovery_codes": "recovery"}

# The details column, by kind: what an attempt failed at, or why a session ended.
NOT_ACCEPTED = {
    SignInFactor.PASSWORD: "Password not accepted",
    SignInFactor.SECOND_FACTOR: "Authenticator or recovery code not accepted",
    SignInFactor.CODE: "Emailed code not accepted",
}
REFUSED_BEFORE = {
    SignInFactor.PASSWORD: "Refused before the password was checked",
    SignInFactor.SECOND_FACTOR: "Refused before the code was checked",
    SignInFactor.CODE: "Refused before the code was checked",
}
ENDING_DETAILS = {Ending.SIGNED_OUT: "With Sign out"}
NOT_RECORDED = "Not recorded"


def sign_in_factor(request, user):
    """What a sign-in of `user` proved, from the steps allauth recorded in the browser's session,
    leaving out re-authentications: the last password or emailed code, and an authenticator or
    recovery code entered after it. Blank when no step is recorded. Read when allauth completes
    the sign-in (ClosedSignupAdapter.login, before Django may empty the session for another
    account), and by accounts.compromised for what a session proved."""
    from allauth.account.internal.flows.login import get_authentication_records

    if request is None or getattr(request, "session", None) is None:
        return ""
    first, second = None, None
    for item in get_authentication_records(request, user):
        if not isinstance(item, dict) or item.get("reauthenticated"):
            continue
        method = item.get("method")
        if method in _FIRST:
            first, second = _FIRST[method], None
        elif method == "mfa" and not item.get("passwordless"):
            if first is not None:
                second = _SECOND.get(item.get("type"))
        else:
            # Another way in (a password reset link that signs in, or a passkey on its own, where
            # the deployment turns them on): what it proved is not one of these, so nothing
            # earlier describes it, and a second factor after it is not counted either.
            first, second = None, None
    if first is None:
        return ""
    value = first if second is None else f"{first}_{second}"
    return value if value in SignInFactor.values else ""


def account_named(credentials):
    """The one account an attempt's username or email address names, in any letter case, as the
    sign-in log matches it; None when none or more than one match."""
    from .signin_log import accounts_named

    users = accounts_named(credentials)
    return users[0] if len(users) == 1 else None


def record(user, kind, request, *, factor="", ending=""):
    """Write one entry for `user` (an account or its id), with the browser and address the request
    reported. A refusal by the failed sign-in limit is left out when one for the same account,
    step and address was written in the last LOCKOUT_EVERY seconds, decided under the account's
    lockout lock (SIGN_IN_LOCKOUTS), so that two at the same moment take turns."""
    from .sessions import reported_by

    agent, address = reported_by(request) if request is not None else ("", None)
    user_id = getattr(user, "pk", user)
    if kind != Kind.LOCKED_OUT:
        SignInEvent.objects.create(
            user_id=user_id,
            kind=kind,
            factor=factor,
            ending=ending,
            user_agent=agent,
            address=address,
        )
        return
    from .acting import INT4

    table = connection.ops.quote_name(SignInEvent._meta.db_table)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [SIGN_IN_LOCKOUTS, user_id % INT4])
        # A new statement once the lock is held, so that one a turn before wrote counts.
        cursor.execute(
            f"INSERT INTO {table} "
            f"(user_id, occurred_at, kind, factor, ending, user_agent, address) "
            f"SELECT %s, statement_timestamp(), %s, %s, %s, %s, %s::inet "
            f"WHERE NOT EXISTS (SELECT 1 FROM {table} "
            f"WHERE user_id = %s AND kind = %s AND factor = %s "
            f"AND address IS NOT DISTINCT FROM %s::inet "
            f"AND occurred_at > statement_timestamp() - make_interval(secs => %s))",
            [
                user_id,
                kind,
                factor,
                ending,
                agent,
                address,
                user_id,
                kind,
                factor,
                address,
                LOCKOUT_EVERY,
            ],
        )


@dataclass(frozen=True)
class Entry:
    id: int
    occurred_at: object
    kind: str
    event: str
    details: str
    browser: str | None
    address: str | None
    # A sign-in of the session the page is read from.
    this_session: bool
    # A failure, a refusal or too many attempts: shown with a warning.
    attention: bool


@dataclass(frozen=True)
class Reading:
    entries: list
    # A shared demonstration account, as the account stood when its entries were read.
    locked: bool


ATTENTION = frozenset({Kind.FAILED, Kind.LOCKED_OUT, Kind.REAUTHENTICATION_FAILED, Kind.REFUSED})


def _details(event):
    if event.kind in (Kind.FAILED, Kind.REAUTHENTICATION_FAILED):
        return NOT_ACCEPTED.get(event.factor, NOT_RECORDED)
    if event.kind == Kind.LOCKED_OUT:
        return REFUSED_BEFORE.get(event.factor, NOT_RECORDED)
    if event.kind in (Kind.SIGNED_OUT, Kind.REFUSED):
        if not event.ending:
            return NOT_RECORDED
        return ENDING_DETAILS.get(event.ending, event.get_ending_display())
    return event.get_factor_display() if event.factor else NOT_RECORDED


def recent(user, *, current=None):
    """The account's entries of the last RECENT_DAYS by the database's clock, newest first, at
    most RECENT_LIMIT, as Entry rows, and whether the account is a shared demonstration account,
    both read in one statement (a Reading). `current` is the id of the session the page is read
    from, whose sign-in is marked. For a shared demonstration account, the browser and address of
    every entry of another session, or of none, are left out (None): they may be another
    visitor's."""
    from .models import User
    from .session_list import browser_label

    current = str(current) if current else None
    users = connection.ops.quote_name(User._meta.db_table)
    events = connection.ops.quote_name(SignInEvent._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT u.credentials_locked, e.id, e.occurred_at, e.kind, e.factor, e.ending, "
            f"e.session, e.user_agent, e.address FROM {users} u LEFT JOIN LATERAL "
            f"(SELECT * FROM {events} WHERE user_id = u.id "
            f"AND occurred_at > statement_timestamp() - make_interval(days => %s) "
            f"ORDER BY occurred_at DESC, id DESC LIMIT %s) e ON true WHERE u.id = %s "
            f"ORDER BY e.occurred_at DESC, e.id DESC",
            [RECENT_DAYS, RECENT_LIMIT, user.pk],
        )
        rows = cursor.fetchall()
    locked = bool(rows and rows[0][0])
    entries = []
    for _locked, pk, occurred_at, kind, factor, ending, session, agent, address in rows:
        if pk is None:
            continue
        event = SignInEvent(pk=pk, kind=kind, factor=factor, ending=ending)
        ours = current is not None and session is not None and str(session) == current
        shown = ours or not locked
        entries.append(
            Entry(
                id=pk,
                occurred_at=occurred_at,
                kind=kind,
                event=event.get_kind_display(),
                details=_details(event),
                browser=browser_label(agent) if shown else None,
                address=_address_text(address) if shown else None,
                this_session=ours and kind == Kind.SIGNED_IN,
                attention=kind in ATTENTION,
            )
        )
    return Reading(entries, locked)


def _address_text(address):
    if address is None:
        return None
    try:
        return ipaddress.ip_address(str(address).split("/", 1)[0]).compressed
    except ValueError:
        return str(address)


# For the audit review workspace (audit_review.sources), which lists every account's entries.
event_details = _details
address_text = _address_text
