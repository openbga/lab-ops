"""The frame's core journey: a laboratory administrator signs in, opens the dashboard, creates a
client organization with a contact, opens a portal account for it, and that client's portal
user sees only their own organization's workspace."""

import re

import pytest
from conftest import DESKTOP, NARROW
from helpers import field, make_user, sign_in
from playwright.sync_api import Page, expect

ORGANIZATION = "E2E Synthetic Diagnostics Group"


@pytest.mark.parametrize("viewport", [DESKTOP, NARROW], ids=["desktop", "phone"])
def test_new_client_organization_and_its_portal_user(page: Page, demo, viewport):
    """Run at a desktop and a phone width: on a phone the menu and the page's primary action
    sit in the thumb bar at the bottom of the screen."""
    from clients.models import Client

    page.set_viewport_size(viewport)
    phone = viewport == NARROW

    owner = make_user("e2e_owner", demo.password, "admin")
    owner.is_staff = owner.is_superuser = True
    owner.save(update_fields=["is_staff", "is_superuser"])

    sign_in(page, "e2e_owner", demo.password)
    expect(page.get_by_role("heading", level=1, name="Dashboard")).to_be_visible()
    # The primary navigation; on a phone it is a drawer, which Bootstrap gives the dialog role.
    nav = page.locator("#vr-nav")
    if phone:
        thumbbar = page.get_by_role("navigation", name="Quick actions")
        expect(thumbbar.get_by_role("link", name="Find", exact=True)).to_be_visible()
        thumbbar.get_by_role("button", name="Menu").click()
    expect(nav.get_by_role("link", name="Client organizations")).to_be_visible()
    expect(nav.get_by_role("link", name="Accounts & roles")).to_be_visible()
    if phone:
        nav.get_by_role("button", name="Close navigation").click()
        expect(nav.get_by_role("link", name="Client organizations")).to_be_hidden()
        # The primary action is within thumb reach: in the bottom quarter of the screen.
        box = page.get_by_role("link", name="New organization").first.bounding_box()
        assert box["y"] > viewport["height"] * 0.75 and box["height"] >= 44

    if phone:
        # Lists stack into cards; the toolbar and the server-side "Sort by" chips open from one
        # "Filter and sort" button (the sheet is covered in test_phone_layout.py).
        page.goto("/clients/")
        page.get_by_role("button", name=re.compile("^Filter and sort")).click()
        sort = page.get_by_role("dialog", name="Filter and sort").get_by_role(
            "link", name=re.compile("^Organization")
        )
        expect(sort).to_be_visible()
        assert sort.bounding_box()["height"] >= 44
        sort.click()
        expect(page).to_have_url(re.compile(r"sort=-?name"))
        expect(
            page.locator("tbody tr").first.locator("[data-vr-label='Account code']")
        ).to_be_visible()
        page.goto("/")

    # Create the organization from the dashboard's primary action.
    page.get_by_role("link", name="New organization").first.click()
    field(page, "Organization name").fill(ORGANIZATION)
    field(page, "Account code").fill("E2EDIAG")
    field(page, "City").fill("Exampleville")
    page.get_by_role("button", name="Create organization").click()
    expect(page.get_by_role("heading", level=1, name=ORGANIZATION)).to_be_visible()
    expect(page.get_by_text(f"{ORGANIZATION} added.")).to_be_visible()
    client = Client.objects.get(account_number="E2EDIAG")

    # A contact the portal user will see as being on file.
    page.get_by_role("link", name="Add contact").click()
    field(page, "Name").fill("E2E Synthetic Practice Lead")
    field(page, "Email").fill("lead@diagnostics.example.invalid")
    page.get_by_role("button", name="Add contact").click()
    expect(page.get_by_text("Contact saved.")).to_be_visible()
    expect(page.get_by_role("link", name="E2E Synthetic Practice Lead")).to_be_visible()

    # Portal access is opened by an administrator; the admin form arrives with the
    # organization chosen.
    page.get_by_role("link", name="Add portal account").click()
    expect(page).to_have_url(re.compile(rf"/admin/accounts/user/add/\?client={client.pk}$"))
    page.get_by_label("Username").fill("e2e_portal")
    page.get_by_label("Email").fill("portal@diagnostics.example.invalid")
    page.locator("#id_password1").fill(demo.password)
    page.locator("#id_password2").fill(demo.password)
    expect(page.locator("#id_client")).to_have_value(str(client.pk))
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_text(re.compile("was added successfully"))).to_be_visible()

    page.goto(f"/clients/{client.pk}/")
    portal_accounts = page.get_by_role("region", name=re.compile("^Portal accounts"))
    expect(portal_accounts.get_by_text("portal@diagnostics.example.invalid")).to_be_visible()

    # The portal user sees their own organization's workspace and nothing of another client.
    page.context.clear_cookies()
    sign_in(page, "e2e_portal", demo.password)
    expect(page).to_have_url(re.compile(r"/portal/$"))
    expect(page.get_by_role("heading", level=1, name=ORGANIZATION)).to_be_visible()
    expect(page.get_by_text("E2E Synthetic Practice Lead")).to_be_visible()
    expect(page.get_by_text("Example Family Clinic")).to_have_count(0)
    expect(page.get_by_text("Synthetic Office Manager")).to_have_count(0)
    expect(page.get_by_role("navigation", name="Primary")).to_have_count(0)
    expect(page.get_by_role("navigation", name="Quick actions")).to_have_count(0)
    if phone:
        home = page.get_by_role("navigation", name="Portal").get_by_role("link", name="Home")
        box = home.bounding_box()
        assert box["y"] > viewport["height"] * 0.75 and box["height"] >= 44
    assert page.evaluate("document.documentElement.scrollWidth") <= viewport["width"]

    # Staff pages stay closed to the portal account, including another client's record.
    other = Client.objects.get(account_number="EXCLIN")
    for url in ("/clients/", f"/clients/{other.pk}/", f"/clients/{client.pk}/"):
        response = page.goto(url)
        assert response.status == 403, url
    page.goto("/")
    expect(page).to_have_url(re.compile(r"/portal/$"))
