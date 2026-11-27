"""Verify long values in the Deactivate preview and the account's historical record."""

import pytest
from helpers import make_user, sign_in
from playwright.sync_api import expect
from test_deactivation import OWNER, OWNER_PASSWORD, PASSWORD, make_owner
from test_review_deactivation import assert_text_fits

from clients.models import Client, ClientSite, PortalAccess

pytestmark = pytest.mark.django_db(transaction=True)


def test_long_organization_and_site_names_fit_preview_and_history(page, view):
    organization = Client.objects.create(name="Organization" * 12, account_number="WRAP")
    site = ClientSite.objects.create(client=organization, code="WEST", name="Laboratory" * 14)
    account = make_user("verify-layout", PASSWORD, client=organization)
    access = PortalAccess.objects.create(user=account, client=organization)
    access.sites.add(site)
    make_owner()
    sign_in(page, OWNER, OWNER_PASSWORD)
    page.goto(f"/admin/accounts/user/{account.pk}/deactivate/")
    for phase in ("preview", "history"):
        held = page.locator("ul.vr-held")
        for value in (organization.name, site.name):
            entry = held.get_by_role("listitem").filter(has_text=value)
            expect(entry).to_be_visible()
            assert_text_fits(entry)
        if phase == "preview":
            page.get_by_label("Reason:").fill("Synthetic layout check")
            page.get_by_role("button", name="Deactivate", exact=True).click()
            expect(page.get_by_text("Reason: Synthetic layout check", exact=True)).to_be_visible()


def test_long_valid_username_fits_the_deactivate_page(page, view):
    username = "LaboratoryOperator" * 6
    account = make_user(username, PASSWORD)
    make_owner()
    sign_in(page, OWNER, OWNER_PASSWORD)
    page.goto(f"/admin/accounts/user/{account.pk}/deactivate/")
    heading = page.get_by_role("heading", name=f"Deactivate: {username}", exact=True)
    expect(heading).to_be_visible()
    assert_text_fits(heading)
