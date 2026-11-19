"""Sixth confirmation of the Security settings contract, using only synthetic data."""

import hashlib
import py_compile
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from importlib.abc import InspectLoader
from importlib.machinery import PathFinder, SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from allauth.account import app_settings
from allauth.account.internal.flows import code_verification
from django.contrib.auth import SESSION_KEY
from django.urls import reverse

from accounts import signin_log
from accounts.adapter import ClosedSignupAdapter
from accounts.tests.test_login import mailed_code, request_code
from config import digest as config_digest
from core import security_settings
from tests.test_review_security_round1_confirm3 import handed_off, record_descriptions
from tests.test_review_security_round1_signoff import CHANGED, ROOT, probe


@pytest.mark.django_db
def test_record_distinguishes_an_expired_email_attempt_from_its_code_in_a_new_attempt(
    client, staff, monkeypatch
):
    user = staff("sixth-email-reader", "technical")
    start = time.time()
    clock = SimpleNamespace(time=lambda: start)
    monkeypatch.setattr(code_verification, "time", clock)
    confirm = reverse("account_confirm_login_code")
    # Make the two emails deterministic. The release forms issue and validate the codes.
    with patch.object(ClosedSignupAdapter, "generate_login_code", side_effect=["ABC234", "DEF567"]):
        assert request_code(client, user.email).url == confirm
        first = mailed_code()
        clock.time = lambda: start + app_settings.LOGIN_BY_CODE_TIMEOUT + 1
        with patch.object(signin_log.logger, "info") as recorder:
            expired = client.post(confirm, {"code": first})
        assert expired.url == reverse("account_request_login_code")
        assert SESSION_KEY not in client.session and not recorder.called
        assert request_code(client, user.email).url == confirm
        assert mailed_code() != first

    with patch.object(signin_log.logger, "info") as recorder:
        retried = client.post(confirm, {"code": first})
    assert retried.status_code == 200 and retried.context["form"].has_error("code")
    assert SESSION_KEY not in client.session
    if handed_off(recorder, "signin.failed", user):
        blanket = [
            surface
            for surface, description in record_descriptions().items()
            if "an expired emailed code," in description.lower()
            and not any(term in description.lower() for term in ("challenge", "new attempt"))
        ]
        assert not blanket, blanket


@pytest.fixture
def private_deployment(tmp_path):
    shutil.copytree(
        ROOT / "config", tmp_path / "config", ignore=shutil.ignore_patterns("__pycache__")
    )
    return tmp_path


def test_deployment_can_start_with_its_existing_logging_dictionary(private_deployment):
    # This is the logging module's pre-upgrade interface, with a valid console configuration.
    (private_deployment / "config/logging.py").write_text(
        "LOGGING = {'version': 1, 'disable_existing_loggers': False, "
        "'handlers': {'console': {'class': 'logging.StreamHandler'}}, "
        "'root': {'handlers': ['console'], 'level': 'INFO'}}\n"
    )
    try:
        value, note = probe(
            private_deployment,
            """
            assert settings.LOGGING["handlers"]["console"]["class"] == "logging.StreamHandler"
            row = https()
            print(json.dumps([row.value, row.note]))
            """,
        )
    except subprocess.CalledProcessError as error:
        pytest.fail(f"Django setup failed: {error.stderr.strip().splitlines()[-1]}")
    assert value == CHANGED and "config/logging.py" in note


@pytest.mark.parametrize("recorded", [True, False], ids=["alternate-source", "no-record"])
def test_change_reasons_describe_the_file_for_which_the_digest_was_recorded(tmp_path, recorded):
    # Adopted: settings record both digests while loading (confirmation 6 resolution).
    stock = (ROOT / "config/settings.py").read_text()
    independent = [
        hashlib.sha256((ROOT / "config" / name).read_bytes()).hexdigest()
        for name in ("settings.py", "logging.py")
    ]
    assert independent == [
        security_settings.RELEASE_SETTINGS_SHA256,
        security_settings.RELEASE_LOGGING_SHA256,
    ]
    if recorded:
        # Another settings module records its own digest (F6-02: it is named alone).
        active_file = tmp_path / "sixth_settings.py"
        active_file.write_text(stock + "\n# Synthetic laboratory settings.\n")
        module = "sixth_settings"
    else:
        # The release settings module with its record missing (a deployment that removed the
        # two lines): the file differs, and nothing says it could not be read.
        shutil.copytree(
            ROOT / "config", tmp_path / "config", ignore=shutil.ignore_patterns("__pycache__")
        )
        active_file = tmp_path / "config/settings.py"
        active_file.write_text(stock.split("SETTINGS_SHA256 =", 1)[0].rsplit("\n#", 1)[0] + "\n")
        module = "config.settings"
    value, note, active_digest, logging_digest = probe(
        tmp_path,
        """
        row = https()
        print(json.dumps([row.value, row.note, getattr(settings, "SETTINGS_SHA256", None),
                          getattr(settings, "LOGGING_SHA256", None)]))
        """,
        module=module,
    )
    assert value == CHANGED
    if recorded:
        assert active_digest == hashlib.sha256(active_file.read_bytes()).hexdigest()
        assert logging_digest == security_settings.RELEASE_LOGGING_SHA256
        assert "sixth_settings" in note
        assert "config/settings.py differs" not in note and "config/logging.py" not in note, note
        assert "could not be read" not in note, note
    else:
        assert active_digest is None and logging_digest is None
        assert "config/settings.py differs" in note, note
        assert "could not be read" not in note, note


@pytest.mark.parametrize("bytecode", [False, True], ids=["source", "bytecode"])
@pytest.mark.parametrize("publish", [False, True], ids=["same-revision", "updated-during-import"])
def test_digest_describes_code_already_selected_by_python(tmp_path, monkeypatch, bytecode, publish):
    # Adopted: settings record both digests while loading (confirmation 6 resolution).
    # This module has only a revision label. The two events hold initialization at a
    # known point after Python selected its code and before the digest read.
    source = tmp_path / "sixth_revision.py"
    first = (
        b"import sys\n"
        b"from config.digest import module_sha256\n"
        b"REVISION = 17\n"
        b"checkpoint()\n"
        b"DIGEST = module_sha256(sys.modules.get(__name__))\n"
    )
    second = first.replace(b"17", b"18")
    source.write_bytes(first)
    origin = source
    if bytecode:
        origin = source.with_suffix(".pyc")
        py_compile.compile(
            str(source),
            cfile=str(origin),
            doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
        )
        source.unlink()
    spec = PathFinder.find_spec("sixth_revision", [str(tmp_path)])
    assert spec.origin == str(origin)
    module = module_from_spec(spec)
    entered, proceed = Event(), Event()

    def checkpoint():
        entered.set()
        assert proceed.wait(5)

    module.checkpoint = checkpoint
    # Like config/settings.py, the module finds itself in sys.modules while it loads.
    monkeypatch.setitem(sys.modules, "sixth_revision", module)
    with ThreadPoolExecutor(max_workers=1) as worker:
        loading = worker.submit(spec.loader.exec_module, module)
        try:
            assert entered.wait(5)
            if publish or bytecode:
                staged = tmp_path / "next.py"
                staged.write_bytes(second if publish else first)
                staged.replace(source)
        finally:
            proceed.set()
        loading.result(timeout=5)
    assert module.REVISION == 17
    if publish:
        # F5-02 decision (docs/operations.md): the digest is the source as read through the
        # module's loader when loading finished. Replacing a configuration file while the
        # application starts is outside the supported deployment, so the record may describe
        # the file that was there at that moment rather than the revision Python selected.
        assert module.DIGEST == hashlib.sha256(second).hexdigest()
    else:
        assert module.DIGEST == hashlib.sha256(first).hexdigest()
        assert module.DIGEST == config_digest.source_sha256(str(source), spec.loader)


@pytest.mark.parametrize(
    "located,readable",
    [(True, True), (True, False), (False, False)],
    ids=["delegated-read", "unavailable-bytes", "no-file-location"],
)
def test_release_logging_module_loads_even_without_a_verifiable_file(located, readable):
    # Adopted: settings record both digests while loading (confirmation 6 resolution).
    path = ROOT / "config/logging.py"
    fullname = "config.logging"

    class PackagedLogging(InspectLoader):
        loader = SourceFileLoader(fullname, str(path))

        def get_source(self, name):
            return self.loader.get_source(name)

        def get_data(self, name):
            if not readable:
                raise OSError("Synthetic source unavailable at digest time")
            assert name == "mounted/logging.py"
            return self.loader.get_data(str(path))

    spec = spec_from_loader(
        fullname, PackagedLogging(), origin="mounted/logging.py", is_package=False
    )
    spec.has_location = located
    module = module_from_spec(spec)
    assert hasattr(module, "__file__") == located
    spec.loader.exec_module(module)  # the released file has no digest code to fail
    assert module.LOGGING["version"] == 1
    assert not hasattr(module, "LOGGING_SHA256")
    digest = config_digest.module_sha256(module)
    assert digest == (hashlib.sha256(path.read_bytes()).hexdigest() if located and readable else "")


def test_reading_the_page_facts_keeps_both_startup_records_until_restart(private_deployment):
    initial, later, reads = probe(
        private_deployment,
        """
        from importlib.machinery import SourceFileLoader
        paths = {Path("config/settings.py").resolve(), Path("config/logging.py").resolve()}
        read = SourceFileLoader.get_data
        reads = []
        def observe(loader, name):
            if Path(name) in paths:
                reads.append(name)
            return read(loader, name)
        with patch.object(SourceFileLoader, "get_data", observe):
            initial = https()
            for path in paths:
                with path.open("ab") as file:
                    file.write(b"\\n# Synthetic update after startup.\\n")
            later = https()
        print(json.dumps([initial.value, later.value, reads]))
        """,
    )
    assert initial == later == "Not required" and reads == []
    value, note = probe(
        private_deployment, "row = https()\nprint(json.dumps([row.value, row.note]))"
    )
    assert value == CHANGED
    assert all(f"config/{name} differs" in note for name in ("settings.py", "logging.py"))
