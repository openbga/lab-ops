"""Scheduled jobs in the browser: the administrator opens it from the Setup menu, edits the
settings in the form dialog, and reads the page at a phone width without sideways scrolling."""

import re

import pytest
from helpers import field
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def test_admin_edits_the_settings_in_the_dialog(page: Page, sign_in_as):
    from core.models import AuditEvent
    from jobs.models import JobSettings

    sign_in_as("admin")
    page.locator("#vr-nav").get_by_role("link", name="Scheduled jobs", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Scheduled jobs")
    main = page.get_by_role("main")
    expect(main).to_contain_text("The scheduled jobs have never run on this deployment.")
    expect(main).to_contain_text("Never (tickets stay resolved until someone closes them)")

    main.get_by_role("link", name="Edit settings", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("heading", name="Edit scheduled job settings")).to_be_visible()
    days = field(dialog, "Close resolved tickets after")
    # Out of range: refused in the dialog, which stays open with the input kept.
    days.fill("400")
    dialog.get_by_role("button", name="Save changes", exact=True).click()
    expect(dialog).to_contain_text("Enter a number of days from 1 to 365, or leave it empty.")
    expect(field(dialog, "Close resolved tickets after")).to_have_value("400")
    field(dialog, "Close resolved tickets after").fill("30")
    dialog.get_by_role("button", name="Save changes", exact=True).click()
    expect(page.get_by_text("Settings saved.")).to_be_visible()
    expect(dialog).to_be_hidden()
    expect(main).to_contain_text("30 days")
    assert JobSettings.objects.get(pk=1).close_resolved_after_days == 30
    assert AuditEvent.objects.filter(action="jobs.settings_changed").count() == 1
    assert page.console_errors == []


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_page_fits_a_phone(page: Page, sign_in_as, theme):
    from django.utils import timezone

    from accounts.models import User
    from desk.models import ServiceRequest
    from jobs.models import Notice, NoticeStatus

    ticket = ServiceRequest.objects.order_by("pk").first()
    Notice.objects.create(
        recipient=User.objects.get(username="demo_admin"),
        topic="desk.request",
        object_id=ticket.pk,
        reference=ticket.number,
        events=["assigned"],
        status=NoticeStatus.FAILED,
        attempts=8,
        last_error="SMTPServerDisconnected",
        next_attempt_at=timezone.now(),
    )
    page.set_viewport_size({"width": 360, "height": 780})
    page.emulate_media(color_scheme=theme)
    sign_in_as("admin")
    page.goto("/lab/scheduled-jobs/")
    expect(page.get_by_role("heading", level=1)).to_have_text("Scheduled jobs")
    table = page.get_by_role("table", name="Notices that need attention")
    expect(table.get_by_role("link", name=ticket.number)).to_be_visible()
    expect(table).to_contain_text(re.compile("Failed"))
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"page scrolls sideways by {overflow}px"
    assert page.console_errors == []
