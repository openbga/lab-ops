"""Custom fields at ticket intake: create_request's on_created runs in
the creation's transaction, and the intake form and request page render the two template slots."""

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from core.models import AuditEvent
from desk.models import ServiceRequest
from desk.tests.test_contract import new_request

pytestmark = pytest.mark.django_db


def test_on_created_sees_the_written_request(agent, category):
    seen = []

    def keep(request):
        seen.append((request.number, request.entries.count()))

    created = new_request(agent, category, on_created=keep)
    assert seen == [(created.number, 2)]  # the created event and the first entry


def test_on_created_failure_undoes_the_whole_creation(agent, category):
    def refuse(request):
        raise ValidationError({"details": "A required request-type field is missing."})

    with pytest.raises(ValidationError):
        new_request(agent, category, on_created=refuse)
    assert not ServiceRequest.objects.exists()
    assert not AuditEvent.objects.filter(action="request.created").exists()


def test_category_section_is_one_swappable_block(client, agent, category):
    client.force_login(agent)
    form = client.get(reverse("desk:request-create")).content.decode()
    assert 'id="category-section"' in form
    partial = client.get(reverse("desk:category-guidance"), {"category": category.pk})
    assert partial.status_code == 200
    names = [t.name for t in partial.templates]
    assert "desk/_category_section.html" in names and "desk/_intake_fields.html" in names


def test_refused_intake_keeps_the_section_with_its_category(client, agent, category):
    client.force_login(agent)
    response = client.post(reverse("desk:request-create"), {"category": category.pk})
    assert response.status_code == 200
    assert response.context["category"] == category
    assert "desk/_intake_fields.html" in [t.name for t in response.templates]


def test_request_page_renders_the_details_slot(client, agent, category):
    request = new_request(agent, category)
    client.force_login(agent)
    response = client.get(reverse("desk:request-detail", args=[request.pk]))
    assert "desk/_request_details.html" in [t.name for t in response.templates]


def test_unknown_timeline_events_render_generically(client, agent, category):
    from desk.models import EntryKind, RequestEntry

    request = new_request(agent, category)
    RequestEntry.objects.create(
        request=request,
        kind=EntryKind.EVENT,
        author=agent,
        event_action="form_values_changed",
        event_data={"fields": ["pickup_window"]},
    )
    client.force_login(agent)
    response = client.get(reverse("desk:request-detail", args=[request.pk]))
    assert response.status_code == 200
    assert "Form values changed" in response.content.decode()
