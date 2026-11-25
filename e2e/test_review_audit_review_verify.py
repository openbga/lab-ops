"""Verify shared filter sheets and saved column layouts after the audit review changes."""

import re
from urllib.parse import urlencode

import pytest
from helpers import field, make_user, sign_in
from playwright.sync_api import expect

from clients.models import Client

pytestmark = pytest.mark.django_db(transaction=True)


def filter_button(page):
    return page.get_by_role("button", name=re.compile("^Filter and sort"))


def sheet_routes():
    clinic = Client.objects.get(account_number="EXCLIN")
    return {
        "demo_admin": [
            ("/lab/announcements/", "state"),
            ("/setup/service-levels/", "active"),
            ("/setup/service-levels/calendars/", "active"),
            ("/setup/systems/", "active"),
            ("/clients/", "state"),
            (f"/clients/{clinic.pk}/timeline/", "kind"),
            ("/quality/nonconformances/", "source"),
            ("/quality/actions/", "status"),
            ("/requests/", "priority"),
        ],
        "demo_supervisor": [
            (f"/audit-review/{source}/", "action")
            for source in ("changes", "access", "sign-ins", "uses")
        ],
        "demo_client": [("/portal/requests/", "status")],
    }


def test_each_list_accepts_a_current_choice_and_focuses_a_retired_choice(page, demo, view):
    """A bookmarked choice can disappear. All sheets must find the refused enhanced control."""
    for account, surfaces in sheet_routes().items():
        page.context.clear_cookies()
        sign_in(page, account, demo.password)
        for path, name in surfaces:
            response = page.goto(path)
            assert response and response.ok, path
            select = page.locator(f".vr-grid-sheet select[name='{name}']")
            value = select.locator('option:not([value=""])').first.get_attribute("value")
            response = page.goto(path + "?" + urlencode({name: value}))
            assert response and response.ok, path
            expect(select).to_have_value(value)
            expect(page.locator(".vr-grid-sheet [aria-invalid=true]")).to_have_count(0)
            if view.phone:
                expect(filter_button(page)).to_have_attribute("aria-expanded", "false")
            response = page.goto(path + "?" + urlencode({name: "former-choice"}))
            assert response and response.ok, path
            if view.phone:
                expect(filter_button(page)).to_have_attribute("aria-expanded", "true")
                expect(page.get_by_role("dialog", name="Filter and sort")).to_be_visible()
            else:
                expect(page.get_by_role("dialog", name="Filter and sort")).to_have_count(0)
            focused = page.locator(".vr-grid-sheet :focus")
            expect(focused).to_have_attribute("aria-invalid", "true")
            description = focused.get_attribute("aria-describedby").split()
            errors = [
                page.locator(f"[id='{ref}']") for ref in description if ref.endswith("_error")
            ]
            assert len(errors) == 1 and errors[0].is_visible(), (path, description)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_closing_reopened_filter_sheet_returns_focus_to_filter_button(page, demo, theme):
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(color_scheme=theme)
    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/requests/")
    button = filter_button(page)
    button.click()
    sheet = page.get_by_role("dialog", name="Filter and sort")
    sheet.get_by_role("button", name="More filters").click()
    field(sheet, "Created from").fill("2026-10-20")
    field(sheet, "Created to").fill("2026-10-10")
    sheet.get_by_role("button", name="Apply").click()
    expect(sheet).to_be_visible()
    expect(field(sheet, "Created to")).to_be_focused()
    sheet.get_by_role("button", name="Close Filter and sort").click()
    expect(button).to_have_attribute("aria-expanded", "false")
    expect(sheet).to_be_hidden()
    expect(button).to_be_focused()


def test_overlength_search_shows_its_field_error_where_it_can_be_corrected(page, demo, view):
    """The shared search control accepts pasted text and the form enforces its 200-char limit."""
    sign_in(page, "demo_admin", demo.password)
    page.goto("/clients/")
    if view.phone:
        filter_button(page).click()
    search = page.get_by_role("searchbox", name="Search")
    search.fill("a" * 201)
    page.get_by_role("button", name="Apply", exact=True).click()
    expect(page.get_by_text("These filters cannot be applied", exact=True)).to_be_attached()
    expect(search).to_be_visible()
    expect(search).to_be_focused()
    expect(search).to_have_value("a" * 201)
    expect(
        page.get_by_text("Ensure this value has at most 200 characters", exact=False)
    ).to_be_visible()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_refused_queue_bulk_action_reopens_only_the_bulk_sheet(page, demo, theme):
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(color_scheme=theme)
    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/requests/?view=all")
    selected = page.locator('tbody input[name="requests"]').first
    selected.check()
    trigger = page.get_by_role("button", name=re.compile("^Change selected tickets"))
    trigger.click()
    sheet = page.get_by_role("dialog", name="Change selected tickets")
    sheet.get_by_role("button", name="Apply to selected").click()
    expect(sheet.get_by_text("Choose a team or a person.")).to_be_visible()
    expect(page.locator(".offcanvas-sm.show")).to_have_count(1)
    expect(filter_button(page)).to_have_attribute("aria-expanded", "false")
    expect(selected).to_be_checked()
    page.keyboard.press("Escape")
    expect(trigger).to_be_focused()


# Saved column layouts


def test_review_grid_layout_follows_account_and_list(page, demo, new_context):
    from django.contrib.auth.models import Permission

    from accounts.models import User
    from core.models import UserPreference
    from desk.models import ServiceRequest

    user = User.objects.get(username="demo_supervisor")
    other = make_user("columns_reader", demo.password, "technical")
    other.user_permissions.add(
        Permission.objects.get(content_type__app_label="accounts", codename="review_audit_records")
    )
    sign_in(page, user.username, demo.password)
    page.goto(f"/requests/{ServiceRequest.objects.order_by('pk').first().pk}/")
    changed = {}
    defaults = {}
    lists = ("changes", "access", "sign-ins", "uses")

    def open_list(name, browser_page):
        browser_page.goto(f"/audit-review/{name}/")
        table = browser_page.locator("table[data-vr-grid]")
        expect(table).to_have_attribute("data-vr-grid-ready", "true")
        expect(table.locator("tbody tr").first).to_be_visible()
        return table

    def ids(table):
        return table.locator("thead th").evaluate_all("cells => cells.map(c => c.dataset.vrCol)")

    for name in lists:
        table = open_list(name, page)
        defaults[name] = ids(table)
        grid_name = table.get_attribute("data-vr-grid")
        assert grid_name == "audit_review." + name.replace("-", "_")
        expect(page.get_by_role("button", name="Columns", exact=True)).to_be_visible()
        page.get_by_role("button", name="Columns", exact=True).click()
        dialog = page.get_by_role("dialog", name="Columns", exact=True)
        expect(dialog.get_by_role("checkbox", name="When")).to_be_checked()
        expect(dialog.get_by_role("checkbox", name="When")).to_be_disabled()
        moved = defaults[name][3]
        move = dialog.locator(f'[data-vr-col-move="up"][data-vr-col-id="{moved}"]')
        move.click()
        page.wait_for_load_state("networkidle")
        dialog.get_by_role("checkbox", name="Person").uncheck()
        page.wait_for_load_state("networkidle")
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        order = list(defaults[name])
        order[2], order[3] = order[3], order[2]
        changed[name] = [col for col in order if col != "person"]
        assert ids(table) == changed[name]
        stored = UserPreference.objects.get(user=user, key=f"grid:{grid_name}:v1")
        assert stored.value == {"order": order, "hidden": ["person"]}

    page.context.clear_cookies()
    sign_in(page, other.username, demo.password)
    for name in lists:
        assert ids(open_list(name, page)) == defaults[name]
    assert not UserPreference.objects.filter(
        user=other, key__startswith="grid:audit_review."
    ).exists()

    workstation = new_context().new_page()
    sign_in(workstation, user.username, demo.password)
    for name in lists:
        table = open_list(name, workstation)
        assert ids(table) == changed[name]
        labels = table.locator("thead th").evaluate_all(
            "cells => cells.map(c => c.dataset.vrColLabel)"
        )
        body_labels = (
            table.locator("tbody tr")
            .first.locator("th, td")
            .evaluate_all("cells => cells.map(c => c.dataset.vrLabel)")
        )
        assert body_labels == labels


@pytest.mark.parametrize("path", ["/audit-review/changes/", "/clients/"])
def test_phone_column_chooser_keeps_keyboard_focus_in_the_visible_dialog(page, demo, path):
    page.set_viewport_size({"width": 390, "height": 844})
    sign_in(page, "demo_supervisor", demo.password)
    page.goto(path)
    opener = page.get_by_role("button", name=re.compile("^Filter and sort"))
    opener.click()
    sheet = page.get_by_role("dialog", name="Filter and sort")
    sheet.get_by_role("button", name="Columns").click()
    columns = page.get_by_role("dialog", name="Columns", exact=True)
    expect(columns).to_be_visible()
    expect(page.locator("#vr-columns-dialog:focus-within")).to_be_visible()
    page.keyboard.press("Escape")
    expect(columns).to_be_hidden()
    # Either the sheet remains open, or the whole interaction returned to its original opener.
    if not sheet.is_visible():
        expect(opener).to_be_focused()
        return
    expect(sheet.get_by_role("button", name="Columns")).to_be_focused()
    for _ in range(25):
        page.keyboard.press("Tab")
        assert sheet.evaluate("el => el.contains(document.activeElement)"), (
            "Keyboard focus left the open Filter and sort sheet"
        )


@pytest.mark.parametrize(
    ("path", "column"), [("/audit-review/changes/", "Person"), ("/clients/", "City")]
)
def test_column_chooser_explains_where_the_layout_is_saved(page, demo, path, column):
    from accounts.models import User
    from core.models import UserPreference

    user = User.objects.get(username="demo_supervisor")
    sign_in(page, user.username, demo.password)
    page.goto(path)
    page.get_by_role("button", name="Columns", exact=True).click()
    dialog = page.get_by_role("dialog", name="Columns", exact=True)
    # Adapted: wait for the save itself rather than for the network to fall quiet.
    with page.expect_response(lambda response: response.url.endswith("/preferences/")) as saved:
        dialog.get_by_role("checkbox", name=column, exact=True).uncheck()
    assert saved.value.ok
    table = page.locator("table[data-vr-grid]").first
    grid = table.get_attribute("data-vr-grid")
    version = table.get_attribute("data-vr-grid-version")  # adapted: the client list is at v2
    assert UserPreference.objects.get(user=user, key=f"grid:{grid}:v{version}").value["hidden"]
    expect(dialog).not_to_contain_text("saved in this browser")


@pytest.mark.xfail(
    strict=True,
    reason="Documented limit (confirmation round): two saves of one setting are kept in the "
    "order they reach the server (docs/architecture.md, preferences); predates the workspace.",
)
@pytest.mark.parametrize(
    ("path", "column"), [("/audit-review/changes/", "Person"), ("/clients/", "City")]
)
def test_last_column_choice_survives_an_earlier_save_arriving_late(page, demo, path, column):
    sign_in(page, "demo_supervisor", demo.password)
    page.goto(path)
    page.get_by_role("button", name="Columns", exact=True).click()
    dialog = page.get_by_role("dialog", name="Columns", exact=True)
    choice = dialog.get_by_role("checkbox", name=column, exact=True)
    expect(choice).to_be_checked()
    # Delay delivery of the first save. Each response still comes from the real endpoint.
    page.evaluate("""() => {
        const send = window.fetch.bind(window);
        const saves = [];
        let releaseFirst;
        const delay = new Promise(resolve => { releaseFirst = resolve; });
        window.fetch = function(resource, options) {
            if (resource !== "/preferences/") return send(resource, options);
            const result = saves.length === 0
                ? delay.then(() => send(resource, options))
                : send(resource, options);
            saves.push(result);
            return result;
        };
        window.finishColumnSaves = async () => {
            // A client that serializes saves can send its second one after the first finishes.
            await Promise.all(saves.slice(1));
            releaseFirst();
            const responses = await Promise.all(saves);
            return responses.length > 0 && responses.every(response => response.ok);
        };
    }""")
    choice.uncheck()
    choice.check()
    assert page.evaluate("() => window.finishColumnSaves()")
    page.wait_for_load_state("networkidle")
    page.reload()
    page.get_by_role("button", name="Columns", exact=True).click()
    expect(choice).to_be_checked()
