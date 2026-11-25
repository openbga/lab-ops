"""Browser verification of the review form disclosures, touch controls and the desktop list."""

import pytest
from django.utils import timezone
from helpers import field, sign_in
from playwright.sync_api import expect

from accounts.models import User
from audit_review import reviews
from audit_review.models import EXAMINED, Review
from audit_review.tests.test_reviews import sign
from desk.story import REVIEW_HOLDER

pytestmark = pytest.mark.django_db(transaction=True)
PHONE = {"width": 390, "height": 844}


def phone(new_context, theme, *, scripts=True):
    session = new_context(has_touch=True, is_mobile=True, java_script_enabled=scripts)
    page = session.new_page()
    page.set_viewport_size(PHONE)
    page.emulate_media(color_scheme=theme)
    return page


def surface_width(surface):
    return surface.evaluate("el => [el.scrollWidth, el.clientWidth]")


def test_desktop_list_shows_the_six_named_columns_and_their_values(page, demo):
    sign_in(page, REVIEW_HOLDER, demo.password)
    review = Review.objects.get()
    page.goto("/audit-review/reviews/")
    table = page.get_by_role("table", name="Reviews")
    headers = ["Review", "Period covered", "Outcome", "Signed by", "Signed", "Notes"]
    values = [
        review.number,
        review.period_from.isoformat(),
        review.get_outcome_display(),
        review.signer_name,
        timezone.localdate(review.signed_at).isoformat(),
        str(review.notes.count()),
    ]
    overflow = {}
    for width in (1280, 1366, 1440):
        page.set_viewport_size({"width": width, "height": 860})
        for index, (name, value) in enumerate(zip(headers, values, strict=True)):
            heading = table.get_by_role("columnheader", name=name, exact=True)
            expect(heading).to_be_visible()
            cell = table.locator("tbody tr").first.locator("th, td").nth(index)
            expect(cell).to_be_visible()
            expect(cell).to_contain_text(value)
        wrapper = table.locator("..")
        scroll_width, client_width = surface_width(wrapper)
        if scroll_width > client_width + 1:
            overflow[width] = (scroll_width, client_width)
    assert not overflow, overflow


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_all_examined_links_and_both_disclosures_fit_a_touch_target(new_context, demo, theme):
    reviewer = User.objects.get(username=REVIEW_HOLDER)
    review = sign(reviewer, examined=[source.value for source in EXAMINED])
    reviews.add_note(actor=reviewer, review=review, text="Synthetic note.", expected_revision=1)
    page = phone(new_context, theme)
    sign_in(page, REVIEW_HOLDER, demo.password)
    page.goto(f"/audit-review/reviews/{review.pk}/")
    assert page.evaluate("matchMedia('(pointer: coarse)').matches && innerWidth === 390")
    links = page.get_by_role("link", name="for the period covered")
    expect(links).to_have_count(len(review.examined))
    controls = {}
    for link in links.all():
        controls[link.inner_text()] = link.bounding_box()
    for path, name in (
        (f"/audit-review/reviews/{review.pk}/note/", "Notes so far"),
        ("/audit-review/reviews/interval/", "Changes to the interval"),
    ):
        page.goto(path)
        summary = page.get_by_role("dialog").locator("summary").filter(has_text=name)
        expect(summary).to_be_visible()
        controls[name] = summary.bounding_box()
    small = {
        name: (rect["width"], rect["height"])
        for name, rect in controls.items()
        if min(rect["width"], rect["height"]) < 44
    }
    assert not small, small


@pytest.mark.parametrize("scripts", [True, False], ids=["dialog", "full-page"])
def test_long_notes_start_unfolded_and_a_reviewer_can_read_then_add_one(new_context, demo, scripts):
    reviewer = User.objects.get(username=REVIEW_HOLDER)
    review = Review.objects.get()
    texts = list(review.notes.values_list("text", flat=True))
    for number in range(1, 13):
        text = f"Synthetic long note {number}.\n" + "R" * 3800
        reviews.add_note(actor=reviewer, review=review, text=text, expected_revision=1 + len(texts))
        texts.append(text)
    page = phone(new_context, "dark", scripts=scripts)
    sign_in(page, REVIEW_HOLDER, demo.password)
    page.goto(f"/audit-review/reviews/{review.pk}/note/")
    surface = page.get_by_role("dialog") if scripts else page.get_by_role("main")
    history = surface.locator("details", has_text="Notes so far")
    expect(history).to_have_attribute("open", "")
    entries = history.locator("li")
    expect(entries).to_have_count(len(texts))
    for entry, text in zip(entries.all(), texts, strict=True):
        expect(entry.locator("time")).to_be_visible()
        expect(entry.locator("div")).to_have_text(text, use_inner_text=True)
    entries.last.scroll_into_view_if_needed()
    expect(entries.last).to_be_in_viewport()
    scroll_width, client_width = surface_width(history)
    assert scroll_width <= client_width + 1
    field(surface, "Note").fill("Final synthetic correction.")
    surface.get_by_role("button", name="Add note", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_contain_text(review.number)
    expect(page.get_by_text("Final synthetic correction.", exact=True)).to_be_visible()
    assert review.notes.count() == len(texts) + 1


def test_save_stays_visible_with_interval_history_open_and_scrolled(view, page, demo):
    reviewer = User.objects.get(username=REVIEW_HOLDER)
    for index in range(10):
        reviews.set_interval(
            actor=reviewer,
            interval_days=60 + index,
            expected_revision=1 + index,
            reason=f"Procedure {index:02d}. " + "Recorded for this synthetic procedure. " * 50,
        )
    sign_in(page, REVIEW_HOLDER, demo.password)
    page.goto("/audit-review/reviews/")
    page.get_by_role("link", name="Change interval").click()
    dialog = page.get_by_role("dialog")
    history = dialog.locator("details", has_text="Changes to the interval")
    expect(history).not_to_have_attribute("open", "")
    history.locator("summary").click()
    expect(history).to_have_attribute("open", "")
    entries = history.locator("li")
    expect(entries).to_have_count(10)
    expect(entries.first).to_contain_text("Procedure 09")
    expect(entries.last).to_contain_text("Procedure 00")
    body = dialog.locator("#vr-form-content")
    height, visible = body.evaluate("el => [el.scrollHeight, el.clientHeight]")
    assert height > visible * 2
    for fraction in (0.5, 1.0):
        body.evaluate("(el, fraction) => el.scrollTo(0, fraction * el.scrollHeight)", fraction)
        expect(dialog.get_by_role("button", name="Save interval")).to_be_in_viewport(ratio=1)
        expect(dialog.get_by_role("link", name="Cancel", exact=True)).to_be_in_viewport(ratio=1)
    assert body.evaluate("el => el.scrollTop") > 0


@pytest.mark.parametrize("scripts", [True, False], ids=["dialog", "full-page"])
def test_long_reference_in_interval_history_fits_its_form_on_a_phone(new_context, demo, scripts):
    reviewer = User.objects.get(username=REVIEW_HOLDER)
    reason = "Synthetic procedure reference " + "R" * 160
    reviews.set_interval(actor=reviewer, interval_days=40, expected_revision=1, reason=reason)
    page = phone(new_context, "light", scripts=scripts)
    sign_in(page, REVIEW_HOLDER, demo.password)
    page.goto("/audit-review/reviews/interval/")
    surface = page.get_by_role("dialog") if scripts else page.get_by_role("main")
    history = surface.locator("details", has_text="Changes to the interval")
    history.locator("summary").click()
    expect(history.get_by_text(reason, exact=True)).to_be_visible()
    body = surface.locator("#vr-form-content") if scripts else page.locator("html")
    scroll_width, client_width = surface_width(body)
    assert scroll_width <= client_width + 1, (scroll_width, client_width)


def test_note_form_wraps_the_full_author_name_on_a_phone(new_context, demo):
    reviewer = User.objects.get(username=REVIEW_HOLDER)
    first, last = "A" * 150, "B" * 150
    author = User.objects.create_user("synthetic-reviewer", first_name=first, last_name=last)
    author.groups.set(reviewer.groups.all())
    author.user_permissions.set(reviewer.user_permissions.all())
    review = Review.objects.get()
    reviews.add_note(
        actor=author, review=review, text="Synthetic witness note.", expected_revision=2
    )
    page = phone(new_context, "dark")
    sign_in(page, REVIEW_HOLDER, demo.password)
    page.goto(f"/audit-review/reviews/{review.pk}/note/")
    dialog = page.get_by_role("dialog")
    history = dialog.locator("details", has_text="Notes so far")
    expect(history.get_by_text(f"{first} {last}", exact=True)).to_be_visible()
    scroll_width, client_width = surface_width(dialog.locator("#vr-form-content"))
    assert scroll_width <= client_width + 1, (scroll_width, client_width)
