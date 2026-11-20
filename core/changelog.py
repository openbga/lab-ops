"""What's new: the dated list of changes, read from one entry file per change in changelog.d/.

Each changelog.d/YYYY-MM-DD-slug.md file has flat `key: value` front matter between `---` lines
(date, type, impact, title; optional area, link, pr, highlight), then a plain-text summary for
people who use the application and an optional `## Technical notes` section for deployers. See the
`changelog` skill (.agents/skills/changelog/SKILL.md).

The same entries feed CHANGELOG.md (`manage.py changelog` writes it, `--check` fails when it is
stale) and the What's new page (core.views.whats_new). The deployment has no version tags, so
entries are grouped by calendar month, newest first, then by impact.

"Seen" is the per-user preference `whats_new.seen` (core.preferences): the newest entry date the
user has seen and the ids of the entries in the window before it, so an entry that merges late
with an earlier date still counts as new. An account without a mark has nothing new: the first
shell it renders records the newest entries as seen. No database migration, no network request.
"""

import datetime
import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRIES_DIR = ROOT / "changelog.d"
CHANGELOG = ROOT / "CHANGELOG.md"

# Keep a Changelog 1.1.0 vocabulary, https://keepachangelog.com/en/1.1.0/
TYPES = {
    "added": "Added",
    "changed": "Changed",
    "fixed": "Fixed",
    "removed": "Removed",
    "deprecated": "Deprecated",
    "security": "Security",
}
IMPACTS = {"high": "High impact", "medium": "Medium impact", "low": "Low impact"}
REQUIRED = ("date", "type", "impact", "title")
OPTIONAL = ("area", "link", "pr", "highlight")
# The slug is at most 80 characters, as an entry id (ID) and a stored seen mark allow.
FILE_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}-(?=[a-z0-9-]{1,80}\.md$)[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
ID = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9-]{1,80}$")
LINK = re.compile(r"^[a-z0-9_-]+(?::[a-z0-9_-]+)*$")  # a Django route name without arguments
NOTES_HEADING = "## Technical notes"
TITLE_MAX = 120
# An entry dated less than this long before the newest one a user has seen still counts as new if
# the user has not seen it (it merged later than its date). Bounds the stored list of ids: with
# more than SEEN_IDS_MAX entries inside the window, the earliest published beyond SEEN_IDS_MAX
# stay unread after a visit.
SEEN_WINDOW = datetime.timedelta(days=60)
SEEN_IDS_MAX = 400


class ChangelogError(ValueError):
    """An entry file that cannot be read; the message names the file and the key."""


@dataclass(frozen=True)
class Entry:
    id: str  # the file name without .md
    date: datetime.date
    type: str
    impact: str
    title: str
    summary: str
    notes: str = ""
    area: str = ""
    link: str = ""
    pr: int | None = None
    highlight: bool = False

    @property
    def type_label(self):
        return TYPES[self.type]


@dataclass(frozen=True)
class Group:
    label: str  # "November 2026"
    start: datetime.date
    end: datetime.date
    sections: tuple  # ((impact, impact label, (Entry, ...)), ...), impact order, empty ones out

    @property
    def count(self):
        return sum(len(entries) for _, _, entries in self.sections)


def parse_entry(path):
    path = Path(path)
    name = path.name
    if not FILE_NAME.match(name):
        raise ChangelogError(f"{name}: the file name must be YYYY-MM-DD-short-slug.md")
    try:
        datetime.date.fromisoformat(name[:10])
    except ValueError:
        raise ChangelogError(f"{name}: the file name must start with a real date") from None
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ChangelogError(f"{name}: must start with a '---' front matter line")
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        raise ChangelogError(f"{name}: the front matter has no closing '---' line") from None
    fields = {}
    for number, line in enumerate(lines[1:end], 2):
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            raise ChangelogError(f"{name}: line {number} is not 'key: value'")
        if key not in REQUIRED + OPTIONAL:
            raise ChangelogError(f"{name}: unknown key '{key}'")
        if key in fields:
            raise ChangelogError(f"{name}: key '{key}' appears twice")
        fields[key] = value
    for key in REQUIRED:
        if not fields.get(key):
            raise ChangelogError(f"{name}: missing required key '{key}'")
    try:
        date = datetime.date.fromisoformat(fields["date"])
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields["date"]):
            raise ValueError
    except ValueError:
        raise ChangelogError(f"{name}: key 'date' must be a date as YYYY-MM-DD") from None
    if fields["type"] not in TYPES:
        raise ChangelogError(f"{name}: key 'type' must be one of {', '.join(TYPES)}")
    if fields["impact"] not in IMPACTS:
        raise ChangelogError(f"{name}: key 'impact' must be one of {', '.join(IMPACTS)}")
    title = fields["title"]
    if title.endswith(".") or len(title) > TITLE_MAX:
        raise ChangelogError(
            f"{name}: key 'title' must be at most {TITLE_MAX} characters, without a trailing period"
        )
    pr = None
    if "pr" in fields:
        if not re.fullmatch(r"[1-9]\d{0,6}", fields["pr"]):
            raise ChangelogError(f"{name}: key 'pr' must be a pull request number")
        pr = int(fields["pr"])
    link = fields.get("link", "")
    if link and not LINK.match(link):
        raise ChangelogError(f"{name}: key 'link' must be a route name such as clients:client-list")
    if "area" in fields and not fields["area"]:
        raise ChangelogError(f"{name}: key 'area' is empty; leave the key out instead")
    if fields.get("highlight", "false") not in {"true", "false"}:
        raise ChangelogError(f"{name}: key 'highlight' must be true or false")
    body = "\n".join(lines[end + 1 :])
    summary, _, notes = body.partition(NOTES_HEADING)
    summary, notes = summary.strip(), notes.strip()
    if not summary:
        raise ChangelogError(f"{name}: missing the summary under the front matter")
    if summary.startswith("#") or "\n#" in summary:
        raise ChangelogError(
            f"{name}: the summary has a heading; only '{NOTES_HEADING}' may follow it"
        )
    return Entry(
        id=name.removesuffix(".md"),
        date=date,
        type=fields["type"],
        impact=fields["impact"],
        title=title,
        summary=summary,
        notes=notes,
        area=fields.get("area", ""),
        link=link,
        pr=pr,
        highlight=fields.get("highlight") == "true",
    )


def sort_key(entry):
    """Newest first, then by title (then id, so the order never depends on the file system)."""
    return (-entry.date.toordinal(), entry.title.casefold(), entry.id)


def read_entries(directory=None):
    directory = Path(directory or ENTRIES_DIR)
    paths = sorted(directory.glob("*.md")) if directory.is_dir() else []
    entries = [parse_entry(path) for path in paths if path.name.lower() != "readme.md"]
    return sorted(entries, key=sort_key)


@lru_cache(maxsize=1)
def _cached_entries():
    return tuple(read_entries())


def entries():
    """The application's entries. Authors see a new file on refresh; production reads once."""
    from django.conf import settings

    return tuple(read_entries()) if settings.DEBUG else _cached_entries()


def group_entries(entries):
    """Calendar-month groups, newest first; inside, high to low impact, each newest first."""
    months = {}
    for entry in sorted(entries, key=sort_key):
        months.setdefault((entry.date.year, entry.date.month), []).append(entry)
    groups = []
    for (year, month), items in months.items():
        sections = tuple(
            (impact, label, tuple(e for e in items if e.impact == impact))
            for impact, label in IMPACTS.items()
            if any(e.impact == impact for e in items)
        )
        groups.append(
            Group(
                label=datetime.date(year, month, 1).strftime("%B %Y"),
                start=min(e.date for e in items),
                end=max(e.date for e in items),
                sections=sections,
            )
        )
    return groups


# ------------------------------------------------------------------ CHANGELOG.md


def repository_url():
    with (ROOT / "pyproject.toml").open("rb") as file:
        return tomllib.load(file)["project"]["urls"]["Homepage"].rstrip("/")


def _indent(text, prefix="  "):
    return "\n".join(prefix + line if line.strip() else "" for line in text.split("\n"))


def render_markdown(entries, repository=None):
    repository = repository if repository is not None else repository_url()
    out = [
        "<!-- Generated from changelog.d/ by `uv run python manage.py changelog`. "
        "Do not edit this file: add or change an entry file in changelog.d/ and regenerate. -->",
        "",
        "# Changelog",
        "",
        "Changes to OpenBGA Lab Ops, grouped by month and impact.",
    ]
    for group in group_entries(entries):
        if group.start == group.end:
            span = group.start.isoformat()
        else:
            span = f"{group.start.isoformat()} to {group.end.isoformat()}"
        out += ["", f"## {group.label} ({span})"]
        for _, label, items in group.sections:
            out += ["", f"### {label}", ""]
            for entry in items:
                facts = [entry.date.isoformat()]
                if entry.area:
                    facts.append(entry.area)
                if entry.pr:
                    facts.append(f"[#{entry.pr}]({repository}/pull/{entry.pr})")
                out.append(f"- **{entry.type_label}:** {entry.title} ({', '.join(facts)})")
                out += ["", _indent(entry.summary)]
                if entry.notes:
                    out += [
                        "",
                        "  <details><summary>Technical notes</summary>",
                        "",
                        _indent(entry.notes),
                        "",
                        "  </details>",
                    ]
                out.append("")
            if out[-1] == "":
                out.pop()
    if not entries:
        out += ["", "No changes are recorded yet."]
    return "\n".join(out) + "\n"


def stale(entries, path=None):
    """True when the committed document differs from what the entries generate."""
    path = Path(path or CHANGELOG)
    return not path.is_file() or path.read_text(encoding="utf-8") != render_markdown(entries)


# ------------------------------------------------------------------ unread indicator


def seen_marker(entries):
    """What to remember when a user opens What's new: the newest date and the ids in the window
    before it, the most recently published first when there are more than SEEN_IDS_MAX, kept in id
    order. None when there are no entries."""
    if not entries:
        return None
    newest = max(entry.date for entry in entries)
    recent = sorted(
        (entry for entry in entries if entry.date > newest - SEEN_WINDOW),
        key=lambda entry: (entry.date, entry.id),
        reverse=True,
    )
    ids = sorted((entry.id for entry in recent[:SEEN_IDS_MAX]), reverse=True)
    return {"date": newest.isoformat(), "ids": ids}


def combined_marker(stored, seen, entries=()):
    """What to remember after a visit that saw `seen` (seen_marker of `entries`) when `stored` is
    the mark kept already, from another tab or a process of another release whose entries differ:
    every entry either marks as seen stays seen, within SEEN_IDS_MAX. The newer date; the ids of
    the mark with that date first (this visit's on a tie), then the other's, each once, without
    those this visit's entries date outside the window before the newer date, in seen_marker's
    order. An id this visit does not know (another release's entry) is kept: its published date is
    unknown here, and a file name's date can differ from it.
    """
    if not valid_marker(stored):
        return seen
    if not valid_marker(seen):
        return stored
    newer, older = (stored, seen) if stored["date"] > seen["date"] else (seen, stored)
    published = {entry.id: entry.date for entry in entries}
    since = datetime.date.fromisoformat(newer["date"]) - SEEN_WINDOW

    def in_window(entry_id):
        dated = published.get(entry_id)
        return dated is None or dated > since

    ids = [
        entry_id
        for entry_id in dict.fromkeys([*newer["ids"], *older["ids"]])
        if in_window(entry_id)
    ]
    return {"date": newer["date"], "ids": sorted(ids[:SEEN_IDS_MAX], reverse=True)}


def valid_marker(value):
    if not isinstance(value, dict) or set(value) != {"date", "ids"}:
        return False
    if not isinstance(value["date"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value["date"]):
        return False
    try:
        datetime.date.fromisoformat(value["date"])
    except ValueError:
        return False
    ids = value["ids"]
    return (
        isinstance(ids, list)
        and len(ids) <= SEEN_IDS_MAX
        and all(isinstance(item, str) and ID.match(item) for item in ids)
    )


def unread(entries, marker):
    """Entries newer than what the marker says the user has seen. No marker: all of them."""
    if not valid_marker(marker):
        return list(entries)
    newest = datetime.date.fromisoformat(marker["date"])
    seen = set(marker["ids"])
    return [
        entry
        for entry in entries
        if entry.date > newest or (entry.date > newest - SEEN_WINDOW and entry.id not in seen)
    ]
