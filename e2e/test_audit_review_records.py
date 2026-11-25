"""Review records of the audit review workspace (/audit-review/reviews/): a reviewer, the
demonstration supervisor who holds the review capability, sets the review interval and records a
signed review in the shared form dialog (a wrong password keeps the typing), reads the review page
and adds a note; on a phone the Reviews tab, a review page and the Record a review form fit the
screen and are usable one-handed; staff without the capability are offered nothing and refused.
Synthetic demonstration data only."""

import re

import pytest
from conftest import NARROW
from helpers import field, sign_in
from playwright.sync_api import Page, expect

from audit_review.models import Review, ReviewSchedule

pytestmark = pytest.mark.django_db(transaction=True)

REVIEWER = "demo_supervisor"
FINDINGS = "Synthetic finding: nothing unexpected in the access records."


def no_sideways_scroll(page: Page):
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
        "the page scrolls sideways"
    )


def open_reviews(page: Page):
    page.goto("/audit-review/changes/")
    page.get_by_role("navigation", name="Audit review lists").get_by_role(
        "link", name="Reviews"
    ).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Audit review: reviews")
    expect(page.get_by_role("heading", name="Where reviews stand")).to_be_visible()


def test_a_reviewer_sets_the_interval_signs_a_review_and_adds_a_note(page: Page, demo):
    sign_in(page, REVIEWER, demo.password)
    # The demonstration already holds an interval of 31 days and one signed review.
    seeded = Review.objects.count()
    assert seeded == 1 and ReviewSchedule.objects.get().interval_days == 31
    open_reviews(page)
    dialog = page.locator("#vr-form-dialog")
    expect(page.get_by_text("Every 31 days")).to_be_visible()

    # The interval, through the dialog.
    page.get_by_role("link", name="Change interval").click()
    expect(dialog).to_be_visible()
    expect(dialog.locator("#vr-form-title")).to_have_text("Review interval")
    field(dialog, "Review every (days)").fill("45")
    field(dialog, "Reason").fill("Synthetic: the laboratory now reviews every six weeks.")
    dialog.get_by_role("button", name="Save", exact=False).last.click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_text("Every 45 days")).to_be_visible()

    # Record a review: a wrong password keeps the typing and the password empty.
    page.get_by_role("link", name="Record a review").first.click()
    expect(dialog).to_be_visible()
    expect(dialog.locator("#vr-form-title")).to_have_text("Record a review")
    dialog.get_by_role("checkbox").first.check()
    dialog.get_by_label("Nothing that needs follow-up").check()
    field(dialog, "Findings").fill(FINDINGS)
    field(dialog, "Your password").fill("not-the-password")
    dialog.get_by_role("button", name="Sign review", exact=True).click()
    expect(dialog).to_contain_text("Incorrect password.")
    expect(field(dialog, "Findings")).to_have_value(FINDINGS)
    expect(field(dialog, "Your password")).to_have_value("")
    assert Review.objects.count() == seeded

    field(dialog, "Your password").fill(demo.password)
    dialog.get_by_role("button", name="Sign review", exact=True).click()
    expect(dialog).to_be_hidden()
    assert Review.objects.count() == seeded + 1
    review = Review.objects.latest("id")
    expect(page).to_have_url(re.compile(rf"/audit-review/reviews/{review.pk}/$"))
    expect(page.get_by_role("heading", level=1)).to_contain_text(review.number)
    expect(page.get_by_text("Matches the review as stored.")).to_be_visible()
    expect(page.get_by_role("main")).to_contain_text(FINDINGS)

    # A note, through the dialog.
    page.get_by_role("link", name="Add a note").first.click()
    expect(dialog).to_be_visible()
    note = "Synthetic note: the follow-up came to nothing."
    field(dialog, "Note").fill(note)
    dialog.get_by_role("button", name=re.compile("note", re.I)).last.click()
    expect(dialog).to_be_hidden()
    notes = page.get_by_role("region", name=re.compile("^Notes"))
    expect(notes).to_contain_text(note)

    # Back on the list.
    page.get_by_role("navigation", name="Breadcrumb").get_by_role("link", name="Reviews").click()
    expect(page.get_by_role("table", name="Reviews").locator("tbody tr")).to_have_count(seeded + 1)
    expect(page.get_by_role("link", name=review.number).first).to_be_visible()
    expect(page.get_by_text("Every 45 days")).to_be_visible()
    expect(page.get_by_text(re.compile(r"in 45 days?"))).to_be_visible()
    assert page.console_errors == []


def test_a_phone_fits_the_reviews_tab_a_review_and_the_form_in_dark(page: Page, demo):
    page.set_viewport_size(NARROW)
    page.emulate_media(color_scheme="dark")
    sign_in(page, REVIEWER, demo.password)
    review = Review.objects.latest("id")  # the demonstration's signed review

    page.goto("/audit-review/reviews/")
    expect(page.get_by_role("heading", level=1)).to_have_text("Audit review: reviews")
    expect(page.get_by_role("link", name=review.number).first).to_be_visible()
    no_sideways_scroll(page)
    page.screenshot(path="/tmp/labops-arr-reviews-phone.png", full_page=True)

    page.goto(f"/audit-review/reviews/{review.pk}/")
    expect(page.get_by_text("Matches the review as stored.")).to_be_visible()
    no_sideways_scroll(page)

    page.goto("/audit-review/reviews/")
    page.get_by_role("link", name="Record a review").first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#vr-form-title")).to_have_text("Record a review")
    # Keyboard focus lands inside the form, not on the page behind.
    assert dialog.evaluate("el => el.contains(document.activeElement)")
    frame = dialog.locator(".modal-content").bounding_box()
    assert frame and frame["width"] == pytest.approx(NARROW["width"], abs=1)
    no_sideways_scroll(page)
    for label in ("From", "To", "Findings", "Follow-up", "Your password"):
        control = field(dialog, label)
        control.scroll_into_view_if_needed()
        box = control.bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= NARROW["width"] + 1, label
    submit = dialog.get_by_role("button", name="Sign review", exact=True)
    submit.scroll_into_view_if_needed()
    box = submit.bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= NARROW["width"] + 1
    assert box["y"] + box["height"] <= NARROW["height"] + 1
    page.screenshot(path="/tmp/labops-arr-form-phone.png")
    assert page.console_errors == []


def test_staff_without_the_capability_are_offered_no_reviews_and_refused(page: Page, demo):
    sign_in(page, "demo_client_services", demo.password)
    page.goto("/")
    expect(page.locator("nav.vr-sidenav").get_by_role("link", name="Audit review")).to_have_count(0)
    for address in ("/audit-review/reviews/", "/audit-review/reviews/new/"):
        response = page.goto(address)
        assert response is not None and response.status == 403, address
    expect(page.get_by_role("link", name="Reviews", exact=True)).to_have_count(0)
