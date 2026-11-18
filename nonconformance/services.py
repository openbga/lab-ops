"""Changes to nonconformances, their actions and their linked tickets.

Every change runs in one transaction: it holds the module's switch (core.modules.lock_enabled),
locks the record, compares the revision the page showed, decides who may make the change from
the record and the accounts as they stand under that lock, writes the change and its audit event
(values before and after) and advances the record's revision. A change to an action or a linked
ticket advances the record's revision too, so one revision guards everything its page shows.
A ticket the record is opened from or linked to gets an event on its timeline and a new revision
of its own.

Who does what (docs/product.md "Nonconformance and corrective action"): every staff role raises
a record and reads every record. The owner or a supervisor works it. A supervisor chooses the
owner, and a supervisor who is not the owner closes it or reopens it with a reason. An action's
assignee may also mark it done.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from django.utils import timezone

from accounts.roles import effective_roles, require_staff
from core.audit import record_event, snapshot
from core.modules import lock_enabled
from desk import services as desk_services
from desk.models import ComplaintReview
from desk.permissions import is_supervisor
from desk.selectors import staff_requests

from .models import (
    ACTIVE_STATUSES,
    CLOSABLE_RESULTS,
    NUMBER_PREFIX,
    ActionStatus,
    ClientNotice,
    CorrectiveAction,
    Effectiveness,
    Impact,
    Nonconformance,
    Severity,
    Source,
    Status,
    TicketLink,
)

MODULE = "nonconformance"
STALE = "This nonconformance changed after you opened it. Reload it and review the latest details."
CLOSED = "A closed nonconformance does not change. A supervisor can reopen it."
TEXT_LIMIT = 4000


# Who may do what, from the record as the caller holds it. Services call these on the locked row.


def can_work(user, nc):
    """The owner or a supervisor works an open record: details, investigation, impact, status,
    actions and linked tickets."""
    return bool(nc.owner_id and user.pk == nc.owner_id) or is_supervisor(user)


def close_refusal(user, nc, open_actions):
    """Why `user` may not close `nc` now, or "" when they may. `open_actions` is the number of
    its actions still open, counted from the same reading as `nc`."""
    if nc.status == Status.CLOSED:
        return "This nonconformance is closed."
    if not is_supervisor(user):
        return "A supervisor closes a nonconformance."
    if nc.owner_id is None:
        return "Choose its owner first."
    if user.pk == nc.owner_id:
        return "You own this nonconformance, so another supervisor closes it."
    if nc.status != Status.VERIFICATION:
        return "Move it to Verification first."
    if not nc.impact:
        return "Record the decision on reported results first."
    if nc.impact == Impact.AFFECTED and not nc.client_notice:
        return "Record whether the client was told first."
    if open_actions:
        return "Finish or cancel its open actions first."
    if nc.effectiveness not in CLOSABLE_RESULTS:
        return "Record an effectiveness check that allows closing first."
    return ""


def reopen_refusal(user, nc):
    if nc.status != Status.CLOSED:
        return "This nonconformance is not closed."
    if not is_supervisor(user):
        return "A supervisor reopens a nonconformance."
    return ""


def can_finish_action(user, nc, action):
    return action.assignee_id == user.pk or can_work(user, nc)


# Helpers


def _text(value, field, label, limit=TEXT_LIMIT, required=True):
    value = (value or "").strip()
    if required and not value:
        raise ValidationError({field: f"{label} is required."})
    if len(value) > limit:
        raise ValidationError({field: f"{label} takes at most {limit} characters."})
    return value


def _choice(value, choices, field, message):
    if value not in choices.values:
        raise ValidationError({field: message})
    return value


def _reason(reason, required=True):
    return _text(reason, "reason", "A reason", required=required)


def _staff_account(user, field, label):
    """`user` as it stands now, locked: an active staff account with a staff role."""
    from accounts.models import User

    current = (
        User.objects.select_for_update(no_key=True)
        .filter(pk=getattr(user, "pk", None), is_active=True, client__isnull=True)
        .first()
    )
    if current is None or not effective_roles(current):
        raise ValidationError({field: f"Choose an active staff member as {label}."})
    return current


def _lock(actor, nc, expected_revision):
    """The record locked for a change, after the module and revision checks."""
    require_staff(actor)
    lock_enabled(MODULE)
    locked = Nonconformance.objects.select_for_update().filter(pk=nc.pk).first()
    if locked is None:
        raise Http404("No such nonconformance.")
    if expected_revision is None or locked.revision != expected_revision:
        raise ValidationError(STALE)
    return locked


def _work(actor, nc, expected_revision):
    """The record locked for the owner's or a supervisor's change to an open record."""
    locked = _lock(actor, nc, expected_revision)
    if locked.status == Status.CLOSED:
        raise ValidationError(CLOSED)
    if not can_work(actor, locked):
        raise PermissionDenied("The owner or a supervisor works this nonconformance.")
    return locked


def _save(nc):
    nc.revision += 1
    nc.save()


def _audit(actor, action, obj, before, reason=""):
    record_event(
        actor=actor, action=action, obj=obj, before=before, after=snapshot(obj), reason=reason
    )


def _locked_ticket(actor, request_id):
    """The ticket locked, if `actor` may read it now; 404 otherwise, saying nothing about it."""
    ticket = staff_requests(actor).select_for_update(of=("self",)).filter(pk=request_id).first()
    if ticket is None:
        raise Http404("No such ticket.")
    return ticket


def _ticket_event(actor, ticket, action, nc):
    desk_services.record_related_event(
        actor=actor,
        request=ticket,
        action=action,
        nonconformance_id=nc.pk,
        number=nc.number,
    )


def _link(actor, nc, ticket):
    link = TicketLink(nonconformance=nc, request=ticket, added_by=actor)
    try:
        with transaction.atomic():
            link.save()
    except IntegrityError as error:
        raise ValidationError({"ticket": "This ticket is already linked."}) from error
    record_event(
        actor=actor,
        action="nonconformance.ticket_linked",
        obj=nc,
        after={"ticket_link_id": link.pk, "request_id": ticket.pk},
    )
    return link


# Raising a record


def _details(title, description, source, severity, area):
    return {
        "title": _text(title, "title", "A title", 160),
        "description": _text(description, "description", "What happened"),
        "source": _choice(source, Source, "source", "Choose where it came from."),
        "severity": _choice(severity, Severity, "severity", "Choose a severity."),
        "area": _text(area, "area", "The area", 80, required=False),
    }


def _create(actor, *, owner, source_request=None, containment="", **details):
    nc = Nonconformance.objects.create(
        **details,
        owner=owner,
        source_request=source_request,
        containment=_text(containment, "containment", "Containment", required=False),
        raised_by=actor,
    )
    nc.number = f"{NUMBER_PREFIX}{nc.pk:06d}"
    nc.save(update_fields=["number"])
    record_event(actor=actor, action="nonconformance.raised", obj=nc, after=snapshot(nc))
    return nc


@transaction.atomic
def raise_nonconformance(
    *, actor, title, description, source, severity, area="", owner=None, containment=""
):
    """Raise a record (any staff role). Only a supervisor chooses its owner here; anyone else's
    record waits for one."""
    require_staff(actor)
    lock_enabled(MODULE)
    details = _details(title, description, source, severity, area)
    if owner is not None:
        if not is_supervisor(actor):
            raise PermissionDenied("A supervisor chooses the owner.")
        owner = _staff_account(owner, "owner", "the owner")
    return _create(actor, owner=owner, containment=containment, **details)


@transaction.atomic
def open_from_complaint(
    *, actor, request, expected_revision, title, description, severity, area="", containment=""
):
    """Open the nonconformance of a complaint from its complaint record: the person who records
    the complaint (its assignee or a supervisor) while the record can still change. The complaint
    keeps the record's number in place of a free-text reference, and the ticket is linked."""
    require_staff(actor)
    lock_enabled(MODULE)
    ticket = _locked_ticket(actor, request.pk)
    if expected_revision is None or ticket.revision != expected_revision:
        raise ValidationError("This ticket changed. Reload it and review the latest entries.")
    if not ticket.is_complaint:
        raise ValidationError("This ticket is not a complaint.")
    if not (actor.pk == ticket.assignee_id or is_supervisor(actor)):
        raise PermissionDenied("The assignee or a supervisor records the complaint.")
    review = ComplaintReview.objects.select_for_update().filter(request=ticket).first()
    if desk_services.review_counts(ticket, review):
        raise ValidationError(
            "This complaint has been reviewed; its record no longer changes. Raise a "
            "nonconformance and link the ticket instead."
        )
    existing = Nonconformance.objects.filter(source_request=ticket).first()
    if existing is not None:
        raise ValidationError(f"This complaint already has nonconformance {existing.number}.")
    details = _details(title, description, Source.COMPLAINT, severity, area)
    try:
        with transaction.atomic():
            nc = _create(
                actor, owner=None, source_request=ticket, containment=containment, **details
            )
    except IntegrityError as error:  # a concurrent opening won the unique constraint
        raise ValidationError("This complaint already has a nonconformance.") from error
    _link(actor, nc, ticket)
    _ticket_event(actor, ticket, "nonconformance_opened", nc)
    return nc


# Working a record


@transaction.atomic
def update_details(
    *, actor, nc, expected_revision, title, description, source, severity, area="", reason=""
):
    nc = _work(actor, nc, expected_revision)
    details = _details(title, description, source, severity, area)
    if nc.source_request_id and details["source"] != Source.COMPLAINT:
        raise ValidationError({"source": "A record opened from a complaint keeps that source."})
    before = snapshot(nc)
    for name, value in details.items():
        setattr(nc, name, value)
    if snapshot(nc) == before:
        raise ValidationError("Change at least one detail before saving.")
    _save(nc)
    _audit(actor, "nonconformance.updated", nc, before, _reason(reason, required=False))
    return nc


@transaction.atomic
def set_owner(*, actor, nc, expected_revision, owner, reason=""):
    """Choose or change the owner (a supervisor)."""
    nc = _lock(actor, nc, expected_revision)
    if nc.status == Status.CLOSED:
        raise ValidationError(CLOSED)
    if not is_supervisor(actor):
        raise PermissionDenied("A supervisor chooses the owner.")
    owner = _staff_account(owner, "owner", "the owner")
    if owner.pk == nc.owner_id:
        raise ValidationError({"owner": "This person already owns it."})
    before = {"owner": nc.owner_id}
    nc.owner = owner
    _save(nc)
    record_event(
        actor=actor,
        action="nonconformance.owner_changed",
        obj=nc,
        before=before,
        after={"owner": owner.pk},
        reason=_reason(reason, required=False),
    )
    return nc


@transaction.atomic
def record_investigation(*, actor, nc, expected_revision, containment, root_cause):
    """Containment (what was done at once) and the root cause."""
    nc = _work(actor, nc, expected_revision)
    before = snapshot(nc)
    nc.containment = _text(containment, "containment", "Containment", required=False)
    nc.root_cause = _text(root_cause, "root_cause", "The root cause", required=False)
    if snapshot(nc) == before:
        raise ValidationError("Change at least one detail before saving.")
    _save(nc)
    _audit(actor, "nonconformance.investigation_recorded", nc, before)
    return nc


@transaction.atomic
def record_impact(
    *,
    actor,
    nc,
    expected_revision,
    impact,
    impact_assessment,
    client_notice="",
    client_told_on=None,
    client_told_how="",
    client_told_by=None,
):
    """The decision on results already reported, what was reviewed to reach it, and whether, when,
    how and by whom the client was told."""
    nc = _work(actor, nc, expected_revision)
    before = snapshot(nc)
    nc.impact = _choice(impact, Impact, "impact", "Choose the decision on reported results.")
    nc.impact_assessment = _text(
        impact_assessment, "impact_assessment", "What was reviewed and the conclusion"
    )
    if client_notice not in ("", *ClientNotice.values):
        raise ValidationError({"client_notice": "Choose whether a client was told."})
    if nc.impact == Impact.AFFECTED and not client_notice:
        raise ValidationError(
            {"client_notice": "Reported results are affected: record whether the client was told."}
        )
    nc.client_notice = client_notice
    if client_notice == ClientNotice.TOLD:
        errors = {}
        if client_told_on is None:
            errors["client_told_on"] = "Enter the date the client was told."
        elif client_told_on > timezone.localdate():
            errors["client_told_on"] = "The date cannot be in the future."
        if client_told_by is None:
            errors["client_told_by"] = "Choose who told the client."
        how = (client_told_how or "").strip()
        if not how:
            errors["client_told_how"] = "Say how the client was told."
        elif len(how) > 160:
            errors["client_told_how"] = "Keep it to 160 characters."
        if errors:
            raise ValidationError(errors)
        nc.client_told_on, nc.client_told_how = client_told_on, how
        nc.client_told_by = _staff_account(client_told_by, "client_told_by", "who told the client")
    else:
        nc.client_told_on, nc.client_told_how, nc.client_told_by = None, "", None
    if snapshot(nc) == before:
        raise ValidationError("Change at least one detail before saving.")
    nc.impact_recorded_by, nc.impact_recorded_at = actor, timezone.now()
    _save(nc)
    _audit(actor, "nonconformance.impact_recorded", nc, before)
    return nc


@transaction.atomic
def change_status(*, actor, nc, expected_revision, status, reason=""):
    """Move an open record between Open, Investigating, Corrective action and Verification.
    Closing and reopening have their own services. Leaving Verification clears its effectiveness
    check, which the history keeps."""
    nc = _work(actor, nc, expected_revision)
    if status not in ACTIVE_STATUSES:
        raise ValidationError(
            {"status": "Choose Open, Investigating, Corrective action or Verification."}
        )
    if status == nc.status:
        raise ValidationError({"status": "It already has this status."})
    before = snapshot(nc)
    if nc.status == Status.VERIFICATION:
        _clear_effectiveness(nc)
    nc.status = status
    _save(nc)
    _audit(actor, "nonconformance.status_changed", nc, before, _reason(reason, required=False))
    return nc


def _clear_effectiveness(nc):
    nc.effectiveness, nc.effectiveness_note = "", ""
    nc.effectiveness_checked_by, nc.effectiveness_checked_at = None, None


@transaction.atomic
def record_effectiveness(*, actor, nc, expected_revision, result, note):
    """The effectiveness check, in Verification. "Effective" needs every action finished."""
    nc = _work(actor, nc, expected_revision)
    if nc.status != Status.VERIFICATION:
        raise ValidationError("Move it to Verification before checking effectiveness.")
    result = _choice(result, Effectiveness, "result", "Choose the result of the check.")
    note = _text(note, "note", "What was checked")
    if result == Effectiveness.EFFECTIVE and nc.actions.filter(status=ActionStatus.OPEN).exists():
        raise ValidationError(
            {"result": "Finish or cancel the open actions before recording them effective."}
        )
    before = snapshot(nc)
    nc.effectiveness, nc.effectiveness_note = result, note
    nc.effectiveness_checked_by, nc.effectiveness_checked_at = actor, timezone.now()
    _save(nc)
    _audit(actor, "nonconformance.effectiveness_recorded", nc, before)
    return nc


@transaction.atomic
def close(*, actor, nc, expected_revision, note):
    """Close the record: a supervisor who is not its owner, once it is in Verification with the
    decision on reported results, every action finished and an effectiveness check that allows
    it. Every condition is checked on the locked record."""
    nc = _lock(actor, nc, expected_revision)
    open_actions = CorrectiveAction.objects.filter(
        nonconformance=nc, status=ActionStatus.OPEN
    ).count()
    if not is_supervisor(actor):
        raise PermissionDenied("A supervisor closes a nonconformance.")
    if refusal := close_refusal(actor, nc, open_actions):
        raise ValidationError(refusal)
    before = snapshot(nc)
    nc.status = Status.CLOSED
    nc.closed_at, nc.closed_by = timezone.now(), actor
    nc.closure_note = _text(note, "note", "A closing note")
    _save(nc)
    _audit(actor, "nonconformance.closed", nc, before)
    return nc


@transaction.atomic
def reopen(*, actor, nc, expected_revision, reason):
    """Reopen a closed record with a reason (a supervisor). It returns to Investigating; the
    closing and the effectiveness check are cleared from the record and kept in its history."""
    nc = _lock(actor, nc, expected_revision)
    if not is_supervisor(actor):
        raise PermissionDenied("A supervisor reopens a nonconformance.")
    if refusal := reopen_refusal(actor, nc):
        raise ValidationError(refusal)
    reason = _reason(reason)
    before = snapshot(nc)
    nc.status = Status.INVESTIGATING
    nc.closed_at, nc.closed_by, nc.closure_note = None, None, ""
    _clear_effectiveness(nc)
    _save(nc)
    _audit(actor, "nonconformance.reopened", nc, before, reason)
    return nc


# Actions


def _action_values(description, assignee, due_on, *, check_date=True):
    errors = {}
    try:
        description = _text(description, "description", "The action", 2000)
    except ValidationError as error:
        errors.update(error.message_dict)
    if due_on is None:
        errors["due_on"] = "Enter a due date."
    elif check_date and due_on < timezone.localdate():
        errors["due_on"] = "Choose today or a later date."
    if assignee is None:
        errors["assignee"] = "Choose who does it."
    if errors:
        raise ValidationError(errors)
    return description, _staff_account(assignee, "assignee", "the person who does it"), due_on


def _locked_action(nc, action):
    locked = (
        CorrectiveAction.objects.select_for_update().filter(pk=action.pk, nonconformance=nc).first()
    )
    if locked is None:
        raise Http404("No such action.")
    return locked


@transaction.atomic
def add_action(*, actor, nc, expected_revision, description, assignee, due_on):
    nc = _work(actor, nc, expected_revision)
    description, assignee, due_on = _action_values(description, assignee, due_on)
    action = CorrectiveAction.objects.create(
        nonconformance=nc,
        description=description,
        assignee=assignee,
        due_on=due_on,
        created_by=actor,
    )
    record_event(actor=actor, action="corrective_action.added", obj=action, after=snapshot(action))
    _save(nc)
    return action


@transaction.atomic
def update_action(*, actor, nc, expected_revision, action, description, assignee, due_on, reason):
    """Change an open action's text, person or due date, with a reason. A due date already past
    may stay as it is; a new one is today or later."""
    nc = _work(actor, nc, expected_revision)
    action = _locked_action(nc, action)
    if action.status != ActionStatus.OPEN:
        raise ValidationError("Only an open action changes.")
    description, assignee, due_on = _action_values(
        description, assignee, due_on, check_date=due_on != action.due_on
    )
    before = snapshot(action)
    action.description, action.assignee, action.due_on = description, assignee, due_on
    if snapshot(action) == before:
        raise ValidationError("Change at least one detail before saving.")
    action.save()
    record_event(
        actor=actor,
        action="corrective_action.updated",
        obj=action,
        before=before,
        after=snapshot(action),
        reason=_reason(reason),
    )
    _save(nc)
    return action


@transaction.atomic
def finish_action(*, actor, nc, expected_revision, action, done, note):
    """Mark an open action done (its assignee, the owner or a supervisor) or cancel it (the owner
    or a supervisor), with what was done or why."""
    nc = _lock(actor, nc, expected_revision)
    if nc.status == Status.CLOSED:
        raise ValidationError(CLOSED)
    action = _locked_action(nc, action)
    if done and not can_finish_action(actor, nc, action):
        raise PermissionDenied("Its assignee, the owner or a supervisor marks an action done.")
    if not done and not can_work(actor, nc):
        raise PermissionDenied("The owner or a supervisor cancels an action.")
    if action.status != ActionStatus.OPEN:
        raise ValidationError("This action is already finished.")
    before = snapshot(action)
    action.status = ActionStatus.DONE if done else ActionStatus.CANCELLED
    action.finished_by, action.finished_at = actor, timezone.now()
    action.finish_note = _text(note, "note", "What was done" if done else "A reason", 2000)
    action.save()
    record_event(
        actor=actor,
        action="corrective_action.done" if done else "corrective_action.cancelled",
        obj=action,
        before=before,
        after=snapshot(action),
    )
    _save(nc)
    return action


# Linked tickets


@transaction.atomic
def link_ticket(*, actor, nc, expected_revision, request):
    """Link a ticket the actor may read. The ticket's timeline records it."""
    nc = _work(actor, nc, expected_revision)
    ticket = _locked_ticket(actor, request.pk)
    link = _link(actor, nc, ticket)
    _ticket_event(actor, ticket, "nonconformance_linked", nc)
    _save(nc)
    return link


@transaction.atomic
def unlink_ticket(*, actor, nc, expected_revision, link, reason):
    """Remove a linked ticket, with a reason; the link is kept with who removed it. The ticket
    the record was opened from stays linked."""
    nc = _work(actor, nc, expected_revision)
    link = (
        TicketLink.objects.select_for_update()
        .filter(pk=link.pk, nonconformance=nc, removed_at__isnull=True)
        .first()
    )
    if link is None:
        raise Http404("No such linked ticket.")
    ticket = _locked_ticket(actor, link.request_id)
    if ticket.pk == nc.source_request_id:
        raise ValidationError("The complaint it was opened from stays linked.")
    link.removed_by, link.removed_at = actor, timezone.now()
    link.removal_reason = _reason(reason)
    link.save()
    record_event(
        actor=actor,
        action="nonconformance.ticket_unlinked",
        obj=nc,
        before={"ticket_link_id": link.pk, "request_id": ticket.pk},
        reason=link.removal_reason,
    )
    _ticket_event(actor, ticket, "nonconformance_unlinked", nc)
    _save(nc)
    return link
