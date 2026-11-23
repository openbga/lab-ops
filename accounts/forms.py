from allauth.account import forms as account_forms
from allauth.mfa.base.forms import AuthenticateForm, ReauthenticateForm
from django.utils.translation import gettext_lazy as _

from .passwords import CHECKED_MINIMUM, guidance_html


class AuthenticatorCodeLabel:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["code"].label = _("Authenticator or recovery code")


class MFAAuthenticateForm(AuthenticatorCodeLabel, AuthenticateForm):
    pass


class MFAReauthenticateForm(AuthenticatorCodeLabel, ReauthenticateForm):
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


class ChangePasswordForm(PasswordGuidance, account_forms.ChangePasswordForm):
    pass


class SetPasswordForm(PasswordGuidance, account_forms.SetPasswordForm):
    pass


class ResetPasswordKeyForm(PasswordGuidance, account_forms.ResetPasswordKeyForm):
    pass
