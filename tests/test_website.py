"""The presentation website (website/, built by tools/website/build.py and published to GitHub
Pages): it builds from this branch, its roadmap page is docs/roadmap.md, and what it promises
visitors matches the repository."""

import html
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SITE_URL = "https://openbga.github.io/lab-ops/"


def _builder():
    spec = importlib.util.spec_from_file_location("website_build", ROOT / "tools/website/build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = _builder()
FRAME = (ROOT / "website/roadmap/index.html").read_text()
OVERVIEW = (ROOT / "website/index.html").read_text()


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    return builder.build(tmp_path_factory.mktemp("website") / "site")


def test_site_builds_with_every_link_resolved(site):
    """build() checks each local address and fragment; a broken one fails the build."""
    assert (site / "index.html").is_file() and (site / "roadmap" / "index.html").is_file()
    assert "plausible" not in (site / "index.html").read_text()


def test_roadmap_page_shows_the_roadmap_of_this_branch(site):
    """Each section, planned item and table row of docs/roadmap.md is on the page."""
    _, sections = builder.parse_roadmap((ROOT / "docs/roadmap.md").read_text())
    page = (site / "roadmap" / "index.html").read_text()
    # What a visitor reads: a name can carry a link or code, which the page marks up.
    words = html.unescape(re.sub(r"<[^>]+>", "", page))
    assert {"built", "next", "later"} <= {section.slug for section in sections}
    for section in sections:
        assert f'id="{section.slug}"' in page
        names = [item.title for item in section.items]
        names += [row[0] for kind, *rest in section.blocks if kind == "table" for row in rest[1]]
        for name in names:
            assert builder.plain(name) in words, name
    assert "<!-- roadmap:" not in page


ITEMS = """# Roadmap

Planned work, without dates. See the [product scope](product.md).

## Built

| Area | What is in place |
| --- | --- |
| Desk | Tickets & replies |

## Next

### Email channel: replies out

- **Delivers:** replies by email, set with `EMAIL_HOST`.
- **Depends on:** [scheduled jobs](#scheduled-jobs).
- **Leaves out:** text messages.

### Scheduled jobs

- **Delivers:** retries.
  Continued on a second line.

## Exploring

Not ready to build. Each waits for evidence.

- A rating after a ticket is resolved.
- A shift handover log.
"""


def test_roadmap_items_render_as_cards_with_their_fields():
    page = builder.render_roadmap(FRAME, ITEMS)
    assert '<li id="email-channel-replies-out">' in page
    assert "<dt>Delivers</dt><dd>replies by email, set with <code>EMAIL_HOST</code>.</dd>" in page
    assert '<dd><a href="#scheduled-jobs">scheduled jobs</a>.</dd>' in page
    assert "<dd>retries. Continued on a second line.</dd>" in page
    assert "<li>A shift handover log.</li>" in page
    assert "Tickets &amp; replies" in page
    # Links to other documents lead to the file on the main branch.
    assert 'href="https://github.com/openbga/lab-ops/blob/main/docs/product.md"' in page


def test_roadmap_summary_names_each_stage_and_counts_the_rest():
    many = "## Next\n\n" + "\n\n".join(f"### Item {n}\n\n- **Delivers:** x." for n in range(9))
    page = builder.render_roadmap(FRAME, ITEMS.split("## Next")[0] + many)
    board = page.split('aria-label="Roadmap at a glance">')[1].split("</nav>")[0]
    assert "<li>Desk</li>" in board  # a stage without items lists its table's first column
    assert '<li><a href="#item-5">Item 5</a></li>' in board and "Item 6" not in board
    assert '<li class="more">and 3 more</li>' in board
    # A stage with neither shows its opening sentence.
    assert "<p>Not ready to build.</p>" in builder.render_roadmap(FRAME, ITEMS)


@pytest.mark.parametrize(
    "markdown, message",
    [
        ("## Next\n\n#### Too deep\n", "use ## for a section"),
        ("## Next\n\n- one\n  - nested\n", "keep lists flat"),
        ("## Next\n\n| A | B |\n| --- | --- |\n| only one |\n", "same number of cells"),
        ("## Next\n\nSee [the plan](missing.md).\n", "not a file in this project"),
        ("## Notes\n\nNothing staged.\n", "none of the sections"),
    ],
)
def test_roadmap_the_page_cannot_show_is_refused(markdown, message):
    with pytest.raises(builder.SiteError, match=message):
        builder.render_roadmap(FRAME, markdown)


def test_broken_local_links_fail_the_check(tmp_path):
    (tmp_path / "index.html").write_text(
        '<a href="missing/">x</a><a href="#nowhere">y</a><img src="https://example.invalid/a.png">'
    )
    with pytest.raises(builder.SiteError) as error:
        builder.check_site(tmp_path)
    assert "missing/ leads to no file" in str(error.value)
    assert "#nowhere names an id" in str(error.value)
    assert "example.invalid" not in str(error.value)


def test_only_the_published_copy_counts_visits(tmp_path):
    published = builder.build(tmp_path / "published", analytics=True)
    for page in ("index.html", "roadmap/index.html"):
        assert (published / page).read_text().count("plausible.io/js/") == 1
    assert "plausible" not in OVERVIEW + FRAME


def test_structured_data_says_the_software_is_free_and_agpl(site):
    block = re.search(r'<script type="application/ld\+json">(.*?)</script>', OVERVIEW, re.S)
    data = json.loads(block[1])
    assert data["@type"] == "SoftwareApplication" and data["name"] == "OpenBGA Lab Ops"
    assert data["isAccessibleForFree"] is True
    assert data["offers"] == {"@type": "Offer", "price": "0", "priceCurrency": "USD"}
    assert data["license"] == "https://spdx.org/licenses/AGPL-3.0-only.html"
    assert 'license = "AGPL-3.0-only"' in (ROOT / "pyproject.toml").read_text()
    assert data["url"] == SITE_URL
    for key in ("image", "screenshot"):
        assert data[key].startswith(SITE_URL)
        assert (site / data[key].removeprefix(SITE_URL)).is_file(), data[key]


def test_trial_commands_are_the_readme_commands():
    """The overview repeats the Docker trial from the README; the two must not drift apart."""
    block = re.search(r"<pre><code>(.*?)</code></pre>", OVERVIEW, re.S)[1]
    commands = html.unescape(re.sub(r"<[^>]+>", "", block)).replace("\\\n  ", "")
    readme = (ROOT / "README.md").read_text()
    for line in commands.splitlines():
        assert line in readme, line
    for account in re.findall(r"<code>(demo_\w+)</code>", OVERVIEW):
        assert f"`{account}`" in readme, account
