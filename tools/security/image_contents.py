"""Print, as JSON, what the application image contains, for sbom_coverage.py --image.

It runs inside the image with the image's own Python and reads the package databases directly,
so the listing does not depend on the SBOM tool it is used to check. Standard library only.

    docker run --rm --user 0 --entrypoint python -v "$PWD/tools/security:/probe:ro" IMAGE \\
        /probe/image_contents.py > image-contents.json
"""

import hashlib
import json
import os
import platform
import re
import stat
import sys
import sysconfig
from importlib import metadata
from pathlib import Path

PSEUDO_FILESYSTEMS = ("/proc", "/sys", "/dev")
SHARED_OBJECT = re.compile(r"\.so(\.|$)")


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
    """{real path: recorded MD5 or None} for every file the Debian packages installed."""
    owned = {}
    for listing in info.glob("*.list"):
        for line in listing.read_text(errors="replace").splitlines():
            if line.strip() and os.path.lexists(line.strip()):
                owned.setdefault(os.path.realpath(line.strip()), None)
    for sums in info.glob("*.md5sums"):
        for line in sums.read_text(errors="replace").splitlines():
            digest, _, relative = line.partition("  ")
            if relative:
                path = "/" + relative.strip()
                if os.path.lexists(path):
                    owned[os.path.realpath(path)] = digest.strip()
    return owned


def _md5(path):
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as content:
        for block in iter(lambda: content.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def unpackaged_programs(root, owned, skip=PSEUDO_FILESYSTEMS):
    """The paths of the executable programs (ELF files with an execute bit, not shared objects)
    anywhere under `root` that no Debian package installed as they are now, such as a tool the
    Dockerfile copies in, Python built from source, or a binary copied over a packaged file (its
    content no longer matches the MD5 dpkg recorded). `owned` maps a packaged file's real path to
    that MD5. Run as root, so that no directory is unreadable."""
    found = []
    for top, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if os.path.join(top, d) not in skip)
        for name in sorted(files):
            path = os.path.join(top, name)
            try:
                info = os.lstat(path)
                if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
                    continue
                if SHARED_OBJECT.search(name):
                    continue
                with open(path, "rb") as program:
                    if program.read(4) != b"\x7fELF":
                        continue
                real = os.path.realpath(path)
                if real in owned and (owned[real] is None or owned[real] == _md5(path)):
                    continue
                found.append(path)
            except OSError:
                continue
    return found


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
        "programs": unpackaged_programs("/", dpkg_files()),
    }
    json.dump(contents, sys.stdout, indent=1, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
