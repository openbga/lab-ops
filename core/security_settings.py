"""What protects sign-in, sessions and patient information in this deployment, and who sets it
(docs/safeguards.md).

`in_force()` reads the running configuration and the database at the time of the request and
returns facts, not a judgement: nothing here scores, rates or tells a laboratory it complies.
Each fact is worked out from the settings that enforce it (Django, allauth and this
application's own), so a changed setting changes the fact; where a setting cannot be read, the
fact says so rather than repeating it. Each fact names where it is set: this release's code,
the application's configuration (config/settings.py: changing it changes the code), a
deployment setting (an environment variable), each person, or a page of the application.

Later safeguards items add the settings a laboratory changes; each such change takes the
setting's row lock, checks the revision the form showed, rereads the acting account, needs a
reason and records one audit event, like the contacts in core.lab_profile.
"""

from dataclasses import dataclass, field

from allauth import app_settings as allauth_settings
from allauth.account import app_settings as account_settings
from allauth.mfa import app_settings as mfa_settings
from allauth.mfa.models import Authenticator
from django.conf import settings
from django.contrib.auth import get_user_model, password_validation
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
    """`1209600` -> "2 weeks", `300` -> "5 minutes": the largest unit that divides evenly; a
    period that is not a whole number of seconds is given in seconds as it is."""
    if seconds != int(seconds):
        return f"{float(seconds):g} seconds"
    seconds = int(seconds)
    for name, size in _UNITS:
        if seconds >= size and seconds % size == 0:
            count = seconds // size
            return f"{count} {name}{'' if count == 1 else 's'}"
    return f"{seconds} seconds"


def _within(seconds):
    return "a minute" if seconds == 60 else duration_text(seconds)


def _count(number, singular, plural=None):
    return f"{number} {singular if number == 1 else (plural or singular + 's')}"


def _either(names):
    """["a", "b", "c"] -> "a, b or c"."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _sentence(parts):
    text = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]
    return text[0].upper() + text[1:]


_PASSWORD_CHECKS = {
    "CommonPasswordValidator": "not a common password",
    "NumericPasswordValidator": "not only digits",
}
_ATTRIBUTE_NAMES = {
    "username": "username",
    "first_name": "first name",
    "last_name": "last name",
    "email": "email address",
}


def _password_rule():
    """The rule every password form applies: allauth's own minimum (used only while Django has
    no validators) and each configured validator. A validator this page does not know is
    counted, never left out."""
    validators = password_validation.get_default_password_validators()
    lengths = [account_settings.PASSWORD_MIN_LENGTH or 0]
    parts, others = [], 0
    for validator in validators:
        name = type(validator).__name__
        if name == "MinimumLengthValidator":
            lengths.append(validator.min_length)
        elif name in _PASSWORD_CHECKS:
            parts.append(_PASSWORD_CHECKS[name])
        elif name == "UserAttributeSimilarityValidator":
            attributes = [
                _ATTRIBUTE_NAMES.get(attribute, attribute.replace("_", " "))
                for attribute in validator.user_attributes
            ]
            if attributes:
                parts.append("not close to the account's " + _either(attributes))
        else:
            others += 1
    if max(lengths):
        parts.insert(0, f"at least {max(lengths)} characters")
    if others:
        parts.append(_count(others, "other check") + " set in the configuration")
    if not parts:
        return "Any password"
    return _sentence(parts)


_PER = {
    "key": "for one username or email address",
    "user": "for one account",
}


def _failed_sign_ins():
    """The failed sign-in limit as allauth enforces it (ACCOUNT_RATE_LIMITS "login_failed"),
    counted in the default cache."""
    backend = settings.CACHES.get("default", {}).get("BACKEND", "")
    rule = account_settings.RATE_LIMITS.get("login_failed")
    if not rule:
        return "No limit", ""
    if backend.endswith("DummyCache"):
        return (
            "Not enforced",
            "The deployment's cache keeps no counters, so failures are not counted.",
        )
    try:
        from allauth.core.internal.ratelimit import parse_rates

        rates = parse_rates(rule)
    except ImportError, ValueError, TypeError, AttributeError:
        return "Set but unreadable", "The configured limit could not be read, so it is not shown."
    if not rates:
        return "No limit", ""
    prefix = allauth_settings.RATE_LIMIT_IPV6_PREFIX
    who = {**_PER, "ip": f"from one network address (an IPv6 /{prefix} network counts as one)"}
    # The per-name limit first: it is the one a person meets.
    rates.sort(key=lambda rate: rate.per != "key")
    parts = [
        f"{_count(rate.amount, 'failure')} within {_within(rate.duration)} "
        f"{who.get(rate.per, 'per ' + rate.per)}"
        for rate in rates
    ]
    note = "A username and the account's email address are counted separately."
    if backend.endswith("LocMemCache"):
        note += " Each server process keeps its own count."
    return "Refused for a time after " + ", or ".join(parts), note


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
    by_code = account_settings.LOGIN_BY_CODE_ENABLED
    if not by_code:
        note = "Available only while the deployment sends email."
    else:
        note = (
            "A code emailed to the account's address takes the place of the password and is "
            f"valid for {duration_text(account_settings.LOGIN_BY_CODE_TIMEOUT)}. The second "
            "factor is still asked for where the account has one."
        )
        kept = next(
            (text for name, text in _MAIL_KEPT.items() if f".{name}." in settings.EMAIL_BACKEND),
            "",
        )
        if kept:
            note += " " + kept
    return Fact(
        "sign-in-code",
        "Sign-in with an emailed code",
        "On" if by_code else "Off",
        "deployment",
        note,
        "EMAIL_BACKEND",
    )


def _sign_in(facts):
    staff_with, staff, portal_with, portal = _second_factor_counts()
    failed, failed_note = _failed_sign_ins()
    kinds = (
        "an authenticator app or a security key"
        if "webauthn" in mfa_settings.SUPPORTED_TYPES
        else "an authenticator app"
    )
    facts += [
        Fact(
            "accounts",
            "New accounts",
            "Created by the laboratory",
            "release",
            "Nobody can sign up.",
        ),
        Fact("passwords", "Passwords", _password_rule(), "configuration"),
        Fact(
            "second-factor",
            "Second sign-in factor",
            "Optional",
            "person",
            f"An account's holder can add {kinds}, with recovery codes. Shared demonstration "
            "accounts cannot. "
            f"Active staff accounts with one: {staff_with} of {staff}. "
            f"Active client portal accounts with one: {portal_with} of {portal}.",
        ),
        Fact("failed-sign-ins", "Failed sign-ins", failed, "configuration", failed_note),
        _sign_in_code(),
        Fact(
            "sign-in-records",
            "Record of sign-ins",
            "Application log, and the last sign-in time",
            "release",
            "Sign-ins, failures and sign-outs are written to the application log with the "
            "username; the deployment's log retention applies. The database keeps each "
            "account's last sign-in time and, while a session lasts, how it signed in. There "
            "is no stored list of sign-ins.",
        ),
    ]


def _browser_close():
    """What closing the browser does. A password sign-in follows Remember me (allauth's
    ACCOUNT_SESSION_REMEMBER: each person's choice when None); every other sign-in, an emailed
    code among them, follows SESSION_EXPIRE_AT_BROWSER_CLOSE."""
    remember = account_settings.SESSION_REMEMBER
    if remember is None:
        password, source = "Ends a password sign-in, unless Remember me was ticked", "person"
    elif remember:
        password, source = "Does not end a password sign-in", "configuration"
    else:
        password, source = "Ends a password sign-in", "configuration"
    parts = [password]
    if account_settings.LOGIN_BY_CODE_ENABLED:
        parts.append(
            "ends a sign-in with an emailed code"
            if settings.SESSION_EXPIRE_AT_BROWSER_CLOSE
            else "does not end a sign-in with an emailed code"
        )
    return Fact(
        "browser-close",
        "Closing the browser",
        "; ".join(parts),
        source,
        "Some browsers keep a session when they reopen the last one.",
    )


def _sessions(facts):
    age = duration_text(settings.SESSION_COOKIE_AGE)
    sliding = settings.SESSION_SAVE_EVERY_REQUEST
    secure, script_safe = settings.SESSION_COOKIE_SECURE, settings.SESSION_COOKIE_HTTPONLY
    own_session_ends = account_settings.LOGOUT_ON_PASSWORD_CHANGE
    facts += [
        Fact(
            "session-length",
            "Session length",
            age,
            "configuration",
            "A session ends this long after its last request."
            if sliding
            else "A session ends this long after it was last saved: at sign-in, and again when "
            "the person changes a display preference.",
        ),
        Fact(
            "inactivity",
            "Sign-out after a period without activity",
            f"After {age}" if sliding else "None",
            "configuration" if sliding else "unavailable",
            "Every request renews the session." if sliding else "",
        ),
        _browser_close(),
        Fact(
            "deactivation",
            "Deactivating an account",
            "Refused at its next request",
            "release",
            "The account's sessions are not deleted: if it is made active again before they "
            "expire, those browsers are signed in again.",
        ),
        Fact(
            "new-password",
            "A new password",
            "Signs out every session of the account"
            if own_session_ends
            else "Signs out the account's other sessions",
            "configuration",
            ""
            if own_session_ends
            else "A person who changes their own password stays signed in where they changed "
            "it. A password a superuser sets signs out every session of the account.",
        ),
        Fact(
            "session-cookie",
            "Session cookie",
            _sentence(
                [
                    "sent over HTTPS only" if secure else "also sent over plain HTTP",
                    "hidden from page scripts" if script_safe else "readable by page scripts",
                ]
            ),
            "deployment",
            "Debug mode is on: for local development only." if settings.DEBUG else "",
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


def _connection(facts):
    hsts = settings.SECURE_HSTS_SECONDS
    if settings.SECURE_SSL_REDIRECT:
        note = "Plain HTTP requests are redirected. " + (
            f"Browsers are told to use only HTTPS for {duration_text(hsts)} (SECURE_HSTS_SECONDS)."
            if hsts
            else "Browsers are not told to keep using HTTPS (SECURE_HSTS_SECONDS is 0)."
        )
        facts.append(Fact("https", "HTTPS", "Required", "deployment", note, "DEBUG"))
    else:
        note = (
            "Debug mode is on: for local development only."
            if settings.DEBUG
            else "Plain HTTP requests are not redirected."
        )
        facts.append(Fact("https", "HTTPS", "Not required", "deployment", note, "DEBUG"))
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
        value, note = (
            "Replaced by the deployment",
            "The deployment's policy decides what pages may load.",
        )
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
