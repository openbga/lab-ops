"""Changes to client organizations and contacts: who may make them, what they record."""

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError

from clients.models import Client, ClientContact
from clients.services import add_contact, create_client, update_client, update_contact
from core.models import AuditEvent

from .conftest import MANAGERS, READ_ONLY

pytestmark = pytest.mark.django_db


@pytest.fixture
def manager(make_user):
    return make_user("manager", "account_manager")


@pytest.mark.parametrize("role", MANAGERS)
def test_client_managers_create_and_change_records(make_user, role):
    actor = make_user(f"actor-{role}", role)
    client = create_client(actor=actor, name="Example Clinic", reason="New account")
    contact = add_contact(
        actor=actor, client_id=client.pk, name="Pat Example", email="pat@example.invalid"
    )
    update_client(
        actor=actor, client_id=client.pk, expected_revision=1, reason="Add phone", phone="555-0100"
    )
    update_contact(
        actor=actor, contact_id=contact.pk, expected_revision=1, reason="Left", is_active=False
    )
    assert Client.objects.get().phone == "555-0100"
    assert not ClientContact.objects.get().is_active


def refused_actors(make_user, clinic, portal_user):
    users = [make_user(f"actor-{role}", role) for role in READ_ONLY]
    owner = make_user("owner", is_staff=True, is_superuser=True)  # no lab role: no access
    return [*users, owner, portal_user(clinic), AnonymousUser()]


@pytest.fixture
def portal_user(make_user):
    return lambda client: make_user("portal", client=client)


def test_every_service_refuses_accounts_without_a_client_manager_role(
    make_user, clinic, pat, portal_user
):
    for actor in refused_actors(make_user, clinic, portal_user):
        calls = [
            lambda actor=actor: create_client(actor=actor, name="Refused"),
            lambda actor=actor: update_client(
                actor=actor, client_id=clinic.pk, expected_revision=1, reason="x", name="Refused"
            ),
            lambda actor=actor: add_contact(
                actor=actor, client_id=clinic.pk, name="Refused", email="x@example.invalid"
            ),
            lambda actor=actor: update_contact(
                actor=actor, contact_id=pat.pk, expected_revision=1, reason="x", name="Refused"
            ),
        ]
        for call in calls:
            with pytest.raises(PermissionDenied):
                call()
    clinic.refresh_from_db()
    assert clinic.name == "Example Clinic" and clinic.revision == 1
    assert not Client.objects.filter(name="Refused").exists()
    assert not ClientContact.objects.filter(name="Refused").exists()
    assert not AuditEvent.objects.exists()


def test_inactive_manager_is_refused(make_user):
    actor = make_user("former", "account_manager")
    actor.is_active = False
    actor.save()
    with pytest.raises(PermissionDenied):
        create_client(actor=actor, name="Refused")


def test_creating_records_an_audit_event_with_the_reason(manager):
    client = create_client(
        actor=manager, name="  Example Clinic ", account_number=" EXCLIN ", reason=" New account "
    )
    assert (client.name, client.account_number, client.revision) == ("Example Clinic", "EXCLIN", 1)
    event = AuditEvent.objects.get()
    assert (event.action, event.object_type, event.object_id) == (
        "client.created",
        "clients.client",
        str(client.pk),
    )
    assert event.actor == manager and event.reason == "New account" and event.before == {}
    assert event.after["name"] == "Example Clinic" and event.after["account_number"] == "EXCLIN"


def test_update_records_before_and_after_and_requires_a_reason(manager, clinic):
    with pytest.raises(ValidationError) as refused:
        update_client(
            actor=manager, client_id=clinic.pk, expected_revision=1, reason="  ", phone="555-0199"
        )
    assert "reason" in refused.value.message_dict
    clinic.refresh_from_db()
    assert clinic.phone == "555-0100" and clinic.revision == 1 and not AuditEvent.objects.exists()

    update_client(
        actor=manager,
        client_id=clinic.pk,
        expected_revision=1,
        reason=" Number changed ",
        phone="555-0199",
        is_active=False,
    )
    clinic.refresh_from_db()
    assert clinic.phone == "555-0199" and not clinic.is_active and clinic.revision == 2
    event = AuditEvent.objects.get(action="client.updated")
    assert event.actor == manager and event.reason == "Number changed"
    assert (event.before["phone"], event.after["phone"]) == ("555-0100", "555-0199")
    assert (event.before["is_active"], event.after["is_active"]) == (True, False)
    assert event.before["name"] == event.after["name"] == "Example Clinic"


def test_a_stale_revision_is_refused_and_nothing_is_written(manager, clinic):
    update_client(
        actor=manager, client_id=clinic.pk, expected_revision=1, reason="First", phone="555-0101"
    )
    with pytest.raises(ValidationError, match="changed"):
        update_client(
            actor=manager,
            client_id=clinic.pk,
            expected_revision=1,
            reason="Second",
            phone="555-0102",
        )
    clinic.refresh_from_db()
    assert clinic.phone == "555-0101" and clinic.revision == 2
    assert AuditEvent.objects.filter(action="client.updated").count() == 1


def test_an_update_that_changes_nothing_is_refused(manager, clinic):
    with pytest.raises(ValidationError, match="Change at least one detail"):
        update_client(
            actor=manager,
            client_id=clinic.pk,
            expected_revision=1,
            reason="No change",
            name="Example Clinic",
        )
    clinic.refresh_from_db()
    assert clinic.revision == 1 and not AuditEvent.objects.exists()


def test_account_code_is_unique_when_given_and_may_be_left_blank(manager, clinic):
    with pytest.raises(ValidationError, match="account code"):
        create_client(actor=manager, name="Another Clinic", account_number="EXCLIN")
    create_client(actor=manager, name="No code one")
    create_client(actor=manager, name="No code two")  # blank codes never collide
    other = create_client(actor=manager, name="Another Clinic", account_number="EXOTHER")
    with pytest.raises(ValidationError, match="account code"):
        update_client(
            actor=manager,
            client_id=other.pk,
            expected_revision=1,
            reason="Retype code",
            account_number="EXCLIN",
        )
    other.refresh_from_db()
    assert other.account_number == "EXOTHER" and other.revision == 1
    assert Client.objects.count() == 4
    assert AuditEvent.objects.filter(action="client.created").count() == 3


def test_a_client_needs_a_name(manager):
    with pytest.raises(ValidationError) as refused:
        create_client(actor=manager, name="  ")
    assert "name" in refused.value.message_dict
    assert not Client.objects.exists() and not AuditEvent.objects.exists()


def test_a_contact_needs_an_email_address_or_a_phone_number(manager, clinic):
    with pytest.raises(ValidationError, match="email address or a phone number"):
        add_contact(actor=manager, client_id=clinic.pk, name="Pat Example")
    with pytest.raises(ValidationError, match="email address or a phone number"):
        add_contact(actor=manager, client_id=clinic.pk, name="Pat Example", email="  ", phone=" ")
    assert not ClientContact.objects.exists() and not AuditEvent.objects.exists()
    by_phone = add_contact(actor=manager, client_id=clinic.pk, name="Sam Example", phone="555-0101")
    by_email = add_contact(
        actor=manager, client_id=clinic.pk, name="Lee Example", email="lee@example.invalid"
    )
    assert by_phone.pk and by_email.pk
    with pytest.raises(ValidationError, match="email address or a phone number"):
        update_contact(
            actor=manager,
            contact_id=by_phone.pk,
            expected_revision=1,
            reason="Remove the only way to reach",
            phone="",
        )
    by_phone.refresh_from_db()
    assert by_phone.phone == "555-0101" and by_phone.revision == 1


def test_contact_events_record_values_and_reasons(manager, clinic):
    contact = add_contact(
        actor=manager,
        client_id=clinic.pk,
        name=" Pat Example ",
        role="Billing",
        email="pat@example.invalid",
    )
    added = AuditEvent.objects.get(action="client_contact.added")
    assert added.actor == manager and added.object_type == "clients.clientcontact"
    assert added.after["name"] == "Pat Example" and added.after["client"] == clinic.pk
    assert added.before == {} and added.reason == ""

    with pytest.raises(ValidationError) as refused:
        update_contact(
            actor=manager, contact_id=contact.pk, expected_revision=1, reason="", role="Results"
        )
    assert "reason" in refused.value.message_dict
    update_contact(
        actor=manager,
        contact_id=contact.pk,
        expected_revision=1,
        reason="Moved to results",
        role="Results",
    )
    with pytest.raises(ValidationError, match="changed"):
        update_contact(
            actor=manager,
            contact_id=contact.pk,
            expected_revision=1,
            reason="Stale",
            role="Billing",
        )
    contact.refresh_from_db()
    assert (contact.role, contact.revision) == ("Results", 2)
    updated = AuditEvent.objects.get(action="client_contact.updated")
    assert (updated.before["role"], updated.after["role"]) == ("Billing", "Results")
    assert updated.reason == "Moved to results" and updated.actor == manager
