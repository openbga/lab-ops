"""Access records for sensitive reads (core.access, core.models.AccessRecord): a record each time a
staff or portal account opens a ticket, its audit history, who opened it or its form details,
downloads a file, or is shown a patient-marked answer; never for a read that was refused; never
holding what was shown; written by the database's clock with the request's ID; append-only.
Change events gain the request ID (AuditEvent.request_ref). All data is synthetic."""

import io

import pytest
from django.contrib.auth.models import Group
from django.db import DatabaseError, connection, transaction
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse
from PIL import Image

from attachments.inspection import inspect_bytes
from attachments.services import add_entry_with_files, withdraw_attachment
from core import access
from core.models import AccessRecord, AuditEvent, RevealConfirmationRule
from desk.models import EntryKind, ServiceRequest, Status
from desk.services import add_entry, change_status
from request_forms.models import FieldValue
from taxonomy.models import RequestCategory
from teams.models import Team, TeamMembership

from .conftest import PATIENT_SENTINEL
from .test_pages import login, raised

pytestmark = pytest.mark.django_db

UPLOADED_NAME = "synthetic-requisition-scan.png"


def png(name=UPLOADED_NAME):
    buffer = io.BytesIO()
    Image.new("RGB", (20, 10), "white").save(buffer, format="PNG")
    return inspect_bytes(name, buffer.getvalue())


def records(**filters):
    return list(AccessRecord.objects.filter(**filters).order_by("id"))


def reread(ticket):
    return ServiceRequest.objects.get(pk=ticket.pk)


@pytest.fixture
def ticket(portal_user, pickup):
    """A portal ticket raised with a client-visible patient-marked answer."""
    return raised(portal_user, pickup, {"count": 2, "patient": PATIENT_SENTINEL})


@pytest.fixture
def patient_row(ticket):
    return FieldValue.objects.get(filled_form__request=ticket, key="patient")


@pytest.fixture
def reply_file(ticket, agent):
    """A file on a client-visible reply, and one on an internal note."""
    reply = add_entry_with_files(
        actor=agent,
        request=ticket,
        kind=EntryKind.REPLY,
        body="Synthetic reply.",
        expected_revision=reread(ticket).revision,
        files=[png()],
    )
    note = add_entry_with_files(
        actor=agent,
        request=ticket,
        kind=EntryKind.NOTE,
        body="Synthetic note.",
        expected_revision=reread(ticket).revision,
        files=[png("internal-only.png")],
    )
    return reply.attachments.get(), note.attachments.get()


@pytest.fixture
def no_role(make_user):
    return make_user("no_role_staff")


# ---------------------------------------------------------------------------------- written


def test_staff_opening_a_ticket_is_recorded_with_who_what_capacity_and_request_id(
    client, agent, ticket
):
    login(client, agent)
    response = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    assert response.status_code == 200
    [record] = records()
    assert (record.account_id, record.kind, record.capacity) == (agent.pk, "ticket", "laboratory")
    assert record.ticket_id == ticket.pk and (record.object_type, record.object_id) == ("", "")
    assert record.request_ref == response["X-Request-ID"] and len(record.request_ref) == 32
    assert record.occurred_at is not None


def test_a_portal_ticket_page_records_the_ticket_and_each_patient_marked_answer_shown(
    client, portal_user, ticket, patient_row
):
    login(client, portal_user)
    response = client.get(reverse("portal:request-detail", args=[ticket.number]))
    assert response.status_code == 200 and PATIENT_SENTINEL in response.content.decode()
    kinds = {(r.kind, r.object_type, r.object_id, r.capacity) for r in records()}
    assert kinds == {
        ("ticket", "", "", "client"),
        ("patient_answer", "request_forms.fieldvalue", str(patient_row.pk), "client"),
    }
    assert {r.request_ref for r in records()} == {response["X-Request-ID"]}
    assert not any(PATIENT_SENTINEL in str(vars(r)) for r in records())


def test_a_portal_ticket_without_patient_answers_records_only_the_ticket(
    client, portal_user, pickup
):
    plain = raised(portal_user, pickup, {"count": 1})
    login(client, portal_user)
    assert client.get(reverse("portal:request-detail", args=[plain.number])).status_code == 200
    assert [r.kind for r in records()] == ["ticket"]


def test_audit_history_and_who_opened_it_are_recorded_for_supervisors(client, capable, ticket):
    login(client, capable)
    assert client.get(reverse("desk:request-history", args=[ticket.pk])).status_code == 200
    assert client.get(reverse("desk:request-access", args=[ticket.pk])).status_code == 200
    assert [r.kind for r in records()] == ["history", "access"]


def test_the_form_details_page_is_recorded(client, agent, ticket):
    login(client, agent)
    assert client.get(reverse("request_details:edit", args=[ticket.number])).status_code == 200
    assert [(r.kind, r.ticket_id) for r in records()] == [("form_details", ticket.pk)]


def test_a_staff_reveal_is_recorded_besides_its_change_event(client, capable, patient_row):
    login(client, capable)
    url = reverse("request_details:reveal", args=[patient_row.pk])
    response = client.post(url, HTTP_HX_REQUEST="true")
    assert response.status_code == 200 and PATIENT_SENTINEL in response.content.decode()
    [record] = records()
    assert (record.kind, record.object_type, record.object_id) == (
        "patient_answer",
        "request_forms.fieldvalue",
        str(patient_row.pk),
    )
    event = AuditEvent.objects.get(action="request_form.value_revealed")
    assert event.request_ref == record.request_ref == response["X-Request-ID"]


def test_downloads_are_recorded_for_staff_and_portal_accounts(
    client, agent, portal_user, reply_file
):
    shown, internal = reply_file
    login(client, agent)
    assert client.get(reverse("attachments:download", args=[internal.pk])).status_code == 200
    client.logout()
    login(client, portal_user)
    assert client.get(reverse("attachments:download", args=[shown.pk])).status_code == 200
    got = [(r.account_id, r.kind, r.capacity, r.object_id) for r in records()]
    assert got == [
        (agent.pk, "file", "laboratory", str(internal.pk)),
        (portal_user.pk, "file", "client", str(shown.pk)),
    ]
    assert all(r.object_type == "attachments.attachment" for r in records())


def test_a_ticket_drawn_again_after_a_refused_entry_is_recorded(client, agent, ticket):
    login(client, agent)
    response = client.post(
        reverse("desk:request-entry", args=[ticket.pk]),
        {"kind": "note", "body": "Synthetic note.", "expected_revision": 0},
    )
    assert response.status_code == 400
    assert [r.kind for r in records()] == ["ticket"]


def test_a_portal_ticket_drawn_again_after_a_refused_reply_is_recorded(client, portal_user, ticket):
    login(client, portal_user)
    response = client.post(
        reverse("portal:request-reply", args=[ticket.number]),
        {"body": "Synthetic reply.", "expected_revision": 0},
    )
    assert response.status_code == 200
    assert {r.kind for r in records()} == {"ticket", "patient_answer"}


# ---------------------------------------------------------------------------------- refused


def test_refused_reads_record_nothing(
    client, agent, no_role, outsider, colleague, portal_user, ticket, patient_row, reply_file
):
    shown, internal = reply_file
    login(client, outsider)
    assert client.get(reverse("portal:request-detail", args=[ticket.number])).status_code == 404
    assert client.get(reverse("attachments:download", args=[shown.pk])).status_code == 404
    client.logout()
    login(client, portal_user)
    assert client.get(reverse("attachments:download", args=[internal.pk])).status_code == 404
    assert client.get(reverse("desk:request-detail", args=[ticket.pk])).status_code == 403
    assert client.get(reverse("desk:request-access", args=[ticket.pk])).status_code == 403
    client.logout()
    login(client, no_role)
    assert client.get(reverse("desk:request-detail", args=[ticket.pk])).status_code == 403
    assert client.get(reverse("attachments:download", args=[internal.pk])).status_code == 403
    client.logout()
    login(client, agent)
    assert client.get(reverse("desk:request-history", args=[ticket.pk])).status_code == 403
    assert client.get(reverse("desk:request-access", args=[ticket.pk])).status_code == 403
    reveal = client.post(reverse("request_details:reveal", args=[patient_row.pk]))
    assert reveal.status_code in (403, 404)
    assert client.get(reverse("desk:request-detail", args=[10**9])).status_code == 404
    assert records() == []


def test_a_restricted_ticket_refused_to_a_non_member_records_nothing(
    client, agent, make_user, group
):
    team = Team.objects.create(slug="privacy", name="Privacy")
    restricted = RequestCategory.objects.create(
        key="privacy", name="Privacy", parent=group, default_team=team, is_restricted=True
    )
    member = make_user("privacy_member", "client_services")
    TeamMembership.objects.create(team=team, user=member)
    from desk.services import create_request

    secret = create_request(
        actor=member,
        category=restricted,
        subject="Synthetic restricted",
        channel="phone",
        first_entry_kind=EntryKind.NOTE,
        first_entry_body="Synthetic.",
    )
    login(client, agent)
    assert client.get(reverse("desk:request-detail", args=[secret.pk])).status_code == 404
    assert client.get(reverse("desk:request-access", args=[secret.pk])).status_code == 404
    assert records() == []
    client.logout()
    login(client, member)
    assert client.get(reverse("desk:request-detail", args=[secret.pk])).status_code == 200
    assert [r.account_id for r in records()] == [member.pk]


def test_a_withdrawn_file_refused_to_other_staff_records_nothing(
    client, agent, capable, reply_file
):
    shown, _ = reply_file
    withdraw_attachment(actor=capable, attachment_id=shown.pk, reason="Wrong ticket")
    login(client, agent)
    assert client.get(reverse("attachments:download", args=[shown.pk])).status_code == 403
    assert records() == []
    client.logout()
    login(client, capable)
    assert client.get(reverse("attachments:download", args=[shown.pk])).status_code == 200
    assert [(r.account_id, r.kind) for r in records()] == [(capable.pk, "file")]


def test_a_closed_tickets_form_details_redirect_records_nothing(client, agent, ticket):
    for status in (Status.RESOLVED, Status.CLOSED):
        change_status(
            actor=agent,
            request=ticket,
            status=status,
            summary="Synthetic resolution.",
            expected_revision=reread(ticket).revision,
        )
    login(client, agent)
    response = client.get(reverse("request_details:edit", args=[ticket.number]))
    assert response.status_code == 302
    assert records() == []


@pytest.mark.confirmation
def test_a_reveal_sent_to_confirm_it_is_you_first_records_nothing(client, capable, patient_row):
    RevealConfirmationRule.objects.create(pk=1, first_reveal=True, revision=1)
    login(client, capable)
    response = client.post(reverse("request_details:reveal", args=[patient_row.pk]))
    assert response.status_code == 302
    assert records() == []


# ------------------------------------------------------------------------------ the page


def test_who_opened_it_lists_records_newest_first_without_content(
    client, agent, capable, portal_user, ticket, patient_row, reply_file
):
    shown, _ = reply_file
    login(client, portal_user)
    client.get(reverse("portal:request-detail", args=[ticket.number]))
    client.get(reverse("attachments:download", args=[shown.pk]))
    client.logout()
    login(client, agent)
    client.get(reverse("desk:request-detail", args=[ticket.pk]))
    client.logout()
    login(client, capable)
    page = client.get(reverse("desk:request-access", args=[ticket.pk]))
    assert page.status_code == 200
    html = page.content.decode()
    for text in ("Opened the ticket", "Shown a patient-marked answer", "Downloaded a file"):
        assert text in html
    assert "Laboratory staff" in html and "Client portal" in html
    assert patient_row.label in html and shown.download_name in html
    assert PATIENT_SENTINEL not in html and UPLOADED_NAME not in html
    assert "Synthetic reply." not in html
    rows = page.context["page_obj"].object_list
    assert [row.person for row in rows][0] == agent.username  # newest first
    assert all(row.request_ref for row in rows)


def test_who_opened_it_is_offered_on_the_ticket_to_supervisors_only(client, agent, capable, ticket):
    url = reverse("desk:request-access", args=[ticket.pk])
    login(client, agent)
    assert url not in client.get(reverse("desk:request-detail", args=[ticket.pk])).content.decode()
    client.logout()
    login(client, capable)
    assert url in client.get(reverse("desk:request-detail", args=[ticket.pk])).content.decode()


def test_who_opened_it_pages_through_more_than_one_page(client, capable, ticket):
    for _ in range(30):
        AccessRecord.objects.create(
            account=capable, capacity="laboratory", kind="ticket", ticket_id=ticket.pk
        )
    login(client, capable)
    first = client.get(reverse("desk:request-access", args=[ticket.pk]))
    assert len(first.context["page_obj"].object_list) == 25
    second = client.get(reverse("desk:request-access", args=[ticket.pk]), {"page": "2"})
    # 30 records and the first page's own: opening the page is recorded too.
    assert len(second.context["page_obj"].object_list) == 6
    odd = client.get(reverse("desk:request-access", args=[ticket.pk]), {"page": "x" * 5000})
    assert odd.status_code == 200


# --------------------------------------------------------------------- storage and writing


def test_access_records_are_append_only(agent, ticket):
    record = AccessRecord.objects.create(
        account=agent, capacity="laboratory", kind="ticket", ticket_id=ticket.pk
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        AccessRecord.objects.filter(pk=record.pk).update(kind="history")
    with pytest.raises(DatabaseError), transaction.atomic():
        AccessRecord.objects.filter(pk=record.pk).delete()
    assert AccessRecord.objects.get(pk=record.pk).kind == "ticket"


def test_an_unknown_kind_or_half_an_object_is_refused_by_the_database(agent, ticket):
    with pytest.raises(DatabaseError), transaction.atomic():
        AccessRecord.objects.create(
            account=agent, capacity="laboratory", kind="search", ticket_id=ticket.pk
        )
    with pytest.raises(DatabaseError), transaction.atomic():
        AccessRecord.objects.create(
            account=agent,
            capacity="laboratory",
            kind="file",
            ticket_id=ticket.pk,
            object_type="attachments.attachment",
        )


def test_a_failed_write_fails_the_answer_and_shows_nothing(client, agent, ticket, monkeypatch):
    def refuse(*args, **kwargs):
        raise DatabaseError("synthetic failure")

    monkeypatch.setattr(AccessRecord.objects, "bulk_create", refuse)
    client.raise_request_exception = False
    login(client, agent)
    response = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    assert response.status_code == 500
    assert ticket.subject not in response.content.decode()


def _middleware_request(user, status):
    request = RequestFactory().get("/")
    request.user = user
    request.request_id = "a" * 32
    access.note(request, "ticket", ticket_id=1)
    access.note(request, "ticket", ticket_id=1)  # the same read twice: one record
    return access.AccessRecordMiddleware(lambda _: HttpResponse(status=status))(request)


@pytest.mark.parametrize("status", [200, 302, 400, 404])
def test_the_middleware_writes_what_was_noted_once_unless_the_answer_failed(agent, status):
    _middleware_request(agent, status)
    assert [(r.kind, r.request_ref) for r in records()] == [("ticket", "a" * 32)]


def test_the_middleware_writes_nothing_for_a_server_error(agent):
    _middleware_request(agent, 500)
    assert records() == []


def test_an_unknown_kind_is_refused_when_noted(agent):
    request = RequestFactory().get("/")
    request.user = agent
    with pytest.raises(ValueError):
        access.note(request, "search", ticket_id=1)


# --------------------------------------------------------------- change events' request ID


def test_a_change_event_carries_the_request_id_of_the_request_that_made_it(client, agent, ticket):
    login(client, agent)
    response = client.post(
        reverse("desk:request-entry", args=[ticket.pk]),
        {"kind": "note", "body": "Synthetic note.", "expected_revision": reread(ticket).revision},
    )
    assert response.status_code == 302
    events = AuditEvent.objects.filter(request_ref=response["X-Request-ID"])
    assert events.exists()


def test_a_change_outside_a_request_has_no_request_id(agent, ticket):
    before = set(AuditEvent.objects.values_list("pk", flat=True))
    add_entry(
        actor=agent,
        request=ticket,
        kind=EntryKind.NOTE,
        body="Synthetic note.",
        expected_revision=reread(ticket).revision,
    )
    new = AuditEvent.objects.exclude(pk__in=before)
    assert new.exists() and set(new.values_list("request_ref", flat=True)) == {""}


def test_an_event_inserted_without_the_column_gets_a_blank_request_id(agent):
    """A release still running while the migration applies inserts events without the column."""
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO core_auditevent (id, occurred_at, actor_id, action, object_type, "
            "object_id, before, after, reason) VALUES (gen_random_uuid(), now(), %s, "
            "'synthetic.check', 'core.lab', '1', '{}', '{}', '') RETURNING request_ref",
            [agent.pk],
        )
        assert cursor.fetchone() == ("",)


def test_no_model_permission_is_offered_for_access_records():
    from django.contrib.auth.models import Permission

    assert not Permission.objects.filter(content_type__model="accessrecord").exists()
    assert Group.objects.filter(permissions__content_type__model="accessrecord").count() == 0


def test_a_client_timeline_records_each_ticket_whose_entries_it_shows(
    client, agent, clinic, ticket, pickup, portal_user
):
    other = raised(portal_user, pickup, {"count": 1})
    login(client, agent)
    response = client.get(reverse("clients:client-timeline", args=[clinic.pk]))
    assert response.status_code == 200
    got = sorted((r.kind, r.ticket_id, r.capacity) for r in records())
    assert got == sorted(
        [("timeline", ticket.pk, "laboratory"), ("timeline", other.pk, "laboratory")]
    )


def test_a_client_timeline_refused_records_nothing(client, portal_user, no_role, clinic, ticket):
    for account in (portal_user, no_role):
        login(client, account)
        response = client.get(reverse("clients:client-timeline", args=[clinic.pk]))
        assert response.status_code in (403, 404)
        client.logout()
    assert records() == []
