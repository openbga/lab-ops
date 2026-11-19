"""What protects sign-in, sessions and patient information in this deployment, and who sets it
(docs/safeguards.md).

`in_force()` reads the running configuration and the database at the time of the request and
returns facts, not a judgement: nothing here scores, rates or tells a laboratory it complies.

Each fact describes this release's configuration (config/settings.py) and follows the deployment
settings that configuration reads from the environment (debug mode, HSTS, the content security
policy, the email backend). The configuration settings each fact depends on are listed with
their values in this release (RELEASE). Where a deployment has changed one of them, the fact says
so, names the setting and describes nothing further: describing every possible Django and allauth
configuration is not this page's job, and a guess would mislead.

Each fact names where it is set: this release's code, the application's configuration, a
deployment setting (an environment variable), each person, or a page of the application. Later
safeguards items add the settings a laboratory changes; each such change takes the setting's row
lock, checks the revision the form showed, rereads the acting account, needs a reason and records
one audit event, like the contacts in core.lab_profile.
"""

import contextvars
import hashlib
import logging
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path

from allauth import app_settings as allauth_settings
from allauth.account import app_settings as account_settings
from allauth.mfa import app_settings as mfa_settings
from allauth.mfa.models import Authenticator
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Count, Exists, OuterRef, Q

from accounts.roles import CAPABILITIES, ROLES

from .security_headers import DEFAULT_CONTENT_SECURITY_POLICY

SECURITY_ROLES = ("admin",)
PATIENT_CAPABILITY = "view_patient_information"

# source key: how the page names where a fact is set
SOURCES = {
    "release": "This release",
    "configuration": "Application configuration",
    "deployment": "Deployment setting",
    "person": "Each person",
    "accounts": "Accounts & roles",
    "forms": "Ticket forms",
    "unavailable": "Not available yet",
}
CHANGED = "Changed in the application configuration"
SETTINGS_MODULE = "config.settings"
# SHA-256 of the two files this release's configuration is written in. A deployment that edits
# either changes code the facts below rely on, so every fact that depends on the configuration
# says so and names the file. Whoever changes either file in this repository checks each fact
# against it and updates the value (tests/test_security_settings.py prints it).
RELEASE_FILES = ("config/settings.py", "config/logging.py")
RELEASE_SETTINGS_SHA256 = "4673a44c6183e0545e74a7801558dcda253f75a0c846dc64248aa78d76c0d462"
RELEASE_LOGGING_SHA256 = "ca4aaff0aa4cc877d50260cad2bbd5918b5cfbf88d26cbaa5b50e4e888e99fe0"
MODULE = "settings module"
FILES = (MODULE, *RELEASE_FILES)


def settings_module_name():
    """The settings module Django loaded, from the loaded settings themselves. While a test
    overrides settings, the override's SETTINGS_MODULE is None and the loaded module is the one
    beneath it; the environment variable can change after start-up and is not read."""
    settings.INSTALLED_APPS  # noqa: B018 - make sure the settings are loaded
    layer = getattr(settings, "_wrapped", None)
    while layer is not None and not getattr(layer, "SETTINGS_MODULE", None):
        layer = getattr(layer, "default_settings", None)
    return getattr(layer, "SETTINGS_MODULE", "") or ""


_digests = {}


def module_digest(module_name):
    """SHA-256 of a module's source, or "" when it cannot be read now. Only a digest that was
    read is kept: a passing failure to read is tried again next time."""
    if module_name not in _digests:
        try:
            data = Path(import_module(module_name).__file__).read_bytes()
        except Exception:  # a file that cannot be read is not this release's
            return ""
        _digests[module_name] = hashlib.sha256(data).hexdigest()
    return _digests[module_name]


def settings_file_digest():
    """SHA-256 of config/settings.py as loaded (kept for the release pin test)."""
    return module_digest(SETTINGS_MODULE)


def _writes_info(name):
    """Whether the running logger `name` passes an INFO record to at least one handler that
    accepts it, following propagation as logging does."""
    logger = logging.getLogger(name)
    if not logger.isEnabledFor(logging.INFO):
        return False
    current = logger
    while current is not None:
        if any(handler.level <= logging.INFO for handler in current.handlers):
            return True
        if not current.propagate:
            return False
        current = current.parent
    return False


def _startup(name):
    """A setting's value as the settings module computed it at startup (from the environment),
    before anything in the running process overrode it."""
    return getattr(import_module(SETTINGS_MODULE), name)


_VALIDATORS = "django.contrib.auth.password_validation."
# setting name: (how to read it now, its value in this release's configuration)
RELEASE = {
    # The settings module and the two files of the configuration: any other change to it.
    MODULE: (settings_module_name, SETTINGS_MODULE),
    "config/settings.py": (lambda: module_digest("config.settings"), RELEASE_SETTINGS_SHA256),
    "config/logging.py": (lambda: module_digest("config.logging"), RELEASE_LOGGING_SHA256),
    "MIDDLEWARE": (
        lambda: list(settings.MIDDLEWARE),
        [
            "core.security_headers.SecurityHeadersMiddleware",
            "core.request_logging.RequestLogMiddleware",
            "core.request_limits.RequestBodyLimitMiddleware",
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
            "django.middleware.clickjacking.XFrameOptionsMiddleware",
            "core.form_dialog.FormDialogMiddleware",
        ],
    ),
    # The configuration as loaded, and the sign-in logger as it runs (its level and its
    # handlers' levels can be changed in place).
    "LOGGING": (
        lambda: settings.LOGGING == _startup("LOGGING") and _writes_info("lab_ops.signin"),
        True,
    ),
    "ACCOUNT_ADAPTER": (lambda: settings.ACCOUNT_ADAPTER, "accounts.adapter.ClosedSignupAdapter"),
    "ACCOUNT_LOGIN_METHODS": (lambda: set(account_settings.LOGIN_METHODS), {"username", "email"}),
    # Follows EMAIL_BACKEND at startup in this release.
    "ACCOUNT_LOGIN_BY_CODE_ENABLED": (
        lambda: (
            account_settings.LOGIN_BY_CODE_ENABLED
            == (settings.STARTUP_EMAIL_BACKEND != f"{_MAIL}dummy.EmailBackend")
        ),
        True,
    ),
    "ACCOUNT_LOGIN_BY_CODE_TRUST_ENABLED": (
        lambda: account_settings.LOGIN_BY_CODE_TRUST_ENABLED,
        False,
    ),
    "MFA_RECOVERY_CODE_COUNT": (lambda: mfa_settings.RECOVERY_CODE_COUNT, 10),
    "AUTHENTICATION_BACKENDS": (
        lambda: list(settings.AUTHENTICATION_BACKENDS),
        [
            "django.contrib.auth.backends.ModelBackend",
            "allauth.account.auth_backends.AuthenticationBackend",
        ],
    ),
    "AUTH_PASSWORD_VALIDATORS": (
        lambda: [dict(entry) for entry in settings.AUTH_PASSWORD_VALIDATORS],
        [
            {"NAME": f"{_VALIDATORS}UserAttributeSimilarityValidator"},
            {"NAME": f"{_VALIDATORS}MinimumLengthValidator"},
            {"NAME": f"{_VALIDATORS}CommonPasswordValidator"},
            {"NAME": f"{_VALIDATORS}NumericPasswordValidator"},
        ],
    ),
    "MFA_SUPPORTED_TYPES": (
        lambda: sorted(mfa_settings.SUPPORTED_TYPES),
        ["recovery_codes", "totp"],
    ),
    "MFA_TRUST_ENABLED": (lambda: mfa_settings.TRUST_ENABLED, False),
    "MFA_REAUTHENTICATION_REQUIRED": (lambda: mfa_settings.REAUTHENTICATION_REQUIRED, True),
    "ACCOUNT_REAUTHENTICATION_TIMEOUT": (lambda: account_settings.REAUTHENTICATION_TIMEOUT, 300),
    "ACCOUNT_RATE_LIMITS": (
        lambda: account_settings.RATE_LIMITS.get("login_failed"),
        "10/m/ip,5/300s/key",
    ),
    "ALLAUTH_RATE_LIMIT_IPV6_PREFIX": (lambda: allauth_settings.RATE_LIMIT_IPV6_PREFIX, 64),
    "CACHES": (
        lambda: settings.CACHES,
        {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    ),
    "ACCOUNT_LOGIN_BY_CODE_TIMEOUT": (lambda: account_settings.LOGIN_BY_CODE_TIMEOUT, 300),
    "SESSION_ENGINE": (lambda: settings.SESSION_ENGINE, "django.contrib.sessions.backends.db"),
    "SESSION_COOKIE_AGE": (lambda: settings.SESSION_COOKIE_AGE, 1209600),
    # allauth's own session age for a remembered password sign-in (deprecated; defaults to
    # Django's): equal to SESSION_COOKIE_AGE in this release.
    "ACCOUNT_SESSION_COOKIE_AGE": (
        lambda: account_settings.SESSION_COOKIE_AGE == settings.SESSION_COOKIE_AGE,
        True,
    ),
    "SESSION_SAVE_EVERY_REQUEST": (lambda: settings.SESSION_SAVE_EVERY_REQUEST, False),
    "SESSION_EXPIRE_AT_BROWSER_CLOSE": (lambda: settings.SESSION_EXPIRE_AT_BROWSER_CLOSE, False),
    "ACCOUNT_SESSION_REMEMBER": (lambda: account_settings.SESSION_REMEMBER, None),
    "ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE": (
        lambda: account_settings.LOGOUT_ON_PASSWORD_CHANGE,
        False,
    ),
    "SESSION_COOKIE_HTTPONLY": (lambda: settings.SESSION_COOKIE_HTTPONLY, True),
    # Both follow debug mode at startup in this release: on exactly when debug mode is off.
    "SESSION_COOKIE_SECURE": (
        lambda: settings.SESSION_COOKIE_SECURE == (not settings.STARTUP_DEBUG),
        True,
    ),
    "SECURE_SSL_REDIRECT": (
        lambda: settings.SECURE_SSL_REDIRECT == (not settings.STARTUP_DEBUG),
        True,
    ),
    "SECURE_REDIRECT_EXEMPT": (lambda: list(settings.SECURE_REDIRECT_EXEMPT), []),
    # Debug mode sets HSTS to 0 in this release; HSTS is sent only with the redirect on.
    "SECURE_HSTS_SECONDS": (
        lambda: settings.SECURE_SSL_REDIRECT or not settings.SECURE_HSTS_SECONDS,
        True,
    ),
}


def changed_settings(*names):
    """Which of `names` differ from this release's configuration (RELEASE), in that order."""
    differ = []
    for name in names:
        read, release = RELEASE[name]
        try:
            current = read()
        except Exception:  # a setting so changed it cannot be read is changed
            differ.append(name)
            continue
        if current != release:
            differ.append(name)
    return differ


@dataclass(frozen=True)
class Fact:
    key: str
    label: str
    value: str
    source: str
    note: str = ""
    # The deployment setting (environment variable) behind a "deployment" fact.
    setting: str = ""

    @property
    def source_label(self):
        return SOURCES[self.source]


@dataclass(frozen=True)
class Group:
    key: str
    title: str
    facts: list = field(default_factory=list)


# The settings module and the configuration files, read once for every fact of one in_force().
_files_reading = contextvars.ContextVar("security_settings_files", default=None)
_FILE_MODULES = (("config/settings.py", "config.settings"), ("config/logging.py", "config.logging"))


def _read_files():
    """{MODULE: loaded settings module name, file name: its digest ("" when unreadable)}."""
    return {
        MODULE: settings_module_name(),
        **{name: module_digest(module) for name, module in _FILE_MODULES},
    }


def _files_differ(reading):
    return [name for name in FILES if reading[name] != RELEASE[name][1]]


def _file_reasons(reading, differ):
    """Why the settings module or a configuration file differs, from the same reading."""
    reasons = []
    if MODULE in differ:
        reasons.append(
            f"this deployment loads its settings from {reading[MODULE]}"
            if reading[MODULE]
            else "this deployment's settings were configured without a settings module"
        )
    for name, _module in _FILE_MODULES:
        if name in differ:
            reasons.append(
                f"this deployment's {name} differs from it"
                if reading[name]
                else f"this deployment's {name} could not be read"
            )
    return reasons


def _fact(key, label, depends_on, build):
    """The fact `build()` returns, or the changed-configuration fact when a setting it depends
    on differs from this release's."""
    reading = _files_reading.get() or _read_files()
    differ = _files_differ(reading) + changed_settings(*depends_on)
    if not differ:
        return build()
    files = _file_reasons(reading, differ)
    names = [name for name in differ if name not in FILES]
    note = "This page describes this release's configuration"
    note += (", and " + " and ".join(files) + ".") if files else "."
    if names:
        note += f" Check {_either(names)} in config/settings.py."
    return Fact(key, label, CHANGED, "configuration", note)


_UNITS = (("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1))


def duration_text(seconds):
    """`1209600` -> "2 weeks", `300` -> "5 minutes": the largest unit that divides evenly."""
    seconds = int(seconds)
    for name, size in _UNITS:
        if seconds >= size and seconds % size == 0:
            count = seconds // size
            return f"{count} {name}{'' if count == 1 else 's'}"
    return f"{seconds} seconds"


def _count(number, singular, plural=None):
    return f"{number} {singular if number == 1 else (plural or singular + 's')}"


def _either(names):
    """["a", "b", "c"] -> "a, b or c"."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _second_factor_counts():
    """(staff with a second factor, active staff, portal with one, active portal), counted in one
    statement so the four numbers describe one reading of the accounts."""
    enrolled = Exists(
        Authenticator.objects.filter(user=OuterRef("pk")).exclude(
            type=Authenticator.Type.RECOVERY_CODES
        )
    )
    counts = (
        get_user_model()
        .objects.filter(is_active=True)
        .annotate(enrolled=enrolled)
        .aggregate(
            staff_with=Count("pk", filter=Q(client_id__isnull=True, enrolled=True)),
            staff=Count("pk", filter=Q(client_id__isnull=True)),
            portal_with=Count("pk", filter=Q(client_id__isnull=False, enrolled=True)),
            portal=Count("pk", filter=Q(client_id__isnull=False)),
        )
    )
    return counts["staff_with"], counts["staff"], counts["portal_with"], counts["portal"]


def patient_capability_holders():
    """Active staff accounts that can view patient information: a laboratory role and the
    explicit grant, directly or through a group (request_forms.selectors.
    may_see_patient_information). A grant to an account without a role is not counted: it
    opens nothing."""
    grant = {"codename": PATIENT_CAPABILITY, "content_type__app_label": "accounts"}
    return (
        get_user_model()
        .objects.filter(is_active=True, client_id__isnull=True, groups__name__in=ROLES)
        .filter(
            Q(**{f"user_permissions__{key}": value for key, value in grant.items()})
            | Q(**{f"groups__permissions__{key}": value for key, value in grant.items()})
        )
        .distinct()
        .count()
    )


def _patient_fields():
    from request_forms.models import TemplateField

    return TemplateField.objects.filter(is_active=True, holds_patient_information=True).count()


_MAIL = "django.core.mail.backends."


def _delivery():
    """Where an emailed code goes, from the exact backend configured (EMAIL_BACKEND)."""
    backend = settings.EMAIL_BACKEND
    if backend == f"{_MAIL}smtp.EmailBackend":
        return ""
    if backend == f"{_MAIL}console.EmailBackend":
        return "This deployment writes email to its console instead of sending it."
    if backend == f"{_MAIL}locmem.EmailBackend":
        return "This deployment keeps email in memory instead of sending it."
    if backend == f"{_MAIL}filebased.EmailBackend":
        if getattr(settings, "EMAIL_FILE_PATH", None):
            return "This deployment writes email to files instead of sending it."
        return "This deployment cannot send email: EMAIL_FILE_PATH is not set."
    return "Whether the code reaches the address depends on this deployment's email backend."


def _sign_in_code():
    if not account_settings.LOGIN_BY_CODE_ENABLED:
        return Fact(
            "sign-in-code",
            "Sign-in with an emailed code",
            "Off",
            "deployment",
            "Available only while the deployment sends email.",
            "EMAIL_BACKEND",
        )
    note = (
        "A code emailed to the account's address takes the place of the password and is valid "
        f"for {duration_text(account_settings.LOGIN_BY_CODE_TIMEOUT)}. The second factor is "
        "still asked for where the account has one."
    )
    kept = _delivery()
    return Fact(
        "sign-in-code",
        "Sign-in with an emailed code",
        "On",
        "deployment",
        f"{note} {kept}".strip(),
        "EMAIL_BACKEND",
    )


def _sign_in(facts):
    staff_with, staff, portal_with, portal = _second_factor_counts()
    facts += [
        _fact(
            "accounts",
            "New accounts",
            ["ACCOUNT_ADAPTER"],
            lambda: Fact(
                "accounts",
                "New accounts",
                "Created by the laboratory",
                "release",
                "Nobody can sign up.",
            ),
        ),
        _fact(
            "passwords",
            "Passwords",
            ["AUTH_PASSWORD_VALIDATORS", "ACCOUNT_ADAPTER"],
            lambda: Fact(
                "passwords",
                "Passwords",
                "At least 8 characters, not a common password, not only digits and not close to "
                "the account's username, first name, last name or email address",
                "configuration",
                "The account pages and the admin console's password form check the same rule.",
            ),
        ),
        _fact(
            "second-factor",
            "Second sign-in factor",
            ["MFA_SUPPORTED_TYPES", "MFA_RECOVERY_CODE_COUNT", "MIDDLEWARE"],
            lambda: Fact(
                "second-factor",
                "Second sign-in factor",
                "Optional",
                "person",
                "An account's holder can add an authenticator app, with recovery codes. Shared "
                "demonstration accounts cannot. "
                f"Active staff accounts with one: {staff_with} of {staff}. "
                f"Active client portal accounts with one: {portal_with} of {portal}.",
            ),
        ),
        _fact(
            "second-factor-changes",
            "Changing the second factor",
            [
                "MFA_REAUTHENTICATION_REQUIRED",
                "ACCOUNT_REAUTHENTICATION_TIMEOUT",
                "MFA_SUPPORTED_TYPES",
            ],
            lambda: Fact(
                "second-factor-changes",
                "Changing the second factor",
                "Needs a recent re-authentication",
                "configuration",
                "Adding or removing an authenticator app, or seeing or renewing recovery codes, "
                "asks a person with a usable password or a second factor to confirm who they are "
                "again unless they did so in the last 5 minutes. An account with neither has no "
                "method to confirm with and is not asked.",
            ),
        ),
        _fact(
            "failed-sign-ins",
            "Failed sign-ins",
            [
                "ACCOUNT_RATE_LIMITS",
                "ALLAUTH_RATE_LIMIT_IPV6_PREFIX",
                "CACHES",
                "ACCOUNT_LOGIN_METHODS",
            ],
            lambda: Fact(
                "failed-sign-ins",
                "Failed sign-ins",
                "Refused for a time after 5 failures within 5 minutes for one username or email "
                "address, or 10 failures within a minute from one network address",
                "configuration",
                "A username and the account's email address are counted separately. An IPv6 /64 "
                "network counts as one address. Each server process keeps its own count.",
            ),
        ),
        _fact(
            "sign-in-code",
            "Sign-in with an emailed code",
            [
                "ACCOUNT_LOGIN_BY_CODE_ENABLED",
                "ACCOUNT_LOGIN_BY_CODE_TIMEOUT",
                "MFA_TRUST_ENABLED",
                "ACCOUNT_LOGIN_BY_CODE_TRUST_ENABLED",
            ],
            _sign_in_code,
        ),
        _fact(
            "sign-in-records",
            "Record of sign-ins",
            ["SESSION_ENGINE", "LOGGING"],
            lambda: Fact(
                "sign-in-records",
                "Record of sign-ins",
                "Application log, and the last sign-in time",
                "release",
                "Sign-ins, failures and sign-outs are written to the application log, with the "
                "username when the account is known; the deployment's log retention applies. "
                "The database keeps each account's last sign-in time and, in each session while "
                "it lasts, how it signed in. There is no stored list of sign-ins.",
            ),
        ),
    ]


def _sessions(facts):
    facts += [
        _fact(
            "session-length",
            "Session length",
            ["SESSION_COOKIE_AGE", "ACCOUNT_SESSION_COOKIE_AGE", "SESSION_SAVE_EVERY_REQUEST"],
            lambda: Fact(
                "session-length",
                "Session length",
                duration_text(settings.SESSION_COOKIE_AGE),
                "configuration",
                "A session ends this long after it was last saved: at sign-in, and again when "
                "the person changes a display preference.",
            ),
        ),
        _fact(
            "inactivity",
            "Sign-out after a period without activity",
            ["SESSION_SAVE_EVERY_REQUEST"],
            lambda: Fact(
                "inactivity", "Sign-out after a period without activity", "None", "unavailable"
            ),
        ),
        _fact(
            "browser-close",
            "Closing the browser",
            [
                "ACCOUNT_SESSION_REMEMBER",
                "SESSION_EXPIRE_AT_BROWSER_CLOSE",
                "ACCOUNT_LOGIN_BY_CODE_ENABLED",
            ],
            lambda: Fact(
                "browser-close",
                "Closing the browser",
                "Ends a password sign-in, unless Remember me was ticked"
                + (
                    "; does not end a sign-in with an emailed code"
                    if account_settings.LOGIN_BY_CODE_ENABLED
                    else ""
                ),
                "person",
                "Some browsers keep a session when they reopen the last one.",
            ),
        ),
        _fact(
            "deactivation",
            "Deactivating an account",
            ["AUTHENTICATION_BACKENDS"],
            lambda: Fact(
                "deactivation",
                "Deactivating an account",
                "Refused at its next request",
                "release",
                "The account's sessions are not deleted: if it is made active again before they "
                "expire, those browsers are signed in again.",
            ),
        ),
        _fact(
            "new-password",
            "A new password",
            ["ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE"],
            lambda: Fact(
                "new-password",
                "A new password",
                "Signs out the account's other sessions",
                "configuration",
                "Whoever changes their own password, on the account pages or in the admin "
                "console, stays signed in where they changed it. A password a superuser sets for "
                "another account signs out every session of that account.",
            ),
        ),
        _fact(
            "session-cookie",
            "Session cookie",
            ["SESSION_COOKIE_HTTPONLY", "SESSION_COOKIE_SECURE"],
            lambda: Fact(
                "session-cookie",
                "Session cookie",
                "Sent over HTTPS only and hidden from page scripts"
                if settings.SESSION_COOKIE_SECURE
                else "Also sent over plain HTTP and hidden from page scripts",
                "deployment",
                ""
                if settings.SESSION_COOKIE_SECURE
                else "In this release's configuration that happens only in debug mode, for local "
                "development.",
                "DEBUG",
            ),
        ),
    ]


def _patient(facts):
    holders = patient_capability_holders()
    marked = _patient_fields()
    facts += [
        Fact(
            "patient-capability",
            CAPABILITIES[PATIENT_CAPABILITY],
            _count(holders, "active staff account"),
            "accounts",
            "A superuser grants it to a person or to a role group. A role has it only when its "
            "group was given it. Counted: active staff accounts with a laboratory role that "
            "hold it.",
        ),
        Fact(
            "patient-fields",
            "Patient fields on ticket forms",
            _count(marked, "active field"),
            "forms",
            "An administrator marks a field as holding patient information. Staff who can view "
            "patient information reveal its values one at a time, and each reveal is recorded.",
        ),
        Fact(
            "person-links",
            "Person-level links",
            "Masked",
            "release",
            "The key is revealed one link at a time, to staff who can view patient "
            "information, and each reveal is recorded without the key.",
        ),
        Fact(
            "patient-portal",
            "Client portal",
            "Shown without a reveal",
            "release",
            "A client sees the patient-marked answers of fields clients see on the tickets it "
            "can read. Showing them is not recorded.",
        ),
        Fact(
            "patient-kept-out",
            "Kept out of",
            "Email, lists, logs, audit details and general search",
            "release",
            "Staff who can view patient information can find a person-level link by its exact "
            "key; each such search is recorded without the key.",
        ),
    ]


def _https():
    if settings.SECURE_SSL_REDIRECT:
        hsts = settings.SECURE_HSTS_SECONDS
        if hsts > 0:
            told = (
                f"Browsers are told to use only HTTPS for {duration_text(hsts)} "
                "(SECURE_HSTS_SECONDS)."
            )
        elif hsts == 0:
            told = "Browsers are not told to keep using HTTPS (SECURE_HSTS_SECONDS is 0)."
        else:
            told = (
                "The value sent to tell browsers to keep using HTTPS is invalid, so browsers "
                "ignore it (SECURE_HSTS_SECONDS is negative)."
            )
        note = "Plain HTTP requests are redirected. " + told
        return Fact("https", "HTTPS", "Required", "deployment", note, "DEBUG")
    return Fact(
        "https",
        "HTTPS",
        "Not required",
        "deployment",
        "Plain HTTP requests are not redirected. In this release's configuration that happens "
        "only in debug mode, for local development.",
        "DEBUG",
    )


def _connection(facts):
    facts.append(
        _fact(
            "https",
            "HTTPS",
            [
                "SECURE_SSL_REDIRECT",
                "SECURE_REDIRECT_EXEMPT",
                "SECURE_HSTS_SECONDS",
                "MIDDLEWARE",
            ],
            _https,
        )
    )
    facts.append(
        _fact("content-security-policy", "Content security policy", ["MIDDLEWARE"], _policy)
    )


def _policy():
    # As SecurityHeadersMiddleware sends it: whitespace runs are one space, blank is no header.
    policy = " ".join((settings.CONTENT_SECURITY_POLICY or "").split())
    if not policy:
        value, note = "None", "The deployment set an empty policy, so pages may load anything."
    elif policy == " ".join(DEFAULT_CONTENT_SECURITY_POLICY.split()):
        value = "The application's default"
        note = (
            "Pages load scripts, styles and fonts only from the application, and images from "
            "the application or embedded in the page (data:)."
        )
    else:
        value = "Replaced by the deployment"
        note = "The deployment's policy decides what pages may load."
    return Fact(
        "content-security-policy",
        "Content security policy",
        value,
        "deployment",
        note,
        "CONTENT_SECURITY_POLICY",
    )


def in_force():
    """Groups of facts, in page order: sign-in, sessions, patient information, connection. The
    settings module and configuration files are read once for all of them."""
    token = _files_reading.set(_read_files())
    try:
        return _groups()
    finally:
        _files_reading.reset(token)


def _groups():
    groups = []
    for key, title, fill in (
        ("sign-in", "Sign-in", _sign_in),
        ("sessions", "Sessions", _sessions),
        ("patient-information", "Patient information", _patient),
        ("connection", "Connection", _connection),
    ):
        facts = []
        fill(facts)
        groups.append(Group(key, title, facts))
    return groups
