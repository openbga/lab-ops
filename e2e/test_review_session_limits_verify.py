"""Foreground warning after the browser has received the limits from the open dialog's request."""

from datetime import timedelta

import pytest
from django.db.models.functions import Now
from django.urls import reverse
from playwright.sync_api import expect
from test_session_limits import STAFF, is_status, open_session, signed_in_at, warning

from accounts.models import User
from core import session_limits

pytestmark = pytest.mark.django_db(transaction=True)


def test_warning_rechecks_after_the_dialog_and_on_the_next_foreground_minute(page, demo):
    signed_in_at(page, demo, STAFF, reverse("clients:client-list"))
    session_limits.save_limits(
        actor=User.objects.get(username="demo_admin"),
        audience="staff",
        new={"inactivity_minutes": 5, "maximum_hours": 12},
        expected_revision=0,
        reason="Example shift",
    )
    with page.expect_response(is_status) as changed:
        page.get_by_role("link", name="New organization", exact=True).first.click()
    expect(page.locator("#vr-form-dialog")).to_be_visible()
    assert changed.value.json()["inactivity_limit_seconds"] == 300
    page.wait_for_load_state("networkidle")

    # Advance one minute at a time on both clocks. Finish each server exchange before
    # advancing the clocks again so that the response and the page agree on its age.
    for minutes in (1, 2, 3):
        assert open_session(STAFF).update(last_active_at=Now() - timedelta(minutes=minutes)) == 1
        with page.expect_response(is_status) as clock:
            page.clock.fast_forward(60_000)
        remaining = clock.value.json()["inactivity_seconds"]
        assert abs(remaining - (300 - 60 * minutes)) <= 2
        page.wait_for_load_state("networkidle")
    expect(warning(page)).to_be_visible()
    assert not page.console_errors
