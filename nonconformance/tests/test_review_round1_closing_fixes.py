"""The cases next to each finding of the round 1 closing confirmation
(.local/review-history/nonconformance-round-1-closing.md). Synthetic data only."""

import pytest
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse

from accounts.models import User
from core.models import LabModule
from desk import services as desk_services

pytestmark = pytest.mark.django_db


# CL-01: a bulk status change refused because the reader is no longer staff draws no list.


def test_a_bulk_status_change_refused_as_staff_draws_no_list(
    client, plain_ticket, agent, monkeypatch
):
    client.force_login(agent)

    def refuse(**kwargs):
        User.objects.filter(pk=agent.pk).update(is_active=False)
        raise PermissionDenied("Your account no longer has a staff role.")

    monkeypatch.setattr(desk_services, "bulk_status", refuse)
    response = client.post(
        reverse("desk:request-list"),
        {
            "action": "status",
            "status": "waiting_client",
            "requests": [f"{plain_ticket.pk}:{plain_ticket.revision}"],
        },
    )
    assert response.status_code in (302, 403)
    assert plain_ticket.subject.encode() not in response.content


# CL-02: an invalid desk complaint form from an earlier reading says the ticket changed.


def test_an_invalid_stale_complaint_decision_says_the_ticket_changed(
    client, complaint, agent, lead
):
    client.force_login(agent)
    drawn = complaint.revision
    desk_services.add_entry(
        actor=lead, request=complaint, kind="note", body="Meanwhile", expected_revision=drawn
    )
    response = client.post(
        reverse("desk:complaint-decision", args=[complaint.pk]),
        {"expected_revision": drawn, "decision": "", "rationale": "Draft rationale"},
    )
    assert response.status_code == 200
    assert b"This ticket changed." in response.content and b"Draft rationale" in response.content


# CL-05: behind the HTTPS redirect, an address of an off module answers like an unknown one.


def test_the_https_redirect_of_an_off_module_address_is_that_of_an_unknown_address(
    settings, module_on
):
    settings.SECURE_SSL_REDIRECT = True
    client = Client()
    LabModule.objects.update(is_enabled=False)
    off = client.get("/quality/nonconformances/")
    unknown = client.get("/unknown-closing-address/")
    assert off.status_code == unknown.status_code == 301
    assert off.get("Cache-Control") == unknown.get("Cache-Control")
