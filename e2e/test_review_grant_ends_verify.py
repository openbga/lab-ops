"""Save the days drawn on real forms through a validation error and another submission."""

import re
from datetime import timedelta

import pytest
from django.db import transaction
from django.urls import reverse
from helpers import field
from playwright.sync_api import expect
from test_review_grant_ends import fits_page, open_admin, permission
from test_review_grant_ends import operator as operator
from test_review_grant_ends import portal_grants as portal_grants
from test_review_grant_ends import staff_grants as staff_grants

from accounts.grant_ends import end_of
from accounts.models import CapabilityEnd
from clients.models import PortalAccess

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def initial_lab_zone(settings):
    settings.TIME_ZONE = "Etc/UTC"


def hidden_day(visible_day, name):
    return visible_day.locator("xpath=ancestor::form").locator(f'input[type=hidden][name="{name}"]')


def account_end(person, code):
    return CapabilityEnd.objects.get(user=person, permission=permission(code)).ends_at


def open_portal(page, operator, organization, username):
    operator("account_manager")
    page.goto(reverse("clients:client-detail", args=[organization.pk]))
    page.get_by_role("link", name=f"Set access for {username}", exact=True).click()
    if username == "ended":
        expect(page.get_by_text("Access ended", exact=True)).to_be_visible()
    return field(page, "Last day of this access (optional)")


def choose_intake_site(page):
    page.get_by_role("radio", name="Listed sites only").check()
    page.get_by_text("Intake station (INTAKE)", exact=True).click()


def test_admin_keeps_one_drawn_day_and_saves_the_other_as_corrected_in_new_zone(
    page, view, operator, staff_grants, settings
):
    person, past, future = staff_grants
    original = account_end(person, "view_patient_information")
    original_review = account_end(person, "review_audit_records")
    patient, review = open_admin(page, operator, staff_grants)
    shown_patient = hidden_day(patient, "shown_last_day_view_patient_information")
    shown_review = hidden_day(review, "shown_last_day_review_audit_records")
    expect(patient).to_have_value(past.isoformat())
    expect(review).to_have_value(future.isoformat())
    expect(shown_patient).to_have_value(past.isoformat())
    expect(shown_review).to_have_value(future.isoformat())
    assert end_of(past) == original

    settings.TIME_ZONE = "Asia/Tokyo"
    review.fill("9999-12-31")
    submit = page.get_by_role("button", name="Save and continue editing")
    submit.click()
    expect(
        page.get_by_text(
            "Choose a day within the next 100 years, or leave it empty for no end.", exact=True
        )
    ).to_be_visible()
    expect(review).to_have_value("9999-12-31")
    expect(patient).to_have_accessible_description(re.compile(r"^Ended:"))
    expect(shown_patient).to_have_value(past.isoformat())
    expect(shown_review).to_have_value(future.isoformat())
    assert account_end(person, "review_audit_records") == original_review
    if view.phone:
        assert all(control.bounding_box()["height"] >= 44 for control in (patient, review))

    # This different day happens to be what the original moment would now print in Tokyo.
    # It was actually chosen, so its end must be recalculated there.
    corrected = future + timedelta(days=1)
    review.fill(corrected.isoformat())
    submit.click()
    expect(page.get_by_text(re.compile("was changed successfully"))).to_be_visible()
    assert account_end(person, "view_patient_information") == original
    assert account_end(person, "review_audit_records") == end_of(corrected) != original_review
    expect(patient).to_have_value((past + timedelta(days=1)).isoformat())
    expect(review).to_have_value(corrected.isoformat())
    fits_page(page)


def test_portal_keeps_drawn_end_after_date_error_zone_change_and_scope_edit(
    page, view, operator, portal_grants, settings
):
    organization, past, _ = portal_grants
    access = PortalAccess.objects.get(user__username="ended")
    original = access.ends_at
    last_day = open_portal(page, operator, organization, "ended")
    shown = hidden_day(last_day, "shown_last_day")
    expect(last_day).to_have_value(past.isoformat())
    expect(shown).to_have_value(past.isoformat())
    settings.TIME_ZONE = "Asia/Tokyo"
    choose_intake_site(page)
    reason = field(page, "Reason for change")
    reason.fill("Keep the scheduled end while changing the saved site.")
    wrong = (past - timedelta(days=2)).isoformat()
    last_day.fill(wrong)
    save = page.get_by_role("button", name="Save access")
    save.click()
    form = last_day.locator("xpath=ancestor::form")
    expect(form.get_by_text("Choose today or a later day.", exact=True)).to_be_visible()
    expect(last_day).to_be_focused()
    expect(last_day).to_have_value(wrong)
    expect(shown).to_have_value(past.isoformat())
    expect(reason).to_have_value("Keep the scheduled end while changing the saved site.")
    access.refresh_from_db()
    assert access.whole_organization and access.ends_at == original

    last_day.fill(past.isoformat())
    save.click()
    expect(page.get_by_text("Portal access saved.")).to_be_visible()
    access.refresh_from_db()
    assert not access.whole_organization
    assert list(access.sites.values_list("code", flat=True)) == ["INTAKE"]
    assert access.ends_at == original
    displayed = past + timedelta(days=1)
    row = page.get_by_role("row").filter(has_text="ended@example.invalid")
    expect(row).to_contain_text(f"Only tickets it raised (access ended after {displayed})")
    expect(page.locator("html")).to_have_attribute("data-bs-theme", view.theme)
    fits_page(page)


@pytest.mark.parametrize("expired", [True, False], ids=["ended", "ending"])
def test_pre_upgrade_admin_form_can_correct_an_error_and_keep_an_untouched_end(
    page, operator, staff_grants, settings, expired
):
    person, past, future = staff_grants
    patient_end = CapabilityEnd.objects.get(
        user=person, permission=permission("view_patient_information")
    )
    patient_day = past - timedelta(days=2) if expired else future + timedelta(days=3)
    with transaction.atomic():
        patient_end.ends_at = end_of(patient_day)
        patient_end.save(update_fields=["ends_at"])
    original = patient_end.ends_at
    # The stored UTC midnight is not a Tokyo midnight, even before this page opens.
    settings.TIME_ZONE = "Asia/Tokyo"
    patient, review = open_admin(page, operator, staff_grants)
    shown_patient = (patient_day + timedelta(days=1)).isoformat()
    shown_review = (future + timedelta(days=1)).isoformat()
    expect(patient).to_have_value(shown_patient)
    expect(review).to_have_value(shown_review)
    # An account page open from the previous release had no hidden shown-day controls.
    for control, name in ((patient, "view_patient_information"), (review, "review_audit_records")):
        hidden_day(control, f"shown_last_day_{name}").evaluate("el => el.remove()")
    first_name = page.get_by_label("First name")
    first_name.fill("Updated")
    review.fill((past - timedelta(days=1)).isoformat())
    save = page.get_by_role("button", name="Save and continue editing")
    save.click()
    expect(
        page.get_by_text(
            "Choose today or a later day. To end it now, remove it instead.", exact=True
        )
    ).to_be_visible()
    expect(patient).to_have_value(shown_patient)
    review.fill(shown_review)
    save.click()
    expect(page.get_by_text(re.compile("was changed successfully"))).to_be_visible()
    person.refresh_from_db()
    assert person.first_name == "Updated"
    assert account_end(person, "view_patient_information") == original


@pytest.mark.parametrize("username", ["ended", "ending"])
def test_pre_upgrade_portal_form_can_correct_a_reason_and_keep_an_untouched_end(
    page, operator, portal_grants, settings, username
):
    organization, past, future = portal_grants
    original = PortalAccess.objects.get(user__username=username).ends_at
    settings.TIME_ZONE = "Asia/Tokyo"
    last_day = open_portal(page, operator, organization, username)
    shown = ((past if username == "ended" else future) + timedelta(days=1)).isoformat()
    expect(last_day).to_have_value(shown)
    # Only the first, pre-upgrade page has the old markup. Submit the redrawn form untouched.
    hidden_day(last_day, "shown_last_day").evaluate("el => el.remove()")
    if username == "ended":
        choose_intake_site(page)
    else:
        page.get_by_role("radio", name="The whole organization").check()
    reason = field(page, "Reason for change")
    save = page.get_by_role("button", name="Save access")
    save.click()
    expect(reason).to_have_attribute("aria-invalid", "true")
    expect(last_day).to_have_value(shown)
    reason.fill("Change the scope and keep the existing last day.")
    save.click()
    expect(page.get_by_text("Portal access saved.")).to_be_visible()
    access = PortalAccess.objects.get(user__username=username)
    assert access.whole_organization == (username == "ending")
    assert access.ends_at == original
