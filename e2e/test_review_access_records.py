"""Browser checks for the supervisor's access records table on a synthetic ticket.

Adopted unchanged from the round-1 independent review of access records: a long username fits
its card, the request ID meets text contrast, and the back link is a touch target on a phone."""

import pytest
from helpers import contrast_ratio, sign_in
from playwright.sync_api import expect

from accounts.models import User
from core.models import AccessCapacity, AccessKind, AccessRecord
from desk.models import ServiceRequest

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def access_page(page, demo, view):
    ticket = ServiceRequest.objects.order_by("pk").first()
    supervisor = User.objects.get(username="demo_supervisor")
    sign_in(page, supervisor.username, demo.password)
    assert page.goto(f"/requests/{ticket.pk}/").status == 200
    # Usernames can be 150 characters, including long unbroken names.
    reader = User.objects.create_user(username="reading_account_" + "w" * 120)
    ref = "b" * 32
    AccessRecord.objects.create(
        ticket_id=ticket.pk,
        account=reader,
        capacity=AccessCapacity.LABORATORY,
        kind=AccessKind.TICKET,
        request_ref=ref,
    )
    assert page.goto(f"/requests/{ticket.pk}/access/").status == 200
    expect(page.locator("html")).to_have_attribute("data-bs-theme", view.theme)
    table = page.get_by_role("table", name="Access records")
    expect(table.get_by_text(ref, exact=True)).to_be_visible()
    return table


def test_entire_access_row_fits_its_card(access_page, page, view):
    table = access_page
    if view.phone:
        cells = table.locator("tbody tr").first.locator("td")
        assert cells.evaluate_all("cells => cells.map(cell => cell.dataset.vrLabel)") == [
            "Person",
            "Capacity",
            "What",
            "Request ID",
        ]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    sizes = table.evaluate(
        """table => {
          const wrap = table.closest(".vr-table-wrap");
          return {content: wrap.scrollWidth, available: wrap.clientWidth};
        }"""
    )
    assert sizes["content"] <= sizes["available"] + 1, sizes


def test_request_id_meets_text_contrast_on_each_row(access_page):
    # The row's td paints its own opaque background. Read each actual foreground and
    # background rather than a generic table sample, which samples another column.
    pairs = access_page.locator("tbody code").evaluate_all(
        """codes => {
          const rgb = colour => colour.match(/[0-9.]+/g).map(Number);
          return codes.map(code => [
            rgb(getComputedStyle(code).color),
            rgb(getComputedStyle(code.closest('td')).backgroundColor)
          ]);
        }"""
    )
    assert len(pairs) == 2
    assert all(len(bg) == 3 for _, bg in pairs), pairs
    ratios = [round(contrast_ratio(fg, bg), 3) for fg, bg in pairs]
    assert min(ratios) >= 4.5, (pairs, ratios)


def test_phone_back_to_ticket_link_has_a_touch_target(access_page, page, view):
    if not view.phone:
        pytest.skip("The desktop link follows desktop sizing.")
    back = page.locator("main p > a").filter(has_text="Back to ")
    expect(back).to_have_count(1)
    # Its paragraph consists entirely of this navigation link.
    assert back.evaluate("a => a.textContent.trim() === a.parentElement.textContent.trim()")
    bounds = back.bounding_box()
    assert bounds["width"] >= 44 and bounds["height"] >= 44, bounds
