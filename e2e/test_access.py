"""Sign-in, role landing and server-side denial, exercised through the real browser."""

import re
from pathlib import Path

import pytest
from conftest import DESKTOP, NARROW, Lab
from helpers import sign_in, sign_out
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def test_staff_roles_land_on_workspace_and_denied_actions_are_refused(
    page: Page, sign_in_as, demo: Lab
):
    # Anonymous access to a staff page goes to sign-in and returns after authentication.
    page.goto("/clients/")
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next=/clients/$"))

    # A wrong password is rejected inline and keeps the typed login.
    sign_in(page, "demo_account_manager", "not-the-password", visit=False)
    expect(page.get_by_role("alert")).to_contain_text("not correct")
    expect(page.get_by_label(re.compile("^(Username or email|Login)"))).to_have_value(
        "demo_account_manager"
    )

    # The account manager signs in, returns to the requested page and may add organizations.
    sign_in(page, "demo_account_manager", demo.password, visit=False)
    expect(page).to_have_url(re.compile(r"/clients/$"))
    expect(page.get_by_role("heading", level=1, name="Client organizations")).to_be_visible()
    page.goto("/")
    expect(page.get_by_role("heading", level=1, name="Dashboard")).to_be_visible()
    expect(page.get_by_role("link", name="New organization")).to_be_visible()
    assert page.console_errors == []
    sign_out(page)

    # The technical role reads but has no button to add one, and the server refuses the URL.
    sign_in(page, "demo_technical", demo.password)
    expect(page.get_by_role("heading", level=1, name="Dashboard")).to_be_visible()
    expect(page.get_by_role("link", name="New organization")).to_have_count(0)
    response = page.goto("/clients/new/")
    assert response.status == 403
    expect(page.get_by_role("heading", level=1)).to_have_text("You do not have access to this page")
    page.go_back()
    sign_out(page)


STAFF_ITEMS = ["Dashboard", "Tickets", "Teams", "Client organizations", "Laboratory profile"]
ADMIN_ONLY = ["Record retention", "Announcements"]


def test_client_lands_in_portal_and_cannot_use_staff_routes(page: Page, sign_in_as):
    sign_in_as("client")
    expect(page).to_have_url(re.compile(r"/portal/$"))
    expect(page.get_by_role("heading", level=1, name="Example Family Clinic")).to_be_visible()
    expect(page.get_by_role("navigation", name="Portal").get_by_role("link")).to_have_text(
        ["Home", "Tickets"]
    )
    # Staff navigation is absent, and staff URLs are refused by the server.
    expect(page.get_by_role("link", name="Client organizations")).to_have_count(0)
    for path in ("/clients/", "/clients/new/", "/lab/profile/", "/lab/announcements/"):
        assert page.goto(path).status == 403, path
    assert page.goto("/").url.endswith("/portal/")


def test_portal_account_sees_only_its_own_organization_and_staff_have_no_portal(
    page: Page, sign_in_as, demo: Lab
):
    from clients.models import Client

    sign_in_as("client_water")
    heading = page.get_by_role("heading", level=1, name="Example County Water Authority")
    expect(heading).to_be_visible()
    expect(page.get_by_role("main")).not_to_contain_text("Example Family Clinic")
    # Another organization's record is a staff page: the portal account is refused it.
    clinic = Client.objects.get(name="Example Family Clinic")
    assert page.goto(f"/clients/{clinic.pk}/").status == 403
    page.goto("/portal/")
    sign_out(page)

    # A staff account has no portal.
    sign_in(page, "demo_admin", demo.password)
    assert page.goto("/portal/").status == 403


@pytest.mark.parametrize("viewport", [DESKTOP, NARROW], ids=["desktop", "mobile"])
@pytest.mark.parametrize(
    ("role", "absent"),
    [
        ("admin", ["Accounts & roles"]),  # a deployment-owner (superuser) destination
        ("supervisor", [*ADMIN_ONLY, "Accounts & roles"]),
        ("client_services", [*ADMIN_ONLY, "Accounts & roles"]),
        ("account_manager", [*ADMIN_ONLY, "Accounts & roles"]),
        ("technical", [*ADMIN_ONLY, "Accounts & roles"]),
    ],
)
def test_role_menu_on_desktop_and_mobile(page: Page, sign_in_as, request, viewport, role, absent):
    page.set_viewport_size(viewport)
    sign_in_as(role)
    nav = page.locator("#vr-nav")  # Bootstrap exposes the open mobile rail as a dialog.
    mobile = viewport == NARROW
    if mobile:
        page.get_by_role("button", name="Menu", exact=True).click()
    expect(nav).to_be_visible()
    for label in absent:
        expect(nav.get_by_role("link", name=label, exact=True)).to_have_count(0)
    expect(nav.locator(".vr-nav-heading")).to_have_text(["Service desk", "Clients", "Setup"])
    for label in STAFF_ITEMS + (ADMIN_ONLY if role == "admin" else []):
        expect(nav.get_by_role("link", name=label, exact=True)).to_be_visible()
    destination = "Client organizations"
    link = nav.get_by_role("link", name=destination, exact=True)
    # Keyboard activation works in the responsive menu as well as the desktop rail.
    link.focus()
    expect(link).to_be_focused()
    if output := request.config.getoption("--screenshots-dir"):
        directory = Path(output)
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=directory / f"menu-{role}-{viewport['width']}.png")
    link.press("Enter")
    expect(page.get_by_role("heading", level=1, name=destination, exact=True)).to_be_visible()
    if mobile:
        page.get_by_role("button", name="Menu", exact=True).click()
    expect(nav.get_by_role("link", name=destination, exact=True)).to_have_attribute(
        "aria-current", "page"
    )
    if mobile:
        expect(nav).to_have_class(re.compile(r"\bshow\b"))
        nav.press("Escape")
        expect(nav).to_be_hidden()
    page.locator(".vr-topbar [data-vr-documentation-toggle]").click()
    panel = page.locator("#vr-documentation")
    expect(panel).to_be_visible()
    # The reference sits under the page's tasks, collapsed; its filter opens the matching section.
    panel.get_by_role("searchbox", name="Find a field or action").fill("Account code")
    expect(panel.get_by_role("term").filter(has_text="Account code").first).to_be_visible()
    assert page.console_errors == []
