"""The service desk's main journeys (docs/workflows.md 1 to 6), at a desktop and a phone width in
both themes: log a ticket from a call, log the contact, reply and wait on the client, assign it,
resolve and close it from the list; and a complaint reviewed by a second supervisor."""

import re

import pytest
from conftest import DESKTOP, NARROW, View
from helpers import field, pick, sign_in
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def open_dialog_action(page: Page, view: View, name: str):
    """A ticket page action (Assign, Status): a header button on a desktop; on a phone the same
    button sits in the page header above the content."""
    page.get_by_role("link", name=name, exact=True).first.click()
    return page.get_by_role("dialog")


def test_log_work_and_close_a_request(page: Page, demo, view: View):
    from desk.models import ServiceRequest, Status

    sign_in(page, "demo_client_services", demo.password)
    expect(page.get_by_role("heading", level=1, name="Dashboard")).to_be_visible()
    expect(page.get_by_role("link", name=re.compile("^My open tickets"))).to_be_visible()

    # Journey 1: log a ticket from a call.
    page.goto("/requests/new/")
    pick(field(page, "Category"), index=1)
    expect(page.locator("#category-section")).to_contain_text("Goes to")
    # The category's ticket form may ask for details the caller does not have yet.
    not_known = page.get_by_label("Some required details are not known yet")
    if not_known.count():
        not_known.check()
    pick(field(page, "Organization"), label="Example Family Clinic")
    field(page, "Subject").fill("E2E result not received")
    field(page, "What the client asked").fill("Caller asks about order ORD-TEST-000099.")
    page.get_by_role("button", name="Log ticket").click()
    heading = page.get_by_role("heading", level=1)
    expect(heading).to_contain_text("E2E result not received")
    created = ServiceRequest.objects.get(subject="E2E result not received")
    expect(heading).to_contain_text(created.number)

    add_entry = page.get_by_role("link", name="Add entry", exact=True)
    expect(add_entry).to_have_attribute("href", "#composer")
    if view.phone:
        box = add_entry.bounding_box()
        assert box["y"] > view.viewport["height"] * 0.75 and box["height"] >= 44
        add_entry.click()
        expect(page.locator("#composer")).to_be_in_viewport()

    # Journey 3: log the contact, then reply and wait on the client.
    composer = page.locator("#composer")
    composer.get_by_label("Log contact").check()
    expect(composer.get_by_text("Internal, not shown to the client")).to_be_visible()
    pick(field(composer, "Direction"), label="To the client")
    pick(field(composer, "By"), label="Phone")
    field(composer, "With whom").fill("Office manager")
    pick(field(composer, "Outcome"), label="Spoke with")
    field(composer, "Message").fill("Explained the result is pending a repeat.")
    composer.get_by_role("button", name="Send").click()
    expect(page.get_by_text("Contact logged.")).to_be_visible()
    expect(page.locator(".vr-entry-contact").first).to_contain_text("Spoke with")

    composer = page.locator("#composer")
    composer.get_by_label("Reply", exact=True).check()
    field(composer, "Message").fill("Please confirm the collection date.")
    pick(field(composer, "Then set status"), label="Waiting on client")
    composer.get_by_role("button", name="Send").click()
    expect(page.get_by_text("Reply sent.")).to_be_visible()
    expect(page.locator(".vr-page-meta")).to_contain_text("Waiting on client")

    # Journey 4: assign it to a person on the team.
    dialog = open_dialog_action(page, view, "Assign")
    pick(field(dialog, "Assign to"), label="Demo Client services (Client services)")
    dialog.get_by_role("button", name="Assign").click()
    expect(page.get_by_text("Assignment changed.")).to_be_visible()
    expect(page.locator(".vr-page-meta")).to_contain_text("Demo Client services")

    # Journey 5: resolve with a summary.
    dialog = open_dialog_action(page, view, "Status")
    dialog.get_by_label("Resolved").check()
    field(dialog, "Resolution summary").fill("Collection date confirmed; result released.")
    dialog.get_by_role("button", name="Change status").click()
    expect(page.get_by_text("Status changed.")).to_be_visible()
    created.refresh_from_db()
    assert created.status == Status.RESOLVED

    # Close it from the list's bulk action.
    page.goto("/requests/?view=resolved")
    expect(page.get_by_role("heading", level=1, name="Resolved not closed")).to_be_visible()
    page.get_by_role("checkbox", name=f"Select {created.number}").check()
    if view.phone:
        page.get_by_role("button", name=re.compile("^Change selected tickets")).click()
    pick(field(page, "With the selected"), label="Close")
    page.get_by_role("button", name="Apply to selected").click()
    expect(page.get_by_text(re.compile(r"ticket.? changed\."))).to_be_visible()
    created.refresh_from_db()
    assert created.status == Status.CLOSED
    assert not page.console_errors


@pytest.mark.parametrize("viewport", [DESKTOP, NARROW], ids=["desktop", "phone"])
def test_complaint_is_reviewed_by_another_supervisor(page: Page, demo, viewport):
    """The demo complaint was decided by its assignee; demo_supervisor, who never handled it,
    reviews it, and then it can be closed."""
    from desk.models import ServiceRequest

    complaint = ServiceRequest.objects.filter(is_complaint=True).first()
    if complaint is None or not hasattr(complaint, "complaint_review"):
        pytest.skip("The demonstration has no decided complaint (no complaint category loaded).")
    page.set_viewport_size(viewport)
    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/")
    page.get_by_role("link", name=re.compile("^Complaints awaiting my review")).click()
    page.get_by_role("link", name=complaint.number).click()
    page.get_by_role("link", name="Review", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("Investigate")
    field(dialog, "Review note").fill("Cause and action checked with the courier schedule.")
    dialog.get_by_role("button", name="Approve review").click()
    expect(page.get_by_text("Complaint reviewed.")).to_be_visible()
    expect(
        page.locator("#complaint-heading").locator("xpath=ancestor::section[1]")
    ).to_contain_text("Demo Supervisor")

    # The decision author may not review: the page refuses.
    page.context.clear_cookies()
    sign_in(page, "demo_client_services", demo.password)
    response = page.goto(f"/requests/{complaint.pk}/complaint/review/")
    assert response.status == 403


def test_unsent_ticket_entry_survives_declining_an_assignment(page: Page, demo):
    from desk.models import ServiceRequest

    ticket = ServiceRequest.objects.filter(
        status="open",
        team__memberships__user__username="demo_client_services",
        assignee__isnull=True,
    ).first()
    sign_in(page, "demo_client_services", demo.password)
    page.goto(f"/requests/{ticket.pk}/")
    composer = page.locator("form.vr-composer")
    field(composer, "Message").fill("Please call the synthetic office back.")
    composer.locator("input[type=file]").set_input_files(
        {"name": "call.txt", "mimeType": "text/plain", "buffer": b"Synthetic call log."}
    )
    take = page.get_by_role("button", name="Take it")
    page.once("dialog", lambda prompt: prompt.dismiss())
    take.click()
    expect(take).to_be_enabled()
    assert take.locator("xpath=ancestor::form").get_attribute("aria-busy") is None
    page.get_by_role("link", name="Assign", exact=True).click()
    dialog = page.get_by_role("dialog")
    pick(field(dialog, "Assign to"), label="Demo Client services (Client services)")
    prompts = []

    def keep_draft(prompt):
        prompts.append(prompt.message)
        prompt.dismiss()

    revision = ticket.revision
    page.once("dialog", keep_draft)
    dialog.get_by_role("button", name="Assign").click()
    expect(dialog.get_by_role("button", name="Assign")).to_be_enabled()
    assert prompts == ["Discard the unsent ticket entry and its selected files to continue?"]
    ticket.refresh_from_db()
    assert ticket.revision == revision
    expect(composer.locator("textarea[name=body]")).to_have_value(
        "Please call the synthetic office back."
    )
    assert composer.locator("input[type=file]").evaluate("el => el.files[0].name") == "call.txt"
    page.once("dialog", lambda prompt: prompt.accept())
    dialog.get_by_role("button", name="Assign").click()
    expect(page.get_by_text("Assignment changed.")).to_be_visible()
    ticket.refresh_from_db()
    assert ticket.revision == revision + 1
