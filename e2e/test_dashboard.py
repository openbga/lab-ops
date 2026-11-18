"""The overview keeps its queues and directory usable as the available space changes."""

import re

import pytest
from conftest import View
from helpers import sign_in
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def test_work_queues_and_recent_organizations(page: Page, demo, view: View):
    sign_in(page, "demo_supervisor", demo.password)
    panel = page.get_by_role("region", name="Your work queues")
    recent = page.get_by_role("region", name="Recently added organizations")
    queues = panel.locator(".vr-queue-link")
    expect(queues.first).to_contain_text("Overdue tickets")
    expect(queues).to_have_count(4)
    expect(recent.locator("tbody tr")).to_have_count(8)

    def box(locator):
        return locator.bounding_box()

    assert all(box(row)["height"] >= 44 for row in queues.all())
    if view.phone:
        assert box(recent)["y"] >= box(panel)["y"] + box(panel)["height"]
        assert box(recent.locator("tbody tr").first)["y"] < view.viewport["height"] - 64
    else:
        assert box(recent)["x"] >= box(panel)["x"] + box(panel)["width"]
        # Help reduces the content width independently of the viewport.
        page.get_by_role("button", name="Help", exact=True).click()
        expect(page.locator("#vr-documentation")).to_be_visible()
        assert box(recent)["y"] >= box(panel)["y"] + box(panel)["height"]
        page.locator("[data-vr-documentation-close]:visible").click()
    assert page.evaluate("document.documentElement.scrollWidth") <= view.viewport["width"]

    control = panel.get_by_role("button", name="Queue definitions")
    control.click()
    sheet = page.get_by_role("dialog", name="Queue definitions")
    expect(sheet).to_be_focused()
    expect(sheet).to_contain_text("A service clock is past due.")
    expect(sheet).not_to_contain_text("Active organizations without an active contact")
    page.keyboard.press("Escape")
    expect(sheet).to_be_hidden()
    expect(control).to_be_focused()

    queues.first.locator(".vr-queue-count").click()
    expect(page).to_have_url(re.compile(r"/requests/\?view=overdue$"))
