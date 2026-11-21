"""Where you are signed in (/account/sessions/) and ending an account's sessions in the account
administration. Each extra browser session of one account is its own browser context."""

import re

import pytest
from django.urls import reverse
from helpers import sign_in
from playwright.sync_api import Page, expect

from accounts.models import User

pytestmark = pytest.mark.django_db(transaction=True)

STAFF = "demo_account_manager"
PORTAL = "demo_client"
ELSEWHERE = "You were signed out from another session of your account. Sign in again to continue."
BY_ADMIN = "An administrator ended your session. Sign in again to continue."
PHONE = {"width": 390, "height": 844}


@pytest.fixture
def contexts(browser, browser_context_args):
    """Opens more browser sessions (contexts) and closes them when the journey ends."""
    opened = []

    def open_one(demo, username, **overrides):
        context = browser.new_context(**{**browser_context_args, **overrides})
        context.set_default_timeout(10_000)
        context.set_default_navigation_timeout(15_000)
        opened.append(context)
        page = context.new_page()
        sign_in(page, username, demo.password)
        expect(page.get_by_role("heading", level=1, name="Sign in")).to_have_count(0)
        return page

    yield open_one
    for context in opened:
        context.close()


def open_page(page: Page) -> None:
    """Reach the page the way a person does: the user menu."""
    page.goto("/")
    page.get_by_role("button", name=re.compile(r"^(Demo |E2E |Account)")).click()
    page.get_by_role("link", name="Where you are signed in").click()
    expect(page).to_have_url(re.compile(r"/account/sessions/$"))
    expect(page.get_by_role("heading", level=1, name="Where you are signed in")).to_be_visible()


def rows(page: Page):
    return page.get_by_role("table", name="Open sessions").locator("tbody tr")


def row_sign_out_buttons(page: Page):
    return rows(page).get_by_role("button", name=re.compile(r"^Sign out "))


def expect_signed_out(page: Page, notice: str) -> None:
    page.goto("/clients/")
    expect(page).to_have_url(re.compile(r"/accounts/login/"))
    expect(page.get_by_text(notice)).to_be_visible()


def test_sign_out_one_other_session(page: Page, demo, contexts):
    sign_in(page, STAFF, demo.password)
    other = contexts(demo, STAFF)
    open_page(page)

    expect(rows(page)).to_have_count(2)
    expect(rows(page).filter(has_text="This browser")).to_have_count(1)
    for header in ("Browser", "Address at sign-in", "Signed in", "Last activity"):
        expect(page.get_by_role("columnheader", name=header)).to_be_visible()
    expect(row_sign_out_buttons(page)).to_have_count(1)

    row_sign_out_buttons(page).click()
    dialog = page.get_by_role("dialog", name="Sign out this session?")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name=re.compile(r"^Sign out \S")).click()

    expect(
        page.get_by_text(
            "That session was signed out. Its browser goes to the sign-in page at its next request."
        )
    ).to_be_visible()
    expect(rows(page)).to_have_count(1)
    expect(rows(page)).to_contain_text("This browser")
    expect(row_sign_out_buttons(page)).to_have_count(0)
    expect(page.get_by_role("button", name="Sign out of all other sessions")).to_have_count(0)

    expect_signed_out(other, ELSEWHERE)
    # This browser is unaffected.
    page.goto("/clients/")
    expect(page).to_have_url(re.compile(r"/clients/$"))
    assert not page.console_errors, page.console_errors


def test_cancel_keeps_the_other_session(page: Page, demo, contexts):
    sign_in(page, STAFF, demo.password)
    other = contexts(demo, STAFF)
    open_page(page)
    row_sign_out_buttons(page).click()
    dialog = page.get_by_role("dialog", name="Sign out this session?")
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden()
    expect(rows(page)).to_have_count(2)
    other.goto("/clients/")
    expect(other).to_have_url(re.compile(r"/clients/$"))


def test_sign_out_of_all_other_sessions_names_one_signed_in_after_the_page(
    page: Page, demo, contexts
):
    sign_in(page, STAFF, demo.password)
    first = contexts(demo, STAFF)
    open_page(page)
    expect(rows(page)).to_have_count(2)
    # A third browser signs in after the page above was drawn.
    third = contexts(demo, STAFF)

    page.get_by_role("button", name="Sign out of all other sessions").click()
    dialog = page.get_by_role("dialog", name="Sign out of all other sessions")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="Sign out of all other sessions").click()

    expect(
        page.get_by_text(
            "Signed out of 2 other sessions. 1 of them was signed in after you opened this page."
        )
    ).to_be_visible()
    expect(rows(page)).to_have_count(1)
    expect(rows(page)).to_contain_text("This browser")
    expect_signed_out(first, ELSEWHERE)
    expect_signed_out(third, ELSEWHERE)


def test_portal_account_on_a_phone_signs_out_one_handed(page: Page, demo, contexts):
    page.set_viewport_size(PHONE)
    sign_in(page, PORTAL, demo.password)
    other = contexts(demo, PORTAL, viewport=PHONE)
    open_page(page)

    expect(rows(page)).to_have_count(2)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
        "the page scrolls sideways"
    )
    button = row_sign_out_buttons(page)
    expect(button).to_be_visible()
    box = button.bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= PHONE["width"]
    button.click()
    dialog = page.get_by_role("dialog", name="Sign out this session?")
    expect(dialog).to_be_visible()
    submit = dialog.get_by_role("button", name=re.compile(r"^Sign out \S"))
    expect(submit).to_be_in_viewport()
    submit.click()
    expect(page.get_by_text("That session was signed out.")).to_be_visible()
    expect(rows(page)).to_have_count(1)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    expect_signed_out(other, ELSEWHERE)


def test_shared_demonstration_account_sees_only_its_own_row(page: Page, demo, contexts):
    User.objects.filter(username=STAFF).update(credentials_locked=True)
    sign_in(page, STAFF, demo.password)
    contexts(demo, STAFF)
    open_page(page)

    expect(page.get_by_text("Shared demonstration account", exact=True).first).to_be_visible()
    expect(rows(page)).to_have_count(1)
    expect(rows(page)).to_contain_text("This browser")
    expect(page.get_by_role("button", name=re.compile(r"^Sign out"))).to_have_count(0)
    expect(page.get_by_text("1 other session is open now.")).to_be_visible()


@pytest.mark.parametrize("size", [{"width": 1366, "height": 860}, PHONE], ids=["desktop", "phone"])
def test_dark_theme(page: Page, demo, contexts, size):
    from helpers import contrast_failures, theme_samples

    page.set_viewport_size(size)
    page.emulate_media(color_scheme="dark")
    sign_in(page, STAFF, demo.password)
    contexts(demo, STAFF)
    open_page(page)

    expect(page.locator("html")).to_have_attribute("data-bs-theme", "dark")
    table = page.get_by_role("table", name="Open sessions")
    expect(table).to_be_visible()
    expect(rows(page)).to_have_count(2)
    assert contrast_failures(theme_samples(page)) == []
    # The table's own text is readable against its background.
    ratio = page.evaluate(
        """() => {
            const cell = document.querySelector('table.vr-table tbody th');
            const rgb = s => s.match(/[\\d.]+/g).slice(0, 3).map(Number);
            let bg = cell, back = [0, 0, 0, 0];
            for (; bg; bg = bg.parentElement) {
                const c = getComputedStyle(bg).backgroundColor.match(/[\\d.]+/g).map(Number);
                if (c.length < 4 || c[3] > 0.9) { back = c; break; }
            }
            return {fg: rgb(getComputedStyle(cell).color), bg: back.slice(0, 3)};
        }"""
    )
    from helpers import contrast_ratio

    assert contrast_ratio(ratio["fg"], ratio["bg"]) >= 4.5, ratio
    row_sign_out_buttons(page).click()
    dialog = page.get_by_role("dialog", name="Sign out this session?")
    expect(dialog).to_be_visible()
    expect(page.locator("html")).to_have_attribute("data-bs-theme", "dark")
    expect(dialog.get_by_role("button", name=re.compile(r"^Sign out \S"))).to_be_visible()
    expect(dialog.get_by_text("That browser goes to the sign-in page")).to_be_visible()
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden()


def test_administrator_ends_an_account_sessions(page: Page, demo, contexts):
    owner = User.objects.get(username="demo_admin")
    owner.is_staff = owner.is_superuser = True
    owner.save(update_fields=["is_staff", "is_superuser"])
    target = User.objects.get(username=STAFF)
    target_page = contexts(demo, STAFF)

    sign_in(page, owner.username, demo.password)
    change = reverse("admin:accounts_user_change", args=[target.pk])
    page.goto(change)
    expect(page.get_by_text("1 open session", exact=False).first).to_be_visible()
    page.get_by_role("link", name="End sessions").click()
    expect(page).to_have_url(re.compile(r"/admin/accounts/user/\d+/sessions/$"))

    # No reason: the browser holds the form back (the field is required) ...
    reason = page.get_by_label("Reason")
    page.get_by_role("button", name="End sessions").click()
    expect(page).to_have_url(re.compile(r"/sessions/$"))
    assert reason.evaluate("el => el.required && !el.validity.valid")
    # ... and a reason of only spaces reaches the server, which refuses it with the form error.
    reason.fill("   ")
    page.get_by_role("button", name="End sessions").click()
    expect(page.get_by_text("Please correct the error below.")).to_be_visible()
    expect(page.get_by_text("This field is required.")).to_be_visible()
    target_page.goto("/clients/")
    expect(target_page).to_have_url(re.compile(r"/clients/$"))

    page.get_by_label("Reason").fill("Synthetic test: lost laptop")
    page.get_by_role("button", name="End sessions").click()
    expect(page).to_have_url(re.compile(re.escape(change) + "$"))
    expect(page.get_by_text(re.compile(rf"Ended 1 session of {STAFF}\."))).to_be_visible()
    expect(page.get_by_text("0 open sessions")).to_be_visible()
    expect_signed_out(target_page, BY_ADMIN)
