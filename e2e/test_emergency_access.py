"""Emergency access, in the browser: staff holding "Can use emergency access" but not "Can view
patient information" open it from a ticket whose patient information is hidden from them (the notice
on the ticket, the form in the shared form dialog), are refused it empty, start it with a reason and
an explanation, see the banner, and reveal one answer, at both widths in both themes; a reviewer
opens the use on Access review's Emergency access tab once it has ended and records a review; a
staff account without the capability is offered nothing and refused at the address. Synthetic
demonstration data only."""

import re
from datetime import timedelta

import pytest
from conftest import DESKTOP, VIEWS, View
from django.urls import reverse
from django.utils import timezone
from helpers import field, sign_in
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

AGENT = "demo_client_services"  # holds the capability, not view_patient_information
REVIEWER = "demo_supervisor"  # holds both view_patient_information and review_audit_records
WITHOUT = "demo_technical"  # holds neither
EXPLANATION = "Synthetic: a caller asked for the identifier on this ticket; nobody else is in."
REVIEW_NOTE = "Synthetic: asked the shift lead, the reason held."


def no_sideways_scroll(page: Page):
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), (
        "the page scrolls sideways"
    )


def hidden_patient_row(username: str = AGENT):
    """A current patient-marked answer on a ticket `username` may read."""
    from accounts.models import User
    from desk.selectors import staff_requests
    from request_forms.models import FieldValue

    readable = staff_requests(User.objects.get(username=username))
    row = (
        FieldValue.objects.filter(
            key="patient-identifier",
            superseded_at__isnull=True,
            filled_form__request__in=readable,
        )
        .select_related("filled_form__request")
        .first()
    )
    assert row is not None, (
        "the demo seed holds a synthetic patient identifier on a readable ticket"
    )
    return row


def ended_use(row):
    """A use by the agent on the row's ticket whose end has passed (rows cannot be updated)."""
    from accounts.models import User
    from desk.models import EmergencyAccess

    agent = User.objects.get(username=AGENT)
    now = timezone.now()
    return EmergencyAccess.objects.create(
        user=agent,
        user_name=agent.get_full_name() or agent.username,
        user_username=agent.username,
        user_roles="Client services",
        ticket=row.filled_form.request,
        kind="patient_information",
        reason="critical_result",
        explanation="Synthetic: an emergency that has ended.",
        started_at=now - timedelta(minutes=65),
        ends_at=now - timedelta(minutes=5),
    )


def test_start_emergency_access_and_reveal(page: Page, demo, view: View):
    from desk.models import EmergencyAccess

    row = hidden_patient_row()
    ticket = reverse("desk:request-detail", args=[row.filled_form.request_id])
    ticket_number = row.filled_form.request.number
    sign_in(page, AGENT, demo.password)
    page.goto(ticket)
    notice = page.get_by_text("Patient information hidden", exact=True)
    expect(notice).to_be_visible()
    details = page.locator(".vr-form-details")
    expect(details.get_by_role("button", name="Reveal")).to_have_count(0)
    expect(page.get_by_text(row.value)).to_have_count(0)
    no_sideways_scroll(page)

    page.get_by_role("link", name="Use emergency access", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    expect(page.get_by_role("dialog", name=f"Emergency access: {ticket_number}")).to_be_visible()
    start = dialog.get_by_role("button", name="Start emergency access", exact=True)
    expect(start).to_be_visible()
    no_sideways_scroll(page)

    if view.viewport == DESKTOP and view.theme == "light":
        start.click()
        expect(dialog.get_by_text("Choose why you need it.").first).to_be_visible()
        expect(
            dialog.get_by_text("Say why you need it, without patient information.").first
        ).to_be_visible()
        assert not EmergencyAccess.objects.exists()

    reason = dialog.get_by_label("Reporting a critical or urgent result")
    reason.check()
    expect(reason).to_be_checked()
    field(dialog, "What the emergency is").fill(EXPLANATION)
    password = field(dialog, "Your password")
    if password.count():
        password.fill(demo.password)
    start.click()

    banner = page.get_by_text(
        re.compile(r"You can reveal the patient information on this ticket until \d\d:\d\d")
    )
    expect(banner).to_be_visible()
    expect(page.get_by_text(re.compile("Emergency access started"))).to_be_visible()
    expect(page).to_have_url(re.compile(re.escape(ticket)))
    use = EmergencyAccess.objects.get()
    assert use.explanation == EXPLANATION and use.reason == "critical_result"
    expect(page.get_by_role("link", name="Use emergency access")).to_have_count(0)
    no_sideways_scroll(page)

    page.locator(".vr-form-details").get_by_role("button", name="Reveal").first.click()
    expect(page.locator(".vr-form-details").get_by_text(row.value)).to_be_visible()
    expect(page.get_by_text("Reveal recorded.").first).to_be_visible()
    no_sideways_scroll(page)
    assert page.console_errors == []


# Desktop in the light theme and a phone in the dark one; each view costs a seeded database.
@pytest.mark.parametrize("view", [VIEWS[0], VIEWS[3]], indirect=True)
def test_a_reviewer_reviews_an_ended_use(page: Page, demo, view: View):
    row = hidden_patient_row()
    use = ended_use(row)
    sign_in(page, REVIEWER, demo.password)
    page.goto(reverse("access_review:accounts"))
    page.get_by_role("link", name=re.compile("^Emergency access")).first.click()
    expect(page).to_have_url(re.compile(r"/access-review/emergency/$"))
    no_sideways_scroll(page)
    page.get_by_role("link", name=re.compile(re.escape(use.number))).first.click()
    expect(page).to_have_url(re.compile(rf"/access-review/emergency/{use.pk}/$"))
    expect(page.get_by_text(use.explanation)).to_be_visible()
    no_sideways_scroll(page)

    page.get_by_label("Appropriate", exact=True).check()
    field(page, "What you checked and found").fill(REVIEW_NOTE)
    page.get_by_role("button", name="Record review", exact=True).click()
    expect(page.get_by_text("Reviewed", exact=True).first).to_be_visible()
    expect(page.get_by_text(REVIEW_NOTE)).to_be_visible()
    review = page.locator("section", has=page.locator("#review-heading"))
    expect(review).to_contain_text("Appropriate")
    expect(page.get_by_role("button", name="Record review")).to_have_count(0)
    no_sideways_scroll(page)
    assert page.console_errors == []


def test_staff_without_the_capability_are_offered_and_given_nothing(page: Page, demo):
    row = hidden_patient_row(WITHOUT)
    ticket_id = row.filled_form.request_id
    sign_in(page, WITHOUT, demo.password)
    page.goto(reverse("desk:request-detail", args=[ticket_id]))
    expect(page.get_by_role("heading", level=1)).to_contain_text(row.filled_form.request.number)
    expect(page.get_by_role("link", name="Use emergency access")).to_have_count(0)
    expect(page.get_by_text("Patient information hidden", exact=True)).to_have_count(0)
    response = page.goto(reverse("desk:request-emergency", args=[ticket_id]))
    assert response.status == 403
