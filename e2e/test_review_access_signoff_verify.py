"""Neighboring browser cases for the decision count and long directory labels."""

import re

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from helpers import field
from playwright.sync_api import expect

from audit_review.models import AccessReview
from clients.models import Client

pytestmark = pytest.mark.django_db(transaction=True)

REVIEWS = "/access-review/reviews/"
NEW = f"{REVIEWS}new/"
User = get_user_model()


def add_portal_monitor(org, number):
    return User.objects.create_user(
        username=f"review_monitor_{number}",
        email=f"review_monitor_{number}@example.invalid",
        first_name=f"Monitor {number}",
        client=org,
    )


@pytest.fixture
def portal_monitors():
    org = Client.objects.create(name="Verification monitoring cooperative")
    return org, [add_portal_monitor(org, index) for index in range(3)]


# Adapted in verification: the count is worked out when the page is drawn, and the page says so
# ("Counted when this page was drawn."); it does not follow the radios.
@pytest.mark.xfail(strict=True, reason="Documented: the count is as of when the page was drawn")
def test_decisions_left_matches_the_radios_as_the_reviewer_works(page, sign_in_as, portal_monitors):
    org, _ = portal_monitors
    sign_in_as("supervisor")
    page.goto(f"{NEW}?scope=portal&organization={org.pk}")
    grants = page.locator("fieldset.vr-decision")
    expect(grants).to_have_count(3)
    summary = page.get_by_text("as they stand now.")
    expect(summary).to_contain_text("3 grants still need a decision.")
    grants.nth(0).get_by_role("radio", name="Keep").check()
    grants.nth(0).get_by_role("radio", name="Remove").check()
    grants.nth(1).get_by_role("radio", name="Keep").check()
    expect(grants.locator("input:checked")).to_have_count(2)
    expect(summary).to_contain_text("1 grant still needs a decision.")


def test_consecutive_redraws_count_new_and_previously_unanswered_grants(
    page, sign_in_as, demo, portal_monitors
):
    org, people = portal_monitors
    sign_in_as("supervisor")
    page.goto(f"{NEW}?scope=portal&organization={org.pk}")
    accounts = page.locator("li.vr-review-account")
    first, changed, unanswered = (accounts.filter(has_text=user.username) for user in people)
    first.get_by_role("radio", name="Keep").check()
    changed.get_by_role("radio", name="Remove").check()
    field(page, "Findings").fill("Revisit the current site assignments.")
    field(page, "Your password").fill(demo.password)
    people[1].first_name = "Renamed monitor"
    people[1].save(update_fields=["first_name"])
    submit = page.get_by_role("button", name="Sign access review")
    submit.click()
    expect(page.get_by_text("2 grants still need a decision.")).to_be_visible()
    expect(first.get_by_role("radio", name="Keep")).to_be_checked()
    expect(changed).to_contain_text("New or changed")
    expect(changed.locator("input:checked")).to_have_count(0)
    expect(unanswered).not_to_contain_text("New or changed")
    expect(unanswered.locator("input:checked")).to_have_count(0)

    changed.get_by_role("radio", name="Remove").check()
    added = add_portal_monitor(org, 3)
    field(page, "Your password").fill(demo.password)
    submit.click()
    expect(page.get_by_text("2 grants still need a decision.")).to_be_visible()
    expect(changed.get_by_role("radio", name="Remove")).to_be_checked()
    expect(changed).not_to_contain_text("New or changed")
    expect(unanswered.locator("input:checked")).to_have_count(0)
    expect(accounts.filter(has_text=added.username)).to_contain_text("New or changed")
    expect(accounts.locator("input:checked")).to_have_count(2)
    expect(field(page, "Findings")).to_have_value("Revisit the current site assignments.")
    expect(field(page, "Your password")).to_have_value("")
    assert not AccessReview.objects.filter(organization=org).exists()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_long_group_and_account_names_wrap_before_and_after_signing(page, sign_in_as, demo, theme):
    person = User.objects.create_user(
        username="verification_technician_" + "northwest" * 12,
        first_name="Montgomery" * 15,
        email="technician@example.invalid",
    )
    group = Group.objects.create(name="TransregionalTechnicians" * 6)
    person.groups.add(group)
    page.emulate_media(color_scheme=theme)
    sign_in_as("supervisor")
    page.goto(NEW)
    expect(page.locator("html")).to_have_attribute("data-bs-theme", theme)
    account = page.locator("li.vr-review-account").filter(has_text=person.username)
    expect(account).to_contain_text(person.first_name)
    expect(account).to_contain_text(group.name)
    for width in (430, 360):
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth") == width
    for radio in page.get_by_role("radio", name="Keep", exact=True).all():
        radio.check()
    account.get_by_role("radio", name="Remove").check()
    field(page, "Findings").fill("Verification" * 35)
    field(page, "Your password").fill(demo.password)
    page.get_by_role("button", name="Sign access review").click()
    expect(page.get_by_text("Matches the access review as stored.")).to_be_visible()
    removal = page.get_by_role("region", name=re.compile("^To remove")).locator("li")
    for text in (group.name, person.first_name, person.username, "Still held"):
        expect(removal).to_contain_text(text)
    for width in (360, 430):
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth") == width


def test_organization_options_show_the_suffix_that_distinguishes_two_long_names(page, sign_in_as):
    common = "RegionalMonitoring" * 8
    names = [f"{common}East", f"{common}West"]
    for name in names:
        Client.objects.create(name=name)
    page.set_viewport_size({"width": 390, "height": 844})
    sign_in_as("supervisor")
    page.goto(f"{NEW}?scope=portal")
    field(page, "Organization").click()
    right_edges = {}
    for name in names:
        option = page.get_by_role("option", name=name, exact=True)
        expect(option).to_be_visible()
        # Text range of the distinguishing suffix in the open, horizontally unscrolled menu.
        right_edges[name[-4:]] = option.evaluate("""option => {
            const text = option.querySelector(".vr-ts-text").firstChild;
            const range = document.createRange();
            range.setStart(text, text.length - 4);
            range.setEnd(text, text.length);
            return {suffix: range.getBoundingClientRect().right,
                    menu: option.closest('[role="listbox"]').getBoundingClientRect().right};
        }""")
    assert all(edge["suffix"] <= edge["menu"] + 1 for edge in right_edges.values()), right_edges
