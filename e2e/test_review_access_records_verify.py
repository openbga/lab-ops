"""Follow the access-history pager and the adjacent Audit history navigation in both themes.

Adopted unchanged from the review's verification round of access records."""

import pytest
from helpers import sign_in
from playwright.sync_api import expect

from accounts.models import User
from core.models import AccessRecord
from desk.models import ServiceRequest

pytestmark = pytest.mark.django_db(transaction=True)


def test_older_reads_stay_in_place_and_history_back_link_is_usable(page, demo, view):
    actor = User.objects.get(username="demo_supervisor")
    ticket = ServiceRequest.objects.order_by("pk").first()
    refs = [f"{n:032x}" for n in range(1, 31)]
    AccessRecord.objects.bulk_create(
        [
            AccessRecord(
                account=actor,
                ticket_id=ticket.pk,
                kind="complaint",
                capacity="laboratory",
                request_ref=ref,
            )
            for ref in refs
        ]
    )
    sign_in(page, actor.username, demo.password)
    response = page.goto(f"/requests/{ticket.pk}/access/")
    assert response.status == 200
    table = page.get_by_role("table", name="Access records")
    assert table.locator("code").all_text_contents() == refs[::-1][:25]
    # Another ordinary read commits before the viewer follows the rendered link.
    AccessRecord.objects.create(
        account=actor,
        ticket_id=ticket.pk,
        kind="ticket",
        capacity="laboratory",
        request_ref="e" * 32,
    )
    page.get_by_role("link", name="Next page", exact=True).click()
    expect(table.locator("tbody tr")).to_have_count(5)
    assert table.locator("code").all_text_contents() == refs[::-1][25:]
    page.get_by_role("link", name="audit history", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_contain_text(ticket.number)
    back = page.get_by_role("link", name=f"Back to {ticket.number}", exact=True)
    expect(back).to_be_visible()
    if view.phone:
        assert back.bounding_box()["height"] >= 44
    back.focus()
    expect(back).to_be_focused()
    back.press("Enter")
    expect(page.get_by_role("heading", level=1)).to_contain_text(ticket.number)
    assert page.url.endswith(f"/requests/{ticket.pk}/")
