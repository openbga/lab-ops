"""Laboratory profile rules: only the admin role changes it, every change is audited in the
same transaction, an uploaded logo is validated and then kept unchanged, and a certificate shows
its state on a given date as the laboratory recorded it."""

import io
import random
from datetime import date
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, transaction
from PIL import Image, ImageFile

from core import lab_profile
from core.concurrency import record_token
from core.models import MAX_LOGO_BYTES, AuditEvent, Lab, LabCertificate, LabLogo

pytestmark = pytest.mark.django_db

TODAY = date(2026, 11, 12)


@pytest.fixture
def lab():
    return Lab.objects.create(name="Example Reference Laboratory")


@pytest.fixture
def admin(staff):
    return staff("profile-admin", "admin")


def image_file(name="logo.png", image_format="PNG", size=(120, 60), mode="RGB", noise=False):
    buffer = io.BytesIO()
    image = Image.new(mode, size, "white" if mode != "CMYK" else (0, 0, 0, 0))
    if noise:  # incompressible content, so the file is long enough to cut anywhere
        image = Image.frombytes(mode, size, random.Random(7).randbytes(size[0] * size[1] * 3))
    image.save(buffer, format=image_format)
    return SimpleUploadedFile(name, buffer.getvalue())


def certificate(**fields):
    values = {"authority": "Example State Laboratory Program", "number": "EX-00000"}
    return LabCertificate(**(values | fields))


def test_only_the_admin_role_changes_the_profile(lab, admin, staff):
    listed, _ = lab_profile.save_certificate(actor=admin, instance=certificate())
    lab_profile.set_logo(actor=admin, uploaded=image_file())
    for role in ("supervisor", "client_services", "account_manager", "technical"):
        user = staff(f"profile-{role}", role)
        attempts = (
            lambda u=user: lab_profile.save_identity(actor=u, values={"city": "Testville"}),
            lambda u=user: lab_profile.set_logo(actor=u, uploaded=image_file()),
            lambda u=user: lab_profile.remove_logo(actor=u),
            lambda u=user: lab_profile.save_certificate(actor=u, instance=certificate()),
            lambda u=user: lab_profile.remove_certificate(actor=u, pk=listed.pk),
        )
        for attempt in attempts:
            with pytest.raises(PermissionDenied):
                attempt()
    lab.refresh_from_db()
    assert lab.city == "" and lab.logo_id is not None
    assert list(LabCertificate.objects.values_list("pk", flat=True)) == [listed.pk]


def test_changes_are_audited_with_values_before_and_after(lab, admin):
    _, changed = lab_profile.save_identity(
        actor=admin,
        values={"address_line1": " 100 Example Way ", "city": "Testville", "state": "NY"},
    )
    assert changed
    assert lab_profile.save_identity(actor=admin, values={"city": "Testville"})[1] is False
    lab_profile.set_logo(actor=admin, uploaded=image_file())
    lab_profile.remove_logo(actor=admin)
    listed, _ = lab_profile.save_certificate(actor=admin, instance=certificate())
    listed.number = "EX-00001"
    lab_profile.save_certificate(actor=admin, instance=listed)
    assert lab_profile.save_certificate(actor=admin, instance=listed)[1] is False
    lab_profile.remove_certificate(actor=admin, pk=listed.pk)

    events = list(AuditEvent.objects.order_by("occurred_at"))
    assert [event.action for event in events] == [
        "lab.profile_changed",
        "lab.logo_changed",
        "lab.logo_removed",
        "lab.certificate_added",
        "lab.certificate_changed",
        "lab.certificate_removed",
    ]
    assert all(event.actor == admin for event in events)
    assert events[0].before["address_line1"] == ""
    assert events[0].after["address_line1"] == "100 Example Way"  # stored trimmed
    assert events[1].before == {} and events[1].after["width"] == 120
    assert events[2].before["sha256"] == events[1].after["sha256"] and events[2].after == {}
    assert events[4].before["number"] == "EX-00000" and events[4].after["number"] == "EX-00001"
    assert events[5].before["number"] == "EX-00001" and events[5].after == {}
    assert not LabCertificate.objects.exists()
    assert [event.pk for event in lab_profile.history()] == [e.pk for e in reversed(events)]


def test_a_change_and_its_audit_event_commit_together(lab, admin):
    listed, _ = lab_profile.save_certificate(actor=admin, instance=certificate())
    with patch("core.lab_profile.record_event", side_effect=RuntimeError("audit failed")):
        with pytest.raises(RuntimeError):
            lab_profile.save_identity(actor=admin, values={"phone": "555-0100"})
        with pytest.raises(RuntimeError):
            lab_profile.set_logo(actor=admin, uploaded=image_file())
        with pytest.raises(RuntimeError):
            lab_profile.remove_certificate(actor=admin, pk=listed.pk)
    lab.refresh_from_db()
    assert lab.phone == "" and lab.logo_id is None and not LabLogo.objects.exists()
    assert LabCertificate.objects.filter(pk=listed.pk).exists()


def test_an_edit_based_on_values_that_changed_since_is_refused(lab, admin):
    opened = record_token(lab)
    lab_profile.save_identity(actor=admin, values={"phone": "555-0100"})
    for stale in (
        lambda: lab_profile.save_identity(
            actor=admin, values={"phone": "555-0199"}, expected_state=opened
        ),
        lambda: lab_profile.set_logo(actor=admin, uploaded=image_file(), expected_state=opened),
        lambda: lab_profile.remove_logo(actor=admin, expected_state=opened),
    ):
        with pytest.raises(ValidationError, match="changed after you opened it"):
            stale()
    listed, _ = lab_profile.save_certificate(actor=admin, instance=certificate())
    opened = record_token(listed)
    listed.number = "EX-00001"
    lab_profile.save_certificate(actor=admin, instance=listed)
    listed.number = "EX-00002"
    with pytest.raises(ValidationError, match="changed after you opened it"):
        lab_profile.save_certificate(actor=admin, instance=listed, expected_state=opened)
    with pytest.raises(ValidationError, match="changed after you opened it"):
        lab_profile.remove_certificate(actor=admin, pk=listed.pk, expected_state=opened)
    lab.refresh_from_db()
    assert lab.phone == "555-0100" and lab.logo_id is None
    assert LabCertificate.objects.get().number == "EX-00001"


def test_identity_and_certificate_values_are_validated(lab, admin):
    for values, field in (
        ({"email": "not-an-address"}, "email"),
        ({"website": "lab.example.invalid"}, "website"),
        ({"website": "javascript:alert(1)"}, "website"),
        ({"state": "ZZ"}, "state"),
    ):
        with pytest.raises(ValidationError) as refused:
            lab_profile.save_identity(actor=admin, values=values)
        assert field in refused.value.error_dict
    with pytest.raises(ValidationError) as refused:
        lab_profile.save_certificate(actor=admin, instance=certificate(number="  "))
    assert "number" in refused.value.error_dict
    with pytest.raises(ValidationError, match="Valid until cannot be earlier"):
        lab_profile.save_certificate(
            actor=admin,
            instance=certificate(valid_from=date(2026, 6, 1), valid_until=date(2026, 5, 31)),
        )
    assert not AuditEvent.objects.exists() and not LabCertificate.objects.exists()


def test_logo_upload_accepts_png_and_jpeg_and_takes_the_type_from_the_content(lab, admin):
    lab_profile.set_logo(actor=admin, uploaded=image_file("logo.png", "PNG", mode="RGBA"))
    assert LabLogo.objects.get().content_type == "image/png"
    # A JPEG named .png is stored as what it is.
    lab_profile.set_logo(actor=admin, uploaded=image_file("misnamed.png", "JPEG"))
    lab.refresh_from_db()
    current = lab_profile.logo_facts(lab)
    assert current["content_type"] == "image/jpeg" and (current["width"], current["height"]) == (
        120,
        60,
    )
    data, content_type = lab_profile.logo_image(current["sha256"])
    assert content_type == "image/jpeg" and data.startswith(b"\xff\xd8\xff")
    assert lab_profile.logo_image("0" * 64) is None


@pytest.mark.parametrize(
    "uploaded, message",
    [
        (SimpleUploadedFile("logo.png", b""), "empty"),
        (SimpleUploadedFile("logo.png", b"<svg xmlns='http://www.w3.org/2000/svg'/>"), "PNG or"),
        (image_file("logo.gif", "GIF"), "PNG or JPEG"),
        (image_file("logo.png", size=(31, 60)), "between 32 and 2000 pixels"),
        (image_file("logo.png", size=(2001, 60)), "between 32 and 2000 pixels"),
        (image_file("logo.jpg", "JPEG", mode="CMYK"), "RGB or greyscale"),
        (SimpleUploadedFile("logo.png", b"\x89PNG\r\n\x1a\n" + b"0" * MAX_LOGO_BYTES), "larger"),
    ],
)
def test_logo_upload_refuses_what_it_cannot_show(lab, admin, uploaded, message):
    with pytest.raises(ValidationError, match=message):
        lab_profile.set_logo(actor=admin, uploaded=uploaded)
    assert not LabLogo.objects.exists() and not AuditEvent.objects.exists()


@pytest.mark.parametrize("image_format", ["PNG", "JPEG"])
@pytest.mark.parametrize("tolerant", [False, True])
def test_a_truncated_image_is_refused(lab, admin, monkeypatch, image_format, tolerant):
    # Another library may make Pillow tolerate truncated files for the whole process; an upload
    # is judged the same either way.
    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", tolerant)
    whole = image_file(image_format=image_format, size=(200, 200), noise=True).read()
    for kept in (len(whole) // 2, len(whole) - 64):  # mid-file, and the last of the pixels
        with pytest.raises(ValidationError, match="could not be read"):
            lab_profile.set_logo(actor=admin, uploaded=SimpleUploadedFile("logo", whole[:kept]))
    assert not LabLogo.objects.exists()
    assert ImageFile.LOAD_TRUNCATED_IMAGES is tolerant  # the process setting is left as found
    lab_profile.set_logo(actor=admin, uploaded=SimpleUploadedFile("logo", whole))
    assert LabLogo.objects.count() == 1


def test_an_image_with_unreadable_orientation_data_is_refused(lab, admin):
    # An image is turned as its EXIF block says; one that cannot be read there is refused
    # although its pixels decode.
    buffer = io.BytesIO()
    Image.new("RGB", (240, 80), "navy").save(buffer, format="PNG", exif=b"\xff" * 40)
    with pytest.raises(ValidationError, match="could not be read"):
        lab_profile.set_logo(
            actor=admin, uploaded=SimpleUploadedFile("logo.png", buffer.getvalue())
        )
    assert not LabLogo.objects.exists()


def test_the_recorded_size_is_the_size_as_shown(lab, admin):
    turned = Image.Exif()
    turned[0x0112] = 6  # EXIF orientation: shown rotated a quarter turn
    buffer = io.BytesIO()
    Image.new("RGB", (120, 60), "white").save(buffer, format="JPEG", exif=turned)
    lab_profile.set_logo(actor=admin, uploaded=SimpleUploadedFile("logo.jpg", buffer.getvalue()))
    stored = LabLogo.objects.get()
    assert (stored.width, stored.height) == (60, 120)
    assert bytes(LabLogo.objects.with_image().get().image) == buffer.getvalue()  # kept as uploaded


def test_an_uploaded_logo_is_kept_unchanged_and_reused(lab, admin):
    first = image_file()
    lab_profile.set_logo(actor=admin, uploaded=first)
    stored = LabLogo.objects.get()
    lab_profile.set_logo(actor=admin, uploaded=image_file(size=(64, 64)))
    first.seek(0)
    _, changed = lab_profile.set_logo(actor=admin, uploaded=first)  # the earlier logo again
    lab.refresh_from_db()
    assert changed and lab.logo_id == stored.pk and LabLogo.objects.count() == 2
    lab_profile.remove_logo(actor=admin)
    assert LabLogo.objects.count() == 2  # the history can still name it
    for mutate in (
        lambda: LabLogo.objects.filter(pk=stored.pk).update(filename="renamed.png"),
        lambda: LabLogo.objects.filter(pk=stored.pk).delete(),
    ):
        with pytest.raises(DatabaseError, match="cannot be updated or deleted"):
            with transaction.atomic():
                mutate()


def test_certificates_show_their_state_on_a_date(lab, admin):
    def add(number, **fields):
        return lab_profile.save_certificate(
            actor=admin, instance=certificate(number=number, **fields)
        )[0]

    add("OPEN", position=1)
    add("FIRST-DAY", position=2, valid_from=TODAY, program="Example program")
    add("LAST-DAY", position=3, valid_until=TODAY)
    add("EXPIRED", position=4, valid_until=date(2026, 11, 11))
    add("UPCOMING", position=5, valid_from=date(2026, 11, 13))
    assert {row.number: row.state for row in lab_profile.certificates_with_state(TODAY)} == {
        "OPEN": "current",
        "FIRST-DAY": "current",
        "LAST-DAY": "current",
        "EXPIRED": "expired",
        "UPCOMING": "not_yet_valid",
    }
