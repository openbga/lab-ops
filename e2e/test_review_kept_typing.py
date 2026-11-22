"""Independent browser regressions for the session-limit typing promise on synthetic tickets.

Adopted from review round 1 (.local/review-history/kept-typing-round-1.md; the original file is
kept beside it). A case changed while adopting is marked "Adopted:" with the reason.
"""

import re
from datetime import timedelta

import pytest
from django.db.models.functions import Now
from django.urls import reverse
from helpers import field, pick, sign_in
from playwright.sync_api import expect
from test_session_limits import INACTIVITY, PORTAL, idle, open_session, signed_in_at, warning

pytestmark = pytest.mark.django_db(transaction=True)


KEPT = (
    "What you were typing is kept for 24 hours. Sign in again and open the same page to get it "
    "back."
)
WARNING_KEPT = re.compile(r"If you are signed out, what you have typed in the form is kept")


def portal_ticket():
    from accounts.models import User
    from desk.portal_scope import portal_requests

    return (
        portal_requests(User.objects.get(username=PORTAL))
        .exclude(status="closed")
        .order_by("pk")
        .first()
    )


def expire(page, username):
    """Let the page learn of a real inactivity ending without waiting thirty minutes."""
    idle(page, username, timedelta(seconds=3))
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next="), timeout=15_000)


def tab_to(page, target):
    for _ in range(80):
        page.keyboard.press("Tab")
        if target.evaluate("button => document.activeElement === button"):
            return
    pytest.fail("The Discard button could not be reached by Tab")


def test_portal_warning_and_recovery_by_keyboard(page, demo, view):
    address = reverse("portal:request-detail", args=[portal_ticket().number])
    signed_in_at(page, demo, PORTAL, address)
    expect(page.locator("html")).to_have_attribute("data-bs-theme", view.theme)
    reply = field(page, "Your reply")
    reply.fill("Two boxes are ready for the courier.")
    idle(page, PORTAL, timedelta(seconds=90))
    dialog = warning(page)
    expect(dialog).to_have_accessible_description(WARNING_KEPT)
    stay = dialog.get_by_role("button", name="Stay signed in")
    expect(stay).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(dialog.get_by_role("button", name="Sign out")).to_be_focused()
    page.keyboard.press("Tab")
    expect(stay).to_be_focused()
    page.keyboard.press("Enter")
    expect(dialog).to_be_hidden()
    expect(reply).to_be_focused()
    expire(page, PORTAL)
    expect(page.get_by_text(KEPT)).to_be_visible()
    sign_in(page, PORTAL, demo.password, visit=False)
    expect(reply).to_have_value("Two boxes are ready for the courier.")
    notice = page.locator("[data-vr-kept-notice]")
    # Adopted: Discard is a link that opens the page again (it works without scripts too).
    discard = notice.get_by_role("link", name="Discard")
    expect(discard).to_be_visible()
    tab_to(page, discard)
    expect(discard).to_be_focused()
    box = discard.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= view.viewport["width"]
    if view.phone:
        assert min(box["width"], box["height"]) >= 44
    page.keyboard.press("Enter")
    expect(notice).to_be_hidden()
    expect(reply).to_have_value("")
    expect(reply).to_be_focused()
    assert not page.console_errors


@pytest.mark.parametrize("answer", ["pickup time", "storage choices"])
def test_pickup_answers_return_in_the_visible_controls(page, demo, answer):
    from accounts.models import KeptDraft

    address = reverse("portal_forms:raise", args=["pickup"])
    signed_in_at(page, demo, PORTAL, address)
    ready_at = field(page, "Ready for pickup at")
    storage = [page.get_by_label(label, exact=True) for label in ("Refrigerated", "Dry ice")]
    if answer == "pickup time":
        ready_at.fill("2026-11-23 16:45")
    else:
        for box in storage:
            box.check()
    field(page, "Your reference").fill("PICKUP-REVIEW-27")
    expire(page, PORTAL)
    expect(page.get_by_text(KEPT)).to_be_visible()
    if answer == "pickup time":
        assert KeptDraft.objects.get().values["field-ready-at"] == "2026-11-23T16:45"
    sign_in(page, PORTAL, demo.password, visit=False)
    expect(page.get_by_role("heading", level=1, name="Request a pickup")).to_be_visible()
    expect(field(page, "Your reference")).to_have_value("PICKUP-REVIEW-27")
    if answer == "pickup time":
        expect(ready_at).to_have_value("2026-11-23 16:45")
    else:
        for box in storage:
            expect(box).to_be_checked()


def test_warning_only_promises_a_copy_when_there_is_typing_to_keep(page, demo):
    from accounts.models import KeptDraft

    signed_in_at(page, demo, PORTAL, reverse("portal:request-create"))
    field(page, "Urgent").check()
    idle(page, PORTAL, timedelta(seconds=90))
    dialog = warning(page)
    expect(dialog).to_be_visible()
    claimed_recovery = dialog.get_by_text(WARNING_KEPT).is_visible()
    assert open_session(PORTAL).update(last_active_at=Now() - INACTIVITY - timedelta(seconds=1))
    page.clock.fast_forward(91_000)
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next="), timeout=15_000)
    assert not KeptDraft.objects.exists()
    assert not claimed_recovery, "The warning promised to restore a choices-only form"


def test_last_reply_kept_when_ticket_closes_is_returned_as_an_internal_note(page, demo):
    from test_kept_typing import STAFF, staff_ticket

    from desk.models import ServiceRequest, Status

    ticket = staff_ticket()
    signed_in_at(page, demo, STAFF, reverse("desk:request-detail", args=[ticket.pk]))
    form = page.locator("form.vr-composer")
    form.get_by_label("Reply", exact=True).check()
    field(form, "Message").fill("The new dispatch time is 14:00.")
    expire(page, STAFF)
    assert (
        ServiceRequest.objects.filter(pk=ticket.pk).update(
            status=Status.CLOSED, resolved_at=Now(), closed_at=Now()
        )
        == 1
    )
    sign_in(page, STAFF, demo.password, visit=False)
    expect(form.get_by_label("Reply", exact=True)).to_have_count(0)
    expect(form.get_by_label("Note", exact=True)).to_be_checked()
    expect(field(form, "Message")).to_have_value("The new dispatch time is 14:00.")
    expect(form.locator("[data-vr-kept-missed]")).to_be_visible()


def refused_reply(page, demo):
    ticket = portal_ticket()
    address = reverse("portal:request-detail", args=[ticket.number])
    signed_in_at(page, demo, PORTAL, address)
    reply = field(page, "Your reply")
    reply.fill("The dispatcher has the return slip.")
    form = page.locator("form", has=reply)
    form.locator("input[type=file]").set_input_files(
        {"name": "return-slip.pdf", "mimeType": "application/pdf", "buffer": b"Plain text receipt"}
    )
    form.get_by_role("button", name="Send reply").click()
    expect(page).to_have_url(re.compile(r"/reply/$"))
    expect(reply).to_have_value("The dispatcher has the return slip.")
    return reply, address


def test_unsent_reply_still_on_a_validation_page_survives_the_limit(page, demo):
    from accounts.models import KeptDraft

    refused_reply(page, demo)
    expire(page, PORTAL)
    draft = KeptDraft.objects.filter(user__username=PORTAL).first()
    assert draft is not None, "The reply still displayed after validation was lost at sign-out"
    assert draft.values["body"] == "The dispatcher has the return slip."


def test_retained_text_is_available_to_correct_after_a_change_to_numeric(page, demo):
    from accounts.models import User
    from request_forms.models import Audience, FieldKind, RequestTemplate
    from request_forms.services import create_field, update_field

    pickup = RequestTemplate.objects.get(key="pickup")
    admin = User.objects.get(username="demo_admin")
    dispatch = create_field(
        actor=admin,
        template_id=pickup.pk,
        key="dispatch",
        label="Dispatch point",
        kind=FieldKind.SHORT_TEXT,
        audience=Audience.CLIENT,
    )
    address = reverse("portal_forms:raise", args=[pickup.key])
    signed_in_at(page, demo, PORTAL, address)
    field(page, "Dispatch point").fill("Side entrance")
    field(page, "Your reference").fill("PICKUP-REVIEW-28")
    expire(page, PORTAL)
    expect(page.get_by_text(KEPT)).to_be_visible()
    update_field(
        actor=admin,
        field_id=dispatch.pk,
        expected_revision=dispatch.revision,
        reason="The dispatch point is now recorded by number.",
        kind=FieldKind.NUMBER,
    )
    sign_in(page, PORTAL, demo.password, visit=False)
    expect(field(page, "Your reference")).to_have_value("PICKUP-REVIEW-28")
    expect(field(page, "Dispatch point")).to_have_value("Side entrance")
    notice = page.locator("[data-vr-kept-notice]")
    expect(notice).to_be_visible()


def test_a_status_choice_replaced_by_resolution_is_reported(page, demo):
    from test_kept_typing import STAFF, staff_ticket

    from desk.models import ServiceRequest, Status

    ticket = staff_ticket()
    signed_in_at(page, demo, STAFF, reverse("desk:request-detail", args=[ticket.pk]))
    form = page.locator("form.vr-composer")
    field(form, "Message").fill("Awaiting the courier.")
    pick(field(form, "Then set status"), value=Status.WAITING_THIRD_PARTY)
    expire(page, STAFF)
    assert (
        ServiceRequest.objects.filter(pk=ticket.pk).update(
            status=Status.RESOLVED, resolved_at=Now()
        )
        == 1
    )
    sign_in(page, STAFF, demo.password, visit=False)
    expect(field(form, "Message")).to_have_value("Awaiting the courier.")
    expect(field(form, "Then set status")).to_have_count(0)
    expect(form.locator("[data-vr-kept-missed]")).to_be_visible()


def test_discard_on_general_portal_ticket_leaves_visible_keyboard_focus(page, demo):
    signed_in_at(page, demo, PORTAL, reverse("portal:request-create"))
    field(page, "Subject").fill("Courier handover")
    expire(page, PORTAL)
    expect(page.get_by_text(KEPT)).to_be_visible()
    sign_in(page, PORTAL, demo.password, visit=False)
    subject = field(page, "Subject")
    expect(subject).to_have_value("Courier handover")
    discard = page.get_by_role("link", name="Discard", exact=True)  # Adopted: a link
    tab_to(page, discard)
    page.keyboard.press("Enter")
    expect(subject).to_have_value("")
    focus = page.locator("form.vr-form").evaluate(
        """form => {
            const target = document.activeElement;
            const rect = target.getBoundingClientRect();
            return {
                inside: form.contains(target), tag: target.tagName, name: target.name,
                width: rect.width, height: rect.height,
            };
        }"""
    )
    assert focus["inside"] and min(focus["width"], focus["height"]) > 1, focus


def test_a_form_posted_after_timeout_is_recoverable_without_javascript(
    browser, browser_context_args, demo
):
    from accounts.models import KeptDraft

    address = reverse("portal:request-detail", args=[portal_ticket().number])
    with browser.new_context(**{**browser_context_args, "java_script_enabled": False}) as context:
        page = context.new_page()
        sign_in(page, PORTAL, demo.password)
        page.goto(address)
        reply = field(page, "Your reply")
        reply.fill("Checking the Friday dispatch.")
        assert open_session(PORTAL).update(last_active_at=Now() - INACTIVITY - timedelta(seconds=1))
        page.get_by_role("button", name="Send reply").click()
        expect(page.get_by_text(KEPT)).to_be_visible()
        assert KeptDraft.objects.get().values["body"] == "Checking the Friday dispatch."
        sign_in(page, PORTAL, demo.password, visit=False)
        expect(page).to_have_url(re.compile(re.escape(address) + "$"))
        assert KeptDraft.objects.get().taken_at is not None
        expect(reply).to_have_value("Checking the Friday dispatch.")


def test_sign_in_after_a_refused_reply_returns_to_the_ticket_page(page, demo):
    reply, address = refused_reply(page, demo)
    reply.fill("The dispatcher has confirmed the Friday collection.")
    expire(page, PORTAL)
    # Adopted: the reply address now sends a GET on to the ticket page (302); wait for the page.
    with page.expect_response(
        lambda response: (
            response.request.is_navigation_request()
            and response.request.method == "GET"
            and response.url.endswith(address)
        )
    ) as landing:
        sign_in(page, PORTAL, demo.password, visit=False)
    assert landing.value.status == 200
    expect(page).to_have_url(re.compile(re.escape(address) + "$"))


def test_supply_order_restores_quantities_along_with_its_delivery_note(page, demo):
    address = reverse("portal_forms:raise", args=["supply-order"])
    signed_in_at(page, demo, PORTAL, address)
    quantity = page.get_by_label(re.compile(r"^Shipping boxes"))
    quantity.fill("7")
    delivery = field(page, "Delivery notes")
    delivery.fill("Drop the delivery at receiving.")
    expire(page, PORTAL)
    expect(page.get_by_text(KEPT)).to_be_visible()
    sign_in(page, PORTAL, demo.password, visit=False)
    expect(delivery).to_have_value("Drop the delivery at receiving.")
    expect(quantity).to_have_value("7")
