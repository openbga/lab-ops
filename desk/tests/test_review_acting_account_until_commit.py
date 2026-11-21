"""Independent review, round 1: the acting account held until commit (candidate 1743376, base
cc8d7fa). All records are synthetic.

Adopted: A1 to A5, resolved by the access gate (accounts.acting), the account page taking it
before its owner check, the import's organizations in file order and the reveals reading the
account as held. A6 (a client's reply against the deactivation of the ticket's assignee)
predates the change and lies outside it; it is the acceptance case of the roadmap follow-up and
is kept outside the suite until that is built.

Each test states intended behaviour (docs/architecture.md "Services and hooks", the docstrings of
accounts/acting.py and desk/signals.py, docs/safeguards.md, the page help of the account page).
The races run every party in its own PostgreSQL session (a thread with its own connection) with a
bounded lock_timeout; a party is held at a named point until the others wait for a lock (seen
with pg_blocking_pids) or have finished. The file uses no helper only the candidate has, so it
runs on the base too.
"""

import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django import forms
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.http import Http404
from django.test import Client
from django.urls import reverse

from accounts.models import User
from core.models import AuditEvent
from desk import services
from desk.models import EntryKind, RequestEntry, ServiceRequest
from links import services as link_services
from links.models import ExternalSystem
from request_forms import definition
from request_forms import services as form_services
from request_forms.models import FieldKind, FieldValue, RequestTemplate
from teams import services as team_services
from teams.models import TeamMembership

from .test_contract import new_request
from .test_review_assignment_lock_order_verify3 import (
    Held,
    Session,
    _blocked_by,
    _deactivate_by,
    _fresh,
    _statement,
    paused,
    run_here,
)

pytestmark = pytest.mark.django_db(transaction=True)

CAPABILITY = "view_patient_information"


@pytest.fixture(autouse=True)
def locmem_mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


@pytest.fixture
def lis(db):
    return ExternalSystem.objects.get_or_create(
        key="lis", defaults={"name": "Laboratory information system", "kind": "lis"}
    )[0]


@pytest.fixture
def owner(make_user):
    return make_user("review_owner", "admin")


@pytest.fixture
def lead(make_user):
    """A supervisor, who manages team members."""
    return make_user("review_lead", "supervisor")


# Helpers ----------------------------------------------------------------------------------------


class Gate:
    """A point a session stops at: `arrived` once it is there, it goes on at `go`."""

    def __init__(self):
        self.arrived, self.go = threading.Event(), threading.Event()

    def stop(self):
        self.arrived.set()
        self.go.wait(timeout=20)


def gated(module, name, gates, *, before=True):
    """Patch module.`name`: a call made by a session's thread listed in `gates` (thread -> Gate)
    stops at that gate once, before (or after) the real call. Other threads go straight through."""
    real = getattr(module, name)
    used = set()

    def wrapper(*args, **kwargs):
        gate = gates.get(threading.current_thread())
        stop = gate is not None and id(gate) not in used
        if stop:
            used.add(id(gate))
        if stop and before:
            gate.stop()
        result = real(*args, **kwargs)
        if stop and not before:
            gate.stop()
        return result

    return patch.object(module, name, wrapper)


def _note(actor, ticket, body):
    seen = _fresh(ticket)
    return services.add_entry(
        actor=actor, request=seen, kind=EntryKind.NOTE, body=body, expected_revision=seen.revision
    )


def _take(actor, ticket):
    seen = _fresh(ticket)
    return services.take(actor=actor, request=seen, expected_revision=seen.revision)


def _grant_capability(person):
    permission = Permission.objects.get(codename=CAPABILITY, content_type__app_label="accounts")
    person.user_permissions.add(permission)


def _person_link(person, ticket, lis, identifier):
    attachment, _created = link_services.add_link(
        actor=person, request_id=ticket.pk, system=lis, record_type="patient", identifier=identifier
    )
    return attachment


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
            for index, part in enumerate(field.widget.decompress(value)):
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


def _admin_deactivation(by, account):
    """`by` unticks Active on `account`'s page in account administration; call it to post."""
    http = Client()
    http.force_login(by)
    data = _admin_change_data(http, account, is_active=False)
    return lambda: http.post(reverse("admin:accounts_user_change", args=[account.pk]), data)


def _refused(session):
    """Whether the session's work was refused as someone who may no longer act or read."""
    return isinstance(session.error, (PermissionDenied, ObjectDoesNotExist, Http404))


# A2. An owner's deactivation and that owner's own take ------------------------------------------


@pytest.mark.parametrize(
    "taken_by", ["the deactivated owner", "the deactivated owner for the other"]
)
def test_deactivating_an_owner_and_their_own_assignment_never_deadlock(
    taken_by, make_user, join, agent, sample_category, accessioning
):
    """An owner (active superuser with staff status) who also works tickets as a supervisor takes a
    ticket of their team, or assigns it to the owner who is deactivating them. At that moment
    the other owner unticks their Active box in account administration.

    The page's owner check holds every owner's row FOR NO KEY UPDATE until the save commits
    (accounts.admin, "so that an owner's own change elsewhere ... does not wait for this one
    while this one waits for that ticket"), then the save locks the account FOR UPDATE. The
    take holds the owner's own account FOR KEY SHARE from its start (accounts.acting) and then
    locks the person it assigns FOR NO KEY UPDATE.

    Intended (accounts/acting.py: ending a person's work and that person's own change "never
    wait for each other in a cycle"; docs/architecture.md: "never in a cycle"): no 40P01. The
    deactivation is saved, the owner is inactive, and the ticket is not left with them."""
    owner_one = make_user("review_owner_one", "admin", is_staff=True, is_superuser=True)
    owner_two = make_user("review_owner_two", "supervisor", is_staff=True, is_superuser=True)
    join(owner_one, accessioning)
    join(owner_two, accessioning)
    ticket = new_request(agent, sample_category, subject="Owner take")
    deactivate = _admin_deactivation(owner_one, owner_two)

    def work():
        seen = _fresh(ticket)
        if taken_by == "the deactivated owner":
            return services.take(actor=owner_two, request=seen, expected_revision=seen.revision)
        return services.assign(
            actor=owner_two,
            request=seen,
            expected_revision=seen.revision,
            team=accessioning,
            assignee=owner_one,
        )

    gates = {}
    change = Session(work)
    gates[change.thread] = Gate()
    with gated(services, "_hold_assignment", gates):
        change.start()
        assert gates[change.thread].arrived.wait(timeout=10), change.error
        admin = Session(deactivate).start()
        admin_state = admin.waiting_or_ended(seconds=4)
        gates[change.thread].go.set()
        change.join()
        admin.join()
    outcomes = {"change": change.outcome, "deactivation": admin.outcome}
    assert "40P01" not in outcomes.values(), f"deadlock: {outcomes} (admin was {admin_state})"
    assert admin.outcome == "done" and admin.result.status_code == 302, outcomes
    assert not User.objects.get(pk=owner_two.pk).is_active
    if taken_by == "the deactivated owner":
        assert _fresh(ticket).assignee_id is None


# A1. A change started while an access change waits for an earlier change -----------------------


def _access_change(case, person, owner):
    if case == "deactivation":
        return _deactivate_by(person, owner)
    return lambda: User.objects.get(pk=person.pk).groups.remove(Group.objects.get(name="technical"))


def _access_taken(case, person):
    person = User.objects.get(pk=person.pk)
    if case == "deactivation":
        return not person.is_active
    return not person.groups.filter(name="technical").exists()


@pytest.mark.parametrize(
    ("case", "second"),
    [
        ("deactivation", "note"),
        ("deactivation", "patient key reveal"),
        ("role removal", "note"),
    ],
)
def test_a_change_started_while_the_access_change_waits_for_an_earlier_one_is_refused(
    case, second, make_user, join, owner, agent, sample_category, accessioning, lis
):
    """A technician adds a note to one ticket (the change has started and holds their rows). An
    owner deactivates the technician, or removes their only role, and that change waits for the
    note. While it waits, the technician starts a second change: a note on another ticket, or a
    reveal of a patient-level key on it.

    Intended (docs/safeguards.md "a change the person is saving at that moment finishes first,
    and one they start afterwards is refused"; the account page's help "one they start after it
    is refused"; docs/architecture.md "A change that starts after one of them reads it and is
    refused"): the second change, started after the access change, waits for it and is refused.
    Nothing of it is saved or revealed."""
    person = make_user(f"review_person_{case.split()[0]}", "technical")
    join(person, accessioning)
    _grant_capability(person)
    first_ticket = new_request(agent, sample_category, subject="First")
    second_ticket = new_request(agent, sample_category, subject="Second")
    attachment = _person_link(person, second_ticket, lis, "PT-TEST-000501")
    revealed = AuditEvent.objects.filter(action="external_link.person_revealed").count()

    def second_change():
        # As a request starting now reads its account (the middleware): fresh.
        actor = User.objects.get(pk=person.pk)
        if second == "note":
            return _note(actor, second_ticket, "Second note ORD-TEST-000502.")
        return link_services.reveal_person_key(actor=actor, attachment_id=attachment.pk)

    gates = {}
    first = Session(lambda: _note(person, first_ticket, "First note ORD-TEST-000501."))
    gates[first.thread] = Gate()
    with gated(services, "_locked_request", gates):
        first.start()
        assert gates[first.thread].arrived.wait(timeout=10), first.error
        access = Session(_access_change(case, person, owner)).start()
        access_state = access.waiting_or_ended(seconds=3)
        behind_first = access.pid is not None and _blocked_by(access.pid, first.pid)
        later = Session(second_change).start()
        later_state = later.waiting_or_ended(seconds=3)
        access_still = access.waiting_or_ended(seconds=0.2)
        gates[first.thread].go.set()
        first.join()
        access.join()
        later.join()
    assert access.outcome == "done", repr(access.error)
    assert _access_taken(case, person)
    detail = {
        "access change": (access_state, "behind the first change" if behind_first else ""),
        "second change": (later_state, later.outcome),
        "access change after the second ended": access_still,
    }
    assert _refused(later), detail
    assert not RequestEntry.objects.filter(body="Second note ORD-TEST-000502.").exists()
    if second == "patient key reveal":
        assert AuditEvent.objects.filter(action="external_link.person_revealed").count() == revealed


def test_a_deactivation_does_not_wait_for_as_long_as_the_persons_changes_overlap(
    make_user, join, owner, agent, sample_category, accessioning
):
    """A technician's note is under way when an owner deactivates them; the save waits for it.
    The technician starts a second note before the first commits, and a third before the second
    commits.

    Intended (as above, and docs/safeguards.md "Deactivating an account ... refuses it at its
    next request"): only the change already under way is waited for. The second and third
    notes, started after the deactivation, wait at their first step and are refused, so the
    deactivation is saved once the first note commits."""
    person = make_user("review_overlap", "technical")
    join(person, accessioning)
    tickets = [new_request(agent, sample_category, subject=f"Overlap {n}") for n in range(3)]

    def note(n):
        # Each request reads its account when it starts (the middleware).
        return lambda: _note(User.objects.get(pk=person.pk), tickets[n], f"Overlap note {n}.")

    gates, sessions, passed_hold, active_at_ticket = {}, [], [], []
    with gated(services, "_locked_request", gates):
        for n in range(3):
            session = Session(note(n))
            gates[session.thread] = Gate()
            sessions.append(session.start())
            # It has held the person's rows and reached its ticket (or it was refused).
            passed_hold.append(gates[session.thread].arrived.wait(timeout=3))
            # Adapted for the automatic sign-out change: the deactivation now also ends the
            # person's sessions, so it commits a few milliseconds after the first note instead of
            # at once, and a note started in that window reads the account still active, waits at
            # its first step and reaches its ticket only once the deactivation has committed (and
            # is refused there). What must not happen is a later note reaching its ticket while
            # the deactivation is still pending: checked by whether the account was still active
            # when it got there (the note is stopped at the gate meanwhile).
            active_at_ticket.append(passed_hold[-1] and User.objects.get(pk=person.pk).is_active)
            if n == 0:
                deactivation = Session(_deactivate_by(person, owner)).start()
                first_wait = deactivation.waiting_or_ended(seconds=3)
            else:
                previous = sessions[n - 1]
                gates[previous.thread].go.set()  # the earlier note commits
                previous.join()
        still_waiting = deactivation.waiting_or_ended(seconds=0.2) == "waiting"
        behind_third = _blocked_by(deactivation.pid, sessions[2].pid) if still_waiting else False
        gates[sessions[2].thread].go.set()
        sessions[2].join()
        deactivation.join()
    assert deactivation.outcome == "done", repr(deactivation.error)
    saved = [RequestEntry.objects.filter(body=f"Overlap note {n}.").exists() for n in range(3)]
    detail = {
        "deactivation after the first note started": first_wait,
        "passed the hold": passed_hold,
        "saved": saved,
        "after the second note committed, still waiting behind the third": behind_third,
        "reached its ticket while the deactivation was pending": active_at_ticket,
    }
    assert passed_hold[1] is False and active_at_ticket[1:] == [False, False], detail
    assert not behind_third, detail
    assert saved[1:] == [False, False], detail


# A5. A reading taken under the hold, before a grant --------------------------------------------


def test_removing_a_team_membership_granted_during_a_change_waits_for_that_change(
    make_user, join, lead, agent, privacy_category, quality, accessioning
):
    """A technician of Accessioning adds a note to a restricted Quality ticket. After the change
    has held the technician's rows, and before it reads the ticket, a supervisor adds them to
    Quality on the team page (an addition waits for nothing). The change reads the ticket in
    scope, locks it and reads the account again (settle). Then the supervisor removes them from
    Quality again.

    Intended (docs/architecture.md "removing ... a team membership ... waits for its row"; the
    team page's Remove help "A change the member is saving at that moment, such as a note,
    finishes first"): the removal waits for the note, which is saved first. Here the note's
    decision rests on a membership the change does not hold, so the removal commits while the
    note is still being saved."""
    person = make_user("review_member", "technical")
    join(person, accessioning)
    ticket = new_request(agent, privacy_category, client=None, subject="Restricted")
    result = {}

    def grant():
        work = lambda: team_services.add_member(actor=lead, team=quality, user=person)  # noqa: E731
        result["grant"] = Session(work).start().join()

    def take_away():
        membership = TeamMembership.objects.get(team=quality, user=person)
        work = lambda: team_services.remove_member(actor=lead, membership=membership)  # noqa: E731
        result["removal"] = Session(work).start()
        result["removal state"] = result["removal"].waiting_or_ended(seconds=3)

    with (
        paused(services, "_locked_request", grant),
        paused(services, "settle", take_away, before=False),
    ):
        outcome = run_here(lambda: _note(person, ticket, "Restricted note ORD-TEST-000511."))
    result["removal"].join()
    assert result["grant"] == "done"
    assert result["removal"].outcome == "done", repr(result["removal"].error)
    assert outcome == "done", outcome
    assert result["removal state"] == "waiting", (
        "the removal did not wait for the change under way",
        result,
    )


def test_removing_a_capability_granted_during_a_change_waits_for_that_change(
    make_user, agent, category, lis
):
    """A technician adds a person-level link to a ticket. After the change has held the
    technician's rows, and before its first check of the capability, an owner gives the
    patient-information capability to the technician role on the role group page (an addition
    waits for nothing). The change checks the capability three times, the last after its last
    lock. Then the owner takes the capability away from the role again.

    Intended (the account page's help "Removing a capability waits for a change the person is
    saving at that moment"; docs/architecture.md "removing ... a capability ... waits for its
    row"): the removal waits for the link addition, which is saved first. Here the decision rests
    on a grant the change does not hold, so the removal commits while the link is being saved."""
    person = make_user("review_linker", "technical")
    ticket = new_request(agent, category, subject="Person link")
    technical = Group.objects.get(name="technical")
    permission = Permission.objects.get(codename=CAPABILITY, content_type__app_label="accounts")
    real = link_services.require_capability
    calls, result = [], {}

    def require_capability(user, codename):
        calls.append(codename)
        if len(calls) == 1:  # before the first check: the grant
            work = lambda: technical.permissions.add(permission)  # noqa: E731
            result["grant"] = Session(work).start().join()
        checked = real(user, codename)
        if len(calls) == 3:  # after the last check, after the last lock: the removal
            work = lambda: technical.permissions.remove(permission)  # noqa: E731
            result["removal"] = Session(work).start()
            result["removal state"] = result["removal"].waiting_or_ended(seconds=3)
        return checked

    with patch.object(link_services, "require_capability", require_capability):
        outcome = run_here(
            lambda: link_services.add_link(
                actor=person,
                request_id=ticket.pk,
                system=lis,
                record_type="patient",
                identifier="PT-TEST-000512",
            )
        )
    result["removal"].join()
    assert (result["grant"], len(calls)) == ("done", 3), (result, calls)
    assert result["removal"].outcome == "done", repr(result["removal"].error)
    assert outcome == "done", outcome
    assert result["removal state"] == "waiting", (
        "the removal did not wait for the change under way",
        result,
    )


# A4. A patient-information reveal started during the deactivation -------------------------------


@pytest.fixture
def patient_form_ticket(make_user, agent, category):
    """A ticket raised with a staff form whose one field holds patient information."""
    template = RequestTemplate.objects.create(
        key="review-patient", name="Patient form", category=category, offered_in_portal=False
    )
    template.fields.create(
        key="patient",
        label="Patient identifier",
        kind=FieldKind.SHORT_TEXT,
        position=10,
        holds_patient_information=True,
    )
    capable = make_user("review_capable", "client_services")
    _grant_capability(capable)
    raised = form_services.create_request_with_form(
        actor=capable,
        template=template,
        expected_definition=definition.token(template),
        values={"patient": "PT-TEST-000521"},
        first_entry_kind=EntryKind.NOTE,
        channel="phone",
    )
    row = FieldValue.objects.get(filled_form__request=raised, superseded_at__isnull=True)
    return SimpleNamespace(ticket=raised, row=row)


@pytest.mark.parametrize("reveal", ["patient key", "key search", "patient value"])
def test_a_reveal_started_during_the_deactivation_is_refused(
    reveal, make_user, join, owner, agent, sample_category, accessioning, lis, patient_form_ticket
):
    """An owner deactivates a technician who holds the patient-information capability. The save
    holds the account and waits to return the technician's open tickets: one is busy. Meanwhile
    the technician, still signed in (their request read the account before the deactivation
    committed, as the authentication middleware does), reveals a patient-level key, searches by
    one, or reveals a patient-information value.

    Intended (accounts/acting.py "a change that starts after it reads the new state and is
    refused"; docs/architecture.md "A change that starts after one of them reads it and is
    refused" and "Every change reads the acting account again after its last lock and takes its
    checks of roles and capabilities from that reading"; the account page's help "one they start
    after it is refused"): the reveal waits for the deactivation at its first step and is then
    refused; nothing is revealed and no reveal is recorded. Here the hold's reading is dropped
    (accounts.acting.acting_change) and the reveal decides from the account object its request
    read before the deactivation committed."""
    person = make_user("review_revealer", "technical")
    join(person, accessioning)
    _grant_capability(person)
    busy = new_request(agent, sample_category, subject="Busy", team=accessioning, assignee=person)
    attachment = _person_link(person, patient_form_ticket.ticket, lis, "PT-TEST-000522")
    actions = {
        "patient key": "external_link.person_revealed",
        "key search": "external_link.person_searched",
        "patient value": "request_form.value_revealed",
    }
    before = AuditEvent.objects.filter(action=actions[reveal]).count()
    signed_in = User.objects.get(pk=person.pk)  # read at the start of the request

    def work():
        if reveal == "patient key":
            return link_services.reveal_person_key(actor=signed_in, attachment_id=attachment.pk)
        if reveal == "key search":
            return link_services.search_person_key(actor=signed_in, key="PT-TEST-000522")
        return form_services.reveal_value(actor=signed_in, value_id=patient_form_ticket.row.pk)

    holder = Held(lambda: ServiceRequest.objects.select_for_update().get(pk=busy.pk))
    deactivation = Session(_deactivate_by(person, User.objects.get(pk=owner.pk))).start()
    try:
        deactivation_state = deactivation.waiting_or_ended()
        session = Session(work).start()
        state = session.waiting_or_ended()
        behind_deactivation = _blocked_by(session.pid, deactivation.pid)
        statement = _statement(session.pid)
    finally:
        assert holder.release() == "done"
    deactivation.join()
    session.join()
    assert deactivation_state == "waiting", deactivation_state  # for the busy ticket
    assert deactivation.outcome == "done", repr(deactivation.error)
    assert not User.objects.get(pk=person.pk).is_active
    detail = {
        "reveal": (state, behind_deactivation, session.outcome),
        "waited in": statement.split()[0] + " ... " + " ".join(statement.split()[-3:]),
    }
    assert _refused(session), detail
    assert AuditEvent.objects.filter(action=actions[reveal]).count() == before, detail


# A3. An external identifier import and a change of an organization's group ---------------------


def test_an_external_identifier_import_and_an_organizations_group_change_never_deadlock(
    make_user, lis
):
    """Example Member Clinic is put into the group Example Health Group on its page (an account
    manager sets its parent organization) while a client manager previews an external identifier
    file with a row for each, the member first. The organization change locks the member, then
    walks up to its new parent (clients.services._check_parent); here it is paused between the
    two. The group was created first, so it has the lower id.

    Intended (clients.services._check_parent: the chain is walked upwards under row locks;
    dataimport: "never a server error for one row: the file is refused"; docs/architecture.md:
    the import "and a link added at the same moment wait for each other one way"): the two wait
    for each other one way, no 40P01, and both go through."""
    from clients import services as client_services
    from clients.models import Client
    from dataimport.importing import EXTERNAL_IDS, preview_file

    group = Client.objects.create(name="Example Health Group", account_number="EXGROUP")
    member = Client.objects.create(name="Example Member Clinic", account_number="EXMEMBER")
    assert group.pk < member.pk
    manager = make_user("review_manager", "account_manager")
    importer = make_user("review_importer", "account_manager")
    text = (
        "account_code,system,record_type,identifier_scheme,identifier,role,display_label,url\r\n"
        "EXMEMBER,lis,order,,ORD-TEST-000561,related,,\r\n"
        "EXGROUP,lis,order,,ORD-TEST-000562,related,,\r\n"
    )
    change = Session(
        lambda: client_services.update_client(
            actor=manager,
            client_id=member.pk,
            expected_revision=member.revision,
            reason="Joined the group",
            parent=group,
        )
    )
    gates = {change.thread: Gate()}
    preview = Session(
        lambda: preview_file(EXTERNAL_IDS, actor=importer, data=text.encode(), filename="ids.csv")
    )
    with gated(client_services, "_check_parent", gates):
        change.start()
        assert gates[change.thread].arrived.wait(timeout=10), change.error  # the member is locked
        preview.start()
        preview_state = preview.waiting_or_ended(seconds=3)
        gates[change.thread].go.set()
        change.join()
        preview.join()
    outcomes = {"organization change": change.outcome, "import preview": preview.outcome}
    assert "40P01" not in outcomes.values(), f"deadlock: {outcomes} (preview was {preview_state})"
    assert outcomes == {"organization change": "done", "import preview": "done"}, outcomes
    assert Client.objects.get(pk=member.pk).parent_id == group.pk


def test_an_import_naming_the_group_first_and_a_group_change_never_deadlock(make_user, lis):
    """The same race with the file naming the group first (the group was created first, so it has
    the lower id). The organization change locks the member, then walks up to its new parent
    (clients.services._check_parent), paused between the two. The import locks the group, then
    waits for the member; the change then waits for the group: the database ends the cycle by
    failing one of them, as it did when the file locked row by row (each row in its own
    savepoint: "The database refused this row"). Neither answers with a server error: the
    organization change goes through, and the preview, which waited first here, reports the file
    refused, with nothing imported."""
    from clients import services as client_services
    from clients.models import Client
    from dataimport.importing import EXTERNAL_IDS, LOCKS_REFUSED, preview_file

    group = Client.objects.create(name="Example Health Group", account_number="EXGROUP")
    member = Client.objects.create(name="Example Member Clinic", account_number="EXMEMBER")
    assert group.pk < member.pk
    manager = make_user("review_manager", "account_manager")
    importer = make_user("review_importer", "account_manager")
    text = (
        "account_code,system,record_type,identifier_scheme,identifier,role,display_label,url\r\n"
        "EXGROUP,lis,order,,ORD-TEST-000562,related,,\r\n"
        "EXMEMBER,lis,order,,ORD-TEST-000561,related,,\r\n"
    )
    change = Session(
        lambda: client_services.update_client(
            actor=manager,
            client_id=member.pk,
            expected_revision=member.revision,
            reason="Joined the group",
            parent=group,
        )
    )
    gates = {change.thread: Gate()}
    preview = Session(
        lambda: preview_file(EXTERNAL_IDS, actor=importer, data=text.encode(), filename="ids.csv")
    )
    with gated(client_services, "_check_parent", gates):
        change.start()
        assert gates[change.thread].arrived.wait(timeout=10), change.error  # the member is locked
        preview.start()
        preview_state = preview.waiting_or_ended(seconds=3)
        gates[change.thread].go.set()
        change.join()
        preview.join()
    outcomes = {"organization change": change.outcome, "import preview": preview.outcome}
    assert "40P01" not in outcomes.values(), f"server error: {outcomes} ({preview_state})"
    assert outcomes == {"organization change": "done", "import preview": "done"}, outcomes
    assert Client.objects.get(pk=member.pk).parent_id == group.pk
    result = preview.result
    refused = result.file_errors == [LOCKS_REFUSED] or any(row.errors for row in result.rows)
    assert refused or result.ok, (result.file_errors, [row.errors for row in result.rows])
