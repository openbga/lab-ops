"""Confirm it is you: the password, and the second factor where the account has one, asked for
again before a change to roles, capabilities, security settings or another account's sign-in
factors (docs/safeguards.md).

Which changes ask. Every save in account administration that changes an account (adding one,
its page, its password page, marking its password as compromised, removing its authenticator) or
a role (adding one or its page), and a change to the session limits or the second-factor rule on
Security settings. Ending an account's sessions does not ask: it takes access away and stays quick
when a device is lost. Nor do the laboratory's contacts, its profile or its modules.

How. Each form that makes such a change mixes in ConfirmIdentity. When it is built, it asks
current() whether this browser's session confirmed recently enough; if not, it adds Your password
(when the account has a usable password) and Authenticator or recovery code (when it has an
authenticator app), and checks them before the form's own clean(): nothing is decided, locked or
saved on a request that has not confirmed. A page drawn while the confirmation was
current and saved after it lapsed asks then, with what was typed kept, passwords aside, as every
form keeps them. A field typed into a page that no longer needs it (confirmed in another tab
meanwhile) is ignored.

Each factor proved is recorded in the browser's session as allauth records a re-authentication
(a "reauthenticated" authentication record with its time), so allauth's own re-authentication
pages, which ask for one factor before a person changes their own authenticators, and these forms
read one record. check() counts only re-authentications, never the sign-in itself, and needs
each factor the account has now (read from the database, never the request's copy of the account),
each proved within the last ACCOUNT_REAUTHENTICATION_TIMEOUT seconds (5 minutes in this release)
and still standing for what the account has: a password re-authentication only while the account
keeps the password that was checked (password_checked keeps the hash the check accepted on the
request, and remember_password, when allauth records the re-authentication, a keyed digest of it
in the session, never the password); a code only while the account keeps the authenticator it
came from (allauth records its id) and when it was given after the account's newest
authenticator app was added. So an authenticator added or replaced, or a password changed (the
account's own Change password keeps its browser signed in), since the last confirmation asks
again. A form confirmed when it was built is checked again as its save is checked, and a form
that asks accepts what was typed only if, when it is checked, the account still has the password
that was checked, the newest app it had when the form asked and the authenticator the code came
from.

Accepted limits, each pinned by a strict xfail in the review tests: the confirmation is read when
the save is checked and not held through the save, so a factor changed by another request between
that check and the commit does not stop the save; two tabs re-authenticating at the same moment in
one browser can each write the session over the other (Django keeps a session whole), so one factor
is asked for again; a view that names its own sensitive fields replaces the list mark_sensitive set
(allauth's sign-in pages, which post no confirmation); Django's filter of a traceback's variables
does not read the everything marker its admin add and password views set before a form is built
(a refusal there is a 403, for which no error report is made, and this deployment sends none);
the password proof is one digest per session, so only a server clock set back between two
re-authentications with different passwords lets the older one's time stand for the newer; a
confirmation refused before its final reading (a field left empty, a wrong password or code)
while the account gains or loses a method is drawn again with the fields it asked for (the save is
refused, and the next form asks for what the account has); a failure of the server
or of the session's storage after a code was accepted can leave it used without the confirmation
recorded (allauth's own pages too), and an admin console save refused after its form was checked
can keep the confirmation while its transaction gives the code back: the person confirms again;
a recovery-code proof stored in the session counts for a replacement app added at the very same
instant as the app it replaced (app times are compared, not their ids, for a stored proof); a
session interrupted while a confirmation is recorded can be written again by the response, which is
interrupted the same way (400).

An account with neither a usable password nor an authenticator cannot confirm, so it cannot make
these changes until it sets a password.

A password or code not accepted is written to the account's sign-in history as a failed
re-authentication (accounts.adapter, accounts.signin_log), counts toward the failed sign-in
limit, and refusals by that limit are recorded as for sign-in. Nothing typed is kept, logged or
written to an audit event: the fields never count as a change of the form (has_changed), a code
is never drawn back into the page, and every request marks the fields sensitive before any view
runs (mark_sensitive, from core.request_logging), so an error report leaves them out. The fields
carry no required attribute, so a page confirmed in another tab meanwhile can still be sent.
"""

import time
from dataclasses import dataclass

from allauth.account import app_settings as account_settings
from allauth.core import context
from django import forms
from django.core.exceptions import ValidationError

PASSWORD_FIELD = "confirm_password"
CODE_FIELD = "confirm_code"
LEGEND = "Confirm it is you"
AGAIN = (
    "Your sign-in factors changed, or your confirmation ran out, while this page was open. Confirm "
    "it is you again."
)
NO_METHOD = (
    "Your account has no password to confirm it is you, so this change cannot be saved. Set a "
    "password on Change password first."
)


def window():
    """Seconds a confirmation stays current: allauth's re-authentication timeout."""
    return account_settings.REAUTHENTICATION_TIMEOUT


def window_label():
    minutes, seconds = divmod(window(), 60)
    if seconds:
        return f"{window()} seconds"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


@dataclass(frozen=True)
class Factors:
    """What an account confirms with, read from the database in one statement (read()): its
    password's stored hash when the password is usable, the ids of its authenticators of a type
    this deployment offers (an authenticator app, recovery codes), and whether it has an
    authenticator app (a second factor: recovery codes alone are not, core.second_factor)."""

    password_hash: str | None = None
    authenticators: frozenset = frozenset()
    app: bool = False
    # When the account's newest authenticator app was added (a timestamp), or None.
    app_since: float | None = None
    # The ids of its authenticator apps.
    apps: frozenset = frozenset()

    @property
    def password(self):
        return self.password_hash is not None

    def asks(self):
        """(password, code): the fields a confirmation asks for."""
        return self.password, self.app


NOTHING = Factors()


def read(user):
    """The Factors of `user` as the database holds them now (NOTHING without a signed-in account):
    never the copy of the account the request loaded, which an earlier reading may have left
    behind a change committed since."""
    from allauth.mfa import app_settings as mfa_settings
    from django.contrib.auth.hashers import is_password_usable
    from django.contrib.postgres.aggregates import ArrayAgg
    from django.db.models import Max, Q

    from core.second_factor import second_factor_types

    from .models import User

    if user is None or not getattr(user, "is_authenticated", False):
        return NOTHING
    offered = Q(authenticator__type__in=list(mfa_settings.SUPPORTED_TYPES))
    apps = Q(authenticator__type__in=second_factor_types())
    row = (
        User.objects.filter(pk=user.pk)
        .annotate(
            ids=ArrayAgg("authenticator__id", filter=offered, default=[]),
            app_ids=ArrayAgg("authenticator__id", filter=apps, default=[]),
            since=Max("authenticator__created_at", filter=apps),
        )
        .values_list("password", "ids", "app_ids", "since")
        .first()
    )
    if row is None:
        return NOTHING
    stored, ids, app_ids, since = row
    return Factors(
        password_hash=stored if is_password_usable(stored) else None,
        authenticators=frozenset(ids),
        app=since is not None,
        app_since=since.timestamp() if since is not None else None,
        apps=frozenset(app_ids),
    )


def factors(user):
    """(password, code): the factors `user` confirms with, as the account stands now."""
    return read(user).asks()


PASSWORD_PROOF_KEY = "_confirmation_password"


def _password_proof(password_hash):
    from django.utils.crypto import salted_hmac

    return salted_hmac("accounts.confirmation.password", password_hash).hexdigest()


CHECKED = "_vr_password_checked"


def password_checked(request, password_hash):
    """A password asked for again was accepted (accounts.adapter.ClosedSignupAdapter.
    reauthenticate, on these forms and on allauth's re-authentication page): the stored hash it was
    checked against (after any upgrade the check made), kept on the request until the
    re-authentication is recorded (remember_password). Nothing is written to the session yet: a
    confirmation refused at its code leaves it as it was."""
    if request is not None:
        setattr(request, CHECKED, password_hash)


def remember_password(sender, request=None, method=None, reauthenticated=False, **kwargs):
    """allauth recorded a step (authentication_step_completed): for a password re-authentication,
    which password it was, as a keyed digest of the hash password_checked kept, in the browser's
    session. A password re-authentication counts only while the account still has that password,
    so a password changed since (the account's own Change password keeps its browser signed in) is
    asked for again."""
    if method != "password" or not reauthenticated or request is None:
        return
    if getattr(request, "session", None) is None:
        return
    checked = getattr(request, CHECKED, None)
    if checked:
        request.session[PASSWORD_PROOF_KEY] = _password_proof(checked)
    else:
        request.session.pop(PASSWORD_PROOF_KEY, None)


def connect():
    from allauth.account.signals import authentication_step_completed

    authentication_step_completed.connect(
        remember_password, dispatch_uid="accounts.confirmation.remember_password"
    )


def _fresh(request, user, state):
    """The methods `user` re-authenticated with in this session within the window, each still
    standing for what the account has now: "password" while it has the password that was given,
    "mfa" while it still has the authenticator (app or recovery codes) whose code was given."""
    from allauth.account.internal.flows.login import get_authentication_records
    from django.utils.crypto import constant_time_compare

    now = time.time()
    proved = set()
    password_kept = state.password and constant_time_compare(
        str(request.session.get(PASSWORD_PROOF_KEY, "")), _password_proof(state.password_hash)
    )
    for item in get_authentication_records(request, user):
        if not isinstance(item, dict) or not item.get("reauthenticated"):
            continue
        at = item.get("at")
        if not isinstance(at, (int, float)) or not 0 <= now - at < window():
            continue
        method = item.get("method")
        if method == "password" and password_kept:
            proved.add("password")
        elif (
            method == "mfa"
            and item.get("id") in state.authenticators
            and (state.app_since is None or at >= state.app_since)
        ):
            # A code from an authenticator the account still has, given after its newest app was
            # added: a recovery code given before a new app was set up does not stand for it.
            proved.add("mfa")
    return proved


def check(request):
    """(current, factors): whether the signed-in account of `request` confirmed it is them
    recently enough (every factor it has now proved again in this session within the window, for
    the password and the authenticator it has now), and its Factors as read for that. False
    without a signed-in request, and for an account with nothing to confirm with."""
    if request is None or getattr(request, "session", None) is None:
        return False, NOTHING
    user = getattr(request, "user", None)
    state = read(user)
    password, code = state.asks()
    if not (password or code):
        return False, state
    proved = _fresh(request, user, state)
    ok = (not password or "password" in proved) and (not code or "mfa" in proved)
    return ok, state


def current(request):
    """Whether the signed-in account of `request` confirmed recently enough (check())."""
    return check(request)[0]


def mark_sensitive(request):
    """Mark the confirmation's fields as Django's sensitive_post_parameters does, so that an error
    report never shows them: every request, before any view (core.request_logging), so also one
    refused before its form is built, and again when a form that asks is built, after a view's own
    sensitive_post_parameters replaced the list (allauth's sign-in pages name theirs; they carry no
    confirmation)."""
    marked = getattr(request, "sensitive_post_parameters", None)
    if marked == "__ALL__":
        # A view marked everything (Django's admin password and add pages). Django's filter of a
        # traceback's variables reads the marker as a list of names, so name them all.
        marked = request.POST.keys()
    request.sensitive_post_parameters = sorted({*(marked or ()), PASSWORD_FIELD, CODE_FIELD})


class ConfirmationField(forms.CharField):
    """A confirmation field: never a change of the form (Django admin's change message lists the
    changed fields), never drawn with its value."""

    def has_changed(self, initial, data):
        return False


class Unrequired:
    """A widget drawn without the required attribute, though its field is required: a page whose
    confirmation was given in another tab meanwhile is still sent, and the server decides."""

    def use_required_attribute(self, initial):
        return False


class PasswordInput(Unrequired, forms.PasswordInput):
    pass


class CodeInput(Unrequired, forms.TextInput):
    """Never drawn with a code typed into it: a recovery code refused beside a wrong password is
    still unused."""

    def format_value(self, value):
        return None


def password_field():
    return ConfirmationField(
        label="Your password",
        strip=False,
        widget=PasswordInput(attrs={"autocomplete": "current-password", "class": "form-control"}),
        error_messages={"required": "Enter your password to save this change."},
    )


def code_field():
    return ConfirmationField(
        label="Authenticator or recovery code",
        widget=CodeInput(
            attrs={"autocomplete": "one-time-code", "spellcheck": "false", "class": "form-control"}
        ),
        error_messages={
            "required": "Enter a code from your authenticator app, or a recovery code, to save "
            "this change."
        },
    )


class ConfirmIdentity:
    """Mixed into a form that makes a change this module covers, before the form's own classes.

    `confirming` says whether the form asks (decided when it is built, from the session and the
    account as they stand then); `confirm_fields()` are the bound fields to draw, in a section
    of their own headed LEGEND, the first with `confirmation_help()` as its help. clean() checks
    them before the form's own clean() and returns without it when they are not accepted (the
    fields' own checks, which only read, have run by then). A confirmation accepted on a form
    whose other fields are refused is kept: the page is drawn again without asking.

    `grouped()` adds the section to a form drawn by sections (core.lab_profile_forms.GroupedForm,
    includes/form_sections.html)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = context.request
        self.confirm_request = request
        if request is not None:
            mark_sensitive(request)  # already marked for every request; also outside one
        self.confirmed_now = False
        self.confirm_refused = None
        self.confirming = False
        self.confirm_with = None
        ok, state = check(request)
        if not ok:
            self._ask(state)

    def _ask(self, state):
        """Ask for the factors `state` (Factors) has: add their fields, the first with the help."""
        self.confirming = True
        self.confirm_state = state
        self.confirm_with = password, code = state.asks()
        if not (password or code):
            self.confirm_refused = NO_METHOD
        if password:
            self.fields[PASSWORD_FIELD] = password_field()
        if code:
            self.fields[CODE_FIELD] = code_field()
        if password or code:
            # Why it asks, and for how long, under the first field (its described-by help).
            first = PASSWORD_FIELD if password else CODE_FIELD
            self.fields[first].help_text = self.confirmation_help()

    def confirm_fields(self):
        """The confirmation's bound fields to draw: none once it is accepted or not asked."""
        if not self.confirming or self.confirmed_now:
            return []
        return [self[name] for name in (PASSWORD_FIELD, CODE_FIELD) if name in self.fields]

    def grouped(self):
        groups = super().grouped()
        fields = self.confirm_fields()
        return [*groups, (LEGEND, fields)] if fields else groups

    def confirmation_help(self):
        password, code = self.confirm_with or (False, False)
        asked = (
            "your password and a code from your authenticator app (or a recovery code)"
            if password and code
            else "a code from your authenticator app (or a recovery code)"
            if code
            else "your password"
        )
        return (
            f"Saving this change asks for {asked} again. Once you have confirmed, you are not "
            f"asked again in this browser for {window_label()}."
        )

    def clean(self):
        if not self.confirming:
            # Confirmed when the form was built: decided again now, as the save is checked, from
            # the session and the account as they stand (a factor changed meanwhile asks again).
            ok, state = check(self.confirm_request)
            if not ok:
                self._ask(state)
                self.add_error(None, self.confirm_refused or AGAIN)
                self._focus_confirmation()
                return self.cleaned_data
        elif not self._confirm():
            # Nothing else is checked: the form's own checks may lock rows or read what the change
            # would decide on, which an unconfirmed request does not get to.
            self._focus_confirmation()
            return self.cleaned_data
        return super().clean()

    def _focus_confirmation(self):
        """A page drawn again because of the confirmation opens on its first field to fill in,
        unless a field of the form's own is refused too (that one comes first on the page)."""
        own = [name for name in self.errors if name not in (PASSWORD_FIELD, CODE_FIELD, "__all__")]
        if own:
            return
        names = [name for name in (PASSWORD_FIELD, CODE_FIELD) if name in self.fields]
        refused = [name for name in names if name in self.errors]
        if not (refused or names):
            return
        for field in self.fields.values():
            field.widget.attrs.pop("autofocus", None)
        self.fields[(refused or names)[0]].widget.attrs["autofocus"] = True

    def _post_clean(self):
        # Nor what a form checks after clean() (a model form's instance, a new password against
        # the account), which may rely on what its clean() worked out.
        if self.confirming and not self.confirmed_now:
            return
        super()._post_clean()

    def _confirm(self):
        """Check what was typed, password first; on success record each factor in the session.
        False, with the errors on the fields, when not accepted."""
        from allauth.account.adapter import get_adapter
        from allauth.account.internal.flows.reauthentication import reauthenticate_by_password
        from allauth.core import ratelimit

        request = self.confirm_request
        if self.confirm_refused or request is None:
            self.add_error(None, self.confirm_refused or NO_METHOD)
            return False
        if PASSWORD_FIELD in self.errors or CODE_FIELD in self.errors:
            return False  # a field left empty: its own message says what to enter
        user = request.user
        if not ratelimit.consume(request, action="reauthenticate", user=user):
            self.add_error(
                None,
                "Too many attempts to confirm it is you. Wait a minute, then try again.",
            )
            return False
        password_ok = PASSWORD_FIELD not in self.fields
        if not password_ok:
            try:
                password_ok = get_adapter(request).reauthenticate(
                    user, self.cleaned_data[PASSWORD_FIELD]
                )
            except ValidationError as error:  # refused by the failed sign-in limit
                self.add_error(PASSWORD_FIELD, error)
                return False
            if not password_ok:
                self.add_error(PASSWORD_FIELD, "Incorrect password.")
                return False
        code_form = None
        if CODE_FIELD in self.fields:
            from .forms import MFAReauthenticateForm

            code_form = MFAReauthenticateForm(
                data={"code": self.cleaned_data[CODE_FIELD]}, user=user
            )
            if not code_form.is_valid():
                if PASSWORD_FIELD in self.fields:
                    # Not kept by the page: it is asked for again with the next code.
                    self.add_error(PASSWORD_FIELD, "Enter your password again with the code.")
                for message in code_form.errors.get("code", ["Incorrect code."]):
                    self.add_error(CODE_FIELD, message)
                return False
        state = read(user)
        given = getattr(request, CHECKED, None) if PASSWORD_FIELD in self.fields else None
        kept = (
            # The password checked is the one the account has now,
            (PASSWORD_FIELD not in self.fields or given == state.password_hash)
            # its apps are the ones it had when the form asked,
            and state.apps == self.confirm_state.apps
            # and the code came from an authenticator it still has.
            and (code_form is None or code_form.authenticator.pk in state.authenticators)
        )
        if state.asks() != self.confirm_with or not kept:
            # The account gained or lost a factor since the form asked (an authenticator added in
            # another tab), or the code came from an authenticator it no longer has: what was given
            # no longer stands for what it has, so ask again for what it has.
            for name in (PASSWORD_FIELD, CODE_FIELD):
                self.fields.pop(name, None)
                self._bound_fields_cache.pop(name, None)
            self._ask(state)
            self.add_error(None, self.confirm_refused or AGAIN)
            return False
        if PASSWORD_FIELD in self.fields:
            reauthenticate_by_password(request)
        if code_form is not None:
            code_form.save()
        # Written now, not only with the response: a code used here (a recovery code is spent) is
        # already committed outside a transaction, and a response that fails afterwards would not
        # save the session, leaving the code spent and the confirmation unrecorded.
        save = getattr(request.session, "save", None)
        if save is not None:
            from django.contrib.sessions.backends.base import UpdateError
            from django.contrib.sessions.exceptions import SessionInterrupted

            try:
                save()
            except UpdateError:
                # As Django's session middleware answers it (400): the session was ended, for
                # example by a sign-out in another tab, while this request was under way. Not
                # marked for writing again with the response; where the response writes it anyway
                # (notices kept in the session, SESSION_SAVE_EVERY_REQUEST), that write is
                # interrupted the same way and the answer stays 400.
                request.session.modified = False
                raise SessionInterrupted(
                    "The request's session was deleted before the request completed."
                ) from None
        self.confirmed_now = True
        return True
