"""Regression journeys for reveal confirmation and account notices using the demo laboratory."""

import re

import pytest
from django.urls import reverse
from django.utils import timezone
from helpers import field, sign_in
from playwright.sync_api import expect

from accounts.models import User
from core.models import AuditEvent
from jobs.models import Notice, NoticeStatus
from links.models import LinkAttachment

pytestmark = pytest.mark.django_db(transaction=True)


def check_view(page, view):
    expect(page.locator("html")).to_have_attribute("data-bs-theme", view.theme)
    assert page.evaluate("window.innerWidth") == view.viewport["width"]
    assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0


@pytest.mark.confirmation
def test_cancel_then_confirm_person_key_in_one_tab_and_reveal_in_another(page, demo, view):
    from core.models import RevealConfirmationRule

    RevealConfirmationRule.objects.create(pk=1, revision=1, first_reveal=True)
    attachment = LinkAttachment.objects.select_related("link").get(
        link__record_type="patient", removed_at__isnull=True
    )
    key = attachment.link.identifier
    ticket_url = reverse("desk:request-detail", args=[attachment.request_id])
    events = AuditEvent.objects.filter(
        action="external_link.person_revealed", object_id=str(attachment.link_id)
    )
    baseline = events.count()

    sign_in(page, "demo_supervisor", demo.password)
    page.goto(ticket_url)
    second = page.context.new_page()
    try:
        second.set_viewport_size(view.viewport)
        second.emulate_media(color_scheme=view.theme)
        second.goto(ticket_url)
        first_reveal = page.locator("#links").get_by_role("button", name="Reveal key")
        first_reveal.click()
        heading = page.get_by_role("heading", name="Confirm it is you", level=1)
        expect(heading).to_be_visible()
        check_view(page, view)
        page.get_by_role("button", name="Help", exact=True).click()
        help_panel = page.get_by_role("dialog", name="Page documentation")
        expect(help_panel).to_contain_text("Confirm and reveal")
        help_panel.get_by_role("button", name="Close documentation").click()
        page.get_by_role("link", name="Cancel", exact=True).click()
        expect(page).to_have_url(re.compile(re.escape(ticket_url) + "$"))
        expect(page.locator("#links")).not_to_contain_text(key)
        first_reveal.click()
        expect(heading).to_be_visible()
        field(page, "Your password").fill(demo.password)
        page.get_by_role("button", name="Confirm", exact=True).click()
        expect(page).to_have_url(re.compile(re.escape(ticket_url) + "$"))
        expect(page.locator("#links")).not_to_contain_text(key)
        assert events.count() == baseline

        second.locator("#links").get_by_role("button", name="Reveal key").click()
        expect(second.locator("#links").get_by_text(key, exact=True)).to_be_visible()
        check_view(second, view)
        assert events.count() == baseline + 1
        latest = events.latest("pk")
        assert latest.actor.username == "demo_supervisor"
        assert latest.after == {"attachment": attachment.pk, "request": attachment.request_id}
        assert page.console_errors == []
    finally:
        second.close()


def test_jobs_labels_retries_and_failures_without_naming_the_account(page, demo, view):
    from desk.models import ServiceRequest

    recipient = User.objects.get(username="demo_technical")
    ticket = ServiceRequest.objects.filter(category__is_restricted=False).order_by("pk").first()
    for status, event, attempts in (
        (NoticeStatus.WAITING, "recovery_codes_replaced", 1),
        (NoticeStatus.FAILED, "authenticator_removed", 8),
    ):
        Notice.objects.create(
            recipient=recipient,
            topic="accounts.security",
            object_id=recipient.pk,
            reference="Account",
            events=[event],
            status=status,
            attempts=attempts,
            last_error="SMTPServerDisconnected",
            next_attempt_at=timezone.now(),
        )
    Notice.objects.create(
        recipient=recipient,
        topic="desk.request",
        object_id=ticket.pk,
        reference=ticket.number,
        events=["assigned"],
        attempts=1,
        next_attempt_at=timezone.now(),
    )

    sign_in(page, "demo_admin", demo.password)
    page.goto(reverse("jobs:overview"))
    table = page.get_by_role("table", name="Notices that need attention")
    recipient_column = table.locator("thead th").all_text_contents().index("For")
    for event, status in (
        ("Authenticator removed", "Failed"),
        ("New recovery codes", "Waiting to retry"),
    ):
        row = table.get_by_role("row").filter(has=page.get_by_role("cell", name=event))
        expect(row.get_by_role("cell", name="Account notice")).to_be_visible()
        recipient_cell = row.locator("th, td").nth(recipient_column)
        expect(recipient_cell.get_by_text("—", exact=True)).to_be_visible()
        expect(row.get_by_role("cell", name=status)).to_be_visible()
        expect(row.get_by_role("link")).to_have_count(0)
        for label in (recipient.username, recipient.get_full_name(), recipient.email):
            if label:
                expect(row).not_to_contain_text(label)
    ticket_row = table.get_by_role("row").filter(has=page.get_by_role("link", name=ticket.number))
    expect(ticket_row.get_by_role("link", name=ticket.number)).to_have_attribute(
        "href", reverse("desk:request-detail", args=[ticket.pk])
    )
    expect(ticket_row).to_contain_text(recipient.get_full_name())
    check_view(page, view)
    assert page.console_errors == []
