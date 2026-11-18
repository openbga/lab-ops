"""Closing resolved tickets automatically after the laboratory's period (the scheduled job in
desk.jobs and desk.services.close_resolved_automatically). Synthetic data."""

from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from clients.models import PortalAccess
from core.models import AuditEvent
from desk import services, timeline
from desk.jobs import close_resolved_tickets
from desk.models import ComplaintDecision, EntryKind, ServiceRequest, Status
from jobs.actor import system_account
from jobs.models import JobSettings
from teams.models import ensure_default_team

from .test_contract import new_request

pytestmark = pytest.mark.django_db


def set_period(days):
    JobSettings.objects.update_or_create(pk=1, defaults={"close_resolved_after_days": days})


def resolve(actor, request, *, days_ago):
    request.refresh_from_db()
    services.change_status(
        actor=actor,
        request=request,
        expected_revision=request.revision,
        status=Status.RESOLVED,
        summary="Fixed.",
    )
    ServiceRequest.objects.filter(pk=request.pk).update(
        resolved_at=timezone.now() - timedelta(days=days_ago, minutes=1)
    )
    request.refresh_from_db()
    return request


@pytest.fixture
def resolved(agent, category):
    """A ticket resolved 10 days and a minute ago."""
    return resolve(agent, new_request(agent, category), days_ago=10)


def status_of(request):
    request.refresh_from_db()
    return request.status


def test_nothing_closes_without_a_period(resolved):
    assert not JobSettings.objects.filter(close_resolved_after_days__isnull=False).exists()
    assert close_resolved_tickets() == {"closed": 0}
    assert status_of(resolved) == Status.RESOLVED
    set_period(None)
    assert close_resolved_tickets() == {"closed": 0}
    assert status_of(resolved) == Status.RESOLVED
    assert not AuditEvent.objects.filter(action="jobs.system_account_created").exists()


def test_a_ticket_resolved_longer_ago_than_the_period_is_closed(resolved):
    set_period(7)
    revision = resolved.revision
    assert close_resolved_tickets() == {"closed": 1}
    resolved.refresh_from_db()
    robot = system_account()
    assert resolved.status == Status.CLOSED
    assert resolved.closed_at is not None
    assert resolved.revision == revision + 1
    event = resolved.entries.filter(kind=EntryKind.EVENT, event_action="status_changed").latest(
        "pk"
    )
    assert event.author_id == robot.pk
    assert event.event_data == {
        "automatic": True,
        "after_days": 7,
        "old": "resolved",
        "new": "closed",
    }
    audit = AuditEvent.objects.get(action="request.status_changed", actor=robot)
    assert audit.object_id == str(resolved.pk)
    assert audit.before == {"status": "resolved"} and audit.after == {"status": "closed"}
    assert audit.reason == "Closed automatically 7 days after it was resolved."
    (described,) = timeline.describe([event])
    assert "closed automatically 7 days after it was resolved" in described.phrase
    assert described.phrase.startswith("Status: ")


@pytest.mark.parametrize(
    ("days", "unit"), [(1, "1 day"), (3, "3 days")], ids=["singular", "plural"]
)
def test_the_wording_follows_the_period(agent, category, days, unit):
    request = resolve(agent, new_request(agent, category), days_ago=days)
    set_period(days)
    assert close_resolved_tickets() == {"closed": 1}
    audit = AuditEvent.objects.get(action="request.status_changed", actor=system_account())
    assert audit.reason == f"Closed automatically {unit} after it was resolved."
    event = request.entries.filter(kind=EntryKind.EVENT, event_action="status_changed").latest("pk")
    assert event.event_data["after_days"] == days
    (described,) = timeline.describe([event])
    assert f"closed automatically {unit} after it was resolved" in described.phrase


def test_tickets_that_are_not_due_are_left_alone(agent, category):
    set_period(7)
    recent = resolve(agent, new_request(agent, category), days_ago=3)
    open_one = new_request(agent, category)
    closed = resolve(agent, new_request(agent, category), days_ago=30)
    services.change_status(
        actor=agent, request=closed, expected_revision=closed.revision, status=Status.CLOSED
    )
    closed.refresh_from_db()
    closed_revision = closed.revision
    waiting = new_request(agent, category)
    services.change_status(
        actor=agent,
        request=waiting,
        expected_revision=waiting.revision,
        status=Status.WAITING_CLIENT,
    )
    ServiceRequest.objects.filter(pk=waiting.pk).update(resolved_at=None)
    before = {r.pk: r.revision for r in ServiceRequest.objects.all()}
    assert close_resolved_tickets() == {"closed": 0}
    assert {r.pk: r.revision for r in ServiceRequest.objects.all()} == before
    assert status_of(recent) == Status.RESOLVED
    assert status_of(open_one) == Status.OPEN
    assert status_of(waiting) == Status.WAITING_CLIENT
    closed.refresh_from_db()
    assert closed.status == Status.CLOSED and closed.revision == closed_revision
    assert not AuditEvent.objects.filter(reason__startswith="Closed automatically").exists()


def test_the_boundary_is_the_period_exactly(agent, category):
    set_period(7)
    now = timezone.now()
    exact = resolve(agent, new_request(agent, category), days_ago=7)
    just_short = resolve(agent, new_request(agent, category), days_ago=7)
    ServiceRequest.objects.filter(pk=exact.pk).update(resolved_at=now - timedelta(days=7))
    ServiceRequest.objects.filter(pk=just_short.pk).update(
        resolved_at=now - timedelta(days=7) + timedelta(seconds=1)
    )
    assert close_resolved_tickets(now=now) == {"closed": 1}
    assert status_of(exact) == Status.CLOSED
    assert status_of(just_short) == Status.RESOLVED


@pytest.fixture
def complaint(agent, category, make_user, join):
    join(agent, ensure_default_team())
    lead = make_user("first_lead", "supervisor")
    request = new_request(agent, category, assignee=agent)
    services.set_complaint(
        actor=agent, request=request, expected_revision=1, is_complaint=True, reason="Late twice"
    )
    request.refresh_from_db()
    services.decide_complaint(
        actor=agent,
        request=request,
        expected_revision=request.revision,
        decision=ComplaintDecision.INVESTIGATE,
        rationale="Repeated delay.",
    )
    return resolve(agent, request, days_ago=10), lead


def test_a_complaint_without_a_review_stays_resolved(complaint):
    # Not even a candidate: it cannot close until its review is recorded (review R1-12).
    request, _ = complaint
    set_period(7)
    revision = request.revision
    assert close_resolved_tickets() == {"closed": 0}
    request.refresh_from_db()
    assert request.status == Status.RESOLVED and request.revision == revision
    assert not AuditEvent.objects.filter(reason__startswith="Closed automatically").exists()


def test_a_reviewed_complaint_closes(complaint):
    request, lead = complaint
    services.review_complaint(
        actor=lead, request=request, expected_revision=request.revision, note="Checked."
    )
    set_period(7)
    assert close_resolved_tickets() == {"closed": 1}
    assert status_of(request) == Status.CLOSED


def test_a_complaint_awaiting_review_does_not_stop_other_tickets(complaint, agent, category):
    other = resolve(agent, new_request(agent, category), days_ago=10)
    set_period(7)
    assert close_resolved_tickets() == {"closed": 1}
    assert status_of(other) == Status.CLOSED


def test_a_member_of_staff_who_opened_the_ticket_before_the_job_ran_is_refused(agent, resolved):
    set_period(7)
    seen = resolved.revision
    close_resolved_tickets()
    with pytest.raises(ValidationError) as error:
        services.change_status(
            actor=agent,
            request=resolved,
            expected_revision=seen,
            status=Status.WAITING_CLIENT,
            reason="Needs more.",
        )
    assert "This ticket changed" in str(error.value)
    assert status_of(resolved) == Status.CLOSED


def test_only_the_scheduled_jobs_account_may_close_this_way(agent, make_user, resolved):
    set_period(7)
    for actor in (agent, make_user("boss", "admin"), make_user("lead", "supervisor")):
        with pytest.raises(PermissionDenied), transaction.atomic():
            services.close_resolved_automatically(actor=actor, request_id=resolved.pk)
    assert status_of(resolved) == Status.RESOLVED


def test_the_service_returns_not_due_when_the_period_was_cleared(resolved):
    robot = system_account()
    set_period(None)
    with transaction.atomic():
        assert (
            services.close_resolved_automatically(actor=robot, request_id=resolved.pk) == "not_due"
        )
    assert status_of(resolved) == Status.RESOLVED


def test_the_service_decides_again_from_the_ticket_as_it_is(agent, resolved):
    robot = system_account()
    set_period(7)
    resolved.refresh_from_db()
    services.change_status(
        actor=agent,
        request=resolved,
        expected_revision=resolved.revision,
        status=Status.OPEN,
        reason="Reopened by hand.",
    )
    with transaction.atomic():
        assert (
            services.close_resolved_automatically(actor=robot, request_id=resolved.pk) == "not_due"
        )
    assert status_of(resolved) == Status.OPEN


def test_a_client_reply_that_reopened_the_ticket_is_not_closed(make_user, resolved, clinic):
    set_period(7)
    portal = make_user("clinic_raiser", client=clinic)
    PortalAccess.objects.create(user=portal, client_id=portal.client_id, whole_organization=True)
    ServiceRequest.objects.filter(pk=resolved.pk).update(client=clinic)
    resolved.refresh_from_db()
    services.add_entry(
        actor=portal,
        request=resolved,
        kind=EntryKind.REPLY,
        body="Still not right.",
        expected_revision=resolved.revision,
    )
    resolved.refresh_from_db()
    assert resolved.status == Status.OPEN and resolved.resolved_at is None
    assert close_resolved_tickets() == {"closed": 0}
    assert status_of(resolved) == Status.OPEN


def test_the_system_account_is_created_only_when_something_is_due(agent, category):
    set_period(7)
    resolve(agent, new_request(agent, category), days_ago=1)
    assert close_resolved_tickets() == {"closed": 0}
    assert not AuditEvent.objects.filter(action="jobs.system_account_created").exists()
