"""Screenshots for the presentation website, taken from the synthetic demonstration.

Opt in with ``--website-shots-dir DIR``. From the repository root:

    uv run --group e2e pytest -c e2e/pytest.ini e2e/test_website_shots.py \\
        --website-shots-dir website/assets/shots

Each page is captured in both themes, 1440 pixels wide on a 2x screen, and saved as WebP at 1440
pixels (``name-1440.webp`` and ``name-dark-1440.webp``); the hero also keeps its 2880 pixel
originals. Run it after a change to a page shown on the website, check the alt text and captions
in website/index.html against the new images, and commit both.
"""

import io
from pathlib import Path

import pytest
from helpers import sign_in
from PIL import Image
from playwright.sync_api import Page

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.website_shots]

WIDTH = 1440
QUALITY = 84
THEMES = {"light": "", "dark": "-dark"}  # theme -> what it adds to the file name


def _shots():
    """name -> (demo role, address, window height, heading of the panel to scroll to, keep 2x).

    The dashboard is the hero: its window is as tall as its content, and it keeps the 2x image.
    """
    from clients.models import Client
    from desk.models import ServiceRequest

    def ticket(subject):
        return ServiceRequest.objects.get(subject=subject).pk

    clinic = Client.objects.get(name="Example Family Clinic").pk
    missing = ticket("Result not received for order ORD-TEST-000042")
    complaint = ticket("Results late three times this month")
    return {
        "dashboard": ("client_services", "/", 800, None, True),
        "queue": ("client_services", "/requests/?view=my-teams", 900, None, False),
        "ticket": ("client_services", f"/requests/{missing}/", 900, "Links", False),
        "complaint": ("supervisor", f"/requests/{complaint}/", 900, "Complaint", False),
        "portal": ("client", "/portal/", 900, None, False),
        "organization": ("account_manager", f"/clients/{clinic}/", 900, None, False),
    }


# Bring the panel under a heading to the top of the window, clear of the fixed top bar.
SCROLL_TO_PANEL = """heading => {
    const title = [...document.querySelectorAll('main h2, main h3')]
        .find(element => element.textContent.trim().startsWith(heading));
    if (!title) throw new Error(`No panel titled ${heading}`);
    const bar = document.querySelector('header');
    const clear = bar ? bar.getBoundingClientRect().bottom : 0;
    const panel = title.closest('section, .card, article') || title;
    window.scrollBy(0, panel.getBoundingClientRect().top - clear - 16);
}"""


def _save(png: bytes, out: Path, name: str, keep_2x: bool) -> None:
    image = Image.open(io.BytesIO(png)).convert("RGB")
    assert image.width == WIDTH * 2, image.size
    if keep_2x:
        image.save(out / f"{name}-{image.width}.webp", "WEBP", quality=QUALITY, method=6)
    small = image.resize((WIDTH, image.height // 2), Image.Resampling.LANCZOS)
    small.save(out / f"{name}-{WIDTH}.webp", "WEBP", quality=QUALITY, method=6)


def test_website_shots(page: Page, demo, browser, browser_context_args, request):
    out = Path(request.config.getoption("--website-shots-dir"))
    out.mkdir(parents=True, exist_ok=True)
    shots = _shots()

    for role in sorted({role for role, *_ in shots.values()}):
        # One real sign-in per role; the saved session is reused for both themes.
        sign_in(page, f"demo_{role}", demo.password)
        session = page.context.storage_state()
        page.context.clear_cookies()
        for theme, suffix in THEMES.items():
            context = browser.new_context(
                **{
                    **browser_context_args,
                    "viewport": {"width": WIDTH, "height": 900},
                    "device_scale_factor": 2,
                    "color_scheme": theme,
                    "storage_state": session,
                }
            )
            context.add_init_script(f"localStorage.setItem('vr-theme', '{theme}')")
            shot = context.new_page()
            for name, (owner, address, height, heading, keep_2x) in shots.items():
                if owner != role:
                    continue
                shot.set_viewport_size({"width": WIDTH, "height": height})
                shot.goto(address)
                shot.wait_for_load_state("networkidle")
                if heading:
                    shot.evaluate(SCROLL_TO_PANEL, heading)
                shot.evaluate("document.fonts.ready")
                _save(shot.screenshot(), out, f"{name}{suffix}", keep_2x)
            context.close()

    written = sorted(path.name for path in out.glob("*.webp"))
    assert len(written) == (len(shots) + 1) * len(THEMES), written
