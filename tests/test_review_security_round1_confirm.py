"""Confirmation of the Security settings fixes at 8612a97, using synthetic records only."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest
from django.test import override_settings
from django.urls import reverse

from accounts.models import User
from accounts.roles import OPERATIONAL_ROLES
from accounts.setup import setup_lab
from core import context_processors, lab_profile_views, security_settings, security_settings_views
from core.models import Lab
from tests.test_review_security_round1_signoff import probe, second_connection

ROOT = Path(__file__).resolve().parents[1]
PAGES = {"lab-profile": lab_profile_views, "security-settings": security_settings_views}
CHANGED = "Changed in the application configuration"


def test_loaded_module_survives_nested_settings_and_environment_changes(monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "synthetic.unused")
    with override_settings(DEBUG=False), override_settings(EMAIL_TIMEOUT=31):
        assert security_settings.settings_module_name() == "config.settings"
        assert security_settings.changed_settings(*security_settings.FILES) == []


@pytest.fixture
def copied_config(tmp_path):
    shutil.copytree(
        ROOT / "config", tmp_path / "config", ignore=shutil.ignore_patterns("__pycache__")
    )
    return tmp_path


def test_changed_module_and_both_files_are_all_named(copied_config):
    # Adopted: settings record both digests while loading (confirmation 6 resolution).
    # F6-02 decision: when another settings module is loaded, only that module is named; what it
    # records describes itself, not config/settings.py or config/logging.py.
    for name in ("settings.py", "logging.py"):
        with (copied_config / "config" / name).open("a") as source:
            source.write("\n# Synthetic packaging annotation.\n")
    (copied_config / "confirm_settings.py").write_text("from config.settings import *\n")
    value, note = probe(
        copied_config,
        """
        from django.test import override_settings
        with override_settings(SECURE_HSTS_SECONDS=60):
            row = https()
        print(json.dumps([row.value, row.note]))
        """,
        module="confirm_settings",
    )
    assert value == CHANGED
    assert all(name in note for name in ("confirm_settings", "SECURE_HSTS_SECONDS")), note
    # The only mention of config/settings.py is where the changed setting lives.
    assert "config/logging.py" not in note, note
    assert "config/settings.py differs" not in note and "could not be read" not in note, note


def test_manually_configured_django_is_not_described_as_loading_another_module():
    # Django supports settings.configure() as an alternative to a named settings module.
    # Load the release values in a fresh process and pass them to that entry point directly.
    program = """
        import json, runpy
        import django
        from django.conf import settings

        values = runpy.run_path("config/settings.py")
        settings.configure(**{name: value for name, value in values.items() if name.isupper()})
        django.setup()
        from core import security_settings as security
        rows = []
        security._connection(rows)
        https = next(row for row in rows if row.key == "https")
        print(json.dumps([settings.SETTINGS_MODULE, security.settings_module_name(),
                          https.value, https.note]))
    """
    env = dict(os.environ, DEBUG="true", DATABASE_URL="postgresql://synthetic@127.0.0.1:1/unused")
    env.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [sys.executable, "-c", dedent(program)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    loaded, reported, value, note = json.loads(result.stdout)
    assert loaded is None and not reported and value == CHANGED
    assert "loads its settings from another module" not in note, note


def test_logging_claim_tracks_delivery_to_the_configured_handler():
    before, after, accepts_info, value, note = probe(
        ROOT,
        """
        import logging
        from io import StringIO
        from accounts.models import User
        from accounts.signin_log import log_sign_out, logger

        # This process has only the release console handler. Observe its output in memory.
        console, = logging.getLogger().handlers
        output = StringIO()
        console.setStream(output)
        def recorded():
            output.seek(0)
            output.truncate(0)
            log_sign_out(None, None, User(username="synthetic-confirmation"))
            return "signout user=synthetic-confirmation" in output.getvalue()

        before = recorded()
        options = settings.LOGGING["handlers"]["console"]
        options["level"] = "WARNING"
        console.setLevel(options["level"])
        after = recorded()
        # Adapted: the second-factor rule is a database reading too since the required second
        # factor change.
        from core.models import RevealConfirmationRule, SecondFactorRule
        with patch.multiple(security, _second_factor_counts=lambda: (0, 0, 0, 0),
                            _second_factor_rule=lambda: SecondFactorRule(pk=1),
                            _reveal_confirmation_rule=lambda: RevealConfirmationRule(pk=1)):
            rows = []
            security._sign_in(rows)
        fact = next(row for row in rows if row.key == "sign-in-records")
        print(json.dumps([before, after, logger.isEnabledFor(logging.INFO), fact.value, fact.note]))
        """,
    )
    assert before and not after and accepts_info
    assert_no_delivery_claim(value, note)


@pytest.mark.parametrize("filename", ["settings.py", "logging.py"])
def test_unreadable_release_file_is_not_reported_as_modified(filename):
    expected = (
        security_settings.RELEASE_SETTINGS_SHA256
        if filename == "settings.py"
        else security_settings.RELEASE_LOGGING_SHA256
    )
    digest, recovered, row_value, note = probe(
        ROOT,
        f"""
        import hashlib
        from importlib.machinery import SourceFileLoader
        file = Path("config/{filename}").resolve()
        get_data = SourceFileLoader.get_data
        digest = hashlib.sha256(file.read_bytes()).hexdigest()

        # Adopted: the digest reads through the module's loader (resolution of confirmation 3).
        def unavailable(self, path):
            if Path(path) == file:
                raise OSError("Synthetic temporary read failure")
            return get_data(self, path)

        with patch.object(SourceFileLoader, "get_data", unavailable):
            row = https()
        recovered = security.module_digest("config." + file.stem)
        print(json.dumps([digest, recovered, row.value, row.note]))
        """,
    )
    assert digest == recovered == expected
    if row_value == CHANGED:
        assert filename in note
    assert f"config/{filename} differs" not in note, note


def test_one_page_gives_one_answer_about_the_release_files():
    first, next_reading = probe(
        ROOT,
        """
        from importlib.machinery import SourceFileLoader
        get_data = SourceFileLoader.get_data
        logging_file = Path("config/logging.py").resolve()
        attempts = []
        # Adopted: the digest reads through the module's loader (resolution of confirmation 3).
        def try_read(self, path):
            if Path(path) == logging_file:
                attempts.append(path)
                if len(attempts) == 1:
                    raise OSError("Synthetic first read unavailable")
            return get_data(self, path)

        def config_states():
            return {row.key: row.value == security.CHANGED for group in security.in_force()
                    if group.key != "patient-information" for row in group.facts}

        # Only the database readings are replaced (adapted: the session limits are one since the
        # automatic sign-out change). Each fact checks the real release files.
        # The second-factor rule too, since the required second factor change.
        from core.models import RevealConfirmationRule, SecondFactorRule, SessionLimit
        limits = [SessionLimit(audience=audience, inactivity_minutes=30, maximum_hours=12)
                  for audience in ("staff", "portal")]
        with patch.multiple(security, _second_factor_counts=lambda: (0, 0, 0, 0),
                            patient_capability_holders=lambda: 0, _patient_fields=lambda: 0,
                            review_capability_holders=lambda: 0,
                            emergency_capability_holders=lambda: 0,
                            _review_interval=lambda: None,
                            _session_limits=lambda: limits,
                            _second_factor_rule=lambda: SecondFactorRule(pk=1),
                            _reveal_confirmation_rule=lambda: RevealConfirmationRule(pk=1)):
            with patch.object(SourceFileLoader, "get_data", try_read):
                first = config_states()
            print(json.dumps([first, config_states()]))
        """,
    )
    assert first and not any(next_reading.values())
    assert all(first.values()) or not any(first.values()), first


def test_set_by_help_covers_a_logging_file_change(copied_config):
    with (copied_config / "config/logging.py").open("a") as source:
        source.write("\n# Synthetic packaging annotation.\n")
    assert (copied_config / "config/settings.py").read_bytes() == (
        ROOT / "config/settings.py"
    ).read_bytes()
    source_label, note = probe(
        copied_config,
        "row = https()\nprint(json.dumps([row.source_label, row.note]))",
    )
    assert source_label == "Application configuration" and "config/logging.py" in note
    help_doc = json.loads((ROOT / "docs/pages/core/security-settings.json").read_text())
    definition = next(
        item["description"]
        for section in help_doc["sections"]
        for item in section.get("items", [])
        if item["label"] == "Set by"
    )
    assert "only by changing that file" not in definition


def before_render(monkeypatch, view, change):
    render = view.render

    def draw(request, template, context):
        second_connection(change)
        return render(request, template, context)

    monkeypatch.setattr(view, "render", draw)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("page", PAGES)
def test_menu_context_keeps_roles_and_client_management_from_view(page, client, staff, monkeypatch):
    Lab.objects.create(name="Synthetic Confirmation Laboratory")
    role = "technical" if page == "lab-profile" else "admin"
    reader = staff("confirmation-context", role)
    client.force_login(reader)
    # Supply distinguishable values from the later template context processor. The two
    # snapshot values supplied by the views must take precedence in the rendered response.
    alternate_roles = {"account_manager"} if role == "technical" else {"technical"}
    # Adapted (audit review): the context processor reads the roles with the capabilities.
    monkeypatch.setattr(
        context_processors, "roles_and_capabilities", lambda user: (alternate_roles, set())
    )
    response = client.get(reverse(page))
    assert response.status_code == 200
    expected = {"admin", *OPERATIONAL_ROLES} if role == "admin" else {"technical"}
    assert response.context["user_roles"] == expected
    assert response.context["can_manage_clients"] is (role == "admin")
    import_link = f'href="{reverse("dataimport:index")}"'.encode()
    assert (import_link in response.content) is (role == "admin")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("page", PAGES)
def test_account_dropdown_and_sidebar_show_the_same_role_reading(page, client, staff, monkeypatch):
    Lab.objects.create(name="Synthetic Role Display Laboratory")
    # The reader remains authorized throughout. Account manager is added only after the snapshot.
    role = "admin" if page == "security-settings" else "technical"
    reader = staff("confirmation-role-display", role)
    client.force_login(reader)

    def displayed_roles(response):
        return response.content.split(b'<span class="vr-user-roles">', 1)[1].split(b"</span>", 1)[0]

    baseline = client.get(reverse(page))
    assert baseline.status_code == 200
    before_render(
        monkeypatch,
        PAGES[page],
        lambda: setup_lab(username=reader.username, roles=["account_manager"]),
    )
    response = client.get(reverse(page))
    assert response.status_code == 200
    assert reader.groups.filter(name="account_manager").exists()
    assert displayed_roles(response) == displayed_roles(baseline)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("page", PAGES)
def test_owner_menu_item_uses_the_account_reread_in_the_page(page, client, staff, monkeypatch):
    Lab.objects.create(name="Synthetic Owner Display Laboratory")
    reader = staff("confirmation-owner-display", "admin")
    client.force_login(reader)
    view = PAGES[page]
    reading_account = view.reading_account
    observed = []

    def read(request):
        # The lab role is unchanged. The deployment owner flag is needed solely for the menu.
        second_connection(
            lambda: User.objects.filter(pk=reader.pk).update(is_staff=True, is_superuser=True)
        )
        account = reading_account(request)
        observed.append(account.is_staff and account.is_superuser)
        return account

    monkeypatch.setattr(view, "reading_account", read)
    response = client.get(reverse(page))
    assert response.status_code == 200 and observed == [True]
    owner_item = f'href="{reverse("admin:index")}"'.encode()
    assert owner_item in response.content


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("page", PAGES)
def test_lab_name_and_contact_actions_agree_on_lab_setup(page, client, staff, monkeypatch):
    reader = staff("confirmation-lab-banner", "admin")
    client.force_login(reader)
    name = "Synthetic Newly Configured Laboratory"
    before_render(
        monkeypatch, PAGES[page], lambda: setup_lab(lab_name=name, username=reader.username)
    )
    response = client.get(reverse(page))
    assert response.status_code == 200 and Lab.objects.get().name == name
    named = name.encode() in response.content
    contact_url = reverse("lab-contact-edit", args=["security"])
    edit_link = f'href="{contact_url}'.encode() in response.content
    assert named == edit_link, (named, edit_link)


def assert_no_delivery_claim(value, note):
    """Decided after the second confirmation (security-settings-round-1-confirmation-2-resolution):
    the sign-in record fact says only what the application passes to its sign-in log; where the
    log goes is the logging configuration's, so no logging change can make it false."""
    assert value == "Sign-in log, and the last sign-in time", value
    assert "set by the logging configuration" in note, note
    assert "written to" not in note and "writes" not in note, note
