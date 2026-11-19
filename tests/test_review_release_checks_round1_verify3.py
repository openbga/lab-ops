"""Independent review: third verification of the release security checks (pull request 13, commit
70c34e1), after the resolution of W-01 to W-07 and the merge of main (pull request 11).

Each test states behaviour that SECURITY.md, docs/safeguards.md, a workflow comment or a script's
docstring promises, for a case beside a fix. A failing test is a finding in
.local/review-history/release-checks-round-1-verification-3.md; tests named ``test_control_*`` and
``test_sound_*`` pass and record what held.

Workflow step scripts are read from the parsed secrets.yml and run with ``bash -e`` in a fresh
repository, as GitHub runs a step without a ``shell:``. Scanner-backed tests use the
checksum-verified gitleaks in LABOPS_REVIEW_TOOLS (default /tmp/labops-rsc-tools/bin) or on PATH,
and skip when it is absent. Private keys are generated at run time and never written outside
pytest's temporary directories; this file holds no secret-shaped value. Only copies are mutated.
"""

import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings

ROOT = Path(settings.BASE_DIR)
TOOLS = Path(os.environ.get("LABOPS_REVIEW_TOOLS", "/tmp/labops-rsc-tools/bin"))
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Review",
    "GIT_AUTHOR_EMAIL": "review@example.invalid",
    "GIT_COMMITTER_NAME": "Review",
    "GIT_COMMITTER_EMAIL": "review@example.invalid",
}
SECRETS_YML = ".github/workflows/secrets.yml"
SECURITY_YML = ".github/workflows/security.yml"


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load("review_v3_image_contents", "tools/security/image_contents.py")
action_pins = _load("review_v3_action_pins", "tools/security/check_action_pins.py")


@pytest.fixture
def checks():
    """A fresh copy of the candidate's tests/test_security_checks.py, safe to monkeypatch."""
    return _load("review_v3_candidate_checks", "tests/test_security_checks.py")


def read(relative):
    return (ROOT / relative).read_text()


def tool(name):
    for directory in (TOOLS, TOOLS / "bin"):
        if (directory / name).is_file():
            return str(directory / name)
    found = shutil.which(name)
    if not found:
        pytest.skip(f"{name} is not installed")
    return found


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=GIT_ENV
    ).stdout.strip()


def secrets_step(name):
    steps = yaml.safe_load(read(SECRETS_YML))["jobs"]["secrets"]["steps"]
    return next(step["run"] for step in steps if step.get("name") == name)


# Adapted: the job builds the configuration its scans read ($RUNNER_TEMP/gitleaks.toml) in a step
# that downloads gitleaks' default one, which tests never do; the helper builds it in-process from
# the default file kept next to the scanners (skipping when absent).
def default_gitleaks_config():
    """The real default gitleaks configuration of the pinned release, or skip."""
    candidates = [
        os.environ.get("LABOPS_GITLEAKS_DEFAULT_CONFIG"),
        TOOLS / "gitleaks-default.toml",
        TOOLS.parent / "gitleaks-default.toml",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    pytest.skip("the default gitleaks configuration is not next to the scanners")


def private_key_lines():
    """A freshly generated RSA private key in PEM form, as lines."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return pem.decode().splitlines()


# =========== W-03: the windows keep single lines whole, but a multi-line key is still cut


SVG_FILLER = [
    f'  <path d="M{i} {i}L{i + 7} {i + 3}Z" fill="#{i % 4096:03x}"/>' for i in range(6000)
]


# Adapted: the stream step is gone. Its intent (a multi-line private key in an .svg, a file the
# default rules skip by path, is reported wherever it sits) is judged on the history step with the
# built configuration, which reads each file of a commit whole, so no position can cut the key.
# The sweep uses fewer positions than the original's 120 (every 200th line of a 6,000-line file,
# 30 positions, as the full sweep took 50 s); the control is the same
# step with the default configuration, which skips the .svg and passes.
def test_the_history_scan_reports_a_private_key_wherever_it_sits_in_a_skipped_file(tmp_path):
    """secrets.yml: the history is scanned with the default configuration "without its list of
    skipped paths", so an .svg, which the default rules skip by path, is read. A private key spans
    about 28 lines; one generated key is moved through a 270 kB SVG, one fresh repository per
    position, through secrets.yml's own history step."""
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    (runner_temp / "gitleaks").symlink_to(tool("gitleaks"))
    builder = _load("review_v3_gitleaks_config", "tools/security/gitleaks_config.py")
    default = default_gitleaks_config()
    (runner_temp / "gitleaks.toml").write_text(
        builder.build(default.read_text(), read(".gitleaks.toml"))
    )
    env = {**GIT_ENV, "RUNNER_TEMP": str(runner_temp), "HOME": str(tmp_path)}
    history = secrets_step("Scan the history for secrets")
    default_only = history.replace('"$RUNNER_TEMP/gitleaks.toml"', f'"{default}"')
    assert default_only != history
    key = [f"  <!-- {line} -->" for line in private_key_lines()]
    missed, skipped_by_path = [], set()
    positions = range(0, len(SVG_FILLER), 200)
    for position in positions:
        repo = tmp_path / f"repo-{position}"
        (repo / "static/img").mkdir(parents=True)
        lines = SVG_FILLER[:position] + key + SVG_FILLER[position:]
        (repo / "static/img/badge.svg").write_text("<svg>\n" + "\n".join(lines) + "\n</svg>\n")
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "add a badge")
        run = subprocess.run(["bash", "-e", "-c", history], cwd=repo, env=env, capture_output=True)
        if run.returncode == 0:
            missed.append(position)
        if position == 0:
            other = subprocess.run(
                ["bash", "-e", "-c", default_only], cwd=repo, env=env, capture_output=True
            )
            skipped_by_path.add(other.returncode)
        shutil.rmtree(repo)
    assert skipped_by_path == {0}, "control: the default configuration skips .svg files by path"
    assert not missed, (
        f"the secrets job passes a private key in an SVG at {len(missed)} of {len(positions)} "
        f"positions (lines {missed})"
    )


# ============================= W-04: a push of more than three tags starts no workflow


def test_the_tag_push_limit_is_stated_where_tag_pushes_are_promised():
    """secrets.yml: "It runs on every push of a branch or a tag ... so a secret pushed and then
    replaced before a pull request exists is still reported"; SECURITY.md: "on every push of a
    branch or a tag", and "What is not checked" now states the [skip ci] limit. GitHub creates no
    push event for tags when more than three tags are pushed at once (GitHub Docs, "Events that
    trigger workflows", push), as `git push --tags` often does, so a commit only under those tags
    is never scanned. That limit is not stated."""
    stated = " ".join((read("SECURITY.md") + read(SECRETS_YML)).lower().split())
    assert "skip ci" in stated, "control: the [skip ci] limit is stated"
    assert re.search(r"(more than|over) (three|3) tags", stated), (
        "neither SECURITY.md nor secrets.yml says a push of more than three tags starts no run"
    )


# ===================== W-07: a program at a path a Debian package installed, replaced


def elf(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x7fELF" + bytes(60))
    path.chmod(0o755)
    return path


def test_the_image_listing_names_a_program_that_replaced_a_debian_packages_file(tmp_path):
    """SECURITY.md: the image check covers "every other program anywhere in the image that no
    Debian package installed". unpackaged_programs() leaves out every path dpkg lists, whatever
    the file now holds, so a binary the Dockerfile copies over a packaged one (COPY over
    /usr/bin/<tool>) is not listed; the SBOM shows the Debian package's version for it. dpkg
    keeps each file's checksum (/var/lib/dpkg/info/*.md5sums), which the probe does not read.
    The program under /opt is the control."""
    replaced = elf(tmp_path / "usr/bin/curl")
    elsewhere = elf(tmp_path / "opt/tool/bin/tool")
    # Adapted: `owned` is now {real path: recorded MD5}; the recorded MD5 here is that of the
    # packaged curl, which the copied-in binary no longer matches.
    owned = {os.path.realpath(replaced): "0" * 32}
    programs = probe.unpackaged_programs(str(tmp_path), owned)
    assert str(elsewhere) in programs, "control: a program no package installed is listed"
    assert str(replaced) in programs, (
        "a binary copied over a file dpkg installed is not listed, so nothing requires the SBOM "
        "to describe what that path now holds"
    )


# ================== docs/safeguards.md (merged from main): the SBOM on every release


def test_the_safeguards_reference_states_when_a_release_carries_its_sbom():
    """docs/safeguards.md, Evaluation (a)(8): "each published release carries a software bill of
    materials of its image". security.yml attaches it only when the image job's coverage check
    passed (`needs.image.outputs.sbom-complete == 'true'`), as SECURITY.md says ("once it has
    passed the comparison above"); a release whose SBOM fails that check carries none."""
    release = yaml.safe_load(read(SECURITY_YML))["jobs"]["release-sbom"]
    assert "sbom-complete" in release["if"], "control: the SBOM is attached conditionally"
    row = next(
        line for line in read("docs/safeguards.md").splitlines() if line.startswith("| Evaluation")
    )
    # Adapted: the row now keeps the claim and adds the condition after it ("once that SBOM has
    # passed its completeness check"), so the claim alone is no longer the failure.
    claim = "each published release carries a software bill of materials"
    unconditional = claim in row and "once that SBOM has passed its completeness check" not in row
    assert not unconditional, f"safeguards.md states it without the condition: {row[:200]}"


# ============================================================ sound: checked and held


SECURITY_PATHS = [
    ".github",
    "tools/security",
    "tests/test_security_checks.py",
    "tests/test_sbom_coverage.py",
    "tests/test_security_tools.py",
    "tests/test_review_release_checks_round1.py",
    "tests/test_review_release_checks_round1_verify.py",
    "tests/test_review_release_checks_round1_verify2.py",
    ".gitleaks.toml",
    ".grype.yaml",
    ".syft.yaml",
    "Dockerfile",
    ".dockerignore",
    "static/vendor",
    "uv.lock",
    "pyproject.toml",
    "CONTRIBUTING.md",
]


def test_sound_the_merge_of_main_left_the_security_files_as_the_fix_commit_had_them():
    """70c34e1 merges main (pull request 11) into 4c9382c. Of the files the checks are made of,
    only checks.yml (a longer timeout) and one appended sentence of SECURITY.md changed."""
    try:
        changed = git(ROOT, "diff", "--name-only", "4c9382c", "70c34e1", "--", *SECURITY_PATHS)
        security_md = git(ROOT, "diff", "-U0", "4c9382c", "70c34e1", "--", "SECURITY.md")
        checks_yml = git(
            ROOT, "diff", "-U0", "4c9382c", "70c34e1", "--", ".github/workflows/checks.yml"
        )
    except subprocess.CalledProcessError:
        pytest.skip("the commits are not in this clone")
    assert changed.split() == [".github/workflows/checks.yml"], changed
    removed = [
        line for line in security_md.splitlines() if line.startswith("-") and "---" not in line
    ]
    assert removed == ["-See [Operations](docs/operations.md) for both."], removed
    edits = [
        line
        for line in checks_yml.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    assert [line for line in edits if not line.lstrip("+- ").startswith("#")] == [
        "-    timeout-minutes: 20",
        "+    timeout-minutes: 35",
    ], edits


def test_sound_a_local_action_in_a_skipped_directory_name_fails_a_candidate_test(
    checks, monkeypatch, tmp_path
):
    """action_files() skips any directory called build, staticfiles, .local and the like, at any
    depth, so .github/actions/build/action.yml is not read; the candidate's test that every
    local `uses: ./path` resolves to a file the checks read reports it."""
    shutil.copytree(ROOT / ".github", tmp_path / ".github")
    shutil.copytree(ROOT / "tools", tmp_path / "tools")
    action = tmp_path / ".github/actions/build/action.yml"
    action.parent.mkdir(parents=True)
    action.write_text("runs:\n  using: composite\n  steps:\n    - uses: evil/action@main\n")
    workflow = tmp_path / ".github/workflows/checks.yml"
    anchor = "      - uses: astral-sh/setup-uv@"
    text = workflow.read_text()
    assert anchor in text
    workflow.write_text(text.replace(anchor, "      - uses: ./.github/actions/build\n" + anchor, 1))
    assert action not in action_pins.action_files(tmp_path), "the file is not read"
    monkeypatch.setattr(checks, "ROOT", tmp_path)
    monkeypatch.setattr(checks, "TOOLS", tmp_path / "tools/security")
    with pytest.raises(AssertionError, match="build"):
        checks.test_every_local_action_a_workflow_runs_is_read_by_the_checks()


@pytest.mark.parametrize(
    "text",
    ["c:\n  !!merge <<: {a: 1}\n", "c:\n  ? <<\n  : {a: 1}\n", "c: {? <<: {a: 1}}\n"],
    ids=["explicit merge tag", "explicit key", "explicit key in flow"],
)
def test_sound_the_loader_refuses_every_spelling_of_a_merge_key(checks, text):
    assert yaml.safe_load(text) == {"c": {"a": 1}}, "control: PyYAML merges it"
    with pytest.raises(yaml.YAMLError, match="merge key"):
        checks.parse_yaml(text)


# Adapted: this test checked the arithmetic of the text windows (every match of up to OVERLAP bytes
# whole in some window). That stream approach, the stream step and tools/security/text_windows.py
# are gone, so the arithmetic is superseded; the body now checks that they are gone and that the
# history scan reads the built configuration.
def test_sound_the_stream_and_its_windows_are_gone_and_the_history_scan_uses_the_built_config():
    steps = yaml.safe_load(read(SECRETS_YML))["jobs"]["secrets"]["steps"]
    assert not [s for s in steps if "stream" in s.get("name", "").lower()]
    assert not (ROOT / "tools/security/text_windows.py").exists()
    assert "text_windows" not in read(SECRETS_YML) and " stdin " not in read(SECRETS_YML)
    history = secrets_step("Scan the history for secrets")
    assert '--config "$RUNNER_TEMP/gitleaks.toml"' in history
