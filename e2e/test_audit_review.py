"""Audit review (/audit-review/): the demonstration supervisor, who holds the review capability,
opens the workspace from the sidebar, filters the access records, exports them and finds both uses
listed under Workspace use; on the People tab finds their own count of tickets opened, opens the
list it counted and exports the summary; at both widths and in both themes. Staff without the
capability are offered nothing and refused. Synthetic demonstration data only."""

import csv
import io
import re

import pytest
from helpers import contrast_failures, sign_in, theme_samples
from playwright.sync_api import Page, expect

from accounts.models import User
from audit_review.models import WorkspaceUse
from desk.portal_scope import portal_requests

pytestmark = pytest.mark.django_db(transaction=True)

REVIEWER = "demo_supervisor"


def rows(page: Page, name: str):
    return page.get_by_role("table", name=name).locator("tbody tr")


def no_sideways_scroll(page: Page):
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
        "the page scrolls sideways"
    )


def test_a_reviewer_filters_exports_and_sees_the_use_recorded(page: Page, demo, view):
    ticket = portal_requests(User.objects.get(username="demo_client")).order_by("pk").first()
    assert ticket is not None, "the demonstration seed gives the portal account a ticket"
    sign_in(page, REVIEWER, demo.password)
    page.goto(f"/requests/{ticket.pk}/")  # an access record to review
    expect(page.get_by_role("heading", level=1)).to_contain_text(ticket.number)

    if view.phone:
        page.goto("/audit-review/changes/")
    else:
        page.locator("nav.vr-sidenav").get_by_role("link", name="Audit review").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Audit review")
    expect(rows(page, "Audit review").first).to_be_visible()
    no_sideways_scroll(page)
    assert not contrast_failures(theme_samples(page))
    page.screenshot(path=f"/tmp/labops-aud-shots/changes-{view.viewport['width']}-{view.theme}.png")

    tabs = page.get_by_role("navigation", name="Audit review lists")
    tabs.get_by_role("link", name="Access records").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Audit review: access records")
    page.goto(f"/audit-review/access/?ticket={ticket.number}&action=ticket")
    table = "Audit review: access records"
    expect(rows(page, table).first).to_contain_text("Opened the ticket")
    expect(rows(page, table).first).to_contain_text(ticket.number)
    expect(rows(page, table).first).to_contain_text(REVIEWER)
    no_sideways_scroll(page)
    page.screenshot(
        path=f"/tmp/labops-aud-shots/access-{view.viewport['width']}-{view.theme}.png",
        full_page=True,
    )

    with page.expect_download() as download:
        page.get_by_role("link", name="Export CSV").click()
    path = download.value.path()
    text = open(path, encoding="utf-8-sig").read()
    header, *lines = list(csv.reader(io.StringIO(text)))
    assert header[:4] == ["When", "Person", "Username", "What"]
    assert lines and all(line[6] == ticket.number for line in lines)
    assert download.value.suggested_filename.startswith("audit-review-access-")

    tabs = page.get_by_role("navigation", name="Audit review lists")
    tabs.get_by_role("link", name="Workspace use").click()
    table = "Audit review: workspace use"
    expect(rows(page, table).first).to_contain_text("Exported")
    expect(rows(page, table).first).to_contain_text(f"Ticket: {ticket.number}")
    expect(rows(page, table).nth(1)).to_contain_text("Viewed a page")
    assert WorkspaceUse.objects.filter(kind="exported", source="access").count() == 1
    no_sideways_scroll(page)
    assert not contrast_failures(theme_samples(page))


def test_the_people_tab_counts_what_a_person_did_and_opens_the_rows_it_counted(
    page: Page, demo, view
):
    tickets = list(portal_requests(User.objects.get(username="demo_client")).order_by("pk")[:2])
    assert len(tickets) == 2, "the demonstration seed gives the portal account two tickets"
    sign_in(page, REVIEWER, demo.password)
    for ticket in tickets:  # two access records to count
        page.goto(f"/requests/{ticket.pk}/")
        expect(page.get_by_role("heading", level=1)).to_contain_text(ticket.number)

    page.goto("/audit-review/changes/")
    page.get_by_role("navigation", name="Audit review lists").get_by_role(
        "link", name="People"
    ).click()
    table = "Audit review: people"
    expect(page.get_by_role("heading", level=1)).to_have_text(table)
    expect(page.locator(".vr-page-intro")).to_contain_text("the last 30 days")
    mine = rows(page, table).filter(has_text=REVIEWER)
    expect(mine).to_have_count(1)
    opened = mine.get_by_role("link", name=re.compile(r"^2 tickets opened by "))
    expect(opened).to_be_visible()
    no_sideways_scroll(page)
    assert not contrast_failures(theme_samples(page))

    opened.click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Audit review: access records")
    listed = rows(page, "Audit review: access records")
    expect(listed).to_have_count(2)
    for ticket in tickets:
        expect(listed.filter(has_text=ticket.number)).to_have_count(1)
    page.go_back()

    with page.expect_download() as download:
        page.get_by_role("link", name="Export CSV").click()
    text = open(download.value.path(), encoding="utf-8-sig").read()
    header, *lines = list(csv.reader(io.StringIO(text)))
    assert header[:5] == ["From", "To", "Person", "Username", "Client organization"]
    [line] = [line for line in lines if line[3] == REVIEWER]
    assert line[header.index("Tickets opened")] == "2"
    assert download.value.suggested_filename.startswith("audit-review-people-")
    kinds = list(
        WorkspaceUse.objects.filter(source="people").order_by("id").values_list("kind", flat=True)
    )
    assert kinds[0] == "viewed" and kinds[-1] == "exported", kinds


def test_a_phone_shows_the_rows_first_and_the_filters_behind_one_button(page: Page, demo):
    page.set_viewport_size({"width": 390, "height": 844})
    sign_in(page, REVIEWER, demo.password)
    page.goto("/audit-review/sign-ins/")
    row = rows(page, "Audit review: sign-ins").first
    expect(row).to_be_visible()
    assert row.bounding_box()["y"] < 844 - 64 - 80, "the first row starts below the first screen"
    button = page.get_by_role("button", name=re.compile(r"^Filter and sort"))
    expect(button).to_be_visible()
    button.click()
    sheet = page.get_by_role("dialog", name="Filter and sort")
    expect(sheet.locator("label", has_text="Person")).to_be_visible()
    page.screenshot(path="/tmp/labops-aud-shots/phone-sheet.png")
    expect(sheet.get_by_role("button", name="Apply")).to_be_visible()
    no_sideways_scroll(page)


def test_staff_without_the_capability_are_offered_nothing_and_refused(page: Page, demo):
    sign_in(page, "demo_client_services", demo.password)
    page.goto("/")
    expect(page.locator("nav.vr-sidenav").get_by_role("link", name="Audit review")).to_have_count(0)
    for address in ("/audit-review/changes/", "/audit-review/access/export/"):
        response = page.goto(address)
        assert response is not None and response.status == 403, address
    assert not WorkspaceUse.objects.exists()
