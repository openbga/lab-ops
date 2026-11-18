"""What's new: the top bar's unread indicator, the list, and the indicator clearing once opened."""

import re

import pytest
from helpers import make_user, sign_in
from playwright.sync_api import Page, expect

from core import changelog

pytestmark = pytest.mark.django_db(transaction=True)


def test_open_whats_new_see_entries_and_the_indicator_clears(page: Page):
    from core.models import UserPreference
    from core.preferences import WHATS_NEW_KEY

    entries = changelog.read_entries()
    assert entries, "changelog.d/ should carry entries"
    newest = entries[0]
    # A returning user who saw everything except the newest change (there may be only one entry,
    # so the marker is written out rather than derived from the older entries).
    user = make_user("e2e_whats_new", "e2e-password-4821!", "technical")
    UserPreference.objects.create(
        user=user,
        key=WHATS_NEW_KEY,
        value={"date": newest.date.isoformat(), "ids": [entry.id for entry in entries[1:]]},
    )
    sign_in(page, "e2e_whats_new", "e2e-password-4821!")
    expect(page.get_by_role("heading", level=1, name="Dashboard")).to_be_visible()

    link = page.locator(".vr-topbar").get_by_role("link", name=re.compile("^What's new"))
    expect(link).to_have_accessible_name("What's new: 1 new change")
    expect(link.locator(".vr-topbar-count")).to_have_text("1")
    link.hover()
    expect(page.get_by_role("tooltip")).to_have_text("What's new: 1 new change")

    link.click()
    expect(page.get_by_role("heading", level=1, name="What's new")).to_be_visible()
    month = page.get_by_role("region", name=newest.date.strftime("%B %Y"))
    expect(month).to_be_visible()
    expect(month.get_by_role("heading", name="Highlights")).to_be_visible()
    highlight = month.locator("ul .vr-change").first
    expect(highlight.get_by_text(newest.title, exact=True)).to_be_visible()
    expect(highlight.get_by_text("New", exact=True)).to_be_visible()
    archive = month.locator(".vr-change-list")
    marked = archive.locator(".vr-change[data-new]")
    expect(marked).to_have_count(1)
    expect(marked).to_be_hidden()
    archive.locator(":scope > summary").press("Enter")
    expect(marked.get_by_text(newest.title, exact=True)).to_be_visible()
    # Opening the list clears the indicator, here and on the next page.
    expect(link).to_have_accessible_name("What's new")
    expect(link.locator(".vr-topbar-count")).to_have_count(0)
    expect(link).to_have_attribute("aria-current", "page")
    notes = archive.locator(".vr-change-notes").first
    if notes.count():
        notes.locator("summary").click()
        expect(notes.locator(".vr-change-notes-body")).to_be_visible()
    page.goto("/")
    expect(page.locator(".vr-topbar").get_by_role("link", name="What's new")).to_be_visible()
    expect(page.locator(".vr-topbar-count")).to_have_count(0)
    assert page.console_errors == []


def test_a_new_account_sees_highlights_and_can_open_the_full_list(page: Page, view):
    entries = changelog.read_entries()
    make_user("e2e_first_visit", "e2e-password-4821!", "supervisor")
    sign_in(page, "e2e_first_visit", "e2e-password-4821!")
    expect(page.get_by_role("heading", level=1, name="Dashboard")).to_be_visible()
    link = page.locator(".vr-topbar").get_by_role("link", name="What's new", exact=True)
    expect(link).to_be_visible()
    expect(page.locator(".vr-topbar-count")).to_have_count(0)
    link.click()
    month = changelog.group_entries(entries)[0]
    expect(page.locator(".vr-change:visible")).to_have_count(min(3, month.count))
    expect(page.locator(".vr-change-list .vr-change")).to_have_count(len(entries))
    expect(page.locator(".vr-change[data-new]")).to_have_count(0)
    archive = page.locator(".vr-change-list").first
    toggle = archive.locator(":scope > summary")
    toggle.press("Space")
    expect(archive.locator(".vr-change:visible")).to_have_count(month.count)
    toggle.press("Space")
    expect(archive.locator(".vr-change").first).to_be_hidden()
    expect(toggle).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator(".vr-topbar [data-vr-documentation-toggle]").click()
    help_panel = page.locator("#vr-documentation")
    expect(help_panel.get_by_text("Catch up on changes", exact=True)).to_be_visible()
    help_panel.get_by_role("button", name="Close documentation", exact=True).click()
