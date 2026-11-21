"""Warning and activity on a foreground page while an administrator edits the limits."""

from datetime import timedelta

import pytest
from django.db.models.functions import Now
from django.urls import reverse
from helpers import sign_in
from playwright.sync_api import expect

from accounts.models import AccountSession, User
from core import session_limits

pytestmark = pytest.mark.django_db(transaction=True)


def test_open_page_warns_on_the_shortened_schedule(page, demo):
    page.clock.install()
    sign_in(page, "demo_account_manager", demo.password)
    page.goto(reverse("clients:client-list"))
    session_limits.save_limits(
        actor=User.objects.get(username="demo_admin"),
        audience="staff",
        new={"inactivity_minutes": 5, "maximum_hours": 12},
        expected_revision=0,
        reason="Example short shift",
    )
    # Opening a form in the same window is activity under the new limits.
    # The original page and its warning script remain on screen.
    page.get_by_role("link", name="New organization", exact=True).first.click()
    expect(page.locator("#vr-form-dialog")).to_be_visible()
    idle_time = timedelta(minutes=3, seconds=20)
    assert (
        AccountSession.objects.filter(
            user__username="demo_account_manager", ended_at__isnull=True
        ).update(last_active_at=Now() - idle_time)
        == 1
    )
    page.clock.fast_forward(int(idle_time.total_seconds() * 1000))
    expect(page.get_by_role("alertdialog", name="Still there?")).to_be_visible()


def test_foreground_warning_closes_after_activity_in_a_real_second_tab(page, demo):
    from test_session_limits import STAFF, idle, is_status, signed_in_at, warning

    address = reverse("clients:client-list")
    signed_in_at(page, demo, STAFF, address)
    idle(page, STAFF, timedelta(seconds=90))
    expect(warning(page)).to_be_visible()
    other = page.context.new_page()
    try:
        response = other.goto(address)
        assert response.status == 200
        assert (
            AccountSession.objects.filter(user__username=STAFF, ended_at__isnull=True).count() == 1
        )
        with page.expect_response(is_status) as updated:
            page.clock.fast_forward(15_000)
        assert updated.value.json()["inactivity_seconds"] > 120
        expect(warning(page)).to_be_hidden()
    finally:
        other.close()


def test_coincident_deadlines_show_the_nonextendible_longest_session_warning(page, demo):
    from test_session_limits import STAFF, signed_in_at

    session_limits.save_limits(
        actor=User.objects.get(username="demo_admin"),
        audience="staff",
        new={"inactivity_minutes": 60, "maximum_hours": 1},
        expected_revision=0,
        reason="Example one-hour shift",
    )
    signed_in_at(page, demo, STAFF, reverse("clients:client-list"))
    elapsed = timedelta(minutes=58, seconds=30)
    cutoff = Now() - elapsed
    active = AccountSession.objects.filter(user__username=STAFF, ended_at__isnull=True)
    assert active.update(started_at=cutoff, last_active_at=cutoff) == 1
    with page.expect_response(
        lambda response: response.url.endswith(reverse("account-session"))
    ) as status:
        page.clock.fast_forward(int(elapsed.total_seconds() * 1000))
    answer = status.value.json()
    assert 0 < answer["inactivity_seconds"] == answer["maximum_seconds"] < 120
    expect(page.get_by_role("alertdialog", name="Your session ends soon")).to_be_visible()
