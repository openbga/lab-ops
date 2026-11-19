"""The admin role's navigation and client organization creation, through the normal workspace."""

import re

import pytest
from helpers import field, sign_in
from playwright.sync_api import Page, expect

from accounts.models import User
from core.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)


def test_admin_can_create_client_organization_from_workspace(page: Page, demo):
    from clients.models import Client

    admin = User.objects.get(username="demo_admin")
    assert set(admin.groups.values_list("name", flat=True)) == {"admin"}
    assert not admin.is_superuser
    sign_in(page, admin.username, demo.password)
    expect(page.get_by_role("heading", name="Dashboard")).to_be_visible()
    expect(page.get_by_role("link", name="New organization")).to_be_visible()
    # The admin role is not a deployment owner: no link into the account console.
    expect(page.get_by_role("link", name="Accounts & roles")).to_have_count(0)
    page.get_by_role("link", name="Client organizations", exact=True).click()
    page.get_by_role("link", name="New organization", exact=True).click()
    field(page, "Organization name").fill("Synthetic Admin Browser Client")
    field(page, "General email").fill("admin-browser@example.invalid")
    page.get_by_role("button", name="Create organization", exact=True).click()
    expect(page.get_by_text("Synthetic Admin Browser Client added.")).to_be_visible()
    # Creating an organization opens its page (standard 7.6: after saving, the record page).
    expect(page).to_have_url(re.compile(r"/clients/\d+/$"))
    expect(page.get_by_role("heading", level=1)).to_have_text("Synthetic Admin Browser Client")
    expect(page.get_by_role("link", name="Add contact", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Edit organization", exact=True)).to_be_visible()
    # An administrator adds portal accounts in the account console, which this role cannot open.
    expect(page.get_by_role("link", name="Add portal account")).to_have_count(0)
    client = Client.objects.get(name="Synthetic Admin Browser Client")
    assert AuditEvent.objects.get(action="client.created", object_id=str(client.pk)).actor == admin
    assert set(admin.groups.values_list("name", flat=True)) == {"admin"}
    assert page.console_errors == []


def test_scheduled_jobs_account_page_offers_no_password_action(page: Page, demo, view):
    """An owner opening the scheduled jobs' account sees its password status and why it has
    none, with no link to the password form that refuses it, at every width and theme."""
    from django.urls import reverse

    from jobs.actor import system_account

    owner = User.objects.get(username="demo_admin")
    owner.is_staff = owner.is_superuser = True
    owner.save(update_fields=["is_staff", "is_superuser"])
    sign_in(page, owner.username, demo.password)
    actor = system_account()
    page.goto(reverse("admin:accounts_user_change", args=[actor.pk]))
    expect(page.get_by_text("No password set.")).to_be_visible()
    expect(
        page.get_by_text(
            "Nobody signs in as this account: it has no password, and none can be set."
        )
    ).to_be_visible()
    password_form = reverse("admin:auth_user_password_change", args=[actor.pk])
    expect(page.locator(f'a[href$="{actor.pk}/password/"]')).to_have_count(0)
    expect(page.get_by_role("link", name=re.compile("(Set|Reset) password"))).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    # An ordinary account's page still offers its password action.
    page.goto(reverse("admin:accounts_user_change", args=[owner.pk]))
    expect(page.get_by_role("link", name=re.compile("(Set|Reset) password"))).to_be_visible()
    assert page.request.get(password_form).status == 403
    assert page.console_errors == []
