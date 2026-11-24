"""Who opened a ticket: its access records (core.access) as the supervisors' page shows them.

Newest first, a fixed page of rows. Each row names the account (its name and username as they
stand now: a record keeps the account's id, not its name), the capacity and what was read; a file
by its generated download name (the ticket number and the file's id, never the name it was
uploaded with) and an answer by the label its form gave it. Nothing a record points at is shown
beyond that, and nothing is read for a record of another ticket.
"""

from dataclasses import dataclass
from datetime import datetime

from django.contrib.auth import get_user_model
from django.core.paginator import Paginator

from attachments.models import Attachment
from core.models import AccessKind, AccessRecord
from request_forms.models import FieldValue

PAGE_SIZE = 25

FILE = Attachment._meta.label_lower
ANSWER = FieldValue._meta.label_lower


@dataclass
class Opened:
    occurred_at: datetime
    person: str
    username: str
    capacity: str
    what: str
    detail: str
    request_ref: str


def newest(ticket):
    """The id of `ticket`'s newest access record, or 0: what a first page is drawn as of."""
    return (
        AccessRecord.objects.filter(ticket_id=ticket.pk)
        .order_by("-id")
        .values_list("id", flat=True)
    ).first() or 0


def page(ticket, number, upto=None):
    """Page `number` (as typed: Django's get_page settles anything else) of `ticket`'s access
    records with an id up to `upto` (by default its newest), newest first, as Opened rows. The
    pages of one reading are all drawn as of the first (its `upto` travels in the pager's links),
    so records written since, this page's own visit included, never move a row to the next."""
    if upto is None:
        upto = newest(ticket)
    records = AccessRecord.objects.filter(ticket_id=ticket.pk, id__lte=upto).order_by(
        "-occurred_at", "-id"
    )
    page_obj = Paginator(records, PAGE_SIZE).get_page(number)
    page_obj.object_list = describe(page_obj.object_list)
    return page_obj


def describe(records):
    """`records` (AccessRecord rows, of one ticket or of several) as Opened rows. A file or an
    answer is named only when it belongs to the record's own ticket."""
    rows = list(records)
    accounts = {
        account.pk: account
        for account in get_user_model().objects.filter(pk__in={row.account_id for row in rows})
    }
    tickets = {row.ticket_id for row in rows}
    files = {
        (attachment.request_id, str(attachment.pk)): attachment.download_name
        for attachment in Attachment.objects.filter(
            request__in=tickets, pk__in=_ids(rows, FILE)
        ).select_related("request")
    }
    answers = {
        (request_id, str(pk)): label
        for pk, request_id, label in FieldValue.objects.filter(
            filled_form__request__in=tickets, pk__in=_ids(rows, ANSWER)
        ).values_list("pk", "filled_form__request_id", "label")
    }
    return [_opened(row, accounts, files, answers) for row in rows]


def _ids(rows, object_type):
    return [int(row.object_id) for row in rows if row.object_type == object_type]


def _opened(row, accounts, files, answers):
    account = accounts.get(row.account_id)
    if account is None:
        person, username = f"Account {row.account_id}", "no longer exists"
    else:
        person = account.get_full_name() or account.get_username()
        username = account.get_username()
    detail = ""
    key = (row.ticket_id, row.object_id)
    if row.kind == AccessKind.FILE:
        detail = files.get(key) or f"File {row.object_id}"
    elif row.kind == AccessKind.PATIENT_ANSWER:
        detail = answers.get(key) or f"Answer {row.object_id}"
    elif row.kind == AccessKind.PERSON_KEY:
        detail = f"Link {row.object_id}"  # never the key or its masked label
    return Opened(
        occurred_at=row.occurred_at,
        person=person,
        username=username,
        capacity=row.get_capacity_display(),
        what=row.get_kind_display(),
        detail=detail,
        request_ref=row.request_ref,
    )
