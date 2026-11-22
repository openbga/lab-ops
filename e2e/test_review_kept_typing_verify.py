"""Visible controls and session polling next to the adopted kept-typing journeys."""

import re
from datetime import timedelta

import pytest
from django.db.models.functions import Now
from django.urls import reverse
from helpers import field, pick, sign_in
from playwright.sync_api import expect
from test_kept_typing import STAFF, staff_ticket
from test_review_kept_typing import expire
from test_session_limits import INACTIVITY, PORTAL, open_session, signed_in_at

pytestmark = pytest.mark.django_db(transaction=True)


def test_last_edit_in_focused_pickup_time_survives_the_next_session_poll(page, demo):
    address = reverse("portal_forms:raise", args=["pickup"])
    signed_in_at(page, demo, PORTAL, address)
    field(page, "Your reference").fill("SYNTHETIC-COLLECTION")
    time = field(page, "Ready for pickup at")
    time.fill("2026-12-02 13:45")
    expect(time).to_be_focused()
    # On a page left open, the regular poll can be the first to learn of the timeout.
    assert (
        open_session(PORTAL).update(last_active_at=Now() - INACTIVITY - timedelta(seconds=1)) == 1
    )
    page.clock.fast_forward(65_000)
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next="), timeout=15_000)
    sign_in(page, PORTAL, demo.password, visit=False)
    expect(field(page, "Your reference")).to_have_value("SYNTHETIC-COLLECTION")
    expect(time).to_have_value("2026-12-02 13:45")


def test_restored_logged_contact_displays_internal_kind_and_contact_controls(page, demo):
    url = reverse("desk:request-detail", args=[staff_ticket().pk])
    signed_in_at(page, demo, STAFF, url)
    composer = page.locator("form.vr-composer")
    composer.get_by_label("Log contact", exact=True).check()
    pick(field(composer, "Direction"), value="to_client")
    pick(field(composer, "By"), value="phone")
    pick(field(composer, "Outcome"), value="message_left")
    field(composer, "With whom").fill("Sample dispatch desk")
    field(composer, "Message").fill("Please call at noon.")
    expire(page, STAFF)
    sign_in(page, STAFF, demo.password, visit=False)
    expect(composer.get_by_label("Log contact", exact=True)).to_be_checked()
    expect(composer.locator(".vr-composer-internal")).to_be_visible()
    expect(composer.locator(".vr-composer-public")).to_be_hidden()
    expect(composer.locator(".vr-composer-files")).to_be_hidden()
    expect(field(composer, "With whom")).to_have_value("Sample dispatch desk")
    expect(field(composer, "Message")).to_have_value("Please call at noon.")
    for name, value in (
        ("contact_direction", "to_client"),
        ("contact_channel", "phone"),
        ("contact_outcome", "message_left"),
    ):
        expect(composer.locator(f'select[name="{name}"]')).to_have_value(value)
    expect(composer.locator("[data-vr-kept-missed]")).to_be_hidden()


def test_no_script_status_choice_disappearance_is_announced(browser, browser_context_args, demo):
    from desk.models import ServiceRequest, Status

    ticket = staff_ticket()
    with browser.new_context(**{**browser_context_args, "java_script_enabled": False}) as context:
        page = context.new_page()
        sign_in(page, STAFF, demo.password)
        page.goto(reverse("desk:request-detail", args=[ticket.pk]))
        composer = page.locator("form.vr-composer")
        field(composer, "Message").fill("Confirm pickup at the gate.")
        composer.locator("select[name=new_status]").select_option(Status.WAITING_THIRD_PARTY)
        assert (
            open_session(STAFF).update(last_active_at=Now() - INACTIVITY - timedelta(seconds=1))
            == 1
        )
        composer.get_by_role("button", name="Send").click()
        expect(page).to_have_url(re.compile(r"/accounts/login/\?next="))
        assert (
            ServiceRequest.objects.filter(pk=ticket.pk).update(
                status=Status.RESOLVED, resolved_at=Now()
            )
            == 1
        )
        sign_in(page, STAFF, demo.password, visit=False)
        expect(field(composer, "Message")).to_have_value("Confirm pickup at the gate.")
        expect(composer.locator("select[name=new_status]")).to_have_count(0)
        expect(composer.locator("[data-vr-kept-missed]")).to_be_visible()


def test_explicit_no_stays_unconfirmed_in_the_reopened_portal_form(page, demo):
    from accounts.models import User
    from request_forms.models import Audience, FieldKind, RequestTemplate
    from request_forms.services import create_field, update_field

    pickup = RequestTemplate.objects.get(key="pickup")
    editor = User.objects.get(username="demo_admin")
    item = create_field(
        actor=editor,
        template_id=pickup.pk,
        key="acknowledgement",
        label="Collection acknowledgement",
        kind=FieldKind.YES_NO,
        audience=Audience.CLIENT,
    )
    signed_in_at(page, demo, PORTAL, reverse("portal_forms:raise", args=[pickup.key]))
    field(page, "Your reference").fill("VERIFY-COLLECTION")
    page.locator('input[name="field-acknowledgement"][value="no"]').check()
    expire(page, PORTAL)
    update_field(
        actor=editor,
        field_id=item.pk,
        expected_revision=item.revision,
        kind=FieldKind.CONFIRMATION,
        reason="Require explicit confirmation before collection.",
    )
    sign_in(page, PORTAL, demo.password, visit=False)
    expect(field(page, "Your reference")).to_have_value("VERIFY-COLLECTION")
    acknowledgement = page.get_by_role("checkbox", name="Collection acknowledgement")
    expect(acknowledgement).to_be_visible()
    expect(acknowledgement).not_to_be_checked()
    expect(page.locator("[data-vr-kept-missed]")).to_be_visible()
