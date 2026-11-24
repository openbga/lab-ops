"""Verification of the confirmation widgets in real admin forms.

Adopted from the review (round 1 verification). Changed on adoption: a refused code also marks
the password, which the page does not keep, as to be entered again, so focus goes to the first
refused field, Your password (docs/ui-standards.md, first invalid field)."""

import re

import pytest
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.urls import reverse
from helpers import make_user, sign_in
from playwright.sync_api import expect

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.confirmation]

PASSPHRASE = "synthetic birch orchard shift 847"


@pytest.fixture
def editor(page):
    user = make_user("verify_owner", PASSPHRASE, "admin")
    user.is_staff = user.is_superuser = True
    user.save(update_fields=["is_staff", "is_superuser"])
    sign_in(page, user.username, PASSPHRASE)
    expect(page.get_by_role("heading", name="Dashboard", level=1)).to_be_visible()
    return user


def test_code_error_receives_keyboard_focus(page, editor):
    TOTP.activate(editor, generate_totp_secret())
    subject = make_user("verify_mark_target", PASSPHRASE)
    page.goto(reverse("admin:accounts_user_compromised", args=[subject.pk]))
    reason = page.get_by_label("Reason")
    password = page.get_by_label("Your password")
    code = page.get_by_label("Authenticator or recovery code")
    reason.fill("Synthetic recovery exercise.")
    password.fill(PASSPHRASE)
    code.fill("invalid synthetic code")
    page.get_by_role("button", name="Mark as compromised").click()
    expect(page.get_by_text("Incorrect code.", exact=True)).to_be_visible()
    expect(page.get_by_text("Enter your password again with the code.")).to_be_visible()
    expect(reason).to_have_value("Synthetic recovery exercise.")
    expect(password).to_have_value("")
    assert page.evaluate("document.activeElement.name") == "confirm_password"
    expect(code).to_have_value("")


def test_role_and_account_inputs_fit_in_popups_and_regular_pages(page, editor, view):
    TOTP.activate(editor, generate_totp_secret())
    pages = [
        (reverse("admin:auth_group_add"), "name"),
        (reverse("admin:accounts_user_change", args=[editor.pk]), "username"),
    ]
    for url, field_name in pages:
        for suffix in ("", "?_popup=1"):
            target = url + suffix
            assert page.goto(target).status == 200
            for name in (field_name, "confirm_password", "confirm_code"):
                control = page.locator(f'input[name="{name}"]')
                control.scroll_into_view_if_needed()
                width, visible = control.evaluate("""el => new Promise(resolve => {
                    const observer = new IntersectionObserver(([entry]) => {
                        observer.disconnect();
                        resolve([entry.boundingClientRect.width, entry.intersectionRect.width]);
                    });
                    observer.observe(el);
                })""")
                assert width > 0 and visible + 1 >= width, (target, name, width, visible)
            group = page.get_by_role("group", name="Confirm it is you", exact=True)
            password = group.get_by_label(re.compile("^Your password"))
            code = group.get_by_label(re.compile("^Authenticator or recovery code"))
            assert password.evaluate("el => !el.required") and code.evaluate("el => !el.required")
