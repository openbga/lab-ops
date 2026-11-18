"""Upload limits, from settings with defaults. The frame caps a whole request body at
MAX_REQUEST_BODY_BYTES (12 MiB by default): raise it together with these."""

from django.conf import settings

MiB = 1024 * 1024
# Hard ceiling in the database (check constraint); settings may lower the limit, not exceed it.
CEILING_BYTES = 50 * MiB


def max_file_bytes():
    return min(int(getattr(settings, "ATTACHMENT_MAX_FILE_BYTES", 10 * MiB)), CEILING_BYTES)


def max_entry_bytes():
    return int(getattr(settings, "ATTACHMENT_MAX_ENTRY_BYTES", 10 * MiB))


def max_files():
    return int(getattr(settings, "ATTACHMENT_MAX_FILES", 5))


def human_size(size):
    if size < 1024:
        return f"{size} bytes"
    if size < MiB:
        return f"{size / 1024:.0f} KB"
    return f"{size / MiB:.1f} MB"
