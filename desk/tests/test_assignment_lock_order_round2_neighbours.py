"""Regression tests for the cases next to the fixes of verification findings V01 to V05 of the
assignment lock order (candidate 5cd2ef1). All records are synthetic.

Each test states the rule it holds. The races run every party in its own PostgreSQL session (a
thread with its own connection) with a bounded lock_timeout; a party is paused at a named point of
desk.services, or holds its transaction open, until the others wait for a lock (seen with
pg_blocking_pids) or have finished.
"""

import threading
import time
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, connection, connections, transaction
from django.http import Http404
from django.test import Client as HttpClient
from django.urls import reverse

from accounts.models import User
from attachments.models import Attachment
from attachments.services import add_entry_with_files, withdraw_attachment
from attachments.tests.conftest import png_file
from clients.models import Client
from core.models import AuditEvent
from desk import services
from desk.models import EntryKind, RequestEntry, ServiceRequest, Status
from links import services as link_services
from links.models import ExternalSystem, LinkAttachment
from request_forms import definition
from request_forms.models import Audience, FieldKind, FieldValue, RequestTemplate
from request_forms.services import correct_values, create_request_with_form
from taxonomy import services as taxonomy_services
from taxonomy.models import RequestCategory
from taxonomy.terms import drawn
from teams import services as team_services
from teams.models import TeamMembership

from .test_contract import new_request

pytestmark = pytest.mark.django_db(transaction=True)

LOCK_WAIT = "8s"  # long enough for the other sessions; a mistake fails, never hangs
PROBE_WAIT = "450ms"  # a probe: does this write wait for a lock someone holds?
ROUTING_CHANGED = "The ticket's team changed while it was saved. Choose again."
CATEGORY_UNUSED = "This category is no longer used."


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
    if isinstance(exc, (ServiceRequest.DoesNotExist, TeamMembership.DoesNotExist, Http404)):
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
    """`work` in its own PostgreSQL session; `outcome`: "done" or what it raised."""

    def __init__(self, work, lock_timeout=LOCK_WAIT):
        self.work, self.lock_timeout = work, lock_timeout
        self.pid, self.outcome, self.error = None, None, None
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
            self.error = exc
            self.outcome = _outcome_of(exc)
        finally:
            self.connected.set()
            self.ended.set()
            connections.close_all()

    def start(self):
        self.thread.start()
        return self

    def waiting_or_ended(self, seconds=8):
        """ "waiting" once the session waits for a lock, "ended" once it has finished."""
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

    def messages(self):
        """What a ValidationError raised, as {field: [messages]} (or {"": [...]})."""
        exc = self.error
        assert isinstance(exc, ValidationError), repr(exc)
        return exc.message_dict if hasattr(exc, "error_dict") else {"": exc.messages}


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


class Held:
    """A session that runs `work` in a transaction and keeps it open until `release()`."""

    def __init__(self, work):
        self.holding, self.released = threading.Event(), threading.Event()

        def run():
            with transaction.atomic():
                work()
                self.holding.set()
                self.released.wait(timeout=20)

        self.session = Session(run).start()
        assert self.holding.wait(timeout=10), self.session.error

    def release(self):
        self.released.set()
        return self.session.join()


def _fresh(ticket):
    return ServiceRequest.objects.get(pk=ticket.pk)


def _assign(actor, ticket, team, person=None):
    ticket = _fresh(ticket)
    return services.assign(
        actor=actor, request=ticket, expected_revision=ticket.revision, team=team, assignee=person
    )


def _recategorize(actor, ticket, category):
    def work():
        current = _fresh(ticket)
        services.update_properties(
            actor=actor,
            request=current,
            expected_revision=current.revision,
            priority=current.priority,
            category=category,
            reason="Sample problem",
        )

    return work


def _held_while(hold, change, meanwhile):
    """`change` waits for a lock that `hold` (run in another session, kept open) takes;
    `meanwhile()` commits; the lock is released. Returns (whether the change waited, its
    Session)."""
    holder = Held(hold)
    session = Session(change).start()
    try:
        state = session.waiting_or_ended()
        meanwhile()
    finally:
        assert holder.release() == "done"
    session.join()
    return state, session


def _ticket_held_while(ticket, change, meanwhile):
    return _held_while(
        lambda: ServiceRequest.objects.select_for_update().get(pk=ticket.pk), change, meanwhile
    )


def _deactivate(user):
    def run():
        with transaction.atomic():
            account = User.objects.get(pk=user.pk)
            account.is_active = False
            account.save()

    return run


def _deactivate_bypassing_save(user):
    """A deactivation by a path that bypasses User.save (a queryset update). It takes a FOR NO KEY
    UPDATE of the account row, which the hold of accounts.acting (FOR KEY SHARE) does not make
    wait."""

    def run():
        User.objects.filter(pk=user.pk).update(is_active=False)

    return run


def _blocked_by(pid, other):
    """Whether session `pid` waits for a lock that session `other` holds."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [other, pid])
        return cursor.fetchone()[0]


def _held_with_access_change(hold, change, access_change):
    """`change` waits for a lock that `hold` takes (another session, kept open). Then
    `access_change` is started in its own session. The change holds the account's rows
    (accounts.acting), so the access change waits behind it. The lock is released and both are
    joined. Returns (whether the change waited, whether the access change waited, whether it
    waited behind the change, the change's Session, the access change's Session)."""
    holder = Held(hold)
    session = Session(change).start()
    try:
        state = session.waiting_or_ended()
        access = Session(access_change).start()
        access_state = access.waiting_or_ended()
        behind_change = access.pid is not None and _blocked_by(access.pid, session.pid)
    finally:
        assert holder.release() == "done"
    session.join()
    access.join()
    return state, access_state, behind_change, session, access


def _ticket_held_with_access_change(ticket, change, access_change):
    return _held_with_access_change(
        lambda: ServiceRequest.objects.select_for_update().get(pk=ticket.pk),
        change,
        access_change,
    )


def _grant(user):
    user.user_permissions.add(
        Permission.objects.get(
            codename="view_patient_information", content_type__app_label="accounts"
        )
    )


def _revoke(user):
    def run():
        user.user_permissions.clear()

    return run


# People and forms -------------------------------------------------------------------------------


@pytest.fixture
def person(make_user, join, accessioning, quality):
    user = make_user("r2_technician", "technical")
    join(user, accessioning)
    join(user, quality)
    return user


@pytest.fixture
def colleague(make_user, join, accessioning, quality):
    user = make_user("r2_colleague", "technical")
    join(user, accessioning)
    join(user, quality)
    return user


@pytest.fixture
def clerk(make_user):
    return make_user("r2_clerk", "client_services")


@pytest.fixture
def lead(make_user):
    """A supervisor, member of no team."""
    return make_user("r2_lead", "supervisor")


@pytest.fixture
def admin(make_user):
    return make_user("r2_setup", "admin")


@pytest.fixture
def staff_form(sample_category):
    """A staff request form in "Sample problem" (which routes to Accessioning)."""
    template = RequestTemplate.objects.create(
        key="r2-form", name="Round two form", category=sample_category, offered_in_portal=False
    )
    template.fields.create(key="note", label="Note", kind=FieldKind.SHORT_TEXT)
    return template


@pytest.fixture
def quality_ticket(agent, category, quality):
    return new_request(agent, category, team=quality, client=None)


def _form_intake(actor, template, **kwargs):
    """A ticket logged through the service with `template`'s form, the page drawn now."""
    token = definition.token(template)

    def work():
        create_request_with_form(
            actor=actor,
            template=template,
            expected_definition=token,
            values={"note": "Synthetic note"},
            first_entry_kind=EntryKind.NOTE,
            channel="phone",
            subject="Recollection needed",
            **kwargs,
        )

    return work


def _intake_page(actor, category, template, team, person):
    """The staff new-ticket page posted with the form and "Assign to" `team`: `person`."""
    data = {
        "category": category.pk,
        "category_terms": drawn([category]),
        "channel": "phone",
        "subject": "Recollection needed",
        "first_entry_kind": "note",
        "body": "Caller reports a leaking sample for order ORD-TEST-000042.",
        "request_form": template.pk,
        "form_definition": definition.token(template),
        "field-note": "Synthetic note",
        "assign_to": f"{team.pk}:{person.pk}",
    }

    def post():
        http = HttpClient()
        http.force_login(actor)
        response = http.post(reverse("desk:request-create"), data)
        assert response.status_code == 302, response.content[:3000]

    return post


def _change_default_team(admin, category, team):
    def run():
        current = RequestCategory.objects.get(pk=category.pk)
        taxonomy_services.update_category(
            actor=admin,
            category_id=current.pk,
            expected_revision=current.revision,
            reason="Rerouted",
            default_team=team,
        )

    return run


# V01/V02. Staff intake naming a person, an assignment to them and a recategorization -------------


def _intake_assignment_recategorization(
    *, intake, ticket, assign_in, agent, clerk, person, sample_category, subject_count=1
):
    """An assignment of `ticket` to `person` paused just after it holds the person's rows, an
    intake naming the person and a recategorization of the ticket, started in that order; then
    the assignment goes on. Returns the outcomes."""
    sessions, seen = {}, {}

    def during():
        sessions["intake"] = Session(intake).start()
        seen["intake"] = sessions["intake"].waiting_or_ended()
        sessions["recategorize"] = Session(_recategorize(clerk, ticket, sample_category)).start()
        seen["recategorize"] = sessions["recategorize"].waiting_or_ended()

    with paused("_hold_assignment", during, before=False):
        assignment = run_here(lambda: _assign(agent, ticket, assign_in, person))
    outcome = {name: session.join() for name, session in sessions.items()}
    outcome["assignment"] = assignment
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}, during the pause {seen}"
    assert outcome["intake"] == "done", (outcome, seen)
    assert outcome["recategorize"] == "done", (outcome, seen)
    assert outcome["assignment"] in ("done", "refused"), outcome
    assert seen["intake"] == "waiting", seen  # for the person the assignment holds
    assert _fresh(ticket).category_id == sample_category.pk
    assert (
        ServiceRequest.objects.filter(subject="Recollection needed", assignee=person).count()
        == subject_count
    )


def test_service_form_intake_naming_the_person_never_deadlocks_with_an_assignment_and_more(
    agent, clerk, person, category, sample_category, accessioning, staff_form
):
    """create_request_with_form naming a technician (the service, not the page), an assignment of
    a ticket to that technician and a recategorization of the ticket into the form's category, at
    the same moment: the intake takes the person's rows before the form's definition locks the
    category, so none ends with 40P01, and the new ticket and the recategorization are saved."""
    ticket = new_request(agent, category, team=accessioning)
    _intake_assignment_recategorization(
        intake=_form_intake(clerk, staff_form, team=accessioning, assignee=person),
        ticket=ticket,
        assign_in=accessioning,
        agent=agent,
        clerk=clerk,
        person=person,
        sample_category=sample_category,
    )


def test_page_form_intake_with_an_explicit_team_never_deadlocks_with_an_assignment(
    agent, clerk, person, category, sample_category, quality, staff_form
):
    """The staff new-ticket page with the request form and "Assign to" a team that is not the
    category's default (Quality; "Sample problem" routes to Accessioning), naming a technician
    member of both, against an assignment to that technician in Quality and a recategorization of
    the assigned ticket into the form's category: the page holds the person's membership of the
    chosen team before the form's locks; no 40P01, the new ticket and the recategorization are
    saved."""
    ticket = new_request(agent, category, team=quality)
    _intake_assignment_recategorization(
        intake=_intake_page(clerk, sample_category, staff_form, quality, person),
        ticket=ticket,
        assign_in=quality,
        agent=agent,
        clerk=clerk,
        person=person,
        sample_category=sample_category,
    )
    created = ServiceRequest.objects.get(subject="Recollection needed")
    assert created.team_id == quality.pk


def test_routing_changed_between_the_hold_and_the_category_lock_refuses_the_ticket(
    clerk, person, admin, quality, sample_category
):
    """A ticket naming a technician is logged without a team: the person's membership of the
    category's default team (Accessioning) is held first. If the category's default team is
    changed (committed) before the category's lock, the membership held is not the one of the team
    now decided: the creation is refused on `assignee` with the message that asks to choose
    again, and nothing is saved."""
    events_before = AuditEvent.objects.filter(action="request.created").count()

    def reroute():
        outcome = Session(_change_default_team(admin, sample_category, quality)).start().join()
        assert outcome == "done", outcome

    with paused("_check_category", reroute, before=True):
        with pytest.raises(ValidationError) as raised:
            new_request(clerk, sample_category, assignee=person)
    assert raised.value.message_dict == {"assignee": [ROUTING_CHANGED]}
    assert ServiceRequest.objects.count() == 0
    assert RequestEntry.objects.count() == 0
    assert AuditEvent.objects.filter(action="request.created").count() == events_before


def test_routing_changed_after_the_page_held_the_person_refuses_the_ticket(
    clerk, person, admin, quality, sample_category
):
    """The staff page's order without a form: it holds the person's rows itself
    (hold_intake_assignee, in the creating transaction) and passes the reading on as
    `held_assignee`. A default team changed (committed) before the category's lock refuses the
    creation on `assignee` with the same message, and nothing is saved."""

    def reroute():
        outcome = Session(_change_default_team(admin, sample_category, quality)).start().join()
        assert outcome == "done", outcome

    with paused("_check_category", reroute, before=True):
        with pytest.raises(ValidationError) as raised, transaction.atomic():
            held = services.hold_intake_assignee(
                team=None, assignee=person, category=sample_category
            )
            new_request(clerk, sample_category, assignee=person, held_assignee=held)
    assert raised.value.message_dict == {"assignee": [ROUTING_CHANGED]}
    assert ServiceRequest.objects.count() == 0


def test_a_form_intake_whose_routing_changed_after_the_hold_saves_nothing(
    clerk, person, admin, quality, sample_category, staff_form
):
    """create_request_with_form: the form's definition locks the category after the hold and
    signs its routing, so a default team changed (committed) between the hold and that lock is
    refused as the form having changed (a ValidationError, not a server error), with no ticket
    and no form value saved. The routing refusal itself cannot be reached here: the category's lock
    taken for the definition is held up to create_request's own."""

    def reroute():
        outcome = Session(_change_default_team(admin, sample_category, quality)).start().join()
        assert outcome == "done", outcome

    work = _form_intake(clerk, staff_form, assignee=person)
    with paused("hold_intake_assignee", reroute, before=False):
        with pytest.raises(ValidationError) as raised:
            work()
    assert "changed after the page was opened" in raised.value.messages[0]
    assert ServiceRequest.objects.count() == 0
    assert FieldValue.objects.count() == 0


def test_a_reading_held_for_another_person_is_refused(
    clerk, person, colleague, accessioning, sample_category
):
    """create_request(held_assignee=...) takes the reading of the request's own assignee only: one
    taken for another person raises ValueError (a programming error, never a silent assignment of
    the wrong person), and so does a reading without an assignee. Nothing is saved."""
    with transaction.atomic():
        held = services.hold_intake_assignee(
            team=accessioning, assignee=colleague, category=sample_category
        )
        with pytest.raises(ValueError, match="held_assignee is the reading of this request's"):
            new_request(clerk, sample_category, assignee=person, held_assignee=held)
        with pytest.raises(ValueError, match="held_assignee is the reading of this request's"):
            new_request(clerk, sample_category, held_assignee=held)
    assert ServiceRequest.objects.count() == 0


def test_form_intakes_naming_different_people_do_not_wait_for_each_other(
    clerk, make_user, person, colleague, sample_category, accessioning, staff_form
):
    """Two staff form intakes in the same category naming different technicians: one is paused
    right after it holds its person (it holds nothing else yet); the other, naming the colleague,
    completes without waiting, and so does a plain intake in the category naming nobody. Only an
    intake naming the same person waits (nothing about the category is held that early)."""
    other_clerk = make_user("r2_other_clerk", "client_services")
    seen, sessions = {}, {}
    # Drawn before: the page's token is read outside the paused intake's transaction.
    second = _form_intake(other_clerk, staff_form, team=accessioning, assignee=colleague)
    first_work = _form_intake(clerk, staff_form, team=accessioning, assignee=person)

    def during():
        sessions["second"] = Session(second).start()
        seen["second"] = sessions["second"].waiting_or_ended()
        seen["plain"] = probe_waits(lambda: new_request(other_clerk, sample_category))

    with paused("hold_intake_assignee", during, before=False):
        first = run_here(first_work)
    assert first == "done"
    assert seen == {"second": "ended", "plain": False}, seen
    assert sessions["second"].join() == "done"
    assigned = ServiceRequest.objects.filter(subject="Recollection needed")
    assert sorted(assigned.values_list("assignee__username", flat=True)) == [
        "r2_colleague",
        "r2_technician",
    ]


# V03. Intake naming a member, their removal, set_lead, their deactivation -------------------------


def _holds_ticket_while(change, work, during):
    """`work` paused just after it locked the ticket (`change` names the point), `during()` runs."""
    with paused(change, during, before=False):
        return run_here(work)


def test_intake_naming_a_member_their_removal_and_set_lead_never_deadlock(
    agent, clerk, lead, person, colleague, category, sample_category, accessioning
):
    """A technician holds a ticket of Accessioning; a colleague takes that ticket over (paused
    once it holds the ticket); a supervisor removes the technician from the team (waits for the
    ticket, holding the membership); another supervisor action sets the lead flag on that
    membership (waits for it); a clerk logs a ticket in "Sample problem" naming the technician
    (waits for the membership). None ends with 40P01; the removal and the takeover are saved, the
    lead change finds no membership, the intake is refused on the assignee, and the technician is
    left on no open ticket of the team."""
    ticket = new_request(agent, category, team=accessioning, assignee=person)
    membership = TeamMembership.objects.get(team=accessioning, user=person)
    seen, sessions = {}, {}

    def during():
        sessions["removal"] = Session(
            lambda: team_services.remove_member(actor=lead, membership=membership)
        ).start()
        seen["removal"] = sessions["removal"].waiting_or_ended()
        sessions["set_lead"] = Session(
            lambda: team_services.set_lead(actor=lead, membership=membership, is_lead=True)
        ).start()
        seen["set_lead"] = sessions["set_lead"].waiting_or_ended()
        sessions["intake"] = Session(
            lambda: new_request(clerk, sample_category, assignee=person)
        ).start()
        seen["intake"] = sessions["intake"].waiting_or_ended()

    change = _holds_ticket_while(
        "_staff_request", lambda: _assign(agent, ticket, accessioning, colleague), during
    )
    outcome = {name: session.join() for name, session in sessions.items()}
    outcome["change"] = change
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}, during the pause {seen}"
    assert seen == {"removal": "waiting", "set_lead": "waiting", "intake": "waiting"}, seen
    assert outcome == {
        "removal": "done",
        "set_lead": "not found",
        "intake": "refused",
        "change": "done",
    }
    assert sessions["intake"].messages() == {
        "assignee": ["Choose an active member of the ticket's team."]
    }
    assert not TeamMembership.objects.filter(pk=membership.pk).exists()
    assert not ServiceRequest.objects.filter(assignee=person).exclude(status=Status.CLOSED)
    assert _fresh(ticket).assignee_id == colleague.pk
    assert ServiceRequest.objects.count() == 1


def test_intake_naming_a_person_their_deactivation_and_a_reassignment_never_deadlock(
    agent, clerk, person, colleague, category, sample_category, accessioning
):
    """A technician holds two tickets of Accessioning; a colleague takes one of them over (paused
    once it holds that ticket); the technician's account is deactivated (waits for the ticket
    while returning their tickets to the queue); a clerk logs a ticket naming the technician
    (waits for the account). None ends with 40P01; the intake is refused on the assignee, and the
    end state leaves the deactivated technician on no open ticket and nothing assigned to them."""
    taken = new_request(agent, category, team=accessioning, assignee=person)
    kept = new_request(agent, category, team=accessioning, assignee=person)
    seen, sessions = {}, {}

    def during():
        sessions["deactivation"] = Session(_deactivate(person)).start()
        seen["deactivation"] = sessions["deactivation"].waiting_or_ended()
        sessions["intake"] = Session(
            lambda: new_request(clerk, sample_category, assignee=person)
        ).start()
        seen["intake"] = sessions["intake"].waiting_or_ended()

    change = _holds_ticket_while(
        "_staff_request", lambda: _assign(agent, taken, accessioning, colleague), during
    )
    outcome = {name: session.join() for name, session in sessions.items()}
    outcome["change"] = change
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}, during the pause {seen}"
    assert seen == {"deactivation": "waiting", "intake": "waiting"}, seen
    assert outcome == {"deactivation": "done", "intake": "refused", "change": "done"}
    assert sessions["intake"].messages() == {
        "assignee": ["Choose an active member of the ticket's team."]
    }
    assert not User.objects.get(pk=person.pk).is_active
    assert not ServiceRequest.objects.filter(assignee=person).exclude(status=Status.CLOSED)
    assert _fresh(taken).assignee_id == colleague.pk
    assert _fresh(kept).assignee_id is None
    assert ServiceRequest.objects.count() == 2


# V04. The category's lock read by its id alone ---------------------------------------------------


def _taxonomy_change(kind, admin, group, sample_category, quality):
    """What an administrator changes (taxonomy update_category), run in the holding session."""
    if kind == "default-team":
        return _change_default_team(admin, sample_category, quality)
    target = {"group-off": group, "category-off": sample_category}[kind]

    def run():
        current = RequestCategory.objects.get(pk=target.pk)
        taxonomy_services.update_category(
            actor=admin,
            category_id=current.pk,
            expected_revision=current.revision,
            reason="Retired",
            is_active=False,
        )

    return run


def _new_tickets():
    return ServiceRequest.objects.filter(subject__in=("Result not received", "Recollection needed"))


@pytest.mark.parametrize("kind", ["default-team", "group-off", "category-off"])
@pytest.mark.parametrize("change", ["intake", "intake-drawn", "recategorize", "form-intake"])
def test_a_change_waiting_for_an_administrators_taxonomy_change_decides_from_the_new_state(
    change,
    kind,
    admin,
    agent,
    clerk,
    category,
    group,
    sample_category,
    accessioning,
    quality,
    staff_form,
):
    """An administrator's taxonomy change holds the category's lock (a default team changed, the
    category deactivated; for the group's deactivation the lock is held by another session and
    the group is deactivated and committed meanwhile) while a ticket is logged in it, with or
    without the terms the page drew, through a request form, or another ticket is
    recategorized into it.
    The change waits for the lock and reads the category as the administrator left it, in new
    statements: never an AttributeError or a server error. With the group or the category
    deactivated every one is refused ("This category is no longer used.") and nothing is saved.
    With the default team changed, the page's drawn terms refuse the intake; the others route
    by the new default team or are refused, and the ticket is never saved in a half state."""
    ticket = new_request(agent, category, team=accessioning)
    drawn_terms = drawn([sample_category])  # the page, drawn before the change
    work = {
        "intake": lambda: new_request(clerk, sample_category),
        "intake-drawn": lambda: new_request(clerk, sample_category, drawn_terms=drawn_terms),
        "recategorize": _recategorize(clerk, ticket, sample_category),
        "form-intake": _form_intake(clerk, staff_form),
    }[change]
    before = (_fresh(ticket).category_id, _new_tickets().count())
    if kind == "group-off":
        # Deactivating a group does not lock its categories: another session holds the
        # category's row, the group is deactivated (committed) meanwhile, then the lock is let go.
        def deactivate_group():
            done = Session(_taxonomy_change(kind, admin, group, sample_category, quality))
            assert done.start().join() == "done", done.error

        def hold():
            RequestCategory.objects.select_for_update().get(pk=sample_category.pk)

        state, session = _held_while(hold, work, deactivate_group)
    else:
        change_held = _taxonomy_change(kind, admin, group, sample_category, quality)
        state, session = _held_while(change_held, work, lambda: None)
    assert state == "waiting", state
    saved = {
        "intake": _new_tickets().filter(category=sample_category),
        "intake-drawn": _new_tickets().filter(category=sample_category),
        "recategorize": ServiceRequest.objects.filter(pk=ticket.pk, category=sample_category),
        "form-intake": _new_tickets().filter(category=sample_category),
    }[change]
    if kind != "default-team":
        assert session.outcome == "refused", (session.outcome, repr(session.error))
        if change != "form-intake":  # the form's own refusals use their own words
            assert session.messages() == {"category": [CATEGORY_UNUSED]}
        assert not saved.exists() and before == (_fresh(ticket).category_id, _new_tickets().count())
        return
    if change == "intake-drawn":
        assert session.outcome == "refused", (session.outcome, repr(session.error))
        assert session.messages() == {"category": [taxonomy_terms_changed()]}
        assert not saved.exists()
    elif change == "intake":
        assert session.outcome == "done", (session.outcome, repr(session.error))
        assert saved.get().team_id == quality.pk  # routed by the new default team
    else:
        assert session.outcome in ("done", "refused"), (session.outcome, repr(session.error))
        assert saved.exists() == (session.outcome == "done")
        if change == "form-intake" and session.outcome == "done":
            assert saved.get().team_id == quality.pk


def taxonomy_terms_changed():
    from taxonomy.terms import CHANGED

    return CHANGED


# V05. The acting account read again after the lock -----------------------------------------------


@pytest.fixture
def lis(db):
    return ExternalSystem.objects.get_or_create(
        key="lis", defaults={"name": "Laboratory information system", "kind": "lis"}
    )[0]


@pytest.fixture
def capable(make_user):
    """Staff holding the patient-information capability."""
    user = make_user("r2_capable", "client_services")
    _grant(user)
    return user


def _link_state():
    return (
        LinkAttachment.objects.count(),
        LinkAttachment.objects.filter(removed_at__isnull=False).count(),
        LinkAttachment.objects.filter(shown_to_client=True).count(),
        RequestEntry.objects.count(),
    )


def _attach(actor, ticket, lis, record_type="order", identifier="ORD-TEST-000044"):
    attachment, _created = link_services.add_link(
        actor=actor,
        request_id=ticket.pk,
        system=lis,
        record_type=record_type,
        identifier=identifier,
    )
    return attachment


@pytest.mark.parametrize("action", ["show", "remove"])
def test_a_link_change_refuses_an_account_deactivated_while_it_waited_bypassing_save(
    action, make_user, agent, quality_ticket, lis
):
    """The account is deactivated while the change waits for the ticket's lock, by a path that
    bypasses User.save (a queryset update), which the hold of accounts.acting does not make
    wait. set_shown_to_client and remove_link read the acting account again after the ticket's
    lock and refuse it (PermissionDenied); the link, its flags and the timeline stay as they
    were."""
    clerk = make_user("r2_link_clerk", "client_services")
    attachment = _attach(agent, quality_ticket, lis)
    before = _link_state()
    work = {
        "show": lambda: link_services.set_shown_to_client(
            actor=clerk, attachment_id=attachment.pk, shown=True
        ),
        "remove": lambda: link_services.remove_link(
            actor=clerk, attachment_id=attachment.pk, reason="Wrong order"
        ),
    }[action]
    state, session = _ticket_held_while(quality_ticket, work, _deactivate_bypassing_save(clerk))
    assert state == "waiting", state
    assert session.outcome == "denied", (session.outcome, repr(session.error))
    assert _link_state() == before


def _patient_link_work(action, capable, quality_ticket, lis):
    """What the capable account does, and the link it acts on (None for add)."""
    attachment = (
        _attach(capable, quality_ticket, lis, "patient", "PAT-TEST-000001")
        if action == "remove"
        else None
    )
    work = {
        "remove": lambda: link_services.remove_link(
            actor=capable, attachment_id=attachment.pk, reason_code="wrong_person"
        ),
        "add": lambda: link_services.add_link(
            actor=capable,
            request_id=quality_ticket.pk,
            system=lis,
            record_type="patient",
            identifier="PAT-TEST-000002",
        ),
    }[action]
    return work, attachment


@pytest.mark.parametrize("action", ["remove", "add"])
def test_a_person_level_link_change_refuses_an_account_deactivated_while_it_waited_bypassing_save(
    action, capable, quality_ticket, lis
):
    """A person-level (patient) link needs the patient-information capability. The account is
    deactivated while the change waits for the ticket's lock, by a path that bypasses User.save
    (a queryset update), which the hold of accounts.acting does not make wait. remove_link and
    add_link check the capability after the ticket's lock on the account as it stands then, and
    refuse it (PermissionDenied); the link stays attached (remove) or nothing is attached (add),
    no timeline entry is written."""
    work, _attachment = _patient_link_work(action, capable, quality_ticket, lis)
    before = _link_state()
    state, session = _ticket_held_while(quality_ticket, work, _deactivate_bypassing_save(capable))
    assert state == "waiting", state
    assert session.outcome == "denied", (session.outcome, repr(session.error))
    assert _link_state() == before
    assert LinkAttachment.objects.filter(removed_at__isnull=False).count() == 0


@pytest.mark.parametrize("action", ["remove", "add"])
def test_a_capability_revocation_waits_for_a_person_level_link_change_under_way(
    action, capable, quality_ticket, lis
):
    """The patient-information capability is revoked while a person-level link change waits for
    the ticket's lock. The change holds the account's grants until it commits (accounts.acting),
    so the revocation waits behind it. The change is saved first, by an account that still had
    the capability (the link is removed, or attached), and the capability is gone afterwards."""
    work, attachment = _patient_link_work(action, capable, quality_ticket, lis)
    state, revoke_state, behind_change, session, revoke = _ticket_held_with_access_change(
        quality_ticket, work, _revoke(capable)
    )
    assert state == "waiting", state
    assert (revoke_state, behind_change) == ("waiting", True)
    assert (session.outcome, revoke.outcome) == ("done", "done"), repr(session.error)
    if action == "remove":
        assert LinkAttachment.objects.get(pk=attachment.pk).removed_at is not None
    else:
        added = LinkAttachment.objects.get(link__identifier="PAT-TEST-000002")
        assert added.removed_at is None
    assert not User.objects.get(pk=capable.pk).user_permissions.exists()


def test_a_link_to_an_organization_refuses_an_account_deactivated_while_it_waited_bypassing_save(
    make_user, lis, clinic
):
    """The account is deactivated while the link waits for the organization's lock, by a path
    that bypasses User.save (a queryset update), which the hold of accounts.acting does not make
    wait. add_link to a client organization reads the acting account again after the
    organization's lock and refuses it (PermissionDenied); no link is attached."""
    clerk = make_user("r2_org_clerk", "client_services")
    before = _link_state()
    state, session = _held_while(
        lambda: Client.objects.select_for_update().get(pk=clinic.pk),
        lambda: link_services.add_link(
            actor=clerk,
            client_id=clinic.pk,
            system=lis,
            record_type="order",
            identifier="ORD-TEST-000045",
        ),
        _deactivate_bypassing_save(clerk),
    )
    assert state == "waiting", state
    assert session.outcome == "denied", (session.outcome, repr(session.error))
    assert _link_state() == before


def test_a_person_level_link_to_an_organization_is_refused_not_saved(capable, lis, clinic):
    """The neighbour of the case above: a person-level link cannot be attached to an organization
    (it goes on a ticket); the capability is not enough, and nothing is saved."""
    with pytest.raises(ValidationError) as raised:
        link_services.add_link(
            actor=capable,
            client_id=clinic.pk,
            system=lis,
            record_type="patient",
            identifier="PAT-TEST-000003",
        )
    assert raised.value.message_dict == {"record_type": ["Add person-level links to a ticket."]}
    assert LinkAttachment.objects.count() == 0


def test_withdrawing_a_file_refuses_a_supervisor_deactivated_while_it_waited_bypassing_save(
    agent, lead, quality_ticket
):
    """withdraw_attachment by a supervisor whose account is deactivated while the withdrawal
    waits for the ticket's lock, by a path that bypasses User.save (a queryset update), which the
    hold of accounts.acting does not make wait: the withdrawal reads the account again after the
    lock and is refused (PermissionDenied), the file stays with clients, and no audit event,
    timeline entry or revision is written."""
    seen = _fresh(quality_ticket)
    add_entry_with_files(
        actor=agent,
        request=seen,
        kind=EntryKind.REPLY,
        body="Requisition attached.",
        expected_revision=seen.revision,
        files=[png_file()],
    )
    attachment = Attachment.objects.get()
    withdrawals = AuditEvent.objects.filter(object_id=str(attachment.pk)).count()
    before = (_fresh(quality_ticket).revision, RequestEntry.objects.count())
    state, session = _ticket_held_while(
        quality_ticket,
        lambda: withdraw_attachment(actor=lead, attachment_id=attachment.pk, reason="Wrong file"),
        _deactivate_bypassing_save(lead),
    )
    assert state == "waiting", state
    assert session.outcome == "denied", (session.outcome, repr(session.error))
    assert Attachment.objects.get(pk=attachment.pk).withdrawn_at is None
    assert before == (_fresh(quality_ticket).revision, RequestEntry.objects.count())
    assert AuditEvent.objects.filter(object_id=str(attachment.pk)).count() == withdrawals


@pytest.fixture
def mixed_form(category):
    """A form with a staff-only field and a patient-information field."""
    template = RequestTemplate.objects.create(
        key="r2-mixed", name="Mixed form", category=category, offered_in_portal=False
    )
    template.fields.create(
        key="internal", label="Internal", kind=FieldKind.SHORT_TEXT, audience=Audience.STAFF
    )
    template.fields.create(
        key="pt",
        label="Patient note",
        kind=FieldKind.SHORT_TEXT,
        audience=Audience.STAFF,
        holds_patient_information=True,
    )
    return template


def _raised_mixed(capable, quality, mixed_form):
    """A ticket raised with the mixed form (both fields "first"), its current values by field key
    and the form's token."""
    raised = create_request_with_form(
        actor=capable,
        template=mixed_form,
        expected_definition=definition.token(mixed_form),
        values={"internal": "first", "pt": "first"},
        first_entry_kind=EntryKind.NOTE,
        channel="phone",
        team=quality,
    )
    rows = {
        row.field.key: row
        for row in FieldValue.objects.filter(
            filled_form__request=raised, superseded_at__isnull=True
        ).select_related("field")
    }
    assert set(rows) == {"internal", "pt"}
    return raised, rows, definition.token(mixed_form)


def _correction(capable, raised, token, rows, field):
    return lambda: correct_values(
        actor=capable,
        request=raised,
        expected_definition=token,
        values={field: "second"},
        reason="Typo",
        reason_code="typing",
        expected={field: rows[field].pk},
    )


@pytest.mark.parametrize("field", ["internal", "pt"])
def test_a_form_correction_does_not_save_what_a_deactivated_account_may_no_longer_change(
    field, make_user, quality, mixed_form, capable
):
    """The account is deactivated while the correction waits for the ticket's lock, by a path
    that bypasses User.save (a queryset update), which the hold of accounts.acting does not make
    wait. correct_values reads the acting account again after the ticket's lock and decides the
    fields it may fill, and the patient capability, from that reading: the correction is refused
    (denied, or refused as nothing the account may change); the old value stays current and no
    new value, timeline entry or revision is written."""
    raised, rows, token = _raised_mixed(capable, quality, mixed_form)
    before = (_fresh(raised).revision, FieldValue.objects.count(), RequestEntry.objects.count())
    state, session = _ticket_held_while(
        raised,
        _correction(capable, raised, token, rows, field),
        _deactivate_bypassing_save(capable),
    )
    assert state == "waiting", state
    assert session.outcome in ("denied", "refused"), (session.outcome, repr(session.error))
    assert FieldValue.objects.get(pk=rows[field].pk).superseded_at is None
    assert before == (
        _fresh(raised).revision,
        FieldValue.objects.count(),
        RequestEntry.objects.count(),
    )
    assert not FieldValue.objects.filter(value="second").exists()


@pytest.mark.parametrize(
    ("field", "meanwhile"),
    [
        ("internal", "role-removed"),
        ("pt", "capability-revoked"),
        ("pt", "role-removed"),
    ],
)
def test_a_role_or_capability_removal_waits_for_a_form_correction_under_way(
    field, meanwhile, make_user, quality, mixed_form, capable
):
    """The account's staff role is removed (or, for a patient-information field, its capability
    revoked) while the correction waits for the ticket's lock. The correction holds the account's
    roles and grants until it commits (accounts.acting), so the removal waits behind it. The
    correction is saved first, by an account that still had the role and the capability (the new
    value becomes current and the old one is superseded), and the access is gone afterwards."""
    raised, rows, token = _raised_mixed(capable, quality, mixed_form)
    revision = _fresh(raised).revision

    def remove_role():
        capable.groups.remove(Group.objects.get(name="client_services"))

    access_change = {"role-removed": remove_role, "capability-revoked": _revoke(capable)}[meanwhile]
    state, access_state, behind_correction, session, access = _ticket_held_with_access_change(
        raised, _correction(capable, raised, token, rows, field), access_change
    )
    assert state == "waiting", state
    assert (access_state, behind_correction) == ("waiting", True)
    assert (session.outcome, access.outcome) == ("done", "done"), repr(session.error)
    assert FieldValue.objects.get(pk=rows[field].pk).superseded_at is not None
    current = FieldValue.objects.get(
        filled_form__request=raised, field__key=field, superseded_at__isnull=True
    )
    assert current.value == "second"
    assert _fresh(raised).revision > revision
    stored = User.objects.get(pk=capable.pk)
    if meanwhile == "capability-revoked":
        assert not stored.user_permissions.exists()
    else:
        assert not stored.groups.filter(name="client_services").exists()
