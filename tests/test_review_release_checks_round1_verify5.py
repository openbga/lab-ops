"""Independent review: fifth verification of the release security checks (pull request 13, commit
3c3c8e4, which replaced 53078d5), after the resolution of Y-01.

Each test states behaviour that a workflow comment, SECURITY.md or a script's docstring promises,
for a case beside the fix. A failing test is a finding in
.local/review-history/release-checks-round-1-verification-5.md; tests named ``test_control_*`` and
``test_sound_*`` pass and record what held.

ADOPTED from the independent reviewer (.local/review-history/
test_release_checks_round1_verify5_original.py). Where the redesign after the review (the stream
step and tools/security/text_windows.py are removed; the history scan reads a configuration the job
builds from gitleaks' default one without its list of skipped paths) changed what a test can ask,
a comment above the adaptation says what changed and why; nothing else differs.

The scan tests run secrets.yml's own step script with ``bash -e`` in a fresh repository. Key
blocks are random bytes in PGP armour, and the armour lines and tokens are built at run time; this
file holds no secret-shaped value. gitleaks and gitleaks' default configuration come from
LABOPS_REVIEW_TOOLS (default /tmp/labops-rsc-tools/bin, the configuration in its parent) or PATH;
the tests skip without them, and never download anything.
"""

import base64
import importlib.util
import os
import random
import re
import shutil
import string
import subprocess
from pathlib import Path

import pytest
import yaml
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
    "GIT_AUTHOR_DATE": "2026-11-19T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-11-19T00:00:00Z",
}
# Joined at run time so that this file holds no text the private-key rule matches.
ARMOUR = "PGP PRIVATE " + "KEY BLOCK-----"
BEGIN = "-----BEGIN " + ARMOUR
END = "-----END " + ARMOUR
HISTORY = "Scan the history for secrets"


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tool(name):
    for directory in (TOOLS, TOOLS / "bin"):
        if (directory / name).is_file():
            return str(directory / name)
    found = shutil.which(name)
    if not found:
        pytest.skip(f"{name} is not installed")
    return found


def secrets_step(name):
    steps = yaml.safe_load((ROOT / ".github/workflows/secrets.yml").read_text())["jobs"]["secrets"]
    return next(step["run"] for step in steps["steps"] if step.get("name") == name)


def key_block(size):
    """A PGP-armoured private key block with `size` bytes of random content, base64-encoded."""
    body = base64.b64encode(os.urandom(size * 3 // 4)).decode()
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return [BEGIN, "", *lines, "=AbCd", END]


# Adapted: no stream offsets any more. The padding file is kept (it moves the SVG within the
# commit, as the original's alignments did), the tools copy and the log offsets are not needed.
def commit_svg(repo, padding, block):
    """One commit: a padding file, then an SVG holding `block`."""
    if repo.exists():
        shutil.rmtree(repo)
    (repo / "static/img").mkdir(parents=True)
    (repo / "aaa_padding.txt").write_text("x" * padding + "\n")
    svg = ["<svg>", "<metadata>", *block, "</metadata>", "</svg>"]
    (repo / "static/img/badge.svg").write_text("\n".join(svg) + "\n")
    commit_all(repo, "add a badge")
    return repo


def commit_all(repo, message):
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", message]):
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, env=GIT_ENV
        )


# Adapted: the job builds the configuration its history scan reads ($RUNNER_TEMP/gitleaks.toml) in
# a step that downloads gitleaks' default one, which tests never do; the helper builds it in-process
# from the default file kept next to the scanners (skipping when absent). With `default`, the step
# reads the unmodified default configuration instead (the control: it skips .svg files by path).
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


def run_step(repo, tmp_path, name, default=False):
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    if not (runner_temp / "gitleaks").exists():
        (runner_temp / "gitleaks").symlink_to(tool("gitleaks"))
    source = default_gitleaks_config().read_text()
    builder = _load("review_v5_gitleaks_config", "tools/security/gitleaks_config.py")
    config = source if default else builder.build(source, (ROOT / ".gitleaks.toml").read_text())
    (runner_temp / "gitleaks.toml").write_text(config)
    env = {**GIT_ENV, "RUNNER_TEMP": str(runner_temp), "HOME": str(tmp_path)}
    result = subprocess.run(
        ["bash", "-e", "-c", secrets_step(name)], cwd=repo, env=env, capture_output=True
    )
    return result.returncode


# ======================== Z-01: the default allowlist also excuses a value starting with true

# A value the generic rule reports (a keyword, an assignment and 24 random characters, entropy
# above 3.5); the alphabet has no l, so the value cannot hold "false" or "null" by chance.
ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "lLoO01")


def generic_value(prefix=""):
    rng = random.SystemRandom()
    return prefix + "".join(rng.choice(ALPHABET) for _ in range(24))


def gitleaks_reports(tmp_path, line):
    folder = tmp_path / "scan"
    folder.mkdir(exist_ok=True)
    (folder / "settings_local.py").write_text(line + "\n")
    result = subprocess.run(
        [
            tool("gitleaks"),
            "dir",
            "--no-banner",
            "--redact",
            "--config",
            str(ROOT / ".gitleaks.toml"),
            str(folder),
        ],
        capture_output=True,
    )
    return result.returncode == 1


def not_checked_bullet():
    text = (ROOT / "SECURITY.md").read_text()
    section = text[text.index("## What is not checked") :]
    return next(
        " ".join(item.split())
        for item in section.split("\n- ")
        if "false" in item and "null" in item
    )


def test_security_md_names_every_value_the_default_allowlist_excuses(tmp_path):
    """SECURITY.md ("What is not checked"): "gitleaks' default rules pass over a value that
    contains "false" or ends in "null", whatever else it holds". The default allowlist regex is
    (?i)^true|false|null$: it also excuses a value that begins with "true", and all three in any
    letter case. Checked with gitleaks 8.30.1 and the repository's .gitleaks.toml."""
    assert gitleaks_reports(tmp_path, f'api_key = "{generic_value()}"'), "control: reported"
    assert not gitleaks_reports(tmp_path, f'api_key = "{generic_value("TrueVal")}"'), (
        "control: a value that begins with True is excused"
    )
    bullet = not_checked_bullet()
    assert re.search(r"\b(begins|starts) with \W?true", bullet, re.I), (
        f"the statement does not name values that begin with true: {bullet}"
    )


# ============ Z-03: a lone BEGIN marker pairs with an unrelated "KEY-----" in the stream


# Adapted: the stream is gone, so the pair is judged on the whole job's history scan with the built
# configuration, which reads each file apart (the control is the same step with the default
# configuration). The intent is unchanged: two files that each hold ordinary text must not make
# the job fail, for every run once such a pair is in main's history.
def test_the_history_scan_does_not_pair_a_lone_marker_with_another_files_key_line(tmp_path):
    """secrets.yml: the history scan should not report what no file holds. The private-key rule is
    lazy ([\\s\\S-]{64,}?KEY-----): a one-line BEGIN marker (code that recognises PEM text) must
    not pair with a later "KEY-----" in another file, such as a document that mentions a public
    key's END line. Both are ordinary text. Once such a pair is in main's history the scan fails on
    every run until the history is rewritten (the cause of 53078d5's red run, by the author's
    account)."""
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "docs").mkdir()
    marker = "-----BEGIN RSA " + "PRIVATE KEY-----"
    (repo / "app/keys.py").write_text(
        f'def is_pem(text):\n    return text.startswith("{marker}")\n'
    )
    guide = "".join(f"Line {i} of the deployment guide.\n" for i in range(150))
    end_line = "-----END PUBLIC " + "KEY-----"
    (repo / "docs/tls.md").write_text(guide + f"The public part ends with {end_line}.\n")
    commit_all(repo, "add PEM check")
    assert run_step(repo, tmp_path, HISTORY, default=True) == 0, (
        "control: the default config passes"
    )
    assert run_step(repo, tmp_path, HISTORY) == 0, (
        "the history scan reports a private key made of a marker in app/keys.py and a sentence in "
        "docs/tls.md"
    )


# =================== Z-02 (resolved in 3c3c8e4): the repository's own history passes


# Adapted: the job has no stream step; its own history is scanned by the history step with the
# built configuration.
def test_sound_the_secrets_job_passes_on_the_repositorys_own_history(tmp_path):
    """secrets.yml runs on every push and pull request; the repository holds no secret, so its
    own history must pass. On 53078d5 the stream step failed here (a test's PGP-shaped literal,
    and a one-line RSA marker); 3c3c8e4 assembles the markers at run time and passes."""
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    assert run_step(ROOT, tmp_path, HISTORY) == 0


# ============================================================ sound: checked and held


# Adapted: with no segments there is no bound of 20,000 bytes or worst start. The test keeps its
# intent (a block of about 19,900 bytes, the size the bound was tested at, in a file the default
# rules skip by path is reported in 10 of 10 runs) on the history step; the control is the default
# configuration, which passes.
def test_sound_a_key_of_about_the_old_bound_is_reported(tmp_path):
    repo = commit_svg(tmp_path / "repo", 50_000, key_block(19_300))
    assert run_step(repo, tmp_path, HISTORY, default=True) == 0, "control: .svg is skipped by path"
    assert sum(run_step(repo, tmp_path, HISTORY) == 1 for _ in range(10)) == 10


# Adapted: the alignments of a block against the segment step no longer exist; the parameter is now
# the size of a padding file committed before the SVG, so the block still sits at different offsets.
@pytest.mark.parametrize("padding", range(0, 4_000, 500))
def test_sound_a_key_near_the_bound_is_reported_at_every_alignment(tmp_path, padding):
    repo = commit_svg(tmp_path / "repo", padding, key_block(19_000))
    assert run_step(repo, tmp_path, HISTORY) == 1


# Adapted: the look-ahead arithmetic and the stated bound (the next two tests) belonged to
# text_windows.py; the stream step, the script and the statement of a 20,000-byte bound are gone,
# so they are superseded. The body checks that, and that the history scan uses the built config.
def test_sound_the_stream_windows_and_their_bound_are_gone():
    steps = yaml.safe_load((ROOT / ".github/workflows/secrets.yml").read_text())["jobs"]["secrets"]
    assert not [s for s in steps["steps"] if "stream" in s.get("name", "").lower()]
    assert not (ROOT / "tools/security/text_windows.py").exists()
    security = " ".join((ROOT / "SECURITY.md").read_text().split())
    workflow = " ".join((ROOT / ".github/workflows/secrets.yml").read_text().split())
    assert "20,000 bytes" not in security and "20,000" not in workflow
    assert '--config "$RUNNER_TEMP/gitleaks.toml"' in secrets_step(HISTORY)


# Adapted: the canary step is renamed, and it now generates three tokens with a function.
def test_sound_the_canary_token_cannot_be_excused_by_the_default_allowlist():
    """The canary's tokens are ghp_ and 36 characters without l: they cannot contain "false", end
    in "null" or begin with "true"."""
    canary = secrets_step(
        "Check that the settings still report a secret, whatever the file is called"
    )
    alphabet = re.search(r"tr -dc '([^']+)'", canary).group(1)
    assert alphabet == "A-KM-Za-km-z0-9"
    assert 'echo "ghp_$(' in canary
