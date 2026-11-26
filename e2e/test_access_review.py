"""Access review (/access-review/): the Accounts and Unused accounts tabs for a reviewer (the
demonstration supervisor), filtering, sorting and export, the unused account period set through the
shared form dialog (and refused when stale), the phone and dark layouts, and the refusal of staff
without the capability. Synthetic demonstration data only."""

import csv
import io
import re

import pytest
from conftest import NARROW
from django.contrib.auth import get_user_model
from helpers import field, native_select, sign_in
from playwright.sync_api import Page, expect

from audit_review import access_report

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()
REVIEWER = "demo_supervisor"
ACCOUNTS = "/access-review/accounts/"
UNUSED = "/access-review/unused/"
REASON = "Synthetic: the laboratory reviews unused accounts after five months."


def no_sideways_scroll(page: Page):
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
        "the page scrolls sideways"
    )


def rows(page: Page, name: str):
    return page.get_by_role("table", name=name).locator("tbody tr")


def test_a_reviewer_reads_filters_sorts_and_exports_the_accounts(page: Page, demo):
    sign_in(page, REVIEWER, demo.password)
    page.locator("nav.vr-sidenav").get_by_role("link", name="Access review").click()
    expect(page).to_have_url(re.compile(r"/access-review/accounts/$"))
    expect(page.get_by_role("heading", level=1)).to_have_text("Access review")
    table = rows(page, "Access review")
    row = table.filter(has_text="Demo Client services")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("Client services")
    expect(row).to_contain_text("Given by Demo Administrator (demo_admin)")
    total = table.count()
    assert total > 1

    # Filter by role: the list narrows and a chip names the filter.
    page.screenshot(path="/tmp/labops-arp-accounts.png")
    toolbar = page.get_by_role("search", name="Filter accounts")
    role = native_select(field(toolbar, "Role"))
    role.select_option(label="Client services")
    toolbar.get_by_role("button", name="Apply").click()
    expect(page).to_have_url(re.compile(r"role="))
    expect(page.locator(".vr-chips")).to_contain_text("Client services")
    expect(table.filter(has_text="Demo Client services")).to_have_count(1)
    assert table.count() < total

    # Sort by last sign-in.
    page.get_by_role("columnheader", name="Last sign-in").get_by_role("link").first.click()
    expect(page.get_by_role("columnheader", name="Last sign-in")).to_have_attribute(
        "aria-sort", "ascending"
    )
    page.get_by_role("columnheader", name="Last sign-in").get_by_role("link").first.click()
    expect(page.get_by_role("columnheader", name="Last sign-in")).to_have_attribute(
        "aria-sort", "descending"
    )

    # Export.
    with page.expect_download() as download:
        page.get_by_role("link", name="Export CSV").click()
    text = open(download.value.path(), encoding="utf-8-sig").read()
    header, *lines = list(csv.reader(io.StringIO(text)))
    assert ",".join(header).startswith("Person,Username,Kind")
    assert lines

    # Unused accounts.
    page.get_by_role("navigation", name="Access review lists").get_by_role(
        "link", name="Unused accounts"
    ).click()
    expect(page).to_have_url(re.compile(r"/access-review/unused/$"))
    expect(page.get_by_text("no sign-in for 90 days").first).to_be_visible()
    expect(
        rows(page, "Access review: unused accounts").filter(has_text="Pat Example")
    ).to_have_count(1)
    assert page.console_errors == []


def open_period_dialog(page: Page):
    page.goto(UNUSED)
    page.get_by_role("link", name="Change the period").click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#vr-form-title")).to_have_text("Unused account period")
    return dialog


def test_the_period_is_set_in_the_dialog_and_a_stale_save_keeps_the_typing(page: Page, demo):
    sign_in(page, REVIEWER, demo.password)
    unused = rows(page, "Access review: unused accounts")
    page.goto(UNUSED)
    expect(unused.filter(has_text="Pat Example")).to_have_count(1)

    dialog = open_period_dialog(page)
    field(dialog, "Unused after (days)").fill("150")
    field(dialog, "Reason").fill(REASON)
    dialog.get_by_role("button", name="Save", exact=False).last.click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_text("Unused account period saved.")).to_be_visible()
    expect(page.get_by_text("No unused accounts").first).to_be_visible()
    expect(page.get_by_text("Pat Example")).to_have_count(0)
    assert access_report.period().days == 150

    # A change saved while the form is open refuses this save and keeps what was typed.
    dialog = open_period_dialog(page)
    stored = access_report.period()
    access_report.set_period(
        actor=User.objects.get(username=REVIEWER),
        days=200,
        expected_revision=stored.revision,
        reason="Synthetic: changed by someone else meanwhile.",
    )
    field(dialog, "Unused after (days)").fill("100")
    field(dialog, "Reason").fill(REASON)
    dialog.get_by_role("button", name="Save", exact=False).last.click()
    expect(dialog).to_contain_text(access_report.STALE_PERIOD)
    expect(field(dialog, "Unused after (days)")).to_have_value("100")
    expect(field(dialog, "Reason")).to_have_value(REASON)
    assert access_report.period().days == 200
    assert page.console_errors == []


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_a_phone_fits_both_tabs_as_cards_with_the_filters_in_a_sheet(page: Page, demo, theme):
    page.set_viewport_size(NARROW)
    page.emulate_media(color_scheme=theme)
    sign_in(page, REVIEWER, demo.password)

    page.goto(ACCOUNTS)
    first = rows(page, "Access review").first
    expect(first).to_be_visible()
    no_sideways_scroll(page)
    # Stacked cards: no header row is shown and the row is a block, not a table row.
    assert first.evaluate("el => getComputedStyle(el).display") != "table-row"
    button = page.get_by_role("button", name=re.compile(r"^Filter and sort"))
    expect(button).to_be_visible()
    button.click()
    sheet = page.get_by_role("dialog", name="Filter and sort")
    expect(sheet.locator("label", has_text="Role")).to_be_visible()
    expect(sheet.get_by_role("button", name="Apply")).to_be_visible()
    no_sideways_scroll(page)
    page.screenshot(path=f"/tmp/labops-arp-phone-{theme}.png")
    page.keyboard.press("Escape")
    expect(sheet).to_be_hidden()

    page.goto(UNUSED)
    pat = rows(page, "Access review: unused accounts").filter(has_text="Pat Example")
    expect(pat).to_have_count(1)
    assert pat.evaluate("el => getComputedStyle(el).display") != "table-row"
    no_sideways_scroll(page)
    expect(page.get_by_role("button", name=re.compile(r"^Filter and sort"))).to_be_visible()
    assert page.console_errors == []


def test_staff_without_the_capability_are_offered_nothing_and_refused(page: Page, demo):
    sign_in(page, "demo_client_services", demo.password)
    page.goto("/")
    expect(page.locator("nav.vr-sidenav").get_by_role("link", name="Access review")).to_have_count(
        0
    )
    for address in (ACCOUNTS, UNUSED, f"{UNUSED}period/"):
        response = page.goto(address)
        assert response is not None and response.status == 403, address
