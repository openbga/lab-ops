"""Build the secret scan's gitleaks configuration: the default rules and allowlist without the
default allowlist's list of paths, plus the exact values in this repository's .gitleaks.toml.

gitleaks' default configuration never scans some files, chosen by name: every package-lock.json,
images (SVG included), fonts, some third-party scripts and any path containing gitleaks.toml. A
repository's own configuration can extend the default but cannot remove those paths, so the
secret scan builds its configuration from the default file of the pinned gitleaks release with
that list removed, and reads every file. The few exceptions individual default rules carry for
named files are kept. Nothing is written unless the result parses and holds the same rules and
the same allowlist apart from the removed paths. Standard library only.

    python3 tools/security/gitleaks_config.py DEFAULT_TOML .gitleaks.toml > gitleaks.toml
"""

import sys
import tomllib


def build(default_text, project_text):
    """The combined configuration's text; ValueError when either input is not as expected."""
    lines = default_text.splitlines()
    headers = [index for index, line in enumerate(lines) if line.startswith("[")]
    starts = [index for index in headers if lines[index].strip() == "[allowlist]"]
    if len(starts) != 1:
        raise ValueError("expected exactly one [allowlist] table in the default configuration")
    start = starts[0]
    end = next((index for index in headers if index > start), len(lines))
    kept, skipping = [], False
    for line in lines[start + 1 : end]:
        if line.startswith("paths = ["):
            skipping = not line.rstrip().endswith("]")
            continue
        if skipping:
            skipping = line.strip() != "]"
            continue
        kept.append(line)
    project = project_text.splitlines()
    first = next((i for i, line in enumerate(project) if line.strip() == "[[allowlists]]"), None)
    if first is None:
        raise ValueError(".gitleaks.toml has no [[allowlists]] table")
    combined = [*lines[:start], "[[allowlists]]", *kept, *lines[end:], "", *project[first:]]
    text = "\n".join(combined) + "\n"
    check(text, tomllib.loads(default_text), tomllib.loads(project_text))
    return text


def check(text, default, project):
    """Raise ValueError unless `text` holds the default rules and allowlist without its paths,
    followed by the project's allowlists, and nothing else."""
    built = tomllib.loads(text)
    problems = []
    if set(built) != {"title", "minVersion", "allowlists", "rules"}:
        problems.append(f"unexpected top-level keys {sorted(built)}")
    if built.get("rules") != default.get("rules"):
        problems.append("the rules differ from the default configuration's")
    lists = built.get("allowlists", [])
    expected = {key: value for key, value in default.get("allowlist", {}).items() if key != "paths"}
    if not lists or lists[0] != expected:
        problems.append("the first allowlist is not the default one without its paths")
    if lists[1:] != project.get("allowlists"):
        problems.append("the remaining allowlists are not the ones in .gitleaks.toml")
    if any("paths" in allowlist for allowlist in lists):
        problems.append("an allowlist still lists paths")
    if problems:
        raise ValueError("; ".join(problems))


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print(__doc__.split("\n\n")[-1].strip(), file=sys.stderr)
        return 2
    with open(args[0]) as default, open(args[1]) as project:
        sys.stdout.write(build(default.read(), project.read()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
