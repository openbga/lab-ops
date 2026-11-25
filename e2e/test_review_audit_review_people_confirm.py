"""Confirm that column choices and row links survive changes of view and screen width."""

import re

import pytest
from helpers import sign_in
from playwright.sync_api import expect

from accounts.models import User
from clients.models import Client
from core.models import UserPreference
from core.preferences import clean

pytestmark = pytest.mark.django_db(transaction=True)


def hide_column(tab, label):
    opener = tab.get_by_role("button", name=re.compile("^Filter and sort"))
    if opener.is_visible():
        opener.click()
    tab.get_by_role("button", name="Columns", exact=True).click()
    dialog = tab.get_by_role("dialog", name="Columns")
    with tab.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/preferences/")
    ) as saved:
        dialog.get_by_role("checkbox", name=label, exact=True).uncheck()
    assert saved.value.status == 204
    tab.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    return saved.value.request.post_data_json["value"]


def test_unjudged_edit_keeps_a_valid_hidden_choice_without_an_order_entry(page, demo):
    # The existing preferences contract accepts independent, partial order and hidden lists.
    user = User.objects.get(username="demo_supervisor")
    key = "grid:audit_review.people:v1"
    initial = {"order": ["person", "changes"], "hidden": ["outside"]}
    assert clean(key, initial) == initial
    pref = UserPreference.objects.create(user=user, key=key, value=initial)
    sign_in(page, user.username, demo.password)
    path = "/audit-review/people/"
    page.goto(path)
    table = page.locator('table[data-vr-grid="audit_review.people"]')
    expect(table.locator("tbody tr").first).to_be_visible()
    expect(table.locator('thead th[data-vr-col="outside"]')).to_have_count(0)
    page.goto(path + "?calendar=none")
    hide_column(page, "Files downloaded")
    page.goto(path)
    expect(table.locator("tbody tr").first).to_be_visible()
    headings = table.locator("thead th").evaluate_all("cells => cells.map(c => c.dataset.vrCol)")
    pref.refresh_from_db()
    assert "downloads" not in headings and "downloads" in pref.value["hidden"]
    assert "outside" not in headings and "outside" in pref.value["hidden"]


@pytest.mark.parametrize("width", [360, 390, 430, 1366])
def test_organization_layout_keeps_two_absent_columns_together_at_capacity(page, demo, width):
    page.set_viewport_size({"width": width, "height": 850})
    user = User.objects.get(username="demo_supervisor")
    key = "grid:clients.clients:v2"
    # Phone and City are new columns from the perspective of this layout.
    previous = [
        "name",
        "account_number",
        "retired_a",
        "retired_b",
        "organization_type",
        "is_active",
        "email",
        "state",
    ]
    previous.extend(f"old_{i}" for i in range(60 - len(previous)))
    pref = UserPreference.objects.create(
        user=user, key=key, value={"order": previous, "hidden": ["retired_b", "old_50", "old_51"]}
    )
    sign_in(page, user.username, demo.password)
    page.goto("/clients/")
    sent = hide_column(page, "City")
    pref.refresh_from_db()
    assert pref.value == sent
    order = sent["order"]
    assert len(order) == len(set(order)) == 60
    anchor = order.index("account_number")
    assert order[anchor + 1 : anchor + 3] == ["retired_a", "retired_b"]
    assert {"phone", "city"} <= set(order)
    # adapted (confirmation round): a saved hidden choice is kept even where the order has no
    # room for its column, so the layout's own hidden IDs stay hidden (static/js/grid.js, save)
    assert sent["hidden"] == ["city", "retired_b", "old_50", "old_51"]
    page.reload()
    expect(page.locator("table tbody tr").first).to_be_visible()
    expect(page.locator('th[data-vr-col="city"]')).to_have_count(0)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_short_and_wrapping_row_titles_fit_phone_cards_and_desktop(page, demo, theme):
    page.emulate_media(color_scheme=theme)
    long_name = "Q" * 110
    for suffix, name in [("S", "Q"), ("L", long_name)]:
        Client.objects.create(name=name, account_number=f"CONFIRM{suffix}")
    sign_in(page, "demo_supervisor", demo.password)
    samples = [
        ("/clients/?q=CONFIRM", 'tbody th > a.vr-row-link:text-is("Q")'),
        ("/clients/?q=CONFIRM", f'tbody th > a.vr-row-link:text-is("{long_name}")'),
        ("/requests/?view=all", "tbody th > a.vr-row-link"),
        ("/quality/summary/", "tfoot td > a"),
    ]
    for width in (360, 390, 430, 1366):
        page.set_viewport_size({"width": width, "height": 850})
        for path, selector in samples:
            page.goto(path)
            link = page.locator(selector).first
            expect(link).to_be_visible()
            dims = link.evaluate("""el => {
                const r = el.getBoundingClientRect(), c = el.parentElement.getBoundingClientRect();
                return {left:r.left, right:r.right, width:r.width, height:r.height, cellLeft:c.left,
                        cellRight:c.right, body:document.documentElement.scrollWidth};
            }""")
            assert dims["body"] <= width, (path, width, dims)
            if width < 576:
                assert dims["width"] >= 43.5 and dims["height"] >= 43.5, (path, width, dims)
                assert (
                    dims["left"] >= dims["cellLeft"] - 1 and dims["right"] <= dims["cellRight"] + 1
                )
            else:
                expect(link.locator("xpath=ancestor::table/thead")).to_be_visible()
