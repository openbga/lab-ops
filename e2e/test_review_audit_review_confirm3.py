"""Third confirmation of Find, Help, search clear controls and Columns focus.

Adopted from the third confirmation (.local/review-history/audit-review-confirm3.md).
"""

import re

import pytest
from helpers import field, sign_in
from playwright.sync_api import expect

pytestmark = pytest.mark.django_db(transaction=True)


def test_fallback_returns_focus_after_rotating_with_columns_open_on_all_four_lists(page, demo):
    from desk.models import ServiceRequest

    page.add_init_script("delete Element.prototype.checkVisibility")
    sign_in(page, "demo_supervisor", demo.password)
    ticket = ServiceRequest.objects.order_by("pk").first()
    page.goto(f"/requests/{ticket.pk}/")
    for source in ("changes", "access", "sign-ins", "uses"):
        page.goto(f"/audit-review/{source}/")
        assert page.evaluate("typeof Element.prototype.checkVisibility") == "undefined"
        trigger = page.get_by_role("button", name=re.compile("^Filter and sort"))
        chooser = page.get_by_role("button", name="Columns", exact=True)
        dialog = page.get_by_role("dialog", name="Columns", exact=True)
        for start, end in ((390, 840), (840, 390)):
            page.set_viewport_size({"width": start, "height": end})
            if start == 390:
                trigger.click()
            chooser.click()
            expect(dialog).to_be_visible()
            for width in (end, start, end):
                page.set_viewport_size({"width": width, "height": 800})
            dialog.get_by_role("button", name="Done").click()
            expect(dialog).to_be_hidden()
            current = chooser if chooser.is_visible() else trigger
            expect(current).to_be_focused()


def test_help_search_reinitializes_between_page_form_and_rejected_form_without_changing_inputs(
    page, demo
):
    from test_documentation import help_article, help_id, open_help

    sign_in(page, "demo_admin", demo.password)
    page.goto("/clients/")
    open_help(page)
    panel = page.locator("#vr-documentation")
    page_id = help_id(page)

    def check_filter_and_clear():
        reference = panel.locator("[data-vr-help-reference]")
        search = reference.get_by_role("searchbox", name="Find a field or action")
        clear = reference.get_by_role("button", name="Clear text", include_hidden=True)
        expect(search).to_have_value("")
        expect(clear).to_have_count(1)
        expect(clear).to_be_hidden()
        term = reference.locator("dt").first.text_content().strip()
        search.fill(term)
        expect(reference.locator("mark").first).to_be_visible()
        search.fill("synthetic unlisted help label")
        expect(reference.get_by_text("No field or action matches.")).to_be_visible()
        search.press("Tab")
        expect(clear).to_be_focused()
        clear.press("Space")
        expect(search).to_be_focused()
        expect(search).to_have_value("")
        expect(clear).to_be_hidden()
        expect(reference.locator("mark")).to_have_count(0)
        expect(reference.get_by_text("No field or action matches.")).to_be_hidden()

    check_filter_and_clear()
    panel.get_by_role("button", name="Close documentation").click()
    open_help(page)
    check_filter_and_clear()
    page.get_by_role("link", name="New organization", exact=True).first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(help_article(page)).not_to_have_attribute("data-vr-documentation-id", page_id)
    form_help = help_id(page)
    name = field(dialog, "Organization name")
    email = field(dialog, "General email")
    name.fill("Synthetic reference confirmation")
    email.fill("invalid")
    check_filter_and_clear()
    expect(name).to_have_value("Synthetic reference confirmation")
    expect(email).to_have_value("invalid")
    dialog.get_by_role("button", name="Create organization", exact=True).click()
    expect(email).to_have_attribute("aria-invalid", "true")
    expect(help_article(page)).to_have_attribute("data-vr-documentation-id", form_help)
    check_filter_and_clear()
    expect(name).to_have_value("Synthetic reference confirmation")
    page.set_viewport_size({"width": 390, "height": 844})
    panel.get_by_role("button", name="Close documentation").click()
    trigger = dialog.locator("[data-vr-documentation-toggle]")
    expect(trigger).to_be_focused()
    trigger.click()
    expect(panel).to_be_visible()
    assert panel.evaluate("el => el.matches(':modal')")
    check_filter_and_clear()
    panel.get_by_role("button", name="Close documentation").click()
    expect(email).to_have_value("invalid")
    expect(name).to_have_value("Synthetic reference confirmation")
    expect(trigger).to_be_focused()
    assert page.console_errors == []


def test_find_counts_pasted_spaces_and_unicode_before_normalizing_the_query(page, demo):

    from clients.models import Client

    lab = Client.objects.create(name="Synthetic 🧪 & calibration laboratory")
    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/requests/find/")
    form = page.get_by_role("search", name="Find tickets")
    box = form.get_by_role("searchbox")
    match = page.get_by_role("link", name=lab.name, exact=True)
    # The displayed name includes a non-BMP character. Python length here is in code points.
    allowed = "\u00a0" + lab.name.ljust(198) + "\u2002"
    assert len(allowed) == 200
    for pasted in (" " + allowed, allowed + "\u2002", " " * 201):
        box.fill(pasted)
        expect(box).to_have_value(pasted)
        with page.expect_navigation():
            form.get_by_role("button", name="Find", exact=True).click()
        expect(box).to_have_attribute("aria-invalid", "true")
        expect(box).to_have_value(pasted)
        expect(box).to_be_focused()
        expect(form.locator("#find-q_error")).to_contain_text("Use at most 200 characters.")
        expect(match).to_have_count(0)
        expect(page.get_by_role("heading", name="Results", exact=True)).to_have_count(0)
    box.fill(allowed)
    with page.expect_navigation():
        box.press("Enter")
    expect(box).to_have_value(lab.name)
    expect(match).to_be_visible()
    expect(form.locator("#find-q_error")).to_have_count(0)


@pytest.mark.parametrize("path", ["/clients/", "/requests/find/"])
def test_tapping_the_end_of_a_refused_search_allows_correction(new_context, demo, path):

    page = new_context(is_mobile=True, has_touch=True).new_page()
    page.set_viewport_size({"width": 390, "height": 844})
    sign_in(page, "demo_admin", demo.password)
    query = "0" * 201
    page.goto(path + "?q=" + query)
    form = page.locator(".vr-find" if path == "/requests/find/" else ".vr-grid-toolbar")
    field = form.get_by_role("searchbox")
    expect(field).to_have_attribute("aria-invalid", "true")
    field.focus()
    field.press("ControlOrMeta+a")
    field.press("ArrowRight")
    assert field.evaluate("el => el.selectionStart") == len(query)
    field.scroll_into_view_if_needed()
    # Tap the middle of the fourth-last digit, measured from the editable text's right edge.
    point = field.evaluate("""field => {
        const rect = field.getBoundingClientRect(), style = getComputedStyle(field);
        const canvas = document.createElement("canvas").getContext("2d");
        canvas.font = style.font;
        const digit = canvas.measureText("0").width;
        const inset = parseFloat(style.borderRightWidth) + parseFloat(style.paddingRight);
        const right = rect.right - inset;
        return {x: right - 3.5 * digit, y: rect.top + rect.height / 2};
    }""")
    page.touchscreen.tap(point["x"], point["y"])
    expect(field).to_have_value(query)
    assert field.evaluate("field => field.selectionStart") < len(query)


# Adopted with the clear button's width withdrawn (a follow-up that predates the workspace): the
# phone and touch variants expect the 44 px target.
WITHDRAWN = pytest.mark.xfail(
    strict=True, reason="Predates the workspace, a follow-up: the clear button is 32 px wide."
)


@pytest.mark.parametrize(
    "width,touch,theme",
    [
        pytest.param(390, False, "light", marks=WITHDRAWN),
        pytest.param(390, True, "dark", marks=WITHDRAWN),
        pytest.param(1024, True, "light", marks=WITHDRAWN),
        (1366, False, "dark"),
    ],
)
def test_search_clear_target_and_keyboard_across_pointer_modes(
    new_context, demo, width, touch, theme
):
    from test_documentation import open_help

    page = new_context(is_mobile=touch, has_touch=touch).new_page()
    page.emulate_media(color_scheme=theme)
    page.set_viewport_size({"width": width, "height": 844})
    assert page.evaluate("matchMedia('(pointer: coarse)').matches") == touch
    sign_in(page, "demo_admin", demo.password)

    def check(scope):
        field = scope.get_by_role("searchbox")
        field.fill("ordinary search")
        clear = scope.get_by_role("button", name="Clear text")
        expect(clear).to_be_visible()
        field.scroll_into_view_if_needed()
        clear_box = clear.bounding_box()
        field_box = field.bounding_box()
        if touch or width < 576:
            assert min(clear_box["width"], clear_box["height"]) >= 44
        assert field_box["x"] <= clear_box["x"]
        assert clear_box["x"] + clear_box["width"] <= field_box["x"] + field_box["width"] + 1
        assert field_box["y"] - 1 <= clear_box["y"]
        assert clear_box["y"] + clear_box["height"] <= field_box["y"] + field_box["height"] + 1
        # A long ordinary entry can be read up to the end without running under the clear glyph.
        padding = field.evaluate("el => parseFloat(getComputedStyle(el).paddingRight)")
        glyph = clear.locator("svg").bounding_box()
        assert field_box["x"] + field_box["width"] - padding <= glyph["x"]
        field.press("Tab")
        expect(clear).to_be_focused()
        clear.press("Enter")
        expect(field).to_have_value("")
        expect(field).to_be_focused()
        expect(clear).to_be_hidden()

    page.goto("/clients/")
    if width < 576:
        page.get_by_role("button", name="Filter and sort", exact=False).click()
    check(page.locator(".vr-grid-toolbar"))
    page.goto("/requests/find/")
    check(page.locator(".vr-find"))
    if width >= 576:
        check(page.locator(".vr-topbar .vr-lookup"))
    open_help(page)
    check(page.locator("#vr-documentation [data-vr-help-reference]"))
