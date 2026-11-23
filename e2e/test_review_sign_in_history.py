"""Read a portal account's history from its open sessions and consult the page's own help."""

from datetime import datetime

import pytest
from django.urls import reverse
from django.utils import timezone
from helpers import sign_in
from playwright.sync_api import expect

from accounts.models import SignInEvent, SignInFactor, User

pytestmark = pytest.mark.django_db(transaction=True)


def test_portal_reader_can_read_all_fields_and_open_the_history_help(page, demo, view):
    user = User.objects.get(username="demo_client")
    address = "2001:db8:abcd:ef01:2345:6789:abcf:1234"
    earlier = SignInEvent.objects.create(
        user=user,
        kind=SignInEvent.Kind.REAUTHENTICATION_FAILED,
        factor=SignInFactor.SECOND_FACTOR,
        address=address,
        user_agent="Mozilla/5.0 (Windows NT 10.0) Firefox/140.0",
    )
    sign_in(page, user.username, demo.password)
    page.goto(reverse("account-sessions"))
    sessions = page.get_by_role("main")
    sessions.get_by_role("link", name="Sign-in history", exact=True).press("Enter")
    expect(page.get_by_role("heading", level=1)).to_have_text("Sign-in history")
    expect(page.locator("html")).to_have_attribute("data-bs-theme", view.theme)
    main = page.get_by_role("main")
    expect(main).to_contain_text("1 attempt listed below was not let through")
    expect(main).to_contain_text("tell the laboratory")

    rows = main.get_by_role("table", name="Recent activity").locator("tbody tr")
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text("This browser")
    when = rows.last.get_by_role("rowheader").locator("time")
    assert datetime.fromisoformat(when.get_attribute("datetime")) == earlier.occurred_at
    expect(when).to_have_text(timezone.localtime(earlier.occurred_at).strftime("%Y-%m-%d %H:%M"))
    columns = ("Event", "Details", "Browser", "Address")
    values = (
        "Re-authentication failed",
        "Authenticator or recovery code not accepted",
        "Firefox on Windows",
        address,
    )
    cells = rows.last.get_by_role("cell")
    for index, (column, value) in enumerate(zip(columns, values, strict=True)):
        cell = cells.nth(index)
        expect(cell).to_be_visible()
        expect(cell).to_have_text(value)
        if view.phone:
            assert cell.evaluate("e => getComputedStyle(e, '::before').content") == f'"{column}"'
        else:
            expect(main.get_by_role("columnheader", name=column, exact=True)).to_be_visible()
        assert cell.evaluate("e => e.scrollWidth <= e.clientWidth + 1"), column
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    help_button = page.locator(".vr-topbar [data-vr-documentation-toggle]")
    help_button.press("Enter")
    guide = page.locator("#vr-documentation")
    expect(guide).to_be_visible()
    expect(guide.locator("[data-vr-documentation-id]")).to_have_attribute(
        "data-vr-documentation-id", "accounts-sign-in-history"
    )
    guide.get_by_role("button", name="Close documentation", exact=True).press("Enter")
    expect(help_button).to_be_focused()
    main.get_by_role("link", name="Where you are signed in", exact=True).press("Enter")
    expect(page.get_by_role("heading", level=1)).to_have_text("Where you are signed in")


def test_shared_account_history_help_distinguishes_actions_unavailable_to_its_visitors(
    page, demo, browser, browser_context_args
):
    username = "demo_client"
    User.objects.filter(username=username).update(credentials_locked=True)
    with browser.new_context(**browser_context_args) as other:
        sign_in(other.new_page(), username, demo.password)
        sign_in(page, username, demo.password)
        main = page.get_by_role("main")
        page.goto(reverse("account-sessions"))
        expect(main).to_contain_text("1 other session is open now")
        expect(main).to_contain_text("cannot be signed out from this page")
        main.get_by_role("link", name="Sign-in history", exact=True).click()
        expect(main.get_by_role("table").locator("tbody tr")).to_have_count(2)
        menu = page.locator(".vr-topbar .dropdown-menu").filter(
            has_text="Shared demonstration account"
        )
        expect(menu).to_contain_text("its password and two-factor settings cannot be changed")
        expect(menu.locator("a", has_text="Change password")).to_have_count(0)

        page.locator(".vr-topbar [data-vr-documentation-toggle]").click()
        tasks = page.locator("#vr-help-tasks")
        expect(tasks).to_be_visible()
        task_text = tasks.inner_text().lower()
        unavailable_advice = (
            "sign it out on where you are signed in",
            "change your password from the account menu",
        )
        if any(advice in task_text for advice in unavailable_advice):
            assert "shared" in task_text, (
                "Common tasks tells a shared visitor to sign out another session or change the "
                "published password without saying which accounts can take those actions."
            )
