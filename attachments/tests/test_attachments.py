"""Attachments on entries: added with their entry, read only by those who may see the entry,
served safely, never changed, withdrawn by a supervisor, and never loaded by list queries."""

import json
import logging

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, transaction
from django.template import Context, Template
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from attachments.models import Attachment, AttachmentBlob
from attachments.selectors import with_files
from attachments.services import (
    add_entry_with_files,
    create_request_with_files,
    withdraw_attachment,
)
from core.models import AuditEvent
from desk.models import RequestEntry

from .conftest import SAMPLES, png_file

SECRET_NAME = "Jane-Synthetic-Doe-requisition.png"


def reply_with(request, actor, files, kind="reply"):
    request.refresh_from_db()
    return add_entry_with_files(
        actor=actor,
        request=request,
        kind=kind,
        body="Synthetic entry.",
        expected_revision=request.revision,
        files=files,
    )


def file_of(entry):
    return Attachment.objects.get(entry=entry)


def get(client, user, attachment):
    client.force_login(user)
    return client.get(reverse("attachments:download", args=[attachment.pk]))


# Adding ------------------------------------------------------------------------------------


def test_entry_and_files_commit_together(agent, staff_request):
    request = staff_request()
    entry = reply_with(request, agent, [png_file(SECRET_NAME), png_file("second.png")])
    files = list(Attachment.objects.filter(entry=entry))
    assert [f.original_name for f in files] == [SECRET_NAME, "second.png"]
    assert all(f.request_id == request.pk and f.uploaded_by == agent for f in files)
    assert AttachmentBlob.objects.filter(attachment__in=files).count() == 2


def test_too_many_files_create_nothing(agent, staff_request, settings):
    settings.ATTACHMENT_MAX_FILES = 1
    request = staff_request()
    entries = RequestEntry.objects.count()
    with pytest.raises(ValidationError, match="at most 1"):
        reply_with(request, agent, [png_file(), png_file()])
    assert RequestEntry.objects.count() == entries and not Attachment.objects.exists()


def test_a_failure_after_the_entry_rolls_the_entry_back(agent, staff_request, monkeypatch):
    request = staff_request()
    entries = RequestEntry.objects.count()

    def broken(**kwargs):
        raise DatabaseError("disk full")

    monkeypatch.setattr(AttachmentBlob.objects, "create", broken)
    with pytest.raises(DatabaseError):
        reply_with(request, agent, [png_file()])
    assert RequestEntry.objects.count() == entries and not Attachment.objects.exists()


def test_files_only_on_replies_and_notes(agent, staff_request):
    request = staff_request()
    with pytest.raises(ValidationError, match="reply or a note"):
        request.refresh_from_db()
        add_entry_with_files(
            actor=agent,
            request=request,
            kind="contact",
            body="Called.",
            expected_revision=request.revision,
            contact={
                "direction": "to_client",
                "channel": "phone",
                "with_whom": "x",
                "outcome": "spoke_with",
            },
            files=[png_file()],
        )
    note = reply_with(request, agent, [png_file()], kind="note")
    assert file_of(note).entry.kind == "note"


def test_client_raises_and_replies_with_files(clinic_portal, clinic, category):
    request = create_request_with_files(
        actor=clinic_portal,
        category=category,
        subject="Synthetic portal request",
        first_entry_kind="reply",
        first_entry_body="Please see the photo.",
        channel="portal",
        files=[png_file()],
    )
    first = Attachment.objects.get(request=request)
    assert first.entry.kind == "reply" and first.entry.author == clinic_portal
    reply_with(request, clinic_portal, [png_file("more.png")])
    assert Attachment.objects.filter(request=request).count() == 2


def test_audit_holds_no_name_or_content(agent, staff_request):
    entry = reply_with(staff_request(), agent, [png_file(SECRET_NAME)])
    event = AuditEvent.objects.get(action="attachment.added")
    payload = json.dumps([event.before, event.after, event.reason])
    assert "Jane" not in payload and "requisition" not in payload
    assert event.after["sha256"] == file_of(entry).sha256 and event.after["size"] > 0
    assert str(file_of(entry)) == f"Attachment {file_of(entry).pk}"


# Reading -----------------------------------------------------------------------------------


def test_download_headers(client, agent, staff_request):
    attachment = file_of(reply_with(staff_request(), agent, [png_file(SECRET_NAME)]))
    response = get(client, agent, attachment)
    assert response.status_code == 200
    assert response.content == SAMPLES["photo.png"]
    assert response["Content-Type"] == "image/png"
    disposition = response["Content-Disposition"]
    assert disposition == f'attachment; filename="{attachment.request.number}-{attachment.pk}.png"'
    assert "Jane" not in disposition
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in response["Content-Security-Policy"]
    assert "no-store" in response["Cache-Control"]


def test_download_is_get_only_and_needs_sign_in(client, agent, staff_request):
    attachment = file_of(reply_with(staff_request(), agent, [png_file()]))
    response = client.get(reverse("attachments:download", args=[attachment.pk]))
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]
    client.force_login(agent)
    assert client.post(reverse("attachments:download", args=[attachment.pk])).status_code == 405
    assert client.get(reverse("attachments:download", args=[999999])).status_code == 404


def test_restricted_request_files_only_for_its_team_and_supervisors(
    client, make_user, quality, technical, supervisor, staff_request, restricted
):
    from teams.models import TeamMembership

    member = make_user("quality_member", "technical")
    TeamMembership.objects.create(team=quality, user=member)
    request = staff_request(actor=member, category_=restricted)
    attachment = file_of(reply_with(request, member, [png_file()], kind="note"))
    assert get(client, technical, attachment).status_code == 404
    assert get(client, member, attachment).status_code == 200
    assert get(client, supervisor, attachment).status_code == 200


def test_portal_reaches_only_client_visible_files_of_its_own_requests(
    client, agent, clinic_portal, staff_request, water, make_user
):
    request = staff_request()
    reply = file_of(reply_with(request, agent, [png_file()]))
    note = file_of(reply_with(request, agent, [png_file("internal.png")], kind="note"))
    assert get(client, clinic_portal, reply).status_code == 200
    assert get(client, clinic_portal, note).status_code == 404  # a guessed id of a note's file
    other_org = make_user("water_portal", client=water)
    assert get(client, other_org, reply).status_code == 404


def test_portal_never_reaches_a_withdrawn_reply_or_a_withdrawn_file(
    client, agent, supervisor, clinic_portal, staff_request
):
    request = staff_request()
    first = reply_with(request, agent, [png_file()])
    second = reply_with(request, agent, [png_file("two.png")])
    # Withdraw the reply itself, as the desk's supervisor action does (the trigger allows it).
    from django.utils import timezone

    RequestEntry.objects.filter(pk=first.pk).update(
        withdrawn_at=timezone.now(), withdrawn_by=supervisor, withdrawal_reason="Wrong request"
    )
    assert get(client, clinic_portal, file_of(first)).status_code == 404
    withdraw_attachment(actor=supervisor, attachment_id=file_of(second).pk, reason="Wrong patient")
    assert get(client, clinic_portal, file_of(second)).status_code == 404


def test_site_scoped_portal_account(
    client, agent, north_portal, staff_request, water, north, south
):
    on_north = staff_request(client=water, site=north)
    on_south = staff_request(client=water, site=south)
    north_file = file_of(reply_with(on_north, agent, [png_file()]))
    south_file = file_of(reply_with(on_south, agent, [png_file()]))
    assert get(client, north_portal, north_file).status_code == 200
    assert get(client, north_portal, south_file).status_code == 404


def test_withdrawal_is_supervisor_only_with_a_reason_and_staff_see_it_labelled(
    client, agent, supervisor, staff_request
):
    attachment = file_of(reply_with(staff_request(), agent, [png_file()]))
    with pytest.raises(PermissionDenied):
        withdraw_attachment(actor=agent, attachment_id=attachment.pk, reason="x")
    with pytest.raises(ValidationError):
        withdraw_attachment(actor=supervisor, attachment_id=attachment.pk, reason=" ")
    withdraw_attachment(actor=supervisor, attachment_id=attachment.pk, reason="Wrong patient")
    with pytest.raises(ValidationError, match="already withdrawn"):
        withdraw_attachment(actor=supervisor, attachment_id=attachment.pk, reason="Again")
    assert get(client, agent, attachment).status_code == 403
    assert get(client, supervisor, attachment).status_code == 200
    event = AuditEvent.objects.get(action="attachment.withdrawn")
    assert event.reason == "Wrong patient"
    assert RequestEntry.objects.filter(event_action="attachment_withdrawn").exists()


def test_withdraw_page(client, agent, supervisor, staff_request):
    attachment = file_of(reply_with(staff_request(), agent, [png_file()]))
    url = reverse("attachments:withdraw", args=[attachment.pk])
    client.force_login(agent)
    assert client.get(url).status_code == 404
    client.force_login(supervisor)
    assert client.get(url).status_code == 200
    response = client.post(url, {"reason": "Attached to the wrong request"})
    assert response.status_code == 302
    attachment.refresh_from_db()
    assert attachment.withdrawal_reason == "Attached to the wrong request"


# History and storage -----------------------------------------------------------------------


def test_attachments_and_bytes_cannot_be_changed_or_deleted(agent, staff_request):
    attachment = file_of(reply_with(staff_request(), agent, [png_file()]))
    for statement in (
        "UPDATE attachments_attachment SET original_name = 'x' WHERE id = %s",
        "DELETE FROM attachments_attachment WHERE id = %s",
        "UPDATE attachments_attachmentblob SET data = '\\x00' WHERE attachment_id = %s",
        "DELETE FROM attachments_attachmentblob WHERE attachment_id = %s",
    ):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(statement, [attachment.pk])


def test_list_queries_never_load_bytes(agent, supervisor, clinic_portal, staff_request):
    request = staff_request()
    for _ in range(3):
        reply_with(request, agent, [png_file(), png_file("b.png")])
    template = Template(
        "{% for entry in entries %}"
        '{% include "attachments/_entry_files.html" with entry=entry audience=audience %}'
        "{% endfor %}"
    )
    for user, audience in ((supervisor, "staff"), (clinic_portal, "portal")):
        with CaptureQueriesContext(connection) as queries:
            entries = with_files(RequestEntry.objects.filter(request=request).order_by("pk"))
            html = template.render(
                Context({"entries": entries, "audience": audience, "user": user})
            )
        sql = " ".join(q["sql"] for q in queries.captured_queries).lower()
        assert "attachmentblob" not in sql and '"data"' not in sql
        assert len(queries.captured_queries) <= 4  # entries, attachments, supervisor check
        assert html.count("/attachments/") >= 6
        assert "Not scanned" in html


def test_entry_files_include_for_the_portal_hides_withdrawn_files(
    agent, supervisor, clinic_portal, staff_request
):
    entry = reply_with(staff_request(), agent, [png_file("kept.png"), png_file("gone.png")])
    gone = Attachment.objects.get(entry=entry, original_name="gone.png")
    withdraw_attachment(actor=supervisor, attachment_id=gone.pk, reason="Wrong file")
    template = Template('{% include "attachments/_entry_files.html" %}')
    entry = with_files(RequestEntry.objects.filter(pk=entry.pk)).get()
    portal = template.render(Context({"entry": entry, "audience": "portal", "user": clinic_portal}))
    assert "kept.png" in portal and "gone.png" not in portal and "Withdraw" not in portal
    staff = template.render(Context({"entry": entry, "audience": "staff", "user": agent}))
    assert "gone.png" in staff and "Withdrawn" in staff and "Withdraw file" not in staff
    assert f"/attachments/{gone.pk}/" not in staff  # only supervisors download a withdrawn file
    boss = template.render(Context({"entry": entry, "audience": "staff", "user": supervisor}))
    assert f"/attachments/{gone.pk}/" in boss and "Withdraw file" in boss


def test_no_name_in_notifications_or_logs(
    client, agent, clinic_portal, clinic, category, caplog, django_capture_on_commit_callbacks
):
    caplog.set_level(logging.DEBUG)
    with django_capture_on_commit_callbacks(execute=True):
        request = create_request_with_files(
            actor=clinic_portal,
            category=category,
            subject="Synthetic",
            first_entry_kind="reply",
            first_entry_body="See file.",
            channel="portal",
            files=[png_file(SECRET_NAME)],
        )
    with django_capture_on_commit_callbacks(execute=True):
        entry = reply_with(request, agent, [png_file(SECRET_NAME)])
    get(client, clinic_portal, file_of(entry))
    assert mail.outbox, "the staff reply should notify the portal account that raised it"
    for message in mail.outbox:
        text = " ".join([message.subject, message.body, json.dumps(dict(message.extra_headers))])
        assert "Jane" not in text and "requisition" not in text
    assert "Jane" not in caplog.text
