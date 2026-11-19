"""Contracts for the release facts, their deployment inputs and the final snapshot boundary."""

import logging.config
import re
import runpy
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import StringIO
from pathlib import Path

import pytest
from allauth.account.adapter import get_adapter
from allauth.account.forms import LoginForm
from allauth.account.internal.flows.reauthentication import get_reauthentication_flows
from allauth.mfa import app_settings as mfa_settings
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from django.conf import settings
from django.contrib.auth.forms import AdminPasswordChangeForm
from django.contrib.auth.models import Group
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.mail.backends.console import EmailBackend as ConsoleBackend
from django.core.management import CommandError, call_command
from django.db import OperationalError, connection, transaction
from django.http import HttpResponse
from django.test import Client, override_settings
from django.urls import reverse

from accounts.adapter import ClosedSignupAdapter
from accounts.models import User
from accounts.roles import ROLES
from accounts.setup import setup_lab
from accounts.signin_log import log_sign_in_failure
from core import lab_profile_views, modules, security_settings
from core.management.commands import seed_demo
from core.models import Lab
from core.security_headers import DEFAULT_CONTENT_SECURITY_POLICY, SecurityHeadersMiddleware
from desk.models import ServiceRequest

pytestmark = pytest.mark.django_db

DEPLOYMENT = (
    # Adopted: the startup values the settings module records (config/settings.py) come from the
    # same environment as the settings derived from them.
    "STARTUP_DEBUG",
    "STARTUP_EMAIL_BACKEND",
    "DEBUG",
    "SECURE_HSTS_SECONDS",
    "SECURE_SSL_REDIRECT",
    "SESSION_COOKIE_SECURE",
    "CONTENT_SECURITY_POLICY",
    "EMAIL_BACKEND",
    "ACCOUNT_LOGIN_BY_CODE_ENABLED",
)


def facts():
    return {f.key: f for group in security_settings.in_force() for f in group.facts}


def assert_changed(key, setting):
    fact = facts()[key]
    assert fact.value == security_settings.CHANGED, (key, fact.value, fact.note)
    assert fact.source == "configuration" and setting in fact.note
    assert not fact.setting


@contextmanager
def deployment(monkeypatch, debug=False, **values):
    """Use the actual environment reader. Keep pytest's database and secret out of the result."""
    with monkeypatch.context() as patch:
        for key in (
            "EMAIL_BACKEND",
            "SECURE_HSTS_SECONDS",
            "SECURE_HSTS_INCLUDE_SUBDOMAINS",
            "CONTENT_SECURITY_POLICY",
        ):
            patch.delenv(key, raising=False)
        patch.setenv("DEBUG", str(debug))
        patch.setenv("SECRET_KEY", "synthetic-final-settings")
        for key, value in values.items():
            patch.setenv(key, value)
        namespace = runpy.run_path(str(Path(settings.BASE_DIR, "config/settings.py")))
        active = {key: namespace[key] for key in DEPLOYMENT}
        with override_settings(**active):
            yield


def second_editor(action):
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


@pytest.mark.parametrize(
    ("debug", "seconds", "policy", "expected_age", "code"),
    [
        (True, "3600", "", None, "On"),
        (False, "0", "  \n ", None, "Off"),
        (False, "61", "default-src 'self'", "max-age=61", "Off"),
        (False, "31536000", DEFAULT_CONTENT_SECURITY_POLICY, "max-age=31536000", "Off"),
    ],
)
def test_release_registry_and_headers_from_real_environment(
    monkeypatch, debug, seconds, policy, expected_age, code
):
    with deployment(
        monkeypatch, debug, SECURE_HSTS_SECONDS=seconds, CONTENT_SECURITY_POLICY=policy
    ):
        assert not security_settings.changed_settings(*security_settings.RELEASE)
        stated = facts()
        assert all(f.value != security_settings.CHANGED for f in stated.values())
        assert stated["sign-in-code"].value == code
        browser = Client()
        url = reverse("health")
        assert browser.get(url).status_code == (200 if debug else 301)
        response = browser.get(url, secure=True)
        assert response.get("Strict-Transport-Security") == expected_age
        assert response.get("Content-Security-Policy") == (policy.strip() or None)


@pytest.mark.parametrize("page", ["lab-profile", "security-settings"])
@pytest.mark.django_db(transaction=True)
def test_access_reads_account_status_and_lab_affiliation_in_the_snapshot(client, staff, page):
    """Measure the account read on an authorized request. No account privileges change."""
    actor = staff("final-active-admin", "admin")
    client.force_login(actor)
    observed = []

    def record(execute, sql, params, many, context):
        table = connection.ops.quote_name(User._meta.db_table)
        reads_actor = f"FROM {table}" in sql and actor.pk in (params or ())
        if reads_actor and f'{table}."is_active"' in sql and f'{table}."client_id"' in sql:
            with connection.cursor() as cursor:
                cursor.execute("SHOW transaction_isolation")
                observed.append(cursor.fetchone()[0])
        return execute(sql, params, many, context)

    with connection.execute_wrapper(record):
        assert client.get(reverse(page)).status_code == 200
    assert "repeatable read" in observed or "serializable" in observed, observed


@pytest.mark.django_db(transaction=True)
def test_profile_module_panel_matches_its_navigation(client, staff, monkeypatch):
    Lab.objects.create(name="Synthetic snapshot laboratory")
    reader = staff("final-profile-reader", "admin")
    editor = staff("final-module-editor", "admin")
    client.force_login(reader)
    original_render = lab_profile_views.render

    def enable_module():
        modules.set_enabled(
            actor=editor,
            key="nonconformance",
            enabled=True,
            expected_revision=0,
            reason="Synthetic module appointment",
        )

    def render(request, template, context, *args, **kwargs):
        second_editor(enable_module)
        return original_render(request, template, context, *args, **kwargs)

    monkeypatch.setattr(lab_profile_views, "render", render)
    response = client.get(reverse("lab-profile"))
    assert response.status_code == 200
    row = next(r for r in response.context["modules"] if r["key"] == "nonconformance")
    menu_link = f'href="{reverse("nonconformance:list")}"'.encode()
    assert (menu_link in response.content) == row["is_enabled"]


@pytest.mark.django_db(transaction=True)
def test_seed_eligibility_governs_the_entire_command(staff, monkeypatch):
    Lab.objects.create(name=seed_demo.DEMO_LAB_NAME)
    for role in ROLES:
        Group.objects.get_or_create(name=role)
    operator = staff("final-setup-owner", "admin")
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-final-seed-729")
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    seed_profile = seed_demo.Command._seed_profile
    renamed = []

    def rename():
        setup_lab(
            lab_name="Synthetic Independent Laboratory", update_lab=True, username=operator.username
        )

    def after_initial_eligibility(command):
        renamed.append(second_editor(rename))
        return seed_profile(command)

    monkeypatch.setattr(seed_demo.Command, "_seed_profile", after_initial_eligibility)
    try:
        call_command("seed_demo", stdout=StringIO(), stderr=StringIO())
    except CommandError as exc:
        assert "non-demo" in str(exc)
    if renamed == [True]:
        assert Lab.objects.get().name == "Synthetic Independent Laboratory"
        written = (
            User.objects.filter(username__startswith="demo_").count(),
            ServiceRequest.objects.count(),
        )
        assert written == (0, 0), "Demo accounts and tickets committed after the setup rename"
    else:
        assert Lab.objects.get().name == seed_demo.DEMO_LAB_NAME


@pytest.mark.parametrize(
    ("key", "setting"),
    [("https", "SECURE_SSL_REDIRECT"), ("session-cookie", "SESSION_COOKIE_SECURE")],
)
def test_debug_derived_flags_are_pinned_to_debug(monkeypatch, key, setting):
    with deployment(monkeypatch, False, SECURE_HSTS_SECONDS="0"):
        assert settings.SESSION_COOKIE_SECURE and settings.SECURE_SSL_REDIRECT
        with override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False):
            assert Client().get(reverse("health")).status_code == 200
            assert_changed(key, setting)


@pytest.mark.parametrize("key", ["sign-in-code", "browser-close"])
def test_mail_derived_code_switch_names_a_code_level_override(monkeypatch, key):
    with deployment(
        monkeypatch, False, EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend"
    ):
        assert settings.ACCOUNT_LOGIN_BY_CODE_ENABLED
        with override_settings(ACCOUNT_LOGIN_BY_CODE_ENABLED=False):
            assert_changed(key, "ACCOUNT_LOGIN_BY_CODE_ENABLED")


def test_csp_classification_uses_the_policy_delivered_to_the_browser(monkeypatch):
    formatted = DEFAULT_CONTENT_SECURITY_POLICY.replace("; ", ";\n  ")
    with deployment(monkeypatch, CONTENT_SECURITY_POLICY=formatted):
        response = Client().get(reverse("health"), secure=True)
        assert response["Content-Security-Policy"] == DEFAULT_CONTENT_SECURITY_POLICY
        assert facts()["content-security-policy"].value == "The application's default"


def test_hsts_fact_describes_only_valid_browser_lifetimes(monkeypatch):
    with deployment(monkeypatch, SECURE_HSTS_SECONDS="-1"):
        response = Client().get(reverse("health"), secure=True)
        header = response.get("Strict-Transport-Security", "")
        valid_age = re.search(r"(?:^|;)\s*max-age=([0-9]+)(?:;|$)", header)
        if not valid_age:
            note = facts()["https"].note.lower()
            assert "told to use only https for" not in note or "invalid" in note


def test_mail_fact_matches_the_backend_implementation(monkeypatch):
    # Django exposes this importable name for the console backend inside its file backend module.
    with deployment(
        monkeypatch, EMAIL_BACKEND="django.core.mail.backends.filebased.ConsoleEmailBackend"
    ):
        output = StringIO()
        sender = mail.get_connection(stream=output)
        assert type(sender) is ConsoleBackend
        sender.send_messages(
            [
                mail.EmailMessage(
                    "Synthetic backend check",
                    "Synthetic message",
                    "office@example.invalid",
                    ["inbox@example.invalid"],
                )
            ]
        )
        assert "Synthetic backend check" in output.getvalue()
        stated = facts()["sign-in-code"]
        assert "writes email to files" not in stated.note


def test_file_backend_without_a_directory_does_not_claim_it_writes_files(monkeypatch):
    with deployment(monkeypatch, EMAIL_BACKEND="django.core.mail.backends.filebased.EmailBackend"):
        try:
            sender = mail.get_connection()
        except (TypeError, ValueError, OSError, ImproperlyConfigured) as exc:
            stated = facts()["sign-in-code"]
            assert "writes email to files" not in stated.note, type(exc).__name__
        else:
            assert sender.file_path


class DeploymentAccountAdapter(ClosedSignupAdapter):
    """Synthetic deployment with a stricter password minimum."""

    def clean_password(self, password, user=None):
        if len(password) < 32:
            raise ValidationError("The deployment requires 32 characters.")
        return super().clean_password(password, user)


def test_password_fact_guards_the_account_forms_adapter(staff):
    account = staff("final-local", "technical")
    password = "Synthetic-Password-829!"
    with override_settings(ACCOUNT_ADAPTER=f"{__name__}.DeploymentAccountAdapter"):
        with pytest.raises(ValidationError, match="32"):
            get_adapter().clean_password(password, user=account)
        assert AdminPasswordChangeForm(
            account, {"password1": password, "password2": password}
        ).is_valid()
        assert_changed("passwords", "ACCOUNT_ADAPTER")


def test_login_methods_guard_the_claim_about_separate_identifiers():
    with override_settings(ACCOUNT_LOGIN_METHODS={"email"}):
        field = LoginForm().fields["login"]
        assert field.clean("synthetic@example.invalid") == "synthetic@example.invalid"
        with pytest.raises(ValidationError):
            field.clean("synthetic-operator")
        assert_changed("failed-sign-ins", "ACCOUNT_LOGIN_METHODS")


def test_cache_guard_includes_its_capacity_for_separate_counters():
    local_cache = {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "final-synthetic-counters",
        "OPTIONS": {"MAX_ENTRIES": 1, "CULL_FREQUENCY": 0},
    }
    with override_settings(CACHES={"default": local_cache}):
        cache.set("synthetic-counter-a", 1, 300)
        cache.set("synthetic-counter-b", 1, 300)
        assert cache.get("synthetic-counter-b") == 1 and cache.get("synthetic-counter-a") is None
        assert_changed("failed-sign-ins", "CACHES")


@pytest.mark.parametrize(
    ("key", "component"),
    [
        ("https", "django.middleware.security.SecurityMiddleware"),
        ("content-security-policy", "core.security_headers.SecurityHeadersMiddleware"),
        ("second-factor", "accounts.middleware.LockedCredentialsMiddleware"),
    ],
)
def test_facts_guard_the_middleware_that_enforces_the_statement(monkeypatch, key, component):
    with deployment(monkeypatch):
        with override_settings(MIDDLEWARE=[m for m in settings.MIDDLEWARE if m != component]):
            response = Client().get(reverse("health"), secure=(key != "https"))
            assert response.status_code == 200
            if key == "content-security-policy":
                assert not response.has_header("Content-Security-Policy")
            assert_changed(key, "MIDDLEWARE")


def test_signin_log_fact_guard_includes_logging_configuration(rf):
    logger = logging.getLogger("lab_ops.signin")
    original_level = logger.level
    # Django's incremental logging config changes only this logger, and is restored below.
    delta = {"version": 1, "incremental": True, "loggers": {"lab_ops.signin": {"level": "WARNING"}}}
    try:
        with override_settings(LOGGING=delta):
            logging.config.dictConfig(settings.LOGGING)
            log_sign_in_failure(
                sender=None, credentials={"username": "synthetic-unknown"}, request=rf.get("/")
            )
            assert not logger.isEnabledFor(logging.INFO)
            assert_changed("sign-in-records", "LOGGING")
    finally:
        logger.setLevel(original_level)


def test_recovery_options_guard_the_second_factor_description(staff):
    user = staff("final-factor-holder", "technical")
    with override_settings(MFA_RECOVERY_CODE_COUNT=0):
        assert len(RecoveryCodes.activate(user).generate_codes()) == 0
        assert_changed("second-factor", "MFA_RECOVERY_CODE_COUNT")


def test_factor_management_description_depends_on_supported_factors():
    with override_settings(MFA_SUPPORTED_TYPES=["totp"]):
        assert "recovery_codes" not in mfa_settings.SUPPORTED_TYPES
        assert_changed("second-factor-changes", "MFA_SUPPORTED_TYPES")


def test_email_code_fact_guard_covers_both_browser_trust_settings():
    from allauth.account import app_settings as account_settings

    with override_settings(
        ACCOUNT_LOGIN_BY_CODE_ENABLED=True, ACCOUNT_LOGIN_BY_CODE_TRUST_ENABLED=True
    ):
        assert account_settings.LOGIN_BY_CODE_TRUST_ENABLED
        assert not mfa_settings.TRUST_ENABLED and mfa_settings._TRUST_STAGE_ENABLED
        assert_changed("sign-in-code", "ACCOUNT_LOGIN_BY_CODE_TRUST_ENABLED")


def test_release_reauthentication_fact_accounts_for_users_without_a_challenge(monkeypatch):
    with deployment(monkeypatch, debug=True):
        user = User.objects.create_user("synthetic-code-only", "code-only@example.invalid")
        assert settings.ACCOUNT_LOGIN_BY_CODE_ENABLED and not user.has_usable_password()
        assert get_reauthentication_flows(user) == []
        stated = facts()["second-factor-changes"]
        text = f"{stated.value} {stated.note}".lower()
        assert any(
            w in text for w in ("usable password", "available method", "no method", "code-only")
        )


def test_blank_policy_keeps_a_more_specific_response_policy(rf):
    def response(request):
        return HttpResponse(headers={"Content-Security-Policy": "default-src 'none'"})

    with override_settings(CONTENT_SECURITY_POLICY="\r\n "):
        result = SecurityHeadersMiddleware(response)(rf.get("/"))
    assert result["Content-Security-Policy"] == "default-src 'none'"
