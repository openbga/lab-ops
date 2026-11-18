"""External links and service clocks on the ticket list and timeline."""

import pytest
from django.urls import reverse

from core.templatetags.navigation import ROUTE_ITEMS
from desk.forms import RequestCreateForm
from desk.models import EntryKind, RequestEntry
from desk.tests.test_contract import new_request
from taxonomy.models import RequestCategory

pytestmark = pytest.mark.django_db


def event(request, actor, action, **data):
    return RequestEntry.objects.create(
        request=request, kind=EntryKind.EVENT, author=actor, event_action=action, event_data=data
    )


def test_link_events_in_the_timeline(client, agent, category):
    request = new_request(agent, category)
    event(request, agent, "link.attached", attachment=1, record_type="order", is_person=False)
    event(request, agent, "link.attached", attachment=2, record_type="patient", is_person=True)
    event(request, agent, "link.removed", attachment=2, record_type="patient", is_person=True)
    client.force_login(agent)
    page = client.get(reverse("desk:request-detail", args=[request.pk])).content.decode()
    assert "Link added: order" in page
    assert "Patient link added" in page and "Patient link removed" in page


def test_list_sorts_by_the_clock(client, agent, category):
    new_request(agent, category)
    client.force_login(agent)
    for sort in ("clock", "-clock"):
        response = client.get(reverse("desk:request-list"), {"view": "all", "sort": sort})
        assert response.status_code == 200
        assert response.context["sort"] == sort


def test_intake_offers_only_categories_of_active_groups(agent, category, sample_category):
    RequestCategory.objects.filter(pk=sample_category.parent_id).update(is_active=False)
    offered = {
        value
        for _, options in RequestCreateForm().fields["category"].choices[1:]
        for value, _ in options
    }
    assert sample_category.pk not in offered


def test_link_pages_mark_the_requests_item():
    for route in (
        "link_records:request-link-add",
        "link_records:attachment-remove",
        "link_records:link-detail",
        "link_records:link-edit",
        "link_records:person-search",
    ):
        assert ROUTE_ITEMS[route] == "requests", route
    assert ROUTE_ITEMS["link_records:client-link-add"] == "clients"
