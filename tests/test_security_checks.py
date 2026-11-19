"""The release security checks only protect while their configuration stays tight: a workflow
step pinned to a movable tag, a token that gained write access, a gitleaks allowlist that excuses a
whole file, or a vendored package missing from the lockfile each weaken a check without failing
any build. These tests pin the settings the checks rely on (.github/workflows, .gitleaks.toml,
.grype.yaml, .syft.yaml, static/vendor/package-lock.json, .github/dependabot.yml).

No YAML parser is installed, so the workflow helpers read the files as text. They rely on the
layout the repository's workflows use (two-space indentation, block style keys) and are pure
(text in, problems out) so the negative tests can feed them bad snippets.
"""

import importlib.util
import json
import re
import shutil
import tomllib
from pathlib import Path

import pytest
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
    """(line number, scope, job id or None) for every write grant in any `permissions:` block,
    block style or flow style (`permissions: { contents: write }`), and `write-all`."""
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
            inline = match.group(2).strip().strip("\"'")
            if inline == "write-all":
                grants.append((number, "*", job))
            for scope in re.findall(r"([\w-]+)\s*:\s*[\"']?write\b", inline):
                grants.append((number, scope, job))
            continue
        if perm_indent is not None:
            grant = re.match(r"^\s+([\w-]+):\s*[\"']?write\b", _code(line))
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

# Characters a regular expression reads as syntax; an allowlist regex may hold them only escaped.
REGEX_META = set(".^$*+?()[]{}|\\")


def regex_literal(regex):
    """The literal an anchored regex `^...$` matches, None when it is not `^`, then only literal
    characters (a metacharacter escaped with a backslash is literal), then `$`."""
    if not regex.startswith("^"):
        return None
    literal, i = [], 1
    while i < len(regex):
        char = regex[i]
        if char == "\\":
            if i + 1 >= len(regex) or regex[i + 1] not in REGEX_META:
                return None
            literal.append(regex[i + 1])
            i += 2
        elif char == "$":
            return "".join(literal) if i == len(regex) - 1 else None
        elif char in REGEX_META:
            return None
        else:
            literal.append(char)
            i += 1
    return None


def allowlist_problems(config):
    """Problems in the parsed .gitleaks.toml (the file's own comments say why): nothing but the
    default rules and allowlists of one exact value each."""
    problems = []
    if set(config) != {"extend", "allowlists"}:
        problems.append(f"top-level keys are {sorted(config)}, expected extend and allowlists")
    if config.get("extend") != {"useDefault": True}:
        problems.append("[extend] must be exactly useDefault = true: the default rules must apply")
    lists = config.get("allowlists")
    if not isinstance(lists, list) or not lists:
        return [*problems, "no [[allowlists]] entries"]
    for index, allowlist in enumerate(lists, 1):
        label = f"allowlist {index}"
        if not isinstance(allowlist, dict):
            problems.append(f"{label} is not a table")
            continue
        if set(allowlist) != {"description", "regexTarget", "regexes"}:
            problems.append(
                f"{label} keys are {sorted(allowlist)}, expected description, regexTarget and "
                "regexes (paths, commits, stopwords, condition and the like excuse more than "
                "one value; gitleaks 8.30.1 ignores condition = AND)"
            )
        if not allowlist.get("description"):
            problems.append(f"{label} has no description")
        if allowlist.get("regexTarget") != "secret":
            problems.append(f"{label} regexTarget is not 'secret'")
        regexes = allowlist.get("regexes")
        if not isinstance(regexes, list) or not regexes:
            problems.append(f"{label} has no regexes")
            continue
        for regex in regexes:
            if not isinstance(regex, str) or regex_literal(regex) in (None, ""):
                problems.append(
                    f"{label} regex {regex!r} is not ^ + one literal value + $ (an unescaped "
                    f"{''.join(sorted(REGEX_META))} matches more than that value)"
                )
    return problems


def allowlisted_values(config):
    """The exact value each allowlist regex stands for."""
    values = []
    for allowlist in config.get("allowlists", []):
        for regex in allowlist.get("regexes", []):
            literal = regex_literal(regex)
            if literal:
                values.append(literal)
    return values


# ------------------------------------------------------------------ grype config

GRYPE_ID = r"(?:CVE-\d{4}-\d+|GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4})"
GRYPE_ORDER = ["vulnerability", "package", "name", "version", "reason"]
GRYPE_LINES = [
    ("vulnerability", re.compile(rf"^  - vulnerability: {GRYPE_ID}$")),
    ("package", re.compile(r"^    package:$")),
    # Grype reads a name containing anything but these characters as a regular expression.
    ("name", re.compile(r"^      name: [A-Za-z0-9_-]+$")),
    ("version", re.compile(r"^      version: [^\s*]+$")),
    ("reason", re.compile(r"^    reason: >-$")),
    ("reason", re.compile(r"^    reason: (?![>|][+-]?$)\S.*$")),
]


def grype_problems(text):
    """Problems in .grype.yaml, which may hold only comments and `ignore:` entries of exactly
    this shape: a vulnerability id, the package's name and version, and a reason."""
    problems, seen_ignore, stage, in_block, block_lines = [], False, None, False, 0

    def finish(number):
        if stage is not None and stage < len(GRYPE_ORDER):
            problems.append(f"line {number}: the entry lacks {GRYPE_ORDER[stage]}")
        if in_block and not block_lines:
            problems.append(f"line {number}: the reason is empty")

    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        line = line.rstrip()
        if not line:
            continue
        if in_block and _indent(line) >= 6:
            block_lines += 1
            continue
        if line.lstrip().startswith("#"):
            continue
        if in_block and not block_lines:
            problems.append(f"line {number}: the reason is empty")
        in_block = False
        if line == "ignore:":
            if seen_ignore:
                problems.append(f"line {number}: a second ignore:")
            seen_ignore = True
            continue
        key = next((name for name, pattern in GRYPE_LINES if pattern.match(line)), None)
        if key is None or not seen_ignore:
            problems.append(f"line {number}: not an allowed .grype.yaml line: {line.strip()!r}")
            continue
        if key == "vulnerability":
            finish(number)
            stage = 1
        elif stage is None or GRYPE_ORDER[stage] != key:
            problems.append(f"line {number}: {key} is out of place in its entry")
            continue
        else:
            stage += 1
        if key == "reason":
            in_block, block_lines = line.endswith(">-"), 0
    finish(len(lines) + 1)
    return problems


# --------------------------------------------------------- vendored npm lockfile

INTEGRITY = re.compile(r"^sha512-[A-Za-z0-9+/]{86}==$")
LICENSE = re.compile(r"^[A-Za-z0-9.-]+$")

# What each directory under static/vendor holds, by npm package. A package whose files do not
# live here has a reason: bootstrap-icons is a subset kept in static/img/icons.svg.
VENDOR_DIRS = {
    "bootstrap-5.3.8": {"bootstrap"},
    "datatables": {
        "datatables.net",
        "datatables.net-bs5",
        "datatables.net-colreorder",
        "datatables.net-colreorder-bs5",
    },
    "htmx-2.0.11": {"htmx.org"},
    "overpass-5.3.0": {"@fontsource-variable/overpass"},
    "tom-select": {"tom-select"},
    "vanilla-calendar-pro-3.4.0": {"vanilla-calendar-pro"},
}
NOT_VENDORED = {"bootstrap-icons"}
# (file, package, banner the file carries for the locked version, bytes searched or None)
VENDOR_BANNERS = [
    ("bootstrap-5.3.8/css/bootstrap.min.css", "bootstrap", "v{version}", 400),
    ("bootstrap-5.3.8/js/bootstrap.bundle.min.js", "bootstrap", "v{version}", 400),
    ("htmx-2.0.11/htmx.min.js", "htmx.org", 'version:"{version}"', None),
    ("datatables/dataTables.min.js", "datatables.net", "DataTables {version}", None),
    (
        "datatables/dataTables.colReorder.min.js",
        "datatables.net-colreorder",
        "ColReorder {version}",
        None,
    ),
    ("tom-select/tom-select.complete.min.js", "tom-select", "Tom Select v{version}", None),
    ("vanilla-calendar-pro-3.4.0/index.js", "vanilla-calendar-pro", "v{version}", 400),
]


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
    """Problems in static/vendor/package-lock.json: its exact shape, and agreement with the
    README table."""
    problems = []
    if set(lock) != {"lockfileVersion", "requires", "packages"}:
        problems.append(f"top-level keys are {sorted(lock)} (a name could collide with an npm one)")
    if lock.get("lockfileVersion") != 3 or isinstance(lock.get("lockfileVersion"), bool):
        problems.append("lockfileVersion is not 3")
    if lock.get("requires") is not True:
        problems.append("requires is not true")
    packages = lock.get("packages", {})
    root = packages.get("", {})
    if set(root) != {"dependencies"}:
        problems.append(f"the root entry keys are {sorted(root)}, expected only dependencies")
    locked = {}
    for key, entry in packages.items():
        if not key:
            continue
        name = key.removeprefix("node_modules/")
        if key == name:
            problems.append(f"{key}: not under node_modules/")
        if set(entry) != {"version", "resolved", "integrity", "license"}:
            problems.append(
                f"{name}: keys are {sorted(entry)}, expected version, resolved, "
                "integrity and license"
            )
        locked[name] = entry
        if not INTEGRITY.match(str(entry.get("integrity"))):
            problems.append(f"{name}: integrity is not a sha512 digest")
        if not LICENSE.match(str(entry.get("license"))):
            problems.append(f"{name}: license {entry.get('license')!r} is not a plain identifier")
        base = name.rsplit("/", 1)[-1]
        expected = f"https://registry.npmjs.org/{name}/-/{base}-{entry.get('version')}.tgz"
        if entry.get("resolved") != expected:
            problems.append(f"{name}: resolved is {entry.get('resolved')!r}, expected {expected}")
    versions = {name: entry.get("version") for name, entry in locked.items()}
    if root.get("dependencies") != versions:
        problems.append("the root dependencies are not the locked name -> version map")
    documented = readme_packages(readme)
    for name in sorted(set(documented) - set(locked)):
        problems.append(f"{name} is in the README table but not in the lockfile")
    for name in sorted(set(locked) - set(documented) - NOT_VENDORED):
        problems.append(f"{name} is in the lockfile but not in the README table")
    for name, (version, integrity) in documented.items():
        entry = locked.get(name)
        if entry and entry.get("version") != version:
            problems.append(f"{name}: lockfile {entry.get('version')} != README {version}")
        if entry and entry.get("integrity") != integrity:
            problems.append(f"{name}: lockfile integrity differs from the README sha512")
    icons = locked.get("bootstrap-icons")
    if not icons or icons.get("version") != "1.13.2":
        problems.append("bootstrap-icons 1.13.2 is not recorded in the lockfile")
    return problems


def vendor_problems(lock, vendor):
    """Problems between the lockfile and the directories (a Path) of vendored files."""
    problems = []
    locked = {
        key.removeprefix("node_modules/"): entry["version"]
        for key, entry in lock.get("packages", {}).items()
        if key
    }
    present = {path.name for path in vendor.iterdir() if path.is_dir()}
    for name in sorted(present - set(VENDOR_DIRS)):
        problems.append(f"static/vendor/{name} is not a known vendored directory")
    for name in sorted(set(VENDOR_DIRS) - present):
        problems.append(f"static/vendor/{name} is missing")
    covered = set().union(*VENDOR_DIRS.values())
    for name in sorted(set(locked) - covered - NOT_VENDORED):
        problems.append(f"{name} is locked but no vendored directory holds it")
    for name in sorted(covered - set(locked)):
        problems.append(f"{name} is expected in a vendored directory but is not locked")
    for directory, packages in VENDOR_DIRS.items():
        match = re.search(r"-(\d+\.\d+\.\d+)$", directory)
        for package in sorted(packages):
            if match and locked.get(package) != match.group(1):
                problems.append(
                    f"{directory} names {match.group(1)}, lockfile has "
                    f"{package} {locked.get(package)}"
                )
    for relative, package, template, window in VENDOR_BANNERS:
        path = vendor / relative
        if not path.is_file():
            problems.append(f"static/vendor/{relative} is missing")
            continue
        data = path.read_bytes()
        text = (data[:window] if window else data).decode("utf-8", "replace")
        banner = template.format(version=locked.get(package))
        if not re.search(re.escape(banner) + r"(?!\d|\.\d)", text):
            where = f"in its first {window} bytes" if window else "in the file"
            problems.append(
                f"static/vendor/{relative}: {banner!r} not found {where}; the "
                f"lockfile has {package} {locked.get(package)}"
            )
    return problems


# ----------------------------------------------------------------- file loaders


def workflow_texts():
    texts = {path.name: path.read_text() for path in sorted((ROOT / WORKFLOWS).glob("*.yml"))}
    assert texts, f"no workflows found in {WORKFLOWS}"
    return texts


def read(relative):
    return (ROOT / relative).read_text()


def load_tool(name):
    spec = importlib.util.spec_from_file_location(
        f"security_{name}", ROOT / f"tools/security/{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------- the security workflow, compared exactly


def normalize_script(text):
    """A shell script with line continuations joined, whitespace collapsed and blank lines
    dropped, so that formatting changes do not matter and any change of a command does."""
    text = re.sub(r"\\[ \t]*\n", " ", text)
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def key_values(lines):
    """{key: value} of the `key: value` lines nested under a key (comments removed)."""
    found = {}
    for line in lines:
        match = re.match(r"^\s+([\w.-]+):\s*(.*)$", _code(line))
        if match:
            found[match.group(1)] = match.group(2).strip()
    return found


def step_summary(step):
    """Everything about a step that decides what it does, as a dict comparable with step()."""
    first = re.sub(r"^(\s*)- ", r"\1  ", step[0])
    fields = child_keys([first, *step[1:]], _indent(step[0]) + 2)
    run = None
    if "run" in fields:
        header = re.match(r"^\s*run:\s*(.*)$", fields["run"][0]).group(1).strip()
        if header in ("|", "|-", ">", ">-", "|+"):
            body = fields["run"][1:]
            cut = min(_indent(line) for line in body)
            run = normalize_script("\n".join(line[cut:] for line in body))
        else:
            run = normalize_script(header)

    def scalar(key):
        return _value(fields[key][0].partition(":")[2]) if key in fields else None

    return {
        "keys": list(fields),
        "name": scalar("name"),
        "id": scalar("id"),
        "uses": step_uses(step),
        "with": step_with(step),
        "env": key_values(fields["env"]) if "env" in fields else {},
        "run": run,
    }


def step(keys, name=None, id=None, uses=None, with_=None, env=None, run=None):
    return {
        "keys": keys.split(),
        "name": name,
        "id": id,
        "uses": uses,
        "with": with_ or {},
        "env": env or {},
        "run": normalize_script(run) if run else None,
    }


def job_summary(lines):
    fields = child_keys(lines, 4)

    def scalar(key):
        """The inline value of `key:` (child_keys keeps its line only when it has one)."""
        if key in fields and re.match(rf"^\s*{re.escape(key)}:", fields[key][0]):
            return _code(fields[key][0].partition(":")[2]).strip()
        return None

    def mapping(key):
        return key_values(fields[key]) if key in fields else None

    permissions = scalar("permissions") or mapping("permissions")
    return {
        "keys": list(fields),
        "runs-on": scalar("runs-on"),
        "if": scalar("if"),
        "needs": scalar("needs"),
        "permissions": permissions,
        "env": mapping("env"),
        "outputs": mapping("outputs"),
        "steps": [step_summary(s) for s in split_steps(lines)],
    }


CHECKOUT = {"persist-credentials": "false"}
GITLEAKS = "$RUNNER_TEMP/gitleaks"
GRYPE_ENV = {"GRYPE": "${{ steps.grype.outputs.cmd }}"}
SYFT_ENV = {"SYFT": "${{ steps.syft.outputs.cmd }}"}
DOWNLOAD_SYFT = step("id uses", id="syft", uses="anchore/sbom-action/download-syft")
DOWNLOAD_GRYPE = step("id uses", id="grype", uses="anchore/scan-action/download-grype")

EXPECTED_JOBS = {
    "dependencies": {
        "keys": ["name", "runs-on", "timeout-minutes", "steps"],
        "runs-on": "ubuntu-latest",
        "if": None,
        "needs": None,
        "permissions": None,
        "env": None,
        "outputs": None,
        "steps": [
            step("uses with", uses="actions/checkout", with_=CHECKOUT),
            step(
                "name env run",
                name="Check that each pinned action is the release its comment names",
                env={"GITHUB_TOKEN": "${{ github.token }}"},
                run="python3 tools/security/check_action_pins.py",
            ),
            DOWNLOAD_SYFT,
            DOWNLOAD_GRYPE,
            step(
                "name env run",
                name="List the locked and vendored dependencies",
                env=SYFT_ENV,
                run=r"""
                "$SYFT" scan dir:. --config .syft.yaml \
                  --output "cyclonedx-json@1.6=$RUNNER_TEMP/dependencies.cdx.json"
                """,
            ),
            step(
                "name run",
                name="Check that every locked package is listed",
                run=(
                    'python3 tools/security/sbom_coverage.py "$RUNNER_TEMP/dependencies.cdx.json"'
                    " --locked"
                ),
            ),
            step(
                "name env run",
                name="Audit them",
                env=GRYPE_ENV,
                run=r"""
                python3 tools/security/grype_gate.py dependencies --grype "$GRYPE" \
                  "$RUNNER_TEMP/dependencies.cdx.json"
                """,
            ),
        ],
    },
    "secrets": {
        "keys": ["name", "runs-on", "timeout-minutes", "env", "steps"],
        "runs-on": "ubuntu-latest",
        "if": None,
        "needs": None,
        "permissions": None,
        "env": {
            "GITLEAKS_VERSION": "8.30.1",
            "GITLEAKS_SHA256": "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
        },
        "outputs": None,
        "steps": [
            step(
                "uses with",
                uses="actions/checkout",
                with_={"fetch-depth": "0", "persist-credentials": "false"},
            ),
            step(
                "name run",
                name="Install gitleaks",
                run=r"""
                archive="$RUNNER_TEMP/gitleaks.tar.gz"
                curl --fail --silent --show-error --location --retry 3 --output "$archive" \
                  "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"
                echo "${GITLEAKS_SHA256}  ${archive}" | sha256sum --check --strict
                tar -xzf "$archive" -C "$RUNNER_TEMP" gitleaks
                """,
            ),
            step(
                "name run",
                name="Check that the settings still report a secret",
                run=r"""
                canary="$RUNNER_TEMP/canary"
                mkdir -p "$canary"
                token="ghp_$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 36)"
                printf 'token = "%s"\n' "$token" > "$canary/settings_local.py"
                status=0
                "$RUNNER_TEMP/gitleaks" dir --redact --no-banner --config .gitleaks.toml "$canary" \
                  || status=$?
                if [ "$status" -ne 1 ]; then
                  echo "::error::gitleaks exited $status on a generated token instead of 1"
                  exit 1
                fi
                """,
            ),
            step(
                "name run",
                name="Scan the history for secrets",
                run=r"""
                "$RUNNER_TEMP/gitleaks" git --log-opts="--diff-merges=first-parent HEAD" \
                  --redact --verbose --no-banner --config .gitleaks.toml .
                """,
            ),
            step(
                "name run",
                name="Scan the files gitleaks skips by name",
                run=r"""
                git log -p --diff-merges=first-parent HEAD -- .gitleaks.toml static/vendor/package-lock.json \
                  | "$RUNNER_TEMP/gitleaks" stdin --redact --verbose --no-banner --config .gitleaks.toml
                """,  # noqa: E501
            ),
        ],
    },
    "image": {
        "keys": ["name", "runs-on", "timeout-minutes", "outputs", "steps"],
        "runs-on": "ubuntu-latest",
        "if": None,
        "needs": None,
        "permissions": None,
        "env": None,
        "outputs": {"sbom-complete": "${{ steps.coverage.outputs.complete }}"},
        "steps": [
            step("uses with", uses="actions/checkout", with_=CHECKOUT),
            DOWNLOAD_SYFT,
            DOWNLOAD_GRYPE,
            step(
                "name env run",
                name="Build the application image (no push)",
                env={"REVISION": "${{ github.event.release.tag_name || github.sha }}"},
                run=r"""
                revision="${REVISION//[^A-Za-z0-9._-]/-}"
                echo "IMAGE=lab-ops:${revision}" >> "$GITHUB_ENV"
                docker build --tag "lab-ops:${revision}" .
                """,
            ),
            step(
                "name env run",
                name="SBOM in SPDX and CycloneDX",
                env=SYFT_ENV,
                run=r"""
                mkdir -p sbom
                "$SYFT" scan "docker:$IMAGE" --config .syft.yaml \
                  --output spdx-json=sbom/lab-ops.spdx.json \
                  --output cyclonedx-json@1.6=sbom/lab-ops.cdx.json
                """,
            ),
            step(
                "name run",
                name="List what the image contains, without the SBOM tool",
                run=r"""
                docker run --rm --entrypoint python --volume "$PWD/tools/security:/probe:ro" \
                  "$IMAGE" /probe/image_contents.py > "$RUNNER_TEMP/image-contents.json"
                """,
            ),
            step(
                "name id run",
                name="Check that the SBOM lists all of it",
                id="coverage",
                run=r"""
                python3 tools/security/sbom_coverage.py sbom/lab-ops.cdx.json \
                  --image "$RUNNER_TEMP/image-contents.json"
                echo "complete=true" >> "$GITHUB_OUTPUT"
                """,
            ),
            step(
                "uses with",
                uses="actions/upload-artifact",
                with_={
                    "name": "sbom",
                    "path": "sbom/",
                    "if-no-files-found": "error",
                    "retention-days": "90",
                },
            ),
            step(
                "name env run",
                name="Fail on a fixable High or Critical vulnerability",
                env=GRYPE_ENV,
                run=(
                    'python3 tools/security/grype_gate.py image --grype "$GRYPE"'
                    " sbom/lab-ops.cdx.json"
                ),
            ),
        ],
    },
    "release-sbom": {
        "keys": ["name", "if", "needs", "runs-on", "timeout-minutes", "permissions", "steps"],
        "runs-on": "ubuntu-latest",
        "if": (
            "${{ github.event_name == 'release' && needs.image.outputs.sbom-complete == 'true'"
            " && !cancelled() }}"
        ),
        "needs": "image",
        "permissions": {"contents": "write"},
        "env": None,
        "outputs": None,
        "steps": [
            step(
                "uses with",
                uses="actions/download-artifact",
                with_={"name": "sbom", "path": "sbom"},
            ),
            step(
                "name env run",
                name="Upload the SBOM as release assets",
                env={
                    "GH_TOKEN": "${{ github.token }}",
                    "TAG": "${{ github.event.release.tag_name }}",
                },
                run=r"""
                case "$TAG" in
                  "" | -*) echo "::error::Unexpected release tag name"; exit 1 ;;
                esac
                name="lab-ops-${TAG//[^A-Za-z0-9._-]/-}"
                mv sbom/lab-ops.spdx.json "sbom/${name}.spdx.json"
                mv sbom/lab-ops.cdx.json "sbom/${name}.cdx.json"
                gh release upload "$TAG" "sbom/${name}.spdx.json" "sbom/${name}.cdx.json" \
                  --clobber --repo "$GITHUB_REPOSITORY"
                """,
            ),
        ],
    },
}


def top_level_keys(text):
    return [m.group(1) for line in text.splitlines() if (m := re.match(r"^([\w-]+):", line))]


def mentions(text, word):
    """Line numbers of the non-comment lines that hold `word`."""
    return [
        number
        for number, line in enumerate(text.splitlines(), 1)
        if _meaningful(line) and word in _code(line)
    ]


def security_workflow_problems(text):
    """Differences between security.yml and the exact jobs, steps and scripts expected above."""
    problems = []
    if top_level_keys(text) != ["name", "on", "permissions", "jobs"]:
        problems.append(f"top-level keys are {top_level_keys(text)}")
    for word in ("continue-on-error", "concurrency"):
        if mentions(text, word):
            problems.append(f"security.yml uses {word} (lines {mentions(text, word)})")
    actual = jobs(text)
    if list(actual) != list(EXPECTED_JOBS):
        return [*problems, f"jobs are {list(actual)}, expected {list(EXPECTED_JOBS)}"]
    for job_id, expected in EXPECTED_JOBS.items():
        got = job_summary(actual[job_id])
        for key, value in expected.items():
            if key == "steps":
                continue
            if got[key] != value:
                problems.append(f"{job_id}: {key} is {got[key]!r}, expected {value!r}")
        if len(got["steps"]) != len(expected["steps"]):
            problems.append(
                f"{job_id}: {len(got['steps'])} steps, expected {len(expected['steps'])}: "
                f"{[s['name'] or s['uses'] for s in got['steps']]}"
            )
            continue
        for index, (have, want) in enumerate(zip(got["steps"], expected["steps"], strict=True), 1):
            for key, value in want.items():
                if have[key] != value:
                    problems.append(
                        f"{job_id} step {index} ({want['name'] or want['uses']}): {key} is "
                        f"{have[key]!r}, expected {value!r}"
                    )
    return problems


# ------------------------------------------------------ codeql.yml, as text


def paths_ignore(text):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = re.match(r"^(\s*)paths-ignore:\s*$", line)
        if match:
            found = []
            for nxt in lines[i + 1 :]:
                item = re.match(r"^\s+-\s+[\"']?(.*?)[\"']?\s*$", _code(nxt))
                if _indent(nxt) <= len(match.group(1)) or not item:
                    break
                found.append(item.group(1))
            return found
    return []


def codeql_problems(text):
    problems = []
    on = triggers(text)
    if set(on) != {"push", "pull_request", "schedule", "workflow_dispatch"}:
        problems.append(f"triggers are {sorted(on)}")
    if mentions(text, "continue-on-error"):
        problems.append("codeql.yml uses continue-on-error")
    languages = re.search(r"^\s+language:\s*\[(.*?)\]", text, re.M)
    found = {v.strip() for v in languages.group(1).split(",")} if languages else set()
    if found != {"python", "javascript-typescript", "actions"}:
        problems.append(f"languages are {sorted(found)}")
    init = [s for s in workflow_steps(text) if s[1] == "github/codeql-action/init"]
    if len(init) != 1:
        problems.append(f"{len(init)} codeql init steps, expected one")
    else:
        if init[0][2].get("queries") != "security-extended":
            problems.append(f"queries are {init[0][2].get('queries')!r}")
        if init[0][2].get("languages") != "${{ matrix.language }}":
            problems.append("the init step does not use the matrix language")
    if "static/vendor/**" not in paths_ignore(text):
        problems.append("paths-ignore does not list static/vendor/**")
    if not any(s[1] == "github/codeql-action/analyze" for s in workflow_steps(text)):
        problems.append("no analyze step")
    return problems


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


def test_no_workflow_of_the_security_checks_continues_on_error():
    for name in ("security.yml", "codeql.yml"):
        assert not mentions(read(f"{WORKFLOWS}/{name}"), "continue-on-error"), name


# ----------------------------------------------------------- tests: security.yml


def test_security_workflow_triggers():
    on = triggers(read(f"{WORKFLOWS}/security.yml"))
    assert set(on) == {"pull_request", "schedule", "workflow_dispatch", "release"}, sorted(on)
    assert "push" not in on, "a push trigger would let a new advisory block the demo deploy"
    assert re.search(r"types:\s*\[\s*published\s*\]", "\n".join(on["release"])), on["release"]
    assert any("cron:" in line for line in on["schedule"]), "schedule has no cron"


def test_security_workflow_jobs_steps_and_scripts_are_exactly_the_expected_ones():
    """Every script that decides a gate, the order of the steps, the release job's condition,
    needs, permissions and steps, the image job's outputs and the secret scan's full checkout."""
    problems = security_workflow_problems(read(f"{WORKFLOWS}/security.yml"))
    assert not problems, "security.yml differs from what the tests pin:\n" + "\n".join(problems)


def test_security_workflow_has_no_concurrency_block():
    assert not mentions(read(f"{WORKFLOWS}/security.yml"), "concurrency"), (
        "a newer push would cancel the run, and its secret scan, of an earlier push"
    )


def test_every_grype_scan_goes_through_the_gate_script():
    text = read(f"{WORKFLOWS}/security.yml")
    assert not re.search(r"scan-action@|anchore/scan-action/[^d]|\bgrype\s+(sbom|dir):", text)
    calls = re.findall(r"tools/security/grype_gate\.py (\w+)", text)
    assert calls == ["dependencies", "image"], calls


def test_the_gate_script_fails_the_levels_the_security_policy_names():
    gate = load_tool("grype_gate")
    assert gate.GATES == {
        "dependencies": {
            "args": ["--fail-on", "low"],
            "canary": {"django", "ecdsa", "bootstrap", "actions/download-artifact"},
        },
        "image": {"args": ["--only-fixed", "--fail-on", "high"], "canary": {"django"}},
    }
    for name in gate.GATES:
        command = gate.grype_command("grype", name, Path("x.json"), [])
        config = Path(command[command.index("--config") + 1])
        assert config.resolve() == (ROOT / ".grype.yaml").resolve()
        assert gate.CANARY.resolve() == (ROOT / "tools/security/canary.cdx.json").resolve()


# ------------------------------------------------------------ tests: codeql.yml


def test_codeql_workflow_triggers_and_configuration():
    problems = codeql_problems(read(f"{WORKFLOWS}/codeql.yml"))
    assert not problems, "\n".join(problems)
    assert re.search(
        r"branches:\s*\[\s*main\s*\]", "\n".join(triggers(read(f"{WORKFLOWS}/codeql.yml"))["push"])
    )


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


def test_grype_config_holds_only_exact_ignore_entries():
    problems = grype_problems(read(".grype.yaml"))
    assert not problems, ".grype.yaml:\n" + "\n".join(problems)


# ---------------------------------------------------- tests: lockfile, dependabot


def test_vendor_lockfile_has_the_exact_shape_and_agrees_with_the_readme_table():
    lock = json.loads(read("static/vendor/package-lock.json"))
    problems = lockfile_problems(lock, read("static/vendor/README.md"))
    assert not problems, "static/vendor/package-lock.json:\n" + "\n".join(problems)


def test_vendored_files_are_the_locked_versions():
    lock = json.loads(read("static/vendor/package-lock.json"))
    problems = vendor_problems(lock, ROOT / "static/vendor")
    assert not problems, "\n".join(problems)


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
        "  - uses: ./local-action\n"
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
    assert permission_problems("permissions: { contents: write }\n")
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


def test_helper_sees_flow_style_and_inline_write_grants():
    head = "permissions:\n  contents: read\njobs:\n  image:\n"
    assert write_grants(head + "    permissions: { contents: write }\n") == [
        (5, "contents", "image")
    ]
    assert write_grants(head + "    permissions: {contents: read, packages: write}\n") == [
        (5, "packages", "image")
    ]
    assert write_grants(head + "    permissions: write-all\n") == [(5, "*", "image")]
    assert write_grants(head + "    permissions:\n      contents: 'write'\n") == [
        (6, "contents", "image")
    ]
    assert not write_grants(head + "    permissions: { contents: read }\n")
    assert unintended_writes("security.yml", head + "    permissions: { contents: write }\n")


GOOD_ALLOWLIST = {
    "extend": {"useDefault": True},
    "allowlists": [{"description": "x", "regexTarget": "secret", "regexes": ["^abc$"]}],
}


def allowlist_with(**changes):
    entry = {**GOOD_ALLOWLIST["allowlists"][0], **changes}
    return {**GOOD_ALLOWLIST, "allowlists": [entry]}


def test_helper_accepts_an_exact_gitleaks_allowlist():
    assert not allowlist_problems(GOOD_ALLOWLIST)
    assert allowlisted_values(GOOD_ALLOWLIST) == ["abc"]
    escaped = allowlist_with(regexes=[r"^a\.b-c_1$", r"^a\$$"])
    assert not allowlist_problems(escaped)
    assert allowlisted_values(escaped) == ["a.b-c_1", "a$"]


@pytest.mark.parametrize(
    "config",
    [
        {**GOOD_ALLOWLIST, "extend": {"useDefault": False}},
        {**GOOD_ALLOWLIST, "extend": {"useDefault": True, "path": "other.toml"}},
        {**GOOD_ALLOWLIST, "title": "x"},
        {**GOOD_ALLOWLIST, "rules": []},
        {"extend": {"useDefault": True}, "allowlist": GOOD_ALLOWLIST["allowlists"][0]},
        allowlist_with(regexes=["^.+$"]),
        allowlist_with(regexes=["^a.c$"]),
        allowlist_with(regexes=["^a(b|c)$"]),
        allowlist_with(regexes=[r"^\d+$"]),
        allowlist_with(regexes=["abc"]),
        allowlist_with(regexes=["^abc"]),
        allowlist_with(regexes=[r"^abc\$"]),
        allowlist_with(regexes=["^$"]),
        allowlist_with(regexes=[]),
        allowlist_with(stopwords=["abc"]),
        allowlist_with(commits=["0" * 40]),
        allowlist_with(paths=["^tests/"]),
        allowlist_with(condition="AND"),
        allowlist_with(regexTarget="line"),
        allowlist_with(description=""),
        {"extend": {"useDefault": True}},
    ],
    ids=lambda config: str(config)[-60:],
)
def test_helper_rejects_gitleaks_settings_that_excuse_more_than_exact_values(config):
    assert allowlist_problems(config)


GOOD_GRYPE = (
    "# comment\n"
    "ignore:\n"
    "  - vulnerability: CVE-2026-1234\n"
    "    package:\n"
    "      name: python\n"
    "      version: 3.14.10\n"
    "    reason: >-\n"
    "      Because.\n"
    "      More.\n"
    "  - vulnerability: GHSA-abcd-1234-wxyz\n"
    "    package:\n"
    "      name: some_pkg-2\n"
    "      version: 1.0\n"
    "    reason: Not used.\n"
)


def test_helper_accepts_exact_grype_entries():
    assert not grype_problems(GOOD_GRYPE)
    assert not grype_problems("# only a comment\n")


@pytest.mark.parametrize(
    "text",
    [
        GOOD_GRYPE + "only-fixed: true\n",
        "only-fixed: true\n" + GOOD_GRYPE,
        GOOD_GRYPE.replace("ignore:", "ignore:\n  fix-state: fixed"),
        "ignore:\n- vulnerability: CVE-2026-1234\n  reason: x\n",
        "ignore: [{vulnerability: CVE-2026-1234, reason: x}]\n",
        GOOD_GRYPE.replace(
            "      version: 3.14.10\n", "      version: 3.14.10\n      type: python\n"
        ),
        GOOD_GRYPE.replace("    reason: >-", "    fix-state: fixed\n    reason: >-"),
        GOOD_GRYPE.replace("name: python", "name: py.*"),
        GOOD_GRYPE.replace("name: python", "name: ^python$"),
        GOOD_GRYPE.replace("version: 3.14.10", "version: '*'"),
        GOOD_GRYPE.replace("version: 3.14.10", "version: 3.*"),
        GOOD_GRYPE.replace("version: 3.14.10", "version:"),
        GOOD_GRYPE.replace("      version: 3.14.10\n", ""),
        GOOD_GRYPE.replace("      name: python\n", ""),
        GOOD_GRYPE.replace("CVE-2026-1234", "*"),
        GOOD_GRYPE.replace("CVE-2026-1234", "CVE-2026"),
        GOOD_GRYPE.replace("    reason: >-\n      Because.\n      More.\n", ""),
        GOOD_GRYPE.replace("    reason: >-\n      Because.\n      More.\n", "    reason: >-\n"),
        GOOD_GRYPE.replace("    reason: Not used.\n", "    reason:\n"),
        GOOD_GRYPE.replace("    reason: >-", "    reason: |"),
        GOOD_GRYPE.replace("    package:\n      name: python", "    package:\n    name: python"),
        GOOD_GRYPE + "ignore:\n",
        GOOD_GRYPE.replace("ignore:", "ignore: []"),
        GOOD_GRYPE.replace("ignore:", "match:\n  ignore-states: [unknown]"),
        "  - vulnerability: CVE-2026-1234\n" + GOOD_GRYPE,
    ],
    ids=lambda text: text[-50:].replace("\n", "|"),
)
def test_helper_rejects_grype_settings_that_excuse_more_than_one_package_version(text):
    assert grype_problems(text)


def lock_with(**changes):
    lock = json.loads(read("static/vendor/package-lock.json"))
    lock = json.loads(json.dumps(lock))
    for key, value in changes.items():
        lock[key] = value
    return lock


def lock_entry(name, **changes):
    lock = lock_with()
    lock["packages"][f"node_modules/{name}"].update(changes)
    return lock


def test_helper_flags_lockfile_drift():
    readme = (
        "| Package | Version | Files | License | Tarball integrity (sha512) |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| a | 1.0.0 | f | MIT | `abc==` |\n"
    )
    lock = {"packages": {"": {}, "node_modules/a": {"version": "1.0.1", "integrity": "sha512-x"}}}
    text = "\n".join(lockfile_problems(lock, readme))
    for expected in ("README 1.0.0", "integrity", "resolved", "bootstrap-icons", "top-level keys"):
        assert expected in text, (expected, text)


def test_helper_accepts_the_repository_lockfile_shape():
    assert not lockfile_problems(lock_with(), read("static/vendor/README.md"))


def mutated_lock(label):
    lock = lock_with()
    entries = lock["packages"]
    if label == "extra field":
        entries["node_modules/htmx.org"]["dev"] = True
    elif label == "missing license":
        del entries["node_modules/htmx.org"]["license"]
    elif label == "license expression":
        entries["node_modules/htmx.org"]["license"] = "MIT OR 0BSD"
    elif label == "short integrity":
        entries["node_modules/htmx.org"]["integrity"] = "sha512-abc=="
    elif label == "sha1 integrity":
        entries["node_modules/htmx.org"]["integrity"] = "sha1-" + "A" * 27 + "="
    elif label == "top-level name":
        lock["name"] = "lab-ops-vendor"
    elif label == "top-level extra":
        lock["dependencies"] = {}
    elif label == "lockfileVersion 2":
        lock["lockfileVersion"] = 2
    elif label == "requires missing":
        del lock["requires"]
    elif label == "root name":
        entries[""]["name"] = "x"
    elif label == "root devDependencies":
        entries[""]["devDependencies"] = {"x": "1"}
    elif label == "root version differs":
        entries[""]["dependencies"]["htmx.org"] = "2.0.10"
    elif label == "root lacks a package":
        del entries[""]["dependencies"]["htmx.org"]
    elif label == "resolved elsewhere":
        entries["node_modules/htmx.org"]["resolved"] = "https://example.invalid/htmx.org.tgz"
    return lock


@pytest.mark.parametrize(
    "label",
    [
        "extra field",
        "missing license",
        "license expression",
        "short integrity",
        "sha1 integrity",
        "top-level name",
        "top-level extra",
        "lockfileVersion 2",
        "requires missing",
        "root name",
        "root devDependencies",
        "root version differs",
        "root lacks a package",
        "resolved elsewhere",
    ],
)
def test_helper_rejects_a_lockfile_outside_the_exact_shape(label):
    assert lockfile_problems(mutated_lock(label), read("static/vendor/README.md")), label


@pytest.fixture
def vendor_copy(tmp_path):
    vendor = tmp_path / "vendor"
    shutil.copytree(ROOT / "static/vendor", vendor)
    return vendor


def test_vendor_helper_accepts_the_repository_and_rejects_drift(vendor_copy):
    lock = lock_with()
    assert not vendor_problems(lock, vendor_copy)
    (vendor_copy / "chartjs-4.4.0").mkdir()
    assert any("chartjs-4.4.0" in p for p in vendor_problems(lock, vendor_copy))


def test_vendor_helper_flags_a_directory_version_that_is_not_the_locked_one(vendor_copy):
    (vendor_copy / "htmx-2.0.11").rename(vendor_copy / "htmx-2.0.12")
    text = "\n".join(vendor_problems(lock_with(), vendor_copy))
    assert "htmx-2.0.12 is not a known" in text and "htmx-2.0.11 is missing" in text


def test_vendor_helper_flags_a_locked_package_no_directory_holds(vendor_copy):
    lock = lock_with()
    lock["packages"]["node_modules/chart.js"] = {"version": "4.4.0"}
    assert any("chart.js is locked" in p for p in vendor_problems(lock, vendor_copy))


def test_vendor_helper_flags_a_lock_version_that_disagrees_with_the_directory(vendor_copy):
    lock = lock_entry("htmx.org", version="2.0.12")
    text = "\n".join(vendor_problems(lock, vendor_copy))
    assert "htmx-2.0.11 names 2.0.11, lockfile has htmx.org 2.0.12" in text


@pytest.mark.parametrize(
    ("relative", "old", "new"),
    [
        ("bootstrap-5.3.8/css/bootstrap.min.css", "v5.3.8", "v5.3.7"),
        ("bootstrap-5.3.8/js/bootstrap.bundle.min.js", "v5.3.8", "v5.3.7"),
        ("htmx-2.0.11/htmx.min.js", 'version:"2.0.11"', 'version:"2.0.10"'),
        ("datatables/dataTables.min.js", "DataTables 3.1.5", "DataTables 3.1.4"),
        ("datatables/dataTables.colReorder.min.js", "ColReorder 3.1.2", "ColReorder 3.1.1"),
        ("tom-select/tom-select.complete.min.js", "Tom Select v2.7.0", "Tom Select v2.6.0"),
        ("vanilla-calendar-pro-3.4.0/index.js", "v3.4.0", "v3.3.0"),
    ],
)
def test_vendor_helper_flags_a_file_that_is_not_the_locked_version(vendor_copy, relative, old, new):
    path = vendor_copy / relative
    data = path.read_bytes()
    assert old.encode() in data
    path.write_bytes(data.replace(old.encode(), new.encode(), 1))
    problems = vendor_problems(lock_with(), vendor_copy)
    assert any(relative in p for p in problems), problems


def test_vendor_helper_flags_a_banner_only_far_into_the_file(vendor_copy):
    path = vendor_copy / "vanilla-calendar-pro-3.4.0/index.js"
    data = path.read_bytes().replace(b"v3.4.0", b"v3.3.0", 1)
    path.write_bytes(data + b"\n" + b" " * 500 + b"/* v3.4.0 */")
    assert vendor_problems(lock_with(), vendor_copy)


# ------------------------------------- tests: the exact workflow comparison catches changes


def moved_upload(text):
    start = text.index("      - uses: actions/upload-artifact")
    end = text.index("      - name: Fail on a fixable")
    block, text = text[start:end], text[:start] + text[end:]
    marker = "\n  release-sbom:"
    return text.replace(marker, "\n" + block.rstrip("\n") + "\n" + marker, 1).replace(
        "\n\n\n", "\n\n"
    )


def drop_line(needle):
    def mutate(text):
        return "".join(line for line in text.splitlines(True) if needle not in line)

    return mutate


def replace(old, new):
    def mutate(text):
        assert old in text, old
        return text.replace(old, new, 1)

    return mutate


WORKFLOW_MUTATIONS = {
    "gate result ignored": replace(
        'grype_gate.py image --grype "$GRYPE" sbom/lab-ops.cdx.json',
        'grype_gate.py image --grype "$GRYPE" sbom/lab-ops.cdx.json || true',
    ),
    "audit result ignored": replace(
        '"$RUNNER_TEMP/dependencies.cdx.json"\n\n  secrets:',
        '"$RUNNER_TEMP/dependencies.cdx.json" || true\n\n  secrets:',
    ),
    "gate looser": replace("grype_gate.py dependencies", "grype_gate.py image"),
    "continue-on-error": replace(
        "    runs-on: ubuntu-latest\n", "    runs-on: ubuntu-latest\n    continue-on-error: true\n"
    ),
    "gitleaks exit code": replace("--no-banner --config", "--no-banner --exit-code 0 --config"),
    "gitleaks not redacting": replace("--redact --verbose", "--redact=0 --verbose"),
    "history narrowed": replace('--log-opts="--diff-merges=first-parent HEAD"', "--log-opts=-1"),
    "history without first-parent": replace("--diff-merges=first-parent HEAD", "HEAD"),
    "shallow checkout": replace("fetch-depth: 0", "fetch-depth: 1"),
    "canary removed": replace('if [ "$status" -ne 1 ]', 'if [ "$status" -ne 0 ]'),
    "stdin scan removed": drop_line("stdin --redact"),
    "stdin scan path narrowed": replace(
        ".gitleaks.toml static/vendor/package-lock.json", "static/vendor/package-lock.json"
    ),
    "coverage mode dropped": replace('dependencies.cdx.json" --locked', 'dependencies.cdx.json"'),
    "image coverage dropped": replace('--image "$RUNNER_TEMP/image-contents.json"', ""),
    "pin check removed": drop_line("check_action_pins.py"),
    "listing config dropped": replace("scan dir:. --config .syft.yaml", "scan dir:."),
    "upload before coverage": moved_upload,
    "coverage id dropped": drop_line("id: coverage"),
    "complete output dropped": drop_line('echo "complete=true"'),
    "outputs dropped": replace(
        "    outputs:\n      sbom-complete: ${{ steps.coverage.outputs.complete }}\n", ""
    ),
    "release if widened": replace(
        "github.event_name == 'release' &&",
        "(github.event_name == 'release' || github.event_name == 'pull_request') &&",
    ),
    "release if without coverage": replace(" && needs.image.outputs.sbom-complete == 'true'", ""),
    "release needs dropped": drop_line("    needs: image"),
    "image job can write (flow style)": replace(
        "    name: image scan and SBOM\n",
        "    name: image scan and SBOM\n    permissions: { contents: write }\n",
    ),
    "release job also gets packages": replace(
        "      contents: write # upload release assets\n",
        "      contents: write # upload release assets\n      packages: write\n",
    ),
    "release job runs a script": replace(
        '          gh release upload "$TAG"',
        '          bash sbom/attach.sh\n          gh release upload "$TAG"',
    ),
    "release job checks out": replace(
        "    steps:\n      - uses: actions/download-artifact",
        "    steps:\n      - uses: actions/checkout@"
        "3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n"
        "      - uses: actions/download-artifact",
    ),
    "concurrency added": replace(
        "permissions:\n  contents: read\n", "concurrency: x\npermissions:\n  contents: read\n"
    ),
    "upload may find nothing": replace("          if-no-files-found: error\n", ""),
    "tool downloaded elsewhere": replace(
        "anchore/scan-action/download-grype", "anchore/scan-action"
    ),
}


def test_the_exact_comparison_accepts_the_real_workflow_text():
    assert not security_workflow_problems(read(f"{WORKFLOWS}/security.yml"))


@pytest.mark.parametrize("label", WORKFLOW_MUTATIONS)
def test_the_exact_comparison_rejects_a_changed_workflow(label):
    original = read(f"{WORKFLOWS}/security.yml")
    mutated = WORKFLOW_MUTATIONS[label](original)
    assert mutated != original, "mutation did not apply"
    assert security_workflow_problems(mutated), label


def test_the_normalizer_ignores_layout_and_not_commands():
    assert normalize_script("a \\\n   b   c\n\n  d\n") == "a b c\nd"
    assert normalize_script("a b\nc") != normalize_script("a b c")


def test_codeql_comparison_rejects_weaker_configurations():
    text = read(f"{WORKFLOWS}/codeql.yml")
    assert not codeql_problems(text)
    for old, new in (
        ("[python, javascript-typescript, actions]", "[python, actions]"),
        ("queries: security-extended", "queries: default"),
        ('              - "static/vendor/**"\n', ""),
        (
            "    runs-on: ubuntu-latest\n",
            "    runs-on: ubuntu-latest\n    continue-on-error: true\n",
        ),
    ):
        assert old in text
        assert codeql_problems(text.replace(old, new, 1)), old
