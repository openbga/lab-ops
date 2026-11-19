"""Fourth confirmation of the Security settings facts on synthetic data."""

import json
import os
import py_compile
import subprocess
import sys
import time
from textwrap import dedent
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile

import pytest
from allauth.account import app_settings
from allauth.account.internal.flows import code_verification
from allauth.core.internal import ratelimit
from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.models import Session
from django.test import override_settings
from django.urls import reverse

from accounts import signin_log
from accounts.tests.test_login import mailed_code, request_code
from core import security_settings
from tests.test_review_security_round1 import PASSWORD, assert_changed, fact, password_sign_in
from tests.test_review_security_round1_confirm3 import handed_off, record_descriptions
from tests.test_review_security_round1_fixes import change_password
from tests.test_review_security_round1_signoff import ROOT, probe


@pytest.mark.django_db
def test_record_describes_a_retained_session_after_deactivation(client, staff):
    account = staff("fourth-session-reader", "admin")
    password_sign_in(client, account)
    key = client.session.session_key
    with patch.object(signin_log.logger, "info") as recorder:
        account.is_active = False
        account.save(update_fields=["is_active"])
        assert client.get(reverse("security-settings")).status_code == 302
        assert Session.objects.get(session_key=key).get_decoded()[SESSION_KEY] == str(account.pk)
        account.is_active = True
        account.save(update_fields=["is_active"])
        page = client.get(reverse("security-settings"))
    assert page.status_code == 200 and client.session.session_key == key
    assert not recorder.called
    rows = {row.key: row for group in page.context["groups"] for row in group.facts}
    assert "signed in again" in rows["deactivation"].note
    claim = "a session ended by a new password or deactivation"
    assert claim not in rows["sign-in-records"].note.lower(), rows["sign-in-records"].note


@pytest.mark.django_db
def test_record_covers_an_existing_accounts_expired_emailed_code(client, staff, monkeypatch):
    account = staff("fourth-code-reader", "technical")
    start = time.time()
    clock = SimpleNamespace(time=lambda: start)
    monkeypatch.setattr(code_verification, "time", clock)
    assert app_settings.LOGIN_BY_CODE_ENABLED
    confirm = reverse("account_confirm_login_code")
    assert request_code(client, account.email).url == confirm
    issued = mailed_code()

    # Keep the browser, account and issued code fixed. Only the verification clock advances.
    clock.time = lambda: start + app_settings.LOGIN_BY_CODE_TIMEOUT + 1
    with patch.object(signin_log.logger, "info") as recorder:
        refused = client.post(confirm, {"code": issued})
    assert refused.url == reverse("account_request_login_code")
    assert SESSION_KEY not in client.session
    if not handed_off(recorder, "signin.failed", account):
        overstated = [
            surface
            for surface, description in record_descriptions().items()
            if "rejected codes for an existing account" in description.lower()
            and not any(term in description.lower() for term in ("expir", "pending", "within"))
        ]
        assert not overstated, overstated


@pytest.mark.django_db
def test_record_distinguishes_a_throttled_form_submission_from_a_password_check(client, staff):
    account = staff("fourth-rate-reader", "technical")
    assert app_settings.RATE_LIMITS["login"]
    # Supply a negative admission decision to the release view for a single ordinary request.
    with (
        patch.object(ratelimit, "consume", return_value=None) as admission,
        patch.object(signin_log.logger, "info") as recorder,
    ):
        response = client.post(
            reverse("account_login"), {"login": account.username, "password": PASSWORD}
        )
    assert admission.call_args.kwargs["action"] == "login"
    assert response.status_code == 429 and SESSION_KEY not in client.session
    if not recorder.called:
        note = fact("sign-in-records").note.lower()
        assert "refused passwords (throttled ones included)" not in note or any(
            qualifier in note for qualifier in ("429", "before authentication", "form submission")
        ), note


@pytest.mark.django_db
def test_record_checks_password_change_logout_when_describing_automatic_signouts(client, staff):
    account = staff("fourth-password-reader", "technical")
    password_sign_in(client, account)
    with (
        override_settings(ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE=True),
        patch.object(signin_log.logger, "info") as recorder,
    ):
        assert change_password(client).status_code == 302
        assert SESSION_KEY not in client.session
        assert handed_off(recorder, "signout", account)
        assert_changed("new-password", "ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE")
        record = fact("sign-in-records")
    if record.value == security_settings.CHANGED:
        assert "ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE" in record.note
    else:
        assert "a session ended by a new password" not in record.note.lower(), record.note


@pytest.mark.django_db
@pytest.mark.parametrize(
    "name,value",
    [
        ("ACCOUNT_ADAPTER", "allauth.account.adapter.DefaultAccountAdapter"),
        ("SESSION_ENGINE", "django.contrib.sessions.backends.cache"),
    ],
)
def test_record_guard_names_its_changed_setting(name, value):
    ordinary = fact("sign-in-records")
    with override_settings(**{name: value}):
        assert_changed("sign-in-records", name)
    assert ordinary.value != security_settings.CHANGED and fact("sign-in-records") == ordinary


@pytest.mark.parametrize("include_bytecode", [False, True], ids=["source", "source-and-bytecode"])
def test_pin_matches_readable_release_source_in_both_archive_layouts(tmp_path, include_bytecode):
    archive = tmp_path / "release.zip"
    with ZipFile(archive, "w") as bundle:
        for source in (ROOT / "config").glob("*.py"):
            bundle.write(source, f"config/{source.name}")
            if include_bytecode and source.stem in {"logging", "settings"}:
                compiled = tmp_path / ("compiled_" + source.stem + ".pyc")
                py_compile.compile(
                    str(source),
                    cfile=str(compiled),
                    doraise=True,
                    invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
                )
                bundle.write(compiled, f"config/{source.stem}.pyc")

    # Start Django with the archive first on the import path, before either config module loads.
    program = """
        import hashlib, json
        from importlib.util import find_spec
        from pathlib import Path
        from zipimport import zipimporter
        import django
        django.setup()
        from core import security_settings as security
        modules = {}
        for name in ("config.settings", "config.logging"):
            spec = find_spec(name)
            assert isinstance(spec.loader, zipimporter)
            source_path = Path(spec.origin).with_suffix(".py")
            raw = spec.loader.get_data(str(source_path))
            assert spec.loader.get_source(name) == raw.decode()
            modules[name] = [Path(spec.origin).suffix, hashlib.sha256(raw).hexdigest()]
        rows = []
        security._connection(rows)
        https = next(row for row in rows if row.key == "https")
        print(json.dumps([modules, https.value, https.note]))
    """
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "PYTHONPATH": os.pathsep.join(map(str, (archive, ROOT))),
        "DEBUG": "true",
        "DATABASE_URL": "postgresql://synthetic@127.0.0.1:1/fourth_confirmation_unused",
    }
    result = subprocess.run(
        [sys.executable, "-c", dedent(program)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    modules, value, note = json.loads(result.stdout)
    suffix = ".pyc" if include_bytecode else ".py"
    assert modules == {
        "config.settings": [suffix, security_settings.RELEASE_SETTINGS_SHA256],
        "config.logging": [suffix, security_settings.RELEASE_LOGGING_SHA256],
    }
    assert value == "Not required", note


def test_source_of_inactive_module_with_standard_lazy_loader_is_still_readable(tmp_path):
    # The active settings use the release logging. The stock settings remain unimported.
    (tmp_path / "fourth_settings.py").write_bytes((ROOT / "config/settings.py").read_bytes())
    digest, note = probe(
        tmp_path,
        """
        import hashlib, sys
        from importlib.machinery import FileFinder, SourceFileLoader
        from importlib.util import LazyLoader, find_spec
        import config
        directory, = config.__path__
        finder = FileFinder(directory, (LazyLoader.factory(SourceFileLoader), [".py"]))
        with patch.dict(sys.path_importer_cache, {directory: finder}):
            spec = find_spec("config.settings")
            assert isinstance(spec.loader, LazyLoader)
            assert "config.settings" not in sys.modules
            source = Path(spec.origin).read_bytes()
            row = https()
            assert "config.settings" not in sys.modules
            print(json.dumps([hashlib.sha256(source).hexdigest(), row.note]))
        """,
        module="fourth_settings",
    )
    assert digest == security_settings.RELEASE_SETTINGS_SHA256
    assert "fourth_settings" in note
    assert "config/settings.py could not be read" not in note, note


def test_unreadable_logging_source_names_only_that_file_and_is_restored_by_its_record():
    # Adopted: digests are recorded while the configuration loads (confirmation 5 resolution).
    # The source is read once, when the file loads (a restart reads it again), so an unreadable
    # source is a recorded "" and there is no retry within the process.
    value, note, recovered = probe(
        ROOT,
        """
        recorded = settings.LOGGING_SHA256
        settings.LOGGING_SHA256 = ""
        initial = https()
        again = https()
        assert (initial.value, initial.note) == (again.value, again.note)
        settings.LOGGING_SHA256 = recorded
        print(json.dumps([initial.value, initial.note, https().value]))
        """,
    )
    assert value == security_settings.CHANGED
    assert "config/logging.py could not be read" in note
    assert "config/settings.py" not in note and "synthetic loader" not in note
    assert recovered == "Not required"
