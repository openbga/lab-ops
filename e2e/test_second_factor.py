"""A second sign-in factor the laboratory requires, in the browser: the administrator sets the
rule in the form dialog on Security settings; a person the rule covers signs in, lands on Set up a
second sign-in factor without the navigation, adds an authenticator app and goes on to the page
they were opening, at desktop and phone widths in both themes."""

import re

import pytest
from conftest import DESKTOP, NARROW
from helpers import field, sign_in, sign_out
from playwright.sync_api import Page, expect

from accounts.models import User
from core.models import AuditEvent, SecondFactorRule

pytestmark = pytest.mark.django_db(transaction=True)

OVERFLOW = "() => document.documentElement.scrollWidth - window.innerWidth"


def totp_code(secret):
    from allauth.mfa.totp.internal.auth import (
        format_hotp_value,
        hotp_value,
        yield_hotp_counters_from_time,
    )

    return format_hotp_value(hotp_value(secret, next(yield_hotp_counters_from_time())))


def test_admin_requires_a_second_factor_of_a_role(page: Page, demo):
    sign_in(page, "demo_admin", demo.password)
    page.goto("/lab/security/")
    main = page.get_by_role("main")
    panel = main.get_by_role("region", name="Second sign-in factor")
    expect(panel.get_by_role("row", name=re.compile("^Staff with these roles"))).to_contain_text(
        "Not required"
    )
    panel.get_by_role("link", name="Edit second sign-in factor rule").click()
    dialog = page.get_by_role("dialog", name="Edit second sign-in factor rule")
    expect(dialog).to_be_visible()
    dialog.get_by_label("Client services", exact=True).check()
    dialog.get_by_role("button", name="Save changes", exact=True).click()
    expect(dialog).to_contain_text("This field is required.")  # the reason
    expect(dialog.get_by_label("Client services", exact=True)).to_be_checked()
    field(dialog, "Reason").fill("Client services staff handle clinic accounts.")
    dialog.get_by_role("button", name="Save changes", exact=True).click()
    expect(page.get_by_text("Second sign-in factor rule saved.")).to_be_visible()
    expect(page).to_have_url(re.compile(r"/lab/security/$"))
    row = panel.get_by_role("row", name=re.compile("^Staff with these roles"))
    expect(row).to_contain_text("Required: Client services")
    expect(panel).to_contain_text("Active accounts required to have one: 2; without one yet: 2.")
    expect(main).to_contain_text("Client services staff handle clinic accounts.")
    stored = SecondFactorRule.objects.get(pk=1)
    assert stored.roles == ["client_services"] and stored.revision == 1
    event = AuditEvent.objects.get(action="lab.second_factor_rule_changed")
    assert event.actor.username == "demo_admin"
    assert page.console_errors == []

    page.set_viewport_size(NARROW)
    for theme in ("light", "dark"):
        page.emulate_media(color_scheme=theme)
        page.goto("/lab/security/")
        expect(panel).to_be_visible()
        assert page.evaluate(OVERFLOW) <= 0, theme
    page.set_viewport_size(DESKTOP)
    page.emulate_media(color_scheme="light")


def test_a_required_person_sets_up_an_app_and_goes_on(page: Page, demo, view):
    SecondFactorRule.objects.create(pk=1, roles=["client_services"], revision=1)
    page.goto("/requests/?view=all")
    sign_in(page, "demo_client_services", demo.password, visit=False)

    # Only the setup page, without the menu, Find or What's new.
    expect(page).to_have_url(re.compile(r"/account/second-factor/$"))
    expect(page.get_by_role("heading", level=1)).to_have_text("Set up a second sign-in factor")
    main = page.get_by_role("main")
    expect(main).to_contain_text(
        "The laboratory requires a second factor of the Client services role"
    )
    expect(page.get_by_role("navigation", name="Primary")).to_have_count(0)
    expect(page.get_by_role("navigation", name="Quick actions")).to_have_count(0)
    expect(page.get_by_role("button", name="Menu")).to_have_count(0)
    assert page.evaluate(OVERFLOW) <= 0
    page.goto("/requests/")
    expect(page).to_have_url(re.compile(r"/account/second-factor/$"))

    # The setup: the password again, then the app's code.
    page.get_by_role("link", name="Set up authenticator app").click()
    expect(page).to_have_url(re.compile(r"/accounts/reauthenticate/"))
    page.get_by_label("Password").fill(demo.password)
    page.get_by_role("button", name="Confirm").click()
    expect(page).to_have_url(re.compile(r"/accounts/2fa/totp/activate/$"))
    secret = page.get_by_label("Authenticator secret").input_value()
    page.get_by_label("Authenticator code").fill(totp_code(secret))
    page.get_by_role("button", name="Activate").click()
    expect(page).to_have_url(re.compile(r"/accounts/2fa/recovery-codes/$"))
    assert page.evaluate(OVERFLOW) <= 0

    # Continue goes on to the page the sign-in was opening, with the navigation back.
    page.get_by_role("link", name="Continue", exact=True).click()
    expect(page).to_have_url(re.compile(r"/requests/\?view=all$"))
    if view.phone:
        expect(page.get_by_role("navigation", name="Quick actions")).to_be_visible()
    else:
        expect(page.get_by_role("navigation", name="Primary")).to_be_visible()
    user = User.objects.get(username="demo_client_services")
    assert user.authenticator_set.filter(type="totp").exists()
    assert page.console_errors == []


def test_a_person_the_rule_does_not_cover_is_not_held(page: Page, demo):
    SecondFactorRule.objects.create(pk=1, roles=["client_services"], revision=1)
    sign_in(page, "demo_technical", demo.password)
    expect(page).not_to_have_url(re.compile(r"/account/second-factor/"))
    expect(page.get_by_role("navigation", name="Primary")).to_be_visible()
    sign_out(page)
