"""External record links at desktop and phone widths in both themes."""

import re

import pytest
from conftest import View
from django.urls import reverse
from helpers import field, make_user, pick, sign_in
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

PATIENT_KEY = "PT-TEST-00004821"


def grant_patient_capability(user):
    from django.contrib.auth.models import Permission

    user.user_permissions.add(
        Permission.objects.get(
            codename="view_patient_information", content_type__app_label="accounts"
        )
    )


def demo_request(**extra):
    from accounts.models import User
    from clients.models import Client
    from desk.services import create_request
    from taxonomy.models import RequestCategory

    return create_request(
        actor=User.objects.get(username="demo_client_services"),
        category=RequestCategory.objects.get(key="missing-result"),
        subject="Synthetic missing result",
        first_entry_kind="note",
        first_entry_body="Caller asked about a synthetic order.",
        channel="phone",
        client=Client.objects.get(account_number="EXCLIN"),
        **extra,
    )


def dialog_or_page(page: Page):
    dialog = page.get_by_role("dialog")
    return dialog if dialog.count() else page


def test_link_an_organization_and_find_its_requests(page: Page, demo, view: View):
    from links.models import ExternalRecordLink

    sign_in(page, "demo_account_manager", demo.password)
    from clients.models import Client

    clinic = Client.objects.get(account_number="EXCLIN")
    page.goto(f"/links/clients/{clinic.pk}/new/")
    expect(page.get_by_role("heading", level=1, name="Add link")).to_be_visible()
    scope = dialog_or_page(page)
    pick(field(scope, "System"), label="Example LIS")
    pick(field(scope, "Record type"), label="Project")
    # Person-level types are never offered on an organization.
    assert scope.locator("option[value='patient']").count() == 0
    field(scope, "Identifier").fill("PROJ-TEST-0042")
    field(scope, "Address").fill("https://elsewhere.example.invalid/PROJ-TEST-0042")
    scope.get_by_role("button", name="Add link").click()
    expect(page.get_by_text("The address must be on the system's own host")).to_be_visible()
    field(dialog_or_page(page), "Address").fill(
        "https://lis.example.invalid/projects/PROJ-TEST-0042"
    )
    dialog_or_page(page).get_by_role("button", name="Add link").click()
    expect(page.get_by_text("Link added.")).to_be_visible()
    link = ExternalRecordLink.objects.get(identifier="PROJ-TEST-0042")

    page.goto(f"/links/{link.pk}/")
    expect(page.get_by_role("heading", level=1, name="PROJ-TEST-0042")).to_be_visible()
    expect(page.get_by_role("link", name="Example Family Clinic")).to_be_visible()
    if view.phone:
        assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0


def test_patient_link_with_and_without_the_capability(page: Page, demo, view: View):
    from links.models import LinkAttachment

    holder = make_user("e2e_holder", demo.password, "client_services")
    grant_patient_capability(holder)
    make_user("e2e_plain", demo.password, "client_services")
    service_request = demo_request()
    add_url = f"/links/requests/{service_request.pk}/new/"

    sign_in(page, "e2e_holder", demo.password)
    page.goto(add_url)
    scope = dialog_or_page(page)
    expect(scope.get_by_text("Person-level: type the source system's key only")).to_be_visible()
    pick(field(scope, "Record type"), label="Patient (person-level)")
    field(scope, "Identifier").fill(PATIENT_KEY)
    field(scope, "Label").fill(f"Patient {PATIENT_KEY}")
    scope.get_by_role("button", name="Add link").click()
    expect(page.get_by_text("The label must not contain the whole key")).to_be_visible()
    field(dialog_or_page(page), "Label").fill("")
    dialog_or_page(page).get_by_role("button", name="Add link").click()
    expect(page.get_by_text("Link added.")).to_be_visible()
    attachment = LinkAttachment.objects.get(request=service_request)
    assert attachment.link.display_label == "Patient ····4821"
    assert PATIENT_KEY not in page.url

    # Exact, POST-only search; the key is not written back into the page.
    page.goto("/links/find-person/")
    field(page, "Patient, participant or donor key").fill(PATIENT_KEY)
    page.get_by_role("button", name="Search").click()
    expect(page.get_by_role("link", name=service_request.number)).to_be_visible()
    assert PATIENT_KEY not in page.content() and PATIENT_KEY not in page.url

    page.goto(f"/links/{attachment.link_id}/")
    expect(page.get_by_role("heading", level=1, name="Patient link")).to_be_visible()
    assert PATIENT_KEY not in page.content()

    # Without the capability: no person types, no link page, no search form.
    page.context.clear_cookies()
    sign_in(page, "e2e_plain", demo.password)
    page.goto(add_url)
    assert dialog_or_page(page).locator("option[value='patient']").count() == 0
    assert page.goto(f"/links/{attachment.link_id}/").status == 404
    page.goto("/links/find-person/")
    expect(page.get_by_text("Permission needed")).to_be_visible()
    expect(page.get_by_label(re.compile("key"))).to_have_count(0)


def test_request_page_links_section_and_reveal(page: Page, demo, view: View):
    from links.models import ExternalSystem
    from links.services import add_link

    holder = make_user("e2e_holder", demo.password, "client_services")
    grant_patient_capability(holder)
    make_user("e2e_plain", demo.password, "client_services")
    service_request = demo_request(client_reference="ORD-TEST-000042")
    lis = ExternalSystem.objects.get(key="lis")
    add_link(
        actor=holder,
        request_id=service_request.pk,
        system=lis,
        record_type="patient",
        identifier=PATIENT_KEY,
    )

    sign_in(page, "e2e_holder", demo.password)
    page.goto(reverse("desk:request-detail", args=[service_request.pk]))
    links = page.locator("#links")
    expect(links.get_by_text("Patient ····4821")).to_be_visible()
    expect(links.get_by_text("ORD-TEST-000042")).to_be_visible()
    links.get_by_role("button", name="Reveal key").click()
    expect(links.get_by_text(PATIENT_KEY)).to_be_visible()
    assert PATIENT_KEY not in page.url
    links.get_by_role("link", name="Make a link").click()
    scope = dialog_or_page(page)
    expect(field(scope, "Identifier")).to_have_value("ORD-TEST-000042")
    pick(field(scope, "Record type"), label="Order")
    scope.get_by_role("button", name="Add link").click()
    expect(page.locator("#links").get_by_role("link", name="ORD-TEST-000042")).to_be_visible()
    if view.phone:
        assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0

    page.context.clear_cookies()
    sign_in(page, "e2e_plain", demo.password)
    page.goto(reverse("desk:request-detail", args=[service_request.pk]))
    links = page.locator("#links")
    expect(links.get_by_text("Patient linked")).to_be_visible()
    assert "4821" not in links.inner_text() and PATIENT_KEY not in page.content()


def test_request_page_shows_both_clocks(page: Page, demo, view: View):
    service_request = demo_request(priority="urgent")
    sign_in(page, "demo_client_services", demo.password)
    page.goto(reverse("desk:request-detail", args=[service_request.pk]))
    clocks = page.get_by_role("list", name="Service clocks")
    expect(clocks.get_by_text("First response")).to_be_visible()
    expect(clocks.get_by_text("Met", exact=True)).to_be_visible()  # answered at intake
    expect(clocks.get_by_text("Resolution")).to_be_visible()


def test_organization_page_links_panel(page: Page, demo, view: View):
    """Organization links: add, see and remove a link."""
    from clients.models import Client
    from links.models import LinkAttachment

    water = Client.objects.get(account_number="EXWATER")
    sign_in(page, "demo_account_manager", demo.password)
    page.goto(f"/clients/{water.pk}/")
    links = page.locator("#links")
    expect(links.get_by_role("link", name="Distribution monitoring project")).to_be_visible()
    links.get_by_role("link", name="Add link").click()
    scope = dialog_or_page(page)
    pick(field(scope, "System"), label="Example Environmental LIMS")
    pick(field(scope, "Record type"), label="Sample")
    field(scope, "Identifier").fill("SMP-TEST-0105")
    scope.get_by_role("button", name="Add link").click()
    expect(page.get_by_text("Link added.")).to_be_visible()
    links = page.locator("#links")
    expect(links.get_by_role("link", name="SMP-TEST-0105")).to_be_visible()
    if view.phone:
        assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
        assert links.get_by_role("link", name="Add link").bounding_box()["height"] >= 44

    row = links.get_by_role("row").filter(has=page.get_by_role("link", name="SMP-TEST-0105"))
    row.get_by_role("link", name="Remove").click()
    scope = dialog_or_page(page)
    field(scope, "Reason for removal").fill("Sample belongs to another project")
    scope.get_by_role("button", name="Remove link").click()
    expect(page.get_by_text("Link removed. Its history stays.")).to_be_visible()
    expect(page.locator("#links").get_by_text("Removed links (1)")).to_be_visible()
    attachment = LinkAttachment.objects.get(link__identifier="SMP-TEST-0105")
    assert attachment.removal_reason == "Sample belongs to another project"


def test_person_link_disclosure_on_ticket_page(page: Page, demo, view: View):
    """No address on a person link, a fixed removal reason
    with a private note, no reveal after removal, and a placeholder-only removed row."""
    from links.models import ExternalSystem, LinkAttachment
    from links.services import add_link

    holder = make_user("e2e_holder", demo.password, "client_services")
    grant_patient_capability(holder)
    make_user("e2e_plain", demo.password, "client_services")
    service_request = demo_request()
    lis = ExternalSystem.objects.get(key="lis")
    attachment, _ = add_link(
        actor=holder,
        request_id=service_request.pk,
        system=lis,
        record_type="patient",
        identifier=PATIENT_KEY,
    )
    request_url = reverse("desk:request-detail", args=[service_request.pk])

    sign_in(page, "e2e_holder", demo.password)
    # An address on a person link is refused, with the typed text kept.
    page.goto(f"/links/requests/{service_request.pk}/new/")
    scope = dialog_or_page(page)
    pick(field(scope, "Record type"), label="Research participant (person-level)")
    field(scope, "Identifier").fill("PARTICIPANT-TEST-0007")
    field(scope, "Address").fill("https://lis.example.invalid/participants/PARTICIPANT-TEST-0007")
    scope.get_by_role("button", name="Add link").click()
    expect(page.get_by_text("Person-level links have no web address").first).to_be_visible()

    page.goto(request_url)
    links = page.locator("#links")
    row = links.get_by_role("row").filter(has_text="Patient ····4821")
    row.get_by_role("button", name="Actions").click()
    row.get_by_role("link", name="Remove link").click()
    scope = dialog_or_page(page)
    pick(field(scope, "Reason for removal"), label="Wrong person")
    field(scope, "Note").fill("Synthetic: matched the clinic's other patient")
    scope.get_by_role("button", name="Remove link").click()
    expect(page.get_by_text("Link removed. Its history stays.")).to_be_visible()
    links = page.locator("#links")
    expect(links.get_by_role("button", name="Reveal key")).to_have_count(0)
    links.get_by_text("Removed links (1)").click()
    expect(links.get_by_text("Wrong person")).to_be_visible()
    expect(links.get_by_text("matched the clinic's other patient")).to_be_visible()
    token = next(c["value"] for c in page.context.cookies() if c["name"] == "csrftoken")
    refused = page.request.post(
        f"/links/attachments/{attachment.pk}/reveal/",
        headers={"X-CSRFToken": token, "HX-Request": "true"},
    )
    assert refused.status == 404 and PATIENT_KEY not in refused.text()
    if view.phone:
        page.set_viewport_size({"width": 360, "height": 780})
        assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0

    page.context.clear_cookies()
    sign_in(page, "e2e_plain", demo.password)
    page.goto(request_url)
    links = page.locator("#links")
    links.get_by_text("Removed links (1)").click()
    removed = links.locator("details li")
    expect(removed).to_have_text("Patient linked")
    for private in ("Wrong person", "matched", "removed by", "e2e_holder"):
        assert private not in links.inner_text()
    assert LinkAttachment.objects.get(pk=attachment.pk).removal_reason_code == "wrong_person"


@pytest.mark.parametrize("direction", ["back", "forward"])
def test_revealed_key_does_not_survive_back_or_forward(browser_type, live_server, demo, direction):
    """A revealed key must not come back with the page after Back or Forward. The browser here
    keeps its back-forward cache on (Playwright turns it off by default). Signed-in pages are
    marked no-store (accounts.sessions), so Chromium fetches the page again instead of restoring
    it, and says why; a browser that restores such a page anyway (pageshow.persisted) must show
    it with the key masked again (static/js/app.js)."""
    from core.models import AuditEvent
    from links.models import ExternalSystem
    from links.services import add_link

    holder = make_user("e2e_holder", demo.password, "client_services")
    grant_patient_capability(holder)
    service_request = demo_request()
    attachment, _ = add_link(
        actor=holder,
        request_id=service_request.pk,
        system=ExternalSystem.objects.get(key="lis"),
        record_type="patient",
        identifier=PATIENT_KEY,
    )
    target = live_server.url + reverse("desk:request-detail", args=[service_request.pk])
    list_url = live_server.url + reverse("desk:request-list")
    # Full Chromium (the headless shell keeps no back-forward cache), with that cache on.
    browser = browser_type.launch(
        channel="chromium", ignore_default_args=["--disable-back-forward-cache"]
    )
    try:
        context = browser.new_context(base_url=live_server.url, timezone_id="America/New_York")
        page = context.new_page()
        page.add_init_script(
            "addEventListener('pageshow', e => { window.vrRestored = e.persisted; })"
        )
        sign_in(page, "e2e_holder", demo.password)
        if direction == "forward":
            page.goto(list_url)
            page.evaluate("url => location.assign(url)", target)
            page.wait_for_url(target)
        else:
            page.goto(target)
        cell = page.locator(f"#key-{attachment.pk}")
        cell.get_by_role("button", name="Reveal key").click()
        expect(cell.get_by_text(PATIENT_KEY, exact=True)).to_be_visible()
        if direction == "back":
            page.evaluate("url => location.assign(url)", list_url)
            page.wait_for_url(list_url, wait_until="commit")
            page.evaluate("history.back()")
        else:
            page.evaluate("history.back()")
            page.wait_for_url(list_url, wait_until="commit")
            page.evaluate("history.forward()")
        page.wait_for_url(target, wait_until="commit")
        page.wait_for_function("typeof window.vrRestored === 'boolean'")
        reasons = page.evaluate(
            "JSON.stringify(performance.getEntriesByType('navigation')[0]"
            ".notRestoredReasons || null)"
        )
        if page.evaluate("window.vrRestored") is not True:
            assert "response-cache-control-no-store" in reasons, reasons
        assert PATIENT_KEY not in page.locator("body").inner_text()
        assert PATIENT_KEY not in page.content()
        # The masked cell is back and works: a new reveal is a new, recorded reveal.
        cell = page.locator(f"#key-{attachment.pk}")
        cell.get_by_role("button", name="Reveal key").click()
        expect(cell.get_by_text(PATIENT_KEY, exact=True)).to_be_visible()
        assert AuditEvent.objects.filter(action="external_link.person_revealed").count() == 2
    finally:
        browser.close()
