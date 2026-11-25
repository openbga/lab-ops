"""Rendered People and Quality summary tables in the four supported browser views."""

import re
from itertools import pairwise

import pytest
from helpers import contrast_failures, field, pick, sign_in, theme_samples
from playwright.sync_api import expect

from desk.models import ServiceRequest
from service_levels.models import BusinessCalendar

pytestmark = pytest.mark.django_db(transaction=True)


def assert_totals(page, table, view, title):
    footer = table.locator("tfoot tr")
    expect(footer.get_by_role("rowheader")).to_have_text(title)
    headings = table.locator("thead tr").last.locator("th").all_text_contents()
    headings = [" ".join(text.split()) for text in headings[1:]]
    amounts = footer.locator("td")
    expect(amounts).to_have_count(len(headings))
    sums = [0] * len(headings)
    for row in table.locator("tbody tr").all():
        values = [int(value.strip()) for value in row.locator("td").all_text_contents()]
        sums = [left + right for left, right in zip(sums, values, strict=True)]
    assert sums == [int(cell.strip()) for cell in amounts.all_text_contents()]

    boxes = [cell.bounding_box() for cell in amounts.all()]
    assert all(box and box["width"] > 0 and box["height"] > 0 for box in boxes)
    assert page.evaluate("document.documentElement.scrollWidth") <= view.viewport["width"]
    if view.phone:
        for i, cell in enumerate(amounts.all()):
            printed = cell.evaluate("el => getComputedStyle(el, '::before').content")
            assert printed.strip('"') == headings[i]
        for above, below in pairwise(boxes):
            assert above["y"] + above["height"] <= below["y"] + 1
        assert boxes[0]["x"] >= 0 and boxes[0]["x"] + boxes[0]["width"] <= view.viewport["width"]
    else:
        for i, heading in enumerate(table.locator("thead tr").last.locator("th").all()[1:]):
            box = heading.bounding_box()
            assert abs(box["x"] - boxes[i]["x"]) <= 1
            assert abs(box["width"] - boxes[i]["width"]) <= 1
    assert not contrast_failures(theme_samples(page))


def test_people_filter_and_column_choices_keep_visible_totals_aligned(page, demo, view):
    saturday = BusinessCalendar.objects.create(
        name="Weekend review", hours={"sat": [["10:00", "12:00"]]}
    )
    sign_in(page, "demo_supervisor", demo.password)
    ticket = ServiceRequest.objects.order_by("pk").first()
    assert page.goto(f"/requests/{ticket.pk}/").status == 200
    page.goto("/audit-review/people/")
    table = page.get_by_role("table", name="Audit review: people")
    expect(table.locator("tbody tr").first).to_be_visible()
    assert_totals(page, table, view, "Everyone listed")

    opener = page.get_by_role("button", name=re.compile("^Filter and sort"))
    toolbar = page.get_by_role("search", name="Filter people")
    if view.phone:
        opener.click()
        sheet = page.get_by_role("dialog", name="Filter and sort")
        expect(sheet).to_be_visible()
        expect(sheet.get_by_role("heading", name="Sort by")).to_be_visible()
        assert sheet.bounding_box()["height"] <= view.viewport["height"] * 0.85 + 1
    else:
        expect(opener).to_be_hidden()
    expect(field(toolbar, "From")).to_be_visible()
    expect(field(toolbar, "To")).to_be_visible()
    toolbar.get_by_role("button", name="More filters").click()
    expect(field(toolbar, "Person")).to_be_visible()
    expect(field(toolbar, "Client organization")).to_be_visible()
    pick(field(toolbar, "Business hours"), label=saturday.name)
    toolbar.get_by_role("button", name="Apply").click()
    expect(page).to_have_url(re.compile(rf"[?&]calendar={saturday.pk}(&|$)"))
    expect(page.locator(".vr-page-intro")).to_contain_text(saturday.name)
    assert_totals(page, table, view, "Everyone listed")

    if view.phone:
        opener.click()
    toolbar.get_by_role("button", name="Columns").click()
    columns = page.get_by_role("dialog", name="Columns")
    columns.get_by_role("checkbox", name="Changes made").uncheck()
    columns.get_by_role("button", name="Move Tickets opened up").click()
    page.keyboard.press("Escape")
    expect(columns).to_be_hidden()
    expect(table.locator("thead")).not_to_contain_text("Changes made")
    labels = table.locator("thead th").all_text_contents()
    assert next(i for i, text in enumerate(labels) if "Tickets opened" in text) < next(
        i for i, text in enumerate(labels) if "Failed or refused sign-ins" in text
    )
    assert_totals(page, table, view, "Everyone listed")


def test_business_hours_menu_offers_not_judged(page, demo, view):
    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/audit-review/people/")
    if view.phone:
        page.get_by_role("button", name=re.compile("^Filter and sort")).click()
    toolbar = page.get_by_role("search", name="Filter people")
    toolbar.get_by_role("button", name="More filters").click()
    field(toolbar, "Business hours").click()
    expect(page.get_by_role("option", name="Not judged", exact=True)).to_be_visible()


def test_quality_summary_keeps_month_totals_aligned(page, demo, view):
    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/quality/summary/")
    table = page.get_by_role("table", name=re.compile("^By month and source"))
    expect(table.locator("tbody tr")).to_have_count(12)
    assert_totals(page, table, view, "Twelve months")


@pytest.mark.parametrize(
    ("path", "link_selector"),
    [
        ("/audit-review/people/", "tbody td > a"),
        ("/quality/summary/", "tfoot td > a"),
    ],
)
def test_phone_count_links_have_full_size_touch_targets(page, demo, path, link_selector):
    page.set_viewport_size({"width": 390, "height": 844})
    sign_in(page, "demo_supervisor", demo.password)
    page.goto(path)
    links = page.locator(f"table.vr-table {link_selector}")
    expect(links.first).to_be_visible()
    targets = [(link.get_attribute("aria-label"), link.bounding_box()) for link in links.all()]
    too_small = [
        (label, round(box["width"], 1), round(box["height"], 1))
        for label, box in targets
        if min(box["width"], box["height"]) < 43.5
    ]
    assert not too_small, too_small


def test_saved_outside_column_choice_survives_an_unjudged_period(page, demo):
    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/audit-review/people/")

    def hide(column):
        page.get_by_role("button", name="Columns", exact=True).click()
        dialog = page.get_by_role("dialog", name="Columns", exact=True)
        with page.expect_response(
            lambda reply: reply.url.endswith("/preferences/") and reply.request.method == "POST"
        ) as saved:
            dialog.get_by_role("checkbox", name=column, exact=True).uncheck()
        assert saved.value.ok
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()

    outside = "Outside business hours"
    hide(outside)
    page.reload()
    headings = page.locator("table thead")
    expect(headings).not_to_contain_text(outside)
    page.goto("/audit-review/people/?calendar=none")
    hide("Files downloaded")
    page.goto("/audit-review/people/")
    expect(headings).not_to_contain_text("Files downloaded")
    expect(headings).not_to_contain_text(outside)
