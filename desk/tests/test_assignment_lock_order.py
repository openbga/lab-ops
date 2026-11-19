"""Assignment and the end of a person's work take their row locks in one order.

An assignment (assign, take, the list's bulk Assign) locks the assignee's account, roles and team
membership, then the tickets, and then the team (desk.services._hold_assignment). Ending a
person's work starts from the same rows: a deactivation updates the account (desk.signals) and a
removal from a team locks the membership (teams.services.remove_member), and each then locks the
person's open tickets to return them to the queue. So the two never wait for each other in a
cycle (PostgreSQL deadlock, 40P01): whichever comes second waits, then decides on what the first
saved, and nobody is left assigned to a team they left or with an account that was deactivated.

Each case runs the assignment here and the end of the person's work in another PostgreSQL
session. The assignment pauses once, at one of the points between its locks, until the other
session waits for a lock or has finished. Before this order, a pause once the tickets were locked
ended in a deadlock for an assignment that touched a ticket the person already held (independent
review of the outbox follow-ups, R03). All data is synthetic.
"""

import re
import threading
import time
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, connection, connections
from django.test.utils import CaptureQueriesContext

from accounts.models import User
from desk import services
from desk.models import ServiceRequest, Status
from teams import services as team_services
from teams.models import Team, TeamMembership

from .test_contract import new_request, selection

pytestmark = pytest.mark.django_db(transaction=True)

LOCK_WAIT = "8s"  # long enough for the other session's work; a mistake fails, never hangs


def _blocked_by_this_session(pid):
    """Whether session `pid` waits for a lock this session holds. pg_blocking_pids reads the lock
    manager live; pg_stat_activity would not do here, as it is read once per transaction."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
        return cursor.fetchone()[0]


def _until_waiting_or_ended(other, ended, seconds=8):
    """What the other session did while the assignment paused: "waiting" for a lock the
    assignment holds, or "ended"."""
    other["connected"].wait(timeout=seconds)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if ended.is_set():
            return "ended"
        if "pid" in other and _blocked_by_this_session(other["pid"]):
            return "waiting"
        time.sleep(0.02)
    return "neither"


def _set_lock_timeout(value):
    with connection.cursor() as cursor:
        cursor.execute(f"SET lock_timeout = '{value}'")


def _outcome_of(exc):
    if isinstance(exc, OperationalError):
        return getattr(exc.__cause__, "sqlstate", None) or "database error"
    if isinstance(exc, ValidationError):
        return "refused"  # what was chosen, or a ticket that changed
    if isinstance(exc, PermissionDenied):
        return "denied"  # who asks
    return repr(exc)


def race(assignment, ending, pause_after):
    """Run `assignment` in this session and `ending` in another. The assignment pauses once, right
    after the desk.services function `pause_after` returns, until the ending waits for a lock or
    has finished; then it goes on. Returns the outcome of each and what the ending did during the
    pause."""
    start, ended = threading.Event(), threading.Event()
    other = {"connected": threading.Event()}
    outcome = {}

    def end_work():
        try:
            start.wait(timeout=10)
            _set_lock_timeout(LOCK_WAIT)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                other["pid"] = cursor.fetchone()[0]
            other["connected"].set()
            ending()
            outcome["ending"] = "done"
        except Exception as exc:  # reported by the assertions
            outcome["ending"] = _outcome_of(exc)
        finally:
            other["connected"].set()
            ended.set()
            connections.close_all()

    real = getattr(services, pause_after)

    def paused(*args, **kwargs):
        result = real(*args, **kwargs)
        if not start.is_set():
            start.set()
            outcome["during_pause"] = _until_waiting_or_ended(other, ended)
        return result

    worker = threading.Thread(target=end_work)
    worker.start()
    try:
        _set_lock_timeout(LOCK_WAIT)
        with patch.object(services, pause_after, paused):
            try:
                assignment()
                outcome["assignment"] = "done"
            except Exception as exc:  # reported by the assertions
                outcome["assignment"] = _outcome_of(exc)
    finally:
        start.set()
        worker.join(timeout=20)
        _set_lock_timeout("0")
    return outcome


# The people and tickets -------------------------------------------------------------------------


@pytest.fixture
def people(make_user, join, accessioning, quality):
    owner = make_user("lock_order_owner", "admin", is_staff=True, is_superuser=True)
    supervisor = make_user("lock_order_supervisor", "supervisor")
    person = make_user("lock_order_technician", "technical")
    join(person, accessioning)
    join(person, quality)
    return owner, supervisor, person


@pytest.fixture
def tickets(agent, category, accessioning, people):
    """One ticket the person already holds in Accessioning and one waiting in its queue."""
    _owner, _supervisor, person = people
    held = new_request(agent, category, team=accessioning, assignee=person)
    waiting = new_request(agent, category, team=accessioning)
    return held, waiting


def _fresh(ticket):
    return ServiceRequest.objects.get(pk=ticket.pk)


def _assign(actor, ticket, team, person):
    ticket = _fresh(ticket)
    services.assign(
        actor=actor, request=ticket, expected_revision=ticket.revision, team=team, assignee=person
    )


class Assignment:
    """One way of assigning, and the team it assigns to."""

    def __init__(self, work, team):
        self.work, self.team = work, team


def _bulk(agent, person, held, waiting, teams):
    services.bulk_assign(
        actor=agent,
        selected=selection([held, waiting]),
        team=teams["accessioning"],
        assignee=person,
    )


def _take(person, ticket):
    ticket = _fresh(ticket)
    services.take(actor=person, request=ticket, expected_revision=ticket.revision)


ASSIGNMENTS = {
    # The ticket the person holds, moved to their other team with them (review R03, first case).
    "move-keeping-the-assignee": Assignment(
        lambda agent, person, held, waiting, teams: _assign(agent, held, teams["quality"], person),
        "quality",
    ),
    # The list's bulk Assign over the waiting ticket and theirs (review R03, second case).
    "bulk-with-one-already-theirs": Assignment(_bulk, "accessioning"),
    "assign-the-waiting-ticket": Assignment(
        lambda agent, person, held, waiting, teams: _assign(
            agent, waiting, teams["accessioning"], person
        ),
        "accessioning",
    ),
    "take-the-waiting-ticket": Assignment(
        lambda agent, person, held, waiting, teams: _take(person, waiting), "accessioning"
    ),
    # Refused as already theirs, after the same locks.
    "take-their-own-ticket": Assignment(
        lambda agent, person, held, waiting, teams: _take(person, held), "accessioning"
    ),
}


def _deactivate(owner, supervisor, person, team):
    account = User.objects.get(pk=person.pk)
    account.is_active = False
    account.changed_by = owner
    account.save()


def _remove(owner, supervisor, person, team):
    membership = TeamMembership.objects.get(team=team, user=person)
    team_services.remove_member(actor=supervisor, membership=membership, reason="Left the team")


# The end of the person's work: deactivating the account, or removing them from the team the
# assignment gives the ticket to.
ENDINGS = {"deactivation": _deactivate, "removal-from-the-team": _remove}


def _left_with_work(person, ending, team):
    """Open tickets the person still holds where the ending says they may not."""
    open_work = ServiceRequest.objects.filter(assignee=person).exclude(status=Status.CLOSED)
    if ending == "removal-from-the-team":
        open_work = open_work.filter(team=team)
    return list(open_work.values_list("number", flat=True))


def _run(assignment, ending, pause_after, agent, people, tickets, teams):
    owner, supervisor, person = people
    held, waiting = tickets
    spec = ASSIGNMENTS[assignment]
    return race(
        lambda: spec.work(agent, person, held, waiting, teams),
        lambda: ENDINGS[ending](owner, supervisor, person, teams[spec.team]),
        pause_after,
    )


@pytest.fixture
def teams(accessioning, quality):
    return {"accessioning": accessioning, "quality": quality}


# Both orders, every assignment path -------------------------------------------------------------


def _pause_after_tickets(assignment):
    return "_bulk_requests" if ASSIGNMENTS[assignment].work is _bulk else "_staff_request"


@pytest.mark.parametrize("ending", list(ENDINGS))
@pytest.mark.parametrize("assignment", list(ASSIGNMENTS))
@pytest.mark.parametrize("pause", ["after-the-persons-rows", "after-the-tickets", "after-the-team"])
def test_an_ending_that_comes_second_waits_then_returns_the_work_to_the_queue(
    pause, assignment, ending, agent, people, tickets, teams
):
    """The assignment holds the person's rows (and then its tickets, and then the team): the
    ending waits for the person's rows, never in a cycle, and once the assignment commits it finds
    whatever the assignment left with the person and returns it to the queue. Before this order,
    a pause after the tickets ended in 40P01 for the cases that touch the ticket the person
    already held."""
    pause_after = {
        "after-the-persons-rows": "_hold_assignment",
        "after-the-tickets": _pause_after_tickets(assignment),
        "after-the-team": "_current_team",
    }[pause]
    outcome = _run(assignment, ending, pause_after, agent, people, tickets, teams)
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}"
    assert outcome["during_pause"] == "waiting", outcome
    assert outcome["ending"] == "done", outcome
    expected = "refused" if assignment == "take-their-own-ticket" else "done"
    assert outcome["assignment"] == expected, outcome
    team = teams[ASSIGNMENTS[assignment].team]
    assert _left_with_work(people[2], ending, team) == []


BEFORE_THE_PERSONS_ROWS = [
    (pause_after, assignment)
    for assignment, spec in ASSIGNMENTS.items()
    for pause_after in (["_bulk_selection"] if spec.work is _bulk else ["_visible_request"])
]


@pytest.mark.parametrize("ending", list(ENDINGS))
@pytest.mark.parametrize(("pause_after", "assignment"), BEFORE_THE_PERSONS_ROWS)
def test_an_assignment_that_comes_second_decides_on_what_the_ending_saved(
    pause_after, assignment, ending, agent, people, tickets, teams
):
    """Paused before the person's rows are locked: the ending goes through without waiting, and
    the assignment then reads the account or membership as the ending left it and is refused
    (the person deactivated and taking a ticket: as no longer staff), with nothing saved and
    nothing left with the person."""
    _owner, _supervisor, person = people
    held, waiting = tickets
    team = teams[ASSIGNMENTS[assignment].team]
    before = {ticket.pk: _fresh(ticket).revision for ticket in tickets}
    outcome = _run(assignment, ending, pause_after, agent, people, tickets, teams)
    assert outcome["during_pause"] == "ended", outcome
    assert outcome["ending"] == "done", outcome
    taker_deactivated = assignment.startswith("take") and ending == "deactivation"
    assert outcome["assignment"] == ("denied" if taker_deactivated else "refused"), outcome
    assert _left_with_work(person, ending, team) == []
    # The ending released the ticket the person held if it covered it; the refused assignment
    # changed nothing.
    released = ending == "deactivation" or team.slug == "accessioning"
    assert _fresh(held).revision == before[held.pk] + (1 if released else 0)
    assert (_fresh(held).assignee_id is None) == released
    assert _fresh(waiting).revision == before[waiting.pk]
    assert _fresh(waiting).assignee_id is None


# The order itself -------------------------------------------------------------------------------


def _locked_tables(sql):
    """The tables a statement locks: those named after FOR ... OF, else the first FROM."""
    if " FOR " not in sql:
        return set()
    head, _, tail = sql.partition(" FOR ")
    named = set(re.findall(r'OF "([^"]+)"', tail))
    if named:
        return named
    first = re.search(r'FROM "([^"]+)"', head)
    return {first.group(1)} if first else set()


def _first_lock(statements, table):
    for index, sql in enumerate(statements):
        if table in _locked_tables(sql):
            return index
    return None


def _lock_positions(work):
    with CaptureQueriesContext(connection) as captured:
        work()
    statements = [query["sql"] for query in captured]
    tables = {
        "team": Team._meta.db_table,
        "account": User._meta.db_table,
        "roles": User.groups.through._meta.db_table,
        "membership": TeamMembership._meta.db_table,
        "ticket": ServiceRequest._meta.db_table,
    }
    return {name: _first_lock(statements, table) for name, table in tables.items()}


@pytest.mark.parametrize("assignment", list(ASSIGNMENTS))
def test_every_assignment_locks_the_persons_rows_before_the_tickets_and_the_team_after(
    assignment, agent, people, tickets, teams
):
    """The order the cases above rest on, read from the statements each path sends: the person's
    rows before the tickets, and the team after them, as the other ticket services lock it."""
    _owner, _supervisor, person = people
    held, waiting = tickets

    def work():
        try:
            ASSIGNMENTS[assignment].work(agent, person, held, waiting, teams)
        except ValidationError:
            assert assignment == "take-their-own-ticket"

    found = _lock_positions(work)
    assert None not in found.values(), found
    assert found["account"] < found["roles"] < found["membership"] < found["ticket"], found
    assert found["ticket"] < found["team"], found


@pytest.mark.parametrize("ending", list(ENDINGS))
def test_every_ending_locks_the_persons_row_before_the_tickets(
    ending, people, tickets, accessioning
):
    """Deactivation and removal start from the person's row, as an assignment does."""
    owner, supervisor, person = people
    ticket_table = ServiceRequest._meta.db_table
    with CaptureQueriesContext(connection) as captured:
        ENDINGS[ending](owner, supervisor, person, accessioning)
    statements = [query["sql"] for query in captured]
    person_table = (
        User._meta.db_table if ending == "deactivation" else TeamMembership._meta.db_table
    )
    person_row = next(
        index
        for index, sql in enumerate(statements)
        if (sql.startswith("UPDATE") or " FOR " in sql) and f'"{person_table}"' in sql
    )
    ticket_lock = _first_lock(statements, ticket_table)
    assert ticket_lock is not None and person_row < ticket_lock


# Intake naming the same person --------------------------------------------------------------------


def _in_session(work, outcome, name, started):
    def run():
        try:
            _set_lock_timeout(LOCK_WAIT)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                outcome[f"{name}_pid"] = cursor.fetchone()[0]
            started.set()
            work()
            outcome[name] = "done"
        except Exception as exc:  # reported by the assertions
            outcome[name] = _outcome_of(exc)
        finally:
            started.set()
            connections.close_all()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def _until_blocked_or_ended(outcome, name, started, seconds=8):
    started.wait(timeout=seconds)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if name in outcome:
            return "ended"
        pid = outcome.get(f"{name}_pid")
        if pid is not None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", [pid])
                if cursor.fetchone()[0]:
                    return "waiting"
        time.sleep(0.02)
    return "neither"


@pytest.mark.parametrize("path", ["assign", "take", "bulk-assign"])
def test_intake_naming_the_person_waits_for_them_before_its_category(
    path, agent, make_user, people, tickets, accessioning, category, sample_category
):
    """An assignment holds the person and waits for its ticket; a colleague recategorizes that
    ticket into a category that routes to the person's team; a new ticket in that category names
    the same person as its assignee. Intake locks the assignee before the category, so it waits
    for the person holding nothing the recategorization needs: no deadlock, the recategorization
    and the new ticket are saved, and the assignment, which comes after the recategorization, is
    refused as changed."""
    _owner, _supervisor, person = people
    clerk = make_user("lock_order_clerk", "client_services")
    _held, waiting = tickets
    work = {
        "assign": lambda: _assign(agent, waiting, accessioning, person),
        "take": lambda: _take(person, waiting),
        "bulk-assign": lambda: services.bulk_assign(
            actor=agent, selected=selection([waiting]), team=accessioning, assignee=person
        ),
    }[path]
    outcome, seen, threads = {}, {}, []

    def recategorize():
        current = _fresh(waiting)
        services.update_properties(
            actor=clerk,
            request=current,
            expected_revision=current.revision,
            priority=current.priority,
            category=sample_category,
            reason="Sample problem",
        )

    def intake():
        new_request(clerk, sample_category, assignee=person)

    def during():
        for name, step in (("intake", intake), ("recategorize", recategorize)):
            started = threading.Event()
            threads.append(_in_session(step, outcome, name, started))
            seen[name] = _until_blocked_or_ended(outcome, name, started)

    real = services._hold_assignment

    def hold_then_pause(team, assignee):
        held = real(team, assignee)
        if not seen:
            during()
        return held

    _set_lock_timeout(LOCK_WAIT)
    try:
        with patch.object(services, "_hold_assignment", hold_then_pause):
            work()
        outcome["assignment"] = "done"
    except Exception as exc:  # reported by the assertions
        outcome["assignment"] = _outcome_of(exc)
    finally:
        _set_lock_timeout("0")
        for thread in threads:
            thread.join(timeout=20)
    results = {name: outcome.get(name) for name in ("intake", "recategorize", "assignment")}
    assert "40P01" not in results.values(), f"deadlock: {results}, during the pause {seen}"
    assert seen == {"intake": "waiting", "recategorize": "ended"}, (seen, results)
    assert results == {"intake": "done", "recategorize": "done", "assignment": "refused"}
    assert _fresh(waiting).category_id == sample_category.pk
    assert ServiceRequest.objects.filter(category=sample_category, assignee=person).count() == 1
