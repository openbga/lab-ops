"""File picker and authenticated download journeys at desktop and phone widths."""

import io

import pytest
from conftest import View
from helpers import field, make_user, pick, sign_in
from PIL import Image
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def png(size=(40, 30)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


def mount_picker(page: Page):
    """A form with the real picker, as a host form renders it."""
    from attachments.forms import AttachmentsField

    html = AttachmentsField().widget.render("files", None, {"id": "id_files"})
    page.evaluate(
        """html => {
            const form = document.createElement('form');
            form.id = 'e2e-host';
            form.method = 'post';
            form.enctype = 'multipart/form-data';
            form.innerHTML = '<label for="id_files">Files</label>' + html
              + '<button class="btn btn-primary" type="submit">Send</button>';
            document.querySelector('#main').prepend(form);
            const script = document.createElement('script');
            script.src = form.querySelector('script').getAttribute('src');
            document.body.append(script);
        }""",
        html,
    )
    expect(page.locator("[data-vr-files-field].vr-file-ready")).to_be_visible()


def test_picker_lists_adds_removes_and_checks_before_sending(page: Page, demo, view: View):
    make_user("e2e_agent", demo.password, "client_services")
    sign_in(page, "e2e_agent", demo.password)
    page.goto("/whats-new/")
    mount_picker(page)
    field = page.locator("[data-vr-files-field]")
    expect(field.get_by_text("camera, photo library or files")).to_be_visible()
    expect(field.get_by_text("Files are not scanned for viruses.")).to_be_visible()

    picker = page.locator("#id_files")
    picker.set_input_files(
        {"name": "requisition-scan.png", "mimeType": "image/png", "buffer": png()}
    )
    # A second pick adds to the first (a photo, then another), it does not replace it.
    picker.set_input_files({"name": "second-photo.png", "mimeType": "image/png", "buffer": png()})
    items = field.locator(".vr-files-item")
    expect(items).to_have_count(2)
    expect(items.first).to_contain_text("requisition-scan.png")
    expect(items.first).to_contain_text("bytes")
    assert page.evaluate("document.querySelector('#id_files').files.length") == 2

    picker.set_input_files({"name": "page.html", "mimeType": "text/html", "buffer": b"<html>"})
    problem = field.locator("[data-vr-files-problem]")
    expect(problem).to_contain_text("page.html: this type of file is not accepted.")
    # The form is not sent while a problem is shown.
    page.get_by_role("button", name="Send").click()
    expect(page).to_have_url(page.url)
    expect(problem).to_be_focused()

    field.get_by_role("button", name="Remove page.html").click()
    expect(problem).to_be_hidden()
    expect(items).to_have_count(2)
    if view.phone:
        for box in (
            field.locator(".vr-drop").bounding_box(),
            field.get_by_role("button", name="Remove second-photo.png").bounding_box(),
        ):
            assert box["height"] >= 44
        assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
    assert not page.console_errors


def test_picker_refuses_an_oversized_or_too_many_upload_before_sending(page: Page, demo, settings):
    settings.ATTACHMENT_MAX_FILES = 2
    make_user("e2e_agent", demo.password, "client_services")
    sign_in(page, "e2e_agent", demo.password)
    page.goto("/whats-new/")
    mount_picker(page)
    picker = page.locator("#id_files")
    picker.set_input_files(
        [{"name": f"p{i}.png", "mimeType": "image/png", "buffer": png()} for i in range(3)]
    )
    problem = page.locator("[data-vr-files-problem]")
    expect(problem).to_contain_text("Attach at most 2 files at a time.")
    page.get_by_role("button", name="Remove p2.png").click()
    picker.set_input_files(
        {
            "name": "huge.pdf",
            "mimeType": "application/pdf",
            "buffer": b"%PDF-" + b"0" * (11 * 1024 * 1024),
        }
    )
    expect(problem).to_contain_text("huge.pdf: larger than 10.0 MB.")


def test_downloads_follow_who_can_see_the_entry(page: Page, demo, view: View):
    from accounts.models import User
    from attachments.inspection import inspect_bytes
    from attachments.services import add_entry_with_files, create_request_with_files
    from clients.models import Client, PortalAccess
    from taxonomy.models import RequestCategory

    clinic = Client.objects.get(account_number="EXCLIN")
    portal = User.objects.get(username="demo_client")
    PortalAccess.objects.update_or_create(
        user=portal, defaults={"client": clinic, "whole_organization": True}
    )
    request = create_request_with_files(
        actor=portal,
        category=RequestCategory.objects.get(key="missing-result"),
        subject="Synthetic missing report",
        first_entry_kind="reply",
        first_entry_body="Photo of the requisition attached.",
        channel="portal",
        site=clinic.sites.filter(is_active=True).order_by("pk").first(),
        files=[inspect_bytes("requisition.png", png())],
    )
    staff = User.objects.get(username="demo_client_services")

    def entry(kind, name):
        request.refresh_from_db()
        made = add_entry_with_files(
            actor=staff,
            request=request,
            kind=kind,
            body="Synthetic.",
            expected_revision=request.revision,
            files=[inspect_bytes(name, png())],
        )
        return made.attachments.get()

    reply_file, note_file = entry("reply", "answer.png"), entry("note", "internal.png")

    sign_in(page, "demo_client", demo.password)
    page.goto("/portal/")
    with page.expect_download() as download:
        page.evaluate(
            "url => { const a = document.createElement('a'); a.href = url; "
            "a.download = ''; document.body.append(a); a.click(); }",
            f"/attachments/{reply_file.pk}/",
        )
    assert download.value.suggested_filename == f"{request.number}-{reply_file.pk}.png"
    assert page.request.get(f"/attachments/{note_file.pk}/").status == 404
    page.context.clear_cookies()

    sign_in(page, "demo_client_services", demo.password)
    response = page.request.get(f"/attachments/{note_file.pk}/")
    assert response.status == 200
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_reply_with_a_photo_from_the_composer(page: Page, demo, view: View):
    """The staff composer on the real request page: pick a photo (as from the camera), send a
    reply, see the file in the conversation and download it. A bad file keeps the typed text."""
    from django.contrib.auth import get_user_model

    from desk.services import create_request
    from taxonomy.models import RequestCategory

    request = create_request(
        actor=get_user_model().objects.get(username="demo_client_services"),
        category=RequestCategory.objects.get(key="missing-result"),
        subject="Synthetic missing result",
        first_entry_kind="note",
        first_entry_body="Caller asked about a synthetic order.",
        channel="phone",
    )
    sign_in(page, "demo_client_services", demo.password)
    sizes = [view.viewport] + ([{"width": 360, "height": 780}] if view.phone else [])
    for size in sizes:
        page.set_viewport_size(size)
        page.goto(f"/requests/{request.pk}/")
        composer = page.locator("#composer")
        composer.get_by_label("Reply").check()
        composer.get_by_label("Message").fill("Synthetic: here is the photo of the label.")
        picker = composer.locator("input[type=file]")
        picker.set_input_files(
            {"name": "label-photo.png", "mimeType": "image/png", "buffer": png()}
        )
        expect(composer.locator(".vr-files-item")).to_contain_text("label-photo.png")
        if view.phone:
            assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
            assert composer.locator(".vr-drop").bounding_box()["height"] >= 44
        # A file that is not what its name says is refused on the server; the text stays.
        picker.set_input_files([])
        page.evaluate("""() => {
            const input = document.querySelector('#composer input[type=file]');
            const t = new DataTransfer();
            t.items.add(new File(['<html>no</html>'], 'fake.png', {type: 'image/png'}));
            input.files = t.files;
        }""")
        composer.get_by_role("button", name="Send").click()
        refused = "fake.png: this is not a PNG image, or it is damaged."
        expect(page.get_by_text(refused)).to_be_visible()
        expect(page.locator("#composer").get_by_label("Message")).to_have_value(
            "Synthetic: here is the photo of the label."
        )
        composer = page.locator("#composer")
        composer.locator("input[type=file]").set_input_files(
            {"name": "label-photo.png", "mimeType": "image/png", "buffer": png()}
        )
        composer.get_by_label("Leave out any listed files I do not choose again").check()
        composer.get_by_role("button", name="Send").click()
        expect(
            page.get_by_text("Reply recorded. No portal account can see this ticket.")
        ).to_be_visible()
        conversation = page.locator(".vr-entries")
        link = conversation.get_by_role("link", name="label-photo.png").first
        expect(link).to_be_visible()
        expect(conversation.get_by_text("Not scanned").first).to_be_visible()
        with page.expect_download() as download:
            link.click()
        assert download.value.suggested_filename.startswith(request.number)
    assert not [e for e in page.console_errors if "400" not in e]


def test_client_raises_and_replies_with_a_photo_in_the_portal(page: Page, demo, view: View):
    """The portal raise form and the reply form take photos; the conversation lists them."""
    from accounts.models import User
    from clients.models import Client, PortalAccess

    clinic = Client.objects.get(account_number="EXCLIN")
    portal = User.objects.get(username="demo_client")
    PortalAccess.objects.update_or_create(
        user=portal, defaults={"client": clinic, "whole_organization": True}
    )
    sign_in(page, "demo_client", demo.password)
    sizes = [view.viewport] + ([{"width": 360, "height": 780}] if view.phone else [])
    for index, size in enumerate(sizes):
        page.set_viewport_size(size)
        page.goto("/portal/requests/new/")
        form = page.locator("form.vr-form")
        pick(field(form, "What is it about?"), index=1)
        if form.locator("select[name=site]").count():
            pick(field(form, "Site"), index=1)
        field(form, "Subject").fill(f"Smudged label {index}")
        form.locator("input[type=file]").set_input_files(
            {"name": "label.png", "mimeType": "image/png", "buffer": png()}
        )
        form.get_by_role("button", name="Send", exact=True).click()
        expect(form.locator("[data-vr-files-retry]")).to_contain_text("label.png")
        field(form, "Description").fill("Photo of the sample label attached.")
        form.get_by_role("button", name="Send", exact=True).click()
        expect(form.locator("[data-vr-files-problem]")).to_be_visible()
        form.locator("input[type=file]").set_input_files(
            {"name": "label.png", "mimeType": "image/png", "buffer": png()}
        )
        expect(form.locator(".vr-files-item")).to_contain_text("label.png")
        if view.phone:
            assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
        form.get_by_role("button", name="Send").click()
        expect(page.get_by_text("It is now with the laboratory.")).to_be_visible()
        expect(page.get_by_role("link", name="label.png")).to_be_visible()
        reply = page.locator("form.vr-form")
        field(reply, "Your reply").fill("And the requisition, too.")
        reply.locator("input[type=file]").set_input_files(
            {"name": "requisition.png", "mimeType": "image/png", "buffer": png()}
        )
        reply.get_by_role("button", name="Send reply").click()
        expect(page.get_by_text("Reply sent to the laboratory.")).to_be_visible()
        link = page.get_by_role("link", name="requisition.png")
        expect(link).to_be_visible()
        with page.expect_download() as download:
            link.click()
        assert download.value.suggested_filename.endswith(".png")
