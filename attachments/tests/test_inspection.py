"""What a file must be to be kept: allow-list by extension, type proven by content, limits and
clean names. Pure checks plus the form field."""

import pytest
from django.core.exceptions import ValidationError

from attachments.forms import AttachmentsField
from attachments.inspection import KINDS, clean_name, inspect_bytes, inspect_upload

from .conftest import PDF, SAMPLES, image_bytes, ooxml_bytes, upload

MiB = 1024 * 1024


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_every_allowed_type_is_accepted_with_its_canonical_type(name):
    inspected = inspect_bytes(name, SAMPLES[name])
    assert inspected.content_type == KINDS[inspected.extension].content_type
    assert len(inspected.sha256) == 64 and inspected.size == len(SAMPLES[name])


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("page.html", b"<html><body>x</body></html>"),
        ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'),
        ("tool.exe", b"MZ\x90\x00"),
        ("bundle.zip", ooxml_bytes()),
        ("macro.docm", ooxml_bytes()),
        ("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
        ("script.js", b"alert(1)"),
        ("noextension", PDF),
        (".png", SAMPLES["photo.png"]),
    ],
)
def test_types_outside_the_allow_list_are_refused(name, data):
    with pytest.raises(ValidationError, match="not accepted"):
        inspect_bytes(name, data)


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("photo.png", b"<html><script>alert(1)</script></html>"),  # HTML named .png
        ("photo.png", b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>'),  # SVG
        ("photo.png", b"\x89PNG\r\n\x1a\n<html>not really</html>"),  # magic bytes only
        ("photo.jpg", SAMPLES["photo.png"]),  # a real PNG named .jpg
        ("photo.gif", SAMPLES["photo.jpg"]),
        ("scan.pdf", b"<html>%PDF-1.4</html>\n%%EOF"),  # header not at the start
        ("scan.pdf", b"%PDF-1.4\n<html>"),  # truncated
        ("letter.docx", SAMPLES["photo.png"] + ooxml_bytes()),  # PNG+ZIP polyglot
        ("letter.docx", ooxml_bytes("xl/workbook.xml")),  # a workbook named .docx
        ("letter.docx", b"PK\x03\x04garbage"),
        ("notes.txt", b"Synthetic\x00binary\x00"),
    ],
)
def test_content_must_match_the_extension(name, data):
    with pytest.raises(ValidationError):
        inspect_bytes(name, data)


@pytest.mark.parametrize(
    "text",
    [
        b"<!DOCTYPE html><p>x</p>",
        b"  <html>",
        b"<svg/>",
        b'<?xml version="1.0"?>',
        b"\xef\xbb\xbf<script>",
    ],
)
def test_markup_in_a_text_file_is_refused(text):
    with pytest.raises(ValidationError, match="markup"):
        inspect_bytes("notes.txt", text)


def test_macro_enabled_document_disguised_as_docx_is_refused():
    with pytest.raises(ValidationError, match="macros"):
        inspect_bytes("letter.docx", ooxml_bytes(extra=["word/vbaProject.bin"]))


def test_image_with_too_many_pixels_is_refused():
    with pytest.raises(ValidationError, match="too large in pixels"):
        inspect_bytes("huge.png", image_bytes("PNG", size=(8000, 8000), mode="1"))
    with pytest.raises(ValidationError, match="too large in pixels"):
        inspect_bytes("wide.png", image_bytes("PNG", size=(21000, 1), mode="1"))


def test_size_limits(settings):
    settings.ATTACHMENT_MAX_FILE_BYTES = 1024
    with pytest.raises(ValidationError, match="larger than 1 KB"):
        inspect_bytes("notes.txt", b"a" * 1025)
    inspect_bytes("notes.txt", b"a" * 1024)
    with pytest.raises(ValidationError, match="empty"):
        inspect_bytes("notes.txt", b"")
    big = upload("notes.txt", b"a" * 2048)
    with pytest.raises(ValidationError, match="larger than"):
        inspect_upload(big)


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("../../etc/passwd.png", "passwd.png"),
        ("C:\\Users\\clerk\\Desktop\\scan.pdf", "scan.pdf"),
        ('re"port\r\nX-Injected: 1.pdf', "report X-Injected 1.pdf"),
        ("photo\u202egnp.exe", "photo gnp.exe"),  # right-to-left override removed
        ("  ..hidden.png", "hidden.png"),
        ("café\u0301.png", "café\u0301.png".replace("é\u0301", "é\u0301")),
        ("", "file"),
        ("a" * 300 + ".png", "a" * 196 + ".png"),
    ],
)
def test_names_are_cleaned_for_display(raw, clean):
    assert clean_name(raw) == clean
    assert len(clean_name(raw)) <= 200


def test_right_to_left_trick_cannot_smuggle_an_executable():
    with pytest.raises(ValidationError, match="not accepted"):
        inspect_bytes("photo\u202egnp.exe", b"MZ")


# The form field -----------------------------------------------------------------------------


def test_field_cleans_to_inspected_files_and_ignores_the_browser_type():
    field = AttachmentsField()
    files = field.clean([upload("photo.png", SAMPLES["photo.png"], "text/html")])
    assert files[0].content_type == "image/png"
    assert field.clean([]) == []


def test_field_reports_every_bad_file_and_asks_to_choose_again():
    field = AttachmentsField()
    with pytest.raises(ValidationError) as error:
        field.clean(
            [
                upload("photo.png", b"<html>"),
                upload("scan.pdf", PDF),
                upload("page.html", b"<html>"),
            ]
        )
    messages = error.value.messages
    assert any(m.startswith("photo.png:") for m in messages)
    assert any(m.startswith("page.html:") for m in messages)
    assert not any(m.startswith("scan.pdf:") for m in messages)
    assert messages[-1].startswith("Choose the files again")


def test_field_count_and_total_limits(settings):
    settings.ATTACHMENT_MAX_FILES = 2
    field = AttachmentsField()
    with pytest.raises(ValidationError, match="at most 2 files"):
        field.clean([upload(f"n{i}.txt", b"x") for i in range(3)])
    settings.ATTACHMENT_MAX_FILES = 5
    settings.ATTACHMENT_MAX_ENTRY_BYTES = 10
    with pytest.raises(ValidationError, match="add up to"):
        field.clean([upload("a.txt", b"123456"), upload("b.txt", b"123456")])


def test_widget_offers_camera_and_library_and_several_files():
    html = AttachmentsField().widget.render("files", None, {"id": "id_files"})
    assert 'type="file"' in html and "multiple" in html
    assert 'accept="image/*,.pdf' in html
    assert "capture" not in html  # capture would force the camera and hide the library
    assert "not scanned" in html
