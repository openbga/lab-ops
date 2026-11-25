"""Second confirmation of the shared search, Columns and account preference controls.

Adopted from the second confirmation (.local/review-history/audit-review-confirm2.md). Two
preference cases are documented limits (strict xfail).
"""

import re

import pytest
from helpers import sign_in
from playwright.sync_api import expect
from test_phone_layout import post_announcement

pytestmark = pytest.mark.django_db(transaction=True)


def test_search_icon_and_keyboard_clear_stay_in_the_box_after_refusal(page, demo, view):

    routes = {
        "demo_admin": (
            "/lab/announcements/",
            "/setup/service-levels/",
            "/setup/service-levels/calendars/",
            "/setup/systems/",
            "/clients/",
            "/quality/nonconformances/",
            "/quality/actions/",
            "/requests/",
            "/requests/find/",
        ),
        "demo_client": ("/portal/requests/",),
    }
    for reader, paths in routes.items():
        page.context.clear_cookies()
        sign_in(page, reader, demo.password)
        for path in paths:
            page.goto(path + "?q=" + "r" * 201)
            form = page.locator(".vr-find" if path == "/requests/find/" else ".vr-grid-toolbar")
            field = form.get_by_role("searchbox")
            expect(field).to_have_attribute("aria-invalid", "true")
            expect(
                page.locator(f"[id='{field.get_attribute('aria-describedby')}']")
            ).to_be_visible()
            field.scroll_into_view_if_needed()
            box = field.bounding_box()
            icon = form.locator("svg:has(use[href$='#i-search'])").first
            clear = form.get_by_role("button", name="Clear text")
            expect(clear).to_be_visible()
            for item in (icon, clear):
                rect = item.bounding_box()
                assert (
                    box["x"] <= rect["x"] and rect["x"] + rect["width"] <= box["x"] + box["width"]
                )
                assert (
                    box["y"] <= rect["y"] and rect["y"] + rect["height"] <= box["y"] + box["height"]
                )
            field.focus()
            field.press("Tab")
            expect(clear).to_be_focused()
            clear.press("Enter")
            expect(field).to_have_value("")
            expect(field).to_be_focused()
            expect(clear).to_be_hidden()
            field.fill("ordinary search")
            expect(clear).to_be_visible()


def test_help_reference_search_has_one_keyboard_clear_button(page, demo, view):
    from test_documentation import open_help

    sign_in(page, "demo_admin", demo.password)
    page.goto("/clients/")
    open_help(page)
    reference = page.locator("#vr-documentation [data-vr-help-reference]")
    field = reference.get_by_role("searchbox", name="Find a field or action")
    field.fill("no reference field named this")
    expect(reference.locator("[data-vr-help-section]:visible")).to_have_count(0)
    clear = reference.get_by_role("button", name="Clear text")
    expect(clear).to_have_count(1)
    field.press("Tab")
    expect(clear).to_be_focused()
    clear.press("Enter")
    expect(field).to_be_focused()
    expect(field).to_have_value("")
    expect(reference.locator("[data-vr-help-section]").first).to_be_visible()


@pytest.mark.xfail(
    strict=True,
    reason="Predates the workspace, a follow-up: the clear button is 32 px wide; widening it "
    "covered the text of a refused field (third confirmation), so it was withdrawn.",
)
def test_search_clear_target_is_large_enough_on_a_phone(new_context, demo):

    context = new_context(is_mobile=True, has_touch=True)
    page = context.new_page()
    page.set_viewport_size({"width": 390, "height": 844})
    sign_in(page, "demo_admin", demo.password)
    assert page.evaluate("matchMedia('(pointer: coarse)').matches")
    sizes = {}
    for path, selector in (("/clients/", ".vr-grid-toolbar"), ("/requests/find/", ".vr-find")):
        page.goto(path)
        if path == "/clients/":
            page.get_by_role("button", name="Filter and sort", exact=False).click()
        form = page.locator(selector)
        form.get_by_role("searchbox").fill("Example")
        clear = form.get_by_role("button", name="Clear text")
        expect(clear).to_be_visible()
        rect = clear.bounding_box()
        sizes[path] = (rect["width"], rect["height"])
    assert all(min(size) >= 44 for size in sizes.values()), sizes


def test_find_keeps_the_rejected_text_and_recovers_from_staff_navigation(page, demo, view):

    from clients.models import Client

    org = Client.objects.create(name="Synthetic Find recovery")
    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/requests/")
    refused = "é" * 201
    if view.phone:
        page.get_by_role("navigation", name="Quick actions").get_by_role(
            "link", name="Find"
        ).click()
        control = page.get_by_role("search", name="Find tickets").get_by_role("searchbox")
    else:
        control = page.get_by_role("searchbox", name="Find a ticket or client organization")
    control.fill(refused)
    control.press("Enter")
    form = page.get_by_role("search", name="Find tickets")
    search = form.get_by_role("searchbox")
    expect(search).to_be_focused()
    expect(search).to_have_value(refused)
    expect(search).to_have_attribute("aria-invalid", "true")
    feedback = form.locator(f'[id="{search.get_attribute("aria-describedby")}"]')
    expect(feedback).to_be_visible()
    expect(feedback).to_have_text("Use at most 200 characters.")
    expect(page.get_by_role("heading", name="Results", exact=True)).to_have_count(0)
    expect(page.get_by_role("heading", name="Client organizations", exact=True)).to_have_count(0)
    search.fill(org.name)
    search.press("Enter")
    expect(form.get_by_role("searchbox")).not_to_have_attribute("aria-invalid", "true")
    expect(page.get_by_role("link", name=org.name, exact=True)).to_be_visible()


def test_find_refuses_all_201_characters_pasted_in_its_search_box(page, demo):

    from clients.models import Client

    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/requests/find/")
    search = page.get_by_role("search", name="Find tickets").get_by_role("searchbox")
    # The extra character is trailing whitespace copied along with a valid email address.
    email = "a" * 60 + "@" + ("b" * 60 + ".") * 2 + "c" * 9 + ".invalid"
    org = Client(name="Synthetic boundary organization", email=email)
    org.full_clean()
    org.save()
    query = email + " "
    assert len(query) == 201
    search.fill(query)
    expect(search).to_have_value(query)
    search.press("Enter")
    expect(page.get_by_role("link", name=org.name, exact=True)).to_have_count(0)
    expect(search).to_have_attribute("aria-invalid", "true")
    expect(search).to_have_value(query)


def test_columns_rotation_without_the_optional_visibility_api(page, demo):
    """Exercise the shipped compatibility path on the actual responsive toolbar."""
    page.add_init_script("delete Element.prototype.checkVisibility")
    page.set_viewport_size({"width": 844, "height": 390})
    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/audit-review/changes/")
    assert page.evaluate("typeof Element.prototype.checkVisibility") == "undefined"
    chooser = page.get_by_role("button", name="Columns", exact=True)
    chooser.click()
    dialog = page.get_by_role("dialog", name="Columns", exact=True)
    expect(dialog).to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    trigger = page.get_by_role("button", name=re.compile("^Filter and sort"))
    expect(trigger).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    # A reopened phone sheet could also restore focus to the original control.
    visible_opener = chooser if chooser.is_visible() else trigger
    expect(visible_opener).to_be_focused()


def test_columns_restores_the_current_opener_on_each_list_across_repeated_resizes(page, demo):
    """Close at both sides of the breakpoint, after multiple resizes, on all 13 grids."""
    from test_review_audit_review_verify import sheet_routes

    from desk.models import ServiceRequest

    post_announcement()
    for username, routes in sheet_routes().items():
        page.context.clear_cookies()
        sign_in(page, username, demo.password)
        if username == "demo_supervisor":
            ticket = ServiceRequest.objects.order_by("pk").first()
            page.goto(f"/requests/{ticket.pk}/")
        for path, _ in routes:
            if path.endswith("/timeline/"):
                continue  # This chronological list has a filter sheet but no column chooser.
            page.goto(path)
            trigger = page.get_by_role("button", name=re.compile("^Filter and sort"))
            chooser = page.get_by_role("button", name="Columns", exact=True)
            dialog = page.get_by_role("dialog", name="Columns", exact=True)
            for start, end in ((575, 576), (576, 575)):
                page.set_viewport_size({"width": start, "height": 830})
                if start < 576:
                    trigger.click()
                chooser.click()
                expect(dialog).to_be_visible()
                for width in (end, start, end):
                    page.set_viewport_size({"width": width, "height": 830})
                if end < 576:
                    dialog.get_by_role("button", name="Close", exact=True).click()
                else:
                    page.keyboard.press("Escape")
                expect(dialog).to_be_hidden()
                current = chooser if chooser.is_visible() else trigger
                expect(current).to_be_focused()


@pytest.mark.xfail(
    strict=True,
    reason="Documented limit (docs/architecture.md, preferences): the save written last is "
    "kept, and a refusal answered late can still be announced; predates the workspace.",
)
def test_layout_saved_by_two_requests_matches_the_order_the_server_received_them(
    page, demo, monkeypatch
):
    from threading import Event

    from accounts.models import User
    from core import preferences
    from core.models import UserPreference

    first_arrived = Event()
    release_first = Event()
    arrivals = []
    write = preferences.save

    def observe_arrival(request, key, value):
        if key == "grid:clients.clients:v2":
            arrivals.append(set(value["hidden"]))
            if len(arrivals) == 1:
                # The web server has parsed and accepted the first user's selection. Pause this
                # handler before its write while the other server thread handles the next change.
                first_arrived.set()
                assert release_first.wait(20)
        return write(request, key, value)

    user = User.objects.get(username="demo_supervisor")
    sign_in(page, user.username, demo.password)
    other = page.context.new_page()
    for tab in (page, other):
        tab.goto("/clients/")
        tab.get_by_role("button", name="Columns", exact=True).click()
    monkeypatch.setattr(preferences, "save", observe_arrival)
    try:
        page.get_by_role("checkbox", name="City", exact=True).uncheck()
        assert first_arrived.wait(5)
        # Dispatch the second request only after receipt of the first by the Django view.
        with other.expect_response(lambda r: r.url.endswith("/preferences/")) as reply:
            other.get_by_role("checkbox", name="Phone", exact=True).uncheck()
        assert reply.value.status == 204
        with page.expect_response(lambda r: r.url.endswith("/preferences/")) as earlier:
            release_first.set()
        assert earlier.value.status == 204
    finally:
        release_first.set()
    assert arrivals == [{"city"}, {"phone"}]
    stored = UserPreference.objects.get(user=user, key="grid:clients.clients:v2")
    other.reload()
    other.get_by_role("button", name="Columns", exact=True).click()
    hidden = other.get_by_role("checkbox", name="Phone", exact=True)
    assert (set(stored.value["hidden"]), hidden.is_checked()) == ({"phone"}, False)


@pytest.mark.xfail(
    strict=True,
    reason="Documented limit (docs/architecture.md, preferences): the save written last is "
    "kept, and a refusal answered late can still be announced; predates the workspace.",
)
def test_delayed_refusal_does_not_report_the_current_saved_layout_as_unsaved(page, demo):
    from accounts.models import User
    from core.models import UserPreference
    from core.preferences import MAX_PREFERENCES

    user = User.objects.get(username="demo_supervisor")
    sign_in(page, user.username, demo.password)
    page.goto("/clients/")
    prefs = UserPreference.objects.filter(user=user)
    UserPreference.objects.bulk_create(
        [
            UserPreference(user=user, key=f"page:confirm_{i}", value={"per_page": 25})
            for i in range(MAX_PREFERENCES - prefs.count())
        ]
    )
    page.get_by_role("button", name="Columns", exact=True).click()
    page.evaluate("""() => {
        const actualFetch = window.fetch.bind(window);
        const requests = [];
        const writes = [];
        const set = window.vrPreferences.set.bind(window.vrPreferences);
        window.vrPreferences.set = (...args) => {
            const finished = set(...args);
            writes.push(finished);
            return finished;
        };
        let release;
        let latestConfirmed = false;
        window.lateNotices = [];
        document.addEventListener("vr:request-error", event => {
            if (latestConfirmed) window.lateNotices.push(event.detail.message);
        });
        const gate = new Promise(resolve => { release = resolve; });
        window.fetch = (url, options) => {
            if (url !== "/preferences/") return actualFetch(url, options);
            const first = requests.length === 0;
            const response = actualFetch(url, options);
            requests.push(response);
            return response.then(async result => {
                if (first) await gate;
                else latestConfirmed = result.ok;
                return result;
            });
        };
        window.finishPreferenceReplies = async () => {
            // An implementation that defers the second request may send it once the first replies.
            if (requests.length > 1) await requests[1];
            release();
            await Promise.all(writes);
        };
    }""")
    with page.expect_response(lambda r: r.url.endswith("/preferences/")) as refusal:
        page.get_by_role("checkbox", name="City", exact=True).uncheck()
    assert refusal.value.status == 400
    prefs.filter(key="page:confirm_0").delete()
    with page.expect_response(lambda r: r.url.endswith("/preferences/")) as accepted:
        page.get_by_role("checkbox", name="Phone", exact=True).uncheck()
        page.evaluate("() => window.finishPreferenceReplies()")
    assert accepted.value.status == 204
    page.keyboard.press("Escape")
    assert set(prefs.get(key="grid:clients.clients:v2").value["hidden"]) == {"phone", "city"}
    expect(page.locator("table[data-vr-grid] th[data-vr-col='phone']")).to_be_hidden()
    notices = page.evaluate("() => window.lateNotices")
    assert not any(
        "Your display setting applies to this page only" in notice for notice in notices
    ), "The displayed column layout was saved but the page then announced: " + repr(notices)


def test_reset_layout_survives_navigation_with_a_previous_save_still_awaiting_its_reply(
    page, demo, monkeypatch
):
    import json
    from threading import Event

    from django.core.handlers.base import BaseHandler

    from accounts.models import User
    from core.models import UserPreference

    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/clients/")
    page.get_by_role("button", name="Columns", exact=True).click()
    prior_save = Event()
    allow_reply = Event()
    reset = Event()
    original = BaseHandler.get_response
    key = "grid:clients.clients:v2"

    def hold_saved_reply(handler, request):
        response = original(handler, request)
        if request.path == "/preferences/" and response.status_code == 204:
            body = json.loads(request.body)
            if body["key"] == key:
                if body["value"] is None:
                    reset.set()
                elif not prior_save.is_set():
                    prior_save.set()
                    assert allow_reply.wait(20)
        return response

    monkeypatch.setattr(BaseHandler, "get_response", hold_saved_reply)
    try:
        page.get_by_role("checkbox", name="City", exact=True).uncheck()
        assert prior_save.wait(5)
        page.get_by_role("button", name="Reset columns", exact=True).click()
        expect(page.get_by_role("checkbox", name="City", exact=True)).to_be_checked()
        page.goto("/requests/")
    finally:
        allow_reply.set()
    assert reset.wait(5)
    user = User.objects.get(username="demo_supervisor")
    assert not UserPreference.objects.filter(user=user, key=key).exists()
    page.goto("/clients/")
    page.get_by_role("button", name="Columns", exact=True).click()
    expect(page.get_by_role("checkbox", name="City", exact=True)).to_be_checked()
