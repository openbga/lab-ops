"""The Links sections on a request page and an organization page.

{% load record_links %}
{% request_links request user %}   the request's links, as the reader may see them
{% client_links client user %}     an organization's links
"""

from django import template
from django.urls import reverse

from accounts.roles import ROLES, has_role
from links.access import client_link_rows, request_link_rows
from links.services import can_see_person_links

register = template.Library()


@register.inclusion_tag("links/request_links_panel.html")
def request_links(service_request, user):
    may_see_person = can_see_person_links(user)
    current, removed = request_link_rows(service_request, may_see_person=may_see_person)
    reference = (service_request.client_reference or "").strip()
    linked_identifiers = {row.identifier.casefold() for row in current if row.identifier}
    return {
        "service_request": service_request,
        "current": current,
        "removed": removed,
        "may_see_person": may_see_person,
        "client_reference": reference,
        "reference_linked": reference.casefold() in linked_identifiers,
        "add_url": reverse("link_records:request-link-add", args=[service_request.pk]),
        "is_staff": has_role(user, *ROLES),
        # A closed request's links are frozen (links.services.require_open): no actions.
        "can_change": has_role(user, *ROLES) and service_request.status != "closed",
        "is_closed": service_request.status == "closed",
    }


@register.inclusion_tag("links/client_links_panel.html")
def client_links(client, user):
    current, removed = client_link_rows(client)
    return {
        "client": client,
        "current": current,
        "removed": removed,
        "add_url": reverse("link_records:client-link-add", args=[client.pk]),
    }
