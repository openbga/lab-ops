"""Independent review: verification of the round 1 fixes to the release security checks (pull
request 13, commit a1d8ef1).

Each test states behaviour that SECURITY.md ("What is checked"), a workflow or configuration
comment, a script's docstring or the candidate's own tests promise, for a case beside a round 1
fix. A failing test is a finding in .local/review-history/release-checks-round-1-verification.md;
tests named ``test_control_*`` and ``test_sound_*`` pass and record what held.

The secret scan tests run security.yml's own step scripts (the canary, the history scan and the
scan of the files gitleaks skips by name) with ``bash -e``, as GitHub runs a step without a
``shell:``. Scanner-backed tests use the checksum-verified gitleaks, Syft and Grype binaries in
LABOPS_REVIEW_TOOLS (default /tmp/labops-rsc-tools/bin) or on PATH, and skip when one is absent.
Grype runs offline against its cached database. Every secret is generated at run time, so this
file holds no secret-shaped value. Only copies are mutated, never the repository's files.

Adopted from the reviewer's file (the original is kept in
.local/review-history/test_release_checks_round1_verify_original.py). Each case still asserts what
the reviewer's did. Where the structure changed after the review (the secret scan lives in
secrets.yml and runs on every push, with four steps; the dependency audit requires the actions and
prepares the SBOM with the repository names of sub-path actions; the image listing has `programs`;
the checks module reads workflows as YAML), a comment above the test says what changed and why.
"""

import importlib.util
import inspect
import json
import os
import random
import re
import shutil
import string
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml
from django.conf import settings

ROOT = Path(settings.BASE_DIR)
SECURITY_YML = ".github/workflows/security.yml"
SECRETS_YML = ".github/workflows/secrets.yml"
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
# actions/download-artifact v4.1.1, GHSA-cxww-7g56-2vh6 (High, fixed in 4.1.3).
VULNERABLE_ACTION = "actions/download-artifact@6b208ae046db98c579e8a3aa621ab581ff575935 # v4.1.1"
# github/codeql-action v3.28.0 (annotated tag 8c78abb peels to this commit), GHSA-vqf5-2xx6-9wfm
# (High, fixed in 3.28.3).
VULNERABLE_SUBPATH_ACTION = (
    "github/codeql-action/init@48ab28a6f5dbc2a99bf1e0131198dd8f1df78169 # v3.28.0"
)


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load("review_verify_grype_gate", "tools/security/grype_gate.py")
action_pins = _load("review_verify_action_pins", "tools/security/check_action_pins.py")
coverage = _load("review_verify_sbom_coverage", "tools/security/sbom_coverage.py")


@pytest.fixture
def checks():
    """A fresh copy of the candidate's tests/test_security_checks.py, safe to monkeypatch."""
    return _load("review_verify_candidate_checks", "tests/test_security_checks.py")


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


def run_grype(sbom, config, cwd):
    out = Path(cwd) / "grype.json"
    subprocess.run(
        [grype(), "-q", "-o", "json", "--file", str(out), "--config", str(config), f"sbom:{sbom}"],
        capture_output=True,
        cwd=cwd,
        env=SCANNER_ENV,
    )
    return json.loads(out.read_text())


def ids(report, key):
    return {match["vulnerability"]["id"] for match in report.get(key, [])}


def run_gate(name, sbom):
    """tools/security/grype_gate.py as the workflow runs it: (exit status, printed output)."""

    def runner(command, **kwargs):
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        return subprocess.run(command, env=SCANNER_ENV, **kwargs)

    outputs = []

    def recording(command, **kwargs):
        result = runner(command, **kwargs)
        outputs.append((result.stdout or "") + (result.stderr or ""))
        return result

    code = gate.run(name, grype(), Path(sbom).resolve(), runner=recording)
    return code, "\n".join(outputs)


def syft_listing(source, config, out):
    """The dependency job's listing step: syft scan dir:SOURCE --config CONFIG."""
    subprocess.run(
        [
            tool("syft"),
            "scan",
            f"dir:{Path(source).resolve()}",
            "--config",
            str(config),
            "-q",
            "--output",
            f"cyclonedx-json@1.6={out}",
        ],
        check=True,
        env=SCANNER_ENV,
    )
    return json.loads(Path(out).read_text())


def cyclonedx(*components):
    return {"bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1, "components": components}


def pypi(name, version):
    purl = f"pkg:pypi/{name}@{version}"
    return {
        "bom-ref": purl,
        "type": "library",
        "name": name,
        "version": version,
        "purl": purl,
        "properties": [{"name": "syft:package:type", "value": "python"}],
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
    """A repository holding the candidate's .gitleaks.toml, as the CI checkout does."""
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


# ------------------------------------------------------------ security.yml step scripts


# Adapted: the step scripts are read from the parsed workflow (a step's `run` value) instead of
# cut out of the lines of security.yml, and the secret scan's steps are in secrets.yml.
def step_script(name, text=None, path=SECURITY_YML):
    """The `run` script of the step called `name` in the workflow at `path` (or in `text`)."""
    document = yaml.safe_load(text or read(path))
    for job in document["jobs"].values():
        for step in job["steps"]:
            if step.get("name") == name:
                return step["run"].rstrip("\n") + "\n"
    raise AssertionError(f"no step called {name!r}")


# Adapted: the secrets job now has four steps that can stop a leak, in this order: the guard
# against a .gitleaksignore file, the canary, the file-by-file history scan and the scan of the
# history as one text stream (which replaced the scan of the files gitleaks skips by name).
SECRET_STEPS = {
    "guard": "Check that only .gitleaks.toml can excuse a finding",
    "canary": "Check that the settings still report a secret",
    "history": "Scan the history for secrets, file by file",
    "stream": "Scan the history for secrets as one text stream",
}


def secret_job(repo, tmp_path):
    """{step: exit status} of the secrets job's gitleaks steps, each run with bash -e in the
    checkout (`repo`), as GitHub runs a step that names no shell."""
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    binary = runner_temp / "gitleaks"
    if not binary.exists():
        binary.symlink_to(tool("gitleaks"))
    env = {**GIT_ENV, "RUNNER_TEMP": str(runner_temp), "HOME": str(tmp_path)}
    # Adapted: the stream step runs `python3 tools/security/text_windows.py` from the checkout, so
    # the repository under test gets an untracked copy of the script (never committed).
    window = repo / "tools/security/text_windows.py"
    if not window.exists():
        window.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "tools/security/text_windows.py", window)
    codes = {}
    for label, name in SECRET_STEPS.items():
        script = runner_temp / f"{label.replace(' ', '-')}.sh"
        script.write_text(step_script(name, path=SECRETS_YML))
        result = subprocess.run(
            ["bash", "-e", str(script)], cwd=repo, env=env, capture_output=True, text=True
        )
        codes[label] = result.returncode
    return codes


# Adapted: a case is caught when ANY step of the job fails (the guard, a scan or the canary); the
# reviewer's rule named the two scans of the old job.
def job_fails_on_the_secret(codes):
    """Some step of the job failed on the committed secret."""
    return any(code != 0 for code in codes.values())


# ======================================= V-01 settings outside .gitleaks.toml hide a token


def control_plain(repo, token):
    commit(repo, {"settings_local.py": f'token = "{token}"\n'})


def gitleaksignore(repo, token):
    """gitleaks loads .gitleaksignore from the scanned directory; a fingerprint without a commit
    (file:rule:line) ignores the finding in every commit."""
    commit(
        repo,
        {
            "settings_local.py": f'token = "{token}"\n',
            ".gitleaksignore": "settings_local.py:github-pat:1\n",
        },
    )


def inline_allow(repo, token):
    """gitleaks skips a line that carries a gitleaks:allow comment."""
    commit(repo, {"settings_local.py": f'token = "{token}"  # gitleaks:allow\n'})


def attributes_no_diff(repo, token):
    """`-diff` makes git log -p print "Binary files differ" instead of the lines."""
    commit(
        repo,
        {
            "settings_local.py": f'token = "{token}"\n',
            ".gitattributes": "settings_local.py -diff\n",
        },
    )


def attributes_added_later(repo, token):
    """git log reads the checked-out .gitattributes for every commit it prints, so a later
    commit can hide an earlier one from the history scan."""
    commit(repo, {"settings_local.py": f'token = "{token}"\n'}, "leak")
    commit(repo, {".gitattributes": "*.py binary\n"}, "attributes")


HIDING = {
    ".gitleaksignore fingerprint": gitleaksignore,
    "gitleaks:allow comment": inline_allow,
    ".gitattributes -diff": attributes_no_diff,
    ".gitattributes binary, added in a later commit": attributes_added_later,
}


def test_control_the_secrets_job_fails_on_a_committed_token(tmp_path):
    repo = new_repo(tmp_path / "repo")
    control_plain(repo, fake_token())
    codes = secret_job(repo, tmp_path)
    # Adapted: the stream scan also reports a token the file-by-file scan reports.
    assert codes == {"guard": 0, "canary": 0, "history": 1, "stream": 1}, codes


@pytest.mark.parametrize("label", HIDING)
def test_no_file_or_comment_outside_the_strict_config_hides_a_token_from_the_secrets_job(
    tmp_path, label
):
    """SECURITY.md: a synthetic test value is excused "as one exact value, in .gitleaks.toml.
    Tests reject any other setting"; the secrets job runs a canary so that a clean result can be
    trusted. Each of these is a committed file or comment, outside .gitleaks.toml; the canary
    still passes and both scans pass the token."""
    repo = new_repo(tmp_path / "repo")
    HIDING[label](repo, fake_token())
    codes = secret_job(repo, tmp_path)
    assert job_fails_on_the_secret(codes), f"{label}: the secrets job passes a token ({codes})"


# ================================== V-02 files gitleaks skips by path, beyond the two named


SKIPPED_PATHS = {
    "an SVG image (text)": "static/img/badge.svg",
    "a first-party script named bootstrap-*.js": "static/js/bootstrap-tooltips.js",
    "another package-lock.json": "e2e/package-lock.json",
    "a file whose name contains gitleaks.toml": "docs/gitleaks.toml.md",
}


@pytest.mark.parametrize("label", SKIPPED_PATHS)
def test_files_the_default_rules_skip_by_path_are_still_scanned(tmp_path, label):
    """SECURITY.md: the secret scan covers every commit, "and the history of the two files
    gitleaks skips by name". gitleaks 8.30.1's default allowlist skips more paths than those two
    (every .svg, any `bootstrap*.js`, every package-lock.json, any path containing
    gitleaks.toml); five SVGs and two bootstrap files are tracked today."""
    repo = new_repo(tmp_path / "repo")
    commit(repo, {SKIPPED_PATHS[label]: f'<!-- deploy key "{fake_token()}" -->\n'})
    codes = secret_job(repo, tmp_path)
    assert job_fails_on_the_secret(codes), f"{label}: the secrets job passes a token ({codes})"


# Adapted: CONTRIBUTING's secret scan is now the same two commands as CI (the file-by-file scan
# and a text stream), written in one sh block, so the block is run whole and CI's reporting is the
# stream step.
def test_the_documented_local_secret_scan_reports_what_ci_reports(tmp_path):
    """CONTRIBUTING: run the secret scan before pushing. Its command was the history scan only;
    CI also scans the history of static/vendor/package-lock.json as text."""
    text = read("CONTRIBUTING.md")
    block = next(
        match
        for match in re.findall(r"```sh\n(.*?)```", text, re.S)
        if re.search(r"^gitleaks git ", match, re.M)
    )
    repo = new_repo(tmp_path / "repo")
    commit(repo, {"static/vendor/package-lock.json": f'{{"token": "{fake_token()}"}}\n'})
    assert secret_job(repo, tmp_path)["stream"] == 1, "control: CI reports it"
    script = re.sub(r"\bgitleaks (git|stdin)\b", rf"{tool('gitleaks')} \1", block)
    local = subprocess.run(
        ["bash", "-ec", script], cwd=repo, env=GIT_ENV, capture_output=True, text=True
    )
    assert local.returncode == 1, f"the documented local scan passes what CI fails:\n{block}"


# Adapted: the token-adding-then-removing case is now judged on the stream step, which reads the
# whole history as text (the path-limited step it replaced no longer exists).
def test_sound_the_text_stream_scan_follows_a_pull_request_that_adds_then_removes_a_token(
    tmp_path,
):
    """git log -p with --diff-merges=first-parent still prints a side branch's own commits, so a
    token added and removed again on the pull request is found through CI's merge commit."""
    repo = new_repo(tmp_path / "repo")
    commit(repo, {"static/vendor/package-lock.json": '{\n "lockfileVersion": 3\n}\n'})
    commit(repo, {"app.py": "a = 2\n"}, "main moves on")
    git(repo, "checkout", "-qb", "pr", "HEAD~1")
    lock = f'{{\n "lockfileVersion": 3,\n "token": "{fake_token()}"\n}}\n'
    commit(repo, {"static/vendor/package-lock.json": lock}, "A: lockfile with a token")
    commit(repo, {"static/vendor/package-lock.json": '{\n "lockfileVersion": 3\n}\n'}, "B")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "pr", "-m", "refs/pull/N/merge")
    assert secret_job(repo, tmp_path)["stream"] == 1


# ===================================== V-03 .grype.yaml entries that pass the strict check

PINNED_ENTRY = (
    "ignore:\n"
    "  - vulnerability: GHSA-3f63-hfp8-52jq\n"
    "    package:\n"
    "      name: pillow\n"
    "      version: 10.0.0\n"
    "    reason: >-\n"
    "      Review fixture: excuses pillow 10.0.0 only.\n"
)
GRYPE_ENTRIES = {
    "control: version 10.0.0": PINNED_ENTRY,
    'version ""': PINNED_ENTRY.replace("version: 10.0.0", 'version: ""'),
    "version ~": PINNED_ENTRY.replace("version: 10.0.0", "version: ~"),
    "version null": PINNED_ENTRY.replace("version: 10.0.0", "version: null"),
    "name null, version ~": PINNED_ENTRY.replace("name: pillow", "name: null").replace(
        "version: 10.0.0", "version: ~"
    ),
}


@pytest.mark.parametrize("label", GRYPE_ENTRIES)
def test_an_accepted_grype_entry_excuses_one_package_version_only(checks, tmp_path, label):
    """.grype.yaml: "Each entry excuses one finding, for one package at one version ... Naming
    the version makes the entry lapse when the package changes"; the strict check accepts "a
    package name without pattern characters, a version". YAML reads "", ~ and null as empty,
    and Grype reads an empty name or version as "any". The canary does not notice, because its
    packages are not the excused one. pillow 10.1.0 is affected by GHSA-3f63-hfp8-52jq."""
    config = GRYPE_ENTRIES[label]
    assert checks.grype_problems(config) == [] or not label.startswith("control")
    if checks.grype_problems(config):
        return  # the repository's own check rejects this entry
    (tmp_path / "grype.yaml").write_text(config)
    sbom = tmp_path / "image.cdx.json"
    sbom.write_text(json.dumps(cyclonedx(pypi("pillow", "10.1.0"))))
    report = run_grype(sbom, tmp_path / "grype.yaml", tmp_path)
    assert "GHSA-3f63-hfp8-52jq" in ids(report, "matches"), (
        f"{label}: an entry for pillow 10.0.0's advisory, accepted by grype_problems(), "
        "excuses it in pillow 10.1.0"
    )


# ========================= V-04 quoted keys: workflow changes the exact comparison ignores


# Adapted: a mutation can change security.yml or secrets.yml (the secret scan moved), so the file
# whose text is replaced is a parameter, as in the adopted round 1 file.
def candidate_failures(checks, monkeypatch, workflow_text, path=SECURITY_YML):
    """Names of the candidate's argument-free tests that fail when the workflow at `path` reads
    as `workflow_text` (the same harness as round 1)."""
    real_read, texts = checks.read, checks.workflow_texts()
    file_name = Path(path).name

    def fake_read(relative):
        return workflow_text if relative == path else real_read(relative)

    monkeypatch.setattr(checks, "read", fake_read)
    monkeypatch.setattr(checks, "workflow_texts", lambda: {**texts, file_name: workflow_text})
    failures = []
    for name in sorted(dir(checks)):
        if name.startswith("test_") and not inspect.signature(getattr(checks, name)).parameters:
            try:
                getattr(checks, name)()
            except AssertionError:
                failures.append(name)
    return failures


def after(anchor, addition):
    def mutate(text):
        assert anchor in text, anchor
        return text.replace(anchor, anchor + addition, 1)

    return mutate


# Adapted: each mutation is (workflow path, edit). The step that skips the history scan is edited
# in secrets.yml, under its new name, and so is the job that skips the secret scan.
QUOTED_KEY_MUTATIONS = {
    "the history scan step never runs ('if': false)": (
        SECRETS_YML,
        after(
            "      - name: Scan the history for secrets, file by file\n", "        'if': false\n"
        ),
    ),
    "the secrets job never runs ('if': false)": (
        SECRETS_YML,
        after("    name: secret scan\n", "    'if': ${{ false }}\n"),
    ),
    "the image job, which builds and runs repository code, can write": (
        SECURITY_YML,
        after("    name: image scan and SBOM\n", "    'permissions': write-all\n"),
    ),
    "steps run without -e, so a failing coverage check marks the SBOM complete": (
        SECURITY_YML,
        after("permissions:\n  contents: read\n", "'defaults':\n  run:\n    shell: bash {0}\n"),
    ),
    "a newer push cancels the run again": (
        SECURITY_YML,
        after(
            "permissions:\n  contents: read\n",
            '"concurrenc\\x79":\n  group: ${{ github.ref }}\n  cancel-in-progress: true\n',
        ),
    ),
}


@pytest.mark.parametrize("label", QUOTED_KEY_MUTATIONS)
def test_the_candidate_tests_fail_when_a_quoted_key_weakens_the_workflow(
    checks, monkeypatch, label
):
    """test_security_checks.py: "the workflow's security-relevant run scripts are compared
    exactly" (resolution, RC-04/RC-05). A quoted YAML key ('if', 'permissions', 'defaults') is
    the same key to GitHub, but the text helpers match only bare keys and drop the line."""
    path, mutate = QUOTED_KEY_MUTATIONS[label]
    original = read(path)
    mutated = mutate(original)
    assert mutated != original
    assert candidate_failures(checks, monkeypatch, mutated, path), (
        f"no candidate test fails: {label}"
    )


def test_control_quoted_keys_parse_as_the_bare_keys():
    """YAML semantics, checked with Ruby's YAML parser: each quoted key is the plain key."""
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("ruby is not installed")
    text = "'if': false\n'permissions': write-all\n'defaults':\n  run:\n    shell: bash {0}\n"
    text += '"concurrenc\\x79": {cancel-in-progress: true}\n'
    parsed = subprocess.run(
        [ruby, "-ryaml", "-rjson", "-e", "puts JSON.generate(YAML.load(STDIN.read))"],
        input=text,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert json.loads(parsed) == {
        "if": False,
        "permissions": "write-all",
        "defaults": {"run": {"shell": "bash {0}"}},
        "concurrency": {"cancel-in-progress": True},
    }


# Adapted: the coverage step now writes the output with `&& echo "complete=true" >> ...`, so a
# shell without -e (a `defaults: run: shell: bash {0}`) no longer marks the SBOM complete after a
# failed check: the output is absent for both shells. The reviewer's control expected it for bash.
@pytest.mark.parametrize("shell", ["bash -e", "bash"])
def test_control_a_shell_without_e_cannot_mark_the_sbom_complete_after_a_failed_coverage_check(
    tmp_path, shell
):
    """The coverage step's script, run as GitHub runs it by default (bash -e) and as a
    `defaults: run: shell: bash {0}` would run it, on an SBOM that lists nothing."""
    (tmp_path / "tools").symlink_to(ROOT / "tools")
    (tmp_path / "sbom").mkdir()
    (tmp_path / "sbom/lab-ops.cdx.json").write_text(json.dumps(cyclonedx()))
    (tmp_path / "image-contents.json").write_text("{}")
    script = tmp_path / "coverage.sh"
    script.write_text(step_script("Check that the SBOM lists all of it"))
    output = tmp_path / "github-output"
    output.touch()
    subprocess.run(
        [*shell.split(), str(script)],
        cwd=tmp_path,
        env={**os.environ, "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(output)},
        capture_output=True,
    )
    complete = "complete=true" in output.read_text()
    assert not complete, (shell, output.read_text())


QUOTED_SCOPES = {
    "control: bare scope": "    permissions:\n      contents: write\n",
    "block style": "    permissions:\n      'contents': write\n",
    "flow style": '    permissions: { "contents": write }\n',
}


@pytest.mark.parametrize("label", QUOTED_SCOPES)
def test_write_grants_sees_a_quoted_scope(checks, monkeypatch, label):
    """test_security_checks.py: write_grants() finds every write grant, block or flow style
    (resolution, RC-05), and only the intended jobs may write. In checks.yml, which only
    write_grants() guards, a quoted scope is not seen."""
    texts = checks.workflow_texts()
    anchor = "    runs-on: ubuntu-latest\n"
    assert anchor in texts["checks.yml"]
    mutated = texts["checks.yml"].replace(anchor, anchor + QUOTED_SCOPES[label], 1)
    monkeypatch.setattr(checks, "workflow_texts", lambda: {**texts, "checks.yml": mutated})
    with pytest.raises(AssertionError):
        checks.test_write_permissions_appear_only_where_intended()


# ====================== V-05 action pins in layouts and files the pin checks never read

PIN_LAYOUTS = {
    "the value on the next line": "      - uses:\n          evil/action@main\n",
    "a flow-style step": "      - {uses: evil/action@main}\n",
    "a quoted key": "      - 'uses': evil/action@main\n",
}


def workflow(steps):
    return (
        "name: w\non: push\npermissions:\n  contents: read\njobs:\n  a:\n"
        f"    runs-on: ubuntu-latest\n    steps:\n{steps}"
    )


@pytest.mark.parametrize("label", PIN_LAYOUTS)
def test_every_layout_of_uses_is_pin_checked(checks, label):
    """test_security_checks.py: every action is pinned to a commit with a version comment;
    check_action_pins.py: each pin is the release its comment names. The same `uses:` written
    in another valid layout reaches neither check (an action at a movable branch runs)."""
    text = workflow(PIN_LAYOUTS[label])
    assert checks.unpinned_uses(workflow("      - uses: evil/action@main\n")), "control"
    found, unreadable = action_pins.pins(text, "w.yml")
    assert checks.unpinned_uses(text) or found or unreadable, f"{label}: no pin check sees it"


# Adapted: check_action_pins.workflow_files() binds its default root when the module loads, so the
# copy is passed to it explicitly; with that, the script (not only the candidate's tests) must
# report the .yaml workflow, which the reviewer's assertion (`failures or pins_exit`) allowed
# either of.
def test_a_workflow_with_the_yaml_extension_is_checked(checks, monkeypatch, tmp_path):
    """GitHub runs workflows named *.yml and *.yaml. The candidate's workflow tests and
    check_action_pins.py read only *.yml, so a *.yaml workflow that grants write-all and runs an
    unpinned action passes every one of them."""
    copy = tmp_path / "repo"
    shutil.copytree(ROOT / ".github/workflows", copy / ".github/workflows")
    (copy / ".github/workflows/extra.yaml").write_text(
        "name: extra\non: pull_request_target\npermissions: write-all\njobs:\n  a:\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - uses: evil/action@main\n"
    )
    monkeypatch.setattr(checks, "ROOT", copy)
    monkeypatch.setattr(action_pins, "ROOT", copy)
    listing = action_pins.workflow_files
    monkeypatch.setattr(action_pins, "workflow_files", lambda root=copy: listing(root))
    monkeypatch.setattr(action_pins, "github", lambda path, token=None: pytest.fail(path))
    failures = []
    for name in (
        "test_every_action_is_pinned_to_a_commit_sha_with_a_version_comment",
        "test_every_checkout_drops_the_token",
        "test_every_workflow_defaults_to_read_only_contents",
        "test_write_permissions_appear_only_where_intended",
    ):
        try:
            getattr(checks, name)()
        except AssertionError:
            failures.append(name)
    pinned = [
        pin
        for path in sorted((copy / ".github/workflows").iterdir())
        for pin in action_pins.pins(path.read_text(), path.name)[0]
    ]
    monkeypatch.setattr(action_pins, "problems", lambda pinned, resolve: [])
    pins_exit = action_pins.main()
    assert pinned, "control: the .yml workflows hold pins"
    assert failures, "a .yaml workflow with write-all and an unpinned action passes the tests"
    assert pins_exit, "a .yaml workflow with an unpinned action passes the pin script"


# ===================================== V-06 the dependency listing may leave out the actions


def dependency_source(tmp_path, uses, syft_config=None):
    """A copy of the inputs the dependency job lists, with one workflow step added."""
    source = tmp_path / "src"
    for relative in ("uv.lock", "pyproject.toml", "static/vendor/package-lock.json"):
        (source / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / relative, source / relative)
    shutil.copytree(ROOT / ".github/workflows", source / ".github/workflows")
    (source / ".github/workflows/added.yml").write_text(workflow(f"      - uses: {uses}\n"))
    config = tmp_path / "syft.yaml"
    config.write_text(syft_config or read(".syft.yaml"))
    return source, config


@pytest.mark.parametrize("label", ["shipped .syft.yaml", ".syft.yaml excludes .github"])
def test_the_dependency_audit_cannot_pass_a_vulnerable_action(checks, tmp_path, label):
    """SECURITY.md: the dependency audit covers "the actions the workflows use", and the listing
    is compared with what it must contain first. sbom_coverage.py --locked requires the uv.lock
    and vendored packages only, and no test reads .syft.yaml (test_security_checks.py says it
    pins it). The pinned action's comment is right, so check_action_pins.py passes too."""
    extra = '  - "./.github/**"\n' if "excludes" in label else ""
    syft_config = read(".syft.yaml") + (f"exclude:\n{extra}" if extra else "")
    source, config = dependency_source(tmp_path, VULNERABLE_ACTION, syft_config)
    listing = tmp_path / "dependencies.cdx.json"
    sbom = syft_listing(source, config, listing)
    # Adapted: check_locked() also takes the workflows' texts and now requires every pinned action.
    workflows = {path.name: path.read_text() for path in action_pins.workflow_files(source)}
    problems, _ = coverage.check_locked(
        sbom,
        tomllib.loads(read("uv.lock")),
        json.loads(read("static/vendor/package-lock.json")),
        workflows,
    )
    if problems:
        return  # the coverage check stops the job before the audit
    code, output = run_gate("dependencies", listing)
    assert code == 2 and "GHSA-cxww-7g56-2vh6" in output, (
        f"{label}: the listing passes coverage and the audit passes {VULNERABLE_ACTION}"
    )


# ============================================= V-07 sub-path actions are never matched


def test_the_dependency_audit_matches_advisories_for_sub_path_actions(tmp_path):
    """SECURITY.md: "Any published vulnerability" in the actions the workflows use fails the
    audit. Syft names a sub-path action owner/repo/path (github/codeql-action/init,
    anchore/sbom-action/download-syft), and Grype matches advisories by owner/repo, so
    github/codeql-action v3.28.0 (GHSA-vqf5-2xx6-9wfm, High) passes. The canary's action,
    actions/download-artifact, has no sub-path."""
    source, config = dependency_source(tmp_path, VULNERABLE_SUBPATH_ACTION)
    listing = tmp_path / "dependencies.cdx.json"
    sbom = syft_listing(source, config, listing)
    names = {c["name"] for c in sbom["components"] if c.get("purl", "").startswith("pkg:github/")}
    assert "github/codeql-action/init" in names
    code, output = run_gate("dependencies", listing)
    assert code == 2 and "GHSA-vqf5-2xx6-9wfm" in output, (
        f"the audit passes {VULNERABLE_SUBPATH_ACTION} (exit {code})"
    )


def test_control_the_dependency_audit_matches_a_top_level_action(tmp_path):
    source, config = dependency_source(tmp_path, VULNERABLE_ACTION)
    listing = tmp_path / "dependencies.cdx.json"
    syft_listing(source, config, listing)
    code, output = run_gate("dependencies", listing)
    assert code == 2 and "GHSA-cxww-7g56-2vh6" in output


# ================================================= V-08 what the image listing leaves out


# Adapted: the listing now has a `programs` key (the ELF files no Debian package installed), so the
# contents name the two files the Dockerfile copies in, the expected count includes them, and the
# listing without the key is reported as incomplete.
def test_the_image_listing_requires_the_binaries_the_dockerfile_copies_in():
    """SECURITY.md: the image's SBOM is compared with "everything the image itself reports".
    The Dockerfile copies uv and uvx into /bin (Syft lists them through their embedded Cargo
    data: 1,026 cargo components in CI's SBOM); image_contents.py reads dpkg, site-packages and
    os-release only, so an SBOM without them passes."""
    copied = re.findall(r"^COPY --from=uv (.+) /bin/$", read("Dockerfile"), re.M)
    assert copied, "control: the Dockerfile copies uv into the image"
    contents = {
        "os": {"id": "debian", "version_id": "12"},
        "python": "3.14.10",
        "deb": {"libc6": ["2.36-9+deb12u13"]},
        "pypi": {"django": ["5.2.19"]},
        "programs": [f"/bin/{name.lstrip('/')}" for name in copied[0].split()],
    }
    pyproject = {"project": {"dependencies": ["django"]}}
    lock = {"packages": {}}
    sbom = cyclonedx(
        {"type": "operating-system", "name": "debian", "version": "12.15"},
        {"purl": "pkg:deb/debian/libc6@2.36-9%2Bdeb12u13?arch=amd64"},
        {"purl": "pkg:generic/python@3.14.10"},
        {"purl": "pkg:pypi/django@5.2.19"},
    )
    problems, expected = coverage.check_image(sbom, contents, pyproject, lock)
    assert expected == 4 + len(copied[0].split()), "control: every entry of the listing is compared"
    assert any("uv" in line for line in problems), (
        f"an SBOM without {copied[0].split()} passes: the listing has no entry for them"
    )
    without_programs = {key: value for key, value in contents.items() if key != "programs"}
    incomplete, _ = coverage.check_image(sbom, without_programs, pyproject, lock)
    assert incomplete and "incomplete" in incomplete[0]


# ===================================================== V-09 loose files under static/vendor


def test_the_vendor_check_flags_a_file_outside_the_known_directories(checks, monkeypatch, tmp_path):
    """test_security_checks.py: a vendored package missing from the lockfile weakens a check
    without failing a build (round 1 RC-11). vendor_problems() lists directories only, so a
    library dropped straight into static/vendor/ is never compared with the lockfile."""
    vendor = tmp_path / "static/vendor"
    shutil.copytree(ROOT / "static/vendor", vendor)
    monkeypatch.setattr(checks, "ROOT", tmp_path)
    checks.test_vendored_files_are_the_locked_versions()  # control: the copy passes
    (vendor / "jquery-3.4.1.min.js").write_text("/*! jQuery v3.4.1 */\n")
    with pytest.raises(AssertionError):
        checks.test_vendored_files_are_the_locked_versions()


# ============================================= V-10 pushes that never reach a pull request


def test_a_commit_pushed_and_replaced_before_any_pull_request_is_scanned(checks):
    """security.yml: "a newer push never cancels a run: the run of a push that carried a secret
    still reports it after the secret is pushed away" (resolution, RC-15). Only a push to a
    branch with an open pull request starts a run: security.yml has no push trigger (to keep an
    advisory from blocking a deploy, a reason that does not apply to the secret scan), so a
    secret pushed to a branch, or straight to main, and force-pushed away before a pull request
    or the weekly run reaches it is never scanned. No other workflow runs gitleaks."""
    texts = checks.workflow_texts()
    on_push = [
        name
        for name, text in texts.items()
        if "push" in checks.triggers(text) and "gitleaks" in text
    ]
    # Adapted: the claim moved with the secret scan, to secrets.yml's header comment (which wraps
    # across lines, so the comment markers and line breaks are removed before looking).
    header = " ".join(texts["secrets.yml"].replace("#", " ").split())
    assert "pushed and then replaced" in header
    assert on_push, "no workflow scans a push for secrets; only pull request pushes are scanned"


# ============================================================ sound: checked and held


def test_sound_grype_reads_no_configuration_from_its_working_directory(tmp_path):
    """grype_gate.py runs the canary in a scratch directory and the real scan in the checkout.
    With --config given, Grype does not also load .grype.yaml or .grype/config.yaml from the
    working directory, so the two scans use the same settings."""
    sbom = tmp_path / "pillow.cdx.json"
    sbom.write_text(json.dumps(cyclonedx(pypi("pillow", "10.1.0"))))
    for relative in (".grype.yaml", ".grype/config.yaml"):
        cwd = tmp_path / relative.replace("/", "-")
        (cwd / relative).parent.mkdir(parents=True)
        (cwd / relative).write_text("ignore:\n  - vulnerability: GHSA-3f63-hfp8-52jq\n")
        report = run_grype(sbom, ROOT / ".grype.yaml", cwd)
        assert "GHSA-3f63-hfp8-52jq" in ids(report, "matches"), relative


def test_sound_the_canaries_pass_on_the_repository_settings(tmp_path):
    """Both Grype canaries report what they must with the shipped .grype.yaml and database, and
    the gitleaks canary passes under bash -e (its `tr | head` pipeline does not stop the step:
    GitHub's default shell has no pipefail)."""
    clean = tmp_path / "clean.cdx.json"
    clean.write_text(json.dumps(cyclonedx(pypi("review-placeholder", "1.0.0"))))
    for name in gate.GATES:
        code, output = run_gate(name, clean)
        assert code == 0 and "cannot be trusted" not in output, (name, output)
    repo = new_repo(tmp_path / "repo")
    # Adapted: the job's four steps (see SECRET_STEPS).
    assert secret_job(repo, tmp_path) == {"guard": 0, "canary": 0, "history": 0, "stream": 0}


def test_sound_the_pin_check_fails_closed_on_errors_it_does_not_name():
    """A timeout or reset connection is not caught by problems(), so the script stops with a
    traceback (exit 1) instead of passing."""

    def timeout(repo, tag):
        raise TimeoutError("read timed out")

    with pytest.raises(TimeoutError):
        action_pins.problems([("w.yml:1", "actions/checkout", "0" * 40, "v1.0.0")], timeout)
