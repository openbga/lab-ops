"""Check the neighboring admin form that shares the changed submit-button rule."""

import pytest
from django.urls import reverse
from helpers import make_user, sign_in
from playwright.sync_api import expect

from accounts.models import User

pytestmark = pytest.mark.django_db(transaction=True)


def test_end_sessions_submit_stays_reachable_after_an_empty_reason(page, view):
    phrase = "A linen notebook for autumn walks"
    owner = User.objects.create_superuser("verify-owner", "owner@example.invalid", phrase)
    person = make_user("verify-reader", phrase, "technical")
    page.goto(reverse("admin:accounts_user_sessions", args=[person.pk]))
    sign_in(page, owner.username, phrase, visit=False)
    reason = page.get_by_label("Reason")
    submit = page.get_by_role("button", name="End sessions")
    reason.fill("   ")
    submit.click()
    expect(reason).to_be_focused()
    expect(reason).to_have_attribute("aria-invalid", "true")
    reason.press("Tab")
    expect(submit).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if view.phone:
        dimensions = submit.bounding_box()
        assert dimensions["width"] >= 44 and dimensions["height"] >= 44
