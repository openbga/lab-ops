"""Milestone screenshot set: key pages in both themes at desktop, tablet and narrow widths.

Opt in with ``--screenshots-dir DIR``. The images are for human inspection; the only assertion
is that no page needs horizontal scrolling (tables scroll inside their own wrapper).
"""

import json
from pathlib import Path

import pytest
from conftest import DESKTOP, NARROW, TABLET
from helpers import make_clients, sign_in
from playwright.sync_api import Page

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.screenshots]

VIEWPORTS = {"desktop": DESKTOP, "tablet": TABLET, "narrow": NARROW}
# page key -> (role whose session is used, viewports to capture)
PAGES = {
    "login": (None, ("desktop", "narrow")),
    "dashboard": ("account_manager", ("desktop", "tablet")),
    "clients": ("account_manager", ("desktop", "tablet", "narrow")),
    "client-detail": ("account_manager", ("desktop", "narrow")),
    "portal": ("client", ("desktop", "narrow")),
}


def test_milestone_screenshots(page: Page, demo, browser, browser_context_args, request):
    out = Path(request.config.getoption("--screenshots-dir"))
    out.mkdir(parents=True, exist_ok=True)

    from clients.models import Client

    make_clients(30)  # a list long enough to page, beside the demo organizations
    clinic = Client.objects.get(name="Example Family Clinic")
    urls = {
        "login": "/accounts/login/",
        "dashboard": "/",
        "clients": "/clients/",
        "client-detail": f"/clients/{clinic.pk}/",
        "portal": "/portal/",
    }

    # One real sign-in per role; the saved state is reused for every theme and width.
    states = {None: None}
    for role in {role for role, _ in PAGES.values() if role}:
        sign_in(page, f"demo_{role}", demo.password)
        states[role] = page.context.storage_state()
        page.context.clear_cookies()

    manifest = []
    for theme in ("light", "dark"):
        for size_name, size in VIEWPORTS.items():
            for role in states:
                keys = [
                    key for key, (r, sizes) in PAGES.items() if r == role and size_name in sizes
                ]
                if not keys:
                    continue
                options = {
                    **browser_context_args,
                    "viewport": size,
                    "color_scheme": theme,
                    "storage_state": states[role],
                    "is_mobile": size_name == "narrow",
                    "has_touch": size_name == "narrow",
                }
                context = browser.new_context(**options)
                context.add_init_script(f"localStorage.setItem('vr-theme', '{theme}')")
                shot = context.new_page()
                shot.set_default_timeout(10_000)
                for key in keys:
                    shot.goto(urls[key])
                    shot.wait_for_load_state("networkidle")
                    overflow = shot.evaluate(
                        "document.documentElement.scrollWidth - window.innerWidth"
                    )
                    name = f"{key}-{theme}-{size_name}.png"
                    # Desktop/tablet: what is visible on arrival. Narrow: the whole stacked page.
                    shot.screenshot(path=str(out / name), full_page=size_name == "narrow")
                    manifest.append({"file": name, "horizontal_overflow_px": overflow})
                context.close()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    overflowing = [m for m in manifest if m["horizontal_overflow_px"] > 1]
    assert not overflowing, f"Pages needing horizontal page scroll: {overflowing}"
