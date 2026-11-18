"""The one rule for what a client may see of a request's conversation.

Client-visible means a reply that has not been withdrawn. The client portal, every outbound
notification and every future channel (email replies) read entries through this module and
nothing else (docs/product.md "Entries and visibility").
"""

from django.db.models import Q

from .models import EntryKind

CLIENT_VISIBLE = Q(kind=EntryKind.REPLY, withdrawn_at__isnull=True)


def client_visible(entries):
    """`entries` (a RequestEntry queryset) narrowed to what a client may see."""
    return entries.filter(CLIENT_VISIBLE)


def is_client_visible(entry):
    return entry.kind == EntryKind.REPLY and entry.withdrawn_at is None
