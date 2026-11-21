"""Seventh confirmation of the Security settings contract on synthetic configurations."""

import hashlib
import json
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest
from allauth.account import app_settings
from django.contrib.auth import SESSION_KEY
from django.urls import reverse

from accounts import signin_log
from accounts.adapter import ClosedSignupAdapter
from accounts.tests.test_login import mailed_code, request_code
from core import security_settings
from tests.test_review_security_round1_confirm3 import handed_off
from tests.test_review_security_round1_signoff import CHANGED, ROOT, probe


@pytest.fixture
def deployment(tmp_path):
    shutil.copytree(
        ROOT / "config", tmp_path / "config", ignore=shutil.ignore_patterns("__pycache__")
    )
    return tmp_path


@pytest.mark.parametrize("missing", ["SETTINGS_SHA256", "LOGGING_SHA256", "both"])
def test_missing_receipt_does_not_misidentify_an_unchanged_logging_file(deployment, missing):
    path = deployment / "config/settings.py"
    names = {"SETTINGS_SHA256", "LOGGING_SHA256"} if missing == "both" else {missing}
    lines = path.read_text().splitlines(keepends=True)
    path.write_text(
        "".join(line for line in lines if not line.startswith(tuple(n + " =" for n in names)))
    )
    value, note, records = probe(
        deployment,
        """
        row = https()
        modules = ("config.settings", "config.logging")
        print(json.dumps([row.value, row.note, [security.module_digest(name) for name in modules]]))
        """,
    )
    actual_settings = hashlib.sha256(path.read_bytes()).hexdigest()
    actual_logging = hashlib.sha256((deployment / "config/logging.py").read_bytes()).hexdigest()
    assert actual_settings != security_settings.RELEASE_SETTINGS_SHA256
    assert actual_logging == security_settings.RELEASE_LOGGING_SHA256
    assert records == [
        None if "SETTINGS_SHA256" in names else actual_settings,
        None if "LOGGING_SHA256" in names else actual_logging,
    ]
    assert value == CHANGED and "config/settings.py" in note
    assert "config/logging.py differs" not in note, note


# Adapted for the automatic sign-out change: 86400 became this release's own value, so a changed
# cookie age is now 43200.
def test_alternate_module_does_not_send_the_reader_to_a_nonexistent_settings_file(deployment):
    stock = deployment / "config/settings.py"
    (deployment / "seventh_site.py").write_bytes(
        stock.read_bytes() + b"\nSESSION_COOKIE_AGE = 43200\n"
    )
    stock.unlink()
    age, stock_found, value, note = probe(
        deployment,
        """
        from importlib.util import find_spec
        rows = []
        security._sessions(rows)
        row = next(row for row in rows if row.key == "session-length")
        print(json.dumps([settings.SESSION_COOKIE_AGE, find_spec("config.settings") is not None,
                          row.value, row.note]))
        """,
        module="seventh_site",
    )
    assert age == 43200 and not stock_found
    assert value == CHANGED and "seventh_site" in note
    assert "SESSION_COOKIE_AGE" in note
    assert "in config/settings.py" not in note, note


def test_architecture_describes_the_module_only_file_reason(deployment):
    (deployment / "seventh_site.py").write_text("from config.settings import *\n")
    with (deployment / "config/logging.py").open("ab") as logging_source:
        logging_source.write(b"\n# Local console configuration.\n")
    value, note, logging_digest = probe(
        deployment,
        """
        row = https()
        print(json.dumps([row.value, row.note, security.module_digest("config.logging")]))
        """,
        module="seventh_site",
    )
    assert (
        logging_digest
        == hashlib.sha256((deployment / "config/logging.py").read_bytes()).hexdigest()
    )
    assert logging_digest != security_settings.RELEASE_LOGGING_SHA256
    assert value == CHANGED and "seventh_site" in note
    assert "config/logging.py" not in note  # F6-02 expressly requires this.
    section = (ROOT / "docs/architecture.md").read_text().split("## Security settings", 1)[1]
    description = " ".join(section.split("\n## ", 1)[0].lower().split())
    unqualified = "when a deployment changes one, the fact says so and names it"
    assert unqualified not in description or any(
        term in description
        for term in (
            "another settings module",
            "alternate settings module",
            "only the module",
            "only the settings module",
            "different settings module",
        )
    ), "The file-naming promise omits the alternate-module decision."


@pytest.mark.parametrize(
    "mode",
    [py_compile.PycInvalidationMode.CHECKED_HASH, py_compile.PycInvalidationMode.UNCHECKED_HASH],
    ids=["checked-cache", "unchecked-cache"],
)
def test_fixed_startup_artifacts_identify_the_revision_python_actually_loaded(tmp_path, mode):
    # This is a standalone build label, not a Django settings module. Finish every packaging
    # operation before launching the reader. Its interpreter cannot write bytecode (-B).
    source = tmp_path / "seventh_label.py"
    first = b"EDITION = 'first'\n"
    second = b"EDITION = 'second'\n"
    source.write_bytes(first)
    cache = Path(py_compile.compile(str(source), invalidation_mode=mode, doraise=True))
    cached = cache.read_bytes()
    source.write_bytes(second)
    program = """
        import json
        import seventh_label
        from config.digest import module_sha256
        print(json.dumps([seventh_label.EDITION, module_sha256(seventh_label)]))
    """
    run = subprocess.run(
        [sys.executable, "-B", "-c", dedent(program)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    edition, recorded = json.loads(run.stdout)
    assert source.read_bytes() == second and cache.read_bytes() == cached
    ran = {"first": first, "second": second}[edition]
    if mode == py_compile.PycInvalidationMode.CHECKED_HASH:
        assert ran == second and recorded == hashlib.sha256(second).hexdigest()
    else:
        # Adopted (confirmation 7 resolution, F5-02): bytecode compiled in unchecked-hash mode is
        # not compared with its source by Python, so the digest of the source cannot identify it.
        # The architecture bounds the claim to the configuration's sources and the operations
        # guide says not to ship the configuration that way; this case asserts that limit.
        assert ran == first and recorded == hashlib.sha256(second).hexdigest()
        root = Path(__file__).resolve().parents[1]
        for document in ("docs/architecture.md", "docs/operations.md"):
            assert "unchecked-hash" in (root / document).read_text(), document


@pytest.mark.parametrize("file_location", [False, True], ids=["fileless", "located"])
def test_settings_record_both_digests_when_django_loads_the_logging_module(tmp_path, file_location):
    # The legacy interface exposes only LOGGING. An ordinary import lets the settings file
    # record what its own loader and the logging module's independent loader can actually read.
    program = """
        import json, sys
        from importlib.abc import InspectLoader
        from importlib.util import spec_from_loader
        from pathlib import Path
        source = Path(sys.argv[1]).read_bytes()
        located = sys.argv[2] == "yes"
        reads = []

        class LoggingLoader(InspectLoader):
            def get_source(self, fullname):
                return source.decode()

            def get_data(self, path):
                reads.append(path)
                assert path == "synthetic/logging.py"
                return source

        class LoggingFinder:
            def find_spec(self, fullname, path, target=None):
                if fullname == "config.logging":
                    spec = spec_from_loader(
                        fullname, LoggingLoader(), origin="synthetic/logging.py", is_package=False
                    )
                    spec.has_location = located
                    return spec

        sys.meta_path.insert(0, LoggingFinder())
        import django
        django.setup()
        from django.conf import settings
        from core import security_settings as security
        startup_reads = list(reads)
        rows = []
        security._connection(rows)
        fact = next(row for row in rows if row.key == "https")
        result = [settings.SETTINGS_SHA256, security.module_digest("config.logging"),
                  settings.LOGGING["version"], startup_reads, reads, fact.value, fact.note]
        print(json.dumps(result))
    """
    path = ROOT / "config/logging.py"
    run = subprocess.run(
        [sys.executable, "-c", dedent(program), str(path), "yes" if file_location else "no"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "DJANGO_SETTINGS_MODULE": "config.settings",
            "DEBUG": "true",
            "DATABASE_URL": "postgresql://synthetic@127.0.0.1:1/seventh_unused",
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    settings_digest, logging_digest, version, initial, final, value, note = json.loads(run.stdout)
    assert settings_digest == hashlib.sha256((ROOT / "config/settings.py").read_bytes()).hexdigest()
    assert version == 1 and initial == final
    if file_location:
        assert initial == ["synthetic/logging.py"]
        assert logging_digest == hashlib.sha256(path.read_bytes()).hexdigest()
        assert value == "Not required"
    else:
        assert initial == [] and logging_digest == ""
        assert value == CHANGED and "config/logging.py could not be read" in note
        assert "config/settings.py" not in note


@pytest.mark.django_db
@pytest.mark.parametrize("replacement", ["resend", "new_attempt"])
def test_signin_log_distinguishes_a_closed_attempt_from_a_superseded_code(
    client, staff, replacement
):
    user = staff("seventh-mail-reader", "technical")
    confirm = reverse("account_confirm_login_code")
    with (
        patch("allauth.account.internal.flows.code_verification.time") as clock,
        patch.object(ClosedSignupAdapter, "generate_login_code", return_value="ABC234") as issue,
        patch.object(signin_log.logger, "info") as recorder,
    ):
        clock.time.return_value = 1_900_000_000
        assert request_code(client, user.email).url == confirm
        first = mailed_code()
        issue.return_value = "DEF567"
        if replacement == "resend":
            assert client.post(confirm, {"action": "resend"}).url == confirm
        else:
            clock.time.return_value += app_settings.LOGIN_BY_CODE_TIMEOUT + 1
            recorder.reset_mock()
            expired = client.post(confirm, {"code": first})
            assert expired.url == reverse("account_request_login_code")
            assert SESSION_KEY not in client.session
            recorder.assert_not_called()
            assert request_code(client, user.email).url == confirm
        second = mailed_code()
        assert first != second
        recorder.reset_mock()
        refused = client.post(confirm, {"code": first})
        assert refused.context["form"].has_error("code") and SESSION_KEY not in client.session
        assert handed_off(recorder, "signin.failed", user)
        recorder.reset_mock()
        client.post(confirm, {"code": second})
        assert client.session[SESSION_KEY] == str(user.pk)
        assert handed_off(recorder, "signin.success", user)
