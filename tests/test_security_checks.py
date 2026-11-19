"""The release security checks only protect while their configuration stays tight: a workflow
step pinned to a movable tag, a token that gained write access, a gitleaks allowlist that excuses a
whole file, or a vendored package missing from the lockfile each weaken a check without failing
any build. These tests pin the settings the checks rely on (.github/workflows, .gitleaks.toml,
.grype.yaml, .syft.yaml, static/vendor/package-lock.json, .github/dependabot.yml).

No YAML parser is installed, so the workflow helpers read the files as text. They rely on the
layout the repository's workflows use (two-space indentation, block style keys) and are pure
(text in, problems out) so the negative tests can feed them bad snippets.
"""

import json
import re
import tomllib
from pathlib import Path

from django.conf import settings

ROOT = Path(settings.BASE_DIR)
WORKFLOWS = ".github/workflows"

# ---------------------------------------------------------------- text helpers


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _code(line):
    """The line without a trailing ` # comment` (comment text never holds settings)."""
    return re.sub(r"\s+#.*$", "", line).rstrip()


def _meaningful(line):
    return bool(line.strip()) and not line.lstrip().startswith("#")


def _value(raw):
    return _code(raw).strip().strip("\"'")


def top_level_block(text, key):
    """(inline value, body lines) of the column-0 `key:`, or None when absent. The body runs to
    the next column-0 line that is not a comment."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = re.match(rf"^{re.escape(key)}:(.*)$", line)
        if not match:
            continue
        body = []
        for nxt in lines[i + 1 :]:
            if nxt.strip() and not nxt.startswith((" ", "#")):
                break
            body.append(nxt)
        return _value(match.group(1)), body
    return None


def child_keys(lines, indent):
    """Map each `key:` at exactly `indent` spaces to the lines nested under it."""
    keys, current = {}, None
    for line in lines:
        if not _meaningful(line):
            continue
        match = re.match(rf"^ {{{indent}}}([\w.-]+):(.*)$", line)
        if match:
            current = match.group(1)
            keys[current] = [line] if _value(match.group(2)) else []
        elif current is not None and _indent(line) > indent:
            keys[current].append(line)
    return keys


def triggers(text):
    """Map each trigger under `on:` to the lines nested under it."""
    block = top_level_block(text, "on")
    return child_keys(block[1], 2) if block else {}


def jobs(text):
    """Map each job id to the lines of its block."""
    block = top_level_block(text, "jobs")
    return child_keys(block[1], 2) if block else {}


def split_steps(job_lines):
    """The steps of a job as lists of lines (a step starts at a `- ` under `steps:`)."""
    steps, start_indent, in_steps = [], None, False
    for line in job_lines:
        if not _meaningful(line):
            continue
        if re.match(r"^\s+steps:\s*$", line):
            in_steps = True
            continue
        if not in_steps:
            continue
        if start_indent is None and line.lstrip().startswith("- "):
            start_indent = _indent(line)
        if start_indent is not None and _indent(line) == start_indent and "- " in line:
            steps.append([line])
        elif steps:
            steps[-1].append(line)
    return steps


def step_uses(step):
    for line in step:
        match = re.match(r"^\s*(?:-\s+)?uses:\s*([^\s@]+)", line)
        if match:
            return match.group(1)
    return None


def step_with(step):
    """The `with:` inputs of a step, first level only (a literal block value is not parsed)."""
    values, with_indent = {}, None
    for line in step:
        match = re.match(r"^(\s*)with:\s*$", line)
        if match:
            with_indent = len(match.group(1))
            continue
        if with_indent is None:
            continue
        if _indent(line) <= with_indent:
            break
        match = re.match(rf"^ {{{with_indent + 2}}}([\w-]+):(.*)$", line)
        if match:
            values[match.group(1)] = _value(match.group(2))
    return values


def workflow_steps(text):
    """(job id, action name, with-inputs, step lines) for every step that uses an action."""
    found = []
    for job_id, lines in jobs(text).items():
        for step in split_steps(lines):
            action = step_uses(step)
            if action:
                found.append((job_id, action, step_with(step), step))
    return found


# ------------------------------------------------------ checks on workflow text

USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
PINNED_REF = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
VERSION_COMMENT = re.compile(r"^\s+#\s*v\d[\w.+-]*\s*$")


def unpinned_uses(text):
    """(line number, message) for each `uses:` not pinned to a full commit SHA with a `# v...`
    comment. Local actions (`./...`) are exempt."""
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        match = USES.match(line)
        if not match or match["ref"].startswith("./"):
            continue
        if not PINNED_REF.match(match["ref"]):
            problems.append((number, f"{match['ref']} is not pinned to a 40-hex commit SHA"))
        elif not VERSION_COMMENT.match(match["rest"]):
            problems.append((number, f"{match['ref']} lacks a trailing '# v...' version comment"))
    return problems


def checkouts_keeping_credentials(text):
    """Line numbers of `actions/checkout` steps that do not set `persist-credentials: false`."""
    problems = []
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        if not re.match(r"^\s*(?:-\s+)?uses:\s*actions/checkout@", line):
            continue
        # The step starts at this line when it is `- uses:`, else at the `- ` line above it.
        first = number - 1
        if not line.lstrip().startswith("- "):
            while first > 0 and not lines[first].lstrip().startswith("- "):
                first -= 1
        dash_indent = _indent(lines[first])
        step = [lines[first]]
        for nxt in lines[first + 1 :]:
            if _meaningful(nxt) and _indent(nxt) <= dash_indent:
                break
            step.append(nxt)
        if not any(re.match(r"^\s+persist-credentials:\s*false\s*$", _code(s)) for s in step):
            problems.append(number)
    return problems


def top_level_permissions(text):
    """The workflow's top-level `permissions:` as a dict, None when absent. An inline value
    such as `write-all` comes back as {"*": value}."""
    block = top_level_block(text, "permissions")
    if block is None:
        return None
    inline, body = block
    if inline:
        return {"*": inline}
    found = {}
    for line in body:
        match = re.match(r"^\s+([\w-]+):\s*(\S+)", _code(line)) if _meaningful(line) else None
        if match:
            found[match.group(1)] = match.group(2)
    return found


def permission_problems(text):
    permissions = top_level_permissions(text)
    if permissions is None:
        return ["no top-level 'permissions:' block (the default token is broader)"]
    if permissions != {"contents": "read"}:
        return [f"top-level permissions are {permissions}, expected only contents: read"]
    return []


def write_grants(text):
    """(line number, scope, job id or None) for every write grant in any `permissions:` block."""
    grants, job, perm_indent, in_jobs = [], None, None, False
    for number, line in enumerate(text.splitlines(), 1):
        if not _meaningful(line):
            continue
        if re.match(r"^jobs:", line):
            in_jobs = True
        if in_jobs and re.match(r"^  [\w-]+:\s*$", line):
            job = line.strip().rstrip(":")
        if perm_indent is not None and _indent(line) <= perm_indent:
            perm_indent = None
        match = re.match(r"^(\s*)permissions:\s*(.*)$", _code(line))
        if match:
            perm_indent = len(match.group(1))
            if match.group(2).strip() == "write-all":
                grants.append((number, "*", job))
            continue
        if perm_indent is not None:
            grant = re.match(r"^\s+([\w-]+):\s*write\b", _code(line))
            if grant:
                grants.append((number, grant.group(1), job))
    return grants


# Where each write scope may appear: (workflow file, job id).
INTENDED_WRITES = {
    "security-events": ("codeql.yml", "analyze"),
    "contents": ("security.yml", "release-sbom"),
    "pages": ("pages.yml", "deploy"),
    "id-token": ("pages.yml", "deploy"),
}


def unintended_writes(name, text):
    problems = []
    for number, scope, job in write_grants(text):
        if INTENDED_WRITES.get(scope) != (name, job):
            problems.append(f"{name}:{number} grants '{scope}: write' (job {job or 'top level'})")
    return problems


# ------------------------------------------------------------- gitleaks config


def allowlist_problems(config):
    """Problems in the parsed .gitleaks.toml allowlists (the file's own comments say why)."""
    problems = []
    if not config.get("extend", {}).get("useDefault"):
        problems.append("[extend] useDefault is not true: the default rules would not apply")
    lists = list(config.get("allowlists", []))
    if "allowlist" in config:
        lists.append(config["allowlist"])
    for index, allowlist in enumerate(lists, 1):
        label = f"allowlist {index}"
        if not allowlist.get("description"):
            problems.append(f"{label} has no description")
        if "paths" in allowlist:
            problems.append(
                f"{label} has paths: gitleaks 8.30.1 ignores condition = AND, so a path "
                "excuses every secret in the file"
            )
        if allowlist.get("regexTarget") != "secret":
            problems.append(f"{label} regexTarget is not 'secret'")
        regexes = allowlist.get("regexes", [])
        if not regexes:
            problems.append(f"{label} has no regexes")
        for regex in regexes:
            if not (regex.startswith("^") and regex.endswith("$") and not regex.endswith("\\$")):
                problems.append(f"{label} regex {regex!r} is not anchored as ^...$")
    return problems


def allowlisted_values(config):
    """The exact value each anchored allowlist regex stands for (anchors stripped, unescaped)."""
    values = []
    for allowlist in config.get("allowlists", []):
        for regex in allowlist.get("regexes", []):
            if regex.startswith("^") and regex.endswith("$"):
                values.append(re.sub(r"\\(.)", r"\1", regex[1:-1]))
    return values


# ------------------------------------------------------------------ grype config


def grype_problems(text):
    """Problems in the `ignore:` entries of .grype.yaml."""
    problems = []
    lines = [line for line in text.splitlines() if _meaningful(line)]
    entries, current = [], None
    for line in lines:
        if re.match(r"^  - ", line):
            current = [line]
            entries.append(current)
        elif current is not None and _indent(line) > 2:
            current.append(line)
    for entry in entries:
        body = "\n".join(entry)
        label = _code(entry[0]).strip()
        if not re.match(r"^- vulnerability:\s*\S+", label):
            problems.append(f"{label}: entry does not start with a vulnerability id")
        for key in ("name", "version"):
            if not re.search(rf"^\s+{key}:[ \t]*\S+", body, re.M):
                problems.append(f"{label}: package {key} missing")
        reason = re.search(r"^\s+reason:[ \t]*(.*)$", body, re.M)
        folded = re.search(r"^\s+reason:[ \t]*[>|][+-]?\s*\n\s+\S", body, re.M)
        if not reason or not (reason.group(1).strip() not in ("", ">-", ">", "|", "|-") or folded):
            problems.append(f"{label}: reason missing or empty")
    return problems


# --------------------------------------------------------- vendored npm lockfile


def readme_packages(text):
    """{package: (version, integrity)} from the vendor README table; integrity as npm writes it."""
    found = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not line.startswith("| ") or len(cells) != 5 or set(cells[0]) <= set("-"):
            continue
        version = re.match(r"\d[\w.+-]*", cells[1])
        sha = cells[4].strip("`")
        if cells[0] != "Package" and version:
            found[cells[0]] = (version.group(0), "sha512-" + sha)
    return found


def lockfile_problems(lock, readme):
    problems = []
    if "name" in lock or "name" in lock.get("packages", {}).get("", {}):
        problems.append("the lockfile names a root package, which could collide with an npm name")
    locked = {
        key.removeprefix("node_modules/"): entry
        for key, entry in lock.get("packages", {}).items()
        if key
    }
    documented = readme_packages(readme)
    for name in sorted(set(documented) - set(locked)):
        problems.append(f"{name} is in the README table but not in the lockfile")
    for name in sorted(set(locked) - set(documented) - {"bootstrap-icons"}):
        problems.append(f"{name} is in the lockfile but not in the README table")
    for name, (version, integrity) in documented.items():
        entry = locked.get(name)
        if not entry:
            continue
        if entry.get("version") != version:
            problems.append(f"{name}: lockfile {entry.get('version')} != README {version}")
        if entry.get("integrity") != integrity:
            problems.append(f"{name}: lockfile integrity differs from the README sha512")
    for name, entry in locked.items():
        base = name.rsplit("/", 1)[-1]
        expected = f"https://registry.npmjs.org/{name}/-/{base}-{entry.get('version')}.tgz"
        if entry.get("resolved") != expected:
            problems.append(f"{name}: resolved is {entry.get('resolved')!r}, expected {expected}")
    icons = locked.get("bootstrap-icons")
    if not icons or icons.get("version") != "1.13.2":
        problems.append("bootstrap-icons 1.13.2 is not recorded in the lockfile")
    return problems


# ----------------------------------------------------------------- file loaders


def workflow_texts():
    texts = {path.name: path.read_text() for path in sorted((ROOT / WORKFLOWS).glob("*.yml"))}
    assert texts, f"no workflows found in {WORKFLOWS}"
    return texts


def read(relative):
    return (ROOT / relative).read_text()


# ------------------------------------------------------------ tests: all workflows


def test_every_action_is_pinned_to_a_commit_sha_with_a_version_comment():
    problems = [
        f"{WORKFLOWS}/{name}:{number}: {message}"
        for name, text in workflow_texts().items()
        for number, message in unpinned_uses(text)
    ]
    assert not problems, "Pin each action to a full commit SHA plus '# vX.Y.Z':\n" + "\n".join(
        problems
    )


def test_every_checkout_drops_the_token():
    problems = [
        f"{WORKFLOWS}/{name}:{number}: actions/checkout without 'persist-credentials: false'"
        for name, text in workflow_texts().items()
        for number in checkouts_keeping_credentials(text)
    ]
    assert not problems, "\n".join(problems)


def test_every_workflow_defaults_to_read_only_contents():
    problems = [
        f"{WORKFLOWS}/{name}: {problem}"
        for name, text in workflow_texts().items()
        for problem in permission_problems(text)
    ]
    assert not problems, "\n".join(problems)


def test_write_permissions_appear_only_where_intended():
    texts = workflow_texts()
    problems = [
        f"{WORKFLOWS}/{problem}"
        for name, text in texts.items()
        for problem in unintended_writes(name, text)
    ]
    assert not problems, "\n".join(problems)
    # The intended grants exist (a missing one would silently break the job that needs it).
    assert {s for _, s, _ in write_grants(texts["codeql.yml"])} == {"security-events"}
    assert {(s, j) for _, s, j in write_grants(texts["security.yml"])} == {
        ("contents", "release-sbom")
    }


def test_release_sbom_job_runs_only_on_release_and_checks_out_nothing():
    lines = jobs(read(f"{WORKFLOWS}/security.yml")).get("release-sbom")
    assert lines, "security.yml has no release-sbom job"
    body = "\n".join(lines)
    condition = [line for line in lines if re.match(r"^\s+if:", line)]
    assert condition, "release-sbom has no 'if:' condition"
    assert "github.event_name == 'release'" in condition[0], condition[0]
    assert "actions/checkout" not in body, "release-sbom must not check out repository code"
    assert "actions/download-artifact" in body
    assert "gh release upload" in body


# ----------------------------------------------------------- tests: security.yml


def test_security_workflow_triggers():
    on = triggers(read(f"{WORKFLOWS}/security.yml"))
    assert set(on) == {"pull_request", "schedule", "workflow_dispatch", "release"}, sorted(on)
    assert "push" not in on, "a push trigger would let a new advisory block the demo deploy"
    assert re.search(r"types:\s*\[\s*published\s*\]", "\n".join(on["release"])), on["release"]
    assert any("cron:" in line for line in on["schedule"]), "schedule has no cron"


def test_dependency_job_audits_everything_without_only_fixed():
    text = read(f"{WORKFLOWS}/security.yml")
    steps = [s for s in workflow_steps(text) if s[0] == "dependencies"]
    listings = [s[2] for s in steps if s[1] == "anchore/sbom-action"]
    assert len(listings) == 1 and listings[0].get("path") == ".", listings
    scans = [s[2] for s in steps if "scan-action" in s[1]]
    assert len(scans) == 1, f"expected one Grype step in 'dependencies', found {len(scans)}"
    inputs = scans[0]
    assert inputs.get("sbom") == listings[0].get("output-file"), "Grype must scan that listing"
    assert inputs.get("severity-cutoff") == "low", inputs
    assert inputs.get("only-fixed", "false") != "true", "only-fixed would hide unfixed findings"
    # A scanner that cannot read uv.lock reports nothing, so coverage is checked before the scan.
    body = "\n".join(jobs(text)["dependencies"])
    assert re.search(r"sbom_coverage\.py \S*dependencies\.cdx\.json\S* --locked", body), body
    assert body.index("sbom_coverage.py") < body.index("anchore/scan-action")


def test_image_job_builds_both_sboms_and_gates_on_fixed_high_findings():
    text = read(f"{WORKFLOWS}/security.yml")
    image = [s for s in workflow_steps(text) if s[0] == "image"]
    sboms = [s[2] for s in image if s[1] == "anchore/sbom-action"]
    assert sorted(s.get("format") for s in sboms) == ["cyclonedx-json@1.6", "spdx-json"], sboms
    # Without .syft.yaml an image scan skips static/vendor/package-lock.json.
    assert all(s.get("config") == ".syft.yaml" for s in sboms), sboms
    assert "+javascript-lock-cataloger" in read(".syft.yaml")
    uploads = [s[2] for s in image if s[1] == "actions/upload-artifact"]
    assert [u.get("name") for u in uploads] == ["sbom"], uploads
    body = "\n".join(jobs(text)["image"])
    assert "sbom_coverage.py sbom/lab-ops.cdx.json --runtime" in body, "image SBOM coverage"
    assert body.index("sbom_coverage.py") < body.index("anchore/scan-action")
    gates = [s[2] for s in image if s[1] == "anchore/scan-action"]
    assert len(gates) == 1, f"expected one Grype gate in 'image', found {len(gates)}"
    assert gates[0].get("sbom"), "the gate must scan the SBOM"
    assert gates[0].get("severity-cutoff") == "high", gates[0]
    assert gates[0].get("only-fixed") == "true", gates[0]


def test_every_grype_scan_uses_the_repository_config():
    scans = [s for s in workflow_steps(read(f"{WORKFLOWS}/security.yml")) if "scan-action" in s[1]]
    assert len(scans) == 2, f"expected two Grype scans, found {len(scans)}"
    for job, action, inputs, _ in scans:
        assert inputs.get("config") == ".grype.yaml", f"{job}: {action} ignores .grype.yaml"


def test_secrets_job_pins_and_verifies_gitleaks_and_scans_full_history():
    text = read(f"{WORKFLOWS}/security.yml")
    lines = jobs(text).get("secrets")
    assert lines, "security.yml has no secrets job"
    body = "\n".join(lines)
    assert re.search(r"^\s+GITLEAKS_VERSION:\s*\d+\.\d+\.\d+\s*$", body, re.M), "version not pinned"
    assert re.search(r"^\s+GITLEAKS_SHA256:\s*[0-9a-f]{64}\s*$", body, re.M), "no 64-hex sha256"
    assert "sha256sum --check" in body, "the download is not verified"
    assert "--redact" in body, "findings would be printed in the log"
    assert "--config .gitleaks.toml" in body
    checkouts = [s for s in workflow_steps(text) if s[0] == "secrets" and "checkout" in s[1]]
    assert len(checkouts) == 1 and checkouts[0][2].get("fetch-depth") == "0", checkouts


# ------------------------------------------------------------ tests: codeql.yml


def test_codeql_workflow_triggers_and_configuration():
    text = read(f"{WORKFLOWS}/codeql.yml")
    on = triggers(text)
    assert set(on) == {"push", "pull_request", "schedule", "workflow_dispatch"}, sorted(on)
    assert re.search(r"branches:\s*\[\s*main\s*\]", "\n".join(on["push"])), on["push"]
    assert any("cron:" in line for line in on["schedule"]), "schedule has no cron"
    languages = re.search(r"^\s+language:\s*\[(.*?)\]", text, re.M)
    assert languages, "no language matrix"
    assert {v.strip() for v in languages.group(1).split(",")} == {"python", "actions"}
    init = [s for s in workflow_steps(text) if s[1] == "github/codeql-action/init"]
    assert len(init) == 1, "expected one codeql init step"
    assert init[0][2].get("queries") == "security-extended", init[0][2]
    assert init[0][2].get("languages") == "${{ matrix.language }}", init[0][2]
    assert any(s[1] == "github/codeql-action/analyze" for s in workflow_steps(text))


# ------------------------------------------------------- tests: gitleaks / grype


def gitleaks_config():
    return tomllib.loads(read(".gitleaks.toml"))


def test_gitleaks_allowlist_stays_exact():
    problems = allowlist_problems(gitleaks_config())
    assert not problems, ".gitleaks.toml:\n" + "\n".join(problems)


def test_every_allowlisted_gitleaks_value_still_occurs_in_a_test():
    values = allowlisted_values(gitleaks_config())
    assert values, ".gitleaks.toml allowlists no values"
    this_file = Path(__file__).resolve()
    sources = [
        path
        for pattern in ("tests/**/*.py", "*/tests/*.py")
        for path in ROOT.glob(pattern)
        if path.resolve() != this_file
    ]
    assert sources, "no test files found"
    corpus = "\n".join(path.read_text() for path in sources)
    stale = [value for value in values if value not in corpus]
    assert not stale, f"Remove from .gitleaks.toml (no test uses them any more): {stale}"


def test_grype_ignores_name_the_vulnerability_package_version_and_reason():
    problems = grype_problems(read(".grype.yaml"))
    assert not problems, ".grype.yaml:\n" + "\n".join(problems)


# ---------------------------------------------------- tests: lockfile, dependabot


def test_vendor_lockfile_agrees_with_the_readme_table():
    lock = json.loads(read("static/vendor/package-lock.json"))
    problems = lockfile_problems(lock, read("static/vendor/README.md"))
    assert not problems, "static/vendor/package-lock.json:\n" + "\n".join(problems)


def test_dependabot_keeps_the_pinned_actions_current():
    text = read(".github/dependabot.yml")
    entries = re.split(r"^  - (?=package-ecosystem:)", text, flags=re.M)[1:]
    actions = [e for e in entries if re.match(r"package-ecosystem:\s*github-actions\s*$", e, re.M)]
    assert actions, "dependabot.yml has no github-actions ecosystem"
    assert any(re.search(r"^\s+directory:\s*/\s*$", e, re.M) for e in actions), "directory is not /"


# ------------------------------------------------- tests: the helpers catch bad input

SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"


def test_helper_flags_tag_pinned_and_uncommented_uses():
    bad = (
        "steps:\n"
        "  - uses: actions/checkout@v4\n"
        f"  - uses: actions/setup-node@{SHA}\n"
        f"  - uses: ./local-action\n"
        f"  - uses: actions/cache@{SHA} # v4.0.0\n"
    )
    assert [number for number, _ in unpinned_uses(bad)] == [2, 3]


def test_helper_flags_checkout_without_persist_credentials():
    bad = (
        "    steps:\n"
        f"      - uses: actions/checkout@{SHA} # v7.0.1\n"
        "        with:\n"
        "          fetch-depth: 0\n"
        f"      - uses: actions/checkout@{SHA} # v7.0.1\n"
        "        with:\n"
        "          persist-credentials: false\n"
        "      - name: Use it\n"
        "        uses: actions/checkout@v7 # v7\n"
    )
    assert checkouts_keeping_credentials(bad) == [2, 9]


def test_helper_flags_top_level_write_permissions():
    assert permission_problems("permissions:\n  contents: write\n")
    assert permission_problems("permissions: write-all\n")
    assert permission_problems("on: push\n")
    assert permission_problems("permissions:\n  contents: read\n  packages: read\n")
    assert not permission_problems("permissions:\n  contents: read\n")
    bad = (
        "permissions:\n  contents: read\njobs:\n  a:\n"
        "    permissions:\n      security-events: write\n"
    )
    assert unintended_writes("security.yml", bad)
    assert unintended_writes("codeql.yml", "permissions:\n  contents: write\njobs:\n  a:\n")
    ok = "jobs:\n  analyze:\n    permissions:\n      security-events: write # upload\n"
    assert not unintended_writes("codeql.yml", ok)


def test_helper_flags_a_gitleaks_allowlist_with_paths():
    good = {
        "extend": {"useDefault": True},
        "allowlists": [{"description": "x", "regexTarget": "secret", "regexes": ["^abc$"]}],
    }
    assert not allowlist_problems(good)
    assert allowlisted_values(good) == ["abc"]
    bad = {
        "extend": {"useDefault": False},
        "allowlists": [
            {"paths": ["^tests/"], "condition": "AND", "regexes": ["abc"]},
        ],
    }
    text = "\n".join(allowlist_problems(bad))
    for expected in ("useDefault", "no description", "paths", "regexTarget", "not anchored"):
        assert expected in text, (expected, text)


def test_helper_flags_grype_entries_and_lockfile_drift():
    assert grype_problems("ignore:\n  - vulnerability: CVE-1\n    reason: x\n")
    good = (
        "ignore:\n  - vulnerability: CVE-1\n    package:\n      name: a\n      version: 1\n"
        "    reason: >-\n      Because.\n"
    )
    assert not grype_problems(good)
    readme = (
        "| Package | Version | Files | License | Tarball integrity (sha512) |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| a | 1.0.0 | f | MIT | `abc==` |\n"
    )
    lock = {"packages": {"": {}, "node_modules/a": {"version": "1.0.1", "integrity": "sha512-x"}}}
    text = "\n".join(lockfile_problems(lock, readme))
    for expected in ("README 1.0.0", "integrity differs", "resolved", "bootstrap-icons"):
        assert expected in text, (expected, text)
