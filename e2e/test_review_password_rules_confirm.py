"""Confirm that phone header actions remain reachable on the password page."""

import pytest
from django.urls import reverse
from helpers import make_user
from playwright.sync_api import expect
from test_review_password_rules import PHRASE, browser_session

pytestmark = pytest.mark.django_db(transaction=True)


def test_phone_header_buttons_fit_and_help_still_opens(page, live_server):
    owner = make_user("confirmation-owner", PHRASE, "admin")
    owner.is_superuser = owner.is_staff = True
    owner.save(update_fields=["is_staff", "is_superuser"])
    browser_session(page, live_server, owner)
    for width in (320, 390, 767, 768):
        page.set_viewport_size({"width": width, "height": 844})
        assert page.goto(reverse("admin:password_change")).status == 200
        help_button = page.get_by_role("button", name="Help", exact=True)
        expect(help_button).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
        for button in page.locator("#header button:visible").all():
            box = button.bounding_box()
            assert 0 <= box["x"] and box["x"] + box["width"] <= width, (width, box)
            if width < 768:
                assert min(box["height"], box["width"]) >= 44
        help_button.click()
        expect(help_button).to_have_attribute("aria-expanded", "true")
        page.keyboard.press("Escape")
        expect(help_button).to_have_attribute("aria-expanded", "false")
