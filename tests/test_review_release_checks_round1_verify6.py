"""Independent review: sixth verification of the release security checks (pull request 13, commit
c4fb9f7), the redesigned secret scan: one file-by-file `gitleaks git` scan with a configuration
built by tools/security/gitleaks_config.py from gitleaks' default configuration without its list of
skipped paths.

Each test states behaviour that SECURITY.md, secrets.yml or the builder's docstring promises. A
failing test is a finding in .local/review-history/release-checks-round-1-verification-6.md; tests
named ``test_control_*`` and ``test_sound_*`` pass and record what held.

secrets.yml's own step scripts run with ``bash -e`` in fresh repositories, with RUNNER_TEMP holding
the checksum-verified gitleaks binary and the configuration built from gitleaks' default
configuration at /tmp/labops-rsc-tools/gitleaks-default.toml (LABOPS_REVIEW_GITLEAKS_DEFAULT); the
tests skip when either is absent and never download. Every token is built at run time, so this file
holds no secret-shaped value. Only copies are mutated.
"""

import gzip
import hashlib
import importlib.util
import json
import os
import random
import shutil
import string
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest
import yaml
from django.conf import settings

ROOT = Path(settings.BASE_DIR)
TOOLS = Path(os.environ.get("LABOPS_REVIEW_TOOLS", "/tmp/labops-rsc-tools/bin"))
DEFAULT_TOML = Path(
    os.environ.get("LABOPS_REVIEW_GITLEAKS_DEFAULT", "/tmp/labops-rsc-tools/gitleaks-default.toml")
)
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Review",
    "GIT_AUTHOR_EMAIL": "review@example.invalid",
    "GIT_COMMITTER_NAME": "Review",
    "GIT_COMMITTER_EMAIL": "review@example.invalid",
}
SECRETS_YML = ROOT / ".github/workflows/secrets.yml"
HISTORY = "Scan the history for secrets"
CANARY = "Check that the settings still report a secret, whatever the file is called"


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = _load("review_v6_gitleaks_config", "tools/security/gitleaks_config.py")


def tool(name):
    path = TOOLS / name
    if path.is_file():
        return str(path)
    found = shutil.which(name)
    if not found:
        pytest.skip(f"{name} is not installed")
    return found


def default_text():
    if not DEFAULT_TOML.is_file():
        pytest.skip("gitleaks' default configuration is not available locally")
    return DEFAULT_TOML.read_text()


def built_text():
    return builder.build(default_text(), (ROOT / ".gitleaks.toml").read_text())


def step(name):
    steps = yaml.safe_load(SECRETS_YML.read_text())["jobs"]["secrets"]["steps"]
    return next(s["run"] for s in steps if s.get("name") == name)


def runner_temp(tmp_path, config_text):
    """RUNNER_TEMP as the job leaves it before its checks: gitleaks and gitleaks.toml."""
    folder = tmp_path / "runner-temp"
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / "gitleaks").exists():
        (folder / "gitleaks").symlink_to(tool("gitleaks"))
    (folder / "gitleaks.toml").write_text(config_text)
    return folder


def run_step(name, cwd, temp):
    env = {**GIT_ENV, "RUNNER_TEMP": str(temp), "HOME": str(temp)}
    return subprocess.run(["bash", "-e", "-c", step(name)], cwd=cwd, env=env, capture_output=True)


def fake_token():
    alphabet = "".join(c for c in string.ascii_letters + string.digits if c not in "lL")
    rng = random.SystemRandom()
    return "ghp" + "_" + "".join(rng.choice(alphabet) for _ in range(36))


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=GIT_ENV)


# ======================= AA-01: a compressed file's contents are not read, and not stated


def compressed_files(folder):
    """A plain settings file and the same kind of content in a zip, an Office workbook (a zip)
    and a gzip file, each holding its own generated token."""
    folder.mkdir(parents=True)
    (folder / "plain.py").write_text(f'token = "{fake_token()}"\n')
    with zipfile.ZipFile(folder / "settings-backup.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("settings_local.py", f'token = "{fake_token()}"\n')
    with zipfile.ZipFile(folder / "contacts.xlsx", "w", zipfile.ZIP_DEFLATED) as workbook:
        workbook.writestr("xl/sharedStrings.xml", f"<sst><si><t>{fake_token()}</t></si></sst>")
    (folder / "settings.py.gz").write_bytes(gzip.compress(f'token = "{fake_token()}"\n'.encode()))
    return {f"exports/{p.name}" for p in folder.iterdir()}


def test_every_file_the_scan_reads_is_reported_or_the_limit_is_stated(tmp_path):
    """SECURITY.md ("What is checked"): the history is "read as text and file by file ... so every
    file is read". A token inside a committed archive, Office document or gzip file is read only as
    compressed bytes, so it is not reported (gitleaks' --max-archive-depth does not apply to its
    git mode); "What is not checked" does not say so. The plain file is the control."""
    repo = tmp_path / "repo"
    files = compressed_files(repo / "exports")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add exports")
    temp = runner_temp(tmp_path, built_text())
    history = step(HISTORY).replace(
        "--config", f'--report-format json --report-path "{temp}/history.json" --config'
    )
    env = {**GIT_ENV, "RUNNER_TEMP": str(temp), "HOME": str(temp)}
    subprocess.run(["bash", "-e", "-c", history], cwd=repo, env=env, capture_output=True)
    reported = {finding["File"] for finding in json.loads((temp / "history.json").read_text())}
    assert "exports/plain.py" in reported, "control: the plain file is reported"
    not_checked = (ROOT / "SECURITY.md").read_text().split("## What is not checked")[1].lower()
    stated = any(word in not_checked for word in ("compressed", "archive", "zip"))
    assert reported == files or stated, f"not reported and not stated: {sorted(files - reported)}"


# ============================================================ sound: checked and held


def test_sound_the_pinned_checksum_is_that_of_the_default_configuration_used_here():
    pinned = yaml.safe_load(SECRETS_YML.read_text())["jobs"]["secrets"]["env"][
        "GITLEAKS_CONFIG_SHA256"
    ]
    assert hashlib.sha256(default_text().encode()).hexdigest() == pinned


def test_sound_the_built_configuration_is_the_default_without_its_paths_plus_the_project():
    built = tomllib.loads(built_text())
    default = tomllib.loads(default_text())
    project = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
    assert built["rules"] == default["rules"] and len(built["rules"]) == 222
    expected = {k: v for k, v in default["allowlist"].items() if k != "paths"}
    assert built["allowlists"] == [expected, *project["allowlists"]]


MUTATIONS = {
    "an extra rule": lambda t: t + '\n[[rules]]\nid = "x"\nregex = "x"\n',
    "a project allowlist with paths": lambda t: "".join(
        (lambda head, sep, tail: (head, sep, '\npaths = [".*"]', tail))(
            *t.rpartition('regexTarget = "secret"')
        )
    ),
    "an extend table": lambda t: t + "\n[extend]\nuseDefault = true\n",
    "a default rule removed": lambda t: t.replace('id = "github-pat"', 'id = "github-pat-x"', 1),
}


@pytest.mark.parametrize("label", MUTATIONS)
def test_sound_the_builder_check_refuses_a_changed_result(label):
    text = MUTATIONS[label](built_text())
    with pytest.raises(ValueError):
        builder.check(
            text,
            tomllib.loads(default_text()),
            tomllib.loads((ROOT / ".gitleaks.toml").read_text()),
        )


def test_sound_the_canary_fails_when_the_default_list_of_skipped_paths_comes_back(tmp_path):
    """The canary plants tokens in a .py file, a package-lock.json and an SVG. With the built
    configuration it passes; with gitleaks' default configuration (which skips the last two by
    name) it fails."""
    work = tmp_path / "work"
    work.mkdir()
    built = runner_temp(tmp_path / "built", built_text())
    assert run_step(CANARY, work, built).returncode == 0
    default = runner_temp(tmp_path / "default", default_text())
    assert run_step(CANARY, work, default).returncode == 1


def test_sound_tokens_in_files_the_default_skips_are_reported_by_the_history_scan(tmp_path):
    repo = tmp_path / "repo"
    for relative in ("e2e/package-lock.json", "static/img/badge.svg", "docs/gitleaks.toml.md"):
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo / relative).write_text(f"note {fake_token()}\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add files the default skips")
    assert run_step(HISTORY, repo, runner_temp(tmp_path, built_text())).returncode == 1
