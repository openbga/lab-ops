"""Check that a software bill of materials lists everything a vulnerability scan must cover.

A scanner that cannot read a lockfile or a package database finds nothing to match and reports
no vulnerabilities, so the security workflow runs this on each SBOM before trusting a clean scan
of it (SECURITY.md, "What is checked"). Standard library only.

    python3 tools/security/sbom_coverage.py SBOM --locked
        every package version in uv.lock and static/vendor/package-lock.json, and every action
        the workflows pin, at the release its comment names
    python3 tools/security/sbom_coverage.py SBOM --image CONTENTS
        everything image_contents.py found in the image: the operating system and its packages,
        Python, every installed Python package and every program no Debian package installed;
        the project's runtime dependencies among them; and the vendored browser packages

SBOM is a CycloneDX JSON file. Exit status 1 lists what is missing.
"""

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_action_pins  # noqa: E402  (a sibling script, standard library only)

ROOT = Path(__file__).resolve().parents[2]
PURL = re.compile(r"^pkg:(?P<type>[\w.+-]+)/(?P<path>[^@?#]+)@(?P<version>[^?#]+)")


def normalize(name):
    """A Python package name as PEP 503 compares it."""
    return re.sub(r"[-_.]+", "-", name).lower()


def sbom_packages(sbom):
    """{(ecosystem, name): {versions}} from the package URLs of a CycloneDX document. Debian
    packages are named without their namespace, npm packages with their scope."""
    found = {}
    for component in sbom.get("components", []):
        match = PURL.match(component.get("purl") or "")
        if not match:
            continue
        kind, path = match["type"], unquote(match["path"])
        if kind == "pypi":
            name = normalize(path)
        elif kind == "deb":
            name = path.rsplit("/", 1)[-1]
        elif kind in ("npm", "generic", "github"):
            name = path
        else:
            continue
        found.setdefault((kind, name), set()).add(unquote(match["version"]))
    return found


def locations(sbom):
    """Every file path an SBOM component was found at."""
    found = set()
    for component in sbom.get("components", []):
        for prop in component.get("properties", []):
            if re.fullmatch(r"syft:location:\d+:path", prop.get("name", "")):
                found.add(prop.get("value", ""))
    return found


def pinned_actions(texts):
    """({owner/repo: {release tags}}, problems) for the actions the given workflows pin."""
    actions, problems = {}, []
    for name, text in texts.items():
        found, unreadable = check_action_pins.pins(text, name)
        problems += unreadable
        for _, action, _, tag in found:
            actions.setdefault(check_action_pins.repository(action), set()).add(tag)
    return actions, problems


def operating_systems(sbom):
    """(name, version) of each operating-system component."""
    return {
        (component.get("name", ""), component.get("version", ""))
        for component in sbom.get("components", [])
        if component.get("type") == "operating-system"
    }


def locked_python(lock):
    """{name: {versions}} for every package in a parsed uv.lock except the project itself. A
    name can have several versions when the resolution differs by platform or Python version."""
    found = {}
    for package in lock.get("package", []):
        if not {"editable", "virtual"} & set(package.get("source", {})):
            found.setdefault(normalize(package["name"]), set()).add(package["version"])
    return found


def runtime_names(pyproject):
    """The project's direct runtime dependencies, without extras or version specifiers."""
    return {
        normalize(re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement).group(1))
        for requirement in pyproject["project"]["dependencies"]
    }


def vendored(lock):
    """{name: {version}} for each package recorded in static/vendor/package-lock.json."""
    return {
        key.removeprefix("node_modules/"): {entry["version"]}
        for key, entry in lock.get("packages", {}).items()
        if key
    }


def missing(found, ecosystem, expected):
    """Lines naming each expected (name, version) the SBOM does not list."""
    lines = []
    for name, versions in sorted(expected.items()):
        listed = found.get((ecosystem, name), set())
        for version in sorted(versions):
            if not listed:
                lines.append(f"{ecosystem} {name} {version}: not in the SBOM")
            elif version not in listed:
                shown = ", ".join(sorted(listed))
                lines.append(f"{ecosystem} {name} {version}: the SBOM lists {shown}")
    return lines


def check_locked(sbom, uv_lock, vendor_lock, workflows):
    """(problems, expected count): every locked and vendored package version, and every pinned
    action, is listed. `workflows` maps a workflow's name to its text."""
    found = sbom_packages(sbom)
    python, browser = locked_python(uv_lock), vendored(vendor_lock)
    actions, problems = pinned_actions(workflows)
    problems += missing(found, "pypi", python) + missing(found, "npm", browser)
    problems += missing(found, "github", actions)
    expected = sum(len(v) for v in (*python.values(), *actions.values())) + len(browser)
    return problems, expected


def check_image(sbom, contents, pyproject, vendor_lock):
    """(problems, expected count): everything the image holds is listed."""
    deb = {name: set(versions) for name, versions in contents.get("deb", {}).items()}
    pypi = {}
    for name, versions in contents.get("pypi", {}).items():
        pypi.setdefault(normalize(name), set()).update(versions)
    system = contents.get("os", {})
    python = contents.get("python", "")
    programs = contents.get("programs")
    if not (deb and pypi and python and system.get("id") and system.get("version_id")) or (
        programs is None
    ):
        return [
            "the image listing is incomplete: it must name the operating system, Python, the "
            "Debian and Python packages and the programs no Debian package installed"
        ], 0
    problems = []
    absent = sorted(runtime_names(pyproject) - set(pypi))
    if absent:
        problems.append(f"runtime dependencies not installed in the image: {absent}")
    found = sbom_packages(sbom)
    browser = vendored(vendor_lock)
    problems += missing(found, "deb", deb)
    problems += missing(found, "pypi", pypi)
    problems += missing(found, "generic", {"python": {python}})
    problems += missing(found, "npm", browser)
    located = locations(sbom)
    problems += [
        f"program {path}: no SBOM entry was found in it" for path in programs if path not in located
    ]
    name, release = system["id"], system["version_id"]
    if not any(
        os_name == name and (version == release or version.startswith(release + "."))
        for os_name, version in operating_systems(sbom)
    ):
        problems.append(f"operating system {name} {release}: not in the SBOM")
    expected = sum(len(v) for v in deb.values()) + sum(len(v) for v in pypi.values())
    return problems, expected + 2 + len(browser) + len(programs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sbom", type=Path, help="CycloneDX JSON file")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--locked", action="store_true", help="every locked package")
    mode.add_argument("--image", type=Path, metavar="CONTENTS", help="image_contents.py output")
    args = parser.parse_args(argv)
    sbom = json.loads(args.sbom.read_text())
    vendor_lock = json.loads((ROOT / "static/vendor/package-lock.json").read_text())
    if args.locked:
        uv_lock = tomllib.loads((ROOT / "uv.lock").read_text())
        workflows = {
            str(path.relative_to(ROOT)): path.read_text()
            for path in check_action_pins.workflow_files(ROOT)
        }
        problems, expected = check_locked(sbom, uv_lock, vendor_lock, workflows)
    else:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
        contents = json.loads(args.image.read_text())
        problems, expected = check_image(sbom, contents, pyproject, vendor_lock)
    if problems:
        print(f"{args.sbom} does not list {len(problems)} of {expected} expected entries:")
        print("\n".join(f"  {line}" for line in problems))
        return 1
    print(f"{args.sbom} lists all {expected} expected entries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
