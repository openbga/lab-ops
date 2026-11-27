"""A portal account moved to another organization keeps no grant from the old one (C4).

The access is reset when the move is saved, attributed and audited (`portal_access.reset`):
through the account administration, and at the model boundary for any other save. A move that
bypasses save() (queryset.update) still grants nothing, because the access records the
organization it was granted for. Synthetic data only."""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.forms import MultiWidget
from django.urls import reverse

from accounts.models import User
from clients.models import ClientContact, ClientSite, PortalAccess
from clients.services import reset_portal_access, set_portal_access
from core.models import AuditEvent
from desk.models import EntryKind
from desk.notifications import client_recipients
from desk.portal_scope import portal_requests, portal_sites
from desk.services import create_request

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db


@pytest.fixture
def manager(make_user):
    return make_user("manager", "account_manager")


@pytest.fixture
def owner(make_user):
    return make_user("owner", "admin", is_staff=True, is_superuser=True)


@pytest.fixture
def moved(make_user, manager, clinic, clinic_site, clinic_contact):
    """A clinic portal account linked to a clinic contact, narrowed to the clinic's main site."""
    account = make_user("moving_portal", client=clinic)
    set_portal_access(
        actor=manager,
        user_id=account.pk,
        expected_client_id=account.client_id,
        contact=clinic_contact,
        sites=[clinic_site],
    )
    return account


@pytest.fixture
def whole_moved(make_user, manager, clinic):
    account = make_user("moving_whole", client=clinic)
    set_portal_access(
        actor=manager,
        user_id=account.pk,
        expected_client_id=account.client_id,
        whole_organization=True,
    )
    return account


def log(agent, category, client, site=None, contact=None):
    return create_request(
        actor=agent,
        category=category,
        subject="Logged",
        first_entry_kind=EntryKind.NOTE,
        first_entry_body="Internal.",
        channel="phone",
        client=client,
        site=site,
        contact=contact,
    )


def admin_change(http, account, **changes):
    """POST the account administration's change form with its current values, changed."""
    url = reverse("admin:accounts_user_change", args=[account.pk])
    form = http.get(url).context["adminform"].form
    data = {}
    for name, field in form.fields.items():
        if field.disabled:
            continue
        value = form[name].value()
        if value is None or value is False:
            continue
        if isinstance(field.widget, MultiWidget):  # date and time as two inputs
            for index, part in enumerate(field.widget.decompress(value)):
                data[f"{name}_{index}"] = part
            continue
        if isinstance(value, (list, tuple)):
            data[name] = [getattr(v, "pk", v) for v in value]
        elif value is True:
            data[name] = "on"
        else:
            data[name] = getattr(value, "pk", value)
    data.update(changes)
    return http.post(url, data)


def assert_reset(account, organization_id, actor):
    access = PortalAccess.objects.get(user=account)
    assert access.client_id == organization_id
    assert access.contact_id is None and not access.whole_organization
    assert not access.sites.exists()
    event = AuditEvent.objects.filter(action="portal_access.reset").get()
    assert event.actor == actor
    assert event.after == {
        "client": organization_id,
        "contact": None,
        "whole_organization": False,
        "sites": [],
        "ends_at": None,
        "user": account.pk,
    }
    return event


def test_admin_move_resets_access_and_audits(
    client, owner, moved, water, clinic, clinic_site, clinic_contact
):
    client.force_login(owner)
    response = admin_change(client, moved, client=water.pk)
    assert response.status_code == 302, response.context["adminform"].form.errors
    moved.refresh_from_db()
    assert moved.client == water
    event = assert_reset(moved, water.pk, owner)
    assert event.before == {
        "client": clinic.pk,
        "contact": clinic_contact.pk,
        "whole_organization": False,
        "sites": [clinic_site.pk],
        "ends_at": None,
    }
    assert event.reason
    # The account administration's own audit of the account is still written.
    assert AuditEvent.objects.filter(action="account.updated", object_id=str(moved.pk)).exists()


def test_admin_move_of_a_whole_organization_account_grants_nothing_there(
    client, owner, whole_moved, agent, category, clinic, water, water_site
):
    water_request = log(agent, category, water, water_site)
    client.force_login(owner)
    assert admin_change(client, whole_moved, client=water.pk).status_code == 302
    whole_moved.refresh_from_db()
    assert_reset(whole_moved, water.pk, owner)
    assert list(portal_requests(whole_moved)) == []
    assert water_request not in portal_requests(whole_moved)
    client.force_login(whole_moved)
    url = reverse("portal:request-detail", args=[water_request.number])
    assert client.get(url).status_code == 404


def test_portal_and_staff_accounts_keep_their_kind(client, owner, moved, agent, clinic):
    """No conversion either way, through the
    admin form, a save, or an update that bypasses save()."""
    client.force_login(owner)
    for account, value in ((moved, ""), (agent, clinic.pk)):
        response = admin_change(client, account, client=value)
        assert response.status_code == 200
        assert "for life" in str(response.context["adminform"].form.errors["client"])
    assert User.objects.get(pk=moved.pk).client == clinic
    assert User.objects.get(pk=agent.pk).client is None
    for account, value in ((moved, None), (agent, clinic)):
        account.client = value
        account.changed_by = owner
        with pytest.raises(ValidationError):
            account.save()
    for pk, value in ((moved.pk, None), (agent.pk, clinic.pk)):
        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=pk).update(client=value)
    assert PortalAccess.objects.get(user=moved).sites.exists()  # untouched by the refusals


def test_save_without_an_actor_is_refused(moved, water, clinic):
    moved.client = water
    with pytest.raises(ValidationError):
        moved.save()
    assert User.objects.get(pk=moved.pk).client == clinic
    access = PortalAccess.objects.get(user=moved)
    assert access.client == clinic and access.sites.exists()


def test_save_with_an_actor_resets_and_audits(moved, water, owner):
    moved.client = water
    moved.changed_by = owner
    moved.save()
    assert_reset(moved, water.pk, owner)


def test_account_without_access_moves_freely(make_user, clinic, water):
    account = make_user("plain_portal", client=clinic)
    account.client = water
    account.save()
    assert not PortalAccess.objects.filter(user=account).exists()
    assert not AuditEvent.objects.filter(action="portal_access.reset").exists()


def test_move_back_does_not_restore_the_old_grant(moved, clinic, water, owner):
    for organization in (water, clinic):
        moved.client = organization
        moved.changed_by = owner
        moved.save()
    access = PortalAccess.objects.get(user=moved)
    assert access.client == clinic and not access.sites.exists() and access.contact is None
    first, second = AuditEvent.objects.filter(action="portal_access.reset").order_by("occurred_at")
    assert first.before["sites"] and second.before["client"] == water.pk
    assert second.after["client"] == clinic.pk and second.after["sites"] == []


def test_move_that_bypasses_save_grants_nothing(
    client,
    manager,
    moved,
    whole_moved,
    agent,
    category,
    clinic,
    clinic_site,
    clinic_contact,
    water,
    water_site,
):
    water_request = log(agent, category, water, water_site)
    User.objects.filter(pk__in=[moved.pk, whole_moved.pk]).update(client=water)
    for account in (moved, whole_moved):
        account.refresh_from_db()
        assert list(portal_requests(account)) == []
        assert list(portal_sites(account)) == []  # no grant: no site choice
    # Not a recipient for the old contact's requests, nor for the new organization's.
    ServiceRequestContact = log(agent, category, water, water_site)
    assert client_recipients(ServiceRequestContact) == []
    assert water_request not in portal_requests(whole_moved)
    # Staff see the access as not set, and setting it replaces the stale grant.
    client.force_login(manager)
    page = client.get(reverse("clients:client-detail", args=[water.pk])).content.decode()
    assert "access not set" in page
    url = reverse("clients:portal-access-edit", args=[water.pk, moved.pk])
    response = client.get(url)
    assert "Access not set" in response.content.decode()
    form = response.context["form"]
    assert list(form.fields["sites"].queryset) == [water_site]
    assert form.initial["contact"] is None
    stale = PortalAccess.objects.get(user=moved)
    response = client.post(
        url,
        {
            "scope": "sites",
            "sites": [water_site.pk],
            "expected_revision": stale.revision,
            "reason": "Moved to the water authority",
        },
    )
    assert response.status_code == 302
    access = PortalAccess.objects.get(user=moved)
    assert access.client == water and list(access.sites.all()) == [water_site]
    assert access.applies_to(moved)


def test_reset_service_is_a_no_op_when_nothing_is_granted(make_user, owner, clinic):
    account = make_user("empty_portal", client=clinic)
    assert (
        reset_portal_access(actor=owner, user_id=account.pk, expected_client_id=account.client_id)
        is None
    )
    PortalAccess.objects.create(user=account, client=clinic)
    assert (
        reset_portal_access(actor=owner, user_id=account.pk, expected_client_id=account.client_id)
        is None
    )
    assert not AuditEvent.objects.filter(action="portal_access.reset").exists()


def test_portal_account_signed_in_after_move_sees_only_its_own(
    client, owner, moved, agent, category, clinic, clinic_site, clinic_contact, water
):
    old = log(agent, category, clinic, clinic_site, ClientContact.objects.get(pk=clinic_contact.pk))
    client.force_login(owner)
    assert admin_change(client, moved, client=water.pk).status_code == 302
    client.logout()
    assert client.login(username=moved.username, password=PASSWORD)
    assert client.get(reverse("portal:request-detail", args=[old.number])).status_code == 404
    home = client.get(reverse("portal:home"))
    assert home.context["open_count"] == 0
    assert ClientSite.objects.filter(client=water).count() == 0  # nothing created on the way
