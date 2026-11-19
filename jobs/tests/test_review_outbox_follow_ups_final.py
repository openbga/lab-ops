"""Final check of the outbox follow-ups at cfc23e0: V01 (the default team chosen under its lock)
and the cases next to it. All records are synthetic.

Each test states intended behaviour. A failing test is a finding in
.local/review-history/outbox-follow-ups-round-1-final-check.md; a passing one is a checked case.
"""

import threading
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import OperationalError, connection, connections, transaction
from django.urls import reverse

from clients.models import PortalAccess
from desk import services
from desk.models import ServiceRequest
from desk.tests.conftest import PASSWORD
from desk.tests.test_contract import new_request
from taxonomy.models import RequestCategory
from taxonomy.terms import drawn
from teams import services as teams
from teams.models import DEFAULT_TEAM_SLUG, Team, ensure_default_team

from . import test_review_outbox_follow_ups_verify as verify
from . import test_review_outbox_round1_verify as prior

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("locmem", "review_database"),
]
review_database = prior.review_database
admin = verify.admin
supervisor = verify.supervisor
edit_team = verify.edit_team


class Held:
    """`work()` in a transaction on another connection, kept open until finish(); it then
    commits, or rolls back with commit=False."""

    def __init__(self, work, commit=True):
        self.work, self.commit = work, commit
        self.ready, self.release, self.box = threading.Event(), threading.Event(), {}
        self.thread = threading.Thread(target=self._run)

    def _run(self):
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '8s'")
                self.box["result"] = self.work()
                self.ready.set()
                self.release.wait(timeout=30)
                if not self.commit:
                    transaction.set_rollback(True)
        except Exception as exc:
            self.box["error"] = exc
            self.ready.set()
        finally:
            connections.close_all()

    def start(self):
        self.thread.start()
        assert self.ready.wait(timeout=20)
        if "error" in self.box:
            raise self.box["error"]
        return self.box.get("result")

    def finish(self):
        self.release.set()
        self.thread.join(timeout=30)
        if "error" in self.box:
            raise self.box["error"]


def move_to_new_team(admin_actor, name):
    return lambda: teams.create_team(actor=admin_actor, name=name, is_default=True).pk


def created_team(agent, category):
    return lambda: new_request(agent, category).team_id


def outcome_of(future):
    try:
        return future.result(timeout=30)
    except OperationalError as exc:
        return getattr(exc.__cause__, "sqlstate", "error")
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc).splitlines()[0]}"


# V01 for its case and next to it --------------------------------------------------------------


def test_a_creation_waiting_for_a_move_that_rolls_back_stays_in_the_default(agent, category, admin):
    old_default = ensure_default_team()
    move = Held(move_to_new_team(admin, "Verify abandoned default"), commit=False)
    move.start()
    created = verify._background(created_team(agent, category))
    assert verify._until_someone_waits()
    move.finish()
    assert outcome_of(created) == old_default.pk
    assert Team.objects.get(is_default=True).pk == old_default.pk


def test_a_category_that_names_the_old_default_keeps_it_through_a_move(agent, category, admin):
    old_default = ensure_default_team()
    RequestCategory.objects.filter(pk=category.pk).update(default_team=old_default)
    category.refresh_from_db()
    move = Held(move_to_new_team(admin, "Verify other default"))
    new_default = move.start()
    created = verify._background(created_team(agent, category))
    assert verify._until_someone_waits()  # the named team is the one being moved from
    move.finish()
    assert outcome_of(created) == old_default.pk != new_default


def test_a_category_that_names_another_team_does_not_wait_for_a_move(
    agent, category, accessioning, admin
):
    ensure_default_team()
    RequestCategory.objects.filter(pk=category.pk).update(default_team=accessioning)
    category.refresh_from_db()
    move = Held(move_to_new_team(admin, "Verify unrelated default"))
    move.start()
    try:
        assert not prior.writer_waits(lambda: new_request(agent, category))
    finally:
        move.finish()


def test_two_moves_during_one_creation_land_in_the_last_default(agent, category, admin):
    """The creation waits for a first move, reads the new default, finds a second move holding it,
    waits again and takes the default in force when it goes ahead (its third reading)."""
    ensure_default_team()
    real = services.ensure_default_team
    second_reading, second_move_holds = threading.Event(), threading.Event()
    calls = []

    def reading():
        calls.append(1)
        if len(calls) == 2:
            second_reading.set()
            assert second_move_holds.wait(timeout=20)
        return real()

    first = Held(move_to_new_team(admin, "Verify first new default"))
    second = Held(move_to_new_team(admin, "Verify second new default"))
    with patch.object(services, "ensure_default_team", reading):
        first.start()
        created = verify._background(created_team(agent, category))
        assert verify._until_someone_waits()
        first.finish()
        assert second_reading.wait(timeout=20)
        last_default = second.start()
        second_move_holds.set()
        assert verify._until_someone_waits()
        second.finish()
        team_id = outcome_of(created)
    assert len(calls) == 3
    assert team_id == last_default == Team.objects.get(is_default=True).pk


def test_a_default_that_keeps_moving_is_refused_as_a_form_error_on_the_portal(
    client, clinic_portal, clinic, category, accessioning
):
    """After three readings that each found a moved default, the creation is refused with a
    message the portal page shows (not a server error), and no ticket is created."""
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    ensure_default_team()
    client.login(username=clinic_portal.username, password=PASSWORD)
    stale = Team.objects.get(pk=accessioning.pk)  # active, never the default
    with patch.object(services, "ensure_default_team", return_value=stale):
        response = client.post(
            reverse("portal:request-create"),
            {
                "category": category.pk,
                "category_terms": drawn(RequestCategory.objects.filter(pk=category.pk)),
                "subject": "Report not received",
                "description": "Synthetic portal request.",
            },
        )
        with pytest.raises(ValidationError) as refused:
            new_request(clinic_portal, category, channel="portal", first_entry_kind="reply")
    assert response.status_code == 200
    assert "The default team is being changed. Try again." in response.content.decode()
    assert refused.value.message_dict == {"team": ["The default team is being changed. Try again."]}
    assert not ServiceRequest.objects.exists()


def test_the_database_refuses_an_inactive_default_team():
    """So _current_default_team's inactive branch cannot be reached by stored data."""
    from django.db import IntegrityError

    default = ensure_default_team()
    with pytest.raises(IntegrityError, match="team_default_active"), transaction.atomic():
        Team.objects.filter(pk=default.pk).update(is_active=False)


@pytest.mark.parametrize("state", ["no-team", "inactive-reserved-team"])
def test_a_database_without_a_default_team_gets_one_for_the_first_ticket(state, agent, category):
    reserved = ensure_default_team()
    assert reserved.slug == DEFAULT_TEAM_SLUG
    if state == "no-team":
        Team.objects.filter(pk=reserved.pk).delete()
    else:
        Team.objects.filter(pk=reserved.pk).update(is_default=False, is_active=False)
    assert not Team.objects.filter(is_default=True).exists()
    ticket = new_request(agent, category)
    team = Team.objects.get(pk=ticket.team_id)
    assert (team.slug, team.is_default, team.is_active) == (DEFAULT_TEAM_SLUG, True, True)


def test_a_ticket_created_while_a_team_is_made_the_first_default_goes_to_it(agent, category, admin):
    """With no default team (a fresh or reset database), an administrator makes a team the
    default while a ticket is being created: the creation waits for it and then takes it, as it
    does for a move; it does not fail with a database error."""
    reserved = ensure_default_team()
    Team.objects.filter(pk=reserved.pk).delete()
    assert not Team.objects.filter(is_default=True).exists()
    move = Held(move_to_new_team(admin, "Verify first default"))
    new_default = move.start()
    created = verify._background(created_team(agent, category))
    assert verify._until_someone_waits(), "the creation did not wait for the new default"
    move.finish()
    assert outcome_of(created) == new_default
    assert Team.objects.filter(is_default=True).count() == 1


# What waits on what, and no deadlock with the team services ------------------------------------


@pytest.mark.parametrize("writer", ["edit-default", "add-member", "make-another-default"])
def test_team_services_wait_for_a_ticket_being_created_into_the_default(
    writer, agent, category, accessioning, admin, supervisor, make_user
):
    default = ensure_default_team()
    newcomer = make_user("final_newcomer", "technical")

    def write():
        if writer == "edit-default":
            edit_team(admin, default)
        elif writer == "add-member":
            teams.add_member(actor=supervisor, team=default, user=newcomer)
        else:
            edit_team(admin, accessioning, is_default=True)

    with transaction.atomic():
        assert new_request(agent, category).team_id == default.pk
        assert prior.writer_waits(write)


def test_making_another_team_default_during_a_creation_finishes_without_deadlock(
    agent, category, accessioning, admin
):
    """update_team locks the team it makes the default, then the old default; the creation holds
    the old default. The move waits for the creation and then completes."""
    default = ensure_default_team()
    creation = Held(lambda: new_request(agent, category).team_id)
    assert creation.start() == default.pk
    moved = verify._background(lambda: edit_team(admin, accessioning, is_default=True).pk)
    assert verify._until_someone_waits()
    creation.finish()
    assert outcome_of(moved) == accessioning.pk
    assert Team.objects.get(is_default=True).pk == accessioning.pk


def test_a_creation_into_another_team_does_not_wait_for_a_default_move(
    agent, category, accessioning, admin
):
    ensure_default_team()
    move = Held(move_to_new_team(admin, "Verify parallel default"))
    move.start()
    try:
        assert not prior.writer_waits(lambda: new_request(agent, category, team=accessioning))
    finally:
        move.finish()
