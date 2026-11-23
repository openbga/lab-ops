import os
import sys
from pathlib import Path

import dj_database_url

from core.security_headers import DEFAULT_CONTENT_SECURITY_POLICY

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"
# Debug mode and the email backend as the environment set them at startup. The test runner
# changes DEBUG and EMAIL_BACKEND afterwards; what is derived from them below stays as it was.
# core.security_settings compares the derived settings with these.
STARTUP_DEBUG = DEBUG
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise RuntimeError("SECRET_KEY must be supplied (or use DEBUG=true locally).")
    SECRET_KEY = "development-only-do-not-use-in-production"
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
# Render supplies this environment value; never derive trust from request headers.
render_hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()
if render_hostname:
    ALLOWED_HOSTS.append(render_hostname)
    CSRF_TRUSTED_ORIGINS.append(f"https://{render_hostname}")
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.forms",
    "allauth",
    "allauth.account",
    "allauth.mfa",
    "accounts",
    "core",
    "clients",
    "teams",
    "taxonomy",
    "desk",
    "service_levels",
    "links",
    "request_forms",
    "dataimport",
    "attachments",
    "jobs",
    "nonconformance",
]
MIDDLEWARE = [
    # Outermost: marks every answer to a signed-in session not to be stored, whichever layer drew
    # or replaced it (accounts.sessions).
    "accounts.sessions.NoStoreMiddleware",
    "core.security_headers.SecurityHeadersMiddleware",
    "core.request_logging.RequestLogMiddleware",
    "core.request_limits.RequestBodyLimitMiddleware",
    # Before the HTTPS redirect, CommonMiddleware and the CSRF check: while an optional module is
    # off its addresses do not exist, and every answer of one that is on is never stored
    # (core.modules).
    "core.modules.ModuleAddressMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.preferences.GridPreferenceMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "accounts.middleware.LockedCredentialsMiddleware",
    # Signs out a session past the laboratory's limits before anything acts for the account and
    # records its activity once a view is reached, after the check above (accounts.sessions).
    "accounts.sessions.SessionLimitMiddleware",
    # A session the laboratory's rule held to a second factor at sign-in reaches only the pages
    # that set one up while its account has none (accounts.enrolment).
    "accounts.enrolment.EnrolmentMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.form_dialog.FormDialogMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.workspace",
                "core.context_processors.form_presentation",
            ]
        },
    }
]
DATABASES = {
    "default": dj_database_url.config(
        default="postgresql://lab_ops:lab_ops@127.0.0.1:55432/lab_ops",
        conn_max_age=60,
        conn_health_checks=True,
    )
}
if test_database_name := os.environ.get("TEST_DATABASE_NAME"):
    if (
        not test_database_name.startswith("test_")
        or test_database_name == DATABASES["default"]["NAME"]
    ):
        raise RuntimeError("TEST_DATABASE_NAME must name a separate test_ database.")
    DATABASES["default"]["TEST"] = {"NAME": test_database_name}
AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
# The rule for a new password (accounts.passwords): 15 characters, or 8 where the laboratory
# requires a second sign-in factor of the account; common and expected passwords refused; no
# composition rule.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "accounts.passwords.LengthByFactorValidator"},
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "accounts.passwords.ExpectedPasswordValidator"},
]
ACCOUNT_ADAPTER = "accounts.adapter.ClosedSignupAdapter"
# The password forms state the rule for their account (accounts.passwords.guidance).
ACCOUNT_FORMS = {
    "change_password": "accounts.forms.ChangePasswordForm",
    "set_password": "accounts.forms.SetPasswordForm",
    "reset_password_from_key": "accounts.forms.ResetPasswordKeyForm",
}
ACCOUNT_LOGIN_METHODS = {"username", "email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "username*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "optional"
# allauth refuses to add an authenticator while any of the account's email addresses is
# unverified, which protects an open sign-up from an unverified address claiming an account.
# Sign-up is closed here (the laboratory creates every account), and where the laboratory requires
# a second factor (core.second_factor) the check would leave a person whose address cannot be
# verified (outgoing email off) unable to sign in at all.
MFA_ALLOW_UNVERIFIED_EMAIL = True
MFA_FORMS = {
    "authenticate": "accounts.forms.MFAAuthenticateForm",
    "reauthenticate": "accounts.forms.MFAReauthenticateForm",
}
ACCOUNT_LOGOUT_ON_GET = False
# Sign-in limits per address use the connecting address. Behind a reverse proxy that is the
# proxy, so every visitor would share one limit and failed attempts by anyone would lock
# everyone out. Set this to the number of trusted proxies that append to X-Forwarded-For
# (0 trusts none). Too high a count lets a client spoof its address: verify it on a real request.
ALLAUTH_TRUSTED_PROXY_COUNT = int(os.environ.get("ALLAUTH_TRUSTED_PROXY_COUNT", "0"))
# Alternative for platforms that set a client-address header themselves and replace any value a
# client sends (never one a client can supply). When set, it is used instead of the proxy count.
ALLAUTH_TRUSTED_CLIENT_IP_HEADER = os.environ.get("ALLAUTH_TRUSTED_CLIENT_IP_HEADER") or None
# The failed sign-in counts, and allauth's other limits, are kept in the database, so that every
# server process of the deployment counts the same attempts (core migration 0007 creates the
# table). Each key is stored as a keyed digest (core.cache): neither a name typed at sign-in nor
# a network address is written.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "lab_ops_cache",
        "KEY_FUNCTION": "core.cache.keyed_digest",
        "OPTIONS": {"MAX_ENTRIES": 100000},
    }
}
LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "/"
ACCOUNT_LOGOUT_REDIRECT_URL = "/accounts/login/"
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend"
    if DEBUG
    else "django.core.mail.backends.dummy.EmailBackend",
)
STARTUP_EMAIL_BACKEND = EMAIL_BACKEND
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "true").lower() == "true"
# Seconds to wait for the mail server. A notice that times out is retried by the scheduled jobs
# (jobs.outbox) instead of holding up the request that saved the change.
EMAIL_TIMEOUT = int(os.environ.get("EMAIL_TIMEOUT", "10"))
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@example.invalid")
# The deployment's public address for links in emailed notices (desk.notifications), which are
# sent after a change commits, with no request to take the host from. Unset: Render's
# RENDER_EXTERNAL_URL, then the first non-local ALLOWED_HOSTS entry over https.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
# Emailed one-time sign-in codes; offered only when mail can actually be delivered.
ACCOUNT_LOGIN_BY_CODE_ENABLED = EMAIL_BACKEND != "django.core.mail.backends.dummy.EmailBackend"
ACCOUNT_LOGIN_BY_CODE_TIMEOUT = 300
ACCOUNT_LOGIN_BY_CODE_SUPPORTS_RESEND = True
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TIME_ZONE", "America/New_York")
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []
# Static files and the presentation are served from this origin only, and never inside a frame.
# WhiteNoise answers before the clickjacking middleware runs, so it sets the header itself.
WHITENOISE_ALLOW_ALL_ORIGINS = False


def _static_file_headers(headers, path, url):
    headers["X-Frame-Options"] = "DENY"


WHITENOISE_ADD_HEADERS_FUNCTION = _static_file_headers
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
# The longest a session may last under the laboratory's limits (core.session_limits): the cookie
# and the stored session expire this long after the session was last saved (at sign-in and some
# account steps). The limits themselves are enforced on every request from the sign-in
# (accounts.sessions), never by the cookie's expiry.
SESSION_COOKIE_AGE = 24 * 60 * 60
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
CSRF_FAILURE_VIEW = "core.views.csrf_failure"
SECURE_SSL_REDIRECT = not DEBUG
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# One hour by default so a mistake is short-lived. Raise it (for example 31536000, one year)
# once every host that serves this deployment is known to work over HTTPS only.
SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "3600")) if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = (
    os.environ.get("SECURE_HSTS_INCLUDE_SUBDOMAINS", "false").lower() == "true"
)
X_FRAME_OPTIONS = "DENY"
# Replace the default policy as a whole when a deployment needs different approved sources.
CONTENT_SECURITY_POLICY = os.environ.get("CONTENT_SECURITY_POLICY", DEFAULT_CONTENT_SECURITY_POLICY)
# Requests declaring a larger body are refused before any of it is read. The largest upload a
# form accepts is 10 MiB; the rest is room for the other fields of a multipart form.
MAX_REQUEST_BODY_BYTES = int(os.environ.get("MAX_REQUEST_BODY_BYTES", str(12 * 1024 * 1024)))
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024

FORM_RENDERER = "django.forms.renderers.TemplatesSetting"

# AGPL-3.0 section 13: every page links users to the corresponding source. Deployments running
# modified code must point this at their own published source.
SOURCE_CODE_URL = os.environ.get("SOURCE_CODE_URL", "https://github.com/openbga/lab-ops")


import config.logging as _logging_configuration  # noqa: E402
from config.digest import module_sha256 as _module_sha256  # noqa: E402
from config.logging import LOGGING  # noqa: E402,F401

# The configuration as it loaded: this file's source and the logging configuration's, each read
# through its own loader now (config.digest; core.security_settings compares them).
SETTINGS_SHA256 = _module_sha256(sys.modules.get(__name__))
LOGGING_SHA256 = _module_sha256(_logging_configuration)
