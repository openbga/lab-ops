"""Independent regression journeys for the audit workspace and its shared list controls."""

import re
from urllib.parse import parse_qs, urlsplit

import pytest
from helpers import contrast_failures, field, pick, sign_in, theme_samples
from playwright.sync_api import expect
from test_phone_layout import check as check_phone_targets

from accounts.models import User
from desk.portal_scope import portal_requests

pytestmark = pytest.mark.django_db(transaction=True)


def open_filters(page, view):
    if view.phone:
        page.get_by_role("button", name=re.compile("^Filter and sort")).click()
        expect(page.get_by_role("dialog", name="Filter and sort")).to_be_visible()
    return page.get_by_role("search", name=re.compile("^Filter"))


def test_open_tabs_apply_ticket_filter_and_read_help(page, demo, view):
    ticket = portal_requests(User.objects.get(username="demo_client")).order_by("pk").first()
    sign_in(page, "demo_supervisor", demo.password)
    page.goto(f"/requests/{ticket.pk}/")
    if view.phone:
        quick_actions = page.get_by_role("navigation", name="Quick actions")
        quick_actions.get_by_role("button", name="Menu").click()
    primary = page.locator("nav.vr-sidenav")
    primary.get_by_role("link", name="Audit review").click()

    tabs = page.get_by_role("navigation", name="Audit review lists")
    phone_problems = []
    for name in ("Changes", "Workspace use", "Sign-ins", "Access records"):
        tabs.get_by_role("link", name=name, exact=True).click()
        expect(tabs.get_by_role("link", name=name)).to_have_attribute("aria-current", "page")
        table = page.get_by_role("table", name=re.compile("^Audit review"))
        first_row = table.locator("tbody tr").first
        expect(first_row).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth") <= view.viewport["width"]
        samples = theme_samples(page)
        assert samples["scheme"] == view.theme
        assert not contrast_failures(samples)
        if view.phone:
            check_phone_targets(page, name, phone_problems)
            assert first_row.bounding_box()["y"] < view.viewport["height"] - 64 - 80
            headers = table.locator("thead th").all_text_contents()
            labels = first_row.locator("td").evaluate_all(
                "cells => cells.map(cell => cell.getAttribute('data-vr-label'))"
            )
            assert labels == headers[1:]

    filters = open_filters(page, view)
    pick(field(filters, "What"), label="Opened the ticket")
    filters.get_by_role("button", name="More filters").click()
    field(filters, "Ticket number").fill(ticket.number)
    if view.phone:
        check_phone_targets(page, "Open access filters", phone_problems)
    filters.get_by_role("button", name="Apply").click()
    query = parse_qs(urlsplit(page.url).query)
    assert query["ticket"] == [ticket.number]
    assert query["action"] == ["ticket"]
    table = page.get_by_role("table", name="Audit review: access records")
    expect(table.locator("tbody tr").first).to_contain_text(ticket.number)
    expect(page.get_by_role("link", name="Export CSV")).to_be_visible()
    page.locator(".vr-topbar [data-vr-documentation-toggle]").click()
    help_panel = page.locator("#vr-documentation")
    expect(help_panel).to_contain_text("Review everything about one ticket")
    help_panel.get_by_role("button", name="Close documentation").click()
    assert not phone_problems, "\n".join(phone_problems)


@pytest.mark.parametrize(
    ("path", "start", "end"),
    [
        pytest.param("/audit-review/changes/", "From", "To", id="audit-review"),
        pytest.param("/requests/", "Created from", "Created to", id="ticket-queue-control"),
    ],
)
def test_reversed_filter_dates_keep_the_error_visible_and_focused(
    page, demo, view, path, start, end
):
    """The ticket queue also exists on the preceding release and uses the same filter sheet."""
    sign_in(page, "demo_supervisor", demo.password)
    page.goto(path)
    filters = open_filters(page, view)
    filters.get_by_role("button", name="More filters").click()
    field(filters, start).fill("2026-11-03")
    field(filters, end).fill("2026-11-02")
    filters.get_by_role("button", name="Apply").click()
    invalid = page.locator("input[aria-invalid=true]").first
    expect(invalid).to_have_value("2026-11-02")
    expect(invalid).to_be_visible()
    expect(invalid).to_be_focused()
    expect(
        page.get_by_text("End date must be on or after the start date.", exact=True)
    ).to_be_visible()
