"""Read the preview and saved reason at both supported widths in both colour schemes."""

import pytest
from helpers import sign_in
from playwright.sync_api import expect
from test_deactivation import OWNER, OWNER_PASSWORD, make_owner, make_staff, open_deactivate

pytestmark = pytest.mark.django_db(transaction=True)


def assert_text_fits(paragraph):
    """Check the text itself, including text an overflow-hidden form row would clip."""
    bounds = paragraph.evaluate(
        """el => {
            const text = document.createRange();
            text.selectNodeContents(el);
            const rects = Array.from(text.getClientRects());
            const box = el.getBoundingClientRect();
            return {
                textRight: Math.max(...rects.map(r => r.right)),
                visibleRight: Math.min(box.right, document.documentElement.clientWidth),
                pageWidth: document.documentElement.scrollWidth,
                viewport: window.innerWidth,
            };
        }"""
    )
    assert bounds["textRight"] <= bounds["visibleRight"] + 1, bounds
    assert bounds["pageWidth"] <= bounds["viewport"] + 1, bounds


@pytest.mark.confirmation
def test_confirmation_focus_and_saved_reason_remain_readable(page, view):
    person = make_staff()
    make_owner()
    sign_in(page, OWNER, OWNER_PASSWORD)
    open_deactivate(page, person)
    reason = page.get_by_label("Reason:")
    password = page.get_by_label("Your password")
    submit = page.get_by_role("button", name="Deactivate", exact=True)
    expect(reason).to_be_focused()
    expect(page.locator("ul.vr-held")).to_contain_text("E2E bench chemistry")
    if view.phone:
        assert submit.bounding_box()["height"] >= 44
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    # An operator pastes a synthetic case reference, within the documented length, long enough to
    # overflow 1366 px under any font unless it wraps.
    reference = "CASE-" + "49AC28E7" * 50
    reason.fill(reference)
    submit.click()
    expect(password).to_be_focused()
    expect(reason).to_have_value(reference)
    password.fill(OWNER_PASSWORD)
    submit.click()
    saved = page.locator(".vr-deactivation p").filter(has_text=reference)
    expect(saved).to_have_text(f"Reason: {reference}")
    saved.scroll_into_view_if_needed()
    assert_text_fits(saved)
    assert page.console_errors == []


def test_the_preview_fits_a_long_team_name(page, view):
    person = make_staff()
    team = person.team_memberships.get().team
    team.name = "AnalyticalInstrumentation" + "Reference" * 5
    team.full_clean()
    team.save(update_fields=["name"])
    make_owner()
    sign_in(page, OWNER, OWNER_PASSWORD)
    page.goto(f"/admin/accounts/user/{person.pk}/deactivate/")
    team_line = page.locator("ul.vr-held li").filter(has_text=team.name)
    expect(team_line).to_have_text(f"Teams: {team.name}")
    assert_text_fits(team_line)
