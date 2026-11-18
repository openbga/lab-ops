"""Spreadsheet import (M2-A): an account manager checks an organizations file with an error,
uploads a corrected one, imports it, and sees the result; the preview stacks into cards on a
phone. Run in every `view` (1366 and 390 px, light and dark), plus the phone sweep of the import
pages at 360 and 430 px."""

import pytest
from conftest import View
from helpers import sign_in
from playwright.sync_api import Page, expect
from test_phone_layout import check

pytestmark = pytest.mark.django_db(transaction=True)

BAD = (
    "account_code,name,organization_type,state\r\n"
    "E2EA,E2E Synthetic Alpha Clinic,practice,NY\r\n"
    "E2EB,E2E Synthetic Beta Plant,spaceship,PA\r\n"
)
GOOD = BAD.replace("spaceship", "manufacturer") + "E2EC,@E2E Synthetic Gamma,other,\r\n"
FIXED = BAD.replace("spaceship", "manufacturer")


def choose_file(page, text, name="organizations.csv"):
    page.locator("input[type=file]").set_input_files(
        {"name": name, "mimeType": "text/csv", "buffer": text.encode()}
    )


def test_account_manager_imports_organizations(page: Page, demo, view: View):
    from clients.models import Client

    sign_in(page, "demo_account_manager", demo.password)
    if view.phone:
        page.get_by_role("navigation", name="Quick actions").get_by_role(
            "button", name="Menu"
        ).click()
    page.locator("#vr-nav").get_by_role("link", name="Spreadsheet import").click()
    expect(page.get_by_role("heading", level=1, name="Spreadsheet import")).to_be_visible()
    page.get_by_role("link", name="Import organizations").click()

    # A file with an error is refused as a whole; the error row is listed first.
    choose_file(page, BAD)
    page.get_by_role("button", name="Check file").click()
    expect(page.get_by_text("has 1 error")).to_be_visible()
    first = page.locator("table.vr-table tbody tr").first
    expect(first).to_contain_text("E2EB")
    expect(first).to_contain_text("'spaceship' is not allowed")
    assert not Client.objects.filter(account_number__startswith="E2E").exists()
    assert page.evaluate("document.documentElement.scrollWidth") <= view.viewport["width"]
    if view.phone:  # the preview is stacked rows: the record is each card's title
        expect(first.locator("[data-vr-label='Problems']")).to_be_visible()

    # A corrected file previews, flags a formula-like cell, and imports.
    page.get_by_role("link", name="Upload a corrected file").click()
    choose_file(page, GOOD)
    page.get_by_role("button", name="Check file").click()
    expect(page.get_by_text("3 rows checked")).to_be_visible()
    expect(page.get_by_text("like a spreadsheet formula")).to_be_visible()
    assert not Client.objects.filter(account_number__startswith="E2E").exists()
    page.get_by_role("button", name="Import 3 rows").click()
    expect(page.get_by_text("Imported organizations.csv: 3 created")).to_be_visible()
    assert Client.objects.get(account_number="E2EC").state == ""
    assert Client.objects.get(account_number="E2EB").organization_type == "manufacturer"


@pytest.mark.parametrize(
    "phone",
    [{"width": 360, "height": 780}, {"width": 430, "height": 900}],
    ids=lambda size: f"{size['width']}px",
)
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_import_pages_fit_a_phone(page: Page, demo, phone, scheme):
    page.set_viewport_size(phone)
    page.emulate_media(color_scheme=scheme)
    problems = []
    sign_in(page, "demo_account_manager", demo.password)
    for url in ("/import/", "/import/organizations/", "/import/contacts/", "/import/external-ids/"):
        page.goto(url)
        check(page, url, problems)
    page.goto("/import/organizations/")
    choose_file(page, BAD)
    page.get_by_role("button", name="Check file").click()
    expect(page.get_by_text("has 1 error")).to_be_visible()
    check(page, "preview with errors", problems)
    page.goto("/import/organizations/")
    choose_file(page, FIXED)
    page.get_by_role("button", name="Check file").click()
    expect(page.get_by_text("2 rows checked")).to_be_visible()
    check(page, "preview ready", problems)
    assert not problems, "\n".join(problems)
