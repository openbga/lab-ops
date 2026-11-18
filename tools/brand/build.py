"""Build the PNG brand assets from their sources.

    uv run --group e2e python -I tools/brand/build.py

Sources: static/img/mark.svg (the mark, also the SVG favicon) and tools/brand/link-preview.html.
Outputs, rendered with Playwright's Chromium:
  static/img/favicon-32.png        the mark at 32 x 32, transparent corners
  static/img/apple-touch-icon.png  180 x 180, the tile full-bleed (square: the device rounds it)
  static/img/link-preview.png      1200 x 630 link preview from link-preview.html
Run it after changing either source and commit the generated files.
"""

import re
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
IMG = ROOT / "static" / "img"
MARK = (IMG / "mark.svg").read_text()


def square_tile(svg):
    """The mark with its tile's corner radius removed (the first rect is the tile)."""
    return re.sub(r'(<rect width="32" height="32") rx="[\d.]+"', r"\1", svg, count=1)


def render_svg(page, svg, size, path):
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "mark.svg"
        src.write_text(svg)
        html = Path(tmp) / "page.html"
        html.write_text(
            f'<body style="margin:0"><img id="m" src="{src.as_uri()}" '
            f'width="{size}" height="{size}" style="display:block"></body>'
        )
        page.set_viewport_size({"width": size, "height": size})
        page.goto(html.as_uri())
        page.locator("#m").screenshot(path=str(path), omit_background=True)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(device_scale_factor=1)
        render_svg(page, MARK, 32, IMG / "favicon-32.png")
        render_svg(page, square_tile(MARK), 180, IMG / "apple-touch-icon.png")
        page.set_viewport_size({"width": 1200, "height": 630})
        page.goto((HERE / "link-preview.html").as_uri())
        page.evaluate("document.fonts.ready")
        page.screenshot(path=str(IMG / "link-preview.png"))
        browser.close()
    for name in ("favicon-32.png", "apple-touch-icon.png", "link-preview.png"):
        path = IMG / name
        print(path.relative_to(ROOT), path.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
