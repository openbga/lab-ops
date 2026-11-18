"""The request forms on the portal's pages: the home's service menu, the chooser, the form
details on the portal request page, and notices that never carry form values."""

import pytest
from django.core import mail
from django.urls import reverse

from desk.models import EntryKind
from desk.services import add_entry
from request_forms.services import create_request_with_form

from .conftest import PASSWORD, PATIENT_SENTINEL, definition_of

pytestmark = pytest.mark.django_db


def portal_raise(user, template, values):
    return create_request_with_form(
        actor=user,
        template=template,
        expected_definition=definition_of(template),
        values=values,
        first_entry_kind=EntryKind.REPLY,
        channel="portal",
    )


def test_home_offers_the_service_menu(client, portal_user, pickup):
    assert client.login(username=portal_user.username, password=PASSWORD)
    page = client.get(reverse("portal:home")).content.decode()
    assert "How can we help?" in page
    assert reverse("portal_forms:raise", args=["pickup"]) in page
    assert reverse("portal:request-create") in page  # Something else
    assert page.index("How can we help?") < page.index("Open tickets")
    listing = client.get(reverse("portal:request-list")).content.decode()
    assert listing.count(f'href="{reverse("portal_forms:choose")}"') >= 2  # header and empty state
    assert f'href="{reverse("portal:request-create")}"' not in listing


def test_new_request_goes_straight_to_the_general_form_without_forms(client, portal_user):
    assert client.login(username=portal_user.username, password=PASSWORD)
    page = client.get(reverse("portal:home")).content.decode()
    listing = client.get(reverse("portal:request-list")).content.decode()
    for html in (page, listing):
        assert reverse("portal_forms:choose") not in html
        assert reverse("portal:request-create") in html


def test_portal_request_page_shows_client_values_only(client, portal_user, agent, pickup):
    from request_forms.services import correct_values

    request = portal_raise(portal_user, pickup, {"count": 2})
    correct_values(
        actor=agent,
        request=request,
        expected_definition=definition_of(request),
        values={"courier": "Route 4", "tracking": "TRK-TEST-7"},
        reason="",
        expected={"courier": None, "tracking": None},
    )
    assert client.login(username=portal_user.username, password=PASSWORD)
    page = client.get(reverse("portal:request-detail", args=[request.number])).content.decode()
    assert "Form details" in page and "Number of coolers" in page and "TRK-TEST-7" in page
    assert "Route 4" not in page and "Courier" not in page
    assert "Request again" in page


def test_notices_never_carry_form_values(
    portal_user, agent, pickup, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        request = portal_raise(portal_user, pickup, {"count": 2, "patient": PATIENT_SENTINEL})
        add_entry(
            actor=agent,
            request=request,
            kind=EntryKind.REPLY,
            body="We will collect today.",
            expected_revision=request.revision,
        )
    assert mail.outbox, "the raiser is told about the staff reply"
    for message in mail.outbox:
        text = message.subject + message.body + str(message.extra_headers)
        assert PATIENT_SENTINEL not in text and "Number of coolers" not in text


def test_home_links_every_form_when_more_are_offered(client, portal_user, pickup):
    from request_forms.models import RequestTemplate

    RequestTemplate.objects.create(
        key="extra", name="Extra form", category=pickup.category, featured=False
    )
    assert client.login(username=portal_user.username, password=PASSWORD)
    page = client.get(reverse("portal:home")).content.decode()
    assert "All services" in page and "Extra form" not in page
