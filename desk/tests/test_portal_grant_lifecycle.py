"""Portal grants during upgrades and account moves, and notifications after withdrawal."""

import importlib

import pytest
from django.apps import apps
from django.core import mail
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from clients import views as client_views
from clients.models import Client, ClientContact, ClientSite, PortalAccess
from clients.portal import current_access
from clients.services import reset_portal_access, set_portal_access
from core.models import AuditEvent
from desk.models import RequestEntry, Status
from desk.portal_scope import portal_requests
from desk.services import add_entry

from .test_portal import raise_staff

pytestmark = pytest.mark.django_db

backfill = importlib.import_module("clients.migrations.0005_backfill_portal_access_client").backfill


@pytest.fixture
def owner(make_user):
    return make_user("review_owner", "admin", is_staff=True, is_superuser=True)


@pytest.fixture
def manager(make_user):
    return make_user("review_manager", "account_manager")


@pytest.fixture
def destination(db):
    return Client.objects.create(name="Example Destination")


# the upgrade keeps a grant only where it was evidently made


def legacy_grant(user, *, granted_for, contact=None, whole=True, sites=(), actor=None):
    """Portal access as it stood at clients/0004: no organization recorded yet, and the
    `portal_access.set` event the services wrote then (naming the account's organization)."""
    access = PortalAccess.objects.create(
        user=user, client=None, contact=contact, whole_organization=whole
    )
    access.sites.set(sites)
    if granted_for is not None:
        AuditEvent.objects.create(
            actor=actor or user,
            action="portal_access.set",
            object_type="clients.portalaccess",
            object_id=str(access.pk),
            after={"client": granted_for, "whole_organization": whole, "user": user.pk},
        )
    return access


def test_upgrade_preserves_original_grant_boundary(agent, category, clinic, destination, make_user):
    raise_staff(agent, category, destination)
    user = make_user("moved", client=destination)
    contact = ClientContact.objects.create(client=clinic, name="Origin Contact", phone="555")
    legacy_grant(user, granted_for=clinic.pk, contact=contact)
    backfill(apps, None)
    access = PortalAccess.objects.get(user=user)
    assert (access.client_id, access.contact_id, access.whole_organization) == (
        destination.pk,
        None,
        False,
    )
    assert not portal_requests(user).exists() and current_access(user) is None


def test_upgrade_resets_grants_without_evidence_or_with_foreign_sites(
    clinic, clinic_site, destination, make_user
):
    no_evidence = make_user("no_evidence", client=destination)
    legacy_grant(no_evidence, granted_for=None)
    foreign_site = make_user("foreign_site", client=destination)
    legacy_grant(foreign_site, granted_for=destination.pk, whole=False, sites=[clinic_site])
    other_org = make_user("other_org", client=destination)
    legacy_grant(other_org, granted_for=clinic.pk)  # moved after a whole-organization grant
    backfill(apps, None)
    for user in (no_evidence, foreign_site, other_org):
        access = PortalAccess.objects.get(user=user)
        assert access.client_id == destination.pk and current_access(user) is None
        assert not access.sites.exists()


def test_upgrade_keeps_an_evident_grant(clinic, clinic_site, clinic_contact, make_user):
    whole = make_user("kept_whole", client=clinic)
    legacy_grant(whole, granted_for=clinic.pk, contact=clinic_contact)
    narrow = make_user("kept_narrow", client=clinic)
    legacy_grant(narrow, granted_for=clinic.pk, whole=False, sites=[clinic_site])
    backfill(apps, None)
    assert current_access(whole).whole_organization
    assert current_access(whole).contact == clinic_contact
    assert list(current_access(narrow).sites.all()) == [clinic_site]


# a write stays in the organization its form was decided for


def test_first_grant_belongs_to_organization_on_form(
    client, manager, owner, clinic, destination, make_user, monkeypatch
):
    user = make_user("moving", client=clinic)
    write = client_views.set_portal_access

    def transfer_then_write(**data):
        user.client = destination
        user.changed_by = owner
        user.save()
        return write(**data)

    monkeypatch.setattr(client_views, "set_portal_access", transfer_then_write)
    client.force_login(manager)
    response = client.post(
        reverse("clients:portal-access-edit", args=[clinic.pk, user.pk]),
        {"scope": "organization", "expected_revision": 0},
    )
    assert response.status_code == 200
    assert "moved to another organization" in response.content.decode()
    assert not PortalAccess.objects.filter(whole_organization=True).exists()


def test_every_portal_access_write_checks_the_expected_organization(
    manager, owner, clinic, destination, make_user
):
    user = make_user("checked", client=clinic)
    with pytest.raises(ValidationError, match="moved"):
        set_portal_access(
            actor=manager,
            user_id=user.pk,
            whole_organization=True,
            expected_client_id=destination.pk,
        )
    set_portal_access(
        actor=manager, user_id=user.pk, whole_organization=True, expected_client_id=clinic.pk
    )
    with pytest.raises(ValidationError, match="moved"):
        reset_portal_access(actor=owner, user_id=user.pk, expected_client_id=destination.pk)
    assert current_access(user).whole_organization


# a move empties the grant in the database, so a return revives nothing


def test_return_after_bulk_move_requires_new_grant(
    manager, clinic, clinic_site, destination, clinic_contact, make_user
):
    user = make_user("returning", client=clinic)
    set_portal_access(
        actor=manager,
        user_id=user.pk,
        contact=clinic_contact,
        sites=[clinic_site],
        expected_client_id=clinic.pk,
    )
    revision = PortalAccess.objects.get(user=user).revision
    for organization in (destination, clinic):
        User.objects.filter(pk=user.pk).update(client=organization)
        user.refresh_from_db()
        assert current_access(user) is None
    access = PortalAccess.objects.get(user=user)
    assert (access.client_id, access.contact_id, access.whole_organization) == (
        clinic.pk,
        None,
        False,
    )
    assert not access.sites.exists() and access.revision == revision + 2


def test_admin_move_still_records_what_it_took_away(
    owner, manager, clinic, clinic_site, destination, make_user
):
    user = make_user("audited", client=clinic)
    set_portal_access(
        actor=manager, user_id=user.pk, sites=[clinic_site], expected_client_id=clinic.pk
    )
    user.client = destination
    user.changed_by = owner
    user.save()
    event = AuditEvent.objects.get(action="portal_access.reset")
    assert event.actor == owner
    assert event.before["sites"] == [clinic_site.pk] and event.before["client"] == clinic.pk
    assert event.after["client"] == destination.pk and event.after["sites"] == []
    assert ClientSite.objects.filter(pk=clinic_site.pk).exists()


# a notice about a reply needs the reply to be still client-visible


@pytest.fixture
def recipient(manager, clinic, clinic_contact, make_user):
    user = make_user("recipient", client=clinic)
    set_portal_access(
        actor=manager,
        user_id=user.pk,
        whole_organization=True,
        contact=clinic_contact,
        expected_client_id=clinic.pk,
    )
    return user


@pytest.fixture
def locmem(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


def withdraw(entry, by):
    RequestEntry.objects.filter(pk=entry.pk).update(
        withdrawn_at=timezone.now(), withdrawn_by=by, withdrawal_reason="Recorded in error"
    )


def test_pending_withdrawal_suppresses_reply_only_notice(
    locmem,
    recipient,
    agent,
    category,
    clinic,
    clinic_contact,
    make_user,
    django_capture_on_commit_callbacks,
):
    supervisor = make_user("withdrawer", "supervisor")
    with django_capture_on_commit_callbacks(execute=True):
        record = raise_staff(agent, category, clinic, contact=clinic_contact)
        withdraw(record.entries.get(kind="reply"), supervisor)
    assert not mail.outbox


def test_withdrawn_reply_leaves_the_status_notice(
    locmem,
    recipient,
    agent,
    category,
    clinic,
    clinic_contact,
    make_user,
    django_capture_on_commit_callbacks,
):
    from desk import notifications

    supervisor = make_user("withdrawer", "supervisor")
    record = raise_staff(agent, category, clinic, contact=clinic_contact)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        reply = add_entry(
            actor=agent,
            request=record,
            kind="reply",
            body="Please send the form.",
            expected_revision=record.revision,
            new_status=Status.WAITING_CLIENT,
        )
        withdraw(reply, supervisor)
    assert len(mail.outbox) == 1
    assert notifications.PHRASES["waiting_client"] in mail.outbox[0].body
