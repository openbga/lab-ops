"""Independent Chromium checks for the two password-marking forms."""

import re

import pytest
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.urls import reverse
from helpers import described_text, field, make_user, sign_in
from playwright.sync_api import expect
from test_second_factor import totp_code

from accounts.models import User

pytestmark = pytest.mark.django_db(transaction=True)
PHRASE = "Several pale asters in autumn"
REPLACEMENT = "Violet lilies beside a window"


@pytest.fixture
def owner():
    return User.objects.create_superuser("review-owner", "owner@example.invalid", PHRASE)


@pytest.fixture
def person():
    return make_user("review-operator", PHRASE, "technical")


def check_phone_controls(view, *controls):
    if view.phone:
        for control in controls:
            box = control.bounding_box()
            assert min(box["width"], box["height"]) >= 44, (control, box)


def test_mark_reason_and_submit_are_usable_after_validation(page, view, owner, person):
    page.goto(reverse("admin:accounts_user_compromised", args=[person.pk]))
    sign_in(page, owner.username, PHRASE, visit=False)
    reason = page.get_by_label("Reason")
    submit = page.get_by_role("button", name="Mark as compromised")
    expect(reason).to_be_focused()
    reason.fill("   ")
    reason.press("Tab")
    expect(submit).to_be_focused()
    submit.press("Enter")
    expect(reason).to_have_attribute("aria-invalid", "true")
    expect(reason).to_be_focused()
    assert "This field is required." in described_text(page, reason.get_attribute("id"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    check_phone_controls(view, reason, submit)


@pytest.fixture
def choose_page(page, owner, person):
    from accounts.compromised import drawn_password, mark_compromised

    secret = generate_totp_secret()
    TOTP.activate(person, secret)
    mark_compromised(
        actor=owner, account_id=person.pk, reason="Practice mark", drawn=drawn_password(person)
    )
    sign_in(page, person.username, PHRASE)
    page.get_by_label("Authenticator or recovery code").fill(totp_code(secret))
    page.get_by_role("button", name=re.compile("^Sign in$", re.I)).click()
    expect(page).to_have_url(re.compile(r"/account/new-password/$"))
    return page.get_by_role("main")


def test_confirmation_error_is_linked_and_focused(page, view, choose_page):
    main = choose_page
    password = field(main, "New password")
    confirm = field(main, "New password confirmation")
    submit = main.get_by_role("button", name="Save new password")
    expect(password).to_be_focused()
    expect(password).to_have_accessible_description(re.compile("at least 15 characters"))
    expect(password).to_have_accessible_description(re.compile("marked as compromised"))
    password.fill(REPLACEMENT)
    password.press("Tab")
    expect(confirm).to_be_focused()
    confirm.fill(REPLACEMENT + "s")
    confirm.press("Enter")
    expect(confirm).to_have_attribute("aria-invalid", "true")
    expect(confirm).to_be_focused()
    assert "match" in described_text(page, confirm.get_attribute("id"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    check_phone_controls(view, password, confirm, submit)


@pytest.mark.parametrize("route", ["account-new-password", "account_change_password"])
def test_password_error_summary_agrees_with_the_fields(page, person, route, request):
    """Compare the new page with the existing account form that shares its error summary."""
    if route == "account-new-password":
        main = request.getfixturevalue("choose_page")
    else:
        sign_in(page, person.username, PHRASE)
        page.goto(reverse(route))
        main = page.get_by_role("main")
        main.locator('input[autocomplete="current-password"]').fill(PHRASE)
    new_fields = main.locator('input[autocomplete="new-password"]')
    expect(new_fields).to_have_count(2)
    new_fields.nth(0).fill(REPLACEMENT)
    new_fields.nth(1).fill(REPLACEMENT + "s")
    new_fields.nth(1).press("Enter")
    summary = main.get_by_role("alert").filter(has_text="highlighted field")
    expect(summary).to_be_visible()
    for control in new_fields.all():
        expect(control).to_have_value("")
    assert "Your entries were kept." not in summary.inner_text()
