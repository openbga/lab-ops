"""Exercise the access review as a reviewer, at the four supported width/theme combinations."""

import pytest
from django.contrib.auth import get_user_model
from helpers import field
from playwright.sync_api import expect

from audit_review.models import AccessReview
from clients.models import Client, ClientSite, PortalAccess

pytestmark = pytest.mark.django_db(transaction=True)

REVIEWS = "/access-review/reviews/"
NEW = f"{REVIEWS}new/"
ACCOUNT_COUNT = 24


@pytest.fixture
def portal_organization():
    """Long unbroken display names fit the directory's 160-character fields."""
    org = Client.objects.create(name="RiversideMonitoringNetwork" * 6)
    site = ClientSite.objects.create(client=org, name="NorthwestCollectionFacility" * 5)
    User = get_user_model()
    for index in range(ACCOUNT_COUNT):
        user = User.objects.create_user(
            username=f"regional_monitoring_{index:02d}",
            email=f"monitor{index}@example.invalid",
            first_name="Alexanderson" * 7,
            last_name=f"Monitor {index:02d}",
            client=org,
        )
        if index == 0:
            access = PortalAccess.objects.create(client=org, user=user)
            access.sites.add(site)
    return org


def check_page(page, view, heading):
    expect(page.locator("html")).to_have_attribute("data-bs-theme", view.theme)
    expect(page.get_by_role("heading", level=1)).to_contain_text(heading)
    actual_width = page.evaluate("document.documentElement.clientWidth")
    assert actual_width == view.viewport["width"]


def page_width(page):
    return page.evaluate("document.documentElement.scrollWidth")


def test_long_portal_names_fit_list_form_redraw_and_signed_review(
    page, view, demo, sign_in_as, portal_organization
):
    sign_in_as("supervisor")
    org = portal_organization
    page.goto(f"{NEW}?scope=portal&organization={org.pk}")
    check_page(page, view, "Record an access review")
    widths = {"form": page_width(page)}
    accounts = page.locator("li.vr-review-account")
    expect(accounts).to_have_count(ACCOUNT_COUNT)
    expect(accounts.first).to_contain_text(org.name)
    expect(accounts.first).to_contain_text("NorthwestCollectionFacility" * 5)
    remove = accounts.first.get_by_role("radio", name="Remove")
    remove.check()
    field(page, "Findings").fill("Check the first monitor's assignment.")
    field(page, "Your password").fill(demo.password)
    submit = page.get_by_role("button", name="Sign access review")
    submit.click()

    check_page(page, view, "Record an access review")
    expect(page.get_by_text("Choose Keep or Remove.", exact=True)).to_have_count(ACCOUNT_COUNT - 1)
    expect(accounts.nth(1).get_by_role("radio", name="Keep")).to_be_focused()
    expect(remove).to_be_checked()
    expect(field(page, "Your password")).to_have_value("")
    expect(field(page, "Findings")).to_have_value("Check the first monitor's assignment.")
    widths["redrawn form"] = page_width(page)

    for index in range(1, ACCOUNT_COUNT):
        accounts.nth(index).get_by_role("radio", name="Keep").check()
    field(page, "Your password").fill(demo.password)
    submit.click()
    expect(page.get_by_text("Matches the access review as stored.")).to_be_visible()
    signed = AccessReview.objects.get(organization=org.pk)
    check_page(page, view, signed.number)
    expect(page.locator("li.vr-review-item")).to_have_count(ACCOUNT_COUNT)
    removal = page.locator("li.vr-review-removal")
    expect(removal).to_contain_text(org.name)
    expect(removal).to_contain_text("Still held")
    widths["signed review"] = page_width(page)

    page.get_by_role("navigation", name="Breadcrumb").get_by_role("link", name="Reviews").click()
    check_page(page, view, "Access review: reviews")
    row = page.get_by_role("row").filter(has=page.get_by_role("link", name=signed.number))
    expect(row).to_contain_text(org.name)
    expect(row).to_contain_text(f"{ACCOUNT_COUNT} accounts")
    widths["list"] = page_width(page)
    assert page.console_errors == []
    assert all(width <= view.viewport["width"] for width in widths.values()), widths


def test_undecided_count_matches_unanswered_grants_when_the_form_opens(page, sign_in_as):
    sign_in_as("supervisor")
    page.goto(NEW)
    grants = page.locator("fieldset.vr-decision")
    unanswered = grants.count()
    assert unanswered > 1
    expect(grants.locator("input:checked")).to_have_count(0)
    note = page.locator("fieldset.vr-form-section").first.locator(".vr-form-note").first
    expect(note).to_contain_text(f"{unanswered} grants still need a decision.")
