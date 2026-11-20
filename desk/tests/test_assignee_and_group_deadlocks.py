"""Review follow-up: deadlocks with a ticket's assignee and with an organization's group.

Two pairs of changes met in a database deadlock (40P01), which PostgreSQL ends by failing whichever
began waiting first:

- a change that names a ticket's assignee without locking the account (a client's reply records a
  notice to the assignee, checked when it commits) and the deactivation of that assignee, which
  locked the account FOR UPDATE and then waited for the person's tickets (review A6 and V3);
- an import whose file names a group before one of its members, and putting that member into the
  group on its page, which locks the member and then walks up to the group (review A3 and V1).

Now the deactivation holds the account FOR NO KEY UPDATE and locks the person's open tickets before
it updates the account (accounts.models.User.save, desk.signals), and every import, every change
that puts an organization under a parent and every creation under one take the organizations'
groups lock first (clients.services.hold_groups). The cases below are the reviewers' (A6 as
written; V1 and V3 adapted where their setup was the deadlock's own interleaving, which can no
longer happen) and their neighbours. Every party runs in its own PostgreSQL session with a bounded
lock_timeout; a party is stopped at a named point until the others wait (pg_blocking_pids) or end.
All records are synthetic.
"""

import dataclasses
import threading

import pytest
from django.db import connection, transaction
from django.test import Client as HttpClient
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import User
from clients import services as client_services
from clients.models import Client, PortalAccess
from desk import notifications, services
from desk.models import EntryKind, RequestEntry, Status
from jobs.models import Notice
from links import services as link_services
from links.models import ExternalRecordLink, ExternalSystem
from teams import services as team_services
from teams.models import TeamMembership

from .test_contract import new_request, set_status
from .test_review_acting_account_until_commit import Gate, _admin_change_data, gated
from .test_review_assignment_lock_order_verify3 import (
    Held,
    Session,
    _blocked_by,
    _deactivate_by,
    _fresh,
    _statement,
)

pytestmark = pytest.mark.django_db(transaction=True)

HEADER = "account_code,system,record_type,identifier_scheme,identifier,role,display_label,url\r\n"


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
    return make_user("agd_owner", "admin", is_staff=True, is_superuser=True)


@pytest.fixture
def assigned(make_user, join, agent, sample_category, accessioning, clinic, clinic_portal):
    """A technician of Accessioning with a ticket of Example Clinic assigned to them, and the
    clinic's portal account, which sees every ticket of the clinic."""
    person = make_user("agd_assignee", "technical")
    join(person, accessioning)
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    ticket = new_request(
        agent, sample_category, client=clinic, team=accessioning, assignee=person, subject="Reply"
    )
    return person, ticket


def _client_reply(portal, ticket, body):
    def run():
        seen = _fresh(ticket)
        return services.add_entry(
            actor=User.objects.get(pk=portal.pk),
            request=seen,
            kind=EntryKind.REPLY,
            body=body,
            expected_revision=seen.revision,
        )

    return run


def _deactivate_renaming(user, owner, email):
    """A deactivation that also changes the account's email in the same save: an update of a
    unique column, which PostgreSQL makes a key update (the row then FOR UPDATE)."""

    def run():
        account = User.objects.get(pk=user.pk)
        account.is_active = False
        account.email = email
        account.changed_by = owner
        account.save()

    return run


def _notices_to(user):
    return Notice.objects.filter(recipient=user).count()


# A6. A client's reply under way, then the assignee's deactivation ------------------------------


def test_a_client_reply_and_the_deactivation_of_the_tickets_assignee_never_deadlock(
    make_user, join, owner, agent, sample_category, accessioning, clinic, clinic_portal
):
    """A client replies in the portal on a ticket assigned to a technician; the reply records a
    notice to the technician (desk.notifications.entry_added). At that moment an owner
    deactivates the technician: the save locks the account and then waits for the ticket, which
    the reply holds. The reply then commits, and PostgreSQL checks the notice's recipient (the
    technician's account) at COMMIT.

    Intended (desk/signals.py and docs/architecture.md: deactivating an account updates it and
    then returns the person's tickets to the queue, and the paths that meet there wait for each
    other one way; accounts/acting.py: the rows a change names are checked at COMMIT, which is
    why the acting account is held): no 40P01. Both are saved and the ticket is back in the
    queue. The hold covers the acting account only, not the other accounts a change names.

    Adopted unchanged from review round 1 of the acting account (A6)."""
    person = make_user("review_assignee", "technical")
    join(person, accessioning)
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    ticket = new_request(
        agent, sample_category, client=clinic, team=accessioning, assignee=person, subject="Reply"
    )

    def reply():
        seen = _fresh(ticket)
        return services.add_entry(
            actor=User.objects.get(pk=clinic_portal.pk),
            request=seen,
            kind=EntryKind.REPLY,
            body="Any news on order ORD-TEST-000571?",
            expected_revision=seen.revision,
        )

    change = Session(reply)
    gates = {change.thread: Gate()}
    with gated(notifications, "entry_added", gates, before=False):
        change.start()
        assert gates[change.thread].arrived.wait(timeout=10), change.error  # notice recorded
        deactivation = Session(_deactivate_by(person, owner)).start()
        deactivation_state = deactivation.waiting_or_ended(seconds=3)
        behind_reply = _blocked_by(deactivation.pid, change.pid)
        gates[change.thread].go.set()
        change.join()
        deactivation.join()
    outcomes = {"client reply": change.outcome, "deactivation": deactivation.outcome}
    assert (deactivation_state, behind_reply) == ("waiting", True)  # for the ticket
    assert "40P01" not in outcomes.values(), f"deadlock: {outcomes}"
    assert outcomes == {"client reply": "done", "deactivation": "done"}, outcomes
    assert not User.objects.get(pk=person.pk).is_active
    assert _fresh(ticket).assignee_id is None


@pytest.mark.parametrize("how", ["email changed too", "resolved ticket", "account page"])
def test_a_client_reply_under_way_and_the_assignees_deactivation_never_deadlock(
    how, assigned, owner, clinic_portal
):
    """A6's neighbours. The reply holds the ticket and has recorded its notice to the assignee
    when the deactivation starts:

    - the deactivation also changes the person's email, a key update that makes the database
      take the row as FOR UPDATE does: the person's tickets are locked before the account is
      updated, so the reply's check of the notice's recipient still does not wait;
    - the ticket is resolved, and the client's reply reopens it (the deactivation returns
      resolved tickets too);
    - the deactivation comes from the account page, which also changes the email.

    Intended: the deactivation waits for the ticket, the reply commits without waiting, both are
    saved, and the ticket is back in its queue."""
    person, ticket = assigned
    if how == "resolved ticket":
        ticket = set_status(ticket, Status.RESOLVED)
    if how == "account page":
        http = HttpClient()
        http.force_login(owner)
        data = _admin_change_data(http, person, is_active=False, email="agd-moved@example.invalid")
        url = reverse("admin:accounts_user_change", args=[person.pk])

        def deactivate():
            return http.post(url, data)

    elif how == "email changed too":
        deactivate = _deactivate_renaming(person, owner, "agd-moved@example.invalid")
    else:
        deactivate = _deactivate_by(person, owner)
    body = f"Any news on order ORD-TEST-000581 ({how})?"
    change = Session(_client_reply(clinic_portal, ticket, body))
    gates = {change.thread: Gate()}
    with gated(notifications, "entry_added", gates, before=False):
        change.start()
        assert gates[change.thread].arrived.wait(timeout=10), change.error  # notice recorded
        deactivation = Session(deactivate).start()
        deactivation_state = deactivation.waiting_or_ended(seconds=3)
        behind_reply = _blocked_by(deactivation.pid, change.pid)
        gates[change.thread].go.set()
        reply_state = change.waiting_or_ended(seconds=3)
        change.join()
        deactivation.join()
    outcomes = {"client reply": change.outcome, "deactivation": deactivation.outcome}
    assert (deactivation_state, behind_reply) == ("waiting", True)  # for the ticket
    assert reply_state == "ended", reply_state  # its COMMIT did not wait for the account
    assert "40P01" not in outcomes.values(), f"deadlock: {outcomes}"
    assert outcomes == {"client reply": "done", "deactivation": "done"}, (
        outcomes,
        deactivation.error,
    )
    if how == "account page":
        assert deactivation.result.status_code == 302
    account = User.objects.get(pk=person.pk)
    assert not account.is_active
    if how != "resolved ticket":
        assert account.email == "agd-moved@example.invalid"
    assert RequestEntry.objects.filter(body=body).exists()
    current = _fresh(ticket)
    assert current.assignee_id is None
    if how == "resolved ticket":
        assert current.status == Status.OPEN  # reopened by the reply


# V3. The deactivation under way, then a client's reply --------------------------------------------


@pytest.mark.parametrize("email_changed", [False, True], ids=["deactivation", "email changed too"])
def test_a_deactivation_under_way_and_a_client_reply_never_deadlock(
    email_changed, assigned, owner, clinic_portal
):
    """The other order (review V3, adapted). The owner's save has locked the technician's
    account and their open tickets and updated the account, and is about to return the tickets
    to their queues, when the client replies on one of them.

    The reviewer stopped the deactivation after its update while the reply already held the
    ticket; the deactivation now locks the tickets before it updates the account, so that
    interleaving no longer occurs: whichever locks the ticket first goes first. Here the
    deactivation does.

    Intended: no 40P01. The reply, drawn before the deactivation, waits for the ticket (not in
    its COMMIT); returning the ticket to the queue changed it, so the reply is refused as changed
    (desk.services.STALE), with nothing saved. Sent again, it is saved, finds the ticket
    unassigned and records no notice to the deactivated technician."""
    person, ticket = assigned
    if email_changed:
        deactivate = _deactivate_renaming(person, owner, "agd-renamed@example.invalid")
    else:
        deactivate = _deactivate_by(person, owner)
    body = "Any news on order ORD-TEST-000711?"
    deactivation = Session(deactivate)
    change = Session(_client_reply(clinic_portal, ticket, body))
    at_release = {deactivation.thread: Gate()}
    notices = _notices_to(person)
    with gated(services, "release_assignments", at_release):
        deactivation.start()
        assert at_release[deactivation.thread].arrived.wait(timeout=10), deactivation.error
        change.start()
        reply_state = change.waiting_or_ended(seconds=3)
        reply_statement = _statement(change.pid)
        behind_deactivation = _blocked_by(change.pid, deactivation.pid)
        at_release[deactivation.thread].go.set()
        deactivation.join()
        change.join()
    outcomes = {"client reply": change.outcome, "deactivation": deactivation.outcome}
    assert (reply_state, behind_deactivation) == ("waiting", True)
    assert reply_statement != "COMMIT" and "desk_servicerequest" in reply_statement
    assert "40P01" not in outcomes.values(), f"deadlock: {outcomes}"
    assert outcomes == {"client reply": "refused", "deactivation": "done"}, outcomes
    assert services.STALE in change.error.messages
    assert not RequestEntry.objects.filter(body=body).exists()
    assert _fresh(ticket).assignee_id is None
    _client_reply(clinic_portal, ticket, body)()  # sent again from the reloaded ticket
    assert RequestEntry.objects.filter(body=body).count() == 1
    assert _notices_to(person) == notices


# Neighbours of the assignee pair ------------------------------------------------------------------


def test_a_staff_reply_and_the_deactivation_of_the_portal_account_it_tells_never_wait(
    make_user, owner, agent, sample_category, accessioning, clinic, clinic_contact, clinic_portal
):
    """A laboratory reply records a notice to the clinic's portal account linked to the ticket's
    contact, and holds the ticket, when an owner deactivates that portal account. A portal
    account is never an assignee, so its deactivation waits for no ticket; the reply's check of
    the notice's recipient at COMMIT does not wait for the deactivation's lock on the account.

    Intended: neither waits for the other; both are saved."""
    PortalAccess.objects.create(
        user=clinic_portal, client=clinic, whole_organization=True, contact=clinic_contact
    )
    ticket = new_request(
        agent, sample_category, client=clinic, contact=clinic_contact, team=accessioning
    )
    body = "The result for order ORD-TEST-000591 is attached in the portal."

    def reply():
        seen = _fresh(ticket)
        return services.add_entry(
            actor=agent,
            request=seen,
            kind=EntryKind.REPLY,
            body=body,
            expected_revision=seen.revision,
        )

    change = Session(reply)
    gates = {change.thread: Gate()}
    with gated(notifications, "entry_added", gates, before=False):
        change.start()
        assert gates[change.thread].arrived.wait(timeout=10), change.error
        assert _notices_to(clinic_portal) == 0  # recorded, not committed
        deactivation = Session(_deactivate_by(clinic_portal, owner)).start()
        deactivation_state = deactivation.waiting_or_ended(seconds=3)
        gates[change.thread].go.set()
        reply_state = change.waiting_or_ended(seconds=3)
        change.join()
        deactivation.join()
    assert deactivation_state == "ended", deactivation_state
    assert reply_state == "ended", reply_state
    assert (change.outcome, deactivation.outcome) == ("done", "done"), (
        change.error,
        deactivation.error,
    )
    assert RequestEntry.objects.filter(body=body).exists()
    assert not User.objects.get(pk=clinic_portal.pk).is_active


def test_a_client_reply_and_the_assignees_removal_from_the_team_never_deadlock(
    make_user, assigned, clinic_portal, accessioning
):
    """A supervisor removes the technician from Accessioning while a client's reply on their
    ticket holds it and has recorded the notice. The removal locks the membership and then the
    person's tickets of that team; the reply's check of the notice's recipient concerns the
    account, which the removal does not lock.

    Intended: the removal waits for the ticket, the reply commits, both are saved, and the
    ticket is back in the team's queue."""
    person, ticket = assigned
    lead = make_user("agd_lead", "supervisor")
    membership = TeamMembership.objects.get(team=accessioning, user=person)
    body = "Any news on order ORD-TEST-000601?"
    change = Session(_client_reply(clinic_portal, ticket, body))
    gates = {change.thread: Gate()}
    with gated(notifications, "entry_added", gates, before=False):
        change.start()
        assert gates[change.thread].arrived.wait(timeout=10), change.error
        removal = Session(
            lambda: team_services.remove_member(
                actor=lead, membership=membership, reason="Moved to another bench"
            )
        ).start()
        removal_state = removal.waiting_or_ended(seconds=3)
        behind_reply = _blocked_by(removal.pid, change.pid)
        gates[change.thread].go.set()
        change.join()
        removal.join()
    assert (removal_state, behind_reply) == ("waiting", True)
    assert (change.outcome, removal.outcome) == ("done", "done"), (change.error, removal.error)
    assert RequestEntry.objects.filter(body=body).exists()
    assert _fresh(ticket).assignee_id is None
    assert not TeamMembership.objects.filter(pk=membership.pk).exists()


def test_an_assignment_during_the_deactivation_still_waits_and_is_refused(
    make_user, assigned, owner, agent, sample_category, accessioning
):
    """The deactivation's lock on the account is FOR NO KEY UPDATE now, which an assignment also
    takes on its assignee. A supervisor assigns another ticket to the technician while the
    deactivation is under way (stopped before returning the tickets).

    Intended (desk/signals.py: an assignment to the account either comes first and its ticket is
    returned, or waits and is refused): the assignment waits for the deactivation and is
    refused; the ticket stays with the team, unassigned."""
    person, _ticket = assigned
    lead = make_user("agd_assigning_lead", "supervisor")
    other = new_request(agent, sample_category, team=accessioning, subject="Another")
    deactivation = Session(_deactivate_by(person, owner))
    at_release = {deactivation.thread: Gate()}

    def assign():
        seen = _fresh(other)
        return services.assign(
            actor=lead,
            request=seen,
            expected_revision=seen.revision,
            team=accessioning,
            assignee=User.objects.get(pk=person.pk),
        )

    with gated(services, "release_assignments", at_release):
        deactivation.start()
        assert at_release[deactivation.thread].arrived.wait(timeout=10), deactivation.error
        assignment = Session(assign).start()
        assignment_state = assignment.waiting_or_ended(seconds=3)
        behind = _blocked_by(assignment.pid, deactivation.pid)
        at_release[deactivation.thread].go.set()
        deactivation.join()
        assignment.join()
    assert (assignment_state, behind) == ("waiting", True)
    assert deactivation.outcome == "done", deactivation.error
    assert assignment.outcome == "refused", (assignment.outcome, assignment.error)
    assert _fresh(other).assignee_id is None


def test_a_deactivation_locks_the_tickets_before_it_updates_the_account(assigned, owner):
    """The statements of a deactivation, in order: the access gate, the account FOR NO KEY
    UPDATE (not FOR UPDATE), the person's open tickets FOR UPDATE, the account's update, then
    each ticket's release."""
    person, ticket = assigned
    with CaptureQueriesContext(connection) as queries:
        _deactivate_by(person, owner)()
    sql = [q["sql"] for q in queries.captured_queries]

    def first(predicate):
        return next(index for index, text in enumerate(sql) if predicate(text))

    gate = first(lambda s: "pg_advisory_xact_lock(" in s)
    account_lock = first(lambda s: 'FROM "accounts_user"' in s and " FOR " in s)
    tickets_lock = first(lambda s: 'FROM "desk_servicerequest"' in s and "FOR UPDATE" in s)
    account_update = first(lambda s: s.startswith('UPDATE "accounts_user"'))
    ticket_update = first(lambda s: s.startswith('UPDATE "desk_servicerequest"'))
    assert "FOR NO KEY UPDATE" in sql[account_lock], sql[account_lock]
    assert gate < account_lock < tickets_lock < account_update < ticket_update
    assert _fresh(ticket).assignee_id is None


# V1. An import and an organization's group change ---------------------------------------------


def _group_and_member():
    group = Client.objects.create(name="Example Health Group", account_number="EXGROUP")
    member = Client.objects.create(name="Example Member Clinic", account_number="EXMEMBER")
    assert group.pk < member.pk
    return group, member


GROUP_FIRST = (
    HEADER
    + "EXGROUP,lis,order,,ORD-TEST-000701,related,,\r\n"
    + "EXMEMBER,lis,order,,ORD-TEST-000702,related,,\r\n"
)


def _paused_after_the_first_organization(gates):
    """The external identifier record type, stopped (in the gated session) once it holds the
    file's first organization and before it locks the second (inside RecordType.hold)."""
    from dataimport import importing

    record_type = importing.EXTERNAL_IDS
    real = record_type.hold

    def hold(rows):
        first = next(
            row.new_fields["client_id"]
            for row in rows
            if not row.errors and row.action == "create" and row.existing is None
        )
        list(Client.objects.select_for_update().filter(pk=first).values_list("pk"))
        gate = gates.get(threading.current_thread())
        if gate is not None:
            gate.stop()
        return real(rows)

    return dataclasses.replace(record_type, hold=hold)


def _join_group(manager, member, group):
    return lambda: client_services.update_client(
        actor=manager,
        client_id=member.pk,
        expected_revision=member.revision,
        reason="Joined the group",
        parent=group,
    )


def test_an_import_holding_the_group_and_a_group_change_take_their_turns(make_user, lis):
    """A client manager imports an external identifier file naming Example Health Group, then
    Example Member Clinic (the group has the lower id). The import holds the group. At that
    moment an account manager puts the member into the group on its page: the change would lock
    the member and walk up to the group (clients.services._check_parent).

    Review V1, adapted to the intended outcome: the reviewer's test expected the file refused,
    as the documentation then said. Now the change waits for the import before its first lock
    (the organizations' groups lock), so the two never meet in a deadlock.

    Intended: the change waits behind the import; no 40P01; the import is saved (both
    identifiers) and then the organization change (the member is in the group)."""
    from dataimport.importing import commit_file, preview_file

    group, member = _group_and_member()
    manager = make_user("agd_org_manager", "account_manager")
    importer = make_user("agd_importer", "account_manager")
    gates = {}
    record_type = _paused_after_the_first_organization(gates)
    token = preview_file(
        record_type, actor=importer, data=GROUP_FIRST.encode(), filename="ids.csv"
    ).state_token
    commit = Session(
        lambda: commit_file(
            record_type,
            actor=importer,
            data=GROUP_FIRST.encode(),
            filename="ids.csv",
            reason="",
            state_token=token,
        )
    )
    gates[commit.thread] = Gate()
    change = Session(_join_group(manager, member, group))
    commit.start()
    assert gates[commit.thread].arrived.wait(timeout=10), commit.error  # holds the group
    change.start()
    change_state = change.waiting_or_ended(seconds=3)
    behind_import = change.pid is not None and _blocked_by(change.pid, commit.pid)
    statement = _statement(change.pid)
    gates[commit.thread].go.set()
    commit.join()
    change.join()
    outcomes = {"organization change": change.outcome, "import": commit.outcome}
    assert (change_state, behind_import) == ("waiting", True)
    assert "pg_advisory_xact_lock" in statement, statement  # before the member's lock
    assert "40P01" not in outcomes.values(), f"deadlock: {outcomes}"
    assert outcomes == {"organization change": "done", "import": "done"}, (
        outcomes,
        change.error,
        commit.error,
    )
    assert Client.objects.get(pk=member.pk).parent_id == group.pk
    assert ExternalRecordLink.objects.filter(identifier__startswith="ORD-TEST-00070").count() == 2


def test_creating_an_organization_under_a_group_an_import_holds_takes_its_turn(make_user, lis):
    """A new organization is created under Example Health Group (the creation walks up from the
    parent) while an import holds the group. Intended: the creation waits for the import, then
    both are saved."""
    from dataimport.importing import commit_file, preview_file

    group, _member = _group_and_member()
    manager = make_user("agd_creator", "account_manager")
    importer = make_user("agd_importer_two", "account_manager")
    gates = {}
    record_type = _paused_after_the_first_organization(gates)
    token = preview_file(
        record_type, actor=importer, data=GROUP_FIRST.encode(), filename="ids.csv"
    ).state_token
    commit = Session(
        lambda: commit_file(
            record_type,
            actor=importer,
            data=GROUP_FIRST.encode(),
            filename="ids.csv",
            reason="",
            state_token=token,
        )
    )
    gates[commit.thread] = Gate()
    creation = Session(
        lambda: client_services.create_client(
            actor=manager, name="Example North Clinic", account_number="EXNORTH", parent=group
        )
    )
    commit.start()
    assert gates[commit.thread].arrived.wait(timeout=10), commit.error
    creation.start()
    creation_state = creation.waiting_or_ended(seconds=3)
    behind_import = _blocked_by(creation.pid, commit.pid)
    gates[commit.thread].go.set()
    commit.join()
    creation.join()
    assert (creation_state, behind_import) == ("waiting", True)
    assert (creation.outcome, commit.outcome) == ("done", "done"), (creation.error, commit.error)
    assert Client.objects.get(account_number="EXNORTH").parent_id == group.pk
    assert ExternalRecordLink.objects.filter(identifier__startswith="ORD-TEST-00070").count() == 2


def test_two_group_changes_that_would_close_a_loop_wait_and_the_second_is_refused(make_user):
    """Two account managers at the same moment: one puts Example Member Clinic under Example
    Health Group, the other the group under the member. Each change locks its own organization
    and walks up from the new parent, so the two used to lock the same rows in opposite orders.

    Intended: the second waits for the first, then reads the new group and is refused because the
    parent would make the organization part of its own group; never a 40P01."""
    group, member = _group_and_member()
    first_manager = make_user("agd_first_manager", "account_manager")
    second_manager = make_user("agd_second_manager", "account_manager")
    first = Session(_join_group(first_manager, member, group))
    second = Session(
        lambda: client_services.update_client(
            actor=second_manager,
            client_id=group.pk,
            expected_revision=group.revision,
            reason="Joined the member",
            parent=member,
        )
    )
    gates = {first.thread: Gate()}
    with gated(client_services, "_check_parent", gates):
        first.start()
        assert gates[first.thread].arrived.wait(timeout=10), first.error  # holds the member
        second.start()
        second_state = second.waiting_or_ended(seconds=3)
        behind_first = _blocked_by(second.pid, first.pid)
        gates[first.thread].go.set()
        first.join()
        second.join()
    assert (second_state, behind_first) == ("waiting", True)
    assert first.outcome == "done", first.error
    assert second.outcome == "refused", (second.outcome, second.error)
    assert "part of its own group" in str(second.error)
    assert Client.objects.get(pk=member.pk).parent_id == group.pk
    assert Client.objects.get(pk=group.pk).parent_id is None


IMPORT_FILES = {
    "organizations": "account_code,name,parent_account_code\r\nEXMEMBER,Example Member Clinic,\r\n",
    "sites": "account_code,site_code,name,kind\r\nEXMEMBER,DRAW,Draw station,collection_site\r\n",
    "contacts": "account_code,email,name\r\nEXMEMBER,desk@member.example.invalid,Example Desk\r\n",
    "external-ids": HEADER + "EXMEMBER,lis,order,,ORD-TEST-000721,related,,\r\n",
}


@pytest.mark.parametrize("slug", sorted(IMPORT_FILES))
def test_every_import_takes_the_groups_lock_before_its_first_organization(make_user, lis, slug):
    """Each record type's preview waits for the organizations' groups lock, held by another
    session, before it reads or locks an organization; once it is released the preview runs."""
    from dataimport import importing

    _group_and_member()
    importer = make_user("agd_type_importer", "account_manager")
    record_type = importing.BY_SLUG[slug]
    holder = Held(client_services.hold_groups)
    preview = Session(
        lambda: importing.preview_file(
            record_type, actor=importer, data=IMPORT_FILES[slug].encode(), filename="f.csv"
        )
    ).start()
    try:
        state = preview.waiting_or_ended(seconds=3)
        behind = _blocked_by(preview.pid, holder.session.pid)
    finally:
        assert holder.release() == "done"
    preview.join()
    assert (state, behind) == ("waiting", True)
    assert preview.outcome == "done", preview.error
    assert preview.result.ok, (preview.result.file_errors, [r.errors for r in preview.result.rows])


@pytest.mark.parametrize("change", ["details", "leaving the group"])
def test_an_organization_change_without_a_new_parent_does_not_wait_for_the_groups_lock(
    make_user, change
):
    """Changing an organization's details, or taking it out of its group, locks that one
    organization and walks no chain: it does not take the groups lock, and does not wait for an
    import or a group change elsewhere."""
    group, member = _group_and_member()
    member.parent = group
    member.save()
    manager = make_user("agd_detail_manager", "account_manager")
    if change == "details":
        data = {"phone": "555-0177"}
    else:
        data = {"parent": None}
    holder = Held(client_services.hold_groups)
    session = Session(
        lambda: client_services.update_client(
            actor=manager,
            client_id=member.pk,
            expected_revision=member.revision,
            reason="Updated",
            **data,
        )
    ).start()
    try:
        state = session.waiting_or_ended(seconds=3)
    finally:
        assert holder.release() == "done"
    session.join()
    assert state == "ended", state
    assert session.outcome == "done", session.error


def test_the_groups_lock_is_taken_inside_a_transaction_only():
    with pytest.raises(RuntimeError):
        client_services.hold_groups()
    with transaction.atomic():
        client_services.hold_groups()
        client_services.hold_groups()  # again in the same transaction: no wait


def test_a_link_on_an_organization_does_not_take_the_groups_lock(make_user, lis):
    """A link added on an organization's page locks that organization and the system only."""
    _group, member = _group_and_member()
    manager = make_user("agd_link_manager", "account_manager")
    holder = Held(client_services.hold_groups)
    session = Session(
        lambda: link_services.add_link(
            actor=manager,
            client_id=member.pk,
            system=lis,
            record_type="order",
            identifier="ORD-TEST-000731",
        )
    ).start()
    try:
        state = session.waiting_or_ended(seconds=3)
    finally:
        assert holder.release() == "done"
    session.join()
    assert state == "ended", state
    assert session.outcome == "done", session.error
