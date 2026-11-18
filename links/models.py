"""Links from requests and client organizations to records in the laboratory's other systems.

docs/product.md "Links to external records". Lab Ops holds a typed reference (system, record
type, scheme, identifier), never a copy of the record. One link row per source record; a request
or organization refers to it through a LinkAttachment, which is never deleted: removal sets
`removed_at` with a reason.

Person-level links (patient, research participant, donor) hold the source system's key and a
masked label only, behind the view_patient_information capability (`links/access.py`).
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Upper

from .masking import PERSON_RECORD_TYPES


class SystemKind(models.TextChoices):
    LIS = "lis", "Laboratory information system (LIS)"
    LIMS = "lims", "Laboratory information management system (LIMS)"
    EHR = "ehr", "Electronic health record (EHR)"
    BILLING = "billing", "Billing system"
    OPENBGA = "openbga", "OpenBGA product"
    OTHER = "other", "Other"


class ExternalSystem(models.Model):
    key = models.SlugField(
        max_length=40, unique=True, help_text="Stable identifier; never changes after creation."
    )
    name = models.CharField(max_length=120, unique=True)
    kind = models.CharField(max_length=10, choices=SystemKind.choices, default=SystemKind.OTHER)
    base_url = models.CharField(
        "base address",
        max_length=500,
        blank=True,
        help_text="https only. When set, link addresses must be on this host.",
    )
    is_active = models.BooleanField("active", default=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["name", "pk"]
        verbose_name = "external system"
        constraints = [
            models.CheckConstraint(
                condition=Q(base_url="") | Q(base_url__startswith="https://"),
                name="external_system_https_base_url",
                violation_error_message="Use an https:// address.",
            )
        ]

    def __str__(self):
        return self.name


class RecordType(models.TextChoices):
    ORDER = "order", "Order"
    ACCESSION = "accession", "Accession"
    REQUISITION = "requisition", "Requisition"
    SAMPLE = "sample", "Sample"
    CUSTODY_FORM = "custody_form", "Custody form"
    REPORT = "report", "Report"
    INVOICE = "invoice", "Invoice"
    SHIPMENT = "shipment", "Shipment"
    PROJECT = "project", "Project"
    LOT = "lot", "Lot or batch"
    STUDY = "study", "Study"
    PATIENT = "patient", "Patient"
    PARTICIPANT = "participant", "Research participant"
    DONOR = "donor", "Donor"
    OTHER = "other", "Other"


NON_PERSON_TYPES = [(v, label) for v, label in RecordType.choices if v not in PERSON_RECORD_TYPES]
PERSON_TYPES = [(v, label) for v, label in RecordType.choices if v in PERSON_RECORD_TYPES]


class ExternalRecordLink(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    system = models.ForeignKey(ExternalSystem, on_delete=models.PROTECT, related_name="links")
    record_type = models.CharField(max_length=20, choices=RecordType.choices)
    identifier_scheme = models.CharField(
        "identifier scheme",
        max_length=40,
        blank=True,
        help_text="Optional, when the system has more than one kind of number "
        "(for example accession or placer order).",
    )
    identifier = models.CharField(
        max_length=200, help_text="As the source system writes it. Never parsed."
    )
    display_label = models.CharField("label", max_length=120)
    url = models.CharField(
        "address", max_length=500, blank=True, help_text="https, on the system's host."
    )
    is_person = models.BooleanField(default=False, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        verbose_name = "external record link"
        constraints = [
            models.UniqueConstraint(
                fields=["system", "record_type", "identifier_scheme", "identifier"],
                name="external_link_natural_key",
            ),
            models.CheckConstraint(
                condition=Q(url="") | Q(url__startswith="https://"),
                name="external_link_https_url",
            ),
            models.CheckConstraint(
                condition=Q(is_person=False) | Q(url=""),
                name="external_link_person_has_no_url",
                violation_error_message="Person-level links have no web address.",
            ),
            models.CheckConstraint(
                condition=Q(is_person=True, record_type__in=sorted(PERSON_RECORD_TYPES))
                | Q(is_person=False) & ~Q(record_type__in=sorted(PERSON_RECORD_TYPES)),
                name="external_link_person_matches_type",
            ),
        ]
        indexes = [
            models.Index(
                Upper("identifier"),
                condition=Q(is_person=False),
                name="external_link_identifier_ci",
            ),
            models.Index(
                fields=["identifier"],
                condition=Q(is_person=True),
                name="external_link_person_key",
            ),
        ]

    def __str__(self):
        # Never the identifier: a person-level key must not reach logs or admin titles.
        return f"{self.get_record_type_display()} link"


class PersonRemovalReason(models.TextChoices):
    """Why a person-level link is removed or changed. A fixed list, so no reason text can carry
    a key into the audit trail or the timeline."""

    WRONG_PERSON = "wrong_person", "Wrong person"
    ENTERED_IN_ERROR = "entered_in_error", "Entered in error"
    NO_LONGER_NEEDED = "no_longer_needed", "No longer needed"
    OTHER = "other", "Other"


class LinkRole(models.TextChoices):
    PRIMARY = "primary", "Primary"
    RELATED = "related", "Related"
    ORIGINAL = "original", "Original"
    REPLACEMENT = "replacement", "Replacement or corrected"


class LinkAttachment(models.Model):
    link = models.ForeignKey(
        ExternalRecordLink, on_delete=models.PROTECT, related_name="attachments"
    )
    request = models.ForeignKey(
        "desk.ServiceRequest",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="link_attachments",
    )
    client = models.ForeignKey(
        "clients.Client",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="link_attachments",
    )
    role = models.CharField(max_length=20, choices=LinkRole.choices, default=LinkRole.RELATED)
    is_person = models.BooleanField(default=False, editable=False)
    shown_to_client = models.BooleanField("show to client", default=False)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    added_at = models.DateTimeField(auto_now_add=True)
    removed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    removed_at = models.DateTimeField(null=True, blank=True)
    removal_reason = models.TextField(blank=True)
    # Person-level links only: the reason chosen from PersonRemovalReason, and an optional note
    # shown only to holders of view_patient_information. removal_reason then holds the label.
    removal_reason_code = models.CharField(
        max_length=30, blank=True, choices=PersonRemovalReason.choices
    )
    removal_note = models.TextField(blank=True)

    class Meta:
        ordering = ["added_at", "pk"]
        verbose_name = "link attachment"
        constraints = [
            models.CheckConstraint(
                condition=Q(request__isnull=False, client__isnull=True)
                | Q(request__isnull=True, client__isnull=False),
                name="link_attachment_one_target",
            ),
            models.CheckConstraint(
                condition=~Q(shown_to_client=True, is_person=True),
                name="link_attachment_person_never_shown",
            ),
            models.CheckConstraint(
                condition=Q(removed_at__isnull=True, removed_by__isnull=True, removal_reason="")
                | Q(removed_at__isnull=False, removed_by__isnull=False) & ~Q(removal_reason=""),
                name="link_attachment_removal_complete",
            ),
            models.UniqueConstraint(
                fields=["link", "request", "role"],
                condition=Q(removed_at__isnull=True, request__isnull=False),
                name="link_attachment_request_role_once",
                violation_error_message="This ticket already carries this link in that role.",
            ),
            models.UniqueConstraint(
                fields=["link", "client", "role"],
                condition=Q(removed_at__isnull=True, client__isnull=False),
                name="link_attachment_client_role_once",
                violation_error_message="This organization already carries this link in that role.",
            ),
            models.UniqueConstraint(
                fields=["request"],
                condition=Q(removed_at__isnull=True, is_person=True, request__isnull=False),
                name="link_attachment_one_person_per_request",
                violation_error_message="A ticket carries at most one person-level link.",
            ),
        ]
        indexes = [
            models.Index(fields=["request", "removed_at"], name="link_attachment_request"),
            models.Index(fields=["client", "removed_at"], name="link_attachment_client"),
        ]

    def __str__(self):
        return f"Link attachment {self.pk}"

    @property
    def is_removed(self):
        return self.removed_at is not None
