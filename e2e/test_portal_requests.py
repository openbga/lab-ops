"""Client journeys 12 to 14 (P1 to P4): a site-scoped portal account opens a ticket, sees it
with the laboratory, finds it waiting for them after a staff reply, replies, filters its list and
cannot open a ticket of another site of its organization. Run in every `view` (desktop 1366 and
phone 390, light and dark), and the intake form and ticket page also at 360 px."""

import re

import pytest
from conftest import View
from helpers import field, pick, sign_in
from playwright.sync_api import Page, expect
from test_phone_layout import open_filter_sheet

pytestmark = pytest.mark.django_db(transaction=True)

P360 = {"width": 360, "height": 780}


def setup_categories():
    from taxonomy.models import RequestCategory

    group = RequestCategory.objects.create(key="e2e-results", name="E2E status and results")
    RequestCategory.objects.create(
        key="e2e-missing",
        name="Missing result or report",
        client_label="A result or report is missing",
        parent=group,
        client_may_raise=True,
    )
    RequestCategory.objects.create(
        key="e2e-internal", name="Internal problem", parent=group, client_may_raise=False
    )


def staff_request(site_code, subject):
    from accounts.models import User
    from clients.models import ClientSite
    from desk.services import create_request
    from taxonomy.models import RequestCategory

    site = ClientSite.objects.get(code=site_code, client__account_number="EXWATER")
    return create_request(
        actor=User.objects.get(username="demo_client_services"),
        category=RequestCategory.objects.get(key="e2e-missing"),
        subject=subject,
        first_entry_kind="note",
        first_entry_body="Logged by phone.",
        channel="phone",
        client=site.client,
        site=site,
    )


def staff_reply(number, body, status=None):
    from accounts.models import User
    from desk.models import ServiceRequest
    from desk.services import add_entry

    request = ServiceRequest.objects.get(number=number)
    add_entry(
        actor=User.objects.get(username="demo_client_services"),
        request=request,
        kind="reply",
        body=body,
        expected_revision=request.revision,
        new_status=status,
    )


def no_sideways_scroll(page, width):
    assert page.evaluate("document.documentElement.scrollWidth") <= width


def test_site_scoped_client_raises_follows_and_replies(page: Page, demo, view: View):
    journey(page, demo, view.viewport["width"], view.phone)


def test_raise_and_reply_at_360(page: Page, demo):
    page.set_viewport_size(P360)
    journey(page, demo, P360["width"], phone=True)


def journey(page, demo, width, phone):
    setup_categories()
    south = staff_request("SOUTH", "Bottle kit for South plant")

    sign_in(page, "demo_client_water", demo.password)
    expect(page).to_have_url(re.compile(r"/portal/$"))
    expect(page.get_by_role("heading", name="Open tickets")).to_be_visible()
    expect(page.get_by_text("Bottle kit for South plant")).to_have_count(0)

    page.get_by_role("link", name="New ticket").first.click()  # the ticket forms chooser
    page.get_by_role("link", name=re.compile("^Something else")).click()
    expect(page.get_by_role("heading", level=1, name="New ticket")).to_be_visible()
    expect(page.get_by_text("Please do not enter patient names")).to_be_visible()
    topic = field(page, "What is it about?")
    pick(topic, label="A result or report is missing")
    pick(field(page, "Site"), label="North treatment plant (NORTH)")
    field(page, "Subject").fill("Missing report for sample SMP-TEST-0042")
    field(page, "Description").fill("The report for this sample has not arrived.")
    reference = field(page, "Your reference")
    reference.fill("SMP-TEST-0042")
    assert reference.get_attribute("type") == "text"
    page.get_by_text("Urgent", exact=True).click()
    no_sideways_scroll(page, width)
    send = page.get_by_role("button", name="Send")
    if phone:
        box = send.bounding_box()
        assert box["height"] >= 44
    send.click()

    expect(page.get_by_text(re.compile(r"Ticket REQ-\d{6} sent"))).to_be_visible()
    number = re.search(r"REQ-\d{6}", page.url).group(0)
    expect(page.get_by_text("With the laboratory").first).to_be_visible()
    expect(page.get_by_text("The report for this sample has not arrived.")).to_be_visible()
    for hidden in ("Priority", "High", "Urgent", "Client services"):
        expect(page.locator("main").get_by_text(hidden, exact=True)).to_have_count(0)
    no_sideways_scroll(page, width)

    # The laboratory replies and waits for the client: the home page says so.
    staff_reply(number, "Which bottle was it?", status="waiting_client")
    page.goto("/portal/")
    expect(page.get_by_text("1 waiting for you")).to_be_visible()
    page.get_by_role("link", name=number).click()
    expect(page.get_by_text("Which bottle was it?")).to_be_visible()
    expect(
        page.get_by_text(re.compile("Client services, Example Reference Laboratory"))
    ).to_be_visible()
    field(page, "Your reply").fill("The one from the north intake.")
    page.get_by_role("button", name="Send reply").click()
    expect(page.get_by_text("Reply sent to the laboratory.")).to_be_visible()
    expect(page.get_by_text("Status: With the laboratory")).to_be_visible()
    no_sideways_scroll(page, width)

    # The ticket list: open first, filter by status, and nothing of the other site.
    page.goto("/portal/requests/")
    expect(page.get_by_role("link", name=number)).to_be_visible()
    scope = open_filter_sheet(page) if phone else page
    pick(field(scope, "Status"), label="Waiting for you")
    scope.get_by_role("button", name="Apply").click()
    expect(page.get_by_text("No tickets match these filters")).to_be_visible()
    page.goto("/portal/requests/?status=active")
    expect(page.get_by_text("Bottle kit for South plant")).to_have_count(0)
    no_sideways_scroll(page, width)
    response = page.goto(f"/portal/requests/{south.number}/")
    assert response.status == 404
    # Staff pages stay closed.
    assert page.goto("/clients/").status == 403


def staff_status(number, status, **details):
    from accounts.models import User
    from desk.models import ServiceRequest
    from desk.services import change_status

    request = ServiceRequest.objects.get(number=number)
    change_status(
        actor=User.objects.get(username="demo_client_services"),
        request=request,
        expected_revision=request.revision,
        status=status,
        **details,
    )


def test_a_reply_to_a_ticket_closed_meanwhile_comes_back_unsent(page: Page, demo, view: View):
    """The laboratory closes the ticket while the client writes: nothing is sent, and the text
    comes back read only beside the way to send it, a follow-up ticket."""
    setup_categories()
    ticket = staff_request("NORTH", "Seal on the north bottle kit")
    staff_reply(ticket.number, "Replaced; closing once you confirm.")
    staff_status(ticket.number, "resolved", summary="Seal replaced.")
    width = view.viewport["width"]

    sign_in(page, "demo_client_water", demo.password)
    page.goto(f"/portal/requests/{ticket.number}/")
    text = "Confirmed, and please also send two spare seals."
    field(page, "Your reply").fill(text)
    staff_status(ticket.number, "closed")
    page.get_by_role("button", name="Send reply").click()

    panel = page.locator("section", has=page.get_by_role("heading", name="Reply not sent"))
    expect(panel).to_be_visible()
    expect(panel.get_by_text("so nothing was sent")).to_be_visible()
    unsent = field(panel, "Your reply, not sent")
    expect(unsent).to_have_value(text)
    expect(unsent).to_have_attribute("readonly", "")
    expect(page.get_by_role("button", name="Send reply")).to_have_count(0)
    follow_up = panel.get_by_role("link", name="New follow-up ticket")
    expect(follow_up).to_be_visible()
    if view.phone:
        assert follow_up.bounding_box()["height"] >= 44
    no_sideways_scroll(page, width)
    follow_up.click()
    expect(page.get_by_role("heading", level=1, name="New follow-up ticket")).to_be_visible()
