"""The per-client timeline (requirement C6): one feed, newest first, of an organization's requests
(opened, replies, notes, logged contacts, status changes including resolved and closed) and of
the changes to the organization, its sites, contacts and portal access.

Requests are read only through desk.selectors.staff_requests, so a restricted request (and every
entry of it) is absent for staff who may not read it. The two sources are merged in one SQL UNION
ordered by (time, source, id as text), which keeps pagination stable when items share a timestamp.
"""

from django.core.paginator import Paginator
from django.db.models import CharField, F, Q, Value
from django.db.models.functions import Cast, LPad

from core.models import AuditEvent

from .models import ClientContact, ClientSite, PortalAccess

PAGE_SIZE = 25

KINDS = (
    ("opened", "Tickets opened"),
    ("reply", "Replies"),
    ("note", "Internal notes"),
    ("contact", "Logged contacts"),
    ("status", "Status changes"),
    ("organization", "Organization changes"),
)
KIND_LABELS = dict(KINDS)
# desk.models.Status labels, kept here so the timeline reads them without importing the desk at
# module load (the desk depends on clients).
STATUS_LABELS = {
    "open": "Open",
    "waiting_client": "Waiting on client",
    "waiting_third_party": "Waiting on third party",
    "resolved": "Resolved",
    "closed": "Closed",
}
# Request events that belong on the client's timeline; other events (assignment, priority) are
# desk detail shown on the request itself.
TIMELINE_EVENTS = ("created", "status_changed")


def _entries(user, client, kinds, site):
    from desk.models import EntryKind, RequestEntry
    from desk.selectors import staff_requests

    requests = staff_requests(user).filter(client=client)
    if site is not None:
        requests = requests.filter(site=site)
    wanted = Q(pk__in=[])
    if "opened" in kinds:
        wanted |= Q(kind=EntryKind.EVENT, event_action="created")
    if "status" in kinds:
        wanted |= Q(kind=EntryKind.EVENT, event_action="status_changed")
    for kind in ("reply", "note", "contact"):
        if kind in kinds:
            wanted |= Q(kind=kind)
    return RequestEntry.objects.filter(request__in=requests.values("pk")).filter(wanted)


def _audit(client, kinds, site):
    if "organization" not in kinds:
        return AuditEvent.objects.none()
    sites = ClientSite.objects.filter(client=client)
    if site is not None:
        sites = sites.filter(pk=site.pk)
    targets = Q(object_type="clients.clientsite", object_id__in=_ids(sites))
    if site is None:
        targets |= Q(object_type="clients.client", object_id=str(client.pk))
        targets |= Q(
            object_type="clients.clientcontact",
            object_id__in=_ids(ClientContact.objects.filter(client=client)),
        )
        targets |= Q(
            object_type="clients.portalaccess",
            object_id__in=_ids(PortalAccess.objects.filter(user__client=client)),
        )
    else:
        targets |= Q(
            object_type="clients.clientcontact",
            object_id__in=_ids(ClientContact.objects.filter(client=client, sites=site)),
        )
    return AuditEvent.objects.filter(targets)


def _ids(queryset):
    return queryset.annotate(text_id=Cast("pk", CharField())).values("text_id")


def timeline_page(user, client, *, kinds=None, site=None, page=1, per_page=PAGE_SIZE):
    """One page of the organization's timeline for `user` (staff), newest first.

    `kinds` narrows to keys of KINDS (all when empty); `site` narrows to one site's requests and
    that site's own changes (and those of contacts serving it). Returns a Django Page whose
    object_list holds dicts: kind, at, and either `entry` (desk.models.RequestEntry with its
    request) or `event` (core.models.AuditEvent).
    """
    from desk.models import RequestEntry

    kinds = set(kinds or KIND_LABELS)
    entries = (
        _entries(user, client, kinds, site)
        .annotate(
            at=F("created_at"),
            source=Value("entry", output_field=CharField()),
            key=LPad(Cast("pk", CharField()), 20, Value("0")),
        )
        .values_list("at", "source", "key")
    )
    events = (
        _audit(client, kinds, site)
        .annotate(
            at=F("occurred_at"),
            source=Value("event", output_field=CharField()),
            key=Cast("pk", CharField()),
        )
        .values_list("at", "source", "key")
    )
    merged = entries.union(events, all=True).order_by("-at", "-source", "-key")
    paginator = Paginator(merged, per_page)
    page_obj = paginator.get_page(page)
    rows = list(page_obj.object_list)
    entry_ids = [int(key) for _at, source, key in rows if source == "entry"]
    event_ids = [key for _at, source, key in rows if source == "event"]
    loaded_entries = RequestEntry.objects.select_related(
        "author", "request", "request__site"
    ).in_bulk(entry_ids)
    loaded_events = {
        str(pk): event
        for pk, event in AuditEvent.objects.select_related("actor").in_bulk(event_ids).items()
    }
    items = []
    for at, source, key in rows:
        if source == "entry":
            entry = loaded_entries[int(key)]
            items.append({"kind": _entry_kind(entry), "at": at, "entry": entry})
        else:
            items.append({"kind": "organization", "at": at, "event": loaded_events[key]})
    page_obj.object_list = items
    return page_obj


def _entry_kind(entry):
    if entry.kind == "event":
        return "opened" if entry.event_action == "created" else "status"
    return entry.kind
