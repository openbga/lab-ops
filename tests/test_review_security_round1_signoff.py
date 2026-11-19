"""Independent sign-off of the settings pin and the transaction boundaries at bd30ff3.

Adopted (original: .local/review-history/test_security_round1_signoff_original.py). The pin tests
follow the resolution of SO-02: each configuration file has its own pinned digest
(RELEASE_SETTINGS_SHA256 for config/settings.py, RELEASE_LOGGING_SHA256 for config/logging.py),
read by module_digest(); what they assert is unchanged.
"""

import hashlib
import json
import logging.config
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from textwrap import dedent

import pytest
from django.conf import settings
from django.core.management import CommandError, call_command
from django.db import connection, transaction
from django.urls import reverse

from accounts.models import User
from accounts.setup import setup_lab
from accounts.signin_log import logger as signin_logger
from core import lab_profile, lab_profile_views, modules, security_settings, security_settings_views
from core.management.commands.seed_demo import DEMO_LAB_NAME
from core.models import Lab, LabContact
from desk.models import ServiceRequest

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_MODULE = "config.settings"
CHANGED = "Changed in the application configuration"


@pytest.fixture
def deployment_copy(tmp_path):
    """A cold process imports temporary copies of the release configuration only."""
    shutil.copytree(
        ROOT / "config", tmp_path / "config", ignore=shutil.ignore_patterns("__pycache__")
    )
    return tmp_path


def probe(directory, body, *, module=SETTINGS_MODULE):
    # These checks use only the connection facts; they require no database or email service.
    setup = """
        import json
        import os
        from pathlib import Path
        from unittest.mock import patch
        import django
        django.setup()
        from django.conf import settings
        from core import security_settings as security

        def https():
            rows = []
            security._connection(rows)
            return next(row for row in rows if row.key == "https")
    """
    process = subprocess.run(
        [sys.executable, "-c", dedent(setup) + dedent(body)],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": module,
            "DEBUG": "true",
            "DATABASE_URL": "postgresql://synthetic@127.0.0.1:1/signoff_unused",
            "PYTHONPATH": str(ROOT),
        },
        cwd=directory,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return json.loads(process.stdout)


@pytest.mark.parametrize("changed", [None, "settings.py", "logging.py"])
def test_pin_checks_both_files_from_a_cold_start(deployment_copy, changed):
    if changed:
        with (deployment_copy / "config" / changed).open("a") as source:
            source.write("\n# Synthetic packaging annotation.\n")
    settings_digest, logging_digest, value = probe(
        deployment_copy,
        """
        print(json.dumps([
            security.module_digest("config.settings"),
            security.module_digest("config.logging"),
            https().value,
        ]))
    """,
    )
    pins = {
        "settings.py": (settings_digest, security_settings.RELEASE_SETTINGS_SHA256),
        "logging.py": (logging_digest, security_settings.RELEASE_LOGGING_SHA256),
    }
    for name, (digest, pinned) in pins.items():
        source = (deployment_copy / "config" / name).read_bytes()
        assert digest == hashlib.sha256(source).hexdigest()
        assert (digest == pinned) == (changed != name)
    assert value == ("Not required" if changed is None else CHANGED)


def test_module_identity_remains_the_one_django_loaded(deployment_copy):
    loaded, reported, value = probe(
        deployment_copy,
        """
        loaded = settings.SETTINGS_MODULE
        os.environ.pop("DJANGO_SETTINGS_MODULE")
        print(json.dumps([loaded, security.settings_module_name(), https().value]))
    """,
    )
    assert loaded == SETTINGS_MODULE
    assert (reported, value) == (loaded, "Not required")


@pytest.mark.parametrize("difference", ["logging file", "alternate module"])
def test_pinned_configuration_diagnostic_identifies_the_actual_difference(
    deployment_copy, difference
):
    module = SETTINGS_MODULE
    if difference == "logging file":
        with (deployment_copy / "config/logging.py").open("a") as source:
            source.write("\n# Synthetic packaging annotation.\n")
    else:
        module = "signoff_settings"
        (deployment_copy / f"{module}.py").write_text("from config.settings import *\n")
    assert (deployment_copy / "config/settings.py").read_bytes() == (
        ROOT / "config/settings.py"
    ).read_bytes()
    value, note = probe(
        deployment_copy,
        """
        row = https()
        print(json.dumps([row.value, row.note]))
    """,
        module=module,
    )
    assert value == CHANGED
    assert "config/settings.py differs" not in note, (difference, note)


def test_pin_recovers_after_a_transient_source_read_error(deployment_copy):
    value, digest = probe(
        deployment_copy,
        """
        from importlib.machinery import SourceFileLoader
        get_data = SourceFileLoader.get_data
        # Adopted: the digest reads through the module's loader (resolution of confirmation 3).
        def unavailable(self, path):
            if Path(path).name == "logging.py":
                raise OSError("Synthetic temporary read failure")
            return get_data(self, path)
        with patch.object(SourceFileLoader, "get_data", unavailable):
            https()
        print(json.dumps([https().value, security.module_digest("config.logging")]))
    """,
    )
    assert (value, digest) == ("Not required", security_settings.RELEASE_LOGGING_SHA256)


@pytest.mark.django_db
def test_file_pin_withholds_all_configuration_derived_rows(monkeypatch):
    monkeypatch.setattr(security_settings, "module_digest", lambda name: "synthetic changed digest")
    for group in security_settings.in_force():
        for row in group.facts:
            if group.key == "patient-information":
                assert row.value != CHANGED
            else:
                assert row.value == CHANGED and row.source == "configuration"
                assert not row.setting


@pytest.mark.django_db
def test_signin_record_fact_detects_an_in_place_logging_change(monkeypatch):
    # Apply exactly the configured level in an incremental logging reconfiguration. Restore the
    # running logger as well as the settings mapping before the next test uses either.
    name = signin_logger.name
    update = {"level": "WARNING"}
    monkeypatch.setitem(settings.LOGGING["loggers"], name, update)
    old_level = signin_logger.level
    try:
        logging.config.dictConfig({"version": 1, "incremental": True, "loggers": {name: update}})
        assert not signin_logger.isEnabledFor(logging.INFO)
        rows = [f for group in security_settings.in_force() for f in group.facts]
        item = next(f for f in rows if f.key == "sign-in-records")
        assert_no_delivery_claim(item.value, item.note)
    finally:
        signin_logger.setLevel(old_level)


def second_connection(write):
    def commit():
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '850ms'")
                write()
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(commit).result(timeout=8)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("page", ["lab-profile", "security-settings"])
def test_menu_and_contact_panel_describe_one_committed_state(page, client, staff, monkeypatch):
    Lab.objects.create(name="Synthetic snapshot laboratory")
    reader = staff("signoff-reader", "admin")
    editor = staff("signoff-colleague", "admin")
    client.force_login(reader)
    view = lab_profile_views if page == "lab-profile" else security_settings_views
    original_render = view.render
    desk = "Synthetic Privacy Office"

    def appoint_and_enable():
        modules.set_enabled(
            actor=editor,
            key="nonconformance",
            enabled=True,
            expected_revision=0,
            reason="Synthetic setup change",
        )
        lab_profile.save_contact(
            actor=editor,
            kind="privacy",
            values={"name": desk},
            expected_revision=0,
            reason="Synthetic appointment",
        )

    def draw(request, template, context):
        second_connection(appoint_and_enable)
        return original_render(request, template, context)

    monkeypatch.setattr(view, "render", draw)
    response = client.get(reverse(page))
    assert response.status_code == 200
    assert LabContact.objects.get(kind="privacy").name == desk
    menu_has_module = f'href="{reverse("nonconformance:list")}"'.encode() in response.content
    panel_has_office = any(row.name == desk for row in response.context["contacts"])
    assert menu_has_module == panel_has_office, (menu_has_module, panel_has_office)


@pytest.mark.django_db(transaction=True)
def test_profile_navigation_and_actions_share_the_reader_role(client, staff, monkeypatch):
    Lab.objects.create(name="Synthetic role-change laboratory")
    reader = staff("signoff-staff-reader", "technical")
    client.force_login(reader)
    render = lab_profile_views.render

    def draw(request, template, context):
        second_connection(lambda: setup_lab(username=reader.username, roles=["admin"]))
        return render(request, template, context)

    monkeypatch.setattr(lab_profile_views, "render", draw)
    response = client.get(reverse("lab-profile"))
    assert response.status_code == 200
    # Both roles may open the profile. This reads the returned page and follows no links.
    security_menu = f'href="{reverse("security-settings")}"'.encode() in response.content
    edit_action = f'href="{reverse("lab-profile-edit")}"'.encode() in response.content
    assert reader.groups.filter(name="admin").exists()
    assert security_menu == edit_action, (security_menu, edit_action)


@pytest.mark.django_db(transaction=True)
def test_seed_refuses_a_rename_committed_before_the_name_lock(staff, monkeypatch):
    Lab.objects.create(name=DEMO_LAB_NAME)
    owner = staff("signoff-setup-owner", "admin")
    monkeypatch.setenv("DEMO_PASSWORD", "Synthetic-seed-signoff-932")
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    renamed = []

    def rename():
        setup_lab(lab_name="Synthetic Renamed Laboratory", update_lab=True, username=owner.username)

    def before_lock(execute, sql, params, many, context):
        if not renamed and 'FROM "core_lab"' in sql and "FOR UPDATE" in sql:
            second_connection(rename)
            renamed.append(True)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(before_lock), pytest.raises(CommandError, match="non-demo"):
        call_command("seed_demo", stdout=StringIO(), stderr=StringIO())
    assert renamed and Lab.objects.get().name == "Synthetic Renamed Laboratory"
    assert not User.objects.filter(username__startswith="demo_").exists()
    assert not LabContact.objects.exists()
    assert not ServiceRequest.objects.exists()


def assert_no_delivery_claim(value, note):
    """Decided after the second confirmation (security-settings-round-1-confirmation-2-resolution):
    the sign-in record fact says only what the application passes to its sign-in log; where the
    log goes is the logging configuration's, so no logging change can make it false."""
    assert value == "Sign-in log, and the last sign-in time", value
    assert "set by the logging configuration" in note, note
    assert "written to" not in note and "writes" not in note, note
