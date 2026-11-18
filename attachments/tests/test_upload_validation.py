"""Attachment validation, read authorization and withdrawal."""

import hashlib
from dataclasses import replace
from io import BytesIO
from unittest.mock import Mock

import pytest
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from attachments.forms import AttachmentsField
from attachments.inspection import inspect_bytes
from attachments.models import Attachment
from attachments.services import (
    attach_to_new_request,
    check_batch,
    create_request_with_files,
    readable_attachment,
    withdraw_attachment,
)
from desk.models import RequestEntry

from .conftest import PDF, image_bytes, ooxml_bytes
from .test_attachments import reply_with


def counted_upload(name, data):
    upload = SimpleUploadedFile(name, data)
    instrumented = Mock(wraps=upload)
    instrumented.name = upload.name
    instrumented.size = upload.size
    return instrumented


# withdrawing a file advances the request revision


def test_withdrawing_a_file_advances_the_request_revision(agent, supervisor, staff_request):
    record = staff_request()
    entry = reply_with(record, agent, [inspect_bytes("summary.txt", b"summary")])
    record.refresh_from_db()
    before = record.revision
    withdraw_attachment(
        actor=supervisor, attachment_id=entry.attachments.get().pk, reason="Wrong document"
    )
    assert RequestEntry.objects.filter(request=record, event_action="attachment_withdrawn").exists()
    record.refresh_from_db()
    assert record.revision == before + 1


def test_a_form_opened_before_the_withdrawal_is_refused(client, agent, supervisor, staff_request):
    record = staff_request()
    entry = reply_with(record, agent, [inspect_bytes("summary.txt", b"summary")])
    record.refresh_from_db()
    opened_at = record.revision
    withdraw_attachment(
        actor=supervisor, attachment_id=entry.attachments.get().pk, reason="Wrong document"
    )
    client.force_login(agent)
    response = client.post(
        reverse("desk:request-entry", args=[record.pk]),
        {"expected_revision": opened_at, "kind": "note", "body": "Written on a stale page."},
    )
    assert response.status_code == 400
    assert not RequestEntry.objects.filter(body="Written on a stale page.").exists()


# files only in the transaction that creates the request


@pytest.mark.django_db(transaction=True)
def test_opening_entry_is_sealed_after_its_creating_transaction(agent, staff_request):
    record = staff_request()
    assert not connection.in_atomic_block
    assert not record.entries.exclude(kind="event").earliest("pk").attachments.exists()
    with transaction.atomic(), pytest.raises(ValidationError, match="never afterwards"):
        attach_to_new_request(agent, record, [inspect_bytes("late.txt", b"ordinary text")])
    assert not Attachment.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_files_in_the_creating_transaction_still_work(agent, category, clinic):
    record = create_request_with_files(
        actor=agent,
        category=category,
        subject="Synthetic",
        first_entry_kind="note",
        first_entry_body="Synthetic.",
        channel="fax",
        client=clinic,
        files=[inspect_bytes("fax.pdf", PDF)],
    )
    assert Attachment.objects.get().request == record


# a batch known to be too large is refused before reading


def test_entry_total_is_checked_before_any_file_is_read(settings):
    settings.ATTACHMENT_MAX_FILE_BYTES = 20
    settings.ATTACHMENT_MAX_ENTRY_BYTES = 10
    uploads = [counted_upload("one.txt", b"123456"), counted_upload("two.txt", b"789012")]
    with pytest.raises(ValidationError, match="add up to"):
        AttachmentsField().clean(uploads)
    assert not any(upload.read.called for upload in uploads)


def test_count_and_single_file_guards_do_not_read_streams(settings):
    settings.ATTACHMENT_MAX_FILES = 1
    settings.ATTACHMENT_MAX_FILE_BYTES = 5
    too_large = counted_upload("ordinary.txt", b"123456")
    field = AttachmentsField()
    for chosen in ([too_large], [too_large, too_large]):
        with pytest.raises(ValidationError):
            field.clean(chosen)
    too_large.read.assert_not_called()


# the service works everything out again from the bytes


def test_service_rechecks_the_current_per_file_limit(settings):
    settings.ATTACHMENT_MAX_FILE_BYTES = 20
    inspected = inspect_bytes("example.txt", b"ordinary test text")
    settings.ATTACHMENT_MAX_FILE_BYTES = 10
    settings.ATTACHMENT_MAX_ENTRY_BYTES = 40
    with pytest.raises(ValidationError, match="larger than"):
        check_batch([inspected])


def test_stored_size_and_checksum_come_from_the_stored_bytes(agent, staff_request):
    checked = inspect_bytes("lines.txt", b"ordinary text\n")
    changed = replace(checked, data=checked.data.replace(b"\n", b"\r\n"))
    entry = reply_with(staff_request(), agent, [changed])
    item = entry.attachments.get()
    stored = bytes(item.blob.data)
    assert stored == b"ordinary text\r\n"
    assert (item.size, item.sha256) == (len(stored), hashlib.sha256(stored).hexdigest())


def test_service_rechecks_the_type_of_changed_bytes(agent, staff_request):
    checked = inspect_bytes("photo.png", image_bytes("PNG"))
    swapped = replace(checked, data=b"<html>not an image</html>")
    with pytest.raises(ValidationError, match="not a PNG"):
        reply_with(staff_request(), agent, [swapped])
    assert not Attachment.objects.exists()


# accepted only if it decodes or parses as that type


def test_incomplete_document_is_not_a_word_file():
    with pytest.raises(ValidationError):
        inspect_bytes("empty.docx", ooxml_bytes(content_types=" ", main=""))
    with pytest.raises(ValidationError):  # declared, but the main part is not a document
        inspect_bytes("odd.docx", ooxml_bytes(main="<notes/>"))
    with pytest.raises(ValidationError):  # an entity declaration is refused before parsing
        inspect_bytes(
            "bomb.docx",
            ooxml_bytes(main='<!DOCTYPE d [<!ENTITY a "aaaa">]><document>&a;</document>'),
        )
    inspect_bytes("letter.docx", ooxml_bytes())


def test_pdf_without_objects_is_not_a_document():
    with pytest.raises(ValidationError):
        inspect_bytes("empty.pdf", b"%PDF-1.7\n%%EOF\n")
    inspect_bytes("scan.pdf", PDF)


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "GIF", "WEBP", "TIFF"])
def test_an_image_that_cannot_be_decoded_is_refused(fmt):
    from PIL import Image

    whole = image_bytes(fmt, size=(64, 48), mode="P" if fmt == "GIF" else "RGB")
    cut_short = whole[: len(whole) * 2 // 3]
    with pytest.raises((OSError, SyntaxError)), Image.open(BytesIO(cut_short)) as image:
        image.load()
    with pytest.raises(ValidationError):
        inspect_bytes(f"cut.{fmt.lower()}", cut_short)
    inspect_bytes(f"whole.{fmt.lower()}", whole)


def test_trailing_bytes_after_an_image_are_kept_as_uploaded():
    footer = b"synthetic scanner footer"
    checked = inspect_bytes("photo.png", image_bytes("PNG") + footer)
    assert checked.data.endswith(footer)


# no role, one answer whether or not the file exists


@pytest.mark.django_db(transaction=True)
def test_account_without_a_role_is_refused_before_any_lookup(make_user):
    user = make_user("synthetic_no_roles")
    with CaptureQueriesContext(connection) as reads:
        with pytest.raises(PermissionDenied):
            readable_attachment(user, -1)
    assert all(Attachment._meta.db_table not in q["sql"] for q in reads)


def test_account_without_a_role_gets_one_answer(client, make_user, agent, staff_request):
    present = reply_with(staff_request(), agent, [inspect_bytes("a.txt", b"x")]).attachments.get()
    user = make_user("roleless")
    answers = []
    for pk in (present.pk, present.pk + 1000):
        try:
            readable_attachment(user, pk)
        except PermissionDenied:
            answers.append(403)
        except ObjectDoesNotExist:
            answers.append(404)
    assert answers == [403, 403]
    client.force_login(user)
    statuses = {
        client.get(reverse("attachments:download", args=[pk])).status_code
        for pk in (present.pk, present.pk + 1000)
    }
    assert statuses == {403}
