"""Fixtures for attachment tests: staff and portal accounts of each audience, requests created
through the desk services, and synthetic files built in memory. No real data."""

import io
import zipfile

import pytest
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from accounts.models import User
from attachments.inspection import inspect_bytes
from clients.models import Client, ClientSite, PortalAccess
from taxonomy.models import RequestCategory
from teams.models import Team, TeamMembership

PASSWORD = "test-password-729!"


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def image_bytes(fmt="PNG", size=(40, 30), mode="RGB"):
    buffer = io.BytesIO()
    Image.new(mode, size, "white").save(buffer, format=fmt)
    return buffer.getvalue()


def pdf_bytes():
    """A minimal, well-formed one-page PDF with a cross-reference table."""
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


PDF = pdf_bytes()

_PACKAGE = "http://schemas.openxmlformats.org/package/2006"
_MAIN = {
    "word/document.xml": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>Synthetic letter</w:t></w:r></w:p></w:body></w:document>",
    ),
    "xl/workbook.xml": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheets/></workbook>",
    ),
}


def ooxml_bytes(part="word/document.xml", extra=(), content_types=None, main=None):
    """A minimal Word or Excel package: content types, package relationships and the main part."""
    content_type, body = _MAIN[part]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr(
            "[Content_Types].xml",
            content_types
            or f'<Types xmlns="{_PACKAGE}/content-types"><Override PartName="/{part}" '
            f'ContentType="{content_type}"/></Types>',
        )
        package.writestr(
            "_rels/.rels",
            f'<Relationships xmlns="{_PACKAGE}/relationships"><Relationship Id="rId1" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            f'officeDocument" Target="{part}"/></Relationships>',
        )
        package.writestr(part, body if main is None else main)
        for name in extra:
            package.writestr(name, b"x")
    return buffer.getvalue()


SAMPLES = {
    "scan.pdf": PDF,
    "photo.png": image_bytes("PNG"),
    "photo.jpg": image_bytes("JPEG"),
    "photo.jpeg": image_bytes("JPEG"),
    "anim.gif": image_bytes("GIF", mode="P"),
    "pic.webp": image_bytes("WEBP"),
    "scan.tiff": image_bytes("TIFF"),
    "notes.txt": b"Synthetic note, TEST only.\n",
    "results.csv": b"\xef\xbb\xbfsample,value\nSMP-TEST-1,4.2\n",
    "letter.docx": ooxml_bytes("word/document.xml"),
    "sheet.xlsx": ooxml_bytes("xl/workbook.xml"),
}


def upload(name, data, content_type="application/octet-stream"):
    return SimpleUploadedFile(name, data, content_type=content_type)


def png_file(name="photo.png"):
    return inspect_bytes(name, SAMPLES["photo.png"])


@pytest.fixture
def make_user(db):
    def make(name, *roles, **extra):
        user = User.objects.create_user(name, f"{name}@example.invalid", PASSWORD, **extra)
        for role in roles:
            user.groups.add(Group.objects.get_or_create(name=role)[0])
        return user

    return make


@pytest.fixture
def team(db):
    return Team.objects.filter(is_default=True).first() or Team.objects.create(
        slug="client-services", name="Client services", is_default=True
    )


@pytest.fixture
def quality(db):
    return Team.objects.create(slug="quality", name="Quality")


@pytest.fixture
def agent(make_user, team):
    user = make_user("agent", "client_services")
    TeamMembership.objects.create(team=team, user=user)
    return user


@pytest.fixture
def supervisor(make_user):
    return make_user("supervisor", "supervisor")


@pytest.fixture
def technical(make_user):
    """Staff outside the quality team: cannot read restricted requests."""
    return make_user("technical", "technical")


@pytest.fixture
def group(db):
    return RequestCategory.objects.create(key="status-results", name="Status and results")


@pytest.fixture
def category(group):
    return RequestCategory.objects.create(
        key="missing-result", name="Missing result or report", parent=group, client_may_raise=True
    )


@pytest.fixture
def restricted(group, quality):
    return RequestCategory.objects.create(
        key="privacy-request",
        name="Privacy request",
        parent=group,
        default_team=quality,
        is_restricted=True,
    )


@pytest.fixture
def clinic(db):
    return Client.objects.create(name="Example Family Clinic", account_number="EXCLIN")


@pytest.fixture
def water(db):
    return Client.objects.create(name="Example County Water Authority", account_number="EXWATER")


@pytest.fixture
def north(water):
    return ClientSite.objects.create(client=water, code="NORTH", name="North plant")


@pytest.fixture
def south(water):
    return ClientSite.objects.create(client=water, code="SOUTH", name="South plant")


@pytest.fixture
def clinic_portal(make_user, clinic):
    user = make_user("clinic_portal", client=clinic)
    PortalAccess.objects.create(user=user, client=clinic, whole_organization=True)
    return user


@pytest.fixture
def north_portal(make_user, water, north):
    user = make_user("north_portal", client=water)
    access = PortalAccess.objects.create(user=user, client=water, whole_organization=False)
    access.sites.add(north)
    return user


@pytest.fixture
def staff_request(agent, category, clinic):
    from desk.services import create_request

    def make(*, actor=None, category_=None, client=None, **extra):
        return create_request(
            actor=actor or agent,
            category=category_ or category,
            subject="Synthetic request",
            first_entry_kind="note",
            first_entry_body="Synthetic intake note.",
            channel="email",
            client=client or clinic,
            **extra,
        )

    return make
