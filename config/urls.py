from django.conf import settings
from django.contrib import admin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, resolve_url
from django.urls import include, path
from django.views.decorators.cache import never_cache

from accounts import compromised, enrolment, session_views
from core import (
    announcement_views,
    lab_profile_views,
    lookups,
    preferences,
    security_settings_views,
    views,
)
from desk.notification_views import email_notices


@never_cache
def admin_login(request, extra_context=None):
    """Send admin sign-in to the application's sign-in page.

    Django's own admin login accepts a password alone, so it would skip the sign-in rate limits
    and an enrolled second factor. It is never called: anonymous visitors go to the normal
    sign-in, and a signed-in account either may use the admin or is refused.
    """
    if not request.user.is_authenticated:
        target = request.GET.get("next") or resolve_url("admin:index")
        return redirect_to_login(target, resolve_url(settings.LOGIN_URL))
    if request.user.is_active and request.user.is_staff:
        return redirect("admin:index")
    raise PermissionDenied


admin.site.login = admin_login

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("health/", views.health, name="health"),
    path("whats-new/", views.whats_new, name="whats-new"),
    path("account/notices/", email_notices, name="email-notices"),
    path("account/session/", session_views.session_status, name="account-session"),
    path(
        "account/session/continue/",
        session_views.session_continue,
        name="account-session-continue",
    ),
    path("account/session/keep/", session_views.keep_typing, name="account-session-keep"),
    path("account/second-factor/", enrolment.enrolment_page, name="account-second-factor"),
    path("account/new-password/", compromised.new_password_page, name="account-new-password"),
    path("account/sessions/", session_views.sessions_page, name="account-sessions"),
    path("account/sign-ins/", session_views.sign_in_history_page, name="account-sign-ins"),
    path(
        "account/sessions/others/sign-out/",
        session_views.sign_out_other_sessions,
        name="account-sessions-sign-out-others",
    ),
    path(
        "account/sessions/<uuid:session_id>/sign-out/",
        session_views.sign_out_session,
        name="account-session-sign-out",
    ),
    path("lab/record-retention/", views.record_retention, name="record-retention"),
    path("lab/announcements/", announcement_views.announcement_list, name="announcement-list"),
    path(
        "lab/announcements/new/",
        announcement_views.announcement_edit,
        name="announcement-create",
    ),
    path(
        "lab/announcements/<int:pk>/",
        announcement_views.announcement_edit,
        name="announcement-edit",
    ),
    path(
        "lab/announcements/<int:pk>/withdraw/",
        announcement_views.announcement_withdraw,
        name="announcement-withdraw",
    ),
    path("lab/scheduled-jobs/", include("jobs.urls")),
    path("lab/profile/", lab_profile_views.lab_profile, name="lab-profile"),
    path("lab/profile/edit/", lab_profile_views.lab_profile_edit, name="lab-profile-edit"),
    path("lab/profile/logo/", lab_profile_views.lab_logo_edit, name="lab-logo-edit"),
    path("lab/profile/logo/remove/", lab_profile_views.lab_logo_remove, name="lab-logo-remove"),
    path(
        "lab/profile/logo/<str:sha256>/",
        lab_profile_views.lab_logo_image,
        name="lab-logo-image",
    ),
    path(
        "lab/profile/certificates/new/",
        lab_profile_views.lab_certificate_edit,
        name="lab-certificate-create",
    ),
    path(
        "lab/profile/certificates/<int:pk>/",
        lab_profile_views.lab_certificate_edit,
        name="lab-certificate-edit",
    ),
    path(
        "lab/profile/certificates/<int:pk>/remove/",
        lab_profile_views.lab_certificate_remove,
        name="lab-certificate-remove",
    ),
    path("lab/modules/<slug:key>/", lab_profile_views.lab_module, name="lab-module"),
    path(
        "lab/contacts/<slug:kind>/",
        lab_profile_views.lab_contact_edit,
        name="lab-contact-edit",
    ),
    path(
        "lab/security/",
        security_settings_views.security_settings_page,
        name="security-settings",
    ),
    path(
        "lab/security/second-factor/",
        security_settings_views.second_factor_edit,
        name="second-factor-edit",
    ),
    path(
        "lab/security/sessions/<slug:audience>/",
        security_settings_views.session_limits_edit,
        name="session-limits-edit",
    ),
    path("ready/", views.ready, name="ready"),
    path("lookups/<slug:name>/", lookups.lookup, name="lookup"),
    path("preferences/", preferences.preference, name="preference"),
    path("accounts/", include("allauth.urls")),
    path("admin/", admin.site.urls),
    path("clients/", include("clients.urls")),
    path("portal/", include("clients.portal_urls")),
    path("requests/", include("desk.urls")),
    path("teams/", include("teams.urls")),
    path("setup/categories/", include("taxonomy.urls")),
    path("setup/service-levels/", include("service_levels.urls")),
    path("setup/systems/", include("links.urls")),
    path("import/", include("dataimport.urls")),
    path("links/", include("links.record_urls")),
    # Request forms
    path("setup/request-forms/", include("request_forms.urls")),
    path("request-forms/", include("request_forms.request_urls")),
    path("portal/forms/", include("request_forms.portal_urls")),
    path("attachments/", include("attachments.urls")),
    # Optional modules (core.modules): their pages answer 404 while the module is off.
    # Optional module: its addresses are Not found while it is off (core.modules).
    path("quality/", include("nonconformance.urls")),
]
