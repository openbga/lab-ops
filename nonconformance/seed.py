"""Demonstration seed for nonconformances (core.seeding): the module switched on, and four records
in different states, worked through the audited services at moments in the past (desk.story's
happened_at). Runs after the desk's story, whose complaints two of them concern. Synthetic data
only: example order, accession and report numbers, no patient details."""

import itertools
from contextlib import contextmanager
from datetime import timedelta
from unittest import mock

from django.db import transaction
from django.utils import timezone

from accounts.models import User
from core import modules
from core.models import LabModule
from desk.models import ServiceRequest
from desk.story import AGENT, LEAD, TECH

from . import services
from .models import Nonconformance, Status

MODULE = "nonconformance"


def seed_setup(ctx):
    """Switch the module on once. A laboratory that has switched it since keeps its choice."""
    if LabModule.objects.filter(key=MODULE).exists():
        return
    modules.set_enabled(
        actor=User.objects.get(username="demo_admin"),
        key=MODULE,
        enabled=True,
        expected_revision=0,
        reason="Demonstration: the example laboratory records nonconformances here.",
    )


def _fresh(obj):
    return type(obj).objects.get(pk=obj.pk)


@contextmanager
def happened_at(moment):
    """Run the services as if it were `moment`, a microsecond later at each reading, so the events
    of one step keep their order in the history."""
    ticks = itertools.count()
    with mock.patch(
        "django.utils.timezone.now",
        side_effect=lambda: moment + timedelta(microseconds=next(ticks)),
    ):
        yield


def _day(moment):
    return timezone.localdate(moment)


class _Clock:
    """Moments for a record's steps: each later than the last, never after `now`."""

    def __init__(self, start, now):
        self.moment, self.now = start, now

    def at(self, minutes=0, days=0):
        self.moment = min(
            self.moment + timedelta(days=days, minutes=minutes), self.now - timedelta(minutes=1)
        )
        return happened_at(self.moment)


def _step(clock, function, *, minutes=0, days=0, **kwargs):
    with transaction.atomic(), clock.at(minutes=minutes, days=days):
        nc = kwargs.get("nc")
        if nc is not None:
            kwargs["nc"] = _fresh(nc)
            kwargs["expected_revision"] = kwargs["nc"].revision
        return function(**kwargs)


def _from_complaint(users, now, ticket):
    """The waiting complaint (late results): opened from its complaint record, owned by the agent,
    an action under way."""
    agent, lead = users[AGENT], users[LEAD]
    recorded = (
        ticket.entries.filter(event_action="complaint_updated")
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    clock = _Clock(recorded or now - timedelta(hours=4), now)
    ticket = _fresh(ticket)
    with transaction.atomic(), clock.at(minutes=20):
        nc = services.open_from_complaint(
            actor=agent,
            request=ticket,
            expected_revision=ticket.revision,
            title="Results reached one clinic late three times in a month",
            description="The clinic received results late three times this month. The results "
            "were released on time; delivery missed the evening courier run.",
            severity="major",
            area="Client services and logistics",
            containment="Re-sent the late reports the same day and confirmed receipt by phone.",
        )
    _step(clock, services.set_owner, minutes=30, actor=lead, nc=nc, owner=agent)
    _step(
        clock, services.change_status, minutes=10, actor=agent, nc=nc, status=Status.INVESTIGATING
    )
    _step(
        clock,
        services.record_investigation,
        minutes=60,
        actor=agent,
        nc=nc,
        containment="Re-sent the late reports the same day and confirmed receipt by phone.",
        root_cause="The afternoon courier pickup was moved later without telling the laboratory.",
        similar_cases="Checked the other two courier routes this month: no change of pickup time.",
    )
    _step(
        clock,
        services.record_impact,
        minutes=30,
        actor=agent,
        nc=nc,
        impact="none",
        impact_assessment="Reviewed orders ORD-TEST-000042 to ORD-TEST-000050 for the clinic: "
        "results were unchanged and only delivered late.",
        client_notice="told",
        client_told_on=_day(clock.moment),
        client_told_how="Phone call to the practice manager",
        client_told_by=agent,
    )
    _step(
        clock,
        services.change_status,
        minutes=10,
        actor=agent,
        nc=nc,
        status=Status.CORRECTIVE_ACTION,
    )
    _step(
        clock,
        services.add_action,
        minutes=10,
        actor=agent,
        nc=nc,
        description="Agree a fixed afternoon pickup time with the courier service in writing.",
        assignee=users["demo_admin"],
        due_on=_day(now + timedelta(days=5)),
    )
    return nc


def _equipment(users, now):
    """A refrigerator excursion: owned by technical staff, one action done and one overdue."""
    tech, lead = users[TECH], users[LEAD]
    clock = _Clock(now - timedelta(days=12), now)
    with transaction.atomic(), clock.at():
        nc = services.raise_nonconformance(
            actor=tech,
            title="Specimen storage refrigerator above range overnight",
            description="The morning check found the specimen refrigerator at 9.1 °C; the "
            "acceptable range on its log is 2 to 8 °C.",
            source="equipment",
            severity="major",
            area="Specimen storage",
            containment="Moved the stored specimens to the backup refrigerator at 07:10.",
        )
    _step(clock, services.set_owner, minutes=45, actor=lead, nc=nc, owner=tech)
    _step(clock, services.change_status, minutes=5, actor=tech, nc=nc, status=Status.INVESTIGATING)
    _step(
        clock,
        services.record_investigation,
        minutes=120,
        actor=tech,
        nc=nc,
        containment="Moved the stored specimens to the backup refrigerator at 07:10.",
        root_cause="The door seal was worn and the door did not close fully.",
        similar_cases="Checked the seals of the two other refrigerators: both intact.",
    )
    _step(
        clock,
        services.record_impact,
        minutes=60,
        actor=tech,
        nc=nc,
        impact="none",
        impact_assessment="Reviewed accessions ACC-TEST-000301 to ACC-TEST-000318 stored "
        "overnight against the laboratory's stability table: none exceeded it.",
        client_notice="not_needed",
    )
    _step(
        clock,
        services.change_status,
        minutes=5,
        actor=tech,
        nc=nc,
        status=Status.CORRECTIVE_ACTION,
    )
    seal = _step(
        clock,
        services.add_action,
        minutes=5,
        actor=tech,
        nc=nc,
        description="Replace the refrigerator's door seal.",
        assignee=tech,
        due_on=_day(clock.moment + timedelta(days=2)),
    )
    _step(
        clock,
        services.add_action,
        minutes=5,
        actor=tech,
        nc=nc,
        description="Add a weekly door-seal check to the storage checklist.",
        assignee=tech,
        due_on=_day(now - timedelta(days=3)),
    )
    _step(
        clock,
        services.finish_action,
        days=1,
        actor=tech,
        nc=nc,
        action=seal,
        done=True,
        note="Seal replaced by the service engineer; door closes fully.",
    )
    return nc


def _closed(users, now, ticket):
    """The reviewed and closed complaint (report to a former contact): raised afterwards, the
    ticket linked, worked through to an independent closing."""
    agent, lead = users[AGENT], users[LEAD]
    last = ticket.entries.order_by("-created_at").values_list("created_at", flat=True).first()
    clock = _Clock(max(last, now - timedelta(days=4)), now)
    with transaction.atomic(), clock.at(minutes=30):
        nc = services.raise_nonconformance(
            actor=lead,
            title="Report sent to a former project contact",
            description="A report went to a contact who had left the client's project, because "
            "the distribution list still held them.",
            source="complaint",
            severity="major",
            area="Reporting",
            owner=agent,
        )
    _step(clock, services.link_ticket, minutes=5, actor=agent, nc=nc, request=ticket)
    _step(clock, services.change_status, minutes=5, actor=agent, nc=nc, status=Status.INVESTIGATING)
    _step(
        clock,
        services.record_investigation,
        minutes=60,
        actor=agent,
        nc=nc,
        containment="Removed the former contact and re-sent the report to the current contacts.",
        root_cause="Contact changes reached the laboratory by email and were not applied to the "
        "distribution list.",
        similar_cases="Compared every project distribution list with the client's contacts: no "
        "other former contacts.",
    )
    _step(
        clock,
        services.record_impact,
        minutes=30,
        actor=agent,
        nc=nc,
        impact="not_applicable",
        impact_assessment="Report RPT-TEST-000210 was correct; only its recipients were wrong.",
        client_notice="told",
        client_told_on=_day(clock.moment),
        client_told_how="Email to the project manager",
        client_told_by=agent,
    )
    _step(
        clock,
        services.change_status,
        minutes=5,
        actor=agent,
        nc=nc,
        status=Status.CORRECTIVE_ACTION,
    )
    action = _step(
        clock,
        services.add_action,
        minutes=5,
        actor=agent,
        nc=nc,
        description="Apply contact changes to distribution lists on the day they arrive.",
        assignee=agent,
        due_on=_day(clock.moment + timedelta(days=1)),
    )
    _step(
        clock,
        services.finish_action,
        minutes=240,
        actor=agent,
        nc=nc,
        action=action,
        done=True,
        note="Added to the client services checklist and explained to the team.",
    )
    _step(clock, services.change_status, minutes=10, actor=agent, nc=nc, status=Status.VERIFICATION)
    _step(
        clock,
        services.record_effectiveness,
        minutes=60,
        actor=agent,
        nc=nc,
        result="effective",
        note="Two later contact changes were applied the same day.",
    )
    _step(
        clock,
        services.close,
        minutes=30,
        actor=lead,
        nc=nc,
        note="Checked the checklist and the two later changes.",
    )
    return nc


def _unowned(users, now):
    """Raised yesterday by the agent and waiting for a supervisor to choose its owner."""
    clock = _Clock(now - timedelta(days=1), now)
    with transaction.atomic(), clock.at():
        return services.raise_nonconformance(
            actor=users[AGENT],
            title="Requisitions received without a collection time",
            description="Three requisitions from one site arrived without a collection time. "
            "Accessions ACC-TEST-000330 to ACC-TEST-000332 were held until the site confirmed it.",
            source="internal",
            severity="minor",
            area="Accessioning",
        )


def seed_records(ctx):
    if Nonconformance.objects.exists() or not modules.is_enabled(MODULE):
        return
    users = User.objects.in_bulk([AGENT, LEAD, TECH, "demo_admin"], field_name="username")
    stories = ctx.created.get("requests", {})
    now = timezone.now()
    _equipment(users, now)
    if (waiting := stories.get("complaint-awaiting")) is not None:
        _from_complaint(users, now, ServiceRequest.objects.get(pk=waiting.pk))
    if (closed := stories.get("complaint-closed")) is not None:
        _closed(users, now, ServiceRequest.objects.get(pk=closed.pk))
    _unowned(users, now)
