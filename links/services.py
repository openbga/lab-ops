"""Changes to external systems and record links (L1 to L5).

Every change locks what it changes, checks who may make it, and records an attributed audit event
in the same transaction; a link added to or removed from a request also writes a timeline event.
Person-level links (patient, participant, donor) need the view_patient_information capability
for every action, including reading. Their key and label never enter an audit payload, a timeline
event or an exception message: audit snapshots go through `_link_snapshot`.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.functions import Upper
from django.utils import timezone

from accounts.acting import acting_change
from accounts.roles import has_capability, require_capability, require_role, require_staff
from clients.models import Client
from core.audit import record_event, snapshot
from desk.selectors import locked_in_scope, staff_requests
from desk.services import STALE

from .identifiers import check_https_url, check_link_url, clean_identifier
from .masking import check_label, is_person_type, key_digest, propose_label
from .models import (
    ExternalRecordLink,
    ExternalSystem,
    LinkAttachment,
    LinkRole,
    PersonRemovalReason,
)

CAPABILITY = "view_patient_information"
SETUP_ROLE = "admin"
SYSTEM_FIELDS = ("name", "kind", "base_url", "is_active")


def person_link_readable(user, link):
    """Whether `user` may see anything of a person-level link (its masked label, system or record
    type): the capability, and at least one request carrying it (current or removed) that the
    user can read now. The capability alone opens nothing."""
    if not can_see_person_links(user):
        return False
    return staff_requests(user).filter(link_attachments__link=link).exists()


def can_see_person_links(user):
    return has_capability(user, CAPABILITY)


def _reason(reason):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required."})
    return reason


def _person_reason(reason_code):
    """A person-level link's reason: a code from PersonRemovalReason, never free text, so no key
    can reach the audit trail or the timeline through it."""
    if reason_code not in PersonRemovalReason.values:
        raise ValidationError({"reason_code": "Choose a reason."})
    return reason_code


def _link_snapshot(link):
    """Audit form of a link. A person-level link's key, label and address are left out."""
    data = {
        "system": link.system_id,
        "record_type": link.record_type,
        "identifier_scheme": link.identifier_scheme,
        "is_person": link.is_person,
    }
    if not link.is_person:
        data |= {
            "identifier": link.identifier,
            "display_label": link.display_label,
            "url": link.url,
        }
    return data


def _attachment_snapshot(attachment):
    return {
        "link": str(attachment.link_id),
        "request": attachment.request_id,
        "client": attachment.client_id,
        "role": attachment.role,
        "is_person": attachment.is_person,
        "shown_to_client": attachment.shown_to_client,
        "removed": attachment.removed_at is not None,
    }


def _timeline(request, actor, action, attachment, **extra):
    """An event entry on the request timeline. Ids and codes only, never a label or key."""
    from desk.models import RequestEntry

    RequestEntry.objects.create(
        request=request,
        kind="event",
        author=actor,
        event_action=action,
        event_data={
            "attachment": attachment.pk,
            "record_type": attachment.link.record_type,
            "is_person": attachment.is_person,
            **extra,
        },
    )


def _advance(request):
    """Like every change to a ticket, a change of its links advances its revision, so a page of
    the ticket drawn before it (a client's reply, a note, a bulk selection) is refused as stale.
    The caller holds `request` locked (locked_request)."""
    request.revision += 1
    request.save(update_fields=["revision", "updated_at"])


# External systems (L1) ---------------------------------------------------------------------


def require_setup(user):
    require_role(user, SETUP_ROLE)


def _clean_system(obj):
    obj.name = (obj.name or "").strip()
    try:
        obj.base_url = check_https_url(obj.base_url)
    except ValidationError as error:
        raise ValidationError({"base_url": error.messages}) from error
    obj.full_clean(validate_constraints=False)


def _save_system(obj):
    try:
        with transaction.atomic():
            obj.save()
    except IntegrityError as error:
        raise ValidationError({"name": "Another system has this name or key."}) from error


@transaction.atomic
def create_system(*, actor, key, reason="", **data):
    require_setup(actor)
    obj = ExternalSystem(
        key=(key or "").strip(), **{k: v for k, v in data.items() if k in SYSTEM_FIELDS}
    )
    _clean_system(obj)
    if ExternalSystem.objects.filter(key=obj.key).exists():
        raise ValidationError({"key": "Another system uses this key."})
    _save_system(obj)
    record_event(
        actor=actor,
        action="external_system.created",
        obj=obj,
        after=snapshot(obj),
        reason=(reason or "").strip(),
    )
    return obj


@transaction.atomic
def update_system(*, actor, system_id, expected_revision, reason, **data):
    require_setup(actor)
    reason = _reason(reason)
    obj = ExternalSystem.objects.select_for_update().get(pk=system_id)
    if obj.revision != expected_revision:
        raise ValidationError("This system changed after you opened it. Reload and review it.")
    if "key" in data and (data["key"] or "").strip() != obj.key:
        raise ValidationError({"key": "A system's key never changes."})
    before = snapshot(obj)
    for name in SYSTEM_FIELDS:
        if name in data:
            setattr(obj, name, data[name])
    _clean_system(obj)
    if snapshot(obj) == before:
        raise ValidationError("Change at least one detail before saving.")
    obj.revision += 1
    _save_system(obj)
    record_event(
        actor=actor,
        action="external_system.updated",
        obj=obj,
        before=before,
        after=snapshot(obj),
        reason=reason,
    )
    return obj


# Links (L2, L3, L4) ------------------------------------------------------------------------


def locked_request(actor, request_id):
    """The request, locked, if `actor` may read it (desk.selectors.locked_in_scope);
    DoesNotExist otherwise (the view's 404). The ticket is locked before anything else a link
    change locks (the attachment, the system, the link). Read the acting account again after the
    change's last lock with `_reread`."""
    require_staff(actor)
    return locked_in_scope(staff_requests(actor), request_id)


def _reread(actor, request=None):
    """The acting account read again after a change's last lock, as the ticket services do: with
    a ticket, it must still be one that reading may open (desk.services.settle); without one, an
    active staff account (current_staff). Checks after the locks use this reading."""
    from desk.services import current_staff, settle

    return settle(actor, request) if request is not None else current_staff(actor)


CLOSED_MESSAGE = "This ticket is closed, so its links can no longer change."


def require_open(service_request):
    """A closed request's links are frozen: no add, removal, or change of role or "show to
    client", for any kind of link. A resolved request is still open for them."""
    from desk.models import Status

    if service_request.status == Status.CLOSED:
        raise ValidationError(CLOSED_MESSAGE, code="closed")


def _find_or_create_link(actor, *, system, record_type, scheme, identifier, label, url):
    """(link, created). Reuses the row with the same natural key, whatever its label."""
    natural = {
        "system": system,
        "record_type": record_type,
        "identifier_scheme": scheme,
        "identifier": identifier,
    }
    link = ExternalRecordLink.objects.select_for_update().filter(**natural).first()
    if link is not None:
        return link, False
    link = ExternalRecordLink(
        **natural,
        display_label=label,
        url=url,
        is_person=is_person_type(record_type),
        created_by=actor,
    )
    try:
        with transaction.atomic():
            link.save()
    except IntegrityError:
        # Created by someone else a moment ago: reuse theirs.
        return ExternalRecordLink.objects.select_for_update().get(**natural), False
    return link, True


@acting_change
def add_link(
    *,
    actor,
    system,
    record_type,
    identifier,
    request_id=None,
    client_id=None,
    identifier_scheme="",
    display_label="",
    url="",
    role=LinkRole.RELATED,
    shown_to_client=False,
    expected_revision=None,
):
    """Attach a link to a request or a client organization, reusing an existing link with the
    same system, record type, scheme and identifier. Returns (attachment, link_created).

    `expected_revision` is the ticket's revision on the page the request came from: a ticket
    changed since in any way is refused as stale (direct callers may omit it)."""
    require_staff(actor)
    if (request_id is None) == (client_id is None):
        raise ValueError("Attach a link to exactly one ticket or organization.")
    person = is_person_type(record_type)
    if person:
        require_capability(actor, CAPABILITY)
    if role not in LinkRole.values:
        raise ValidationError({"role": "Choose a role."})
    request = locked_request(actor, request_id) if request_id is not None else None
    if request is not None:
        # Who asks is answered before anything about the ticket (changed, closed, its links), and
        # again after the last lock below.
        actor = _reread(actor, request)
        if person:
            require_capability(actor, CAPABILITY)
        _check_revision(request, expected_revision)
        require_open(request)
    client = Client.objects.select_for_update().get(pk=client_id) if client_id is not None else None
    if client is not None and person:
        raise ValidationError({"record_type": "Add person-level links to a ticket."})
    system = ExternalSystem.objects.select_for_update().get(pk=system.pk)
    if request is not None:
        # The system's lock can be waited for: who asks (the account, and for a person-level link
        # the capability) is answered again before the checks below about the ticket (its
        # person-level link), and once more after the link's lock.
        actor = _reread(actor, request)
        if person:
            require_capability(actor, CAPABILITY)
    if not system.is_active:
        raise ValidationError({"system": "This system is inactive and takes no new links."})
    errors = {}
    try:
        identifier = clean_identifier(identifier)
    except ValidationError as error:
        errors["identifier"] = error.messages
    try:
        url = check_link_url(url, system.base_url)
    except ValidationError as error:
        errors["url"] = error.messages
    scheme = (identifier_scheme or "").strip()
    label = (display_label or "").strip()
    if person:
        if (url or "").strip():
            errors["url"] = (
                "Person-level links have no web address. Open the person in the source system."
            )
        if shown_to_client:
            errors["shown_to_client"] = "Person-level links are never shown to clients."
        if "identifier" not in errors:
            label = label or propose_label(record_type, identifier)
            try:
                label = check_label(label, identifier)
            except ValidationError as error:
                errors["display_label"] = error.messages
    else:
        label = label or (identifier if "identifier" not in errors else "")
        if len(label) > 120:
            errors["display_label"] = "Keep the label to 120 characters."
    if errors:
        raise ValidationError(errors)
    if (
        request is not None
        and person
        and LinkAttachment.objects.filter(
            request=request, is_person=True, removed_at__isnull=True
        ).exists()
    ):
        raise ValidationError(
            "This ticket already has a person-level link. Remove it first to link another."
        )
    link, created = _find_or_create_link(
        actor,
        system=system,
        record_type=record_type,
        scheme=scheme,
        identifier=identifier,
        label=label,
        url=url,
    )
    # The acting account read again after the change's last lock (the link's), and the
    # capability for a person-level link checked on that reading.
    actor = _reread(actor, request)
    if person:
        require_capability(actor, CAPABILITY)
    if created:
        record_event(
            actor=actor, action="external_link.created", obj=link, after=_link_snapshot(link)
        )
    duplicate = LinkAttachment.objects.filter(link=link, role=role, removed_at__isnull=True)
    if duplicate.filter(request=request, client=client).exists():
        raise ValidationError(
            f"This {'ticket' if request else 'organization'} already carries this link "
            "in that role."
        )
    attachment = LinkAttachment(
        link=link,
        request=request,
        client=client,
        role=role,
        is_person=link.is_person,
        shown_to_client=bool(shown_to_client) and request is not None and not link.is_person,
        added_by=actor,
    )
    try:
        with transaction.atomic():
            attachment.save()
    except IntegrityError as error:
        raise ValidationError(
            "This ticket already has a person-level link, or already carries this link."
        ) from error
    record_event(
        actor=actor,
        action="external_link.attached",
        obj=attachment,
        after=_attachment_snapshot(attachment),
    )
    if request is not None:
        _timeline(request, actor, "link.attached", attachment)
        _advance(request)
    return attachment, created


def _locked_attachment(actor, attachment_id, expected_revision=None):
    """(attachment, current, request): the attachment's ticket locked first, as add_link locks it
    before the link, then the attachment and its link; then the acting account read again after
    those locks, and the capability for a person-level link checked on that reading. `request` is
    the locked ticket, or None for an organization's link.

    `expected_revision` (the ticket's revision on the page the request came from) is checked right
    after who asks, before anything else about the ticket, as the desk's services check it: a
    ticket changed since in any way (a link shown, hidden or removed, the ticket closed, a note)
    is refused as stale."""
    request_id = (
        LinkAttachment.objects.filter(pk=attachment_id).values_list("request_id", flat=True).get()
    )  # read without a lock: an attachment never moves to another ticket
    request = locked_request(actor, request_id) if request_id is not None else None
    attachment = (
        LinkAttachment.objects.select_for_update().select_related("link").get(pk=attachment_id)
    )
    actor = _reread(actor, request)  # after the last lock, before anything about the ticket
    if attachment.is_person:
        require_capability(actor, CAPABILITY)
    if request is not None:
        _check_revision(request, expected_revision)
        require_open(request)
    return attachment, actor, request


def _check_revision(request, expected_revision):
    """Refuse as stale a change sent from a page of `request` drawn before its latest change."""
    if expected_revision is not None and request.revision != expected_revision:
        raise ValidationError(STALE)


@acting_change
def remove_link(
    *, actor, attachment_id, reason="", reason_code="", note="", expected_revision=None
):
    """Detach a link with a reason. The attachment row stays, marked removed.

    A non-person link takes a free-text reason. A person-level link takes `reason_code` from
    PersonRemovalReason and an optional `note`, kept on the attachment and shown only to holders
    of the capability; the audit event and the timeline carry the code only. On a ticket,
    `expected_revision` is its revision on the removal page: a ticket changed since in any way
    (this link shown or hidden, another link added, a note) is refused as stale (direct callers
    may omit it)."""
    require_staff(actor)
    attachment, actor, request = _locked_attachment(
        actor, attachment_id, expected_revision=expected_revision
    )
    if attachment.is_person:
        code = _person_reason(reason_code)
        reason, note = PersonRemovalReason(code).label, (note or "").strip()
    else:
        code, reason, note = "", _reason(reason), ""
    if attachment.removed_at is not None:
        raise ValidationError("This link was already removed.")
    before = _attachment_snapshot(attachment)
    attachment.removed_at = timezone.now()
    attachment.removed_by = actor
    attachment.removal_reason = reason
    attachment.removal_reason_code = code
    attachment.removal_note = note
    attachment.save(
        update_fields=[
            "removed_at",
            "removed_by",
            "removal_reason",
            "removal_reason_code",
            "removal_note",
        ]
    )
    record_event(
        actor=actor,
        action="external_link.detached",
        obj=attachment,
        before=before,
        after=_attachment_snapshot(attachment),
        reason=code or reason,
    )
    if request is not None:
        extra = {"reason_code": code} if code else {}
        _timeline(request, actor, "link.removed", attachment, **extra)
        _advance(request)
    return attachment


@acting_change
def set_shown_to_client(*, actor, attachment_id, shown, expected_revision=None):
    """Show a ticket's link to the client or hide it. `expected_revision` is the ticket's revision
    on the page the request came from: a ticket changed since in any way (closed, the link
    removed, a note) is refused as stale (direct callers may omit it)."""
    require_staff(actor)
    attachment, actor, request = _locked_attachment(
        actor, attachment_id, expected_revision=expected_revision
    )
    if attachment.removed_at is not None or request is None:
        raise ValidationError("Only a current link on a ticket can be shown to the client.")
    if attachment.is_person and shown:
        raise ValidationError("Person-level links are never shown to clients.")
    if attachment.shown_to_client == bool(shown):
        raise ValidationError("Nothing to change.")
    before = _attachment_snapshot(attachment)
    attachment.shown_to_client = bool(shown)
    attachment.save(update_fields=["shown_to_client"])
    record_event(
        actor=actor,
        action="external_link.attachment_updated",
        obj=attachment,
        before=before,
        after=_attachment_snapshot(attachment),
    )
    _advance(request)
    return attachment


@acting_change
def update_link(
    *, actor, link_id, expected_revision, display_label, url="", reason="", reason_code=""
):
    """Correct a link's label or address. The natural key never changes. A person-level link
    has no address and takes a `reason_code` (PersonRemovalReason) instead of free text, and
    only a reader of one of its carrier requests may change it."""
    require_staff(actor)
    # The system before the link, as add_link takes them (a link's system never changes), then
    # the acting account read again after both.
    system_id = ExternalRecordLink.objects.values_list("system_id", flat=True).get(pk=link_id)
    ExternalSystem.objects.select_for_update().get(pk=system_id)
    link = ExternalRecordLink.objects.select_for_update().select_related("system").get(pk=link_id)
    actor = _reread(actor)
    if link.is_person:
        if not person_link_readable(actor, link):
            raise ExternalRecordLink.DoesNotExist
        reason = _person_reason(reason_code)
        if (url or "").strip():
            raise ValidationError({"url": "Person-level links have no web address."})
    else:
        reason = _reason(reason)
    if link.revision != expected_revision:
        raise ValidationError("This link changed after you opened it. Reload and review it.")
    errors = {}
    label = (display_label or "").strip()
    try:
        label = check_label(label, link.identifier) if link.is_person else label
    except ValidationError as error:
        errors["display_label"] = error.messages
    if not label and "display_label" not in errors:
        errors["display_label"] = "Give a label."
    try:
        url = check_link_url(url, link.system.base_url)
    except ValidationError as error:
        errors["url"] = error.messages
    if errors:
        raise ValidationError(errors)
    if label == link.display_label and url == link.url:
        raise ValidationError("Change at least one detail before saving.")
    changed = sorted(
        name
        for name, value in (("display_label", label), ("url", url))
        if getattr(link, name) != value
    )
    before = _link_snapshot(link)
    link.display_label, link.url = label, url
    link.revision += 1
    link.save(update_fields=["display_label", "url", "revision"])
    after = _link_snapshot(link)
    if link.is_person:
        after["changed"] = changed  # which fields changed, never their values
    record_event(
        actor=actor,
        action="external_link.updated",
        obj=link,
        before=before,
        after=after,
        reason=reason,
    )
    return link


@acting_change
def reveal_person_key(*, actor, attachment_id):
    """The full key of a person-level link on a request the actor may read. Audited; the event
    names the link and the request, never the key."""
    require_staff(actor)
    actor = _reread(actor)  # the account as held (accounts.acting), not the request's copy
    require_capability(actor, CAPABILITY)
    attachment = LinkAttachment.objects.select_related("link", "link__system").get(pk=attachment_id)
    if (
        not attachment.is_person
        or attachment.request_id is None
        or attachment.removed_at is not None  # a removed person link is no longer revealable
    ):
        raise LinkAttachment.DoesNotExist
    staff_requests(actor).get(pk=attachment.request_id)
    record_event(
        actor=actor,
        action="external_link.person_revealed",
        obj=attachment.link,
        after={"request": attachment.request_id, "attachment": attachment.pk},
    )
    return attachment.link


# Searches (L3, L5) -------------------------------------------------------------------------


def request_ids_for_identifier(user, term):
    """Ids of requests the user may read that carry a current non-person link whose identifier
    equals `term`, ignoring case. Person-level keys are never matched here: that search is
    POST-only and audited (`search_person_key`)."""
    term = (term or "").strip()
    if not term:
        return []
    links = ExternalRecordLink.objects.annotate(upper=Upper("identifier")).filter(
        is_person=False, upper=term.upper()
    )
    return list(
        staff_requests(user)
        .filter(link_attachments__link__in=links, link_attachments__removed_at__isnull=True)
        .order_by("-pk")
        .values_list("pk", flat=True)
        .distinct()
    )


@acting_change
def search_person_key(*, actor, key):
    """Ids of requests the actor may read that carry a current person-level link whose key is
    exactly `key`. Audited on the searching account with a keyed digest of the key."""
    require_staff(actor)
    actor = _reread(actor)  # the account as held (accounts.acting), not the request's copy
    require_capability(actor, CAPABILITY)
    key = clean_identifier(key)
    links = list(
        ExternalRecordLink.objects.filter(is_person=True, identifier=key).values_list(
            "pk", flat=True
        )
    )
    request_ids = list(
        staff_requests(actor)
        .filter(link_attachments__link__in=links, link_attachments__removed_at__isnull=True)
        .order_by("-pk")
        .values_list("pk", flat=True)
        .distinct()
    )
    record_event(
        actor=actor,
        action="external_link.person_searched",
        obj=actor,
        after={
            "key_digest": key_digest(key, settings.SECRET_KEY),
            "links": [str(pk) for pk in links],
            "requests": request_ids,
        },
    )
    return request_ids
