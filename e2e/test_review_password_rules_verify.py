"""Phone controls beside the password fields on the real administration page."""

import pytest
from django.urls import reverse
from helpers import make_user
from playwright.sync_api import expect
from test_review_password_rules import PHRASE, browser_session

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("width", [390, 767])
def test_password_page_buttons_meet_the_phone_touch_size(page, live_server, width):
    """Low, pre-existing. The phone rule promises 44 px targets for buttons as well as fields."""
    page.set_viewport_size({"width": width, "height": 844})
    owner = make_user("verify-console", PHRASE, "admin")
    owner.is_superuser = owner.is_staff = True
    owner.save(update_fields=["is_staff", "is_superuser"])
    browser_session(page, live_server, owner)
    assert page.goto(reverse("admin:password_change")).status == 200
    submit = page.locator('#content input[type="submit"]')
    expect(submit).to_be_visible()
    assert submit.bounding_box()["height"] >= 44
    expect(page.get_by_role("button", name="Help", exact=True)).to_be_visible()
    buttons = page.locator("#header button:visible").evaluate_all(
        """buttons => buttons.map(button => {
          const {width, height} = button.getBoundingClientRect()
          const label = button.getAttribute('aria-label') || button.innerText.trim()
          return [label, width, height]
        })"""
    )
    undersized = [(name, w, h) for name, w, h in buttons if min(w, h) < 44]
    assert not undersized, undersized
