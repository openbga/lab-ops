"""Attachments are added with their entry and withdrawn by a supervisor; nothing else changes them.

Host pages create entries through `add_entry_with_files` and `create_request_with_files`, which
call the desk services and store the files in the same transaction, so an entry and its files
commit together or not at all. The audit payload of an attachment holds its id, entry, size,
type and checksum: never its name or content.

Who may read a file is decided here once (`readable_attachment`) and used by the download view.
"""

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.roles import require_staff
from core.audit import record_event
from desk import services as desk_services
from desk.models import AuthorCapacity, EntryKind, RequestEntry
from desk.permissions import is_supervisor
from desk.portal_scope import portal_requests
from desk.selectors import staff_requests
from desk.visibility import is_client_visible

from .inspection import Inspected, inspect_bytes
from .limits import human_size, max_entry_bytes, max_files
from .models import Attachment, AttachmentBlob

STAFF_KINDS = frozenset({EntryKind.REPLY, EntryKind.NOTE})


def is_portal_account(user):
    return user.is_authenticated and user.client_id is not None


def check_batch(files):
    """Count, type and size of the files of one entry, worked out again from their bytes.

    The service never trusts an inspected object's recorded size, checksum or type, nor the
    limits in force when it was inspected: each file is inspected again
    under the current limits, and the result is what is stored."""
    files = list(files or ())
    if len(files) > max_files():
        raise ValidationError(f"Attach at most {max_files()} files at a time.", code="count")
    for item in files:
        if not isinstance(item, Inspected):
            raise TypeError("Pass files through attachments.inspection first.")
    total = sum(len(item.data) for item in files)
    if total > max_entry_bytes():
        raise ValidationError(
            f"The files add up to {human_size(total)}; the limit is {human_size(max_entry_bytes())}"
            " at a time.",
            code="total",
        )
    return [inspect_bytes(item.name, bytes(item.data)) for item in files]


def _audit_payload(attachment):
    return {
        "entry": attachment.entry_id,
        "request": attachment.request_id,
        "size": attachment.size,
        "content_type": attachment.content_type,
        "sha256": attachment.sha256,
    }


def _attach(actor, entry, files):
    files = check_batch(files)
    if not files:
        return []
    # The entry's recorded capacity decides, never the account looked up again (desk.models).
    portal = entry.author_capacity == AuthorCapacity.CLIENT
    if entry.author_id != actor.pk:
        raise PermissionDenied("Files are added by the author of the entry, with the entry.")
    if portal and entry.kind != EntryKind.REPLY:
        raise PermissionDenied("A client account attaches files to its replies only.")
    if not portal and entry.kind not in STAFF_KINDS:
        raise ValidationError("Files can be attached to a reply or a note.")
    stored = []
    for item in files:
        attachment = Attachment.objects.create(
            request_id=entry.request_id,
            entry=entry,
            original_name=item.name,
            extension=item.extension,
            content_type=item.content_type,
            size=item.size,
            sha256=item.sha256,
            uploaded_by=actor,
        )
        AttachmentBlob.objects.create(attachment=attachment, data=item.data)
        record_event(
            actor=actor, action="attachment.added", obj=attachment, after=_audit_payload(attachment)
        )
        stored.append(attachment)
    return stored


@transaction.atomic
def add_entry_with_files(*, actor, request, files=(), **entry_arguments):
    """desk.services.add_entry, plus the entry's files in the same transaction."""
    check_batch(files)
    entry = desk_services.add_entry(actor=actor, request=request, **entry_arguments)
    _attach(actor, entry, files)
    return entry


def attach_to_new_request(actor, request, files):
    """Attach `files` to the opening entry of a request `actor` has just created, in the
    transaction that created it (for creation paths other than create_request_with_files, such as
    a request raised with a request form). Refused outside a transaction, when the opening entry
    is not the actor's, or when it already has files."""
    files = check_batch(files)
    if not files:
        return []
    if not desk_services.created_in_current_transaction(request):
        raise ValidationError(
            "Files are added with the ticket's opening entry, in the transaction that creates "
            "it, and never afterwards.",
            code="not_new",
        )
    first = (
        RequestEntry.objects.filter(request=request)
        .exclude(kind=EntryKind.EVENT)
        .order_by("created_at", "pk")
        .first()
    )
    if first is None or first.attachments.exists():
        raise ValidationError("Files are added with the ticket's opening entry only.")
    return _attach(actor, first, files)


@transaction.atomic
def create_request_with_files(*, actor, files=(), **request_arguments):
    """desk.services.create_request, plus files on its first entry, in the same transaction."""
    check_batch(files)
    request = desk_services.create_request(actor=actor, **request_arguments)
    attach_to_new_request(actor, request, files)
    return request


@transaction.atomic
def withdraw_attachment(*, actor, attachment_id, reason):
    """A supervisor withdraws a file with a reason. Clients no longer reach it; staff see it
    labelled; only supervisors can still download it."""
    require_staff(actor)
    if not is_supervisor(actor):
        raise PermissionDenied("Only a supervisor can withdraw a file.")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required."})
    attachment = Attachment.objects.select_for_update().get(pk=attachment_id)
    # The acting account read again after the ticket's lock: a supervisor role removed, or an
    # account deactivated, while the change waited is refused.
    request, actor = desk_services.locked_staff_request(actor, attachment.request_id)
    if not is_supervisor(actor):
        raise PermissionDenied("Only a supervisor can withdraw a file.")
    if attachment.withdrawn_at is not None:
        raise ValidationError("This file was already withdrawn.")
    attachment.withdrawn_at = timezone.now()
    attachment.withdrawn_by = actor
    attachment.withdrawal_reason = reason
    attachment.save(update_fields=["withdrawn_at", "withdrawn_by", "withdrawal_reason"])
    record_event(
        actor=actor,
        action="attachment.withdrawn",
        obj=attachment,
        before={"withdrawn": False},
        after={"withdrawn": True, **_audit_payload(attachment)},
        reason=reason,
    )
    RequestEntry.objects.create(
        request_id=attachment.request_id,
        kind=EntryKind.EVENT,
        author=actor,
        author_capacity=AuthorCapacity.LABORATORY,
        event_action="attachment_withdrawn",
        event_data={"attachment": attachment.pk, "entry": attachment.entry_id},
    )
    # Like every change to a request, it advances the revision, so a form or bulk selection
    # opened before the withdrawal is refused as stale.
    request.revision += 1
    request.save(update_fields=["revision", "updated_at"])
    return attachment


def readable_attachment(user, attachment_id):
    """The attachment `user` may download, or ObjectDoesNotExist (the view's 404).

    Staff: the request is in staff_requests (restricted categories). A withdrawn file only for a
    supervisor (PermissionDenied for other staff, who can see that it exists). A portal account:
    the request is in portal_requests, the entry is client-visible (a reply not withdrawn) and
    the file is not withdrawn; anything else is indistinguishable from a missing file."""
    if not is_portal_account(user):
        # The role check comes before any lookup, so an account without a role learns nothing
        # about which files exist.
        require_staff(user)
    attachment = Attachment.objects.select_related("entry", "request").get(pk=attachment_id)
    if is_portal_account(user):
        if (
            attachment.withdrawn_at is not None
            or not is_client_visible(attachment.entry)
            or not portal_requests(user).filter(pk=attachment.request_id).exists()
        ):
            raise Attachment.DoesNotExist
        return attachment
    if not staff_requests(user).filter(pk=attachment.request_id).exists():
        raise Attachment.DoesNotExist
    if attachment.withdrawn_at is not None and not is_supervisor(user):
        raise PermissionDenied("This file was withdrawn; only a supervisor can download it.")
    return attachment


def file_bytes(attachment):
    """The bytes of one attachment the caller has already authorized."""
    try:
        return AttachmentBlob.objects.values_list("data", flat=True).get(attachment=attachment)
    except ObjectDoesNotExist as error:
        raise Attachment.DoesNotExist from error
