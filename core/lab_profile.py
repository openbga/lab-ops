"""The laboratory profile: the identity staff and clients see, its logo, its certificates and the
people it names as its security contact and its privacy contact.

The admin role changes the profile. Every change locks the laboratory row first and is audited
in the same transaction with the values before and after. A change to a contact also needs a
reason, kept with its audit event.

Certificates are listed as the laboratory recorded them, with their state on today's date.
Nothing is checked against a certificate and nothing here is a certification or accreditation
claim.
"""

import hashlib
import io
import os
import threading
from contextlib import contextmanager

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from PIL import Image, ImageFile, ImageOps

from accounts.roles import has_role, require_role

from .audit import record_event, snapshot
from .concurrency import check_original_state
from .models import MAX_LOGO_BYTES, AuditEvent, Lab, LabCertificate, LabContact, LabLogo

PROFILE_ROLES = ("admin",)
IDENTITY_FIELDS = (
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
    "phone",
    "email",
    "website",
)
MAX_CERTIFICATES = 50
# Pillow format name: stored content type. Other decoders are never tried.
LOGO_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg"}
LOGO_MODES = frozenset({"1", "L", "LA", "P", "RGB", "RGBA"})
MIN_LOGO_SIDE = 32
MAX_LOGO_SIDE = 2000
PROFILE_ACTIONS = (
    "lab.profile_changed",
    "lab.logo_changed",
    "lab.logo_removed",
    "lab.certificate_added",
    "lab.certificate_changed",
    "lab.certificate_removed",
    "lab.contact_changed",
)
CONTACT_FIELDS = ("name", "title", "email", "phone")
# state key: (label, status tone)
CERTIFICATE_STATES = {
    "current": ("Current", "success"),
    "not_yet_valid": ("Not yet valid", "info"),
    "expired": ("Expired", "warning"),
}


def can_manage(user):
    return has_role(user, *PROFILE_ROLES)


def _lock_lab():
    lab = Lab.objects.select_for_update().filter(pk=1).first()
    if lab is None:
        raise ValidationError("The laboratory is not set up yet. Run setup_lab first.")
    return lab


def _identity(lab):
    return {name: getattr(lab, name) for name in IDENTITY_FIELDS}


@transaction.atomic
def save_identity(*, actor, values, expected_state=None):
    """Change the address and contact details. Returns (lab, changed)."""
    require_role(actor, *PROFILE_ROLES)
    lab = _lock_lab()
    check_original_state(lab, expected_state)
    before = _identity(lab)
    for name in IDENTITY_FIELDS:
        if name in values:
            setattr(lab, name, (values[name] or "").strip())
    after = _identity(lab)
    if after == before:
        return lab, False
    lab.full_clean()
    lab.save(update_fields=list(IDENTITY_FIELDS))
    record_event(actor=actor, action="lab.profile_changed", obj=lab, before=before, after=after)
    return lab, True


_STRICT_DECODING = threading.Lock()


@contextmanager
def _strict_decoding():
    """Have Pillow refuse truncated or damaged image data, whatever the process-wide setting.

    ImageFile.LOAD_TRUNCATED_IMAGES is process-wide and any library may set it, so the check
    pins it for the duration of one inspection.
    """
    with _STRICT_DECODING:
        tolerant = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = False
        try:
            yield
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = tolerant


def inspect_logo(uploaded):
    """Field values for a LabLogo from an uploaded file. Raises ValidationError.

    The file is untrusted data: only the PNG and JPEG decoders are tried, the pixel size is
    bounded before decoding, and the image is decoded strictly and in full so a damaged file
    is refused here and not shown incomplete. It is then turned and converted the way a
    renderer would, so an image those steps reject is refused too. The stored content type comes
    from the decoded format, never from the upload's name or header. The bytes are kept as
    uploaded; the size recorded is the size as displayed, after any EXIF orientation.
    """
    name = os.path.basename(getattr(uploaded, "name", "") or "")[:255]
    if uploaded.size > MAX_LOGO_BYTES:
        raise ValidationError(f"The logo is larger than {MAX_LOGO_BYTES // 1024} KiB.")
    data = uploaded.read()
    if not data:
        raise ValidationError("The file is empty.")
    if len(data) > MAX_LOGO_BYTES:
        raise ValidationError(f"The logo is larger than {MAX_LOGO_BYTES // 1024} KiB.")
    try:
        with (
            _strict_decoding(),
            Image.open(io.BytesIO(data), formats=tuple(LOGO_FORMATS)) as image,
        ):
            image_format, mode = image.format, image.mode
            width, height = image.size
            sized = (
                MIN_LOGO_SIDE <= width <= MAX_LOGO_SIDE and MIN_LOGO_SIDE <= height <= MAX_LOGO_SIDE
            )
            if image_format in LOGO_FORMATS and sized and mode in LOGO_MODES:
                image.load()
                printed = ImageOps.exif_transpose(image) if "exif" in image.info else image
                printed.convert("RGBA")
                width, height = printed.size
    except Exception as exc:  # Pillow raises many types for damaged or oversized images
        raise ValidationError("Upload a PNG or JPEG image. This file could not be read.") from exc
    if image_format not in LOGO_FORMATS:  # for example a multi-picture JPEG
        raise ValidationError("Upload a PNG or JPEG image. This file could not be read.")
    if not sized:
        raise ValidationError(
            f"The logo is {width} × {height} pixels. Each side must be between "
            f"{MIN_LOGO_SIDE} and {MAX_LOGO_SIDE} pixels."
        )
    if mode not in LOGO_MODES:
        raise ValidationError("Save the logo as an RGB or greyscale image and upload it again.")
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "image": data,
        "content_type": LOGO_FORMATS[image_format],
        "width": width,
        "height": height,
        "size": len(data),
        "filename": name,
    }


def logo_facts(lab):
    """What identifies the laboratory's current logo, without its bytes; None when there is none."""
    if lab is None or lab.logo_id is None:
        return None
    return (
        LabLogo.objects.filter(pk=lab.logo_id)
        .values("sha256", "content_type", "width", "height")
        .first()
    )


def logo_image(sha256):
    """(bytes, content type) of a stored logo checked against its hash; None when unknown."""
    row = (
        LabLogo.objects.with_image()
        .filter(sha256=sha256)
        .values_list("image", "content_type")
        .first()
    )
    if row is None:
        return None
    data = bytes(row[0])
    if hashlib.sha256(data).hexdigest() != sha256:
        return None
    return data, row[1]


@transaction.atomic
def set_logo(*, actor, uploaded, expected_state=None):
    """Make an uploaded image the laboratory's logo. Returns (lab, changed).

    An image uploaded before is reused, so restoring an earlier logo adds no second copy.
    """
    require_role(actor, *PROFILE_ROLES)
    facts = inspect_logo(uploaded)
    lab = _lock_lab()
    check_original_state(lab, expected_state)
    logo = LabLogo.objects.filter(sha256=facts["sha256"]).first()
    if logo is None:
        logo = LabLogo.objects.create(uploaded_by=actor, **facts)
    if lab.logo_id == logo.pk:
        return lab, False
    before = logo_facts(lab) or {}
    lab.logo = logo
    lab.save(update_fields=["logo"])
    record_event(
        actor=actor, action="lab.logo_changed", obj=lab, before=before, after=logo_facts(lab)
    )
    return lab, True


@transaction.atomic
def remove_logo(*, actor, expected_state=None):
    """Stop showing a logo. The image stays stored, so the profile history can still show it."""
    require_role(actor, *PROFILE_ROLES)
    lab = _lock_lab()
    check_original_state(lab, expected_state)
    if lab.logo_id is None:
        return lab, False
    before = logo_facts(lab)
    lab.logo = None
    lab.save(update_fields=["logo"])
    record_event(actor=actor, action="lab.logo_removed", obj=lab, before=before, after={})
    return lab, True


def _lock_certificate(pk):
    row = LabCertificate.objects.select_for_update().filter(pk=pk).first()
    if row is None:
        raise ValidationError("This certificate no longer exists.")
    return row


@transaction.atomic
def save_certificate(*, actor, instance, expected_state=None):
    """Add a certificate or change one. Returns (certificate, changed)."""
    require_role(actor, *PROFILE_ROLES)
    _lock_lab()
    old = None
    if instance.pk:
        old = _lock_certificate(instance.pk)
        check_original_state(old, expected_state)
    elif LabCertificate.objects.count() >= MAX_CERTIFICATES:
        raise ValidationError(f"A laboratory can list at most {MAX_CERTIFICATES} certificates.")
    for name in ("authority", "program", "number"):
        setattr(instance, name, (getattr(instance, name) or "").strip())
    before = snapshot(old) if old else {}
    instance.full_clean()
    if old and snapshot(instance) == before:
        return instance, False
    instance.save()
    record_event(
        actor=actor,
        action="lab.certificate_changed" if old else "lab.certificate_added",
        obj=instance,
        before=before,
        after=snapshot(instance),
    )
    return instance, True


@transaction.atomic
def remove_certificate(*, actor, pk, expected_state=None):
    """Remove a certificate from the list and return what it held, which the audit event keeps."""
    require_role(actor, *PROFILE_ROLES)
    _lock_lab()
    row = _lock_certificate(pk)
    check_original_state(row, expected_state)
    removed = snapshot(row)
    record_event(actor=actor, action="lab.certificate_removed", obj=row, before=removed, after={})
    row.delete()
    return removed


def certificate_state(certificate, on):
    """`current`, `not_yet_valid` or `expired` on a date. Both ends are inclusive and a date
    that is not recorded is no bound."""
    if certificate.valid_from and on < certificate.valid_from:
        return "not_yet_valid"
    if certificate.valid_until and on > certificate.valid_until:
        return "expired"
    return "current"


def certificates_with_state(on):
    """Every certificate in list order, each with `state` on that date."""
    rows = list(LabCertificate.objects.all())
    for row in rows:
        row.state = certificate_state(row, on)
    return rows


def history(limit=20):
    """The newest profile changes, newest first: identity, logo, certificates and contacts."""
    return (
        AuditEvent.objects.filter(action__in=PROFILE_ACTIONS)
        .select_related("actor")
        .order_by("-occurred_at")[:limit]
    )


def contacts():
    """The security contact and the privacy contact, in that order. One never recorded is an
    unsaved LabContact with revision 0, whose fields are blank."""
    rows = {row.kind: row for row in LabContact.objects.all()}
    return [rows.get(kind) or LabContact(kind=kind) for kind in LabContact.Kind.values]


def contact_values(contact):
    return {name: getattr(contact, name) for name in CONTACT_FIELDS}


@transaction.atomic
def save_contact(*, actor, kind, values, expected_revision, reason):
    """Record, change or clear one contact, with a reason. Returns (contact, changed).

    `expected_revision` is the contact's revision the form showed (0 for one never recorded): a
    change saved in between refuses this one. Blank fields clear the contact.
    """
    require_role(actor, *PROFILE_ROLES)
    if kind not in LabContact.Kind.values:
        raise ValueError(f"Unknown contact {kind!r}.")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required."})
    _lock_lab()  # serializes every profile change, the first save of a contact included
    contact = LabContact.objects.select_for_update().filter(kind=kind).first()
    if contact is None:
        contact = LabContact(kind=kind)
    # Who may change it is decided again from the account as it stands once the rows are locked:
    # a role removed or an account deactivated while the request waited counts.
    current = get_user_model().objects.filter(pk=actor.pk).first()
    if current is None:
        raise PermissionDenied("Your role does not permit this action.")
    require_role(current, *PROFILE_ROLES)
    if expected_revision is None or contact.revision != expected_revision:
        raise ValidationError(
            f"The {contact.get_kind_display().lower()} was changed after you opened this form. "
            "Your input has been kept; reload and compare the current details before saving "
            "again."
        )
    before = contact_values(contact)
    for name in CONTACT_FIELDS:
        setattr(contact, name, " ".join((values.get(name) or "").split()))
    after = contact_values(contact)
    if after == before:
        return contact, False
    if not after["name"] and any(after.values()):
        raise ValidationError({"name": "Give the name of the person or office."})
    contact.full_clean(exclude=["revision"])
    contact.revision += 1
    contact.save()
    record_event(
        actor=current,
        action="lab.contact_changed",
        obj=contact,
        before={"kind": kind, **before},
        after={"kind": kind, **after},
        reason=reason,
    )
    return contact, True
