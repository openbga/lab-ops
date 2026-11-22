"""What a person was typing, kept when their session ends because it passed a limit.

A page drawn for a signed-in session marks each form that keeps typing ({% form_page keep=... %},
rendered by accounts/templatetags/kept.py) with a signed token naming the account, the session
row, the form and the fields that may be kept: the visible fields a person types in or chooses
from, never a file, a hidden value (a revision, a definition token), a password, a field fetched
as one types or a field that holds patient information. The token is no secret of the account:
it names a session row, not the browser's session, and proves only that the server drew that
form for that account and session.

Kept. Once the page learns that its session has ended, before it goes to the sign-in page
(static/js/session.js), it sends what is typed in those forms with their tokens to the keep
address (accounts.session_views.keep_typing), which needs no signed-in session: the browser's
session has ended by then. A form sent after its session ended is kept from what it posted
(keep_posted, from accounts.sessions._refusal). A copy is kept only when the token's session
ended because it passed a limit (LIMIT_ENDINGS) at most KEPT_FOR ago, the
account is active, it has not signed out with Sign out since that session ended, and some field
that is typed in holds text. The first copy for a session and form stands; later ones for the
same session and form change nothing.

In this browser only. A copy records the digest of a random value the browser holds in a cookie
of its own (BROWSER_COOKIE: HttpOnly, nothing typed in it), set or renewed by the answer that kept
the copy. It is given back only to a request carrying that cookie, so an account several people
share (the public demonstration's) never gives one person's typing to another, and a copy never
follows the account to another browser.

Given back. The same form, drawn for the same account in the same browser on a page opened in the
window with GET within KEPT_FOR of the session's end, carries the newest copy not yet given back
and the time its session ended, and the page puts the values back (static/js/kept-typing.js) and
says so. Only the names the form keeps as drawn now are given back, so a field this person may no
longer fill stays empty. Every copy for that form in that browser is taken when the page is
drawn: emptied, and kept as a marker until it is removed, once the page's reading ends
(core.concurrency.after_reading).

Removed. All of an account's copies at Sign out (discard_all); the rest by the scheduled job
(remove_expired) once KEPT_FOR has passed since their session ended. Copies are never logged,
audited, listed or shown to anyone but the account that typed them.
"""

import hashlib
import json
import secrets
import uuid
from datetime import timedelta

from django import forms
from django.conf import settings
from django.core import signing
from django.core.exceptions import BadRequest, SuspiciousOperation
from django.db import connection, transaction
from django.db.models.functions import Now
from django.http.multipartparser import MultiPartParserError

from .acting import INT4
from .models import AccountSession, KeptDraft, User

Ending = AccountSession.Ending
# How long after its session ended a copy can be kept and given back.
KEPT_FOR = timedelta(hours=24)
# Ended because it passed a limit: no activity, the longest session, or a change from the session
# that found it past one (accounts.sessions.SessionLimitMiddleware.process_exception).
LIMIT_ENDINGS = (Ending.INACTIVITY, Ending.MAXIMUM, Ending.CHANGE_REFUSED)
# A page stays open at most until its session reaches the longest limit (24 hours at most), and
# what it holds can be kept up to KEPT_FOR after that: an older token is refused all the same.
TOKEN_AGE = timedelta(hours=24) + KEPT_FOR
SALT = "accounts.kept"
# The hidden input that carries the token in the form, so that a form posted after its session
# ended can be kept from what it posted.
FIELD = "_vr_kept"
# What one keep request and one copy may hold. A copy that does not fit is not kept at all, so a
# person is never given back part of what they typed as if it were all of it.
MAX_FORMS = 4
MAX_FIELDS = 40
MAX_VALUE = 50_000  # characters in one field
MAX_TOTAL = 100_000  # characters in one copy
MAX_KEY = KeptDraft._meta.get_field("form").max_length
# The advisory lock namespace (PostgreSQL's two-key form; see accounts.acting and
# accounts.session_list for the others): the second key is the account. Keeping takes it shared
# and Sign out exclusively, so a copy is never kept after the Sign out that removed the others.
KEPT_LOCK = 19560
# Widgets whose value is what the person typed or chose on the page. Exact classes: a subclass
# (a list fetched as one types, a widget of several values) is not kept.
TYPED_WIDGETS = (
    forms.TextInput,
    forms.Textarea,
    forms.EmailInput,
    forms.URLInput,
    forms.NumberInput,
    forms.DateInput,
    forms.TimeInput,
    forms.DateTimeInput,
)
CHOSEN_WIDGETS = (forms.Select, forms.RadioSelect, forms.CheckboxInput)

# The browser's own cookie (see the module): a random value, never anything typed.
BROWSER_COOKIE = "lab_ops_kept"
BROWSER_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

KEPT_MESSAGE = (
    "What you were typing is kept for 24 hours. Sign in again and open the same page to get it "
    "back."
)


def _browser(request, *, create):
    """(value, digest) of the browser's keep cookie; a new value when it carries none (or one not
    of this kind) and `create`, else None."""
    value = request.COOKIES.get(BROWSER_COOKIE, "")
    if not (isinstance(value, str) and 40 <= len(value) <= 64 and set(value) <= BROWSER_ALPHABET):
        if not create:
            return None
        value = secrets.token_urlsafe(32)
    return value, hashlib.sha256(value.encode()).hexdigest()


def remember_browser(request, response):
    """Set or renew the browser's keep cookie on the answer to a request that kept a copy."""
    value = getattr(request, "vr_kept_browser", None)
    if value:
        response.set_cookie(
            BROWSER_COOKIE,
            value,
            max_age=int(KEPT_FOR.total_seconds()),
            secure=settings.SESSION_COOKIE_SECURE,
            httponly=True,
            samesite="Lax",
        )


def keep_for(request, value, values):
    """keep() for this request's browser: on success the answer sets its cookie
    (remember_browser)."""
    browser, digest = _browser(request, create=True)
    held = keep(value, values, digest)
    if held:
        request.vr_kept_browser = browser
    return held


def keepable(form):
    """(names, typed): the html names of the form's fields whose values may be kept, in the
    form's order, and those of them a person types in. Never a disabled field or one whose name
    the form lists in `patient_information_fields`."""
    patient = set(getattr(form, "patient_information_fields", ()) or ())
    names, typed = [], []
    for bound in form.visible_fields():
        field, widget = bound.field, type(bound.field.widget)
        if field.disabled or bound.name in patient or bound.html_name in patient:
            continue
        if widget in TYPED_WIDGETS:
            typed.append(bound.html_name)
        elif widget not in CHOSEN_WIDGETS:
            continue
        names.append(bound.html_name)
    return names, typed


def token(request, key, names, typed):
    """The form's token for a page drawn for a signed-in session, or "" when there is nothing to
    keep or no session row (a page drawn for a visitor)."""
    state = getattr(request, "account_session", None)
    user = getattr(request, "user", None)
    if state is None or user is None or not user.is_authenticated or not typed or not key:
        return ""
    data = {"u": user.pk, "s": str(state.id), "k": str(key)[:MAX_KEY], "f": names, "t": typed}
    return signing.dumps(data, salt=SALT, compress=True)


def _read(value):
    """The token's contents, checked, or None."""
    if not isinstance(value, str) or not value or len(value) > 8000:
        return None
    try:
        data = signing.loads(value, salt=SALT, max_age=TOKEN_AGE)
    except signing.BadSignature, ValueError, TypeError:
        return None
    if not isinstance(data, dict):
        return None
    account, session, key = data.get("u"), data.get("s"), data.get("k")
    names, typed = data.get("f"), data.get("t")
    if not isinstance(account, int) or not isinstance(key, str) or not key:
        return None
    try:
        session = str(uuid.UUID(str(session)))
    except ValueError, TypeError, AttributeError:
        return None
    if not (isinstance(names, list) and isinstance(typed, list)):
        return None
    if not all(isinstance(name, str) for name in names + typed) or not set(typed) <= set(names):
        return None
    return {"u": account, "s": session, "k": key, "f": names[:MAX_FIELDS], "t": typed}


def _clean(values, names, typed):
    """The values to keep, by name (strings only, in the token's order), or None when there is
    nothing typed to keep or the copy is too large."""
    if not isinstance(values, dict):
        return None
    out, total = {}, 0
    for name in names:
        value = values.get(name, "")
        if not isinstance(value, str):
            return None
        value = value.replace("\x00", "")  # PostgreSQL's JSON refuses it
        if len(value) > MAX_VALUE:
            return None
        total += len(value)
        out[name] = value
    if total > MAX_TOTAL or not any(out.get(name, "").strip() for name in typed):
        return None
    return out


def _lock(account_id, *, shared):
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {function}(%s, %s)", [KEPT_LOCK, account_id % INT4])


def keep(value, values, browser):
    """Keep a copy of `values` for the form and session the token names, for the browser whose
    cookie has the digest `browser`, if that session ended because it passed a limit (see the
    module). True when a copy for that session and form is held for that browser after this (kept
    now, or by an earlier request), False otherwise."""
    data = _read(value)
    if data is None or not browser:
        return False
    cleaned = _clean(values, data["f"], data["t"])
    if cleaned is None:
        return False
    table = connection.ops.quote_name(KeptDraft._meta.db_table)
    sessions = connection.ops.quote_name(AccountSession._meta.db_table)
    users = connection.ops.quote_name(User._meta.db_table)
    with transaction.atomic(), connection.cursor() as cursor:
        _lock(data["u"], shared=True)
        # One statement decides and inserts: the session's ending and its time never change once
        # recorded; the account's state and a later Sign out are read as they stand, under the
        # lock Sign out takes before it removes copies.
        cursor.execute(
            f'INSERT INTO {table} (session_id, user_id, form, browser, "values", kept_at) '
            f"SELECT s.id, s.user_id, %s, %s, %s::jsonb, now() FROM {sessions} s "
            f"JOIN {users} u ON u.id = s.user_id "
            f"WHERE s.id = %s AND s.user_id = %s AND u.is_active "
            f"AND s.ending = ANY(%s) AND s.ended_at > now() - %s "
            f"AND NOT EXISTS (SELECT 1 FROM {sessions} o WHERE o.user_id = s.user_id "
            f"AND o.ending = %s AND o.ended_at >= s.ended_at) "
            f"ON CONFLICT (session_id, form) DO NOTHING RETURNING id",
            [
                data["k"],
                browser,
                json.dumps(cleaned),
                data["s"],
                data["u"],
                list(LIMIT_ENDINGS),
                KEPT_FOR,
                Ending.SIGNED_OUT,
            ],
        )
        if cursor.fetchone() is not None:
            return True
        # Not inserted: refused, or a copy for this session and form exists (an earlier request
        # of the same page, whose answer was lost). Held only while it has not been given back.
        return KeptDraft.objects.filter(
            session_id=data["s"],
            user_id=data["u"],
            form=data["k"],
            browser=browser,
            taken_at__isnull=True,
            session__ended_at__gt=Now() - KEPT_FOR,
        ).exists()


def keep_posted(request):
    """Keep what a form posted after its session ended, when it carries a token (FIELD). Returns
    whether a copy is held; never raises for a body that cannot be read."""
    if request.method != "POST":
        return False
    try:
        value = request.POST.get(FIELD)
        data = _read(value)
        if data is None:
            return False
        values = {name: request.POST.get(name, "") for name in data["f"]}
    except BadRequest, SuspiciousOperation, MultiPartParserError, OSError, ValueError:
        return False
    return keep_for(request, value, values)


def offer(request, key, names):
    """For a form drawn on a page opened in the window with GET by a signed-in session: (values,
    ended_at) of the newest copy of this account's for the form, kept in this browser and not yet
    given back, within KEPT_FOR of its session's end, holding only `names`; or None. Every such
    copy is taken once the page's reading ends, so a copy is given back once."""
    from core.concurrency import after_reading

    from .sessions import _is_navigation

    user = getattr(request, "user", None)
    state = getattr(request, "account_session", None)
    if request.method != "GET" or state is None or user is None or not user.is_authenticated:
        return None
    if not _is_navigation(request) or "prefetch" in request.headers.get("Sec-Purpose", ""):
        return None
    browser = _browser(request, create=False)
    if browser is None:
        return None
    rows = list(
        KeptDraft.objects.filter(
            user=user,
            form=key,
            browser=browser[1],
            taken_at__isnull=True,
            session__ending__in=LIMIT_ENDINGS,
            session__ended_at__gt=Now() - KEPT_FOR,
        )
        .exclude(session_id=state.id)
        .order_by("-session__ended_at", "-id")
        .values_list("id", "values", "session__ended_at")
    )
    if not rows:
        return None
    ids = [row[0] for row in rows]

    def take():
        KeptDraft.objects.filter(id__in=ids, taken_at__isnull=True).update(
            values=None, taken_at=Now()
        )

    after_reading(take)
    _, values, ended_at = rows[0]
    allowed = set(names)
    given = {name: value for name, value in (values or {}).items() if name in allowed}
    if not any(isinstance(value, str) and value.strip() for value in given.values()):
        return None
    return given, ended_at


def discard_all(user):
    """Remove every copy of the account's (Sign out), after waiting for a copy being kept."""
    with transaction.atomic():
        _lock(user.pk, shared=False)
        KeptDraft.objects.filter(user_id=user.pk).delete()


def remove_expired(now=None):
    """The scheduled job: remove the copies whose session ended KEPT_FOR ago or more."""
    from django.utils import timezone

    now = now or timezone.now()
    removed, _ = KeptDraft.objects.filter(session__ended_at__lte=now - KEPT_FOR).delete()
    return {"kept_removed": removed}
