"""What the security settings page states (core.security_settings.in_force): each statement is
read from the running configuration or the database, and the claims about sessions are proven
against the real middleware."""

import pytest
from allauth.account import app_settings as account_settings
from allauth.mfa.models import Authenticator
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.test import Client as BrowserClient
from django.test import override_settings
from django.urls import reverse

from accounts.models import User
from accounts.roles import has_capability
from core.security_settings import (
    CHANGED,
    SOURCES,
    duration_text,
    in_force,
    patient_capability_holders,
)
from request_forms.models import FieldKind, RequestTemplate, TemplateField
from taxonomy.models import RequestCategory

pytestmark = pytest.mark.django_db

PASSWORD = "test-password-729!"
NEW_PASSWORD = "another-password-418?"
GROUP_KEYS = {"sign-in", "sessions", "patient-information", "connection"}
DEFAULT_ENGINE = "django.core.cache.backends.locmem.LocMemCache"


@pytest.fixture(autouse=True)
def clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    """Render real templates without a collectstatic manifest, and without the HTTPS redirect."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    settings.SECURE_SSL_REDIRECT = False


def assert_changed(key, *names):
    """The fact is the changed-configuration fact: it names exactly `names` and states nothing
    else. Returns everything the fact says, lowered, for the absence checks of the caller."""
    item = fact(key)
    assert item.value == CHANGED
    assert item.source == "configuration"
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
    assert item.note.endswith(f"Check {listed} in config/settings.py."), item.note
    return f"{item.value} {item.note}".lower()


def facts():
    return {fact.key: fact for group in in_force() for fact in group.facts}


def fact(key):
    return facts()[key]


# duration_text


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (1209600, "2 weeks"),
        (604800, "1 week"),
        (86400, "1 day"),
        (3600, "1 hour"),
        (300, "5 minutes"),
        (60, "1 minute"),
        (90, "90 seconds"),
        (1, "1 second"),
    ],
)
def test_duration_text(seconds, text):
    assert duration_text(seconds) == text


# the structure of what is stated


def test_every_group_key_and_source_is_valid_and_no_fact_claims_a_standard():
    groups = in_force()
    assert {group.key for group in groups} == GROUP_KEYS
    assert [group.key for group in groups] == [
        "sign-in",
        "sessions",
        "patient-information",
        "connection",
    ]
    seen = set()
    for group in groups:
        assert group.title and group.facts
        for item in group.facts:
            assert item.key not in seen
            seen.add(item.key)
            assert item.source in SOURCES
            assert item.source_label == SOURCES[item.source]
            text = " ".join([item.label, item.value, item.note]).lower()
            for word in ("complian", "certif", "validat"):
                assert word not in text, (item.key, word)


# statements read from the configuration


def test_session_length_follows_the_release_setting():
    assert fact("session-length").value == "2 weeks"
    with override_settings(SESSION_COOKIE_AGE=1800):
        stated = assert_changed("session-length", "SESSION_COOKIE_AGE")
    assert "30 minutes" not in stated


CODE_ENDS = "ends a sign-in with an emailed code"
CODE_KEEPS = "does not end a sign-in with an emailed code"
UNLESS = "Ends a password sign-in, unless Remember me was ticked"


def test_closing_the_browser_under_the_release_configuration():
    # A password sign-in follows Remember me; an emailed-code sign-in follows
    # SESSION_EXPIRE_AT_BROWSER_CLOSE (allauth sets the expiry only for a password sign-in).
    with override_settings(**SENDS):
        stated = fact("browser-close")
    assert stated.value == f"{UNLESS}; {CODE_KEEPS}" and stated.source == "person"
    assert stated.note == "Some browsers keep a session when they reopen the last one."
    with override_settings(**SENDS_NOTHING):
        assert fact("browser-close").value == UNLESS


@pytest.mark.parametrize(
    ("expire", "remember", "names"),
    [
        (True, None, ["SESSION_EXPIRE_AT_BROWSER_CLOSE"]),
        (False, False, ["ACCOUNT_SESSION_REMEMBER"]),
        (False, True, ["ACCOUNT_SESSION_REMEMBER"]),
        (True, False, ["ACCOUNT_SESSION_REMEMBER", "SESSION_EXPIRE_AT_BROWSER_CLOSE"]),
        (True, True, ["ACCOUNT_SESSION_REMEMBER", "SESSION_EXPIRE_AT_BROWSER_CLOSE"]),
    ],
)
def test_closing_the_browser_is_changed_when_expiry_or_remember_me_differ(expire, remember, names):
    with override_settings(
        SESSION_EXPIRE_AT_BROWSER_CLOSE=expire,
        ACCOUNT_SESSION_REMEMBER=remember,
        ACCOUNT_LOGIN_BY_CODE_ENABLED=True,
    ):
        stated = assert_changed("browser-close", *names)
    assert "ends" not in stated.replace("changed in", "") and "remember me" not in stated


def test_a_session_that_ends_with_the_browser_follows_remember_me_at_sign_in(make_staff):
    make_staff("remember-user", "admin")
    default = BrowserClient()
    assert (
        default.post(
            reverse("account_login"), {"login": "remember-user", "password": PASSWORD}
        ).status_code
        == 302
    )
    assert default.session.get_expire_at_browser_close() is True
    remembered = BrowserClient()
    assert (
        remembered.post(
            reverse("account_login"),
            {"login": "remember-user", "password": PASSWORD, "remember": "on"},
        ).status_code
        == 302
    )
    assert remembered.session.get_expire_at_browser_close() is False


def test_sign_in_by_code_follows_the_setting():
    with override_settings(**SENDS_NOTHING):
        assert fact("sign-in-code").value == "Off"
    with override_settings(ACCOUNT_LOGIN_BY_CODE_TIMEOUT=300, **SENDS):
        stated = fact("sign-in-code")
    assert stated.value == "On" and "5 minutes" in stated.note
    # The switch turned off while the deployment sends email is a changed configuration.
    with override_settings(**{**SENDS, "ACCOUNT_LOGIN_BY_CODE_ENABLED": False}):
        assert_changed("sign-in-code", "ACCOUNT_LOGIN_BY_CODE_ENABLED")


# A deployment with debug mode off, and one with it on, at startup (STARTUP_DEBUG,
# config/settings.py): the redirect and the secure cookie follow it.
REDIRECT = {"STARTUP_DEBUG": False, "SECURE_SSL_REDIRECT": True, "SESSION_COOKIE_SECURE": True}
NO_REDIRECT = {"STARTUP_DEBUG": True, "SECURE_SSL_REDIRECT": False, "SESSION_COOKIE_SECURE": False}
# A deployment that sends email, and one that does not (STARTUP_EMAIL_BACKEND).
SENDS = {
    "STARTUP_EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
    "ACCOUNT_LOGIN_BY_CODE_ENABLED": True,
}
SENDS_NOTHING = {
    "STARTUP_EMAIL_BACKEND": "django.core.mail.backends.dummy.EmailBackend",
    "ACCOUNT_LOGIN_BY_CODE_ENABLED": False,
}


def test_https_required_names_the_hsts_period():
    with override_settings(SECURE_HSTS_SECONDS=3600, **REDIRECT):
        stated = fact("https")
    assert stated.value == "Required" and "1 hour" in stated.note
    with override_settings(SECURE_HSTS_SECONDS=0, **REDIRECT):
        stated = fact("https")
    assert stated.value == "Required" and "only HTTPS" not in stated.note


def test_https_not_required_when_the_redirect_is_off():
    with override_settings(DEBUG=True, SECURE_HSTS_SECONDS=0, **NO_REDIRECT):
        stated = fact("https")
    assert stated.value == "Not required" and "only in debug mode" in stated.note


def test_content_security_policy_default_or_replaced():
    assert fact("content-security-policy").value == "The application's default"
    with override_settings(CONTENT_SECURITY_POLICY="default-src 'none'"):
        assert fact("content-security-policy").value == "Replaced by the deployment"


def test_session_cookie_wording_follows_the_secure_flag():
    with override_settings(SECURE_HSTS_SECONDS=0, DEBUG=False, **REDIRECT):
        stated = fact("session-cookie")
    assert stated.value == "Sent over HTTPS only and hidden from page scripts"
    assert stated.note == ""
    with override_settings(SECURE_HSTS_SECONDS=0, DEBUG=True, **NO_REDIRECT):
        stated = fact("session-cookie")
    assert stated.value == "Also sent over plain HTTP and hidden from page scripts"
    assert "local development" in stated.note
    with override_settings(SESSION_COOKIE_HTTPONLY=False):
        stated = assert_changed("session-cookie", "SESSION_COOKIE_HTTPONLY")
    assert "hidden from page scripts" not in stated


def test_password_rule_is_the_release_rule_and_any_other_is_changed():
    assert fact("passwords").value == (
        "At least 8 characters, not a common password, not only digits and not close to "
        "the account's username, first name, last name or email address"
    )
    validators = [
        {
            "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
            "OPTIONS": {"min_length": 12},
        }
    ]
    with override_settings(AUTH_PASSWORD_VALIDATORS=validators):
        stated = assert_changed("passwords", "AUTH_PASSWORD_VALIDATORS")
    assert "12" not in stated and "characters" not in stated
    with override_settings(AUTH_PASSWORD_VALIDATORS=[]):
        stated = assert_changed("passwords", "AUTH_PASSWORD_VALIDATORS")
    assert "6" not in stated and "any password" not in stated


def test_failed_sign_in_limit_is_the_release_limit_and_any_other_is_changed():
    stated = fact("failed-sign-ins")
    assert stated.value == (
        "Refused for a time after 5 failures within 5 minutes for one username or email "
        "address, or 10 failures within a minute from one network address"
    )
    with override_settings(ACCOUNT_RATE_LIMITS={"login_failed": "3/10m/key"}):
        stated = assert_changed("failed-sign-ins", "ACCOUNT_RATE_LIMITS")
    assert "3 failures" not in stated and "10 minutes" not in stated
    with override_settings(ACCOUNT_RATE_LIMITS={"login_failed": False}):
        stated = assert_changed("failed-sign-ins", "ACCOUNT_RATE_LIMITS")
    assert "no limit" not in stated


def test_per_process_note_belongs_to_the_local_memory_cache_of_the_release():
    assert "Each server process keeps its own count." in fact("failed-sign-ins").note
    for backend in (
        "django.core.cache.backends.db.DatabaseCache",
        "django.core.cache.backends.dummy.DummyCache",
    ):
        with override_settings(CACHES={"default": {"BACKEND": backend}}):
            stated = assert_changed("failed-sign-ins", "CACHES")
        assert "server process" not in stated and "not enforced" not in stated


# counts


@pytest.fixture
def make_staff(db):
    def make(name, *roles, **extra):
        user = User.objects.create_user(name, f"{name}@example.invalid", PASSWORD, **extra)
        for role in roles:
            user.groups.add(Group.objects.get_or_create(name=role)[0])
        return user

    return make


@pytest.fixture
def grant():
    return Permission.objects.get(
        codename="view_patient_information", content_type__app_label="accounts"
    )


def test_patient_capability_holders_count_each_account_once(make_staff, grant, client_org):
    assert patient_capability_holders() == 0
    direct = make_staff("holder-direct", "technical")
    direct.user_permissions.add(grant)
    through_group = make_staff("holder-group", "client_services")
    group = Group.objects.create(name="patient-viewers")
    group.permissions.add(grant)
    through_group.groups.add(group)
    both = make_staff("holder-both", "supervisor")
    both.user_permissions.add(grant)
    both.groups.add(group)
    assert patient_capability_holders() == 3

    inactive = make_staff("holder-inactive", is_active=False)
    inactive.user_permissions.add(grant)
    portal = make_staff("holder-portal", client=client_org)
    portal.user_permissions.add(grant)
    superuser = make_staff("holder-super", is_superuser=True)
    # A grant opens nothing without a laboratory role (may_see_patient_information).
    make_staff("holder-roleless").user_permissions.add(grant)
    assert patient_capability_holders() == 3
    assert not has_capability(superuser, "view_patient_information")
    assert not has_capability(portal, "view_patient_information")
    assert not has_capability(inactive, "view_patient_information")
    assert fact("patient-capability").value == "3 active staff accounts"


def test_patient_capability_value_is_singular_for_one(make_staff, grant):
    make_staff("holder-only", "technical").user_permissions.add(grant)
    assert fact("patient-capability").value == "1 active staff account"


def second_factor_note():
    return fact("second-factor").note


def totp(user):
    return Authenticator.objects.create(user=user, type=Authenticator.Type.TOTP, data={})


def recovery(user):
    return Authenticator.objects.create(user=user, type=Authenticator.Type.RECOVERY_CODES, data={})


def test_second_factor_counts_staff_and_portal_accounts_separately(
    make_staff, client_org, portal_user
):
    enrolled = make_staff("mfa-staff")
    totp(enrolled)
    recovery(enrolled)
    make_staff("mfa-staff-codes-only")
    recovery(User.objects.get(username="mfa-staff-codes-only"))
    make_staff("mfa-staff-none")
    inactive = make_staff("mfa-inactive", is_active=False)
    totp(inactive)
    portal = portal_user(client_org, "mfa-portal")
    totp(portal)
    portal_user(client_org, "mfa-portal-none")
    note = second_factor_note()
    assert "Active staff accounts with one: 1 of 3." in note
    assert "Active client portal accounts with one: 1 of 2." in note


def test_second_factor_with_no_accounts(db):
    note = second_factor_note()
    assert "staff accounts with one: 0 of 0." in note
    assert "portal accounts with one: 0 of 0." in note


@pytest.fixture
def template(db):
    category = RequestCategory.objects.create(key="status-and-results", name="Status and results")
    return RequestTemplate.objects.create(key="pickup", name="Pickup", category=category)


def field(template, key, **extra):
    return TemplateField.objects.create(
        template=template, key=key, label=key, kind=FieldKind.SHORT_TEXT, **extra
    )


def test_patient_fields_count_only_active_marked_fields(template):
    assert fact("patient-fields").value == "0 active fields"
    field(template, "marked", holds_patient_information=True)
    assert fact("patient-fields").value == "1 active field"
    field(template, "marked-too", holds_patient_information=True)
    field(template, "retired", holds_patient_information=True, is_active=False)
    field(template, "unmarked")
    field(template, "unmarked-retired", is_active=False)
    assert fact("patient-fields").value == "2 active fields"


# the claims about sessions, against the real middleware


@pytest.fixture
def signed_in(make_staff):
    def make(user):
        browser = BrowserClient()
        browser.force_login(user)
        assert browser.get("/").status_code == 200
        return browser

    return make


def is_signed_out(browser):
    response = browser.get("/")
    return response.status_code == 302 and "login" in response["Location"]


def test_the_page_states_what_the_sessions_tests_below_prove():
    stated = fact("deactivation")
    assert stated.value == "Refused at its next request" and "not deleted" in stated.note
    stated = fact("new-password")
    assert stated.value == "Signs out the account's other sessions"
    assert "stays signed in where they changed it" in stated.note


def test_a_deactivated_account_is_refused_at_its_next_request(make_staff, signed_in):
    user = make_staff("session-inactive", "admin")
    browser = signed_in(user)
    other = signed_in(user)
    user.is_active = False
    user.save()
    assert is_signed_out(browser)
    assert is_signed_out(other)


def test_a_new_password_signs_out_every_existing_session(make_staff, signed_in):
    user = make_staff("session-password", "admin")
    first, second = signed_in(user), signed_in(user)
    user.set_password(NEW_PASSWORD)
    user.save()
    assert is_signed_out(first)
    assert is_signed_out(second)


def test_changing_a_password_on_the_account_page_keeps_only_that_session(make_staff, signed_in):
    assert account_settings.LOGOUT_ON_PASSWORD_CHANGE is False
    user = make_staff("session-self-change", "admin")
    changer, other = signed_in(user), signed_in(user)
    response = changer.post(
        reverse("account_change_password"),
        {"oldpassword": PASSWORD, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
    )
    assert response.status_code == 302, (
        getattr(response, "context", None) and response.context["form"].errors
    )
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)
    assert changer.get("/").status_code == 200
    assert is_signed_out(other)


def test_the_release_settings_file_is_pinned():
    """The facts describe this release's config/settings.py and config/logging.py. Whoever
    changes either checks each fact in core/security_settings.py against the change (RELEASE,
    the notes) and then sets RELEASE_SETTINGS_SHA256 to the new digest printed here."""
    from core.security_settings import RELEASE_SETTINGS_SHA256, settings_file_digest

    digest = settings_file_digest()
    assert digest == RELEASE_SETTINGS_SHA256, (
        "config/settings.py changed: check the Security settings facts against it, then set "
        f"core.security_settings.RELEASE_SETTINGS_SHA256 = {digest!r}"
    )
