"""Files through the client portal: raising a request (generic form), replying, and the
conversation. A file on an internal note or a withdrawn file never shows there."""

from django.urls import reverse

from attachments.models import Attachment
from attachments.services import add_entry_with_files, withdraw_attachment
from desk.models import RequestEntry, ServiceRequest
from taxonomy.terms import drawn

from .conftest import PDF, SAMPLES, png_file, upload


def portal_page(client, request):
    return client.get(reverse("portal:request-detail", args=[request.number])).content.decode()


def test_retry_requires_reattaching_or_omitting_each_previous_file(client, clinic_portal, category):
    client.force_login(clinic_portal)
    url = reverse("portal:request-create")
    data = {"category": category.pk, "category_terms": drawn([category]), "subject": "Packing list"}
    first, second = "labels & notes.txt", "checklist.txt"
    failed = client.post(url, {**data, "files": [upload(first, b"One"), upload(second, b"Two")]})
    assert b'name="files-previous" value="labels &amp; notes.txt"' in failed.content
    assert b'name="files-previous" value="checklist.txt"' in failed.content
    assert b"Your entries were kept" not in failed.content
    data.update(
        {"description": "Synthetic instructions attached.", "files-previous": [first, second]}
    )
    for files in ([], [upload(first, b"One")]):
        refused = client.post(url, {**data, "files": files})
        assert b"Choose these files again or confirm" in refused.content
        assert b'name="files-previous" value="checklist.txt"' in refused.content
    invalid_replacement = client.post(
        url, {**data, "files": [upload("cooler.png", b"Not an image")], "files-omit": "yes"}
    )
    assert b"cooler.png: this is not a PNG image" in invalid_replacement.content
    assert b'name="files-previous" value="cooler.png"' in invalid_replacement.content
    assert b'name="files-previous" value="checklist.txt"' not in invalid_replacement.content
    assert not ServiceRequest.objects.exists()
    assert not Attachment.objects.exists()
    sent = client.post(url, {**data, "files": [upload(first, b"One")], "files-omit": "yes"})
    assert sent.status_code == 302
    assert Attachment.objects.get().original_name == first


def test_client_raises_a_request_with_a_photo(client, clinic_portal, category):
    client.force_login(clinic_portal)
    response = client.post(
        reverse("portal:request-create"),
        {
            "category": category.pk,
            "category_terms": drawn([category]),
            "subject": "Label is smudged",
            "description": "Photo of the sample label attached.",
            "client_reference": "SMP-TEST-1",
            "files": [upload("label.jpg", SAMPLES["photo.jpg"])],
        },
    )
    request = ServiceRequest.objects.get()
    assert response.status_code == 302, response.content.decode()[:3000]
    attachment = Attachment.objects.get()
    assert attachment.entry.kind == "reply" and attachment.uploaded_by == clinic_portal
    page = portal_page(client, request)
    assert "label.jpg" in page and f'href="/attachments/{attachment.pk}/"' in page
    assert (
        'enctype="multipart/form-data"'
        in client.get(reverse("portal:request-create")).content.decode()
    )
    assert client.get(reverse("attachments:download", args=[attachment.pk])).status_code == 200


def test_client_replies_with_a_file_and_a_bad_one_keeps_the_text(
    client, agent, clinic_portal, staff_request
):
    request = staff_request()
    client.force_login(clinic_portal)
    url = reverse("portal:request-reply", args=[request.number])
    request.refresh_from_db()
    response = client.post(
        url,
        {
            "body": "Here is the corrected requisition, typed with care.",
            "expected_revision": request.revision,
            "files": [upload("requisition.pdf", b"<html>not a pdf</html>")],
        },
    )
    page = response.content.decode()
    assert "typed with care" in page and "requisition.pdf: this is not a PDF file." in page
    assert not Attachment.objects.exists()
    response = client.post(
        url,
        {
            "body": "Here is the corrected requisition.",
            "expected_revision": request.revision,
            "files": [upload("requisition.pdf", PDF)],
        },
    )
    assert response.status_code == 302
    assert Attachment.objects.get().entry.author == clinic_portal
    assert "requisition.pdf" in portal_page(client, request)


def test_portal_conversation_shows_only_files_of_visible_replies(
    client, agent, supervisor, clinic_portal, staff_request
):
    request = staff_request()

    def entry(kind, name):
        request.refresh_from_db()
        return add_entry_with_files(
            actor=agent,
            request=request,
            kind=kind,
            body="Synthetic.",
            expected_revision=request.revision,
            files=[png_file(name)],
        )

    entry("reply", "answer.png")
    entry("note", "internal-note.png")
    withdrawn = entry("reply", "wrong-file.png").attachments.get()
    withdraw_attachment(actor=supervisor, attachment_id=withdrawn.pk, reason="Wrong request")
    client.force_login(clinic_portal)
    page = portal_page(client, request)
    assert "answer.png" in page
    assert "internal-note.png" not in page and "wrong-file.png" not in page
    assert RequestEntry.objects.filter(request=request, kind="note").exists()
