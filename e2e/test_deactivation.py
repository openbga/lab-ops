"""Deactivating an account from the admin console, in the browser: an owner of the account
administration opens a staff account's page, follows Deactivate, reads what the account holds,
gives a reason and lands on the account's page with the record of what it held; a page opened
before a change of what the account holds is refused, with the reason kept and the list redrawn,
and saves when sent again; a portal account's page lists its portal access. Synthetic data only."""

import re

import pytest
from conftest import View
from django.contrib.auth.models import Group, Permission
from helpers import make_user, sign_in
from playwright.sync_api import Page, expect

from accounts.models import Deactivation, User
from teams.models import Team, TeamMembership

pytestmark = pytest.mark.django_db(transaction=True)

OWNER = "e2e_owner"
OWNER_PASSWORD = "e2e-owner-lantern-418"
PASSWORD = "e2e-quiet-ember-729"
OVERFLOW = "() => document.documentElement.scrollWidth - window.innerWidth"
REASON = "Synthetic test: left the laboratory"


def make_owner():
    owner = User.objects.create_superuser(OWNER, f"{OWNER}@example.invalid", OWNER_PASSWORD)
    # A laboratory role too, so the start page after sign-in opens.
    owner.groups.add(Group.objects.get_or_create(name="admin")[0])
    return owner


def make_staff():
    person = make_user("e2e_leaver", PASSWORD, "supervisor")
    person.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="accounts", codename="view_patient_information"
        )
    )
    team = Team.objects.create(slug="e2e-bench", name="E2E bench chemistry")
    TeamMembership.objects.create(team=team, user=person)
    return person


def open_deactivate(page: Page, person):
    page.goto(f"/admin/accounts/user/{person.pk}/change/")
    expect(page.get_by_text("Deactivate shows what the account holds now")).to_be_visible()
    page.get_by_role("link", name="Deactivate", exact=True).click()
    expect(page).to_have_url(re.compile(rf"/admin/accounts/user/{person.pk}/deactivate/$"))
    expect(page.get_by_text("holds now:")).to_be_visible()


def no_sideways_scroll(page: Page, view: View):
    if view.phone:
        assert page.evaluate(OVERFLOW) <= 1, "the page scrolls sideways"


def test_an_owner_deactivates_a_staff_account_and_reads_the_record(page: Page, view: View):
    if view.phone != (view.theme == "dark"):
        pytest.skip("run at desktop light and phone dark")
    person = make_staff()
    make_owner()
    sign_in(page, OWNER, OWNER_PASSWORD)
    expect(page.get_by_role("heading", level=1)).to_be_visible()

    open_deactivate(page, person)
    expect(page.get_by_label("Reason")).to_be_focused()
    held = page.locator("ul.vr-held")
    expect(held).to_contain_text("Roles: ")
    expect(held).to_contain_text("Capabilities: ")
    expect(held).to_contain_text("Teams: E2E bench chemistry")
    expect(held).to_contain_text("Admin console: ")
    no_sideways_scroll(page, view)

    page.get_by_label("Reason").fill(REASON)
    page.get_by_role("button", name="Deactivate", exact=True).click()
    expect(page).to_have_url(re.compile(rf"/admin/accounts/user/{person.pk}/change/$"))
    expect(
        page.get_by_text(re.compile(r"Deactivated e2e_leaver\. Its sessions ended\."))
    ).to_be_visible()
    record = page.locator(".vr-deactivation")
    expect(record).to_contain_text(re.compile(rf"Deactivated on .* by {OWNER}\."))
    expect(record).to_contain_text(f"Reason: {REASON}")
    expect(record).to_contain_text("What it held when it was deactivated:")
    expect(record.locator("ul.vr-held")).to_contain_text("Teams: E2E bench chemistry")
    no_sideways_scroll(page, view)
    assert not User.objects.get(pk=person.pk).is_active
    assert Deactivation.objects.filter(user=person).count() == 1
    assert page.console_errors == []


def test_a_page_opened_before_a_change_is_refused_then_saves(page: Page, view: View):
    if not (view.theme == "dark" and not view.phone):
        pytest.skip("run at desktop dark")
    person = make_staff()
    make_owner()
    sign_in(page, OWNER, OWNER_PASSWORD)
    open_deactivate(page, person)
    expect(page.locator("ul.vr-held")).not_to_contain_text("Technical staff")

    person.groups.add(Group.objects.get_or_create(name="technical")[0])
    page.get_by_label("Reason").fill(REASON)
    page.get_by_role("button", name="Deactivate", exact=True).click()
    expect(
        page.get_by_text("What this account holds changed after you opened this page")
    ).to_be_visible()
    expect(page.get_by_label("Reason")).to_have_value(REASON)
    expect(page.locator("ul.vr-held")).to_contain_text("Technical staff")
    assert User.objects.get(pk=person.pk).is_active

    page.get_by_role("button", name="Deactivate", exact=True).click()
    expect(page).to_have_url(re.compile(rf"/admin/accounts/user/{person.pk}/change/$"))
    expect(page.locator(".vr-deactivation")).to_contain_text(f"Reason: {REASON}")
    expect(page.locator(".vr-deactivation ul.vr-held")).to_contain_text("Technical staff")
    assert not User.objects.get(pk=person.pk).is_active
    assert page.console_errors == []


def test_a_portal_account_lists_its_portal_access_and_deactivates(page: Page, view: View):
    if not (view.phone and view.theme == "light"):
        pytest.skip("run at phone light")
    from clients.models import Client, PortalAccess

    organization = Client.objects.create(name="E2E Water Utility", account_number="E2EWAT")
    portal = make_user("e2e_portal", PASSWORD, client=organization)
    PortalAccess.objects.create(user=portal, client=organization, whole_organization=True)
    make_owner()
    sign_in(page, OWNER, OWNER_PASSWORD)

    open_deactivate(page, portal)
    held = page.locator("ul.vr-held")
    expect(held).to_contain_text("Portal access: Its organization's tickets")
    expect(held).not_to_contain_text("Roles:")
    no_sideways_scroll(page, view)

    page.get_by_label("Reason").fill(REASON)
    page.get_by_role("button", name="Deactivate", exact=True).click()
    expect(page).to_have_url(re.compile(rf"/admin/accounts/user/{portal.pk}/change/$"))
    expect(page.locator(".vr-deactivation")).to_contain_text(
        "Portal access: Its organization's tickets"
    )
    assert not User.objects.get(pk=portal.pk).is_active
    no_sideways_scroll(page, view)
    assert page.console_errors == []
