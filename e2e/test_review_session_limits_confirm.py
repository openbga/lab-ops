"""Recovery from one lost status response on a synthetic signed-in page."""

from datetime import timedelta

import pytest
from django.urls import reverse
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import expect
from test_session_limits import STAFF, idle, is_status, signed_in_at, warning

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("phase", ["foreground", "warning-open"])
def test_one_unanswered_status_is_retried_after_two_seconds(page, demo, phase):
    signed_in_at(page, demo, STAFF, reverse("clients:client-list"))
    if phase == "warning-open":
        idle(page, STAFF, timedelta(seconds=100))
        expect(warning(page)).to_be_visible()
    page.wait_for_load_state("networkidle")
    path = reverse("account-session")
    page.route("**" + path, lambda route: route.abort(), times=1)
    with page.expect_event("requestfailed", predicate=lambda req: req.url.endswith(path)):
        page.clock.fast_forward(15_000 if phase == "warning-open" else 60_000)
    page.wait_for_load_state("networkidle")
    try:
        with page.expect_response(is_status, timeout=3000) as retry:
            page.clock.fast_forward(2100)
    except PlaywrightTimeout:
        # Distinguish delayed recovery from a broken connection or a page that never polls.
        with page.expect_response(is_status) as later:
            page.clock.fast_forward(15_000 if phase == "warning-open" else 60_000)
        assert later.value.ok and later.value.json()["signed_in"]
        pytest.fail("The next regular poll succeeded but the two-second retry was missing")
    assert retry.value.ok and retry.value.json()["signed_in"]
