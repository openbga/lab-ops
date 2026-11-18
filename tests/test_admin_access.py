"""An admin-only account can do the clients work without any explicit capability."""

import pytest
from django.urls import reverse

from accounts.roles import has_capability
from clients.models import Client, ClientContact
from core.models import AuditEvent


@pytest.mark.django_db
def test_admin_creates_client_and_contact_with_audited_changes(client, staff):
    admin = staff("operational_admin", "admin")
    client.force_login(admin)
    listing = client.get(reverse("clients:client-list"))
    assert b"New organization" in listing.content
    created = client.post(
        reverse("clients:client-create"),
        {"name": "Synthetic Admin Client", "email": "front-desk@example.invalid"},
    )
    assert created.status_code == 302
    customer = Client.objects.get(name="Synthetic Admin Client")
    assert (
        client.post(
            reverse("clients:contact-create", args=[customer.pk]),
            {"name": "Sam Example", "email": "sam@example.invalid"},
        ).status_code
        == 302
    )
    assert ClientContact.objects.get(client=customer).name == "Sam Example"
    assert set(
        AuditEvent.objects.filter(
            action__in=["client.created", "client_contact.added"]
        ).values_list("actor_id", flat=True)
    ) == {admin.pk}
    assert set(admin.groups.values_list("name", flat=True)) == {"admin"}
    assert not has_capability(admin, "view_patient_information")
