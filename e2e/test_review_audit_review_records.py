"""Independent Chromium regressions for signed review records, using synthetic reviews and notes."""

import re
from datetime import timedelta

import pytest
from django.utils import timezone
from helpers import field, sign_in
from playwright.sync_api import expect

from accounts.models import User
from audit_review import reviews
from audit_review.models import Review
from desk.story import REVIEW_HOLDER

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def long_review(demo):
    reviewer = User.objects.get(username=REVIEW_HOLDER)
    today = timezone.localdate()
    review = reviews.sign_review(
        actor=reviewer,
        period_from=today - timedelta(days=4),
        period_to=today,
        examined=["changes", "access", "sign_ins", "people", "uses"],
        examined_other="Synthetic provider record " + "R" * 1800,
        outcome=Review.Outcome.FOLLOW_UP,
        findings="A long synthetic reference " + "F" * 3900,
        follow_up="Compare the following synthetic identifiers. " * 70,
    )
    for count in range(18):
        reviews.add_note(
            actor=reviewer,
            review=review,
            text=f"Synthetic follow-up {count + 1:02d}: " + "q" * 200,
            expected_revision=1 + count,
        )
    return review


def check_page_width(page):
    sizes = page.evaluate("() => [document.documentElement.scrollWidth, window.innerWidth]")
    assert sizes[0] <= sizes[1], (page.url, sizes)


def test_review_list_and_long_signed_record_fit_the_screen(view, page, demo, long_review):
    sign_in(page, REVIEW_HOLDER, demo.password)
    page.goto("/audit-review/changes/")
    tabs = page.get_by_role("navigation", name="Audit review lists")
    tabs.get_by_role("link", name="Reviews", exact=True).click()
    expect(tabs.get_by_role("link", name="Reviews")).to_have_attribute("aria-current", "page")
    expect(page.get_by_role("heading", name="Where reviews stand")).to_be_visible()
    table = page.get_by_role("table", name="Reviews")
    expect(table.locator("tbody tr")).to_have_count(2)
    check_page_width(page)
    table.get_by_role("link", name=long_review.number).click()
    expect(page.get_by_role("heading", level=1)).to_contain_text(long_review.number)
    for value in (long_review.examined_other, long_review.findings, long_review.follow_up):
        expect(page.get_by_role("main")).to_contain_text(value.strip())
    expect(page.get_by_text("Matches the review as stored.", exact=True)).to_be_visible()
    notes = page.locator("ol.vr-plain-list > li")
    expect(notes).to_have_count(18)
    for index in (0, 17):
        notes.nth(index).scroll_into_view_if_needed()
        expect(notes.nth(index)).to_contain_text(f"Synthetic follow-up {index + 1:02d}")
    check_page_width(page)
    assert page.console_errors == []


@pytest.mark.parametrize("scripts", [True, False], ids=["dialog", "full-page"])
def test_review_forms_show_their_controls_and_save(view, new_context, demo, scripts):
    page = new_context(java_script_enabled=scripts).new_page()
    page.set_viewport_size(view.viewport)
    page.emulate_media(color_scheme=view.theme)
    sign_in(page, REVIEW_HOLDER, demo.password)

    def open_form(path, title, action):
        page.goto(path)
        surface = page.get_by_role("dialog") if scripts else page.get_by_role("main")
        expect(surface.get_by_role("heading", name=title, exact=True)).to_be_visible()
        expect(surface.get_by_text("No patient data", exact=True)).to_be_visible()
        submit = surface.get_by_role("button", name=action, exact=True)
        expect(submit).to_be_visible()
        if scripts:
            expect(surface.locator(":focus")).to_have_count(1)
        check_page_width(page)
        return surface, submit

    interval, save = open_form(
        "/audit-review/reviews/interval/", "Review interval", "Save interval"
    )
    if not scripts:
        # Adapted (round 1, L11): the history is a disclosure inside the form, with its count.
        expect(
            interval.get_by_text(re.compile(r"^Changes to the interval \(\d+\)$"))
        ).to_be_visible()
    field(interval, "Review every (days)").fill("42")
    field(interval, "Reason").fill("Synthetic procedure revision.")
    save.click()
    expect(page.get_by_text("Every 42 days", exact=True)).to_be_visible()

    record, sign = open_form("/audit-review/reviews/new/", "Record a review", "Sign review")
    record.get_by_role("checkbox").first.check()
    record.get_by_label("Nothing that needs follow-up").check()
    field(record, "Findings").fill("Synthetic summary.")
    field(record, "Your password").fill(demo.password)
    sign.click()
    expect(page.get_by_role("heading", name="What was reviewed")).to_be_visible()
    expect(page.get_by_text("Synthetic summary.", exact=True)).to_be_visible()
    review = Review.objects.get(findings="Synthetic summary.")
    note, add = open_form(f"/audit-review/reviews/{review.pk}/note/", "Add a note", "Add note")
    field(note, "Note").fill("Synthetic follow-up complete.")
    add.click()
    expect(page.get_by_text("Synthetic follow-up complete.", exact=True)).to_be_visible()


def test_interval_dialog_keeps_save_visible_at_the_end_of_its_scroll_region(view, page, demo):
    reviewer = User.objects.get(username=REVIEW_HOLDER)
    for index in range(10):
        reviews.set_interval(
            actor=reviewer,
            interval_days=60 + index,
            expected_revision=1 + index,
            reason=f"Synthetic revision {index:02d}. " + "Recorded for the laboratory. " * 25,
        )
    sign_in(page, REVIEW_HOLDER, demo.password)
    page.goto("/audit-review/reviews/")
    page.get_by_role("link", name="Change interval").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    body = dialog.locator("#vr-form-content")
    body.evaluate("el => el.scrollTo(0, el.scrollHeight)")
    expect(dialog.get_by_role("button", name="Save interval")).to_be_in_viewport()


@pytest.mark.parametrize("control", ["examined_list", "notes_disclosure"])
def test_standalone_review_controls_have_a_phone_touch_target(new_context, demo, control):
    page = new_context(has_touch=True, is_mobile=True).new_page()
    page.set_viewport_size({"width": 390, "height": 844})
    sign_in(page, REVIEW_HOLDER, demo.password)
    assert page.evaluate("matchMedia('(pointer: coarse)').matches")
    assert page.evaluate("window.innerWidth") == 390
    review = Review.objects.get()
    if control == "examined_list":
        page.goto(f"/audit-review/reviews/{review.pk}/")
        target = page.get_by_role("link", name="Changes for the period covered")
    else:
        page.goto(f"/audit-review/reviews/{review.pk}/note/")
        target = page.get_by_role("dialog").locator("summary").filter(has_text="Notes so far")
    expect(target).to_be_visible()
    rect = target.bounding_box()
    assert rect and min(rect["width"], rect["height"]) >= 44, (control, rect)
