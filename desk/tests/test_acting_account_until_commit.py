"""The acting account held from the start of a change until it commits (accounts.acting), and an
external identifier import that locks in the link services' order. All records are synthetic.

These are the acceptance cases of the roadmap item "Review follow-up: the acting account until
commit, and the import's link order", from the third verification of the assignment lock order
(V3-02 to V3-04). The V3-03 and V3-04 tests are the reviewer's, unchanged.

The V3-02 tests keep the reviewer's setup: a change whose commit waits (for an organization, a
closed ticket, a form field, or its own account) while the account it acts for loses access.
Their outcome follows the design. A change holds its account's rows from before its first lock,
so an account change made while the change waits now waits for the change, and the change is
saved first, by an account that still had its access. An account change already under way when
a change starts makes the change wait at its first step, and the change is then refused.
"""

from types import SimpleNamespace

import pytest
from django.contrib.auth.models import Group

from accounts.models import User
from dataimport.importing import EXTERNAL_IDS, commit_file, preview_file
from desk import services
from desk.models import EntryKind, RequestEntry, ServiceRequest, Status
from links import services as link_services
from links.models import ExternalRecordLink, ExternalSystem, LinkAttachment
from request_forms import definition
from request_forms import services as form_services
from request_forms.models import FieldKind, FieldValue, RequestTemplate

from .test_contract import new_request, set_status
from .test_review_assignment_lock_order_verify3 import (
    Held,
    Session,
    _blocked_by,
    _created_events,
    _deactivate,
    _deactivate_by,
    _fresh,
    _race,
    _remove_role,
    _statement,
    paused,
    run_here,
)

pytestmark = pytest.mark.django_db(transaction=True)

SUBJECT = "Acting account creation"


@pytest.fixture(autouse=True)
def locmem_mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


@pytest.fixture
def clerk(make_user):
    return make_user("acting_clerk", "client_services")


@pytest.fixture
def colleague(make_user):
    return make_user("acting_colleague", "client_services")


@pytest.fixture
def setup_admin(make_user):
    return make_user("acting_admin", "admin")


@pytest.fixture
def lis(db):
    return ExternalSystem.objects.get_or_create(
        key="lis", defaults={"name": "Laboratory information system", "kind": "lis"}
    )[0]


@pytest.fixture
def two_site_portal(clinic_portal, clinic):
    """The clinic's portal account, granted two of its sites (A and B)."""
    from clients.models import ClientSite, PortalAccess

    site_a = ClientSite.objects.create(client=clinic, code="SA", name="Site A")
    site_b = ClientSite.objects.create(client=clinic, code="SB", name="Site B")
    access = PortalAccess.objects.create(user=clinic_portal, client=clinic)
    access.sites.set([site_a, site_b])
    return SimpleNamespace(user=clinic_portal, access=access, a=site_a, b=site_b)


@pytest.fixture
def form_ticket(agent, category, quality):
    """A ticket raised with a staff form of two fields, "note" (holding "first") and "extra"."""
    template = RequestTemplate.objects.create(
        key="acting-correction", name="Correction form", category=category, offered_in_portal=False
    )
    template.fields.create(key="note", label="Note", kind=FieldKind.SHORT_TEXT, position=10)
    extra = template.fields.create(
        key="extra", label="Extra", kind=FieldKind.SHORT_TEXT, position=20
    )
    raised = form_services.create_request_with_form(
        actor=agent,
        template=template,
        expected_definition=definition.token(template),
        values={"note": "first"},
        first_entry_kind=EntryKind.NOTE,
        channel="phone",
        team=quality,
    )
    row = FieldValue.objects.get(filled_form__request=raised, superseded_at__isnull=True)
    return SimpleNamespace(ticket=raised, template=template, row=row, extra=extra)


def _holds_account_rows(statement):
    """Whether `statement` is one of accounts.acting's holds (it waited before its first lock)."""
    return "FOR KEY SHARE" in statement and statement.lstrip().upper().startswith("SELECT")


# V3-02. A creation's commit waits while its account loses access --------------------------------

ACCOUNT_CHANGES = [
    "staff-deactivated",
    "staff-role-removed",
    "portal-deactivated",
    "portal-site-dropped",
]


def _creation_and_account_change(case, clerk, portal, category, clinic):
    if case.startswith("staff"):

        def create():
            return new_request(clerk, category, client=clinic, subject=SUBJECT)

        change = {
            "staff-deactivated": _deactivate(clerk),
            "staff-role-removed": _remove_role(clerk, "client_services"),
        }[case]
    else:

        def create():
            return services.create_request(
                actor=portal.user,
                category=category,
                subject=SUBJECT,
                first_entry_kind=EntryKind.REPLY,
                first_entry_body="Where is the report for order ORD-TEST-000301?",
                channel="portal",
                site=portal.a,
            )

        change = {
            "portal-deactivated": _deactivate(portal.user),
            "portal-site-dropped": lambda: portal.access.sites.remove(portal.a),
        }[case]
    return create, change


def _access_taken(case, clerk, portal):
    if case == "staff-deactivated":
        return not User.objects.get(pk=clerk.pk).is_active
    if case == "staff-role-removed":
        return not User.objects.get(pk=clerk.pk).groups.filter(name="client_services").exists()
    if case == "portal-deactivated":
        return not User.objects.get(pk=portal.user.pk).is_active
    return not portal.access.sites.filter(pk=portal.a.pk).exists()


@pytest.mark.parametrize("case", ACCOUNT_CHANGES)
def test_an_account_change_waits_for_a_creation_whose_commit_waits_for_the_organization(
    case, clerk, colleague, two_site_portal, clinic, category, lis
):
    """A ticket for an organization is written while a colleague adds a link to that
    organization: add_link holds the organization FOR UPDATE and waits for the external system,
    which another session holds. The creation's COMMIT waits, because the new row's foreign key
    to the organization is checked there. Meanwhile the clerk is deactivated or loses their only
    staff role, or the portal account is deactivated or loses its grant of the chosen site.

    The creation holds its account's rows from before its first lock (accounts.acting), so the
    account change waits for the creation. The ticket is saved by an account that still had its
    access when it committed, and the account change takes effect after it."""
    create, change = _creation_and_account_change(case, clerk, two_site_portal, category, clinic)
    created = _created_events()
    holder = Held(lambda: ExternalSystem.objects.select_for_update().get(pk=lis.pk))
    org_link = Session(
        lambda: link_services.add_link(
            actor=colleague,
            client_id=clinic.pk,
            system=lis,
            record_type="order",
            identifier="ORD-TEST-000302",
        )
    ).start()
    try:
        org_link_state = org_link.waiting_or_ended()
        creation = Session(create).start()
        creation_state = creation.waiting_or_ended()
        behind_org_link = _blocked_by(creation.pid, org_link.pid)
        statement = _statement(creation.pid)
        account_change = Session(change).start()
        account_state = account_change.waiting_or_ended()
        behind_creation = _blocked_by(account_change.pid, creation.pid)
    finally:
        assert holder.release() == "done"
    org_link.join()
    creation.join()
    account_change.join()
    assert org_link_state == "waiting", org_link_state  # for the system
    assert org_link.outcome == "done", repr(org_link.error)
    # The reviewer's race: the creation waits in its COMMIT, behind the organization's lock.
    assert (creation_state, behind_org_link, statement) == ("waiting", True, "COMMIT")
    # The account change waits for the creation, which then commits first.
    assert (account_state, behind_creation) == ("waiting", True)
    assert creation.outcome == "done", repr(creation.error)
    assert account_change.outcome == "done", repr(account_change.error)
    assert ServiceRequest.objects.filter(subject=SUBJECT).count() == 1
    assert _created_events() == created + 1
    assert _access_taken(case, clerk, two_site_portal)


@pytest.mark.parametrize("case", ACCOUNT_CHANGES)
def test_a_creation_that_starts_while_its_account_loses_access_is_refused(
    case, clerk, two_site_portal, clinic, category
):
    """The account change is under way (a deactivation's save holds the account FOR UPDATE; a
    removal of the role or of the site's grant has deleted its row) and not yet committed. A
    creation started meanwhile waits for it before its first lock, reads the account as the
    change left it, and is refused: no ticket and no creation event."""
    create, change = _creation_and_account_change(case, clerk, two_site_portal, category, clinic)
    created = _created_events()
    state, statement, session = _race(change, create, lambda: None)
    assert state == "waiting", state
    assert _holds_account_rows(statement), statement
    assert session.outcome in ("denied", "refused", "not found"), (
        session.outcome,
        repr(session.error),
    )
    assert not ServiceRequest.objects.filter(subject=SUBJECT).exists()
    assert _created_events() == created
    assert _access_taken(case, clerk, two_site_portal)


def test_a_role_removal_waits_for_a_follow_up_whose_commit_waits_for_the_closed_ticket(
    clerk, agent, category
):
    """A clerk logs a follow-up of a closed ticket while a change to that ticket holds its lock.
    The follow-up's COMMIT waits, because its foreign key to the closed ticket is checked there.
    The clerk loses their only staff role meanwhile. The removal waits for the follow-up, which
    is saved first; the role is gone afterwards."""
    closed = set_status(new_request(agent, category, subject="Closed before"), Status.CLOSED)
    created = _created_events()
    holder = Held(lambda: ServiceRequest.objects.select_for_update().get(pk=closed.pk))
    creation = Session(
        lambda: new_request(clerk, category, subject=SUBJECT, follow_up_of=_fresh(closed))
    ).start()
    try:
        state = creation.waiting_or_ended()
        statement = _statement(creation.pid)
        removal = Session(_remove_role(clerk, "client_services")).start()
        removal_state = removal.waiting_or_ended()
        behind_creation = _blocked_by(removal.pid, creation.pid)
    finally:
        assert holder.release() == "done"
    creation.join()
    removal.join()
    assert (state, statement) == ("waiting", "COMMIT")  # its deferred foreign key checks
    assert (removal_state, behind_creation) == ("waiting", True)
    assert (creation.outcome, removal.outcome) == ("done", "done"), repr(creation.error)
    assert ServiceRequest.objects.filter(subject=SUBJECT, follow_up_of=closed).count() == 1
    assert _created_events() == created + 1
    assert not User.objects.get(pk=clerk.pk).groups.filter(name="client_services").exists()


@pytest.mark.parametrize("change", ["link", "correction", "creation", "note"])
def test_a_change_that_starts_during_its_actors_deactivation_is_refused(
    change, make_user, join, agent, category, sample_category, accessioning, lis, form_ticket
):
    """An owner deactivates a technician in account administration. The save holds the account
    and waits to return the technician's open tickets to their queue: one of them is busy.
    Meanwhile the technician, still signed in, adds a link to another ticket, corrects a form
    value on it, logs a new ticket or adds a note. The change waits for the deactivation before
    its first lock, behind the deactivation (not in its COMMIT, as before), then reads the
    account as inactive and is refused. Nothing is saved."""
    owner = make_user("acting_owner", "admin")
    person = make_user("acting_person", "technical")
    join(person, accessioning)
    busy = new_request(agent, sample_category, subject="Busy", team=accessioning, assignee=person)
    other = form_ticket.ticket
    token = definition.token(form_ticket.template)
    entries = RequestEntry.objects.filter(request=other).count()

    def work():
        if change == "link":
            return link_services.add_link(
                actor=person,
                request_id=other.pk,
                system=lis,
                record_type="order",
                identifier="ORD-TEST-000331",
            )
        if change == "correction":
            return form_services.correct_values(
                actor=person,
                request=other,
                expected_definition=token,
                values={"note": "second"},
                reason="Typo",
                expected={"note": form_ticket.row.pk},
            )
        if change == "creation":
            return new_request(person, category, subject=SUBJECT)
        seen = _fresh(other)
        return services.add_entry(
            actor=person,
            request=seen,
            kind=EntryKind.NOTE,
            body="Order ORD-TEST-000332 checked.",
            expected_revision=seen.revision,
        )

    holder = Held(lambda: ServiceRequest.objects.select_for_update().get(pk=busy.pk))
    deactivation = Session(_deactivate_by(person, owner)).start()
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
    assert (state, behind_deactivation) == ("waiting", True)
    assert _holds_account_rows(statement), statement
    assert session.outcome in ("denied", "not found"), (session.outcome, repr(session.error))
    assert not LinkAttachment.objects.filter(request=other).exists()
    assert FieldValue.objects.get(pk=form_ticket.row.pk).superseded_at is None
    assert not ServiceRequest.objects.filter(subject=SUBJECT).exists()
    assert RequestEntry.objects.filter(request=other).count() == entries
    assert _fresh(busy).assignee_id is None  # the deactivation returned it to the queue


def test_a_role_removal_waits_for_a_correction_whose_commit_waits_for_the_field(
    clerk, setup_admin, form_ticket
):
    """An administrator reorders the form's fields while a clerk corrects "note" on a ticket:
    move_field holds the fields FOR UPDATE until it commits. The correction's COMMIT waits,
    because the new value row's foreign key to the field is checked there. The clerk loses their
    only staff role meanwhile. The removal waits for the correction, which is saved first."""
    token = definition.token(form_ticket.template)
    revision = _fresh(form_ticket.ticket).revision
    holder = Held(
        lambda: form_services.move_field(
            actor=setup_admin, field_id=form_ticket.extra.pk, direction="up"
        )
    )
    correction = Session(
        lambda: form_services.correct_values(
            actor=clerk,
            request=form_ticket.ticket,
            expected_definition=token,
            values={"note": "second"},
            reason="Typo",
            expected={"note": form_ticket.row.pk},
        )
    ).start()
    try:
        state = correction.waiting_or_ended()
        statement = _statement(correction.pid)
        removal = Session(_remove_role(clerk, "client_services")).start()
        removal_state = removal.waiting_or_ended()
        behind_correction = _blocked_by(removal.pid, correction.pid)
    finally:
        assert holder.release() == "done"
    correction.join()
    removal.join()
    assert (state, statement) == ("waiting", "COMMIT")  # for the field, held by the reordering
    assert (removal_state, behind_correction) == ("waiting", True)
    assert (correction.outcome, removal.outcome) == ("done", "done"), repr(correction.error)
    assert FieldValue.objects.get(pk=form_ticket.row.pk).superseded_at is not None
    assert _fresh(form_ticket.ticket).revision == revision + 1
    assert not User.objects.get(pk=clerk.pk).groups.filter(name="client_services").exists()


# V3-03. Deactivating a person while they change one of their own tickets ------------------------


@pytest.mark.parametrize("change", ["hand-over", "note"])
def test_deactivating_a_person_and_their_own_change_to_their_ticket_never_deadlock(
    change, make_user, join, agent, sample_category, accessioning
):
    """A technician of Accessioning holds a ticket and hands it to a colleague (an assignment),
    or adds a note to it, while an owner deactivates the technician in account administration.
    The change has locked the ticket when the deactivation starts. The deactivation updates the
    account row with a full save (every column, the unique username and email among them, which
    PostgreSQL treats as a key update, taking the row as FOR UPDATE does), then returns the
    technician's open tickets to their queues and waits for this one (desk.signals,
    release_assignments). The change then commits; its deferred foreign key checks (the timeline
    entry's author, the audit event's actor) take FOR KEY SHARE on the technician's account,
    which the deactivation holds. Neither may fail with a deadlock (40P01): "When an assignment
    and the end of that person's work meet ... Neither fails with a deadlock"
    (docs/architecture.md); desk.signals "the order an assignment takes them in". The change and
    the deactivation both go through, in some order, and the account ends inactive."""
    owner = make_user("verify3_owner", "admin")
    person = make_user("verify3_person", "technical")
    join(person, accessioning)
    colleague = make_user("verify3_colleague_tech", "technical")
    join(colleague, accessioning)
    ticket = new_request(
        agent, sample_category, subject="Recollection", team=accessioning, assignee=person
    )
    sessions, seen = {}, {}

    def during():
        sessions["deactivation"] = Session(_deactivate_by(person, owner)).start()
        seen["deactivation"] = sessions["deactivation"].waiting_or_ended()

    def own_change():
        fresh = _fresh(ticket)
        if change == "hand-over":
            services.assign(
                actor=person,
                request=fresh,
                expected_revision=fresh.revision,
                team=accessioning,
                assignee=colleague,
                reason="Handing over",
            )
        else:
            services.add_entry(
                actor=person,
                request=fresh,
                kind=EntryKind.NOTE,
                body="Recollection booked for the next round.",
                expected_revision=fresh.revision,
            )

    # The person's change is paused once it holds the ticket, until the deactivation waits.
    with paused(services, "_locked_request", during, before=False):
        outcome = {"own change": run_here(own_change)}
    outcome["deactivation"] = sessions["deactivation"].join()
    assert seen["deactivation"] == "waiting", seen  # for the ticket
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}"
    assert outcome == {"own change": "done", "deactivation": "done"}, outcome
    assert not User.objects.get(pk=person.pk).is_active
    assert _fresh(ticket).assignee_id in (None, colleague.pk)


# V3-04. An external identifier import and an organization's link addition ----------------------

HEADER = "account_code,system,record_type,identifier_scheme,identifier,role,display_label,url\r\n"
FILE = (
    HEADER
    + "EXCLIN,lis,order,,ORD-TEST-000321,related,,\r\n"
    + "EXWATER,lis,order,,ORD-TEST-000322,related,,\r\n"
)


def test_an_external_identifier_import_and_an_organization_link_addition_never_deadlock(
    make_user, colleague, clinic, water, lis
):
    """A client manager previews (or imports) an external identifier file with rows for two
    organizations, Example Clinic then Example Water Authority, both in the LIS. The file is
    written in one transaction, each row through links.services.add_link, which locks the
    organization, then the system, then the link; the second row's organization is therefore
    locked after the system. A colleague adds an LIS link on Example Water Authority's page at
    the same moment: add_link locks that organization, then waits for the system. The file is
    paused after its first row until the colleague waits. No one ends with a deadlock (40P01):
    a link change "locks the ticket first, then the link's system, then the link"
    (docs/architecture.md), the organization in the ticket's place, and the paths that lock the
    same rows "never fail with a deadlock" (docs/architecture.md, "Neither fails with a
    deadlock"). Both go through."""
    manager = make_user("verify3_manager", "account_manager")
    text = FILE
    sessions, seen, result = {}, {}, {}

    def during():
        sessions["page"] = Session(
            lambda: link_services.add_link(
                actor=colleague,
                client_id=water.pk,
                system=lis,
                record_type="order",
                identifier="ORD-TEST-000323",
            )
        ).start()
        seen["page"] = sessions["page"].waiting_or_ended()

    def preview():
        result["preview"] = preview_file(
            EXTERNAL_IDS, actor=manager, data=text.encode(), filename="ids.csv"
        )

    with paused(link_services, "add_link", during, before=False):
        outcome = {"file": run_here(preview)}
    outcome["page"] = sessions["page"].join()
    assert seen["page"] == "waiting", seen  # for the system, held by the file's first row
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}"
    rows = [row.errors for row in result["preview"].rows] if "preview" in result else None
    assert outcome == {"file": "done", "page": "done"}, (outcome, rows)
    assert rows == [[], []], rows
    assert ExternalRecordLink.objects.filter(identifier="ORD-TEST-000323").exists()


@pytest.mark.parametrize("page", ["second organization", "first organization", "ticket"])
def test_an_external_identifier_import_and_a_link_addition_never_deadlock_on_import(
    page, make_user, colleague, agent, category, clinic, water, lis
):
    """The same race with the import itself (commit_file), and with the page's link added to
    the file's second organization, its first, or a ticket in the same system (a ticket's link
    locks the ticket, then the system). The file locks its organizations and then its systems
    before its first row (dataimport.importing.hold_external_ids), so the page waits for the
    file, or the file for the page, and both are saved."""
    manager = make_user("acting_manager", "account_manager")
    ticket = new_request(agent, category, client=clinic)
    token = preview_file(
        EXTERNAL_IDS, actor=manager, data=FILE.encode(), filename="ids.csv"
    ).state_token
    target = {
        "second organization": {"client_id": water.pk},
        "first organization": {"client_id": clinic.pk},
        "ticket": {"request_id": ticket.pk},
    }[page]
    sessions, seen, result = {}, {}, {}

    def during():
        sessions["page"] = Session(
            lambda: link_services.add_link(
                actor=colleague,
                system=lis,
                record_type="order",
                identifier="ORD-TEST-000324",
                **target,
            )
        ).start()
        seen["page"] = sessions["page"].waiting_or_ended()

    def commit():
        result["import"] = commit_file(
            EXTERNAL_IDS,
            actor=manager,
            data=FILE.encode(),
            filename="ids.csv",
            reason="",
            state_token=token,
        )

    with paused(link_services, "add_link", during, before=False):
        outcome = {"file": run_here(commit)}
    outcome["page"] = sessions["page"].join()
    assert seen["page"] == "waiting", seen
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}"
    assert outcome == {"file": "done", "page": "done"}, outcome
    assert set(
        ExternalRecordLink.objects.filter(identifier__startswith="ORD-TEST-00032").values_list(
            "identifier", flat=True
        )
    ) == {"ORD-TEST-000321", "ORD-TEST-000322", "ORD-TEST-000324"}


# Every ticket service holds its actor -----------------------------------------------------------


def _holding_services():
    from attachments import services as attachment_services
    from nonconformance import services as nc_services

    return {
        services: [
            "create_request",
            "add_entry",
            "assign",
            "take",
            "change_status",
            "update_properties",
            "set_complaint",
            "decide_complaint",
            "update_complaint",
            "review_complaint",
            "withdraw_reply",
            "bulk_assign",
            "bulk_status",
            "bulk_close",
        ],
        attachment_services: [
            "add_entry_with_files",
            "create_request_with_files",
            "withdraw_attachment",
        ],
        link_services: [
            "add_link",
            "remove_link",
            "set_shown_to_client",
            "update_link",
            "reveal_person_key",
            "search_person_key",
        ],
        form_services: ["create_request_with_form", "correct_values", "reveal_value"],
        nc_services: [
            "raise_nonconformance",
            "open_from_complaint",
            "update_details",
            "set_owner",
            "record_investigation",
            "record_impact",
            "change_status",
            "record_effectiveness",
            "close",
            "reopen",
            "add_action",
            "update_action",
            "finish_action",
            "link_ticket",
            "unlink_ticket",
        ],
    }


def test_every_ticket_service_holds_its_actor_before_anything_else():
    """Each service that changes a ticket, what belongs to one, or a nonconformance runs through
    accounts.acting.acting_change, which holds the acting account before the service's own
    first lock."""
    missing = [
        f"{module.__name__}.{name}"
        for module, names in _holding_services().items()
        for name in names
        if not getattr(getattr(module, name), "holds_actor", False)
    ]
    assert missing == []


def test_no_ticket_service_opens_its_own_transaction_without_holding_its_actor():
    """A service added to these modules later is held too: no function taking `actor` is
    decorated with transaction.atomic alone, apart from the setup services listed here (an
    administrator's change to external systems and request forms)."""
    import ast
    import inspect

    setup = {
        "links.services": {"create_system", "update_system"},
        "request_forms.services": {
            "create_template",
            "update_template",
            "clone_template",
            "move_template",
            "create_field",
            "update_field",
            "move_field",
            "create_catalog",
            "update_catalog",
            "create_item",
            "update_item",
            "move_item",
        },
    }
    unheld = []
    for module in _holding_services():
        tree = ast.parse(inspect.getsource(module))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            arguments = {a.arg for a in node.args.args + node.args.kwonlyargs}
            atomic = any(ast.unparse(d) == "transaction.atomic" for d in node.decorator_list)
            if atomic and "actor" in arguments:
                if node.name not in setup.get(module.__name__, set()):
                    unheld.append(f"{module.__name__}.{node.name}")
    assert unheld == []


def test_a_role_granted_meanwhile_does_not_make_a_change_wait(clerk, agent, category):
    """Additions wait for nothing: a role given to the acting account while its change holds the
    account is saved at once."""
    ticket = new_request(agent, category)
    reviewer = Group.objects.get_or_create(name="account_manager")[0]
    holder = Held(
        lambda: services.add_entry(
            actor=clerk,
            request=_fresh(ticket),
            kind=EntryKind.NOTE,
            body="Order ORD-TEST-000340 checked.",
            expected_revision=_fresh(ticket).revision,
        )
    )
    grant = Session(lambda: User.objects.get(pk=clerk.pk).groups.add(reviewer)).start()
    try:
        state = grant.waiting_or_ended()
    finally:
        assert holder.release() == "done"
    assert grant.join() == "done"
    assert state == "ended", state
