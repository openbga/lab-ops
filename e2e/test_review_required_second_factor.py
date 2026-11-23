"""Second-factor settings and enrolment as presented to administrators and portal users.

Written by the round-1 independent reviewer (Codex) at 6215b89 and adopted unchanged; the original
is archived in .local/review-history/required-second-factor-round-1-original/."""

import re

import pytest
from helpers import contrast_failures, field, sign_in, theme_samples
from playwright.sync_api import expect

from accounts.models import User
from core.models import SecondFactorRule

pytestmark = pytest.mark.django_db(transaction=True)


def check_layout(page, view):
    expect(page.locator("html")).to_have_attribute("data-bs-theme", view.theme)
    assert page.evaluate("document.documentElement.scrollWidth") <= view.viewport["width"]
    assert contrast_failures(theme_samples(page)) == []


def test_rule_panel_and_form_show_the_saved_choices(page, demo, view):
    sign_in(page, "demo_admin", demo.password)
    page.goto("/lab/security/")
    panel = page.get_by_role("region", name="Second sign-in factor", exact=True)
    expect(panel.get_by_text("Not required", exact=True)).to_have_count(5)
    edit = panel.get_by_role("link", name="Edit second sign-in factor rule")
    edit.scroll_into_view_if_needed()
    expect(edit).to_be_in_viewport(ratio=1)
    check_layout(page, view)

    edit.click()
    dialog = page.get_by_role("dialog", name="Edit second sign-in factor rule")
    portal = dialog.get_by_role("checkbox", name="Client portal accounts")
    portal.check()
    field(dialog, "Reason").fill("Cover our synthetic client accounts.")
    save = dialog.get_by_role("button", name="Save changes")
    expect(save).to_be_in_viewport(ratio=1)
    check_layout(page, view)
    save.click()

    expect(dialog).to_be_hidden()
    portal_row = panel.locator("tr").filter(has_text="Client portal accounts")
    expect(portal_row.get_by_text("Required", exact=True)).to_be_visible()
    expect(panel.get_by_text("Not required", exact=True)).to_have_count(4)
    expect(panel).to_contain_text("Active accounts required to have one:")
    edit.click()
    expect(portal).to_be_checked()
    first = dialog.get_by_role("checkbox", name="Every staff account")
    expect(first).not_to_be_checked()
    expect(first).to_be_focused()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(edit).to_be_focused()


def test_portal_enrolment_names_the_reason_and_omits_work_navigation(page, demo, view):
    SecondFactorRule.objects.create(pk=1, portal=True, revision=1)
    sign_in(page, "demo_client", demo.password)
    expect(page).to_have_url(re.compile(r"/account/second-factor/$"))
    main = page.get_by_role("main")
    expect(main.get_by_role("heading", level=1)).to_have_text("Set up a second sign-in factor")
    expect(main.get_by_role("region", name="Why it is needed")).to_contain_text(
        "The laboratory requires a second factor of client portal accounts."
    )
    expect(main.get_by_role("link", name="Set up authenticator app")).to_be_visible()
    expect(main.get_by_role("button", name="Sign out")).to_be_visible()
    for name in ("Primary", "Quick actions", "Portal"):
        expect(page.get_by_role("navigation", name=name, exact=True)).to_have_count(0)
    expect(page.get_by_role("searchbox")).to_have_count(0)
    expect(page.get_by_role("link", name="What's new")).to_have_count(0)
    check_layout(page, view)


def test_locked_demo_setup_offers_only_sign_out_in_account_menu(page, demo):
    User.objects.filter(username="demo_client_services").update(credentials_locked=True)
    SecondFactorRule.objects.create(pk=1, staff=True, revision=1)
    sign_in(page, "demo_client_services", demo.password)
    main = page.get_by_role("main")
    expect(main).to_contain_text("This shared demonstration account cannot add a second factor")
    expect(main.get_by_role("link", name="Set up authenticator app")).to_have_count(0)
    expect(main.get_by_role("button", name="Sign out")).to_be_visible()
    account = page.get_by_role("button", name=re.compile("^Demo "))
    if account.is_visible():
        account.click()
    destinations = page.get_by_role(
        "link", name=re.compile(r"^(Where you are signed in|Email notices)$")
    )
    assert destinations.all_text_contents() == []


def test_each_rule_form_section_has_a_legend(page, demo):
    sign_in(page, "demo_admin", demo.password)
    page.goto("/lab/security/")
    page.get_by_role("link", name="Edit second sign-in factor rule").click()
    dialog = page.get_by_role("dialog", name="Edit second sign-in factor rule")
    expect(field(dialog, "Reason")).to_be_visible()
    sections = dialog.locator("fieldset.vr-form-section")
    if sections.count() > 1:
        for section in sections.all():
            expect(section.locator(":scope > legend")).to_have_text(re.compile(r"\S"))


def test_rule_form_can_be_saved_as_a_full_page_without_script(browser, browser_context_args, demo):
    with browser.new_context(**browser_context_args, java_script_enabled=False) as context:
        page = context.new_page()
        sign_in(page, "demo_admin", demo.password)
        page.goto("/lab/security/")
        edit = page.get_by_role("link", name="Edit second sign-in factor rule")
        edit.click()
        expect(page).to_have_url(re.compile(r"/lab/security/second-factor/$"))
        expect(page.get_by_role("heading", level=1)).to_have_text("Edit second sign-in factor rule")
        page.get_by_label("Client portal accounts", exact=True).check()
        field(page, "Reason").fill("Include our synthetic portal accounts.")
        page.get_by_role("button", name="Save changes").click()
        expect(page).to_have_url(re.compile(r"/lab/security/$"))
        panel = page.get_by_role("region", name="Second sign-in factor", exact=True)
        expect(panel.get_by_role("row", name="Client portal accounts Required")).to_be_visible()
