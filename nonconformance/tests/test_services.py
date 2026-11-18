"""Regression tests for nonconformance.services: the module switch, roles, stale edits, closed
records, closing and reopening, impact, actions, and tickets. All data is synthetic."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from django.utils import timezone

from core.models import AuditEvent, LabModule
from desk import services as desk_services
from desk.models import ComplaintDecision, EntryKind
from nonconformance import services
from nonconformance.models import (
    ActionStatus,
    CorrectiveAction,
    Nonconformance,
    Status,
    TicketLink,
)
from taxonomy.models import RequestCategory

pytestmark = pytest.mark.django_db

TODAY = timezone.localdate


def fresh(obj):
    return type(obj).objects.get(pk=obj.pk)


def row(obj):
    return type(obj).objects.filter(pk=obj.pk).values().get()


def audit_count():
    return AuditEvent.objects.count()


# Fixtures


@pytest.fixture
def helper(make_user):
    """A technical staff member who owns nothing and is assigned nothing."""
    return make_user("helper", "technical")


@pytest.fixture
def other_ticket(agent, plain_category, clinic):
    return desk_services.create_request(
        actor=agent,
        category=plain_category,
        subject="Second question TESTSUBJ",
        first_entry_kind="note",
        first_entry_body="Second synthetic entry TESTBODY.",
        channel="phone",
        client=clinic,
    )


@pytest.fixture
def restricted_complaint_category(group, quality_team):
    return RequestCategory.objects.create(
        key="privacy_complaint",
        name="Privacy complaint",
        parent=group,
        default_team=quality_team,
        is_restricted=True,
        is_complaint=True,
    )


@pytest.fixture
def ctx(module_on, lead, lead2, agent, tech, helper, admin, plain_ticket, other_ticket, complaint):
    """A record owned by `agent`, in Verification, with one open action (assignee `tech`) and
    `plain_ticket` linked."""
    nc = services.raise_nonconformance(
        actor=lead,
        title="Sample labelling error",
        description="A tube was labelled with the wrong identifier.",
        source="internal",
        severity="minor",
        owner=agent,
    )
    action = services.add_action(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        description="Retrain the receiving bench.",
        assignee=tech,
        due_on=TODAY() + timedelta(days=7),
    )
    nc = fresh(nc)
    link = services.link_ticket(
        actor=agent, nc=nc, expected_revision=nc.revision, request=plain_ticket
    )
    nc = fresh(nc)
    nc = services.change_status(
        actor=agent, nc=nc, expected_revision=nc.revision, status=Status.VERIFICATION
    )
    return SimpleNamespace(
        nc=nc,
        action=action,
        link=link,
        ticket=plain_ticket,
        other=other_ticket,
        complaint=complaint,
        lead=lead,
        lead2=lead2,
        agent=agent,
        tech=tech,
        helper=helper,
        admin=admin,
    )


def current(c):
    return fresh(c.nc)


def finish(c, *, done=True, note="Finished."):
    return services.finish_action(
        actor=c.agent,
        nc=current(c),
        expected_revision=current(c).revision,
        action=c.action,
        done=done,
        note=note,
    )


def impact(c, *, actor=None, **kwargs):
    values = {
        "impact": "none",
        "impact_assessment": "Reviewed the affected orders; no results changed.",
    }
    values.update(kwargs)
    nc = current(c)
    return services.record_impact(
        actor=actor or c.agent, nc=nc, expected_revision=nc.revision, **values
    )


def effectiveness(c, result="effective", note="Checked two weeks of samples."):
    nc = current(c)
    return services.record_effectiveness(
        actor=c.agent, nc=nc, expected_revision=nc.revision, result=result, note=note
    )


def closable(c):
    """Bring the record to the point where a supervisor other than the owner can close it."""
    finish(c)
    impact(c)
    return effectiveness(c)


def close_by(c, actor=None, note="Checked."):
    nc = current(c)
    return services.close(actor=actor or c.lead2, nc=nc, expected_revision=nc.revision, note=note)


@pytest.fixture
def closed(ctx):
    closable(ctx)
    close_by(ctx)
    return ctx


# One call per service, with a valid change for the state `ctx` is in. Each takes the context,
# the actor and the revision to send (the current one by default).


def _rev(c, rev):
    return current(c).revision if rev is None else rev


def _details(c, actor, rev=None):
    return services.update_details(
        actor=actor,
        nc=c.nc,
        expected_revision=_rev(c, rev),
        title="A new title",
        description="A new description of what happened.",
        source="audit",
        severity="major",
    )


def _investigation(c, actor, rev=None):
    return services.record_investigation(
        actor=actor,
        nc=c.nc,
        expected_revision=_rev(c, rev),
        containment="Tubes relabelled.",
        root_cause="Labels printed from a stale list.",
        similar_cases="One other tube the same day.",
    )


def _impact(c, actor, rev=None):
    return services.record_impact(
        actor=actor,
        nc=c.nc,
        expected_revision=_rev(c, rev),
        impact="none",
        impact_assessment="Reviewed orders; nothing reported.",
    )


def _status(c, actor, rev=None):
    return services.change_status(
        actor=actor,
        nc=c.nc,
        expected_revision=_rev(c, rev),
        status=Status.INVESTIGATING,
    )


def _effectiveness(c, actor, rev=None):
    return services.record_effectiveness(
        actor=actor,
        nc=c.nc,
        expected_revision=_rev(c, rev),
        result="not_effective",
        note="Another error occurred.",
    )


def _add_action(c, actor, rev=None):
    return services.add_action(
        actor=actor,
        nc=c.nc,
        expected_revision=_rev(c, rev),
        description="Check the label printer.",
        assignee=c.tech,
        due_on=TODAY() + timedelta(days=3),
    )


def _update_action(c, actor, rev=None):
    return services.update_action(
        actor=actor,
        nc=c.nc,
        expected_revision=_rev(c, rev),
        action=c.action,
        description="Retrain the whole receiving team.",
        assignee=c.tech,
        due_on=c.action.due_on,
        reason="Wider scope.",
    )


def _finish_action(c, actor, rev=None):
    return services.finish_action(
        actor=actor,
        nc=c.nc,
        expected_revision=_rev(c, rev),
        action=c.action,
        done=True,
        note="Training held.",
    )


def _link(c, actor, rev=None):
    return services.link_ticket(
        actor=actor, nc=c.nc, expected_revision=_rev(c, rev), request=c.other
    )


def _unlink(c, actor, rev=None):
    return services.unlink_ticket(
        actor=actor,
        nc=c.nc,
        expected_revision=_rev(c, rev),
        link=c.link,
        reason="Linked by mistake.",
    )


def _set_owner(c, actor, rev=None):
    return services.set_owner(
        actor=actor, nc=c.nc, expected_revision=_rev(c, rev), owner=c.lead2, reason="Handover."
    )


def _close(c, actor, rev=None):
    return services.close(actor=actor, nc=c.nc, expected_revision=_rev(c, rev), note="Closing.")


def _reopen(c, actor, rev=None):
    return services.reopen(
        actor=actor, nc=c.nc, expected_revision=_rev(c, rev), reason="Happened again."
    )


# Services the owner or a supervisor works on an open record.
WORK = {
    "update_details": _details,
    "record_investigation": _investigation,
    "record_impact": _impact,
    "change_status": _status,
    "record_effectiveness": _effectiveness,
    "add_action": _add_action,
    "update_action": _update_action,
    "link_ticket": _link,
    "unlink_ticket": _unlink,
}
# Every service on an existing record, for stale edits and the module switch.
ON_RECORD = {
    **WORK,
    "finish_action": _finish_action,
    "set_owner": _set_owner,
    "close": _close,
    "reopen": _reopen,
}
# The changes that must succeed on `ctx` as it stands (close and reopen need other states).
CHANGES = {**WORK, "finish_action": _finish_action, "set_owner": _set_owner}


def _raise(c, actor, rev=None, **extra):
    return services.raise_nonconformance(
        actor=actor,
        title="Freezer excursion",
        description="The freezer log shows a night excursion.",
        source="equipment",
        severity="major",
        **extra,
    )


def _open_from_complaint(c, actor, rev=None):
    return services.open_from_complaint(
        actor=actor,
        request=c.complaint,
        expected_revision=fresh(c.complaint).revision if rev is None else rev,
        title="Late results",
        description="Results arrived late three times.",
        severity="major",
    )


EVERYTHING = {
    **ON_RECORD,
    "raise_nonconformance": _raise,
    "open_from_complaint": _open_from_complaint,
}

names = pytest.mark.parametrize


# The module switch


@names("name", sorted(EVERYTHING))
def test_every_service_refuses_while_the_module_is_off(ctx, name):
    LabModule.objects.filter(key="nonconformance").update(is_enabled=False)
    before = (audit_count(), Nonconformance.objects.count(), row(ctx.nc))
    with pytest.raises(Http404):
        EVERYTHING[name](ctx, ctx.lead)
    assert (audit_count(), Nonconformance.objects.count(), row(ctx.nc)) == before


@names("name", sorted(EVERYTHING))
def test_every_service_refuses_when_the_module_has_no_row(ctx, name):
    LabModule.objects.all().delete()
    with pytest.raises(Http404):
        EVERYTHING[name](ctx, ctx.lead)


def test_a_record_changes_again_when_the_module_is_switched_back_on(ctx):
    LabModule.objects.filter(key="nonconformance").update(is_enabled=False)
    LabModule.objects.filter(key="nonconformance").update(is_enabled=True)
    assert _details(ctx, ctx.agent).title == "A new title"


# Roles


@names("actor_name", ["agent", "tech", "lead", "admin", "helper"])
def test_every_staff_role_raises_a_record(ctx, actor_name):
    nc = _raise(ctx, getattr(ctx, actor_name))
    assert nc.raised_by == getattr(ctx, actor_name)
    assert nc.status == Status.OPEN and nc.owner is None
    assert nc.revision == 1


def test_a_record_gets_its_number_and_a_raised_event(ctx):
    nc = _raise(ctx, ctx.agent)
    assert nc.number == f"NC-{nc.pk:06d}"
    event = AuditEvent.objects.filter(action="nonconformance.raised", object_id=str(nc.pk)).get()
    assert event.actor == ctx.agent


def test_a_portal_account_cannot_raise_a_record(ctx, portal):
    with pytest.raises(PermissionDenied):
        _raise(ctx, portal)


def test_an_account_without_a_role_cannot_raise_a_record(ctx, no_role):
    with pytest.raises(PermissionDenied):
        _raise(ctx, no_role)


@names("name", sorted(ON_RECORD))
def test_a_portal_account_cannot_use_any_service(ctx, portal, name):
    before = (audit_count(), row(ctx.nc))
    with pytest.raises(PermissionDenied):
        ON_RECORD[name](ctx, portal)
    assert (audit_count(), row(ctx.nc)) == before


@names("name", sorted(ON_RECORD))
def test_an_account_without_a_role_cannot_use_any_service(ctx, no_role, name):
    before = (audit_count(), row(ctx.nc))
    with pytest.raises(PermissionDenied):
        ON_RECORD[name](ctx, no_role)
    assert (audit_count(), row(ctx.nc)) == before


def test_a_portal_account_cannot_open_a_record_from_a_complaint(ctx, portal):
    with pytest.raises(PermissionDenied):
        _open_from_complaint(ctx, portal)


def test_only_a_supervisor_sets_an_owner_when_raising(ctx):
    with pytest.raises(PermissionDenied):
        _raise(ctx, ctx.agent, owner=ctx.agent)
    assert _raise(ctx, ctx.lead, owner=ctx.tech).owner == ctx.tech


def test_admin_counts_as_a_supervisor_when_raising_with_an_owner(ctx):
    assert _raise(ctx, ctx.admin, owner=ctx.agent).owner == ctx.agent


@names("kind", ["inactive", "portal", "no_role"])
def test_an_owner_must_be_an_active_staff_account(ctx, portal, no_role, tech, kind):
    owner = {"portal": portal, "no_role": no_role, "inactive": tech}[kind]
    if kind == "inactive":
        tech.is_active = False
        tech.save()
    with pytest.raises(ValidationError):
        _raise(ctx, ctx.lead, owner=owner)
    with pytest.raises(ValidationError):
        services.set_owner(
            actor=ctx.lead, nc=ctx.nc, expected_revision=current(ctx).revision, owner=owner
        )


@names("actor_name", ["agent", "lead", "admin"])
@names("name", sorted(WORK))
def test_the_owner_or_a_supervisor_works_an_open_record(ctx, name, actor_name):
    WORK[name](ctx, getattr(ctx, actor_name))


@names("name", sorted(WORK))
def test_other_staff_do_not_work_a_record(ctx, name):
    before = (audit_count(), row(ctx.nc))
    with pytest.raises(PermissionDenied):
        WORK[name](ctx, ctx.helper)
    assert (audit_count(), row(ctx.nc)) == before


def test_a_record_without_an_owner_is_worked_by_a_supervisor_only(ctx, agent):
    nc = _raise(ctx, ctx.agent)
    with pytest.raises(PermissionDenied):
        services.change_status(
            actor=agent, nc=nc, expected_revision=nc.revision, status=Status.INVESTIGATING
        )
    done = services.change_status(
        actor=ctx.lead, nc=nc, expected_revision=nc.revision, status=Status.INVESTIGATING
    )
    assert done.status == Status.INVESTIGATING


def test_the_owner_does_not_set_the_owner(ctx):
    with pytest.raises(PermissionDenied):
        services.set_owner(
            actor=ctx.agent, nc=ctx.nc, expected_revision=current(ctx).revision, owner=ctx.tech
        )


def test_a_supervisor_changes_the_owner(ctx):
    nc = _set_owner(ctx, ctx.lead)
    assert nc.owner == ctx.lead2
    event = AuditEvent.objects.filter(action="nonconformance.owner_changed").get()
    assert event.before == {"owner": ctx.agent.pk} and event.after == {"owner": ctx.lead2.pk}


def test_setting_the_same_owner_is_refused(ctx):
    with pytest.raises(ValidationError):
        services.set_owner(
            actor=ctx.lead, nc=ctx.nc, expected_revision=current(ctx).revision, owner=ctx.agent
        )


def test_an_actions_assignee_may_mark_it_done(ctx):
    action = _finish_action(ctx, ctx.tech)
    assert action.status == ActionStatus.DONE and action.finished_by == ctx.tech


def test_an_actions_assignee_may_not_cancel_it(ctx):
    with pytest.raises(PermissionDenied):
        services.finish_action(
            actor=ctx.tech,
            nc=ctx.nc,
            expected_revision=current(ctx).revision,
            action=ctx.action,
            done=False,
            note="Not needed.",
        )
    assert fresh(ctx.action).status == ActionStatus.OPEN


def test_other_staff_may_not_mark_an_action_done(ctx):
    with pytest.raises(PermissionDenied):
        _finish_action(ctx, ctx.helper)


@names("actor_name", ["agent", "lead", "admin"])
def test_the_owner_or_a_supervisor_cancels_an_action(ctx, actor_name):
    action = services.finish_action(
        actor=getattr(ctx, actor_name),
        nc=ctx.nc,
        expected_revision=current(ctx).revision,
        action=ctx.action,
        done=False,
        note="Superseded.",
    )
    assert action.status == ActionStatus.CANCELLED


# Raising: validation


@names(
    "field,value",
    [
        ("title", " "),
        ("title", "x" * 161),
        ("description", ""),
        ("description", "x" * 4001),
        ("source", "nowhere"),
        ("severity", "huge"),
        ("area", "x" * 81),
    ],
)
def test_raising_validates_the_details(ctx, field, value):
    values = {
        "title": "Freezer excursion",
        "description": "Log shows an excursion.",
        "source": "equipment",
        "severity": "major",
        field: value,
    }
    with pytest.raises(ValidationError):
        services.raise_nonconformance(actor=ctx.agent, **values)


# Stale edits


@names("name", sorted(ON_RECORD))
def test_every_service_refuses_a_stale_revision_and_changes_nothing(ctx, name):
    before = (
        audit_count(),
        row(ctx.nc),
        CorrectiveAction.objects.count(),
        list(TicketLink.objects.values()),
        row(ctx.complaint),
        row(ctx.other),
    )
    with pytest.raises(ValidationError):
        ON_RECORD[name](ctx, ctx.lead, rev=current(ctx).revision + 1)
    assert (
        audit_count(),
        row(ctx.nc),
        CorrectiveAction.objects.count(),
        list(TicketLink.objects.values()),
        row(ctx.complaint),
        row(ctx.other),
    ) == before


@names("name", sorted(ON_RECORD))
def test_every_service_refuses_an_older_revision_after_another_change(ctx, name):
    old = current(ctx).revision
    services.record_investigation(
        actor=ctx.agent,
        nc=ctx.nc,
        expected_revision=old,
        containment="Something new.",
        root_cause="",
    )
    before = (audit_count(), row(ctx.nc))
    with pytest.raises(ValidationError):
        ON_RECORD[name](ctx, ctx.lead, rev=old)
    assert (audit_count(), row(ctx.nc)) == before


def test_a_missing_revision_is_refused(ctx):
    with pytest.raises(ValidationError):
        services.change_status(
            actor=ctx.lead, nc=ctx.nc, expected_revision=None, status=Status.OPEN
        )


@names("name", sorted(CHANGES))
def test_a_successful_change_advances_the_revision_by_one(ctx, name):
    before = current(ctx).revision
    CHANGES[name](ctx, ctx.lead)
    assert current(ctx).revision == before + 1


# Linking and unlinking a ticket change the ticket too: it gets an audit event of its own.
TICKET_CHANGES = {
    "link_ticket": "request.nonconformance_linked",
    "unlink_ticket": "request.nonconformance_unlinked",
}


@names("name", sorted(CHANGES))
def test_a_successful_change_writes_one_audit_event(ctx, name):
    before = (
        AuditEvent.objects.exclude(object_type="desk.servicerequest").count(),
        AuditEvent.objects.filter(object_type="desk.servicerequest").count(),
    )
    CHANGES[name](ctx, ctx.lead)
    after = (
        AuditEvent.objects.exclude(object_type="desk.servicerequest").count(),
        AuditEvent.objects.filter(object_type="desk.servicerequest").count(),
    )
    ticket_events = 1 if name in TICKET_CHANGES else 0
    assert after == (before[0] + 1, before[1] + ticket_events)


@names("name", sorted(TICKET_CHANGES))
def test_a_ticket_change_is_in_the_ticket_audit_history_with_ids_only(ctx, name):
    CHANGES[name](ctx, ctx.lead)
    event = AuditEvent.objects.filter(
        object_type="desk.servicerequest", action=TICKET_CHANGES[name]
    ).latest("occurred_at")
    assert event.actor == ctx.lead
    assert set(event.after) == {"nonconformance_id", "number"}


def test_closing_and_reopening_each_advance_the_revision_by_one(ctx):
    closable(ctx)
    before = current(ctx).revision
    close_by(ctx)
    assert current(ctx).revision == before + 1
    _reopen(ctx, ctx.lead2)
    assert current(ctx).revision == before + 2


# Closed records


@names("name", sorted(set(WORK) | {"finish_action", "set_owner"}))
def test_a_closed_record_does_not_change(closed, name):
    snapshot_before = (audit_count(), row(closed.nc))
    calls = {**WORK, "finish_action": _finish_action, "set_owner": _set_owner}
    with pytest.raises(ValidationError):
        calls[name](closed, closed.lead)
    assert (audit_count(), row(closed.nc)) == snapshot_before


def test_a_closed_record_changes_again_once_reopened(closed):
    _reopen(closed, closed.lead2)
    assert _details(closed, closed.agent).title == "A new title"


# Closing


def test_a_supervisor_other_than_the_owner_closes(ctx):
    closable(ctx)
    nc = close_by(ctx, note="All checked.")
    assert nc.status == Status.CLOSED
    assert nc.closed_by == ctx.lead2 and nc.closed_at is not None
    assert nc.closure_note == "All checked."
    assert AuditEvent.objects.filter(action="nonconformance.closed").count() == 1


def test_no_action_needed_also_closes(ctx):
    finish(ctx, done=False, note="Nothing to do.")
    impact(ctx, impact="not_applicable")
    effectiveness(ctx, "no_action_needed", "No action was needed.")
    assert close_by(ctx).status == Status.CLOSED


def test_admin_closes_as_a_supervisor(ctx):
    closable(ctx)
    assert close_by(ctx, actor=ctx.admin).closed_by == ctx.admin


@names("actor_name", ["agent", "tech", "helper"])
def test_only_a_supervisor_closes(ctx, actor_name):
    closable(ctx)
    with pytest.raises(PermissionDenied):
        close_by(ctx, actor=getattr(ctx, actor_name))
    assert current(ctx).status == Status.VERIFICATION


def test_the_owner_does_not_close_even_as_a_supervisor(ctx):
    closable(ctx)
    services.set_owner(
        actor=ctx.lead, nc=ctx.nc, expected_revision=current(ctx).revision, owner=ctx.lead2
    )
    with pytest.raises(ValidationError, match="another supervisor"):
        close_by(ctx, actor=ctx.lead2)


def test_a_record_without_an_owner_is_not_closed(ctx):
    nc = _raise(ctx, ctx.lead)
    nc = services.change_status(
        actor=ctx.lead, nc=nc, expected_revision=nc.revision, status=Status.VERIFICATION
    )
    nc = services.record_impact(
        actor=ctx.lead,
        nc=nc,
        expected_revision=nc.revision,
        impact="none",
        impact_assessment="Reviewed; none.",
    )
    nc = services.record_effectiveness(
        actor=ctx.lead,
        nc=nc,
        expected_revision=nc.revision,
        result="no_action_needed",
        note="Nothing needed.",
    )
    with pytest.raises(ValidationError, match="owner"):
        services.close(actor=ctx.lead2, nc=nc, expected_revision=nc.revision, note="Closing.")


@names("status", [Status.OPEN, Status.INVESTIGATING, Status.CORRECTIVE_ACTION])
def test_a_record_outside_verification_is_not_closed(ctx, status):
    closable(ctx)
    Nonconformance.objects.filter(pk=ctx.nc.pk).update(status=status)
    with pytest.raises(ValidationError, match="Verification"):
        close_by(ctx)


def test_a_record_without_an_impact_decision_is_not_closed(ctx):
    finish(ctx)
    effectiveness(ctx)
    with pytest.raises(ValidationError, match="reported results"):
        close_by(ctx)


def test_affected_results_without_a_client_notice_are_not_closed(ctx):
    closable(ctx)
    Nonconformance.objects.filter(pk=ctx.nc.pk).update(impact="affected", client_notice="")
    with pytest.raises(ValidationError, match="client"):
        close_by(ctx)


def test_affected_results_with_a_client_notice_close(ctx):
    finish(ctx)
    impact(ctx, impact="affected", client_notice="not_needed")
    effectiveness(ctx)
    assert close_by(ctx).status == Status.CLOSED


def test_open_actions_block_closing(ctx):
    impact(ctx)
    effectiveness(ctx, "no_action_needed", "Nothing needed.")
    with pytest.raises(ValidationError, match="open actions"):
        close_by(ctx)


def test_a_not_effective_check_blocks_closing(ctx):
    finish(ctx)
    impact(ctx)
    effectiveness(ctx, "not_effective", "Errors continue.")
    with pytest.raises(ValidationError, match="effectiveness"):
        close_by(ctx)


def test_no_effectiveness_check_blocks_closing(ctx):
    finish(ctx)
    impact(ctx)
    with pytest.raises(ValidationError, match="effectiveness"):
        close_by(ctx)


def test_a_failed_close_changes_nothing(ctx):
    before = (audit_count(), row(ctx.nc))
    with pytest.raises(ValidationError):
        close_by(ctx)
    assert (audit_count(), row(ctx.nc)) == before


def test_a_closing_note_is_required(ctx):
    closable(ctx)
    with pytest.raises(ValidationError):
        close_by(ctx, note=" ")
    assert current(ctx).status == Status.VERIFICATION


def test_the_database_refuses_a_closer_who_is_the_owner(ctx):
    with pytest.raises(IntegrityError), transaction.atomic():
        Nonconformance.objects.filter(pk=ctx.nc.pk).update(
            status=Status.CLOSED, closed_at=timezone.now(), closed_by=ctx.agent
        )


def test_the_database_refuses_a_closer_on_a_record_without_an_owner(ctx):
    nc = _raise(ctx, ctx.lead)
    with pytest.raises(IntegrityError), transaction.atomic():
        Nonconformance.objects.filter(pk=nc.pk).update(
            status=Status.CLOSED, closed_at=timezone.now(), closed_by=ctx.lead
        )


# Reopening


def test_a_supervisor_reopens_a_closed_record(closed):
    nc = _reopen(closed, closed.lead2)
    assert nc.status == Status.INVESTIGATING
    assert nc.closed_at is None and nc.closed_by is None and nc.closure_note == ""
    assert nc.effectiveness == "" and nc.effectiveness_note == ""
    assert nc.effectiveness_checked_by is None and nc.effectiveness_checked_at is None


def test_reopening_keeps_the_previous_values_in_the_audit_event(closed):
    _reopen(closed, closed.lead2)
    event = AuditEvent.objects.filter(action="nonconformance.reopened").get()
    assert event.reason == "Happened again."
    assert event.before["status"] == "closed"
    assert event.before["closed_by"] == closed.lead2.pk
    assert event.before["closure_note"] == "Checked."
    assert event.before["effectiveness"] == "effective"
    assert event.after["status"] == "investigating"
    assert event.after["effectiveness"] == ""


@names("actor_name", ["agent", "tech", "helper"])
def test_only_a_supervisor_reopens(closed, actor_name):
    with pytest.raises(PermissionDenied):
        _reopen(closed, getattr(closed, actor_name))
    assert fresh(closed.nc).status == Status.CLOSED


def test_the_owner_who_is_not_a_supervisor_does_not_reopen(closed):
    with pytest.raises(PermissionDenied):
        _reopen(closed, closed.agent)


@names("reason", ["", "  "])
def test_reopening_needs_a_reason(closed, reason):
    with pytest.raises(ValidationError):
        services.reopen(
            actor=closed.lead,
            nc=closed.nc,
            expected_revision=fresh(closed.nc).revision,
            reason=reason,
        )
    assert fresh(closed.nc).status == Status.CLOSED


def test_an_open_record_is_not_reopened(ctx):
    with pytest.raises(ValidationError):
        _reopen(ctx, ctx.lead)


# Status


@names("status", ["closed", "nowhere", ""])
def test_change_status_refuses_anything_but_the_four_working_statuses(ctx, status):
    with pytest.raises(ValidationError):
        services.change_status(
            actor=ctx.lead, nc=ctx.nc, expected_revision=current(ctx).revision, status=status
        )
    assert current(ctx).status == Status.VERIFICATION


@names("status", [Status.OPEN, Status.INVESTIGATING, Status.CORRECTIVE_ACTION])
def test_change_status_moves_among_the_working_statuses(ctx, status):
    assert (
        services.change_status(
            actor=ctx.lead, nc=ctx.nc, expected_revision=current(ctx).revision, status=status
        ).status
        == status
    )


def test_change_status_refuses_the_same_status(ctx):
    with pytest.raises(ValidationError):
        services.change_status(
            actor=ctx.lead,
            nc=ctx.nc,
            expected_revision=current(ctx).revision,
            status=Status.VERIFICATION,
        )


def test_leaving_verification_clears_the_effectiveness_check(ctx):
    effectiveness(ctx, "not_effective", "Errors continue.")
    nc = services.change_status(
        actor=ctx.lead,
        nc=ctx.nc,
        expected_revision=current(ctx).revision,
        status=Status.CORRECTIVE_ACTION,
        reason="Back to work.",
    )
    assert nc.effectiveness == "" and nc.effectiveness_note == ""
    assert nc.effectiveness_checked_by is None and nc.effectiveness_checked_at is None
    event = AuditEvent.objects.filter(action="nonconformance.status_changed").latest("occurred_at")
    assert event.before["effectiveness"] == "not_effective"
    assert event.reason == "Back to work."


def test_moving_between_other_statuses_keeps_the_effectiveness_fields_empty(ctx):
    nc = services.change_status(
        actor=ctx.lead,
        nc=ctx.nc,
        expected_revision=current(ctx).revision,
        status=Status.OPEN,
    )
    assert nc.effectiveness == ""


# Effectiveness


@names("status", [Status.OPEN, Status.INVESTIGATING, Status.CORRECTIVE_ACTION])
def test_effectiveness_is_recorded_only_in_verification(ctx, status):
    Nonconformance.objects.filter(pk=ctx.nc.pk).update(status=status)
    with pytest.raises(ValidationError, match="Verification"):
        effectiveness(ctx, "not_effective")


def test_effective_is_refused_while_an_action_is_open(ctx):
    with pytest.raises(ValidationError):
        effectiveness(ctx, "effective")
    assert current(ctx).effectiveness == ""


def test_effective_is_recorded_once_actions_are_finished(ctx):
    finish(ctx)
    nc = effectiveness(ctx)
    assert nc.effectiveness == "effective" and nc.effectiveness_checked_by == ctx.agent
    assert nc.effectiveness_checked_at is not None


def test_effectiveness_needs_a_valid_result_and_a_note(ctx):
    with pytest.raises(ValidationError):
        effectiveness(ctx, "perhaps")
    with pytest.raises(ValidationError):
        effectiveness(ctx, "not_effective", " ")


# Investigation


def test_record_investigation_stores_the_three_texts(ctx):
    nc = _investigation(ctx, ctx.agent)
    assert nc.containment == "Tubes relabelled."
    assert nc.root_cause == "Labels printed from a stale list."
    assert nc.similar_cases == "One other tube the same day."


def test_similar_cases_is_optional(ctx):
    nc = services.record_investigation(
        actor=ctx.agent,
        nc=ctx.nc,
        expected_revision=current(ctx).revision,
        containment="Contained.",
        root_cause="",
    )
    assert nc.similar_cases == ""


def test_similar_cases_is_stripped(ctx):
    nc = services.record_investigation(
        actor=ctx.agent,
        nc=ctx.nc,
        expected_revision=current(ctx).revision,
        containment="",
        root_cause="",
        similar_cases="  Two earlier cases.  ",
    )
    assert nc.similar_cases == "Two earlier cases."


def test_similar_cases_takes_at_most_4000_characters(ctx):
    with pytest.raises(ValidationError):
        services.record_investigation(
            actor=ctx.agent,
            nc=ctx.nc,
            expected_revision=current(ctx).revision,
            containment="",
            root_cause="",
            similar_cases="x" * 4001,
        )
    assert current(ctx).similar_cases == ""


def test_similar_cases_alone_counts_as_a_change(ctx):
    nc = services.record_investigation(
        actor=ctx.agent,
        nc=ctx.nc,
        expected_revision=current(ctx).revision,
        containment="",
        root_cause="",
        similar_cases="Another tube.",
    )
    assert nc.similar_cases == "Another tube."


def test_the_audit_event_of_an_investigation_carries_similar_cases(ctx):
    _investigation(ctx, ctx.agent)
    event = AuditEvent.objects.filter(action="nonconformance.investigation_recorded").get()
    assert event.before["similar_cases"] == ""
    assert event.after["similar_cases"] == "One other tube the same day."


def test_an_investigation_without_a_change_is_refused(ctx):
    _investigation(ctx, ctx.agent)
    with pytest.raises(ValidationError):
        _investigation(ctx, ctx.agent)


@names("field", ["containment", "root_cause", "similar_cases"])
def test_investigation_texts_are_limited_to_4000_characters(ctx, field):
    values = {"containment": "", "root_cause": "", "similar_cases": "", field: "x" * 4001}
    with pytest.raises(ValidationError):
        services.record_investigation(
            actor=ctx.agent, nc=ctx.nc, expected_revision=current(ctx).revision, **values
        )


# Details


def test_update_details_changes_the_details(ctx):
    nc = _details(ctx, ctx.agent)
    assert (nc.title, nc.source, nc.severity) == ("A new title", "audit", "major")


def test_update_details_without_a_change_is_refused(ctx):
    nc = current(ctx)
    with pytest.raises(ValidationError):
        services.update_details(
            actor=ctx.agent,
            nc=nc,
            expected_revision=nc.revision,
            title=nc.title,
            description=nc.description,
            source=nc.source,
            severity=nc.severity,
            area=nc.area,
        )


def test_a_record_opened_from_a_complaint_keeps_its_source(ctx):
    nc = _open_from_complaint(ctx, ctx.agent)
    with pytest.raises(ValidationError):
        services.update_details(
            actor=ctx.lead,
            nc=nc,
            expected_revision=nc.revision,
            title="Changed",
            description=nc.description,
            source="internal",
            severity=nc.severity,
        )


# Impact


def test_impact_is_recorded_with_who_and_when(ctx):
    nc = impact(ctx)
    assert nc.impact == "none" and nc.impact_recorded_by == ctx.agent
    assert nc.impact_recorded_at is not None


@names("values", [{"impact": ""}, {"impact": "bogus"}, {"impact_assessment": " "}])
def test_impact_and_assessment_are_required(ctx, values):
    with pytest.raises(ValidationError):
        impact(ctx, **values)


def test_affected_results_need_a_client_notice(ctx):
    with pytest.raises(ValidationError):
        impact(ctx, impact="affected")
    assert impact(ctx, impact="affected", client_notice="not_needed").impact == "affected"


def test_an_unknown_client_notice_is_refused(ctx):
    with pytest.raises(ValidationError):
        impact(ctx, client_notice="maybe")


def test_a_client_told_is_recorded_with_date_how_and_teller(ctx):
    nc = impact(
        ctx,
        impact="affected",
        client_notice="told",
        client_told_on=TODAY(),
        client_told_how="Phone call",
        client_told_by=ctx.tech,
    )
    assert nc.client_notice == "told" and nc.client_told_on == TODAY()
    assert nc.client_told_how == "Phone call" and nc.client_told_by == ctx.tech


@names(
    "missing",
    [
        {"client_told_on": None},
        {"client_told_how": ""},
        {"client_told_how": "  "},
        {"client_told_by": None},
        {"client_told_on": "future"},
    ],
)
def test_telling_the_client_needs_date_how_and_teller(ctx, missing):
    values = {
        "client_notice": "told",
        "client_told_on": TODAY(),
        "client_told_how": "Phone call",
        "client_told_by": ctx.tech,
        **missing,
    }
    if values["client_told_on"] == "future":
        values["client_told_on"] = TODAY() + timedelta(days=1)
    with pytest.raises(ValidationError):
        impact(ctx, **values)
    assert current(ctx).impact == ""


def test_the_teller_must_be_active_staff(ctx):
    ctx.tech.is_active = False
    ctx.tech.save()
    with pytest.raises(ValidationError):
        impact(
            ctx,
            client_notice="told",
            client_told_on=TODAY(),
            client_told_how="Phone",
            client_told_by=ctx.tech,
        )


def test_a_portal_account_is_not_a_teller(ctx, portal):
    with pytest.raises(ValidationError):
        impact(
            ctx,
            client_notice="told",
            client_told_on=TODAY(),
            client_told_how="Phone",
            client_told_by=portal,
        )


def test_another_notice_clears_the_told_fields(ctx):
    impact(
        ctx,
        impact="affected",
        client_notice="told",
        client_told_on=TODAY(),
        client_told_how="Phone",
        client_told_by=ctx.tech,
    )
    nc = impact(
        ctx,
        impact="affected",
        client_notice="not_needed",
        client_told_on=TODAY(),
        client_told_how="Phone",
        client_told_by=ctx.tech,
    )
    assert nc.client_notice == "not_needed"
    assert nc.client_told_on is None and nc.client_told_how == "" and nc.client_told_by is None


def test_an_empty_notice_clears_the_told_fields(ctx):
    impact(
        ctx,
        impact="none",
        client_notice="told",
        client_told_on=TODAY(),
        client_told_how="Phone",
        client_told_by=ctx.tech,
    )
    nc = impact(ctx, impact="none", impact_assessment="Reviewed again; unchanged result.")
    assert nc.client_notice == "" and nc.client_told_by is None


# Actions


@names("kind", ["inactive", "portal", "no_role"])
def test_an_action_assignee_must_be_an_active_staff_account_with_a_role(
    ctx, portal, no_role, tech, kind
):
    assignee = {"inactive": tech, "portal": portal, "no_role": no_role}[kind]
    if kind == "inactive":
        tech.is_active = False
        tech.save()
    before = (audit_count(), CorrectiveAction.objects.count(), current(ctx).revision)
    with pytest.raises(ValidationError):
        services.add_action(
            actor=ctx.agent,
            nc=ctx.nc,
            expected_revision=current(ctx).revision,
            description="Do it.",
            assignee=assignee,
            due_on=TODAY(),
        )
    with pytest.raises(ValidationError):
        services.update_action(
            actor=ctx.agent,
            nc=ctx.nc,
            expected_revision=current(ctx).revision,
            action=ctx.action,
            description="Changed.",
            assignee=assignee,
            due_on=ctx.action.due_on,
            reason="Reassigned.",
        )
    assert (audit_count(), CorrectiveAction.objects.count(), current(ctx).revision) == before


def test_an_action_due_today_is_added(ctx):
    action = services.add_action(
        actor=ctx.agent,
        nc=ctx.nc,
        expected_revision=current(ctx).revision,
        description="Do it today.",
        assignee=ctx.tech,
        due_on=TODAY(),
    )
    assert action.due_on == TODAY() and action.created_by == ctx.agent


def test_an_action_due_in_the_past_is_refused_on_add(ctx):
    with pytest.raises(ValidationError):
        services.add_action(
            actor=ctx.agent,
            nc=ctx.nc,
            expected_revision=current(ctx).revision,
            description="Too late.",
            assignee=ctx.tech,
            due_on=TODAY() - timedelta(days=1),
        )


@names("values", [{"description": " "}, {"due_on": None}, {"assignee": None}])
def test_an_action_needs_text_a_date_and_an_assignee(ctx, values):
    args = {"description": "Do it.", "assignee": ctx.tech, "due_on": TODAY(), **values}
    with pytest.raises(ValidationError):
        services.add_action(
            actor=ctx.agent, nc=ctx.nc, expected_revision=current(ctx).revision, **args
        )


def _past_due(ctx):
    CorrectiveAction.objects.filter(pk=ctx.action.pk).update(due_on=TODAY() - timedelta(days=5))
    return fresh(ctx.action)


def _update(ctx, action, **values):
    args = {
        "description": action.description,
        "assignee": action.assignee,
        "due_on": action.due_on,
        "reason": "Corrected.",
        **values,
    }
    return services.update_action(
        actor=ctx.agent,
        nc=ctx.nc,
        expected_revision=current(ctx).revision,
        action=action,
        **args,
    )


def test_an_already_past_due_date_may_stay_when_the_action_is_updated(ctx):
    action = _past_due(ctx)
    updated = _update(ctx, action, description="New wording.")
    assert updated.due_on == action.due_on and updated.description == "New wording."


def test_a_changed_due_date_may_not_be_in_the_past(ctx):
    action = _past_due(ctx)
    with pytest.raises(ValidationError):
        _update(ctx, action, due_on=TODAY() - timedelta(days=1))


def test_a_past_due_action_gets_a_new_date_today_or_later(ctx):
    action = _past_due(ctx)
    assert _update(ctx, action, due_on=TODAY()).due_on == TODAY()


@names("reason", ["", " ", None])
def test_updating_an_action_needs_a_reason(ctx, reason):
    with pytest.raises(ValidationError):
        _update(ctx, ctx.action, description="Reworded.", reason=reason)


def test_updating_an_action_without_a_change_is_refused(ctx):
    with pytest.raises(ValidationError):
        _update(ctx, fresh(ctx.action))


def test_an_updated_action_audit_event_has_the_reason_and_both_values(ctx):
    _update(ctx, ctx.action, description="Reworded.", reason="Clearer.")
    event = AuditEvent.objects.filter(action="corrective_action.updated").get()
    assert event.reason == "Clearer."
    assert event.before["description"] == "Retrain the receiving bench."
    assert event.after["description"] == "Reworded."


@names("done", [True, False])
def test_a_finished_action_does_not_change(ctx, done):
    finish(ctx, done=done)
    action = fresh(ctx.action)
    with pytest.raises(ValidationError):
        _update(ctx, action, description="Reworded.")
    with pytest.raises(ValidationError):
        services.finish_action(
            actor=ctx.agent,
            nc=ctx.nc,
            expected_revision=current(ctx).revision,
            action=action,
            done=done,
            note="Again.",
        )
    assert row(action) == row(fresh(ctx.action))


@names("done", [True, False])
def test_finishing_an_action_needs_a_note(ctx, done):
    with pytest.raises(ValidationError):
        finish(ctx, done=done, note=" ")
    assert fresh(ctx.action).status == ActionStatus.OPEN


def test_finishing_an_action_records_who_when_and_the_note(ctx):
    action = finish(ctx, note="Training held on Monday.")
    assert action.finished_by == ctx.agent and action.finished_at is not None
    assert action.finish_note == "Training held on Monday."
    assert AuditEvent.objects.filter(action="corrective_action.done").count() == 1


def test_cancelling_an_action_writes_a_cancelled_event(ctx):
    finish(ctx, done=False, note="Superseded.")
    assert AuditEvent.objects.filter(action="corrective_action.cancelled").count() == 1


def test_an_action_of_another_record_is_not_found(ctx):
    other = _raise(ctx, ctx.lead, owner=ctx.agent)
    with pytest.raises(Http404):
        services.finish_action(
            actor=ctx.agent,
            nc=other,
            expected_revision=other.revision,
            action=ctx.action,
            done=True,
            note="Done.",
        )


# Opening from a complaint


def test_the_complaints_assignee_opens_a_record(ctx):
    nc = _open_from_complaint(ctx, ctx.agent)
    assert nc.source == "complaint" and nc.source_request == ctx.complaint
    assert nc.owner is None and nc.raised_by == ctx.agent
    assert nc.number == f"NC-{nc.pk:06d}"


def test_a_supervisor_opens_a_record_from_a_complaint(ctx):
    assert _open_from_complaint(ctx, ctx.lead).raised_by == ctx.lead


def test_staff_who_are_not_the_assignee_cannot_open_from_a_complaint(ctx):
    before = (audit_count(), Nonconformance.objects.count(), row(ctx.complaint))
    with pytest.raises(PermissionDenied):
        _open_from_complaint(ctx, ctx.tech)
    assert (audit_count(), Nonconformance.objects.count(), row(ctx.complaint)) == before


def test_opening_from_a_complaint_links_it_and_advances_the_ticket_by_one(ctx):
    before = fresh(ctx.complaint).revision
    nc = _open_from_complaint(ctx, ctx.agent)
    assert fresh(ctx.complaint).revision == before + 1
    link = TicketLink.objects.get(nonconformance=nc)
    assert link.request == ctx.complaint and link.removed_at is None
    assert nc.revision == 1


def test_opening_from_a_complaint_writes_one_ticket_event_with_ids_only(ctx):
    nc = _open_from_complaint(ctx, ctx.agent)
    event = fresh(ctx.complaint).entries.get(
        kind=EntryKind.EVENT, event_action="nonconformance_opened"
    )
    assert event.event_data == {"nonconformance_id": nc.pk, "number": nc.number}


def test_a_ticket_that_is_not_a_complaint_is_refused(ctx):
    ctx.complaint = ctx.other
    before = (audit_count(), Nonconformance.objects.count())
    with pytest.raises(ValidationError):
        _open_from_complaint(ctx, ctx.agent)
    assert (audit_count(), Nonconformance.objects.count()) == before


def test_a_stale_ticket_revision_is_refused(ctx):
    before = (audit_count(), Nonconformance.objects.count())
    with pytest.raises(ValidationError):
        _open_from_complaint(ctx, ctx.agent, rev=fresh(ctx.complaint).revision + 1)
    assert (audit_count(), Nonconformance.objects.count()) == before


def test_a_complaint_has_one_record_only(ctx):
    _open_from_complaint(ctx, ctx.agent)
    before = (audit_count(), row(ctx.complaint))
    with pytest.raises(ValidationError):
        _open_from_complaint(ctx, ctx.agent)
    assert Nonconformance.objects.filter(source_request=ctx.complaint).count() == 1
    assert (audit_count(), row(ctx.complaint)) == before


def test_a_reviewed_complaint_does_not_open_a_record(ctx):
    desk_services.decide_complaint(
        actor=ctx.agent,
        request=ctx.complaint,
        expected_revision=fresh(ctx.complaint).revision,
        decision=ComplaintDecision.INVESTIGATE,
        rationale="Repeated delay.",
    )
    desk_services.review_complaint(
        actor=ctx.lead,
        request=ctx.complaint,
        expected_revision=fresh(ctx.complaint).revision,
        note="Checked.",
    )
    before = (audit_count(), Nonconformance.objects.count())
    with pytest.raises(ValidationError, match="reviewed"):
        _open_from_complaint(ctx, ctx.agent)
    assert (audit_count(), Nonconformance.objects.count()) == before


def test_a_decided_complaint_without_a_review_still_opens_a_record(ctx):
    desk_services.decide_complaint(
        actor=ctx.agent,
        request=ctx.complaint,
        expected_revision=fresh(ctx.complaint).revision,
        decision=ComplaintDecision.INVESTIGATE,
        rationale="Repeated delay.",
    )
    assert _open_from_complaint(ctx, ctx.agent).source == "complaint"


def test_a_restricted_complaint_the_actor_cannot_read_is_a_404_and_writes_nothing(
    ctx, restricted_complaint_category
):
    secret = desk_services.create_request(
        actor=ctx.lead,
        category=restricted_complaint_category,
        subject="Restricted complaint TESTSUBJ",
        first_entry_kind="note",
        first_entry_body="Synthetic restricted entry.",
        channel="internal",
    )
    ctx.complaint = secret
    before = (
        audit_count(),
        Nonconformance.objects.count(),
        TicketLink.objects.count(),
        row(secret),
    )
    with pytest.raises(Http404):
        _open_from_complaint(ctx, ctx.agent)
    assert (
        audit_count(),
        Nonconformance.objects.count(),
        TicketLink.objects.count(),
        row(secret),
    ) == before


# Linked tickets


def test_link_ticket_advances_the_record_and_the_ticket_by_one(ctx):
    ticket_before = fresh(ctx.other).revision
    record_before = current(ctx).revision
    link = _link(ctx, ctx.agent)
    assert link.request == ctx.other and link.added_by == ctx.agent
    assert fresh(ctx.other).revision == ticket_before + 1
    assert current(ctx).revision == record_before + 1


def test_link_ticket_writes_one_ticket_event_with_ids_only(ctx):
    _link(ctx, ctx.agent)
    event = fresh(ctx.other).entries.get(kind=EntryKind.EVENT, event_action="nonconformance_linked")
    assert event.event_data == {"nonconformance_id": ctx.nc.pk, "number": ctx.nc.number}


def test_unlink_ticket_writes_one_ticket_event_with_ids_only(ctx):
    before = fresh(ctx.ticket).revision
    _unlink(ctx, ctx.agent)
    assert fresh(ctx.ticket).revision == before + 1
    event = fresh(ctx.ticket).entries.get(
        kind=EntryKind.EVENT, event_action="nonconformance_unlinked"
    )
    assert event.event_data == {"nonconformance_id": ctx.nc.pk, "number": ctx.nc.number}


def test_linking_a_ticket_twice_is_refused(ctx):
    _link(ctx, ctx.agent)
    before = (audit_count(), row(ctx.nc), row(ctx.other), TicketLink.objects.count())
    with pytest.raises(ValidationError):
        _link(ctx, ctx.agent)
    assert (audit_count(), row(ctx.nc), row(ctx.other), TicketLink.objects.count()) == before


def test_a_restricted_ticket_the_actor_cannot_read_is_a_404_and_writes_nothing(
    ctx, restricted_ticket
):
    ctx.other = restricted_ticket
    before = (audit_count(), row(ctx.nc), row(restricted_ticket), TicketLink.objects.count())
    with pytest.raises(Http404):
        _link(ctx, ctx.agent)
    assert (
        audit_count(),
        row(ctx.nc),
        row(restricted_ticket),
        TicketLink.objects.count(),
    ) == before


def test_a_supervisor_links_a_restricted_ticket(ctx, restricted_ticket):
    ctx.other = restricted_ticket
    assert _link(ctx, ctx.lead).request == restricted_ticket


def test_a_team_member_who_owns_the_record_links_a_restricted_ticket(
    ctx, restricted_ticket, quality_team
):
    from teams.models import TeamMembership

    TeamMembership.objects.create(team=restricted_ticket.team, user=ctx.agent)
    ctx.other = restricted_ticket
    assert _link(ctx, ctx.agent).request == restricted_ticket


def test_a_link_to_an_unreadable_ticket_cannot_be_removed_by_the_actor(
    ctx, restricted_ticket, agent
):
    ctx.other = restricted_ticket
    link = _link(ctx, ctx.lead)
    ctx.link = link
    before = (audit_count(), TicketLink.objects.filter(removed_at__isnull=False).count())
    with pytest.raises(Http404):
        _unlink(ctx, agent)
    assert (audit_count(), TicketLink.objects.filter(removed_at__isnull=False).count()) == before


def test_removing_a_link_keeps_the_row_with_who_and_why(ctx):
    link = _unlink(ctx, ctx.agent)
    kept = fresh(link)
    assert kept.removed_by == ctx.agent and kept.removed_at is not None
    assert kept.removal_reason == "Linked by mistake."
    assert TicketLink.objects.filter(pk=link.pk).exists()


@names("reason", ["", "  "])
def test_removing_a_link_needs_a_reason(ctx, reason):
    with pytest.raises(ValidationError):
        services.unlink_ticket(
            actor=ctx.agent,
            nc=ctx.nc,
            expected_revision=current(ctx).revision,
            link=ctx.link,
            reason=reason,
        )
    assert fresh(ctx.link).removed_at is None


def test_the_complaint_a_record_was_opened_from_cannot_be_unlinked(ctx):
    nc = _open_from_complaint(ctx, ctx.agent)
    link = TicketLink.objects.get(nonconformance=nc)
    before = (audit_count(), row(link), nc.revision)
    with pytest.raises(ValidationError):
        services.unlink_ticket(
            actor=ctx.lead, nc=nc, expected_revision=nc.revision, link=link, reason="Mistake."
        )
    assert (audit_count(), row(link), fresh(nc).revision) == before


def test_a_removed_link_is_not_removed_again(ctx):
    _unlink(ctx, ctx.agent)
    with pytest.raises(Http404):
        _unlink(ctx, ctx.agent)


def test_a_removed_ticket_can_be_linked_again(ctx):
    _unlink(ctx, ctx.agent)
    ctx.other = ctx.ticket
    link = _link(ctx, ctx.agent)
    assert link.removed_at is None
    assert TicketLink.objects.filter(nonconformance=ctx.nc, request=ctx.ticket).count() == 2


def test_a_link_of_another_record_is_not_found(ctx):
    other = _raise(ctx, ctx.lead, owner=ctx.agent)
    with pytest.raises(Http404):
        services.unlink_ticket(
            actor=ctx.agent,
            nc=other,
            expected_revision=other.revision,
            link=ctx.link,
            reason="Wrong record.",
        )


# Audit payloads


def test_no_audit_event_carries_a_ticket_subject_or_entry_text(ctx, plain_ticket):
    baseline = set(AuditEvent.objects.values_list("pk", flat=True))
    nc = _open_from_complaint(ctx, ctx.agent)
    _link(ctx, ctx.agent)
    link = TicketLink.objects.get(nonconformance=ctx.nc, request=ctx.other)
    ctx.link = link
    _unlink(ctx, ctx.agent)
    ctx.link = TicketLink.objects.get(nonconformance=ctx.nc, request=ctx.ticket)
    _unlink(ctx, ctx.agent)
    ctx.other = ctx.ticket
    _link(ctx, ctx.lead)
    services.set_owner(
        actor=ctx.lead, nc=nc, expected_revision=nc.revision, owner=ctx.agent, reason="Owner."
    )
    written = AuditEvent.objects.exclude(pk__in=baseline)
    assert written.count() >= 6
    payload = " ".join(json.dumps([e.before, e.after, e.reason]) for e in written)
    for text in (
        "Results late three times",
        "Practice manager reports late results",
        "Question about a report",
        "Caller asked about report timing",
        "Second question TESTSUBJ",
        "Second synthetic entry TESTBODY",
    ):
        assert text not in payload
    linked = written.filter(action="nonconformance.ticket_linked")
    assert linked.exists()
    for event in linked:
        assert set(event.after) == {"ticket_link_id", "request_id"}
