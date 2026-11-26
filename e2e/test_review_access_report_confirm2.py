"""A reviewer sees the same scope in the empty list and its Help drawer."""

import pytest
from django.urls import reverse
from django.utils import timezone
from helpers import sign_in
from playwright.sync_api import expect

from accounts.models import User
from audit_review import access_report
from audit_review.models import WorkspaceUse
from jobs.actor import system_account

pytestmark = pytest.mark.django_db(transaction=True)


def test_unused_human_accounts_and_the_jobs_exception_on_the_page_and_in_help(page, view, demo):
    from datetime import timedelta

    reviewer = User.objects.get(username="demo_supervisor")
    human = User.objects.get(username="former_technician")
    now = timezone.now()
    access_report.set_period(
        actor=reviewer,
        days=1,
        expected_revision=access_report.period().revision,
        reason="Synthetic",
    )
    User.objects.filter(is_active=True).update(last_login=now)
    robot = system_account()
    User.objects.filter(pk=robot.pk).update(
        is_active=True, last_login=None, date_joined=now - timedelta(days=4)
    )
    sign_in(page, reviewer.username, demo.password)
    page.goto(reverse("access_review:unused"))
    expect(page.locator("html")).to_have_attribute("data-bs-theme", view.theme)
    empty = page.locator(".vr-empty-body")
    expect(empty).to_be_visible()
    for phrase in ("scheduled jobs' own aside", "last sign-in", "never signed in", "older than"):
        expect(empty).to_contain_text(phrase)
    message = empty.inner_text()
    page.get_by_role("button", name="Help", exact=True).click()
    drawer = page.get_by_role("dialog", name="Page documentation")
    section = drawer.locator("details").filter(has_text="No unused accounts")
    section.locator("summary").click()
    expect(drawer.get_by_text(message, exact=True)).to_be_visible()
    drawer.get_by_role("button", name="Close documentation").click()
    use = WorkspaceUse.objects.filter(account=reviewer, source="unused").latest("occurred_at")
    assert use.rows == 0
    page.goto(reverse("audit_review:uses"))
    row = page.locator("tbody tr").filter(has_text=use.request_ref)
    expect(row).to_contain_text("Unused after 1 day")
    expect(row).not_to_contain_text("1 days")
    User.objects.filter(pk=human.pk).update(last_login=now - timedelta(days=3))
    page.goto(reverse("access_review:unused"))
    rows = page.locator("table tbody tr")
    expect(rows).to_have_count(1)
    expect(rows).to_contain_text(human.username)
    expect(rows).not_to_contain_text(robot.username)
    assert page.console_errors == []
