"""Confirm it is you before the first reveal of patient information in a session
(core.reveal_confirmation), in the browser: an administrator turns it on in the form dialog on
Security settings, confirming with the password; a supervisor who can view patient information is
then sent to Confirm it is you by the first Reveal, comes back to the ticket and reveals, and is not
asked again; at both widths in both themes."""

import re

import pytest
from django.urls import reverse
from helpers import field, sign_in
from playwright.sync_api import Page, expect

from core.models import AuditEvent, RevealConfirmationRule

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.confirmation]

OVERFLOW = "() => document.documentElement.scrollWidth - window.innerWidth"


def test_first_reveal_asks_once_where_the_laboratory_turns_it_on(page: Page, demo, view):
    from django.contrib.auth.models import Permission

    from accounts.models import User
    from request_forms.models import FieldValue

    sign_in(page, "demo_admin", demo.password)
    page.goto(reverse("security-settings"))
    panel = page.locator("section", has=page.locator("#reveal-confirmation-heading"))
    expect(panel).to_contain_text("Not asked")
    page.get_by_role("link", name="Edit confirmation before revealing patient information").click()
    dialog = page.locator("#vr-form-dialog")
    expect(
        page.get_by_role("dialog", name="Edit confirmation before revealing patient information")
    ).to_be_visible()
    dialog.get_by_label("Confirm it is you before the first reveal in a session").check()
    field(dialog, "Reason").fill("Shared workstations at the front desk.")
    field(dialog, "Your password").fill(demo.password)
    dialog.get_by_role("button", name="Save changes", exact=True).click()
    expect(
        page.get_by_text("Confirmation before revealing patient information saved.")
    ).to_be_visible()
    expect(panel).to_contain_text("Asked once in each session")
    assert RevealConfirmationRule.objects.get(pk=1).first_reveal
    # Another person on this browser: the session's status check settles first.
    page.wait_for_load_state("networkidle")
    page.context.clear_cookies()

    row = (
        FieldValue.objects.filter(key="patient-identifier", superseded_at__isnull=True)
        .select_related("filled_form__request")
        .first()
    )
    assert row is not None, "the demo seed holds one synthetic patient identifier"
    supervisor = User.objects.get(username="demo_supervisor")
    supervisor.user_permissions.add(
        Permission.objects.get(
            codename="view_patient_information", content_type__app_label="accounts"
        )
    )
    sign_in(page, "demo_supervisor", demo.password)
    ticket = f"/requests/{row.filled_form.request.pk}/"
    page.goto(ticket)
    details = page.locator(".vr-form-details")
    details.get_by_role("button", name="Reveal").click()
    expect(page).to_have_url(re.compile(r"/account/confirm-reveal/\?next="))
    expect(page.get_by_role("heading", name="Confirm it is you", level=1)).to_be_visible()
    password = field(page, "Your password")
    expect(password).to_have_attribute("autocomplete", "current-password")
    assert page.evaluate(OVERFLOW) <= 0
    assert not AuditEvent.objects.filter(action="request_form.value_revealed").exists()

    password.fill("not the password")
    page.get_by_role("button", name="Confirm", exact=True).click()
    expect(page.get_by_text("Incorrect password.")).to_be_visible()
    field(page, "Your password").fill(demo.password)
    page.get_by_role("button", name="Confirm", exact=True).click()
    expect(page).to_have_url(re.compile(re.escape(ticket) + "$"))
    expect(page.get_by_text("Confirmed. You can reveal patient information")).to_be_visible()

    details = page.locator(".vr-form-details")
    expect(details.get_by_text(row.value)).to_have_count(0)
    details.get_by_role("button", name="Reveal").click()
    expect(details.get_by_text(row.value)).to_be_visible()
    expect(details.get_by_text("Reveal recorded.")).to_be_visible()
    # Not asked again in this session.
    page.reload()
    page.locator(".vr-form-details").get_by_role("button", name="Reveal").click()
    expect(page.locator(".vr-form-details").get_by_text(row.value)).to_be_visible()
    assert AuditEvent.objects.filter(action="request_form.value_revealed").count() == 2
    assert page.evaluate(OVERFLOW) <= 0
    assert page.console_errors == []
