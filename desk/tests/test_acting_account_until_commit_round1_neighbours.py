"""The acting account held until commit, round 1: the cases next to each fix of the review's
findings A1 to A5 (access gates, accounts.acting). All records are synthetic.

Every party runs in its own PostgreSQL session (a thread with its own connection) with a bounded
lock_timeout. Each test first asserts that the party under test was seen waiting (and behind
whom, with pg_blocking_pids), and only then the outcome.
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django import forms
from django.contrib.auth.models import Group, Permission
from django.db import connection, transaction
from django.test import Client
from django.urls import reverse

from accounts.acting import ACCESS_GATE, close_access, hold_actor
from accounts.models import User
from clients import services as client_services
from clients.models import Client as Client_model
from clients.models import ClientSite, PortalAccess
from core.models import AuditEvent, LabModule
from dataimport.importing import EXTERNAL_IDS, commit_file, preview_file
from desk import services
from desk.models import EntryKind
from links import views as link_views
from links.models import ExternalRecordLink, ExternalSystem, LinkAttachment
from nonconformance import services as nc_services
from request_forms import definition
from request_forms import request_views as form_views
from request_forms import services as form_services
from request_forms.models import FieldKind, FieldValue, RequestTemplate
from teams import services as team_services
from teams.models import TeamMembership

from .test_acting_account_until_commit_neighbours import (
    _add_person_link,
    _grant,
    _narrow_to_b,
    _notes,
    _reply,
)
from .test_contract import new_request
from .test_review_acting_account_until_commit import (
    Gate,
    _admin_change_data,
    _grant_capability,
    _note,
    _person_link,
    _refused,
    gated,
)
from .test_review_assignment_lock_order_verify3 import (
    Held,
    Session,
    _blocked_by,
    _deactivate_by,
    _fresh,
    _set_lock_timeout,
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
def lead(make_user):
    return make_user("nb2_lead", "supervisor")


def _pid():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def _post_as(user, url, data):
    """The page's POST as `user` would send it, as a function to run in a session."""
    http = Client()
    http.force_login(user)
    return lambda: http.post(url, data)


# A1. A second change started while an access change waits for the person's first change ---------

CASES = [
    "team member removal",
    "capability removed from the person",
    "capability removed from the role",
    "portal sites narrowed",
    "portal site removed",
    "account deactivated (is_active only)",
    "role removed from the group's side",
]


def _arrange(case, fx):
    """A person with a first change to hold open, the access change, a second change of theirs
    (as a request starting now reads its account), and what to check afterwards."""
    make_user, agent = fx("make_user"), fx("agent")
    out = SimpleNamespace(refused_by="denied")
    if case == "team member removal":
        person = make_user("nb2_member", "technical")
        membership = fx("join")(person, fx("quality"))
        privacy = fx("privacy_category")
        t1 = new_request(agent, privacy, client=None, subject="One")
        t2 = new_request(agent, privacy, client=None, subject="Two")
        out.first = lambda: _note(person, t1, "First restricted note ORD-TEST-000601.")
        out.access = lambda: team_services.remove_member(actor=fx("lead"), membership=membership)
        out.second = lambda: _note(User.objects.get(pk=person.pk), t2, "Second restricted note.")
        out.saved = lambda: _notes(t2, "Second restricted note.") > 0
        out.taken = lambda: not TeamMembership.objects.filter(pk=membership.pk).exists()
        out.refused_by = "not found"
    elif case in ("capability removed from the person", "capability removed from the role"):
        how = "direct" if "person" in case else "role"
        person = make_user(f"nb2_{how}", "client_services")
        category, lis = fx("category"), fx("lis")
        t1, t2 = (new_request(agent, category, subject=s) for s in ("One", "Two"))
        out.access = _grant(person, how)
        out.first = _add_person_link(person, t1, lis, "ORD-TEST-000602")
        out.second = lambda: _add_person_link(
            User.objects.get(pk=person.pk), t2, lis, "ORD-TEST-000603"
        )()
        out.saved = lambda: LinkAttachment.objects.filter(request=t2).exists()
        out.taken = lambda: (
            not (
                User.objects.get(pk=person.pk).user_permissions.filter(codename=CAPABILITY).exists()
                if how == "direct"
                else User.objects.get(pk=person.pk)
                .groups.filter(permissions__codename=CAPABILITY)
                .exists()
            )
        )
    elif case in ("portal sites narrowed", "portal site removed"):
        clinic = fx("clinic")
        portal_user = fx("clinic_portal")
        a = ClientSite.objects.create(client=clinic, code="SA", name="Site A")
        b = ClientSite.objects.create(client=clinic, code="SB", name="Site B")
        access = PortalAccess.objects.create(user=portal_user, client=clinic)
        access.sites.set([a, b])
        portal = SimpleNamespace(user=portal_user, access=access, a=a, b=b)
        category = fx("category")
        t1, t2 = (
            new_request(agent, category, client=clinic, site=a, subject=s) for s in ("1", "2")
        )
        out.first = _reply(portal, t1, "First reply ORD-TEST-000604.")
        if case == "portal sites narrowed":
            out.access = _narrow_to_b(make_user("nb2_manager", "account_manager"), portal)
        else:
            out.access = lambda: PortalAccess.objects.get(pk=access.pk).sites.remove(a)
        out.second = _reply(portal, t2, "Second reply ORD-TEST-000605.")
        out.saved = lambda: _notes(t2, "Second reply ORD-TEST-000605.") > 0
        out.taken = lambda: list(PortalAccess.objects.get(pk=access.pk).sites.all()) == [b]
        out.refused_by = "not found"
    else:
        person = make_user("nb2_clerk", "client_services")
        category = fx("category")
        t1, t2 = (new_request(agent, category, subject=s) for s in ("One", "Two"))
        out.first = lambda: _note(person, t1, "First note ORD-TEST-000606.")
        out.second = lambda: _note(User.objects.get(pk=person.pk), t2, "Second note.")
        out.saved = lambda: _notes(t2, "Second note.") > 0
        if case == "role removed from the group's side":
            out.access = lambda: Group.objects.get(name="client_services").user_set.remove(
                User.objects.get(pk=person.pk)
            )
            out.taken = lambda: not User.objects.get(pk=person.pk).groups.exists()
        else:

            def deactivate():
                account = User.objects.get(pk=person.pk)
                account.is_active = False
                account.save(update_fields=["is_active"])

            out.access = deactivate
            out.taken = lambda: not User.objects.get(pk=person.pk).is_active
    return out


@pytest.mark.parametrize("case", CASES)
def test_a_change_started_while_an_access_change_waits_queues_behind_it_and_is_refused(
    case, request, lead, lis, agent, category, privacy_category, quality, clinic, clinic_portal
):
    """A person's first change is under way (its transaction is open). An access change to that
    person (a team removal, a capability removed from them or from their role, narrowed or
    removed portal sites, a deactivation saved with is_active alone, a role removed from the
    group's side) waits for it. While it waits, the person starts a second change.

    Intended (accounts/acting.py: "a change that starts while an access change waits queues
    behind it, reads the account once it has committed, and is refused"): the second change is
    seen waiting, blocked by the access change's session (not overtaking it), and is refused once
    the access change commits. Nothing of it is saved. The first change is saved first."""
    fx = request.getfixturevalue
    s = _arrange(case, fx)
    holder = Held(s.first)
    access = Session(s.access).start()
    try:
        access_state = access.waiting_or_ended(seconds=3)
        behind_first = access.pid is not None and _blocked_by(access.pid, holder.session.pid)
        later = Session(s.second).start()
        later_state = later.waiting_or_ended(seconds=3)
        behind_access = later.pid is not None and _blocked_by(later.pid, access.pid)
        access_still_waiting = access.waiting_or_ended(seconds=0.2) == "waiting"
    finally:
        assert holder.release() == "done"
    access.join()
    later.join()
    detail = {
        "access change": (access_state, "behind the first change" if behind_first else "?"),
        "second change": (later_state, "behind the access change" if behind_access else "?"),
        "access change still waiting": access_still_waiting,
        "outcomes": (access.outcome, later.outcome),
    }
    assert (access_state, behind_first) == ("waiting", True), detail
    assert (later_state, behind_access, access_still_waiting) == ("waiting", True, True), detail
    assert access.outcome == "done", repr(access.error)
    assert s.taken()
    assert _refused(later), detail
    assert later.outcome == s.refused_by, detail
    assert not s.saved(), detail


# A1. The role group page ---------------------------------------------------------------------


def _group_page_data(http, group, *, without):
    """The role group page's form as drawn, with the capability `without` unticked."""
    url = reverse("admin:auth_group_change", args=[group.pk])
    response = http.get(url)
    assert response.status_code == 200
    form = response.context["adminform"].form
    data = {}
    for name, field in form.fields.items():
        if field.disabled:
            continue
        value = form[name].value()
        if isinstance(value, (list, tuple)):
            data[name] = [str(item) for item in value]
        elif value is not None and not isinstance(field.widget, forms.CheckboxInput):
            data[name] = value
    data["permissions"] = [p for p in data.get("permissions", []) if p != str(without.pk)]
    return url, data


@pytest.mark.parametrize("other", ["lower id", "higher id"])
def test_the_role_group_page_removing_a_capability_waits_for_a_members_change(
    other, make_user, agent, category, lis
):
    """A member of the client services role has the patient-information capability through the
    role and is adding a person-level link (open transaction). An owner opens the role group page
    and unticks the capability. While the page waits, another member's person-level link
    addition starts.

    Intended (accounts/acting.py: the role group page takes every member's gate first): the page
    waits for the link addition under way, which is saved. The page then saves, and the role no
    longer holds the capability.

    The page takes the members' gates one after another in id order. "lower id": the other member
    has a lower id than the member whose change the page waits for, so the page already holds
    their gate: their addition queues behind the page and is refused. "higher id": the page has
    not reached their gate yet while it waits, so their addition goes ahead and is saved first,
    by a member who still held the capability; the page then waits for it in turn (it never
    commits before a change it has not waited for). Either way nothing is saved after the
    removal."""
    group = Group.objects.get_or_create(name="client_services")[0]
    permission = Permission.objects.get(codename=CAPABILITY, content_type__app_label="accounts")
    group.permissions.add(permission)
    if other == "lower id":  # the page has already closed the gate of this member when it waits
        second_member = make_user("nb2_member_two", "client_services")
        member = make_user("nb2_member_one", "client_services")
    else:
        member = make_user("nb2_member_one", "client_services")
        second_member = make_user("nb2_member_two", "client_services")
    superuser = make_user("nb2_group_owner", "admin", is_staff=True, is_superuser=True)
    t1, t2 = (new_request(agent, category, subject=s) for s in ("One", "Two"))
    http = Client()
    http.force_login(superuser)
    url, data = _group_page_data(http, group, without=permission)
    holder = Held(_add_person_link(member, t1, lis, "ORD-TEST-000611"))
    page = Session(lambda: http.post(url, data)).start()
    try:
        page_state = page.waiting_or_ended(seconds=3)
        behind_addition = page.pid is not None and _blocked_by(page.pid, holder.session.pid)
        later = Session(
            lambda: _add_person_link(
                User.objects.get(pk=second_member.pk), t2, lis, "ORD-TEST-000612"
            )()
        ).start()
        later_state = later.waiting_or_ended(seconds=3)
        behind_page = later.pid is not None and _blocked_by(later.pid, page.pid)
    finally:
        assert holder.release() == "done"
    page.join()
    later.join()
    detail = (page_state, behind_addition, later_state, behind_page, later.outcome)
    assert (page_state, behind_addition) == ("waiting", True), detail
    if other == "lower id":
        assert (later_state, behind_page) == ("waiting", True), detail
    assert page.outcome == "done" and page.result.status_code == 302, repr(page.error)
    assert LinkAttachment.objects.filter(request=t1, is_person=True).count() == 1
    if other == "lower id":
        assert later.outcome == "denied", detail
        assert not LinkAttachment.objects.filter(request=t2).exists()
    else:
        assert later.outcome == "done", detail
        removal = AuditEvent.objects.filter(action="group.updated").latest("occurred_at")
        added = AuditEvent.objects.filter(action="external_link.attached").latest("occurred_at")
        assert added.occurred_at < removal.occurred_at, "the addition is saved before the removal"
    assert not group.permissions.filter(pk=permission.pk).exists()


# A2. An owner's account page ----------------------------------------------------------------


def _two_owners(make_user, join, accessioning):
    one = make_user("nb2_owner_one", "supervisor", is_staff=True, is_superuser=True)
    two = make_user("nb2_owner_two", "supervisor", is_staff=True, is_superuser=True)
    join(one, accessioning)
    join(two, accessioning)
    return one, two


def _outcomes_have_no_deadlock(outcomes, note=""):
    assert "40P01" not in outcomes.values(), f"deadlock: {outcomes} {note}"
    assert all(o in ("done", "denied", "refused", "not found") for o in outcomes.values()), (
        f"server error: {outcomes} {note}"
    )


def test_an_owner_stepping_down_and_their_own_take_never_deadlock(
    make_user, join, agent, sample_category, accessioning
):
    """An owner unticks their own Superuser status on the account page (another owner remains)
    while their own take of a ticket of their team runs in another session, paused after its
    first locks (_hold_assignment).

    Intended (accounts/acting.py: ending a person's work and that person's own change never wait
    for each other in a cycle; the page takes the account's gate before its owner check): no
    40P01 and no server error. The page waits for the take, which is saved; the page then saves
    and the account is no longer a superuser."""
    owner_one, _owner_two = _two_owners(make_user, join, accessioning)
    ticket = new_request(agent, sample_category, subject="Step down")
    http = Client()
    http.force_login(owner_one)
    data = _admin_change_data(http, owner_one, is_superuser=False)
    url = reverse("admin:accounts_user_change", args=[owner_one.pk])

    def take():
        seen = _fresh(ticket)
        return services.take(actor=owner_one, request=seen, expected_revision=seen.revision)

    change = Session(take)
    gates = {change.thread: Gate()}
    with gated(services, "_hold_assignment", gates):
        change.start()
        assert gates[change.thread].arrived.wait(timeout=10), change.error
        page = Session(lambda: http.post(url, data)).start()
        page_state = page.waiting_or_ended(seconds=3)
        behind_take = page.pid is not None and _blocked_by(page.pid, change.pid)
        gates[change.thread].go.set()
        change.join()
        page.join()
    outcomes = {"take": change.outcome, "page": page.outcome}
    _outcomes_have_no_deadlock(outcomes, f"(page was {page_state})")
    assert (page_state, behind_take) == ("waiting", True), (page_state, behind_take)
    assert change.outcome == "done"
    assert page.result.status_code == 302, page.result.status_code
    assert _fresh(ticket).assignee_id == owner_one.pk
    assert not User.objects.get(pk=owner_one.pk).is_superuser


def test_an_owner_deactivating_an_owner_who_names_a_record_owner_never_deadlock(
    make_user, join, lead, accessioning
):
    """An owner deactivates another owner on the account page. That second owner is a supervisor
    setting the owner of a nonconformance, choosing the deactivating owner as its new owner; the
    change has locked the record when the page is posted.

    Intended: no 40P01 and no server error. The page waits for the change (the account is held
    until it commits). Both go through: the owner is set, the account is inactive."""
    LabModule.objects.create(key="nonconformance", is_enabled=True, revision=1)
    owner_one, owner_two = _two_owners(make_user, join, accessioning)
    technician = make_user("nb2_nc_owner", "technical")
    record = nc_services.raise_nonconformance(
        actor=lead,
        title="Freezer excursion",
        description="The freezer log shows a night excursion.",
        source="equipment",
        severity="major",
        owner=technician,
    )
    page_post = _post_as(
        owner_one,
        reverse("admin:accounts_user_change", args=[owner_two.pk]),
        _admin_change_data(_logged(owner_one), owner_two, is_active=False),
    )

    def choose():
        return nc_services.set_owner(
            actor=owner_two, nc=record, expected_revision=record.revision, owner=owner_one
        )

    change = Session(choose)
    gates = {change.thread: Gate()}
    with gated(nc_services, "_lock", gates, before=False):
        change.start()
        assert gates[change.thread].arrived.wait(timeout=10), change.error
        page = Session(page_post).start()
        page_state = page.waiting_or_ended(seconds=3)
        behind_change = page.pid is not None and _blocked_by(page.pid, change.pid)
        gates[change.thread].go.set()
        change.join()
        page.join()
    outcomes = {"set owner": change.outcome, "page": page.outcome}
    _outcomes_have_no_deadlock(outcomes, f"(page was {page_state})")
    assert (page_state, behind_change) == ("waiting", True), (page_state, behind_change)
    assert change.outcome == "done", repr(change.error)
    assert page.result.status_code == 302, page.result.status_code
    assert not User.objects.get(pk=owner_two.pk).is_active
    assert nc_services.Nonconformance.objects.get(pk=record.pk).owner_id == owner_one.pk


def _logged(user):
    http = Client()
    http.force_login(user)
    return http


def test_an_owner_deactivated_while_taking_in_a_request_for_the_other_owner_never_deadlocks(
    make_user, join, sample_category, accessioning
):
    """An owner receives a new request by phone and assigns it to the other owner (an intake with
    an assignee); the intake has locked the assignee's rows when the other owner deactivates the
    first one on the account page.

    Intended: no 40P01 and no server error. The page waits for the intake (the account is held
    until it commits). Both go through: the request exists, assigned to the other owner, and the
    account is inactive."""
    owner_one, owner_two = _two_owners(make_user, join, accessioning)
    page_post = _post_as(
        owner_one,
        reverse("admin:accounts_user_change", args=[owner_two.pk]),
        _admin_change_data(_logged(owner_one), owner_two, is_active=False),
    )
    change = Session(
        lambda: new_request(
            owner_two, sample_category, subject="Intake", team=accessioning, assignee=owner_one
        )
    )
    gates = {change.thread: Gate()}
    with gated(services, "hold_intake_assignee", gates, before=False):
        change.start()
        assert gates[change.thread].arrived.wait(timeout=10), change.error
        page = Session(page_post).start()
        page_state = page.waiting_or_ended(seconds=3)
        behind_intake = page.pid is not None and _blocked_by(page.pid, change.pid)
        gates[change.thread].go.set()
        change.join()
        page.join()
    outcomes = {"intake": change.outcome, "page": page.outcome}
    _outcomes_have_no_deadlock(outcomes, f"(page was {page_state})")
    assert (page_state, behind_intake) == ("waiting", True), (page_state, behind_intake)
    assert change.outcome == "done", repr(change.error)
    assert page.result.status_code == 302, page.result.status_code
    assert change.result.assignee_id == owner_one.pk
    assert not User.objects.get(pk=owner_two.pk).is_active


# A3. The import, committed ------------------------------------------------------------------

HEADER = "account_code,system,record_type,identifier_scheme,identifier,role,display_label,url\r\n"


def _group_change_and_commit(make_user, member_first):
    group = Client_model.objects.create(name="Example Health Group", account_number="EXGROUP")
    member = Client_model.objects.create(name="Example Member Clinic", account_number="EXMEMBER")
    assert group.pk < member.pk
    manager = make_user("nb2_org_manager", "account_manager")
    importer = make_user("nb2_importer", "account_manager")
    rows = [("EXMEMBER", "ORD-TEST-000621"), ("EXGROUP", "ORD-TEST-000622")]
    if not member_first:
        rows.reverse()
    text = HEADER + "".join(f"{code},lis,order,,{key},related,,\r\n" for code, key in rows)
    token = preview_file(
        EXTERNAL_IDS, actor=importer, data=text.encode(), filename="ids.csv"
    ).state_token
    change = Session(
        lambda: client_services.update_client(
            actor=manager,
            client_id=member.pk,
            expected_revision=member.revision,
            reason="Joined the group",
            parent=group,
        )
    )
    commit = Session(
        lambda: commit_file(
            EXTERNAL_IDS,
            actor=importer,
            data=text.encode(),
            filename="ids.csv",
            reason="",
            state_token=token,
        )
    )
    gates = {change.thread: Gate()}
    with gated(client_services, "_check_parent", gates):
        change.start()
        assert gates[change.thread].arrived.wait(timeout=10), change.error
        commit.start()
        commit_state = commit.waiting_or_ended(seconds=3)
        gates[change.thread].go.set()
        change.join()
        commit.join()
    return group, member, change, commit, commit_state


def _linked():
    return ExternalRecordLink.objects.filter(identifier__startswith="ORD-TEST-00062").count()


def test_a_committed_import_and_an_organizations_group_change_never_deadlock(make_user, lis):
    """The organization change (locks the member, then walks up to its group, paused between) and
    the commit of a file naming the member first, then the group.

    Intended: the import waits for the member, then goes on; no 40P01, no server error; both are
    saved (the organization is in the group, both identifiers are imported)."""
    group, member, change, commit, state = _group_change_and_commit(make_user, member_first=True)
    outcomes = {"organization change": change.outcome, "commit": commit.outcome}
    assert "40P01" not in outcomes.values(), f"deadlock: {outcomes} (commit was {state})"
    assert outcomes == {"organization change": "done", "commit": "done"}, (outcomes, commit.error)
    assert Client_model.objects.get(pk=member.pk).parent_id == group.pk
    assert _linked() == 2


def test_a_committed_import_naming_the_group_first_waits_for_the_group_change(make_user, lis):
    """The same race with the file naming the group first. The import used to lock the group,
    then wait for the member the organization change held, while the change then needed the
    group: the database ended the cycle by failing one of them, and the import refused the file.

    Adapted in the follow-up on deadlocks with an organization's group: the change takes the
    organization directory lock before the member, and the import takes it before it reads the
    file (clients.services.hold_directory). Intended: the commit waits for the change
    before locking anything of the file, then goes on; no 40P01, no refusal; both are saved."""
    group, member, change, commit, state = _group_change_and_commit(make_user, member_first=False)
    outcomes = {"organization change": change.outcome, "commit": commit.outcome}
    assert state == "waiting", state
    assert "40P01" not in outcomes.values(), f"server error: {outcomes} (commit was {state})"
    assert outcomes == {"organization change": "done", "commit": "done"}, (outcomes, commit.error)
    assert Client_model.objects.get(pk=member.pk).parent_id == group.pk
    assert _linked() == 2


# A4. The reveal pages ---------------------------------------------------------------------


@pytest.fixture
def patient_value(make_user, category):
    template = RequestTemplate.objects.create(
        key="nb2-patient", name="Patient form", category=category, offered_in_portal=False
    )
    template.fields.create(
        key="patient",
        label="Patient identifier",
        kind=FieldKind.SHORT_TEXT,
        position=10,
        holds_patient_information=True,
    )
    capable = make_user("nb2_capable", "client_services")
    _grant_capability(capable)
    raised = form_services.create_request_with_form(
        actor=capable,
        template=template,
        expected_definition=definition.token(template),
        values={"patient": "PT-TEST-000631"},
        first_entry_kind=EntryKind.NOTE,
        channel="phone",
    )
    row = FieldValue.objects.get(filled_form__request=raised, superseded_at__isnull=True)
    return SimpleNamespace(ticket=raised, row=row)


@pytest.mark.parametrize("page", ["patient key", "key search", "patient value"])
def test_a_reveal_page_posted_before_the_deactivation_commits_is_refused(
    page, make_user, join, owner_account, accessioning, lis, patient_value
):
    """A technician holding the patient-information capability posts a reveal page (a patient
    key, a search by key, or a patient-information value). The page has loaded the session's
    user; then the technician's deactivation commits, just before the page calls its service.

    Intended (accounts/acting.py: a change that starts after the deactivation reads the account
    as held and is refused; the reveals read the account again after the hold): the page answers
    403 or 404, and no reveal or search is recorded in the audit trail."""
    person = make_user("nb2_revealer", "technical")
    join(person, accessioning)
    _grant_capability(person)
    attachment = _person_link(person, patient_value.ticket, lis, "PT-TEST-000632")
    action = {
        "patient key": "external_link.person_revealed",
        "key search": "external_link.person_searched",
        "patient value": "request_form.value_revealed",
    }[page]
    url, data, module, name = {
        "patient key": (
            reverse("link_records:attachment-reveal", args=[attachment.pk]),
            {},
            link_views,
            "reveal_person_key",
        ),
        "key search": (
            reverse("link_records:person-search"),
            {"key": "PT-TEST-000632"},
            link_views,
            "search_person_key",
        ),
        "patient value": (
            reverse("request_details:reveal", args=[patient_value.row.pk]),
            {},
            form_views,
            "services_reveal_value",
        ),
    }[page]
    before = AuditEvent.objects.filter(action=action).count()
    real = getattr(module, name)
    deactivated = []

    def after_deactivation(*args, **kwargs):
        deactivated.append(Session(_deactivate_by(person, owner_account)).start().join())
        return real(*args, **kwargs)

    with patch.object(module, name, after_deactivation):
        session = Session(_post_as(person, url, data)).start()
        session.join()
    assert deactivated == ["done"], deactivated
    assert session.outcome == "done", repr(session.error)
    assert session.result.status_code in (403, 404), session.result.status_code
    assert AuditEvent.objects.filter(action=action).count() == before


@pytest.fixture
def owner_account(make_user):
    return make_user("nb2_account_owner", "admin")


# A5. A grant after a change began, removed again before it commits ---------------------------


def test_removing_a_portal_site_granted_during_a_reply_waits_for_the_reply(
    clinic, clinic_portal, agent, category
):
    """A portal account granted site A replies on a ticket of site B. After the reply has held
    the account's rows, and before it reads the ticket, site B is granted to the account (an
    addition waits for nothing). The reply reads the ticket in scope. Then site B is removed
    again.

    Intended (accounts/acting.py: the removal of a portal site takes the account's gate): the
    removal waits for the reply, which is saved first; the site is gone afterwards."""
    a = ClientSite.objects.create(client=clinic, code="SA", name="Site A")
    b = ClientSite.objects.create(client=clinic, code="SB", name="Site B")
    access = PortalAccess.objects.create(user=clinic_portal, client=clinic)
    access.sites.set([a])
    ticket = new_request(agent, category, client=clinic, site=b)
    portal = SimpleNamespace(user=clinic_portal)
    body = "Reply for site B ORD-TEST-000641."
    reply = Session(_reply(portal, ticket, body))
    gates = {reply.thread: Gate()}
    with gated(services, "_locked_request", gates):
        reply.start()
        assert gates[reply.thread].arrived.wait(timeout=10), reply.error
        grant = Session(lambda: PortalAccess.objects.get(pk=access.pk).sites.add(b)).start().join()
        removal = Session(lambda: PortalAccess.objects.get(pk=access.pk).sites.remove(b)).start()
        state = removal.waiting_or_ended(seconds=3)
        behind_reply = removal.pid is not None and _blocked_by(removal.pid, reply.pid)
        gates[reply.thread].go.set()
        reply.join()
        removal.join()
    assert grant == "done"
    assert (state, behind_reply) == ("waiting", True), (state, behind_reply)
    assert removal.outcome == "done", repr(removal.error)
    assert reply.outcome == "done", (reply.outcome, repr(reply.error))
    assert _notes(ticket, body) == 1
    assert list(PortalAccess.objects.get(pk=access.pk).sites.all()) == [a]


def test_removing_a_role_granted_during_a_staff_change_waits_for_that_change(
    make_user, join, agent, sample_category, accessioning
):
    """A technician of Accessioning adds a note. After the change has held the technician's rows,
    and before it reads the ticket, an owner gives the technician the supervisor role (an addition
    waits for nothing). Then the role is removed again.

    Intended: the removal waits for the note, which is saved first; the role is gone afterwards."""
    person = make_user("nb2_roles", "technical")
    join(person, accessioning)
    ticket = new_request(agent, sample_category, subject="Role", team=accessioning)
    supervisor = Group.objects.get_or_create(name="supervisor")[0]
    body = "Role note ORD-TEST-000651."
    change = Session(lambda: _note(person, ticket, body))
    gates = {change.thread: Gate()}
    with gated(services, "_locked_request", gates):
        change.start()
        assert gates[change.thread].arrived.wait(timeout=10), change.error
        grant = Session(lambda: User.objects.get(pk=person.pk).groups.add(supervisor)).start()
        assert grant.join() == "done"
        removal = Session(lambda: User.objects.get(pk=person.pk).groups.remove(supervisor)).start()
        state = removal.waiting_or_ended(seconds=3)
        behind_change = removal.pid is not None and _blocked_by(removal.pid, change.pid)
        gates[change.thread].go.set()
        change.join()
        removal.join()
    assert (state, behind_change) == ("waiting", True), (state, behind_change)
    assert removal.outcome == "done", repr(removal.error)
    assert change.outcome == "done", (change.outcome, repr(change.error))
    assert _notes(ticket, body) == 1
    assert not User.objects.get(pk=person.pk).groups.filter(name="supervisor").exists()


# Gate units -----------------------------------------------------------------------------------


def test_close_access_outside_a_transaction_is_an_error(make_user):
    """An access gate is held for a transaction: outside one there is nothing to hold it until."""
    person = make_user("nb2_gate_alone", "client_services")
    assert not connection.in_atomic_block
    with pytest.raises(RuntimeError):
        close_access([person.pk])


def test_a_second_hold_does_not_wait_while_an_exclusive_gate_request_is_queued(make_user):
    """A change holds its account (the gate shared). An access change queues for the gate
    exclusively. The same change holds the same account again.

    Intended: the second hold is re-entrant and waits for nothing, even though an exclusive
    request is queued ahead of any new shared request; the transaction commits, and then the
    access change goes through."""
    person = make_user("nb2_reentrant", "client_services")
    queued = None
    _set_lock_timeout("3s")
    try:
        started = None
        with transaction.atomic():
            assert hold_actor(person).pk == person.pk
            queued = Session(_deactivate_by(person, person)).start()
            state = queued.waiting_or_ended(seconds=3)
            behind_us = _blocked_by(queued.pid, _pid())
            started = time.monotonic()
            again = hold_actor(person)
            took = time.monotonic() - started
    finally:
        _set_lock_timeout("0")
        if queued is not None:
            queued.join()
    assert (state, behind_us) == ("waiting", True)
    assert again is not None and again.pk == person.pk
    assert took < 1.0, f"the second hold waited {took:.1f}s"
    assert queued.outcome == "done", repr(queued.error)
    assert not User.objects.get(pk=person.pk).is_active


def _gate_rows(pid):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT classid::int, objid::int, mode, granted FROM pg_locks "
            "WHERE locktype = 'advisory' AND pid = %s ORDER BY objid",
            [pid],
        )
        return cursor.fetchall()


def test_close_access_takes_the_gates_in_id_order(make_user):
    """Another session holds the gate of the account with the lower id. A session closes access
    to both accounts, naming the higher id first.

    Intended: it takes the lower id first (so it waits for that gate without yet holding the
    higher one), and once it has both, advisory locks of the access gate namespace are held
    exclusively for exactly the two account ids."""
    low = make_user("nb2_gate_low", "client_services")
    high = make_user("nb2_gate_high", "client_services")
    assert low.pk < high.pk
    first = Held(lambda: close_access([low.pk]))
    holds_both, release = threading.Event(), threading.Event()

    def both():
        with transaction.atomic():
            close_access([high.pk, low.pk])
            holds_both.set()
            release.wait(timeout=20)

    second = Session(both).start()
    try:
        state = second.waiting_or_ended(seconds=3)
        waiting_rows = _gate_rows(second.pid)
        behind_first = _blocked_by(second.pid, first.session.pid)
    finally:
        assert first.release() == "done"
    assert holds_both.wait(timeout=10), second.error
    try:
        held_rows = _gate_rows(second.pid)
    finally:
        release.set()
        second.join()
    assert (state, behind_first) == ("waiting", True)
    assert waiting_rows == [(ACCESS_GATE, low.pk, "ExclusiveLock", False)], waiting_rows
    assert held_rows == [
        (ACCESS_GATE, low.pk, "ExclusiveLock", True),
        (ACCESS_GATE, high.pk, "ExclusiveLock", True),
    ], held_rows
    assert second.outcome == "done", repr(second.error)
