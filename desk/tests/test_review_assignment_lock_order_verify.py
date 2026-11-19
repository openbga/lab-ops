"""Verification of review round 1 of the assignment lock order (candidate 5cd2ef1). All records
are synthetic.

Each test states intended behaviour. The races run every party in its own PostgreSQL session (a
thread with its own connection) with a bounded lock_timeout; a party is paused at a named point
of desk.services, or holds its transaction open, until the others wait for a lock (seen with
pg_blocking_pids) or have finished. The file uses no helper only the candidate has, so it runs on
the base and on 3032c44 too.
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, connection, connections, transaction
from django.http import Http404
from django.test import Client
from django.urls import reverse

from accounts.models import User
from attachments.models import Attachment
from attachments.services import add_entry_with_files, withdraw_attachment
from attachments.tests.conftest import png_file
from core.models import AuditEvent
from desk import notifications, services
from desk.models import EntryKind, RequestEntry, ServiceRequest
from jobs import outbox
from jobs.models import Notice
from links import services as link_services
from links.models import ExternalSystem, LinkAttachment
from request_forms import definition
from request_forms.models import FieldKind, FieldValue, RequestTemplate
from request_forms.services import correct_values, create_request_with_form
from taxonomy import services as taxonomy_services
from taxonomy.models import RequestCategory
from taxonomy.terms import drawn
from teams import services as team_services
from teams.models import TeamMembership

from .test_contract import new_request, selection

pytestmark = pytest.mark.django_db(transaction=True)

LOCK_WAIT = "8s"  # long enough for the other sessions; a mistake fails, never hangs
PROBE_WAIT = "450ms"  # a probe: does this write wait for a lock someone holds?


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
    if isinstance(exc, (ServiceRequest.DoesNotExist, Http404)):
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


# People -----------------------------------------------------------------------------------------


@pytest.fixture
def person(make_user, join, accessioning):
    user = make_user("verify_technician", "technical")
    join(user, accessioning)
    return user


@pytest.fixture
def colleague(make_user, join, accessioning):
    user = make_user("verify_colleague", "technical")
    join(user, accessioning)
    return user


@pytest.fixture
def clerk(make_user):
    return make_user("verify_clerk", "client_services")


@pytest.fixture
def lead(make_user):
    """A supervisor, member of no team."""
    return make_user("verify_lead", "supervisor")


# V1. Staff intake with a form naming the person, an assignment to them and a recategorization ----
#
# The new order: an assignment locks the assignee's rows, then the ticket; a recategorization
# locks the ticket, then the category; and "a new ticket naming an assignee locks that person
# before its category" (docs/architecture.md). The staff new-ticket page with a request form
# takes the form's locks first (desk/views.py request_create: intake.lock(), then
# create_request_with_files), and request_forms.definition.read(for_write=True) locks the category
# there, before create_request locks the assignee.


@pytest.fixture
def staff_form(sample_category):
    """A staff request form in "Sample problem" (which routes to Accessioning)."""
    template = RequestTemplate.objects.create(
        key="verify-form",
        name="Verification form",
        category=sample_category,
        offered_in_portal=False,
    )
    template.fields.create(key="note", label="Note", kind=FieldKind.SHORT_TEXT)
    return template


def _intake_page(actor, category, template, team, person):
    """The staff new-ticket page posted with the form and "Assign to" `team`: `person`, as the
    page draws it."""
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
        http = Client()
        http.force_login(actor)
        response = http.post(reverse("desk:request-create"), data)
        assert response.status_code == 302, response.content[:3000]

    return post


def _assignment_paths(person, agent, ticket, accessioning):
    """Each assignment to `person`, with the point just before it locks the ticket."""
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
def test_form_intake_naming_the_person_an_assignment_and_a_recategorization_never_deadlock(
    path, agent, clerk, person, category, sample_category, accessioning, staff_form
):
    """An assignment of a ticket to a technician, a recategorization of that ticket into "Sample
    problem", and a new ticket logged on the staff page with that category's form naming the same
    technician, at the same moment. Each may wait for another, never in a cycle: none ends with a
    deadlock (40P01), and the new ticket and the recategorization are saved. The assignment is
    paused just before it locks the ticket (it holds the person's rows); the intake starts, then
    the recategorization, then the assignment goes on."""
    ticket = new_request(agent, category, team=accessioning)
    lock_point, work = _assignment_paths(person, agent, ticket, accessioning)[path]
    intake = _intake_page(clerk, sample_category, staff_form, accessioning, person)  # page drawn
    seen, sessions = {}, {}

    def during():
        sessions["intake"] = Session(intake).start()
        seen["intake"] = sessions["intake"].waiting_or_ended()
        sessions["recategorize"] = Session(_recategorize(clerk, ticket, sample_category)).start()
        seen["recategorize"] = sessions["recategorize"].waiting_or_ended()

    with paused(lock_point, during, before=True):
        assignment = run_here(work)
    outcome = {name: session.join() for name, session in sessions.items()}
    outcome["assignment"] = assignment
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}, during the pause {seen}"
    assert outcome["intake"] == "done", (outcome, seen)
    assert outcome["recategorize"] == "done", (outcome, seen)
    assert outcome["assignment"] in ("done", "refused"), outcome
    assert (
        ServiceRequest.objects.filter(subject="Recollection needed", assignee=person).count() == 1
    )


def test_form_intake_naming_the_person_an_assignment_and_a_form_correction_never_deadlock(
    agent, clerk, person, sample_category, accessioning, staff_form
):
    """The same with a request form's records as the third party: a ticket raised with the "Sample
    problem" form is assigned to a technician while a clerk corrects its form values
    (request_forms correct_values: the ticket, then the form's definition, its category included)
    and another clerk logs a new ticket with that form naming the same technician. None ends with
    40P01; the new ticket and the correction are saved."""
    raised = create_request_with_form(
        actor=agent,
        template=staff_form,
        expected_definition=definition.token(staff_form),
        values={"note": "first"},
        first_entry_kind=EntryKind.NOTE,
        channel="phone",
        team=accessioning,
    )
    row = FieldValue.objects.get(filled_form__request=raised, superseded_at__isnull=True)
    token = definition.token(staff_form)  # as the correction dialog drew it
    intake = _intake_page(clerk, sample_category, staff_form, accessioning, person)  # page drawn
    seen, sessions = {}, {}

    def correct():
        correct_values(
            actor=clerk,
            request=raised,
            expected_definition=token,
            values={"note": "second"},
            reason="Typo",
            expected={"note": row.pk},
        )

    def during():
        sessions["intake"] = Session(intake).start()
        seen["intake"] = sessions["intake"].waiting_or_ended()
        sessions["correction"] = Session(correct).start()
        seen["correction"] = sessions["correction"].waiting_or_ended()

    with paused("_staff_request", during, before=True):
        assignment = run_here(lambda: _assign(agent, raised, accessioning, person))
    outcome = {name: session.join() for name, session in sessions.items()}
    outcome["assignment"] = assignment
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}, during the pause {seen}"
    assert outcome["intake"] == "done", (outcome, seen)
    assert outcome["correction"] == "done", (outcome, seen)
    assert outcome["assignment"] in ("done", "refused"), outcome


@pytest.fixture
def notice_on_ticket(agent, category, accessioning, person):
    """A ticket of Accessioning (plain category) assigned to `person`, with its staff notice
    waiting to be sent."""
    ticket = new_request(agent, category, team=accessioning)
    with patch.object(outbox, "send_now", return_value={}):
        _assign(agent, ticket, accessioning, person)
    row = Notice.objects.get(recipient=person, topic=notifications.TOPIC)
    return SimpleNamespace(ticket=ticket, row=row)


def test_a_form_intake_waiting_behind_an_assignment_does_not_hold_up_its_category(
    notice_on_ticket, agent, clerk, colleague, sample_category, accessioning, staff_form, make_user
):
    """While a staff notice about a ticket is being sent, a colleague's reassignment of that ticket
    waits for the send, holding the new assignee's rows; that "holds up only work about the same
    person" (docs/architecture.md). A ticket logged on the staff page with the "Sample problem"
    form naming that same assignee waits for them too. Logging another ticket in "Sample problem"
    that names nobody is not work about that person and must not wait behind them."""
    intake = _intake_page(clerk, sample_category, staff_form, accessioning, colleague)
    other_clerk = make_user("verify_other_clerk", "client_services")
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
        form_intake = Session(intake).start()
        form_intake.waiting_or_ended()
        waited = probe_waits(lambda: new_request(other_clerk, sample_category))
    finally:
        release.set()
        holder.join()
    assert reassignment.join() == "done"
    assert form_intake.join() == "done"
    assert not waited, "intake in the category waited behind an assignment to another person"


# V2. Intake naming a member, their removal from the team and a change to one of their tickets ---
#
# create_request locks the assignee's membership after the category and the team
# (_team_and_assignee), teams.services.remove_member locks the membership and then the person's
# tickets in the team, and the ticket services lock the ticket and then the team (_assign) or the
# category (update_properties).


def _changes_to_their_ticket(clerk, agent, colleague, ticket, accessioning, sample_category):
    """Each change to `ticket`, with the point just after it has locked the ticket."""
    return {
        "assign": ("_staff_request", lambda: _assign(agent, ticket, accessioning, colleague)),
        "take": ("_staff_request", lambda: _take(colleague, ticket)),
        "bulk-assign": (
            "_bulk_requests",
            lambda: services.bulk_assign(
                actor=agent, selected=selection([ticket]), team=accessioning, assignee=colleague
            ),
        ),
        "recategorize": ("_staff_request", _recategorize(clerk, ticket, sample_category)),
    }


@pytest.mark.parametrize("path", ["assign", "take", "bulk-assign", "recategorize"])
def test_intake_naming_a_member_their_removal_and_a_change_to_their_ticket_never_deadlock(
    path, agent, clerk, lead, person, colleague, category, sample_category, accessioning
):
    """A technician holds a ticket of Accessioning. A colleague reassigns (or takes, or bulk
    assigns, or recategorizes into "Sample problem") that ticket, a supervisor removes the
    technician from Accessioning, and a clerk logs a new ticket in "Sample problem" (routed to
    Accessioning) naming the technician, at the same moment. Removing a member and assigning
    serialize ("Neither fails with a deadlock", docs/architecture.md; the team page's Remove
    help): none ends with 40P01. The change is paused once it holds the ticket; the removal starts
    (it waits for the ticket), then the intake, then the change goes on. The removal and the
    change are saved; the intake, which comes after the removal, is refused on the assignee."""
    ticket = new_request(agent, category, team=accessioning, assignee=person)
    membership = TeamMembership.objects.get(team=accessioning, user=person)
    lock_point, work = _changes_to_their_ticket(
        clerk, agent, colleague, ticket, accessioning, sample_category
    )[path]
    seen, sessions = {}, {}

    def during():
        sessions["removal"] = Session(
            lambda: team_services.remove_member(actor=lead, membership=membership)
        ).start()
        seen["removal"] = sessions["removal"].waiting_or_ended()
        sessions["intake"] = Session(
            lambda: new_request(clerk, sample_category, assignee=person)
        ).start()
        seen["intake"] = sessions["intake"].waiting_or_ended()

    with paused(lock_point, during, before=False):
        change = run_here(work)
    outcome = {name: session.join() for name, session in sessions.items()}
    outcome["change"] = change
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}, during the pause {seen}"
    assert seen == {"removal": "waiting", "intake": "waiting"}, (seen, outcome)
    assert outcome == {"removal": "done", "intake": "refused", "change": "done"}
    assert not TeamMembership.objects.filter(pk=membership.pk).exists()


# V3. A change waiting for a category that is moved to another group meanwhile -----------------
#
# The case next to R05 for the category: desk.services._check_category locks the category
# through a query joined to its group and default team (select_related), so PostgreSQL rechecks
# a row it waited for against the joined rows it read before waiting.


@pytest.fixture
def other_group(db):
    return RequestCategory.objects.create(key="verify-other-group", name="Sample logistics")


def _changes_naming_the_category(clerk, agent, category, sample_category, accessioning):
    ticket = new_request(agent, category, team=accessioning)
    return {
        "staff-intake": lambda: new_request(
            clerk, sample_category, drawn_terms=drawn([sample_category])
        ),
        "recategorize": _recategorize(clerk, ticket, sample_category),
    }


@pytest.mark.parametrize("change", ["staff-intake", "recategorize"])
def test_a_change_waiting_for_a_category_being_moved_to_another_group_is_not_a_server_error(
    change, make_user, agent, clerk, category, sample_category, accessioning, other_group
):
    """An administrator moves "Sample problem" to another active group (taxonomy update_category)
    while a ticket is logged in it, or another ticket is recategorized into it. The change waits
    for the category's lock and then uses "the category as it stands now"
    (desk.services._check_category): the ticket is saved in the moved category. It must not end
    in an error from reading the category's group (a server error, nothing saved)."""
    admin = make_user("verify_setup", "admin")
    work = _changes_naming_the_category(clerk, agent, category, sample_category, accessioning)[
        change
    ]

    def move():
        current = RequestCategory.objects.get(pk=sample_category.pk)
        taxonomy_services.update_category(
            actor=admin,
            category_id=current.pk,
            expected_revision=current.revision,
            reason="Regrouped",
            parent=other_group,
        )

    holder = Held(move)
    session = Session(work).start()
    try:
        state = session.waiting_or_ended()
    finally:
        assert holder.release() == "done"
    outcome = session.join()
    assert state == "waiting", state
    assert outcome == "done", outcome
    assert ServiceRequest.objects.filter(category=sample_category).count() == 1


# V4. The other services that lock a ticket through locked_in_scope read the acting account again --
#
# The cases next to R04: "an account deactivated, moved to the portal or left without a staff
# role while its request waited is refused" (desk.services.current_staff), read after the change's
# locks; R04 added this reread to the list's bulk actions. desk (settle) and nonconformance
# (_current_actor) read the account again after the ticket's lock. attachments, links and request
# forms decide from the account as the request began.


@pytest.fixture
def quality_ticket(agent, category, quality):
    return new_request(agent, category, team=quality, client=None)


def _ticket_held_while(ticket, change, meanwhile):
    """`change` waits for the ticket's lock, held by another session; `meanwhile()` commits;
    the ticket is released. Returns (whether the change waited, its outcome)."""

    def hold():
        ServiceRequest.objects.select_for_update().get(pk=ticket.pk)

    holder = Held(hold)
    session = Session(change).start()
    try:
        state = session.waiting_or_ended()
        meanwhile()
    finally:
        assert holder.release() == "done"
    return state, session.join()


def _deactivate_bypassing_save(user):
    """A deactivation by a path that bypasses User.save (a queryset update). It takes a FOR NO KEY
    UPDATE of the account row, which the hold of accounts.acting (FOR KEY SHARE) does not make
    wait."""

    def run():
        User.objects.filter(pk=user.pk).update(is_active=False)

    return run


def _demote(user):
    """The supervisor role taken away; technical staff remains."""

    def run():
        account = User.objects.get(pk=user.pk)
        account.groups.remove(Group.objects.get(name="supervisor"))
        account.groups.add(Group.objects.get_or_create(name="technical")[0])

    return run


def _blocked_by(pid, other):
    """Whether session `pid` waits for a lock that session `other` holds."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [other, pid])
        return cursor.fetchone()[0]


def _ticket_held_with_access_change(ticket, change, access_change):
    """`change` waits for the ticket's lock, held by another session. `access_change` is then
    started in its own session: the change holds the account's rows (accounts.acting), so it
    waits behind the change. The ticket is released and both are joined. Returns (whether the
    change waited, whether the access change waited, whether it waited behind the change, the
    change's Session, the access change's Session)."""

    def hold():
        ServiceRequest.objects.select_for_update().get(pk=ticket.pk)

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


def _file_on(agent, ticket):
    seen = _fresh(ticket)
    add_entry_with_files(
        actor=agent,
        request=seen,
        kind=EntryKind.REPLY,
        body="Requisition attached.",
        expected_revision=seen.revision,
        files=[png_file()],
    )
    return Attachment.objects.get()


def test_a_demotion_waits_for_a_file_withdrawal_by_a_supervisor_under_way(
    agent, lead, quality_ticket
):
    """withdraw_attachment ("A supervisor withdraws a file with a reason"): the supervisor role is
    taken away (technical staff remains) while the withdrawal waits for the ticket's lock. The
    withdrawal holds the account's roles until it commits (accounts.acting), so the demotion
    waits behind it. The file is withdrawn first, by an account that still was a supervisor, with
    an audit event and a timeline entry; the supervisor role is gone afterwards."""
    attachment = _file_on(agent, quality_ticket)
    revision = _fresh(quality_ticket).revision
    entries = RequestEntry.objects.filter(request=quality_ticket).count()
    state, demotion_state, behind_withdrawal, session, demotion = _ticket_held_with_access_change(
        quality_ticket,
        lambda: withdraw_attachment(actor=lead, attachment_id=attachment.pk, reason="Wrong file"),
        _demote(lead),
    )
    assert state == "waiting", state
    assert (demotion_state, behind_withdrawal) == ("waiting", True)
    assert (session.outcome, demotion.outcome) == ("done", "done"), repr(session.error)
    assert Attachment.objects.get(pk=attachment.pk).withdrawn_at is not None
    assert _fresh(quality_ticket).revision > revision
    assert RequestEntry.objects.filter(request=quality_ticket).count() == entries + 1
    roles = set(User.objects.get(pk=lead.pk).groups.values_list("name", flat=True))
    assert roles == {"technical"}


def test_withdrawing_a_file_refuses_a_supervisor_demoted_before_it_began(
    agent, lead, quality_ticket
):
    """The supervisor role was taken away (technical staff remains) and the change committed
    before the withdrawal began, while the caller still holds the account as it was. The
    withdrawal is refused like any other supervisor-only change made by a demoted account: the
    file stays with clients, no audit event or timeline entry is written, the revision stays."""
    attachment = _file_on(agent, quality_ticket)
    before = (
        _fresh(quality_ticket).revision,
        AuditEvent.objects.count(),
        RequestEntry.objects.count(),
    )
    _demote(lead)()
    session = Session(
        lambda: withdraw_attachment(actor=lead, attachment_id=attachment.pk, reason="Wrong file")
    ).start()
    session.join()
    assert session.outcome in ("denied", "not found"), (session.outcome, repr(session.error))
    assert Attachment.objects.get(pk=attachment.pk).withdrawn_at is None
    assert before == (
        _fresh(quality_ticket).revision,
        AuditEvent.objects.count(),
        RequestEntry.objects.count(),
    )


@pytest.fixture
def lis(db):
    return ExternalSystem.objects.get_or_create(
        key="lis", defaults={"name": "Laboratory information system", "kind": "lis"}
    )[0]


def test_adding_a_link_refuses_an_account_deactivated_while_it_waited_bypassing_save(
    make_user, quality_ticket, lis
):
    """links.services.add_link on a ticket: the account is deactivated while the link waits for
    the ticket's lock, by a path that bypasses User.save (a queryset update), which the hold of
    accounts.acting does not make wait. The reading after the lock refuses it, nothing stored (no
    link, no audit event, no timeline entry)."""
    clerk = make_user("verify_link_clerk", "client_services")
    before = (
        LinkAttachment.objects.count(),
        AuditEvent.objects.count(),
        RequestEntry.objects.count(),
    )
    state, outcome = _ticket_held_while(
        quality_ticket,
        lambda: link_services.add_link(
            actor=clerk,
            request_id=quality_ticket.pk,
            system=lis,
            record_type="order",
            identifier="ORD-TEST-000044",
        ),
        _deactivate_bypassing_save(clerk),
    )
    after = (
        LinkAttachment.objects.count(),
        AuditEvent.objects.count(),
        RequestEntry.objects.count(),
    )
    assert state == "waiting", state
    assert outcome in ("denied", "not found"), outcome
    # The deactivation itself writes one audit event (accounts) at most; nothing of the link.
    assert after[0] == before[0] and after[2] == before[2]


def test_correcting_form_values_refuses_an_account_deactivated_while_it_waited_bypassing_save(
    make_user, agent, category, quality
):
    """request_forms.services.correct_values: the account is deactivated while the correction
    waits for the ticket's lock, by a path that bypasses User.save (a queryset update), which the
    hold of accounts.acting does not make wait. The reading after the lock refuses it; the stored
    value stays current and no new value row, timeline entry or revision is written."""
    clerk = make_user("verify_form_clerk", "client_services")
    template = RequestTemplate.objects.create(
        key="verify-correction", name="Correction form", category=category, offered_in_portal=False
    )
    template.fields.create(key="note", label="Note", kind=FieldKind.SHORT_TEXT)
    raised = create_request_with_form(
        actor=agent,
        template=template,
        expected_definition=definition.token(template),
        values={"note": "first"},
        first_entry_kind=EntryKind.NOTE,
        channel="phone",
        team=quality,
    )
    row = FieldValue.objects.get(filled_form__request=raised, superseded_at__isnull=True)
    before = (_fresh(raised).revision, FieldValue.objects.count(), RequestEntry.objects.count())
    state, outcome = _ticket_held_while(
        raised,
        lambda: correct_values(
            actor=clerk,
            request=raised,
            expected_definition=definition.token(template),
            values={"note": "second"},
            reason="Typo",
            expected={"note": row.pk},
        ),
        _deactivate_bypassing_save(clerk),
    )
    assert state == "waiting", state
    assert outcome in ("denied", "not found"), outcome
    assert FieldValue.objects.get(pk=row.pk).superseded_at is None
    assert before == (
        _fresh(raised).revision,
        FieldValue.objects.count(),
        RequestEntry.objects.count(),
    )
