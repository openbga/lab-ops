"""Independent browser checks for the confirmation section and an already-open admin form."""

import re

import pytest
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.urls import reverse
from helpers import make_user, sign_in
from playwright.sync_api import expect

from jobs.actor import system_account

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.confirmation]
PASSWORD = "synthetic laboratory review passphrase"


@pytest.fixture
def owner(page):
    account = make_user("review_owner", PASSWORD, "admin")
    account.is_staff = account.is_superuser = True
    account.save(update_fields=["is_staff", "is_superuser"])
    sign_in(page, account.username, PASSWORD)
    expect(page.get_by_role("heading", level=1, name="Dashboard")).to_be_visible()
    return account


@pytest.fixture
def person():
    return make_user("review_person", PASSWORD, "technical")


def field_widths(control):
    return control.evaluate(
        """el => new Promise(resolve => {
            const observer = new IntersectionObserver(([entry]) => {
                observer.disconnect();
                resolve([entry.boundingClientRect.width, entry.intersectionRect.width]);
            });
            observer.observe(el);
        })"""
    )


def test_confirmation_fields_have_labels_help_and_fit_every_page(page, owner, person, view):
    TOTP.activate(owner, generate_totp_secret())
    authenticator = TOTP.activate(person, generate_totp_secret()).instance
    role = person.groups.get()
    urls = [
        reverse("admin:accounts_user_add"),
        reverse("admin:accounts_user_add") + "?_popup=1",
        reverse("admin:accounts_user_change", args=[person.pk]),
        reverse("admin:auth_user_password_change", args=[person.pk]),
        reverse("admin:accounts_user_compromised", args=[person.pk]),
        reverse("admin:auth_group_add"),
        reverse("admin:auth_group_change", args=[role.pk]),
        reverse("admin:mfa_authenticator_delete", args=[authenticator.pk]),
        reverse("session-limits-edit", args=["staff"]),
        reverse("second-factor-edit"),
    ]

    clipped = []
    for url in urls:
        response = page.goto(url)
        assert response.status == 200, url
        group = page.get_by_role("group", name="Confirm it is you", exact=True)
        expect(group, url).to_be_visible()
        password = group.get_by_label(re.compile("^Your password"))
        code = group.get_by_label(re.compile("^Authenticator or recovery code"))
        expect(password, url).to_have_attribute("autocomplete", "current-password")
        expect(password, url).to_have_attribute("type", "password")
        expect(password, url).to_have_accessible_description(
            re.compile("a code from your authenticator app .*5 minutes")
        )
        expect(code, url).to_have_attribute("autocomplete", "one-time-code")
        for control in [password, code]:
            control.scroll_into_view_if_needed()
            width, visible_width = field_widths(control)
            if width - visible_width > 1:
                clipped.append((url, control.get_attribute("name"), width, visible_width))
        if page.evaluate("document.documentElement.scrollWidth > window.innerWidth"):
            clipped.append((url, "page scrolls horizontally"))
    assert clipped == []


def test_existing_input_fits_the_account_add_popup(page, owner):
    """Compare the same pre-existing field on both revisions at the phone width."""
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(reverse("admin:accounts_user_add") + "?_popup=1")
    password = page.locator("[name=password1]")
    password.scroll_into_view_if_needed()
    width, visible_width = field_widths(password)
    assert width - visible_width <= 1, (width, visible_width)


def test_read_only_account_has_no_confirmation_or_save(page, owner):
    account = system_account()
    page.goto(reverse("admin:accounts_user_change", args=[account.pk]))
    expect(page.get_by_text("No password set.", exact=True)).to_be_visible()
    expect(page.get_by_role("group", name="Confirm it is you")).to_have_count(0)
    expect(page.locator("input[name^='confirm_']")).to_have_count(0)
    expect(page.get_by_role("button", name=re.compile("^Save"))).to_have_count(0)


@pytest.mark.parametrize("action", ["mark", "password", "remove"])
def test_form_open_in_one_tab_can_save_after_confirming_in_another(page, owner, person, action):
    if action == "mark":
        url = reverse("admin:accounts_user_compromised", args=[person.pk])
        button_name = "Mark as compromised"
    elif action == "password":
        url = reverse("admin:auth_user_password_change", args=[person.pk])
        button_name = "Change password"
    else:
        authenticator = TOTP.activate(person, generate_totp_secret()).instance
        url = reverse("admin:mfa_authenticator_delete", args=[authenticator.pk])
        button_name = "Yes, I’m sure"
    page.goto(url)
    if action == "mark":
        page.get_by_label("Reason").fill("Synthetic account recovery exercise.")
    elif action == "password":
        new_password = "another synthetic laboratory passphrase"
        page.locator("[name=password1]").fill(new_password)
        page.locator("[name=password2]").fill(new_password)

    second = page.context.new_page()
    try:
        reauth = reverse("account_reauthenticate")
        second.goto(reauth)
        second.get_by_label("Password").fill(PASSWORD)
        second.get_by_role("button", name="Confirm", exact=True).click()
        expect(second).not_to_have_url(re.compile(re.escape(reauth)))
    finally:
        second.close()

    page.get_by_role("button", name=button_name, exact=True).click()
    expect(page).not_to_have_url(re.compile(re.escape(url) + "$"))
    if action == "mark":
        from accounts.compromised import open_mark

        assert open_mark(person.pk) is not None
    elif action == "password":
        person.refresh_from_db()
        assert person.check_password(new_password)
    else:
        assert not person.authenticator_set.filter(pk=authenticator.pk).exists()


def test_marking_form_focuses_the_incorrect_password_with_the_reason_kept(page, owner, person):
    page.goto(reverse("admin:accounts_user_compromised", args=[person.pk]))
    reason = page.get_by_label("Reason")
    reason.fill("Synthetic account recovery exercise.")
    password = page.get_by_label("Your password")
    password.fill("incorrect synthetic passphrase")
    page.get_by_role("button", name="Mark as compromised").click()
    expect(page.get_by_text("Incorrect password.", exact=True)).to_be_visible()
    expect(reason).to_have_value("Synthetic account recovery exercise.")
    expect(password).to_have_value("")
    expect(password).to_be_focused()
