"""What a reviewer sees after viewing the period form and a genuinely empty list."""

import re

import pytest
from django.urls import reverse
from django.utils import timezone
from helpers import sign_in
from playwright.sync_api import expect

from accounts.models import User
from audit_review import access_report
from audit_review.models import WorkspaceUse

pytestmark = pytest.mark.django_db(transaction=True)


def test_workspace_use_shows_the_one_day_list_visit_after_an_empty_result(page, view, demo):
    reviewer = User.objects.get(username="demo_supervisor")
    access_report.set_period(
        actor=reviewer,
        days=1,
        expected_revision=access_report.period().revision,
        reason="Synthetic one-day review",
    )
    User.objects.filter(is_active=True).update(last_login=timezone.now())
    sign_in(page, reviewer.username, demo.password)
    visits = WorkspaceUse.objects.filter(account=reviewer, source="unused")
    before = visits.count()
    page.goto(reverse("access_review:unused-period"))
    expect(
        page.get_by_role("heading", name="Unused account period", exact=True).first
    ).to_be_visible()
    assert visits.count() == before
    page.goto(reverse("access_review:unused"))
    expect(page.get_by_text("No unused accounts", exact=True).first).to_be_visible()
    expect(
        page.get_by_text(
            re.compile("creation if it never signed in, is older than the period")
        ).first
    ).to_be_visible()
    assert visits.count() == before + 1
    visit = visits.latest("occurred_at")
    page.goto(reverse("audit_review:uses"))
    expect(page.locator("html")).to_have_attribute("data-bs-theme", view.theme)
    expect(
        page.get_by_text(re.compile("Access review lists shown, each review opened")).first
    ).to_be_visible()
    row = page.locator("tbody tr").filter(has_text=visit.request_ref)
    expect(row).to_contain_text("Unused after 1 day")
    expect(row).not_to_contain_text("1 days")
    assert page.console_errors == []
