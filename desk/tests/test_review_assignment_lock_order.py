"""Independent review of the assignment lock order (candidate 3032c44), round 1, adopted with the
team cases of the held-reading check adapted to the round 1 resolution (the team is locked after
the ticket). All records are synthetic.

Each test states intended behaviour. The races run every party in its own PostgreSQL session (a
thread with its own connection), with a bounded lock_timeout, and pause the assignment at a named
point of desk.services until the other sessions wait for a lock or have finished. A test that
uses a helper only the candidate has is skipped on the base.
"""

import re
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django import forms
from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, connection, connections, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import User
from core.models import AuditEvent
from desk import notifications, services
from desk.models import EntryKind, ServiceRequest, Status
from jobs import outbox
from jobs.actor import system_account
from jobs.models import Notice
from teams import services as team_services
from teams.models import Team, TeamMembership

from .test_contract import new_request, selection, set_status

pytestmark = pytest.mark.django_db(transaction=True)

LOCK_WAIT = "8s"  # the races: long enough for the other sessions; a mistake fails, never hangs
PROBE_WAIT = "450ms"  # a probe: does this write wait for a lock someone holds?
CANDIDATE = hasattr(services, "_hold_assignment")
candidate_only = pytest.mark.skipif(not CANDIDATE, reason="uses a helper only the candidate has")


@pytest.fixture(autouse=True)
def locmem_mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


# Sessions ---------------------------------------------------------------------------------------


def _outcome_of(exc):
    if isinstance(exc, OperationalError):
        return getattr(exc.__cause__, "sqlstate", None) or "database error"
    if isinstance(exc, ValidationError):
        return "refused"
    if isinstance(exc, PermissionDenied):
        return "denied"
    if isinstance(exc, ServiceRequest.DoesNotExist):
        return "not found"
    return repr(exc)


def _set_lock_timeout(value):
    with connection.cursor() as cursor:
        cursor.execute(f"SET lock_timeout = '{value}'")


def _blocked(pid):
    with connection.cursor() as cursor:
        cursor.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", [pid])
        return cursor.fetchone()[0]


class Session:
    """`work` in its own PostgreSQL session."""

    def __init__(self, work, lock_timeout=LOCK_WAIT):
        self.work, self.lock_timeout = work, lock_timeout
        self.pid, self.outcome = None, None
        self.connected, self.ended = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            _set_lock_timeout(self.lock_timeout)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                self.pid = cursor.fetchone()[0]
            self.connected.set()
            self.work()
            self.outcome = "done"
        except Exception as exc:  # reported by the assertions
            self.outcome = _outcome_of(exc)
        finally:
            self.connected.set()
            self.ended.set()
            connections.close_all()

    def start(self):
        self.thread.start()
        return self

    def waiting_or_ended(self, seconds=8):
        """Return "waiting" once the session waits for a lock, "ended" once it has finished."""
        self.connected.wait(timeout=seconds)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.ended.is_set():
                return "ended"
            if self.pid is not None and _blocked(self.pid):
                return "waiting"
            time.sleep(0.02)
        return "neither"

    def join(self, seconds=30):
        self.thread.join(timeout=seconds)
        return self.outcome


def paused(name, during, *, before):
    """Patch desk.services.`name` to run `during()` once, before or after its first call."""
    real = getattr(services, name)
    fired = []

    def wrapper(*args, **kwargs):
        if before and not fired:
            fired.append(True)
            during()
        result = real(*args, **kwargs)
        if not before and not fired:
            fired.append(True)
            during()
        return result

    return patch.object(services, name, wrapper)


def run_here(work):
    """`work` in this session, with the races' lock_timeout; its outcome."""
    _set_lock_timeout(LOCK_WAIT)
    try:
        work()
        return "done"
    except Exception as exc:  # reported by the assertions
        return _outcome_of(exc)
    finally:
        _set_lock_timeout("0")


def probe_waits(write):
    """Whether `write`, run in another session up to its commit and then rolled back, waits for a
    lock someone holds (lock_timeout PROBE_WAIT)."""

    def work():
        with transaction.atomic():
            write()
            connection.check_constraints()
            transaction.set_rollback(True)

    outcome = Session(work, lock_timeout=PROBE_WAIT).start().join()
    assert outcome in ("done", "55P03"), outcome
    return outcome == "55P03"


def _fresh(ticket):
    return ServiceRequest.objects.get(pk=ticket.pk)


def _assign(actor, ticket, team, person=None):
    ticket = _fresh(ticket)
    return services.assign(
        actor=actor, request=ticket, expected_revision=ticket.revision, team=team, assignee=person
    )


def _take(actor, ticket):
    ticket = _fresh(ticket)
    return services.take(actor=actor, request=ticket, expected_revision=ticket.revision)


# People -----------------------------------------------------------------------------------------


@pytest.fixture
def person(make_user, join, accessioning):
    user = make_user("review_technician", "technical")
    join(user, accessioning)
    return user


@pytest.fixture
def colleague(make_user, join, accessioning):
    user = make_user("review_colleague", "technical")
    join(user, accessioning)
    return user


@pytest.fixture
def clerk(make_user):
    return make_user("review_clerk", "client_services")


# R01. Assignment, recategorizing and intake -----------------------------------------------------
#
# The candidate's assignment locks the team and then the ticket. Recategorizing a ticket
# (update_properties) locks the ticket and then the category, and creating a ticket
# (create_request) locks the category and then its team. On the base the assignment locked the
# ticket first, so the three took their locks in one order: ticket, category, team.


def _assignment_paths(person, agent, ticket, accessioning):
    return {
        "assign": ("_staff_request", lambda: _assign(agent, ticket, accessioning, person)),
        "take": ("_staff_request", lambda: _take(person, ticket)),
        "bulk-assign": (
            "_bulk_requests",
            lambda: services.bulk_assign(
                actor=agent, selected=selection([ticket]), team=accessioning, assignee=person
            ),
        ),
    }


@pytest.mark.parametrize("path", ["assign", "take", "bulk-assign"])
def test_assignment_recategorizing_and_intake_never_wait_in_a_cycle(
    path, agent, clerk, person, category, sample_category, accessioning
):
    """An assignment to Accessioning, a recategorization of the same ticket into a category that
    routes to Accessioning, and a new ticket in that category, at the same moment. Each may wait
    for another, never in a cycle: none ends with a deadlock (40P01), and the recategorization
    and the new ticket are saved. The assignment is paused just before it locks the ticket; the
    new ticket starts, then the recategorization, then the assignment goes on."""
    ticket = new_request(agent, category, team=accessioning)  # in its queue, plain category
    lock_point, work = _assignment_paths(person, agent, ticket, accessioning)[path]
    seen, sessions = {}, {}

    def recategorize():
        current = _fresh(ticket)
        services.update_properties(
            actor=clerk,
            request=current,
            expected_revision=current.revision,
            priority=current.priority,
            category=sample_category,
            reason="Sample problem",
        )

    def during():
        sessions["intake"] = Session(lambda: new_request(clerk, sample_category)).start()
        seen["intake"] = sessions["intake"].waiting_or_ended()
        sessions["recategorize"] = Session(recategorize).start()
        seen["recategorize"] = sessions["recategorize"].waiting_or_ended()

    with paused(lock_point, during, before=True):
        assignment = run_here(work)
    outcome = {name: session.join() for name, session in sessions.items()}
    outcome["assignment"] = assignment
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}, during the pause {seen}"
    assert outcome["intake"] == "done", (outcome, seen)
    assert outcome["recategorize"] == "done", (outcome, seen)
    # The recategorization changed the ticket, so an assignment that comes after it is refused.
    assert outcome["assignment"] in ("done", "refused"), outcome


# R02. An assignment waiting for its ticket holds the team -------------------------------------


@pytest.fixture
def notice_on_ticket(agent, category, accessioning, person):
    """A ticket of Accessioning assigned to `person`, with its staff notice waiting to be sent."""
    ticket = new_request(agent, category, team=accessioning)
    with patch.object(outbox, "send_now", return_value={}):
        _assign(agent, ticket, accessioning, person)
    row = Notice.objects.get(recipient=person, topic=notifications.TOPIC)
    return SimpleNamespace(ticket=ticket, row=row)


@pytest.mark.parametrize("other_work", ["assign-another-ticket", "new-ticket-in-the-team"])
def test_a_reassignment_waiting_for_a_notice_does_not_hold_up_the_team(
    other_work,
    notice_on_ticket,
    agent,
    clerk,
    category,
    sample_category,
    accessioning,
    colleague,
    make_user,
    join,
):
    """While a staff notice about a ticket is being sent, its team is held only against members
    and leads changing, so assigning other tickets goes on (desk/notifications.py). Someone
    reassigning the notice's ticket waits for the send; the rest of the team's work must not wait
    behind them: assigning another ticket of the team and creating a ticket in it."""
    third = make_user("review_third", "technical")
    join(third, accessioning)
    other = new_request(agent, category, team=accessioning)
    sending, release = threading.Event(), threading.Event()

    def send():
        with transaction.atomic():
            assert notifications.prepare_request_notice(notice_on_ticket.row) is not None
            sending.set()
            release.wait(timeout=20)

    holder = Session(send).start()
    try:
        assert sending.wait(timeout=10)
        reassignment = Session(
            lambda: _assign(agent, notice_on_ticket.ticket, accessioning, colleague)
        ).start()
        assert reassignment.waiting_or_ended() == "waiting"  # for the notice being sent
        if other_work == "assign-another-ticket":
            waited = probe_waits(lambda: _assign(agent, other, accessioning, third))
        else:
            # Another category that routes to the team: the notice holds its own ticket's
            # category, which intake in that category waits for (a separate, older hold).
            waited = probe_waits(lambda: new_request(clerk, sample_category))
    finally:
        release.set()
        holder.join()
    assert reassignment.join() == "done"
    assert not waited, "the team's other work waited behind the reassignment"


# R03. An owner stepping down against another owner's change -----------------------------------


def _admin_change_data(http, user, **changes):
    """The account page's form as drawn for `user`, with `changes`: what the browser posts."""
    response = http.get(reverse("admin:accounts_user_change", args=[user.pk]))
    assert response.status_code == 200
    form = response.context["adminform"].form
    data = {}
    for name, field in form.fields.items():
        if field.disabled:
            continue
        value = form[name].value()
        if isinstance(field.widget, forms.MultiWidget):
            parts = field.widget.decompress(value)
            for index, part in enumerate(parts):
                data[f"{name}_{index}"] = "" if part is None else str(part)
        elif isinstance(value, (list, tuple)):
            data[name] = [str(item) for item in value]
        elif isinstance(value, bool):
            if value:
                data[name] = "on"
        else:
            data[name] = "" if value is None else value
    for name, value in changes.items():
        if value is False:
            data.pop(name, None)
        else:
            data[name] = value
    return data


@pytest.fixture
def owners(make_user, join, accessioning):
    """Three owners (active staff superusers with the admin role), in id order. The second holds
    a ticket of Accessioning."""
    made = [
        make_user(f"review_owner_{n}", "admin", is_staff=True, is_superuser=True) for n in (1, 2, 3)
    ]
    for owner in made:
        join(owner, accessioning)
    return made


def _deactivate_in_admin(http, account):
    data = _admin_change_data(http, account, is_active=False)

    def post():
        response = http.post(reverse("admin:accounts_user_change", args=[account.pk]), data)
        assert response.status_code == 302, response.content[:2000]

    return post


def test_deactivating_an_owner_does_not_deadlock_with_another_owner_reassigning_their_ticket(
    owners, agent, category, accessioning, person
):
    """An owner deactivates a second owner in account administration while a third owner hands
    the second owner's ticket to a technician. Deactivation returns the person's open tickets to
    their queues and an assignment serializes with it (docs/architecture.md, Services and hooks:
    "Neither fails with a deadlock"). Paused once the assignment holds the ticket: the
    deactivation waits for it, then the assignment commits."""
    first, second, third = owners
    ticket = new_request(agent, category, team=accessioning, assignee=second)
    http = Client()
    http.force_login(first)
    deactivate = _deactivate_in_admin(http, second)
    seen = {}

    def during():
        seen["deactivation"] = Session(deactivate).start()
        seen["during_pause"] = seen["deactivation"].waiting_or_ended()

    with paused("_staff_request", during, before=False):
        assignment = run_here(lambda: _assign(third, ticket, accessioning, person))
    outcome = {"assignment": assignment, "deactivation": seen["deactivation"].join()}
    assert seen["during_pause"] == "waiting", (seen, outcome)
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}"
    assert outcome == {"assignment": "done", "deactivation": "done"}
    assert _fresh(ticket).assignee_id == person.pk
    assert not User.objects.get(pk=second.pk).is_active


def test_an_owner_taking_the_ticket_of_an_owner_being_deactivated_does_not_deadlock(
    owners, agent, category, accessioning
):
    """The variant the new order fixes: the third owner takes the second owner's ticket while the
    first deactivates the second. The take holds its own account before the ticket, so the
    deactivation (which locks every owner first) waits for it."""
    first, second, third = owners
    ticket = new_request(agent, category, team=accessioning, assignee=second)
    http = Client()
    http.force_login(first)
    deactivate = _deactivate_in_admin(http, second)
    seen = {}

    def during():
        seen["deactivation"] = Session(deactivate).start()
        seen["during_pause"] = seen["deactivation"].waiting_or_ended()

    with paused("_staff_request", during, before=False):
        taking = run_here(lambda: _take(third, ticket))
    outcome = {"take": taking, "deactivation": seen["deactivation"].join()}
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}"
    assert outcome == {"take": "done", "deactivation": "done"}, outcome
    assert _fresh(ticket).assignee_id == third.pk


# R04. Bulk status and close by an account deactivated while its request waited ----------------


@pytest.mark.parametrize("action", ["bulk_status", "bulk_close", "bulk_assign"])
def test_a_bulk_action_by_an_account_deactivated_meanwhile_is_refused_as_who_asks(
    action, make_user, category, accessioning, person
):
    """The acting account is read again after the change's locks: an account deactivated while
    its request waited is refused (desk/services.py current_staff, settle), before anything about
    the tickets. `actor` is the copy the request began with; the account is deactivated in the
    database before the service runs, as if it happened while the request waited."""
    actor = make_user("review_bulk_actor", "supervisor")
    ticket = new_request(actor, category, team=accessioning)
    if action == "bulk_close":
        services.change_status(
            actor=actor,
            request=_fresh(ticket),
            expected_revision=_fresh(ticket).revision,
            status=Status.RESOLVED,
            summary="Result resent",
        )
    before = _fresh(ticket).revision
    stored = User.objects.get(pk=actor.pk)
    stored.is_active = False
    stored.save()
    calls = {
        "bulk_status": lambda: services.bulk_status(
            actor=actor, selected=selection([ticket]), status=Status.WAITING_CLIENT
        ),
        "bulk_close": lambda: services.bulk_close(actor=actor, selected=selection([ticket])),
        "bulk_assign": lambda: services.bulk_assign(
            actor=actor, selected=selection([ticket]), team=accessioning, assignee=person
        ),
    }
    with pytest.raises(PermissionDenied):
        calls[action]()
    assert _fresh(ticket).revision == before


@pytest.mark.parametrize("problem", ["closed-ticket", "list-out-of-date"])
def test_bulk_assign_answers_who_asks_before_what_is_wrong_with_the_selection(
    problem, make_user, category, accessioning, person
):
    """Who asks is answered before what is wrong (desk/services.py _assign): an account
    deactivated while its request waited is refused as such, not told that a ticket is closed or
    that the list is out of date."""
    actor = make_user("review_bulk_asker", "supervisor")
    ticket = new_request(actor, category, team=accessioning)
    chosen = selection([ticket])
    if problem == "closed-ticket":
        set_status(ticket, Status.CLOSED)  # a queryset update: the revision stays
        chosen = selection([ticket])
    else:
        ServiceRequest.objects.filter(pk=ticket.pk).update(revision=chosen[ticket.pk] + 1)
    User.objects.filter(pk=actor.pk).update(is_active=False)
    with pytest.raises(PermissionDenied):
        services.bulk_assign(actor=actor, selected=chosen, team=accessioning, assignee=person)


# Checked: decisions from the held reading -------------------------------------------------------


def _probe_changes(person, accessioning):
    """Changes to the rows an assignment decides from, by paths that bypass the services."""
    role = Group.objects.get(name="technical")
    membership = TeamMembership.objects.filter(team=accessioning, user=person)
    return {
        "account-update": lambda: User.objects.filter(pk=person.pk).update(is_active=False),
        "role-removed": lambda: User.objects.get(pk=person.pk).groups.remove(role),
        "membership-deleted": lambda: membership.delete(),
        "team-update": lambda: Team.objects.filter(pk=accessioning.pk).update(is_active=False),
        "team-made-default": lambda: team_services.update_team(
            actor=User.objects.get(username="review_admin"),
            team_id=accessioning.pk,
            expected_revision=Team.objects.get(pk=accessioning.pk).revision,
            reason="Make it the default",
            is_default=True,
        ),
    }


@candidate_only
@pytest.mark.parametrize("change", ["account-update", "role-removed", "membership-deleted"])
def test_a_held_reading_holds_while_the_assignment_waits_for_its_ticket(
    change, agent, make_user, category, accessioning, person
):
    """Paused after the person's rows are held and before the ticket: every change to the
    person's rows the assignment decides from waits, including those that bypass the services.
    (Adopted with the team cases moved to the next test: after round 1 the team is locked after
    the ticket, R01 and R02.)"""
    make_user("review_admin", "admin")
    ticket = new_request(agent, category, team=accessioning)
    waited = {}

    def during():
        waited[change] = probe_waits(_probe_changes(person, accessioning)[change])

    with paused("_hold_assignment", during, before=False):
        assert run_here(lambda: _assign(agent, ticket, accessioning, person)) == "done"
    assert waited == {change: True}


@candidate_only
@pytest.mark.parametrize(
    ("change", "expected"), [("team-update", "refused"), ("team-made-default", "done")]
)
def test_a_team_change_while_the_person_is_held_is_read_under_the_teams_own_lock(
    change, expected, agent, make_user, category, accessioning, person
):
    """Paused after the person's rows are held: the team is not held yet (it is locked after the
    ticket, as the other ticket services do, so a waiting assignment holds up only work about the
    same person). A change to the team commits, and the assignment decides on the team as read
    under its lock: an inactive team is refused saying so, nothing saved; a team made the default
    takes the ticket."""
    make_user("review_admin", "admin")
    ticket = new_request(agent, category, team=accessioning)
    before = _fresh(ticket).revision
    waited = {}

    def during():  # committed, with the probes' short lock_timeout: "55P03" if it waited
        made = Session(_probe_changes(person, accessioning)[change], lock_timeout=PROBE_WAIT)
        waited[change] = made.start().join()

    refusal = []

    def assign():
        try:
            _assign(agent, ticket, accessioning, person)
        except ValidationError as exc:
            refusal.extend(exc.messages)
            raise

    with paused("_hold_assignment", during, before=False):
        outcome = run_here(assign)
    assert waited == {change: "done"}
    assert outcome == expected
    if expected == "refused":
        assert refusal == ["This team is inactive."]
        assert (_fresh(ticket).revision, _fresh(ticket).assignee_id) == (before, None)
    else:
        assert _fresh(ticket).assignee_id == person.pk


@candidate_only
@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("account-update", "Choose an active member of the ticket's team."),
        ("role-removed", "Choose an active member of the ticket's team."),
        ("membership-deleted", "Choose an active member of the ticket's team."),
        ("team-update", "This team is inactive."),
    ],
)
def test_a_change_before_the_hold_is_read_and_refused(
    change, expected, agent, category, accessioning, person
):
    """Paused after the unlocked scope check and before any lock: the change commits, and the
    assignment reads it under its locks and is refused saying why, with nothing saved."""
    ticket = new_request(agent, category, team=accessioning)
    before = _fresh(ticket).revision
    made = {}

    def during():
        session = Session(_probe_changes(person, accessioning)[change]).start()
        made[change] = session.join()

    with paused("_visible_request", during, before=False):
        with pytest.raises(ValidationError) as refused:
            _assign(agent, ticket, accessioning, person)
    assert made == {change: "done"}
    assert expected in " ".join(refused.value.messages)
    assert _fresh(ticket).revision == before
    assert _fresh(ticket).assignee_id is None


@candidate_only
@pytest.mark.parametrize("change", ["moved", "moved-out-of-sight", "closed-quietly"])
def test_take_when_the_ticket_changes_while_it_holds_the_taker(
    change, agent, make_user, category, privacy_category, accessioning, quality, person
):
    """Take reads the ticket's team before its lock. A move advances the revision and is refused
    as changed; a move to a restricted ticket of a team the taker is not in answers as missing
    (who asks first); a ticket closed without a new revision is refused as closed. Nothing is
    saved."""
    supervisor = make_user("review_mover", "supervisor")
    ticket = new_request(agent, category, team=accessioning)
    expected_revision = _fresh(ticket).revision

    def move():
        if change == "closed-quietly":
            set_status(ServiceRequest.objects.get(pk=ticket.pk), Status.CLOSED)
            return
        if change == "moved-out-of-sight":
            ServiceRequest.objects.filter(pk=ticket.pk).update(category=privacy_category)
        _assign(supervisor, ticket, quality)

    made = {}

    def during():
        made["move"] = Session(move).start().join()

    with paused("_hold_assignment", during, before=False):
        outcome = run_here(
            lambda: services.take(
                actor=person, request=_fresh(ticket), expected_revision=expected_revision
            )
        )
    assert made == {"move": "done"}
    assert (
        outcome
        == {
            "moved": "refused",
            "moved-out-of-sight": "not found",
            "closed-quietly": "refused",
        }[change]
    )
    assert _fresh(ticket).assignee_id is None


@candidate_only
@pytest.mark.parametrize("change", ["hidden", "revised"])
def test_bulk_assign_decides_the_selection_again_under_the_locks(
    change, agent, make_user, category, privacy_category, accessioning, quality, person
):
    """The selection is checked without a lock before the person's rows and again under the
    tickets' locks: a ticket that left the actor's sight or changed meanwhile refuses the whole
    action, and nothing is saved."""
    actor = make_user("review_bulk_operator", "client_services")
    first = new_request(agent, category, team=accessioning)
    second = new_request(agent, category, team=accessioning)
    chosen = selection([first, second])

    def change_second():
        if change == "hidden":
            ServiceRequest.objects.filter(pk=second.pk).update(
                category=privacy_category, team=quality
            )
        else:
            _assign(agent, second, quality)

    made = {}

    def during():
        made["change"] = Session(change_second).start().join()

    with paused("_hold_assignment", during, before=False):
        outcome = run_here(
            lambda: services.bulk_assign(
                actor=actor, selected=chosen, team=accessioning, assignee=person
            )
        )
    assert made == {"change": "done"}
    assert outcome == ("denied" if change == "hidden" else "refused")
    assert _fresh(first).assignee_id is None


# Checked: who asks, and what an outsider can learn ----------------------------------------------


@pytest.fixture
def outsiders(make_user, clinic_portal, join, quality):
    no_role = make_user("review_no_role", is_staff=True)
    outside = make_user("review_outside", "technical")  # not in Quality
    deactivated = make_user("review_deactivated", "technical")
    User.objects.filter(pk=deactivated.pk).update(is_active=False)
    return {
        "staff-without-role": no_role,
        "outside-restricted-team": outside,
        "portal-account": clinic_portal,
        "jobs-account": system_account(),
        "deactivated": User.objects.get(pk=deactivated.pk),
    }


AUDIENCES = [
    "staff-without-role",
    "outside-restricted-team",
    "portal-account",
    "jobs-account",
    "deactivated",
]
PERSON_TABLES = {
    Team._meta.db_table,
    User._meta.db_table,
    User.groups.through._meta.db_table,
    TeamMembership._meta.db_table,
}


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


@pytest.fixture
def restricted(make_user, privacy_category, quality, join):
    supervisor = make_user("review_quality_supervisor", "supervisor")
    member = make_user("review_quality_member", "technical")
    join(member, quality)
    ticket = new_request(supervisor, privacy_category, team=quality)
    return SimpleNamespace(ticket=ticket, member=member, supervisor=supervisor)


def _calls(actor, ticket, team, person):
    return {
        "assign": lambda: _assign(actor, ticket, team, person),
        "take": lambda: _take(actor, ticket),
        "bulk-assign": lambda: services.bulk_assign(
            actor=actor, selected=selection([ticket]), team=team, assignee=person
        ),
    }


@pytest.mark.parametrize("call", ["assign", "take", "bulk-assign"])
@pytest.mark.parametrize("audience", AUDIENCES)
def test_an_outsider_is_refused_before_any_persons_row_and_without_waiting(
    audience, call, outsiders, restricted, quality
):
    """Someone who may not read the ticket is refused before any team or person lock, the same
    way whatever is wrong with the choice, and without waiting while the chosen person's account
    is held by another change."""
    actor = outsiders[audience]
    held, release = threading.Event(), threading.Event()

    def hold_the_member():
        with transaction.atomic():
            list(User.objects.select_for_update().filter(pk=restricted.member.pk))
            held.set()
            release.wait(timeout=20)

    holder = Session(hold_the_member).start()
    try:
        assert held.wait(timeout=10)
        _set_lock_timeout(PROBE_WAIT)
        refusals = []
        for team_active in (True, False):
            Team.objects.filter(pk=quality.pk).update(is_active=team_active)
            with CaptureQueriesContext(connection) as captured:
                try:
                    _calls(actor, restricted.ticket, quality, restricted.member)[call]()
                    refusals.append("done")
                except Exception as exc:  # compared below
                    refusals.append((type(exc).__name__, str(exc)))
            locks = [
                query["sql"] for query in captured if _locked_tables(query["sql"]) & PERSON_TABLES
            ]
            assert locks == [], locks
    finally:
        _set_lock_timeout("0")
        release.set()
        holder.join()
    assert refusals[0] == refusals[1], refusals
    assert refusals[0] != "done"
    assert "55P03" not in str(refusals)
    assert _fresh(restricted.ticket).assignee_id is None


@pytest.mark.parametrize("page", ["assign", "take", "bulk"])
@pytest.mark.parametrize("audience", AUDIENCES[:3])
def test_pages_refuse_an_outsider_without_saying_anything_about_the_ticket(
    page, audience, outsiders, restricted, quality, client
):
    """The assign page, Take and the list's bulk Assign, posted directly by someone who may not
    read the ticket: refused (403 or 404, or for the list the same message for any hidden
    ticket), and nothing saved."""
    actor = outsiders[audience]
    client.force_login(actor)
    ticket = _fresh(restricted.ticket)
    target = f"{quality.pk}:{restricted.member.pk}"
    if page == "assign":
        response = client.post(
            reverse("desk:request-assign", args=[ticket.pk]),
            {"assign_to": target, "expected_revision": ticket.revision, "reason": ""},
        )
        assert response.status_code in (403, 404)
    elif page == "take":
        response = client.post(
            reverse("desk:request-take", args=[ticket.pk]),
            {"expected_revision": ticket.revision},
        )
        assert response.status_code in (403, 404)
    else:
        response = client.post(
            reverse("desk:request-bulk"),
            {
                "action": "assign",
                "assign_to": target,
                "requests": [f"{ticket.pk}:{ticket.revision}"],
            },
        )
        assert response.status_code in (302, 403)
        if response.status_code == 302:
            shown = [str(message) for message in get_messages(response.wsgi_request)]
            assert shown == ["Some selected tickets are not available to you."], shown
    assert _fresh(ticket).revision == ticket.revision
    assert _fresh(ticket).assignee_id is None


# Checked: history when an assignment and a deactivation meet ------------------------------------


@candidate_only
def test_an_assignment_then_a_deactivation_leave_both_changes_in_the_history(
    agent, make_user, category, accessioning, person
):
    """The assignment comes first and the deactivation waits, then returns the ticket to the
    queue: two timeline events and two audit events, each naming its actor, and two revisions."""
    owner = make_user("review_history_owner", "admin", is_staff=True, is_superuser=True)
    ticket = new_request(agent, category, team=accessioning)
    before = _fresh(ticket).revision

    def deactivate():
        account = User.objects.get(pk=person.pk)
        account.is_active, account.changed_by = False, owner
        account.save()

    seen = {}

    def during():
        seen["deactivation"] = Session(deactivate).start()
        seen["during_pause"] = seen["deactivation"].waiting_or_ended()

    with paused("_hold_assignment", during, before=False):
        assert run_here(lambda: _assign(agent, ticket, accessioning, person)) == "done"
    assert seen["deactivation"].join() == "done"
    assert seen["during_pause"] == "waiting"
    current = _fresh(ticket)
    assert current.assignee_id is None and current.revision == before + 2
    events = list(
        current.entries.filter(kind=EntryKind.EVENT, event_action="assigned")
        .order_by("pk")
        .values_list("author_id", "event_data")
    )
    assert [(author, data["assignee_id"]) for author, data in events] == [
        (agent.pk, person.pk),
        (owner.pk, None),
    ]
    audit = list(
        AuditEvent.objects.filter(action="request.assigned", object_id=str(ticket.pk))
        .order_by("occurred_at")
        .values_list("actor_id", "after")
    )
    assert [(actor, after["assignee_id"]) for actor, after in audit] == [
        (agent.pk, person.pk),
        (owner.pk, None),
    ]


# R05. A staff change that waits for a ticket being recategorized ---------------------------------


@pytest.mark.parametrize("path", ["assign", "take", "bulk-assign", "note"])
def test_a_change_waiting_for_a_recategorization_is_told_the_ticket_changed(
    path, agent, clerk, make_user, category, sample_category, accessioning, person
):
    """A technician's change waits for the ticket's lock while a colleague recategorizes it into
    another unrestricted category. Once the recategorization commits, the ticket is still one
    the technician may open: the change is refused as changed (the refusal the page shows with
    the input kept), never answered as a missing ticket."""
    ticket = new_request(agent, category, team=accessioning)
    seen = _fresh(ticket)
    holding, release = threading.Event(), threading.Event()

    def recategorize():
        with transaction.atomic():
            current = _fresh(ticket)
            services.update_properties(
                actor=clerk,
                request=current,
                expected_revision=current.revision,
                priority=current.priority,
                category=sample_category,
                reason="Sample problem",
            )
            holding.set()
            release.wait(timeout=20)

    calls = {
        "assign": lambda: services.assign(
            actor=person,
            request=seen,
            expected_revision=seen.revision,
            team=accessioning,
            assignee=person,
        ),
        "take": lambda: services.take(actor=person, request=seen, expected_revision=seen.revision),
        "bulk-assign": lambda: services.bulk_assign(
            actor=person, selected={seen.pk: seen.revision}, team=accessioning, assignee=person
        ),
        "note": lambda: services.add_entry(
            actor=person,
            request=seen,
            kind=EntryKind.NOTE,
            body="Called the clinic back.",
            expected_revision=seen.revision,
        ),
    }
    holder = Session(recategorize).start()
    assert holding.wait(timeout=10)
    change = Session(calls[path]).start()
    try:
        assert change.waiting_or_ended() == "waiting"
    finally:
        release.set()
    assert holder.join() == "done"
    assert change.join() == "refused"
    assert services.staff_requests(person).filter(pk=ticket.pk).exists()


@pytest.mark.parametrize("role", ["technical", "supervisor"])
def test_the_assign_page_keeps_the_form_when_the_ticket_was_recategorized_meanwhile(
    role, agent, clerk, make_user, join, category, sample_category, accessioning
):
    """The assign page posted while a colleague's recategorization holds the ticket: once it
    commits, the page is drawn again with the reason typed and the ticket-changed refusal, as for
    any other change saved first. A supervisor (whose scope has no category condition) is the
    control."""
    user = make_user(f"review_page_{role}", role)
    join(user, accessioning)
    ticket = new_request(agent, category, team=accessioning)
    seen = _fresh(ticket)
    http = Client()
    http.force_login(user)
    holding, release = threading.Event(), threading.Event()

    def recategorize():
        with transaction.atomic():
            current = _fresh(ticket)
            services.update_properties(
                actor=clerk,
                request=current,
                expected_revision=current.revision,
                priority=current.priority,
                category=sample_category,
                reason="Sample problem",
            )
            holding.set()
            release.wait(timeout=20)

    answer = {}

    def post():
        response = http.post(
            reverse("desk:request-assign", args=[seen.pk]),
            {
                "assign_to": f"{accessioning.pk}:{user.pk}",
                "expected_revision": seen.revision,
                "reason": "Synthetic reason typed by the reviewer",
            },
        )
        answer["status"] = response.status_code
        answer["body"] = response.content.decode()

    holder = Session(recategorize).start()
    assert holding.wait(timeout=10)
    page = Session(post).start()
    try:
        assert page.waiting_or_ended() == "waiting"
    finally:
        release.set()
    assert holder.join() == "done"
    assert page.join() == "done"
    assert answer["status"] == 200, answer["status"]
    assert "This ticket changed" in answer["body"]
    assert "Synthetic reason typed by the reviewer" in answer["body"]
