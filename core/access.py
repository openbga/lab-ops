"""Access records: a record of each sensitive read (core.models.AccessRecord).

Recorded: a staff or client portal account opening a ticket's page (also when the page is drawn
again after a refused reply, note or contact), a ticket's audit history or its Who opened it
page, the staff Form details page of a ticket or one of its complaint forms (Record decision,
Complaint record, Review: they show the complaint's rationale and findings); downloading a file;
being shown a patient-marked answer, whether a staff member reveals it or a portal account's
ticket page shows it; a staff member revealing a ticket's person-level key; a portal account
opening a new ticket with Request again, which copies answers from an earlier ticket; and each
ticket whose entries a client organization's timeline page shows (the start of their text). Not
recorded: rows shown in a list, a search or its text, the action pages that show only a ticket's
number (Assign, Status, Edit category and priority and the like), and a HEAD request, which
answers without the page.

Each record holds the account that made the request (as signed in when its view was called), the
capacity it read in (laboratory staff or client portal; an account's kind never changes), what was
read (the kind, the ticket and, for a file, an answer or a person-level link, which one), the
request ID (core.request_logging) and the time by the database's clock. Never what was shown: no
subject, text, file name, value or key, and nothing about the record is logged.

A view, or a template tag drawing part of a page, calls `note` once access has been decided and
what it shows has been read, so a refusal (404, 403) or a redirect to confirm it is you first
notes nothing. Two middlewares write the notes only with the answer they belong to:
AccessBindMiddleware, the innermost, moves what the view noted onto the answer the view returned,
and forgets it when the view raised instead (whatever answer the error then becomes);
AccessRecordMiddleware, outside the session middleware, writes in one statement the notes the
answer it is about to send still carries, after any reading the page was drawn in has ended (a page
drawn in core.concurrency.one_reading writes nothing inside it). An answer another layer put in the
view's place (the session ended in another tab, say, so Django answers 400 instead) carries no
notes and records nothing; so does an answer of 500 or more. When the write fails, the answer is
not sent: the request fails as a server error, so nothing is shown without its record. The record
is written after the page was drawn and before it is sent; a browser that drops the connection
meanwhile still leaves the record.

`keep` hands the same two middlewares a row of another append-only history table, written with the
answer on the same terms (the audit review workspace's record of its own use, audit_review).
"""

from django.db import transaction

from core.models import AccessCapacity, AccessKind, AccessRecord

PENDING = "_vr_access_pending"
# The account the request was made by, as signed in when its view was called (AccessBindMiddleware):
# every record of the request names it. A page that signs the browser out while it is drawn
# (request.user then turns anonymous) is still recorded for it. Not covered: code that signs
# another account in during the request (nothing in this release does) is not followed.
ACCOUNT = "_vr_access_account"
# The notes moved onto the answer the view returned (AccessBindMiddleware).
CARRIED = "_vr_access_records"
# What to do once the records an answer carries are written (after_written).
AFTER = "_vr_access_after"


def carries(response):
    """Whether `response` carries records still to be written before it is sent."""
    return bool(getattr(response, CARRIED, None))


def after_written(response, action):
    """Run `action` once the records `response` carries are written (AccessRecordMiddleware) and
    only if the answer's status, as it then stands, is below 400 (a successful answer or a
    redirect): never for a HEAD request, an answer of 500 or more, a write that fails, or a client
    error whose records were written. A list's sort saved for an answer (core.preferences) is then
    saved only for an answer that is sent and succeeded."""
    actions = getattr(response, AFTER, None)
    if actions is None:
        actions = []
        setattr(response, AFTER, actions)
    actions.append(action)


def capacity_of(account):
    """The capacity `account` reads in: a client portal account reads as the client. An account's
    kind never changes (clients migration 0006), so the account as read for the page decides."""
    return AccessCapacity.CLIENT if account.client_id is not None else AccessCapacity.LABORATORY


def note(request, kind, *, ticket_id, target=None, account=None):
    """Note that the request read `kind` of ticket `ticket_id` (for a file, an answer or a
    person-level link, `target`: the Attachment, FieldValue or LinkAttachment). The record names
    the account that made the request (ACCOUNT), or `account` where a caller without the
    middleware gives one. Written with the answer the view returns (see the module's docstring).
    The same read noted twice in one request is recorded once."""
    if kind not in AccessKind.values:
        raise ValueError(f"Unknown access kind {kind!r}.")
    # The account captured when the view was called wins; `account` only stands in for it where
    # no middleware captured one (no shipped view passes it).
    account = request.__dict__.get(ACCOUNT) or account or getattr(request, "user", None)
    if getattr(account, "pk", None) is None:
        return  # nobody was signed in: nothing of a ticket was drawn
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


def request_account(request):
    """The account the request was made by, as signed in when its view was called (ACCOUNT), or
    the request's user where no middleware captured one; None when nobody is signed in."""
    account = request.__dict__.get(ACCOUNT) or getattr(request, "user", None)
    return account if getattr(account, "pk", None) is not None else None


def keep(request, record):
    """Write `record`, an unsaved row of another append-only history table, with the answer the
    view returns, as the access records `note` adds are (see the module's docstring): bound to that
    answer, forgotten when the view raises, not written for a HEAD request or an answer of 500 or
    more, and written before the answer is sent, which fails if the write fails."""
    request.__dict__.setdefault(PENDING, {})[("kept", id(record))] = record


def noted(request):
    """What the request has noted so far and not yet bound to an answer (for tests)."""
    return list(request.__dict__.get(PENDING, {}).values())


def write(request, records=None):
    """Write `records` (by default what `request` has noted and kept and not yet bound to an
    answer): one statement for each table, in one transaction when there are several. Raises what
    the database raises: the caller must not send what was read without its record."""
    if records is None:
        records = list(request.__dict__.pop(PENDING, {}).values())
    by_model = {}
    for record in records:
        by_model.setdefault(type(record), []).append(record)
    if len(by_model) == 1:
        [(model, rows)] = by_model.items()
        model.objects.bulk_create(rows)
    elif by_model:
        with transaction.atomic():  # all of them or none
            for model, rows in by_model.items():
                model.objects.bulk_create(rows)


class AccessBindMiddleware:
    """The innermost middleware: binds what the view noted to the answer it returned, and forgets
    it when the view raised (process_exception runs before Django turns the exception into an
    error answer). Not covered: this release's views draw their pages at once; a deferred template
    response whose template-response hook or post-render callback (a deployment's own) replaces or
    rewrites the page binds the notes to the answer Django ends with, whatever it shows."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        pending = request.__dict__.pop(PENDING, None)
        if pending:
            setattr(response, CARRIED, list(pending.values()))
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            request.__dict__[ACCOUNT] = user
        return None

    def process_exception(self, request, exception):
        request.__dict__.pop(PENDING, None)


class AccessRecordMiddleware:
    """Writes the access records the answer about to be sent carries (see the module's
    docstring). Placed inside RequestLogMiddleware, so a failed write is logged with the request's
    ID, and outside the session middleware and everything else that can put another answer in the
    view's place."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        carried = getattr(response, CARRIED, None)
        if not carried:
            return response
        setattr(response, CARRIED, None)
        actions = getattr(response, AFTER, None) or []
        setattr(response, AFTER, None)
        if request.method == "HEAD" or response.status_code >= 500:
            return response  # nothing of the page is sent
        write(request, carried)
        if response.status_code < 400:  # as another layer may have left it
            for action in actions:
                action()
        return response
