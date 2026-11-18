"""Phone use (docs/ui-standards.md section 13) on the service desk and team pages: at 360 and
430 px no page scrolls sideways and no control is under 44 px, the ticket list's rows come first
behind "Filter and sort", and the filter sheet keeps the chosen view."""

import re

import pytest
from conftest import Lab
from helpers import sign_in
from playwright.sync_api import Page, expect
from test_phone_layout import PHONES, THUMBBAR, check, filter_button, open_filter_sheet

pytestmark = pytest.mark.django_db(transaction=True)

PAGES = [
    "/requests/",
    "/requests/?view=all",
    "/requests/new/",
    "/requests/find/?q=Example",
    "/requests/{request}/",
    "/requests/{request}/assign/",
    "/requests/{request}/status/",
    "/requests/{request}/details/",
    "/requests/{request}/history/",
    "/requests/views/new/?list=view%3Dall",
    "/teams/",
    "/teams/{team}/",
    "/teams/new/",
]


@pytest.mark.parametrize("phone", PHONES, ids=lambda size: f"{size['width']}px")
def test_desk_pages_fit_a_phone_with_44px_targets(page: Page, demo: Lab, phone):
    from desk.models import ServiceRequest
    from teams.models import Team

    request = ServiceRequest.objects.exclude(status="closed").order_by("pk").first().pk
    team = Team.objects.get(is_default=True).pk
    page.set_viewport_size(phone)
    sign_in(page, "demo_admin", demo.password)
    problems = []
    for url in PAGES:
        page.goto(url.format(request=request, team=team))
        check(page, url, problems)
    # The ticket page's action dialogs and its composer in each mode.
    page.goto(f"/requests/{request}/")
    page.get_by_role("link", name="Assign", exact=True).first.click()
    page.get_by_role("dialog").get_by_role("button", name="Assign").wait_for()
    check(page, "assign dialog", problems)
    page.keyboard.press("Escape")
    page.goto(f"/requests/{request}/")
    page.locator("#composer").get_by_label("Log contact").check()
    check(page, "composer, log contact", problems)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("phone", PHONES, ids=lambda size: f"{size['width']}px")
def test_request_list_rows_come_first_and_the_sheet_keeps_the_view(page: Page, demo: Lab, phone):
    page.set_viewport_size(phone)
    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/requests/?view=all")
    row = page.locator("table.vr-table tbody tr").first
    expect(row).to_be_visible()
    assert row.bounding_box()["y"] < phone["height"] - THUMBBAR - 80
    assert filter_button(page).bounding_box()["height"] >= 44
    sheet = open_filter_sheet(page)
    sheet.get_by_role("searchbox").fill("Example")
    sheet.get_by_role("button", name="Apply").click()
    expect(page).to_have_url(re.compile(r"view=all"))
    expect(page.get_by_role("heading", level=1, name="All tickets")).to_be_visible()
