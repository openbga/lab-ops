"""The author's capacity (laboratory or client) is recorded on each request entry when it is
written, never re-derived from the account later. Synthetic data."""

import importlib

import pytest
from django.apps import apps
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from clients.models import PortalAccess
from desk.models import AuthorCapacity, EntryKind, RequestEntry
from desk.portal_views import _conversation
from desk.services import add_entry, create_request

from .test_portal import raise_staff

pytestmark = pytest.mark.django_db

backfill = importlib.import_module("desk.migrations.0005_backfill_entry_author_capacity").backfill
LAB, CLIENT = AuthorCapacity.LABORATORY, AuthorCapacity.CLIENT


@pytest.fixture
def portal(make_user, clinic):
    user = make_user("capacity_portal", client=clinic)
    PortalAccess.objects.create(user=user, client=clinic, whole_organization=True)
    return user


def raise_in_portal(user, category):
    return create_request(
        actor=user,
        category=category,
        subject="From the portal",
        first_entry_kind=EntryKind.REPLY,
        first_entry_body="Please help.",
        channel="portal",
    )


def capacities(request):
    return [
        (e.kind, e.event_action, e.author_capacity)
        for e in RequestEntry.objects.filter(request=request).order_by("created_at", "pk")
    ]


def with_trigger_disabled(table, trigger, sql, params):
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")  # no pending deferred checks for ALTER
        cursor.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
        try:
            cursor.execute(sql, params)
        finally:
            cursor.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")


def convert_kind_as_a_legacy_release_did(user, client):
    """A staff account turned into a portal account before kinds were permanent (clients/0006)."""
    with_trigger_disabled(
        "accounts_user",
        "clients_account_kind_is_permanent",
        "UPDATE accounts_user SET client_id = %s WHERE id = %s",
        [client.pk if client else None, user.pk],
    )
    user.refresh_from_db()


# Written with the entry ---------------------------------------------------------------------


def test_services_record_the_capacity(agent, portal, category, clinic):
    staff_logged = raise_staff(agent, category, clinic)
    assert capacities(staff_logged) == [("event", "created", LAB), ("reply", "", LAB)]
    raised = raise_in_portal(portal, category)
    assert capacities(raised) == [("event", "created", CLIENT), ("reply", "", CLIENT)]
    raised.refresh_from_db()
    note = add_entry(
        actor=agent,
        request=raised,
        kind=EntryKind.NOTE,
        body="Checking.",
        expected_revision=raised.revision,
    )
    raised.refresh_from_db()
    answer = add_entry(
        actor=portal,
        request=raised,
        kind=EntryKind.REPLY,
        body="More detail.",
        expected_revision=raised.revision,
    )
    assert (note.author_capacity, answer.author_capacity) == (LAB, CLIENT)


def test_a_writer_that_leaves_it_blank_gets_it_from_the_author(agent, portal, category, clinic):
    request = raise_staff(agent, category, clinic)
    staff_event = RequestEntry.objects.create(
        request=request, kind="event", author=agent, event_action="link.attached"
    )
    client_event = RequestEntry.objects.create(
        request=request, kind="event", author=portal, event_action="link.attached"
    )
    assert (staff_event.author_capacity, client_event.author_capacity) == (LAB, CLIENT)
    # bulk_create skips save(): the database trigger fills it at insert.
    bulk = RequestEntry.objects.bulk_create(
        [RequestEntry(request=request, kind="event", author=agent, event_action="bulk")]
    )[0]
    assert bulk.author_capacity == ""  # the object in memory has not seen the database
    assert bulk.recorded_capacity() == LAB


def test_link_events_are_the_laboratorys(agent, category, clinic):
    from links.models import ExternalSystem
    from links.services import add_link

    request = raise_staff(agent, category, clinic)
    add_link(
        actor=agent,
        system=ExternalSystem.objects.get(key="lis"),
        record_type="order",
        identifier="ORD-TEST-000042",
        request_id=request.pk,
    )
    event = request.entries.get(kind="event", event_action="link.attached")
    assert event.author_capacity == LAB


def test_no_entry_is_stored_without_a_known_capacity(agent, category, clinic):
    request = raise_staff(agent, category, clinic)
    columns = (
        "request_id, kind, author_id, author_capacity, body, contact_direction, contact_channel,"
        " contact_with, contact_outcome, event_action, event_data, withdrawal_reason, created_at"
    )
    insert = (
        f"INSERT INTO desk_requestentry ({columns}) VALUES "
        "(%s, 'note', %s, %s, 'Raw.', '', '', '', '', '', '{}', '', now()) RETURNING id"
    )
    with connection.cursor() as cursor:
        cursor.execute(insert, [request.pk, agent.pk, ""])  # blank: filled at insert
        filled = cursor.fetchone()[0]
    assert RequestEntry.objects.get(pk=filled).author_capacity == LAB
    for value, error in ((None, IntegrityError), ("robot", IntegrityError)):
        with pytest.raises(error), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(insert, [request.pk, agent.pk, value])
    with pytest.raises(DatabaseError), transaction.atomic():
        RequestEntry.objects.filter(pk=filled).update(author_capacity="")


def test_the_capacity_never_changes_and_takes_only_known_values(agent, category, clinic):
    request = raise_staff(agent, category, clinic)
    entry = request.entries.get(kind="reply")
    with pytest.raises(DatabaseError), transaction.atomic():
        RequestEntry.objects.filter(pk=entry.pk).update(author_capacity=CLIENT)
    with pytest.raises(IntegrityError), transaction.atomic():
        RequestEntry.objects.create(
            request=request, kind="note", author=agent, author_capacity="robot", body="x"
        )
    # Withdrawing a reply is still the one permitted update.
    RequestEntry.objects.filter(pk=entry.pk).update(
        withdrawn_at=timezone.now(), withdrawn_by=agent, withdrawal_reason="Wrong request"
    )
    assert RequestEntry.objects.get(pk=entry.pk).author_capacity == LAB


# Read from the entry, not the account ------------------------------------------------------


def test_signatures_follow_the_stored_capacity_not_the_account(
    client, agent, portal, category, clinic
):
    record = raise_staff(agent, category, clinic)
    agent.first_name = "Distinctive"
    agent.save()
    convert_kind_as_a_legacy_release_did(agent, clinic)
    assert not _conversation(record)[0]["from_client"]
    client.force_login(portal)
    html = client.get(reverse("portal:request-detail", args=[record.number])).content.decode()
    main = html[html.index("<main") : html.index("</main>")]
    assert "Reply from the laboratory" in main and "Distinctive" not in main


def test_upgrade_keeps_legacy_laboratory_attribution(agent, clinic, category):
    """An account converted on a release that allowed it, then
    the migrations run to the head again."""
    from django.db.migrations.executor import MigrationExecutor

    assert connection.settings_dict["NAME"].startswith("test_")
    record = raise_staff(agent, category, clinic)
    assert not _conversation(record)[0]["from_client"]
    before = [("clients", "0005_backfill_portal_access_client")]
    executor = MigrationExecutor(connection)
    head = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(before)
        legacy = executor.loader.project_state(before).apps
        agent.groups.clear()
        legacy.get_model("accounts", "User").objects.filter(pk=agent.pk).update(client_id=clinic.pk)
    finally:
        MigrationExecutor(connection).migrate(head)
    assert not _conversation(record)[0]["from_client"]


# Backfill of rows written before the capacity existed ----------------------------------------


CONSTRAINT = "request_entry_author_capacity"


def blank_all():
    """The rows as they stood before desk/0005: no capacity, and no constraint asking for one."""
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(f"ALTER TABLE desk_requestentry DROP CONSTRAINT {CONSTRAINT}")
    with_trigger_disabled(
        "desk_requestentry",
        "desk_entry_immutable",
        "UPDATE desk_requestentry SET author_capacity = ''",
        [],
    )


def restore_constraint():
    with connection.cursor() as cursor:
        cursor.execute(
            f"ALTER TABLE desk_requestentry ADD CONSTRAINT {CONSTRAINT} "
            "CHECK (author_capacity IN ('laboratory', 'client'))"
        )


def test_backfill_reads_the_entry_before_the_account(make_user, agent, portal, category, clinic):
    staff_logged = raise_staff(agent, category, clinic)
    staff_logged.refresh_from_db()
    add_entry(
        actor=agent,
        request=staff_logged,
        kind=EntryKind.NOTE,
        body="Internal.",
        expected_revision=staff_logged.revision,
    )
    raised = raise_in_portal(portal, category)
    raised.refresh_from_db()
    add_entry(
        actor=portal,
        request=raised,
        kind=EntryKind.REPLY,
        body="More.",
        expected_revision=raised.revision,
    )
    raised.refresh_from_db()
    later_staff = make_user("later_staff", "client_services")
    add_entry(
        actor=later_staff,
        request=raised,
        kind=EntryKind.REPLY,
        body="Answer.",
        expected_revision=raised.revision,
    )
    # A release that allowed it turned the first staff author into a client account.
    convert_kind_as_a_legacy_release_did(agent, clinic)
    blank_all()
    backfill(apps, None)
    restore_constraint()  # every row has a known capacity again
    assert capacities(staff_logged) == [
        ("event", "created", LAB),  # the request's channel was the phone
        ("reply", "", LAB),  # the first reply of a phone request
        ("note", "", LAB),  # a note is always the laboratory's
    ]
    assert capacities(raised) == [
        ("event", "created", CLIENT),
        ("reply", "", CLIENT),
        ("reply", "", CLIENT),  # later replies: the author's account
        ("reply", "", LAB),
    ]
    assert User.objects.get(pk=agent.pk).client_id == clinic.pk  # the account stays as it was
