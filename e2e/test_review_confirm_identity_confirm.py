"""A refused combined confirmation in the Security settings dialog."""

import pytest
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.urls import reverse
from helpers import field, sign_in
from playwright.sync_api import expect

from accounts.models import User
from core.models import SessionLimit

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.confirmation]


def test_refused_code_in_dialog_refocuses_the_empty_password(page, demo, view):
    user = User.objects.get(username="demo_admin")
    sign_in(page, user.username, demo.password)
    page.goto(reverse("security-settings"))
    TOTP.activate(user, generate_totp_secret())
    page.get_by_role("main").get_by_role("link", name="Staff", exact=True).click()
    dialog = page.get_by_role("dialog", name="Edit staff session limits")
    reason = field(dialog, "Reason")
    password = field(dialog, "Your password")
    code = field(dialog, "Authenticator or recovery code")
    reason.fill("Synthetic shift change")
    password.fill(demo.password)
    code.fill("incorrect synthetic code")
    dialog.get_by_role("button", name="Save changes").click()
    expect(dialog.get_by_text("Incorrect code.", exact=True)).to_be_visible()
    expect(dialog.get_by_text("Enter your password again with the code.")).to_be_visible()
    expect(reason).to_have_value("Synthetic shift change")
    expect(password).to_have_value("")
    expect(code).to_have_value("")
    expect(password).to_be_focused()
    assert not SessionLimit.objects.filter(audience="staff", revision__gt=0).exists()
