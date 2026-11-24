"""Confirm it is you (accounts.confirmation) in the browser: the session limits form in the form
dialog asks for the password before saving and keeps what was typed, at both widths in both
themes; an account's page in the admin console asks too, and not again once confirmed."""

import re

import pytest
from conftest import NARROW
from django.urls import reverse
from helpers import field, sign_in
from playwright.sync_api import Page, expect

from accounts.models import User
from core.models import SessionLimit

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.confirmation]

OVERFLOW = "() => document.documentElement.scrollWidth - window.innerWidth"


def test_session_limits_ask_for_the_password_in_the_dialog(page: Page, demo, view):
    sign_in(page, "demo_admin", demo.password)
    page.goto(reverse("security-settings"))
    page.get_by_role("main").get_by_role("link", name="Staff", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    expect(page.get_by_role("dialog", name="Edit staff session limits")).to_be_visible()
    confirm = dialog.get_by_role("group", name="Confirm it is you")
    expect(confirm).to_be_visible()
    password = field(dialog, "Your password")
    expect(password).to_have_attribute("type", "password")
    expect(password).to_have_attribute("autocomplete", "current-password")
    expect(password).to_have_accessible_description(re.compile("^Saving this change asks for"))

    field(dialog, "Sign out after no activity for").fill("20")
    field(dialog, "Reason").fill("Shared front-desk workstations.")
    dialog.get_by_role("button", name="Save changes", exact=True).click()
    expect(dialog).to_contain_text("Enter your password to save this change.")
    expect(field(dialog, "Sign out after no activity for")).to_have_value("20")
    expect(field(dialog, "Reason")).to_have_value("Shared front-desk workstations.")
    assert not SessionLimit.objects.filter(audience="staff", revision__gt=0).exists()

    field(dialog, "Your password").fill("not the password")
    dialog.get_by_role("button", name="Save changes", exact=True).click()
    expect(dialog).to_contain_text("Incorrect password.")
    assert page.evaluate(OVERFLOW) <= 0

    field(dialog, "Your password").fill(demo.password)
    dialog.get_by_role("button", name="Save changes", exact=True).click()
    expect(page.get_by_text("Staff session limits saved.")).to_be_visible()
    expect(page).to_have_url(re.compile(r"/lab/security/$"))
    assert SessionLimit.objects.get(audience="staff").inactivity_minutes == 20

    # Confirmed: the next change in this browser does not ask again.
    page.get_by_role("main").get_by_role("link", name="Client portal", exact=True).click()
    expect(page.get_by_role("dialog", name="Edit client portal session limits")).to_be_visible()
    expect(dialog.get_by_role("group", name="Confirm it is you")).to_have_count(0)
    assert page.console_errors == []


def test_an_account_page_in_the_admin_console_asks_once(page: Page, demo):
    owner = User.objects.get(username="demo_admin")
    owner.is_staff = owner.is_superuser = True
    owner.save(update_fields=["is_staff", "is_superuser"])
    person = User.objects.get(username="demo_technical")
    sign_in(page, owner.username, demo.password)
    url = reverse("admin:accounts_user_change", args=[person.pk])
    for size in (NARROW, {"width": 1366, "height": 860}):
        page.set_viewport_size(size)
        page.goto(url)
        expect(page.get_by_role("heading", name="Confirm it is you")).to_be_visible()
        expect(page.get_by_label("Your password")).to_be_visible()
        assert page.evaluate(OVERFLOW) <= 0
    page.get_by_label("First name").fill("Morgan")
    page.get_by_role("button", name="Save and continue editing").click()
    expect(page.get_by_text("Enter your password to save this change.")).to_be_visible()
    expect(page.get_by_label("First name")).to_have_value("Morgan")
    page.get_by_label("Your password").fill(demo.password)
    page.get_by_role("button", name="Save and continue editing").click()
    expect(page.get_by_text(re.compile("was changed successfully"))).to_be_visible()
    person.refresh_from_db()
    assert person.first_name == "Morgan"
    expect(page.get_by_role("heading", name="Confirm it is you")).to_have_count(0)
    assert page.console_errors == []
