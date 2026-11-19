"""Independent review: fourth verification of the release security checks (pull request 13, commit
794b3c3), after the resolution of X-01 to X-04.

Each test states behaviour that a workflow comment, SECURITY.md or a script's docstring promises,
for a case beside a fix. A failing test is a finding in
.local/review-history/release-checks-round-1-verification-4.md; tests named ``test_control_*`` and
``test_sound_*`` pass and record what held.

The scan tests run secrets.yml's own history step with ``bash -e`` in a fresh repository, on the
configuration the job builds (gitleaks' default one without its list of skipped paths; the stream
step and text_windows.py of the reviewed commit are gone, see the comments above the adapted
tests). Key blocks are random bytes in PEM armour, built at run time; this file holds no
secret-shaped value. gitleaks and the default configuration come from LABOPS_REVIEW_TOOLS (default
/tmp/labops-rsc-tools/bin, the configuration in its parent) or PATH; the tests skip without them.
"""

import base64
import hashlib
import importlib.util
import os
import shutil
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


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load("review_v4_image_contents", "tools/security/image_contents.py")


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


# The armour lines are joined at run time so that this file holds no text the private-key rule
# matches.
ARMOUR = "PGP PRIVATE " + "KEY BLOCK-----"
BEGIN = "-----BEGIN " + ARMOUR
END = "-----END " + ARMOUR

HISTORY = "Scan the history for secrets"


def key_block(size):
    """A PGP-armoured private key block of about `size` bytes (random content)."""
    body = base64.b64encode(os.urandom(size * 3 // 4)).decode()
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return [
        BEGIN,
        "",
        *lines,
        "=AbCd",
        END,
    ]


def commit_svg(repo, block):
    """A repository with one commit: an SVG holding `block`."""
    if repo.exists():
        shutil.rmtree(repo)
    (repo / "static/img").mkdir(parents=True)
    svg = ["<svg>", "<metadata>", *block, "</metadata>", "</svg>"]
    (repo / "static/img/badge.svg").write_text("\n".join(svg) + "\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "add a badge"]):
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, env=GIT_ENV
        )
    return repo


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
    source = default_gitleaks_config()
    builder = _load("review_v4_gitleaks_config", "tools/security/gitleaks_config.py")
    config = (
        source.read_text()
        if default
        else builder.build(source.read_text(), (ROOT / ".gitleaks.toml").read_text())
    )
    (runner_temp / "gitleaks.toml").write_text(config)
    env = {**GIT_ENV, "RUNNER_TEMP": str(runner_temp), "HOME": str(tmp_path)}
    result = subprocess.run(
        ["bash", "-e", "-c", secrets_step(name)], cwd=repo, env=env, capture_output=True
    )
    return result.returncode


# ============== Y-01: a secret longer than the overlap can lie whole in no segment

# An armoured PGP secret key with an RSA-4096 primary key and three RSA-4096 subkeys (sign,
# encrypt, authenticate) exported with `gpg --armor --export-secret-keys` measured 14,056 bytes.
LONG_KEY = 14_000


def reported(repo, tmp_path, runs):
    """How many of `runs` runs of the history step report the key."""
    return sum(run_step(repo, tmp_path, HISTORY) == 1 for _ in range(runs))


# Adapted: the stream step and its overlapping segments are gone, so there is no start offset to
# choose. The test keeps its intent (a 14 kB private key in an SVG, which the default rules skip by
# path, is reported in 10 of 10 runs) and judges it on the history step with the built
# configuration, which reads the file whole. Its control is the same step with the default
# configuration, which passes.
def test_the_history_scan_reports_a_private_key_longer_than_the_old_overlap(tmp_path):
    """secrets.yml: the history is scanned with the default configuration "without its list of
    skipped paths", so a real PGP secret key with subkeys (14 kB) in an SVG is reported."""
    repo = commit_svg(tmp_path / "repo", key_block(LONG_KEY))
    assert run_step(repo, tmp_path, HISTORY, default=True) == 0, "control: .svg is skipped by path"
    found = reported(repo, tmp_path, 10)
    assert found == 10, f"a 14 kB private key block in an SVG passed the secrets job {10 - found}x"


# Adapted: the control no longer depends on segment offsets; a 6 kB block (an RSA-8192 PEM key is
# about that size) is reported every time.
def test_control_a_shorter_key_is_reported(tmp_path):
    repo = commit_svg(tmp_path / "repo", key_block(6_000))
    assert reported(repo, tmp_path, 5) == 5


# ============================================================ sound: checked and held


# Adapted: this test checked that text_windows.py reads a trickled pipe into full-width segments.
# The stream approach is gone (no stream step, no tools/security/text_windows.py), so it is
# superseded; the body checks that they are gone and that the history step uses the built config.
def test_sound_the_stream_and_its_windows_are_gone_and_the_history_scan_uses_the_built_config():
    steps = yaml.safe_load((ROOT / ".github/workflows/secrets.yml").read_text())["jobs"]["secrets"]
    assert not [s for s in steps["steps"] if "stream" in s.get("name", "").lower()]
    assert not (ROOT / "tools/security/text_windows.py").exists()
    assert '--config "$RUNNER_TEMP/gitleaks.toml"' in secrets_step(HISTORY)


def md5(path):
    return hashlib.md5(Path(path).read_bytes(), usedforsecurity=False).hexdigest()


@pytest.fixture
def packaged_tree(tmp_path):
    """A tree with merged /usr (bin -> usr/bin) holding one packaged program, recorded by a dpkg
    info directory as dpkg records it: listed through the /bin symlink, with its MD5."""
    root = tmp_path / "root"
    (root / "usr/bin").mkdir(parents=True)
    (root / "bin").symlink_to("usr/bin")
    program = root / "usr/bin/tool"
    program.write_bytes(b"\x7fELF" + b"packaged" * 8)
    program.chmod(0o755)
    info = tmp_path / "info"
    info.mkdir()
    listed = root / "bin/tool"
    (info / "tool.list").write_text(f"{root / 'bin'}\n{listed}\n")
    relative = str(listed).lstrip("/")
    (info / "tool.md5sums").write_text(f"{md5(program)}  {relative}\n")
    return root, info, program


def test_sound_a_packaged_program_is_recognised_through_merged_usr(packaged_tree):
    root, info, _ = packaged_tree
    assert probe.unpackaged_programs(str(root), probe.dpkg_files(info)) == []


def test_sound_a_packaged_program_with_new_content_is_listed(packaged_tree):
    root, info, program = packaged_tree
    program.write_bytes(b"\x7fELF" + b"replaced" * 8)
    assert probe.unpackaged_programs(str(root), probe.dpkg_files(info)) == [str(program)]


def test_sound_a_packaged_program_replaced_by_a_link_to_another_binary_is_listed(packaged_tree):
    root, info, program = packaged_tree
    other = root / "opt/custom/tool"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"\x7fELF" + b"custom!!" * 8)
    other.chmod(0o755)
    program.unlink()
    program.symlink_to(other)
    assert probe.unpackaged_programs(str(root), probe.dpkg_files(info)) == [str(other)]
