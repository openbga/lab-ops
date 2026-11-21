"""Keyboard access to personal session confirmations and the administration reason form.

Written by the independent reviewer (round 1 at 86e3571). Adopted with one change, marked
"Adopted:": the keyboard journey waits for the dialog to finish opening before pressing Tab."""

import re

import pytest
from django.urls import reverse
from helpers import sign_in
from playwright.sync_api import expect

from accounts.models import User

pytestmark = pytest.mark.django_db(transaction=True)


def test_keyboard_cancels_and_returns_to_each_opener(
    page, browser, browser_context_args, demo, view
):
    username = "demo_technical"
    with browser.new_context(**browser_context_args) as other:
        sign_in(other.new_page(), username, demo.password)
        sign_in(page, username, demo.password)
        page.goto("/account/sessions/")
        expect(page.locator("html")).to_have_attribute("data-bs-theme", view.theme)
        table = page.get_by_role("table", name="Open sessions")
        rows = table.locator("tbody tr")
        expect(rows).to_have_count(2)
        for opener in (
            table.get_by_role("button", name=re.compile("^Sign out ")),
            page.get_by_role("button", name="Sign out of all other sessions"),
        ):
            opener.focus()
            page.keyboard.press("Enter")
            dialog = page.get_by_role("dialog")
            expect(dialog).to_be_visible()
            # Adopted: wait until the dialog has finished opening and holds the focus (Bootstrap
            # traps it once its opening transition ends), as a person's first key press would.
            page.wait_for_function(
                "() => document.querySelector('.modal.show')?.contains(document.activeElement)"
            )
            for key in ("Tab", "Tab", "Tab", "Tab", "Shift+Tab"):
                page.keyboard.press(key)
                assert dialog.evaluate("d => d.contains(document.activeElement)")
            page.keyboard.press("Escape")
            expect(dialog).to_be_hidden()
            expect(opener).to_be_focused()
            expect(rows).to_have_count(2)


def test_reason_error_returns_keyboard_focus_to_the_field(page, demo, view):
    owner = User.objects.get(username="demo_admin")
    owner.is_superuser = owner.is_staff = True
    owner.save(update_fields=["is_superuser", "is_staff"])
    sign_in(page, owner.username, demo.password)
    page.goto(reverse("admin:accounts_user_sessions", args=[owner.pk]))
    reason = page.get_by_role("textbox", name="Reason")
    reason.fill("   ")
    page.get_by_role("button", name="End sessions").press("Enter")
    expect(page.get_by_text("This field is required.")).to_be_visible()
    expect(reason).to_be_focused(timeout=1500)
