"""Closing a ticket freezes its links."""

import pytest
from django.core.exceptions import ValidationError
from django.template import Context, Template
from django.urls import reverse

from desk import services as desk_services
from desk.models import Status
from links.models import LinkAttachment
from links.services import add_link, remove_link, set_shown_to_client

from .conftest import PASSWORD, PATIENT_KEY


def change_status(record, actor, status, **values):
    record.refresh_from_db()
    desk_services.change_status(
        actor=actor, request=record, expected_revision=record.revision, status=status, **values
    )
    record.refresh_from_db()
    return record


def resolve(record, actor):
    return change_status(record, actor, Status.RESOLVED, summary="Completed")


def close(record, actor):
    return change_status(resolve(record, actor), actor, Status.CLOSED)


def panel(service_request, user):
    return Template("{% load record_links %}{% request_links r u %}").render(
        Context({"r": service_request, "u": user})
    )


@pytest.fixture
def linked(agent, lis, service_request):
    attachment, _ = add_link(
        actor=agent,
        request_id=service_request.pk,
        system=lis,
        record_type="order",
        identifier="ORD-TEST-CLS",
    )
    return attachment


def test_closed_request_takes_no_new_link_of_any_kind(agent, holder, lis, service_request):
    close(service_request, agent)
    for actor, record_type, identifier in (
        (agent, "order", "ORD-TEST-CLS"),
        (holder, "patient", PATIENT_KEY),
    ):
        with pytest.raises(ValidationError, match="closed"):
            add_link(
                actor=actor,
                request_id=service_request.pk,
                system=lis,
                record_type=record_type,
                identifier=identifier,
                shown_to_client=record_type == "order",
            )
    assert not LinkAttachment.objects.exists()


def test_closed_request_links_cannot_be_removed_or_shown(
    agent, holder, lis, service_request, linked
):
    person, _ = add_link(
        actor=holder,
        request_id=service_request.pk,
        system=lis,
        record_type="patient",
        identifier=PATIENT_KEY,
    )
    close(service_request, agent)
    with pytest.raises(ValidationError, match="closed"):
        set_shown_to_client(actor=agent, attachment_id=linked.pk, shown=True)
    with pytest.raises(ValidationError, match="closed"):
        remove_link(actor=agent, attachment_id=linked.pk, reason="Too late")
    with pytest.raises(ValidationError, match="closed"):
        remove_link(actor=holder, attachment_id=person.pk, reason_code="wrong_person")
    assert not LinkAttachment.objects.filter(removed_at__isnull=False).exists()
    linked.refresh_from_db()
    assert not linked.shown_to_client


def test_resolved_request_still_takes_link_changes(agent, lis, service_request, linked):
    resolve(service_request, agent)
    set_shown_to_client(actor=agent, attachment_id=linked.pk, shown=True)
    add_link(
        actor=agent,
        request_id=service_request.pk,
        system=lis,
        record_type="report",
        identifier="RPT-TEST-1",
    )
    remove_link(actor=agent, attachment_id=linked.pk, reason="Not this order")


def test_closed_request_links_section_offers_no_actions(
    client, agent, holder, lis, service_request, linked
):
    add_link(
        actor=holder,
        request_id=service_request.pk,
        system=lis,
        record_type="patient",
        identifier=PATIENT_KEY,
    )
    open_html = panel(service_request, holder)
    assert "Add link" in open_html and "Remove link" in open_html
    close(service_request, agent)
    service_request.refresh_from_db()
    html = panel(service_request, holder)
    assert "This ticket is closed, so its links can no longer change." in html
    for action in ("Add link", "Make a link", "Remove link", "Show to client", "dropdown-toggle"):
        assert action not in html
    assert "Reveal key" in html  # reading stays possible, and audited
    client.login(username=agent.username, password=PASSWORD)
    for url in (
        reverse("link_records:request-link-add", args=[service_request.pk]),
        reverse("link_records:attachment-remove", args=[linked.pk]),
    ):
        response = client.get(url)
        assert response.status_code == 302 and response["Location"].startswith("/requests/")
    response = client.post(
        reverse("link_records:attachment-shown", args=[linked.pk]), {"shown": "1"}
    )
    assert response.status_code == 302
    linked.refresh_from_db()
    assert not linked.shown_to_client
