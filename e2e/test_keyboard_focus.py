"""Shared keyboard focus, explicit filter submission and text-resize regressions."""

import re

import pytest
from helpers import field, make_clients, pick, sign_in
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def assert_focus_ring(control):
    control.focus()
    control.page.keyboard.press("Tab")
    control.page.keyboard.press("Shift+Tab")
    expect(control).to_be_focused()
    assert control.evaluate(
        "el => { const s = getComputedStyle(el); return s.outlineStyle !== 'none' && "
        "parseFloat(s.outlineWidth) >= 2; }"
    )


def test_edit_fields_clear_sticky_actions(page: Page, sign_in_as):
    from clients.models import Client

    sign_in_as("account_manager")
    page.set_viewport_size({"width": 390, "height": 600})
    client = Client.objects.get(name="Example Family Clinic")
    page.goto(f"/clients/{client.pk}/edit/")
    reason = field(page, "Reason for change")
    reason.focus()
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    expect(reason).to_be_focused()
    assert reason.evaluate(
        """el => { const r = el.getBoundingClientRect();
        const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
        return el === hit || el.contains(hit); }"""
    ), "Focused field is covered by the sticky actions"
    # A refused save moves focus to the message, which is not hidden behind anything either.
    page.get_by_role("button", name="Save changes", exact=True).click()
    expect(reason).to_have_attribute("aria-invalid", "true")
    expect(reason).to_be_focused()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_portal_navigation_has_visible_focus(page: Page, sign_in_as, theme):
    sign_in_as("client")
    page.goto("/portal/")
    page.evaluate("theme => document.documentElement.dataset.bsTheme = theme", theme)
    assert_focus_ring(page.get_by_role("navigation", name="Portal").get_by_role("link").first)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_pager_has_visible_focus(page: Page, sign_in_as, theme):
    sign_in_as("account_manager")
    assert make_clients(30) > 25
    page.goto("/clients/?q=Synthetic+Organization")
    page.evaluate("theme => document.documentElement.dataset.bsTheme = theme", theme)
    assert_focus_ring(page.get_by_role("link", name="Next page", exact=True))


def test_theme_choice_returns_keyboard_focus(page: Page, sign_in_as):
    sign_in_as("technical")
    toggle = page.get_by_role("button", name="Theme", exact=True)
    toggle.focus()
    toggle.press("Enter")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(page.locator(".vr-theme-option:visible")).to_have_count(0)
    expect(toggle).to_be_focused()


@pytest.mark.parametrize(
    "label,value,submit,parameter,submitted",
    [
        ("Rows per page", "50", "Show", "per_page", "50"),
        ("State", "Texas", "Apply", "state", "TX"),
    ],
)
def test_selects_wait_for_explicit_submission(
    page: Page, sign_in_as, label, value, submit, parameter, submitted
):
    sign_in_as("technical")
    page.goto("/clients/")
    if label == "State":
        page.get_by_role("button", name="More filters").click()
    control = page.get_by_role("combobox", name=label, exact=True)
    control.focus()
    before = page.url
    if label == "Rows per page":
        pick(control, value)
    else:
        pick(control, label=value)
    # A frame boundary allows change handlers and navigation to settle.
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    assert page.url == before
    expect(control).to_be_focused()
    button = control.locator("xpath=ancestor::form").get_by_role("button", name=submit, exact=True)
    expect(button).to_be_visible()
    button.click()
    expect(page).to_have_url(re.compile(rf"[?&]{parameter}={submitted}(?:&|$)"))
    if label == "Rows per page":
        page.locator(".vr-topbar [data-vr-documentation-toggle]").click()
        expect(page.locator("#vr-documentation")).to_contain_text(
            "Choose Rows per page, then select Show"
        )


@pytest.mark.parametrize("role", ["admin", "supervisor", "account_manager", "technical"])
def test_staff_shell_fits_enlarged_text(page: Page, sign_in_as, role):
    sign_in_as(role)
    page.goto("/clients/")
    page.evaluate("document.documentElement.style.fontSize = '175%'")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
        "Page overflows at 200% text"
    )
    assert page.locator(".vr-topbar-end").evaluate(
        "el => el.getBoundingClientRect().right <= innerWidth"
    )
    assert page.locator(".vr-sidenav .vr-navlink").evaluate_all(
        "els => els.every(el => el.scrollWidth <= el.clientWidth)"
    ), "Sidebar labels clip at 200% text"


@pytest.mark.parametrize("width,height", [(1366, 860), (820, 1180), (390, 844)])
def test_direct_form_dialog_has_page_semantics_and_clean_edges(
    page: Page, sign_in_as, width, height
):
    sign_in_as("account_manager")
    page.set_viewport_size({"width": width, "height": height})
    page.goto("/clients/new/")
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("heading", level=1, name="New client organization")).to_be_visible()
    expect(page.get_by_role("main")).to_have_count(1)
    expect(dialog.get_by_role("main")).to_be_visible()
    assert dialog.get_by_role("main").evaluate(
        "el => { const s = getComputedStyle(el); "
        "return s.backgroundImage === 'none' && s.paddingLeft === '0px'; }"
    )
    expect(dialog.locator('select[name="state"]')).to_have_attribute("aria-hidden", "true")
    expect(dialog.get_by_role("listbox")).to_have_count(0)
    expect(dialog.get_by_role("combobox", name="State")).to_have_count(1)
    body = dialog.locator("#vr-form-content")
    body.evaluate("el => el.scrollTop = el.scrollHeight / 2")
    actions = dialog.locator(".vr-form-actions")
    assert actions.evaluate(
        "el => Math.abs(el.getBoundingClientRect().bottom - "
        "el.closest('.modal-body').getBoundingClientRect().bottom) <= 1"
    ), "The dialog action row leaves a strip of content underneath"
    assert page.console_errors == []


def test_failed_sign_in_focuses_non_field_error(page: Page, demo):
    page.goto("/accounts/login/")
    page.get_by_label(re.compile("^(Username or email|Login)")).fill("demo_account_manager")
    field(page, "Password").fill("synthetic-wrong-password")
    page.get_by_role("button", name="Sign in", exact=True).click()
    error = page.locator('.vr-entrance-panel .alert-danger[role="alert"]')
    expect(error).to_be_visible()
    expect(error).to_be_focused()


def test_phone_portal_destinations_are_all_visible_without_margin(page: Page, sign_in_as):
    sign_in_as("client")
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto("/portal/")
    assert page.locator("main").evaluate("el => getComputedStyle(el).backgroundImage === 'none'")
    assert (
        page.get_by_role("navigation", name="Portal")
        .get_by_role("link")
        .evaluate_all(
            "els => els.every(el => { const r = el.getBoundingClientRect(); "
            "return r.x >= 0 && r.right <= innerWidth; })"
        )
    )
    expect(page.get_by_role("group", name="Theme")).to_have_count(0)  # Closed dropdown.
    page.get_by_role("button", name="Theme", exact=True).click()
    expect(page.get_by_role("group", name="Theme")).to_be_visible()


def test_page_titles_use_product_name(page: Page, sign_in_as, demo):
    from clients.models import Client

    sign_in_as("client")
    expect(page).to_have_title("Example Family Clinic · OpenBGA Lab Ops")
    page.context.clear_cookies()
    sign_in(page, "demo_technical", demo.password)
    clinic = Client.objects.get(name="Example Family Clinic")
    page.goto(f"/clients/{clinic.pk}/")
    expect(page).to_have_title("Example Family Clinic · Client organizations · OpenBGA Lab Ops")
