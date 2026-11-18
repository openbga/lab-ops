"""Changes to requests. Each change locks the request, rechecks its revision, writes its timeline
entries and an attributed audit event in the same transaction, then calls the service-clock and
notification hooks (docs/architecture.md "Services and hooks").

Intake, entries, assignment, status, properties,
complaints, reply withdrawal and the bulk actions of the request list.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.utils import timezone

from accounts.roles import require_staff
from core.audit import record_event, snapshot
from service_levels import hooks as clock_hooks
from taxonomy import terms
from taxonomy.choices import Priority
from taxonomy.models import RequestCategory
from teams.models import Team, TeamMembership, ensure_default_team

from . import notifications
from .models import (
    NUMBER_PREFIX,
    AuthorCapacity,
    Channel,
    ComplaintDecision,
    ComplaintReview,
    ContactChannel,
    ContactDirection,
    ContactOutcome,
    EntryKind,
    RequestEntry,
    ServiceRequest,
    Status,
)
from .permissions import is_supervisor
from .portal_scope import check_portal_site, portal_requests
from .selectors import staff_requests

# Staff may set these with an entry; resolving and closing need their own service.
ENTRY_STATUSES = frozenset({Status.OPEN, Status.WAITING_CLIENT, Status.WAITING_THIRD_PARTY})
# A client reply sends the request back to the laboratory from these.
REOPENED_BY_CLIENT = frozenset({Status.WAITING_CLIENT, Status.RESOLVED})


def is_portal_account(user):
    return user.is_authenticated and user.client_id is not None


def _text(value, field, label, limit=None):
    value = (value or "").strip()
    if not value:
        raise ValidationError({field: f"{label} is required."})
    if limit and len(value) > limit:
        raise ValidationError({field: f"{label} takes at most {limit} characters."})
    return value


def _event(request, actor, action, **data):
    return RequestEntry.objects.create(
        request=request, kind=EntryKind.EVENT, author=actor, event_action=action, event_data=data
    )


def _check_category(category, *, portal, drawn_terms=None):
    """The category as it stands now (not as a form loaded it): an active category in an active
    group; for a portal account also one clients may raise. The write boundary for staff and
    portal intake and for recategorizing.

    `drawn_terms`, when given, is what the page told the person about the category's protection
    and routing (taxonomy.terms); the write is refused if the category no longer matches it. The
    comparison is made under the row lock, so a setup change either comes first and refuses the
    write or waits for it."""
    current = (
        RequestCategory.objects.select_for_update(of=("self",), no_key=True)
        .select_related("parent", "default_team")
        .filter(pk=category.pk)
        .first()
        if category.pk
        else None
    )
    if current is None or current.parent_id is None:
        raise ValidationError({"category": "Choose a category, not a group."})
    if not current.is_active or not current.parent.is_active:
        raise ValidationError({"category": "This category is no longer used."})
    if portal and not current.client_may_raise:
        raise ValidationError({"category": "Choose one of the categories offered to clients."})
    if drawn_terms is not None:
        terms.check(current, drawn_terms, portal=portal)
    return current


def _check_organization(client, site, contact):
    if client is not None and not client.is_active:
        raise ValidationError({"client": "This organization is inactive."})
    if site is not None and (client is None or site.client_id != client.pk):
        raise ValidationError({"site": "Choose a site of the ticket's organization."})
    if site is not None and not site.is_active:
        raise ValidationError({"site": "This site is inactive."})
    if contact is not None and (client is None or contact.client_id != client.pk):
        raise ValidationError({"contact": "Choose a contact of the ticket's organization."})
    if contact is not None and not contact.is_active:
        raise ValidationError({"contact": "This contact is inactive."})


def _current_team(team):
    """The team as it stands now, locked: an earlier copy's active flag is not trusted."""
    current = Team.objects.select_for_update(no_key=True).filter(pk=team.pk).first()
    if current is None or not current.is_active:
        raise ValidationError({"team": "This team is inactive."})
    return current


def _current_assignee(assignee, team):
    """The assignee as it stands now: an active staff account and an active member of `team`.

    Both rows are locked until the transaction ends, so an assignment and
    the end of that membership or account serialize: a removal (teams.services.remove_member) or
    deactivation that comes first makes this check fail, and one that comes after waits, then
    finds the new assignment and returns it to the queue (release_assignments)."""
    from accounts.models import User
    from jobs.actor import is_system_account

    current = User.objects.select_for_update(no_key=True).filter(pk=assignee.pk).first()
    if not (
        current is not None
        and not is_system_account(current)  # it does no work, even if someone gave it a role
        and current.is_active
        and current.client_id is None
        and TeamMembership.objects.select_for_update(no_key=True)
        .filter(team=team, user=current)
        .exists()
    ):
        raise ValidationError({"assignee": "Choose an active member of the ticket's team."})
    return current


def _team_and_assignee(team, assignee, category):
    """The team and assignee for a new request, from current state: the caller's team, else
    the category's current default team, else the default team."""
    team = _current_team(team or category.default_team or ensure_default_team())
    if assignee is not None:
        assignee = _current_assignee(assignee, team)
    return team, assignee


def _contact_fields(contact):
    """The four fields of a logged contact, validated."""
    contact = contact or {}
    fields = {
        "contact_direction": (contact.get("direction"), ContactDirection, "Direction"),
        "contact_channel": (contact.get("channel"), ContactChannel, "Channel"),
        "contact_outcome": (contact.get("outcome"), ContactOutcome, "Outcome"),
    }
    values = {}
    for name, (value, choices, label) in fields.items():
        if value not in choices.values:
            raise ValidationError({name: f"{label} is required."})
        values[name] = value
    values["contact_with"] = (contact.get("with_whom") or "").strip()[:160]
    return values


_CREATED = "_vr_created_requests"


def _transaction_id(conn):
    """PostgreSQL's id of the open top-level transaction: the same inside every savepoint of it,
    never reused (64-bit), and different on another connection."""
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_current_xact_id()::text")
        return cursor.fetchone()[0]


def _remember_created(request):
    """Record that `request` was created in the transaction now open on this thread's database
    connection. Things that may only be added with a new request (its opening entry's files) ask
    created_in_current_transaction."""
    conn = transaction.get_connection()
    current = _transaction_id(conn)
    created = {pk: xid for pk, xid in getattr(conn, _CREATED, {}).items() if xid == current}
    created[request.pk] = current
    setattr(conn, _CREATED, created)


def created_in_current_transaction(request):
    """Whether `request` was created by create_request in the transaction still open on this
    thread's connection, and still exists in it.

    The record lives on the thread's own connection, never the shared proxy, so another connection
    does not see it. It names PostgreSQL's transaction id, so it no longer matches once that
    transaction has ended, and the request row must still be there, so a creation undone by a
    rolled-back savepoint does not count."""
    conn = transaction.get_connection()
    if not conn.in_atomic_block:
        return False
    marked = getattr(conn, _CREATED, {}).get(request.pk)
    return (
        marked is not None
        and marked == _transaction_id(conn)
        and ServiceRequest.objects.filter(pk=request.pk).exists()
    )


@transaction.atomic
def create_request(
    *,
    actor,
    category,
    subject,
    first_entry_kind,
    first_entry_body,
    channel,
    client=None,
    site=None,
    contact=None,
    reported_by="",
    priority=None,
    team=None,
    assignee=None,
    client_reference="",
    follow_up_of=None,
    on_created=None,
    drawn_terms=None,
):
    """Create a request with its first entry and return it.

    `on_created(request)`, when given, runs in the same transaction after the request, its first
    entry and its audit event are written and before the clock and notification hooks: the seam
    for values another app keeps with the request (request-type fields). Whatever it raises undoes
    the whole creation.

    Staff: any staff role; any channel; the first entry is a reply or a note. A client portal
    account: the organization is its own, the channel is the portal, the first entry is a reply,
    the category must be one clients may raise, the site must be in its scope, priority may only
    be raised to High ("urgent"), and team and assignee are not its to choose.

    A page that draws categories passes `drawn_terms`, the taxonomy.terms value it carried: the
    creation is refused when the category's protection or routing differs from what it showed.
    """
    portal = is_portal_account(actor)
    if portal:
        if not actor.is_active:
            raise PermissionDenied("This account is inactive.")
        if client is not None and client.pk != actor.client_id:
            raise PermissionDenied("A client account raises tickets for its own organization.")
        if team is not None or assignee is not None:
            raise PermissionDenied("The laboratory routes client tickets.")
        client = actor.client
        channel, first_entry_kind = Channel.PORTAL, EntryKind.REPLY
        if priority not in (None, Priority.HIGH):
            raise ValidationError({"priority": "Mark the ticket urgent or leave it."})
        check_portal_site(actor, site)
    else:
        require_staff(actor)
        if channel not in Channel.values:
            raise ValidationError({"channel": "Choose how the ticket arrived."})
        if first_entry_kind not in (EntryKind.REPLY, EntryKind.NOTE):
            raise ValidationError({"first_entry_kind": "Choose a reply or a note."})
    # Routing, the default priority and the complaint flag come from the category as it stands
    # at write time, never from the caller's copy.
    category = _check_category(category, portal=portal, drawn_terms=drawn_terms)
    _check_organization(client, site, contact)
    team, assignee = _team_and_assignee(team, assignee, category)
    if priority is not None and priority not in Priority.values:
        raise ValidationError({"priority": "Choose a priority."})
    if follow_up_of is not None:
        # Scope first, so the checks below say nothing about a request the caller cannot read.
        scope = portal_requests(actor) if portal else staff_requests(actor)
        if not scope.filter(pk=follow_up_of.pk).exists():
            raise PermissionDenied("That ticket is not available to follow up.")
        if follow_up_of.status != Status.CLOSED:
            raise ValidationError({"follow_up_of": "Only a closed ticket takes a follow-up."})
        if follow_up_of.client_id != (client.pk if client else None):
            raise ValidationError({"follow_up_of": "A follow-up stays with the organization."})
    request = ServiceRequest.objects.create(
        client=client,
        site=site,
        contact=contact,
        reported_by="" if portal else (reported_by or "").strip()[:160],
        channel=channel,
        category=category,
        subject=_text(subject, "subject", "A subject", 120),
        priority=priority or category.default_priority,
        team=team,
        assignee=assignee,
        is_complaint=category.is_complaint,
        client_reference=(client_reference or "").strip()[:80],
        follow_up_of=follow_up_of,
        created_by=actor,
    )
    request.number = f"{NUMBER_PREFIX}{request.pk:06d}"
    request.save(update_fields=["number"])
    _remember_created(request)
    created = _event(
        request,
        actor,
        "created",
        channel=channel,
        team_id=team.pk,
        assignee_id=assignee.pk if assignee else None,
    )
    first = RequestEntry.objects.create(
        request=request,
        kind=first_entry_kind,
        author=actor,
        author_capacity=AuthorCapacity.CLIENT if portal else AuthorCapacity.LABORATORY,
        body=_text(first_entry_body, "first_entry_body", "The ticket's text"),
    )
    record_event(
        actor=actor,
        action="request.created",
        obj=request,
        after={
            "number": request.number,
            "channel": channel,
            "category": category.key,
            "priority": request.priority,
            "team_id": team.pk,
            "assignee_id": assignee.pk if assignee else None,
            "client_id": request.client_id,
            "site_id": request.site_id,
            "contact_id": request.contact_id,
            "is_complaint": request.is_complaint,
            "follow_up_of_id": request.follow_up_of_id,
        },
    )
    record_event(actor=actor, action="entry.added", obj=first, after={"kind": first.kind})
    if on_created is not None:
        on_created(request)
    clock_hooks.request_created(request)
    for entry in (created, first):
        clock_hooks.entry_added(request, entry)
        notifications.entry_added(request, entry)
    if assignee is not None and assignee != actor:
        notifications.assigned(request, assignee=assignee, actor=actor)
    return request


def _locked_request(actor, request_id, *, portal):
    scope = portal_requests(actor) if portal else staff_requests(actor)
    try:
        return scope.select_for_update(of=("self",)).get(pk=request_id)
    except ServiceRequest.DoesNotExist:
        raise ServiceRequest.DoesNotExist("No such ticket.") from None


def _set_status(request, actor, new):
    old = request.status
    if new == old:
        return None
    request.status = new
    if old == Status.RESOLVED:
        request.resolved_at = None
    _event(request, actor, "status_changed", old=old, new=new)
    record_event(
        actor=actor,
        action="request.status_changed",
        obj=request,
        before={"status": old},
        after={"status": new},
    )
    return old


@transaction.atomic
def add_entry(*, actor, request, kind, body, expected_revision, contact=None, new_status=None):
    """Add a reply, note or logged contact to `request` and return the entry.

    `contact` (kind "contact") holds direction, channel, with_whom and outcome. Staff may set
    `new_status` to Open or one of the waiting statuses in the same change. A client portal
    account always adds a reply; it reopens a request that waits on the client or is resolved,
    and cannot write on a closed one. Staff cannot reply or log a contact on a closed request.
    """
    portal = is_portal_account(actor)
    if not portal:
        require_staff(actor)
    request = _locked_request(actor, request.pk, portal=portal)
    if request.revision != expected_revision:
        raise ValidationError("This ticket changed. Reload it and review the latest entries.")
    fields = {}
    if portal:
        if kind != EntryKind.REPLY or new_status is not None or contact is not None:
            raise PermissionDenied("A client account can only reply.")
        if request.status == Status.CLOSED:
            raise ValidationError("This ticket is closed. Open a follow-up ticket instead.")
        new_status = Status.OPEN if request.status in REOPENED_BY_CLIENT else None
    else:
        if kind not in (EntryKind.REPLY, EntryKind.NOTE, EntryKind.CONTACT):
            raise ValidationError({"kind": "Choose a reply, a note or a logged contact."})
        if request.status == Status.CLOSED and kind != EntryKind.NOTE:
            raise ValidationError("This ticket is closed. Only internal notes can be added.")
        if request.status == Status.CLOSED and new_status is not None:
            raise ValidationError(
                {"new_status": "A closed ticket keeps its status; add the note without one."}
            )
        if new_status is not None and new_status not in ENTRY_STATUSES:
            raise ValidationError({"new_status": "Resolve or close the ticket on its own."})
        if new_status is not None and request.status == Status.RESOLVED:
            raise ValidationError(
                {"new_status": "Reopen a resolved ticket with Status, giving the reason."}
            )
        if kind == EntryKind.CONTACT:
            fields = _contact_fields(contact)
        elif contact is not None:
            raise ValidationError({"kind": "Contact details belong to a logged contact."})
    entry = RequestEntry.objects.create(
        request=request,
        kind=kind,
        author=actor,
        author_capacity=AuthorCapacity.CLIENT if portal else AuthorCapacity.LABORATORY,
        body=_text(body, "body", "The text"),
        **fields,
    )
    record_event(actor=actor, action="entry.added", obj=entry, after={"kind": kind})
    old_status = _set_status(request, actor, new_status) if new_status else None
    request.revision += 1
    request.save()
    clock_hooks.entry_added(request, entry)
    notifications.entry_added(request, entry)
    if old_status is not None:
        clock_hooks.request_changed(request, previous={"status": old_status})
        notifications.status_changed(request, old=old_status, new=request.status, actor=actor)
    return entry


def _reason(reason, required=True):
    reason = (reason or "").strip()
    if required and not reason:
        raise ValidationError({"reason": "A reason is required."})
    return reason


def _staff_request(actor, request, expected_revision):
    """The request locked for a staff change, after the revision check."""
    require_staff(actor)
    locked = _locked_request(actor, request.pk, portal=False)
    if expected_revision is None or locked.revision != expected_revision:
        raise ValidationError("This ticket changed. Reload it and review the latest entries.")
    return locked


def _save(request):
    request.revision += 1
    request.save()


def _is_member(user, team):
    return TeamMembership.objects.filter(team=team, user=user, user__is_active=True).exists()


def _assign(request, actor, team, assignee, reason):
    """Move `request` to `team` and `assignee` (None to leave it with the team). Returns whether
    anything changed. Called with the request locked."""
    if request.status == Status.CLOSED:
        raise ValidationError("A closed ticket cannot be reassigned.")
    team = _current_team(team)
    if assignee is not None:
        assignee = _current_assignee(assignee, team)
    old_team_id, old_assignee_id = request.team_id, request.assignee_id
    if team.pk == old_team_id and (assignee.pk if assignee else None) == old_assignee_id:
        return False
    request.team, request.assignee = team, assignee
    _event(
        request,
        actor,
        "assigned",
        team_id=team.pk,
        assignee_id=assignee.pk if assignee else None,
        old_team_id=old_team_id,
        old_assignee_id=old_assignee_id,
    )
    record_event(
        actor=actor,
        action="request.assigned",
        obj=request,
        before={"team_id": old_team_id, "assignee_id": old_assignee_id},
        after={"team_id": team.pk, "assignee_id": assignee.pk if assignee else None},
        reason=reason,
    )
    if assignee is not None and assignee.pk != old_assignee_id and assignee != actor:
        notifications.assigned(request, assignee=assignee, actor=actor)
    return True


@transaction.atomic
def assign(*, actor, request, expected_revision, team, assignee=None, reason=""):
    """Give the request to `team` and optionally to one of its active members."""
    request = _staff_request(actor, request, expected_revision)
    if not _assign(request, actor, team, assignee, _reason(reason, required=False)):
        raise ValidationError("Choose another team or person.")
    _save(request)
    return request


@transaction.atomic
def take(*, actor, request, expected_revision):
    """Assign the request to the acting staff member, who must belong to its team."""
    request = _staff_request(actor, request, expected_revision)
    if not _is_member(actor, request.team):
        raise ValidationError(
            f"Only members of {request.team} can take it. Move it to one of your teams first."
        )
    if not _assign(request, actor, request.team, actor, ""):
        raise ValidationError("This ticket is already yours.")
    _save(request)
    return request


def _change_status(request, actor, new, *, summary="", reason="", event_data=None):
    """Move a locked request to status `new` under the life-cycle rules; returns the old one.
    `event_data` (ids and codes only) is added to the timeline event."""
    old = request.status
    if new not in Status.values:
        raise ValidationError({"status": "Choose a status."})
    if new == old:
        raise ValidationError({"status": "The ticket already has this status."})
    if old == Status.CLOSED:
        raise ValidationError("A closed ticket cannot change. Open a new ticket instead.")
    if new == Status.CLOSED:
        if old != Status.RESOLVED:
            raise ValidationError({"status": "Resolve the ticket before closing it."})
        review = getattr(request, "complaint_review", None)
        if request.is_complaint and not (review and review.is_reviewed):
            raise ValidationError("This complaint closes only after a supervisor has reviewed it.")
        if request.is_complaint and not review_counts(request, review):
            raise ValidationError(
                "The complaint's reviewer has been assigned this ticket or has recorded its "
                "decision, so the review does not count. A new review by another supervisor is "
                "needed before closing."
            )
    if old == Status.RESOLVED and new != Status.CLOSED:
        reason = _reason(reason)  # a reopen
    if new == Status.RESOLVED:
        request.resolution_summary = _text(summary, "summary", "A resolution summary")
        request.resolved_at = timezone.now()
    elif new == Status.CLOSED:
        request.closed_at = timezone.now()
    else:
        request.resolved_at = None
    request.status = new
    _event(request, actor, "status_changed", **(event_data or {}), old=old, new=new)
    record_event(
        actor=actor,
        action="request.status_changed",
        obj=request,
        before={"status": old},
        after={"status": new},
        reason=(reason or "").strip(),
    )
    return old


def _after_status(request, actor, old):
    clock_hooks.request_changed(request, previous={"status": old})
    notifications.status_changed(request, old=old, new=request.status, actor=actor)


@transaction.atomic
def change_status(*, actor, request, expected_revision, status, summary="", reason=""):
    """Set the status. Resolved needs a summary; reopening a resolved request needs a reason;
    Closed only follows Resolved, and a complaint closes only after its review."""
    request = _staff_request(actor, request, expected_revision)
    old = _change_status(request, actor, status, summary=summary, reason=reason)
    _save(request)
    _after_status(request, actor, old)
    return request


@transaction.atomic
def update_properties(*, actor, request, expected_revision, priority, category, reason=""):
    """Change priority and category. A complaint category makes the request a complaint; moving
    out of one never unmarks it (a supervisor does that)."""
    request = _staff_request(actor, request, expected_revision)
    if request.status == Status.CLOSED:
        raise ValidationError("A closed ticket cannot change.")
    if priority not in Priority.values:
        raise ValidationError({"priority": "Choose a priority."})
    previous = {}
    if priority != request.priority:
        previous["priority"] = request.priority
        request.priority = priority
    if category.pk != request.category_id:
        category = _check_category(category, portal=False)  # as it stands now, not the copy
        previous["category_id"] = request.category_id
        request.category = category
        if category.is_complaint and not request.is_complaint:
            request.is_complaint = True
            _event(request, actor, "complaint_marked", by_category=True)
    if not previous:
        raise ValidationError("Change the priority or the category before saving.")
    after = {"priority": request.priority, "category_id": request.category_id}
    _event(request, actor, "updated", fields=sorted(previous), **after)
    record_event(
        actor=actor,
        action="request.updated",
        obj=request,
        before=previous,
        after={name: after[name] for name in previous},
        reason=_reason(reason, required=False),
    )
    _save(request)
    clock_hooks.request_changed(request, previous=previous)
    return request


@transaction.atomic
def set_complaint(*, actor, request, expected_revision, is_complaint, reason):
    """Mark a request as a complaint (any staff role) or unmark it (supervisor only)."""
    request = _staff_request(actor, request, expected_revision)
    reason = _reason(reason)
    if bool(is_complaint) == request.is_complaint:
        raise ValidationError("Nothing to change.")
    if not is_complaint and not is_supervisor(actor):
        raise PermissionDenied("Only a supervisor can unmark a complaint.")
    if request.status == Status.CLOSED:
        raise ValidationError("A closed ticket cannot change.")
    request.is_complaint = bool(is_complaint)
    action = "complaint_marked" if is_complaint else "complaint_unmarked"
    _event(request, actor, action)
    record_event(
        actor=actor,
        action=f"request.{action}",
        obj=request,
        before={"is_complaint": not request.is_complaint},
        after={"is_complaint": request.is_complaint},
        reason=reason,
    )
    _save(request)
    return request


def former_assignee_ids(request):
    """Everyone who has been the request's assignee, from its timeline (creation and every
    assignment), plus the current one."""
    ids = {request.assignee_id} if request.assignee_id else set()
    for data in request.entries.filter(
        kind=EntryKind.EVENT, event_action__in=("created", "assigned")
    ).values_list("event_data", flat=True):
        if data.get("assignee_id"):
            ids.add(data["assignee_id"])
    return ids


def decision_author_ids(request, review=None):
    """Everyone who has recorded or revised the complaint's investigation decision, from the
    timeline (every complaint_decided event), plus the stored author."""
    ids = set(
        request.entries.filter(kind=EntryKind.EVENT, event_action="complaint_decided").values_list(
            "author_id", flat=True
        )
    )
    if review is not None and review.decided_by_id:
        ids.add(review.decided_by_id)
    return ids


def review_counts(request, review):
    """Whether a recorded review still makes the complaint closable: its reviewer must never
    have been the request's assignee, also after the review, nor
    have recorded or revised the decision, also in a review stored before that rule."""
    return bool(
        review is not None
        and review.is_reviewed
        and review.reviewed_by_id not in former_assignee_ids(request)
        and review.reviewed_by_id not in decision_author_ids(request, review)
    )


def review_refusal(user, request, review):
    """Why `user` may not review this complaint now, or "" when they may. Independence is per
    account: anyone who ever recorded or revised the decision, and anyone who has ever been the
    request's assignee, is refused."""
    if not is_supervisor(user):
        return "Only a supervisor reviews a complaint."
    if review is None or not review.decision:
        return "Record the investigation decision first."
    if review_counts(request, review):
        return "This complaint has been reviewed."
    if user.pk in decision_author_ids(request, review):
        return "You recorded the decision, so another supervisor must review it."
    if user.pk in former_assignee_ids(request):
        return "You have been assigned this ticket, so another supervisor must review it."
    return ""


def _complaint_record(request, actor):
    if not request.is_complaint:
        raise ValidationError("This ticket is not a complaint.")
    if not (actor.pk == request.assignee_id or is_supervisor(actor)):
        raise PermissionDenied("The assignee or a supervisor records the complaint.")
    review, _ = ComplaintReview.objects.select_for_update().get_or_create(request=request)
    if review_counts(request, review):
        raise ValidationError("This complaint has been reviewed; its record no longer changes.")
    return review


@transaction.atomic
def decide_complaint(*, actor, request, expected_revision, decision, rationale):
    """Record (or revise, before the review) the investigation decision and its rationale."""
    request = _staff_request(actor, request, expected_revision)
    review = _complaint_record(request, actor)
    if decision not in ComplaintDecision.values:
        raise ValidationError({"decision": "Choose whether to investigate."})
    before = snapshot(review)
    review.decision = decision
    review.rationale = _text(rationale, "rationale", "The rationale")
    review.decided_by, review.decided_at = actor, timezone.now()
    review.revision += 1
    review.save()
    _event(request, actor, "complaint_decided", decision=decision)
    record_event(
        actor=actor, action="complaint.decided", obj=review, before=before, after=snapshot(review)
    )
    _save(request)
    return review


COMPLAINT_FIELDS = (
    "findings",
    "corrective_action_reference",
    "outcome_communicated_on",
    "outcome_communicated_how",
)


@transaction.atomic
def update_complaint(*, actor, request, expected_revision, **data):
    """Record findings and actions, the corrective action reference and how the outcome was
    communicated."""
    request = _staff_request(actor, request, expected_revision)
    review = _complaint_record(request, actor)
    before = snapshot(review)
    for name in COMPLAINT_FIELDS:
        if name in data:
            value = data[name]
            setattr(review, name, value.strip() if isinstance(value, str) else value)
    if snapshot(review) == before:
        raise ValidationError("Change at least one detail before saving.")
    review.revision += 1
    review.save()
    _event(request, actor, "complaint_updated")
    record_event(
        actor=actor, action="complaint.updated", obj=review, before=before, after=snapshot(review)
    )
    _save(request)
    return review


@transaction.atomic
def review_complaint(*, actor, request, expected_revision, note):
    """Approve the complaint record as an independent reviewer: a supervisor who did not record
    the decision and has never been the request's assignee."""
    request = _staff_request(actor, request, expected_revision)
    if not request.is_complaint:
        raise ValidationError("This ticket is not a complaint.")
    review = ComplaintReview.objects.select_for_update().filter(request=request).first()
    if refusal := review_refusal(actor, request, review):
        raise PermissionDenied(refusal)
    previous_reviewer = review.reviewed_by_id  # set when an earlier review no longer counts
    review.review_note = _text(note, "note", "A review note")
    review.reviewed_by, review.reviewed_at = actor, timezone.now()
    review.revision += 1
    review.save()
    _event(request, actor, "complaint_reviewed")
    record_event(
        actor=actor,
        action="complaint.reviewed",
        obj=review,
        before={"reviewed_by": previous_reviewer} if previous_reviewer else None,
        after={"reviewed_by": actor.pk, "decision": review.decision},
    )
    _save(request)
    return review


@transaction.atomic
def withdraw_reply(*, actor, entry, reason, expected_revision):
    """Withdraw a reply from client view (supervisor, with a reason). Staff still see it.
    `expected_revision` is the request's revision the supervisor saw; a later change refuses."""
    if not is_supervisor(actor):
        raise PermissionDenied("Only a supervisor can withdraw a reply.")
    request = _staff_request(actor, entry.request, expected_revision)
    entry = RequestEntry.objects.select_for_update().get(pk=entry.pk, request=request)
    if entry.kind != EntryKind.REPLY:
        raise ValidationError("Only a reply can be withdrawn from client view.")
    if entry.withdrawn_at is not None:
        raise ValidationError("This reply has already been withdrawn.")
    entry.withdrawn_at, entry.withdrawn_by = timezone.now(), actor
    entry.withdrawal_reason = _reason(reason)
    entry.save(update_fields=["withdrawn_at", "withdrawn_by", "withdrawal_reason"])
    _event(request, actor, "reply_withdrawn", entry_id=entry.pk)
    record_event(
        actor=actor,
        action="entry.withdrawn",
        obj=entry,
        after={"withdrawn_by": actor.pk},
        reason=entry.withdrawal_reason,
    )
    _save(request)
    return entry


def _bulk_requests(actor, selected):
    """The selected requests, locked, in id order. `selected` maps each request id to the
    revision the operator saw in the list. The whole action is refused if any request is outside
    what the actor may read (a hidden or missing id is never acted on silently), if a revision is
    missing, or if a request changed since the list was opened (named in the refusal)."""
    require_staff(actor)
    try:
        wanted = {int(pk): int(revision) for pk, revision in dict(selected).items()}
    except TypeError, ValueError:
        raise ValidationError("This list is out of date. Reload it and select again.") from None
    if not wanted:
        raise ValidationError("Select at least one ticket.")
    found = list(
        staff_requests(actor).filter(pk__in=wanted).select_for_update(of=("self",)).order_by("pk")
    )
    if len(found) != len(wanted):
        raise PermissionDenied("Some selected tickets are not available to you.")
    changed = [request.number for request in found if request.revision != wanted[request.pk]]
    if changed:
        raise ValidationError(
            f"Changed since you opened the list: {', '.join(changed)}. Reload and select again."
        )
    return found


@transaction.atomic
def bulk_assign(*, actor, selected, team, assignee=None, reason=""):
    """Assign several requests at once; each is audited. Requests already there are skipped."""
    changed = 0
    for request in _bulk_requests(actor, selected):
        if _assign(request, actor, team, assignee, _reason(reason, required=False)):
            _save(request)
            changed += 1
    return changed


BULK_STATUSES = frozenset({Status.OPEN, Status.WAITING_CLIENT, Status.WAITING_THIRD_PARTY})


@transaction.atomic
def bulk_status(*, actor, selected, status):
    """Set Open or a waiting status on several requests that are not resolved or closed."""
    if status not in BULK_STATUSES:
        raise ValidationError({"status": "Resolve and close tickets one at a time or with Close."})
    requests = _bulk_requests(actor, selected)
    blocked = [r.number for r in requests if r.status in (Status.RESOLVED, Status.CLOSED)]
    if blocked:
        raise ValidationError(
            f"Resolved or closed tickets change status one at a time: {', '.join(blocked)}."
        )
    changed = 0
    for request in requests:
        if request.status != status:
            old = _change_status(request, actor, status)
            _save(request)
            _after_status(request, actor, old)
            changed += 1
    return changed


@transaction.atomic
def bulk_close(*, actor, selected):
    """Close several resolved requests. All or nothing: a request that is not resolved, or a
    complaint without its review, stops the whole action."""
    requests = _bulk_requests(actor, selected)
    for request in requests:
        try:
            old = _change_status(request, actor, Status.CLOSED)
        except ValidationError as exc:
            raise ValidationError(f"{request.number}: {' '.join(exc.messages)}") from exc
        _save(request)
        _after_status(request, actor, old)
    return len(requests)


def open_assignments(user, team=None):
    """Requests not closed that are assigned to `user` (in `team` when given): what
    release_assignments would return to the team queue."""
    queryset = ServiceRequest.objects.filter(assignee=user).exclude(status=Status.CLOSED)
    return queryset.filter(team=team) if team is not None else queryset


def release_assignments(*, actor, user, team=None, reason):
    """Return `user`'s requests that are not closed (in `team`, or every team) to their team's
    queue, with a timeline event and an audit event on each. Called inside the transaction that
    removes the membership or deactivates the account, so nobody
    stays the assignee of a team they left. Returns how many requests were released."""
    if not connection.in_atomic_block:
        raise RuntimeError("Release assignments in the transaction that ends the membership.")
    released = 0
    for request in open_assignments(user, team).select_for_update(of=("self",)).order_by("pk"):
        _event(
            request,
            actor,
            "assigned",
            team_id=request.team_id,
            assignee_id=None,
            old_team_id=request.team_id,
            old_assignee_id=user.pk,
        )
        record_event(
            actor=actor,
            action="request.assigned",
            obj=request,
            before={"team_id": request.team_id, "assignee_id": user.pk},
            after={"team_id": request.team_id, "assignee_id": None},
            reason=reason,
        )
        request.assignee = None
        _save(request)
        released += 1
    return released


def close_resolved_automatically(*, actor, request_id, now=None):
    """The scheduled job's close of one resolved request (desk.jobs.close_resolved_tickets).

    Decided from one reading taken under the locks of the change: the laboratory's period
    (jobs.models.JobSettings, locked so a change to it waits for this close and the close sees
    the period in force) and the request (locked, skipped when someone else holds it). The
    request closes only if it is still resolved and was resolved at least the period ago, under
    the same rules as closing by hand: a complaint closes only after its review counts.

    `actor` is the scheduled jobs' account (jobs.actor) and nothing else: the job acts for the
    laboratory, is started only from the server (manage.py run_scheduled_jobs) and reads every
    request, so no person's scope applies. Returns "closed", "not_due" or "busy"; raises
    ValidationError when a rule keeps the request open (a complaint awaiting its review).
    """
    from datetime import timedelta

    from jobs.actor import is_system_account
    from jobs.models import JobSettings

    if not connection.in_atomic_block:
        raise RuntimeError("Close a resolved ticket inside the job's transaction.")
    if not is_system_account(actor):
        raise PermissionDenied("Only the scheduled jobs close tickets automatically.")
    now = now or timezone.now()
    settings_row = JobSettings.objects.select_for_update(no_key=True).filter(pk=1).first()
    days = settings_row.close_resolved_after_days if settings_row else None
    if not days:
        return "not_due"
    request = (
        ServiceRequest.objects.select_for_update(of=("self",), skip_locked=True)
        .filter(pk=request_id)
        .first()
    )
    if request is None:
        return "busy"
    if request.status != Status.RESOLVED or request.resolved_at > now - timedelta(days=days):
        return "not_due"
    old = _change_status(
        request,
        actor,
        Status.CLOSED,
        reason=f"Closed automatically {days} day{'' if days == 1 else 's'} after it was resolved.",
        event_data={"automatic": True, "after_days": days},
    )
    _save(request)
    _after_status(request, actor, old)
    return "closed"
