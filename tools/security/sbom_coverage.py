"""Check that a software bill of materials lists the packages a vulnerability scan must cover.

A scanner that cannot read a lockfile finds nothing to match and reports no vulnerabilities, so
the security workflow runs this on each SBOM before trusting a clean scan of it
(SECURITY.md, "What is checked"). Standard library only.

    python3 tools/security/sbom_coverage.py SBOM --locked    # every package in uv.lock
    python3 tools/security/sbom_coverage.py SBOM --runtime   # the project's runtime dependencies

Both modes also require every package in static/vendor/package-lock.json. SBOM is a CycloneDX
JSON file. Exit status 1 lists what is missing.
"""

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]


def normalize(name):
    """A Python package name as PEP 503 compares it."""
    return re.sub(r"[-_.]+", "-", name).lower()


def sbom_packages(sbom):
    """{(ecosystem, name): {versions}} from the package URLs of a CycloneDX document."""
    found = {}
    for component in sbom.get("components", []):
        match = re.match(
            r"^pkg:(?P<type>[\w.+-]+)/(?P<path>[^@?#]+)@(?P<version>[^?#]+)",
            (component.get("purl") or ""),
        )
        if not match or match["type"] not in ("pypi", "npm"):
            continue
        name = unquote(match["path"])
        if match["type"] == "pypi":
            name = normalize(name)
        found.setdefault((match["type"], name), set()).add(unquote(match["version"]))
    return found


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


def missing(found, ecosystem, expected, every_version=True):
    """Lines naming what the SBOM lacks. With every_version, each expected version must be
    listed; otherwise one of them is enough (an image installs one version per platform)."""
    lines = []
    for name, versions in sorted(expected.items()):
        listed = found.get((ecosystem, name), set())
        wanted = sorted(versions)
        if not listed:
            lines.append(f"{ecosystem} {name} {' or '.join(wanted)}: not in the SBOM")
        elif every_version:
            lines.extend(
                f"{ecosystem} {name} {version}: the SBOM lists {', '.join(sorted(listed))}"
                for version in wanted
                if version not in listed
            )
        elif not versions & listed:
            shown = ", ".join(sorted(listed))
            lines.append(f"{ecosystem} {name} {' or '.join(wanted)}: the SBOM lists {shown}")
    return lines


def check(sbom, mode, uv_lock, pyproject, vendor_lock):
    """(problems, expected count) for an SBOM in mode 'locked' or 'runtime'."""
    found = sbom_packages(sbom)
    python = locked_python(uv_lock)
    browser = vendored(vendor_lock)
    if mode == "runtime":
        names = runtime_names(pyproject)
        absent = sorted(names - set(python))
        if absent:
            return [f"runtime dependencies missing from uv.lock: {absent}"], 0
        python = {name: python[name] for name in names}
        problems = missing(found, "pypi", python, every_version=False)
        expected = len(python)
    else:
        problems = missing(found, "pypi", python)
        expected = sum(len(versions) for versions in python.values())
    problems += missing(found, "npm", browser)
    return problems, expected + len(browser)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sbom", type=Path, help="CycloneDX JSON file")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--locked", action="store_const", const="locked", dest="mode")
    mode.add_argument("--runtime", action="store_const", const="runtime", dest="mode")
    args = parser.parse_args(argv)
    problems, expected = check(
        json.loads(args.sbom.read_text()),
        args.mode,
        tomllib.loads((ROOT / "uv.lock").read_text()),
        tomllib.loads((ROOT / "pyproject.toml").read_text()),
        json.loads((ROOT / "static/vendor/package-lock.json").read_text()),
    )
    if problems:
        print(f"{args.sbom} does not list {len(problems)} of {expected} packages:")
        print("\n".join(f"  {line}" for line in problems))
        return 1
    print(f"{args.sbom} lists all {expected} packages expected ({args.mode}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
