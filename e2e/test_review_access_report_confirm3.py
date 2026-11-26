"""The Unused accounts list identifies each empty state when a reviewer changes filters."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from helpers import sign_in
from playwright.sync_api import expect

from accounts.models import User
from audit_review import access_report
from audit_review.models import WorkspaceUse

pytestmark = pytest.mark.django_db(transaction=True)


def test_clearing_a_filter_distinguishes_no_match_from_no_unused_account(page, view, demo):
    reviewer = User.objects.get(username="demo_supervisor")
    human = User.objects.get(username="former_technician")
    access_report.set_period(
        actor=reviewer,
        days=1,
        expected_revision=access_report.period().revision,
        reason="Synthetic",
    )
    now = timezone.now()
    User.objects.filter(is_active=True).update(last_login=now)
    User.objects.filter(pk=human.pk).update(last_login=now - timedelta(days=2))
    sign_in(page, reviewer.username, demo.password)
    url = reverse("access_review:unused")
    visits = WorkspaceUse.objects.filter(account=reviewer, source="unused")
    page.goto(f"{url}?role=admin")
    expect(page.locator("html")).to_have_attribute("data-bs-theme", view.theme)
    no_match = page.locator('.vr-empty[data-kind="no-results"]')
    expect(no_match).to_contain_text("No unused account matches these filters")
    expect(no_match).not_to_contain_text("scheduled jobs")
    assert visits.latest("occurred_at").rows == 0
    no_match.get_by_role("link", name="Clear filters").click()
    rows = page.locator("table tbody tr")
    expect(rows).to_have_count(1)
    expect(rows).to_contain_text(human.username)
    assert visits.latest("occurred_at").rows == 1
    User.objects.filter(pk=human.pk).update(last_login=now)
    page.reload()
    empty = page.locator('.vr-empty[data-kind="empty"]')
    expect(empty).to_contain_text("No unused accounts")
    expect(empty).to_contain_text("scheduled jobs' own aside")
    final = visits.latest("occurred_at")
    assert final.rows == 0 and final.filters["days"] == "1"
    page.goto(reverse("audit_review:uses"))
    row = page.locator("tbody tr").filter(has_text=final.request_ref)
    expect(row).to_contain_text("Unused after 1 day")
    assert page.console_errors == []
