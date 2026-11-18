"""Assignment locks, deactivation and complaint review eligibility."""

import threading
import time

import pytest
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.models import AuditEvent
from desk import lists, services
from desk.models import ComplaintReview, ServiceRequest, Status
from desk.tests.test_ticket_actions import change, decide
from teams import services as team_services
from teams.models import TeamMembership, ensure_default_team

from .test_contract import new_request

pytestmark = pytest.mark.django_db


def test_inherited_review_counts_every_decision_author(make_user, category):
    """An approval stored by an earlier release, given by someone who had
    recorded an earlier version of the decision, does not count at closing or in the queue."""
    former = make_user("initial_decider", "supervisor")
    latest = make_user("last_decider", "supervisor")
    third = make_user("independent_reviewer", "supervisor")
    record = new_request(former, category)
    change(record, services.set_complaint, former, is_complaint=True, reason="Complaint")
    decide(record, former)
    decide(record, latest)
    # The stored approval of an open complaint inherited from the previous release.
    ComplaintReview.objects.filter(request=record).update(
        reviewed_at=timezone.now(), reviewed_by=former
    )
    review = ComplaintReview.objects.get(request=record)
    assert former.pk in services.decision_author_ids(record, review)
    assert not services.review_counts(record, review)

    # The needs-review queue lists it again, and offers its review to an independent supervisor.
    assert record in lists.view_queryset(third, {"view": "complaints-review"})
    awaiting = ServiceRequest.objects.filter(pk=record.pk)
    assert awaiting.filter(lists.awaiting_review_by(third)).exists()
    assert not awaiting.filter(lists.awaiting_review_by(former)).exists()

    # Closing is refused until that supervisor reviews it.
    change(record, services.change_status, latest, status=Status.RESOLVED, summary="Done")
    with pytest.raises(ValidationError, match="new review"):
        change(record, services.change_status, latest, status=Status.CLOSED)
    change(record, services.review_complaint, third, note="Fresh independent review")
    change(record, services.change_status, latest, status=Status.CLOSED)
    record.refresh_from_db()
    assert record.status == Status.CLOSED


def test_assignee_validation_locks_the_membership_row(agent):
    """The membership row stays locked, so its
    removal (which locks it for update) waits for the assignment's transaction."""
    team = ensure_default_team()
    TeamMembership.objects.create(team=team, user=agent)
    with CaptureQueriesContext(connection) as statements:
        assert services._current_assignee(agent, team).pk == agent.pk
    member_reads = [
        q["sql"].upper() for q in statements if TeamMembership._meta.db_table in q["sql"]
    ]
    assert member_reads
    assert all("FOR NO KEY UPDATE" in sql for sql in member_reads)


def _wait_until_blocked(seconds=10):
    """Whether another session is waiting for a lock within `seconds`."""
    deadline = time.monotonic() + seconds
    with connection.cursor() as cursor:
        while time.monotonic() < deadline:
            cursor.execute(
                "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE NOT l.granted AND a.datname = current_database()"
            )
            if cursor.fetchone()[0]:
                return True
            time.sleep(0.05)
    return False


@pytest.mark.django_db(transaction=True)
def test_removal_during_an_assignment_waits_and_returns_it_to_the_queue(
    agent, make_user, category, join
):
    """An assignment validated the membership and has not committed; the member's removal starts
    in another session. It waits for the membership row, then finds the committed assignment
    and returns the request to the queue, so nobody is left assigned to a team they left."""
    lead = make_user("membership_lead", "supervisor")
    team = ensure_default_team()
    membership = join(agent, team)
    record = new_request(lead, category)
    outcome = {}

    def remove():
        try:
            outcome["released"] = team_services.remove_member(
                actor=lead, membership=membership, reason="Left the team"
            )
        except Exception as error:  # reported by the assertion below
            outcome["error"] = error
        finally:
            connection.close()

    remover = threading.Thread(target=remove)
    with transaction.atomic():
        change(record, services.assign, lead, team=team, assignee=agent)
        remover.start()
        blocked = _wait_until_blocked()
    remover.join(timeout=30)
    assert blocked, "The removal did not wait for the assignment's membership lock"
    assert outcome == {"released": 1}
    record.refresh_from_db()
    assert record.assignee_id is None
    assert not TeamMembership.objects.filter(pk=membership.pk).exists()


def test_model_account_deactivation_releases_work(agent, category, join):
    """A plain model save, without the account administration."""
    join(agent, ensure_default_team())
    record = new_request(agent, category, assignee=agent)
    agent.is_active = False
    agent.save(update_fields=["is_active"])
    record.refresh_from_db()
    assert record.assignee_id is None
    # No actor was named on the save: the release is attributed to the account and says so.
    event = AuditEvent.objects.get(action="request.assigned", object_id=str(record.pk))
    assert event.actor_id == agent.pk
    assert event.reason == "Account deactivated; no actor was recorded on the save"
    assert agent.released_assignments == 1


def test_deactivation_names_the_person_who_made_it(agent, make_user, category, join):
    owner = make_user("owner", "admin", is_staff=True, is_superuser=True)
    join(agent, ensure_default_team())
    record = new_request(agent, category, assignee=agent)
    closed = new_request(agent, category, assignee=agent)
    for status, extra in ((Status.RESOLVED, {"summary": "Done"}), (Status.CLOSED, {})):
        change(closed, services.change_status, agent, status=status, **extra)
    agent.is_active = False
    agent.changed_by = owner
    agent.save()
    record.refresh_from_db()
    closed.refresh_from_db()
    assert record.assignee_id is None
    assert closed.assignee_id == agent.pk  # a closed request keeps its history
    event = AuditEvent.objects.get(action="request.assigned", object_id=str(record.pk))
    assert (event.actor_id, event.reason) == (owner.pk, "Account deactivated")


def test_saves_that_do_not_deactivate_release_nothing(agent, category, join):
    join(agent, ensure_default_team())
    record = new_request(agent, category, assignee=agent)
    agent.last_login = timezone.now()
    agent.save(update_fields=["last_login"])  # each sign-in
    agent.first_name = "Renamed"
    agent.save()
    record.refresh_from_db()
    assert record.assignee_id == agent.pk
    assert not AuditEvent.objects.filter(action="request.assigned").exists()
