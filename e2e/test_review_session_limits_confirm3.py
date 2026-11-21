"""How the warning reconciles a lost Stay reply with the next status answers."""

from datetime import timedelta

import pytest
from django.db.models.functions import Now
from django.urls import reverse
from playwright.sync_api import expect
from test_session_limits import STAFF, idle, is_status, open_session, signed_in_at, warning

pytestmark = pytest.mark.django_db(transaction=True)


def open_warning(page, demo, *, maximum_warning=False):
    signed_in_at(page, demo, STAFF, reverse("clients:client-list"))
    if maximum_warning:
        assert (
            open_session(STAFF).update(
                started_at=Now() - timedelta(hours=12) + timedelta(minutes=4)
            )
            == 1
        )
    idle(page, STAFF, timedelta(seconds=90))
    expect(warning(page)).to_be_visible()
    page.wait_for_load_state("networkidle")


def press_and_lose_reply(page):
    stay = reverse("account-session-continue")
    receipts = []

    def lose_committed_reply(route):
        response = route.fetch()
        receipts.append(response.json())
        response.dispose()
        route.abort()

    page.route("**" + stay, lose_committed_reply)
    with page.expect_event("requestfailed", predicate=lambda r: r.url.endswith(stay)):
        warning(page).get_by_role("button", name="Stay signed in").click()
    error = page.locator("[data-vr-session-error]")
    expect(error).to_be_visible()
    assert len(receipts) == 1 and receipts[0]["inactivity_seconds"] > 29 * 60
    return receipts


def test_recovery_after_losing_stay_reply_and_first_status(page, demo):
    open_warning(page, demo)
    status = reverse("account-session")
    page.route("**" + status, lambda route: route.abort(), times=1)
    receipts = press_and_lose_reply(page)
    with page.expect_event(
        "requestfailed", predicate=lambda r: r.url.endswith(status), timeout=3500
    ):
        page.clock.fast_forward(2100)
    page.wait_for_load_state("networkidle")
    with page.expect_response(is_status, timeout=3500) as recovered:
        page.clock.fast_forward(2100)
    assert recovered.value.json()["inactivity_seconds"] > 29 * 60
    expect(warning(page)).to_be_hidden()
    assert len(receipts) == 1


def test_reconciled_stay_clears_error_when_the_maximum_warning_is_due(page, demo):
    open_warning(page, demo, maximum_warning=True)
    receipts = press_and_lose_reply(page)
    # Also reach the ordinary poll on the earlier candidate, which had no short retry.
    with page.expect_response(is_status) as recovered:
        page.clock.fast_forward(15_000)
    assert recovered.value.json()["inactivity_seconds"] > 29 * 60
    expect(
        warning(page, "Your session ends soon").get_by_role("button", name="Close")
    ).to_be_visible()
    assert len(receipts) == 1
    expect(page.locator("[data-vr-session-error]")).to_be_hidden(timeout=1500)


def test_lost_stay_gets_a_status_read_after_the_press(page, demo):
    open_warning(page, demo)
    status = reverse("account-session")
    pending = []

    def hold_old_status(route):
        response = route.fetch()
        pending.append((route, response))

    page.route("**" + status, hold_old_status, times=1)
    with page.expect_request(lambda req: req.url.endswith(status)):
        page.clock.fast_forward(15_000)
    # Deliver only after the Stay's two-second timer. The response describes the row before Stay.
    receipts = press_and_lose_reply(page)
    [(route, old)] = pending
    assert 0 < old.json()["inactivity_seconds"] <= 90
    page.clock.fast_forward(2100)
    with page.expect_response(is_status):
        route.fulfill(response=old)
    old.dispose()
    page.wait_for_load_state("networkidle")
    page.clock.fast_forward(2100)
    try:
        expect(warning(page)).to_be_hidden(timeout=1500)
    except AssertionError:
        with page.expect_response(is_status) as later:
            page.clock.fast_forward(15_000)
        assert later.value.json()["inactivity_seconds"] > 29 * 60
        expect(warning(page)).to_be_hidden()
        pytest.fail(
            "Only the next ordinary poll recovered the committed Stay after the stale reply"
        )
    assert len(receipts) == 1
