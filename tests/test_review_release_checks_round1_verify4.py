"""Independent review: fourth verification of the release security checks (pull request 13, commit
794b3c3), after the resolution of X-01 to X-04.

Each test states behaviour that a workflow comment, SECURITY.md or a script's docstring promises,
for a case beside a fix. A failing test is a finding in
.local/review-history/release-checks-round-1-verification-4.md; tests named ``test_control_*`` and
``test_sound_*`` pass and record what held.

The stream-scan tests run secrets.yml's own step script with ``bash -e`` in a fresh repository
whose commit dates are fixed, so the byte offsets of the history stream, and therefore where
text_windows.py cuts it, are known in advance. Key blocks are random bytes in PEM armour, built at
run time; this file holds no secret-shaped value. gitleaks comes from LABOPS_REVIEW_TOOLS (default
/tmp/labops-rsc-tools/bin) or PATH; the tests skip without it.
"""

import base64
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
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


windows = _load("review_v4_text_windows", "tools/security/text_windows.py")
probe = _load("review_v4_image_contents", "tools/security/image_contents.py")


def tool(name):
    path = TOOLS / name
    if path.is_file():
        return str(path)
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

STREAM = "Scan the history for secrets as one text stream"
FILE_BY_FILE = "Scan the history for secrets, file by file"


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


def commit_svg(repo, padding, block):
    """A repository with one commit: a padding file, then an SVG holding `block`; returns the
    offset of the block's first byte in `git log -p` (the stream the step scans)."""
    if repo.exists():
        shutil.rmtree(repo)
    (repo / "static/img").mkdir(parents=True)
    shutil.copytree(ROOT / "tools", repo / "tools")
    shutil.copy(ROOT / ".gitleaks.toml", repo / ".gitleaks.toml")
    (repo / "aaa_padding.txt").write_text("x" * padding + "\n")
    svg = ["<svg>", "<metadata>", *block, "</metadata>", "</svg>"]
    (repo / "static/img/badge.svg").write_text("\n".join(svg) + "\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "add a badge"]):
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, env=GIT_ENV
        )
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "-p", "--text", "--diff-merges=first-parent", "HEAD"],
        check=True,
        capture_output=True,
        env=GIT_ENV,
    ).stdout
    return log.index(("+" + BEGIN).encode()), log


def place(tmp_path, block, start_in_step):
    """A repository whose stream holds `block` starting `start_in_step` bytes after a multiple of
    text_windows.STEP; returns (repo, start, end) of the block in the stream."""
    repo = tmp_path / "repo"
    first, _ = commit_svg(repo, 50_000, block)
    padding = 50_000 + (start_in_step - first) % windows.STEP
    start, log = commit_svg(repo, padding, block)
    assert start % windows.STEP == start_in_step, (start, start_in_step)
    end = log.index(END.encode(), start + 40) + len(END)
    return repo, start, end


def run_step(repo, tmp_path, name):
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    if not (runner_temp / "gitleaks").exists():
        (runner_temp / "gitleaks").symlink_to(tool("gitleaks"))
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
    """How many of `runs` runs of the stream step report the key (gitleaks reads its input from a
    pipe, so where its chunks end varies from run to run)."""
    return sum(run_step(repo, tmp_path, STREAM) == 1 for _ in range(runs))


# Adapted (coordinator, round 4 resolution): OVERLAP is now 20,000 bytes and STEP 4,000, so the
# 14 kB key lies whole in some segment wherever it starts. The test keeps its intent (the key is
# reported in 10 of 10 runs) at the worst start for the new step (500 bytes before a step ends),
# and its control is inverted: a segment now holds the whole block, which is the fix.
def test_the_stream_scan_reports_a_private_key_longer_than_the_overlap(tmp_path):
    """secrets.yml: the history is scanned again "as one text stream, where no file is skipped
    by its name"; text_windows.py: "any match up to OVERLAP bytes long, a multi-line private key
    included, lies whole inside some segment". A real PGP secret key with subkeys (14 kB) must be
    reported wherever it sits. The file-by-file scan skips .svg files."""
    repo, start, end = place(tmp_path, key_block(LONG_KEY), windows.STEP - 500)
    assert end - start <= windows.OVERLAP
    assert any(s <= start and end <= s + windows.WIDTH for s in range(0, end, windows.STEP)), (
        "a segment holds the whole block"
    )
    assert run_step(repo, tmp_path, FILE_BY_FILE) == 0, "control: .svg is skipped by path"
    found = reported(repo, tmp_path, 10)
    assert found == 10, (
        f"a {end - start}-byte private key block in an SVG passed the secrets job in "
        f"{10 - found} of 10 runs"
    )


def test_control_the_same_key_inside_one_segment_is_reported(tmp_path):
    repo, start, end = place(tmp_path, key_block(LONG_KEY), 500)
    assert (start % windows.STEP) + (end - start) <= windows.WIDTH
    assert reported(repo, tmp_path, 5) == 5


# Adapted (coordinator, round 4 resolution): with STEP 4,000 a start 9,000 bytes into a step no
# longer exists; the worst start, 500 bytes before a step ends, is used instead.
def test_control_a_key_within_the_overlap_is_reported_wherever_it_starts(tmp_path):
    """The fix holds within its bound: a 6 kB block (an RSA-8192 PEM key is about that size)
    starting late in a step lies whole in a segment and is reported every time."""
    repo, start, end = place(tmp_path, key_block(6_000), windows.STEP - 500)
    assert end - start <= windows.OVERLAP
    assert reported(repo, tmp_path, 5) == 5


# ============================================================ sound: checked and held


def test_sound_segments_are_whole_when_stdin_arrives_in_small_slow_writes():
    """gitleaks may read less than 100 kB at a time; text_windows.py reads its own input with
    BufferedReader.read(n), which waits for n bytes, so a trickled pipe of 300,000 bytes still
    gives full-width segments, each overlapping the next by OVERLAP."""
    writer = (
        "import sys, time\n"
        "for i in range(3000):\n"
        "    sys.stdout.buffer.write(bytes([65 + i % 26]) * 100)\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.0003)\n"
    )
    reader = (
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT / 'tools/security')!r})\n"
        "import text_windows\n"
        "sizes = [len(s) for s in text_windows.segments(sys.stdin.buffer)]\n"
        "print(len(sizes), sorted(set(sizes)))\n"
    )
    producer = subprocess.Popen([sys.executable, "-c", writer], stdout=subprocess.PIPE)
    result = subprocess.run(
        [sys.executable, "-c", reader], stdin=producer.stdout, capture_output=True, text=True
    )
    producer.wait()
    # Adapted (coordinator, round 4 resolution): the expected count and size follow the module's
    # constants (24,000-byte segments every 4,000 bytes give 70 for 300,000 bytes).
    expected = 1 + -(-(300_000 - windows.WIDTH) // windows.STEP)
    assert result.stdout.split("\n")[0] == f"{expected} [{windows.WIDTH}]", (
        result.stdout + result.stderr
    )


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
