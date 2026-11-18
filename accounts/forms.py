from allauth.mfa.base.forms import AuthenticateForm, ReauthenticateForm
from django.utils.translation import gettext_lazy as _


class AuthenticatorCodeLabel:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["code"].label = _("Authenticator or recovery code")


class MFAAuthenticateForm(AuthenticatorCodeLabel, AuthenticateForm):
    pass


class MFAReauthenticateForm(AuthenticatorCodeLabel, ReauthenticateForm):
    pass
