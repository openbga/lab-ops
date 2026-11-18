"""Exercise migration-level protections using writes that bypass model save hooks."""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, transaction
from PIL import Image

from core import lab_profile
from core.models import AuditEvent, Lab, LabLogo


@pytest.fixture
def event(staff, db):
    actor = staff("history-admin", "admin")
    with transaction.atomic():
        lab = Lab.objects.create(name="Example Reference Laboratory")
        return AuditEvent.objects.create(
            actor=actor,
            action="lab.profile_changed",
            object_type="core.lab",
            object_id=str(lab.pk),
            after={"city": "Exampleville"},
        )


@pytest.fixture
def logo(staff, event):
    buffer = io.BytesIO()
    Image.new("RGB", (120, 60), "white").save(buffer, format="PNG")
    lab_profile.set_logo(
        actor=event.actor, uploaded=SimpleUploadedFile("logo.png", buffer.getvalue())
    )
    return LabLogo.objects.get()


@pytest.mark.django_db
def test_audit_events_cannot_be_updated_or_deleted_by_any_write_path(event):
    rows = AuditEvent.objects.filter(pk=event.pk)
    for attempt in (
        lambda: rows.update(after={"city": "Rewritten"}),
        lambda: rows.update(reason="Edited later"),
        rows.delete,
    ):
        with pytest.raises(DatabaseError, match="cannot be updated or deleted"):
            with transaction.atomic():
                attempt()
    event.refresh_from_db()
    assert event.after == {"city": "Exampleville"} and event.reason == ""


@pytest.mark.django_db
def test_uploaded_logos_cannot_be_updated_or_deleted_by_any_write_path(logo):
    rows = LabLogo.objects.filter(pk=logo.pk)
    for attempt in (
        lambda: rows.update(filename="renamed.png"),
        lambda: rows.update(sha256="0" * 64),
        lambda: rows._raw_delete(rows.db),  # skips Django's protection of the row in use
    ):
        with pytest.raises(DatabaseError, match="cannot be updated or deleted"):
            with transaction.atomic():
                attempt()
    assert LabLogo.objects.filter(pk=logo.pk, filename=logo.filename).exists()


@pytest.mark.django_db
def test_new_history_rows_can_still_be_added(event, staff):
    with transaction.atomic():
        AuditEvent.objects.create(
            actor=event.actor, action="lab.profile_changed", object_type="core.lab", object_id="1"
        )
    assert AuditEvent.objects.count() == 2
