"""The project website in website/ is published to GitHub Pages by .github/workflows/pages.yml.
The application does not serve it. A static host reports no missing file or bad link, so these
checks run here."""

import json
import re
from html.parser import HTMLParser
from pathlib import Path

from django.conf import settings

SITE = Path(settings.BASE_DIR) / "website"
PUBLISHED_URL = "https://openbga.github.io/lab-ops/"


def page():
    return (SITE / "index.html").read_text()


class _Head(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.meta = {}
        self.links = {}
        self.json_ld = []
        self.images = []
        self.headings = []
        self._in = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._in = "title"
        elif tag == "meta":
            key = attrs.get("name") or attrs.get("property")
            if key:
                self.meta[key] = attrs.get("content")
        elif tag == "link" and attrs.get("rel"):
            self.links[attrs["rel"]] = attrs.get("href")
        elif tag == "script" and attrs.get("type") == "application/ld+json":
            self._in = "json"
            self.json_ld.append("")
        elif tag == "img":
            self.images.append(attrs)
        elif tag in {"h1", "h2", "h3"}:
            self.headings.append(tag)

    def handle_endtag(self, tag):
        if tag in {"title", "script"}:
            self._in = None

    def handle_data(self, data):
        if self._in == "title":
            self.title += data
        elif self._in == "json":
            self.json_ld[-1] += data


def parsed():
    parser = _Head()
    parser.feed(page())
    return parser


def test_head_has_title_description_and_canonical():
    head = parsed()
    assert head.title.startswith("OpenBGA Lab Ops")
    assert len(head.title) <= 70
    description = head.meta["description"]
    assert 140 <= len(description) <= 160
    assert description.lower().count("free, open-source") == 1
    assert head.links["canonical"] == PUBLISHED_URL
    assert head.meta["og:url"] == PUBLISHED_URL
    assert head.meta["og:image"].startswith(PUBLISHED_URL)


def test_structured_data_repeats_the_description_and_carries_no_rating():
    head = parsed()
    assert len(head.json_ld) == 1
    data = json.loads(head.json_ld[0])
    assert data["@type"] == "SoftwareApplication"
    assert data["name"] == "OpenBGA Lab Ops"
    assert data["description"] == head.meta["description"]
    assert data["url"] == PUBLISHED_URL
    assert data["isAccessibleForFree"] is True
    assert data["offers"] == {"@type": "Offer", "price": 0, "priceCurrency": "USD"}
    assert data["license"] == "https://www.gnu.org/licenses/agpl-3.0.html"
    assert data["sameAs"] == ["https://github.com/openbga/lab-ops"]
    assert not {"aggregateRating", "review", "operatingSystem"} & data.keys()


def test_page_references_exactly_the_assets_it_ships():
    html = page()
    referenced = set(re.findall(r'(?:src|href|srcset)="(assets/[^" ]+)', html))
    for srcset in re.findall(r'srcset="([^"]+)"', html):
        referenced.update(candidate.split()[0] for candidate in srcset.split(","))
    # The link-preview image must be an absolute URL, so it points at the published copy.
    referenced.update(
        url.removeprefix(PUBLISHED_URL)
        for url in re.findall(r'property="og:image" content="([^"]+)"', html)
    )
    shipped = {
        path.relative_to(SITE).as_posix() for path in (SITE / "assets").iterdir() if path.is_file()
    }
    assert referenced == shipped


def test_in_page_links_and_icons_resolve():
    html = page()
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    anchors = set(re.findall(r'href="#([^"]+)"', html))
    assert anchors and anchors <= ids
    icons = set(re.findall(r'<use href="#([^"]+)"', html))
    assert icons and icons <= ids


def test_page_is_accessible_in_structure():
    head = parsed()
    assert head.headings.count("h1") == 1
    assert all("alt" in image for image in head.images)
    screenshots = [image for image in head.images if image["src"].endswith(".webp")]
    assert screenshots
    for image in screenshots:
        assert "shown with demo data" in image["alt"]
        assert image.get("width") and image.get("height")


def test_cost_and_support_wording():
    html = page()
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html))
    assert "no licence fee and no per-user pricing" in text
    assert "Are you looking for support? Contact bgasoft.com." in text
    assert 'href="https://bgasoft.com"' in html
    assert 'href="https://bgasoft.com/openbga/"' in html
    assert 'href="https://bgasoft.com/services/open-source-support/"' in html


def test_page_never_describes_the_product_as_a_lims():
    assert "lims" not in page().lower()


def test_committed_page_loads_no_third_party_script():
    """The deploy workflow adds the analytics snippet to the published copy only."""
    html = page()
    assert "plausible" not in html.lower()
    sources = re.findall(r'<script[^>]+src="([^"]+)"', html)
    assert sources and all(source.startswith("assets/") for source in sources)
