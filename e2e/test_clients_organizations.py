"""An account manager adds a site, sets a contact's
responsibilities and sites, narrows a portal account to sites, and reads the organization's
timeline; tap-to-call links on a phone. Plus the phone sweep of the client pages (staff and
portal, both themes): no sideways scroll, no control under 44 px outside running text."""

import re

import pytest
from conftest import View
from helpers import field, pick, sign_in
from playwright.sync_api import Page, expect
from test_phone_layout import check, open_filter_sheet

pytestmark = pytest.mark.django_db(transaction=True)


def test_account_manager_keeps_sites_contacts_and_portal_access(page: Page, demo, view: View):
    from clients.models import Client, PortalAccess

    viewport = view.viewport
    water = Client.objects.get(account_number="EXWATER")
    sign_in(page, "demo_account_manager", demo.password)
    page.goto(f"/clients/{water.pk}/")
    expect(page.get_by_role("heading", level=1, name=water.name)).to_be_visible()

    # Tap-to-call and tap-to-email on contacts.
    contacts = page.get_by_role("region", name=re.compile("^Contacts"))
    call = contacts.locator("a[href^='tel:']").first
    expect(call).to_be_visible()
    expect(contacts.locator("a[href^='mailto:']").first).to_be_visible()
    if view.phone:
        assert call.bounding_box()["height"] >= 44

    # A new site.
    page.get_by_role("link", name="Add site").click()
    field(page, "Site name").fill("East reservoir")
    field(page, "Site code").fill("EAST")
    page.get_by_role("button", name="Add site").click()
    expect(page.get_by_text("Site saved.")).to_be_visible()
    expect(page.get_by_role("link", name="East reservoir")).to_be_visible()

    # The field sampler now also serves the new site.
    page.get_by_role("link", name="Synthetic Field Sampler").click()
    page.get_by_text("East reservoir (EAST)", exact=True).click()
    field(page, "Reason for change").fill("Covers the east reservoir too")
    page.get_by_role("button", name="Save changes").click()
    expect(page.get_by_text("Contact saved.")).to_be_visible()

    # Narrow the portal account to two sites.
    page.get_by_role("link", name=re.compile("^Set access")).click()
    page.get_by_text("South treatment plant (SOUTH)", exact=True).click()
    field(page, "Reason for change").fill("Now also samples the south plant")
    page.get_by_role("button", name="Save access").click()
    expect(page.get_by_text("Portal access saved.")).to_be_visible()
    access = PortalAccess.objects.get(user__username="demo_client_water")
    assert sorted(access.sites.values_list("code", flat=True)) == ["NORTH", "SOUTH"]

    # The timeline shows the changes; filtered to organization changes, through the sheet on a
    # phone.
    page.get_by_role("link", name="Timeline").first.click()
    expect(page.get_by_role("heading", level=1, name=re.compile("^Timeline"))).to_be_visible()
    expect(page.get_by_text("Portal access set").first).to_be_visible()
    scope = open_filter_sheet(page) if view.phone else page
    pick(field(scope, "Kind"), label="Organization changes")
    scope.get_by_role("button", name="Apply").click()
    expect(page).to_have_url(re.compile(r"kind=organization"))
    expect(page.get_by_text("Site created").first).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth") <= viewport["width"]


PHONES = [{"width": 360, "height": 780}, {"width": 430, "height": 900}]


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("phone", PHONES, ids=lambda size: f"{size['width']}px")
def test_client_pages_fit_a_phone(page: Page, demo, phone, scheme):
    from accounts.models import User
    from clients.models import Client
    from desk.services import create_request
    from taxonomy.models import RequestCategory

    group = RequestCategory.objects.create(key="e2e-group", name="E2E status and results")
    category = RequestCategory.objects.create(
        key="e2e-topic", name="Missing result", parent=group, client_may_raise=True
    )
    clinic = Client.objects.get(account_number="EXCLIN")
    request = create_request(
        actor=User.objects.get(username="demo_client"),
        category=category,
        subject="Missing report",
        first_entry_kind="reply",
        first_entry_body="Please check.",
        channel="portal",
        site=clinic.sites.get(code="MAIN"),
    )
    contact = clinic.contacts.first()
    portal_user = User.objects.get(username="demo_client")
    site = clinic.sites.first()
    page.set_viewport_size(phone)
    page.emulate_media(color_scheme=scheme)
    problems = []
    sign_in(page, "demo_account_manager", demo.password)
    for url in (
        f"/clients/{clinic.pk}/",
        f"/clients/{clinic.pk}/timeline/",
        f"/clients/{clinic.pk}/sites/new/",
        f"/clients/{clinic.pk}/sites/{site.pk}/",
        f"/clients/{clinic.pk}/contacts/{contact.pk}/",
        f"/clients/{clinic.pk}/portal-accounts/{portal_user.pk}/access/",
        "/clients/?organization_type=utility",
        "/account/notices/",
    ):
        page.goto(url)
        check(page, url, problems)
    page.context.clear_cookies()
    sign_in(page, "demo_client", demo.password)
    for url in (
        "/portal/",
        "/portal/requests/",
        "/portal/requests/new/",
        f"/portal/requests/{request.number}/",
    ):
        page.goto(url)
        check(page, url, problems)
    assert not problems, "\n".join(problems)
