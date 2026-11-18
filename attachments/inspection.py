"""Decide whether an uploaded file may be kept, from its bytes.

The extension must be on the allow-list, and the content must prove that type: images are opened
by Pillow in that format only and verified, a PDF must start with its header, Word and Excel files
must be OOXML packages with the expected part and no macros, and text must not be markup. The
browser's Content-Type and the file name are never trusted. The type a file is served with is the
canonical one decided here. OWASP File Upload Cheat Sheet (read 2026-11-17).

The original name is cleaned for display only (no path, no control or header characters); the
download uses a generated name.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import re
import unicodedata
import warnings
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree

from django.core.exceptions import ValidationError
from PIL import Image, ImageFile, UnidentifiedImageError

from .limits import human_size, max_file_bytes

NAME_MAX = 200
MAX_PIXELS = 60_000_000  # JPEG, decoded at an eighth of its size
MAX_DECODED_PIXELS = 30_000_000  # other formats, decoded at full size
MAX_SIDE = 20_000
MAX_FRAMES = 100  # pages of a TIFF, frames of an animated image
MAX_TOTAL_DECODED_PIXELS = 120_000_000  # all frames together, as decoded
OOXML_MAX_PARTS = 2_000
OOXML_MAX_UNCOMPRESSED = 200 * 1024 * 1024
_MARKUP = re.compile(
    rb"^\s*<\s*(!doctype|html|head|body|script|svg|\?xml|!--|iframe|object|embed|meta|style|a\b)",
    re.IGNORECASE,
)
_UNSAFE_NAME_CHARS = set('"\\/<>|*?:;\x7f')


@dataclass(frozen=True)
class Kind:
    label: str
    content_type: str
    check: str  # name of the content check below
    image_format: str = ""
    ooxml_part: str = ""


WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
EXCEL = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
KINDS = {
    "pdf": Kind("PDF", "application/pdf", "pdf"),
    "png": Kind("PNG image", "image/png", "image", image_format="PNG"),
    "jpg": Kind("JPEG image", "image/jpeg", "image", image_format="JPEG"),
    "jpeg": Kind("JPEG image", "image/jpeg", "image", image_format="JPEG"),
    "gif": Kind("GIF image", "image/gif", "image", image_format="GIF"),
    "webp": Kind("WebP image", "image/webp", "image", image_format="WEBP"),
    "tif": Kind("TIFF image", "image/tiff", "image", image_format="TIFF"),
    "tiff": Kind("TIFF image", "image/tiff", "image", image_format="TIFF"),
    "txt": Kind("Text", "text/plain", "text"),
    "csv": Kind("CSV", "text/csv", "text"),
    "docx": Kind("Word document", WORD, "ooxml", ooxml_part="word/document.xml"),
    "xlsx": Kind("Excel workbook", EXCEL, "ooxml", ooxml_part="xl/workbook.xml"),
}
ALLOWED_EXTENSIONS = tuple(KINDS)
# For the file input's accept attribute: extensions plus image/* so phones offer the camera and
# the photo library. The server decides; this only shapes the picker.
ACCEPT = ",".join(["image/*", *(f".{ext}" for ext in KINDS)])
ALLOWED_TEXT = "PDF, PNG, JPEG, GIF, WebP, TIFF, text, CSV, Word (.docx) or Excel (.xlsx)"


@dataclass(frozen=True)
class Inspected:
    name: str
    extension: str
    content_type: str
    size: int
    sha256: str
    data: bytes

    @property
    def kind_label(self):
        return KINDS[self.extension].label


def clean_name(raw: str) -> str:
    """A display name: the last path part, NFC, without control, quote, path or header
    characters, at most NAME_MAX characters keeping the extension."""
    name = unicodedata.normalize("NFC", str(raw or ""))
    name = re.split(r"[\\/]", name)[-1]
    name = "".join(
        " " if unicodedata.category(ch)[0] in "CZ" and ch != " " else ch
        for ch in name
        if ch not in _UNSAFE_NAME_CHARS
    )
    name = " ".join(name.split()).lstrip(". ")
    if len(name) > NAME_MAX:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 8:
            name = stem[: NAME_MAX - len(ext) - 1].rstrip() + "." + ext
        else:
            name = name[:NAME_MAX]
    return name or "file"


def extension_of(name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    return ext.lower() if dot and stem else ""


def _refuse(name, message, code):
    raise ValidationError(f"{name}: {message}", code=code)


_PDF_OBJECT = re.compile(rb"\d+\s+\d+\s+obj\b")


def _check_pdf(data, kind, name):
    """A PDF header at the start, at least one object, a cross-reference start and a document
    root, and the end-of-file marker. Without a PDF library this establishes the file's structure,
    not that every page renders."""
    if not data.startswith(b"%PDF-"):
        _refuse(name, "this is not a PDF file.", "content_mismatch")
    if (
        b"%%EOF" not in data[-2048:]
        or b"startxref" not in data[-4096:]
        or not _PDF_OBJECT.search(data)
        or b"endobj" not in data
        or b"/Root" not in data
    ):
        _refuse(name, "the PDF file is incomplete or not a document.", "content_mismatch")


@contextlib.contextmanager
def _strict_images():
    """Decode without Pillow's tolerance for truncated files, whatever another library set."""
    tolerant = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            yield
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = tolerant


def _check_image(data, kind, name):
    """Verify the structure and decode every frame or page. Limit dimensions and pixels per
    frame, the number of frames and the total decoded pixels. Decode JPEGs at one eighth of
    their size to read all compressed data. Other formats decode at full size and have a lower
    pixel limit."""
    jpeg = kind.image_format == "JPEG"
    limit = MAX_PIXELS if jpeg else MAX_DECODED_PIXELS
    decoded = 0
    try:
        with _strict_images():
            with Image.open(io.BytesIO(data), formats=(kind.image_format,)) as image:
                _check_frame_size(image, limit, name)
                image.verify()
            with Image.open(io.BytesIO(data), formats=(kind.image_format,)) as image:
                frame = 0
                while True:
                    width, height = _check_frame_size(image, limit, name)
                    if jpeg:
                        decoded += _decode_jpeg_picture(data, frame)
                    else:
                        decoded += width * height
                    if decoded > MAX_TOTAL_DECODED_PIXELS:
                        _refuse(name, "the image is too large in pixels.", "image_size")
                    if not jpeg:
                        image.load()
                    frame += 1
                    try:
                        image.seek(frame)
                    except EOFError:
                        break
                    if frame >= MAX_FRAMES:
                        _refuse(
                            name,
                            f"an image may have at most {MAX_FRAMES} pages or frames.",
                            "image_frames",
                        )
    except ValidationError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        _refuse(name, f"this is not a {kind.label}, or it is damaged.", "content_mismatch")


def _decode_jpeg_picture(data, frame):
    """One picture of a JPEG (the first, or another of a multi-picture file) decoded at an eighth
    of its size; returns the pixels decoded. Opened afresh for each picture, because Pillow keeps
    the reduced-size setting of the first picture after moving to the next one."""
    with Image.open(io.BytesIO(data), formats=("JPEG",)) as picture:
        picture.seek(frame)
        width, height = picture.size
        picture.draft("RGB", (max(1, width // 8), max(1, height // 8)))
        picture.load()
        return picture.size[0] * picture.size[1]


def _check_frame_size(image, limit, name):
    width, height = image.size
    if width > MAX_SIDE or height > MAX_SIDE or width * height > limit:
        _refuse(name, "the image is too large in pixels.", "image_size")
    return width, height


def _check_text(data, kind, name):
    if b"\x00" in data:
        _refuse(name, f"this is not a {kind.label} file.", "content_mismatch")
    head = data[:4096].removeprefix(b"\xef\xbb\xbf")
    if _MARKUP.match(head):
        _refuse(name, "web page or markup content is not accepted.", "markup")
    try:
        text = data.removeprefix(b"\xef\xbb\xbf").decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    controls = sum(1 for ch in text if unicodedata.category(ch) == "Cc" and ch not in "\t\n\r\f")
    if controls > max(8, len(text) // 100):
        _refuse(name, f"this is not a {kind.label} file.", "content_mismatch")


CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_OOXML = "application/vnd.openxmlformats-officedocument"
OOXML_MAIN_TYPES = {
    "word/document.xml": f"{_OOXML}.wordprocessingml.document.main+xml",
    "xl/workbook.xml": f"{_OOXML}.spreadsheetml.sheet.main+xml",
}
OOXML_ROOTS = {"word/document.xml": "document", "xl/workbook.xml": "workbook"}
OOXML_PART_MAX = 50 * 1024 * 1024
_DTD = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def _xml_part(package, info, name, kind):
    """One package part parsed as XML. Office never writes a DTD, so any DOCTYPE or ENTITY is
    refused before parsing (no entity expansion); parts are size-limited before reading."""
    if info.file_size > OOXML_PART_MAX:
        _refuse(name, "the document is too complex or protected.", "content_mismatch")
    raw = package.read(info)
    if _DTD.search(raw):
        _refuse(name, f"this is not a {kind.label}.", "content_mismatch")
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        _refuse(name, f"this is not a {kind.label}, or it is damaged.", "content_mismatch")


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _check_ooxml(data, kind, name):
    """A Word or Excel package: no macros, no encryption, within part limits, its content types
    declaring the main part with the right type, a package relationships part, and the main part
    parsing as a document or workbook."""
    if not data.startswith(b"PK\x03\x04"):
        _refuse(name, f"this is not a {kind.label}.", "content_mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            infos = package.infolist()
            names = {info.filename: info for info in infos}
            if (
                len(infos) > OOXML_MAX_PARTS
                or sum(info.file_size for info in infos) > OOXML_MAX_UNCOMPRESSED
                or any(info.flag_bits & 0x1 for info in infos)  # encrypted entries
            ):
                _refuse(name, "the document is too complex or protected.", "content_mismatch")
            if any(part.lower().endswith(("vbaproject.bin", "vbadata.xml")) for part in names):
                _refuse(name, "documents with macros are not accepted.", "macros")
            required = ("[Content_Types].xml", "_rels/.rels", kind.ooxml_part)
            if any(part not in names for part in required):
                _refuse(name, f"this is not a {kind.label}.", "content_mismatch")
            types = _xml_part(package, names["[Content_Types].xml"], name, kind)
            declared = {
                element.get("PartName", "").lstrip("/"): element.get("ContentType", "")
                for element in types
                if _local(element.tag) == "Override"
            }
            if (
                types.tag != f"{{{CONTENT_TYPES_NS}}}Types"
                or declared.get(kind.ooxml_part) != OOXML_MAIN_TYPES[kind.ooxml_part]
            ):
                _refuse(name, f"this is not a {kind.label}.", "content_mismatch")
            if _local(_xml_part(package, names["_rels/.rels"], name, kind).tag) != "Relationships":
                _refuse(name, f"this is not a {kind.label}.", "content_mismatch")
            main = _xml_part(package, names[kind.ooxml_part], name, kind)
            if _local(main.tag) != OOXML_ROOTS[kind.ooxml_part]:
                _refuse(name, f"this is not a {kind.label}.", "content_mismatch")
    except ValidationError:
        raise
    except zipfile.BadZipFile, OSError, ValueError, EOFError, KeyError, NotImplementedError:
        _refuse(name, f"this is not a {kind.label}, or it is damaged.", "content_mismatch")


CHECKS = {"pdf": _check_pdf, "image": _check_image, "text": _check_text, "ooxml": _check_ooxml}


def inspect_bytes(raw_name: str, data: bytes) -> Inspected:
    name = clean_name(raw_name)
    extension = extension_of(name)
    if extension not in KINDS:
        _refuse(name, f"this type of file is not accepted. Use {ALLOWED_TEXT}.", "type")
    if not data:
        _refuse(name, "the file is empty.", "empty")
    if len(data) > max_file_bytes():
        _refuse(name, f"the file is larger than {human_size(max_file_bytes())}.", "size")
    kind = KINDS[extension]
    CHECKS[kind.check](data, kind, name)
    return Inspected(
        name=name,
        extension=extension,
        content_type=kind.content_type,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def inspect_upload(uploaded) -> Inspected:
    """An UploadedFile to Inspected, reading at most one byte past the limit."""
    name = clean_name(getattr(uploaded, "name", ""))
    if uploaded.size is not None and uploaded.size > max_file_bytes():
        _refuse(name, f"the file is larger than {human_size(max_file_bytes())}.", "size")
    uploaded.seek(0)
    data = uploaded.read(max_file_bytes() + 1)
    return inspect_bytes(getattr(uploaded, "name", ""), data)
