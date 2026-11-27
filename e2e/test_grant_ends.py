"""Last days of access grants, in the browser: an account manager sets a last day on a client portal
account's access (a past day refused on the field with what was typed kept; a later day saved and
shown on the organization's page, at every width and theme), reads the page of access whose end has
passed, a superuser sets the last day of a capability given directly on an account's page in the
admin console (and is refused one for a capability not given directly), and the access report
shows the last day. Synthetic demonstration data only."""

import re
from datetime import timedelta

import pytest
from conftest import View
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from helpers import field, sign_in
from playwright.sync_api import Page, expect

from accounts.grant_ends import end_of, today
from accounts.models import CapabilityEnd

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()
PORTAL_ACCOUNT = "demo_client_water"
STAFF = "demo_technical"
# Set access's own wording (accounts.grant_ends.PORTAL_PAST_DAY), as the whole text of an element:
# the page help quotes it too.
PAST_DAY = re.compile(r"^Choose today or a later day\.$")
NOT_DIRECT = (
    "Give this capability to the account directly (under User permissions), or clear its last day."
)
PATIENT_FIELD = "last_day_view_patient_information"
REVIEW_FIELD = "last_day_review_audit_records"


def day_text(day) -> str:
    return day.strftime("%Y-%m-%d")


def no_sideways_scroll(page: Page):
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), (
        "the page scrolls sideways"
    )


def water():
    from clients.models import Client

    return Client.objects.get(account_number="EXWATER")


def portal_row(page: Page):
    person = User.objects.get(username=PORTAL_ACCOUNT)
    return page.get_by_role("row").filter(has_text=person.get_full_name() or PORTAL_ACCOUNT)


def open_set_access(page: Page):
    page.goto(f"/clients/{water().pk}/")
    portal_row(page).get_by_role("link", name=re.compile("^Set access")).click()
    expect(page.get_by_role("button", name="Save access")).to_be_visible()


def make_owner(demo):
    owner = User.objects.get(username="demo_admin")
    owner.is_staff = owner.is_superuser = True
    owner.save(update_fields=["is_staff", "is_superuser"])
    return owner


def permission(codename):
    return Permission.objects.get(content_type__app_label="accounts", codename=codename)


def test_set_a_last_day_on_portal_access(page: Page, demo, view: View):
    from clients.models import PortalAccess

    sign_in(page, "demo_account_manager", demo.password)
    open_set_access(page)
    last_day = field(page, "Last day of this access (optional)")
    # controls.js turns the date input into a text field with a picker (YYYY-MM-DD).
    expect(last_day).to_have_attribute("data-vr-date", "date")
    seeded = PortalAccess.objects.get(user__username=PORTAL_ACCOUNT).ends_at  # the demo's own

    yesterday = today() - timedelta(days=1)
    last_day.fill(day_text(yesterday))
    field(page, "Reason for change").fill("Synthetic: contract ends")
    page.get_by_role("button", name="Save access").click()
    expect(page.get_by_text(PAST_DAY)).to_be_visible()
    expect(last_day).to_have_value(day_text(yesterday))
    expect(field(page, "Reason for change")).to_have_value("Synthetic: contract ends")
    expect(last_day).to_have_attribute("aria-invalid", "true")
    if view.phone:
        for target in (last_day, page.get_by_text(PAST_DAY)):
            target.scroll_into_view_if_needed()
            box = target.bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= view.viewport["width"] + 1
        no_sideways_scroll(page)
    assert PortalAccess.objects.get(user__username=PORTAL_ACCOUNT).ends_at == seeded

    later = today() + timedelta(days=30)
    last_day.fill(day_text(later))
    page.get_by_role("button", name="Save access").click()
    expect(page.get_by_text("Portal access saved.")).to_be_visible()
    expect(portal_row(page)).to_contain_text(f"Last day {day_text(later)}")
    access = PortalAccess.objects.get(user__username=PORTAL_ACCOUNT)
    assert access.ends_at == end_of(later)
    if view.phone:
        no_sideways_scroll(page)
    assert page.console_errors == []


def test_ended_portal_access_is_shown_as_ended(page: Page, demo):
    from clients.models import PortalAccess

    access = PortalAccess.objects.get(user__username=PORTAL_ACCOUNT)
    ended_day = today() - timedelta(days=3)
    access.ends_at = end_of(ended_day)
    with transaction.atomic():  # setting an end takes the account's access gate
        access.save(update_fields=["ends_at"])

    sign_in(page, "demo_account_manager", demo.password)
    page.goto(f"/clients/{water().pk}/")
    expect(portal_row(page)).to_contain_text(
        f"Only tickets it raised (access ended after {day_text(ended_day)})"
    )
    portal_row(page).get_by_role("link", name=re.compile("^Set access")).click()
    expect(page.get_by_text("Access ended", exact=True)).to_be_visible()
    expect(field(page, "Last day of this access (optional)")).to_have_value(day_text(ended_day))
    assert page.console_errors == []


def test_a_superuser_sets_the_last_day_of_a_capability_given_directly(page: Page, demo, view: View):
    if view.phone != (view.theme == "dark"):
        pytest.skip("run at desktop light and phone dark")
    owner = make_owner(demo)
    person = User.objects.get(username=STAFF)
    person.user_permissions.add(permission("view_patient_information"))
    sign_in(page, owner.username, demo.password)
    url = reverse("admin:accounts_user_change", args=[person.pk])
    page.goto(url)
    expect(page.get_by_role("heading", name="Capability end dates")).to_be_visible()
    patient = page.get_by_label("Last day of “Can view patient information”")
    expect(patient).to_have_attribute("name", PATIENT_FIELD)
    expect(patient).to_have_value("")

    last_day = today() + timedelta(days=10)
    patient.fill(day_text(last_day))
    page.get_by_role("button", name="Save and continue editing").click()
    expect(page.get_by_text(re.compile("was changed successfully"))).to_be_visible()
    end = CapabilityEnd.objects.get(user=person, permission=permission("view_patient_information"))
    assert end.ends_at == end_of(last_day)
    page.goto(url)
    expect(page.get_by_label("Last day of “Can view patient information”")).to_have_value(
        day_text(last_day)
    )

    # A day for a capability that is not given directly is refused, and what was typed is kept.
    review = page.get_by_label("Last day of “Can review access and audit records”")
    expect(review).to_have_attribute("name", REVIEW_FIELD)
    review.fill(day_text(last_day))
    page.get_by_role("button", name="Save and continue editing").click()
    expect(page.get_by_text(NOT_DIRECT)).to_be_visible()
    expect(review).to_have_value(day_text(last_day))
    assert not CapabilityEnd.objects.filter(
        user=person, permission=permission("review_audit_records")
    ).exists()
    if view.phone:
        no_sideways_scroll(page)
    assert page.console_errors == []


def test_the_access_report_shows_the_last_day_of_a_direct_grant(page: Page, demo):
    person = User.objects.get(username=STAFF)
    person.user_permissions.add(permission("view_patient_information"))
    last_day = today() + timedelta(days=10)
    with transaction.atomic():  # a change of an end takes the account's access gate
        CapabilityEnd.objects.create(
            user=person,
            permission=permission("view_patient_information"),
            ends_at=end_of(last_day),
        )
    assert timezone.now() < end_of(last_day)

    sign_in(page, "demo_supervisor", demo.password)
    page.goto(reverse("access_review:accounts"))
    expect(page.get_by_role("heading", level=1)).to_contain_text("Access review")
    page.get_by_role("searchbox").first.fill(STAFF)
    row = page.locator("tr, li, article").filter(has_text=STAFF).last
    expect(row.get_by_text(f"Directly, last day {day_text(last_day)}")).to_be_visible()
    assert page.console_errors == []
