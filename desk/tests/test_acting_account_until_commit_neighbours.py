"""The acting account held until commit (accounts.acting): the neighbouring cases around the
acceptance file. All records are synthetic.

Whatever takes access away from an account (a deactivation, a move to the portal, a removed role,
capability or team membership, changed portal access) waits for that account's change already
under way, which is saved first. A change that starts while such an account change is under way
(not committed) waits at its first step and is then refused, saving nothing. And ending a
person's work never deadlocks (PostgreSQL 40P01) with that person's own change.

Every party runs in its own PostgreSQL session with a bounded lock_timeout. Each test asserts
first that the party under test was seen waiting (and behind whom), and only then the outcome.
"""

import threading
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.db import connection, transaction

from accounts.acting import hold_actor
from accounts.models import User
from clients.models import ClientSite, PortalAccess
from clients.services import set_portal_access
from core.models import LabModule
from dataimport.importing import EXTERNAL_IDS, commit_file, preview_file
from desk import services
from desk.models import EntryKind, RequestEntry, Status
from links import services as link_services
from links.models import ExternalRecordLink, ExternalSystem, LinkAttachment
from nonconformance import services as nc_services
from nonconformance.models import Nonconformance
from teams import services as team_services
from teams.models import TeamMembership

from .test_contract import new_request, selection
from .test_review_assignment_lock_order_verify3 import (
    Held,
    Session,
    _blocked_by,
    _deactivate_by,
    _fresh,
    _remove_role,
    _set_lock_timeout,
    paused,
    run_here,
)

pytestmark = pytest.mark.django_db(transaction=True)

CAPABILITY = "view_patient_information"


@pytest.fixture(autouse=True)
def locmem_mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


@pytest.fixture
def clerk(make_user):
    return make_user("neighbour_clerk", "client_services")


@pytest.fixture
def owner(make_user):
    return make_user("neighbour_owner", "admin")


@pytest.fixture
def lead(make_user):
    return make_user("neighbour_lead", "supervisor")


@pytest.fixture
def lis(db):
    return ExternalSystem.objects.get_or_create(
        key="lis", defaults={"name": "Laboratory information system", "kind": "lis"}
    )[0]


@pytest.fixture
def two_site_portal(clinic_portal, clinic):
    """The clinic's portal account, granted two of its sites (A and B)."""
    site_a = ClientSite.objects.create(client=clinic, code="SA", name="Site A")
    site_b = ClientSite.objects.create(client=clinic, code="SB", name="Site B")
    access = PortalAccess.objects.create(user=clinic_portal, client=clinic)
    access.sites.set([site_a, site_b])
    return SimpleNamespace(user=clinic_portal, access=access, a=site_a, b=site_b)


def _permission():
    return Permission.objects.get(codename=CAPABILITY, content_type__app_label="accounts")


def _note(actor, ticket, body):
    seen = _fresh(ticket)
    return services.add_entry(
        actor=actor,
        request=seen,
        kind=EntryKind.NOTE,
        body=body,
        expected_revision=seen.revision,
    )


def _notes(ticket, body):
    return RequestEntry.objects.filter(request=ticket, body=body).count()


def _in_both_orders_never_deadlock(first, second, hold_name="_hold_assignment"):
    """Run `first` and `second` in two sessions that both take their first locks (the call of
    services.`hold_name`) before either goes on; returns their outcomes."""
    barrier = threading.Barrier(2, timeout=3)
    real = getattr(services, hold_name)

    def meeting(*args, **kwargs):
        held = real(*args, **kwargs)
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass  # the other one waits for a lock: reported by the outcomes
        return held

    from unittest.mock import patch

    with patch.object(services, hold_name, meeting):
        sessions = [Session(first).start(), Session(second).start()]
        outcomes = [session.join() for session in sessions]
    return outcomes, sessions


# 1. Capability: a person-level link addition and the removal of the capability ------------------


def _grant(person, how, role="client_services"):
    """Give `person` the capability directly or through their role; returns how to remove it."""
    permission = _permission()
    if how == "direct":
        person.user_permissions.add(permission)
        return lambda: person.user_permissions.remove(permission)
    group = Group.objects.get(name=role)
    group.permissions.add(permission)
    return lambda: group.permissions.remove(permission)


def _holds_capability(person, how):
    person = User.objects.get(pk=person.pk)
    if how == "direct":
        return person.user_permissions.filter(codename=CAPABILITY).exists()
    return person.groups.filter(permissions__codename=CAPABILITY).exists()


def _add_person_link(person, ticket, lis, identifier):
    return lambda: link_services.add_link(
        actor=person,
        request_id=ticket.pk,
        system=lis,
        record_type="patient",
        identifier=identifier,
    )


@pytest.mark.parametrize("how", ["direct", "role"])
def test_removing_the_capability_waits_for_a_person_link_addition_under_way(
    how, clerk, agent, category, lis
):
    """A clerk with the patient-information capability (granted to them, or through their role)
    adds a person-level link to a ticket; the transaction is still open. The capability is taken
    away meanwhile. The removal waits for the addition, which is saved first by an account that
    still had the capability; the capability is gone afterwards."""
    ticket = new_request(agent, category)
    remove = _grant(clerk, how)
    holder = Held(_add_person_link(clerk, ticket, lis, "ORD-TEST-000401"))
    removal = Session(remove).start()
    try:
        state = removal.waiting_or_ended()
        behind_addition = _blocked_by(removal.pid, holder.session.pid)
    finally:
        assert holder.release() == "done"
    removal.join()
    assert (state, behind_addition) == ("waiting", True)
    assert removal.outcome == "done", repr(removal.error)
    assert LinkAttachment.objects.filter(request=ticket, is_person=True).count() == 1
    assert not _holds_capability(clerk, how)


@pytest.mark.parametrize("how", ["direct", "role"])
def test_a_person_link_addition_that_starts_during_the_capability_removal_is_refused(
    how, clerk, agent, category, lis
):
    """The capability's removal is under way (its row deleted, not committed). A person-level
    link addition started meanwhile waits for it at its first step, then reads the account
    without the capability and is refused: no link is saved."""
    ticket = new_request(agent, category)
    remove = _grant(clerk, how)
    holder = Held(remove)
    addition = Session(_add_person_link(clerk, ticket, lis, "ORD-TEST-000402")).start()
    try:
        state = addition.waiting_or_ended()
        behind_removal = _blocked_by(addition.pid, holder.session.pid)
    finally:
        assert holder.release() == "done"
    addition.join()
    assert (state, behind_removal) == ("waiting", True)
    assert addition.outcome == "denied", (addition.outcome, repr(addition.error))
    assert not LinkAttachment.objects.filter(request=ticket).exists()
    assert not _holds_capability(clerk, how)


# 2. Team membership -----------------------------------------------------------------------------


@pytest.fixture
def quality_member(make_user, join, quality):
    person = make_user("neighbour_quality", "technical")
    return SimpleNamespace(user=person, membership=join(person, quality))


def test_removing_a_team_member_waits_for_the_members_note_on_a_restricted_ticket(
    quality_member, lead, agent, privacy_category
):
    """A technical member of Quality writes a note on a restricted ticket of the team (Privacy
    request); the transaction is still open. A supervisor removes the member from the team
    meanwhile. The removal waits for the note, which is saved first; the membership is gone
    afterwards."""
    ticket = new_request(agent, privacy_category, client=None)
    body = "Request ORD-TEST-000411 verified."
    holder = Held(lambda: _note(quality_member.user, ticket, body))
    removal = Session(
        lambda: team_services.remove_member(actor=lead, membership=quality_member.membership)
    ).start()
    try:
        state = removal.waiting_or_ended()
        behind_note = _blocked_by(removal.pid, holder.session.pid)
    finally:
        assert holder.release() == "done"
    removal.join()
    assert (state, behind_note) == ("waiting", True)
    assert removal.outcome == "done", repr(removal.error)
    assert _notes(ticket, body) == 1
    assert not TeamMembership.objects.filter(pk=quality_member.membership.pk).exists()


def test_a_note_started_during_a_member_removal_is_refused(
    quality_member, lead, agent, privacy_category
):
    """The member's removal is under way (membership deleted, not committed). The member's note
    on the team's restricted ticket, started meanwhile, waits for it at its first step, then no
    longer finds the ticket (not found), and saves nothing."""
    ticket = new_request(agent, privacy_category, client=None)
    body = "Request ORD-TEST-000412 verified."
    holder = Held(
        lambda: team_services.remove_member(actor=lead, membership=quality_member.membership)
    )
    note = Session(lambda: _note(quality_member.user, ticket, body)).start()
    try:
        state = note.waiting_or_ended()
        behind_removal = _blocked_by(note.pid, holder.session.pid)
    finally:
        assert holder.release() == "done"
    note.join()
    assert (state, behind_removal) == ("waiting", True)
    assert note.outcome == "not found", (note.outcome, repr(note.error))
    assert _notes(ticket, body) == 0


@pytest.mark.parametrize("change", ["note", "hand-over"])
def test_removing_a_member_and_the_members_own_change_to_their_ticket_never_deadlock(
    change, quality_member, lead, agent, make_user, join, quality, sample_category, accessioning
):
    """A member of Accessioning holds a ticket and adds a note to it, or hands it to a colleague,
    while a supervisor removes the member from the team. The member's change has locked the ticket
    when the removal starts (it waits for the membership the change holds). Neither ends with a
    deadlock (40P01): both go through, the change first."""
    person = make_user("neighbour_tech", "technical")
    membership = join(person, accessioning)
    colleague = make_user("neighbour_tech2", "technical")
    join(colleague, accessioning)
    ticket = new_request(
        agent, sample_category, subject="Recollection", team=accessioning, assignee=person
    )
    body = "Recollection booked for the next round."
    sessions, seen = {}, {}

    def during():
        sessions["removal"] = Session(
            lambda: team_services.remove_member(actor=lead, membership=membership)
        ).start()
        seen["removal"] = sessions["removal"].waiting_or_ended()

    def own_change():
        if change == "note":
            _note(person, ticket, body)
            return
        fresh = _fresh(ticket)
        services.assign(
            actor=person,
            request=fresh,
            expected_revision=fresh.revision,
            team=accessioning,
            assignee=colleague,
            reason="Handing over",
        )

    with paused(services, "_locked_request", during, before=False):
        outcome = {"own change": run_here(own_change)}
    outcome["removal"] = sessions["removal"].join()
    assert seen["removal"] == "waiting", seen
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}"
    assert outcome == {"own change": "done", "removal": "done"}, outcome
    assert not TeamMembership.objects.filter(pk=membership.pk).exists()
    if change == "note":
        assert _notes(ticket, body) == 1
    # The removal returned the member's tickets; a ticket already handed over stays handed over.
    assert _fresh(ticket).assignee_id == (None if change == "note" else colleague.pk)


# 3. Portal access -------------------------------------------------------------------------------


def _narrow_to_b(manager, portal):
    return lambda: set_portal_access(
        actor=manager,
        user_id=portal.user.pk,
        sites=[portal.b],
        expected_client_id=portal.user.client_id,
        expected_revision=PortalAccess.objects.get(pk=portal.access.pk).revision,
        reason="Site A closed",
    )


def _reply(portal, ticket, body):
    def run():
        seen = _fresh(ticket)
        return services.add_entry(
            actor=User.objects.get(pk=portal.user.pk),
            request=seen,
            kind=EntryKind.REPLY,
            body=body,
            expected_revision=seen.revision,
        )

    return run


@pytest.fixture
def site_a_ticket(agent, category, clinic, two_site_portal):
    return new_request(agent, category, client=clinic, site=two_site_portal.a)


def test_narrowing_portal_access_waits_for_a_reply_under_way(
    make_user, two_site_portal, site_a_ticket
):
    """A portal account with sites A and B replies on a ticket for site A; the transaction is
    still open. A client manager narrows the account to site B meanwhile. The change waits for
    the reply, which is saved first by an account that still had site A; the narrowing follows."""
    manager = make_user("neighbour_manager", "account_manager")
    body = "Thank you, ORD-TEST-000421 received."
    holder = Held(_reply(two_site_portal, site_a_ticket, body))
    narrowing = Session(_narrow_to_b(manager, two_site_portal)).start()
    try:
        state = narrowing.waiting_or_ended()
        behind_reply = _blocked_by(narrowing.pid, holder.session.pid)
    finally:
        assert holder.release() == "done"
    narrowing.join()
    assert (state, behind_reply) == ("waiting", True)
    assert narrowing.outcome == "done", repr(narrowing.error)
    assert _notes(site_a_ticket, body) == 1
    assert list(PortalAccess.objects.get(pk=two_site_portal.access.pk).sites.all()) == [
        two_site_portal.b
    ]


def test_a_portal_reply_started_during_the_narrowing_is_refused(
    make_user, two_site_portal, site_a_ticket
):
    """The narrowing to site B is under way (not committed). A reply on a site A ticket, started
    meanwhile, waits for it at its first step, then no longer finds the ticket (not found), and
    saves nothing."""
    manager = make_user("neighbour_manager", "account_manager")
    body = "Thank you, ORD-TEST-000422 received."
    holder = Held(_narrow_to_b(manager, two_site_portal))
    reply = Session(_reply(two_site_portal, site_a_ticket, body)).start()
    try:
        state = reply.waiting_or_ended()
        behind_narrowing = _blocked_by(reply.pid, holder.session.pid)
    finally:
        assert holder.release() == "done"
    reply.join()
    assert (state, behind_narrowing) == ("waiting", True)
    assert reply.outcome == "not found", (reply.outcome, repr(reply.error))
    assert _notes(site_a_ticket, body) == 0


# 4 and 5. A save of the account waits for the account's change under way ------------------------


def _change_under_way(person, ticket, body):
    return Held(lambda: _note(person, ticket, body))


def test_moving_a_staff_account_to_the_portal_waits_for_its_change_under_way(
    clerk, agent, category, clinic
):
    """A clerk adds a note; the transaction is still open. The account is moved to the portal
    meanwhile (client set, saved with update_fields=["client"]). The save waits for the note,
    which is saved first by an account that was still staff. (The move itself is then refused by
    the application's own rule that an account never changes kind; the wait is what is tested.)"""
    ticket = new_request(agent, category)
    body = "Order ORD-TEST-000431 checked."
    holder = _change_under_way(clerk, ticket, body)

    def move():
        account = User.objects.get(pk=clerk.pk)
        account.client = clinic
        account.save(update_fields=["client"])

    move_session = Session(move).start()
    try:
        state = move_session.waiting_or_ended()
        behind_note = _blocked_by(move_session.pid, holder.session.pid)
    finally:
        assert holder.release() == "done"
    move_session.join()
    assert (state, behind_note) == ("waiting", True)
    # The application then refuses the move itself (clients.signals: an account stays a
    # laboratory account or a portal account for life); the save waited first all the same.
    assert move_session.outcome == "refused", (move_session.outcome, repr(move_session.error))
    assert _notes(ticket, body) == 1
    assert User.objects.get(pk=clerk.pk).client_id is None


def test_a_save_of_is_active_alone_waits_for_the_accounts_change_under_way(clerk, agent, category):
    """Not only a full save: a save that names only is_active in update_fields also waits for
    the account's change under way, which is saved first; the account ends inactive."""
    ticket = new_request(agent, category)
    body = "Order ORD-TEST-000432 checked."
    holder = _change_under_way(clerk, ticket, body)

    def deactivate():
        account = User.objects.get(pk=clerk.pk)
        account.is_active = False
        account.save(update_fields=["is_active"])

    session = Session(deactivate).start()
    try:
        state = session.waiting_or_ended()
        behind_note = _blocked_by(session.pid, holder.session.pid)
    finally:
        assert holder.release() == "done"
    session.join()
    assert (state, behind_note) == ("waiting", True)
    assert session.outcome == "done", repr(session.error)
    assert _notes(ticket, body) == 1
    assert not User.objects.get(pk=clerk.pk).is_active


def test_a_save_of_the_last_sign_in_does_not_wait_for_the_accounts_change(clerk, agent, category):
    """A sign-in's save (update_fields=["last_login"]) takes no access away: it does not wait for
    the account's change under way."""
    ticket = new_request(agent, category)
    holder = _change_under_way(clerk, ticket, "Order ORD-TEST-000433 checked.")

    def sign_in():
        account = User.objects.get(pk=clerk.pk)
        account.last_login = account.date_joined
        account.save(update_fields=["last_login"])

    session = Session(sign_in).start()
    try:
        state = session.waiting_or_ended()
    finally:
        assert holder.release() == "done"
    session.join()
    assert state == "ended", state
    assert session.outcome == "done", repr(session.error)
    assert User.objects.get(pk=clerk.pk).last_login is not None


# 6. No deadlock between people ------------------------------------------------------------------


def test_two_people_assigning_tickets_to_each_other_never_deadlock(
    make_user, join, agent, sample_category, accessioning
):
    """A hands ticket 1 to B while B hands ticket 2 to A, both members of the team. Both have
    taken their first locks (the person they assign to, in _hold_assignment) before either goes
    on. Neither waits for the other there (the account is held FOR KEY SHARE, an assignment takes
    FOR NO KEY UPDATE), so neither ends with a deadlock (40P01): both are done."""
    first = make_user("neighbour_a", "technical")
    second = make_user("neighbour_b", "technical")
    join(first, accessioning)
    join(second, accessioning)
    ticket1 = new_request(agent, sample_category, subject="One", team=accessioning, assignee=first)
    ticket2 = new_request(agent, sample_category, subject="Two", team=accessioning, assignee=second)

    def hand_over(actor, ticket, to):
        def run():
            seen = _fresh(ticket)
            services.assign(
                actor=actor,
                request=seen,
                expected_revision=seen.revision,
                team=accessioning,
                assignee=to,
                reason="Swapping",
            )

        return run

    outcomes, sessions = _in_both_orders_never_deadlock(
        hand_over(first, ticket1, second), hand_over(second, ticket2, first)
    )
    assert "40P01" not in outcomes, f"deadlock: {outcomes}"
    assert outcomes == ["done", "done"], (outcomes, [repr(s.error) for s in sessions])
    assert _fresh(ticket1).assignee_id == second.pk
    assert _fresh(ticket2).assignee_id == first.pk


def test_one_person_taking_two_tickets_at_once_never_deadlocks(
    make_user, join, agent, sample_category, accessioning
):
    """A technician takes two unassigned tickets in two sessions at the same moment, both having
    taken their first locks before either goes on. No deadlock (40P01): both are done (or one is
    refused as changed, never a database error)."""
    person = make_user("neighbour_taker", "technical")
    join(person, accessioning)
    tickets = [
        new_request(agent, sample_category, subject=f"Free {n}", team=accessioning) for n in (1, 2)
    ]

    def take(ticket):
        def run():
            seen = _fresh(ticket)
            services.take(actor=person, request=seen, expected_revision=seen.revision)

        return run

    outcomes, sessions = _in_both_orders_never_deadlock(take(tickets[0]), take(tickets[1]))
    assert "40P01" not in outcomes, f"deadlock: {outcomes}"
    assert set(outcomes) <= {"done", "refused"}, (outcomes, [repr(s.error) for s in sessions])
    assert "done" in outcomes
    taken = [_fresh(t).assignee_id == person.pk for t in tickets]
    assert taken.count(True) == outcomes.count("done"), (outcomes, taken)


# 7. Nonconformance ------------------------------------------------------------------------------


@pytest.fixture
def owned_record(db, lead, make_user):
    LabModule.objects.create(key="nonconformance", is_enabled=True, revision=1)
    person = make_user("neighbour_nc_owner", "technical")
    record = nc_services.raise_nonconformance(
        actor=lead,
        title="Freezer excursion",
        description="The freezer log shows a night excursion.",
        source="equipment",
        severity="major",
        owner=person,
    )
    return SimpleNamespace(person=person, record=record)


def _revise(owned, *, actor=None):
    def run():
        record = Nonconformance.objects.get(pk=owned.record.pk)
        return nc_services.update_details(
            actor=actor or owned.person,
            nc=record,
            expected_revision=record.revision,
            title="Freezer excursion at night",
            description=record.description,
            source=record.source,
            severity=record.severity,
            area="",
            reason="Clearer title",
        )

    return run


def test_deactivating_a_record_owner_and_their_own_change_to_it_never_deadlock(owned_record, owner):
    """The owner of a nonconformance corrects its details; the change has locked the record when
    an administrator deactivates the owner (a full save). The deactivation waits for the change
    (the account is held until it commits). No deadlock (40P01): both are done, the change
    first, and the account ends inactive."""
    sessions, seen = {}, {}

    def during():
        sessions["deactivation"] = Session(_deactivate_by(owned_record.person, owner)).start()
        seen["deactivation"] = sessions["deactivation"].waiting_or_ended()

    with paused(nc_services, "_lock", during, before=False):
        outcome = {"own change": run_here(_revise(owned_record))}
    outcome["deactivation"] = sessions["deactivation"].join()
    assert seen["deactivation"] == "waiting", seen
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}"
    assert outcome == {"own change": "done", "deactivation": "done"}, outcome
    assert not User.objects.get(pk=owned_record.person.pk).is_active
    assert (
        Nonconformance.objects.get(pk=owned_record.record.pk).title == "Freezer excursion at night"
    )


def test_a_nonconformance_change_that_starts_during_its_actors_deactivation_is_refused(
    owned_record, owner
):
    """The owner's deactivation is under way (not committed). The owner's change to the record,
    started meanwhile, waits for it at its first step, then reads the account as inactive and is
    refused. The record is unchanged."""
    holder = Held(_deactivate_by(owned_record.person, owner))
    change = Session(_revise(owned_record)).start()
    try:
        state = change.waiting_or_ended()
        behind_deactivation = _blocked_by(change.pid, holder.session.pid)
    finally:
        assert holder.release() == "done"
    change.join()
    assert (state, behind_deactivation) == ("waiting", True)
    assert change.outcome == "denied", (change.outcome, repr(change.error))
    assert Nonconformance.objects.get(pk=owned_record.record.pk).title == "Freezer excursion"
    assert not User.objects.get(pk=owned_record.person.pk).is_active


# 8. Bulk change ---------------------------------------------------------------------------------


def test_a_bulk_status_change_and_the_deactivation_of_the_person_never_deadlock(
    make_user, join, owner, agent, sample_category, accessioning
):
    """A technician sets a status on two tickets they hold in one bulk action. The action has
    locked the tickets when an administrator deactivates the technician. The deactivation waits
    for the action (the account is held until it commits) instead of taking the tickets in the
    other order. No deadlock (40P01): both are done, and the tickets return to the queue."""
    person = make_user("neighbour_bulk", "technical")
    join(person, accessioning)
    tickets = [
        new_request(agent, sample_category, subject=f"Bulk {n}", team=accessioning, assignee=person)
        for n in (1, 2)
    ]
    wanted = selection(tickets)
    sessions, seen = {}, {}

    def during():
        sessions["deactivation"] = Session(_deactivate_by(person, owner)).start()
        seen["deactivation"] = sessions["deactivation"].waiting_or_ended()

    with paused(services, "_bulk_requests", during, before=False):
        outcome = {
            "bulk": run_here(
                lambda: services.bulk_status(
                    actor=person, selected=wanted, status=Status.WAITING_CLIENT
                )
            )
        }
    outcome["deactivation"] = sessions["deactivation"].join()
    assert seen["deactivation"] == "waiting", seen
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}"
    assert outcome == {"bulk": "done", "deactivation": "done"}, outcome
    assert not User.objects.get(pk=person.pk).is_active
    for ticket in tickets:
        fresh = _fresh(ticket)
        assert fresh.status == Status.WAITING_CLIENT
        assert fresh.assignee_id is None


# 9. External identifier import ------------------------------------------------------------------

HEADER = "account_code,system,record_type,identifier_scheme,identifier,role,display_label,url\r\n"
FILE = (
    HEADER
    + "EXCLIN,lis,order,,ORD-TEST-000441,related,,\r\n"
    + "EXWATER,lis,order,,ORD-TEST-000442,related,,\r\n"
)


def _imported():
    return ExternalRecordLink.objects.filter(identifier__startswith="ORD-TEST-00044").count()


def test_removing_the_importing_managers_role_waits_for_an_import_under_way(
    make_user, clinic, water, lis
):
    """A client manager imports a file of external identifiers; the transaction is still open.
    Their role is removed meanwhile. The removal waits for the import, which is saved first; the
    role is gone afterwards."""
    manager = make_user("neighbour_importer", "account_manager")
    token = preview_file(
        EXTERNAL_IDS, actor=manager, data=FILE.encode(), filename="ids.csv"
    ).state_token
    holder = Held(
        lambda: commit_file(
            EXTERNAL_IDS,
            actor=manager,
            data=FILE.encode(),
            filename="ids.csv",
            reason="",
            state_token=token,
        )
    )
    removal = Session(_remove_role(manager, "account_manager")).start()
    try:
        state = removal.waiting_or_ended()
        behind_import = _blocked_by(removal.pid, holder.session.pid)
    finally:
        assert holder.release() == "done"
    removal.join()
    assert (state, behind_import) == ("waiting", True)
    assert removal.outcome == "done", repr(removal.error)
    assert _imported() == 2
    assert not User.objects.get(pk=manager.pk).groups.filter(name="account_manager").exists()


def test_an_import_started_during_the_managers_role_removal_is_refused(
    make_user, clinic, water, lis
):
    """The manager's role removal is under way (not committed). An import started meanwhile waits
    for it before its first lock, then reads the account without the role and is refused
    (PermissionDenied). Nothing is written."""
    manager = make_user("neighbour_importer", "account_manager")
    holder = Held(_remove_role(manager, "account_manager"))
    session = Session(
        lambda: preview_file(EXTERNAL_IDS, actor=manager, data=FILE.encode(), filename="ids.csv")
    ).start()
    try:
        state = session.waiting_or_ended()
        behind_removal = _blocked_by(session.pid, holder.session.pid)
    finally:
        assert holder.release() == "done"
    session.join()
    assert (state, behind_removal) == ("waiting", True)
    assert session.outcome == "denied", (session.outcome, repr(session.error))
    assert _imported() == 0


# 10. hold_actor ---------------------------------------------------------------------------------


def test_hold_actor_outside_a_transaction_is_an_error(clerk):
    """The account is held for a transaction: outside one there is nothing to hold it until."""
    assert not connection.in_atomic_block
    with pytest.raises(RuntimeError):
        hold_actor(clerk)


@pytest.mark.parametrize("kind", ["anonymous", "unsaved", "missing"])
def test_hold_actor_returns_none_for_an_account_that_does_not_exist(kind):
    """An anonymous visitor, an unsaved account and an id with no row hold nothing: None."""
    actor = {
        "anonymous": AnonymousUser(),
        "unsaved": User(username="nobody"),
        "missing": User(pk=987654321),
    }[kind]
    with transaction.atomic():
        assert hold_actor(actor) is None


def test_hold_actor_returns_the_account_as_read_under_its_lock(clerk):
    """The account held is returned as the database has it now."""
    with transaction.atomic():
        current = hold_actor(User(pk=clerk.pk))
    assert current.pk == clerk.pk
    assert current.username == clerk.username


def test_a_second_hold_in_the_same_transaction_does_not_wait(clerk):
    """The transaction holds the account; another session waits to deactivate it. A second hold
    in the same transaction waits for nothing (it holds nothing new) and the transaction commits,
    after which the deactivation goes through."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        my_pid = cursor.fetchone()[0]
    deactivation = None
    _set_lock_timeout("3s")
    try:
        with transaction.atomic():
            assert hold_actor(clerk).pk == clerk.pk
            deactivation = Session(_deactivate_by(clerk, clerk)).start()
            state = deactivation.waiting_or_ended()
            behind_us = _blocked_by(deactivation.pid, my_pid)
            again = hold_actor(clerk)
    finally:
        _set_lock_timeout("0")
        if deactivation is not None:
            deactivation.join()
    assert (state, behind_us) == ("waiting", True)
    assert again is not None and again.pk == clerk.pk
    assert deactivation.outcome == "done", repr(deactivation.error)
    assert not User.objects.get(pk=clerk.pk).is_active
