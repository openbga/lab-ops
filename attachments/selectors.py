"""Reads of attachment metadata for host pages. Bytes are never loaded here."""

from django.db.models import Prefetch

from .models import Attachment

LIST_FIELDS = (
    "id",
    "request_id",
    "entry_id",
    "original_name",
    "extension",
    "content_type",
    "size",
    "scan_status",
    "uploaded_at",
    "withdrawn_at",
    "withdrawal_reason",
)


def metadata():
    """Attachments with only the columns a list shows (the bytes are in another table anyway)."""
    return Attachment.objects.only(*LIST_FIELDS).order_by("pk")


def with_files(entries):
    """A RequestEntry queryset with each entry's attachments prefetched (one extra query)."""
    return entries.prefetch_related(Prefetch("attachments", queryset=metadata()))
