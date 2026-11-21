"""The warning before automatic sign-out (accounts/sessions.py, includes/session_warning.html,
static/js/session.js).

The limits are minutes and hours, so these journeys move time instead of waiting for it: the
session's row (accounts.models.AccountSession) is moved back, as if the time had passed on the
server, and the page's clock (Playwright's clock) is moved forward by as much, as if it had passed
in the browser. Opening a page is activity, so the row is moved after the page has loaded.
"""

import re
from datetime import timedelta

import pytest
from django.db.models.functions import Now
from django.urls import reverse
from helpers import field, sign_in
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

# The starting limits, for staff and for the client portal (core.session_limits.DEFAULTS).
INACTIVITY = timedelta(minutes=30)
LONGEST = timedelta(hours=12)
STAFF = "demo_account_manager"
PORTAL = "demo_client"
NOTICE = "You were signed out after 30 minutes without activity. Sign in again to continue."
INACTIVITY_TEXT = re.compile(
    r"^You will be signed out in [01]:\d\d because there has been no activity\.$"
)
LONGEST_TEXT = re.compile(
    r"^Your session reaches its longest length in [34]:\d\d\. "
    r"Save your work; you will need to sign in again\.$"
)


# The nearest element that would announce changes to the countdown, if any.
LIVE_ANCESTOR = """el => {
    const live = el.closest(
        '[aria-live]:not([aria-live="off"]), [role=alert], [role=status], [role=log]');
    return live && live.outerHTML.slice(0, 80);
}"""


def open_session(username):
    from accounts.models import AccountSession

    return AccountSession.objects.filter(user__username=username, ended_at__isnull=True)


def idle(page: Page, username: str, left: timedelta) -> None:
    """As if nothing had happened until only `left` remains before the sign-out for no activity:
    on the server (the row's last activity, by the database's clock) and on the page's clock."""
    assert open_session(username).update(last_active_at=Now() - (INACTIVITY - left)) == 1
    page.clock.fast_forward(int((INACTIVITY - left).total_seconds() * 1000))


def warning(page: Page, title: str = "Still there?"):
    return page.get_by_role("alertdialog", name=title)


def is_status(response) -> bool:
    return response.url.endswith(reverse("account-session"))


def signed_in_at(page: Page, demo, username: str, address: str) -> None:
    """Signed in, with the page's clock under the test's control, on `address`."""
    page.clock.install()
    sign_in(page, username, demo.password)
    expect(page.get_by_role("heading", level=1, name="Sign in")).to_have_count(0)
    page.goto(address)


def test_stay_signed_in_extends_the_session_and_leaves_the_page_usable(page: Page, demo):
    signed_in_at(page, demo, STAFF, reverse("clients:client-list"))
    idle(page, STAFF, timedelta(seconds=90))

    dialog = warning(page)
    expect(dialog).to_be_visible()
    expect(dialog).to_have_accessible_description(INACTIVITY_TEXT)
    stay = dialog.get_by_role("button", name="Stay signed in")
    expect(stay).to_be_focused()
    expect(dialog.get_by_role("button", name="Sign out")).to_be_visible()
    expect(dialog.get_by_role("button", name="Close")).to_be_hidden()
    # The countdown goes down each second, outside any live region: the dialog is announced
    # once, when it opens, not every second.
    countdown = dialog.locator("[data-vr-session-countdown]").filter(visible=True)
    expect(countdown).not_to_have_text(countdown.inner_text())
    assert countdown.evaluate(LIVE_ANCESTOR) is None

    stay.click()
    expect(dialog).to_be_hidden()
    assert open_session(STAFF).filter(last_active_at__gt=Now() - timedelta(seconds=15)).exists()
    # Nothing of the dialog is left over the page: its controls work.
    expect(page.locator(".modal-backdrop")).to_have_count(0)
    page.get_by_role("link", name="New organization", exact=True).first.click()
    expect(page.locator("#vr-form-dialog")).to_be_visible()
    assert not page.console_errors, page.console_errors


def test_at_zero_the_page_goes_to_sign_in_and_signing_in_returns_to_it(page: Page, demo):
    from accounts.models import AccountSession

    address = reverse("clients:client-list") + "?q=a&sort=-name"
    signed_in_at(page, demo, STAFF, address)
    session = open_session(STAFF).get()
    idle(page, STAFF, timedelta(seconds=3))

    expect(warning(page)).to_be_visible()
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next="), timeout=15_000)
    expect(page.get_by_text(NOTICE)).to_be_visible()
    session = AccountSession.objects.get(pk=session.pk)
    assert session.ended_at is not None and session.ending == AccountSession.Ending.INACTIVITY

    sign_in(page, STAFF, demo.password, visit=False)
    expect(page).to_have_url(re.compile(re.escape(address) + "$"))
    expect(warning(page)).to_be_hidden()


def test_the_longest_session_warning_says_to_save_and_close_dismisses_it(page: Page, demo):
    sign_in(page, STAFF, demo.password)
    open_session(STAFF).update(started_at=Now() - (LONGEST - timedelta(minutes=4)))
    page.goto(reverse("clients:client-list"))

    dialog = warning(page, "Your session ends soon")
    expect(dialog).to_be_visible()
    expect(dialog).to_have_accessible_description(LONGEST_TEXT)
    close = dialog.get_by_role("button", name="Close")
    expect(close).to_be_focused()
    # Nothing extends the longest session.
    expect(dialog.get_by_role("button", name="Stay signed in")).to_be_hidden()
    close.click()
    expect(dialog).to_be_hidden()
    expect(page.locator(".modal-backdrop")).to_have_count(0)
    assert not page.console_errors, page.console_errors


def test_activity_in_another_tab_puts_the_warning_off_or_closes_it(page: Page, demo):
    signed_in_at(page, demo, STAFF, reverse("clients:client-list"))
    # Another tab was active a moment before this page's warning was due.
    open_session(STAFF).update(last_active_at=Now())
    with page.expect_response(is_status) as status:
        page.clock.fast_forward(int((INACTIVITY - timedelta(seconds=90)).total_seconds() * 1000))
    assert status.value.json()["inactivity_seconds"] > 120
    page.wait_for_timeout(500)
    expect(warning(page)).to_be_hidden()

    # The page now counts from that answer: idle from there, it warns.
    idle(page, STAFF, timedelta(seconds=90))
    expect(warning(page)).to_be_visible()
    # Stay signed in in another tab while the warning is open: the next check closes it here.
    open_session(STAFF).update(last_active_at=Now())
    page.clock.fast_forward(15_000)
    expect(warning(page)).to_be_hidden()
    assert not page.console_errors, page.console_errors


def test_sign_out_in_the_warning_ends_the_session(page: Page, demo):
    from accounts.models import AccountSession

    signed_in_at(page, demo, STAFF, reverse("clients:client-list"))
    session = open_session(STAFF).get()
    idle(page, STAFF, timedelta(seconds=90))

    warning(page).get_by_role("button", name="Sign out").click()
    expect(page.get_by_role("heading", level=1, name="Sign in")).to_be_visible()
    session.refresh_from_db()
    assert session.ending == AccountSession.Ending.SIGNED_OUT


def test_the_client_portal_warns_the_same_way_and_answers_the_keyboard(page: Page, demo):
    signed_in_at(page, demo, PORTAL, reverse("portal:home"))
    idle(page, PORTAL, timedelta(seconds=90))

    dialog = warning(page)
    expect(dialog).to_be_visible()
    expect(dialog).to_have_accessible_description(INACTIVITY_TEXT)
    stay = dialog.get_by_role("button", name="Stay signed in")
    expect(stay).to_be_focused()
    # Escape does not close it (that would let the session end unseen); Tab stays inside.
    page.keyboard.press("Escape")
    expect(dialog).to_be_visible()
    page.keyboard.press("Shift+Tab")
    expect(dialog.get_by_role("button", name="Sign out")).to_be_focused()
    page.keyboard.press("Tab")
    expect(stay).to_be_focused()
    page.keyboard.press("Enter")
    expect(dialog).to_be_hidden()
    assert open_session(PORTAL).filter(last_active_at__gt=Now() - timedelta(seconds=15)).exists()
    assert not page.console_errors, page.console_errors


def test_the_warning_opens_over_a_form_and_gives_it_back(page: Page, demo):
    signed_in_at(page, demo, STAFF, reverse("clients:client-list"))
    page.get_by_role("link", name="New organization", exact=True).first.click()
    form = page.locator("#vr-form-dialog")
    name = field(form, "Organization name")
    expect(name).to_be_focused()
    name.fill("E2E unsaved organization")
    idle(page, STAFF, timedelta(seconds=90))

    dialog = warning(page)
    expect(dialog).to_be_visible()
    expect(dialog).to_have_accessible_description(INACTIVITY_TEXT)
    stay = dialog.get_by_role("button", name="Stay signed in")
    expect(stay).to_be_focused()
    stay.click()
    expect(dialog).to_be_hidden()
    expect(form).to_be_visible()
    expect(name).to_have_value("E2E unsaved organization")
    expect(name).to_be_focused()
    assert not page.console_errors, page.console_errors


def test_the_warning_fits_the_screen_and_the_thumb(page: Page, demo, view):
    signed_in_at(page, demo, STAFF, reverse("clients:client-list"))
    idle(page, STAFF, timedelta(seconds=90))

    dialog = warning(page)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("button", name="Stay signed in")).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
    viewport = page.viewport_size
    boxes = {}
    for name in ("Sign out", "Stay signed in"):
        box = dialog.get_by_role("button", name=name).bounding_box()
        assert box, name
        assert box["x"] >= 0 and box["x"] + box["width"] <= viewport["width"], (name, box)
        assert box["y"] >= 0 and box["y"] + box["height"] <= viewport["height"], (name, box)
        boxes[name] = box
    if view.phone:
        # In thumb reach: at the foot of the screen, full width, the primary one last.
        stay, sign_out = boxes["Stay signed in"], boxes["Sign out"]
        assert stay["y"] > viewport["height"] * 0.75, stay
        assert stay["y"] > sign_out["y"] + sign_out["height"] - 1, (stay, sign_out)
        assert min(stay["height"], sign_out["height"]) >= 44, boxes
        assert stay["width"] > viewport["width"] * 0.7, stay


def test_a_dialog_opened_or_saved_after_the_session_ended_goes_to_sign_in(page: Page, demo):
    """The server decides, whatever the page believes: a form dialog opened, or a dialog's form
    saved, after the session passed its limit lands on the sign-in page with the notice, set to
    return to the page the dialog was opened on, and saves nothing."""
    from clients.models import Client

    clients = reverse("clients:client-list")
    sign_in(page, STAFF, demo.password)
    page.goto(clients)
    assert open_session(STAFF).update(last_active_at=Now() - INACTIVITY - timedelta(seconds=5))
    page.get_by_role("link", name="New organization", exact=True).first.click()
    page.wait_for_url(re.compile(r"/accounts/login/"))
    expect(page.get_by_text(NOTICE)).to_be_visible()
    assert page.url.endswith("/accounts/login/?next=/clients/")

    sign_in(page, STAFF, demo.password)
    page.goto(clients)
    page.get_by_role("link", name="New organization", exact=True).first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    field(dialog, "Organization name").fill("Example Late Clinic")
    assert open_session(STAFF).update(last_active_at=Now() - INACTIVITY - timedelta(seconds=5))
    dialog.get_by_role("button", name="Create organization").click()
    page.wait_for_url(re.compile(r"/accounts/login/"))
    expect(page.get_by_text(NOTICE)).to_be_visible()
    assert page.url.endswith("/accounts/login/?next=/clients/")
    assert not Client.objects.filter(name="Example Late Clinic").exists()
