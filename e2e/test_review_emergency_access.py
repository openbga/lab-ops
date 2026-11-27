"""Synthetic browser regressions for the emergency access forms and review queue."""

import re

import pytest
from django.urls import reverse
from django.utils import timezone
from helpers import field, pick, sign_in
from playwright.sync_api import expect
from test_emergency_access import AGENT, REVIEWER, ended_use, hidden_patient_row

from accounts.models import User
from desk import emergency
from desk.models import EmergencyAccess, EmergencyReview
from links.models import LinkAttachment

pytestmark = pytest.mark.django_db(transaction=True)


def check_layout(page, view):
    expect(page.locator("html")).to_have_attribute("data-bs-theme", view.theme)
    assert page.evaluate("window.innerWidth") == view.viewport["width"]
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.confirmation
def test_emergency_dialog_keeps_explanation_and_confirms_before_start(page, demo, view):
    row = hidden_patient_row()
    ticket = row.filled_form.request
    sign_in(page, AGENT, demo.password)
    page.goto(reverse("desk:request-detail", args=[ticket.pk]))
    offer = page.get_by_role("link", name="Use emergency access", exact=True)
    expect(offer).to_be_visible()
    check_layout(page, view)
    offer.click()

    dialog = page.get_by_role("dialog", name=f"Emergency access: {ticket.number}")
    reason = dialog.get_by_role("radio", name="Another emergency")
    explanation = field(dialog, "What the emergency is")
    submit = dialog.get_by_role("button", name="Start emergency access")
    explanation.fill("Synthetic duty call about a delayed result.")
    field(dialog, "Your password").fill(demo.password)
    submit.click()
    expect(dialog.get_by_text("Choose why you need it.").first).to_be_visible()
    expect(explanation).to_have_value("Synthetic duty call about a delayed result.")
    expect(field(dialog, "Your password")).to_have_count(0)
    assert not EmergencyAccess.objects.filter(ticket=ticket).exists()
    check_layout(page, view)

    reason.check()
    submit.click()
    expect(dialog).to_have_count(0)
    use = EmergencyAccess.objects.get(ticket=ticket, user__username=AGENT)
    end = timezone.localtime(use.ends_at).strftime("%H:%M")
    expect(
        page.get_by_text(re.compile(rf"patient information on this ticket until {end}\."))
    ).to_be_visible()
    expect(offer).to_have_count(0)
    expect(
        page.locator(".vr-form-details").get_by_role("button", name="Reveal").first
    ).to_be_visible()
    check_layout(page, view)
    assert page.console_errors == []


def test_reviewer_filters_and_reviews_an_ended_use_with_other_uses_in_force(page, demo, view):
    row = hidden_patient_row()
    ticket = row.filled_form.request
    past = ended_use(row)
    current = emergency.start(
        actor=User.objects.get(username=AGENT),
        ticket_id=ticket.pk,
        reason="other",
        explanation="Synthetic callback on the current shift.",
    )
    sign_in(page, REVIEWER, demo.password)
    page.goto(reverse("access_review:emergency"))
    table = page.get_by_role("table", name="Uses of emergency access")
    expect(table.get_by_role("link")).to_have_text([current.number, past.number])
    expect(table).not_to_contain_text(current.explanation)
    expect(table).not_to_contain_text(past.explanation)
    check_layout(page, view)

    pick(field(page, "Status"), label="Waiting for review")
    page.get_by_role("button", name="Show", exact=True).click()
    expect(table.get_by_role("link")).to_have_text([past.number])
    table.get_by_role("link", name=past.number).click()
    expect(page.get_by_text(past.explanation, exact=True)).to_be_visible()
    expect(
        page.get_by_text("Nothing has been revealed under this use.", exact=True)
    ).to_be_visible()
    check_layout(page, view)

    feedback = field(page, "What you checked and found")
    note = "Synthetic shift lead confirmed the call."
    feedback.fill(note)
    submit = page.get_by_role("button", name="Record review", exact=True)
    submit.click()
    expect(page.get_by_text("Choose the outcome.", exact=True).first).to_be_visible()
    expect(feedback).to_have_value(note)
    assert not EmergencyReview.objects.filter(use=past).exists()
    page.get_by_role("radio", name="Appropriate", exact=True).check()
    submit.click()
    expect(page.get_by_text(note, exact=True)).to_be_visible()
    expect(submit).to_have_count(0)
    check_layout(page, view)
    page.goto(reverse("access_review:emergency"))
    pick(field(page, "Status"), label="Reviewed")
    page.get_by_role("button", name="Show", exact=True).click()
    expect(table.get_by_role("link")).to_have_text([past.number])
    expect(table).not_to_contain_text(note)
    assert page.console_errors == []


def test_invalid_review_status_shows_a_filter_error_and_no_rows(page, demo):
    use = ended_use(hidden_patient_row())
    sign_in(page, REVIEWER, demo.password)
    page.goto(reverse("access_review:emergency") + "?status=retired")
    expect(page.get_by_role("link", name=use.number, exact=True)).to_have_count(0)
    expect(page.get_by_text("These filters cannot be applied", exact=True)).to_be_visible()


def test_filter_before_the_first_use_shows_the_documented_empty_queue(page, demo):
    assert not EmergencyAccess.objects.exists()
    sign_in(page, REVIEWER, demo.password)
    page.goto(reverse("access_review:emergency"))
    empty = page.get_by_text("No emergency access used yet", exact=True)
    expect(empty).to_be_visible()
    pick(field(page, "Status"), label="Waiting for review")
    page.get_by_role("button", name="Show", exact=True).click()
    expect(empty).to_be_visible()


@pytest.mark.browser_context_args(java_script_enabled=False)
def test_both_reveals_work_as_full_pages_without_javascript(page, demo):
    row = hidden_patient_row()
    attachment = LinkAttachment.objects.select_related("link", "request").get(
        link__record_type="patient", removed_at__isnull=True
    )
    sign_in(page, AGENT, demo.password)
    for ticket_id, address, button, value in (
        (
            row.filled_form.request_id,
            reverse("request_details:reveal", args=[row.pk]),
            "Reveal",
            row.value,
        ),
        (
            attachment.request_id,
            reverse("link_records:attachment-reveal", args=[attachment.pk]),
            "Reveal key",
            attachment.link.identifier,
        ),
    ):
        ticket_url = reverse("desk:request-detail", args=[ticket_id])
        page.goto(ticket_url)
        offer = page.get_by_role("link", name="Use emergency access", exact=True)
        if offer.count():
            offer.click()
            expect(page).to_have_url(
                re.compile(re.escape(reverse("desk:request-emergency", args=[ticket_id])))
            )
            page.get_by_role("radio", name="Another emergency").check()
            field(page, "What the emergency is").fill("Synthetic shift handover.")
            page.get_by_role("button", name="Start emergency access").click()
        form = page.locator(f'form[action="{address}"]')
        with page.expect_navigation() as navigation:
            form.get_by_role("button", name=button, exact=True).click()
        response = navigation.value
        assert response.status == 200
        assert "no-store" in response.headers["cache-control"]
        expect(page).to_have_url(re.compile(re.escape(address) + "$"))
        expect(page.get_by_text(value, exact=True)).to_be_visible()
        expect(page.get_by_text("Reveal recorded.", exact=True)).to_be_visible()
        page.get_by_role("link", name="Back to the ticket", exact=True).click()
        expect(page).to_have_url(re.compile(re.escape(ticket_url)))
        expect(page.get_by_text(value, exact=True)).to_have_count(0)


def test_queue_intro_does_not_describe_every_start_as_a_completed_reveal(page, demo):
    row = hidden_patient_row()
    use = emergency.start(
        actor=User.objects.get(username=AGENT),
        ticket_id=row.filled_form.request_id,
        reason="other",
        explanation="Synthetic shift handover ended before a reveal was needed.",
    )
    sign_in(page, REVIEWER, demo.password)
    page.goto(reverse("access_review:emergency-use", args=[use.pk]))
    expect(
        page.get_by_text("Nothing has been revealed under this use.", exact=True)
    ).to_be_visible()
    page.goto(reverse("access_review:emergency"))
    expect(page.get_by_role("link", name=use.number, exact=True)).to_be_visible()
    expect(page.locator(".vr-page-intro")).not_to_contain_text(
        "Each time someone without the usual grant revealed patient information on a ticket"
    )
