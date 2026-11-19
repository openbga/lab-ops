"""The cases next to each case of the round 1 confirmation
(.local/review-history/nonconformance-round-1-confirmation.md). Synthetic data only."""

import pytest
from django.test import Client
from django.urls import reverse

from accounts.models import User
from core.models import LabModule
from desk import services as desk_services
from desk.models import EntryKind, RequestEntry, ServiceRequest
from nonconformance.models import Nonconformance

from . import test_review_round1 as previous

pytestmark = pytest.mark.django_db

owned = previous.owned
at_read = previous.at_read


def lock_of(table):
    return lambda sql: f'FROM "{table}"' in sql and "FOR" in sql and "UPDATE" in sql


# C-01: a portal account deactivated while its reply waited for the ticket's lock.


@pytest.mark.django_db(transaction=True)
def test_a_portal_reply_rechecks_the_account_after_the_ticket_lock(portal, plain_ticket):
    # The account reads the ticket through a whole-organization grant, so its reply reaches the
    # ticket's lock (an account that cannot read it is refused before any lock is taken).
    from clients.models import PortalAccess

    PortalAccess.objects.create(user=portal, client=portal.client, whole_organization=True)
    assert desk_services.portal_requests(portal).filter(pk=plain_ticket.pk).exists()
    with at_read(
        lock_of("desk_servicerequest"),
        lambda: User.objects.filter(pk=portal.pk).update(is_active=False),
        before=True,
    ) as concurrent:
        with pytest.raises(ServiceRequest.DoesNotExist):
            desk_services.add_entry(
                actor=portal,
                request=plain_ticket,
                kind=EntryKind.REPLY,
                body="Synthetic client reply",
                expected_revision=plain_ticket.revision,
            )
    assert concurrent == [True]
    assert not RequestEntry.objects.filter(body="Synthetic client reply").exists()


# C-05: every method of a slashless or dialog request to an off module is an unknown address.


@pytest.mark.parametrize("method", ["head", "put", "delete", "options"])
def test_slashless_off_addresses_answer_like_unknown_ones_for_every_method(
    method, settings, module_on
):
    settings.DEBUG = False
    client = Client(enforce_csrf_checks=True)
    LabModule.objects.update(is_enabled=False)
    submit = getattr(client, method)
    path = reverse("nonconformance:action-list").rstrip("/")
    off, unknown = submit(path), submit("/unknown-confirmation-address")
    assert (off.status_code, off.get("Location")) == (unknown.status_code, unknown.get("Location"))


@pytest.mark.parametrize("name", ["status", "investigation", "close"])
def test_off_dialog_requests_show_no_help_of_the_module(client, owned, agent, name):
    client.force_login(agent)
    LabModule.objects.update(is_enabled=False)
    response = client.get(reverse(f"nonconformance:{name}", args=[owned.pk]), HTTP_X_VR_FORM="1")
    unknown = client.get("/unknown-confirmation-address/", HTTP_X_VR_FORM="1")
    assert response.status_code == unknown.status_code == 404
    assert [t.name for t in response.templates] == [t.name for t in unknown.templates]
    # Every page carries the application's form-dialog address patterns (public structure); the
    # page help of the module's form must not be there.
    for help_title in (b"Change status", b"Record investigation", b"Close nonconformance"):
        assert help_title not in response.content


# C-06: every early refusal of the request size is never stored.


def test_an_invalid_request_length_is_never_stored(client):
    response = client.generic(
        "POST", "/quality/nonconformances/", b"x", CONTENT_LENGTH="1x", content_type="text/plain"
    )
    assert response.status_code == 400 and "no-store" in response["Cache-Control"]


# C-07: a write that finds the module off answers like the module being off, menu included.


def test_a_save_that_finds_the_module_off_draws_no_module_menu(client, owned, agent, monkeypatch):
    from core import modules

    client.force_login(agent)
    on = client.get(reverse("nonconformance:list"))
    assert b">Nonconformances<" in on.content  # the menu entry, while the module is on

    real = modules.lock_enabled

    def switched_off(key):
        LabModule.objects.update(is_enabled=False)
        return real(key)

    monkeypatch.setattr("nonconformance.services.lock_enabled", switched_off)
    response = client.post(
        reverse("nonconformance:investigation", args=[owned.pk]),
        {"expected_revision": owned.revision, "containment": "Synthetic"},
    )
    assert response.status_code == 404
    assert b">Nonconformances<" not in response.content  # no menu entry of the module


# C-08: a correction without a reason keeps what was typed.


def test_a_correction_without_a_reason_is_refused_with_the_input_kept(client, owned, agent):
    client.force_login(agent)
    response = client.post(
        reverse("nonconformance:edit", args=[owned.pk]),
        {
            "expected_revision": owned.revision,
            "title": "Corrected synthetic title",
            "description": owned.description,
            "source": "audit",
            "severity": owned.severity,
            "area": "",
            "reason": "",
        },
    )
    assert response.status_code == 200 and b"Corrected synthetic title" in response.content
    assert Nonconformance.objects.get(pk=owned.pk).title == owned.title
