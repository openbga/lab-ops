"""Confirmation of the search, focus and preference fixes on the real list pages.

Adopted from the confirmation round (.local/review-history/audit-review-confirm.md). Two saves
of one setting arriving out of order is a documented limit (strict xfail).
"""

import json
import re
from threading import Event
from urllib.parse import urlencode

import pytest
from helpers import sign_in
from playwright.sync_api import expect
from test_review_audit_review_verify import sheet_routes

pytestmark = pytest.mark.django_db(transaction=True)


def test_each_grid_saves_its_visible_columns_on_the_account(page, demo):
    """Visit every list that exposes Columns. Check a completed save and the next page."""
    from test_phone_layout import post_announcement

    from accounts.models import User
    from core.models import UserPreference
    from desk.models import ServiceRequest

    post_announcement()
    routes = {
        "demo_admin": [
            "/lab/announcements/",
            "/setup/service-levels/",
            "/setup/service-levels/calendars/",
            "/setup/systems/",
            "/clients/",
            "/quality/nonconformances/",
            "/quality/actions/",
            "/requests/",
        ],
        "demo_supervisor": [
            f"/audit-review/{source}/" for source in ("changes", "access", "sign-ins", "uses")
        ],
        "demo_client": ["/portal/requests/"],
    }
    for username, paths in routes.items():
        page.context.clear_cookies()
        sign_in(page, username, demo.password)
        if username == "demo_supervisor":
            ticket = ServiceRequest.objects.order_by("pk").first()
            page.goto(f"/requests/{ticket.pk}/")
        user = User.objects.get(username=username)
        for path in paths:
            response = page.goto(path)
            assert response.ok, path
            table = page.locator("table[data-vr-grid]")
            expect(table).to_have_attribute("data-vr-grid-ready", "true")
            grid = table.get_attribute("data-vr-grid")
            version = table.get_attribute("data-vr-grid-version")
            key = f"grid:{grid}:v{version}"
            page.get_by_role("button", name="Columns", exact=True).click()
            dialog = page.get_by_role("dialog", name="Columns")
            choice = dialog.locator("input[data-vr-col-toggle]:checked:enabled").first
            column = choice.get_attribute("data-vr-col-toggle")
            with page.expect_response(
                lambda reply: reply.url.endswith("/preferences/") and reply.request.method == "POST"
            ) as saved:
                choice.uncheck()
            assert saved.value.status == 204, path
            assert column in UserPreference.objects.get(user=user, key=key).value["hidden"], path
            page.reload()
            expect(table).to_have_attribute("data-vr-grid-ready", "true")
            expect(table.locator(f'thead th[data-vr-col="{column}"]')).to_have_count(0)


@pytest.mark.parametrize("start_width,end_width", [(390, 844), (844, 390)])
def test_columns_returns_focus_to_the_visible_opener_after_rotation(
    page, demo, start_width, end_width
):
    """A phone can rotate through the 576px filter-toolbar breakpoint with Columns open."""
    page.set_viewport_size({"width": start_width, "height": end_width})
    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/clients/")
    filter_button = page.get_by_role("button", name=re.compile(r"^Filter and sort"))
    columns_button = page.get_by_role("button", name="Columns", exact=True)
    if start_width < 576:
        filter_button.click()
    columns_button.click()
    columns = page.get_by_role("dialog", name="Columns", exact=True)
    expect(columns).to_be_visible()
    page.set_viewport_size({"width": end_width, "height": start_width})
    columns.get_by_role("button", name="Done").click()
    expect(columns).to_be_hidden()
    # A reopened sheet may itself contain the original, visible Columns button.
    expected = columns_button if columns_button.is_visible() else filter_button
    expect(expected).to_be_visible()
    expect(expected).to_be_focused()


def test_each_phone_filter_sheet_and_available_column_chooser_restore_keyboard_focus(page, demo):
    page.set_viewport_size({"width": 390, "height": 844})
    chooser_grids = set()
    for account, routes in sheet_routes().items():
        page.context.clear_cookies()
        sign_in(page, account, demo.password)
        for path, filter_name in routes:
            page.goto(path)
            opener = page.get_by_role("button", name=re.compile(r"^Filter and sort"))
            opener.click()
            sheet = page.get_by_role("dialog", name="Filter and sort")
            expect(sheet).to_be_visible()
            columns_button = sheet.get_by_role("button", name="Columns", exact=True)
            if columns_button.is_visible():
                chooser_grids.add(columns_button.get_attribute("data-vr-columns-for"))
                columns_button.click()
                columns = page.get_by_role("dialog", name="Columns", exact=True)
                expect(columns).to_be_visible()
                expect(sheet).to_be_hidden()
                expect(page.get_by_role("dialog")).to_have_count(1)
                expect(page.locator("#vr-columns-dialog:focus-within")).to_be_visible()
                for key in ("Shift+Tab", "Tab", "Tab"):
                    page.keyboard.press(key)
                    focused = columns.evaluate("""(el) => ({
                        page: document.hasFocus(),
                        inside: el.contains(document.activeElement),
                        tag: document.activeElement.tagName,
                        label: document.activeElement.getAttribute("aria-label"),
                    })""")
                    assert not focused["page"] or focused["inside"], (path, key, focused)
                expect(columns).to_contain_text("saved for your account")
                columns.get_by_role("button", name="Done").click()
            else:
                page.keyboard.press("Escape")
            expect(opener).to_have_attribute("aria-expanded", "false")
            expect(opener).to_be_focused()
            page.goto(path + "?" + urlencode({filter_name: "retired-choice"}))
            expect(sheet).to_be_visible()
            expect(sheet.locator(":focus")).to_have_attribute("aria-invalid", "true")
            page.keyboard.press("Escape")
            expect(sheet).to_be_hidden()
            expect(opener).to_be_focused()
    assert {"clients.clients", "audit_review.changes", "desk.requests"} <= chooser_grids


@pytest.mark.parametrize("control", ["sidebar", "columns"])
def test_second_display_choice_survives_leaving_before_the_first_response(
    page, demo, monkeypatch, control
):
    from django.core.handlers.base import BaseHandler

    first_saved = Event()
    release_response = Event()
    latest_saved = Event()
    get_response = BaseHandler.get_response
    key = "nav.collapsed" if control == "sidebar" else "grid:clients.clients:v2"

    def hold_first_response(handler, request):
        response = get_response(handler, request)
        if request.path == "/preferences/" and response.status_code == 204:
            body = json.loads(request.body)
            if body["key"] == key:
                if not first_saved.is_set():
                    # The real preference transaction has committed. Only the reply is delayed.
                    first_saved.set()
                    assert release_response.wait(20)
                else:
                    latest_saved.set()
        return response

    monkeypatch.setattr(BaseHandler, "get_response", hold_first_response)
    sign_in(page, "demo_supervisor", demo.password)
    working = page.context.new_page()
    working.goto("/clients/")
    try:
        if control == "sidebar":
            working.get_by_role("button", name="Collapse menu").click()
            assert first_saved.wait(5)
            working.get_by_role("button", name="Expand menu").click()
            expect(working.get_by_role("button", name="Collapse menu")).to_be_visible()
        else:
            working.get_by_role("button", name="Columns", exact=True).click()
            city = working.get_by_role("checkbox", name="City", exact=True)
            city.uncheck()
            assert first_saved.wait(5)
            city.check()
            expect(city).to_be_checked()
        working.close()
    finally:
        release_response.set()
    latest_saved.wait(3)
    page.goto("/clients/")
    if control == "sidebar":
        expect(page.get_by_role("button", name="Collapse menu")).to_be_visible()
    else:
        page.get_by_role("button", name="Columns", exact=True).click()
        expect(page.get_by_role("checkbox", name="City", exact=True)).to_be_checked()


@pytest.mark.xfail(
    strict=True,
    reason="Documented limit: two saves of one setting are kept in the order they reach the "
    "server (docs/architecture.md, preferences); predates the workspace.",
)
def test_two_tabs_keep_the_last_sidebar_choice_when_an_earlier_request_arrives_last(page, demo):
    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/clients/")
    later = page.context.new_page()
    later.goto("/requests/")
    delayed = []
    page.route("**/preferences/", lambda route: delayed.append(route))
    with page.expect_request(lambda request: request.url.endswith("/preferences/")):
        page.get_by_role("button", name="Collapse menu").click()

    # These normal clicks are saved on the second page while the earlier click is in flight.
    def is_save(response):
        return response.url.endswith("/preferences/")

    for name in ("Collapse menu", "Expand menu"):
        with later.expect_response(is_save) as saved:
            later.get_by_role("button", name=name).click()
        assert saved.value.status == 204
    with page.expect_response(is_save) as earlier:
        delayed[0].continue_()
    assert earlier.value.status == 204
    later.reload()
    expect(later.get_by_role("button", name="Collapse menu")).to_be_visible()


def test_sidebar_and_column_saves_are_independent_and_recover_after_refusal(page, demo):
    from accounts.models import User
    from core.models import UserPreference
    from core.preferences import MAX_PREFERENCES

    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/clients/")
    user = User.objects.get(username="demo_supervisor")
    existing = UserPreference.objects.filter(user=user)
    UserPreference.objects.bulk_create(
        [
            UserPreference(user=user, key=f"page:confirm_{n}", value={"per_page": 25})
            for n in range(MAX_PREFERENCES - existing.count())
        ]
    )
    held = []

    def hold_sidebar_only(route):
        if route.request.post_data_json["key"] == "nav.collapsed" and not held:
            held.append(route)
        else:
            route.continue_()

    page.route("**/preferences/", hold_sidebar_only)
    with page.expect_request(lambda r: r.url.endswith("/preferences/")):
        page.get_by_role("button", name="Collapse menu").click()
    page.get_by_role("button", name="Columns", exact=True).click()
    city = page.get_by_role("checkbox", name="City", exact=True)
    with page.expect_response(lambda r: r.url.endswith("/preferences/")) as refused:
        city.uncheck()
    assert refused.value.status == 400
    notice = page.locator("[data-vr-request-error]")
    expect(notice).to_contain_text("as many saved list settings and layouts as it can")
    existing.filter(key="page:confirm_0").delete()
    with page.expect_response(lambda r: r.url.endswith("/preferences/")) as saved:
        city.check()
    assert saved.value.status == 204
    assert "city" not in existing.get(key="grid:clients.clients:v2").value["hidden"]
    existing.filter(key="page:confirm_1").delete()
    with page.expect_response(lambda r: r.url.endswith("/preferences/")) as sidebar:
        held[0].continue_()
    assert sidebar.value.status == 204
    assert existing.get(key="nav.collapsed").value is True


def test_search_errors_and_recovery_on_every_searchable_toolbar(page, demo, view):
    from urllib.parse import urlencode

    from helpers import sign_in
    from playwright.sync_api import expect

    routes = {
        "demo_admin": [
            ("/lab/announcements/", "state"),
            ("/setup/service-levels/", "active"),
            ("/setup/service-levels/calendars/", "active"),
            ("/setup/systems/", "active"),
            ("/clients/", "state"),
            ("/quality/nonconformances/", "source"),
            ("/quality/actions/", "status"),
            ("/requests/", "priority"),
        ],
        "demo_client": [("/portal/requests/", "status")],
    }
    for reader, pages in routes.items():
        page.context.clear_cookies()
        sign_in(page, reader, demo.password)
        for path, field in pages:
            q = "x" * 201
            response = page.goto(path + "?" + urlencode({"q": q, field: "former-choice"}))
            assert response and response.ok, path
            toolbar = page.locator(".vr-grid-toolbar")
            search = toolbar.locator('input[type="search"]')
            expect(search).to_have_value(q)
            expect(search).to_be_focused()
            expect(search).to_have_attribute("aria-invalid", "true")
            error = page.locator(f"[id='{search.get_attribute('aria-describedby')}']")
            expect(error).to_be_visible()
            expect(error).to_contain_text("at most 200 characters")
            second = toolbar.locator(f"select[name='{field}']")
            expect(second).to_have_attribute("aria-invalid", "true")
            if view.phone:
                expect(page.get_by_role("dialog", name="Filter and sort")).to_be_visible()

            accepted = page.goto(path + "?" + urlencode({"q": "x" * 200}))
            assert accepted and accepted.ok, path
            expect(search).to_have_value("x" * 200)
            expect(toolbar.locator("[aria-invalid=true]")).to_have_count(0)
            if view.phone:
                expect(page.get_by_role("dialog", name="Filter and sort")).to_have_count(0)


def test_refused_search_is_excluded_from_phone_active_filter_summary(page, demo):
    from helpers import sign_in
    from playwright.sync_api import expect

    page.set_viewport_size({"width": 390, "height": 844})
    sign_in(page, "demo_admin", demo.password)
    page.goto("/clients/?q=" + "x" * 201)
    # The error state explicitly says that the filters could not be applied.
    expect(page.get_by_text("These filters cannot be applied", exact=True)).to_be_attached()
    sheet = page.get_by_role("dialog", name="Filter and sort")
    if sheet.is_visible():
        sheet.get_by_role("button", name="Close Filter and sort").click()
        expect(sheet).to_be_hidden()
    button = page.locator(".vr-filter-button")
    expect(button.locator(".vr-filter-count")).to_have_count(0)
    expect(page.locator("[data-vr-filter-state]")).not_to_contain_text("x" * 201)


def test_search_icon_stays_in_the_input_when_a_field_error_is_shown(page, demo, view):
    from helpers import sign_in
    from playwright.sync_api import expect

    sign_in(page, "demo_admin", demo.password)
    page.goto("/clients/?q=" + "x" * 201)
    search = page.locator(".vr-grid-search:has([name=q])")
    control = search.locator("input")
    expect(page.get_by_text("These filters cannot be applied", exact=True)).to_be_attached()
    if view.phone:
        if not control.get_attribute("aria-invalid"):
            page.get_by_role("button", name="Filter and sort", exact=False).click()
        expect(page.locator(".vr-grid-sheet.show")).to_be_visible()
    expect(control).to_be_visible()
    field = control.bounding_box()
    # Adapted: the icon sits with the input in its own box (includes/grid_search.html).
    icon = search.locator(".vr-grid-search-box > .vr-icon").bounding_box()
    assert field["y"] <= icon["y"] and icon["y"] + icon["height"] <= field["y"] + field["height"], (
        "The search icon falls outside its input when the field shows an error",
        field,
        icon,
    )


def test_find_search_does_not_report_a_match_for_a_different_email(page, demo):
    from helpers import sign_in
    from playwright.sync_api import expect

    from clients.models import Client

    # A valid 200-character address, using ordinary-length local part and domain labels.
    email = "a" * 60 + "@" + ("b" * 60 + ".") * 2 + "c" * 9 + ".invalid"
    assert len(email) == 200
    org = Client(name="Example Find Laboratory", email=email)
    org.full_clean()
    org.save()
    sign_in(page, "demo_admin", demo.password)
    page.goto("/requests/find/")
    find = page.get_by_role("search", name="Find tickets")
    search = find.get_by_role("searchbox")
    result = page.get_by_role("link", name=org.name, exact=True)
    search.fill(email)
    find.get_by_role("button", name="Find").click()
    expect(result).to_be_visible()

    other = email + "x"
    search.fill(other)
    if search.input_value() == email:
        return  # A search box enforcing its documented limit keeps the visible, accepted text.
    expect(search).to_have_value(other)
    find.get_by_role("button", name="Find").click()
    expect(result).to_have_count(0)
