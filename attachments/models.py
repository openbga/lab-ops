"""Files attached to request entries.

An attachment belongs to one entry and is added with it, in the same transaction; it is never
edited or deleted. A supervisor can withdraw it with a reason (a database trigger allows only
that). The bytes live in AttachmentBlob, a separate table, so no query on attachments, entries or
requests can load them by accident; only the download view reads a blob, one at a time.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from .limits import CEILING_BYTES


class ScanStatus(models.TextChoices):
    NOT_SCANNED = "not_scanned", "Not scanned"


class Attachment(models.Model):
    request = models.ForeignKey(
        "desk.ServiceRequest", on_delete=models.PROTECT, related_name="attachments"
    )
    entry = models.ForeignKey(
        "desk.RequestEntry", on_delete=models.PROTECT, related_name="attachments"
    )
    # Shown to those who can see the entry; never used for storage or the download name.
    original_name = models.CharField(max_length=200)
    extension = models.CharField(max_length=8)
    content_type = models.CharField(max_length=100)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    scan_status = models.CharField(
        max_length=20, choices=ScanStatus.choices, default=ScanStatus.NOT_SCANNED
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawn_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    withdrawal_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(size__gt=0, size__lte=CEILING_BYTES), name="attachment_size"
            ),
            models.CheckConstraint(
                condition=Q(
                    withdrawn_at__isnull=True, withdrawn_by__isnull=True, withdrawal_reason=""
                )
                | Q(withdrawn_at__isnull=False, withdrawn_by__isnull=False)
                & ~Q(withdrawal_reason=""),
                name="attachment_withdrawal_complete",
            ),
        ]
        indexes = [models.Index(fields=["entry"], name="attachment_entry")]

    def __str__(self):
        # Never the file name: it may carry a person's name and reaches admin titles and logs.
        return f"Attachment {self.pk}"

    @property
    def is_withdrawn(self):
        return self.withdrawn_at is not None

    @property
    def download_name(self):
        """A generated name: the request number and the attachment id, never the original."""
        return f"{self.request.number or 'ticket'}-{self.pk}.{self.extension}"


class AttachmentBlob(models.Model):
    attachment = models.OneToOneField(
        Attachment, primary_key=True, on_delete=models.PROTECT, related_name="blob"
    )
    data = models.BinaryField()

    def __str__(self):
        return f"Bytes of attachment {self.attachment_id}"
