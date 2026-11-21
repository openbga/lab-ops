"""Recovery while an inactivity warning is already on screen."""

from datetime import timedelta

import pytest
from django.urls import reverse
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import expect
from test_session_limits import STAFF, idle, is_status, signed_in_at, warning

pytestmark = pytest.mark.django_db(transaction=True)


def test_open_warning_recovers_after_two_consecutive_lost_status_responses(page, demo):
    address = reverse("clients:client-list")
    signed_in_at(page, demo, STAFF, address)
    idle(page, STAFF, timedelta(seconds=100))
    expect(warning(page)).to_be_visible()
    page.wait_for_load_state("networkidle")
    path = reverse("account-session")
    page.route("**" + path, lambda route: route.abort(), times=2)
    for delay in (15_000, 2100):
        with page.expect_event(
            "requestfailed", predicate=lambda req: req.url.endswith(path), timeout=4500
        ):
            page.clock.fast_forward(delay)
        page.wait_for_load_state("networkidle")
    other = page.context.new_page()
    try:
        assert other.goto(address).ok
        with page.expect_response(is_status, timeout=4500) as recovered:
            page.clock.fast_forward(2100)
        assert recovered.value.json()["inactivity_seconds"] > 120
        expect(warning(page)).to_be_hidden()
    finally:
        other.close()


def test_open_warning_checks_status_after_the_stay_reply_is_lost(page, demo):
    signed_in_at(page, demo, STAFF, reverse("clients:client-list"))
    idle(page, STAFF, timedelta(seconds=100))
    dialog = warning(page)
    expect(dialog).to_be_visible()
    page.wait_for_load_state("networkidle")
    path = reverse("account-session-continue")
    received = []

    def lose_reply(route):
        # The server committed the activity. Only its reply to this tab was lost.
        result = route.fetch()
        received.append(result.json())
        result.dispose()
        route.abort()

    page.route("**" + path, lose_reply, times=1)
    with page.expect_event("requestfailed", predicate=lambda req: req.url.endswith(path)):
        dialog.get_by_role("button", name="Stay signed in").click()
    expect(page.locator("[data-vr-session-error]")).to_be_visible()
    assert received and received[0]["inactivity_seconds"] > 29 * 60
    try:
        with page.expect_response(is_status, timeout=3000) as recovered:
            page.clock.fast_forward(2100)
    except PlaywrightTimeout:
        with page.expect_response(is_status) as later:
            page.clock.fast_forward(15_000)
        assert later.value.json()["inactivity_seconds"] > 29 * 60
        expect(dialog).to_be_hidden()
        pytest.fail(
            "The ordinary poll recovered the committed Stay, but the short status retry was missing"
        )
    assert recovered.value.json()["inactivity_seconds"] > 29 * 60
    expect(dialog).to_be_hidden()
