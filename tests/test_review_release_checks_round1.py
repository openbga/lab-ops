"""Independent review, round 1, of the release security checks (pull request 13, commit e20e658).

Each test states behaviour that SECURITY.md ("What is checked", "What is not checked"), a workflow
or configuration comment, a docstring or the candidate's own tests promise. A failing test is a
finding in .local/review-history/release-checks-round-1.md; tests named ``test_control_*`` and
``test_sound_*`` pass and record what held.

Scanner-backed tests run the checksum-verified gitleaks, Syft and Grype binaries found in
LABOPS_REVIEW_TOOLS (default /tmp/labops-rsc-tools/bin) or on PATH, and skip when one is absent.
Grype runs offline against its cached database. Every secret is generated at run time, so this
file holds no secret-shaped value. Only copies are mutated, never the repository's files.

Adopted unchanged in intent from the reviewer's file (the original is kept in
.local/review-history/test_release_checks_round1_original.py). Where a test needed a change for
the interface after the review (the Grype gate script, the image coverage mode, the
stdin scan, the new strict helpers), a comment above it says what changed and why.
"""

import importlib.util
import inspect
import json
import os
import random
import re
import shlex
import shutil
import string
import subprocess
import tomllib
from pathlib import Path

import pytest
from django.conf import settings
from packaging.markers import Marker

ROOT = Path(settings.BASE_DIR)
SECURITY_YML = ".github/workflows/security.yml"
TOOLS = Path(os.environ.get("LABOPS_REVIEW_TOOLS", "/tmp/labops-rsc-tools/bin"))
SCANNER_ENV = {
    **os.environ,
    "GRYPE_DB_AUTO_UPDATE": "false",
    "GRYPE_CHECK_FOR_APP_UPDATE": "false",
    "SYFT_CHECK_FOR_APP_UPDATE": "false",
}
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Review",
    "GIT_AUTHOR_EMAIL": "review@example.invalid",
    "GIT_COMMITTER_NAME": "Review",
    "GIT_COMMITTER_EMAIL": "review@example.invalid",
}
# actions/download-artifact v4.1.1 (tag object 21e5c25 peels to this commit); GHSA-cxww-7g56-2vh6
# affects releases before 4.1.3.
VULNERABLE_ACTION = "actions/download-artifact@6b208ae046db98c579e8a3aa621ab581ff575935"
# GHSA-wj6h-64fc-37mp (ecdsa, High) has no fixed release; 0.19.2 has no other advisory.
UNFIXED_PYPI = ("ecdsa", "0.19.2", "GHSA-wj6h-64fc-37mp")
EXCUSED_CVE = "CVE-2026-119170"


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


coverage = _load("review_round1_sbom_coverage", "tools/security/sbom_coverage.py")
action_pins = _load("review_round1_action_pins", "tools/security/check_action_pins.py")


@pytest.fixture
def checks():
    """A fresh copy of the candidate's tests/test_security_checks.py, safe to monkeypatch."""
    return _load("review_round1_candidate_checks", "tests/test_security_checks.py")


def read(relative):
    return (ROOT / relative).read_text()


# ------------------------------------------------------------------------------ scanners


def tool(name):
    path = TOOLS / name
    if path.is_file():
        return str(path)
    found = shutil.which(name)
    if not found:
        pytest.skip(f"{name} is not installed")
    return found


def grype():
    binary = tool("grype")
    status = subprocess.run(
        [binary, "db", "status"], capture_output=True, text=True, env=SCANNER_ENV
    )
    if status.returncode:
        pytest.skip("Grype has no usable cached vulnerability database")
    return binary


def run_grype(sbom, *args, cwd):
    """Grype as anchore/scan-action v7.4.2 invokes it, plus JSON output for the assertions."""
    out = Path(cwd) / "grype.json"
    result = subprocess.run(
        [grype(), "-q", "-o", "json", "--file", str(out), *args, f"sbom:{sbom}"],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=SCANNER_ENV,
    )
    report = json.loads(out.read_text()) if out.exists() and out.stat().st_size else {}
    return result.returncode, report


def matched_ids(report):
    return {match["vulnerability"]["id"] for match in report.get("matches", [])}


def ignored_ids(report):
    return {match["vulnerability"]["id"] for match in report.get("ignoredMatches", [])}


def cyclonedx(*components):
    return {"bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1, "components": components}


def component(purl, name, version, **extra):
    return {
        "bom-ref": purl,
        "type": "library",
        "name": name,
        "version": version,
        "purl": purl,
        **extra,
    }


# --------------------------------------------------------------------------------- git


def fake_token():
    """A GitHub token-shaped random value, built at run time."""
    alphabet = string.ascii_letters + string.digits
    return "ghp" + "_" + "".join(random.SystemRandom().choice(alphabet) for _ in range(36))


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=GIT_ENV
    ).stdout.strip()


def new_repo(path):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "app.py").write_text("a = 1\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    return path


def _shell_words(command):
    """The words of one logical shell command (continuation lines joined), quotes removed."""
    return shlex.split(" ".join(line.strip().removesuffix("\\") for line in command))


def _workflow_command(marker):
    """The lines of the first logical command in security.yml that holds `marker`."""
    lines = read(SECURITY_YML).splitlines()
    start = next(i for i, line in enumerate(lines) if marker in line)
    command = []
    for line in lines[start:]:
        command.append(line)
        if not line.rstrip().endswith("\\"):
            return command
    return command


# Adapted: the reviewer's helper ran only the history command. The workflow's secret scan is
# two commands (the history scan, and a stdin scan of the history of the files gitleaks skips by
# name), so RC-10 is judged on the job. This runs both as the workflow writes them, with the
# quoted --log-opts="..." read as one argument, and the repository's config unless one is given.
def secret_scan_commands(config=None):
    """[(git command or None, gitleaks arguments)] of security.yml's secret scan, with the
    gitleaks binary left out and the config path replaced by `config`."""
    config = str(config or ROOT / ".gitleaks.toml")

    def with_config(args):
        return [config if arg == ".gitleaks.toml" else arg for arg in args]

    history = _shell_words(_workflow_command('"$RUNNER_TEMP/gitleaks" git '))[1:]
    assert history[0] == "git" and history[-1] == ".", history
    assert any(arg.startswith("--log-opts=") for arg in history), history
    stdin = _workflow_command("git log -p ")
    left, _, right = " ".join(line.strip().removesuffix("\\") for line in stdin).partition(" | ")
    pipe = _shell_words([right])[1:]
    assert pipe[0] == "stdin", pipe
    return [(None, with_config(history[:-1])), (_shell_words([left]), with_config(pipe))]


def secret_scan(repo, config=None):
    """Exit status (1 when either command reports a secret) and output of the workflow's scans."""
    codes, output = [], ""
    for git_command, args in secret_scan_commands(config):
        if git_command is None:
            command, stdin = [tool("gitleaks"), *args, str(repo)], None
        else:
            log = subprocess.run(
                git_command, cwd=repo, capture_output=True, text=True, env=GIT_ENV, check=True
            )
            command, stdin = [tool("gitleaks"), *args], log.stdout
        result = subprocess.run(
            command, input=stdin, capture_output=True, text=True, env=SCANNER_ENV
        )
        codes.append(result.returncode)
        output += result.stdout + result.stderr
    return (1 if 1 in codes else next((c for c in codes if c), 0)), output


# ============================================ RC-01 and RC-15 the history the secret scan reads


def test_control_secret_in_an_ordinary_commit_fails_the_secret_scan_and_stays_redacted(tmp_path):
    token = fake_token()
    repo = new_repo(tmp_path / "repo")
    (repo / "settings_local.py").write_text(f'token = "{token}"\n')
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "add local settings")
    code, output = secret_scan(repo)
    assert code == 1, output
    assert token not in output and token[4:12] not in output


def test_secret_added_in_a_merge_commit_fails_the_secret_scan(tmp_path):
    """SECURITY.md: the secret scan covers every commit in the history of the change. A branch
    that merges main and adds a file while concluding the merge commits that file in the merge
    commit only. `git log -p` (what --log-opts=HEAD runs) shows no diff for merge commits."""
    token = fake_token()
    repo = new_repo(tmp_path / "repo")
    git(repo, "checkout", "-qb", "feature")
    (repo / "app.py").write_text("a = 1\nb = 2\n")
    git(repo, "commit", "-qam", "feature change")
    git(repo, "checkout", "-q", "main")
    (repo / "other.py").write_text("c = 3\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "main change")
    git(repo, "checkout", "-q", "feature")
    git(repo, "merge", "-q", "--no-ff", "--no-commit", "main")
    (repo / "settings_local.py").write_text(f'token = "{token}"\n')
    git(repo, "add", "settings_local.py")
    git(repo, "commit", "-qm", "Merge main into feature")
    assert git(repo, "show", "HEAD:settings_local.py").startswith("token = ")
    code, output = secret_scan(repo)
    assert code == 1, f"the secret scan passed a token committed in a merge commit:\n{output}"


def test_a_newer_push_does_not_cancel_the_secret_scan_of_an_earlier_one():
    """CONTRIBUTING: anything pushed to a public branch must be treated as exposed. With
    cancel-in-progress, a push that replaces a leaking commit cancels the only run that would
    have reported it; the next run scans a history that no longer contains the commit."""
    text = read(SECURITY_YML)
    cancels = re.search(
        r"cancel-in-progress:\s*\$\{\{\s*github\.event_name == 'pull_request'\s*\}\}", text
    )
    has_secret_job = re.search(r"^  secrets:\s*$", text, re.M)
    assert not (cancels and has_secret_job), (
        "security.yml cancels an in-progress pull request run, including its secret scan"
    )


# =========================================================== RC-02 Grype configuration check

SHIPPED_GRYPE = read(".grype.yaml")
GRYPE_WIDENINGS = {
    "global only-fixed": SHIPPED_GRYPE + "only-fixed: true\n",
    "ignore list at column 0": (
        f"ignore:\n- vulnerability: {UNFIXED_PYPI[2]}\n  reason: >-\n    Not used for signing.\n"
    ),
    "flow-style ignore list": f"ignore: [{{vulnerability: {UNFIXED_PYPI[2]}, reason: unused}}]\n",
}


@pytest.mark.parametrize("label", GRYPE_WIDENINGS)
def test_grype_config_check_rejects_settings_that_excuse_more_than_one_package_version(
    checks, label
):
    """.grype.yaml: each entry excuses one finding for one package at one version, and
    tests/test_security_checks.py checks every entry. These valid configurations excuse more and
    the check reports nothing."""
    assert checks.grype_problems(GRYPE_WIDENINGS[label]), f"{label} passes grype_problems()"


@pytest.mark.parametrize("label", ["shipped", *GRYPE_WIDENINGS])
def test_a_grype_config_the_repository_accepts_cannot_hide_an_unfixed_advisory(
    checks, tmp_path, label
):
    """The dependency audit fails on any published vulnerability, fixed or not (SECURITY.md).
    Grype runs as the workflow runs it (--fail-on low -c CONFIG); the shipped config is the
    control."""
    config = SHIPPED_GRYPE if label == "shipped" else GRYPE_WIDENINGS[label]
    if checks.grype_problems(config):
        return  # the repository's own check rejects this configuration
    (tmp_path / "grype.yaml").write_text(config)
    name, version, advisory = UNFIXED_PYPI
    sbom = tmp_path / "deps.cdx.json"
    sbom.write_text(json.dumps(cyclonedx(component(f"pkg:pypi/{name}@{version}", name, version))))
    code, report = run_grype(sbom, "--fail-on", "low", "-c", "grype.yaml", cwd=tmp_path)
    assert code == 2 and advisory in matched_ids(report), (
        f"{label}: an accepted .grype.yaml lets the audit pass {name} {version} ({advisory})"
    )


# ========================================================= RC-03 gitleaks allowlist check


def gitleaks_toml(extra_regex=None, extra_lines=""):
    regex = extra_regex or "^a-long-unique-passphrase-93$"
    return (
        "[extend]\nuseDefault = true\n\n[[allowlists]]\n"
        'description = "Synthetic passwords and keys used by the tests"\n'
        'regexTarget = "secret"\n'
        f"regexes = ['''{regex}''']\n{extra_lines}"
    )


def allowlist_widenings(token, commit):
    return {
        "anchored wildcard": gitleaks_toml(extra_regex="^.+$"),
        "stopword": gitleaks_toml(extra_lines=f'stopwords = ["{token[4:8]}"]\n'),
        "commit": gitleaks_toml(extra_lines=f'commits = ["{commit}"]\n'),
    }


@pytest.mark.parametrize("label", ["anchored wildcard", "stopword", "commit"])
def test_gitleaks_allowlist_check_rejects_entries_wider_than_one_value(checks, label):
    """.gitleaks.toml: each entry is one exact value, so anything else in the same file is
    still reported. The helper accepts an anchored wildcard, a stopword and a commit."""
    config = tomllib.loads(allowlist_widenings("ghp" + "_" + "x" * 36, "0" * 40)[label])
    assert checks.allowlist_problems(config), f"{label} passes allowlist_problems()"


@pytest.mark.parametrize("label", ["shipped", "anchored wildcard", "stopword", "commit"])
def test_a_gitleaks_config_the_repository_accepts_still_reports_a_real_token(
    checks, tmp_path, label
):
    token = fake_token()
    repo = new_repo(tmp_path / "repo")
    (repo / "settings_local.py").write_text(f'token = "{token}"\n')
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "add local settings")
    config = (
        read(".gitleaks.toml")
        if label == "shipped"
        else allowlist_widenings(token, git(repo, "rev-parse", "HEAD"))[label]
    )
    if checks.allowlist_problems(tomllib.loads(config)):
        return  # the repository's own check rejects this configuration
    (tmp_path / "gitleaks.toml").write_text(config)
    # Adapted: secret_scan() takes the config path and runs both of the workflow's scans.
    code, output = secret_scan(repo, tmp_path / "gitleaks.toml")
    assert code == 1, f"{label}: an accepted .gitleaks.toml hides a committed token:\n{output}"


# ====================================== RC-04 and RC-05 mutations the candidate tests miss


# Adapted: the candidate's module now also holds parametrized tests and tests with fixtures
# (they need arguments and test the helpers, not the workflow). Only the tests that take no
# arguments are run, as before.
def candidate_failures(checks, monkeypatch, security_yml):
    """Names of the candidate's tests that fail when security.yml reads as `security_yml`."""
    real_read, texts = checks.read, checks.workflow_texts()

    def fake_read(relative):
        return security_yml if relative == SECURITY_YML else real_read(relative)

    monkeypatch.setattr(checks, "read", fake_read)
    monkeypatch.setattr(checks, "workflow_texts", lambda: {**texts, "security.yml": security_yml})
    failures = []
    for name in sorted(dir(checks)):
        if name.startswith("test_") and not inspect.signature(getattr(checks, name)).parameters:
            try:
                getattr(checks, name)()
            except AssertionError:
                failures.append(name)
    return failures


def _insert_after(anchor, addition):
    return lambda text: text.replace(anchor, anchor + addition)


# Adapted: the workflow changed, so the strings these mutations edit changed. The behaviour each
# one switches off is the reviewer's: a Grype result that cannot fail the job (`|| true` after a
# grype_gate.py call replaces fail-build: false), continue-on-error, gitleaks exit code and
# redaction, and a narrowed history. Each mutated text must differ from the original.
GATE_MUTATIONS = {
    "Grype result ignored (|| true after the image gate)": lambda text: text.replace(
        'grype_gate.py image --grype "$GRYPE" sbom/lab-ops.cdx.json',
        'grype_gate.py image --grype "$GRYPE" sbom/lab-ops.cdx.json || true',
    ),
    "Grype result ignored (|| true after the dependency gate)": lambda text: text.replace(
        '          python3 tools/security/grype_gate.py dependencies --grype "$GRYPE" \\\n'
        '            "$RUNNER_TEMP/dependencies.cdx.json"',
        '          python3 tools/security/grype_gate.py dependencies --grype "$GRYPE" \\\n'
        '            "$RUNNER_TEMP/dependencies.cdx.json" || true',
    ),
    "every job may fail without failing the run (continue-on-error)": _insert_after(
        "    runs-on: ubuntu-latest\n", "    continue-on-error: true\n"
    ),
    "gitleaks exits 0 on a leak (--exit-code 0)": lambda text: text.replace(
        "--no-banner", "--no-banner --exit-code 0"
    ),
    "gitleaks prints secrets in the public log (--redact=0)": lambda text: text.replace(
        "--redact ", "--redact=0 "
    ),
    "gitleaks scans only the last commit (--log-opts narrowed)": lambda text: text.replace(
        '--log-opts="--diff-merges=first-parent HEAD"', "--log-opts=-1"
    ),
}
BOUNDARY_MUTATIONS = {
    "release-sbom also runs, with contents: write, on pull requests": lambda text: text.replace(
        "if: ${{ github.event_name == 'release' &&",
        "if: ${{ (github.event_name == 'release' || github.event_name == 'pull_request') &&",
    ),
    "the image job, which runs repository code, can write (flow style)": _insert_after(
        "    name: image scan and SBOM\n", "    permissions: { contents: write }\n"
    ),
    "release-sbom runs a script from the artifact": lambda text: text.replace(
        '          gh release upload "$TAG"',
        '          bash sbom/attach.sh\n          gh release upload "$TAG"',
    ),
}


def test_control_the_candidate_tests_pass_on_the_real_workflow(checks, monkeypatch):
    assert candidate_failures(checks, monkeypatch, read(SECURITY_YML)) == []


@pytest.mark.parametrize("label", GATE_MUTATIONS)
def test_the_candidate_tests_fail_when_a_gate_is_disabled(checks, monkeypatch, label):
    """tests/test_security_checks.py: a setting that weakens a check without failing any build
    is what these tests pin."""
    original = read(SECURITY_YML)
    mutated = GATE_MUTATIONS[label](original)
    assert mutated != original, "mutation did not apply"
    assert candidate_failures(checks, monkeypatch, mutated), f"no candidate test fails: {label}"


@pytest.mark.parametrize("label", BOUNDARY_MUTATIONS)
def test_the_candidate_tests_fail_when_the_write_boundary_moves(checks, monkeypatch, label):
    """security.yml: only release-sbom can write, it runs only for a published release and it
    runs no repository code."""
    original = read(SECURITY_YML)
    mutated = BOUNDARY_MUTATIONS[label](original)
    assert mutated != original, "mutation did not apply"
    assert candidate_failures(checks, monkeypatch, mutated), f"no candidate test fails: {label}"


def test_write_grant_helper_sees_flow_style_permissions(checks):
    text = "permissions:\n  contents: read\njobs:\n  image:\n    permissions: { contents: write }\n"
    assert checks.write_grants(text), "write_grants() misses `permissions: { contents: write }`"


# ======================================== RC-06 the action audit trusts the version comment


# Adapted: the comment is no longer left to the audit. check_action_pins.problems() must report a
# comment (v4.1.8) that does not name the pinned commit (v4.1.1), so the workflow stops before the
# audit reads the wrong release. The resolver stub maps each tag to its commit as GitHub does. The
# Grype part stays for the v4.1.1 control: when the comment is right, the audit catches the
# advisory in the pinned commit.
ACTION_TAGS = {
    ("actions/download-artifact", "v4.1.1"): "6b208ae046db98c579e8a3aa621ab581ff575935",
    ("actions/download-artifact", "v4.1.8"): "fa0a91b85d4f404e444e00e005971372dc801d16",
}


@pytest.mark.parametrize("comment", ["v4.1.1", "v4.1.8"])
def test_dependency_audit_judges_an_action_by_its_pinned_commit(checks, tmp_path, comment):
    """SECURITY.md: the dependency audit covers the actions the workflows use. The pinned commit
    is v4.1.1 in both cases; v4.1.1 as the comment is the control."""
    workflow = tmp_path / "src/.github/workflows/a.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "on: push\npermissions:\n  contents: read\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
        f"    steps:\n      - uses: {VULNERABLE_ACTION} # {comment}\n"
    )
    assert checks.unpinned_uses(workflow.read_text()) == []
    pinned, unreadable = action_pins.pins(workflow.read_text(), "a.yml")
    assert pinned and not unreadable
    problems = action_pins.problems(pinned, lambda repo, tag: ACTION_TAGS[(repo, tag)])
    if comment != "v4.1.1":
        assert problems, f"the pin check accepts {VULNERABLE_ACTION} (v4.1.1) commented {comment}"
        return
    assert problems == []
    src = (tmp_path / "src").resolve()
    sbom = tmp_path / "deps.cdx.json"
    subprocess.run(
        [tool("syft"), "scan", f"dir:{src}", "-q", "-o", f"cyclonedx-json@1.6={sbom}"],
        check=True,
        env=SCANNER_ENV,
        cwd=tmp_path,
    )
    code, report = run_grype(sbom, "--fail-on", "low", cwd=tmp_path)
    assert code == 2 and "GHSA-cxww-7g56-2vh6" in matched_ids(report), (
        f"the audit passes {VULNERABLE_ACTION} (v4.1.1) when its comment says {comment}"
    )


# ==================================================== RC-07 what the image coverage requires

LINUX = {
    "sys_platform": "linux",
    "platform_system": "Linux",
    "os_name": "posix",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
    "python_version": "3.14",
    "python_full_version": "3.14.10",
    "platform_machine": "x86_64",
    "extra": "",
}


def runtime_closure(lock):
    """Every package the image installs on Linux: the project's dependencies and theirs."""
    packages = {coverage.normalize(p["name"]): p for p in lock["package"]}
    project = next(p for p in lock["package"] if "virtual" in p.get("source", {}))
    todo, seen = list(project.get("dependencies", [])), set()
    while todo:
        dep = todo.pop()
        if dep.get("marker") and not Marker(dep["marker"]).evaluate(LINUX):
            continue
        name = coverage.normalize(dep["name"])
        package = packages[name]
        if name not in seen:
            seen.add(name)
            todo += package.get("dependencies", [])
        for extra in dep.get("extra", []):
            todo += package.get("optional-dependencies", {}).get(extra, [])
    return seen


def repository_inputs():
    return (
        tomllib.loads(read("uv.lock")),
        tomllib.loads(read("pyproject.toml")),
        json.loads(read("static/vendor/package-lock.json")),
    )


def sbom_of(python, vendored):
    components = [
        component(f"pkg:pypi/{name}@{version}", name, version)
        for name, versions in python.items()
        for version in versions
    ] + [
        component(f"pkg:npm/{name.replace('@', '%40')}@{version}", name, version)
        for name, versions in vendored.items()
        for version in versions
    ]
    return cyclonedx(*components)


def image_inputs(lock, python):
    """What image_contents.py would report for an image holding `python` ({name: versions}), one
    Debian package, Python and Debian 13, and the SBOM components that list the Debian package,
    the Python binary and the operating system (so a test removes only what it examines)."""
    contents = {
        "os": {"id": "debian", "version_id": "13"},
        "python": "3.14.10",
        "deb": {"libc6": ["2.41-12"]},
        "pypi": {name: sorted(versions) for name, versions in python.items()},
    }
    extras = [
        component("pkg:deb/debian/libc6@2.41-12?arch=amd64", "libc6", "2.41-12"),
        component("pkg:generic/python@3.14.10", "python", "3.14.10", type="application"),
        {"type": "operating-system", "name": "debian", "version": "13"},
    ]
    return contents, extras


# Adapted: --runtime is now check_image(sbom, contents, pyproject, vendor). The contents listing
# is the reviewer's runtime closure (every package the image installs on Linux) plus a Debian
# package, the Python version and the operating system; the SBOM lists the seven direct
# dependencies and those extras, as the reviewer's did, and must now fail for each transitive one.
def test_runtime_coverage_requires_every_python_package_the_image_installs():
    """sbom_coverage.py --image: everything image_contents.py found; SECURITY.md: the image scan
    first checks that it lists the image's runtime packages. It once checked the seven direct
    dependencies only."""
    lock, pyproject, vendor = repository_inputs()
    locked = coverage.locked_python(lock)
    direct = coverage.runtime_names(pyproject)
    closure = runtime_closure(lock)
    transitive = closure - direct
    assert {"asgiref", "sqlparse", "cryptography", "psycopg-binary"} <= transitive
    contents, extras = image_inputs(lock, {name: locked[name] for name in closure})
    listed = sbom_of({name: locked[name] for name in direct}, coverage.vendored(vendor))
    document = cyclonedx(*listed["components"], *extras)
    problems, expected = coverage.check_image(document, contents, pyproject, vendor)
    unchecked = sorted(n for n in transitive if not any(f"pypi {n} " in p for p in problems))
    assert not unchecked, f"--image passes ({expected} expected) without {unchecked}"


# Adapted: the SBOM now lacks the Debian package, the Python binary and the distro while the
# contents listing (what the image reports) holds them; check_image must name each.
def test_runtime_coverage_requires_the_operating_system_packages_and_python():
    """SECURITY.md: the image check covers operating-system packages and Python, and each scan
    checks that it lists what it must cover. An SBOM in which the Debian packages, the distro and
    the Python binary are missing (an unreadable dpkg database, say) passes."""
    lock, pyproject, vendor = repository_inputs()
    locked = coverage.locked_python(lock)
    closure = {name: locked[name] for name in runtime_closure(lock)}
    contents, extras = image_inputs(lock, closure)
    document = sbom_of(closure, coverage.vendored(vendor))
    problems, _ = coverage.check_image(document, contents, pyproject, vendor)
    text = "\n".join(problems)
    assert "deb libc6" in text and "generic python" in text and "operating system debian" in text
    full = cyclonedx(*document["components"], *extras)
    assert coverage.check_image(full, contents, pyproject, vendor)[0] == [], "the control passes"


# ================================== RC-08 an SBOM that failed its check reaches the release


# Adapted: success() is no longer what keeps a bad SBOM from a release. The coverage step now runs
# before the upload, and release-sbom requires the coverage step's `complete` output
# (needs.image.outputs.sbom-complete == 'true'), which only a passing coverage step sets.
def test_an_sbom_that_fails_its_coverage_check_is_not_attached_to_a_release(checks):
    """SECURITY.md: the coverage check stops a listing the scanner could not complete from
    passing. release-sbom runs whenever the run is not cancelled (`!cancelled()` replaces the
    implicit success()), and the artifact is uploaded before the coverage step."""
    text = read(SECURITY_YML)
    steps = checks.split_steps(checks.jobs(text)["image"])
    names = ["\n".join(step) for step in steps]
    upload = next(i for i, s in enumerate(names) if "actions/upload-artifact" in s)
    check = next(i for i, s in enumerate(names) if "sbom_coverage.py" in s)
    condition = next(
        line for line in checks.jobs(text)["release-sbom"] if re.match(r"^\s+if:", line)
    )
    requires_complete = "needs.image.outputs.sbom-complete == 'true'" in condition
    assert check < upload and requires_complete, (
        f"release-sbom ({condition.strip()}) attaches the artifact uploaded at step {upload} "
        f"even when the coverage check at step {check} fails"
    )
    coverage_step = names[check]
    assert "id: coverage" in coverage_step and 'echo "complete=true"' in coverage_step
    assert "steps.coverage.outputs.complete" in "\n".join(checks.jobs(text)["image"])


# ===================================== RC-09 the SPDX SBOM and the operations instructions


def python_binary_sboms(tmp_path):
    cpes = [
        "cpe:2.3:a:python_software_foundation:python:3.14.10:*:*:*:*:*:*:*",
        "cpe:2.3:a:python:python:3.14.10:*:*:*:*:*:*:*",
    ]
    spdx = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "review",
        "documentNamespace": "https://example.invalid/review",
        "creationInfo": {"created": "2026-11-19T00:00:00Z", "creators": ["Tool: review"]},
        "packages": [
            {
                "name": "python",
                "SPDXID": "SPDXRef-Package-binary-python",
                "versionInfo": "3.14.10",
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "externalRefs": [
                    {
                        "referenceCategory": "SECURITY",
                        "referenceType": "cpe23Type",
                        "referenceLocator": c,
                    }
                    for c in cpes
                ]
                + [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": "pkg:generic/python@3.14.10",
                    }
                ],
            }
        ],
    }
    cdx = cyclonedx(
        component(
            "pkg:generic/python@3.14.10",
            "python",
            "3.14.10",
            type="application",
            cpe=cpes[0],
            properties=[
                {"name": "syft:package:type", "value": "binary"},
                {"name": "syft:cpe23", "value": cpes[1]},
            ],
        )
    )
    (tmp_path / "python.spdx.json").write_text(json.dumps(spdx))
    (tmp_path / "python.cdx.json").write_text(json.dumps(cdx))
    return {"spdx": tmp_path / "python.spdx.json", "cyclonedx": tmp_path / "python.cdx.json"}


@pytest.mark.parametrize("fmt", ["cyclonedx", "spdx"])
def test_the_grype_exception_applies_to_both_published_sbom_formats(tmp_path, fmt):
    """docs/operations.md tells operators to scan an SPDX SBOM with --config .grype.yaml (its
    .grype.yaml applies). Syft writes the Python binary into both formats the same way (copied
    from the image SBOM); CycloneDX is the control."""
    sbom = python_binary_sboms(tmp_path)[fmt]
    shutil.copy(ROOT / ".grype.yaml", tmp_path / "grype.yaml")
    _, report = run_grype(sbom, "-c", "grype.yaml", cwd=tmp_path)
    assert EXCUSED_CVE not in matched_ids(report), f"{fmt}: .grype.yaml does not excuse it"
    assert EXCUSED_CVE in ignored_ids(report)
    assert matched_ids(report), "the entry excuses only its own vulnerability"


# =========================== RC-10 default gitleaks exclusions and the new public lockfile


def test_a_token_in_the_served_vendor_lockfile_fails_the_secret_scan(tmp_path):
    """static/vendor/package-lock.json is served at /static/vendor/package-lock.json. gitleaks'
    default rules skip every package-lock.json, so nothing in it is scanned."""
    token = fake_token()
    repo = new_repo(tmp_path / "repo")
    lock = repo / "static/vendor/package-lock.json"
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"lockfileVersion": 3, "packages": {"": {"note": token}}}))
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "record vendored packages")
    code, output = secret_scan(repo)
    assert code == 1, f"a token in the served vendor lockfile passes the secret scan:\n{output}"


# ======================================= RC-11 the vendored files and their lockfile entries


# Adapted: the check that must fail is now test_vendored_files_are_the_locked_versions (the
# README table test it replaces judged only the table against the lockfile).
def test_the_vendor_check_fails_when_a_vendored_package_is_missing_from_the_lockfile(
    checks, monkeypatch, tmp_path
):
    """tests/test_security_checks.py: a vendored package missing from the lockfile weakens a
    check without failing a build. In a copy, add a vendored directory and move htmx to a new
    version directory without touching README.md or package-lock.json."""
    vendor = tmp_path / "static/vendor"
    shutil.copytree(ROOT / "static/vendor", vendor)
    (vendor / "chartjs-4.4.0").mkdir()
    (vendor / "chartjs-4.4.0/chart.umd.min.js").write_text("/* vendored */\n")
    (vendor / "htmx-2.0.11").rename(vendor / "htmx-2.0.12")
    monkeypatch.setattr(checks, "ROOT", tmp_path)
    with pytest.raises(AssertionError):
        checks.test_vendored_files_are_the_locked_versions()


# ============================================== RC-12 to RC-14 documentation accuracy


def section(text, heading):
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, heading
    return match.group(1)


def test_static_analysis_covers_the_first_party_javascript_or_says_it_does_not():
    """SECURITY.md: the checks cover this project's source; the changelog: each change is checked
    for unsafe code. static/js holds the application's own JavaScript (about 3,500 lines)."""
    assert list((ROOT / "static/js").glob("*.js"))
    languages = re.search(r"^\s+language:\s*\[(.*?)\]", read(".github/workflows/codeql.yml"), re.M)
    analysed = {v.strip() for v in languages.group(1).split(",")}
    not_checked = section(read("SECURITY.md"), "What is not checked")
    assert analysed & {"javascript", "javascript-typescript"} or "JavaScript" in not_checked, (
        f"CodeQL analyses {sorted(analysed)}; What is not checked does not mention JavaScript"
    )


def test_architecture_does_not_say_codeql_is_apart_from_the_deployment_checks(checks):
    """docs/operations.md: deploy after CI passes. codeql.yml runs on every push to main."""
    pushes = "push" in checks.triggers(read(".github/workflows/codeql.yml"))
    architecture = " ".join(read("docs/architecture.md").split())
    claim = "They run apart from the checks a deployment waits for" in architecture
    assert not (pushes and claim), "architecture.md places static analysis outside those checks"


# Adapted: CONTRIBUTING.md no longer says `grype dir:. ...`. Its "Security checks" section lists
# the dependencies by copying the files Git would commit into build/scan and running Syft on
# that copy, then audits the SBOM. The test runs the documented listing commands (everything
# before the coverage check) in a git checkout, with and without gitignored local state, and
# audits both SBOMs as the workflow does.
def documented_listing_commands():
    text = (ROOT / "CONTRIBUTING.md").read_text()
    match = re.search(r"^### Security checks\n(.*?)(?=^#{1,3} )", text, re.M | re.S)
    assert match, "CONTRIBUTING.md has no Security checks section"
    block = re.search(r"```sh\n(.*?)```", match.group(1), re.S)
    assert block, "the Security checks section has no command block"
    lines = block.group(1).splitlines()
    end = next(i for i, line in enumerate(lines) if "sbom_coverage.py" in line)
    commands = lines[:end]
    assert any("syft scan" in line for line in commands), commands
    return "\n".join(commands)


def test_the_documented_local_dependency_audit_reports_what_ci_reports(tmp_path):
    """CONTRIBUTING: run the dependency audit before pushing. Gitignored local state (.local/,
    node_modules/) must not be scanned, so the documented listing reports what CI reports."""
    commands = documented_listing_commands()
    checkout = (tmp_path / "checkout").resolve()
    for relative in (
        "uv.lock",
        "pyproject.toml",
        ".grype.yaml",
        ".syft.yaml",
        "static/vendor/package-lock.json",
    ):
        (checkout / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / relative, checkout / relative)
    shutil.copytree(ROOT / ".github/workflows", checkout / ".github/workflows")
    (checkout / ".gitignore").write_text(".local/\nbuild/\nnode_modules/\n")
    git(checkout, "init", "-q", "-b", "main")
    env = {**SCANNER_ENV, "PATH": f"{TOOLS}:{os.environ['PATH']}", "PWD": str(checkout)}
    env.update({k: v for k, v in GIT_ENV.items() if k.startswith("GIT_")})

    def audit():
        tool("syft")
        subprocess.run(["bash", "-ec", commands], check=True, cwd=checkout, env=env)
        _, report = run_grype(
            checkout / "build/dependencies.cdx.json", "-c", ".grype.yaml", cwd=checkout
        )
        return matched_ids(report)

    ci_ids = audit()
    local_state = checkout / ".local/research/requirements.txt"
    local_state.parent.mkdir(parents=True)
    local_state.write_text("django==5.2.0\n")
    local_ids = audit()
    assert local_ids == ci_ids, (
        f"the documented listing adds {len(local_ids - ci_ids)} findings from gitignored .local/"
    )


# ================================================================ sound: checked and held


def run_block(checks, job, step_name):
    lines = checks.jobs(read(SECURITY_YML))[job]
    start = next(i for i, line in enumerate(lines) if f"name: {step_name}" in line)
    block, inside = [], False
    for line in lines[start + 1 :]:
        if re.match(r"^\s+run: \|\s*$", line):
            inside = True
            continue
        if inside:
            if line.strip() and len(line) - len(line.lstrip()) <= 8:
                break
            block.append(line[10:])
    return "\n".join(block)


HOSTILE = 'v1.0"\nX=1 $(touch pwned) ../../a/b'


def test_sound_the_image_job_cannot_inject_into_github_env(checks, tmp_path):
    # Adapted: the step now ends with `docker build`, which the test must not run, so the script
    # is cut there instead of at the old `mkdir`; the injection surface (IMAGE into GITHUB_ENV)
    # is the part before it.
    script = run_block(checks, "image", "Build the application image (no push)")
    script = script.split("docker build")[0]
    env_file = tmp_path / "github_env"
    subprocess.run(
        ["bash", "-ec", script],
        check=True,
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "REVISION": HOSTILE, "GITHUB_ENV": str(env_file)},
    )
    lines = env_file.read_text().splitlines()
    assert len(lines) == 1 and re.fullmatch(r"IMAGE=lab-ops:[A-Za-z0-9._-]+", lines[0]), lines
    assert not (tmp_path / "pwned").exists()


def test_sound_release_asset_names_are_sanitised_and_the_tag_stays_quoted(checks, tmp_path):
    script = run_block(checks, "release-sbom", "Upload the SBOM as release assets")
    sbom = tmp_path / "sbom"
    sbom.mkdir()
    for name in ("lab-ops.spdx.json", "lab-ops.cdx.json"):
        (sbom / name).write_text("{}")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$GH_ARGS"\n')
    (fake_bin / "gh").chmod(0o755)
    args_file = tmp_path / "gh_args"
    subprocess.run(
        ["bash", "-ec", script],
        check=True,
        cwd=tmp_path,
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "TAG": HOSTILE,
            "GH_ARGS": str(args_file),
            "GITHUB_REPOSITORY": "example/example",
        },
    )
    args = args_file.read_text().split("\n")
    assert args[:2] == ["release", "upload"] and HOSTILE.split("\n")[0] in args[2]
    assets = sorted(p.name for p in sbom.iterdir())
    assert all(re.fullmatch(r"lab-ops-[A-Za-z0-9._-]+\.(spdx|cdx)\.json", a) for a in assets)
    assert not (tmp_path / "pwned").exists()


def test_sound_no_workflow_expands_a_template_inside_a_run_script():
    for name in ("security.yml", "codeql.yml"):
        text = read(f".github/workflows/{name}")
        for block in re.findall(r"run: \|\n((?:\s{10,}.*\n?)+)", text):
            assert "${{" not in block, (name, block)
