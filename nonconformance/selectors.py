"""Reads of nonconformances. Every page, list, count and lookup of the module starts here: the
reader must be a staff account and the module on (404 otherwise, so its addresses do not exist
while it is off). Every staff role reads every record. A linked ticket shows only to a reader who
may read that ticket now (desk.selectors.staff_requests); one in a restricted category the
reader cannot open is left out without a trace."""

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from accounts.roles import ROLES, has_role, require_staff
from core.modules import enabled_for, is_enabled, require_enabled
from desk.selectors import staff_requests

from .models import ACTIVE_STATUSES, ActionStatus, CorrectiveAction, Nonconformance, TicketLink

MODULE = "nonconformance"


def staff_nonconformances(user):
    require_staff(user)
    require_enabled(MODULE)
    return Nonconformance.objects.all()


def staff_actions(user):
    """Actions of every record, for the actions list."""
    require_staff(user)
    require_enabled(MODULE)
    return CorrectiveAction.objects.all()


def overdue(actions, today=None):
    """Open actions whose due date has passed (laboratory dates, core.due)."""
    today = today or timezone.localdate()
    return actions.filter(status=ActionStatus.OPEN, due_on__lt=today)


def with_action_counts(records, today=None):
    """Annotate `open_actions` and `overdue_actions` booleans for list rows."""
    today = today or timezone.localdate()
    open_actions = CorrectiveAction.objects.filter(
        nonconformance=OuterRef("pk"), status=ActionStatus.OPEN
    )
    return records.annotate(
        has_open_actions=Exists(open_actions),
        has_overdue_actions=Exists(open_actions.filter(due_on__lt=today)),
    )


def mine(records, user):
    """Records `user` owns or holds an open action on."""
    holds = CorrectiveAction.objects.filter(
        nonconformance=OuterRef("pk"), status=ActionStatus.OPEN, assignee=user
    )
    return records.filter(Q(owner=user) | Exists(holds))


def active(records):
    return records.filter(status__in=ACTIVE_STATUSES)


def linked_tickets(user, nc, include_removed=False):
    """The record's current ticket links that `user` may read now, oldest first (with removed
    ones too when `include_removed`)."""
    links = TicketLink.objects.filter(nonconformance=nc)
    if not include_removed:
        links = links.filter(removed_at__isnull=True)
    return (
        links.filter(request__in=staff_requests(user).values("pk"))
        .select_related("request", "added_by")
        .order_by("added_at", "pk")
    )


def module_on_for(user, request=None):
    """Whether pages outside the module (a ticket, the menu) show its records to `user`; with
    `request`, in that request's one reading of the switch (core.modules.states_for)."""
    return has_role(user, *ROLES) and (
        enabled_for(request, MODULE) if request is not None else is_enabled(MODULE)
    )


def linked_records(request):
    """The records currently linked to a ticket, oldest first. The caller has authorized the
    ticket and checked the module's switch once for its page."""
    return (
        Nonconformance.objects.filter(
            ticket_links__request=request, ticket_links__removed_at__isnull=True
        )
        .distinct()
        .order_by("raised_at", "pk")
    )


def for_ticket(user, request):
    """The records currently linked to a ticket, for its page: none while the module is off. The
    caller has already authorized the ticket (staff_requests)."""
    if not module_on_for(user):
        return Nonconformance.objects.none()
    return (
        Nonconformance.objects.filter(
            ticket_links__request=request, ticket_links__removed_at__isnull=True
        )
        .distinct()
        .order_by("raised_at", "pk")
    )
