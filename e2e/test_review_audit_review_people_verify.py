"""Browser verification of the shared grid and phone changes, using synthetic records."""

import re

import pytest
from helpers import sign_in
from playwright.sync_api import expect

from accounts.models import User
from clients.models import Client
from core.models import UserPreference
from core.preferences import clean

pytestmark = pytest.mark.django_db(transaction=True)


def change_columns(tab, label, *, move=False):
    tab.get_by_role("button", name="Columns", exact=True).click()
    modal = tab.get_by_role("dialog", name="Columns")
    with tab.expect_response(
        lambda reply: reply.url.endswith("/preferences/") and reply.request.method == "POST"
    ) as saved:
        if move:
            modal.get_by_role("button", name=f"Move {label} up", exact=True).click()
        else:
            modal.get_by_role("checkbox", name=label, exact=True).uncheck()
    tab.keyboard.press("Escape")
    expect(modal).to_be_hidden()
    return saved.value


@pytest.mark.parametrize("old_size", [8, 59, 60])
def test_client_grid_accepts_a_new_choice_after_a_column_is_renamed(page, demo, old_size):
    # A real, accepted prior format for this grid: the old city ID and, at the limit, retired IDs.
    columns = [
        "name",
        "account_number",
        "organization_type",
        "is_active",
        "email",
        "phone",
        "old_city",
        "state",
    ]
    previous = columns + [f"retired_{i}" for i in range(old_size - len(columns))]
    key = "grid:clients.clients:v2"
    old_layout = {"order": previous, "hidden": ["old_city"]}
    assert clean(key, old_layout) == old_layout
    user = User.objects.get(username="demo_supervisor")
    UserPreference.objects.create(user=user, key=key, value=old_layout)
    sign_in(page, user.username, demo.password)
    page.goto("/clients/")
    header = page.locator('th[data-vr-col="city"]')
    expect(header).to_be_visible()
    response = change_columns(page, "City")
    sent = response.request.post_data_json["value"]
    assert response.status == 204, (response.status, len(sent["order"]), response.text())
    page.reload()
    expect(header).to_have_count(0)
    assert "city" in UserPreference.objects.get(user=user, key=key).value["hidden"]


def test_unjudged_edit_keeps_saved_position_of_the_outside_column(page, demo):
    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/audit-review/people/")
    assert change_columns(page, "Outside business hours", move=True).ok
    heading = page.locator("table thead th")
    page.reload()
    original = [h.get_attribute("data-vr-col") for h in heading.all()]
    assert original.index("outside") < original.index("changes")
    page.goto("/audit-review/people/?calendar=none")
    assert change_columns(page, "Files downloaded").ok
    page.goto("/audit-review/people/")
    after = [h.get_attribute("data-vr-col") for h in heading.all()]
    assert after == [col for col in original if col != "downloads"]


@pytest.mark.xfail(
    strict=True,
    reason="documented limit (verification round): as on every list, when two tabs change the "
    "columns the layout saved last wins; a tab opened earlier does not see the other tab's choice",
)
def test_older_unjudged_tab_keeps_the_outside_choice_saved_from_another_tab(page, demo):
    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/audit-review/people/")
    other = page.context.new_page()
    try:
        other.goto("/audit-review/people/?calendar=none")
        assert change_columns(page, "Outside business hours").ok
        assert change_columns(other, "Files downloaded").ok
        page.reload()
        headings = page.locator("table thead")
        expect(headings).not_to_contain_text("Files downloaded")
        expect(headings).not_to_contain_text("Outside business hours")
    finally:
        other.close()


def test_touch_targets_on_organization_ticket_and_quality_tables(page, demo):
    Client.objects.create(name="A", email="x@example.invalid", account_number="TAPCHECK")
    sign_in(page, "demo_supervisor", demo.password)
    samples = [
        ("/clients/", 'tbody th > a.vr-row-link:text-is("A")'),
        ("/clients/", 'a[href="mailto:x@example.invalid"]'),
        ("/requests/?view=all", "tbody th > a.vr-row-link"),
        ("/quality/summary/", "tfoot td > a"),
    ]
    bad = []
    for width in (360, 390, 430):
        page.set_viewport_size({"width": width, "height": 850})
        for path, selector in samples:
            page.goto(path)
            link = page.locator(selector).first
            expect(link).to_be_visible()
            box = link.bounding_box()
            if min(box["width"], box["height"]) < 43.5:
                bad.append((width, path, selector, box["width"], box["height"]))
            assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert not bad, bad


def test_apply_and_clear_all_drop_the_carried_reading(page, demo):
    sign_in(page, "demo_supervisor", demo.password)
    path = "/audit-review/people/"
    query = "?reading_from=2024-01-15&reading_to=2024-01-16&reading_calendar=none"
    page.goto(path + query + "&calendar=none")
    toolbar = page.get_by_role("search", name="Filter people")
    expect(page.locator(".vr-page-intro")).to_contain_text("15 January 2024")
    clear = toolbar.get_by_role("link", name="Clear all")
    assert clear.get_attribute("href") == path
    assert not toolbar.locator('input[name^="reading_"], input[name^="upto_"]').count()
    toolbar.get_by_role("button", name="Apply", exact=True).click()
    expect(page.locator(".vr-page-intro")).not_to_contain_text("15 January 2024")
    assert "reading_" not in page.url
    clear.click()
    expect(page).to_have_url(re.compile(r"/audit-review/people/$"))
    expect(page.get_by_role("link", name="Clear all")).to_have_count(0)


def test_two_tabs_of_the_client_grid_restore_the_last_saved_layout(page, demo):
    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/clients/")
    second = page.context.new_page()
    try:
        second.goto("/clients/")
        assert change_columns(page, "City").ok
        last = change_columns(second, "Phone")
        assert last.ok
        sent = last.request.post_data_json["value"]
        assert "phone" in sent["hidden"] and "city" not in sent["hidden"]
        page.reload()
        expect(page.locator('th[data-vr-col="city"]')).to_be_visible()
        expect(page.locator('th[data-vr-col="phone"]')).to_have_count(0)
    finally:
        second.close()
