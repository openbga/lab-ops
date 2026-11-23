"""The password rule in the browser: Change password states the rule for the account under New
Password, refuses a password that is too short or predictable with the reason beside the field,
and saves a long passphrase, at desktop and phone widths in both themes; Security settings states
the rule."""

import re

import pytest
from helpers import field, make_user, sign_in
from playwright.sync_api import Page, expect

from core.models import SecondFactorRule

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "e2e-quiet-ember-729"
OVERFLOW = "() => document.documentElement.scrollWidth - window.innerWidth"


def test_change_password_states_and_checks_the_rule(page: Page, view, live_server):
    make_user("e2e_changer", PASSWORD, "technical")
    sign_in(page, "e2e_changer", PASSWORD)
    expect(page.get_by_role("main")).to_be_visible()
    page.goto("/accounts/password/change/")
    main = page.get_by_role("main")
    new_password = field(main, "New Password")
    help_id = new_password.evaluate("el => el.getAttribute('aria-describedby')")
    guidance = page.locator(f"#{help_id.split()[0]}")
    expect(guidance).to_contain_text("Your password must contain at least 15 characters.")
    expect(guidance).to_contain_text("unrelated words")

    field(main, "Current Password").fill(PASSWORD)
    new_password.fill("Password1234567!")
    field(main, "New Password (again)").fill("Password1234567!")
    main.get_by_role("button", name="Change Password").click()
    errors = main.locator("#id_password1_error")
    expect(errors).to_contain_text("common password with only digits, symbols")
    expect(field(main, "New Password")).to_have_attribute("aria-invalid", "true")
    assert page.evaluate(OVERFLOW) <= 0

    field(main, "Current Password").fill(PASSWORD)
    field(main, "New Password").fill("short quiet")
    field(main, "New Password (again)").fill("short quiet")
    main.get_by_role("button", name="Change Password").click()
    expect(errors).to_contain_text("It must contain at least 15 characters.")

    passphrase = "lantern by the old mill"
    field(main, "Current Password").fill(PASSWORD)
    field(main, "New Password").fill(passphrase)
    field(main, "New Password (again)").fill(passphrase)
    main.get_by_role("button", name="Change Password").click()
    expect(page.get_by_text("Password successfully changed.")).to_be_visible()
    assert page.evaluate(OVERFLOW) <= 0
    assert page.console_errors == []

    from accounts.models import User

    assert User.objects.get(username="e2e_changer").check_password(passphrase)


def test_security_settings_states_the_password_rule(page: Page, demo):
    # Client portal accounts only: the administrator signing in is not held to set one up.
    SecondFactorRule(pk=1, portal=True, revision=1).save()
    sign_in(page, "demo_admin", demo.password)
    page.goto("/lab/security/")
    row = page.get_by_role("main").get_by_role("row", name=re.compile("^Passwords"))
    expect(row).to_contain_text(
        "At least 15 characters, or 8 where a second sign-in factor is required"
    )
    expect(row).to_contain_text("The rule now covers: client portal accounts.")
    failed = page.get_by_role("main").get_by_role("row", name=re.compile("^Failed sign-ins"))
    expect(failed).to_contain_text("every server process of the deployment counts the same")
    assert page.console_errors == []
