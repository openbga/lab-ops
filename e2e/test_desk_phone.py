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


def test_reply_notice_leaves_room_for_its_message(page: Page, demo: Lab):
    from desk.models import ServiceRequest

    ticket = ServiceRequest.objects.filter(client__isnull=True).exclude(status="closed").first()
    sign_in(page, "demo_supervisor", demo.password)
    page.goto(f"/requests/{ticket.pk}/")
    message = page.locator("#composer .alert > .vr-status + div")
    expect(message).to_be_visible()
    for width in (360, 390, 1199, 1200, 1280, 1440):
        page.set_viewport_size({"width": width, "height": 844})
        assert message.evaluate(
            "el => el.clientWidth >= 10 * parseFloat(getComputedStyle(el).fontSize)"
        ), f"The reply notice squeezes its message at {width}px"


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


def test_bulk_correction_keeps_the_selection_and_due_times_fit(page: Page, demo: Lab):
    page.set_viewport_size({"width": 360, "height": 780})
    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/requests/?view=all")
    boxes = page.locator('tbody input[name="requests"]')
    selected = [boxes.nth(i).input_value() for i in (1, 3, 8)]
    for i in (1, 3, 8):
        boxes.nth(i).check()
    trigger = page.get_by_role("button", name=re.compile("^Change selected tickets"))
    expect(trigger).to_be_in_viewport()
    trigger.click()
    sheet = page.get_by_role("dialog", name="Change selected tickets")
    sheet.get_by_role("button", name="Apply to selected").click()
    expect(sheet).to_be_visible()
    expect(sheet.get_by_text("Choose a team or a person.")).to_be_visible()
    assert (
        page.locator('tbody input[name="requests"]:checked').evaluate_all(
            "boxes => boxes.map(box => box.value)"
        )
        == selected
    )
    page.keyboard.press("Escape")
    expect(trigger).to_be_focused()
    deadline = page.locator("tbody .vr-clock-badge time").first
    expect(deadline).to_be_attached()
    assert deadline.evaluate(
        "el => el.getBoundingClientRect().right <= "
        "el.closest('td').getBoundingClientRect().right + 1"
    )
    master = page.get_by_role("checkbox", name="Select all tickets on this page")
    master.check()
    assert boxes.count() == page.locator('tbody input[name="requests"]:checked').count()


@pytest.mark.parametrize("direct", [False, True], ids=["list", "direct"])
def test_saved_view_delete_confirmation_stays_open(page: Page, demo: Lab, direct):
    from django.urls import reverse

    from accounts.models import User
    from desk.models import SavedView
    from desk.saved_views import create_view

    owner = User.objects.get(username="demo_supervisor")
    saved = create_view(actor=owner, name="Review queue", shared_with="private", params={})
    sign_in(page, owner.username, demo.password)
    if direct:
        page.goto(reverse("desk:view-edit", args=[saved.pk]))
    else:
        page.goto(f"/requests/?view=all&saved={saved.pk}")
        page.get_by_role("link", name=re.compile("Rename, share or delete")).click()
    editor = page.get_by_role("dialog")
    editor.get_by_role("link", name="Delete view", exact=True).click()
    confirmation = page.get_by_role("dialog", name="Delete view Review queue?")
    expect(confirmation.get_by_role("button", name="Delete view", exact=True)).to_be_visible()
    assert SavedView.objects.filter(pk=saved.pk).exists()
    confirmation.get_by_role("button", name="Delete view", exact=True).click()
    expect(page.get_by_text("View “Review queue” deleted.", exact=True)).to_be_visible()
    assert not SavedView.objects.filter(pk=saved.pk).exists()
