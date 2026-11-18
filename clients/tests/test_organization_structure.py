"""C1 organization type, parent and account owner; C3 contact responsibilities and tap-to-call;
C4 portal access (linked contact and scope)."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from clients.models import Client, ClientContact, PortalAccess
from clients.services import (
    add_contact,
    create_client,
    set_portal_access,
    update_client,
    update_contact,
)
from core.models import AuditEvent

from .conftest import PASSWORD, READ_ONLY

pytestmark = pytest.mark.django_db


@pytest.fixture
def manager(make_user):
    return make_user("manager", "account_manager")


def _reparent(actor, client, parent):
    client.refresh_from_db()
    return update_client(
        actor=actor,
        client_id=client.pk,
        expected_revision=client.revision,
        reason="Group change",
        parent=parent,
    )


# C1 -----------------------------------------------------------------------------------------


def test_parent_and_type_are_recorded_and_audited(manager):
    system = create_client(
        actor=manager, name="Example Health System", organization_type="healthcare"
    )
    hospital = create_client(actor=manager, name="Example General Hospital", parent=system)
    assert hospital.parent == system
    assert list(system.children.all()) == [hospital]
    created = AuditEvent.objects.get(action="client.created", object_id=str(hospital.pk))
    assert created.after["parent"] == system.pk


def test_organization_cannot_be_its_own_parent(manager):
    org = create_client(actor=manager, name="Example Org")
    with pytest.raises(ValidationError) as exc:
        _reparent(manager, org, org)
    assert "parent" in exc.value.message_dict


def test_two_level_cycle_refused(manager):
    a = create_client(actor=manager, name="Example A")
    b = create_client(actor=manager, name="Example B", parent=a)
    with pytest.raises(ValidationError) as exc:
        _reparent(manager, a, b)
    assert "parent" in exc.value.message_dict
    a.refresh_from_db()
    assert a.parent is None


def test_three_level_cycle_refused(manager):
    a = create_client(actor=manager, name="Example A")
    b = create_client(actor=manager, name="Example B", parent=a)
    c = create_client(actor=manager, name="Example C", parent=b)
    with pytest.raises(ValidationError):
        _reparent(manager, a, c)
    # Moving within the tree without a loop is fine.
    _reparent(manager, c, a)
    assert Client.objects.get(pk=c.pk).parent == a


def test_database_refuses_self_parent():
    org = Client.objects.create(name="Example Org")
    org.parent_id = org.pk
    with pytest.raises(IntegrityError), transaction.atomic():
        org.save()


def test_account_owner_must_be_active_staff(manager, make_user, clinic):
    portal = make_user("portal", client=clinic)
    no_role = make_user("norole")
    inactive = make_user("gone", "client_services", is_active=False)
    for owner in (portal, no_role, inactive):
        with pytest.raises(ValidationError) as exc:
            create_client(actor=manager, name="Example Org", account_owner=owner)
        assert "account_owner" in exc.value.message_dict
    staff = make_user("owner", "account_manager")
    org = create_client(actor=manager, name="Example Org", account_owner=staff)
    assert org.account_owner == staff
    # An owner who later lost their role does not block unrelated edits.
    staff.groups.clear()
    update_client(
        actor=manager, client_id=org.pk, expected_revision=1, reason="Phone", phone="555-0199"
    )
    with pytest.raises(ValidationError):
        update_client(
            actor=manager, client_id=clinic.pk, expected_revision=1, reason="x", account_owner=staff
        )


def test_client_form_saves_type_parent_and_owner(client, manager, clinic):
    system = Client.objects.create(name="Example Health System")
    client.force_login(manager)
    response = client.post(
        reverse("clients:client-edit", args=[clinic.pk]),
        {
            "name": clinic.name,
            "account_number": clinic.account_number,
            "organization_type": "practice",
            "parent": system.pk,
            "account_owner": manager.pk,
            "email": clinic.email,
            "phone": clinic.phone,
            "city": clinic.city,
            "state": clinic.state,
            "notes": clinic.notes,
            "is_active": "on",
            "expected_revision": clinic.revision,
            "reason": "Joined the health system",
        },
    )
    assert response.status_code == 302
    clinic.refresh_from_db()
    assert (clinic.organization_type, clinic.parent, clinic.account_owner) == (
        "practice",
        system,
        manager,
    )
    page = client.get(reverse("clients:client-detail", args=[system.pk])).content.decode()
    assert "Member organizations" in page and clinic.name in page


def test_list_filters_by_organization_type(client, manager, clinic, water):
    water.organization_type = "utility"
    water.save()
    client.force_login(manager)
    page = client.get(reverse("clients:client-list"), {"organization_type": "utility"})
    names = [c.name for c in page.context["page_obj"]]
    assert names == [water.name]


# C3 -----------------------------------------------------------------------------------------


def test_contact_responsibilities_are_validated_and_ordered(manager, clinic):
    contact = add_contact(
        actor=manager,
        client_id=clinic.pk,
        name="Example Liaison",
        email="liaison@clinic.example.invalid",
        responsibilities=["results", "ordering"],
    )
    assert contact.responsibilities == ["ordering", "results"]
    assert contact.get_responsibilities_display() == ["Ordering", "Results and reports"]
    with pytest.raises(ValidationError):
        update_contact(
            actor=manager,
            contact_id=contact.pk,
            expected_revision=1,
            reason="x",
            responsibilities=["signing_authority"],
        )


def test_contact_list_has_tap_to_call_and_email(client, manager, clinic):
    ClientContact.objects.create(
        client=clinic,
        name="Example Courier",
        email="courier@clinic.example.invalid",
        phone="555 0102",
        responsibilities=["logistics"],
    )
    client.force_login(manager)
    page = client.get(reverse("clients:client-detail", args=[clinic.pk])).content.decode()
    assert 'href="tel:5550102"' in page
    assert 'href="mailto:courier@clinic.example.invalid"' in page
    assert 'href="tel:555-0100"' in page  # the organization's main phone
    assert "Supplies and pickups" in page


def test_contact_form_saves_responsibilities(client, manager, clinic, pat):
    client.force_login(manager)
    response = client.post(
        reverse("clients:contact-edit", args=[clinic.pk, pat.pk]),
        {
            "name": pat.name,
            "role": pat.role,
            "email": pat.email,
            "responsibilities": ["billing", "general"],
            "is_active": "on",
            "expected_revision": 1,
            "reason": "Also the general contact",
        },
    )
    assert response.status_code == 302
    pat.refresh_from_db()
    assert pat.responsibilities == ["billing", "general"]


# C4 -----------------------------------------------------------------------------------------


@pytest.fixture
def portal_account(make_user, clinic):
    return make_user("portal", client=clinic)


def test_portal_access_set_then_changed_with_reason_and_audit(manager, portal_account, pat):
    access = set_portal_access(
        actor=manager,
        user_id=portal_account.pk,
        expected_client_id=portal_account.client_id,
        contact=pat,
        whole_organization=True,
    )
    assert (access.contact, access.whole_organization, access.revision) == (pat, True, 1)
    with pytest.raises(ValidationError):  # changing it needs a reason
        set_portal_access(
            actor=manager,
            user_id=portal_account.pk,
            expected_client_id=portal_account.client_id,
            contact=None,
            whole_organization=True,
            expected_revision=1,
        )
    set_portal_access(
        actor=manager,
        user_id=portal_account.pk,
        expected_client_id=portal_account.client_id,
        contact=None,
        whole_organization=True,
        expected_revision=1,
        reason="Contact left",
    )
    events = AuditEvent.objects.filter(action="portal_access.set").order_by("occurred_at")
    assert [e.reason for e in events] == ["", "Contact left"]
    assert events[1].before["contact"] == pat.pk and events[1].after["contact"] is None
    assert events[1].after["client"] == portal_account.client_id
    assert PortalAccess.objects.get().revision == 2


def test_portal_access_refuses_stale_and_unchanged(manager, portal_account):
    set_portal_access(
        actor=manager,
        user_id=portal_account.pk,
        expected_client_id=portal_account.client_id,
        whole_organization=True,
    )
    with pytest.raises(ValidationError, match="changed"):
        set_portal_access(
            actor=manager,
            user_id=portal_account.pk,
            expected_client_id=portal_account.client_id,
            whole_organization=False,
            expected_revision=0,
            reason="x",
        )
    with pytest.raises(ValidationError, match="at least one"):
        set_portal_access(
            actor=manager,
            user_id=portal_account.pk,
            expected_client_id=portal_account.client_id,
            whole_organization=True,
            expected_revision=1,
            reason="x",
        )


def test_portal_access_refuses_contact_of_another_organization(manager, portal_account, water):
    stranger = ClientContact.objects.create(client=water, name="Other", phone="555-0109")
    with pytest.raises(ValidationError) as exc:
        set_portal_access(
            actor=manager,
            user_id=portal_account.pk,
            expected_client_id=portal_account.client_id,
            contact=stranger,
        )
    assert "contact" in exc.value.message_dict
    assert not PortalAccess.objects.exists()


def test_portal_access_is_for_portal_accounts_and_client_managers(
    make_user, manager, portal_account
):
    staff = make_user("staffer", "technical")
    with pytest.raises(ValidationError):
        set_portal_access(
            actor=manager,
            user_id=staff.pk,
            expected_client_id=staff.client_id,
            whole_organization=True,
        )
    for role in READ_ONLY:
        with pytest.raises(PermissionDenied):
            set_portal_access(
                actor=make_user(f"r-{role}", role),
                user_id=portal_account.pk,
                expected_client_id=portal_account.client_id,
                whole_organization=True,
            )
    with pytest.raises(PermissionDenied):
        set_portal_access(
            actor=portal_account,
            user_id=portal_account.pk,
            expected_client_id=portal_account.client_id,
            whole_organization=True,
        )


def test_portal_access_page(client, make_user, manager, portal_account, pat, water):
    url = reverse("clients:portal-access-edit", args=[pat.client_id, portal_account.pk])
    client.force_login(make_user("tech", "technical"))
    assert client.get(url).status_code == 403
    client.force_login(manager)
    # Another organization's URL for this account is a 404.
    assert (
        client.get(
            reverse("clients:portal-access-edit", args=[water.pk, portal_account.pk])
        ).status_code
        == 404
    )
    page = client.get(url)
    assert page.status_code == 200 and "Access not set" in page.content.decode()
    response = client.post(
        url, {"contact": pat.pk, "scope": "organization", "expected_revision": 0}
    )
    assert response.status_code == 302
    detail = client.get(reverse("clients:client-detail", args=[pat.client_id])).content.decode()
    assert "The whole organization" in detail
    # A stale second form keeps its input and explains.
    response = client.post(
        url,
        {"contact": "", "scope": "organization", "expected_revision": 0, "reason": "Stale"},
    )
    assert response.status_code == 200
    assert "changed" in response.content.decode()


def test_portal_account_cannot_reach_staff_pages(client, portal_account, clinic):
    client.login(username=portal_account.username, password=PASSWORD)
    url = reverse("clients:portal-access-edit", args=[clinic.pk, portal_account.pk])
    assert client.get(url).status_code == 403
