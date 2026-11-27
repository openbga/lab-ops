"""Follow a maximum-length account name from the preview through refusal and the saved result."""

import pytest
from helpers import sign_in
from playwright.sync_api import expect
from test_deactivation import OWNER, OWNER_PASSWORD, PASSWORD, make_owner

from accounts.models import Deactivation, User
from teams.models import Team, TeamMembership

pytestmark = pytest.mark.django_db(transaction=True)


def check_text_width(page, selectors):
    """Capture each text range so a clipped value fails even on a page without a scrollbar."""
    widths = page.evaluate(
        """selectors => {
            const limit = document.documentElement.clientWidth;
            const measurements = {};
            for (const selector of selectors) {
                const element = document.querySelector(selector);
                const range = document.createRange();
                range.selectNodeContents(element);
                const right = Math.max(...Array.from(range.getClientRects(), r => r.right));
                measurements[selector] = Math.ceil(
                    right - Math.min(element.getBoundingClientRect().right, limit)
                );
            }
            measurements.page = document.documentElement.scrollWidth - limit;
            return measurements;
        }""",
        selectors,
    )
    assert all(excess <= 1 for excess in widths.values()), widths


def test_long_username_stays_readable_on_refusal_and_after_saving(page, view):
    username = "W" * 150
    target = User.objects.create_user(username, "bench@example.invalid", PASSWORD)
    target.full_clean()
    make_owner()
    sign_in(page, OWNER, OWNER_PASSWORD)
    page.goto(f"/admin/accounts/user/{target.pk}/deactivate/")
    heading = page.get_by_role("heading", name=f"Deactivate: {username}", exact=True)
    expect(heading).to_be_visible()
    preview_elements = ["#content > h1", ".breadcrumbs", ".vr-deactivate form p:has(strong)"]
    check_text_width(page, preview_elements)

    team = Team.objects.create(name="Late shift", slug="confirm-late")
    TeamMembership.objects.create(team=team, user=target)
    reason = "Synthetic shift ended"
    page.get_by_label("Reason:").fill(reason)
    button = page.get_by_role("button", name="Deactivate", exact=True)
    button.click()
    expect(page.locator(".vr-held")).to_contain_text(team.name)
    expect(page.get_by_label("Reason:")).to_have_value(reason)
    check_text_width(page, preview_elements)
    button.click()
    expect(page.locator(".vr-deactivation")).to_contain_text(reason)
    expect(page.locator(".messagelist li")).to_contain_text(username)
    assert Deactivation.objects.get(user=target).reason == reason
    check_text_width(page, [".messagelist li", "#content > h2", ".breadcrumbs"])


def test_long_owner_username_fits_the_own_account_refusal(page, view):
    owner = make_owner()
    owner.username = "BenchOperator" * 11
    owner.full_clean()
    owner.save(update_fields=["username"])
    sign_in(page, owner.username, OWNER_PASSWORD)
    page.goto(f"/admin/accounts/user/{owner.pk}/deactivate/")
    expect(page.get_by_text("This is your own account.", exact=False).first).to_be_visible()
    expect(page.get_by_role("button", name="Deactivate", exact=True)).to_have_count(0)
    expect(page.locator("#user-tools strong")).to_have_text(owner.username)
    check_text_width(page, ["#user-tools strong", "#content > h1", ".breadcrumbs"])
