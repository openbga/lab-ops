"""Who opened it (/requests/<id>/access/): a client portal account opens one of its tickets, then a
supervisor opens the same ticket and reads, from its More menu, who opened it, at both widths and
in both themes. Synthetic demonstration data only."""

import re

import pytest
from helpers import contrast_failures, menu_item, sign_in, sign_out, theme_samples
from playwright.sync_api import Page, expect

from accounts.models import User
from core.models import AccessRecord
from desk.portal_scope import portal_requests

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL = "demo_client"
SUPERVISOR = "demo_supervisor"


def rows(page: Page):
    return page.get_by_role("table", name="Access records").locator("tbody tr")


def test_a_supervisor_reads_who_opened_a_ticket(page: Page, demo, view):
    ticket = portal_requests(User.objects.get(username=PORTAL)).order_by("pk").first()
    assert ticket is not None, "the demonstration seed gives the portal account a ticket"

    sign_in(page, PORTAL, demo.password)
    page.goto(f"/portal/requests/{ticket.number}/")
    expect(page.get_by_role("heading", level=1)).to_contain_text(ticket.number)
    # The page's own status request has answered before the session ends (session.js).
    page.wait_for_load_state("networkidle")
    sign_out(page, who=r"^(Demo |E2E |Account)")

    sign_in(page, SUPERVISOR, demo.password)
    page.goto(f"/requests/{ticket.pk}/")
    recorded = AccessRecord.objects.filter(ticket_id=ticket.pk).count()
    menu_item(page, "More", "Who opened it").click()
    expect(page).to_have_url(re.compile(rf"/requests/{ticket.pk}/access/"))
    expect(page.get_by_role("heading", level=1)).to_contain_text(f"Who opened {ticket.number}")

    # Newest first: the supervisor's own opening of the ticket, then the portal account's (with a
    # row for each patient-marked answer its page showed, if the seed gave the ticket any).
    expect(rows(page)).to_have_count(recorded)
    if not view.phone:  # a phone shows each row stacked, its headers as labels in the cells
        for header in ("When", "Person", "Capacity", "What", "Request ID"):
            expect(page.get_by_role("columnheader", name=header)).to_be_visible()
    mine, theirs = rows(page).nth(0), rows(page).nth(1)
    expect(mine).to_contain_text(SUPERVISOR)
    expect(mine).to_contain_text("Laboratory staff")
    expect(mine).to_contain_text("Opened the ticket")
    expect(theirs).to_contain_text(PORTAL)
    expect(theirs).to_contain_text("Client portal")
    expect(theirs).to_contain_text("Opened the ticket")
    expect(page.get_by_text(f"Showing 1–{recorded} of {recorded} access records")).to_be_visible()

    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
        "the page scrolls sideways"
    )
    assert not contrast_failures(theme_samples(page))

    # Opening this page and the audit history is recorded too; a page lists what was recorded
    # before it was drawn, so the newest row is the audit history opened just now.
    page.get_by_role("link", name="audit history").click()
    expect(page.get_by_role("heading", level=1)).to_contain_text(f"History of {ticket.number}")
    page.get_by_role("link", name="Who opened it").click()
    expect(rows(page)).to_have_count(recorded + 2)
    expect(rows(page).nth(0)).to_contain_text("Opened the audit history")
    expect(rows(page).nth(1)).to_contain_text("Opened who opened it")


def test_other_staff_are_not_offered_who_opened_it(page: Page, demo):
    ticket = portal_requests(User.objects.get(username=PORTAL)).order_by("pk").first()
    sign_in(page, "demo_client_services", demo.password)
    page.goto(f"/requests/{ticket.pk}/")
    page.get_by_role("button", name="More", exact=True).click()
    expect(
        page.locator(".dropdown-menu.show").get_by_role("link", name="Who opened it")
    ).to_have_count(0)
    response = page.goto(f"/requests/{ticket.pk}/access/")
    assert response is not None and response.status == 403
