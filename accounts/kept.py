"""What a person was typing, kept when their session ends because it passed a limit.

A full-page form drawn with {% form_page keep=... %} (core.templatetags.ui.form_page_kept, which
calls prepare before the fields are drawn; includes/kept_typing.html) carries a signed token
naming the account, the session row, the form and the fields that may be kept: the visible fields
a person types in or chooses from (lists of choices and the quantity boxes of an item list
included), never a file, a hidden value (a revision, a definition token), a password or a field
that holds patient information, and a list fetched as one types only where the form names it in
`kept_fetched` (its chosen record, by id, given back only while the field still offers it). A
form with more than MAX_FIELDS such fields gets no token. Several forms drawn as one HTML form
(Parts) keep as one; a page whose forms depend on what is given back takes the copy first (take)
and draws its forms from it (draw) before the marker is worked out (prepare with taken=). The
token is no secret of the account: it names a session row, not the browser's session, and proves
only that the server drew that form for that account and session.

Kept. Once the page learns that its session has ended, before it goes to the sign-in page
(static/js/session.js), it sends what is typed in those forms with their tokens to the keep
address (accounts.session_views.keep_typing), which needs no signed-in session: the browser's
session has ended by then. A form sent after its session ended is kept from what it posted
(keep_posted): when the middleware refuses it (accounts.sessions._refusal), when a view sends it
to sign in, and when its CSRF check fails because the person signed in again in another tab
(core.views.csrf_failure). A copy is kept only when the token's session ended because it passed a
limit (LIMIT_ENDINGS) less than KEPT_FOR before the decision, the account is active, it has not
signed out with Sign out since that session ended, no copy for that form already given back in
that browser came from a session that ended later, and some field that is typed in holds text.
Fields of a request form that hold patient information as the form stands at that moment are
dropped first (`recheck`), whatever the page was drawn with. The first copy for a session and form
stands; later ones for the same session and form change nothing, and are answered as held only
while that copy may still be given back.

In this browser only. A copy records the digest of a value the browser holds in a cookie of its
own (BROWSER_COOKIE: HttpOnly, nothing typed in it), set or renewed with every page that draws a
form that keeps typing and with every answer that held a copy. A page drawn for a browser without
one gets a value derived from its signed-in session row (a keyed hash: unguessable without the
server's secret), so every answer of that session sets the same value whatever order they arrive
in. A
copy is given back only to a request carrying that cookie, so an account several people share
(the public demonstration's) never gives one person's typing to another, and a copy never follows
the account to another browser.

Given back. The same form, drawn for the same account in the same browser on a page opened in the
window with GET (not a prefetch) within KEPT_FOR of the session's end, is drawn with the newest
eligible copy (_eligible) as its initial values, before its fields are drawn, so nothing depends
on scripts; the page says when its session ended, and when something could not be put back (a
value of the wrong shape for the field as it stands now, a choice no longer offered, a field no
longer on the form; or all of it). The copies for that form are taken as they are read: locked
(FOR UPDATE SKIP LOCKED) and emptied in a savepoint of the page's reading. A page finding the
newest held by another page, or taken by one that committed after its reading began, gives
nothing back, never an older copy; a page that fails to draw rolls the take back, so the forms
that keep typing are drawn inside a reading (core.concurrency.one_reading). An emptied copy stays
as a marker until it is removed. Discard opens the page again.

Removed. All of an account's copies at Sign out, and also when Sign out (its CSRF check passed)
arrives just after the session passed a limit (discard_all), which records the moment so that a
copy sent later from a session that ended before it is refused; the rest by the scheduled job
(remove_expired) once KEPT_FOR has passed since their session ended. Copies are never logged,
audited, listed or shown to anyone but the account that typed them.
"""

import base64
import datetime
import hashlib
import json
import re
import secrets
import uuid
from datetime import timedelta
from typing import NamedTuple

from django import forms
from django.conf import settings
from django.core import signing
from django.core.exceptions import BadRequest, SuspiciousOperation
from django.db import OperationalError, connection, transaction
from django.db.models import Exists, OuterRef
from django.db.models.functions import Now
from django.http.multipartparser import MultiPartParserError
from django.utils.crypto import salted_hmac
from django.utils.module_loading import import_string

from core.lookups import RemoteSelect

from .acting import INT4
from .models import AccountSession, KeptDraft, KeptDraftCutoff, User

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
# person is never given back part of what they typed as if it were all of it; a form with more
# fields than MAX_FIELDS is not marked at all.
MAX_FORMS = 4
MAX_FIELDS = 200
MAX_CHOICES = 100  # values of one field of several values
MAX_VALUE = 50_000  # characters in one field
MAX_TOTAL = 100_000  # characters in one copy
MAX_KEY = KeptDraft._meta.get_field("form").max_length
# The advisory lock namespace (PostgreSQL's two-key form; see accounts.acting and
# accounts.session_list for the others): the second key is the account. Keeping takes it shared
# and Sign out exclusively, so a copy is never kept after the Sign out that removed the others.
KEPT_LOCK = 19560
# Widgets whose value is what the person typed or chose on the page. Exact classes: a subclass
# (a list fetched as one types) is not kept.
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
SEVERAL_WIDGETS = (forms.CheckboxSelectMultiple,)  # a list of values
# What a form names to have its own fields read again when a copy is kept (its `kept_recheck`,
# (name, id) or (name, id, chooser)): (the function, the prefix of the names it decides). The
# function, given the id and the account, returns the names with that prefix that the form would
# draw for the account as it stands now (none that hold patient information), holding what a change
# of them locks first so that one under way finishes before; a name with the prefix that it does not
# return is dropped. Its third element names the form's own fields outside the prefix (a box of
# the form's), decided by the function in the same way. `chooser` is the html name of the field
# that chose that form on a larger page (the desk's ticket form picker): when the copy says another
# was chosen, the form's fields belong to the form drawn before the choice, and when the form is
# gone the rest of the page still stands, so in both cases every field of the form's is dropped
# and the rest kept. Without a chooser the page is the form's own, and a copy of a form that is
# gone is not kept at all.
RECHECKS = {
    "request_form": (
        "request_forms.selectors.kept_eligible_fields",
        "field-",
        ("details_not_known",),
    )
}
# The kinds of control a value can come from: a value goes back only into a control of the same
# kind (or between one choice and several, one answer), so a field that changed kind since never
# shows an answer given to another question (a "no" as a ticked confirmation).
FAMILIES = {
    forms.TextInput: "text",
    forms.Textarea: "text",
    forms.EmailInput: "text",
    forms.URLInput: "text",
    forms.NumberInput: "number",
    forms.DateInput: "date",
    forms.DateTimeInput: "datetime",
    forms.TimeInput: "time",
    forms.Select: "choice",
    forms.RadioSelect: "choice",
    forms.CheckboxSelectMultiple: "choices",
    forms.CheckboxInput: "box",
}
# Requests a browser makes without the person opening the page.
PREFETCH_HEADERS = ("Sec-Purpose", "Purpose", "X-Purpose", "X-Moz")

# The browser's own cookie (see the module): a random value, never anything typed.
BROWSER_COOKIE = "lab_ops_kept"
BROWSER_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


class Parts:
    """Several forms drawn as one HTML form (the desk's new ticket: the ticket's own fields, the
    ticket form picker and the chosen ticket form's fields), kept as one: their fields in order,
    each drawn back into its own form. Their html names must differ. Parts that are None (not
    drawn) are left out."""

    def __init__(self, *forms):
        self.forms = [form for form in forms if form is not None]

    def visible_fields(self):
        return [bound for form in self.forms for bound in form.visible_fields()]

    @property
    def is_bound(self):
        return any(form.is_bound for form in self.forms)

    @property
    def patient_information_fields(self):
        return [
            name
            for form in self.forms
            for name in (getattr(form, "patient_information_fields", ()) or ())
        ]

    @property
    def kept_recheck(self):
        return next(
            (form.kept_recheck for form in self.forms if getattr(form, "kept_recheck", None)),
            None,
        )


def _fetched(bound):
    """Whether `bound` is a list fetched as one types (one record) that its form names in
    `kept_fetched`: kept as the chosen record's id."""
    widget = bound.field.widget
    return (
        type(widget) is RemoteSelect
        and not widget.allow_multiple_selected
        and bound.name in (getattr(bound.form, "kept_fetched", ()) or ())
    )


KEPT_MESSAGE = (
    "What you were typing is kept for 24 hours. Sign in again and open the same page to get it "
    "back."
)


def _typed(value):
    """A value as the person typed or chose it, for comparing two deliveries of one form: line
    endings as one, a list in any order."""
    if isinstance(value, list):
        return sorted(item.replace("\r\n", "\n") for item in value)
    return value.replace("\r\n", "\n") if isinstance(value, str) else value


def _same_copy(stored, sent):
    """Whether a stored copy, (values, controls, dropped), gives back exactly what `sent` would:
    the same answers (empty ones aside) from the same kinds of control, and the same omission."""

    def answers(values, controls):
        return {
            name: (_typed(value), controls.get(name))
            for name, value in (values or {}).items()
            if _filled(value)
        }

    (values, controls, dropped), (new_values, new_controls, new_dropped) = stored, sent
    return bool(dropped) == bool(new_dropped) and answers(values, controls or {}) == answers(
        new_values, new_controls or {}
    )


def notify(request):
    """Tell the sign-in page that what was typed is kept, once: by the request that stored a copy
    (a refused post and the page's keep request can both hold one), and not when the notice
    already waits. What waits is looked at without using it up or changing it (the storage's
    loaded and queued messages); a request without message storage says nothing. Not covered
    (docs/architecture.md): a copy of another form stored after the sign-in page has shown the
    notice is announced again, and a repeated delivery whose first answer was lost is not."""
    from django.contrib import messages

    storage = getattr(request, "_messages", None)
    if storage is None or not getattr(request, "vr_kept_announce", False):
        return
    waiting = [
        *(getattr(storage, "_loaded_messages", None) or []),  # loads what waits, uses nothing up
        *(getattr(storage, "_queued_messages", None) or []),
    ]
    if not any(getattr(message, "message", None) == KEPT_MESSAGE for message in waiting):
        messages.info(request, KEPT_MESSAGE, fail_silently=True)


def _valid_browser(value):
    return isinstance(value, str) and 40 <= len(value) <= 64 and set(value) <= BROWSER_ALPHABET


def _browser(request, *, create):
    """(value, digest) of this browser's keep cookie: the value already chosen for this request,
    else the cookie it carries; when it carries none (or one not of this kind) and `create`, a
    value derived from this request's signed-in session row, so that every answer of one session
    to a browser without the cookie sets the same value whatever order they arrive in (a request
    without a session, such as the keep address, gets a random one: a token never leads to a
    browser's value); else None."""
    value = getattr(request, "vr_kept_choice", None) or request.COOKIES.get(BROWSER_COOKIE, "")
    if not _valid_browser(value):
        if not create:
            return None
        state = getattr(request, "account_session", None)
        if state is not None:
            digest = salted_hmac("accounts.kept.browser", str(state.id), algorithm="sha256")
            value = base64.urlsafe_b64encode(digest.digest()).decode().rstrip("=")
        else:
            value = secrets.token_urlsafe(32)
    request.vr_kept_choice = value
    return value, hashlib.sha256(value.encode()).hexdigest()


def ensure_browser(request):
    """For a page that draws a form keeping typing: the browser's cookie, created now if missing,
    set (renewed) on the page's answer, so that every tab keeps under one value."""
    request.vr_kept_browser = _browser(request, create=True)[0]


def remember_browser(request, response):
    """Set or renew the browser's keep cookie on the answer to a request that chose its value
    (accounts.sessions.SessionLimitMiddleware calls this for every answer)."""
    value = getattr(request, "vr_kept_browser", None)
    if value:
        response.set_cookie(
            BROWSER_COOKIE,
            value,
            # As long as a page's token: a page's typing can be kept with the cookie it set.
            max_age=int(TOKEN_AGE.total_seconds()),
            secure=settings.SESSION_COOKIE_SECURE,
            httponly=True,
            samesite="Lax",
        )


def keep_for(request, value, values):
    """keep() for this request's browser (one cookie value for the whole request), set or renewed
    on the answer only when a copy is held."""
    choice, digest = _browser(request, create=True)
    held, own, inserted = _keep(value, values, digest)
    if held:
        request.vr_kept_browser = choice
    # Whether what this request sent is the copy held (stored now, or the same text stored by an
    # earlier delivery), not one kept before for the session and form from other text (another
    # tab): the guards of the page that sent it rely on it (accounts.sessions._refusal, the keep
    # address's `held`). Whether this request stored it: the sign-in page's notice.
    request.vr_kept_own = own
    request.vr_kept_announce = getattr(request, "vr_kept_announce", False) or inserted
    return held


def keepable(form):
    """(names, typed, several): the html names of the form's fields whose values may be kept, in
    the form's order (the quantity boxes of an item list each by its own name), those of them a
    person types in, and those that hold a list of values. Never a disabled field or one whose
    name the form lists in `patient_information_fields`."""
    patient = set(getattr(form, "patient_information_fields", ()) or ())
    names, typed, several = [], [], []
    for bound in form.visible_fields():
        field, widget = bound.field, bound.field.widget
        kind, name = type(widget), bound.html_name
        if field.disabled or bound.name in patient or name in patient:
            continue
        if _fetched(bound):
            pass  # its chosen record, by id
        elif kind in TYPED_WIDGETS:
            typed.append(name)
        elif kind in SEVERAL_WIDGETS:
            several.append(name)
        elif isinstance(widget, forms.MultiWidget) and all(
            type(part) in TYPED_WIDGETS for part in widget.widgets
        ):
            parts = [name + suffix for suffix in widget.widgets_names]
            names.extend(parts)
            typed.extend(parts)
            continue
        elif kind not in CHOSEN_WIDGETS:
            continue
        names.append(name)
    return names, typed, several


def controls_of(form):
    """Field name -> the kind of control (FAMILIES) of each name keepable() gives."""
    out = {}
    for bound in form.visible_fields():
        widget = bound.field.widget
        if _fetched(bound):
            out[bound.html_name] = "choice"
        elif isinstance(widget, forms.MultiWidget) and type(widget) not in FAMILIES:
            for suffix, part in zip(widget.widgets_names, widget.widgets, strict=True):
                if type(part) in FAMILIES:
                    out[bound.html_name + suffix] = FAMILIES[type(part)]
        elif type(widget) in FAMILIES:
            out[bound.html_name] = FAMILIES[type(widget)]
    return out


def token(request, key, form):
    """The form's token for a page drawn for a signed-in session, or "" when there is nothing to
    keep, too much, or no session row (a page drawn for a visitor)."""
    names, typed, several = keepable(form)
    state = getattr(request, "account_session", None)
    user = getattr(request, "user", None)
    if state is None or user is None or not user.is_authenticated or not typed or not key:
        return ""
    if len(names) > MAX_FIELDS:
        return ""
    families = controls_of(form)
    data = {
        "u": user.pk,
        "s": str(state.id),
        "k": str(key)[:MAX_KEY],
        "f": names,
        "t": typed,
        "w": {name: families[name] for name in names if name in families},
    }
    if several:
        data["m"] = several
    recheck = getattr(form, "kept_recheck", None)
    if recheck:
        data["r"] = list(recheck)
    return signing.dumps(data, salt=SALT, compress=True)


def _strings(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _read(value):
    """The token's contents, checked, or None."""
    if not isinstance(value, str) or not value or len(value) > 16000:
        return None
    try:
        data = signing.loads(value, salt=SALT, max_age=TOKEN_AGE)
    except signing.BadSignature, ValueError, TypeError:
        return None
    if not isinstance(data, dict):
        return None
    account, session, key = data.get("u"), data.get("s"), data.get("k")
    names, typed, several = data.get("f"), data.get("t"), data.get("m", [])
    recheck, families = data.get("r"), data.get("w", {})
    if not isinstance(account, int) or not isinstance(key, str) or not key:
        return None
    try:
        session = str(uuid.UUID(str(session)))
    except ValueError, TypeError, AttributeError:
        return None
    if not (_strings(names) and _strings(typed) and _strings(several)) or len(names) > MAX_FIELDS:
        return None
    if not (set(typed) | set(several)) <= set(names):
        return None
    if not isinstance(families, dict) or not all(
        isinstance(value, str) and name in names for name, value in families.items()
    ):
        return None
    if recheck is not None and not (
        isinstance(recheck, list)
        and len(recheck) in (2, 3)
        and recheck[0] in RECHECKS
        and isinstance(recheck[1], int)
        and (len(recheck) == 2 or (isinstance(recheck[2], str) and recheck[2]))
    ):
        return None
    return {"u": account, "s": session, "k": key, "f": names, "t": typed, "m": several,
            "r": recheck, "w": families}  # fmt: skip


def _clean(values, names, typed, several, *, sent_typed=None):
    """The values to keep, by name in the token's order (a string, or a list of strings for a
    field of several values), or None when nothing was typed or the copy is too large. What was
    typed is looked for in `sent_typed` (the names the page drew) when given, so a copy whose only
    text was in a field left out since is still kept, with that left out."""
    if not isinstance(values, dict):
        return None
    out, total = {}, 0
    for name in names:
        if name in several:
            value = values.get(name, [])
            if not _strings(value) or len(value) > MAX_CHOICES:
                return None
            value = [item.replace("\x00", "") for item in value]
            size = sum(len(item) for item in value)
        else:
            value = values.get(name, "")
            if not isinstance(value, str):
                return None
            value = value.replace("\x00", "")  # PostgreSQL's JSON refuses it
            size = len(value)
        if size > MAX_VALUE:
            return None
        total += size
        out[name] = value
    if total > MAX_TOTAL:
        return None
    looked = typed if sent_typed is None else sent_typed
    if not any(isinstance(values.get(name), str) and values[name].strip() for name in looked):
        return None
    return out


def _lock(account_id, *, shared):
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {function}(%s, %s)", [KEPT_LOCK, account_id % INT4])


def _without_patient_fields(data, values=None):
    """The token's names less those of the form's own fields that may not be kept as the form
    stands now (its recheck): fields that hold patient information, removed, or changed so that a
    drawn name (an item list's old quantity box) no longer exists. With a chooser (the form is part
    of a larger page), all of the form's fields when `values` say that another form was chosen than
    the one whose fields it drew, or when the form is gone; without one, None when the form is
    gone (the page is the form's own: nothing of it can be given back)."""
    if not data["r"]:
        return data
    function, prefix, extra = RECHECKS[data["r"][0]]
    chooser = data["r"][2] if len(data["r"]) == 3 else None
    if chooser and (not isinstance(values, dict) or values.get(chooser) != str(data["r"][1])):
        eligible = None  # another form chosen since its fields were drawn
    else:
        eligible = import_string(function)(data["r"][1], data["u"])
        if eligible is None and not chooser:
            return None
    eligible = set(eligible or ())

    def allowed(name):
        if name.startswith(prefix) or name in extra:
            return name in eligible  # empty when the form does not stand
        return True

    return {
        **data,
        "f": [name for name in data["f"] if allowed(name)],
        "t": [name for name in data["t"] if allowed(name)],
        "m": [name for name in data["m"] if allowed(name)],
    }


def keep(value, values, browser):
    """Keep a copy of `values` for the form and session the token names, for the browser whose
    cookie has the digest `browser`, if that session ended because it passed a limit (see the
    module). True when a copy for that session and form is held for that browser after this (kept
    now, or by an earlier request), False otherwise."""
    return _keep(value, values, browser)[0]


def _keep(value, values, browser):
    """keep(): (held, whether the copy held is what this call sent: stored now or the same text
    stored before, whether this call stored it)."""
    data = _read(value)
    if data is None or not browser:
        return False, False, False
    table = connection.ops.quote_name(KeptDraft._meta.db_table)
    sessions = connection.ops.quote_name(AccountSession._meta.db_table)
    users = connection.ops.quote_name(User._meta.db_table)
    cutoffs = connection.ops.quote_name(KeptDraftCutoff._meta.db_table)
    with transaction.atomic(), connection.cursor() as cursor:
        _lock(data["u"], shared=True)
        held = _eligible(data["u"], data["k"], browser).filter(
            session_id=data["s"], user__is_active=True
        )
        # The form's own fields as they stand, after a change of them under way.
        checked = _without_patient_fields(data, values)
        if checked is None:
            return False, False, False  # the form is gone: nothing of it can be given back
        cleaned = _clean(values, checked["f"], checked["t"], checked["m"], sent_typed=data["t"])
        if cleaned is None:
            # Nothing to keep from this delivery; a copy kept by an earlier one may still stand.
            return held.exists(), False, False
        dropped = isinstance(values, dict) and any(
            _filled(values.get(name, "")) for name in data["f"] if name not in checked["f"]
        )
        data = checked
        controls = {name: data["w"][name] for name in cleaned if name in data["w"]}
        # One statement decides and inserts, by the clock when it runs: the session's ending and
        # its time never change once recorded; the account's state, a later Sign out (recorded as
        # an ending by any release, or as the account's discard moment) are read as they stand,
        # under the lock Sign out takes before it removes copies.
        cursor.execute(
            f'INSERT INTO {table} (session_id, user_id, form, browser, "values", controls, '
            f"dropped, kept_at) SELECT s.id, s.user_id, %s, %s, %s::jsonb, %s::jsonb, %s, "
            f"clock_timestamp() FROM {sessions} s "
            f"JOIN {users} u ON u.id = s.user_id "
            f"WHERE s.id = %s AND s.user_id = %s AND u.is_active "
            f"AND s.ending = ANY(%s) AND s.ended_at > clock_timestamp() - %s "
            f"AND NOT EXISTS (SELECT 1 FROM {sessions} o WHERE o.user_id = s.user_id "
            f"AND o.ending = %s AND o.ended_at >= s.ended_at) "
            f"AND NOT EXISTS (SELECT 1 FROM {cutoffs} c WHERE c.user_id = s.user_id "
            f"AND c.discarded_at >= s.ended_at) "
            f"AND NOT EXISTS (SELECT 1 FROM {table} t JOIN {sessions} ts ON ts.id = t.session_id "
            f"WHERE t.user_id = s.user_id AND t.form = %s AND t.browser = %s "
            f"AND t.taken_at IS NOT NULL AND ts.ended_at > s.ended_at) "
            f"ON CONFLICT (session_id, form) DO NOTHING RETURNING id",
            [
                data["k"],
                browser,
                json.dumps(cleaned),
                json.dumps(controls),
                dropped,
                data["s"],
                data["u"],
                list(LIMIT_ENDINGS),
                KEPT_FOR,
                Ending.SIGNED_OUT,
                data["k"],
                browser,
            ],
        )
        if cursor.fetchone() is not None:
            return True, True, True
        # Not inserted: refused, or a copy for this session and form exists (an earlier request
        # of the same page, whose answer was lost, or another tab). Held only while it may still
        # be given back; this delivery's own only when it holds the same text.
        stored = held.values_list("values", "controls", "dropped").first()
        if stored is None or stored[0] is None:
            return held.exists(), False, False
        mine = (cleaned, controls, dropped)
        return True, _same_copy(stored, mine), False


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
        values = {
            name: request.POST.getlist(name) if name in data["m"] else request.POST.get(name, "")
            for name in data["f"]
        }
    except BadRequest, SuspiciousOperation, MultiPartParserError, OSError, ValueError:
        return False
    return keep_for(request, value, values)


def _prefetch(request):
    return any("prefetch" in request.headers.get(name, "").lower() for name in PREFETCH_HEADERS)


def _eligible(user_id, key, digest):
    """The copies of this account for this form and browser that may still be given back: not
    given back yet, ended by a limit less than KEPT_FOR ago, with no Sign out after their session
    ended (an ending recorded by any release, or the account's discard moment) and no copy for
    the form already given back in this browser from a session that ended later."""
    ended = OuterRef("session__ended_at")
    return (
        KeptDraft.objects.filter(
            user_id=user_id,
            form=key,
            browser=digest,
            taken_at__isnull=True,
            session__ending__in=LIMIT_ENDINGS,
            session__ended_at__gt=Now() - KEPT_FOR,
        )
        .exclude(
            Exists(
                KeptDraft.objects.filter(
                    user_id=user_id,
                    form=key,
                    browser=digest,
                    taken_at__isnull=False,
                    session__ended_at__gt=ended,
                )
            )
        )
        .exclude(
            Exists(
                AccountSession.objects.filter(
                    user_id=user_id, ending=Ending.SIGNED_OUT, ended_at__gte=ended
                )
            )
        )
        .exclude(Exists(KeptDraftCutoff.objects.filter(user_id=user_id, discarded_at__gte=ended)))
    )


def _filled(value):
    return bool(value.strip()) if isinstance(value, str) else bool(value)


def _serialization_refusal(error):
    """Whether a database error is PostgreSQL refusing to lock a row changed since this
    transaction's snapshot (SQLSTATE 40001)."""
    cause = error.__cause__
    return getattr(cause, "sqlstate", None) == "40001" or getattr(cause, "pgcode", None) == "40001"


class Taken(NamedTuple):
    """A copy taken for a page (take): every value it holds, its session's end, the kind of
    control each value came from, and whether something was dropped when it was kept."""

    values: dict
    ended_at: datetime.datetime
    controls: dict
    dropped: bool


def offer(request, key, names):
    """For a form drawn on a page opened in the window with GET by a signed-in session: (values,
    ended_at, dropped) of the newest eligible copy of this account's for the form in this browser
    (_eligible) holding only `names`, and whether it held something else that cannot be put back
    now (values may then be empty); or None. The copies are taken as they are read (take)."""
    taken = take(request, key)
    if taken is None:
        return None
    request.vr_kept_controls = taken.controls  # for _put_back, as the copy was drawn
    return _given(taken, names)


def _given(taken, names):
    """offer()'s answer from a copy taken already, for a form drawing `names`."""
    allowed = set(names)
    values = taken.values
    given = {name: value for name, value in values.items() if name in allowed}
    dropped = taken.dropped or any(
        _filled(value) for name, value in values.items() if name not in allowed
    )
    if not any(_filled(value) for value in given.values()):
        return ({}, taken.ended_at, True) if dropped else None
    return given, taken.ended_at, dropped


def take(request, key):
    """For a page opened in the window with GET by a signed-in session: the newest eligible copy
    of this account's for the form `key` in this browser (_eligible), taken (Taken), or None. The
    copies are taken as they are read, in a savepoint: when another page holds the newest at that
    moment, or took it after this page's reading began, nothing is given back (never an older copy
    instead). A page whose forms depend on what it gives back (the desk's new ticket) takes the
    copy first, draws its forms from it (draw), then passes it to prepare."""
    from .sessions import _is_navigation

    user = getattr(request, "user", None)
    state = getattr(request, "account_session", None)
    if request.method != "GET" or state is None or user is None or not user.is_authenticated:
        return None
    if not _is_navigation(request) or _prefetch(request):
        return None
    browser = _browser(request, create=False)
    if browser is None:
        return None
    candidates = _eligible(user.pk, key, browser[1]).exclude(session_id=state.id)
    try:
        with transaction.atomic():
            rows = list(
                candidates.order_by("-session__ended_at", "-id").values_list(
                    "id", "values", "session__ended_at", "controls", "dropped"
                )
            )
            if not rows:
                return None
            locked = set(
                candidates.filter(id__in=[row[0] for row in rows])
                .select_for_update(skip_locked=True, of=("self",))
                .values_list("id", flat=True)
            )
            if rows[0][0] not in locked:
                return None  # another page is giving the newest back
            KeptDraft.objects.filter(id__in=locked).update(values=None, taken_at=Now())
    except OperationalError as error:
        if not _serialization_refusal(error):
            raise
        # Taken by a page whose change committed after this page's reading began (REPEATABLE
        # READ refuses to lock it): that page gave it back.
        return None
    _, values, ended_at, controls, dropped_at_keep = rows[0]
    return Taken(values or {}, ended_at, controls or {}, bool(dropped_at_keep))


def draw(form, taken):
    """Draw a taken copy's values in the unbound `form` as _put_back does, for a page that decides
    what else it draws from them (the category and the ticket form chosen); prepare then draws the
    whole page's forms."""
    _put_back(form, taken.values, taken.controls)


def discard_all(user_id):
    """Remove every copy of the account's (Sign out) and record the moment, after waiting for a
    copy being kept, so that no copy from a session that ended before it is kept afterwards."""
    with transaction.atomic():
        _lock(user_id, shared=False)
        cutoffs = connection.ops.quote_name(KeptDraftCutoff._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {cutoffs} (user_id, discarded_at) VALUES (%s, clock_timestamp()) "
                f"ON CONFLICT (user_id) DO UPDATE SET discarded_at = EXCLUDED.discarded_at",
                [user_id],
            )
        KeptDraft.objects.filter(user_id=user_id).delete()


def remove_expired(now=None):
    """The scheduled job: remove the copies whose session ended KEPT_FOR ago or more, skipping a
    copy another transaction holds at that moment (a page giving it back) until the next run."""
    from django.utils import timezone

    now = now or timezone.now()
    table = connection.ops.quote_name(KeptDraft._meta.db_table)
    sessions = connection.ops.quote_name(AccountSession._meta.db_table)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM {table} WHERE id IN (SELECT k.id FROM {table} k "
            f"JOIN {sessions} s ON s.id = k.session_id WHERE s.ended_at <= %s "
            f"FOR UPDATE OF k SKIP LOCKED)",
            [now - KEPT_FOR],
        )
        removed = cursor.rowcount
    busy = KeptDraft.objects.filter(session__ended_at__lte=now - KEPT_FOR).count()
    return {"kept_removed": removed, "busy": busy}


def _offered(field, value):
    """Whether a choice field offers `value` (a string) as drawn now. A list fetched as one types
    offers what its field's own records hold (one query, not every choice)."""
    if value == "":
        return True
    if type(field.widget) is RemoteSelect:
        try:
            return field.to_python(value) is not None
        except forms.ValidationError:
            return False
    return any(str(key) == value for key, _label in _flat_choices(field))


def _flat_choices(field):
    for key, label in field.choices:
        if isinstance(label, (list, tuple)):  # a group
            yield from label
        else:
            yield key, label


# What a browser's date and date-time inputs take (anything else it empties before the date
# editor reads it).
DATE_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?$")


def _holds_date(input_type, text):
    """Whether a browser's date or date-time input can hold `text` (a real date, in its form)."""
    pattern = DATE_VALUE if input_type == "date" else DATETIME_VALUE
    if not pattern.match(text):
        return False
    try:
        if input_type == "date":
            datetime.date.fromisoformat(text)
        else:
            datetime.datetime.fromisoformat(text.replace(" ", "T", 1))
    except ValueError:
        return False
    return True


def _carries(drawn, now):
    """Whether a value from a control of kind `drawn` may go into one of kind `now`: a ticked box
    only from a ticked box (a box is an affirmation: an answer to another question, a "no" or an
    "on" chosen from a list, must not tick it); any other control takes the value as it is, its
    own checks deciding (a choice still offered, a date's form). A value whose kind is not known
    never ticks a box."""
    return now != "box" or drawn == "box"


def _put_back(form, given, controls):
    """Draw the copy's values in the unbound form: its initial values, field by field, as the
    widgets read them. A ticked box takes a value only from a ticked box (`controls`, FAMILIES, as
    the copy was drawn) and only the value a ticked box posts; one answer carries between one
    choice and several; a choice goes back only while offered; a date its input cannot hold (still
    being typed) is handed to the date editor as text (data-vr-date-text, static/js/controls.js);
    anything else is left as the form starts. Each value goes into its own field's form (Parts).
    Returns (how many values were drawn, whether something could not be put back, the (label,
    text) of each date handed to the editor)."""
    missed, drawn, texts = False, 0, []
    names, _typed, several = keepable(form)
    allowed = set(names)
    now = controls_of(form)

    def fits(name, value):
        nonlocal missed
        ok = _carries(controls.get(name), now.get(name))
        if not ok and _filled(value):
            missed = True
        return ok

    for bound in form.visible_fields():
        field, widget, name = bound.field, bound.field.widget, bound.html_name
        if isinstance(widget, forms.MultiWidget) and type(widget) not in TYPED_WIDGETS:
            parts = {}
            for suffix in widget.widgets_names:
                value = given.get(name + suffix)
                if name + suffix not in allowed or value is None:
                    continue
                if not isinstance(value, str):
                    missed = True
                elif fits(name + suffix, value):
                    parts[suffix[1:]] = value
                    drawn += bool(value.strip())
            if parts:
                bound.form.initial[bound.name] = parts
            continue
        if name not in allowed or name not in given:
            continue
        value = given[name]
        if not fits(name, value):
            continue
        # A field that changed between one choice and several since: one answer carries over.
        if name in several and isinstance(value, str):
            value = [value] if value else []
        elif name not in several and isinstance(value, list):
            if len(value) > 1:
                missed = True
                continue
            value = value[0] if value else ""
        if name in several:
            offered = [item for item in value if _offered(field, item)]
            missed = missed or len(offered) < len(value)
            bound.form.initial[bound.name] = offered
            drawn += bool(offered)
        elif isinstance(widget, forms.CheckboxInput):
            # What a ticked box posts: its value attribute, "on" without one; "" is unticked.
            ticked = str(widget.attrs.get("value", "on"))
            if value == "":
                bound.form.initial[bound.name] = False
            elif value == ticked:
                bound.form.initial[bound.name] = True
                drawn += 1
            else:
                missed = True
        elif isinstance(field, forms.ChoiceField) or hasattr(field, "queryset"):
            if _offered(field, value):
                bound.form.initial[bound.name] = value
                drawn += _filled(value)
            else:
                missed = True
        elif getattr(widget, "input_type", "") in ("date", "datetime-local") and value.strip():
            if _holds_date(widget.input_type, value.strip()):
                bound.form.initial[bound.name] = value.strip().replace(" ", "T", 1)
            else:
                # Still being typed: the browser's date input would show nothing, so the date
                # editor starts from the text instead.
                bound.form.initial[bound.name] = value
                widget.attrs["data-vr-date-text"] = value
                texts.append((bound.label, value))
            drawn += 1
        else:
            bound.form.initial[bound.name] = value
            drawn += _filled(value)
    return drawn, missed, texts


_OFFER = object()  # prepare: take the copy now


def prepare(request, form, key, *, taken=_OFFER, give=True):
    """For a full-page form drawn with {% form_page keep=... %}, before its fields are drawn: the
    contents of its marker (includes/kept_typing.html), or None when the form keeps nothing for
    this request. When a copy is given back (offer), its values are drawn in the form, the page
    says when its session ended and whether something could not be put back, and Discard opens
    the page again (the copy is taken by then). Drawing the marker sets the browser's cookie.
    `taken`: the copy the page took already (take; None when it found none), instead of taking
    it now. `give`: False for a part of a page drawn again in place (the desk's category section),
    which never gives a copy back."""
    if request is None or not key or not form:
        return None
    value = token(request, key, form)
    if not value and not isinstance(taken, Taken):
        return None
    # A copy taken already is drawn all the same, with the notice, when the form can no longer be
    # marked (it has grown past MAX_FIELDS since): the page then keeps nothing more.
    ensure_browser(request)
    names, _typed, several = keepable(form)
    contents = {
        "token": value,
        "field": FIELD,
        "names": json.dumps(names),
        "several": json.dumps(several),
        "key": key,
        "bound": form.is_bound,
    }
    if form.is_bound or not give:
        offered = None
    elif taken is _OFFER:
        offered = offer(request, key, names)
    elif taken is None:
        offered = None
    else:
        request.vr_kept_controls = taken.controls
        offered = _given(taken, names)
    if offered:
        given, ended_at, dropped = offered
        drawn, missed, texts = (
            _put_back(form, given, getattr(request, "vr_kept_controls", {}))
            if given
            else (0, False, [])
        )
        # What a page gave back, for its tests to read.
        request.vr_kept_given = {**getattr(request, "vr_kept_given", {}), key: given}
        filled = [
            name
            for name, value in given.items()
            if (value.strip() if isinstance(value, str) else value)
        ]
        contents.update(
            restored=True,
            # Nothing of it could be drawn: the page says so instead.
            nothing=not drawn,
            ended_at=ended_at,
            missed=dropped or missed or bool(texts),
            # Only dates handed to the date editor as text were not drawn in their inputs: the
            # script hides the sentence once the editor shows them (none without scripts).
            dates_only=bool(texts) and not (dropped or missed),
            # Shown where scripts do not run, so the text typed is not lost from sight.
            date_texts=texts,
            # The names given back with a value: a template may leave a field of the form out of
            # the page (kept-typing.js then shows the sentence on what was not put back).
            given=json.dumps(filled),
            discard_url=request.get_full_path(),
            has_files=any(
                isinstance(bound.field.widget, forms.FileInput) for bound in form.visible_fields()
            ),
        )
    return contents
