"""The release security checks only protect while their configuration stays tight: a workflow
step pinned to a movable tag, a token that gained write access, a gitleaks allowlist that excuses a
whole file, or a vendored package missing from the lockfile each weaken a check without failing
any build. These tests pin the settings the checks rely on (.github/workflows, .gitleaks.toml,
.grype.yaml, .syft.yaml, static/vendor, .github/dependabot.yml).

YAML files are parsed with PyYAML, so every spelling GitHub reads as the same key or value reads
the same here (quoted keys, flow style, a value on the next line, escapes) and the tests compare
whole parsed structures instead of lines of text. GitHubLoader below reads YAML as GitHub does:
only `true` and `false` (in any letter case) are booleans, so `on`, `yes` and `no` stay strings,
a mapping with the same key twice is an error, and so is a merge key (`<<`), which GitHub refuses.
Action names are compared without regard to letter case, as GitHub resolves them. The helpers
are pure (text in, problems out) so the negative tests can feed them bad snippets.
"""

import collections.abc
import importlib.util
import json
import re
import shutil
import tomllib
from pathlib import Path

import pytest
import yaml
from django.conf import settings

ROOT = Path(settings.BASE_DIR)
# The repository's own tools; ROOT is replaced by a copy in the tests that read one.
TOOLS = ROOT / "tools/security"
WORKFLOWS = ".github/workflows"

# ------------------------------------------------------------------ YAML as GitHub reads it

BOOL_TAG = "tag:yaml.org,2002:bool"


class GitHubLoader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 booleans, as GitHub Actions reads workflow files, that rejects
    a mapping holding one key twice (a later key would silently replace an earlier one) and a
    merge key (`<<`), which PyYAML expands and GitHub refuses to run."""

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key_node, _ in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        "found a merge key (<<), which GitHub Actions does not support",
                        key_node.start_mark,
                    )
                key = self.construct_object(key_node, deep=True)
                if not isinstance(key, collections.abc.Hashable):
                    continue  # SafeLoader reports an unhashable key itself
                if (type(key), key) in seen:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        f"found the key {key!r} twice",
                        key_node.start_mark,
                    )
                seen.add((type(key), key))
        return super().construct_mapping(node, deep=deep)


# A copy of the resolvers without the YAML 1.1 boolean, which reads on, off, yes, no, y and n.
GitHubLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != BOOL_TAG]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
GitHubLoader.add_implicit_resolver(
    BOOL_TAG, re.compile(r"^(?:true|false)$", re.IGNORECASE), list("tTfF")
)


def parse_yaml(text):
    """The document as GitHub reads it. Raises yaml.YAMLError for text that is not one YAML
    document or that repeats a key."""
    return yaml.load(text, Loader=GitHubLoader)


def load_mapping(text):
    """(parsed mapping or None, problems) for the text of a YAML file whose root is a mapping."""
    try:
        document = parse_yaml(text)
    except yaml.YAMLError as error:
        return None, [f"not readable as YAML: {' '.join(str(error).split())}"]
    if not isinstance(document, dict):
        return None, [f"the document is {type(document).__name__}, expected a mapping"]
    return document, []


def entries(value, path="$"):
    """(path, mapping, key) for every key of every mapping in a parsed document."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield path, value, key
            yield from entries(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from entries(item, f"{path}[{index}]")


def differences(actual, expected, path="$"):
    """Where a parsed structure differs from the expected one: every mapping key, list item and
    value, scalar types included (1 is not true), so an added key anywhere is reported."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: is {actual!r}, expected a mapping"]
        found = [f"{path}: unexpected key {key!r}" for key in actual if key not in expected]
        found += [f"{path}: missing key {key!r}" for key in expected if key not in actual]
        for key in expected:
            if key in actual:
                found += differences(actual[key], expected[key], f"{path}.{key}")
        return found
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return [f"{path}: is {actual!r}, expected {expected!r}"]
        found = []
        for index, (have, want) in enumerate(zip(actual, expected, strict=True)):
            found += differences(have, want, f"{path}[{index}]")
        return found
    if type(actual) is not type(expected) or actual != expected:
        return [f"{path}: is {actual!r}, expected {expected!r}"]
    return []


# ------------------------------------------------------ checks on workflow text

PINNED_REF = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
VERSION_COMMENT = r"\s+#\s*v\d[\w.+-]*\s*$"


def unpinned_uses(text):
    """Problems with the `uses` of the parsed workflow, at any depth (a step or a job calling a
    reusable workflow): each must be pinned to a full commit SHA and, on its own line written
    `uses: owner/repo@sha # v...`, name the release. Local actions (`./...`) are exempt."""
    document, problems = load_mapping(text)
    if document is None:
        return problems
    lines = text.splitlines()
    for path, mapping, key in entries(document):
        if key != "uses":
            continue
        value = mapping[key]
        if not isinstance(value, str):
            problems.append(f"{path}.uses is {value!r}, not a string")
        elif value.startswith("./"):
            continue
        elif not PINNED_REF.match(value):
            problems.append(f"{path}.uses {value} is not pinned to a 40-hex commit SHA")
        elif not any(
            re.match(rf"^\s*(?:-\s+)?uses:\s*{re.escape(value)}{VERSION_COMMENT}", line)
            for line in lines
        ):
            problems.append(
                f"{path}.uses {value} has no line `uses: {value} # v...` naming its release"
            )
    return problems


def checkouts_keeping_credentials(text):
    """Paths of the `actions/checkout` steps that do not set `persist-credentials: false`."""
    document, problems = load_mapping(text)
    if document is None:
        return problems
    found = []
    for path, mapping, key in entries(document):
        value = mapping[key]
        if (
            key == "uses"
            and isinstance(value, str)
            and value.lower().startswith("actions/checkout@")
        ):
            inputs = mapping.get("with")
            if not isinstance(inputs, dict) or inputs.get("persist-credentials") is not False:
                found.append(path)
    return found


def triggers(text):
    """The workflow's `on:` mapping ({} when it is absent or not a mapping)."""
    document, _ = load_mapping(text)
    on = document.get("on") if document else None
    return on if isinstance(on, dict) else {}


def permission_problems(text):
    """The workflow's top-level `permissions:` must be exactly `contents: read`."""
    document, problems = load_mapping(text)
    if document is None:
        return problems
    if "permissions" not in document:
        return ["no top-level 'permissions:' block (the default token is broader)"]
    if document["permissions"] != {"contents": "read"}:
        return [f"top-level permissions are {document['permissions']!r}, expected contents: read"]
    return []


def write_grants(text):
    """(scope, job id or None) for every write grant in the top-level or a job's `permissions:`:
    `write-all`, a scope set to `write`, and ("?", job) for a value that is neither a mapping nor
    `read-all`, which no token setting reads as a safe one."""
    document, problems = load_mapping(text)
    if document is None:
        return [("?", None)]
    grants = []

    def scan(permissions, job):
        if permissions is None or permissions == "read-all":
            return
        if permissions == "write-all":
            grants.append(("*", job))
        elif isinstance(permissions, dict):
            for scope, level in permissions.items():
                if level == "write":
                    grants.append((str(scope), job))
                elif level not in ("read", "none"):
                    grants.append(("?", job))
        else:
            grants.append(("?", job))

    scan(document.get("permissions"), None)
    jobs = document.get("jobs")
    for job_id, job in jobs.items() if isinstance(jobs, dict) else []:
        if isinstance(job, dict):
            scan(job.get("permissions"), job_id)
    return grants


# Where each write scope may appear: (workflow file, job id).
INTENDED_WRITES = {
    "security-events": ("codeql.yml", "analyze"),
    "contents": ("security.yml", "release-sbom"),
    "pages": ("pages.yml", "deploy"),
    "id-token": ("pages.yml", "deploy"),
}


def unintended_writes(name, text):
    return [
        f"{name} grants '{scope}: write' (job {job or 'top level'})"
        for scope, job in write_grants(text)
        if INTENDED_WRITES.get(scope) != (name, job)
    ]


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


# ------------------------------------------------------------- grype and syft config

GRYPE_FIELDS = {
    "vulnerability": re.compile(r"(?:CVE-\d{4}-\d{4,}|GHSA(?:-[23456789cfghjmpqrvwx]{4}){3})"),
    # Grype reads a name containing anything but these characters as a regular expression.
    "name": re.compile(r"[A-Za-z0-9_-]+"),
    "version": re.compile(r"[A-Za-z0-9][A-Za-z0-9.+:~-]*"),
}
GRYPE_REASON_MINIMUM = 40


def grype_field_problems(label, field, value, pattern=None):
    """Problems with one value of a .grype.yaml entry: a non-empty string (YAML reads "", ~ and
    null as empty, which Grype takes for "any"), and the pattern for its kind."""
    if not isinstance(value, str) or not value.strip():
        return [f"{label}: {field} is {value!r}, expected a non-empty string"]
    if pattern is not None and not pattern.fullmatch(value):
        return [f"{label}: {field} {value!r} does not match {pattern.pattern}"]
    return []


def grype_problems(text):
    """Problems in .grype.yaml, which must parse to exactly {"ignore": [entries]}, each entry
    naming a vulnerability, the package's name and version, and a reason."""
    document, problems = load_mapping(text)
    if document is None:
        return problems
    if set(document) != {"ignore"}:
        problems.append(f"top-level keys are {sorted(map(str, document))}, expected only ignore")
    ignore = document.get("ignore")
    if not isinstance(ignore, list):
        return [*problems, f"ignore is {ignore!r}, expected a list of entries"]
    for index, entry in enumerate(ignore, 1):
        label = f"entry {index}"
        if not isinstance(entry, dict) or set(entry) != {"vulnerability", "package", "reason"}:
            keys = sorted(map(str, entry)) if isinstance(entry, dict) else entry
            problems.append(
                f"{label}: is {keys!r}, expected the keys vulnerability, package, reason"
            )
            continue
        problems += grype_field_problems(
            label, "vulnerability", entry["vulnerability"], GRYPE_FIELDS["vulnerability"]
        )
        package = entry["package"]
        if not isinstance(package, dict) or set(package) != {"name", "version"}:
            problems.append(f"{label}: package is {package!r}, expected only name and version")
        else:
            for field in ("name", "version"):
                problems += grype_field_problems(label, field, package[field], GRYPE_FIELDS[field])
        problems += grype_field_problems(label, "reason", entry["reason"])
        reason = entry["reason"]
        if isinstance(reason, str) and len(reason.strip()) < GRYPE_REASON_MINIMUM:
            problems.append(f"{label}: the reason is under {GRYPE_REASON_MINIMUM} characters")
    return problems


def syft_problems(text):
    """Problems in .syft.yaml, which may only add the lockfile cataloger: no exclude or other
    setting can drop a package from a listing the coverage check trusts."""
    document, problems = load_mapping(text)
    if document is None:
        return problems
    if document != {"select-catalogers": ["+javascript-lock-cataloger"]}:
        problems.append(f"the document is {document!r}, expected only select-catalogers")
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
# The only files that may sit directly in static/vendor/; every other entry is a directory above.
VENDOR_FILES = {"README.md", "package-lock.json"}
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
    for entry in sorted(vendor.iterdir()):
        if entry.is_dir() and entry.name not in VENDOR_DIRS:
            problems.append(f"static/vendor/{entry.name} is not a known vendored directory")
        elif not entry.is_dir() and entry.name not in VENDOR_FILES:
            problems.append(
                f"static/vendor/{entry.name} is a file outside the known directories, "
                "README.md and package-lock.json"
            )
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
    """{file name: text} of every workflow GitHub runs: .yml and .yaml files."""
    folder = ROOT / WORKFLOWS
    texts = {
        path.name: path.read_text()
        for path in sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")])
    }
    assert texts, f"no workflows found in {WORKFLOWS}"
    return texts


def action_texts():
    """{path relative to the repository: text} of every file whose `uses` run in a job: the
    workflows and each action definition (action.yml or action.yaml), as the pin script reads
    them."""
    files = load_tool("check_action_pins").action_files(ROOT)
    texts = {path.relative_to(ROOT).as_posix(): path.read_text() for path in files}
    assert texts, "no workflows or actions found"
    return texts


def unresolved_local_actions(text, files, root):
    """The local `uses: ./path` values of a parsed file that do not name an action.yml or
    action.yaml (or, for a reusable workflow, a workflow file) that action_files() returns, so
    that the pin and credentials tests read every one of them."""
    document, problems = load_mapping(text)
    if document is None:
        return problems
    read_files = {Path(path).resolve() for path in files}
    for path, mapping, key in entries(document):
        value = mapping[key]
        if key != "uses" or not isinstance(value, str) or not value.startswith("./"):
            continue
        target = (root / value).resolve()
        found = [target] if target.is_file() else [target / "action.yml", target / "action.yaml"]
        if not any(candidate in read_files for candidate in found):
            problems.append(f"{path}.uses {value} is not an action file the checks read")
    return problems


def read(relative):
    return (ROOT / relative).read_text()


def load_tool(name):
    spec = importlib.util.spec_from_file_location(f"security_{name}", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------- the three security workflows, compared exactly


def normalize_script(text):
    """A shell script with line continuations joined, whitespace collapsed and blank lines
    dropped, so that formatting changes do not matter and any change of a command does."""
    text = re.sub(r"\\[ \t]*\n", " ", text)
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def canonical(value):
    """A parsed workflow with each `uses` value (already checked by unpinned_uses) reduced to
    the action it names in lower case (GitHub ignores the letter case of an owner and repository
    name), and each `run` script normalized, so only what the workflow does is
    compared with the expected document."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "uses" and isinstance(item, str) and PINNED_REF.match(item):
                item = item.partition("@")[0].lower()
            elif key == "run" and isinstance(item, str):
                item = normalize_script(item)
            result[key] = canonical(item)
        return result
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


def exact_workflow_problems(name, text):
    """unpinned_uses() problems, plus every difference between the whole parsed workflow and the
    document the tests hold for it: any added key, changed value or changed script."""
    problems = unpinned_uses(text)
    document, unreadable = load_mapping(text)
    if document is None:
        return problems
    return [*problems, *differences(canonical(document), EXPECTED_WORKFLOWS[name])]


def sh(script):
    return normalize_script(script)


def action(uses, /, **inputs):
    """A step that runs an action; the expected form of a pinned `uses` is the action's name."""
    return {"uses": uses, "with": inputs} if inputs else {"uses": uses}


CHECKOUT = action("actions/checkout", **{"persist-credentials": False})
GRYPE_ENV = {"GRYPE": "${{ steps.grype.outputs.cmd }}"}
SYFT_ENV = {"SYFT": "${{ steps.syft.outputs.cmd }}"}
DOWNLOAD_SYFT = {"id": "syft", "uses": "anchore/sbom-action/download-syft"}
DOWNLOAD_GRYPE = {"id": "grype", "uses": "anchore/scan-action/download-grype"}
SCHEDULE = [{"cron": "41 5 * * 1"}]
READ_ONLY = {"contents": "read"}

SECURITY_WORKFLOW = {
    "name": "security",
    "on": {
        "pull_request": None,
        "schedule": SCHEDULE,
        "workflow_dispatch": None,
        "release": {"types": ["published"]},
    },
    "permissions": READ_ONLY,
    "jobs": {
        "dependencies": {
            "name": "dependency audit",
            "runs-on": "ubuntu-latest",
            "timeout-minutes": 10,
            "steps": [
                CHECKOUT,
                {
                    "name": "Check that each pinned action is the release its comment names",
                    "env": {"GITHUB_TOKEN": "${{ github.token }}"},
                    "run": sh("python3 tools/security/check_action_pins.py"),
                },
                DOWNLOAD_SYFT,
                DOWNLOAD_GRYPE,
                {
                    "name": "List the locked and vendored dependencies",
                    "env": SYFT_ENV,
                    "run": sh(
                        r"""
                        "$SYFT" scan dir:. --config .syft.yaml \
                          --output "cyclonedx-json@1.6=$RUNNER_TEMP/dependencies.cdx.json"
                        """
                    ),
                },
                {
                    "name": "Check that every locked package is listed",
                    "run": sh(
                        "python3 tools/security/sbom_coverage.py"
                        ' "$RUNNER_TEMP/dependencies.cdx.json" --locked'
                    ),
                },
                {
                    "name": "Audit them",
                    "env": GRYPE_ENV,
                    "run": sh(
                        r"""
                        python3 tools/security/grype_gate.py dependencies --grype "$GRYPE" \
                          "$RUNNER_TEMP/dependencies.cdx.json"
                        """
                    ),
                },
            ],
        },
        "image": {
            "name": "image scan and SBOM",
            "runs-on": "ubuntu-latest",
            "timeout-minutes": 20,
            "outputs": {"sbom-complete": "${{ steps.coverage.outputs.complete }}"},
            "steps": [
                CHECKOUT,
                DOWNLOAD_SYFT,
                DOWNLOAD_GRYPE,
                {
                    "name": "Build the application image (no push)",
                    "env": {"REVISION": "${{ github.event.release.tag_name || github.sha }}"},
                    "run": sh(
                        r"""
                        revision="${REVISION//[^A-Za-z0-9._-]/-}"
                        echo "IMAGE=lab-ops:${revision}" >> "$GITHUB_ENV"
                        docker build --tag "lab-ops:${revision}" .
                        """
                    ),
                },
                {
                    "name": "SBOM in SPDX and CycloneDX",
                    "env": SYFT_ENV,
                    "run": sh(
                        r"""
                        mkdir -p sbom
                        "$SYFT" scan "docker:$IMAGE" --config .syft.yaml \
                          --output spdx-json=sbom/lab-ops.spdx.json \
                          --output cyclonedx-json@1.6=sbom/lab-ops.cdx.json
                        """
                    ),
                },
                {
                    "name": "List what the image contains, without the SBOM tool",
                    "run": sh(
                        r"""
                        docker run --rm --user 0 --entrypoint python \
                          --volume "$PWD/tools/security:/probe:ro" "$IMAGE" \
                          /probe/image_contents.py > "$RUNNER_TEMP/image-contents.json"
                        """
                    ),
                },
                {
                    "name": "Check that the SBOM lists all of it",
                    "id": "coverage",
                    "run": sh(
                        r"""
                        python3 tools/security/sbom_coverage.py sbom/lab-ops.cdx.json \
                          --image "$RUNNER_TEMP/image-contents.json" \
                          && echo "complete=true" >> "$GITHUB_OUTPUT"
                        """
                    ),
                },
                action(
                    "actions/upload-artifact",
                    **{
                        "name": "sbom",
                        "path": "sbom/",
                        "if-no-files-found": "error",
                        "retention-days": 90,
                    },
                ),
                {
                    "name": "Fail on a fixable High or Critical vulnerability",
                    "env": GRYPE_ENV,
                    "run": sh(
                        'python3 tools/security/grype_gate.py image --grype "$GRYPE"'
                        " sbom/lab-ops.cdx.json"
                    ),
                },
            ],
        },
        "release-sbom": {
            "name": "attach SBOM to release",
            "if": (
                "${{ github.event_name == 'release' && needs.image.outputs.sbom-complete == 'true'"
                " && !cancelled() }}"
            ),
            "needs": "image",
            "runs-on": "ubuntu-latest",
            "timeout-minutes": 5,
            "permissions": {"contents": "write"},
            "steps": [
                action("actions/download-artifact", name="sbom", path="sbom"),
                {
                    "name": "Upload the SBOM as release assets",
                    "env": {
                        "GH_TOKEN": "${{ github.token }}",
                        "TAG": "${{ github.event.release.tag_name }}",
                    },
                    "run": sh(
                        r"""
                        case "$TAG" in
                          "" | -*) echo "::error::Unexpected release tag name"; exit 1 ;;
                        esac
                        name="lab-ops-${TAG//[^A-Za-z0-9._-]/-}"
                        mv sbom/lab-ops.spdx.json "sbom/${name}.spdx.json"
                        mv sbom/lab-ops.cdx.json "sbom/${name}.cdx.json"
                        gh release upload "$TAG" "sbom/${name}.spdx.json" "sbom/${name}.cdx.json" \
                          --clobber --repo "$GITHUB_REPOSITORY"
                        """
                    ),
                },
            ],
        },
    },
}

SECRETS_WORKFLOW = {
    "name": "secrets",
    "on": {
        "push": {"branches": ["**"], "tags": ["**"]},
        "pull_request": None,
        "schedule": SCHEDULE,
        "workflow_dispatch": None,
    },
    "permissions": READ_ONLY,
    "jobs": {
        "secrets": {
            "name": "secret scan",
            "runs-on": "ubuntu-latest",
            "timeout-minutes": 10,
            "env": {
                "GITLEAKS_VERSION": "8.30.1",
                "GITLEAKS_SHA256": (
                    "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
                ),
                "GITLEAKS_CONFIG_SHA256": (
                    "e163e53b9e7e8a8511e77271e2b323ed057759542a6d988258afe3a1fa329caf"
                ),
            },
            "steps": [
                action("actions/checkout", **{"fetch-depth": 0, "persist-credentials": False}),
                {
                    "name": "Install gitleaks",
                    "run": sh(
                        r"""
                        archive="$RUNNER_TEMP/gitleaks.tar.gz"
                        curl --fail --silent --show-error --location --retry 3 --output "$archive" \
                          "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"
                        echo "${GITLEAKS_SHA256}  ${archive}" | sha256sum --check --strict
                        tar -xzf "$archive" -C "$RUNNER_TEMP" gitleaks
                        """  # noqa: E501
                    ),
                },
                {
                    "name": "Build the configuration without the default list of skipped paths",
                    "run": sh(
                        r"""
                        default="$RUNNER_TEMP/gitleaks-default.toml"
                        curl --fail --silent --show-error --location --retry 3 --output "$default" \
                          "https://raw.githubusercontent.com/gitleaks/gitleaks/v${GITLEAKS_VERSION}/config/gitleaks.toml"
                        echo "${GITLEAKS_CONFIG_SHA256}  ${default}" | sha256sum --check --strict
                        python3 tools/security/gitleaks_config.py "$default" .gitleaks.toml \
                          > "$RUNNER_TEMP/gitleaks.toml"
                        """  # noqa: E501
                    ),
                },
                {
                    "name": "Check that only .gitleaks.toml can excuse a finding",
                    "run": sh(
                        r"""
                        if find . -name .gitleaksignore -not -path ./.git/\* | grep .; then
                          echo "::error::Remove .gitleaksignore; record a synthetic value in .gitleaks.toml"
                          exit 1
                        fi
                        """  # noqa: E501
                    ),
                },
                {
                    "name": (
                        "Check that the settings still report a secret, whatever the file is called"
                    ),
                    "run": sh(
                        r"""
                        canary="$RUNNER_TEMP/canary"
                        mkdir -p "$canary/static/vendor"
                        token() { echo "ghp_$(LC_ALL=C tr -dc 'A-KM-Za-km-z0-9' < /dev/urandom | head -c 36)"; }
                        printf 'token = "%s"\n' "$(token)" > "$canary/settings_local.py"
                        printf '{"note": "%s"}\n' "$(token)" > "$canary/static/vendor/package-lock.json"
                        printf '<svg><!-- %s --></svg>\n' "$(token)" > "$canary/badge.svg"
                        status=0
                        "$RUNNER_TEMP/gitleaks" dir --ignore-gitleaks-allow --redact --no-banner \
                          --config "$RUNNER_TEMP/gitleaks.toml" --report-format json \
                          --report-path "$RUNNER_TEMP/canary.json" "$canary" || status=$?
                        found=$(python3 -c 'import json, sys; print(len({f["File"] for f in json.load(open(sys.argv[1]))}))' "$RUNNER_TEMP/canary.json")
                        if [ "$status" -ne 1 ] || [ "$found" -ne 3 ]; then
                          echo "::error::gitleaks exited $status and reported $found of 3 generated tokens"
                          exit 1
                        fi
                        """  # noqa: E501
                    ),
                },
                {
                    "name": "Scan the history for secrets",
                    "run": sh(
                        r"""
                        "$RUNNER_TEMP/gitleaks" git --log-opts="--text --diff-merges=first-parent HEAD" \
                          --ignore-gitleaks-allow --redact --verbose --no-banner \
                          --config "$RUNNER_TEMP/gitleaks.toml" .
                        """  # noqa: E501
                    ),
                },
            ],
        },
    },
}

CODEQL_WORKFLOW = {
    "name": "codeql",
    "on": {
        "push": {"branches": ["main"]},
        "pull_request": None,
        "schedule": [{"cron": "17 5 * * 1"}],
        "workflow_dispatch": None,
    },
    "permissions": READ_ONLY,
    "concurrency": {
        "group": "codeql-${{ github.event_name }}-${{ github.ref }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    },
    "jobs": {
        "analyze": {
            "name": "analyze (${{ matrix.language }})",
            "runs-on": "ubuntu-latest",
            "timeout-minutes": 30,
            "permissions": {"contents": "read", "security-events": "write"},
            "strategy": {
                "fail-fast": False,
                "matrix": {"language": ["python", "javascript-typescript", "actions"]},
            },
            "steps": [
                CHECKOUT,
                action(
                    "github/codeql-action/init",
                    languages="${{ matrix.language }}",
                    **{"build-mode": "none"},
                    queries="security-extended",
                    config=(
                        'paths-ignore:\n  - "**/tests/**"\n  - "e2e/**"\n  - "static/vendor/**"\n'
                    ),
                ),
                action("github/codeql-action/analyze", category="/language:${{ matrix.language }}"),
            ],
        },
    },
}

EXPECTED_WORKFLOWS = {
    "security.yml": SECURITY_WORKFLOW,
    "secrets.yml": SECRETS_WORKFLOW,
    "codeql.yml": CODEQL_WORKFLOW,
}


# ------------------------------------------------------------ tests: all workflows


def test_the_loader_reads_yaml_as_github_does():
    assert parse_yaml("on: push\nyes: 1\nno: [on, off, y, n]\n") == {
        "on": "push",
        "yes": 1,
        "no": ["on", "off", "y", "n"],
    }
    assert parse_yaml("[true, True, TRUE, false, False, FALSE, tRuE]") == [True] * 3 + [
        False
    ] * 3 + [True]
    assert parse_yaml('"concurrenc\\x79": 1\n') == {"concurrency": 1}
    assert parse_yaml("a: ~\nb: null\nc:\nd: ''\n") == {"a": None, "b": None, "c": None, "d": ""}


@pytest.mark.parametrize(
    "text",
    [
        "a: 1\na: 2\n",
        "a: 1\n'a': 2\n",
        'a: 1\n"\\x61": 2\n',
        "x:\n  b: 1\n  b: 2\n",
        "{a: 1, a: 2}\n",
        "a: 1\n---\nb: 2\n",
    ],
    ids=["twice", "quoted twice", "escaped twice", "nested", "flow", "two documents"],
)
def test_the_loader_rejects_a_repeated_key_and_a_second_document(text):
    with pytest.raises(yaml.YAMLError):
        parse_yaml(text)
    assert load_mapping(text)[0] is None


def test_every_workflow_file_is_a_mapping_with_github_keys():
    for name, text in workflow_texts().items():
        document, problems = load_mapping(text)
        assert document is not None, (name, problems)
        assert {"name", "on", "jobs"} <= set(document), (name, sorted(map(str, document)))
        assert True not in document and None not in document, name


def test_every_action_is_pinned_to_a_commit_sha_with_a_version_comment():
    # Every workflow and every local composite action: the actions inside one run in the job too.
    problems = [
        f"{name}: {problem}"
        for name, text in action_texts().items()
        for problem in unpinned_uses(text)
    ]
    assert not problems, "Pin each action to a full commit SHA plus '# vX.Y.Z':\n" + "\n".join(
        problems
    )


def test_the_pin_check_script_reads_every_uses_line_of_every_workflow():
    action_pins = load_tool("check_action_pins")
    for name, text in action_texts().items():
        found, unreadable = action_pins.pins(text, name)
        assert not unreadable, (name, unreadable)
        assert not unpinned_uses(text), name
        parsed = [
            mapping["uses"]
            for _, mapping, key in entries(parse_yaml(text))
            if key == "uses" and not mapping["uses"].startswith("./")
        ]
        assert len(parsed) == len(found), (name, "the script sees a different number of pins")


def test_every_checkout_drops_the_token():
    problems = [
        f"{name}: {path}: actions/checkout without 'persist-credentials: false'"
        for name, text in action_texts().items()
        for path in checkouts_keeping_credentials(text)
    ]
    assert not problems, "\n".join(problems)


def test_every_local_action_a_workflow_runs_is_read_by_the_checks():
    """`uses: ./.github/actions/x` runs that action's action.yml, whose own `uses` the pin check
    reads only if action_files() returns the file. A local action in a skipped directory or
    under a name no check reads would run unchecked."""
    files = load_tool("check_action_pins").action_files(ROOT)
    problems = [
        f"{name}: {problem}"
        for name, text in action_texts().items()
        for problem in unresolved_local_actions(text, files, ROOT)
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
    assert set(write_grants(texts["codeql.yml"])) == {("security-events", "analyze")}
    assert set(write_grants(texts["security.yml"])) == {("contents", "release-sbom")}
    assert set(write_grants(texts["pages.yml"])) == {("pages", "deploy"), ("id-token", "deploy")}
    assert not write_grants(texts["checks.yml"]) and not write_grants(texts["changelog.yml"])
    assert not write_grants(texts["secrets.yml"])


def test_no_workflow_of_the_security_checks_continues_on_error():
    for name in ("security.yml", "secrets.yml", "codeql.yml"):
        document = parse_yaml(workflow_texts()[name])
        assert not [key for _, _, key in entries(document) if key == "continue-on-error"], name


# ------------------------------------------------- tests: the three workflows, exactly


def test_security_workflow_is_exactly_the_expected_document():
    """Every key, value and script of security.yml: the gates, the order of the steps, the
    release job's condition, needs and permissions, and the image job's outputs."""
    problems = exact_workflow_problems("security.yml", workflow_texts()["security.yml"])
    assert not problems, "security.yml differs from what the tests pin:\n" + "\n".join(problems)


def test_secrets_workflow_is_exactly_the_expected_document():
    problems = exact_workflow_problems("secrets.yml", workflow_texts()["secrets.yml"])
    assert not problems, "secrets.yml differs from what the tests pin:\n" + "\n".join(problems)


def test_the_secret_scan_pins_the_default_configuration_it_builds_from():
    """The configuration downloaded for the pinned release is checked against a SHA-256, and the
    download address names that same release through GITLEAKS_VERSION, never a moving branch."""
    job = yaml.safe_load(read(f"{WORKFLOWS}/secrets.yml"))["jobs"]["secrets"]
    assert re.fullmatch(r"[0-9a-f]{64}", job["env"]["GITLEAKS_CONFIG_SHA256"])
    assert re.fullmatch(r"[0-9a-f]{64}", job["env"]["GITLEAKS_SHA256"])
    assert job["env"]["GITLEAKS_CONFIG_SHA256"] != job["env"]["GITLEAKS_SHA256"]
    build = next(step["run"] for step in job["steps"] if step.get("name", "").startswith("Build"))
    urls = re.findall(r'https://[^"\s]+', build)
    assert urls == [
        "https://raw.githubusercontent.com/gitleaks/gitleaks/v${GITLEAKS_VERSION}"
        "/config/gitleaks.toml"
    ], urls
    assert "${GITLEAKS_CONFIG_SHA256}" in build


def test_codeql_workflow_is_exactly_the_expected_document():
    problems = exact_workflow_problems("codeql.yml", workflow_texts()["codeql.yml"])
    assert not problems, "codeql.yml differs from what the tests pin:\n" + "\n".join(problems)


def test_security_workflow_triggers():
    on = triggers(workflow_texts()["security.yml"])
    assert set(on) == {"pull_request", "schedule", "workflow_dispatch", "release"}, sorted(on)
    assert "push" not in on, "a push trigger would let a new advisory block the demo deploy"
    assert on["release"] == {"types": ["published"]} and on["schedule"][0]["cron"]


def test_the_secret_scan_runs_on_every_push_and_only_in_its_own_workflow():
    texts = workflow_texts()
    on = triggers(texts["secrets.yml"])
    assert on["push"] == {"branches": ["**"], "tags": ["**"]}, (
        "a push to any branch or tag must be scanned"
    )
    assert {"pull_request", "schedule", "workflow_dispatch"} <= set(on)
    scanning = [name for name, text in texts.items() if "gitleaks" in text]
    assert scanning == ["secrets.yml"], scanning


def test_no_security_workflow_has_a_concurrency_group_that_cancels_a_run():
    for name in ("security.yml", "secrets.yml"):
        document = parse_yaml(workflow_texts()[name])
        assert "concurrency" not in document, (
            f"{name}: a newer push would cancel the run, and its secret scan, of an earlier push"
        )


def test_every_grype_scan_goes_through_the_gate_script():
    text = workflow_texts()["security.yml"]
    assert not re.search(r"scan-action@|anchore/scan-action/[^d]|\bgrype\s+(sbom|dir):", text)
    calls = re.findall(r"tools/security/grype_gate\.py (\w+)", text)
    assert calls == ["dependencies", "image"], calls


def test_the_gate_script_fails_the_levels_the_security_policy_names():
    gate = load_tool("grype_gate")
    assert gate.GATES == {
        "dependencies": {
            "args": ["--fail-on", "low"],
            "canary": {
                "django",
                "ecdsa",
                "bootstrap",
                "actions/download-artifact",
                "github/codeql-action",
                "kartverket/github-workflows",
            },
        },
        "image": {"args": ["--only-fixed", "--fail-on", "high"], "canary": {"django"}},
    }
    for name in gate.GATES:
        command = gate.grype_command("grype", name, Path("x.json"), [])
        config = Path(command[command.index("--config") + 1])
        assert config.resolve() == (ROOT / ".grype.yaml").resolve()
        assert gate.CANARY.resolve() == (ROOT / "tools/security/canary.cdx.json").resolve()


# ------------------------------------------------------- tests: gitleaks / grype / syft


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
    assert parse_yaml(read(".grype.yaml"))["ignore"], "the shipped entry is the positive control"


def test_syft_config_only_adds_the_lockfile_cataloger():
    assert not syft_problems(read(".syft.yaml"))
    assert parse_yaml(read(".syft.yaml")) == {"select-catalogers": ["+javascript-lock-cataloger"]}


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
    updates = parse_yaml(read(".github/dependabot.yml"))["updates"]
    actions = [u for u in updates if u.get("package-ecosystem") == "github-actions"]
    assert actions, "dependabot.yml has no github-actions ecosystem"
    assert any(u.get("directory") == "/" for u in actions), "directory is not /"


# ------------------------------------------------- tests: the helpers catch bad input

SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
HEAD = (
    "name: w\non: push\npermissions:\n  contents: read\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
)


def steps(*lines):
    return HEAD + "    steps:\n" + "".join(f"      {line}\n" for line in lines)


def test_helper_flags_tag_pinned_and_uncommented_uses():
    bad = steps(
        "- uses: actions/checkout@v4",
        f"- uses: actions/setup-node@{SHA}",
        "- uses: ./local-action",
        f"- uses: actions/cache@{SHA} # v4.0.0",
        "- uses: docker://alpine:3",
    )
    found = unpinned_uses(bad)
    assert len(found) == 3, found
    assert "actions/checkout@v4" in found[0] and "setup-node" in found[1]
    assert "docker://alpine:3" in found[2]
    assert not unpinned_uses(steps(f"- uses: actions/cache@{SHA} # v4.0.0"))


@pytest.mark.parametrize(
    "lines",
    [
        ["- uses:", "    evil/action@main"],
        ["- {uses: evil/action@main}"],
        ["- 'uses': evil/action@main"],
        ['- "uses": evil/action@main'],
        [f"- 'uses': actions/cache@{SHA} # v4.0.0"],
        ["- uses:", f"    actions/cache@{SHA} # v4.0.0"],
        [f"- {{uses: actions/cache@{SHA}}}"],
        ["- uses: evil/action@main # v1"],
        ["- uses: [evil/action@main]"],
        ["- uses: 7"],
    ],
    ids=lambda lines: " | ".join(lines),
)
def test_helper_flags_every_layout_of_uses_that_is_not_one_pinned_line(lines):
    assert unpinned_uses(steps(*lines))


def test_helper_flags_an_unpinned_job_level_reusable_workflow():
    text = HEAD.replace(
        "    runs-on: ubuntu-latest\n", "    uses: evil/repo/.github/workflows/w.yml@main\n"
    )
    assert unpinned_uses(text)
    pinned = text.replace("@main", f"@{SHA} # v1.0.0")
    assert not unpinned_uses(pinned)


def test_helper_flags_checkout_without_persist_credentials():
    bad = steps(
        f"- uses: actions/checkout@{SHA} # v7.0.1",
        "  with:",
        "    fetch-depth: 0",
        f"- uses: actions/checkout@{SHA} # v7.0.1",
        "  with:",
        "    persist-credentials: false",
        "- name: Use it",
        f"  uses: actions/checkout@{SHA} # v7.0.1",
        f"- uses: actions/checkout@{SHA} # v7.0.1",
        "  with: {persist-credentials: 'false'}",
        f"- uses: actions/checkout@{SHA} # v7.0.1",
        "  with: {persist-credentials: true}",
        f"- uses: actions/checkout@{SHA} # v7.0.1",
        "  with:",
        "    'persist-credentials': false",
    )
    assert [path for path in checkouts_keeping_credentials(bad)] == [
        "$.jobs.a.steps[0]",
        "$.jobs.a.steps[2]",
        "$.jobs.a.steps[3]",
        "$.jobs.a.steps[4]",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "name: w\npermissions:\n  contents: write\n",
        "name: w\npermissions: write-all\n",
        "name: w\n'permissions': write-all\n",
        "name: w\npermissions: { contents: write }\n",
        "name: w\non: push\n",
        "name: w\npermissions:\n  contents: read\n  packages: read\n",
        "name: w\npermissions: read-all\n",
        "name: w\npermissions:\n",
        "name: w\npermissions: {}\n",
        "name: w\npermissions:\n  contents: read\npermissions:\n  contents: read\n",
    ],
)
def test_helper_flags_top_level_permissions_that_are_not_contents_read(text):
    assert permission_problems(text)


def test_helper_accepts_top_level_contents_read_only():
    assert not permission_problems("name: w\npermissions:\n  contents: read\n")
    assert not permission_problems("name: w\n\"permissions\": {'contents': 'read'}\n")


def test_helper_reports_writes_outside_the_intended_jobs():
    bad = (
        "permissions:\n  contents: read\njobs:\n  a:\n"
        "    permissions:\n      security-events: write\n"
    )
    assert unintended_writes("security.yml", bad)
    assert unintended_writes("codeql.yml", "permissions:\n  contents: write\njobs:\n  a:\n")
    ok = "jobs:\n  analyze:\n    permissions:\n      security-events: write # upload\n"
    assert not unintended_writes("codeql.yml", ok)
    assert unintended_writes("codeql.yml", ok.replace("analyze", "other"))


def test_helper_sees_every_spelling_of_a_write_grant():
    head = "permissions:\n  contents: read\njobs:\n  image:\n"
    for grant, scope in (
        ("    permissions: { contents: write }\n", "contents"),
        ("    permissions: {contents: read, packages: write}\n", "packages"),
        ("    permissions:\n      contents: 'write'\n", "contents"),
        ("    permissions:\n      'contents': write\n", "contents"),
        ('    permissions: { "contents": write }\n', "contents"),
        ("    'permissions':\n      contents: write\n", "contents"),
        ("    permissions: write-all\n", "*"),
        ("    'permissions': write-all\n", "*"),
        ("    permissions: 'write-all'\n", "*"),
    ):
        assert write_grants(head + grant) == [(scope, "image")], grant
        assert unintended_writes("security.yml", head + grant), grant
    assert not write_grants(head + "    permissions: { contents: read }\n")
    assert not write_grants(head + "    permissions: read-all\n")
    assert not write_grants(head + "    permissions: {}\n")
    assert write_grants(head + "    permissions: write\n") == [("?", "image")]
    assert write_grants(head + "    permissions: {contents: admin}\n") == [("?", "image")]
    assert write_grants("permissions:\n  contents: write\n") == [("contents", None)]


def test_a_yaml_extension_workflow_is_read_by_every_workflow_test(monkeypatch, tmp_path):
    shutil.copytree(ROOT / WORKFLOWS, tmp_path / WORKFLOWS)
    (tmp_path / WORKFLOWS / "extra.yaml").write_text(
        "name: extra\non: pull_request_target\npermissions: write-all\njobs:\n  a:\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n"
    )
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    assert "extra.yaml" in workflow_texts()
    for test in (
        test_every_action_is_pinned_to_a_commit_sha_with_a_version_comment,
        test_every_checkout_drops_the_token,
        test_every_workflow_defaults_to_read_only_contents,
        test_write_permissions_appear_only_where_intended,
    ):
        with pytest.raises(AssertionError) as failure:
            test()
        assert "extra.yaml" in str(failure.value), test.__name__


DOWNLOAD_V411 = "actions/download-artifact@6b208ae046db98c579e8a3aa621ab581ff575935"
SETUP_UV = "      - uses: astral-sh/setup-uv@"


def repository_copy(tmp_path, monkeypatch):
    """A copy of .github (the tests read it as ROOT) that the pin script reads as well."""
    shutil.copytree(ROOT / ".github", tmp_path / ".github")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    return tmp_path


def add_composite_action(copy, uses_lines, name="fetch"):
    """A local composite action holding `uses_lines` (steps), run by a step added to checks.yml."""
    action = copy / f".github/actions/{name}/action.yml"
    action.parent.mkdir(parents=True)
    steps = "".join(f"    {line}\n" for line in uses_lines)
    action.write_text(
        "name: fetch\ndescription: Fetch the build output\nruns:\n  using: composite\n"
        f"  steps:\n{steps}"
    )
    workflow = copy / ".github/workflows/checks.yml"
    text = workflow.read_text()
    assert SETUP_UV in text
    workflow.write_text(
        text.replace(SETUP_UV, f"      - uses: ./.github/actions/{name}\n" + SETUP_UV, 1)
    )
    return action


def test_a_composite_action_with_pinned_steps_passes_every_workflow_test(monkeypatch, tmp_path):
    copy = repository_copy(tmp_path, monkeypatch)
    add_composite_action(
        copy,
        [f"- uses: {DOWNLOAD_V411} # v4.1.1", f"- uses: actions/checkout@{SHA} # v7.0.1"]
        + ["  with:", "    persist-credentials: false"],
    )
    assert "/fetch/action.yml" in "".join(action_texts())
    for test in (
        test_every_action_is_pinned_to_a_commit_sha_with_a_version_comment,
        test_the_pin_check_script_reads_every_uses_line_of_every_workflow,
        test_every_checkout_drops_the_token,
        test_every_local_action_a_workflow_runs_is_read_by_the_checks,
    ):
        test()


@pytest.mark.parametrize(
    "line",
    ["- uses: evil/action@main", f"- uses: {DOWNLOAD_V411}", "- uses: actions/cache@v4"],
    ids=["tag", "no version comment", "major tag"],
)
def test_an_unpinned_action_in_a_composite_action_is_reported(monkeypatch, tmp_path, line):
    copy = repository_copy(tmp_path, monkeypatch)
    action = add_composite_action(copy, [line])
    with pytest.raises(AssertionError) as failure:
        test_every_action_is_pinned_to_a_commit_sha_with_a_version_comment()
    assert ".github/actions/fetch/action.yml" in str(failure.value)
    found, unreadable = load_tool("check_action_pins").pins(action.read_text(), "action.yml")
    assert unreadable and not found, "the pin script reports it too"


def test_a_pin_commented_with_another_release_in_a_composite_action_is_reported_by_the_script(
    monkeypatch, tmp_path
):
    """The v4.1.1 commit commented v4.1.8: the pin is a commit with a version comment, so the
    workflow tests accept it, and the pin script's resolution of the comment reports it."""
    copy = repository_copy(tmp_path, monkeypatch)
    add_composite_action(copy, [f"- uses: {DOWNLOAD_V411} # v4.1.8"])
    test_every_action_is_pinned_to_a_commit_sha_with_a_version_comment()
    pin_script = load_tool("check_action_pins")
    pinned = []
    for path in pin_script.action_files(copy):
        found, unreadable = pin_script.pins(path.read_text(), path.relative_to(copy).as_posix())
        assert not unreadable
        pinned += found
    assert {(repo, tag) for _, repo, _, tag in pinned} >= {("actions/download-artifact", "v4.1.8")}
    # Each comment resolves to the commit pinned for it, except v4.1.8: the real tag's commit.
    resolved = {(pin_script.repository(repo), tag): sha for _, repo, sha, tag in pinned}
    resolved[("actions/download-artifact", "v4.1.8")] = "a" * 40
    lines = pin_script.problems(pinned, lambda repo, tag: resolved[(repo, tag)])
    assert len(lines) == 1, lines
    assert lines[0].startswith(".github/actions/fetch/action.yml:6: actions/download-artifact@")
    assert "is commented v4.1.8" in lines[0]


def test_a_checkout_that_keeps_the_token_in_a_composite_action_is_reported(monkeypatch, tmp_path):
    copy = repository_copy(tmp_path, monkeypatch)
    add_composite_action(copy, [f"- uses: actions/checkout@{SHA} # v7.0.1"])
    with pytest.raises(AssertionError) as failure:
        test_every_checkout_drops_the_token()
    assert "fetch/action.yml" in str(failure.value)


def test_a_layout_of_uses_the_script_cannot_read_in_a_composite_action_is_reported(
    monkeypatch, tmp_path
):
    copy = repository_copy(tmp_path, monkeypatch)
    add_composite_action(copy, ["- {uses: evil/action@main}"])
    with pytest.raises(AssertionError) as failure:
        test_the_pin_check_script_reads_every_uses_line_of_every_workflow()
    assert "action.yml" in str(failure.value)


def test_a_local_action_that_does_not_exist_is_reported(monkeypatch, tmp_path):
    copy = repository_copy(tmp_path, monkeypatch)
    workflow = copy / ".github/workflows/checks.yml"
    text = workflow.read_text()
    workflow.write_text(text.replace(SETUP_UV, "      - uses: ./missing\n" + SETUP_UV, 1))
    with pytest.raises(AssertionError) as failure:
        test_every_local_action_a_workflow_runs_is_read_by_the_checks()
    assert "checks.yml" in str(failure.value) and "./missing" in str(failure.value)


def test_a_local_action_the_checks_do_not_read_is_reported(monkeypatch, tmp_path):
    """An action.yml in a skipped directory, or a directory with none, runs unchecked."""
    copy = repository_copy(tmp_path, monkeypatch)
    (copy / ".local/actions/x").mkdir(parents=True)
    (copy / ".local/actions/x/action.yml").write_text("runs:\n  using: composite\n  steps: []\n")
    (copy / "empty").mkdir()
    files = load_tool("check_action_pins").action_files(copy)
    workflow = (
        "jobs:\n  a:\n    steps:\n      - uses: ./.local/actions/x\n      - uses: ./empty\n"
        "      - uses: ./../outside\n      - uses: ./.github/workflows/checks.yml\n"
    )
    problems = unresolved_local_actions(workflow, files, copy)
    assert [problem.split(".uses ")[1].split(" ")[0] for problem in problems] == [
        "./.local/actions/x",
        "./empty",
        "./../outside",
    ], problems


def test_every_local_action_resolves_to_an_action_file_that_is_returned(tmp_path):
    (tmp_path / ".github/actions/y").mkdir(parents=True)
    action = tmp_path / ".github/actions/y/action.yaml"
    action.write_text("runs:\n  using: composite\n  steps: []\n")
    files = [action, tmp_path / "w.yml"]
    ok = (
        "jobs:\n  a:\n    steps:\n      - uses: ./.github/actions/y\n"
        "      - uses: ./.github/actions/y/\n"
    )
    assert unresolved_local_actions(ok, files, tmp_path) == []
    assert unresolved_local_actions("a: [", files, tmp_path)
    assert unresolved_local_actions(ok, [], tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        "a: &x {b: 1}\nc:\n  <<: *x\n",
        "c:\n  <<: {a: 1}\n",
        "<<: {a: 1}\n",
        "c:\n  <<: [{a: 1}, {b: 2}]\n  d: 1\n",
        "c: {<<: {a: 1}}\n",
        "c:\n  '<<': 1\n  <<: {a: 1}\n",
    ],
    ids=["alias", "flow", "top level", "sequence", "inline", "after a quoted key"],
)
def test_the_loader_rejects_a_merge_key_which_github_refuses(text):
    assert yaml.safe_load(text), "control: PyYAML expands it"
    with pytest.raises(yaml.YAMLError, match="merge key"):
        parse_yaml(text)
    document, problems = load_mapping(text)
    assert document is None and "merge key" in problems[0]


def test_the_loader_keeps_anchors_aliases_and_a_quoted_merge_key_text():
    assert parse_yaml("a: &x {b: 1}\nc: *x\n") == {"a": {"b": 1}, "c": {"b": 1}}
    assert parse_yaml("'<<': 1\n") == {"<<": 1}
    assert parse_yaml("a: '<<'\nb: x<<y\n") == {"a": "<<", "b": "x<<y"}


def test_a_merge_key_in_a_workflow_fails_the_exact_comparison():
    text = read(f"{WORKFLOWS}/secrets.yml")
    old = "        with:\n          fetch-depth: 0\n          persist-credentials: false\n"
    assert old in text
    merged = text.replace(
        old, "        with:\n          <<: {fetch-depth: 0, persist-credentials: false}\n", 1
    )
    assert yaml.safe_load(merged) == yaml.safe_load(text), "control: PyYAML expands it"
    problems = exact_workflow_problems("secrets.yml", merged)
    assert problems and "merge key" in problems[0]
    assert not exact_workflow_problems("secrets.yml", text)


@pytest.mark.parametrize("spelling", ["Actions/Checkout", "ACTIONS/CHECKOUT", "actions/Checkout"])
def test_a_checkout_in_another_letter_case_is_still_found(spelling):
    bad = steps(f"- uses: {spelling}@{SHA} # v7.0.1")
    assert checkouts_keeping_credentials(bad) == ["$.jobs.a.steps[0]"]
    kept = steps(f"- uses: {spelling}@{SHA} # v7.0.1", "  with:", "    persist-credentials: true")
    assert checkouts_keeping_credentials(kept) == ["$.jobs.a.steps[0]"]
    good = steps(f"- uses: {spelling}@{SHA} # v7.0.1", "  with:", "    persist-credentials: false")
    assert checkouts_keeping_credentials(good) == []


@pytest.mark.parametrize("spelling", ["Actions/Checkout", "ACTIONS/CHECKOUT"])
def test_every_checkout_drops_the_token_whatever_the_letter_case(monkeypatch, tmp_path, spelling):
    copy = repository_copy(tmp_path, monkeypatch)
    workflow = copy / ".github/workflows/checks.yml"
    text = workflow.read_text()
    kept = re.sub(
        r"uses: actions/checkout(@\w+ # v[\w.]+)\n\s+with:\n\s+persist-credentials: false\n",
        rf"uses: {spelling}\1\n",
        text,
        count=1,
    )
    assert kept != text
    workflow.write_text(kept)
    with pytest.raises(AssertionError) as failure:
        test_every_checkout_drops_the_token()
    assert "checks.yml" in str(failure.value)
    # With the setting the spelling is accepted, by the checkout test and the exact comparison.
    secrets = read(f"{WORKFLOWS}/secrets.yml")
    respelled = secrets.replace("uses: actions/checkout@", f"uses: {spelling}@", 1)
    assert respelled != secrets
    assert checkouts_keeping_credentials(respelled) == []
    assert not exact_workflow_problems("secrets.yml", respelled)
    without = respelled.replace("          persist-credentials: false\n", "", 1)
    assert checkouts_keeping_credentials(without) and exact_workflow_problems(
        "secrets.yml", without
    )


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
    "      Affects only an option the image does not use. Remove this entry when the base\n"
    "      image moves to the fixed release.\n"
    "  - vulnerability: GHSA-cfgh-2345-jmpq\n"
    "    package:\n"
    "      name: some_pkg-2\n"
    "      version: 1:2.0+b1~rc-3\n"
    "    reason: Not reachable, the application never calls the affected function.\n"
)


def test_helper_accepts_exact_grype_entries():
    assert not grype_problems(GOOD_GRYPE)
    assert not grype_problems("ignore: []\n")
    flow = (
        "ignore:\n  - {vulnerability: CVE-2026-1234, package: {name: python, version: '3.14'},"
        " reason: 'Not used by the image: its option is never enabled here.'}\n"
    )
    assert not grype_problems(flow)


@pytest.mark.parametrize(
    "text",
    [
        "# only a comment\n",
        "",
        "ignore:\n",
        "ignore: ~\n",
        "ignore: {}\n",
        GOOD_GRYPE + "only-fixed: true\n",
        "only-fixed: true\n" + GOOD_GRYPE,
        GOOD_GRYPE.replace("ignore:", "ignore:\n  fix-state: fixed"),
        GOOD_GRYPE.replace("ignore:", "match:\n  ignore-states: [unknown]\nignore:"),
        GOOD_GRYPE + "ignore:\n",
        "'ignore': [{vulnerability: CVE-2026-1234, reason: x}]\n",
        "ignore:\n- vulnerability: CVE-2026-1234\n  reason: x\n",
        "ignore: [{vulnerability: CVE-2026-1234, reason: unused}]\n",
        GOOD_GRYPE.replace(
            "      version: 3.14.10\n", "      version: 3.14.10\n      type: python\n"
        ),
        GOOD_GRYPE.replace("    reason: >-", "    fix-state: fixed\n    reason: >-"),
        GOOD_GRYPE.replace("name: python", 'name: ""'),
        GOOD_GRYPE.replace("name: python", "name: ''"),
        GOOD_GRYPE.replace("name: python", "name: ~"),
        GOOD_GRYPE.replace("name: python", "name: null"),
        GOOD_GRYPE.replace("name: python", "name: 1"),
        GOOD_GRYPE.replace("name: python", "name: py.*"),
        GOOD_GRYPE.replace("name: python", "name: ^python$"),
        GOOD_GRYPE.replace("version: 3.14.10", 'version: ""'),
        GOOD_GRYPE.replace("version: 3.14.10", "version: ~"),
        GOOD_GRYPE.replace("version: 3.14.10", "version: null"),
        GOOD_GRYPE.replace("version: 3.14.10", "version:"),
        GOOD_GRYPE.replace("version: 3.14.10", "version: 1.10"),
        GOOD_GRYPE.replace("version: 3.14.10", "version: 3"),
        GOOD_GRYPE.replace("version: 3.14.10", "version: true"),
        GOOD_GRYPE.replace("version: 3.14.10", "version: '*'"),
        GOOD_GRYPE.replace("version: 3.14.10", "version: 3.*"),
        GOOD_GRYPE.replace("version: 3.14.10", "version: '>=3'"),
        GOOD_GRYPE.replace("      version: 3.14.10\n", ""),
        GOOD_GRYPE.replace("      name: python\n", ""),
        GOOD_GRYPE.replace("CVE-2026-1234", "*"),
        GOOD_GRYPE.replace("CVE-2026-1234", "CVE-2026"),
        GOOD_GRYPE.replace("CVE-2026-1234", "CVE-2026-123"),
        GOOD_GRYPE.replace("GHSA-cfgh-2345-jmpq", "GHSA-cfgh-2345-jmp"),
        GOOD_GRYPE.replace("GHSA-cfgh-2345-jmpq", "GHSA-CFGH-2345-JMPQ"),
        GOOD_GRYPE.replace("CVE-2026-1234", "~"),
        GOOD_GRYPE.replace(
            "    reason: Not reachable, the application never calls the affected function.\n", ""
        ),
        GOOD_GRYPE.replace(
            "Not reachable, the application never calls the affected function.", "Not used."
        ),
        GOOD_GRYPE.replace(
            "Not reachable, the application never calls the affected function.", "~"
        ),
        GOOD_GRYPE.replace(
            "Not reachable, the application never calls the affected function.", '""'
        ),
        GOOD_GRYPE.replace(
            "    reason: >-\n", "    reason: >-\n      Short.\n  # x\n  - 1\n    reason:\n"
        ),
        GOOD_GRYPE.replace("    package:\n      name: python", "    package:\n    name: python"),
        GOOD_GRYPE.replace(
            "    package:\n      name: python\n      version: 3.14.10\n", "    package: python\n"
        ),
        GOOD_GRYPE.replace(
            "  - vulnerability: GHSA",
            "  - vulnerability: GHSA-cfgh-2345-jmpq\n    vulnerability: GHSA",
        ),
        GOOD_GRYPE + "---\nignore: []\n",
    ],
    ids=lambda text: text[-60:].replace("\n", "|"),
)
def test_helper_rejects_grype_settings_that_excuse_more_than_one_package_version(text):
    assert grype_problems(text)


def test_helper_accepts_only_the_lockfile_cataloger_in_the_syft_config():
    assert not syft_problems('# c\nselect-catalogers:\n  - "+javascript-lock-cataloger"\n')
    assert not syft_problems("'select-catalogers': ['+javascript-lock-cataloger']\n")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "select-catalogers: []\n",
        "select-catalogers:\n  - '+javascript-lock-cataloger'\n  - '-python-package-cataloger'\n",
        "select-catalogers: ['+javascript-lock-cataloger']\nexclude: ['./.github/**']\n",
        "select-catalogers: ['+javascript-lock-cataloger']\n'exclude': ['./.github/**']\n",
        "select-catalogers: ['+javascript-lock-cataloger']\nenrich: [go]\n",
        "select-catalogers: ['javascript-lock-cataloger']\n",
        "select-catalogers: ['+javascript-lock-cataloger']\nselect-catalogers: ['x']\n",
        "exclude: ['./.github/**']\n",
        "- select-catalogers\n",
    ],
)
def test_helper_rejects_syft_settings_that_can_drop_a_package_from_a_listing(text):
    assert syft_problems(text)


def test_vendor_helper_flags_a_file_outside_the_known_directories(vendor_copy):
    assert not vendor_problems(lock_with(), vendor_copy)
    (vendor_copy / "jquery-3.4.1.min.js").write_text("/*! jQuery v3.4.1 */\n")
    problems = vendor_problems(lock_with(), vendor_copy)
    assert any("jquery-3.4.1.min.js" in problem for problem in problems), problems


def test_vendor_helper_accepts_only_the_two_known_files(vendor_copy):
    assert {p.name for p in vendor_copy.iterdir() if p.is_file()} == VENDOR_FILES
    (vendor_copy / "NOTICE.txt").write_text("x\n")
    assert any("NOTICE.txt" in problem for problem in vendor_problems(lock_with(), vendor_copy))


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
        assert needle in text, needle
        return "".join(line for line in text.splitlines(True) if needle not in line)

    return mutate


def replace(old, new, count=1):
    def mutate(text):
        assert old in text, old
        return text.replace(old, new, count)

    return mutate


def after(anchor, addition):
    return replace(anchor, anchor + addition)


TOP = "permissions:\n  contents: read\n"
JOB_FIELDS = "    runs-on: ubuntu-latest\n"
SCAN_STEP = "      - name: Scan the history for secrets\n"

# (workflow file, label) -> a change of that file's text. Each must be reported.
WORKFLOW_MUTATIONS = {
    ("security.yml", "gate result ignored"): replace(
        'grype_gate.py image --grype "$GRYPE" sbom/lab-ops.cdx.json',
        'grype_gate.py image --grype "$GRYPE" sbom/lab-ops.cdx.json || true',
    ),
    ("security.yml", "audit result ignored"): replace(
        '"$RUNNER_TEMP/dependencies.cdx.json"\n\n  image:',
        '"$RUNNER_TEMP/dependencies.cdx.json" || true\n\n  image:',
    ),
    ("security.yml", "gate looser"): replace("grype_gate.py dependencies", "grype_gate.py image"),
    ("security.yml", "continue-on-error"): after(JOB_FIELDS, "    continue-on-error: true\n"),
    ("security.yml", "coverage mode dropped"): replace(
        'dependencies.cdx.json" --locked', 'dependencies.cdx.json"'
    ),
    ("security.yml", "image coverage dropped"): replace(
        '--image "$RUNNER_TEMP/image-contents.json"', ""
    ),
    ("security.yml", "pin check removed"): drop_line("check_action_pins.py"),
    ("security.yml", "listing config dropped"): replace(
        "scan dir:. --config .syft.yaml", "scan dir:."
    ),
    ("security.yml", "upload before coverage"): moved_upload,
    ("security.yml", "coverage id dropped"): drop_line("id: coverage"),
    ("security.yml", "complete output dropped"): drop_line('echo "complete=true"'),
    ("security.yml", "complete written whatever the check says"): replace(
        '            && echo "complete=true"', '            ; echo "complete=true"'
    ),
    ("security.yml", "outputs dropped"): replace(
        "    outputs:\n      sbom-complete: ${{ steps.coverage.outputs.complete }}\n", ""
    ),
    ("security.yml", "release if widened"): replace(
        "github.event_name == 'release' &&",
        "(github.event_name == 'release' || github.event_name == 'pull_request') &&",
    ),
    ("security.yml", "release if without coverage"): replace(
        " && needs.image.outputs.sbom-complete == 'true'", ""
    ),
    ("security.yml", "release needs dropped"): drop_line("    needs: image"),
    ("security.yml", "image job can write (flow style)"): after(
        "    name: image scan and SBOM\n", "    permissions: { contents: write }\n"
    ),
    ("security.yml", "release job also gets packages"): after(
        "      contents: write # upload release assets\n", "      packages: write\n"
    ),
    ("security.yml", "release job runs a script"): replace(
        '          gh release upload "$TAG"',
        '          bash sbom/attach.sh\n          gh release upload "$TAG"',
    ),
    ("security.yml", "release job checks out"): replace(
        "    steps:\n      - uses: actions/download-artifact",
        "    steps:\n      - uses: actions/checkout@"
        "3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n"
        "      - uses: actions/download-artifact",
    ),
    ("security.yml", "upload may find nothing"): drop_line("if-no-files-found: error"),
    ("security.yml", "tool downloaded elsewhere"): replace(
        "anchore/scan-action/download-grype", "anchore/scan-action"
    ),
    ("security.yml", "a push trigger"): replace("\non:\n", "\non:\n  push:\n"),
    ("security.yml", "quoted 'if': false on a step"): after(
        "      - name: Audit them\n", "        'if': false\n"
    ),
    ("security.yml", "quoted 'if' on a job"): after(
        "    name: image scan and SBOM\n", "    'if': ${{ false }}\n"
    ),
    ("security.yml", "quoted 'permissions': write-all"): after(
        "    name: image scan and SBOM\n", "    'permissions': write-all\n"
    ),
    ("security.yml", "top-level defaults (quoted)"): after(
        TOP, "'defaults':\n  run:\n    shell: bash {0}\n"
    ),
    ("security.yml", "top-level defaults"): after(TOP, "defaults:\n  run:\n    shell: bash {0}\n"),
    ("security.yml", "escaped key concurrency"): after(
        TOP, '"concurrenc\\x79":\n  group: ${{ github.ref }}\n  cancel-in-progress: true\n'
    ),
    ("security.yml", "concurrency added"): after(TOP, "concurrency: x\n"),
    ("security.yml", "shell on a step"): after(
        "      - name: Audit them\n", "        shell: bash {0}\n"
    ),
    ("security.yml", "env var on a job"): after(JOB_FIELDS, "    env:\n      PYTHONPATH: x\n"),
    ("security.yml", "env var on a step"): after(
        "        env:\n          GRYPE: ${{ steps.grype.outputs.cmd }}\n",
        "          GRYPE_DB_AUTO_UPDATE: true\n",
    ),
    ("security.yml", "continue-on-error (quoted) on a step"): after(
        "      - name: Audit them\n", "        'continue-on-error': true\n"
    ),
    ("security.yml", "top-level permissions widened"): replace(
        TOP, "permissions:\n  contents: read\n  packages: write\n"
    ),
    ("security.yml", "a key repeated"): after(TOP, TOP),
    ("security.yml", "a step key repeated"): after(
        "      - name: Audit them\n", "        name: Audit them\n"
    ),
    ("security.yml", "a second document"): lambda text: text + "---\nname: other\n",
    ("security.yml", "timeout removed"): drop_line("timeout-minutes: 5"),
    ("security.yml", "timeout as a boolean"): replace(
        "timeout-minutes: 5", "timeout-minutes: true"
    ),
    ("secrets.yml", "exit code"): replace(
        "--verbose --no-banner", "--verbose --no-banner --exit-code 0"
    ),
    ("secrets.yml", "not redacting"): replace("--redact --verbose", "--redact=0 --verbose"),
    ("secrets.yml", "history narrowed"): replace(
        '--log-opts="--text --diff-merges=first-parent HEAD"', "--log-opts=-1"
    ),
    ("secrets.yml", "history without first-parent"): replace(
        "--text --diff-merges=first-parent HEAD", "--text HEAD"
    ),
    ("secrets.yml", "history read without --text"): replace(
        '--log-opts="--text --diff-merges', '--log-opts="--diff-merges'
    ),
    ("secrets.yml", "gitleaks:allow honoured"): replace(
        "          --ignore-gitleaks-allow --redact --verbose --no-banner",
        "          --redact --verbose --no-banner",
    ),
    ("secrets.yml", "shallow checkout"): replace("fetch-depth: 0", "fetch-depth: 1"),
    ("secrets.yml", "token kept"): replace(
        "persist-credentials: false", "persist-credentials: true"
    ),
    ("secrets.yml", "canary not required to fail"): replace(
        'if [ "$status" -ne 1 ]', 'if [ "$status" -ne 0 ]'
    ),
    # The stream scan and text_windows.py are gone; the mutations below are for the redesign.
    ("secrets.yml", "canary file count lowered"): replace('"$found" -ne 3', '"$found" -ne 1'),
    ("secrets.yml", "canary count not checked"): replace(' || [ "$found" -ne 3 ]', ""),
    ("secrets.yml", "canary without the skipped lock-file token"): drop_line("package-lock.json"),
    ("secrets.yml", "canary without the skipped image token"): drop_line("badge.svg"),
    ("secrets.yml", "builder script renamed"): replace(
        "python3 tools/security/gitleaks_config.py", "python3 tools/security/gitleaks_confg.py"
    ),
    ("secrets.yml", "history scan with the repository's config"): replace(
        '--config "$RUNNER_TEMP/gitleaks.toml" .', "--config .gitleaks.toml ."
    ),
    ("secrets.yml", "canary with the repository's config"): replace(
        '--config "$RUNNER_TEMP/gitleaks.toml" --report-format',
        "--config .gitleaks.toml --report-format",
    ),
    ("secrets.yml", "config checksum check removed"): drop_line("GITLEAKS_CONFIG_SHA256}"),
    ("secrets.yml", "config checksum changed"): replace("e163e53b", "f163e53b"),
    ("secrets.yml", "config checksum not required"): replace(
        'echo "${GITLEAKS_CONFIG_SHA256}  ${default}" | sha256sum --check --strict',
        'echo "${GITLEAKS_CONFIG_SHA256}  ${default}" | sha256sum --check',
    ),
    ("secrets.yml", "builder not run"): drop_line("python3 tools/security/gitleaks_config.py"),
    ("secrets.yml", "builder output not written"): replace(
        '            > "$RUNNER_TEMP/gitleaks.toml"\n', "            > /dev/null\n"
    ),
    ("secrets.yml", "default config from the main branch"): replace(
        "gitleaks/v${GITLEAKS_VERSION}/config", "gitleaks/master/config"
    ),
    ("secrets.yml", "config version unpinned"): replace(
        "raw.githubusercontent.com/gitleaks/gitleaks/v${GITLEAKS_VERSION}",
        "raw.githubusercontent.com/gitleaks/gitleaks/v8.30.0",
    ),
    ("secrets.yml", "history scan failure ignored"): replace(
        '--config "$RUNNER_TEMP/gitleaks.toml" .\n',
        '--config "$RUNNER_TEMP/gitleaks.toml" . || true\n',
    ),
    ("secrets.yml", "tag pushes not scanned"): replace('    tags: ["**"]\n', ""),
    ("secrets.yml", "tag pushes limited"): replace('tags: ["**"]', 'tags: ["v*"]'),
    ("secrets.yml", "a merge key"): replace(
        "        with:\n          fetch-depth: 0\n          persist-credentials: false\n",
        "        with:\n          <<: {fetch-depth: 0, persist-credentials: false}\n",
    ),
    ("security.yml", "image probe not as root"): replace(
        "docker run --rm --user 0 --entrypoint python", "docker run --rm --entrypoint python"
    ),
    ("security.yml", "image probe as another user"): replace("--user 0", "--user 1000"),
    ("secrets.yml", ".gitleaksignore guard removed"): replace(
        "find . -name .gitleaksignore", "find . -name .gitleaksignore-none"
    ),
    ("secrets.yml", "download not verified"): drop_line("sha256sum --check"),
    ("secrets.yml", "checksum changed"): replace("551f6fc8", "661f6fc8"),
    ("secrets.yml", "push trigger removed"): replace('  push:\n    branches: ["**"]\n', ""),
    ("secrets.yml", "push limited to main"): replace('branches: ["**"]', "branches: [main]"),
    ("secrets.yml", "schedule removed"): drop_line("cron:"),
    ("secrets.yml", "concurrency added"): after(
        TOP, "concurrency:\n  group: x\n  cancel-in-progress: true\n"
    ),
    ("secrets.yml", "quoted 'if': false on the history scan"): after(
        SCAN_STEP, "        'if': false\n"
    ),
    ("secrets.yml", "quoted 'if' on the job"): after(
        "    name: secret scan\n", "    'if': ${{ false }}\n"
    ),
    ("secrets.yml", "continue-on-error"): after(JOB_FIELDS, "    continue-on-error: true\n"),
    ("secrets.yml", "job can write"): after(JOB_FIELDS, "    permissions: write-all\n"),
    ("secrets.yml", "defaults shell"): after(TOP, "defaults:\n  run:\n    shell: bash {0}\n"),
    ("secrets.yml", "a step removed"): drop_line("      - name: Scan the history for secrets"),
    ("codeql.yml", "a language dropped"): replace(
        "[python, javascript-typescript, actions]", "[python, actions]"
    ),
    ("codeql.yml", "queries"): replace("queries: security-extended", "queries: default"),
    ("codeql.yml", "vendored paths analysed"): drop_line('- "static/vendor/**"'),
    ("codeql.yml", "continue-on-error"): after(JOB_FIELDS, "    continue-on-error: true\n"),
    ("codeql.yml", "job can write contents"): replace(
        "      contents: read # check out the code\n",
        "      contents: write # check out the code\n",
    ),
    ("codeql.yml", "if added"): after(JOB_FIELDS, "    'if': false\n"),
    ("codeql.yml", "build mode"): replace("build-mode: none", "build-mode: autobuild"),
    ("codeql.yml", "pull request trigger removed"): drop_line("  pull_request:"),
    ("codeql.yml", "no longer cancels on a pull request only"): replace(
        "${{ github.event_name == 'pull_request' }}", "true"
    ),
}


def test_the_exact_comparison_accepts_the_real_workflows():
    for name, text in workflow_texts().items():
        if name in EXPECTED_WORKFLOWS:
            assert not exact_workflow_problems(name, text), name


@pytest.mark.parametrize(("name", "label"), WORKFLOW_MUTATIONS, ids=lambda part: part)
def test_the_exact_comparison_rejects_a_changed_workflow(name, label):
    original = read(f"{WORKFLOWS}/{name}")
    mutated = WORKFLOW_MUTATIONS[(name, label)](original)
    assert mutated != original, "mutation did not apply"
    assert exact_workflow_problems(name, mutated), (name, label)


def test_the_comparison_names_the_difference_and_ignores_layout():
    original = read(f"{WORKFLOWS}/security.yml")
    problems = exact_workflow_problems(
        "security.yml", original.replace("name: security", "name: security\n'if': false", 1)
    )
    assert any("unexpected key 'if'" in problem for problem in problems), problems
    reflowed = original.replace('"$SYFT" scan dir:. --config', '"$SYFT"   scan dir:.   --config')
    assert reflowed != original and not exact_workflow_problems("security.yml", reflowed)
    assert not differences({"a": [1, {"b": None}]}, {"a": [1, {"b": None}]})
    assert differences({"a": 1}, {"a": True}) and differences({"a": 0}, {"a": False})
    assert differences({"a": [1]}, {"a": [1, 2]}) and differences({"a": 1, "b": 2}, {"a": 1})


def test_the_normalizer_ignores_layout_and_not_commands():
    assert normalize_script("a \\\n   b   c\n\n  d\n") == "a b c\nd"
    assert normalize_script("a b\nc") != normalize_script("a b c")


def test_a_pin_comment_that_names_no_release_is_reported_for_the_real_workflows():
    original = read(f"{WORKFLOWS}/codeql.yml")
    assert not unpinned_uses(original)
    assert unpinned_uses(original.replace(" # v4.39.3", "", 1))
    assert unpinned_uses(original.replace("@33d7b85da1a04171e211befe2c739303b9a4c066", "@v4", 1))


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


def test_the_documented_local_checksum_is_the_pinned_one():
    """CONTRIBUTING's local secret scan checks the downloaded default configuration against the
    same SHA-256 the workflow pins, so the two cannot drift apart."""
    pinned = re.search(
        r"GITLEAKS_CONFIG_SHA256:\s*([0-9a-f]{64})", read(f"{WORKFLOWS}/secrets.yml")
    ).group(1)
    documented = re.findall(
        r"\b([0-9a-f]{64})  build/gitleaks-default\.toml", read("CONTRIBUTING.md")
    )
    assert documented == [pinned], documented
