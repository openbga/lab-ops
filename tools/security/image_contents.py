"""Print, as JSON, what the application image contains, for sbom_coverage.py --image.

It runs inside the image with the image's own Python and reads the package databases directly,
so the listing does not depend on the SBOM tool it is used to check. Standard library only.

    docker run --rm --entrypoint python -v "$PWD/tools/security:/probe:ro" IMAGE \\
        /probe/image_contents.py > image-contents.json
"""

import json
import os
import platform
import sys
import sysconfig
from importlib import metadata
from pathlib import Path

PROGRAM_DIRS = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")


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


def dpkg_files(info=Path("/var/lib/dpkg/info")):
    """The real paths of every file the Debian packages installed."""
    owned = set()
    for listing in info.glob("*.list"):
        for line in listing.read_text(errors="replace").splitlines():
            if line.strip() and os.path.lexists(line.strip()):
                owned.add(os.path.realpath(line.strip()))
    return owned


def unpackaged_programs(directories, owned):
    """The real paths of the executable programs (ELF files) in `directories` that no Debian
    package installed, such as a tool the Dockerfile copies in or Python built from source."""
    found = set()
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        for entry in os.listdir(directory):
            path = os.path.realpath(os.path.join(directory, entry))
            if path in owned or not os.path.isfile(path):
                continue
            with open(path, "rb") as program:
                if program.read(4) == b"\x7fELF":
                    found.add(path)
    return sorted(found)


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
    directories = [*os.environ.get("PATH", "").split(os.pathsep), *PROGRAM_DIRS]
    contents = {
        "os": os_release(),
        "python": platform.python_version(),
        "deb": deb_packages(),
        "pypi": python_packages(),
        "programs": unpackaged_programs(dict.fromkeys(filter(None, directories)), dpkg_files()),
    }
    json.dump(contents, sys.stdout, indent=1, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
