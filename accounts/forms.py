from allauth.account import forms as account_forms
from allauth.mfa.base.forms import AuthenticateForm, ReauthenticateForm
from django.utils.translation import gettext_lazy as _

from .passwords import guidance_html


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
    passwords.guidance), drawn when the form is: the length follows the laboratory's second-factor
    rule as it stands then, and the save checks the rule again as it stands at the save."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password1"].help_text = guidance_html(self.user, own=True)


class ChangePasswordForm(PasswordGuidance, account_forms.ChangePasswordForm):
    pass


class SetPasswordForm(PasswordGuidance, account_forms.SetPasswordForm):
    pass


class ResetPasswordKeyForm(PasswordGuidance, account_forms.ResetPasswordKeyForm):
    pass
