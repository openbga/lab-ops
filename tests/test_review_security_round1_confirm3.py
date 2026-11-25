"""Third confirmation of the Security settings decision at fc02be0."""

import hashlib
import importlib
import importlib.util
import json
import os
import py_compile
import shutil
import subprocess
import sys
from importlib import invalidate_caches
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile

import pytest
from allauth.account.adapter import get_adapter
from allauth.account.internal.flows.login_by_code import LoginCodeVerificationProcess
from allauth.account.models import Login
from django.contrib.auth import SESSION_KEY
from django.core.exceptions import ValidationError
from django.test import Client, override_settings
from django.urls import reverse

from accounts import signin_log
from config.digest import source_sha256
from core import security_settings
from tests.test_review_security_round1 import PASSWORD
from tests.test_review_security_round1_signoff import probe

ROOT = Path(__file__).resolve().parents[1]
CHANGED = security_settings.CHANGED


def record_fact():
    return next(
        fact
        for group in security_settings.in_force()
        for fact in group.facts
        if fact.key == "sign-in-records"
    )


def record_descriptions():
    reference = (ROOT / "docs/safeguards.md").read_text().splitlines()
    headings = ("Information system activity review", "Log-in monitoring")
    return {"Security settings": record_fact().note} | {
        heading: next(line for line in reference if line.startswith(f"| {heading}"))
        for heading in headings
    }


def unqualified_descriptions(kind):
    # The page and both reference rows can satisfy the contract by describing the narrower
    # event set. The implementation can also satisfy it by passing the documented events.
    scope = {
        "failure": (
            "credential failures",
            "invalid credentials",
            "rejected passwords",
            "inactive account",
            "authentication signal",
            "reported failures",
        ),
        "sign-out": (
            "explicit sign-out",
            "explicitly sign",
            "explicit logout",
            "session invalidat",
            "password change",
            "sign out action",
        ),
    }[kind]
    blanket = ("each sign-in, failure and sign-out", "sign-ins, failures and sign-outs are passed")
    return [
        surface
        for surface, text in record_descriptions().items()
        if any(phrase in text.lower() for phrase in blanket)
        and not any(qualifier in text.lower() for qualifier in scope)
    ]


def handed_off(recorder, event, user):
    return any(call.args[1:] == (event, user.username) for call in recorder.call_args_list)


@pytest.mark.django_db
def test_record_description_covers_a_known_account_refused_at_sign_in(client, staff):
    user = staff("third-inactive-reader", "technical")
    user.is_active = False
    user.save(update_fields=["is_active"])
    with patch.object(signin_log.logger, "info") as recorder:
        response = client.post(
            reverse("account_login"), {"login": user.username, "password": PASSWORD}
        )
    assert response.status_code == 302 and response.url == reverse("account_inactive")
    assert SESSION_KEY not in client.session
    if not handed_off(recorder, "signin.failed", user):
        assert not (unqualified := unqualified_descriptions("failure")), unqualified


@pytest.mark.django_db
def test_record_description_covers_counted_code_rejections_without_an_account(rf, staff):
    user = staff("third-code-reader", "technical")

    def notification(identified_user):
        # Inspect the failure-accounting callback only. No address, code or password is submitted.
        state = LoginCodeVerificationProcess.initial_state(identified_user)
        stage = SimpleNamespace(request=rf.get("/"), state=state, login=Login(user=identified_user))
        process = LoginCodeVerificationProcess(stage)
        with patch.object(process, "persist"), patch.object(signin_log.logger, "info") as recorder:
            process.record_invalid_attempt()
        assert state["failed_attempts"] == 1
        return recorder

    assert handed_off(notification(user), "signin.failed", user)
    if not notification(None).called:
        assert not (unqualified := unqualified_descriptions("failure")), unqualified


@pytest.mark.django_db
def test_record_description_distinguishes_password_signouts_from_explicit_logout(client, staff):
    user = staff("third-password-reader", "admin")
    other = Client()
    for browser in (client, other):
        response = browser.post(
            reverse("account_login"), {"login": user.username, "password": PASSWORD}
        )
        assert response.status_code == 302 and browser.session[SESSION_KEY] == str(user.pk)

    new_password = "Synthetic-account-change-8541"
    with patch.object(signin_log.logger, "info") as recorder:
        change = client.post(
            reverse("account_change_password"),
            {"oldpassword": PASSWORD, "password1": new_password, "password2": new_password},
        )
        assert change.status_code == 302
        user.refresh_from_db()
        assert user.check_password(new_password)
        checked = other.get(reverse("dashboard"))
        assert checked.status_code == 302 and SESSION_KEY not in other.session
        assert client.session[SESSION_KEY] == str(user.pk)
        automatic = handed_off(recorder, "signout", user)
        recorder.reset_mock()
        assert client.post(reverse("account_logout")).status_code == 302
        assert handed_off(recorder, "signout", user)
    if not automatic:
        assert not (unqualified := unqualified_descriptions("sign-out")), unqualified


@pytest.mark.django_db
def test_record_fact_guard_covers_the_account_adapters_failure_notification(rf, staff):
    user = staff("third-adapter-reader", "technical")
    request = rf.get("/")
    default_adapter = "allauth.account.adapter.DefaultAccountAdapter"

    def handles_refusal():
        # Both adapters receive the same synthetic negative admission decision. Neither
        # authenticates anything. Compare the callback at the logger boundary.
        with (
            patch("allauth.account.adapter.ratelimit.consume", return_value=False),
            patch.object(signin_log.logger, "info") as recorder,
            pytest.raises(ValidationError),
        ):
            get_adapter(request).pre_authenticate(request, username=user.username)
        return handed_off(recorder, "signin.failed", user)

    assert handles_refusal()
    with override_settings(ACCOUNT_ADAPTER=default_adapter):
        assert not handles_refusal()
        fact = record_fact()
    if fact.value == CHANGED:
        assert "ACCOUNT_ADAPTER" in fact.note
    else:
        assert "each sign-in, failure and sign-out" not in fact.note.lower(), fact.note


def test_default_console_and_security_page_help_match_the_logging_decision():
    stdout, rendered = probe(
        ROOT,
        """
        import logging, sys
        from io import StringIO
        from accounts.models import User
        from accounts.signin_log import log_sign_out
        console, = logging.getLogger().handlers
        stdout = console.stream is sys.stdout
        output = StringIO()
        console.setStream(output)
        log_sign_out(None, None, User(username="third-console-reader"))
        print(json.dumps([stdout, output.getvalue()]))
        """,
    )
    assert (
        stdout and "lab_ops.signin" in rendered and "signout user=third-console-reader" in rendered
    )
    ref = (ROOT / "docs/safeguards.md").read_text()
    assert "this release's logging configuration writes them to the console" in ref
    help_text = (ROOT / "docs/pages/core/security-settings.json").read_text().lower()
    assert "written to the application log" not in help_text


@pytest.mark.django_db
def test_security_settings_help_explains_the_unverified_row(client, staff, monkeypatch):
    reader = staff("third-help-reader", "admin")
    client.force_login(reader)
    digest = security_settings.module_digest

    def read(module):
        return "" if module == "config.logging" else digest(module)

    monkeypatch.setattr(security_settings, "module_digest", read)
    response = client.get(reverse("security-settings"))
    assert response.status_code == 200
    shown = next(
        row for group in response.context["groups"] for row in group.facts if row.key == "https"
    )
    assert shown.value == CHANGED and "config/logging.py could not be read" in shown.note
    help_doc = response.context["page_documentation"]
    fields = {
        item["label"]: item["description"]
        for section in help_doc["sections"]
        if section["title"] == "Groups of settings"
        for item in section["items"]
    }
    assert all(
        name in fields["Set by"]
        for name in ("config/settings.py", "config/logging.py", "another settings module")
    )
    assert "could not be read" in fields["Details"]


def test_byte_identical_source_in_an_importable_zip_matches_the_release_pins(tmp_path):
    archive = tmp_path / "synthetic-release.zip"
    with ZipFile(archive, "w") as bundle:
        for path in (ROOT / "config").glob("*.py"):
            bundle.write(path, arcname=f"config/{path.name}")
    program = """
        import hashlib, json
        from importlib.util import find_spec
        import django
        django.setup()
        from core import security_settings as security
        independently_read = {}
        for module in ("config.settings", "config.logging"):
            spec = find_spec(module)
            assert type(spec.loader).__module__ == "zipimport"
            source = spec.loader.get_data(spec.origin)
            independently_read[module] = hashlib.sha256(source).hexdigest()
        facts = []
        security._connection(facts)
        https = next(row for row in facts if row.key == "https")
        print(json.dumps([independently_read, https.value, https.note]))
    """
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(map(str, [archive, ROOT])),
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "DEBUG": "true",
        "DATABASE_URL": "postgresql://synthetic@127.0.0.1:1/third_confirmation_unused",
    }
    result = subprocess.run(
        [sys.executable, "-c", dedent(program)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    digests, value, note = json.loads(result.stdout)
    assert digests == {
        "config.settings": security_settings.RELEASE_SETTINGS_SHA256,
        "config.logging": security_settings.RELEASE_LOGGING_SHA256,
    }
    assert value == "Not required", note


def test_inactive_package_initialization_cannot_abort_the_facts(tmp_path):
    ignore = shutil.ignore_patterns("__pycache__")
    custom = tmp_path / "synthetic_site"
    stock = tmp_path / "config"
    shutil.copytree(ROOT / "config", custom, ignore=ignore)
    shutil.copytree(ROOT / "config", stock, ignore=ignore)
    active = custom / "settings.py"
    # Adopted: settings record both digests while loading (confirmation 6 resolution).
    text = active.read_text()
    for before_text, after_text in (
        ("from config.logging", "from .logging"),
        ("from config.digest", "from .digest"),
        ("import config.logging as", "from . import logging as"),
    ):
        text = text.replace(before_text, after_text)
    active.write_text(text)
    # This prerequisite belongs only to the inactive package. Both .py files are readable.
    (stock / "__init__.py").write_text('raise RuntimeError("Synthetic package not initialized")\n')
    module, before, outcome = probe(
        tmp_path,
        """
        import sys
        before = "config" in sys.modules
        try:
            # The second-factor rule too, since the required second factor change.
            from core.models import RevealConfirmationRule, SecondFactorRule
            with patch.multiple(security, _second_factor_counts=lambda: (0, 0, 0, 0),
                                patient_capability_holders=lambda: 0, _patient_fields=lambda: 0,
                                review_capability_holders=lambda: 0,
                                _review_interval=lambda: None,
                                _second_factor_rule=lambda: SecondFactorRule(pk=1),
                            _reveal_confirmation_rule=lambda: RevealConfirmationRule(pk=1)):
                result = security.in_force()
            outcome = [row.value for group in result if group.key != "patient-information"
                       for row in group.facts]
        except RuntimeError as exc:
            outcome = [str(exc)]
        # Nothing is looked up at request time: the inactive package is never imported.
        print(json.dumps([settings.SETTINGS_MODULE, before or "config" in sys.modules, outcome]))
        """,
        module="synthetic_site.settings",
    )
    assert module == "synthetic_site.settings" and not before
    assert outcome and set(outcome) == {CHANGED}, outcome


def loading_digest(name):
    """What a module loading under `name` records: `config.digest.source_sha256` called with
    its own file and loader, as config/settings.py and config/logging.py call it. A module
    that cannot be found has no file and no loader, so nothing is recorded."""
    spec = importlib.util.find_spec(name)
    if spec is None:
        return source_sha256("", None)
    return source_sha256(spec.origin or "", spec.loader)


@pytest.mark.parametrize("form", ["namespace", "absent", "bytecode", "literal source"])
def test_digest_distinguishes_source_from_other_module_specs(tmp_path, monkeypatch, form):
    # Adopted: digests are recorded while the configuration loads (confirmation 5 resolution).
    name = "third_" + form.replace(" ", "_")
    source = b'# Synthetic Python source.\r\nraise AssertionError("digest executed the module")\r\n'
    if form == "namespace":
        (tmp_path / name).mkdir()
    elif form == "bytecode":
        original = tmp_path / "only_for_compilation.py"
        original.write_bytes(source)
        py_compile.compile(str(original), cfile=str(tmp_path / f"{name}.pyc"), doraise=True)
    elif form == "literal source":
        (tmp_path / f"{name}.py").write_bytes(source)
    monkeypatch.syspath_prepend(str(tmp_path))
    expected = hashlib.sha256(source).hexdigest() if form == "literal source" else ""
    assert loading_digest(name) == expected
    assert name not in sys.modules


def test_a_module_records_the_source_it_ran_not_one_that_appears_later(tmp_path, monkeypatch):
    # Adopted: digests are recorded while the configuration loads (confirmation 5 resolution).
    # The source is read once, when the module loads (a restart reads it again).
    name = "third_appearing_source"
    monkeypatch.syspath_prepend(str(tmp_path))
    assert loading_digest(name) == ""
    body = (
        b"from config.digest import source_sha256\nDIGEST = source_sha256(__file__, __loader__)\n"
    )
    source = b"# A synthetic module made available after the first reading.\n" + body
    (tmp_path / f"{name}.py").write_bytes(source)
    invalidate_caches()
    with patch.dict(sys.modules):
        module = importlib.import_module(name)
        assert module.DIGEST == hashlib.sha256(source).hexdigest()
        (tmp_path / f"{name}.py").write_bytes(source + b"# a later revision\n")
        assert module.DIGEST == hashlib.sha256(source).hexdigest()
