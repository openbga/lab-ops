"""The four lists of the audit review workspace and what each row of them shows.

Changes are the change history (core.models.AuditEvent), Access records the record of sensitive
reads (core.models.AccessRecord), Sign-ins each account's sign-in history (accounts.models.
SignInEvent) and Workspace use this workspace's own record of its use (audit_review.models.
WorkspaceUse). Each is filtered by the fields that apply to it (audit_review.forms), newest first,
and every page and export of one reading is drawn as of the moment its first page was (`upto`).

A row says who did what to which record and when, and never what was shown or changed: no snapshot
of a change, no reason, text, value, file name or key, as the ticket's own pages keep them. Names
are read as they stand now (a record keeps an account's id, not its name). A ticket is shown by its
number and its client organization, whatever its category: the ticket's own pages still decide who
may open it. The browser and address of a sign-in of a shared demonstration account (one whose
credentials are locked) are left out: they may be another visitor's.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db.models import CharField, Q
from django.db.models.functions import Cast
from django.utils import timezone

from accounts import signin_history
from accounts.models import SignInEvent
from accounts.session_list import browser_label
from attachments.models import Attachment
from clients.models import Client, ClientContact, ClientSite, PortalAccess
from core.models import AccessKind, AccessRecord, AuditEvent
from core.templatetags.ui import action_label
from desk import opened
from desk.models import ComplaintReview, RequestEntry, ServiceRequest
from links.models import LinkAttachment
from request_forms.models import FilledForm

from .models import Source, UseKind, WorkspaceUse

PAGE_SIZE = 50
# An export holds at most this many rows; a larger result is narrowed first.
EXPORT_LIMIT = 10_000
NOT_SHOWN = "Not shown: shared demonstration account"
NOT_RECORDED = "Not recorded"


@dataclass(frozen=True)
class Column:
    label: str
    # The label of the line under the cell's text, given its own column in an export.
    note: str = ""
    # Shown as an identifier (the request ID).
    code: bool = False


@dataclass
class Cell:
    text: str
    note: str = ""
    # Shown as an identifier (its column's `code`, set by describe).
    code: bool = False
    # Nothing was recorded for it: shown muted.
    missing: bool = False


@dataclass
class Row:
    occurred_at: datetime
    person: str
    username: str
    cells: list = field(default_factory=list)


# What each list's rows and exports hold after When and Person.
COLUMNS = {
    Source.CHANGES: (
        Column("Action"),
        Column("Record", note="Client organization"),
        Column("Request ID", code=True),
    ),
    Source.ACCESS: (
        Column("What", note="Detail"),
        Column("Capacity"),
        Column("Ticket", note="Client organization"),
        Column("Request ID", code=True),
    ),
    Source.SIGN_INS: (
        Column("Event", note="Details"),
        Column("Browser"),
        Column("Address"),
        Column("Client organization"),
    ),
    Source.USES: (
        Column("What", note="List"),
        Column("Filters"),
        Column("Rows"),
        Column("Request ID", code=True),
    ),
}

# Which filters each list has (audit_review.forms.ReviewFilter).
FILTERS = {
    Source.CHANGES: ("person", "organization", "action", "ticket", "date_from", "date_to"),
    Source.ACCESS: ("person", "organization", "action", "ticket", "date_from", "date_to"),
    Source.SIGN_INS: ("person", "organization", "action", "date_from", "date_to"),
    Source.USES: ("person", "action", "date_from", "date_to"),
}

MODELS = {
    Source.CHANGES: AuditEvent,
    Source.ACCESS: AccessRecord,
    Source.SIGN_INS: SignInEvent,
    Source.USES: WorkspaceUse,
}

# The column naming the account, for each list.
ACCOUNT = {
    Source.CHANGES: "actor_id",
    Source.ACCESS: "account_id",
    Source.SIGN_INS: "user_id",
    Source.USES: "account_id",
}


def action_choices(source):
    """The Action filter's choices for `source`. For Changes, the actions recorded so far."""
    if source == Source.ACCESS:
        return list(AccessKind.choices)
    if source == Source.SIGN_INS:
        return list(SignInEvent.Kind.choices)
    if source == Source.USES:
        return list(UseKind.choices)
    codes = AuditEvent.objects.order_by("action").values_list("action", flat=True).distinct()
    return [(code, action_label(code)) for code in codes]


# ------------------------------------------------------------------------------ reading


def newest(source):
    """What a first page of `source` is drawn as of (`upto`): for a list whose rows have ids given
    out in the order they are written (access records, sign-in events, workspace uses), the newest
    id now, or 0; for Changes, whose ids are random, the moment now, in whole microseconds since
    the epoch, by the application's clock, which also times each change event. An id, not a time,
    where there is one: a sign-out by a session limit is written with the earlier moment the limit
    passed."""
    if source == Source.CHANGES:
        return to_marker(timezone.now())
    return MODELS[source].objects.order_by("-pk").values_list("pk", flat=True).first() or 0


def to_marker(moment):
    return int((moment - datetime(1970, 1, 1, tzinfo=UTC)) / timedelta(microseconds=1))


def from_marker(marker):
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=marker)


def rows_for(source, cleaned, upto):
    """The records of `source` the filters `cleaned` (ReviewFilter.cleaned_data) select, as of
    `upto` (newest), newest first."""
    model = MODELS[source]
    if source == Source.CHANGES:
        records = model.objects.filter(occurred_at__lte=from_marker(upto))
    else:
        records = model.objects.filter(pk__lte=upto)
    if person := cleaned.get("person"):
        records = records.filter(**{ACCOUNT[source]: person.pk})
    if action := cleaned.get("action"):
        records = records.filter(**{_ACTION_FIELD[source]: action})
    if start := cleaned.get("date_from"):
        records = records.filter(occurred_at__gte=_day_start(start))
    if end := cleaned.get("date_to"):
        records = records.filter(occurred_at__lt=_day_start(end + timedelta(days=1)))
    ticket = cleaned.get("ticket")
    organization = cleaned.get("organization")
    if source == Source.CHANGES:
        if ticket is not None:
            records = records.filter(ticket_objects(ServiceRequest.objects.filter(pk=ticket.pk)))
        if organization is not None:
            records = records.filter(organization_objects(organization))
    elif source == Source.ACCESS:
        if ticket is not None:
            records = records.filter(ticket_id=ticket.pk)
        if organization is not None:
            records = records.filter(
                ticket_id__in=ServiceRequest.objects.filter(client=organization).values("pk")
            )
    elif source == Source.SIGN_INS and organization is not None:
        records = records.filter(
            user_id__in=get_user_model().objects.filter(client=organization).values("pk")
        )
    return records.order_by("-occurred_at", "-pk")


_ACTION_FIELD = {
    Source.CHANGES: "action",
    Source.ACCESS: "kind",
    Source.SIGN_INS: "kind",
    Source.USES: "kind",
}


def _day_start(day):
    """The start of `day` in the deployment's time zone, as the dates on the page read."""
    return timezone.make_aware(datetime.combine(day, time.min))


def _text_ids(model, **lookup):
    return (
        model.objects.filter(**lookup)
        .annotate(text_id=Cast("pk", output_field=CharField()))
        .values("text_id")
    )


# Change events about a ticket: on the ticket itself or one of its parts, by the part's model.
TICKET_PARTS = {
    "desk.servicerequest": (ServiceRequest, "pk__in"),
    "desk.requestentry": (RequestEntry, "request__in"),
    "desk.complaintreview": (ComplaintReview, "request__in"),
    "attachments.attachment": (Attachment, "request__in"),
    "links.linkattachment": (LinkAttachment, "request__in"),
    "request_forms.filledform": (FilledForm, "request__in"),
}


def ticket_objects(tickets):
    """Change events on the tickets of `tickets` (a ServiceRequest queryset) or one of their parts:
    their entries, complaint record, files, links and form."""
    condition = Q()
    for object_type, (model, lookup) in TICKET_PARTS.items():
        condition |= Q(
            object_type=object_type,
            object_id__in=_text_ids(model, **{lookup: tickets.values("pk")}),
        )
    return condition


def organization_objects(organization):
    """Change events on the client organization, its contacts, sites, portal access, portal
    accounts and links, and on its tickets (ticket_objects)."""
    condition = ticket_objects(ServiceRequest.objects.filter(client=organization))
    condition |= Q(object_type="clients.client", object_id=str(organization.pk))
    for object_type, model in (
        ("clients.clientcontact", ClientContact),
        ("clients.clientsite", ClientSite),
        ("clients.portalaccess", PortalAccess),
        ("links.linkattachment", LinkAttachment),
    ):
        condition |= Q(object_type=object_type, object_id__in=_text_ids(model, client=organization))
    condition |= Q(
        object_type=get_user_model()._meta.label_lower,
        object_id__in=_text_ids(get_user_model(), client=organization),
    )
    return condition


# ------------------------------------------------------------------------------ describing


def describe(source, records):
    """`records` (one page, or an export) as Row objects."""
    records = list(records)
    accounts = _accounts({getattr(record, ACCOUNT[source]) for record in records})
    describe_rows = {
        Source.CHANGES: _changes,
        Source.ACCESS: _access,
        Source.SIGN_INS: _sign_ins,
        Source.USES: _uses,
    }[source]
    rows = []
    for record, cells in zip(records, describe_rows(records, accounts), strict=True):
        for cell, column in zip(cells, COLUMNS[source], strict=True):
            cell.code = column.code
        person, username = _person(accounts, getattr(record, ACCOUNT[source]))
        rows.append(Row(record.occurred_at, person, username, cells))
    return rows


def _accounts(ids):
    return get_user_model().objects.filter(pk__in=ids).select_related("client").in_bulk()


def _person(accounts, account_id):
    account = accounts.get(account_id)
    if account is None:
        return f"Account {account_id}", "no longer exists"
    return account.get_full_name() or account.get_username(), account.get_username()


def _request_cell(request_ref):
    return Cell(request_ref) if request_ref else Cell(NOT_RECORDED, missing=True)


def _tickets(ids):
    return ServiceRequest.objects.filter(pk__in=ids).select_related("client").in_bulk()


def _ticket_cell(ticket, ticket_id):
    if ticket is None:
        return Cell(f"Ticket {ticket_id}", "")
    return Cell(str(ticket), ticket.client.name)


def _changes(events, _accounts_read):
    # The ticket each event's object belongs to, read by the object's model.
    by_type = {}
    for event in events:
        by_type.setdefault(event.object_type, set()).add(event.object_id)
    owner = {}  # (object_type, object_id) -> ticket id
    for object_type, (model, _lookup) in TICKET_PARTS.items():
        ids = [int(pk) for pk in by_type.get(object_type, ()) if pk.isdigit()]
        if not ids:
            continue
        column = "pk" if object_type == "desk.servicerequest" else "request_id"
        for pk, ticket_id in model.objects.filter(pk__in=ids).values_list("pk", column):
            if ticket_id is not None:
                owner[(object_type, str(pk))] = ticket_id
    tickets = _tickets(set(owner.values()))
    organizations = _organizations(by_type)
    # An account changed is named by its username as it stands now.
    user_type = get_user_model()._meta.label_lower
    usernames = {
        str(pk): username
        for pk, username in get_user_model()
        .objects.filter(pk__in=[int(pk) for pk in by_type.get(user_type, ()) if pk.isdigit()])
        .values_list("pk", "username")
    }
    cells = []
    for event in events:
        ticket_id = owner.get((event.object_type, event.object_id))
        if ticket_id is not None:
            record = _ticket_cell(tickets.get(ticket_id), ticket_id)
            if event.object_type != "desk.servicerequest":
                record.text = f"{_type_label(event.object_type)} on {record.text}"
        else:
            name = usernames.get(event.object_id) if event.object_type == user_type else None
            record = Cell(
                f"Account {name}"
                if name
                else f"{_type_label(event.object_type)} {event.object_id}",
                organizations.get((event.object_type, event.object_id), ""),
            )
        cells.append([Cell(action_label(event.action)), record, _request_cell(event.request_ref)])
    return cells


def _organizations(by_type):
    """The client organization of each event object that belongs to one and not to a ticket."""
    found = {}
    clients = [int(pk) for pk in by_type.get("clients.client", ()) if pk.isdigit()]
    for client in Client.objects.filter(pk__in=clients):
        found[("clients.client", str(client.pk))] = client.name
    user_type = get_user_model()._meta.label_lower
    for object_type, model in (
        ("clients.clientcontact", ClientContact),
        ("clients.clientsite", ClientSite),
        ("clients.portalaccess", PortalAccess),
        ("links.linkattachment", LinkAttachment),
        (user_type, get_user_model()),
    ):
        ids = [int(pk) for pk in by_type.get(object_type, ()) if pk.isdigit()]
        if not ids:
            continue
        for pk, name in model.objects.filter(pk__in=ids, client__isnull=False).values_list(
            "pk", "client__name"
        ):
            found[(object_type, str(pk))] = name
    return found


def _type_label(object_type):
    try:
        model = apps.get_model(object_type)
    except LookupError, ValueError:
        return object_type
    return str(model._meta.verbose_name).capitalize()


def _access(records, _accounts_read):
    described = opened.describe(records)
    tickets = _tickets({record.ticket_id for record in records})
    return [
        [
            Cell(row.what, row.detail),
            Cell(row.capacity),
            _ticket_cell(tickets.get(record.ticket_id), record.ticket_id),
            _request_cell(row.request_ref),
        ]
        for record, row in zip(records, described, strict=True)
    ]


def _sign_ins(events, accounts):
    cells = []
    for event in events:
        account = accounts.get(event.user_id)
        shared = account is not None and account.credentials_locked
        if shared:
            browser = address = NOT_SHOWN
        else:
            browser = browser_label(event.user_agent) or NOT_RECORDED
            address = signin_history.address_text(event.address) or NOT_RECORDED
        organization = account.client.name if account is not None and account.client else ""
        cells.append(
            [
                Cell(event.get_kind_display(), signin_history.event_details(event)),
                Cell(browser),
                Cell(address),
                Cell(organization),
            ]
        )
    return cells


def _uses(uses, accounts):
    people = _accounts(
        {int(use.filters["person"]) for use in uses if str(use.filters.get("person", "")).isdigit()}
    )
    tickets = _tickets(
        {int(use.filters["ticket"]) for use in uses if str(use.filters.get("ticket", "")).isdigit()}
    )
    clients = Client.objects.in_bulk(
        {
            int(use.filters["organization"])
            for use in uses
            if str(use.filters.get("organization", "")).isdigit()
        }
    )
    cells = []
    for use in uses:
        cells.append(
            [
                Cell(use.get_kind_display(), use.get_source_display()),
                Cell(describe_filters(use, people, tickets, clients)),
                Cell(f"{use.rows:,}"),
                _request_cell(use.request_ref),
            ]
        )
    return cells


def describe_filters(use, people, tickets, clients):
    """The filters a use recorded, in words, with names as they stand now."""
    filters = use.filters or {}
    if filters.get("invalid"):
        return "A filter was not accepted: nothing listed"
    parts = []
    if person := filters.get("person"):
        account = people.get(int(person)) if str(person).isdigit() else None
        name = _person(people, account.pk)[0] if account else f"Account {person}"
        parts.append(f"Person: {name}")
    if organization := filters.get("organization"):
        client = clients.get(int(organization)) if str(organization).isdigit() else None
        parts.append(f"Organization: {client.name if client else f'Organization {organization}'}")
    if action := filters.get("action"):
        labels = dict(action_choices(use.source)) if use.source != Source.CHANGES else {}
        parts.append(f"Action: {labels.get(action) or action_label(action)}")
    if ticket := filters.get("ticket"):
        found = tickets.get(int(ticket)) if str(ticket).isdigit() else None
        parts.append(f"Ticket: {found if found else f'Ticket {ticket}'}")
    if start := filters.get("date_from"):
        parts.append(f"From {start}")
    if end := filters.get("date_to"):
        parts.append(f"To {end}")
    if use.kind == UseKind.VIEWED and filters.get("page") not in (None, "", "1"):
        parts.append(f"Page {filters['page']}")
    return "; ".join(parts) or "No filters"
