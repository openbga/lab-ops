"""Print, as JSON, what the application image contains, for sbom_coverage.py --image.

It runs inside the image with the image's own Python and reads the package databases directly,
so the listing does not depend on the SBOM tool it is used to check. Standard library only.

    docker run --rm --entrypoint python -v "$PWD/tools/security:/probe:ro" IMAGE \\
        /probe/image_contents.py > image-contents.json
"""

import json
import platform
import sys
import sysconfig
from importlib import metadata
from pathlib import Path


def os_release(path=Path("/etc/os-release")):
    """{'id': ..., 'version_id': ...} from os-release."""
    fields = {}
    for line in path.read_text().splitlines():
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip().strip('"')
    return {"id": fields.get("ID", ""), "version_id": fields.get("VERSION_ID", "")}


def deb_packages(path=Path("/var/lib/dpkg/status")):
    """{name: [versions]} of the Debian packages dpkg records as installed."""
    found = {}
    for paragraph in path.read_text().split("\n\n"):
        fields = {}
        for line in paragraph.splitlines():
            if line and not line[0].isspace():
                key, _, value = line.partition(":")
                fields[key] = value.strip()
        if fields.get("Package") and fields.get("Status", "").endswith(" installed"):
            found.setdefault(fields["Package"], set()).add(fields["Version"])
    return {name: sorted(versions) for name, versions in found.items()}


def python_packages():
    """{name: [versions]} installed in the interpreter's environment and its base installation."""
    paths = []
    for base in (sys.prefix, sys.base_prefix):
        scheme = sysconfig.get_paths(vars={"base": base, "platbase": base})
        paths += [scheme["purelib"], scheme["platlib"]]
    found = {}
    for distribution in metadata.distributions(path=list(dict.fromkeys(paths))):
        name = distribution.metadata["Name"]
        if name:
            found.setdefault(name, set()).add(distribution.version)
    return {name: sorted(versions) for name, versions in found.items()}


def main():
    contents = {
        "os": os_release(),
        "python": platform.python_version(),
        "deb": deb_packages(),
        "pypi": python_packages(),
    }
    json.dump(contents, sys.stdout, indent=1, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
