"""A password marked as compromised, in the browser: a superuser marks it from the account's page in
the admin console, with a reason; the person signs in with the password and an authenticator code,
lands on Choose a new password without the navigation, is refused the marked password and goes on
with a new one, at desktop and phone widths in both themes. A sign-in with the marked password
alone is signed out at once with the notice saying what to do."""

import re

import pytest
from helpers import field, make_user, sign_in
from playwright.sync_api import Page, expect

from accounts.models import AccountSession, CompromisedPassword, User
from core.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "e2e-quiet-ember-729"
OWNER_PASSWORD = "e2e-owner-lantern-418"
NEW_PASSWORD = "copper lantern by the orchard"
OVERFLOW = "() => document.documentElement.scrollWidth - window.innerWidth"


def totp_code(secret):
    # Taken with time left in its period: a code sent just before the period ends is refused
    # after it (the app accepts only the current period).
    from accounts.tests.test_confirm_identity import code_for

    return code_for(secret)


def make_owner():
    owner = User.objects.create_superuser("e2e_owner", "e2e_owner@example.invalid", OWNER_PASSWORD)
    owner.first_name, owner.last_name = "E2E", "Owner"
    owner.save(update_fields=["first_name", "last_name"])
    # A laboratory role too, so the start page after sign-in opens (a superuser has none by that).
    from django.contrib.auth.models import Group

    owner.groups.add(Group.objects.get_or_create(name="admin")[0])
    return owner


def mark_in_admin(page: Page, person):
    page.goto(f"/admin/accounts/user/{person.pk}/change/")
    expect(page.get_by_text("Not marked.")).to_be_visible()
    page.get_by_role("link", name="Mark as compromised").click()
    expect(page).to_have_url(re.compile(rf"/admin/accounts/user/{person.pk}/compromised/$"))
    expect(page.get_by_text("the account has an authenticator app")).to_be_visible()
    assert page.evaluate(OVERFLOW) <= 0
    page.get_by_label("Reason").fill("   ")
    page.get_by_role("button", name="Mark as compromised").click()
    expect(page.get_by_text("This field is required.")).to_be_visible()
    page.get_by_label("Reason").fill("Synthetic test: typed into a page posing as the laboratory's")
    page.get_by_role("button", name="Mark as compromised").click()
    expect(page).to_have_url(re.compile(rf"/admin/accounts/user/{person.pk}/change/$"))
    expect(
        page.get_by_text(re.compile(rf"Marked the password of {person.username} as compromised"))
    ).to_be_visible()
    expect(page.get_by_text(re.compile(r"^Marked on .* by e2e_owner\."))).to_be_visible()


def test_a_marked_password_is_replaced_before_anything_else(page: Page, view, live_server):
    from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret

    person = make_user("e2e_marked", PASSWORD, "technical")
    secret = generate_totp_secret()
    TOTP.activate(person, secret)
    make_owner()

    sign_in(page, "e2e_owner", OWNER_PASSWORD)
    expect(page.get_by_role("heading", level=1)).to_be_visible()
    mark_in_admin(page, person)
    assert page.console_errors == []
    page.context.clear_cookies()

    sign_in(page, "e2e_marked", PASSWORD)
    page.get_by_label("Authenticator or recovery code").fill(totp_code(secret))
    page.get_by_role("button", name=re.compile("^Sign in$", re.IGNORECASE)).click()
    expect(page).to_have_url(re.compile(r"/account/new-password/$"))
    expect(page.get_by_role("heading", level=1, name="Choose a new password")).to_be_visible()
    expect(page.locator("#vr-nav")).to_have_count(0)
    expect(page.locator(".vr-thumbbar")).to_have_count(0)
    assert page.evaluate(OVERFLOW) <= 0
    # Another page sends the person back here.
    page.goto("/requests/")
    expect(page).to_have_url(re.compile(r"/account/new-password/$"))

    main = page.get_by_role("main")
    field(main, "New password").fill(PASSWORD)
    field(main, "New password confirmation").fill(PASSWORD)
    main.get_by_role("button", name="Save new password").click()
    expect(main).to_contain_text("This password was marked as compromised for this account.")
    expect(field(main, "New password")).to_have_attribute("aria-invalid", "true")
    assert page.evaluate(OVERFLOW) <= 0

    field(main, "New password").fill(NEW_PASSWORD)
    field(main, "New password confirmation").fill(NEW_PASSWORD)
    main.get_by_role("button", name="Save new password").click()
    expect(page.get_by_text("Your new password is saved.")).to_be_visible()
    expect(page).not_to_have_url(re.compile(r"/account/new-password/$"))
    page.goto("/requests/")
    expect(page).to_have_url(re.compile(r"/requests/$"))
    assert page.console_errors == []

    person.refresh_from_db()
    assert person.check_password(NEW_PASSWORD)
    assert CompromisedPassword.objects.get(user=person).lifted_at is not None
    event = AuditEvent.objects.get(action="account.password_marked_compromised")
    assert event.actor.username == "e2e_owner" and event.object_id == str(person.pk)


def test_the_marked_password_alone_is_signed_out_with_what_to_do(page: Page, live_server):
    person = make_user("e2e_alone", PASSWORD, "technical")
    make_owner()
    sign_in(page, "e2e_owner", OWNER_PASSWORD)
    expect(page.get_by_role("heading", level=1)).to_be_visible()
    page.goto(f"/admin/accounts/user/{person.pk}/compromised/")
    expect(page.get_by_text("the account has no second factor")).to_be_visible()
    page.get_by_label("Reason").fill("Synthetic test: found in a published list")
    page.get_by_role("button", name="Mark as compromised").click()
    expect(page.get_by_text(re.compile(r"^Marked on "))).to_be_visible()
    page.context.clear_cookies()

    sign_in(page, "e2e_alone", PASSWORD)
    expect(page.get_by_role("heading", level=1, name="Sign in")).to_be_visible()
    expect(page.get_by_text("An administrator marked your password as compromised")).to_be_visible()
    expect(page.get_by_text(re.compile("Successfully signed in"))).to_have_count(0)
    assert AccountSession.objects.get(user=person).ending == "password_compromised"
    assert page.console_errors == []
