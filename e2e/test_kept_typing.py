"""What was typed, kept across a sign-out by a session limit (accounts/kept.py,
static/js/kept-typing.js, static/js/session.js).

As in e2e/test_session_limits.py, time is moved instead of waited for: the session's row is moved
back on the server and the page's clock forward by as much.
"""

import re
from datetime import timedelta

import pytest
from django.db.models.functions import Now
from django.urls import reverse
from helpers import field, sign_in
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

INACTIVITY = timedelta(minutes=30)
STAFF = "demo_client_services"
PORTAL = "demo_client"
TYPED = "Courier booked for 16:00; two coolers at the north entrance."
SIGNED_OUT = "You were signed out after 30 minutes without activity. Sign in again to continue."
KEPT = (
    "What you were typing is kept for 24 hours. Sign in again and open the same page to get it "
    "back."
)
WARNING_KEPT = re.compile(r"If you are signed out, what you have typed in the form is kept")
PUT_BACK = re.compile(
    r"What you were typing here when you were signed out at .* is back in the form"
)


def open_session(username):
    from accounts.models import AccountSession

    return AccountSession.objects.filter(user__username=username, ended_at__isnull=True)


def idle(page: Page, username: str, left: timedelta) -> None:
    assert open_session(username).update(last_active_at=Now() - (INACTIVITY - left)) == 1
    page.clock.fast_forward(int((INACTIVITY - left).total_seconds() * 1000))


def staff_ticket():
    from desk.models import ServiceRequest

    return ServiceRequest.objects.filter(
        status="open",
        category__is_restricted=False,
        team__memberships__user__username=STAFF,
    ).first()


def portal_ticket():
    from accounts.models import User
    from desk.portal_scope import portal_requests

    user = User.objects.get(username=PORTAL)
    return portal_requests(user).exclude(status="closed").order_by("pk").first()


def signed_in_at(page: Page, demo, username: str, address: str) -> None:
    page.clock.install()
    sign_in(page, username, demo.password)
    expect(page.get_by_role("heading", level=1, name="Sign in")).to_have_count(0)
    page.goto(address)


def test_a_note_typed_before_the_sign_out_comes_back_after_signing_in(page: Page, demo):
    from accounts.models import KeptDraft

    ticket = staff_ticket()
    address = f"/requests/{ticket.pk}/"
    signed_in_at(page, demo, STAFF, address)
    composer = page.locator("form.vr-composer")
    composer.get_by_label("Note", exact=True).check()
    field(composer, "Message").fill(TYPED)

    idle(page, STAFF, timedelta(seconds=90))
    dialog = page.get_by_role("alertdialog", name="Still there?")
    expect(dialog).to_be_visible()
    # The warning says what happens to the typing; its description is read when it opens.
    expect(dialog.get_by_text(WARNING_KEPT)).to_be_visible()
    expect(dialog).to_have_accessible_description(WARNING_KEPT)

    # Zero: the session ends on the server, the page sends the typing and goes to sign in
    # without asking about its unsent text (a prompt would hold the page here).
    assert open_session(STAFF).update(last_active_at=Now() - INACTIVITY - timedelta(seconds=1))
    page.clock.fast_forward(91_000)
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next="), timeout=15_000)
    expect(page.get_by_text(SIGNED_OUT)).to_be_visible()
    expect(page.get_by_text(KEPT)).to_be_visible()
    assert KeptDraft.objects.get().values["body"] == TYPED

    sign_in(page, STAFF, demo.password, visit=False)
    expect(page).to_have_url(re.compile(re.escape(address) + "$"))
    notice = composer.locator("[data-vr-kept-notice]")
    expect(notice).to_be_visible()
    expect(notice).to_contain_text(PUT_BACK)
    expect(notice).to_contain_text("Files you had chosen were not kept.")
    expect(composer.get_by_label("Note", exact=True)).to_be_checked()
    expect(field(composer, "Message")).to_have_value(TYPED)
    expect(notice.locator("[data-vr-kept-missed]")).to_be_hidden()
    assert KeptDraft.objects.get().taken_at is not None

    # Discard returns the form to how the page drew it and keeps the focus in the form.
    notice.get_by_role("link", name="Discard").click()
    expect(notice).to_be_hidden()
    expect(field(composer, "Message")).to_have_value("")
    assert composer.evaluate("form => form.contains(document.activeElement)")
    assert not page.console_errors, page.console_errors


def test_a_reply_sent_after_the_session_ended_is_kept_from_what_it_posted(page: Page, demo):
    from accounts.models import KeptDraft

    ticket = staff_ticket()
    address = f"/requests/{ticket.pk}/"
    signed_in_at(page, demo, STAFF, address)
    composer = page.locator("form.vr-composer")
    field(composer, "Message").fill(TYPED)
    # The server's limit passes while the page still believes it has time (a phone put away).
    assert open_session(STAFF).update(last_active_at=Now() - INACTIVITY - timedelta(seconds=5))
    composer.get_by_role("button", name="Send").click()
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next="))
    expect(page.get_by_text(KEPT)).to_be_visible()
    assert not ticket.entries.filter(body=TYPED).exists()
    assert KeptDraft.objects.get().values["body"] == TYPED

    sign_in(page, STAFF, demo.password, visit=False)
    expect(page).to_have_url(re.compile(re.escape(address) + "$"))
    expect(field(composer, "Message")).to_have_value(TYPED)
    expect(composer.locator("[data-vr-kept-notice]")).to_be_visible()


def test_a_portal_reply_comes_back_and_its_notice_fits_the_screen(page: Page, demo, view):
    ticket = portal_ticket()
    address = reverse("portal:request-detail", args=[ticket.number])
    signed_in_at(page, demo, PORTAL, address)
    form = page.locator("form", has=page.locator("textarea[name=body]"))
    field(form, "Your reply").fill(TYPED)
    idle(page, PORTAL, timedelta(seconds=3))
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next="), timeout=15_000)
    expect(page.get_by_text(KEPT)).to_be_visible()

    sign_in(page, PORTAL, demo.password, visit=False)
    expect(page).to_have_url(re.compile(re.escape(address) + "$"))
    notice = form.locator("[data-vr-kept-notice]")
    expect(notice).to_be_visible()
    expect(field(form, "Your reply")).to_have_value(TYPED)
    assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
    discard = notice.get_by_role("link", name="Discard")
    box = discard.bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= page.viewport_size["width"]
    if view.phone:
        assert box["height"] >= 44, box
    assert not page.console_errors, page.console_errors


def test_another_browser_of_the_same_account_gets_nothing_back(page: Page, demo, browser):
    from accounts.models import KeptDraft

    ticket = staff_ticket()
    address = f"/requests/{ticket.pk}/"
    signed_in_at(page, demo, STAFF, address)
    field(page.locator("form.vr-composer"), "Message").fill(TYPED)
    idle(page, STAFF, timedelta(seconds=3))
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next="), timeout=15_000)
    assert KeptDraft.objects.filter(taken_at__isnull=True).count() == 1

    # Another visitor of the same shared account, in a browser of their own.
    other = browser.new_context(base_url=page.url.split("/accounts/")[0])
    try:
        elsewhere = other.new_page()
        sign_in(elsewhere, STAFF, demo.password)
        elsewhere.goto(address)
        composer = elsewhere.locator("form.vr-composer")
        expect(field(composer, "Message")).to_have_value("")
        expect(composer.locator("[data-vr-kept-notice]")).to_have_count(0)
    finally:
        other.close()
    assert KeptDraft.objects.filter(taken_at__isnull=True).count() == 1
