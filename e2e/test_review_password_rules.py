"""Password guidance and Security settings at the two reference widths in each theme."""

import pytest
from allauth.account.forms import default_token_generator
from allauth.account.utils import user_pk_to_url_str
from django.conf import settings
from django.test import Client
from django.urls import reverse
from helpers import make_user
from playwright.sync_api import expect

from core.models import SecondFactorRule

pytestmark = pytest.mark.django_db(transaction=True)
PHRASE = "Glasswing lantern among pale feathers"


def browser_session(page, live_server, person):
    browser = Client()
    browser.force_login(person)
    page.context.clear_cookies()
    page.context.add_cookies(
        [
            {
                "name": settings.SESSION_COOKIE_NAME,
                "value": browser.cookies[settings.SESSION_COOKIE_NAME].value,
                "url": live_server.url,
            }
        ]
    )


def guidance(page, url, field, minimum):
    response = page.goto(url)
    assert response.status == 200
    control = page.locator(f'input[name="{field}"]')
    expect(control).to_be_visible()
    ids = (control.get_attribute("aria-describedby") or "").split()
    assert ids, f"{field} needs a description on {response.url}"
    help_items = page.locator(", ".join(f"#{item} li" for item in ids))
    expect(help_items.filter(has_text=f"at least {minimum} characters")).to_be_visible()
    advice = help_items.filter(has_text="unrelated words")
    expect(advice).to_be_visible()
    box = control.bounding_box()
    first_line = help_items.first.bounding_box()
    assert first_line["y"] >= box["y"] + box["height"] - 1, response.url
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), response.url


def test_password_pages_and_settings(page, view, live_server):
    owner = make_user("review-owner", PHRASE, "admin")
    owner.is_staff = owner.is_superuser = True
    owner.save(update_fields=["is_staff", "is_superuser"])
    managed = make_user("review-managed", PHRASE, "technical")
    without_password = make_user("review-setting", None, "technical")

    browser_session(page, live_server, owner)
    SecondFactorRule.objects.create(pk=1, admin_console=True, revision=1)
    guidance(page, reverse("account_change_password"), "password1", 8)
    guidance(page, reverse("admin:password_change"), "new_password1", 8)
    guidance(page, reverse("admin:auth_user_password_change", args=[managed.pk]), "password1", 15)
    guidance(page, reverse("admin:accounts_user_add"), "password1", 15)
    page.goto(reverse("security-settings"))
    expect(page.locator("main").get_by_text("Passwords", exact=True).first).to_be_visible()
    expect(page.locator("main")).to_contain_text("accounts that can open the admin console")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.wait_for_load_state("networkidle")

    browser_session(page, live_server, without_password)
    guidance(page, reverse("account_set_password"), "password1", 15)
    page.wait_for_load_state("networkidle")

    page.context.clear_cookies()
    reset = reverse(
        "account_reset_password_from_key",
        args=[user_pk_to_url_str(managed), default_token_generator.make_token(managed)],
    )
    guidance(page, reset, "password1", 15)
    assert page.console_errors == []
    page.wait_for_load_state("networkidle")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_admin_password_controls_meet_the_phone_touch_size(page, live_server, theme):
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(color_scheme=theme)
    owner = make_user("touch-size-owner", PHRASE, "admin")
    owner.is_staff = owner.is_superuser = True
    owner.save(update_fields=["is_staff", "is_superuser"])
    browser_session(page, live_server, owner)
    undersized = []
    for name, args in [
        ("admin:password_change", []),
        ("admin:auth_user_password_change", [owner.pk]),
        ("admin:accounts_user_add", []),
    ]:
        assert page.goto(reverse(name, args=args)).status == 200
        controls = page.locator('input[type="password"]:visible')
        assert controls.count() >= 2
        for field in controls.all():
            height = field.bounding_box()["height"]
            if height < 44:
                undersized.append((name, field.get_attribute("name"), height))
    page.wait_for_load_state("networkidle")
    assert not undersized, undersized
