"""Eighth confirmation of the Security settings statements, on synthetic deployments."""

import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest
from django.test import override_settings

from core import security_settings
from tests.test_review_security_round1_signoff import CHANGED, ROOT, probe


@pytest.mark.parametrize("missing", ["neither", "logging", "both"])
def test_changed_settings_explain_which_source_went_unrecorded(tmp_path, missing):
    config = tmp_path / "config"
    shutil.copytree(ROOT / "config", config, ignore=shutil.ignore_patterns("__pycache__"))
    source = config / "settings.py"
    lines = source.read_text().splitlines(keepends=True)
    excluded = []
    if missing in {"logging", "both"}:
        excluded.append("LOGGING_SHA256 =")
    if missing == "both":
        excluded.append("SETTINGS_SHA256 =")
    source.write_text(
        "# Synthetic local settings revision.\n"
        + "".join(line for line in lines if not line.startswith(tuple(excluded)))
    )

    settings_digest, logging_digest, value, note = probe(
        tmp_path,
        """
        fact = https()
        print(json.dumps([security.settings_file_digest(), security.module_digest("config.logging"),
                          fact.value, fact.note]))
        """,
    )
    actual_settings = hashlib.sha256(source.read_bytes()).hexdigest()
    actual_logging = hashlib.sha256((config / "logging.py").read_bytes()).hexdigest()
    assert actual_settings != security_settings.RELEASE_SETTINGS_SHA256
    assert actual_logging == security_settings.RELEASE_LOGGING_SHA256
    assert settings_digest == (None if missing == "both" else actual_settings)
    assert logging_digest == (actual_logging if missing == "neither" else None)
    assert value == CHANGED and "config/settings.py" in note
    assert "config/logging.py differs" not in note
    if missing == "neither":
        assert "config/logging.py" not in note
    else:
        assert "config/logging.py" in note and "record" in note, note


@pytest.mark.parametrize("logging_digest", [None, ""])
def test_explanation_distinguishes_a_missing_record_from_an_unreadable_source(logging_digest):
    with override_settings(LOGGING_SHA256=logging_digest):
        rows = []
        security_settings._connection(rows)
    fact = next(row for row in rows if row.key == "https")
    assert fact.value == CHANGED and "config/logging.py" in fact.note
    if logging_digest is None:
        assert "config/settings.py" in fact.note and "does not record" in fact.note
    else:
        assert "could not be read" in fact.note and "does not record" not in fact.note


def _python(directory, program, *args, write_cache=False):
    # Ignore ambient Python path/cache options. Only the label's first import writes bytecode.
    options = ["-E"] if write_cache else ["-E", "-B"]
    result = subprocess.run(
        [sys.executable, *options, "-c", dedent(program), *args],
        cwd=directory,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("module", ["eighth_site.settings", None], ids=["dotted-module", "unnamed"])
# Adapted for the automatic sign-out change: 86400 became this release's own value, so a changed
# cookie age is now 43200.
def test_changed_setting_locations_follow_the_active_django_configuration(tmp_path, module):
    # Both entry points use the same settings and change the same two session rules.
    package = tmp_path / "eighth_site"
    package.mkdir()
    (package / "__init__.py").touch()
    (package / "settings.py").write_text(
        "from config.settings import *\n"
        "SESSION_COOKIE_AGE = 43200\n"
        "SESSION_SAVE_EVERY_REQUEST = True\n"
    )
    loaded, age, save_every_request, value, note = _python(
        tmp_path,
        """
        import json, os, sys
        sys.path.append(sys.argv[1])
        os.environ.update(DEBUG="true", DATABASE_URL="postgresql://synthetic@127.0.0.1:1/unused")
        name = sys.argv[2]
        if name:
            os.environ["DJANGO_SETTINGS_MODULE"] = name
        else:
            os.environ.pop("DJANGO_SETTINGS_MODULE", None)
        import django
        from django.conf import settings
        if not name:
            from eighth_site import settings as site
            settings.configure(**{key: getattr(site, key) for key in dir(site) if key.isupper()})
        django.setup()
        from core import security_settings as security
        rows = []
        security._sessions(rows)
        row = next(row for row in rows if row.key == "session-length")
        print(json.dumps([settings.SETTINGS_MODULE, settings.SESSION_COOKIE_AGE,
                          settings.SESSION_SAVE_EVERY_REQUEST, row.value, row.note]))
        """,
        str(ROOT),
        module or "",
    )
    assert loaded == module and age == 43200 and save_every_request is True
    assert value == CHANGED
    assert "SESSION_COOKIE_AGE" in note and "SESSION_SAVE_EVERY_REQUEST" in note
    assert "config/settings.py" not in note and "config/logging.py" not in note
    expected_location = module or "the deployment's settings"
    assert f"in {expected_location}." in note, note


@pytest.mark.parametrize(
    ("second_label", "seconds_later"),
    [("blue", 0), ("blue", 1), ("green", 0)],
    ids=["same-size-within-one-second", "later-second", "different-size"],
)
def test_timestamp_cache_and_documented_source_identity(tmp_path, second_label, seconds_later):
    # Real imports create and consume the cache of an inert build label. Packaging finishes
    # before the second interpreter starts. Neither interpreter loads Django settings.
    source = tmp_path / "eighth_label.py"
    initial = b"LABEL = 'gold'\n"
    staged = f"LABEL = '{second_label}'\n".encode()
    base_ns = 1_800_000_000 * 10**9
    source.write_bytes(initial)
    os.utime(source, ns=(base_ns, base_ns + 100_000_000))
    label, cached = _python(
        tmp_path,
        """
        import json, eighth_label
        print(json.dumps([eighth_label.LABEL, eighth_label.__cached__]))
        """,
        write_cache=True,
    )
    assert label == "gold"
    cache = Path(cached)
    cached_bytes = cache.read_bytes()
    # The default import produced a timestamp-based cache, with no hash-mode bit set.
    assert int.from_bytes(cached_bytes[4:8], "little") == 0
    source.write_bytes(staged)
    second_ns = base_ns + seconds_later * 10**9 + 900_000_000
    os.utime(source, ns=(second_ns, second_ns))

    loaded, observed = _python(
        tmp_path,
        """
        import json, sys, eighth_label
        sys.path.append(sys.argv[1])
        from config.digest import module_sha256
        print(json.dumps([eighth_label.LABEL, module_sha256(eighth_label)]))
        """,
        str(ROOT),
    )
    assert source.read_bytes() == staged and source.stat().st_mtime_ns == second_ns
    assert cache.read_bytes() == cached_bytes
    assert observed == hashlib.sha256(staged).hexdigest()
    if seconds_later or len(staged) != len(initial):
        assert loaded == second_label
    if loaded != second_label:
        assert loaded == "gold" and observed != hashlib.sha256(initial).hexdigest()
        architecture = " ".join((ROOT / "docs/architecture.md").read_text().split())
        claim = (
            "Python runs code compiled from those sources unless bytecode was compiled "
            "in unchecked-hash mode, which this project does not use."
        )
        overstated = []
        if claim in architecture:
            overstated.append("docs/architecture.md excepts only unchecked-hash mode")
        if "exactly what this process runs" in inspect.getdoc(security_settings.module_digest):
            overstated.append("module_digest promises the executed revision")
        assert not overstated, f"Loaded {loaded}, recorded source {second_label}: {overstated}"
