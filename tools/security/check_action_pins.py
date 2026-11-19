"""Check that each action the workflows pin to a commit is the release its comment names.

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
    r"^\s*(?:-\s+)?uses:\s*(?P<repo>[\w.-]+/[\w.-]+)(?:/[\w./-]+)?@(?P<sha>[0-9a-f]{40})"
    r"\s+#\s*(?P<tag>v\d[\w.+-]*)\s*$"
)
ANY_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)")


def pins(text, name="workflow"):
    """[(location, repo, sha, tag)] for each pinned action, and the `uses:` lines not in that
    form (other than local actions)."""
    found, unreadable = [], []
    for number, line in enumerate(text.splitlines(), 1):
        if not ANY_USES.match(line) or ANY_USES.match(line)["ref"].startswith("./"):
            continue
        match = USES.match(line)
        if match:
            found.append((f"{name}:{number}", match["repo"], match["sha"], match["tag"]))
        else:
            unreadable.append(f"{name}:{number}: not a commit pin with a version comment")
    return found, unreadable


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
    for location, repo, sha, tag in pinned:
        key = (repo, tag)
        if key not in cache:
            try:
                cache[key] = resolve(repo, tag)
            except (urllib.error.URLError, KeyError, ValueError) as error:
                cache[key] = f"unresolved ({error})"
        if cache[key] != sha:
            lines.append(f"{location}: {repo}@{sha[:12]} is commented {tag}, which is {cache[key]}")
    return lines


def main():
    token = os.environ.get("GITHUB_TOKEN")
    pinned, lines = [], []
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
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
