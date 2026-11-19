"""ADOPTED from the independent reviewer (.local/review-history/
test_release_checks_round1_verify2_original.py). Where the structure changed since the reviewed
commit, a comment above the adaptation says what changed and why; nothing else differs.

Independent review: second verification of the release security checks (pull request 13, commit
853f62b), after the resolution of verification findings V-01 to V-11.

Each test states behaviour that SECURITY.md ("What is checked"), a workflow comment, a script's
docstring or the candidate's own tests promise, for a case beside a fix. A failing test is a finding
in .local/review-history/release-checks-round-1-verification-2.md; tests named ``test_control_*``
and ``test_sound_*`` pass and record what held.

Workflow step scripts are read from the parsed secrets.yml and run with ``bash -e``, as GitHub runs
a step without a ``shell:``. Scanner-backed tests use the checksum-verified gitleaks, Syft and
Grype binaries in LABOPS_REVIEW_TOOLS (default /tmp/labops-rsc-tools/bin) or on PATH, and skip
when one is absent. Grype runs offline against its cached database. Every secret is generated at
run time, so this file holds no secret-shaped value. Only copies are mutated.
"""

import importlib.util
import io
import json
import os
import random
import shutil
import string
import subprocess
import tomllib
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import yaml
from django.conf import settings

ROOT = Path(settings.BASE_DIR)
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
# actions/download-artifact: the v4.1.1 commit (GHSA-cxww-7g56-2vh6, High, fixed in 4.1.3).
DOWNLOAD_V411 = "actions/download-artifact@6b208ae046db98c579e8a3aa621ab581ff575935"
# kartverket/github-workflows v2.7.4 (lightweight tag), GHSA-f9qj-7gh3-mhj4 (High, fixed 2.7.5).
REUSABLE_V274 = (
    "kartverket/github-workflows/.github/workflows/run-trivy.yml"
    "@f1d9eac96bc37a19b81a465ef221f14195a8b974 # v2.7.4"
)
CHECKOUT_PIN = "3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1"
SETUP_UV_LINE = (
    "      - uses: astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2 # v10.3.3\n"
)


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load("review_v2_grype_gate", "tools/security/grype_gate.py")
action_pins = _load("review_v2_action_pins", "tools/security/check_action_pins.py")
coverage = _load("review_v2_sbom_coverage", "tools/security/sbom_coverage.py")
probe = _load("review_v2_image_contents", "tools/security/image_contents.py")


@pytest.fixture
def checks():
    """A fresh copy of the candidate's tests/test_security_checks.py, safe to monkeypatch."""
    return _load("review_v2_candidate_checks", "tests/test_security_checks.py")


def read(relative):
    return (ROOT / relative).read_text()


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


def run_gate(name, sbom):
    """tools/security/grype_gate.py as the workflow runs it: (exit status, scanner output)."""
    outputs = []

    def runner(command, **kwargs):
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        result = subprocess.run(command, env=SCANNER_ENV, **kwargs)
        outputs.append((result.stdout or "") + (result.stderr or ""))
        return result

    code = gate.run(name, grype(), Path(sbom).resolve(), runner=runner)
    return code, "\n".join(outputs)


def cyclonedx(*components):
    return {"bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1, "components": components}


# ------------------------------------------------------------------- repository copies


def repository_copy(tmp_path):
    """The inputs the workflow tests and the dependency job read: workflows, tools, locks."""
    copy = tmp_path / "repo"
    shutil.copytree(ROOT / ".github", copy / ".github")
    shutil.copytree(ROOT / "tools", copy / "tools")
    for relative in ("uv.lock", "pyproject.toml", ".syft.yaml", "static/vendor/package-lock.json"):
        (copy / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / relative, copy / relative)
    return copy


def add_composite_action(copy, uses_lines):
    """A local composite action holding `uses_lines`, run by a step added to checks.yml."""
    action = copy / ".github/actions/fetch/action.yml"
    action.parent.mkdir(parents=True)
    steps = "".join(f"    - uses: {line}\n" for line in uses_lines)
    action.write_text(
        "name: fetch\ndescription: Fetch the build output\nruns:\n  using: composite\n"
        f"  steps:\n{steps}"
    )
    checks_yml = copy / ".github/workflows/checks.yml"
    text = checks_yml.read_text()
    assert SETUP_UV_LINE in text
    checks_yml.write_text(
        text.replace(SETUP_UV_LINE, SETUP_UV_LINE + "      - uses: ./.github/actions/fetch\n", 1)
    )


def dependency_listing(copy, out):
    """The dependency job's listing step (Syft on the checkout with .syft.yaml)."""
    subprocess.run(
        [
            tool("syft"),
            "scan",
            f"dir:{copy.resolve()}",
            "--config",
            str(copy / ".syft.yaml"),
            "-q",
            "--output",
            f"cyclonedx-json@1.6={out}",
        ],
        check=True,
        env=SCANNER_ENV,
    )
    return json.loads(Path(out).read_text())


# Adapted: sbom_coverage.py --locked and check_action_pins.py now read action_files(): the
# workflows and every local composite action (W-01).
def locked_coverage_problems(copy, sbom):
    """sbom_coverage.py --locked on the copy: what main() reads, from the copy."""
    workflows = {
        str(path.relative_to(copy)): path.read_text() for path in action_pins.action_files(copy)
    }
    problems, _ = coverage.check_locked(
        sbom,
        tomllib.loads((copy / "uv.lock").read_text()),
        json.loads((copy / "static/vendor/package-lock.json").read_text()),
        workflows,
    )
    return problems


WORKFLOW_TESTS = (
    "test_every_workflow_file_is_a_mapping_with_github_keys",
    "test_every_action_is_pinned_to_a_commit_sha_with_a_version_comment",
    "test_the_pin_check_script_reads_every_uses_line_of_every_workflow",
    "test_every_checkout_drops_the_token",
    # Adapted: added with the check that every local action is one the other checks read.
    "test_every_local_action_a_workflow_runs_is_read_by_the_checks",
    "test_every_workflow_defaults_to_read_only_contents",
    "test_write_permissions_appear_only_where_intended",
    "test_security_workflow_is_exactly_the_expected_document",
    "test_secrets_workflow_is_exactly_the_expected_document",
    "test_codeql_workflow_is_exactly_the_expected_document",
)


def failing_workflow_tests(checks, monkeypatch, copy):
    """Names of the candidate's workflow tests that fail when the repository is `copy`."""
    monkeypatch.setattr(checks, "ROOT", copy)
    failures = []
    for name in WORKFLOW_TESTS:
        try:
            getattr(checks, name)()
        except AssertionError:
            failures.append(name)
    return failures


# Adapted: the pin script reads action_files() (workflows and composite actions), as main() does.
def pin_script_findings(copy):
    """(pins, problems) check_action_pins.py reads from the copy, without asking GitHub."""
    found, unreadable = [], []
    for path in action_pins.action_files(copy):
        pins, problems = action_pins.pins(path.read_text(), path.relative_to(copy).as_posix())
        found += pins
        unreadable += problems
    return found, unreadable


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
    shutil.copy(ROOT / ".gitleaks.toml", path / ".gitleaks.toml")
    (path / "app.py").write_text("a = 1\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    return path


def commit(repo, files, message="change"):
    for relative, text in files.items():
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo / relative).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


# Adapted: with composite actions read, the pin script is what resolves a version comment. This is
# its comparison on the copy, against a resolver that knows every pin of the workflows as true, the
# v4.1.1 commit as v4.1.1, and any other release of download-artifact as another commit.
def pin_script_problems(copy):
    pinned, _ = pin_script_findings(copy)
    truth = {(action_pins.repository(repo), tag): sha for _, repo, sha, tag in pinned}
    truth[("actions/download-artifact", "v4.1.1")] = DOWNLOAD_V411.split("@")[1]
    truth[("actions/download-artifact", "v4.1.8")] = "a" * 40
    return action_pins.problems(pinned, lambda repo, tag: truth[(repo, tag)])


def secrets_step(name):
    """The `run` script of the secrets.yml step called `name`."""
    steps = yaml.safe_load(read(".github/workflows/secrets.yml"))["jobs"]["secrets"]["steps"]
    return next(step["run"] for step in steps if step.get("name") == name)


def run_step(script, repo, tmp_path, extra_env=None):
    # Adapted: the stream step runs `python3 tools/security/text_windows.py` from the checkout, so
    # the repository under test gets an untracked copy of the script (never committed).
    window = repo / "tools/security/text_windows.py"
    if not window.exists():
        window.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "tools/security/text_windows.py", window)
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    if not (runner_temp / "gitleaks").exists():
        (runner_temp / "gitleaks").symlink_to(tool("gitleaks"))
    path = runner_temp / "step.sh"
    path.write_text(script)
    env = {**GIT_ENV, "RUNNER_TEMP": str(runner_temp), "HOME": str(tmp_path), **(extra_env or {})}
    return subprocess.run(["bash", "-e", str(path)], cwd=repo, env=env, capture_output=True)


# ============================== W-01 local composite actions: pins and audit never read them


def test_every_action_a_workflow_runs_through_a_local_composite_action_is_checked(
    checks, monkeypatch, tmp_path
):
    """SECURITY.md: the actions the workflows use are pinned to commits, and an action whose
    pinned commit is not the release its comment names fails the audit. A step may run a local
    composite action (`uses: ./.github/actions/fetch`, exempt as local); the actions inside its
    action.yml run too, but neither check_action_pins.py nor the candidate's tests read
    .github/actions/. Here it holds an unpinned action and the v4.1.1 commit commented v4.1.8."""
    copy = repository_copy(tmp_path)
    control = failing_workflow_tests(checks, monkeypatch, copy)
    assert control == [], control
    add_composite_action(copy, ["evil/action@main", f"{DOWNLOAD_V411} # v4.1.8"])
    failures = failing_workflow_tests(checks, monkeypatch, copy)
    found, unreadable = pin_script_findings(copy)
    assert failures or unreadable, (
        "an unpinned action and a misnamed pin inside .github/actions/fetch/action.yml pass "
        f"every workflow test and the pin script ({len(found)} pins read)"
    )


@pytest.mark.parametrize("comment", ["v4.1.1", "v4.1.8"])
def test_the_audit_judges_an_action_in_a_composite_action_by_its_commit(tmp_path, comment):
    """Resolution of RC-06: the dependency audit reads an action's version from its comment, so
    every comment must be checked against the pinned commit. Syft lists the actions inside a
    local composite action; the comment there is never checked, so the v4.1.1 commit commented
    v4.1.8 passes coverage and the audit. The v4.1.1 comment is the control."""
    copy = repository_copy(tmp_path)
    add_composite_action(copy, [f"{DOWNLOAD_V411} # {comment}"])
    listing = tmp_path / "dependencies.cdx.json"
    sbom = dependency_listing(copy, listing)
    listed = {
        (c["name"], c["version"]) for c in sbom["components"] if "download-artifact" in c["name"]
    }
    assert ("actions/download-artifact", comment) in listed, listed
    if locked_coverage_problems(copy, sbom):
        return  # the coverage check stops the job before the audit
    # Adapted: a composite action's pins are now read by the coverage check and by the pin check
    # (W-01), which runs first in the job; a comment that names another release stops it there.
    if pin_script_problems(copy):
        return
    code, output = run_gate("dependencies", listing)
    assert code == 2 and "GHSA-cxww-7g56-2vh6" in output, (
        f"the audit passes {DOWNLOAD_V411} (v4.1.1) commented {comment} in a composite action"
    )


# ========================= W-02 reusable workflows keep their path name and are never matched


def test_the_audit_matches_advisories_for_a_reusable_workflow_call(tmp_path):
    """SECURITY.md: the actions the workflows use are audited, "an action inside a repository
    ... under its repository's name". grype_gate.with_repository_actions() adds that name only for
    Syft's `github-action` type; a job that calls a reusable workflow is `github-action-workflow`,
    so kartverket/github-workflows v2.7.4 (GHSA-f9qj-7gh3-mhj4, High) passes the audit."""
    copy = repository_copy(tmp_path)
    (copy / ".github/workflows/trivy.yml").write_text(
        "name: trivy\non: pull_request\npermissions:\n  contents: read\njobs:\n  scan:\n"
        f"    uses: {REUSABLE_V274}\n"
    )
    listing = tmp_path / "dependencies.cdx.json"
    sbom = dependency_listing(copy, listing)
    kinds = {
        prop["value"]
        for c in sbom["components"]
        if c["name"].startswith("kartverket/")
        for prop in c.get("properties", [])
        if prop["name"] == "syft:package:type"
    }
    assert kinds == {"github-action-workflow"}, kinds
    assert locked_coverage_problems(copy, sbom) == [], "control: coverage accepts the listing"
    code, output = run_gate("dependencies", listing)
    assert code == 2 and "GHSA-f9qj-7gh3-mhj4" in output, f"the audit passes it (exit {code})"


def test_control_the_reusable_workflow_is_matched_under_its_repository_name(tmp_path):
    component = {
        "bom-ref": "rw",
        "type": "library",
        "name": "kartverket/github-workflows",
        "version": "v2.7.4",
        "purl": "pkg:github/kartverket/github-workflows@v2.7.4",
        "properties": [{"name": "syft:package:type", "value": "github-action"}],
    }
    listing = tmp_path / "renamed.cdx.json"
    listing.write_text(json.dumps(cyclonedx(component)))
    code, output = run_gate("dependencies", listing)
    assert code == 2 and "GHSA-f9qj-7gh3-mhj4" in output


# ========================= W-03 the text stream is scanned in chunks that can split a token


# Adapted (coordinator, round 4 resolution): gitleaks 8.30.1's default rules pass over a value
# that contains "false" or ends in "null" (the global allowlist regex `(?i)^true|false|null$`), so
# among 6,000 random tokens one occasionally went unreported for that reason, not because of a
# chunk cut. The planted tokens therefore have no letter l; SECURITY.md states the default rules'
# behaviour.
def unexcused_token():
    alphabet = "".join(c for c in string.ascii_letters + string.digits if c not in "lL")
    return "ghp" + "_" + "".join(random.SystemRandom().choice(alphabet) for _ in range(36))


def test_the_stream_scan_reports_every_token_in_a_file_the_file_by_file_scan_skips(tmp_path):
    """secrets.yml: the history is scanned again "as one text stream, where no file is skipped
    by its name". gitleaks reads stdin in chunks (100 kB, then up to 25 kB more looking for a
    blank line, sources/file.go and sources/common.go at v8.30.1); a diff line has no blank line,
    so a long line is cut where a read ends and a token across the cut is not reported. The
    file-by-file scan skips package-lock.json by path, so nothing else reads it."""
    repo = new_repo(tmp_path / "repo")
    tokens = [unexcused_token() for _ in range(6000)]
    lock = '{"lockfileVersion": 3, "note": "' + " ".join(tokens) + '"}\n'
    commit(repo, {"static/vendor/package-lock.json": lock}, "record vendored packages")
    history = secrets_step("Scan the history for secrets, file by file")
    assert run_step(history, repo, tmp_path).returncode == 0, "control: skipped by path"
    report = tmp_path / "stream.json"
    # Adapted: windows overlap by design, so a token inside an overlap is reported once per window
    # that holds it. The test therefore compares the set of reported tokens (the step is run
    # without --redact so that the report names them) with the planted ones: every one is found.
    stream = secrets_step("Scan the history for secrets as one text stream").rstrip()
    assert " --redact " in stream
    stream = stream.replace(" --redact ", " ")
    stream += ' --report-format json --report-path "$REPORT"\n'
    result = run_step(stream, repo, tmp_path, {"REPORT": str(report)})
    assert result.returncode == 1, "control: the stream scan reports tokens"
    reported = {finding["Secret"] for finding in json.loads(report.read_text())}
    assert reported == set(tokens), (
        f"the stream scan reported {len(reported & set(tokens))} of the {len(tokens)} tokens in "
        f"package-lock.json, and {len(reported - set(tokens))} others"
    )


# ============================= W-04 pushes the secret workflow is never started for


def test_every_push_the_secret_workflow_promises_to_scan_starts_it():
    """secrets.yml: "It runs on every push to any branch ... so a secret pushed and then replaced
    before a pull request exists is still reported by the run of the push that carried it";
    SECURITY.md: "The secret scan also runs on every push to any branch". GitHub starts no push
    or pull_request run when the head commit's message holds [skip ci] (or [ci skip], [no ci],
    [skip actions], [actions skip]), and `branches: ["**"]` alone excludes tag pushes, so a
    commit pushed only under a tag is never scanned. Neither limit is stated."""
    # Adapted: secrets.yml now scans tag pushes (`tags: ["**"]`), and its header and SECURITY.md
    # ("What is not checked") state the [skip ci] limit; the assertion below accepts that statement.
    workflow = yaml.safe_load(read(".github/workflows/secrets.yml"))
    push = workflow[True]["push"] if True in workflow else workflow["on"]["push"]
    covers_tags = not push or "tags" in push
    stated = (read("SECURITY.md") + read(".github/workflows/secrets.yml")).lower()
    assert covers_tags and "skip ci" in stated, (
        f"push filter {push}: tags covered={covers_tags}; [skip ci] limit stated="
        f"{'skip ci' in stated}"
    )


# =============================== W-05 a checkout spelled in another letter case


@pytest.mark.parametrize("spelling", ["actions/checkout", "Actions/Checkout"])
def test_every_checkout_drops_the_token_whatever_the_letter_case(
    checks, monkeypatch, tmp_path, spelling
):
    """test_security_checks.py: every checkout sets persist-credentials: false. GitHub resolves
    owner and repository names without regard to case (as Grype's matching does, see
    test_sound_grype_matches_action_names_in_any_letter_case), but checkouts_keeping_credentials()
    looks for values that start with `actions/checkout@`. The lower-case spelling is the control."""
    copy = repository_copy(tmp_path)
    workflow = copy / ".github/workflows/checks.yml"
    text = workflow.read_text()
    old = f"      - uses: actions/checkout@{CHECKOUT_PIN}\n        with:\n"
    old += "          persist-credentials: false\n"
    assert old in text
    workflow.write_text(text.replace(old, f"      - uses: {spelling}@{CHECKOUT_PIN}\n", 1))
    monkeypatch.setattr(checks, "ROOT", copy)
    with pytest.raises(AssertionError):
        checks.test_every_checkout_drops_the_token()


# ===================== W-06 the loader accepts YAML merge keys, which GitHub refuses


def test_the_loader_refuses_what_github_refuses_to_run(checks, monkeypatch):
    """test_security_checks.py: GitHubLoader "reads YAML as GitHub does". GitHub Actions accepts
    anchors and aliases but not merge keys (`<<`); such a workflow does not run. PyYAML expands
    them, so a secrets.yml that GitHub will not run passes the exact comparison."""
    text = read(".github/workflows/secrets.yml")
    old = "        with:\n          fetch-depth: 0\n          persist-credentials: false\n"
    assert old in text
    merged = text.replace(
        old, "        with:\n          <<: {fetch-depth: 0, persist-credentials: false}\n", 1
    )
    # Adapted: the loader now refuses the merge key (W-06), so the control is PyYAML's own loader.
    assert yaml.safe_load(merged) == yaml.safe_load(text), "control: PyYAML expands it"
    with pytest.raises(yaml.YAMLError, match="merge key"):
        checks.parse_yaml(merged)
    texts = checks.workflow_texts()
    monkeypatch.setattr(checks, "workflow_texts", lambda: {**texts, "secrets.yml": merged})
    with pytest.raises(AssertionError):
        checks.test_secrets_workflow_is_exactly_the_expected_document()


# ========================== W-07 the image listing reads programs in PATH directories only


def test_the_image_listing_names_every_program_no_debian_package_installed(monkeypatch, tmp_path):
    """SECURITY.md: the image check covers "every other program no Debian package installed (such
    as uv)", and coverage requires each as an SBOM location. image_contents.py looks for ELF files
    in PATH and six fixed directories only, so a program installed elsewhere (/opt/<tool>/bin,
    run by its full path) is not listed and nothing requires the SBOM to know it."""
    on_path = tmp_path / "usr/local/bin"
    elsewhere = tmp_path / "opt/report-tool/bin"
    for folder, name in ((on_path, "uv"), (elsewhere, "report-tool")):
        folder.mkdir(parents=True)
        (folder / name).write_bytes(b"\x7fELF" + bytes(60))
        (folder / name).chmod(0o755)
    # Adapted: the probe walks the whole tree from "/" (W-07), so it is pointed at the tree built
    # here instead of the directory list (PROGRAM_DIRS no longer exists).
    real_programs = probe.unpackaged_programs
    monkeypatch.setattr(
        probe,
        "unpackaged_programs",
        lambda root, owned: real_programs(str(tmp_path.resolve()), owned),
    )
    monkeypatch.setattr(probe, "dpkg_files", lambda: set())
    monkeypatch.setattr(probe, "os_release", lambda: {"id": "debian", "version_id": "12"})
    monkeypatch.setattr(probe, "deb_packages", lambda: {"libc6": ["2.36"]})
    monkeypatch.setattr(probe, "python_packages", lambda: {"django": ["5.2.19"]})
    out = io.StringIO()
    with redirect_stdout(out):
        probe.main()
    programs = json.loads(out.getvalue())["programs"]
    assert str((on_path / "uv").resolve()) in programs, "control: a program on PATH is listed"
    assert str((elsewhere / "report-tool").resolve()) in programs, (
        f"a program outside PATH is not listed: {programs}"
    )


# ============================================================ sound: checked and held


def test_sound_a_uses_key_the_pin_script_cannot_see_fails_the_candidate_tests(
    checks, monkeypatch, tmp_path
):
    """check_action_pins.py matches `uses` keys by line, so YAML's explicit-key form
    (`? uses` / `: value`) escapes it; the candidate's tests parse the document and compare the
    number of pins with the script's, so the change still fails a test."""
    copy = repository_copy(tmp_path)
    workflow = copy / ".github/workflows/checks.yml"
    text = workflow.read_text()
    workflow.write_text(
        text.replace(
            SETUP_UV_LINE,
            SETUP_UV_LINE + f"      - ? uses\n        : {DOWNLOAD_V411} # v4.1.8\n",
            1,
        )
    )
    assert checks.parse_yaml(workflow.read_text())
    assert pin_script_findings(copy)[1] == [], "the script does not see the explicit key"
    assert "test_the_pin_check_script_reads_every_uses_line_of_every_workflow" in (
        failing_workflow_tests(checks, monkeypatch, copy)
    )


@pytest.mark.parametrize("relative", ["docs/.gitleaksignore", ".gitleaksignore"])
def test_sound_the_guard_finds_a_gitleaksignore_anywhere(tmp_path, relative):
    repo = new_repo(tmp_path / "repo")
    commit(repo, {relative: "settings_local.py:github-pat:1\n"})
    guard = secrets_step("Check that only .gitleaks.toml can excuse a finding")
    assert run_step(guard, repo, tmp_path).returncode == 1


@pytest.mark.parametrize("value", ["!!str", "' '", "'*'", "!!null 3.14.10", "&v ''"])
def test_sound_the_grype_check_rejects_versions_that_read_as_empty_or_any(checks, value):
    text = read(".grype.yaml").replace("version: 3.14.10", f"version: {value}")
    assert text != read(".grype.yaml")
    assert checks.grype_problems(text), value


def test_sound_grype_matches_action_names_in_any_letter_case(tmp_path):
    component = {
        "bom-ref": "a",
        "type": "library",
        "name": "Actions/Download-Artifact",
        "version": "v4.1.1",
        "purl": "pkg:github/Actions/Download-Artifact@v4.1.1",
        "properties": [{"name": "syft:package:type", "value": "github-action"}],
    }
    listing = tmp_path / "case.cdx.json"
    listing.write_text(json.dumps(cyclonedx(component)))
    code, output = run_gate("dependencies", listing)
    assert code == 2 and "GHSA-cxww-7g56-2vh6" in output
