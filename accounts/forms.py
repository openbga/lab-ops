from allauth.account import forms as account_forms
from allauth.core import context
from allauth.mfa.base.forms import AuthenticateForm, ReauthenticateForm
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from .passwords import CHECKED_MINIMUM, guidance_html

# allauth's error code for an attempt refused by the failed sign-in limit.
TOO_MANY_ATTEMPTS = "too_many_login_attempts"


class AuthenticatorCodeLabel:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["code"].label = _("Authenticator or recovery code")


class RecordedLockout:
    """A code refused for too many failed attempts, before it was checked, is written to the
    account's sign-in history (accounts.signin_history). A code checked and not accepted is
    written by accounts.signin_log, from allauth's signal."""

    def clean_code(self):
        try:
            return super().clean_code()
        except ValidationError as error:
            if any(item.code == TOO_MANY_ATTEMPTS for item in error.error_list):
                from .models import SignInEvent, SignInFactor
                from .signin_history import record

                record(
                    self.user,
                    SignInEvent.Kind.LOCKED_OUT,
                    context.request,
                    factor=SignInFactor.SECOND_FACTOR,
                )
            raise


class MFAAuthenticateForm(RecordedLockout, AuthenticatorCodeLabel, AuthenticateForm):
    pass


class MFAReauthenticateForm(RecordedLockout, AuthenticatorCodeLabel, ReauthenticateForm):
    pass


class PasswordGuidance:
    """A new password's field states the rule for the account it is chosen for (accounts.
    passwords.guidance). Drawn with the form, from the second-factor rule as it stands then; once
    the form has checked a password, drawn again with the length that check used, so the page
    never states one length beside a refusal that names another."""

    guidance_field = "password1"
    guidance_own = True

    def guided_account(self):
        return self.user

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.draw_guidance()

    def draw_guidance(self, minimum=None):
        field = self.fields.get(self.guidance_field)
        if field is not None:
            field.help_text = guidance_html(
                self.guided_account(), own=self.guidance_own, minimum=minimum
            )
            # A bound field copies its field's help when the form first builds it (checking the
            # form builds them all), and a page may hold it already: give it this help too.
            bound = self._bound_fields_cache.get(self.guidance_field)
            if bound is not None:
                bound.help_text = field.help_text
                # Its inputs are drawn again too, with the errors the check found.
                bound.__dict__.pop("subwidgets", None)

    def full_clean(self):
        account = self.guided_account()
        if account is not None:
            try:
                delattr(account, CHECKED_MINIMUM)
            except AttributeError:
                pass
        super().full_clean()
        minimum = getattr(account, CHECKED_MINIMUM, None) if account is not None else None
        if minimum is not None:
            self.draw_guidance(minimum)


def record_current_password_refused(user):
    """A signed-in account's current password not accepted by a password form (Change password,
    and the admin console's own): a re-authentication that failed, in its sign-in history."""
    from .models import SignInEvent, SignInFactor
    from .signin_history import record

    record(
        user,
        SignInEvent.Kind.REAUTHENTICATION_FAILED,
        context.request,
        factor=SignInFactor.PASSWORD,
    )


class ChangePasswordForm(PasswordGuidance, account_forms.ChangePasswordForm):
    def clean_oldpassword(self):
        try:
            return super().clean_oldpassword()
        except ValidationError:
            record_current_password_refused(self.user)
            raise


class SetPasswordForm(PasswordGuidance, account_forms.SetPasswordForm):
    pass


class ResetPasswordKeyForm(PasswordGuidance, account_forms.ResetPasswordKeyForm):
    pass
