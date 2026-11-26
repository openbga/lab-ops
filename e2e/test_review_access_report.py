"""Independent browser checks for the dense Access review lists and period form."""

import re
from datetime import timedelta

import pytest
from django.contrib.auth.models import Group, Permission
from django.utils import timezone
from helpers import field, sign_in
from playwright.sync_api import expect

from accounts.models import User
from accounts.roles import REVIEW_CAPABILITY, ROLE_LABELS, ROLES
from audit_review import access_report
from clients.models import Client, ClientSite, PortalAccess
from teams.models import Team, TeamMembership

pytestmark = pytest.mark.django_db(transaction=True)

ACCOUNTS = "/access-review/accounts/"
UNUSED = "/access-review/unused/"
PERIOD = UNUSED + "period/"
REVIEWER = "demo_supervisor"


@pytest.fixture
def dense(demo):
    old = timezone.now() - timedelta(days=200)
    User.objects.bulk_create(
        [
            User(
                username=f"unused-filler-{number:02}",
                email=f"filler-{number}@example.invalid",
                date_joined=old + timedelta(days=1),
                password="!",
            )
            for number in range(55)
        ]
    )
    staff = User.objects.create_user(
        "review-staff",
        "review-staff@example.invalid",
        first_name="A" * 148,
        last_name="N" * 149,
        date_joined=old,
    )
    staff.groups.set(Group.objects.filter(name__in=ROLES))
    staff.user_permissions.set(Permission.objects.filter(codename=REVIEW_CAPABILITY))
    for number in range(5):
        team = Team.objects.create(slug=f"review-team-{number}", name=f"Team{number}" + "T" * 70)
        TeamMembership.objects.create(team=team, user=staff, is_lead=True)
    organization = Client.objects.create(name="Org" + "C" * 155, account_number="DENSE")
    portal = User.objects.create_user(
        "review-portal",
        "review-portal@example.invalid",
        first_name="B" * 149,
        client=organization,
        date_joined=old,
    )
    grant = PortalAccess.objects.create(user=portal, client=organization)
    for number in range(7):
        grant.sites.add(
            ClientSite.objects.create(client=organization, name=f"Site{number}" + "S" * 150)
        )
    return staff, portal


def fits(surface):
    content, available = surface.evaluate("el => [el.scrollWidth, el.clientWidth]")
    assert content <= available + 1, (content, available)


def test_dense_accounts_and_unused_lists_at_each_size_and_theme(page, view, demo, dense):
    staff, portal = dense
    sign_in(page, REVIEWER, demo.password)
    page.goto(ACCOUNTS)
    for path in (ACCOUNTS, UNUSED):
        if path == UNUSED:
            page.get_by_role("navigation", name="Access review lists").get_by_role(
                "link", name="Unused accounts"
            ).click()
        expect(page.locator("html")).to_have_attribute("data-bs-theme", view.theme)
        table = page.locator("table.vr-table")
        rows = table.locator("tbody tr")
        expect(rows).to_have_count(50)
        staff_row = rows.filter(has_text=staff.username)
        portal_row = rows.filter(has_text=portal.username)
        expect(staff_row).to_contain_text(staff.get_full_name())
        expect(portal_row).to_contain_text(portal.client.name)
        for name in ROLE_LABELS.values():
            expect(staff_row).to_contain_text(name)
        expect(staff_row).to_contain_text("Can review access and audit records")
        if view.phone:
            assert rows.first.bounding_box()["y"] <= view.viewport["height"] - 64 - 80
        if path == ACCOUNTS and view.phone:
            page.get_by_role("button", name=re.compile("^Filter and sort")).click()
            page.get_by_role("button", name="Columns", exact=True).click()
            chooser = page.get_by_role("dialog", name="Columns")
            for label in ("Teams", "Portal access"):
                chooser.get_by_role("checkbox", name=label, exact=True).check()
            page.keyboard.press("Escape")
            expect(chooser).to_be_hidden()
            expect(page.get_by_role("button", name=re.compile("^Filter and sort"))).to_be_focused()
        if path == ACCOUNTS:
            for membership in staff.team_memberships.select_related("team"):
                expect(staff_row).to_contain_text(membership.team.name)
            for site in portal.portal_access.sites.all():
                expect(portal_row).to_contain_text(site.name)
        if view.phone:
            heading = table.locator("thead")
            if path == ACCOUNTS:
                expect(heading).to_be_hidden()
            else:
                # The unsortable header remains accessible to screen readers.
                assert heading.evaluate("el => getComputedStyle(el).clipPath") == "inset(50%)"
            assert staff_row.evaluate("el => getComputedStyle(el).display") != "table-row"
        else:
            expect(table.locator("thead")).to_be_visible()
        fits(page.locator("html"))
        fits(table.locator(".."))
        for sample in (staff_row, portal_row):
            for cell in sample.locator("th:visible, td:visible").all():
                fits(cell)
        page.get_by_role("link", name="Next page", exact=True).click()
        expect(rows.first).to_be_visible()
        assert 0 < rows.count() < 50
    assert page.console_errors == []


@pytest.mark.parametrize("use_dialog", [True, False], ids=["dialog", "full-page"])
def test_period_form_can_read_history_and_save_at_each_size_and_theme(page, view, demo, use_dialog):
    text = "R" * 3900
    reviewer = User.objects.get(username=REVIEWER)
    access_report.set_period(
        actor=reviewer, days=60, expected_revision=access_report.period().revision, reason=text
    )
    sign_in(page, REVIEWER, demo.password)
    if use_dialog:
        page.goto(UNUSED)
        page.get_by_role("link", name="Change the period").click()
        surface = page.get_by_role("dialog", name="Unused account period")
    else:
        # Retain theme initialization while exercising the server-rendered form without its
        # progressive dialog enhancement. Ordinary full-page navigation submits this form.
        page.route(
            "**/js/form-dialog.js*",
            lambda route: route.fulfill(status=200, content_type="application/javascript", body=""),
        )
        page.goto(PERIOD)
        surface = page.get_by_role("main")
    expect(page.locator("html")).to_have_attribute("data-bs-theme", view.theme)
    details = surface.locator("details", has_text="Changes to the period")
    details.locator("summary").click()
    expect(details.get_by_text(text, exact=True)).to_be_visible()
    fits(details)
    fits(page.locator("html"))
    expect(field(surface, "Unused after (days)")).to_have_value("60")
    field(surface, "Unused after (days)").fill("55")
    field(surface, "Reason").fill("Synthetic form layout check.")
    surface.get_by_role("button", name="Save period").click()
    expect(page.get_by_text("Unused account period saved.")).to_be_visible()
    assert access_report.period().days == 55
    assert page.console_errors == []


def test_plain_period_page_names_the_section_and_tab_in_its_breadcrumb(new_context, demo):
    tab = new_context(java_script_enabled=False).new_page()
    sign_in(tab, REVIEWER, demo.password)
    tab.goto(PERIOD)
    trail = tab.get_by_role("navigation", name="Breadcrumb")
    expect(trail.get_by_role("link", name="Unused accounts")).to_have_attribute("href", UNUSED)
    expect(trail.locator("[aria-current=page]")).to_have_text("Unused account period")
    labels = trail.locator("li").all_text_contents()
    main = tab.get_by_role("main")
    field(main, "Unused after (days)").fill("43")
    field(main, "Reason").fill("Synthetic full-page submission.")
    main.get_by_role("button", name="Save period").click()
    expect(tab.get_by_text("Unused account period saved.")).to_be_visible()
    assert access_report.period().days == 43
    assert labels == [
        "Access review",
        "Unused accounts",
        "Unused account period",
    ]
