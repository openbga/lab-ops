"""allauth's two-factor pages that change the person's sign-in factors, served by accounts.mfa_views
at allauth's own paths and URL names. Included under accounts/2fa/ before allauth's URLs, so these
three resolve here and every other allauth page is allauth's."""

from django.urls import path

from . import mfa_views

urlpatterns = [
    path("totp/activate/", mfa_views.activate_totp, name="mfa_activate_totp"),
    path("totp/deactivate/", mfa_views.deactivate_totp, name="mfa_deactivate_totp"),
    path(
        "recovery-codes/generate/",
        mfa_views.generate_recovery_codes,
        name="mfa_generate_recovery_codes",
    ),
]
