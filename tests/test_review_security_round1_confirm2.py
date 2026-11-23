"""Second confirmation of Security settings at 4388afc, with synthetic data."""

import json
import logging
import py_compile
import shutil
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from threading import Barrier, local

import pytest
from django.db import connection
from django.urls import reverse

from accounts import signin_log
from accounts.models import User
from accounts.setup import setup_lab
from core import lab_profile_views, security_settings, security_settings_views
from core.models import Lab
from tests.test_review_security_round1_signoff import probe, second_connection

ROOT = Path(__file__).resolve().parents[1]
CHANGED = "Changed in the application configuration"
PAGES = {"lab-profile": lab_profile_views, "security-settings": security_settings_views}


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("route", "delivered"),
    [
        ("parent stream", True),
        ("other handler accepts", True),
        ("ancestor logger filter", True),
        ("propagation stops", False),
        ("discard handler", False),
        ("origin filter", False),
        ("handler filter", False),
    ],
)
def test_signin_fact_matches_output_of_the_running_logging_route(monkeypatch, route, delivered):
    # Adopted: digests are recorded while the configuration loads (confirmation 5 resolution).
    # The fact no longer reads the logging configuration, so only the delivery is observed.
    # Private logger hierarchy. The receiver and the fact each see the same test logger.
    # The process's application loggers and handlers continue to run normally.
    root = logging.RootLogger(logging.INFO)
    manager = logging.Manager(root)
    logger = manager.getLogger("lab_ops.signin")
    stream = StringIO()
    console = logging.StreamHandler(stream)
    root.addHandler(console)
    other_namespace = logging.Filter("synthetic.instrument")
    if route == "propagation stops":
        logger.propagate = False
    elif route == "discard handler":
        root.handlers = [logging.NullHandler()]
    elif route == "origin filter":
        logger.addFilter(other_namespace)
    elif route == "ancestor logger filter":
        root.addFilter(other_namespace)
    elif route in {"handler filter", "other handler accepts"}:
        console.addFilter(other_namespace)
        if route == "other handler accepts":
            root.addHandler(logging.StreamHandler(stream))

    monkeypatch.setattr(signin_log, "logger", logger)
    signin_log.log_sign_out(None, None, User(username="synthetic-confirm-two"))
    assert ("signout user=synthetic-confirm-two" in stream.getvalue()) is delivered
    rows = [row for group in security_settings.in_force() for row in group.facts]
    fact = next(row for row in rows if row.key == "sign-in-records")
    assert_no_delivery_claim(fact.value, fact.note)


def test_signin_fact_names_changed_logging_when_console_omits_the_event_details():
    before, after, value, note = probe(
        ROOT,
        """
        import logging
        from io import StringIO
        from accounts.models import User
        from accounts.signin_log import log_sign_out
        from django.test import RequestFactory

        # A fresh worker, its release console captured in memory, and a known synthetic account.
        console, = logging.getLogger().handlers
        output = StringIO()
        console.setStream(output)
        user = User(username="synthetic-confirm-two")
        request = RequestFactory().post("/")
        def observe():
            output.seek(0)
            output.truncate(0)
            log_sign_out(None, request, user)
            return output.getvalue().strip()

        before = observe()
        options = settings.LOGGING["formatters"]["standard"]
        options["format"] = "%(levelname)s %(name)s"
        console.setFormatter(logging.Formatter(options["format"]))
        after = observe()
        # Adapted: the second-factor rule is a database reading too since the required second
        # factor change.
        from core.models import SecondFactorRule
        with patch.multiple(security, _second_factor_counts=lambda: (0, 0, 0, 0),
                            _second_factor_rule=lambda: SecondFactorRule(pk=1)):
            rows = []
            security._sign_in(rows)
        row = next(row for row in rows if row.key == "sign-in-records")
        print(json.dumps([before, after, row.value, row.note]))
        """,
    )
    assert "signout user=synthetic-confirm-two" in before
    assert after == "INFO lab_ops.signin"
    assert_no_delivery_claim(value, note)


@pytest.mark.parametrize("module", ["settings", "logging"])
def test_compiled_module_without_source_is_reported_as_unreadable(tmp_path, module):
    # Standard Python sourceless import from a temporary copy of the release configuration.
    name = f"{module}.py"
    package = tmp_path / "config"
    shutil.copytree(ROOT / "config", package, ignore=shutil.ignore_patterns("__pycache__", name))
    py_compile.compile(
        str(ROOT / "config" / name), cfile=str(package / f"{module}.pyc"), doraise=True
    )
    assert not (package / name).exists()
    origin, value, note = probe(
        tmp_path,
        f"""
        from importlib.util import find_spec
        row = https()
        print(json.dumps([find_spec("config.{module}").origin, row.value, row.note]))
        """,
    )
    assert Path(origin) == package / f"{module}.pyc"
    assert value == CHANGED and f"config/{name}" in note
    assert f"config/{name} differs" not in note, note


def test_readable_inactive_settings_file_can_be_hashed_without_importing_it(tmp_path, monkeypatch):
    package = tmp_path / "config"
    shutil.copytree(ROOT / "config", package, ignore=shutil.ignore_patterns("__pycache__"))
    release = (package / "settings.py").read_text()
    # The alternative settings module accepts a seconds suffix in its own mail timeout.
    expression = 'os.environ.get("EMAIL_TIMEOUT", "10")'
    assert expression in release
    (tmp_path / "synthetic_settings.py").write_text(
        release.replace(expression, expression + '.removesuffix("s")')
    )
    monkeypatch.setenv("EMAIL_TIMEOUT", "10s")
    timeout, actual, pinned, value, note = probe(
        tmp_path,
        """
        import hashlib
        digest = hashlib.sha256(Path("config/settings.py").read_bytes()).hexdigest()
        row = https()
        print(json.dumps([settings.EMAIL_TIMEOUT, digest, security.RELEASE_SETTINGS_SHA256,
                          row.value, row.note]))
        """,
        module="synthetic_settings",
    )
    assert timeout == 10 and actual == pinned
    assert value == CHANGED and "synthetic_settings" in note
    assert "config/settings.py could not be read" not in note, note


def help_item(label):
    document = json.loads((ROOT / "docs/pages/core/security-settings.json").read_text())
    return next(
        item["description"]
        for section in document["sections"]
        for item in section.get("items", [])
        if item["label"] == label
    )


# Adapted for the automatic sign-out change: 86400 became this release's own value, so a changed
# cookie age is now 43200.
def test_source_help_allows_an_alternate_module_with_both_release_files_intact(tmp_path):
    (tmp_path / "synthetic_settings.py").write_text(
        "from config.settings import *\nSESSION_COOKIE_AGE = 43200\n"
    )
    age, label, note, changed = probe(
        tmp_path,
        """
        rows = []
        security._sessions(rows)
        row = next(row for row in rows if row.key == "session-length")
        print(json.dumps([settings.SESSION_COOKIE_AGE, row.source_label, row.note,
                          security.changed_settings(*security.FILES)]))
        """,
        module="synthetic_settings",
    )
    assert age == 43200 and label == "Application configuration"
    assert "synthetic_settings" in note and "SESSION_COOKIE_AGE" in note
    assert changed == [security_settings.MODULE]
    assert "only by changing those files" not in help_item("Set by")


def test_changed_label_help_allows_a_release_file_that_cannot_be_verified():
    # Adopted: digests are recorded while the configuration loads (confirmation 5 resolution).
    value, note, matched = probe(
        ROOT,
        """
        # The source is read once, when the file loads (a restart reads it again): an
        # unreadable source is recorded as "" and stays so until then.
        recorded = settings.LOGGING_SHA256
        settings.LOGGING_SHA256 = ""
        row = https()
        settings.LOGGING_SHA256 = recorded
        restored = https()
        matched = recorded == security.RELEASE_LOGGING_SHA256 and restored.value != security.CHANGED
        print(json.dumps([row.value, row.note, matched]))
        """,
    )
    assert matched and value == CHANGED and "could not be read" in note
    description = help_item("Details").lower()
    assert "means the deployment changed" not in description or any(
        word in description for word in ("unreadable", "verif", "could not", "cannot")
    ), description


def config_rows(groups):
    return [row for group in groups if group.key != "patient-information" for row in group.facts]


@pytest.mark.django_db
@pytest.mark.parametrize("fail_inside", [False, True])
def test_nested_facts_restore_the_outer_file_reading(monkeypatch, fail_inside):
    digest = security_settings.module_digest
    active = "outer"
    calls = []

    def read(module):
        calls.append((active, module))
        if active == "inner" and module == "config.logging":
            return ""
        return digest(module)

    def counts():
        nonlocal active
        if active == "inner":
            if fail_inside:
                raise ValueError("Synthetic interrupted count")
            return (0, 0, 0, 0)
        active = "inner"
        try:
            if fail_inside:
                with pytest.raises(ValueError, match="Synthetic interrupted count"):
                    security_settings.in_force()
            else:
                inner = config_rows(security_settings.in_force())
                assert all(
                    row.value == CHANGED and "could not be read" in row.note for row in inner
                )
        finally:
            active = "outer"
        return (0, 0, 0, 0)

    monkeypatch.setattr(security_settings, "module_digest", read)
    monkeypatch.setattr(security_settings, "_second_factor_counts", counts)
    outer = config_rows(security_settings.in_force())
    assert all(row.value != CHANGED for row in outer)
    for which in ("outer", "inner"):
        assert sorted(module for label, module in calls if label == which) == [
            "config.logging",
            "config.settings",
        ]


def test_overlapping_readings_keep_their_own_file_reasons(monkeypatch):
    # Both calls have read their files before either proceeds to build its facts.
    meet = Barrier(2, timeout=5)
    worker = local()
    pins = {
        "config.settings": security_settings.RELEASE_SETTINGS_SHA256,
        "config.logging": security_settings.RELEASE_LOGGING_SHA256,
    }

    def read(module):
        return "" if module == worker.unavailable else pins[module]

    def counts():
        meet.wait()
        return (0, 0, 0, 0)

    def collect(unavailable):
        worker.unavailable = unavailable
        return config_rows(security_settings.in_force())

    monkeypatch.setattr(security_settings, "module_digest", read)
    monkeypatch.setattr(security_settings, "_second_factor_counts", counts)
    monkeypatch.setattr(security_settings, "patient_capability_holders", lambda: 0)
    monkeypatch.setattr(security_settings, "_patient_fields", lambda: 0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(collect, "config.settings")
        right = pool.submit(collect, "config.logging")
        for result, missing, other in (
            (left, "settings", "logging"),
            (right, "logging", "settings"),
        ):
            rows = result.result(timeout=10)
            for row in rows:
                assert row.value == CHANGED and f"config/{missing}.py could not be read" in row.note
                assert f"config/{other}.py" not in row.note


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("page", PAGES)
def test_account_groups_and_lab_header_use_one_snapshot_and_refresh_next_request(
    page, client, staff, monkeypatch
):
    first_lab, next_lab = "Synthetic Original Laboratory", "Synthetic Renamed Laboratory"
    first_name, next_name = "Original reader", "Renamed reader"
    policy = "Synthetic laboratory retention text for the confirmation fixture."
    Lab.objects.create(name=first_lab, record_retention_statement=policy)
    reader = staff("confirm-two-reader", "admin")
    User.objects.filter(pk=reader.pk).update(first_name=first_name)
    client.force_login(reader)
    view = PAGES[page]
    original = view.reading_account
    committed = []

    def change():
        User.objects.filter(pk=reader.pk).update(first_name=next_name)
        setup_lab(username=reader.username, lab_name=next_lab, update_lab=True, roles=["technical"])

    def during_account_read(request):
        def after_select(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if not committed and 'FROM "accounts_user"' in sql:
                second_connection(change)
                committed.append(True)
            return result

        with connection.execute_wrapper(after_select):
            return original(request)

    with monkeypatch.context() as patch:
        patch.setattr(view, "reading_account", during_account_read)
        first = client.get(reverse(page))
    later = client.get(reverse(page))
    assert committed and first.status_code == later.status_code == 200
    for response, lab, account, technical in (
        (first, first_lab, first_name, False),
        (later, next_lab, next_name, True),
    ):
        content = response.content.decode()
        assert f">{lab}</span>" in content and f"<strong>{account}</strong>" in content
        assert policy not in content
        roles = content.split('<span class="vr-user-roles">', 1)[1].split("</span>", 1)[0]
        assert "Administrator" in roles and ("Technical staff" in roles) is technical


def assert_no_delivery_claim(value, note):
    """Decided after the second confirmation (security-settings-round-1-confirmation-2-resolution):
    the sign-in record fact says only what the application passes to its sign-in log; where the
    log goes is the logging configuration's, so no logging change can make it false."""
    assert value == "Sign-in log, and the last sign-in time", value
    assert "set by the logging configuration" in note, note
    assert "written to" not in note and "writes" not in note, note
