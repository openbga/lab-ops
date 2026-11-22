"""What a staff member was typing in a new ticket, kept across a sign-out by a session limit and
given back with its category, organization and ticket form (accounts/kept.py,
desk.views.KEEP_NEW_TICKET, static/js/kept-typing.js).

As in e2e/test_kept_typing.py, time is moved instead of waited for: the session's row is moved
back on the server and the page's clock forward by as much.
"""

import re
from datetime import timedelta

import pytest
from django.db.models.functions import Now
from helpers import field, native_select, pick, sign_in
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

INACTIVITY = timedelta(minutes=30)
STAFF = "demo_client_services"
ADDRESS = "/requests/new/"
ORGANIZATION = "Example Family Clinic"
SUBJECT = "Cooler pickup for this afternoon"
ASKED = "Caller needs two coolers collected from the north entrance."
ROUTE = "Route 7, after 15:00"
KEPT = (
    "What you were typing is kept for 24 hours. Sign in again and open the same page to get it "
    "back."
)
PUT_BACK = re.compile(
    r"What you were typing here when you were signed out at .* is back in the form"
)


def open_session(username):
    from accounts.models import AccountSession

    return AccountSession.objects.filter(user__username=username, ended_at__isnull=True)


def signed_out_by_inactivity(page: Page) -> None:
    """Move the session past its limit and the page's clock with it, then wait for sign in."""
    assert open_session(STAFF).update(last_active_at=Now() - (INACTIVITY - timedelta(seconds=3)))
    page.clock.fast_forward(int((INACTIVITY - timedelta(seconds=3)).total_seconds() * 1000))
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next="), timeout=15_000)
    expect(page.get_by_text(KEPT)).to_be_visible()


def typed_new_ticket(page: Page, demo) -> None:
    page.clock.install()
    sign_in(page, STAFF, demo.password)
    page.goto(ADDRESS)
    pick(field(page, "Category"), label="Pickup or missed pickup")
    section = page.locator("#category-section")
    expect(section).to_contain_text("Ticket form")
    pick(field(page, "Ticket form"), label="Pickup")
    expect(section.get_by_text("Courier or route")).to_be_visible()
    pick(field(page, "Organization"), label=ORGANIZATION)
    field(page, "Subject").fill(SUBJECT)
    field(page, "What the client asked").fill(ASKED)
    field(page, "Courier or route").fill(ROUTE)


def no_sideways_scroll(page: Page) -> None:
    assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0


def test_a_new_ticket_comes_back_with_its_category_and_ticket_form(page: Page, demo, view):
    from accounts.models import KeptDraft

    typed_new_ticket(page, demo)
    signed_out_by_inactivity(page)
    values = KeptDraft.objects.get().values
    assert values["subject"] == SUBJECT and values["field-courier"] == ROUTE

    sign_in(page, STAFF, demo.password, visit=False)
    expect(page).to_have_url(re.compile(re.escape(ADDRESS) + "$"))
    form = page.locator("form.vr-form", has=page.locator("#category-section"))
    notice = form.locator("[data-vr-kept-notice]")
    expect(notice).to_be_visible()
    expect(notice).to_contain_text(PUT_BACK)
    expect(notice.locator("[data-vr-kept-missed]")).to_be_hidden()
    expect(field(page, "Subject")).to_have_value(SUBJECT)
    expect(field(page, "What the client asked")).to_have_value(ASKED)
    expect(field(page, "Courier or route")).to_have_value(ROUTE)
    expect(page.locator("#category-section")).to_contain_text("Ticket form")
    expect(form.locator(".ts-wrapper", has_text=ORGANIZATION).first).to_be_visible()
    no_sideways_scroll(page)
    discard = notice.get_by_role("link", name="Discard")
    box = discard.bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= page.viewport_size["width"]
    if view.phone:
        assert box["height"] >= 44, box

    # Discard draws the page as it starts, and the focus stays in the form.
    discard.click()
    expect(notice).to_be_hidden()
    expect(field(page, "Subject")).to_have_value("")
    expect(page.locator("#category-section")).not_to_contain_text("Courier or route")
    assert form.evaluate("form => form.contains(document.activeElement)")
    assert not page.console_errors, page.console_errors


def test_typing_given_back_is_kept_again_after_the_ticket_form_changes(page: Page, demo):
    from accounts.models import KeptDraft

    typed_new_ticket(page, demo)
    signed_out_by_inactivity(page)
    sign_in(page, STAFF, demo.password, visit=False)
    expect(page).to_have_url(re.compile(re.escape(ADDRESS) + "$"))
    expect(field(page, "Subject")).to_have_value(SUBJECT)

    # Choosing No form draws the category section, and its marker, again; the page still holds
    # what was given back and not sent, so it is kept again with the choice made.
    pick(field(page, "Ticket form"), label="No form")
    section = page.locator("#category-section")
    expect(section.get_by_text("Courier or route")).to_have_count(0)
    expect(native_select(field(page, "Ticket form"))).to_have_value("")
    signed_out_by_inactivity(page)
    copy = KeptDraft.objects.filter(taken_at__isnull=True).get()
    assert copy.values["subject"] == SUBJECT and copy.values["request_form"] == ""
    assert not [name for name in copy.values if name.startswith("field-")]

    sign_in(page, STAFF, demo.password, visit=False)
    expect(field(page, "Subject")).to_have_value(SUBJECT)
    expect(native_select(field(page, "Ticket form"))).to_have_value("")
    expect(section.get_by_text("Courier or route")).to_have_count(0)
    assert not page.console_errors, page.console_errors


def test_a_new_ticket_typed_in_the_dialog_opens_again_after_signing_in(page: Page, demo):
    from accounts.models import KeptDraft

    page.clock.install()
    sign_in(page, STAFF, demo.password)
    page.goto("/requests/?view=all")
    page.get_by_role("link", name="New ticket", exact=True).first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#vr-form-title")).to_have_text("New ticket")
    pick(field(dialog, "Category"), label="Pickup or missed pickup")
    expect(dialog.locator("#category-section")).to_contain_text("Ticket form")
    field(dialog, "Subject").fill(SUBJECT)
    field(dialog, "What the client asked").fill(ASKED)

    # The page behind the dialog is the list; signing in again opens the form's own page.
    signed_out_by_inactivity(page)
    assert "next=%2Frequests%2Fnew%2F" in page.url
    assert KeptDraft.objects.get().values["subject"] == SUBJECT
    sign_in(page, STAFF, demo.password, visit=False)
    expect(page).to_have_url(re.compile(re.escape(ADDRESS) + "$"))
    expect(page.locator("[data-vr-kept-notice]")).to_be_visible()
    expect(field(page, "Subject")).to_have_value(SUBJECT)
    expect(field(page, "What the client asked")).to_have_value(ASKED)
    expect(page.locator("#category-section")).to_contain_text("Ticket form")
    assert not page.console_errors, page.console_errors
