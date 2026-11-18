"""What protects sign-in, sessions and patient information in this deployment, and who sets it
(docs/safeguards.md).

`in_force()` reads the running configuration and the database at the time of the request and
returns facts, not a judgement: nothing here scores, rates or tells a laboratory it complies.
Each fact names where it is set: fixed in this release, a deployment setting, each person, or a
page of the application. Later safeguards items add the settings a laboratory changes; each such
change takes the setting's row lock, checks the revision the form showed, needs a reason and
records one audit event, like the contacts in core.lab_profile.
"""

from dataclasses import dataclass, field

from allauth.account import app_settings as account_settings
from allauth.mfa.models import Authenticator
from django.conf import settings
from django.contrib.auth import get_user_model, password_validation
from django.db.models import Q

from accounts.roles import CAPABILITIES

from .security_headers import DEFAULT_CONTENT_SECURITY_POLICY

SECURITY_ROLES = ("admin",)
PATIENT_CAPABILITY = "view_patient_information"

# source key: how the page names who sets a fact
SOURCES = {
    "application": "Fixed in this release",
    "deployment": "Deployment setting",
    "person": "Each person",
    "accounts": "Accounts & roles",
    "forms": "Ticket forms",
    "unavailable": "Not available yet",
}


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


_UNITS = (("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1))


def duration_text(seconds):
    """`1209600` -> "2 weeks", `300` -> "5 minutes": the largest unit that divides evenly."""
    seconds = int(seconds)
    for name, size in _UNITS:
        if seconds >= size and seconds % size == 0:
            count = seconds // size
            return f"{count} {name}{'' if count == 1 else 's'}"
    return f"{seconds} seconds"


def _within(seconds):
    return "a minute" if int(seconds) == 60 else duration_text(seconds)


def _count(number, singular, plural=None):
    return f"{number} {singular if number == 1 else (plural or singular + 's')}"


_PASSWORD_CHECKS = (
    ("CommonPasswordValidator", "not a common password"),
    ("NumericPasswordValidator", "not only digits"),
    ("UserAttributeSimilarityValidator", "not close to the account's username or email address"),
)


def _password_rule():
    validators = {
        type(validator).__name__: validator
        for validator in password_validation.get_default_password_validators()
    }
    parts = []
    if "MinimumLengthValidator" in validators:
        parts.append(f"at least {validators['MinimumLengthValidator'].min_length} characters")
    parts += [text for name, text in _PASSWORD_CHECKS if name in validators]
    if not parts:
        return "No rule"
    text = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]
    return text[0].upper() + text[1:]


def _failed_sign_ins():
    """The failed sign-in limit as allauth enforces it (ACCOUNT_RATE_LIMITS "login_failed")."""
    rule = account_settings.RATE_LIMITS.get("login_failed")
    if not rule:
        return "No limit", ""
    try:
        from allauth.core.internal.ratelimit import parse_rates

        rates = parse_rates(rule)
    except ImportError, ValueError:
        return rule, ""
    who = {"key": "for one account", "user": "for one account", "ip": "from one address"}
    # The per-account limit first: it is the one a person meets.
    rates.sort(key=lambda rate: rate.per != "key")
    parts = [
        f"{_count(rate.amount, 'failure')} {who.get(rate.per, 'per ' + rate.per)} "
        f"within {_within(rate.duration)}"
        for rate in rates
    ]
    value = "Refused for a time after " + ", or ".join(parts)
    backend = settings.CACHES.get("default", {}).get("BACKEND", "")
    note = "Counted separately by each server process." if backend.endswith("LocMemCache") else ""
    return value, note


def _second_factor_counts():
    users = get_user_model().objects.filter(is_active=True)
    enrolled = Authenticator.objects.exclude(type=Authenticator.Type.RECOVERY_CODES).values(
        "user_id"
    )
    staff = users.filter(client_id__isnull=True)
    portal = users.filter(client_id__isnull=False)
    return (
        staff.filter(pk__in=enrolled).count(),
        staff.count(),
        portal.filter(pk__in=enrolled).count(),
        portal.count(),
    )


def patient_capability_holders():
    """Active staff accounts holding Can view patient information, directly or through a group:
    the accounts accounts.roles.has_capability answers yes for."""
    grant = {"codename": PATIENT_CAPABILITY, "content_type__app_label": "accounts"}
    return (
        get_user_model()
        .objects.filter(is_active=True, client_id__isnull=True)
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


def _sign_in(facts):
    staff_with, staff, portal_with, portal = _second_factor_counts()
    failed, failed_note = _failed_sign_ins()
    by_code = settings.ACCOUNT_LOGIN_BY_CODE_ENABLED
    facts += [
        Fact(
            "accounts",
            "New accounts",
            "Created by the laboratory",
            "application",
            "Nobody can sign up.",
        ),
        Fact("passwords", "Passwords", _password_rule(), "application"),
        Fact(
            "second-factor",
            "Second sign-in factor",
            "Optional",
            "person",
            "Each person can add an authenticator app, with recovery codes. "
            f"Active staff accounts with one: {staff_with} of {staff}. "
            f"Active client portal accounts with one: {portal_with} of {portal}.",
        ),
        Fact("failed-sign-ins", "Failed sign-ins", failed, "application", failed_note),
        Fact(
            "sign-in-code",
            "Sign-in with an emailed code",
            "On" if by_code else "Off",
            "deployment",
            (
                "A code sent to the account's email address takes the place of the password "
                f"and is valid for {duration_text(settings.ACCOUNT_LOGIN_BY_CODE_TIMEOUT)}. "
                "The second factor is still asked for where the account has one."
            )
            if by_code
            else "Available only while the deployment sends email.",
            "EMAIL_BACKEND",
        ),
        Fact(
            "sign-in-records",
            "Record of sign-ins",
            "Application log only",
            "application",
            "Sign-ins, failures and sign-outs are written to the application log with the "
            "username, not stored in the database. The deployment's log retention applies.",
        ),
    ]


def _sessions(facts):
    secure = settings.SESSION_COOKIE_SECURE and settings.SESSION_COOKIE_HTTPONLY
    facts += [
        Fact(
            "session-length",
            "Session length",
            duration_text(settings.SESSION_COOKIE_AGE),
            "application",
            "A session ends this long after it was last saved: at sign-in, and again when the "
            "person changes a display preference.",
        ),
        Fact(
            "inactivity",
            "Sign-out after a period without activity",
            "None",
            "unavailable",
        ),
        Fact(
            "browser-close",
            "Closing the browser",
            "Ends the session"
            if settings.SESSION_EXPIRE_AT_BROWSER_CLOSE
            else "Does not end the session",
            "application",
        ),
        Fact(
            "account-changes",
            "Deactivation or a new password",
            "Ends the account's other sessions",
            "application",
            "A deactivated account is signed out at its next request. A new password signs out "
            "the account's sessions, except the one that set it.",
        ),
        Fact(
            "session-cookie",
            "Session cookie",
            "Sent over HTTPS only and hidden from page scripts"
            if secure
            else "Hidden from page scripts, also sent over plain HTTP",
            "deployment",
            "" if secure else "Debug mode is on: for local development only.",
            "DEBUG",
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
            "Granted to a person or a role group by a superuser. No role includes it.",
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
            "application",
            "The key is revealed one link at a time, to staff who can view patient "
            "information, and each reveal is recorded without the key.",
        ),
        Fact(
            "patient-portal",
            "Client portal",
            "Shown without a reveal",
            "application",
            "A client sees the patient-marked answers of fields clients see on the tickets it "
            "can read. Showing them is not recorded.",
        ),
        Fact(
            "patient-kept-out",
            "Kept out of",
            "Email, lists, search, logs and audit details",
            "application",
        ),
    ]


def _connection(facts):
    if settings.SECURE_SSL_REDIRECT:
        hsts = settings.SECURE_HSTS_SECONDS
        facts.append(
            Fact(
                "https",
                "HTTPS",
                "Required",
                "deployment",
                "Plain HTTP requests are redirected"
                + (
                    f", and browsers are told to use only HTTPS for {duration_text(hsts)}."
                    if hsts
                    else "."
                ),
                "SECURE_HSTS_SECONDS",
            )
        )
    else:
        facts.append(
            Fact(
                "https",
                "HTTPS",
                "Not required",
                "deployment",
                "Debug mode is on: for local development only.",
                "DEBUG",
            )
        )
    replaced = settings.CONTENT_SECURITY_POLICY != DEFAULT_CONTENT_SECURITY_POLICY
    facts.append(
        Fact(
            "content-security-policy",
            "Content security policy",
            "Replaced by the deployment" if replaced else "The application's default",
            "deployment",
            "The deployment's policy decides what pages may load."
            if replaced
            else "Pages load scripts, styles and images only from the application.",
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
