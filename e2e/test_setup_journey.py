"""An administrator loads ticket category presets,
adds a business calendar with typed opening hours and a holiday, a service target counting in
it, and an external system. Run at desktop and phone widths in both themes. A phone sweep checks
every setup page for sideways scrolling and 44 px targets."""

import re

import pytest
from conftest import View
from helpers import field, make_user, pick, sign_in
from playwright.sync_api import Page, expect
from test_phone_layout import PHONES, check

pytestmark = pytest.mark.django_db(transaction=True)


def admin(demo):
    make_user("e2e_setup", demo.password, "admin")
    return "e2e_setup"


def primary(page: Page, view: View, name: str):
    """The page's primary action: in the thumb bar on a phone, in the header otherwise."""
    link = page.get_by_role("link", name=name).first
    if view.phone:
        box = link.bounding_box()
        assert box["y"] > view.viewport["height"] * 0.75 and box["height"] >= 44
    return link


def test_set_up_categories_calendar_target_and_system(page: Page, demo, view: View):
    from links.models import ExternalSystem
    from service_levels.models import BusinessCalendar, ServiceTarget

    sign_in(page, admin(demo), demo.password)

    # Ticket categories: the demo already loaded every preset; loading again adds nothing.
    page.goto("/setup/categories/")
    expect(page.get_by_role("heading", level=1, name="Ticket categories")).to_be_visible()
    expect(page.get_by_role("heading", name="Supplies and logistics")).to_be_visible()
    expect(page.get_by_role("link", name="Bottle kit order")).to_be_visible()
    page.get_by_role("link", name="Load presets").click()
    expect(page.get_by_role("heading", level=1, name="Load presets")).to_be_visible()
    page.get_by_label("Biorepository").check()
    page.get_by_role("button", name="Load presets").click()
    expect(
        page.get_by_text("Nothing new: everything in these presets is already here.")
    ).to_be_visible()

    # A calendar with typed hours, as a phone user would type them.
    page.goto("/setup/service-levels/calendars/")
    primary(page, view, "New business calendar").click()
    dialog = page.get_by_role("dialog")
    form = dialog if dialog.count() else page
    field(form, "Calendar name").fill("E2E night desk")
    field(form, "Monday").fill("7-19")
    field(form, "Tuesday").fill("22:00-08:00")
    form.get_by_role("button", name="Create calendar").click()
    expect(page.get_by_text("ends before it starts")).to_be_visible()
    field(
        page.get_by_role("dialog") if page.get_by_role("dialog").count() else page, "Tuesday"
    ).fill("00:00-06:00, 22:00-24:00")
    (page.get_by_role("dialog") if page.get_by_role("dialog").count() else page).get_by_role(
        "button", name="Create calendar"
    ).click()
    expect(page.get_by_role("heading", level=1, name="E2E night desk")).to_be_visible()
    calendar = BusinessCalendar.objects.get(name="E2E night desk")
    assert calendar.hours == {
        "mon": [["07:00", "19:00"]],
        "tue": [["00:00", "06:00"], ["22:00", "24:00"]],
    }
    page.get_by_role("link", name="Add holiday").click()
    scope = page.get_by_role("dialog") if page.get_by_role("dialog").count() else page
    field(scope, "Date").fill("2026-12-24")
    field(scope, "Holiday name").fill("Example closure")
    scope.get_by_role("button", name="Add holiday").click()
    expect(page.locator("td").get_by_text("Example closure", exact=True)).to_be_visible()

    # A target on that calendar.
    page.goto("/setup/service-levels/")
    expect(page.get_by_role("heading", level=1, name="Service targets")).to_be_visible()
    primary(page, view, "New service target").click()
    scope = page.get_by_role("dialog") if page.get_by_role("dialog").count() else page
    field(scope, "Target name").fill("E2E urgent night")
    pick(field(scope, "Priority"), label="Urgent")
    field(scope, "First-response time").fill("15")
    pick(field(scope, "Unit").first, label="minutes")
    field(scope, "Resolution time").fill("2")
    pick(field(scope, "Calendar"), label="E2E night desk")
    scope.get_by_role("button", name="Create target").click()
    expect(page.get_by_text("already covers this category and priority")).to_be_visible()
    # The demo's urgent target covers "any category, Urgent": narrow this one to a group.
    scope = page.get_by_role("dialog") if page.get_by_role("dialog").count() else page
    pick(field(scope, "Category or group"), label="Status and results (whole group)")
    scope.get_by_role("button", name="Create target").click()
    expect(page.get_by_role("link", name="E2E urgent night")).to_be_visible()
    target = ServiceTarget.objects.get(name="E2E urgent night")
    assert (target.first_response_minutes, target.resolution_minutes) == (15, 120)
    assert target.calendar == calendar

    # An external system: https only.
    page.goto("/setup/systems/")
    primary(page, view, "New external system").click()
    scope = page.get_by_role("dialog") if page.get_by_role("dialog").count() else page
    field(scope, "System name").fill("E2E Reference Lab Portal")
    pick(field(scope, "Kind"), label="Other")
    field(scope, "Base address").fill("http://partner.example.invalid/")
    scope.get_by_role("button", name="Create system").click()
    expect(page.get_by_text("Use an https:// address.")).to_be_visible()
    scope = page.get_by_role("dialog") if page.get_by_role("dialog").count() else page
    field(scope, "Base address").fill("https://partner.example.invalid/")
    scope.get_by_role("button", name="Create system").click()
    expect(page.get_by_role("link", name="E2E Reference Lab Portal")).to_be_visible()
    assert (
        ExternalSystem.objects.get(name="E2E Reference Lab Portal").key
        == "e2e-reference-lab-portal"
    )
    assert not page.console_errors


SLICE_C_PAGES = [
    "/setup/categories/",
    "/setup/categories/?show=all",
    "/setup/categories/new/",
    "/setup/categories/groups/new/",
    "/setup/categories/{category}/edit/",
    "/setup/categories/presets/",
    "/setup/service-levels/",
    "/setup/service-levels/targets/new/",
    "/setup/service-levels/targets/{target}/edit/",
    "/setup/service-levels/calendars/",
    "/setup/service-levels/calendars/new/",
    "/setup/service-levels/calendars/{calendar}/",
    "/setup/service-levels/calendars/{calendar}/edit/",
    "/setup/service-levels/calendars/{calendar}/holidays/new/",
    "/setup/service-levels/holidays/{holiday}/remove/",
    "/setup/systems/",
    "/setup/systems/new/",
    "/setup/systems/{system}/edit/",
    "/links/clients/{client}/new/",
    "/links/attachments/{attachment}/remove/",
    "/links/{link}/",
    "/links/{link}/edit/",
    "/links/find-person/",
]


@pytest.mark.parametrize("phone", PHONES, ids=lambda size: f"{size['width']}px")
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_setup_pages_fit_a_phone(page: Page, demo, phone, theme):
    from clients.models import Client
    from links.models import LinkAttachment
    from service_levels.models import BusinessCalendar, ServiceTarget
    from taxonomy.models import RequestCategory

    page.set_viewport_size(phone)
    page.emulate_media(color_scheme=theme)
    calendar = BusinessCalendar.objects.get()
    attachment = LinkAttachment.objects.filter(client__isnull=False).first()
    ids = {
        "category": RequestCategory.objects.get(key="bottle-kit-order").pk,
        "target": ServiceTarget.objects.first().pk,
        "calendar": calendar.pk,
        "holiday": calendar.holidays.first().pk,
        "system": attachment.link.system_id,
        "client": Client.objects.get(account_number="EXCLIN").pk,
        "attachment": attachment.pk,
        "link": attachment.link_id,
    }
    sign_in(page, admin(demo), demo.password)
    problems = []
    for url in SLICE_C_PAGES:
        url = url.format(**ids)
        response = page.goto(url)
        assert response.status == 200, url
        check(page, url, problems)
    assert not problems, "\n".join(problems)


def test_categories_reorder_and_edit_with_reason(page: Page, demo, view: View):
    from taxonomy.models import RequestCategory

    sign_in(page, admin(demo), demo.password)
    page.goto("/setup/categories/")
    group = page.locator("#group-" + str(RequestCategory.objects.get(key="supplies-logistics").pk))
    row = group.get_by_role("row").filter(
        has=page.get_by_role("link", name="Pickup or missed pickup")
    )
    row.get_by_role("button", name="Move").click()
    row.get_by_role("button", name="Move up").click()
    expect(page).to_have_url(re.compile(r"#group-"))
    keys = list(
        RequestCategory.objects.filter(parent__key="supplies-logistics", is_active=True)
        .order_by("position")
        .values_list("key", flat=True)
    )
    assert keys.index("pickup") < keys.index("supply-order")

    page.get_by_role("link", name="Bottle kit order").click()
    scope = page.get_by_role("dialog") if page.get_by_role("dialog").count() else page
    scope.get_by_label(re.compile("^Restricted")).check()
    scope.get_by_role("button", name="Save changes").click()
    expect(
        page.get_by_text("A reason is required.").or_(page.get_by_text("This field is required."))
    ).to_be_visible()
    scope = page.get_by_role("dialog") if page.get_by_role("dialog").count() else page
    field(scope, "Reason for change").fill("Supplies go through the quality team for this demo")
    scope.get_by_role("button", name="Save changes").click()
    expect(page.get_by_text("Changes recorded with their reason.")).to_be_visible()
    assert RequestCategory.objects.get(key="bottle-kit-order").is_restricted
