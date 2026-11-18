"""Regression tests for nonconformance.selectors: who reads, the module switch, linked-ticket
visibility, and the action filters. All data is synthetic."""

from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.utils import timezone

from core.models import LabModule
from nonconformance import selectors, services
from nonconformance.models import CorrectiveAction, Nonconformance, Status
from teams.models import TeamMembership

pytestmark = pytest.mark.django_db


def today():
    return timezone.localdate()


def fresh(obj):
    return type(obj).objects.get(pk=obj.pk)


@pytest.fixture
def record(module_on, lead, agent):
    return services.raise_nonconformance(
        actor=lead,
        title="Pipette calibration overdue",
        description="Two pipettes were used past their calibration date.",
        source="equipment",
        severity="minor",
        owner=agent,
    )


def add_action(nc, actor, assignee, due_on):
    nc = fresh(nc)
    return services.add_action(
        actor=actor,
        nc=nc,
        expected_revision=nc.revision,
        description="Recalibrate.",
        assignee=assignee,
        due_on=due_on,
    )


def backdate(action, days):
    CorrectiveAction.objects.filter(pk=action.pk).update(due_on=today() - timedelta(days=days))
    return fresh(action)


def finish(nc, actor, action, done=True):
    nc = fresh(nc)
    return services.finish_action(
        actor=actor,
        nc=nc,
        expected_revision=nc.revision,
        action=action,
        done=done,
        note="Finished.",
    )


def link(nc, actor, ticket):
    nc = fresh(nc)
    return services.link_ticket(actor=actor, nc=nc, expected_revision=nc.revision, request=ticket)


# staff_nonconformances and staff_actions


@pytest.mark.parametrize("who", ["agent", "tech", "lead", "admin"])
def test_every_staff_role_reads_every_record(record, request, who):
    assert list(selectors.staff_nonconformances(request.getfixturevalue(who))) == [record]


@pytest.mark.parametrize("who", ["agent", "tech", "lead", "admin"])
def test_every_staff_role_reads_every_action(record, lead, tech, request, who):
    action = add_action(record, lead, tech, today())
    assert list(selectors.staff_actions(request.getfixturevalue(who))) == [action]


@pytest.mark.parametrize("who", ["portal", "no_role"])
def test_portal_and_roleless_accounts_cannot_read_records(record, request, who):
    with pytest.raises(PermissionDenied):
        selectors.staff_nonconformances(request.getfixturevalue(who))


@pytest.mark.parametrize("who", ["portal", "no_role"])
def test_portal_and_roleless_accounts_cannot_read_actions(record, request, who):
    with pytest.raises(PermissionDenied):
        selectors.staff_actions(request.getfixturevalue(who))


def test_reading_records_is_a_404_while_the_module_is_off(record, lead):
    LabModule.objects.filter(key="nonconformance").update(is_enabled=False)
    with pytest.raises(Http404):
        selectors.staff_nonconformances(lead)


def test_reading_actions_is_a_404_while_the_module_is_off(record, lead):
    LabModule.objects.filter(key="nonconformance").update(is_enabled=False)
    with pytest.raises(Http404):
        selectors.staff_actions(lead)


def test_reading_is_a_404_when_the_module_has_no_row(lead):
    with pytest.raises(Http404):
        selectors.staff_nonconformances(lead)


def test_records_reappear_unchanged_when_the_module_is_switched_back_on(record, lead):
    before = Nonconformance.objects.filter(pk=record.pk).values().get()
    LabModule.objects.filter(key="nonconformance").update(is_enabled=False)
    LabModule.objects.filter(key="nonconformance").update(is_enabled=True)
    assert list(selectors.staff_nonconformances(lead)) == [record]
    assert Nonconformance.objects.filter(pk=record.pk).values().get() == before


# linked_tickets and for_ticket


def test_linked_tickets_lists_current_links_oldest_first(record, agent, plain_ticket, complaint):
    first = link(record, agent, plain_ticket)
    second = link(record, agent, complaint)
    assert list(selectors.linked_tickets(agent, record)) == [first, second]


def test_linked_tickets_leaves_out_a_removed_link(record, agent, plain_ticket):
    made = link(record, agent, plain_ticket)
    nc = fresh(record)
    services.unlink_ticket(
        actor=agent, nc=nc, expected_revision=nc.revision, link=made, reason="Wrong ticket."
    )
    assert list(selectors.linked_tickets(agent, record)) == []


def test_linked_tickets_hides_a_restricted_ticket_from_a_non_member(
    record, lead, agent, restricted_ticket
):
    made = link(record, lead, restricted_ticket)
    assert list(selectors.linked_tickets(agent, record)) == []
    assert list(selectors.linked_tickets(lead, record)) == [made]


def test_linked_tickets_shows_a_restricted_ticket_to_a_supervisor(
    record, lead, lead2, restricted_ticket
):
    made = link(record, lead, restricted_ticket)
    assert list(selectors.linked_tickets(lead2, record)) == [made]


def test_linked_tickets_shows_a_restricted_ticket_to_a_team_member(
    record, lead, agent, restricted_ticket
):
    made = link(record, lead, restricted_ticket)
    TeamMembership.objects.create(team=restricted_ticket.team, user=agent)
    assert list(selectors.linked_tickets(agent, record)) == [made]


def test_linked_tickets_hides_only_the_unreadable_ones(
    record, lead, agent, plain_ticket, restricted_ticket
):
    open_link = link(record, lead, plain_ticket)
    link(record, lead, restricted_ticket)
    assert list(selectors.linked_tickets(agent, record)) == [open_link]


def test_linked_tickets_refuses_a_portal_account(record, portal):
    with pytest.raises(PermissionDenied):
        selectors.linked_tickets(portal, record)


def test_for_ticket_lists_the_records_linked_to_a_ticket(record, agent, plain_ticket):
    link(record, agent, plain_ticket)
    assert list(selectors.for_ticket(agent, plain_ticket)) == [record]


def test_for_ticket_is_empty_when_the_module_is_off(record, agent, plain_ticket):
    link(record, agent, plain_ticket)
    LabModule.objects.filter(key="nonconformance").update(is_enabled=False)
    assert list(selectors.for_ticket(agent, plain_ticket)) == []


def test_for_ticket_is_empty_when_the_module_has_no_row(agent, plain_ticket):
    assert list(selectors.for_ticket(agent, plain_ticket)) == []


def test_for_ticket_is_empty_for_a_portal_account(record, agent, portal, plain_ticket):
    link(record, agent, plain_ticket)
    assert list(selectors.for_ticket(portal, plain_ticket)) == []


def test_for_ticket_leaves_out_a_removed_link(record, agent, plain_ticket):
    made = link(record, agent, plain_ticket)
    nc = fresh(record)
    services.unlink_ticket(
        actor=agent, nc=nc, expected_revision=nc.revision, link=made, reason="Wrong ticket."
    )
    assert list(selectors.for_ticket(agent, plain_ticket)) == []


def test_for_ticket_lists_a_record_once_even_after_a_relink(record, agent, plain_ticket):
    made = link(record, agent, plain_ticket)
    nc = fresh(record)
    services.unlink_ticket(
        actor=agent, nc=nc, expected_revision=nc.revision, link=made, reason="Mistake."
    )
    link(record, agent, plain_ticket)
    assert list(selectors.for_ticket(agent, plain_ticket)) == [record]


def test_module_on_for_needs_a_staff_role_and_the_module_on(record, agent, portal, no_role):
    assert selectors.module_on_for(agent) is True
    assert selectors.module_on_for(portal) is False
    assert selectors.module_on_for(no_role) is False
    LabModule.objects.filter(key="nonconformance").update(is_enabled=False)
    assert selectors.module_on_for(agent) is False


# mine


def test_mine_covers_the_owner(record, agent):
    assert list(selectors.mine(Nonconformance.objects.all(), agent)) == [record]


def test_mine_covers_the_assignee_of_an_open_action(record, lead, tech):
    add_action(record, lead, tech, today())
    assert list(selectors.mine(Nonconformance.objects.all(), tech)) == [record]


def test_mine_leaves_out_the_assignee_of_a_done_action(record, lead, tech):
    action = add_action(record, lead, tech, today())
    finish(record, lead, action)
    assert list(selectors.mine(Nonconformance.objects.all(), tech)) == []


def test_mine_leaves_out_the_assignee_of_a_cancelled_action(record, lead, tech):
    action = add_action(record, lead, tech, today())
    finish(record, lead, action, done=False)
    assert list(selectors.mine(Nonconformance.objects.all(), tech)) == []


def test_mine_leaves_out_other_staff(record, helper):
    assert list(selectors.mine(Nonconformance.objects.all(), helper)) == []


def test_mine_lists_a_record_once_for_an_owner_with_several_open_actions(record, lead, agent):
    add_action(record, lead, agent, today())
    add_action(record, lead, agent, today())
    assert list(selectors.mine(Nonconformance.objects.all(), agent)) == [record]


@pytest.fixture
def helper(make_user):
    return make_user("helper", "technical")


# with_action_counts and overdue


def flags(nc):
    row = selectors.with_action_counts(Nonconformance.objects.all()).get(pk=nc.pk)
    return row.has_open_actions, row.has_overdue_actions


def test_a_record_without_actions_has_neither_flag(record):
    assert flags(record) == (False, False)


def test_an_open_action_due_today_is_open_but_not_overdue(record, lead, tech):
    add_action(record, lead, tech, today())
    assert flags(record) == (True, False)


def test_an_open_action_due_in_the_future_is_not_overdue(record, lead, tech):
    add_action(record, lead, tech, today() + timedelta(days=5))
    assert flags(record) == (True, False)


def test_an_open_action_due_before_today_is_overdue(record, lead, tech):
    backdate(add_action(record, lead, tech, today()), 1)
    assert flags(record) == (True, True)


def test_a_done_action_due_before_today_is_not_open_or_overdue(record, lead, tech):
    action = add_action(record, lead, tech, today())
    finish(record, lead, action)
    backdate(action, 3)
    assert flags(record) == (False, False)


def test_a_cancelled_action_due_before_today_is_not_open_or_overdue(record, lead, tech):
    action = add_action(record, lead, tech, today())
    finish(record, lead, action, done=False)
    backdate(action, 3)
    assert flags(record) == (False, False)


def test_with_action_counts_takes_the_day_to_count_from(record, lead, tech):
    add_action(record, lead, tech, today())
    rows = selectors.with_action_counts(
        Nonconformance.objects.all(), today=today() + timedelta(days=1)
    )
    assert rows.get(pk=record.pk).has_overdue_actions is True


def test_with_action_counts_does_not_duplicate_rows(record, lead, tech):
    for _ in range(3):
        add_action(record, lead, tech, today())
    assert selectors.with_action_counts(Nonconformance.objects.all()).count() == 1


def test_overdue_lists_only_open_actions_due_before_today(record, lead, tech):
    late = backdate(add_action(record, lead, tech, today()), 2)
    add_action(record, lead, tech, today())
    add_action(record, lead, tech, today() + timedelta(days=2))
    done = add_action(record, lead, tech, today())
    finish(record, lead, done)
    backdate(done, 4)
    assert list(selectors.overdue(CorrectiveAction.objects.all())) == [late]


def test_overdue_takes_the_day_to_count_from(record, lead, tech):
    action = add_action(record, lead, tech, today())
    assert list(selectors.overdue(CorrectiveAction.objects.all())) == []
    assert list(
        selectors.overdue(CorrectiveAction.objects.all(), today=today() + timedelta(days=1))
    ) == [action]


# active


def test_active_keeps_working_statuses_and_leaves_out_a_closed_record(record, lead):
    assert list(selectors.active(Nonconformance.objects.all())) == [record]
    Nonconformance.objects.filter(pk=record.pk).update(status=Status.VERIFICATION)
    assert list(selectors.active(Nonconformance.objects.all())) == [record]
    Nonconformance.objects.filter(pk=record.pk).update(
        status=Status.CLOSED, closed_at=timezone.now(), closed_by=lead
    )
    assert list(selectors.active(Nonconformance.objects.all())) == []
