"""What a reader may see of the links on a request or an organization.

Templates never get an attachment's link directly: they get `LinkRow` objects built here. For a
person-level link and a reader without view_patient_information, the row holds a
`PersonPlaceholder` and nothing else: no identifier, label, address or link id. Holders of the
capability see the masked label; the key itself is shown only by the audited reveal. A
person-level link's address is never rendered on a list (it may hold the key); it comes with the
reveal.
"""

from dataclasses import dataclass

from .identifiers import url_on_system_host
from .masking import PersonPlaceholder
from .models import LinkAttachment


@dataclass(frozen=True)
class LinkRow:
    attachment_id: int
    record_type: str
    record_type_label: str
    role_label: str
    is_person: bool
    removed: bool
    added_by: str
    added_at: object
    removed_by: str = ""
    removed_at: object = None
    removal_reason: str = ""
    removal_note: str = ""  # person-level only, for holders of the capability
    # Present only for a non-person link, or a person link the reader may see.
    link_id: str = ""
    label: str = ""
    system: str = ""
    identifier: str = ""
    identifier_scheme: str = ""
    url: str = ""
    url_on_host: bool = False  # render as a link only while on the system's current host
    shown_to_client: bool = False
    placeholder: PersonPlaceholder | None = None

    @property
    def can_reveal(self):
        return self.is_person and self.placeholder is None and not self.removed


def _person_name(user):
    return (user.get_full_name() or user.get_username()) if user else ""


def _row(attachment, may_see_person):
    link = attachment.link
    removed = attachment.removed_at is not None
    if attachment.is_person and not may_see_person:
        # The placeholder is the whole row: no actor, time, reason or role.
        return LinkRow(
            attachment_id=attachment.pk,
            record_type=link.record_type,
            record_type_label=link.get_record_type_display(),
            role_label="",
            is_person=True,
            removed=removed,
            added_by="",
            added_at=None,
            placeholder=PersonPlaceholder(link.record_type),
        )
    return LinkRow(
        attachment_id=attachment.pk,
        record_type=link.record_type,
        record_type_label=link.get_record_type_display(),
        role_label=attachment.get_role_display(),
        is_person=attachment.is_person,
        removed=removed,
        added_by=_person_name(attachment.added_by),
        added_at=attachment.added_at,
        removed_by=_person_name(attachment.removed_by),
        removed_at=attachment.removed_at,
        removal_reason=attachment.removal_reason,
        removal_note=attachment.removal_note if attachment.is_person else "",
        link_id=str(link.pk),
        label=link.display_label,
        system=link.system.name,
        identifier="" if link.is_person else link.identifier,
        identifier_scheme=link.identifier_scheme,
        url="" if link.is_person else link.url,
        url_on_host=not link.is_person and url_on_system_host(link.url, link.system.base_url),
        shown_to_client=attachment.shown_to_client,
    )


def _rows(queryset, may_see_person):
    queryset = queryset.select_related("link", "link__system", "added_by", "removed_by")
    current, removed = [], []
    for attachment in queryset.order_by("added_at", "pk"):
        row = _row(attachment, may_see_person)
        (removed if row.removed else current).append(row)
    return current, removed


def request_link_rows(request, *, may_see_person):
    """(current, removed) rows for a request the reader is already authorized for."""
    return _rows(LinkAttachment.objects.filter(request=request), may_see_person)


def client_link_rows(client):
    """(current, removed) rows for an organization. Organizations carry no person-level links."""
    return _rows(LinkAttachment.objects.filter(client=client), False)


@dataclass(frozen=True)
class ClientShownLink:
    label: str
    record_type_label: str


def client_shown_links(request):
    """Links staff chose to show on a request, for the client portal: current,
    non-person and ticked "Show to client". Label and record type only."""
    attachments = (
        LinkAttachment.objects.filter(
            request=request, shown_to_client=True, is_person=False, removed_at__isnull=True
        )
        .select_related("link")
        .order_by("added_at", "pk")
    )
    return [
        ClientShownLink(a.link.display_label, a.link.get_record_type_display()) for a in attachments
    ]
