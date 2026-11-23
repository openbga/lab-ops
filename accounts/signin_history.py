"""Each account's sign-in history, kept in the database (SignInEvent rows), and the page where a
person reads their own.

What is recorded, for the account it concerns:

- a sign-in (the session starts), with what it proved, and a sign-out (the session ends), with
  why: written by the database itself when a session row starts or ends (accounts migration
  0009), with the browser and address the session reported at sign-in;
- a sign-in refused as it starts: the password was right, but it was marked as compromised and
  nothing more was proved (the session starts ended, accounts.compromised), or the account is
  deactivated (no session starts);
- a failed attempt: a password, an authenticator or recovery code, or an emailed code that was
  not accepted at sign-in, and a password or code not accepted when a signed-in page asked for it
  again (re-authentication);
- a refusal after too many failed attempts (the failed sign-in limit, accounts.adapter), before
  the password or the code was checked.

Recorded only when the attempt names exactly one account (by username or email address, in any
letter case, as the sign-in log matches it, accounts.signin_log): an attempt for a name or an
address that matches no account, or more than one, names nobody and stays in the sign-in log.
Never a password, a code, or what was typed as the username or email address. The browser and the
address are what the request reported (accounts.sessions.reported_by), not proof of anything.

Not recorded: an attempt the request limit stopped before the form was read, a code entered after
its emailed sign-in expired, and a request for an emailed code.

Every account, staff or client portal, reads its own entries of the last RECENT_DAYS, the most
recent RECENT_LIMIT, on Sign-in history. A shared demonstration account (credentials_locked) is
used by other visitors too: its entries are listed, but the browser and address only for the
session the page is read from. Nobody else's history is shown to anyone in this release.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.db.models.functions import Now

from .models import AccountSession, SignInEvent, SignInFactor

Kind = SignInEvent.Kind
Ending = AccountSession.Ending
RECENT_DAYS = 90
RECENT_LIMIT = 100

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
    """What this browser's sign-in of `user` proved, from the steps allauth recorded in its
    session: the last password or emailed code, and an authenticator or recovery code entered
    after it. Blank when the steps are not known."""
    from allauth.account.internal.flows.login import get_authentication_records

    if request is None or getattr(request, "session", None) is None:
        return ""
    first, second = None, None
    for item in get_authentication_records(request, user):
        if not isinstance(item, dict):
            continue
        method = item.get("method")
        if method in _FIRST:
            first, second = _FIRST[method], None
        elif method == "mfa" and first is not None:
            second = _SECOND.get(item.get("type"))
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
    reported."""
    from .sessions import reported_by

    agent, address = reported_by(request) if request is not None else ("", None)
    SignInEvent.objects.create(
        user_id=getattr(user, "pk", user),
        kind=kind,
        factor=factor,
        ending=ending,
        user_agent=agent,
        address=address,
    )


def signed_in_account(request):
    """The account a signed-in request acts for (a re-authentication), or None."""
    user = getattr(request, "user", None) if request is not None else None
    return user if user is not None and user.is_authenticated else None


@dataclass(frozen=True)
class Entry:
    id: int
    occurred_at: object
    kind: str
    event: str
    details: str
    browser: str | None
    address: str | None
    this_session: bool
    # A failure, a refusal or too many attempts: shown with a warning.
    attention: bool


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
    most RECENT_LIMIT, as Entry rows. `current` is the id of the session the page is read from,
    which is marked. For a shared demonstration account, the browser and address of every other
    entry are left out (None): they may be another visitor's."""
    from .session_list import browser_label

    current = str(current) if current else None
    rows = (
        SignInEvent.objects.filter(
            user_id=user.pk, occurred_at__gt=Now() - timedelta(days=RECENT_DAYS)
        )
        .order_by("-occurred_at", "-id")
        .only("id", "occurred_at", "kind", "factor", "ending", "session", "user_agent", "address")[
            :RECENT_LIMIT
        ]
    )
    entries = []
    for event in rows:
        this = current is not None and event.session is not None and str(event.session) == current
        shown = this or not user.credentials_locked
        entries.append(
            Entry(
                id=event.pk,
                occurred_at=event.occurred_at,
                kind=event.kind,
                event=event.get_kind_display(),
                details=_details(event),
                browser=browser_label(event.user_agent) if shown else None,
                address=event.address if shown else None,
                this_session=this,
                attention=event.kind
                in (Kind.FAILED, Kind.LOCKED_OUT, Kind.REAUTHENTICATION_FAILED, Kind.REFUSED),
            )
        )
    return entries
