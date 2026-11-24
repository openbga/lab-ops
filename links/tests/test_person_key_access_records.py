"""Revealing a person-level key leaves an access record (core.access) besides its change event,
and Who opened it shows the row without the key or its masked label; a refused reveal records
nothing. Synthetic data only."""

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from core.models import AccessRecord, AuditEvent

from .conftest import PASSWORD, PATIENT_KEY

pytestmark = pytest.mark.django_db


def test_a_person_key_reveal_is_recorded_besides_its_change_event(holder, person, client):
    client.login(username=holder.username, password=PASSWORD)
    url = reverse("link_records:attachment-reveal", args=[person.pk])
    response = client.post(url, HTTP_HX_REQUEST="true")
    assert response.status_code == 200 and PATIENT_KEY in response.content.decode()
    [record] = AccessRecord.objects.all()
    assert (record.account_id, record.kind, record.capacity) == (
        holder.pk,
        "person_key",
        "laboratory",
    )
    assert (record.ticket_id, record.object_type, record.object_id) == (
        person.request_id,
        "links.linkattachment",
        str(person.pk),
    )
    event = AuditEvent.objects.get(action="external_link.person_revealed")
    assert event.request_ref == record.request_ref == response["X-Request-ID"]
    assert PATIENT_KEY not in str(vars(record))


def test_a_refused_person_key_reveal_records_nothing(agent, person, client):
    client.login(username=agent.username, password=PASSWORD)
    url = reverse("link_records:attachment-reveal", args=[person.pk])
    assert client.post(url).status_code == 404
    assert client.get(url).status_code == 405
    assert not AccessRecord.objects.exists()


def test_who_opened_it_shows_a_person_key_row_without_the_key(holder, person, client):
    holder.groups.add(Group.objects.get_or_create(name="supervisor")[0])
    client.login(username=holder.username, password=PASSWORD)
    client.post(reverse("link_records:attachment-reveal", args=[person.pk]))
    page = client.get(reverse("desk:request-access", args=[person.request_id]))
    html = page.content.decode()
    assert "Shown a person-level key" in html and f"Link {person.pk}" in html
    assert PATIENT_KEY not in html
