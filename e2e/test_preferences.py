"""Per-user settings follow the user, not the workstation: grid columns, page size, sidebar."""

import re

import pytest
from conftest import DESKTOP
from helpers import (
    column_headers,
    column_values,
    make_clients,
    make_user,
    pick,
    sign_in,
    sign_out,
)
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "e2e-preferences-Password-1"


def settled(page: Page):
    """The setting's POST has reached the server (static/js/preferences.js)."""
    page.wait_for_load_state("networkidle")


def test_grid_columns_page_size_and_sort_are_saved_per_user(page: Page, demo):
    assert make_clients(60) > 50  # more than one page at 25 and at 50 rows
    make_user("pref_first", PASSWORD, "technical")
    make_user("pref_second", PASSWORD, "technical")

    sign_in(page, "pref_first", PASSWORD)
    page.goto("/clients/")
    defaults = column_headers(page)
    assert column_values(page, "Organization")[0].startswith("Example ")  # name, ascending
    page.get_by_role("button", name="Columns", exact=True).click()
    dialog = page.get_by_role("dialog", name="Columns")
    dialog.get_by_role("checkbox", name="City", exact=True).uncheck()
    page.keyboard.press("Escape")
    settled(page)
    pick(page.get_by_role("combobox", name="Rows per page"), label="50")
    page.get_by_role("button", name="Show", exact=True).click()
    expect(page.get_by_text(re.compile(r"Showing 1–50"))).to_be_visible()
    page.locator("table.vr-table thead").get_by_role(
        "link", name=re.compile("^Organization")
    ).click()
    expect(page).to_have_url(re.compile(r"sort=-name"))
    assert column_values(page, "Organization")[0] == "Synthetic Organization 060"
    settled(page)

    page.goto("/clients/")  # no parameters: the saved sort, page size and columns apply
    expect(page.get_by_text(re.compile(r"Showing 1–50"))).to_be_visible()
    assert column_values(page, "Organization")[0] == "Synthetic Organization 060"
    assert "City" not in column_headers(page)
    assert page.evaluate("Object.keys(localStorage).filter(k => k.startsWith('vr-grid'))") == []
    sign_out(page)

    # The next person at the same workstation gets the defaults, not the previous layout.
    sign_in(page, "pref_second", PASSWORD)
    page.goto("/clients/")
    expect(page.get_by_text(re.compile(r"Showing 1–25"))).to_be_visible()
    assert column_headers(page) == defaults
    assert column_values(page, "Organization")[0].startswith("Example ")
    sign_out(page)

    sign_in(page, "pref_first", PASSWORD)  # and the first user's settings are still there
    page.goto("/clients/")
    expect(page.get_by_text(re.compile(r"Showing 1–50"))).to_be_visible()
    assert column_values(page, "Organization")[0] == "Synthetic Organization 060"
    assert "City" not in column_headers(page)


def test_sidebar_collapses_to_an_icon_rail_and_remembers_it(page: Page, demo):
    make_user("rail_first", PASSWORD, "technical")
    make_user("rail_second", PASSWORD, "technical")
    page.set_viewport_size({"width": 1280, "height": DESKTOP["height"]})
    sign_in(page, "rail_first", PASSWORD)
    page.goto("/clients/")
    nav = page.get_by_role("navigation", name="Primary")
    toggle = nav.get_by_role("button", name="Collapse menu")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(toggle).to_have_attribute("aria-controls", "vr-nav")
    toggle.focus()
    page.keyboard.press("Enter")
    expand = nav.get_by_role("button", name="Expand menu")
    expect(expand).to_have_attribute("aria-expanded", "false")
    settled(page)

    # Icons only; the label is still the link's name and shows as a tooltip on keyboard focus.
    clients = nav.get_by_role("link", name="Client organizations", exact=True)
    expect(clients).to_have_attribute("aria-current", "page")
    assert clients.bounding_box()["width"] < 64
    clients.focus()
    tooltip = page.locator(".tooltip.vr-nav-tooltip")
    expect(tooltip).to_have_text("Client organizations")
    box = tooltip.bounding_box()
    assert box["x"] > clients.bounding_box()["x"] + clients.bounding_box()["width"]
    assert box["x"] + box["width"] <= 1280  # inside the window, not clipped by the rail

    page.reload()
    expect(nav.get_by_role("button", name="Expand menu")).to_be_visible()
    sign_out(page)
    sign_in(page, "rail_second", PASSWORD)
    page.goto("/clients/")
    expect(nav.get_by_role("button", name="Collapse menu")).to_have_attribute(
        "aria-expanded", "true"
    )


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_a_layout_refused_at_the_limit_says_it_applies_to_this_page_only(page: Page, demo, theme):
    """At the limit of saved preferences the server refuses a new column layout: the layout still
    applies to the page, and a dismissible notice says it was not saved (static/js/preferences.js,
    the shared request-error notice of app.js). Desktop only: on a phone neither the Columns
    control nor the sidebar rail, the two settings saved in the background, is offered."""
    page.set_viewport_size(DESKTOP)
    page.emulate_media(color_scheme=theme)
    from core.models import UserPreference
    from core.preferences import MAX_PREFERENCES

    make_clients(3)
    user = make_user("pref_full", PASSWORD, "technical")
    UserPreference.objects.bulk_create(
        UserPreference(user=user, key=f"page:list{n}", value={"per_page": 25})
        for n in range(MAX_PREFERENCES)
    )
    sign_in(page, "pref_full", PASSWORD)
    page.goto("/clients/")
    page.get_by_role("button", name="Columns", exact=True).click()
    dialog = page.get_by_role("dialog", name="Columns")
    dialog.get_by_role("checkbox", name="City", exact=True).uncheck()
    page.keyboard.press("Escape")
    notice = page.locator("[data-vr-request-error]")
    expect(notice).to_be_visible()
    expect(notice).to_contain_text("applies to this page only")
    assert "City" not in column_headers(page)
    assert not UserPreference.objects.filter(user=user, key__startswith="grid:").exists()
    notice.get_by_role("button", name=re.compile("Dismiss|Close")).click()
    expect(notice).to_have_count(0)
