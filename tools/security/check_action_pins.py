"""Check that each action the workflows pin to a commit is the release its comment names.

The workflows and any local composite actions they run (action.yml files) are read.

The dependency audit reads an action's version from the `# vX.Y.Z` comment after its commit
pin, so a stale or wrong comment would audit one release while another runs. This resolves each
comment's tag on GitHub and fails when it does not point at the pinned commit (SECURITY.md, "What
is checked"). A tag moved after pinning fails too. Standard library only; it reads GITHUB_TOKEN
when set.

    python3 tools/security/check_action_pins.py
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
USES = re.compile(
    r"^\s*(?:-\s+)?uses:\s*(?P<repo>[\w.-]+/[\w.-]+)(?P<path>/[\w./-]+)?@(?P<sha>[0-9a-f]{40})"
    r"\s+#\s*(?P<tag>v\d[\w.+-]*)\s*$"
)
LOCAL = re.compile(r"^\s*(?:-\s+)?uses:\s*\./[\w./-]+\s*$")
# `uses` as a key in any layout YAML allows: bare or quoted, at the start of a line (after a
# dash) or inside a flow mapping, with its value on the same line or the next. Each one must be
# in the single-line form above.
ANY_USES = re.compile(r"""^\s*(?:-\s+)?["']?uses["']?\s*:|[{,]\s*["']?uses["']?\s*:""")
READ_AS = "write it as `uses: owner/repo@<40-hex commit> # vX.Y.Z` on one line"


def workflow_files(root=None):
    """Every workflow GitHub runs: .yml and .yaml files in .github/workflows."""
    folder = (root or ROOT) / ".github/workflows"
    return sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")])


SKIPPED_DIRS = {".git", ".venv", "node_modules", "build", "staticfiles", ".local"}


def action_files(root=None):
    """The workflows and every action definition (action.yml or action.yaml) in the repository: a
    workflow can run a local composite action, and the actions that one uses run in the job."""
    root = root or ROOT
    found = list(workflow_files(root))
    for path in sorted([*root.rglob("action.yml"), *root.rglob("action.yaml")]):
        if not SKIPPED_DIRS & set(path.relative_to(root).parts[:-1]):
            found.append(path)
    return found


def _code(line):
    """The line without a trailing comment (a `#` after whitespace)."""
    return re.split(r"\s#", line, maxsplit=1)[0]


def pins(text, name="workflow"):
    """[(location, repo, sha, tag)] for each pinned action, and a problem for every other line
    that uses `uses` as a key. Failing closed keeps a layout this cannot read from going
    unchecked. Local actions (`./...`) are allowed."""
    found, unreadable = [], []
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#") or not ANY_USES.search(_code(line)):
            continue
        match = USES.match(line)
        if match:
            repo = match["repo"] + (match["path"] or "")
            found.append((f"{name}:{number}", repo, match["sha"], match["tag"]))
        elif not LOCAL.match(line):
            unreadable.append(f"{name}:{number}: {READ_AS}")
    return found, unreadable


def repository(action):
    """owner/repo of an action reference that may name a path inside the repository."""
    return "/".join(action.split("/")[:2])


def github(path, token=None):
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def tag_commit(repo, tag, fetch=github):
    """The commit a tag points at, following annotated tags."""
    obj = fetch(f"/repos/{repo}/git/ref/tags/{tag}")["object"]
    for _ in range(5):
        if obj["type"] != "tag":
            return obj["sha"]
        obj = fetch(f"/repos/{repo}/git/tags/{obj['sha']}")["object"]
    raise ValueError(f"{repo} {tag}: too many nested tags")


def problems(pinned, resolve):
    """Lines naming each pin whose comment's tag is not the pinned commit."""
    lines, cache = [], {}
    for location, action, sha, tag in pinned:
        repo = repository(action)
        key = (repo, tag)
        if key not in cache:
            try:
                cache[key] = resolve(repo, tag)
            except (urllib.error.URLError, KeyError, ValueError) as error:
                cache[key] = f"unresolved ({error})"
        if cache[key] != sha:
            lines.append(
                f"{location}: {action}@{sha[:12]} is commented {tag}, which is {cache[key]}"
            )
    return lines


def main():
    token = os.environ.get("GITHUB_TOKEN")
    pinned, lines = [], []
    for path in action_files():
        found, unreadable = pins(path.read_text(), path.relative_to(ROOT))
        pinned += found
        lines += unreadable
    lines += problems(pinned, lambda repo, tag: tag_commit(repo, tag, lambda p: github(p, token)))
    if lines:
        print("Action pins that do not match their version comments:", *lines, sep="\n  ")
        return 1
    print(f"All {len(pinned)} action pins match the release their comment names.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
