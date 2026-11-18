"""Build the presentation website into build/website.

    python3 tools/website/build.py [--out DIR] [--analytics]

Standard library only (Python 3.9 or later), so publishing needs no project environment.

The source is website/: the overview page, the stylesheet, the screenshots and the frame of the
roadmap page. The build copies them, adds the brand assets the application already ships (mark,
link preview, sign-in scene, font and icons) and renders roadmap/index.html from docs/roadmap.md.
The published roadmap is therefore the one on the branch that was built, and docs/roadmap.md
stays the only list of planned work. Every local link and fragment in the result is then checked.

To look at the result: python3 -m http.server --bind 127.0.0.1 --directory build/website
"""

import argparse
import posixpath
import re
import shutil
import sys
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "website"
ROADMAP = ROOT / "docs" / "roadmap.md"
DEFAULT_OUT = ROOT / "build" / "website"
SOURCE_URL = "https://github.com/openbga/lab-ops/blob/main/"

# Application assets the site reuses, copied at build time so each has one source.
SHARED = (
    ("static/img/mark.svg", "assets/mark.svg"),
    ("static/img/link-preview.png", "assets/og.png"),
    ("static/img/entrance/scene-light.svg", "assets/scene-light.svg"),
    ("static/img/entrance/scene-dark.svg", "assets/scene-dark.svg"),
    ("static/img/icons.svg", "assets/icons.svg"),
    # The MIT and OFL licences ask that their notices travel with copies.
    ("static/img/icons.LICENSE", "assets/icons.LICENSE.txt"),
    (
        "static/vendor/overpass-5.3.0/overpass-latin-wght-normal.woff2",
        "assets/overpass-latin-wght-normal.woff2",
    ),
    ("static/vendor/overpass-5.3.0/OFL.txt", "assets/overpass-OFL.txt"),
)

# The roadmap's stages, by the slug of their heading, with the title each section shows.
STAGES = {
    "built": "What is in place.",
    "next": "What comes next.",
    "later": "After that.",
    "exploring": "Being explored.",
}
BOARD_LIMIT = 6  # names listed per stage in the summary; the rest are counted

# Visits are counted on the published copy only, as on the other OpenBGA product sites.
ANALYTICS = """<!-- Privacy-friendly analytics by Plausible -->
<script async src="https://plausible.io/js/pa-As5SrVpCQgama4d7wsW_9.js"></script>
<script>
  window.plausible=window.plausible||function(){(plausible.q=plausible.q||[]).push(arguments)},plausible.init=plausible.init||function(i){plausible.o=i||{}};
  plausible.init()
</script>
"""  # noqa: E501


class SiteError(Exception):
    """The site cannot be built from these sources; the message says what to change."""


# ------------------------------------------------------------------ docs/roadmap.md

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
BULLET = re.compile(r"^[-*]\s+(.*)$")
NUMBERED = re.compile(r"^(\d+)\.\s+(.*)$")
TABLE_RULE = re.compile(r"^:?-{3,}:?$")
FIELD = re.compile(r"^\*\*([^*:]+):\*\*\s*(.+)$", re.S)
CODE = re.compile(r"`([^`]+)`")
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
BOLD = re.compile(r"\*\*(.+?)\*\*")
KEPT = re.compile("\x00(\\d+)\x00")
SENTENCE_END = re.compile(r"(?<=[.!?])\s")


@dataclass
class Part:
    """A section (##) with its items (###), or one item. Blocks are ("p", text),
    ("ul" or "ol", [text, ...]) and ("table", [header, ...], [[cell, ...], ...])."""

    title: str = ""
    slug: str = ""
    blocks: list = field(default_factory=list)
    items: list = field(default_factory=list)


def plain(text):
    """Markdown source as plain text: link targets, backticks and bold marks removed."""
    return BOLD.sub(r"\1", LINK.sub(r"\1", text)).replace("`", "")


class Slugs:
    """Heading anchors as GitHub makes them, so an in-page link in the roadmap works here too."""

    def __init__(self):
        self.seen = {}

    def take(self, title):
        slug = re.sub(r"[^\w\- ]", "", plain(title).lower()).replace(" ", "-")
        count = self.seen.get(slug, 0)
        self.seen[slug] = count + 1
        return f"{slug}-{count}" if count else slug


def _list_item(line):
    """(ordered, text) for a line that starts a top-level list item, or None."""
    if match := BULLET.match(line):
        return False, match[1]
    if match := NUMBERED.match(line):
        return True, match[2]
    return None


def _cells(line):
    row = line.strip()
    row = row[1:] if row.startswith("|") else row
    row = row[:-1] if row.endswith("|") and not row.endswith("\\|") else row
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", row)]


def parse_roadmap(text):
    """Read the Markdown the roadmap is written in: headings to ###, paragraphs, flat lists and
    tables, with bold, code and links inside them. Anything it cannot place is an error, so a
    change to the roadmap that this page cannot show fails the checks instead of publishing."""
    intro, sections, slugs = Part(), [], Slugs()
    target = intro
    lines = text.splitlines()
    index = 0

    def starts_block(line):
        stripped = line.strip()
        return not stripped or HEADING.match(line) or stripped.startswith("|")

    while index < len(lines):
        line = lines[index]
        where = f"docs/roadmap.md line {index + 1}"
        if not line.strip():
            index += 1
        elif heading := HEADING.match(line):
            level, title = len(heading[1]), heading[2]
            if level == 2:
                target = Part(title, slugs.take(title))
                sections.append(target)
            elif level == 3 and sections:
                target = Part(title, slugs.take(title))
                sections[-1].items.append(target)
            elif level != 1 or sections or intro.blocks:
                raise SiteError(f"{where}: use ## for a section and ### for an item within one.")
            index += 1
        elif line.strip().startswith("|"):
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(_cells(lines[index]))
                index += 1
            ruled = len(rows) > 1 and all(TABLE_RULE.match(cell) for cell in rows[1])
            if not ruled or any(len(row) != len(rows[0]) for row in rows):
                raise SiteError(
                    f"{where}: a table needs a header row, a rule and the same number of cells "
                    "in every row."
                )
            target.blocks.append(("table", rows[0], rows[2:]))
        elif item := _list_item(line):
            ordered, items = item[0], []
            while index < len(lines) and not starts_block(lines[index]):
                item = _list_item(lines[index])
                if item:
                    items.append(item[1].strip())
                elif _list_item(lines[index].strip()):
                    raise SiteError(f"docs/roadmap.md line {index + 1}: keep lists flat.")
                else:
                    items[-1] += " " + lines[index].strip()
                index += 1
            target.blocks.append(("ol" if ordered else "ul", items))
        else:
            words = []
            while index < len(lines) and not starts_block(lines[index]):
                # A list can follow a paragraph without a blank line; a numbered one only from 1.
                item = BULLET.match(lines[index]) or NUMBERED.match(lines[index])
                if words and item and (item.re is BULLET or item[1] == "1"):
                    break
                words.append(lines[index].strip())
                index += 1
            target.blocks.append(("p", " ".join(words)))
    if not any(section.slug in STAGES for section in sections):
        raise SiteError("docs/roadmap.md has none of the sections Built, Next, Later, Exploring.")
    return intro.blocks, sections


def link_address(target):
    """Where a link in the roadmap leads from the website: the same page for a fragment, and
    the file on the main branch for a path relative to docs/."""
    if target.startswith("#") or re.match(r"(https?://|mailto:)", target):
        return target
    path, _, fragment = target.partition("#")
    resolved = posixpath.normpath(posixpath.join("docs", path))
    if resolved.startswith("..") or not (ROOT / resolved).exists():
        raise SiteError(f"docs/roadmap.md links to {target}, which is not a file in this project.")
    return SOURCE_URL + resolved + (f"#{fragment}" if fragment else "")


def inline(text):
    """One line of roadmap Markdown as HTML: code, links and bold, everything else escaped."""
    kept = []

    def keep(markup):
        kept.append(markup)
        return f"\x00{len(kept) - 1}\x00"

    def marks(fragment):
        return BOLD.sub(r"<strong>\1</strong>", escape(fragment, quote=False))

    text = CODE.sub(lambda match: keep(f"<code>{escape(match[1], quote=False)}</code>"), text)
    text = LINK.sub(
        lambda match: keep(f'<a href="{escape(link_address(match[2]))}">{marks(match[1])}</a>'),
        text,
    )
    text = marks(text)
    while "\x00" in text:
        text = KEPT.sub(lambda match: kept[int(match[1])], text)
    return text


def render_blocks(blocks, caption):
    """Paragraphs and lists as running text, tables as tables, and a list whose every item is
    "**Label:** text" (Delivers, Basis, Depends on, Leaves out) as labelled fields."""
    html, prose = [], []

    def flush():
        if prose:
            html.append('<div class="prose">\n' + "\n".join(prose) + "\n</div>")
            prose.clear()

    for kind, *content in blocks:
        if kind == "p":
            prose.append(f"<p>{inline(content[0])}</p>")
        elif kind == "table":
            flush()
            head = "".join(f'<th scope="col">{inline(cell)}</th>' for cell in content[0])
            rows = "\n".join(
                f'<tr><th scope="row">{inline(row[0])}</th>'
                + "".join(f"<td>{inline(cell)}</td>" for cell in row[1:])
                + "</tr>"
                for row in content[1]
            )
            html.append(
                f'<div class="table-scroll">\n<table>\n<caption>{escape(caption)}</caption>\n'
                f"<thead><tr>{head}</tr></thead>\n<tbody>\n{rows}\n</tbody>\n</table>\n</div>"
            )
        elif kind == "ul" and all(fields := [FIELD.match(item) for item in content[0]]):
            flush()
            html.append(
                '<dl class="fields">\n'
                + "\n".join(
                    f"<div><dt>{escape(match[1])}</dt><dd>{inline(match[2])}</dd></div>"
                    for match in fields
                )
                + "\n</dl>"
            )
        else:
            items = "".join(f"<li>{inline(item)}</li>" for item in content[0])
            prose.append(f"<{kind}>{items}</{kind}>")
    flush()
    return "\n".join(html)


def render_section(section, tinted):
    stage = section.slug in STAGES
    classes = ["section", "roadmap-section"]
    classes += [f"status-{section.slug}"] if stage else []
    classes += ["band"] if tinted else []
    title = escape(plain(section.title))
    heading = f'<h2 id="{section.slug}-title">{STAGES[section.slug] if stage else title}</h2>'
    if stage:
        heading = f'<p class="status status-{section.slug}">{title}</p>\n{heading}'
    parts = [heading, render_blocks(section.blocks, plain(section.title))]
    if section.items:
        parts.append(
            '<ul class="items">\n'
            + "\n".join(
                f'<li id="{item.slug}">\n<h3>{inline(item.title)}</h3>\n'
                f"{render_blocks(item.blocks, plain(item.title))}\n</li>"
                for item in section.items
            )
            + "\n</ul>"
        )
    body = "\n".join(part for part in parts if part)
    return (
        f'<section class="{" ".join(classes)}" id="{section.slug}" '
        f'aria-labelledby="{section.slug}-title">\n<div class="wrap">\n{body}\n</div>\n</section>'
    )


def _summary(section):
    """What the summary shows for a stage: its items by name, else the first column of its
    first table, else its opening sentence."""
    names = [(plain(item.title), item.slug) for item in section.items]
    if not names:
        table = next((block for block in section.blocks if block[0] == "table"), None)
        names = [(plain(row[0]), None) for row in table[2]] if table else []
    if not names:
        opening = next((block[1] for block in section.blocks if block[0] == "p"), "")
        return f"<p>{escape(SENTENCE_END.split(plain(opening))[0])}</p>" if opening else ""
    rows = [
        f'<li><a href="#{slug}">{escape(name)}</a></li>' if slug else f"<li>{escape(name)}</li>"
        for name, slug in names[:BOARD_LIMIT]
    ]
    if len(names) > BOARD_LIMIT:
        rows.append(f'<li class="more">and {len(names) - BOARD_LIMIT} more</li>')
    return "<ul>\n" + "\n".join(rows) + "\n</ul>"


def render_roadmap(frame, text):
    """The roadmap page: the frame from website/roadmap/index.html, filled from the roadmap."""
    intro, sections = parse_roadmap(text)
    stages = [section for section in sections if section.slug in STAGES]
    filled = {
        "nav": "\n".join(
            f'<a href="#{stage.slug}">{escape(plain(stage.title))}</a>' for stage in stages
        ),
        "intro": render_blocks(intro, "OpenBGA Lab Ops roadmap"),
        "board": "\n".join(
            f'<div class="status-{stage.slug}">\n<a class="status status-{stage.slug}" '
            f'href="#{stage.slug}">{escape(plain(stage.title))}</a>\n{_summary(stage)}\n</div>'
            for stage in stages
        ),
        "sections": "\n\n".join(
            render_section(section, tinted=number % 2 == 0)
            for number, section in enumerate(sections)
        ),
    }
    for name, markup in filled.items():
        marker = f"<!-- roadmap:{name} -->"
        if frame.count(marker) != 1:
            raise SiteError(f"website/roadmap/index.html needs exactly one {marker} marker.")
        frame = frame.replace(marker, markup)
    return frame


# ------------------------------------------------------------------ the built site


class _Page(HTMLParser):
    """The ids a page defines and the addresses it refers to."""

    def __init__(self):
        super().__init__()
        self.ids, self.refs = [], []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if not value:
                continue
            if name == "id":
                self.ids.append(value)
            elif name in ("href", "src"):
                self.refs.append(value)
            elif name == "srcset":
                self.refs.extend(part.split()[0] for part in value.split(",") if part.strip())


def check_site(out):
    """Every local address in the built pages leads to a file in the site, and every fragment to
    an id in that file. Addresses on other sites are not fetched."""
    out = out.resolve()
    pages = {path: _Page() for path in sorted(out.rglob("*.html"))}
    problems = []
    for path, page in pages.items():
        page.feed(path.read_text(encoding="utf-8"))
        repeated = sorted({value for value in page.ids if page.ids.count(value) > 1})
        if repeated:
            problems.append(f"{path.relative_to(out)}: ids used more than once: {repeated}")
    for path, page in pages.items():
        for ref in page.refs:
            if re.match(r"([a-z][a-z0-9+.-]*:|//)", ref):
                continue
            address, _, fragment = ref.partition("#")
            target = (path.parent / address).resolve() if address else path
            target = target / "index.html" if target.is_dir() else target
            name = f"{path.relative_to(out)}: {ref}"
            if out not in target.parents or not target.is_file():
                problems.append(f"{name} leads to no file in the site")
            elif fragment and target in pages and fragment not in pages[target].ids:
                problems.append(f"{name} names an id the page does not have")
            elif fragment and target.suffix == ".svg":
                if f'id="{fragment}"' not in target.read_text(encoding="utf-8"):
                    problems.append(f"{name} names a symbol the file does not have")
    for sheet in sorted(out.rglob("*.css")):
        for ref in re.findall(r'url\("?([^")]+)"?\)', sheet.read_text(encoding="utf-8")):
            if not (sheet.parent / ref).is_file():
                problems.append(f"{sheet.relative_to(out)}: {ref} leads to no file in the site")
    if problems:
        raise SiteError("The built site has broken links:\n  " + "\n  ".join(problems))


def build(out=DEFAULT_OUT, analytics=False):
    out = Path(out)
    if out.resolve() == DEFAULT_OUT.resolve():
        shutil.rmtree(out, ignore_errors=True)
    elif out.exists() and any(out.iterdir()):
        raise SiteError(f"{out} is not empty; build into a new or empty directory.")
    shutil.copytree(SITE, out, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".DS_Store"))
    for source, name in SHARED:
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / source, out / name)
    page = out / "roadmap" / "index.html"
    page.write_text(
        render_roadmap(page.read_text(encoding="utf-8"), ROADMAP.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    if analytics:
        for path in sorted(out.rglob("*.html")):
            html = path.read_text(encoding="utf-8")
            if html.count("</head>") != 1 or "plausible" in html:
                raise SiteError(f"{path.relative_to(out)}: cannot add the analytics script.")
            path.write_text(html.replace("</head>", ANALYTICS + "</head>"), encoding="utf-8")
    check_site(out)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="directory to build into")
    parser.add_argument(
        "--analytics",
        action="store_true",
        help="add the visit counter; for the published copy only",
    )
    options = parser.parse_args()
    try:
        out = build(options.out, options.analytics)
    except SiteError as error:
        sys.exit(str(error))
    pages = len(list(out.rglob("*.html")))
    print(f"Built the OpenBGA Lab Ops website: {out} ({pages} pages)")


if __name__ == "__main__":
    main()
