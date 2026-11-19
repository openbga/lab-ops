"""The security workflow trusts a clean scan only after tools/security/sbom_coverage.py has shown
that the SBOM lists what the scan must cover: a scanner that cannot read a lockfile reports no
vulnerabilities rather than an error. These tests hold the script to that."""

import importlib.util
import json
import tomllib
from pathlib import Path

import pytest
from django.conf import settings

ROOT = Path(settings.BASE_DIR)
SPEC = importlib.util.spec_from_file_location(
    "sbom_coverage", ROOT / "tools/security/sbom_coverage.py"
)
coverage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coverage)

UV_LOCK = {
    "package": [
        {"name": "lab-ops", "version": "0.1.0", "source": {"virtual": "."}},
        {"name": "Django", "version": "5.2.19", "source": {"registry": "https://pypi.org/simple"}},
        {"name": "psycopg_binary", "version": "3.3.8", "source": {"registry": "x"}},
        {"name": "pytest", "version": "9.0.0", "source": {"registry": "x"}},
    ]
}
PYPROJECT = {"project": {"dependencies": ["Django>=5.2.19,<5.3", "psycopg-binary[pool]>=3"]}}
VENDOR = {
    "lockfileVersion": 3,
    "packages": {
        "": {"dependencies": {"htmx.org": "2.0.11"}},
        "node_modules/htmx.org": {"version": "2.0.11"},
        "node_modules/@fontsource-variable/overpass": {"version": "5.3.0"},
    },
}


def sbom(*purls):
    return {"components": [{"purl": purl} for purl in purls]}


COMPLETE = sbom(
    "pkg:pypi/django@5.2.19",
    "pkg:pypi/psycopg-binary@3.3.8",
    "pkg:pypi/pytest@9.0.0",
    "pkg:npm/htmx.org@2.0.11",
    "pkg:npm/%40fontsource-variable/overpass@5.3.0",
    "pkg:deb/debian/libc6@2.36?arch=amd64",
)


def run(document, mode):
    return coverage.check(document, mode, UV_LOCK, PYPROJECT, VENDOR)


@pytest.mark.parametrize("mode", ["locked", "runtime"])
def test_a_complete_sbom_passes(mode):
    problems, expected = run(COMPLETE, mode)
    assert problems == []
    assert expected == (5 if mode == "locked" else 4)


def test_the_project_itself_is_not_expected():
    assert "lab-ops" not in coverage.locked_python(UV_LOCK)


def test_a_missing_locked_package_is_reported_by_name_and_version():
    document = sbom(*[c["purl"] for c in COMPLETE["components"] if "pytest" not in c["purl"]])
    problems, _ = run(document, "locked")
    assert problems == ["pypi pytest 9.0.0: not in the SBOM"]
    assert run(document, "runtime")[0] == [], "a development tool is not in the image"


def test_a_different_version_is_reported():
    document = sbom("pkg:pypi/django@5.2.0", *[c["purl"] for c in COMPLETE["components"][1:]])
    assert run(document, "runtime")[0] == ["pypi django 5.2.19: the SBOM lists 5.2.0"]


def test_vendored_browser_packages_are_required_in_both_modes():
    document = sbom(*[c["purl"] for c in COMPLETE["components"] if "npm" not in c["purl"]])
    for mode in ("locked", "runtime"):
        problems, _ = run(document, mode)
        assert problems == [
            "npm @fontsource-variable/overpass 5.3.0: not in the SBOM",
            "npm htmx.org 2.0.11: not in the SBOM",
        ]


def test_an_empty_sbom_fails():
    problems, expected = run({"components": []}, "locked")
    assert len(problems) == expected == 5


def test_names_compare_as_python_normalizes_them():
    document = sbom("pkg:pypi/Django@5.2.19", "pkg:pypi/psycopg_binary@3.3.8")
    assert not coverage.missing(
        coverage.sbom_packages(document), "pypi", {"django": "5.2.19", "psycopg-binary": "3.3.8"}
    )


def test_a_runtime_dependency_absent_from_the_lock_is_reported():
    problems, _ = coverage.check(
        COMPLETE, "runtime", UV_LOCK, {"project": {"dependencies": ["gunicorn>=26"]}}, VENDOR
    )
    assert problems == ["runtime dependencies missing from uv.lock: ['gunicorn']"]


def test_the_repository_lockfiles_parse_and_name_the_runtime_dependencies():
    uv_lock = tomllib.loads((ROOT / "uv.lock").read_text())
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    locked = coverage.locked_python(uv_lock)
    runtime = coverage.runtime_names(pyproject)
    assert {"django", "django-allauth", "psycopg", "gunicorn", "whitenoise"} <= runtime
    assert runtime <= set(locked)
    vendored = coverage.vendored(json.loads((ROOT / "static/vendor/package-lock.json").read_text()))
    assert vendored["htmx.org"] and vendored["bootstrap"]


def test_the_command_exits_1_and_names_what_is_missing(tmp_path, capsys):
    path = tmp_path / "empty.cdx.json"
    path.write_text(json.dumps({"components": []}))
    assert coverage.main([str(path), "--runtime"]) == 1
    output = capsys.readouterr().out
    assert "does not list" in output and "pypi django" in output
