"""Outbound notices about requests (requirement N1, docs/product.md "Notifications").

desk.services calls the three hooks below inside its transaction, after its own writes. Nothing
is sent then: each hook adds to a batch held on the request instance, and the batch sends once
the transaction commits (`transaction.on_commit`), to the recipients still eligible at that
moment (checked again at dispatch), and a notice about a reply only while that reply is still
client-visible (desk.visibility). A rolled-back change sends nothing, two hooks of
one change (a reply that also sets "Waiting on client") send one notice per person, and a failed
send is logged and never undoes the change.

Who is told:

- A client-visible staff reply, or a change to Waiting on client or Resolved: the portal account
  that raised the request and the portal accounts linked to the request's contact, each only
  while it can see the request (desk.portal_scope).
- A request assigned to someone else: the new assignee.
- A client reply on an assigned request: the assignee.

Staff can turn their own notices off (preference `notifications.email`); a client account
cannot, because the notice is how it learns of an answer.

What a notice says is an allow-list: `notice_context` returns the request number, a fixed phrase
for the event, the laboratory's name and a sign-in link that leads to the request after signing
in. Nothing else of the request, its entries, organization, site, contact, category, team or
links reaches a template; tests render every template with sentinel values in all of them.
"""

import logging
import os
from urllib.parse import urlencode

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse

logger = logging.getLogger("lab_ops.notifications")

OPT_OUT_KEY = "notifications.email"  # core.preferences: False turns a staff member's notices off

# Fixed phrases, one per event. The only event-specific text a notice carries.
PHRASES = {
    "reply": "The laboratory has replied to your ticket.",
    "waiting_client": "The laboratory is waiting for information from you on your ticket.",
    "resolved": "The laboratory has resolved your ticket.",
    "assigned": "A ticket has been assigned to you.",
    "client_reply": "The client has replied on a ticket assigned to you.",
}
# When one change raises several client events, the client gets the most telling one.
CLIENT_PRECEDENCE = ("resolved", "waiting_client", "reply")
CLIENT_EVENTS = frozenset(CLIENT_PRECEDENCE)
STAFF_EVENTS = frozenset({"assigned", "client_reply"})
SUBJECT_TEMPLATE = "desk/email/notice_subject.txt"
BODY_TEMPLATE = "desk/email/notice_body.txt"


# Hooks (contract with desk.services) ---------------------------------------------------------


def entry_added(request, entry):
    """A reply, note, contact or event was added to `request`."""
    from .models import EntryKind
    from .visibility import is_client_visible

    if entry.kind != EntryKind.REPLY or not is_client_visible(entry):
        return  # notes, logged contacts and events reach nobody
    if (
        entry.recorded_capacity() != "client"
    ):  # written by the laboratory (desk.models.AuthorCapacity)
        for user in client_recipients(request):
            if user.pk != entry.author_id:
                _notify(request, user, "reply", entry=entry)
    elif request.assignee_id is not None and request.assignee_id != entry.author_id:
        if wants_notices(request.assignee) and _staff_can_read(request.assignee, request):
            _notify(request, request.assignee, "client_reply", entry=entry)


def status_changed(request, *, old, new, actor):
    """`request` moved from status `old` to `new` (desk.models.Status values)."""
    from .models import Status

    event = {Status.WAITING_CLIENT: "waiting_client", Status.RESOLVED: "resolved"}.get(new)
    if event is None or actor.client_id is not None:
        return
    for user in client_recipients(request):
        _notify(request, user, event)


def assigned(request, *, assignee, actor):
    """`request` was assigned to `assignee` (a staff account) by `actor`."""
    if assignee is None or assignee.pk == actor.pk:
        return
    if wants_notices(assignee) and _staff_can_read(assignee, request):
        _notify(request, assignee, "assigned")


# Recipients ----------------------------------------------------------------------------------


def client_recipients(request):
    """Active portal accounts to tell about a staff reply or status change: the raiser and the
    accounts linked to the request's contact, each only if it can see the request now."""
    from accounts.models import User

    from .portal_scope import portal_requests

    if request.client_id is None:
        return []
    candidates = User.objects.filter(client_id=request.client_id, is_active=True).exclude(email="")
    # The raiser counts only for a request it raised through the portal (desk.portal_scope).
    if request.channel == "portal":
        linked = candidates.filter(pk=request.created_by_id)
    else:
        linked = candidates.none()
    if request.contact_id is not None:
        linked = linked | candidates.filter(
            portal_access__contact_id=request.contact_id,
            portal_access__client_id=request.client_id,  # a grant for another organization: none
        )
    return [
        user
        for user in linked.distinct().order_by("pk")
        if portal_requests(user).filter(pk=request.pk).exists()
    ]


def wants_notices(user):
    """False when a staff member turned their own notices off."""
    from core.models import UserPreference

    return not UserPreference.objects.filter(user=user, key=OPT_OUT_KEY, value=False).exists()


def _staff_can_read(user, request):
    from django.core.exceptions import PermissionDenied

    from .selectors import staff_requests

    if not user.is_active or user.client_id is not None or not user.email:
        return False
    try:
        return staff_requests(user).filter(pk=request.pk).exists()
    except PermissionDenied:
        return False


# Content -------------------------------------------------------------------------------------


def base_url():
    """The deployment's public address, for links in notices: settings.PUBLIC_URL, else
    Render's RENDER_EXTERNAL_URL, else the first allowed host that is not local, over https."""
    configured = (getattr(settings, "PUBLIC_URL", "") or "").strip().rstrip("/")
    if configured:
        return configured
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if render_url:
        return render_url
    for host in settings.ALLOWED_HOSTS:
        host = host.strip().lstrip(".")
        if host and host not in {"localhost", "127.0.0.1", "testserver", "*"}:
            return f"https://{host}"
    return "http://localhost:8000"


def request_path(number, request_id, *, portal):
    """Where the request opens after signing in. The number is the only request data used."""
    if portal:
        return reverse("portal:request-detail", args=[number])
    return reverse("desk:request-detail", args=[request_id])


def sign_in_url(path):
    """The sign-in page, then `path`. No token: the content needs a signed-in account."""
    return f"{base_url()}{reverse('account_login')}?{urlencode({'next': path})}"


def lab_name():
    from core.models import Lab

    return Lab.objects.filter(pk=1).values_list("name", flat=True).first() or "The laboratory"


def notice_context(*, number, event, url, laboratory):
    """The whole template context of a notice: the allow-list, and nothing else."""
    return {
        "number": number,
        "event_phrase": PHRASES[event],
        "lab_name": laboratory,
        "sign_in_url": url,
    }


def render_notice(context):
    """(subject, body) of one notice."""
    subject = " ".join(render_to_string(SUBJECT_TEMPLATE, context).split())
    body = render_to_string(BODY_TEMPLATE, context)
    return subject, body


# Sending -------------------------------------------------------------------------------------


class _Batch:
    """Notices of one change, sent once it commits. Keyed by recipient; ids only."""

    def __init__(self, request):
        self.number = request.number
        self.request_id = request.pk
        self.recipients = {}  # user pk -> (is portal account, set of events); ids only
        self.entries = {}  # event about a reply -> ids of the replies it is about

    def add(self, user, event, entry=None):
        portal, events = self.recipients.setdefault(user.pk, (user.client_id is not None, set()))
        events.add(event)
        if entry is not None:  # a notice about a reply rests on that reply
            self.entries.setdefault(event, set()).add(entry.pk)

    def _entry_events_still_true(self):
        """Events about a reply whose reply is still client-visible now (not withdrawn since)."""
        from .models import RequestEntry
        from .visibility import client_visible

        return {
            event
            for event, ids in self.entries.items()
            if client_visible(RequestEntry.objects.filter(pk__in=ids)).exists()
        }

    def eligible(self):
        """Recipients still eligible now, at dispatch, with their current address: anything may
        have changed since the hook chose them (an account deactivated, access narrowed, the
        request reassigned or moved). Choosing again is the same rule as choosing first."""
        from accounts.models import User

        from .models import ServiceRequest

        request = ServiceRequest.objects.filter(pk=self.request_id).first()
        if request is None:
            return {}
        clients = {user.pk: user for user in client_recipients(request)}
        staff = User.objects.in_bulk(
            [pk for pk, (portal, _events) in self.recipients.items() if not portal]
        )
        stale = set(self.entries) - self._entry_events_still_true()
        chosen = {}
        for user_id, (portal, events) in self.recipients.items():
            events = events - stale  # e.g. a reply withdrawn before the notice went out
            if not events:
                continue
            if portal:
                user = clients.get(user_id)
            else:
                user = staff.get(user_id)
                still = (
                    user is not None
                    and request.assignee_id == user.pk
                    and wants_notices(user)
                    and _staff_can_read(user, request)
                )
                user = user if still else None
            if user is not None and user.email:
                chosen[user_id] = (user.email, portal, events)
        return chosen

    def flush(self):
        laboratory = lab_name()
        for user_id, (email, portal, events) in self.eligible().items():
            if portal:
                chosen = next(e for e in CLIENT_PRECEDENCE if e in events)
            else:
                chosen = "assigned" if "assigned" in events else "client_reply"
            context = notice_context(
                number=self.number,
                event=chosen,
                url=sign_in_url(request_path(self.number, self.request_id, portal=portal)),
                laboratory=laboratory,
            )
            subject, body = render_notice(context)
            try:
                EmailMessage(subject, body, to=[email]).send()
            except Exception as exc:  # a delivery problem never undoes the change
                logger.warning(
                    "Notice not sent: request=%s recipient=%s event=%s error=%s",
                    self.number,
                    user_id,
                    chosen,
                    type(exc).__name__,
                )


def _notify(request, user, event, entry=None):
    connection = transaction.get_connection()
    if not connection.in_atomic_block:  # services always run atomically; be safe regardless
        batch = _Batch(request)
        batch.add(user, event, entry)
        batch.flush()
        return
    _batch(request, connection).add(user, event, entry)


def _batch(request, connection):
    """This change's batch for `request`, registering its sending on commit the first time."""
    batch = getattr(request, "_pending_notices", None)
    registered = batch is not None and any(
        getattr(item[1], "__self__", None) is batch for item in connection.run_on_commit
    )
    if not registered:
        batch = _Batch(request)
        request._pending_notices = batch
        transaction.on_commit(batch.flush)
    return batch
