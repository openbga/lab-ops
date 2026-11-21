"""Neighbours of the assignment lock order (desk.services._hold_assignment): the nonconformance
module that locks a person too, who-asks-first under the new order, stale forms, take across a
team move and the pages. The assignment against deactivation and removal themselves is in
test_assignment_lock_order.py. All data is synthetic."""

import threading
import time
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, connections, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from core.models import AuditEvent, LabModule
from desk import services
from desk.models import ServiceRequest
from jobs.tests.test_review_outbox_round1_verify import writer_waits
from jobs.tests.test_review_round1_fixes import independent
from nonconformance import services as nc_services
from taxonomy.models import RequestCategory
from teams import services as team_services
from teams.models import Team, TeamMembership

from .test_contract import new_request, selection

pytestmark = pytest.mark.django_db


# Helpers ----------------------------------------------------------------------------------------


@pytest.fixture
def owner_admin(make_user):
    return make_user("owner_x", "admin", is_staff=True, is_superuser=True)


def deactivate(user, by):
    """The way the admin ends an account."""
    user.is_active = False
    user.changed_by = by
    user.save()


def rev(ticket):
    return ServiceRequest.objects.get(pk=ticket.pk).revision


def holder(ticket):
    return ServiceRequest.objects.get(pk=ticket.pk).assignee_id


def audit_count():
    return AuditEvent.objects.count()


def person_locks(statements):
    """The statements that lock an account row for update."""
    return [
        q["sql"]
        for q in statements
        if "accounts_user" in q["sql"] and "FOR NO KEY UPDATE" in q["sql"].upper()
    ]


def run_in_thread(work):
    """Run `work` on its own PostgreSQL session with a bounded lock wait. Returns (thread, outcome);
    outcome gets "done" (the result) or "error"."""
    outcome = {}

    def target():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '20s'")
            outcome["done"] = work()
        except Exception as error:  # reported by the assertions
            outcome["error"] = error
        finally:
            connections.close_all()

    thread = threading.Thread(target=target)
    return thread, outcome


def wait_until_blocked(seconds=10):
    """Whether another session is waiting for a lock this session holds, within `seconds`. (Not read
    from pg_stat_activity: its snapshot is fixed per transaction, so a session that connected
    later is never seen from inside one.)"""
    deadline = time.monotonic() + seconds
    with connection.cursor() as cursor:
        while time.monotonic() < deadline:
            cursor.execute(
                "SELECT count(*) FROM pg_locks "
                "WHERE NOT granted AND pg_backend_pid() = ANY(pg_blocking_pids(pid))"
            )
            if cursor.fetchone()[0]:
                return True
            time.sleep(0.05)
    return False


def start_and_wait(thread):
    thread.start()
    return wait_until_blocked()


def fresh_user(user):
    return User.objects.get(pk=user.pk)


# 1. Nonconformance: record, then person; never a ticket -----------------------------------------


@pytest.fixture
def complaint_setup(make_user, join):
    """A complaint ticket held by `holder_` (a member of its team), and the nonconformance opened
    from it by a supervisor `boss`."""
    LabModule.objects.create(key="nonconformance", is_enabled=True, revision=1)
    boss = make_user("nc_boss", "supervisor")
    holder_ = make_user("nc_holder", "client_services")
    group = RequestCategory.objects.create(key="service", name="Service")
    category = RequestCategory.objects.create(
        key="complaint", name="Complaint", parent=group, is_complaint=True
    )
    ticket = new_request(boss, category)
    team = Team.objects.get(pk=ticket.team_id)
    join(holder_, team)
    ticket = services.assign(
        actor=boss,
        request=ticket,
        expected_revision=ticket.revision,
        team=team,
        assignee=holder_,
    )
    nc = nc_services.open_from_complaint(
        actor=boss,
        request=ticket,
        expected_revision=ticket.revision,
        title="Late results",
        description="Results reached the client late.",
        severity="major",
    )
    return {"boss": boss, "holder": holder_, "ticket": ticket, "team": team, "nc": nc}


def _nc_operation(kind, setup, person):
    """(call, field): the nonconformance change naming `person`, and the field refused if the
    person is no longer an active staff account."""
    boss, nc = setup["boss"], setup["nc"]
    revision = nc_services.Nonconformance.objects.get(pk=nc.pk).revision
    if kind == "set_owner":
        return (
            lambda: nc_services.set_owner(
                actor=boss, nc=nc, expected_revision=revision, owner=person
            ),
            "owner",
        )
    if kind == "add_action":
        return (
            lambda: nc_services.add_action(
                actor=boss,
                nc=nc,
                expected_revision=revision,
                description="Retrain the team",
                assignee=person,
                due_on=timezone.localdate() + timedelta(days=7),
            ),
            "assignee",
        )
    return (
        lambda: nc_services.record_impact(
            actor=boss,
            nc=nc,
            expected_revision=revision,
            impact="affected",
            impact_assessment="Reviewed the reports sent last week.",
            client_notice="told",
            client_told_on=timezone.localdate(),
            client_told_how="By phone",
            client_told_by=person,
        ),
        "client_told_by",
    )


NC_KINDS = ["set_owner", "add_action", "record_impact"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", NC_KINDS)
def test_deactivation_waits_for_a_nonconformance_change_naming_the_person_then_completes(
    kind, complaint_setup, owner_admin
):
    """A nonconformance change locks the record and then the person, never a ticket: while it
    holds them, the person's deactivation (account row, then the ticket they hold) waits for the
    account only and then completes and releases the complaint ticket; nobody is deadlocked."""
    setup = complaint_setup
    person, ticket = setup["holder"], setup["ticket"]
    call, _field = _nc_operation(kind, setup, person)
    deactivation, outcome = run_in_thread(
        lambda: deactivate(User.objects.get(pk=person.pk), User.objects.get(pk=owner_admin.pk))
    )
    with transaction.atomic():
        call()  # holds the record and the person until this block commits
        # It never took the complaint ticket, which another session still changes freely.
        assert not writer_waits(
            lambda: ServiceRequest.objects.filter(pk=ticket.pk).update(team=ticket.team)
        )
        blocked = start_and_wait(deactivation)
    deactivation.join(timeout=40)
    assert blocked, ("The deactivation did not wait for the person's account row", outcome)
    assert "error" not in outcome, outcome.get("error")
    assert fresh_user(person).is_active is False
    assert holder(ticket) is None  # released in the deactivation's transaction


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", NC_KINDS)
def test_removal_from_the_ticket_team_does_not_wait_for_a_nonconformance_change(
    kind, complaint_setup
):
    """A nonconformance change never locks the team membership or a ticket, so removing the person
    from the complaint ticket's team completes at once beside it (no wait, no deadlock) and returns
    the ticket to the queue."""
    setup = complaint_setup
    person, ticket, team, boss = (setup[k] for k in ("holder", "ticket", "team", "boss"))
    membership = TeamMembership.objects.get(team=team, user=person)
    call, _field = _nc_operation(kind, setup, person)

    def remove():
        return team_services.remove_member(
            actor=User.objects.get(pk=boss.pk),
            membership=TeamMembership.objects.get(pk=membership.pk),
            reason="Left",
        )

    with transaction.atomic():
        call()
        released = independent(remove)  # lock_timeout 450ms: a wait would raise
    assert released == 1
    assert holder(ticket) is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", NC_KINDS)
def test_nonconformance_change_waits_for_a_deactivation_then_is_refused_on_its_field(
    kind, complaint_setup, owner_admin
):
    """The deactivation holds the account row (and released the ticket); the nonconformance change
    from another session waits for it, then finds the person inactive and is refused on the person's
    field, saving nothing."""
    setup = complaint_setup
    person, ticket, nc = setup["holder"], setup["ticket"], setup["nc"]
    call, field = _nc_operation(kind, setup, person)
    nc_revision = nc_services.Nonconformance.objects.get(pk=nc.pk).revision
    before_audit = audit_count()
    change, outcome = run_in_thread(call)
    with transaction.atomic():
        deactivate(fresh_user(person), owner_admin)
        blocked = start_and_wait(change)
        events_in_flight = audit_count()
    change.join(timeout=40)
    assert blocked, ("The change did not wait for the person's account row", outcome)
    error = outcome.get("error")
    assert isinstance(error, ValidationError), outcome
    assert list(error.message_dict) == [field]
    assert events_in_flight >= before_audit
    assert nc_services.Nonconformance.objects.get(pk=nc.pk).revision == nc_revision
    assert holder(ticket) is None
    assert not AuditEvent.objects.filter(action__startswith="nonconformance.owner").exists()
    if kind == "add_action":
        assert not nc_services.CorrectiveAction.objects.filter(nonconformance=nc).exists()


@pytest.mark.django_db(transaction=True)
def test_opening_from_a_complaint_and_linking_lock_a_ticket_never_a_person(
    complaint_setup, make_user
):
    """open_from_complaint and link_ticket lock a ticket and never a person:
    while a deactivation holds the person's account row, neither waits for it."""
    setup = complaint_setup
    boss, ticket, nc, person = (setup[k] for k in ("boss", "ticket", "nc", "holder"))
    link_to = new_request(boss, ticket.category, subject="A second complaint")
    open_on = new_request(boss, ticket.category, subject="A third complaint")
    admin = make_user("owner_y", "admin", is_staff=True, is_superuser=True)
    revision = nc_services.Nonconformance.objects.get(pk=nc.pk).revision

    def link():
        return nc_services.link_ticket(
            actor=User.objects.get(pk=boss.pk),
            nc=nc_services.Nonconformance.objects.get(pk=nc.pk),
            expected_revision=revision,
            request=link_to,
        )

    def open_record():
        return nc_services.open_from_complaint(
            actor=User.objects.get(pk=boss.pk),
            request=open_on,
            expected_revision=open_on.revision,
            title="Another late result",
            description="Results reached the client late again.",
            severity="minor",
        )

    with transaction.atomic():
        deactivate(fresh_user(person), admin)  # holds the account row until the block ends
        assert independent(link).pk
        assert independent(open_record).pk


# 2. Who asks is decided before what is wrong with the choice ------------------------------------


@pytest.fixture
def restricted(make_user, join, privacy_category, quality):
    """A restricted ticket in Quality, read by `lead` (supervisor) and the Quality members;
    `outsider` has a staff role and is in no team that can read it."""
    lead = make_user("quality_lead", "supervisor")
    member = make_user("quality_member", "technical")
    outsider = make_user("outsider", "client_services")
    join(member, quality)
    ticket = new_request(lead, privacy_category, team=quality)
    return {
        "lead": lead,
        "member": member,
        "outsider": outsider,
        "ticket": ticket,
        "team": quality,
    }


def test_an_actor_who_cannot_read_the_ticket_gets_not_found_for_an_inactive_team(
    restricted, make_user
):
    """Who asks is decided before what is wrong with the choice: assigning a ticket the actor cannot
    read to an inactive team is DoesNotExist, never the team's ValidationError, and no account is
    locked for update on the way."""
    ticket, outsider, member = (restricted[k] for k in ("ticket", "outsider", "member"))
    gone = Team.objects.create(slug="closed-down", name="Closed down", is_active=False)
    before = (rev(ticket), audit_count())
    with CaptureQueriesContext(connection) as statements:
        with pytest.raises(ServiceRequest.DoesNotExist):
            services.assign(
                actor=outsider,
                request=ticket,
                expected_revision=ticket.revision,
                team=gone,
                assignee=None,
            )
    assert person_locks(statements) == []
    assert (rev(ticket), audit_count()) == before
    assert holder(ticket) == ticket.assignee_id
    assert member.is_active


@pytest.mark.parametrize("why", ["inactive", "not-a-member", "no-role"])
def test_an_actor_who_cannot_read_the_ticket_gets_not_found_for_an_ineligible_person(
    why, restricted, make_user
):
    """The same for an ineligible person (inactive, outside the team, or without a staff role):
    DoesNotExist, never the assignee's ValidationError, and the person's account is not locked."""
    ticket, outsider, team = restricted["ticket"], restricted["outsider"], restricted["team"]
    person = make_user("choice_" + why.replace("-", "_"), "technical")
    if why != "not-a-member":
        TeamMembership.objects.create(team=team, user=person)
    if why == "inactive":
        person.is_active = False
        person.save()
    if why == "no-role":
        person.groups.clear()
    before = (rev(ticket), audit_count())
    with CaptureQueriesContext(connection) as statements:
        with pytest.raises(ServiceRequest.DoesNotExist):
            services.assign(
                actor=outsider,
                request=ticket,
                expected_revision=ticket.revision,
                team=team,
                assignee=person,
            )
    assert person_locks(statements) == []
    assert (rev(ticket), audit_count()) == before


def test_take_by_an_actor_who_cannot_read_the_ticket_is_not_found_without_a_person_lock(restricted):
    """take decides who asks first too: an actor who cannot read the restricted ticket gets
    DoesNotExist and locks no account."""
    ticket, outsider = restricted["ticket"], restricted["outsider"]
    before = (rev(ticket), audit_count())
    with CaptureQueriesContext(connection) as statements:
        with pytest.raises(ServiceRequest.DoesNotExist):
            services.take(actor=outsider, request=ticket, expected_revision=ticket.revision)
    assert person_locks(statements) == []
    assert (rev(ticket), audit_count()) == before


def test_bulk_assign_with_one_out_of_scope_ticket_is_refused_before_any_person_lock(
    restricted, make_user, join, category
):
    """The scope of a bulk selection is checked before any person is locked: one ticket the actor
    cannot read refuses the whole action with the not-available message, in the refused case and
    even when the person is ineligible, and nothing is saved."""
    outsider, hidden, team = restricted["outsider"], restricted["ticket"], restricted["team"]
    ordinary = new_request(outsider, category)
    person = make_user("bulk_choice", "technical")
    join(person, team)
    pick = selection([ordinary, hidden])
    before = (rev(ordinary), rev(hidden), audit_count())
    for chosen in (person, None):
        with CaptureQueriesContext(connection) as statements:
            with pytest.raises(PermissionDenied) as caught:
                services.bulk_assign(actor=outsider, selected=pick, team=team, assignee=chosen)
        assert caught.value.args[0] == services.NOT_AVAILABLE
        assert person_locks(statements) == []
    person.is_active = False
    person.save()
    with pytest.raises(PermissionDenied):
        services.bulk_assign(actor=outsider, selected=pick, team=team, assignee=person)
    assert (rev(ordinary), rev(hidden), audit_count()) == before


def _try_to_end_access_while_holding(remove_access, seen):
    """A `_hold_assignment` that, after the real hold, tries to end the actor's access in an
    independent session (lock_timeout 450ms) and records what happened: "55P03" when it had to
    wait for the assignment, "ended" when it went through."""
    from django.db import OperationalError

    real = services._hold_assignment

    def hold(team, assignee):
        held = real(team, assignee)
        if not seen:  # the patch is seen by every session
            try:
                independent(remove_access)
                seen.append("ended")
            except OperationalError as exc:
                seen.append(getattr(exc.__cause__, "sqlstate", None))
        return held

    return hold


def _access_cases(team, actor):
    return {
        "membership": lambda: TeamMembership.objects.filter(team=team, user=actor).delete(),
        "last-role": lambda: User.objects.get(pk=actor.pk).groups.clear(),
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("access", ["membership", "last-role"])
def test_ending_an_actors_access_waits_for_their_assignment_under_way(
    access, restricted, make_user, join
):
    """The actor's team membership, or their only role, is taken away while their assignment
    waits for the person's rows. The assignment holds the actor's rows from its start
    (accounts.acting), so the change of access cannot go through meanwhile: it waits for the
    assignment (here it gives up after its bounded wait), and the assignment, decided by an actor
    who still had that access, is saved."""
    ticket, team = restricted["ticket"], restricted["team"]
    actor = make_user(f"quality_actor_{access}", "technical")
    join(actor, team)
    person = make_user(f"quality_person_{access}", "technical")
    join(person, team)
    seen = []
    attempt = _try_to_end_access_while_holding(_access_cases(team, actor)[access], seen)
    with patch.object(services, "_hold_assignment", attempt):
        services.assign(
            actor=actor,
            request=ticket,
            expected_revision=ticket.revision,
            team=team,
            assignee=person,
        )
    assert seen == ["55P03"], seen  # it waited for the assignment
    assert holder(ticket) == person.pk
    assert TeamMembership.objects.filter(team=team, user=actor).exists()
    assert User.objects.get(pk=actor.pk).groups.filter(name="technical").exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("access", ["membership", "last-role"])
def test_an_assignment_by_an_actor_whose_access_ended_before_it_is_refused(
    access, restricted, make_user, join
):
    """The actor's page was drawn while they had access; their membership of the restricted
    ticket's team, or their only role, ended before they saved. The assignment reads the account
    as it is held and refuses them (not found, or no longer staff), saving nothing."""
    ticket, team = restricted["ticket"], restricted["team"]
    actor = make_user(f"quality_actor_before_{access}", "technical")
    join(actor, team)
    person = make_user(f"quality_person_before_{access}", "technical")
    join(person, team)
    before = (rev(ticket), audit_count(), holder(ticket))
    _access_cases(team, actor)[access]()
    expected = ServiceRequest.DoesNotExist if access == "membership" else PermissionDenied
    with pytest.raises(expected):
        services.assign(
            actor=actor,
            request=ticket,
            expected_revision=ticket.revision,
            team=team,
            assignee=person,
        )
    assert (rev(ticket), audit_count(), holder(ticket)) == before


@pytest.mark.django_db(transaction=True)
def test_a_bulk_assignment_by_an_actor_whose_access_ends_meanwhile_waits_or_is_refused(
    restricted, make_user, join
):
    """The same in bulk. Ending the actor's membership while their bulk Assign waits for the
    person's rows has to wait for it, and the assignment is saved; ended before the action
    starts, the whole action is refused and nothing is saved."""
    ticket, team = restricted["ticket"], restricted["team"]
    actor = make_user("quality_actor3", "technical")
    join(actor, team)
    person = make_user("quality_person3", "technical")
    join(person, team)
    seen = []
    attempt = _try_to_end_access_while_holding(_access_cases(team, actor)["membership"], seen)
    with patch.object(services, "_hold_assignment", attempt):
        services.bulk_assign(actor=actor, selected=selection([ticket]), team=team, assignee=person)
    assert seen == ["55P03"], seen
    assert holder(ticket) == person.pk
    _access_cases(team, actor)["membership"]()
    ticket = ServiceRequest.objects.get(pk=ticket.pk)
    before = (rev(ticket), audit_count(), holder(ticket))
    with pytest.raises((PermissionDenied, ServiceRequest.DoesNotExist)):
        services.bulk_assign(actor=actor, selected=selection([ticket]), team=team, assignee=None)
    assert (rev(ticket), audit_count(), holder(ticket)) == before


# 3. Stale forms ---------------------------------------------------------------------------------


@pytest.fixture
def held_ticket(make_user, join, category, accessioning):
    """A ticket held by `worker`, in Accessioning, with a second member `peer` and a supervisor."""
    boss = make_user("stale_boss", "supervisor")
    worker = make_user("stale_worker", "technical")
    peer = make_user("stale_peer", "technical")
    join(worker, accessioning)
    join(peer, accessioning)
    ticket = new_request(boss, category, team=accessioning, assignee=worker)
    return {"boss": boss, "worker": worker, "peer": peer, "ticket": ticket, "team": accessioning}


def _end_work(how, person, team, boss, owner_admin):
    if how == "deactivation":
        deactivate(fresh_user(person), owner_admin)
    else:
        team_services.remove_member(
            actor=boss,
            membership=TeamMembership.objects.get(team=team, user=person),
            reason="Left",
        )


@pytest.mark.parametrize("how", ["deactivation", "removal"])
def test_a_form_opened_before_the_person_left_is_stale_once_their_ticket_is_released(
    how, held_ticket, owner_admin
):
    """An assignment page drawn while the person held the ticket carries the old revision; the
    ending releases the ticket (advancing the revision), so assigning with the old revision is
    refused as stale and nothing is saved."""
    boss, worker, peer, ticket, team = (
        held_ticket[k] for k in ("boss", "worker", "peer", "ticket", "team")
    )
    seen = rev(ticket)
    _end_work(how, worker, team, boss, owner_admin)
    released = rev(ticket)
    assert released > seen and holder(ticket) is None
    before = audit_count()
    with pytest.raises(ValidationError) as caught:
        services.assign(
            actor=boss, request=ticket, expected_revision=seen, team=team, assignee=peer
        )
    assert caught.value.messages == [services.STALE]
    assert (rev(ticket), holder(ticket), audit_count()) == (released, None, before)


@pytest.mark.parametrize("how", ["deactivation", "removal"])
def test_assigning_a_person_who_left_since_the_form_was_drawn_is_refused_on_assignee(
    how, held_ticket, owner_admin, make_user, join
):
    """The ticket was not the person's, so ending their work leaves its revision as the page saw
    it; assigning them is refused on the assignee field and nothing is saved."""
    boss, ticket, team = (held_ticket[k] for k in ("boss", "ticket", "team"))
    newcomer = make_user("stale_newcomer", "technical")
    join(newcomer, team)
    seen = rev(ticket)
    _end_work(how, newcomer, team, boss, owner_admin)
    assert rev(ticket) == seen
    before = (holder(ticket), audit_count())
    with pytest.raises(ValidationError) as caught:
        services.assign(
            actor=boss, request=ticket, expected_revision=seen, team=team, assignee=newcomer
        )
    assert list(caught.value.message_dict) == ["assignee"]
    assert (rev(ticket), holder(ticket), audit_count()) == (seen, before[0], before[1])


# 4. take across a team move ---------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("revision_step", [0, 1], ids=["old-revision", "revision-after-the-move"])
def test_take_is_refused_when_the_ticket_moved_team_after_its_unlocked_team_read(
    revision_step, make_user, join, category, accessioning, quality
):
    """take reads the ticket's team before locking anything; if another session moves the ticket
    to another team after the person's rows were held, take is refused as changed, even with the
    revision the move produced, and saves nothing."""
    boss = make_user("take_boss", "supervisor")
    taker = make_user("taker", "technical")
    join(taker, accessioning)
    ticket = new_request(boss, category, team=accessioning)
    seen = rev(ticket)
    real = services._hold_assignment
    moved = {}

    def hold(team, assignee):
        held = real(team, assignee)
        if moved:  # the independent session's own assignment is not the one under test
            return held

        def move():
            services.assign(
                actor=User.objects.get(pk=boss.pk),
                request=ServiceRequest.objects.get(pk=ticket.pk),
                expected_revision=seen,
                team=Team.objects.get(pk=quality.pk),
                assignee=None,
            )

        moved["revision"] = 0
        independent(move)
        moved["revision"] = rev(ticket)
        return held

    with patch.object(services, "_hold_assignment", hold):
        with pytest.raises(ValidationError) as caught:
            services.take(actor=taker, request=ticket, expected_revision=seen + revision_step)
    assert moved["revision"] == seen + 1
    assert caught.value.messages == [services.STALE]
    after = ServiceRequest.objects.get(pk=ticket.pk)
    assert (after.team_id, after.assignee_id, after.revision) == (quality.pk, None, seen + 1)
    assert not AuditEvent.objects.filter(action="request.assigned", actor=taker).exists()


# 5. Pages ---------------------------------------------------------------------------------------


@pytest.fixture
def desk_page(client, held_ticket, make_user, join, owner_admin):
    """A signed-in supervisor, the ticket and a person deactivated just before."""
    boss, ticket, team = (held_ticket[k] for k in ("boss", "ticket", "team"))
    gone = make_user("page_gone", "technical")
    join(gone, team)
    client.force_login(boss)
    return {"client": client, "boss": boss, "ticket": ticket, "team": team, "gone": gone}


def _choices_with(gone, team):
    """The assign choices as an earlier page drew them: the person still offered."""
    from desk import forms

    real = forms.assign_choices

    def choices(*args, **kwargs):
        return [*real(*args, **kwargs), ("Earlier page", [(f"{team.pk}:{gone.pk}", "Gone person")])]

    return choices


def test_the_assign_page_refuses_a_person_deactivated_before_it_is_posted(desk_page, owner_admin):
    """The assign page offers only active members: a person deactivated before the post is refused
    as an ordinary form error (status 200), never a server error, and nothing is assigned."""
    c, ticket, team, gone = (desk_page[k] for k in ("client", "ticket", "team", "gone"))
    seen = rev(ticket)
    deactivate(gone, owner_admin)
    response = c.post(
        reverse("desk:request-assign", args=[ticket.pk]),
        {"expected_revision": seen, "assign_to": f"{team.pk}:{gone.pk}", "reason": ""},
    )
    assert response.status_code == 200
    assert "Select a valid choice" in response.content.decode()
    assert (rev(ticket), holder(ticket)) == (seen, ticket.assignee_id)


def test_the_assign_page_shows_the_services_refusal_for_a_person_who_left_after_the_form(
    desk_page, owner_admin
):
    """When the form still accepts a person (drawn before the deactivation) the service refuses
    them: the page shows the assignee error with status 200, never a 500, and saves nothing."""
    c, ticket, team, gone = (desk_page[k] for k in ("client", "ticket", "team", "gone"))
    seen = rev(ticket)
    deactivate(gone, owner_admin)
    with patch("desk.forms.assign_choices", _choices_with(gone, team)):
        response = c.post(
            reverse("desk:request-assign", args=[ticket.pk]),
            {"expected_revision": seen, "assign_to": f"{team.pk}:{gone.pk}", "reason": ""},
        )
    assert response.status_code == 200
    assert "Choose an active member of the ticket" in response.content.decode()
    assert (rev(ticket), holder(ticket)) == (seen, ticket.assignee_id)


def test_the_take_action_refuses_a_member_removed_just_before_with_a_message(
    held_ticket, client, make_user, join
):
    """take for an account removed from the ticket's team just before: a redirect carrying the
    refusal message (never a 500), and the ticket is not assigned to them."""
    boss, ticket, team = (held_ticket[k] for k in ("boss", "ticket", "team"))
    taker = make_user("page_taker", "technical")
    membership = join(taker, team)
    client.force_login(taker)
    seen = rev(ticket)
    team_services.remove_member(actor=boss, membership=membership, reason="Left")
    response = client.post(
        reverse("desk:request-take", args=[ticket.pk]), {"expected_revision": seen}, follow=True
    )
    assert response.status_code == 200
    assert "Only members of" in response.content.decode()
    assert (rev(ticket), holder(ticket)) == (seen, ticket.assignee_id)


def test_the_take_action_refuses_a_stale_page_with_a_message(held_ticket, client, make_user, join):
    """take with the revision of a page drawn before the ticket changed: a redirect carrying the
    stale message, never a 500."""
    boss, ticket, team = (held_ticket[k] for k in ("boss", "ticket", "team"))
    taker = make_user("page_taker2", "technical")
    join(taker, team)
    client.force_login(taker)
    seen = rev(ticket)
    services.assign(
        actor=boss, request=ticket, expected_revision=seen, team=team, assignee=held_ticket["peer"]
    )
    response = client.post(
        reverse("desk:request-take", args=[ticket.pk]), {"expected_revision": seen}, follow=True
    )
    assert response.status_code == 200
    assert services.STALE in response.content.decode().replace("&#x27;", "'")
    assert holder(ticket) == held_ticket["peer"].pk


def _bulk_post(c, ticket, team, gone, revision, url_name):
    return c.post(
        reverse(url_name) + "?view=all",
        {
            "action": "assign",
            "assign_to": f"{team.pk}:{gone.pk}",
            "requests": [f"{ticket.pk}:{revision}"],
        },
        follow=True,
    )


def test_the_list_bulk_assign_refuses_a_person_deactivated_just_before(desk_page, owner_admin):
    """The list's bulk assign for a person deactivated before the post: the list is drawn again
    (status 200) with the form's refusal, never a 500, and nothing is assigned."""
    c, ticket, team, gone = (desk_page[k] for k in ("client", "ticket", "team", "gone"))
    seen = rev(ticket)
    deactivate(gone, owner_admin)
    response = _bulk_post(c, ticket, team, gone, seen, "desk:request-list")
    assert response.status_code == 200
    # The refusal does not repeat the posted value (review of the queue's way back, third
    # confirmation, finding 6).
    assert "Choose one of the options offered." in response.content.decode()
    assert (rev(ticket), holder(ticket)) == (seen, ticket.assignee_id)


def test_the_list_bulk_assign_shows_the_services_refusal_for_a_person_who_left_after_the_form(
    desk_page, owner_admin
):
    """When the bulk form still accepts the person the service refuses them: the list is drawn
    again (200) with the message, never a 500, and nothing is assigned."""
    c, ticket, team, gone = (desk_page[k] for k in ("client", "ticket", "team", "gone"))
    seen = rev(ticket)
    deactivate(gone, owner_admin)
    with patch("desk.forms.assign_choices", _choices_with(gone, team)):
        response = _bulk_post(c, ticket, team, gone, seen, "desk:request-list")
    assert response.status_code == 200
    assert "Choose an active member of the ticket" in response.content.decode()
    assert (rev(ticket), holder(ticket)) == (seen, ticket.assignee_id)


def test_the_old_bulk_endpoint_refuses_a_person_who_left_after_the_form(desk_page, owner_admin):
    """The older bulk endpoint turns the same refusal into a message and a redirect (302), never
    a 500, and nothing is assigned."""
    c, ticket, team, gone = (desk_page[k] for k in ("client", "ticket", "team", "gone"))
    seen = rev(ticket)
    deactivate(gone, owner_admin)
    with patch("desk.forms.assign_choices", _choices_with(gone, team)):
        response = c.post(
            reverse("desk:request-bulk"),
            {
                "action": "assign",
                "assign_to": f"{team.pk}:{gone.pk}",
                "requests": [f"{ticket.pk}:{seen}"],
            },
        )
    assert response.status_code == 302
    assert (rev(ticket), holder(ticket)) == (seen, ticket.assignee_id)
    shown = [str(m) for m in response.wsgi_request._messages]
    assert any("Choose an active member of the ticket" in text for text in shown), shown
