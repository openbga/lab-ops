"""The first-response clock and the historical author capacity backfill."""

import importlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.utils import timezone

from core.models import AuditEvent
from desk.models import AuthorCapacity, EntryKind, RequestEntry
from desk.services import add_entry
from service_levels import clocks
from service_levels.models import RequestClock

from .test_entry_author_capacity import (
    blank_all,
    capacities,
    convert_kind_as_a_legacy_release_did,
    raise_in_portal,
    restore_constraint,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def portal(make_user, clinic):
    from clients.models import PortalAccess

    user = make_user("historical_portal", client=clinic)
    PortalAccess.objects.create(user=user, client=clinic, whole_organization=True)
    return user


migration = importlib.import_module("desk.migrations.0005_backfill_entry_author_capacity")
LAB, CLIENT = AuthorCapacity.LABORATORY, AuthorCapacity.CLIENT


def test_staff_only_event_has_recoverable_legacy_capacity():
    row = SimpleNamespace(
        pk=9,
        kind="event",
        event_action="complaint_decided",
        event_data={"decision": "x"},
        author=SimpleNamespace(client_id=12),
        request=SimpleNamespace(channel="portal"),
    )
    assert migration.capacity(row, set()) == LAB
    for action, data in (
        ("assigned", {}),
        ("status_changed", {"new": "resolved"}),
        ("link.attached", {}),
        ("form_values_changed", {}),
    ):
        row.event_action, row.event_data = action, data
        assert migration.capacity(row, set()) == LAB, action


def test_later_legacy_reply_with_staff_provenance_keeps_capacity():
    """For a historical reply, the
    account administration recorded the conversion after the reply."""
    written = timezone.now() - timedelta(days=2)
    row = SimpleNamespace(
        pk=8,
        kind="reply",
        author_id=5,
        created_at=written,
        author=SimpleNamespace(client_id=12),
        request=SimpleNamespace(channel="portal"),
    )
    converted = {5: [(written + timedelta(days=1), False)]}
    assert migration.capacity(row, {2}, converted) == LAB
    assert migration.capacity(row, {2}) == CLIENT  # no record: the account as it is (docstring)


def test_backfill_uses_recorded_conversions(
    make_user,
    owner_admin,
    agent,
    portal,
    category,
    clinic,
):
    raised = raise_in_portal(portal, category)
    raised.refresh_from_db()
    add_entry(
        actor=agent,
        request=raised,
        kind=EntryKind.REPLY,
        body="Answer.",
        expected_revision=raised.revision,
        new_status="waiting_client",
    )
    # The account administration converted the staff author afterwards (a release allowed it).
    AuditEvent.objects.create(
        actor=owner_admin,
        action="account.updated",
        object_type="accounts.user",
        object_id=str(agent.pk),
        before={"client": None},
        after={"client": clinic.pk},
        reason="Django admin",
    )
    convert_kind_as_a_legacy_release_did(agent, clinic)
    blank_all()
    migration.backfill(apps, None)
    restore_constraint()
    assert capacities(raised) == [
        ("event", "created", CLIENT),
        ("reply", "", CLIENT),
        ("reply", "", LAB),  # recorded conversion after the reply
        ("event", "status_changed", LAB),  # to waiting: a staff-only change
    ]


@pytest.fixture
def owner_admin(make_user):
    return make_user("owner_admin", "admin", is_staff=True, is_superuser=True)


def test_clock_uses_entered_capacity(portal, category):
    record = raise_in_portal(portal, category)
    entry = RequestEntry.objects.create(
        request=record,
        author=portal,
        kind=EntryKind.REPLY,
        author_capacity=AuthorCapacity.LABORATORY,
        body="Historical staff reply",
    )
    clock, _ = RequestClock.objects.update_or_create(
        request=record,
        defaults={
            "first_response_due_at": timezone.now() + timedelta(hours=1),
            "first_response_met_at": None,
        },
    )
    clocks.entry_added(record, entry)
    clock.refresh_from_db()
    assert clock.first_response_met_at == entry.created_at
