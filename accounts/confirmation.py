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
read one record. current() counts only re-authentications, never the sign-in itself, and needs
each factor the account has now, each proved within the last ACCOUNT_REAUTHENTICATION_TIMEOUT
seconds (5 minutes in this release): an authenticator added since the last confirmation asks
again. An account with neither a usable password nor an authenticator cannot confirm, so it
cannot make these changes until it sets a password.

A password or code not accepted is written to the account's sign-in history as a failed
re-authentication (accounts.adapter, accounts.signin_log), counts toward the failed sign-in
limit, and refusals by that limit are recorded as for sign-in. Nothing typed is kept, logged or
written to an audit event: the fields never count as a change of the form (has_changed).
"""

import time

from allauth.account import app_settings as account_settings
from allauth.core import context
from django import forms
from django.core.exceptions import ValidationError

PASSWORD_FIELD = "confirm_password"
CODE_FIELD = "confirm_code"
LEGEND = "Confirm it is you"
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


def factors(user):
    """(password, code): the factors `user` confirms with, as the account stands now: a usable
    password, and an authenticator app of a type this deployment offers."""
    from core.second_factor import has_second_factor

    if user is None or not getattr(user, "is_authenticated", False):
        return False, False
    return user.has_usable_password(), has_second_factor(user)


def _fresh(request, user):
    """The methods `user` re-authenticated with in this session within the window: "password"
    and "mfa" (an authenticator or recovery code)."""
    from allauth.account.internal.flows.login import get_authentication_records
    from allauth.mfa import app_settings as mfa_settings

    now = time.time()
    proved = set()
    for item in get_authentication_records(request, user):
        if not isinstance(item, dict) or not item.get("reauthenticated"):
            continue
        at = item.get("at")
        if not isinstance(at, (int, float)) or not 0 <= now - at < window():
            continue
        method = item.get("method")
        if method == "password":
            proved.add("password")
        elif method == "mfa" and item.get("type") in mfa_settings.SUPPORTED_TYPES:
            proved.add("mfa")
    return proved


def current(request):
    """Whether the signed-in account of `request` confirmed it is them recently enough: every
    factor it has now proved again in this session within the window. False without a signed-in
    request, and for an account with nothing to confirm with."""
    user = getattr(request, "user", None)
    if request is None or getattr(request, "session", None) is None:
        return False
    password, code = factors(user)
    if not (password or code):
        return False
    proved = _fresh(request, user)
    return (not password or "password" in proved) and (not code or "mfa" in proved)


class ConfirmationField(forms.CharField):
    """A confirmation field: never a change of the form (Django admin's change message lists the
    changed fields), never drawn with its value."""

    def has_changed(self, initial, data):
        return False


def password_field():
    return ConfirmationField(
        label="Your password",
        strip=False,
        widget=forms.PasswordInput(
            attrs={"autocomplete": "current-password", "class": "form-control"}
        ),
        error_messages={"required": "Enter your password to save this change."},
    )


def code_field():
    return ConfirmationField(
        label="Authenticator or recovery code",
        widget=forms.TextInput(
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
        self.confirmed_now = False
        self.confirm_refused = None
        self.confirming = not current(request)
        self.confirm_with = factors(getattr(request, "user", None)) if self.confirming else None
        if self.confirming:
            password, code = self.confirm_with
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
        if self.confirming and not self._confirm():
            # Nothing else is checked: the form's own checks may lock rows or read what the change
            # would decide on, which an unconfirmed request does not get to.
            return self.cleaned_data
        return super().clean()

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
                for message in code_form.errors.get("code", ["Incorrect code."]):
                    self.add_error(CODE_FIELD, message)
                return False
        if factors(user) != self.confirm_with:
            # The account gained or lost a factor since the form asked (an authenticator added in
            # another tab): what was asked is no longer what it has, so ask again.
            self.add_error(
                None,
                "Your sign-in factors changed while this page was open. Confirm it is you again.",
            )
            return False
        if PASSWORD_FIELD in self.fields:
            reauthenticate_by_password(request)
        if code_form is not None:
            code_form.save()
        self.confirmed_now = True
        return True
