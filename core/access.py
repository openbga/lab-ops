"""Access records: a record of each sensitive read (core.models.AccessRecord).

Recorded: a staff or client portal account opening a ticket's page (also when the page is drawn
again after a refused reply, note or contact), a ticket's audit history or its Who opened it
page, the staff Form details page of a ticket; downloading a file; being shown a patient-marked
answer, whether a staff member reveals it or a portal account's ticket page shows it; a staff
member revealing a ticket's person-level key; and each ticket whose entries a client
organization's timeline page shows (the start of their text). Not recorded: rows shown in a list,
a search or its text, and the pages that only change a ticket (their forms show its number and
subject).

Each record holds the account, the capacity it read in (laboratory staff or client portal, the
account as it was read for the page), what was read (the kind, the ticket and, for a file or an
answer, which one), the request ID (core.request_logging) and the time by the database's clock.
Never what was shown: no subject, text, file name or value, and nothing about the record is
logged.

A view, or a template tag drawing part of a page, calls `note` once access has been decided and
what it shows has been read, so a refusal (404, 403) or a redirect to confirm it is you first
notes nothing. AccessRecordMiddleware writes what the request noted once the view has answered,
in one statement, after any reading the page was drawn in has ended (a page drawn in
core.concurrency.one_reading writes nothing inside it). An answer of 500 or more showed nothing
and records nothing. When the write fails, the answer is not sent: the request fails as a server
error, so nothing is shown without its record. The record is written after the page was drawn
and before it is sent; a browser that drops the connection meanwhile still leaves the record.
"""

from core.models import AccessCapacity, AccessKind, AccessRecord

PENDING = "_vr_access_pending"


def capacity_of(account):
    """The capacity `account` reads in: a client portal account reads as the client. An account's
    kind never changes (clients migration 0006), so the account as read for the page decides."""
    return AccessCapacity.CLIENT if account.client_id is not None else AccessCapacity.LABORATORY


def note(request, kind, *, ticket_id, target=None, account=None):
    """Note that the request read `kind` of ticket `ticket_id` (for a file or an answer, `target`,
    the Attachment or FieldValue), as `account` (by default the request's user: on a page drawn
    in one reading, the account read in it). Written by AccessRecordMiddleware when the view has
    answered. The same read noted twice in one request is recorded once."""
    if kind not in AccessKind.values:
        raise ValueError(f"Unknown access kind {kind!r}.")
    account = account if account is not None else request.user
    object_type, object_id = ("", "") if target is None else (target._meta.label_lower, target.pk)
    pending = request.__dict__.setdefault(PENDING, {})
    key = (account.pk, kind, int(ticket_id), object_type, str(object_id))
    if key in pending:
        return
    pending[key] = AccessRecord(
        account_id=account.pk,
        capacity=capacity_of(account),
        kind=kind,
        ticket_id=int(ticket_id),
        object_type=object_type,
        object_id=str(object_id) if object_type else "",
        request_ref=getattr(request, "request_id", "") or "",
    )


def noted(request):
    """What the request has noted so far and not yet written (for tests and the middleware)."""
    return list(request.__dict__.get(PENDING, {}).values())


def write(request):
    """Write what the request noted, in one statement, and forget it. Raises what the database
    raises: the caller must not send what was read without its record."""
    pending = request.__dict__.pop(PENDING, None)
    if pending:
        AccessRecord.objects.bulk_create(pending.values())


class AccessRecordMiddleware:
    """Writes each request's access records once its view has answered (see the module's
    docstring). Placed inside RequestLogMiddleware, so a failed write is logged with the request's
    ID, and outside everything that draws or replaces an answer."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.status_code >= 500:
            request.__dict__.pop(PENDING, None)  # nothing was shown
            return response
        write(request)
        return response
