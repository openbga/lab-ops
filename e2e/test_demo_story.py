"""The demonstration story, at a desktop and a phone width in both themes.

1. A ticket travels between the desk and the portal: staff log it for the clinic, link the
   order, reply and wait on the client (the clock pauses); the clinic's portal account replies
   (it comes back to the laboratory, the clock runs again); staff resolve it and the portal shows
   it resolved.
2. The seeded story as each demo account sees it: queues with work, the complaint awaiting the
   supervisor's review, the patient link masked for everyone but the supervisor, the restricted
   ticket hidden from the technical account, each portal account's service menu offering its
   laboratory type's forms only, and the water utility's portal limited to its plant.
"""

import re
from datetime import timedelta

import pytest
from conftest import View
from django.utils import timezone
from helpers import field, pick, sign_in
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


COMMON_MENU = ("Request a pickup", "Order supplies", "Ask about a result")
CLINIC_MENU = {*COMMON_MENU, "Request an urgent pickup"}
WATER_MENU = {*COMMON_MENU, "Order bottle kits"}
NEVER_OFFERED = "Request repository materials"  # the research institute's (biorepository)


def assert_service_menu(page: Page, featured, absent=()):
    """The portal home's menu tiles are exactly `featured` and Something else; every form the
    account may use (All services) leaves out the biorepository's and `absent`."""
    page.goto("/portal/")
    menu = page.locator(".vr-service-menu")
    expect(menu.get_by_role("heading", name="How can we help?")).to_be_visible()
    labels = menu.locator(".vr-service-label")
    expect(labels).to_have_count(len(featured) + 1)
    assert set(labels.all_inner_texts()) == {*featured, "Something else"}
    menu.get_by_role("link", name="All services").click()
    expect(page.get_by_role("heading", level=1, name="New ticket")).to_be_visible()
    for name in (NEVER_OFFERED, *absent):
        expect(page.get_by_text(name, exact=True)).to_have_count(0)
    for name in featured:
        expect(page.get_by_text(name, exact=True).first).to_be_visible()
    assert page.goto("/portal/forms/material-request/").status == 404


def scope_of(page: Page):
    dialog = page.get_by_role("dialog")
    return dialog if dialog.count() else page


def test_a_request_travels_between_desk_and_portal(page: Page, demo, view: View):
    from desk.models import ServiceRequest

    # Staff log it for the clinic and link the order.
    sign_in(page, "demo_client_services", demo.password)
    page.goto("/requests/new/")
    pick(field(page, "Category"), label="Missing result or report")
    pick(field(page, "Organization"), label="Example Family Clinic")
    field(page, "Subject").fill("E2E story: result for ORD-TEST-000777 missing")
    field(page, "What the client asked").fill("Office asks about order ORD-TEST-000777.")
    page.get_by_role("button", name="Log ticket").click()
    expect(page.get_by_text(re.compile(r"Ticket REQ-\d{6} logged\."))).to_be_visible()
    created = ServiceRequest.objects.get(subject="E2E story: result for ORD-TEST-000777 missing")
    expect(page.get_by_role("heading", level=1)).to_contain_text(created.number)

    page.locator("#links").get_by_role("link", name="Add link").click()
    scope = scope_of(page)
    pick(field(scope, "System"), label="Example LIS")
    pick(field(scope, "Record type"), label="Order")
    field(scope, "Identifier").fill("ORD-TEST-000777")
    scope.get_by_label("Show to client").check()
    scope.get_by_role("button", name="Add link").click()
    expect(page.get_by_text("Link added.")).to_be_visible()

    composer = page.locator("#composer")
    composer.get_by_label("Reply", exact=True).check()
    field(composer, "Message").fill("Which collection date should we look for?")
    pick(field(composer, "Then set status"), label="Waiting on client")
    composer.get_by_role("button", name="Send").click()
    expect(page.get_by_text("Reply sent.")).to_be_visible()
    clocks = page.get_by_role("list", name="Service clocks")
    expect(clocks.get_by_text("Paused", exact=True)).to_be_visible()

    # The clinic answers in the portal.
    page.context.clear_cookies()
    sign_in(page, "demo_client", demo.password)
    page.goto(f"/portal/requests/{created.number}/")
    expect(page.get_by_text("Which collection date should we look for?")).to_be_visible()
    expect(page.get_by_text("ORD-TEST-000777").first).to_be_visible()  # the shown link
    field(page, "Your reply").fill("It was collected on Monday morning.")
    page.get_by_role("button", name="Send reply").click()
    expect(page.get_by_text("Status: With the laboratory")).to_be_visible()

    # Back at the desk: open again, the clock runs; resolve it.
    page.context.clear_cookies()
    sign_in(page, "demo_client_services", demo.password)
    page.goto(f"/requests/{created.pk}/")
    expect(page.get_by_text("It was collected on Monday morning.")).to_be_visible()
    expect(page.locator(".vr-page-meta")).to_contain_text("Open")
    expect(
        page.get_by_role("list", name="Service clocks").get_by_text("Paused", exact=True)
    ).to_have_count(0)
    page.get_by_role("link", name="Status", exact=True).first.click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Resolved").check()
    field(dialog, "Resolution summary").fill("Result re-sent for the Monday collection.")
    dialog.get_by_role("button", name="Change status").click()
    expect(page.get_by_text("Status changed.")).to_be_visible()

    page.context.clear_cookies()
    sign_in(page, "demo_client", demo.password)
    page.goto(f"/portal/requests/{created.number}/")
    expect(page.get_by_text("Status: Resolved")).to_be_visible()
    if view.phone:
        assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
    assert not page.console_errors


def test_the_story_as_each_account_sees_it(page: Page, demo, view: View):
    from desk.models import ServiceRequest

    privacy = ServiceRequest.objects.get(category__key="privacy-request")
    patient_request = ServiceRequest.objects.get(link_attachments__link__record_type="patient")
    south = ServiceRequest.objects.get(subject="Bottle kits for the south plant")
    awaiting = ServiceRequest.objects.get(subject="Results late three times this month")

    # Supervisor: the complaint awaiting review, the restricted ticket, the patient label.
    sign_in(page, "demo_supervisor", demo.password)
    queue = page.get_by_role("link", name=re.compile("^Complaints awaiting my review"))
    expect(queue).to_be_visible()
    queue.click()
    expect(page.get_by_role("link", name=awaiting.number)).to_be_visible()
    assert page.goto(f"/requests/{privacy.pk}/").status == 200
    page.goto(f"/requests/{patient_request.pk}/")
    expect(page.locator("#links").get_by_text(re.compile(r"Patient ····4821"))).to_be_visible()

    # Client services: the same patient link is only "Patient linked".
    page.context.clear_cookies()
    sign_in(page, "demo_client_services", demo.password)
    expect(page.get_by_role("link", name=re.compile("^My open tickets"))).to_be_visible()
    page.goto(f"/requests/{patient_request.pk}/")
    links = page.locator("#links")
    expect(links.get_by_text("Patient linked")).to_be_visible()
    expect(links.get_by_text(re.compile("4821"))).to_have_count(0)

    # Technical staff: the restricted privacy request does not exist for them.
    page.context.clear_cookies()
    sign_in(page, "demo_technical", demo.password)
    assert page.goto(f"/requests/{privacy.pk}/").status == 404
    page.goto("/requests/?view=all")
    expect(page.get_by_text(privacy.subject)).to_have_count(0)

    # Each portal account's service menu offers its laboratory type's forms and those for every
    # laboratory, never another type's (request_forms.seed): no repository materials for a clinic.
    page.context.clear_cookies()
    sign_in(page, "demo_client", demo.password)
    assert_service_menu(page, CLINIC_MENU, absent=("Order bottle kits",))
    page.context.clear_cookies()
    sign_in(page, "demo_client_water", demo.password)
    assert_service_menu(page, WATER_MENU, absent=("Request an urgent pickup",))

    # The water utility's portal account: its plant's tickets, never the south plant's.
    page.goto("/portal/requests/")
    expect(page.get_by_text("Two samples on COC-TEST-000021", exact=False)).to_be_visible()
    expect(page.get_by_text(south.subject)).to_have_count(0)
    assert page.goto(f"/portal/requests/{south.number}/").status == 404
    if view.phone:
        assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0


def test_service_menu_and_form_details_over_the_story(page: Page, demo, view: View):
    """The water utility's portal account uses the service menu and Request again on the bottle
    kits it ordered in the story; staff see what a phone call still lacks."""
    from desk.models import ServiceRequest

    kits = ServiceRequest.objects.get(filled_form__template_key="bottle-kit-order")
    call = ServiceRequest.objects.get(filled_form__template_key="result-status")

    sign_in(page, "demo_client_water", demo.password)
    expect(page.get_by_role("heading", name="How can we help?")).to_be_visible()
    if view.phone:
        tile = page.locator(".vr-service-menu a, [data-vr-service-menu] a").first
        if tile.count():
            assert tile.bounding_box()["height"] >= 44
    page.goto(f"/portal/requests/{kits.number}/")
    expect(page.get_by_text("TRK-TEST-0105")).to_be_visible()  # staff filled, client sees
    expect(page.get_by_text("PROJ-TEST-0001").first).to_be_visible()
    page.get_by_role("link", name="Request again").click()
    expect(page.get_by_text(f"Answers copied from {kits.number}")).to_be_visible()
    expect(field(page, "Project name or number")).to_have_value("PROJ-TEST-0001")
    expect(field(page, "Planned sampling date")).to_have_value("")  # one occasion: not copied
    field(page, "Planned sampling date").fill(
        (timezone.localdate() + timedelta(days=14)).isoformat()
    )
    page.get_by_role("button", name="Send ticket").click()
    expect(page.get_by_text(re.compile(r"Ticket REQ-\d{6} sent"))).to_be_visible()
    if view.phone:
        assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0

    page.context.clear_cookies()
    sign_in(page, "demo_client_services", demo.password)
    page.goto(f"/requests/{call.pk}/")
    expect(page.get_by_text("Still needed")).to_be_visible()
    expect(page.get_by_text("Order, requisition or sample number").first).to_be_visible()
