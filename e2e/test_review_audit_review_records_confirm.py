"""Confirm review record layout with legal maximum-length names and both pointer layouts."""

from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import pytest
from helpers import sign_in
from playwright.sync_api import expect

from accounts.models import User
from audit_review import reviews
from audit_review.models import EXAMINED, Review
from audit_review.tests.test_reviews import sign
from desk.story import REVIEW_HOLDER

pytestmark = pytest.mark.django_db(transaction=True)


@dataclass
class History:
    actor: User
    note_author: User
    review: Review
    reason: str


@pytest.fixture
def history(demo):
    reviewer = User.objects.get(username=REVIEW_HOLDER)

    def account(username, **names):
        actor = User.objects.create_user(
            username, f"{username[:16]}@example.invalid", demo.password, **names
        )
        actor.groups.set(reviewer.groups.all())
        actor.user_permissions.set(reviewer.user_permissions.all())
        return actor

    actor = account("synthetic-signer", first_name="A" * 150, last_name="B" * 150)
    author = account("u" * 150)  # notes use the username when a full name was never entered
    record = sign(actor, examined=[source.value for source in EXAMINED])
    reviews.add_note(actor=author, review=record, text="Synthetic correction.", expected_revision=1)
    reason = "S" * reviews.TEXT_LIMIT
    reviews.set_interval(actor=actor, interval_days=32, expected_revision=1, reason=reason)
    return History(actor, author, record, reason)


def fits(container):
    width, space = container.evaluate("el => [el.scrollWidth, el.clientWidth]")
    assert width <= space + 1, (width, space)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_long_names_and_interval_reason_wrap_on_each_review_surface(page, demo, history, theme):
    page.emulate_media(color_scheme=theme)
    sign_in(page, REVIEW_HOLDER, demo.password)
    name = history.actor.get_full_name()
    username = history.note_author.username
    for width in (360, 430, 1280, 1440):
        page.set_viewport_size({"width": width, "height": 860})
        page.goto("/audit-review/reviews/")
        expect(page.locator("html")).to_have_attribute("data-bs-theme", theme)
        state = page.get_by_role("region", name="Where reviews stand")
        expect(state.get_by_text(name, exact=True)).to_be_visible()
        fits(state)
        table = page.get_by_role("table", name="Reviews")
        row = table.locator("tbody tr").filter(has_text=history.review.number)
        expect(row.locator("td").nth(2)).to_contain_text(name)
        cell = row.locator("td").first
        expect(cell.locator("time")).to_have_count(2)
        if width >= 1280:
            first, last = cell.locator("time").all()
            assert first.bounding_box()["y"] == pytest.approx(last.bounding_box()["y"], abs=1)
        fits(cell)
        fits(table.locator(".."))
        row.get_by_role("link", name=history.review.number).click()
        signature = page.get_by_role("region", name="Sign-off")
        expect(signature.get_by_text(name, exact=True)).to_be_visible()
        fits(signature)
        notes = page.get_by_role("region", name="Notes", exact=True)
        expect(notes.get_by_text(username, exact=True)).to_be_visible()
        fits(notes)
        page.get_by_role("link", name="Add a note", exact=True).first.click()
        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_text(name, exact=True)).to_be_visible()
        expect(dialog.get_by_text(username, exact=True)).to_be_visible()
        fits(dialog.locator("#vr-form-content"))
        page.goto("/audit-review/reviews/interval/")
        dialog.locator("summary").filter(has_text="Changes to the interval").click()
        expect(dialog.get_by_text(history.reason, exact=True)).to_be_visible()
        expect(dialog.get_by_text(name, exact=True)).to_be_visible()
        fits(dialog.locator("#vr-form-content"))


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_shortest_examined_link_has_a_usable_touch_target_on_phone_and_tablet(
    new_context, demo, history, theme
):
    page = new_context(is_mobile=True, has_touch=True).new_page()
    page.emulate_media(color_scheme=theme)
    sign_in(page, REVIEW_HOLDER, demo.password)
    for width in (360, 820):
        page.set_viewport_size({"width": width, "height": 844})
        page.goto(f"/audit-review/reviews/{history.review.pk}/")
        expect(page.locator("html")).to_have_attribute("data-bs-theme", theme)
        assert page.evaluate("matchMedia('(pointer: coarse)').matches")
        link = page.get_by_role("link", name="People for the period covered")
        link.scroll_into_view_if_needed()
        box = link.bounding_box()
        assert box is not None and min(box["width"], box["height"]) >= 44
        # Each corner of the 44px target must belong to the link in the page's hit-test.
        assert link.evaluate("""el => {
            const box = el.getBoundingClientRect();
            return [box.left + 2, box.right - 2].every(x =>
                [box.top + 2, box.bottom - 2].every(y =>
                    el.contains(document.elementFromPoint(x, y))));
        }""")
        # Tap below the text, with room for touch contact on both sides of the point.
        with page.expect_navigation():
            page.touchscreen.tap(box["x"] + box["width"] / 2, box["y"] + box["height"] - 10)
        assert urlsplit(page.url).path == "/audit-review/people/"
        query = parse_qs(urlsplit(page.url).query)
        assert query["date_from"] == [history.review.period_from.isoformat()]
        assert query["date_to"] == [history.review.period_to.isoformat()]


@pytest.mark.parametrize("scripts", [True, False], ids=["dialog", "full-page"])
def test_signer_can_read_their_full_name_in_the_signing_form_on_a_phone(
    new_context, demo, history, scripts
):
    page = new_context(is_mobile=True, has_touch=True, java_script_enabled=scripts).new_page()
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(color_scheme="dark")
    sign_in(page, history.actor.username, demo.password)
    page.goto("/audit-review/reviews/new/")
    surface = page.get_by_role("dialog") if scripts else page.get_by_role("main")
    signature = surface.get_by_role("group", name="Sign-off")
    expect(signature).to_contain_text(history.actor.get_full_name())
    fits(signature.locator("p"))
