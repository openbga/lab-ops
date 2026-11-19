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

from dataclasses import dataclass, field

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

_VALIDATORS = "django.contrib.auth.password_validation."
# setting name: (how to read it now, its value in this release's configuration)
RELEASE = {
    "ACCOUNT_ADAPTER": (lambda: settings.ACCOUNT_ADAPTER, "accounts.adapter.ClosedSignupAdapter"),
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
        lambda: settings.CACHES.get("default", {}).get("BACKEND"),
        "django.core.cache.backends.locmem.LocMemCache",
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
    # Both follow debug mode in this release: secure cookie exactly when HTTP is redirected.
    "SESSION_COOKIE_SECURE": (
        lambda: settings.SESSION_COOKIE_SECURE == settings.SECURE_SSL_REDIRECT,
        True,
    ),
    "SECURE_SSL_REDIRECT": (
        lambda: settings.SECURE_SSL_REDIRECT == settings.SESSION_COOKIE_SECURE,
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


def _fact(key, label, depends_on, build):
    """The fact `build()` returns, or the changed-configuration fact when a setting it depends
    on differs from this release's."""
    differ = changed_settings(*depends_on)
    if differ:
        return Fact(
            key,
            label,
            CHANGED,
            "configuration",
            "This page describes this release's configuration. Check "
            + _either(differ)
            + " in config/settings.py.",
        )
    return build()


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


_MAIL_KEPT = {
    "console": "This deployment writes email to its console instead of sending it.",
    "filebased": "This deployment writes email to files instead of sending it.",
    "locmem": "This deployment keeps email in memory instead of sending it.",
}


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
    kept = next(
        (text for name, text in _MAIL_KEPT.items() if f".{name}." in settings.EMAIL_BACKEND), ""
    )
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
            ["AUTH_PASSWORD_VALIDATORS"],
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
            ["MFA_SUPPORTED_TYPES"],
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
            ["MFA_REAUTHENTICATION_REQUIRED", "ACCOUNT_REAUTHENTICATION_TIMEOUT"],
            lambda: Fact(
                "second-factor-changes",
                "Changing the second factor",
                "Needs a recent re-authentication",
                "configuration",
                "Adding or removing an authenticator app, or seeing or renewing recovery codes, "
                "asks the person to confirm who they are again unless they did so in the last 5 "
                "minutes.",
            ),
        ),
        _fact(
            "failed-sign-ins",
            "Failed sign-ins",
            ["ACCOUNT_RATE_LIMITS", "ALLAUTH_RATE_LIMIT_IPV6_PREFIX", "CACHES"],
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
            ["ACCOUNT_LOGIN_BY_CODE_TIMEOUT", "MFA_TRUST_ENABLED"],
            _sign_in_code,
        ),
        _fact(
            "sign-in-records",
            "Record of sign-ins",
            ["SESSION_ENGINE"],
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
            ["ACCOUNT_SESSION_REMEMBER", "SESSION_EXPIRE_AT_BROWSER_CLOSE"],
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
        note = "Plain HTTP requests are redirected. " + (
            f"Browsers are told to use only HTTPS for {duration_text(hsts)} (SECURE_HSTS_SECONDS)."
            if hsts
            else "Browsers are not told to keep using HTTPS (SECURE_HSTS_SECONDS is 0)."
        )
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
            ["SECURE_SSL_REDIRECT", "SECURE_REDIRECT_EXEMPT", "SECURE_HSTS_SECONDS"],
            _https,
        )
    )
    policy = (settings.CONTENT_SECURITY_POLICY or "").strip()
    if not policy:
        value, note = "None", "The deployment set an empty policy, so pages may load anything."
    elif policy == DEFAULT_CONTENT_SECURITY_POLICY:
        value = "The application's default"
        note = (
            "Pages load scripts, styles and fonts only from the application, and images from "
            "the application or embedded in the page (data:)."
        )
    else:
        value = "Replaced by the deployment"
        note = "The deployment's policy decides what pages may load."
    facts.append(
        Fact(
            "content-security-policy",
            "Content security policy",
            value,
            "deployment",
            note,
            "CONTENT_SECURITY_POLICY",
        )
    )


def in_force():
    """Groups of facts, in page order: sign-in, sessions, patient information, connection."""
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
