"""Last-day fields, validation feedback, and report rows at both reference widths and themes.

Adopted from the independent review of feat/grant-end-dates, round 1 (report
.local/review-history/grant-ends-round-1.md; original archived beside it)."""

import re
import secrets
from datetime import timedelta

import pytest
from django.contrib.auth.models import Permission
from django.db import transaction
from django.urls import reverse
from helpers import field, make_user, sign_in
from playwright.sync_api import expect

from accounts.grant_ends import end_of, today
from accounts.models import CapabilityEnd, User
from clients.models import Client, ClientSite, PortalAccess

pytestmark = pytest.mark.django_db(transaction=True)

PATIENT = "Can view patient information"
REVIEW = "Can review access and audit records"
PAST_DAY_ERROR = re.compile(r"^Choose today or a later day\.")


@pytest.fixture
def operator(page):
    def enter(role, *, owner=False, reviewer=False):
        password = secrets.token_urlsafe(24)
        person = make_user("review-operator", password, role)
        if owner:
            person.is_staff = person.is_superuser = True
            person.save(update_fields=["is_staff", "is_superuser"])
        if reviewer:
            person.user_permissions.add(permission("review_audit_records"))
        sign_in(page, person.username, password)

    return enter


def permission(code):
    return Permission.objects.get(content_type__app_label="accounts", codename=code)


@pytest.fixture
def staff_grants():
    account = make_user("review-worker", None, "technical")
    yesterday = today() - timedelta(days=1)
    later = today() + timedelta(days=8)
    with transaction.atomic():
        for code, last_day in (
            ("view_patient_information", yesterday),
            ("review_audit_records", later),
        ):
            grant = permission(code)
            account.user_permissions.add(grant)
            CapabilityEnd.objects.create(user=account, permission=grant, ends_at=end_of(last_day))
    return account, yesterday, later


@pytest.fixture
def portal_grants():
    organization = Client.objects.create(name="Example Watershed", account_number="RVIEW")
    site = ClientSite.objects.create(client=organization, name="Intake station", code="INTAKE")
    yesterday = today() - timedelta(days=2)
    later = today() + timedelta(days=7)
    with transaction.atomic():
        for name in ("ended", "ending", "undated", "unset"):
            person = User.objects.create_user(
                username=name, email=f"{name}@example.invalid", client=organization
            )
            if name == "unset":
                continue
            last_day = {"ended": yesterday, "ending": later, "undated": None}[name]
            access = PortalAccess.objects.create(
                user=person,
                client=organization,
                whole_organization=name != "ending",
                ends_at=end_of(last_day),
            )
            if name == "ending":
                access.sites.add(site)
    return organization, yesterday, later


def horizontally_visible(element, width):
    expect(element).to_be_visible()
    box = element.bounding_box()
    assert box["x"] >= -1 and box["x"] + box["width"] <= width + 1, box


def fits_page(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert page.console_errors == []


def open_admin(page, operator, staff_grants):
    operator("admin", owner=True)
    person, _, _ = staff_grants
    page.goto(reverse("admin:accounts_user_change", args=[person.pk]))
    expect(page.get_by_role("heading", name="Capability end dates")).to_be_visible()
    return tuple(page.get_by_label(f"Last day of “{label}”") for label in (PATIENT, REVIEW))


def test_account_last_days_are_readable_and_touch_sized(page, view, operator, staff_grants):
    patient, review = open_admin(page, operator, staff_grants)
    _, yesterday, later = staff_grants
    expect(patient).to_have_value(yesterday.isoformat())
    expect(patient).to_have_accessible_description(re.compile(r"^Ended:"))
    expect(review).to_have_value(later.isoformat())
    expect(review).not_to_have_accessible_description(re.compile(r"^Ended:"))
    for control in (patient, review):
        horizontally_visible(control, view.viewport["width"])
    assert page.locator("html").evaluate("el => getComputedStyle(el).colorScheme") == view.theme
    fits_page(page)
    if view.phone:
        sizes = [
            (item.bounding_box()["width"], item.bounding_box()["height"])
            for item in (patient, review)
        ]
        assert all(width >= 44 and height >= 44 for width, height in sizes), sizes


def test_account_keeps_ended_guidance_when_another_day_is_refused(
    page, view, operator, staff_grants
):
    patient, review = open_admin(page, operator, staff_grants)
    person, yesterday, later = staff_grants
    expect(patient).to_have_accessible_description(re.compile(r"^Ended:"))
    review.fill(yesterday.isoformat())
    page.get_by_role("button", name="Save and continue editing").click()
    expect(page.get_by_text(PAST_DAY_ERROR)).to_be_visible()
    expect(review).to_have_value(yesterday.isoformat())
    expect(patient).to_have_value(yesterday.isoformat())
    assert CapabilityEnd.objects.get(
        user=person, permission=permission("review_audit_records")
    ).ends_at == end_of(later)
    fits_page(page)
    expect(patient).to_have_accessible_description(re.compile(r"^Ended:"))


def test_portal_rows_and_validation_then_clear_ended_access(page, view, portal_grants, operator):
    organization, yesterday, later = portal_grants
    operator("account_manager")
    page.goto(reverse("clients:client-detail", args=[organization.pk]))
    root = page.locator("html")
    expect(root).to_have_attribute("data-bs-theme", view.theme)

    panel = page.get_by_role("region", name=re.compile("^Portal accounts"))

    def scope_cell(username):
        row = panel.get_by_role("row").filter(has_text=f"{username}@example.invalid")
        return row.get_by_role("cell").first

    ended_scope = f"Only tickets it raised (access ended after {yesterday})"
    # A dated, undated, ended and never-saved grant coexist in the same Sees column.
    for name, expected in (
        ("ended", ended_scope),
        ("ending", f"Intake station Last day {later}"),
        ("undated", "The whole organization"),
        ("unset", "Only tickets it raised (access not set)"),
    ):
        cell = scope_cell(name)
        # Adjacent HTML elements separate the site and its date visually.
        expect(cell).to_have_text(expected, use_inner_text=True)
        horizontally_visible(cell, view.viewport["width"])
    fits_page(page)

    panel.get_by_role("link", name="Set access for ended", exact=True).click()
    expect(page.get_by_text("Access ended", exact=True)).to_be_visible()
    last_day = field(page, "Last day of this access (optional)")
    reason = field(page, "Reason for change")
    expect(last_day).to_have_value(yesterday.isoformat())
    horizontally_visible(last_day, view.viewport["width"])
    new_past = (yesterday - timedelta(days=1)).isoformat()
    last_day.fill(new_past)
    reason.fill("Resume organization access after renewal.")
    page.get_by_role("button", name="Save access").click()
    error = page.get_by_text(PAST_DAY_ERROR)
    expect(error).to_be_visible()
    expect(last_day).to_have_value(new_past)
    expect(last_day).to_be_focused()
    expect(last_day).to_have_accessible_description(re.compile(r"Choose today or a later day\."))
    expect(reason).to_have_value("Resume organization access after renewal.")
    horizontally_visible(error, view.viewport["width"])
    fits_page(page)

    last_day.fill("")
    page.get_by_role("button", name="Save access").click()
    expect(page.get_by_text("Portal access saved.")).to_be_visible()
    expect(scope_cell("ended")).to_have_text("The whole organization")
    assert PortalAccess.objects.get(user__username="ended").ends_at is None
    fits_page(page)


def test_access_report_shows_ended_and_current_grants(
    page, view, operator, portal_grants, staff_grants
):
    organization, portal_past, portal_future = portal_grants
    worker, staff_past, staff_future = staff_grants
    operator("supervisor", reviewer=True)
    page.goto(reverse("access_review:accounts"))
    expect(page.locator("html")).to_have_attribute("data-bs-theme", view.theme)
    table = page.get_by_role("table", name="Access review")
    staff_row = table.get_by_role("row").filter(has_text=worker.username)
    for item in (
        f"{PATIENT} (ended)",
        f"Directly, ended after {staff_past}, gives nothing",
        f"Directly, last day {staff_future}",
    ):
        horizontally_visible(staff_row.get_by_text(item), view.viewport["width"])

    if view.phone:
        page.get_by_role("button", name=re.compile("^Filter and sort")).click()
        page.get_by_role("button", name="Columns", exact=True).click()
        chooser = page.get_by_role("dialog", name="Columns")
        chooser.get_by_role("checkbox", name="Portal access", exact=True).check()
        page.keyboard.press("Escape")
        expect(chooser).to_be_hidden()
    for name, expected in (
        ("ended", f"Only the tickets it raised (access ended after {portal_past})"),
        ("ending", f"Tickets of Intake station, and those it raised, last day {portal_future}"),
        ("undated", "Its organization's tickets"),
        ("unset", "Only the tickets it raised"),
    ):
        row = table.get_by_role("row").filter(has=page.get_by_text(name, exact=True))
        expect(row).to_contain_text(organization.name)
        horizontally_visible(row.get_by_text(expected, exact=True), view.viewport["width"])
    fits_page(page)
