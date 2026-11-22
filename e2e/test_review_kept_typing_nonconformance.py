"""Browser regressions for the changed-record warning and its Discard action.

Adopted from the independent review of the nonconformance kept typing (round 1). The last case was
changed on adoption: the help now says that after a refused save the form is opened again from the
record (the refused form is drawn from what was posted and carries no Discard), and the case
follows those steps."""

import re
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse
from helpers import field, sign_in
from playwright.sync_api import expect
from test_review_kept_typing import expire, tab_to
from test_session_limits import signed_in_at

from accounts.models import User
from desk.selectors import staff_requests
from nonconformance import services
from nonconformance.models import Nonconformance

pytestmark = pytest.mark.django_db(transaction=True)

OWNER = "demo_client_services"
RECORD = "Results reached one clinic late three times in a month"
DRAFT = "Only the afternoon collection was checked."
SAVED = "The supervisor checked all four collection times."
CHANGED = "This record changed after you typed it, so saving this form is refused."
STALE = "This nonconformance changed after you opened it. Reload it and review the latest details."


@pytest.fixture
def changed_investigation(page, demo):
    record = Nonconformance.objects.get(title=RECORD)
    address = reverse("nonconformance:investigation", args=[record.pk])
    signed_in_at(page, demo, OWNER, reverse("nonconformance:detail", args=[record.pk]))
    page.get_by_role("link", name="Record investigation").first.click()
    field(page.locator("#vr-form-dialog"), "Similar cases").fill(DRAFT)
    expire(page, OWNER)
    assert parse_qs(urlsplit(page.url).query)["next"] == [address]
    updated = services.record_investigation(
        actor=User.objects.get(username="demo_supervisor"),
        nc=record,
        expected_revision=record.revision,
        containment=record.containment,
        root_cause=record.root_cause,
        similar_cases=SAVED,
    )
    sign_in(page, OWNER, demo.password, visit=False)
    expect(page).to_have_url(re.compile(re.escape(address) + "$"))
    expect(field(page, "Similar cases")).to_have_value(DRAFT)
    expect(page.locator('input[name="expected_revision"]')).to_have_value(str(record.revision))
    return address, updated


def test_discard_on_the_changed_record_warning_opens_the_current_form(
    page, view, changed_investigation
):
    address, updated = changed_investigation
    expect(page.locator("html")).to_have_attribute("data-bs-theme", view.theme)
    notice = page.locator("[data-vr-kept-notice]")
    expect(notice).to_be_visible()
    expect(notice).to_have_class(re.compile(r"\balert-warning\b"))
    expect(notice).to_contain_text(CHANGED)
    discard = notice.get_by_role("link", name="Discard", exact=True)
    expect(discard).to_have_js_property("href", page.url)
    tab_to(page, discard)
    expect(discard).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    box = discard.bounding_box()
    assert box and 0 <= box["x"] <= box["x"] + box["width"] <= view.viewport["width"]
    if view.phone:
        assert min(box["height"], box["width"]) >= 44

    page.keyboard.press("Enter")
    expect(notice).to_have_count(0)
    expect(page).to_have_url(re.compile(re.escape(address) + "$"))
    expect(field(page, "Similar cases")).to_have_value(SAVED)
    expect(field(page, "Containment")).to_be_focused()
    expect(page.locator('input[name="expected_revision"]')).to_have_value(str(updated.revision))
    updated.refresh_from_db()
    assert updated.similar_cases == SAVED
    assert not page.console_errors


def test_discard_clears_the_restored_reason_when_the_ticket_link_was_removed(page, demo):
    record = Nonconformance.objects.get(title=RECORD)
    owner = User.objects.get(username=OWNER)
    ticket = staff_requests(owner).exclude(pk=record.source_request_id).order_by("pk").first()
    link = services.link_ticket(
        actor=owner, nc=record, expected_revision=record.revision, request=ticket
    )
    record.refresh_from_db()
    address = reverse("nonconformance:ticket-unlink", args=[record.pk, link.pk])
    signed_in_at(page, demo, OWNER, address)
    reason = field(page, "Reason")
    reason.fill("The pickup appears on a different ticket.")
    expire(page, OWNER)
    services.unlink_ticket(
        actor=User.objects.get(username="demo_supervisor"),
        nc=record,
        expected_revision=record.revision,
        link=link,
        reason="Supervisor reconciled the linked tickets.",
    )
    sign_in(page, OWNER, demo.password, visit=False)
    expect(reason).to_have_value("The pickup appears on a different ticket.")
    notice = page.locator("[data-vr-kept-notice]")
    expect(notice).to_contain_text(CHANGED)
    notice.get_by_role("link", name="Discard", exact=True).click()
    expect(reason).to_be_hidden()
    expect(notice).to_be_hidden()


def test_the_form_opens_again_from_the_record_after_the_restored_investigation_is_refused(
    page, changed_investigation
):
    """Follow the investigation help's recovery steps after trying Save: the refused form keeps
    the typing to copy, and the form opened again from the record shows it as it stands."""
    address, updated = changed_investigation
    page.get_by_role("button", name="Save investigation").click()
    expect(page.get_by_text(STALE).first).to_be_visible()
    expect(field(page, "Similar cases")).to_have_value(DRAFT)
    updated.refresh_from_db()
    assert updated.similar_cases == SAVED

    page.goto(reverse("nonconformance:detail", args=[updated.pk]))
    page.get_by_role("link", name="Record investigation").first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(field(dialog, "Similar cases")).to_have_value(SAVED)
    expect(dialog.locator('input[name="expected_revision"]')).to_have_value(str(updated.revision))
