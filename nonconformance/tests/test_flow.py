"""The life of one record, end to end through the services: raised from a complaint, owned,
investigated, its impact decided, an action done, checked effective, closed by another supervisor
and reopened with a reason."""

from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from core.models import AuditEvent
from desk.models import EntryKind
from nonconformance import services
from nonconformance.models import ActionStatus, Nonconformance, Status


def fresh(obj):
    return type(obj).objects.get(pk=obj.pk)


@pytest.mark.django_db
def test_a_record_from_complaint_to_closing(module_on, agent, lead, lead2, complaint):
    nc = services.open_from_complaint(
        actor=agent,
        request=complaint,
        expected_revision=complaint.revision,
        title="Late results for one client",
        description="Results reached the clinic late three times.",
        severity="major",
        area="Client services",
    )
    assert nc.number == f"NC-{nc.pk:06d}" and nc.source == "complaint"
    ticket = fresh(complaint)
    assert ticket.revision == complaint.revision + 1
    assert ticket.entries.filter(
        kind=EntryKind.EVENT, event_action="nonconformance_opened"
    ).exists()
    with pytest.raises(ValidationError):  # one per complaint
        services.open_from_complaint(
            actor=agent,
            request=ticket,
            expected_revision=ticket.revision,
            title="Again",
            description="Again",
            severity="minor",
        )

    with pytest.raises(PermissionDenied):  # the agent is not the owner and not a supervisor
        services.change_status(
            actor=agent, nc=nc, expected_revision=nc.revision, status=Status.INVESTIGATING
        )
    nc = services.set_owner(actor=lead, nc=nc, expected_revision=nc.revision, owner=agent)
    nc = services.change_status(
        actor=agent, nc=nc, expected_revision=nc.revision, status=Status.INVESTIGATING
    )
    nc = services.record_investigation(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        containment="Reports re-sent the same day.",
        root_cause="Courier pickup moved later in the day.",
    )
    nc = services.record_impact(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        impact="none",
        impact_assessment="Reviewed ORD-TEST-000042 to 000050: results unchanged, only late.",
        client_notice="told",
        client_told_on=timezone.localdate(),
        client_told_how="Phone call to the practice manager",
        client_told_by=agent,
    )
    action = services.add_action(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        description="Restore the afternoon pickup time.",
        assignee=agent,
        due_on=timezone.localdate() + timedelta(days=7),
    )
    nc = fresh(nc)
    nc = services.change_status(
        actor=agent, nc=nc, expected_revision=nc.revision, status=Status.VERIFICATION
    )
    with pytest.raises(ValidationError):  # an open action blocks "effective"
        services.record_effectiveness(
            actor=agent, nc=nc, expected_revision=nc.revision, result="effective", note="Ok"
        )
    services.finish_action(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        action=action,
        done=True,
        note="Pickup back at 15:00.",
    )
    assert fresh(action).status == ActionStatus.DONE
    nc = fresh(nc)
    nc = services.record_effectiveness(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        result="effective",
        note="No late results in the next two weeks.",
    )
    with pytest.raises(PermissionDenied):  # the agent owns it and is no supervisor
        services.close(actor=agent, nc=nc, expected_revision=nc.revision, note="Done")
    stale = nc.revision
    nc = services.close(actor=lead, nc=nc, expected_revision=nc.revision, note="Checked.")
    assert nc.status == Status.CLOSED and nc.closed_by == lead
    with pytest.raises(ValidationError):
        services.reopen(actor=lead2, nc=nc, expected_revision=stale, reason="Stale page")
    nc = services.reopen(
        actor=lead2, nc=nc, expected_revision=nc.revision, reason="Late again this week."
    )
    assert nc.status == Status.INVESTIGATING and nc.effectiveness == ""
    actions = list(
        AuditEvent.objects.filter(object_type="nonconformance.nonconformance").values_list(
            "action", flat=True
        )
    )
    assert actions[0] == "nonconformance.raised" and actions[-1] == "nonconformance.reopened"
    assert Nonconformance.objects.count() == 1
