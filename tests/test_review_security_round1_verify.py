"""Independent verification of the security settings contracts on synthetic records."""

import re
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from allauth.account.adapter import get_adapter
from allauth.core.internal import ratelimit
from allauth.mfa.recovery_codes.internal.flows import auto_generate_recovery_codes
from django.conf import settings
from django.contrib.auth import SESSION_KEY, password_validation
from django.contrib.auth.forms import AdminPasswordChangeForm
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.contrib.sessions.models import Session
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.management import CommandError, call_command
from django.db import OperationalError, connection, transaction
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.test import Client, override_settings
from django.urls import reverse

from accounts.models import User
from accounts.roles import ROLES
from accounts.setup import setup_lab
from core import lab_profile, lab_profile_views, security_settings, security_settings_views
from core.form_surfaces import FORM_SURFACES
from core.management.commands import seed_demo
from core.models import AuditEvent, Lab, LabContact, UserPreference
from core.security_headers import SecurityHeadersMiddleware
from request_forms.selectors import may_see_patient_information

pytestmark = pytest.mark.django_db
PAGES = ("lab-profile", "security-settings")
PASSWORD = "test-password-729!"


@pytest.fixture
def admin(staff):
    return staff("verification-admin", "admin")


def facts():
    return {item.key: item for group in security_settings.in_force() for item in group.facts}


def wording(key):
    item = facts()[key]
    return f"{item.value} {item.note}".lower()


def assert_changed(key, *names):
    """The fact is the changed-configuration fact: it names exactly `names` and states nothing
    else. Returns everything the fact says, lowered, for the absence checks of the caller."""
    item = facts()[key]
    assert item.value == security_settings.CHANGED
    assert item.source == "configuration"
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
    assert item.note.endswith(f"Check {listed} in config/settings.py."), item.note
    return f"{item.value} {item.note}".lower()


def safeguard_row(heading):
    lines = (Path(settings.BASE_DIR) / "docs/safeguards.md").read_text().splitlines()
    return next(line.split("|")[2].strip() for line in lines if line.startswith("| " + heading))


def isolation():
    with connection.cursor() as cursor:
        cursor.execute("SHOW transaction_isolation")
        return cursor.fetchone()[0]


def concurrent_edit(action):
    """Return False if the page or command serialized before the second editor."""

    def edit():
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '850ms'")
                action()
        except OperationalError as exc:
            if getattr(exc.__cause__, "sqlstate", None) != "55P03":
                raise
            return False
        finally:
            connection.close()
        return True

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(edit).result(timeout=8)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("page", PAGES)
def test_access_decision_shares_the_panel_reading(client, admin, page, monkeypatch):
    """Observe a normal, authorized GET without changing privileges or attempting access."""
    Lab.objects.create(name="Synthetic verification laboratory")
    client.force_login(admin)
    module, gate = (
        (lab_profile_views, "require_staff")
        if page == "lab-profile"
        else (security_settings_views, "require_role")
    )
    original = getattr(module, gate)
    checks = []

    def authorized(*args, **kwargs):
        original(*args, **kwargs)
        checks.append(isolation())

    monkeypatch.setattr(module, gate, authorized)
    assert client.get(reverse(page)).status_code == 200
    assert "repeatable read" in checks, checks


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("page", PAGES)
def test_first_shell_does_not_write_preferences_inside_panel_snapshot(client, admin, page):
    Lab.objects.create(name="Synthetic verification laboratory")
    client.force_login(admin)
    assert not UserPreference.objects.filter(user=admin).exists()
    writes = []

    def observe(execute, sql, params, many, context):
        if UserPreference._meta.db_table in sql and sql.startswith(
            ("INSERT ", "UPDATE ", "DELETE ")
        ):
            writes.append(isolation())
        return execute(sql, params, many, context)

    with connection.execute_wrapper(observe):
        assert client.get(reverse(page)).status_code == 200
    assert "repeatable read" not in writes, writes


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("page", PAGES)
@pytest.mark.parametrize("atomic_requests", [False, True])
def test_request_transaction_keeps_contact_panel_and_history_together(
    client, admin, staff, monkeypatch, page, atomic_requests
):
    Lab.objects.create(name="Synthetic verification laboratory")
    editor = staff("verification-colleague", "admin")
    client.force_login(admin)
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", atomic_requests)
    original = lab_profile.contacts
    reason = "Synthetic appointment V41"
    name = "Verification Liaison"
    edits = []

    def appoint():
        lab_profile.save_contact(
            actor=editor, kind="privacy", values={"name": name}, reason=reason, expected_revision=0
        )

    def read_contacts():
        rows = original()
        if not edits:
            edits.append(concurrent_edit(appoint))
        return rows

    monkeypatch.setattr(lab_profile, "contacts", read_contacts)
    response = client.get(reverse(page))
    assert response.status_code == 200
    panel = next(row for row in response.context["contacts"] if row.kind == "privacy")
    timeline = response.context["history"]
    assert (panel.name == name) == any(event.reason == reason for event in timeline)


@pytest.mark.parametrize("dialog", [False, True])
def test_contact_csrf_refusal_is_never_stored(admin, dialog):
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(admin)
    response = browser.post(
        reverse("lab-contact-edit", args=["privacy"]),
        {"reason": "Synthetic draft V42"},
        headers={"X-VR-Form": "1"} if dialog else {},
    )
    assert response.status_code == 403
    assert b"Synthetic draft V42" not in response.content
    assert not AuditEvent.objects.filter(action="lab.contact_changed").exists()
    assert "no-store" in response.get("Cache-Control", "").lower()


def test_contact_transport_redirect_is_out_of_scope_and_carries_no_content(client):
    # Decision (round 1 verification V-05): Django's SecurityMiddleware answers the HTTPS 301
    # before any view runs, for every address alike. It carries no content and no input, so it
    # is not made no-store; only the view's own answers and the CSRF refusal are.
    with override_settings(SECURE_SSL_REDIRECT=True):
        response = client.get(reverse("lab-contact-edit", args=["security"]))
    assert response.status_code == 301
    assert response.content == b""


@pytest.mark.parametrize("template", ["400.html", "403.html", "404.html"])
def test_error_help_on_each_registered_dialog_surface(rf, admin, portal_user, client_org, template):
    for user in (admin, portal_user(client_org), AnonymousUser()):
        for route in FORM_SURFACES:
            request = rf.get("/", headers={"X-VR-Form": "1"})
            request.user = user
            request.resolver_match = SimpleNamespace(view_name=route, kwargs={})
            html = render_to_string(template, request=request)
            assert "data-vr-documentation-source" in html, (route, template)
            assert re.findall(r'data-vr-documentation-id="([^"]+)"', html) == [
                "core-unavailable"
            ], (route, template)


@pytest.mark.django_db(transaction=True)
def test_seed_respects_an_intervening_setup_lab_rename(staff, monkeypatch):
    Lab.objects.create(name=seed_demo.DEMO_LAB_NAME)
    for role in ROLES:
        Group.objects.get_or_create(name=role)
    editor = staff("synthetic-setup-owner", "admin")
    monkeypatch.setenv("DEMO_PASSWORD", "verification-demo-password-704")
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    original = seed_demo.Command._seed_profile
    renamed = []

    def seed_profile(command):
        renamed.append(
            concurrent_edit(
                lambda: setup_lab(
                    lab_name="Synthetic Renamed Laboratory",
                    update_lab=True,
                    username=editor.username,
                )
            )
        )
        return original(command)

    monkeypatch.setattr(seed_demo.Command, "_seed_profile", seed_profile)
    try:
        call_command("seed_demo", stdout=StringIO(), stderr=StringIO())
    except CommandError as exc:
        assert "non-demo" in str(exc)
    if renamed == [True]:
        assert Lab.objects.get().name == "Synthetic Renamed Laboratory"
        assert not LabContact.objects.exists(), "Seed appointed contacts on the renamed laboratory"
    else:
        assert Lab.objects.get().name == seed_demo.DEMO_LAB_NAME


class NumericPasswordValidator:
    """A deployment validator with the same short name as a built-in."""

    def validate(self, password, user=None):
        if not password.endswith("7"):
            raise ValidationError("Must end with seven")

    def get_help_text(self):
        return "End with seven."


def test_password_summary_recognizes_validator_implementation():
    with override_settings(
        AUTH_PASSWORD_VALIDATORS=[{"NAME": f"{__name__}.NumericPasswordValidator"}]
    ):
        password_validation.validate_password("77777777")
        with pytest.raises(ValidationError):
            password_validation.validate_password("Example-8")
        stated = assert_changed("passwords", "AUTH_PASSWORD_VALIDATORS")
    assert "only digits" not in stated and "other check" not in stated


def test_password_minimum_distinguishes_the_admin_console(admin):
    with override_settings(AUTH_PASSWORD_VALIDATORS=[], ACCOUNT_PASSWORD_MIN_LENGTH=16):
        password = "Short-729!"
        with pytest.raises(ValidationError):
            get_adapter().clean_password(password, user=admin)
        assert AdminPasswordChangeForm(
            admin, {"password1": password, "password2": password}
        ).is_valid()
        stated = assert_changed("passwords", "AUTH_PASSWORD_VALIDATORS")
    assert "16" not in stated and "characters" not in stated


def test_ip_only_failure_limit_describes_a_shared_counter(rf):
    with override_settings(ACCOUNT_RATE_LIMITS={"login_failed": "2/m/ip"}):
        (rate,) = ratelimit.parse_rates("2/m/ip")

        def counter(key):
            return ratelimit.get_cache_key(
                rf.get("/"), app="allauth.account", action="login_failed", rate=rate, key=key
            )

        assert counter("synthetic-operator") == counter("synthetic@example.invalid")
        stated = assert_changed("failed-sign-ins", "ACCOUNT_RATE_LIMITS")
    assert "counted separately" not in stated


@pytest.mark.parametrize(
    "scope,error", [("unknown_unit", ValueError), ("user", ImproperlyConfigured)]
)
def test_failed_sign_in_scope_must_be_usable_at_sign_in(rf, scope, error):
    rule = f"2/5m/{scope}"
    request = rf.get("/")
    request.user = AnonymousUser()
    (rate,) = ratelimit.parse_rates(rule)
    with pytest.raises(error):
        ratelimit.get_cache_key(
            request,
            app="allauth.account",
            action="login_failed",
            rate=rate,
            key="synthetic-operator",
        )
    with override_settings(ACCOUNT_RATE_LIMITS={"login_failed": rule}):
        stated = assert_changed("failed-sign-ins", "ACCOUNT_RATE_LIMITS")
    assert "unknown_unit" not in stated and "2/5m" not in stated and "failures" not in stated


@pytest.mark.parametrize("period", ["nan", "inf"])
def test_unusable_rate_period_leaves_the_other_facts_readable(period):
    with override_settings(ACCOUNT_RATE_LIMITS={"login_failed": f"2/{period}s/key"}):
        shown = facts()
        assert_changed("failed-sign-ins", "ACCOUNT_RATE_LIMITS")
    assert "session-length" in shown
    assert shown["session-length"].value != security_settings.CHANGED


def test_remembered_session_age_matches_issued_cookie(client, admin):
    with override_settings(ACCOUNT_SESSION_COOKIE_AGE=420, SESSION_COOKIE_AGE=1800):
        response = client.post(
            reverse("account_login"),
            {"login": admin.username, "password": PASSWORD, "remember": "on"},
        )
        assert response.status_code == 302 and client.session[SESSION_KEY] == str(admin.pk)
        assert response.cookies[settings.SESSION_COOKIE_NAME]["max-age"] == 420
        stated = assert_changed(
            "session-length", "SESSION_COOKIE_AGE", "ACCOUNT_SESSION_COOKIE_AGE"
        )
    assert "7 minutes" not in stated and "420" not in stated and "30 minutes" not in stated


@pytest.mark.parametrize("logout", [False, True])
def test_owners_own_admin_password_change_matches_statement(client, logout):
    owner = User.objects.create_superuser("verification-owner", "owner@example.invalid", PASSWORD)
    client.force_login(owner)
    with override_settings(ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE=logout):
        response = client.post(
            reverse("admin:auth_user_password_change", args=[owner.pk]),
            {
                "password1": "Next-password-441!",
                "password2": "Next-password-441!",
                "set-password": "1",
                "usable_password": "true",
            },
        )
        assert response.status_code == 302
        owner.refresh_from_db()
        assert owner.check_password("Next-password-441!")
        assert client.get(reverse("admin:index")).status_code == 200
        assert client.session[SESSION_KEY] == str(owner.pk)
        if logout:
            stated = assert_changed("new-password", "ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE")
            assert "stays signed in" not in stated
        else:
            stated = wording("new-password")
            # Release configuration: whoever changes their own password, in the admin console
            # too, stays signed in (proved above); only another account's sessions end.
            assert "in the admin console, stays signed in" in stated
            assert "for another account signs out every session of that account" in stated


def test_enrolment_options_match_registered_mfa_methods(rf, admin):
    request = rf.get("/")
    request.user = admin
    with override_settings(MFA_SUPPORTED_TYPES=["totp"]):
        assert auto_generate_recovery_codes(request) is None
        stated = assert_changed("second-factor", "MFA_SUPPORTED_TYPES")
    assert "recovery codes" not in stated and "authenticator app" not in stated


def test_configured_reauthentication_is_reported():
    values = []
    for required in (True, False):
        with override_settings(MFA_REAUTHENTICATION_REQUIRED=required):
            assert get_adapter().is_reauthentication_required(app="allauth.mfa") is required
            values.append(facts()["second-factor-changes"])
    assert values[0] != values[1], "The page is identical under opposite re-authentication rules"
    with override_settings(MFA_REAUTHENTICATION_REQUIRED=False):
        stated = assert_changed("second-factor-changes", "MFA_REAUTHENTICATION_REQUIRED")
    assert "recent" not in stated and "minutes" not in stated


def test_reference_describes_stored_session_method(client, admin):
    assert (
        client.post(
            reverse("account_login"), {"login": admin.username, "password": PASSWORD}
        ).status_code
        == 302
    )
    stored = Session.objects.get(session_key=client.session.session_key).get_decoded()
    assert stored["account_authentication_methods"]
    admin.refresh_from_db()
    assert admin.last_login
    row = safeguard_row("Log-in monitoring")
    assert "keeps only each account's last sign-in time" not in row


def test_reference_allows_for_grants_to_role_groups(staff):
    reader = staff("verification-technical", "technical")
    group = reader.groups.get(name="technical")
    group.permissions.add(
        Permission.objects.get(
            content_type__app_label="accounts", codename="view_patient_information"
        )
    )
    assert may_see_patient_information(reader)
    row = safeguard_row("Authorization and supervision").lower()
    assert "no role includes" not in row or "by default" in row


def test_reference_explains_locked_demo_enrolment(client, staff):
    user = staff("verification-shared", "technical")
    user.credentials_locked = True
    user.save(update_fields=["credentials_locked"])
    client.force_login(user)
    response = client.get(reverse("mfa_activate_totp"))
    assert response.status_code == 302 and response.url == reverse("dashboard")
    row = safeguard_row("Person or entity authentication").lower()
    assert "each person chooses" not in row or "demonstration" in row


def test_hsts_report_tracks_header_without_a_redirect(client):
    both_off = {"SECURE_SSL_REDIRECT": False, "SESSION_COOKIE_SECURE": False}
    with override_settings(SECURE_HSTS_SECONDS=3600, **both_off):
        response = client.get(reverse("health"), secure=True)
        assert "max-age=3600" in response["Strict-Transport-Security"]
        # HSTS without the redirect is not this release's configuration: nothing is claimed.
        first = assert_changed("https", "SECURE_HSTS_SECONDS")
    with override_settings(SECURE_HSTS_SECONDS=0, **both_off):
        assert "Strict-Transport-Security" not in Client().get(reverse("health"), secure=True)
        second = wording("https")
    assert first != second, "Opposite browser HTTPS instructions have identical facts"
    assert "not required" in second


def test_redirect_fact_qualifies_exempt_routes(client):
    # A deployment with debug mode off (STARTUP_DEBUG, config/settings.py) and an exemption.
    with override_settings(
        STARTUP_DEBUG=False,
        SECURE_SSL_REDIRECT=True,
        SESSION_COOKIE_SECURE=True,
        SECURE_REDIRECT_EXEMPT=["^health/$"],
    ):
        assert client.get(reverse("health")).status_code == 200
        assert client.get(reverse("security-settings")).status_code == 301
        stated = assert_changed("https", "SECURE_REDIRECT_EXEMPT")
    assert "redirected" not in stated and "required" not in stated.replace("check", "")


def test_whitespace_csp_answer_can_be_delivered(rf):
    with override_settings(CONTENT_SECURITY_POLICY=" \n\t "):
        assert facts()["content-security-policy"].value == "None"
        response = SecurityHeadersMiddleware(lambda _: HttpResponse())(rf.get("/"))
    assert not response.get("Content-Security-Policy", "").strip()


def test_contact_detail_edit_keeps_before_and_after_even_if_label_stays_the_same(client, admin):
    Lab.objects.create(name="Synthetic history laboratory")
    client.force_login(admin)
    for revision, title in enumerate(("Coordinator", "Director")):
        response = client.post(
            reverse("lab-contact-edit", args=["privacy"]),
            {
                "expected_revision": revision,
                "name": "Example Office",
                "title": title,
                "reason": f"Synthetic title appointment {revision}",
            },
        )
        assert response.status_code == 302
    event = AuditEvent.objects.get(
        action="lab.contact_changed", reason="Synthetic title appointment 1"
    )
    assert (event.before["title"], event.after["title"]) == ("Coordinator", "Director")
    for page in PAGES:
        shown = client.get(reverse(page))
        entry = next(e for e in shown.context["history"] if e.pk == event.pk)
        assert entry.detail == "Privacy contact: Example Office"
        assert shown.content.count(b"Synthetic title appointment 1") == 1


def test_debug_help_matches_the_cookie_and_redirect_received_by_the_browser(client, admin):
    with override_settings(DEBUG=True, SESSION_COOKIE_SECURE=True, SECURE_SSL_REDIRECT=True):
        client.force_login(admin)
        assert client.get(reverse("security-settings")).status_code == 301
        response = client.get(reverse("security-settings"), secure=True)
        assert response.status_code == 200
        assert response.cookies[settings.SESSION_COOKIE_NAME]["secure"]
        html = response.content.decode()
    assert "where the session cookie and HTTPS are not enforced" not in html


def test_sign_in_record_description_allows_for_a_session_outside_the_database(client, admin):
    with override_settings(SESSION_ENGINE="django.contrib.sessions.backends.cache"):
        assert (
            client.post(
                reverse("account_login"), {"login": admin.username, "password": PASSWORD}
            ).status_code
            == 302
        )
        assert client.session["account_authentication_methods"]
        assert not Session.objects.exists()
        stated = assert_changed("sign-in-records", "SESSION_ENGINE")
    assert "database keeps" not in stated and "how it signed in" not in stated


def test_unknown_login_is_described_as_an_unidentified_log_entry(rf, caplog):
    from accounts.signin_log import log_sign_in_failure

    with caplog.at_level("INFO", logger="lab_ops.signin"):
        log_sign_in_failure(
            sender=None, request=rf.get("/"), credentials={"username": "unknown-example"}
        )
    assert "signin.failed user=<unknown>" in caplog.messages
    assert not any("unknown-example" in message for message in caplog.messages)
    for text in (
        wording("sign-in-records"),
        safeguard_row("Information system activity review").lower(),
    ):
        assert "with the username" not in text or any(
            word in text for word in ("known", "identif", "match")
        )


def test_product_distinguishes_the_two_staff_count_populations(admin):
    from allauth.mfa.models import Authenticator

    roleless = User.objects.create_user("verification-roleless")
    Authenticator.objects.create(user=roleless, type=Authenticator.Type.TOTP, data={})
    shown = facts()
    assert not roleless.groups.exists()
    assert "1 of 2" in shown["second-factor"].note
    assert shown["patient-capability"].value == "0 active staff accounts"
    prose = " ".join((Path(settings.BASE_DIR) / "docs/product.md").read_text().split())
    assert "with a role can view patient information and have a second factor" not in prose


def test_emailed_code_fact_distinguishes_the_trusted_browser_choice():
    from allauth.mfa import app_settings as mfa_settings

    results = []
    for trust in (False, True):
        with override_settings(MFA_TRUST_ENABLED=trust, ACCOUNT_LOGIN_BY_CODE_ENABLED=True):
            assert mfa_settings.TRUST_ENABLED is trust
            results.append(wording("sign-in-code"))
    assert "still asked for where the account has one" in results[0]
    with override_settings(MFA_TRUST_ENABLED=True, ACCOUNT_LOGIN_BY_CODE_ENABLED=True):
        stated = assert_changed("sign-in-code", "MFA_TRUST_ENABLED")
    assert "still asked" not in stated


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("page", PAGES)
def test_two_first_staff_pages_can_mark_whats_new_seen(admin, monkeypatch, page):
    from core import preferences

    Lab.objects.create(name="Synthetic concurrency laboratory")
    first = Client(raise_request_exception=False)
    second = Client()
    for browser in (first, second):
        browser.force_login(admin)
    assert not UserPreference.objects.filter(user=admin, key=preferences.WHATS_NEW_KEY).exists()
    original = preferences.save

    def read_whats_new():
        assert second.get(reverse("whats-new")).status_code == 200

    def save(request, key, value):
        if request.resolver_match.view_name == page and key == preferences.WHATS_NEW_KEY:
            concurrent_edit(read_whats_new)
        return original(request, key, value)

    monkeypatch.setattr(preferences, "save", save)
    response = first.get(reverse(page))
    assert response.status_code == 200
    assert UserPreference.objects.filter(user=admin, key=preferences.WHATS_NEW_KEY).count() == 1
