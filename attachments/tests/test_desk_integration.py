"""Files through the staff request desk: the composer, the new request form, the
conversation and the request history."""

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from attachments.models import Attachment
from attachments.services import withdraw_attachment
from desk.models import RequestEntry
from taxonomy.terms import drawn

from .conftest import PDF, SAMPLES, upload


def compose(client, request, **data):
    request.refresh_from_db()
    return client.post(
        reverse("desk:request-entry", args=[request.pk]),
        {"expected_revision": request.revision, "body": "Synthetic reply.", **data},
    )


def test_reply_with_files_from_the_composer(client, agent, staff_request):
    request = staff_request()
    client.force_login(agent)
    response = compose(
        client,
        request,
        kind="reply",
        files=[upload("requisition.png", SAMPLES["photo.png"]), upload("scan.pdf", PDF)],
    )
    assert response.status_code == 302
    entry = RequestEntry.objects.get(request=request, kind="reply")
    assert sorted(entry.attachments.values_list("original_name", flat=True)) == [
        "requisition.png",
        "scan.pdf",
    ]
    page = client.get(reverse("desk:request-detail", args=[request.pk])).content.decode()
    for attachment in entry.attachments.all():
        assert f'href="/attachments/{attachment.pk}/"' in page
    assert "Not scanned" in page and 'enctype="multipart/form-data"' in page


def test_bad_file_keeps_the_text_and_creates_nothing(client, agent, staff_request):
    request = staff_request()
    entries = RequestEntry.objects.count()
    client.force_login(agent)
    response = compose(
        client,
        request,
        kind="note",
        body="Synthetic note the user typed with care.",
        files=[upload("photo.png", b"<html>not an image</html>")],
    )
    assert response.status_code == 400
    html = response.content.decode()
    assert "Synthetic note the user typed with care." in html
    assert "photo.png: this is not a PNG image, or it is damaged." in html
    assert "Choose the files again" in html
    assert RequestEntry.objects.count() == entries and not Attachment.objects.exists()


def test_files_are_refused_on_a_logged_contact(client, agent, staff_request):
    request = staff_request()
    client.force_login(agent)
    response = compose(
        client,
        request,
        kind="contact",
        contact_direction="to_client",
        contact_channel="phone",
        contact_with="Office manager",
        contact_outcome="spoke_with",
        files=[upload("photo.png", SAMPLES["photo.png"])],
    )
    assert response.status_code == 400
    assert "not a logged contact" in response.content.decode()
    assert not Attachment.objects.exists()


def test_new_request_with_a_file(client, agent, category, clinic):
    client.force_login(agent)
    response = client.post(
        reverse("desk:request-create"),
        {
            "category": category.pk,
            "category_terms": drawn([category]),
            "client": clinic.pk,
            "channel": "fax",
            "subject": "Faxed requisition",
            "first_entry_kind": "note",
            "body": "Requisition arrived by fax; scan attached.",
            "files": [upload("fax-scan.pdf", PDF)],
        },
    )
    assert response.status_code == 302, response.content.decode()[:2000]
    attachment = Attachment.objects.get()
    assert attachment.entry.kind == "note" and attachment.original_name == "fax-scan.pdf"


def test_request_page_lists_files_without_loading_bytes(client, agent, staff_request):
    request = staff_request()
    client.force_login(agent)
    for index in range(3):
        compose(client, request, kind="note", files=[upload(f"n{index}.png", SAMPLES["photo.png"])])
    with CaptureQueriesContext(connection) as queries:
        page = client.get(reverse("desk:request-detail", args=[request.pk]))
    assert page.status_code == 200
    sql = " ".join(q["sql"] for q in queries.captured_queries).lower()
    assert "attachmentblob" not in sql
    assert page.content.decode().count("/attachments/") >= 3


def test_withdrawn_file_on_the_timeline_and_in_the_history(
    client, agent, supervisor, staff_request
):
    request = staff_request()
    client.force_login(agent)
    compose(client, request, kind="reply", files=[upload("wrong-file.png", SAMPLES["photo.png"])])
    attachment = Attachment.objects.get()
    withdraw_attachment(actor=supervisor, attachment_id=attachment.pk, reason="Wrong request")
    page = client.get(reverse("desk:request-detail", args=[request.pk])).content.decode()
    assert "A file was withdrawn" in page and "Withdrawn" in page
    client.force_login(supervisor)
    history = client.get(reverse("desk:request-history", args=[request.pk])).content.decode()
    assert "Attachment added" in history and "Attachment withdrawn" in history
    assert "wrong-file.png" not in history  # audit events never carry a file's name
