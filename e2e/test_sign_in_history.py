"""Sign-in history (/account/sign-ins/): a person reads their own sign-ins, sign-outs and failed
attempts, reached from the user menu, at both widths and in both themes."""

import re

import pytest
from helpers import contrast_failures, sign_in, sign_out, theme_samples
from playwright.sync_api import Page, expect

from accounts.models import User

pytestmark = pytest.mark.django_db(transaction=True)

STAFF = "demo_account_manager"
PORTAL = "demo_client"


def open_page(page: Page) -> None:
    """Reach the page the way a person does: the user menu."""
    page.goto("/")
    page.get_by_role("button", name=re.compile(r"^(Demo |E2E |Account)")).click()
    page.get_by_role("link", name="Sign-in history").click()
    expect(page).to_have_url(re.compile(r"/account/sign-ins/$"))
    expect(page.get_by_role("heading", level=1, name="Sign-in history")).to_be_visible()


def rows(page: Page):
    return page.get_by_role("table", name="Recent activity").locator("tbody tr")


def test_a_person_reads_their_own_sign_ins_and_failed_attempts(page: Page, demo, view):
    # A wrong password, a sign-in and a Sign out, then the sign-in that reads the page.
    sign_in(page, STAFF, "not-the-password-typed")
    expect(page.get_by_role("heading", level=1, name="Sign in")).to_be_visible()
    sign_in(page, STAFF, demo.password)
    sign_out(page, who=r"^(Demo |E2E |Account)")
    sign_in(page, STAFF, demo.password)
    open_page(page)

    expect(rows(page)).to_have_count(4)
    if not view.phone:  # a phone shows each row stacked, its headers as labels in the cells
        for header in ("When", "Event", "Details", "Browser", "Address"):
            expect(page.get_by_role("columnheader", name=header)).to_be_visible()
    first, signed_out, earlier, failed = (rows(page).nth(index) for index in range(4))
    expect(first).to_contain_text("Signed in")
    expect(first).to_contain_text("This browser")
    expect(first).to_contain_text("Password")
    expect(signed_out).to_contain_text("Signed out")
    expect(signed_out).to_contain_text("With Sign out")
    expect(earlier).not_to_contain_text("This browser")
    expect(failed).to_contain_text("Sign-in failed")
    expect(failed).to_contain_text("Password not accepted")
    expect(page.get_by_text("not-the-password-typed")).to_have_count(0)
    expect(page.get_by_text("Check the attempts that failed")).to_be_visible()

    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
        "the page scrolls sideways"
    )
    assert not contrast_failures(theme_samples(page))


def test_a_portal_account_reads_its_history_on_a_phone(page: Page, demo):
    page.set_viewport_size({"width": 390, "height": 844})
    sign_in(page, PORTAL, demo.password)
    open_page(page)
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text("This browser")
    expect(page.get_by_text("Check the attempts that failed")).to_have_count(0)
    # The way to Where you are signed in, to sign out a session it lists.
    page.get_by_role("link", name="Where you are signed in").last.click()
    expect(page.get_by_role("heading", level=1, name="Where you are signed in")).to_be_visible()


def test_a_shared_demonstration_account_does_not_show_other_visitors(
    page: Page, demo, browser, browser_context_args
):
    User.objects.filter(username=STAFF).update(credentials_locked=True)
    visitor = browser.new_context(**browser_context_args)
    try:
        other = visitor.new_page()
        sign_in(other, STAFF, demo.password)
        expect(other.get_by_role("heading", level=1, name="Sign in")).to_have_count(0)
    finally:
        visitor.close()
    sign_in(page, STAFF, demo.password)
    open_page(page)
    expect(page.get_by_text("Shared demonstration account", exact=True).first).to_be_visible()
    expect(rows(page)).to_have_count(2)
    expect(rows(page).first).to_contain_text("This browser")
    expect(rows(page).nth(1)).to_contain_text("Not shown")
