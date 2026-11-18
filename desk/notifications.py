"""Outbound notices about requests (requirement N1, docs/product.md "Notifications").

desk.services calls the three hooks below inside its transaction, after its own writes. Each
hook adds to a batch held on the request instance, and the batch records one notice per person
in the outbox (jobs.outbox) in the same transaction: a rolled-back change records nothing, and
two hooks of one change (a reply that also sets "Waiting on client") make one notice per person.
Nothing is sent before commit. The outbox gives each notice its first attempt once the change
commits and the scheduled job retries one that could not be sent; a failed send is logged and
never undoes the change.

At every attempt the outbox asks `prepare_request_notice` whether the notice still applies, from
the request and the recipient as they are then (an account deactivated, access narrowed, the
request reassigned or moved), and a notice about a reply only while that reply is still
client-visible (desk.visibility). Choosing again is the same rule as choosing first.

Who is told:

- A client-visible staff reply, or a change to Waiting on client or Resolved: the portal account
  that raised the request and the portal accounts linked to the request's contact, each only
  while it can see the request (desk.portal_scope).
- A request assigned to someone else: the new assignee.
- A client reply on an assigned request: the assignee.
- A request past its first-response or resolution target (the scheduled job in
  service_levels.jobs): the assignee and the leads of the request's team, or, when it has neither,
  every member of the team (`breach_recipients`).

Staff can turn their own notices off (preference `notifications.email`); a client account
cannot, because the notice is how it learns of an answer.

What a notice says is an allow-list: `notice_context` returns the request number, a fixed phrase
for the event, the laboratory's name and a sign-in link that leads to the request after signing
in. Nothing else of the request, its entries, organization, site, contact, category, team or
links reaches a template; tests render every template with sentinel values in all of them.
"""

import os
from urllib.parse import urlencode

from django.conf import settings
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

OPT_OUT_KEY = "notifications.email"  # core.preferences: False turns a staff member's notices off
TOPIC = "desk.request"  # jobs.registry.NOTICE_TOPICS
BREACH_TOPIC = "desk.breach"

# Fixed phrases, one per event. The only event-specific text a notice carries.
PHRASES = {
    "reply": "The laboratory has replied to your ticket.",
    "waiting_client": "The laboratory is waiting for information from you on your ticket.",
    "resolved": "The laboratory has resolved your ticket.",
    "assigned": "A ticket has been assigned to you.",
    "client_reply": "The client has replied on a ticket assigned to you.",
    "first_response_breached": "A ticket has passed its first-response target.",
    "resolution_breached": "A ticket has passed its resolution target.",
}
# When one change raises several client events, the client gets the most telling one.
CLIENT_PRECEDENCE = ("resolved", "waiting_client", "reply")
CLIENT_EVENTS = frozenset(CLIENT_PRECEDENCE)
STAFF_EVENTS = frozenset({"assigned", "client_reply"})
# A breach notice's event, by the clock it is about (service_levels.models.RequestClock).
BREACH_EVENTS = {
    "first_response": "first_response_breached",
    "resolution": "resolution_breached",
}
# Why a person is told about a breach, kept on the notice and checked again at sending.
BREACH_ROLES = ("assignee", "lead", "member")
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


def prepared(*, user, number, request_id, event, portal):
    """The outbox's Prepared notice: `event`'s allow-listed words for `user`'s current address."""
    from jobs.outbox import Prepared

    context = notice_context(
        number=number,
        event=event,
        url=sign_in_url(request_path(number, request_id, portal=portal)),
        laboratory=lab_name(),
    )
    subject, body = render_notice(context)
    return Prepared(to=user.email, subject=subject, body=body, event=event)


# Recording -----------------------------------------------------------------------------------


class _Batch:
    """Notices of one change to one request, recorded in the outbox, one per recipient. Ids and
    codes only. `send` gives them their first attempt once the change commits."""

    def __init__(self, request):
        self.number = request.number
        self.request_id = request.pk
        self.notices = {}  # user pk -> jobs.models.Notice recorded in this change

    def add(self, user, event, entry=None):
        from jobs import outbox

        notice = self.notices.get(user.pk)
        if notice is None:
            notice = outbox.record(
                recipient=user,
                topic=TOPIC,
                object_id=self.request_id,
                reference=self.number,
                events=[event],
                # How the notice was addressed: a portal account is told only client events.
                data={"portal": user.client_id is not None, "entries": {}},
            )
            self.notices[user.pk] = notice
        elif event not in notice.events:
            notice.events = sorted({*notice.events, event})
        if entry is not None:  # a notice about a reply rests on that reply
            ids = notice.data["entries"].setdefault(event, [])
            if entry.pk not in ids:
                ids.append(entry.pk)
        notice.save(update_fields=["events", "data"])

    def send(self):
        from jobs import outbox

        outbox.send_now([notice.pk for notice in self.notices.values()])


def _notify(request, user, event, entry=None):
    connection = transaction.get_connection()
    if not connection.in_atomic_block:  # services always run atomically; be safe regardless
        with transaction.atomic():
            _batch(request, connection).add(user, event, entry)
        return
    _batch(request, connection).add(user, event, entry)


def _batch(request, connection):
    """This change's batch for `request`, registering its sending on commit the first time. A
    batch belongs to the transaction that registered it: once that commits or rolls back, its
    callback is gone and the next change on the same instance starts a new batch."""
    batch = getattr(request, "_pending_notices", None)
    registered = batch is not None and any(
        getattr(item[1], "__self__", None) is batch for item in connection.run_on_commit
    )
    if not registered:
        batch = _Batch(request)
        request._pending_notices = batch
        transaction.on_commit(batch.send)
    return batch


# Checked and worded at each sending attempt (jobs.outbox) --------------------------------------


def _replies_still_visible(entries):
    """Events about a reply whose reply is still client-visible now (not withdrawn since)."""
    from .models import RequestEntry
    from .visibility import client_visible

    return {
        event
        for event, ids in (entries or {}).items()
        if client_visible(RequestEntry.objects.filter(pk__in=ids)).exists()
    }


def prepare_request_notice(notice):
    """`notice` (topic desk.request) worded for sending, or None when it no longer applies."""
    from accounts.models import User

    from .models import ServiceRequest

    request = ServiceRequest.objects.filter(pk=notice.object_id).first()
    user = User.objects.filter(pk=notice.recipient_id).first()
    if request is None or user is None:
        return None
    entries = notice.data.get("entries") or {}
    stale = set(entries) - _replies_still_visible(entries)
    events = set(notice.events) - stale  # e.g. a reply withdrawn before the notice went out
    portal = bool(notice.data.get("portal"))
    if portal:
        events &= CLIENT_EVENTS
        if not events or user.pk not in {u.pk for u in client_recipients(request)}:
            return None
        event = next(e for e in CLIENT_PRECEDENCE if e in events)
    else:
        events &= STAFF_EVENTS
        if not (
            events
            and request.assignee_id == user.pk
            and wants_notices(user)
            and _staff_can_read(user, request)
        ):
            return None
        event = "assigned" if "assigned" in events else "client_reply"
    if not user.email:
        return None
    return prepared(
        user=user, number=request.number, request_id=request.pk, event=event, portal=portal
    )


# Service target breaches ---------------------------------------------------------------------


def breach_recipients(request):
    """[(staff account, role)] to tell that `request` passed a service target: the assignee and
    the leads of the request's team; when it has neither, every member of the team. Only active
    staff who can read the request and have not turned their notices off."""
    from accounts.models import User
    from teams.models import TeamMembership

    memberships = TeamMembership.objects.filter(team_id=request.team_id)
    lead_ids = set(memberships.filter(is_lead=True).values_list("user_id", flat=True))
    chosen = []
    if request.assignee_id is not None:
        chosen.append((request.assignee_id, "assignee"))
    chosen += [(pk, "lead") for pk in sorted(lead_ids - {request.assignee_id})]
    if not chosen:
        chosen = [(pk, "member") for pk in sorted(memberships.values_list("user_id", flat=True))]
    users = User.objects.in_bulk([pk for pk, _role in chosen])
    return [
        (users[pk], role)
        for pk, role in chosen
        if pk in users and wants_notices(users[pk]) and _staff_can_read(users[pk], request)
    ]


def _still_in_role(user, request, role):
    from teams.models import TeamMembership

    if role == "assignee":
        return request.assignee_id == user.pk
    members = TeamMembership.objects.filter(team_id=request.team_id, user=user)
    if role == "lead":
        return members.filter(is_lead=True).exists()
    return members.exists()


def breach_still_running(clock, which, now):
    """Whether `clock`'s `which` clock ("first_response" or "resolution") is past due and not met
    at `now` (a resolution clock paused while waiting is not running)."""
    if clock is None:
        return False
    due = getattr(clock, f"{which}_due_at")
    met = getattr(clock, f"{which}_met_at")
    paused = which == "resolution" and clock.paused_since is not None
    return due is not None and met is None and not paused and due <= now


def prepare_breach_notice(notice):
    """`notice` (topic desk.breach) worded for sending, or None when it no longer applies: the
    request was resolved or its clock met or paused since, or the person is no longer the
    assignee or in the team, cannot read the request, or turned notices off."""
    from accounts.models import User
    from service_levels.models import RequestClock

    from .models import ServiceRequest, Status

    request = ServiceRequest.objects.filter(pk=notice.object_id).first()
    user = User.objects.filter(pk=notice.recipient_id).first()
    which, role = notice.data.get("clock"), notice.data.get("role")
    if request is None or user is None or which not in BREACH_EVENTS or role not in BREACH_ROLES:
        return None
    if request.status in (Status.RESOLVED, Status.CLOSED):
        return None
    clock = RequestClock.objects.filter(request=request).first()
    if not breach_still_running(clock, which, timezone.now()):
        return None
    if not (
        user.email
        and _still_in_role(user, request, role)
        and wants_notices(user)
        and _staff_can_read(user, request)
    ):
        return None
    return prepared(
        user=user,
        number=request.number,
        request_id=request.pk,
        event=BREACH_EVENTS[which],
        portal=False,
    )
