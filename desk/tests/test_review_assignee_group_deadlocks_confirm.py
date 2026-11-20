"""Confirmation of the verification fixes (candidate 9e33573, earlier 628d014), using synthetic
PostgreSQL transactions. Adopted from the reviewer's confirmation round; findings C1 to C8 were
resolved in the next commit (assignee-group-deadlocks-round-1-confirmation-resolution.md in the
review history). Adapted: C8's "leave group" case asserts the corrected help (leaving a group
does not wait for an import)."""

from threading import Event

import pytest
from django.contrib.auth.models import Group
from django.db import DatabaseError, connection, transaction
from django.urls import reverse

from clients import services as directory
from clients.models import Client, ClientContact, ClientSite
from core.models import AuditEvent
from dataimport import importing

from .test_review_acting_account_until_commit import Gate
from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by, _statement

pytestmark = pytest.mark.django_db(transaction=True)


def _observe_wait(worker, holder):
    state = worker.waiting_or_ended(seconds=5)
    if state == "waiting":
        assert _blocked_by(worker.pid, holder.pid), _statement(worker.pid)
        return state, _statement(worker.pid)
    return state, ""


@pytest.mark.parametrize("first_action", ["create", "rename"])
def test_a_site_form_that_loses_a_code_to_another_site_shows_its_duplicate_message(
    first_action, client, make_user, agent, clinic, clinic_site
):
    manager = make_user("confirm_site_manager", "account_manager")
    client.force_login(manager)
    client.raise_request_exception = False
    if first_action == "create":
        first = Held(
            lambda: directory.create_site(
                actor=agent, client_id=clinic.pk, name="West office", code="West"
            )
        )
        url = reverse("clients:site-edit", args=[clinic.pk, clinic_site.pk])
        data = {
            "name": clinic_site.name,
            "kind": clinic_site.kind,
            "expected_revision": clinic_site.revision,
            "reason": "Office renumbering",
            "is_active": "on",
        }
        attempted_event = "site.updated"
    else:
        first = Held(
            lambda: directory.update_site(
                actor=agent,
                site_id=clinic_site.pk,
                expected_revision=clinic_site.revision,
                reason="Office renumbering",
                code="West",
            )
        )
        url = reverse("clients:site-create", args=[clinic.pk])
        data = {"name": "West annex", "kind": "office"}
        attempted_event = "site.created"
    data["code"] = "WEST"
    page = Session(lambda: client.post(url, data)).start()
    try:
        state, sql = _observe_wait(page, first.session)
    finally:
        assert first.release() == "done", first.session.error
        page.join()
    assert page.outcome == "done", page.error
    assert state == "waiting", sql
    assert ClientSite.objects.filter(client=clinic, code__iexact="WEST").count() == 1
    assert not AuditEvent.objects.filter(actor=manager, action=attempted_event).exists()
    response = page.result
    cause = response.exc_info[1].__cause__ if response.exc_info else None
    assert response.status_code == 200, (
        getattr(cause, "sqlstate", None),
        getattr(getattr(cause, "diag", None), "constraint_name", None),
    )
    form = response.context["form"]
    assert form["code"].value() == data["code"]
    assert "Another site of this organization already uses this code" in str(form.errors)


@pytest.mark.parametrize(
    "field", ["site code", "site phone", "organization name", "organization phone"]
)
def test_directory_edits_during_an_unrelated_import(
    field, agent, make_user, clinic, clinic_site, water
):
    manager = make_user("confirm_editor", "account_manager")
    args = dict(
        actor=agent,
        filename="sites.csv",
        data=b"account_code,site_code,name\nEXWATER,EAST,Water east\n",
    )
    preview = importing.preview_file(importing.SITES, **args)
    assert preview.ok
    first = Held(
        lambda: importing.commit_file(
            importing.SITES, **args, state_token=preview.state_token, reason=""
        )
    )
    operation, attr = field.split()
    if operation == "site":
        model, kwargs, service = clinic_site, {"site_id": clinic_site.pk}, directory.update_site
    else:
        model, kwargs, service = clinic, {"client_id": clinic.pk}, directory.update_client
    new_value = {"code": "WEST", "phone": "555-0187", "name": "Example Regional"}[attr]
    change = Session(
        lambda: service(
            actor=manager,
            expected_revision=model.revision,
            reason="Updated directory",
            **kwargs,
            **{attr: new_value},
        )
    ).start()
    try:
        state, sql = _observe_wait(change, first.session)
    finally:
        assert first.release() == "done", first.session.error
        change.join()
    assert change.outcome == "done", change.error
    model.refresh_from_db()
    assert (getattr(model, attr), model.revision) == (new_value, 2)
    assert ClientSite.objects.get(client=water, code="EAST")
    if attr == "phone":
        assert state == "ended", sql
    else:
        assert state == "waiting" and "pg_advisory_xact_lock" in sql, sql


@pytest.mark.parametrize("action", ["create", "edit"])
def test_site_save_holds_or_refreshes_its_manager_role_across_a_row_wait(
    action, make_user, agent, clinic, clinic_site
):
    """Switch between two permitted roles. Observe whether the save holds or rereads its role."""
    manager = make_user("confirm_site_actor", "client_services")
    old_role = Group.objects.get(name="client_services")
    new_role = Group.objects.get_or_create(name="account_manager")[0]
    switched = Event()
    reads_after_switch = []

    def switch_roles():
        with transaction.atomic():
            manager.groups.add(new_role)
            manager.groups.remove(old_role)
        switched.set()

    def observe_read(execute, sql, params, many, context):
        if switched.is_set() and "accounts_user_groups" in sql.lower():
            reads_after_switch.append(sql)
        return execute(sql, params, many, context)

    def save():
        with connection.execute_wrapper(observe_read):
            if action == "create":
                return directory.create_site(
                    actor=manager, client_id=clinic.pk, name="Annex", code="EXANNEX"
                )
            return directory.update_site(
                actor=manager,
                site_id=clinic_site.pk,
                expected_revision=clinic_site.revision,
                reason="New public phone",
                phone="555-0192",
            )

    def hold_row():
        if action == "create":
            directory.update_client(
                actor=agent,
                client_id=clinic.pk,
                expected_revision=clinic.revision,
                phone="555-0194",
                reason="New phone",
            )
        else:
            ClientSite.objects.select_for_update().get(pk=clinic_site.pk)

    first = Held(hold_row)
    save_session = Session(save).start()
    switch = None
    try:
        assert _observe_wait(save_session, first.session)[0] == "waiting"
        switch = Session(switch_roles).start()
        state, sql = _observe_wait(switch, save_session)
    finally:
        assert first.release() == "done"
        save_session.join()
        if switch is not None:
            switch.join()
    assert save_session.outcome == switch.outcome == "done", (save_session.error, switch.error)
    assert (
        AuditEvent.objects.filter(
            actor=manager, action={"create": "site.created", "edit": "site.updated"}[action]
        ).count()
        == 1
    )
    assert state == "waiting" or (state == "ended" and reads_after_switch), (state, sql)


def _actor_gate_is_free(account):
    """Probe from a different connection, returning immediately and releasing the probe lock."""
    from accounts.acting import ACCESS_GATE, INT4

    def probe():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_xact_lock(%s, %s)", [ACCESS_GATE, account.pk % INT4]
            )
            return cursor.fetchone()[0]

    session = Session(probe).start()
    assert session.join() == "done", session.error
    return session.result


def _change_display_name(account, value):
    """A compatible, non-key field edit on a different PostgreSQL connection."""
    from accounts.models import User

    def change():
        current = User.objects.get(pk=account.pk)
        current.first_name = value
        current.save(update_fields=["first_name"])

    session = Session(change).start()
    assert session.join() == "done", session.error


@pytest.mark.parametrize(
    "scope", ["outer", "open savepoint", "released savepoint", "savepoint=False"]
)
def test_repeat_actor_hold_keeps_the_first_reading_while_the_gate_remains_held(agent, scope):
    from contextlib import nullcontext

    from accounts.acting import hold_actor

    with transaction.atomic():
        # In the open-savepoint case both calls are inside the same savepoint. In the
        # released cases the first call is inside the nested block and the second outside.
        with transaction.atomic() if scope == "open savepoint" else nullcontext():
            if scope in ("released savepoint", "savepoint=False"):
                with transaction.atomic(savepoint=scope == "released savepoint"):
                    first = hold_actor(agent)
            else:
                first = hold_actor(agent)
            assert not _actor_gate_is_free(agent)
            _change_display_name(agent, "Second reading")
            second = hold_actor(agent)
            assert second.first_name == first.first_name, (
                scope,
                first.first_name,
                second.first_name,
            )
    assert _actor_gate_is_free(agent)


@pytest.mark.parametrize("api", ["atomic", "explicit savepoint", "rollback enclosing savepoint"])
def test_actor_hold_is_reacquired_after_savepoint_rollback(agent, api):
    from accounts.acting import hold_actor

    with transaction.atomic():
        if api == "explicit savepoint":
            point = transaction.savepoint()
            hold_actor(agent)
            transaction.savepoint_rollback(point)
            transaction.savepoint_commit(point)
        else:
            with transaction.atomic():
                if api == "rollback enclosing savepoint":
                    with transaction.atomic():
                        hold_actor(agent)
                else:
                    hold_actor(agent)
                transaction.set_rollback(True)

        assert _actor_gate_is_free(agent)
        _change_display_name(agent, "Current reading")
        reading = hold_actor(agent)
        assert (reading.first_name, _actor_gate_is_free(agent)) == ("Current reading", False)


@pytest.mark.parametrize("rollback", [False, True], ids=["commit", "rollback"])
def test_actor_hold_expires_with_its_outer_transaction_on_the_same_connection(agent, rollback):
    from accounts.acting import hold_actor

    with transaction.atomic():
        first = hold_actor(agent)
        assert not _actor_gate_is_free(agent)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            backend = cursor.fetchone()[0]
        transaction.set_rollback(rollback)

    assert _actor_gate_is_free(agent)
    _change_display_name(agent, "Next transaction")
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            assert cursor.fetchone()[0] == backend
        second = hold_actor(agent)
        assert second.first_name == "Next transaction" and second.first_name != first.first_name
        assert not _actor_gate_is_free(agent)


def test_repeated_ticket_service_does_not_relock_a_later_team_grant(
    make_user, agent, quality, sample_category
):
    """A request and its next note share an outer transaction and the actor's original hold."""
    from desk import services
    from desk.models import EntryKind, RequestEntry
    from teams import services as team_services

    from .test_contract import new_request

    supervisor = make_user("confirmation_team_supervisor", "supervisor")
    created = Gate()
    note = "Example follow-up entered in the same transaction"

    def record():
        with transaction.atomic():
            ticket = new_request(agent, sample_category)
            created.stop()
            services.add_entry(
                actor=agent,
                request=ticket,
                expected_revision=ticket.revision,
                kind=EntryKind.NOTE,
                body=note,
            )
            return ticket.pk

    writer = Session(record).start()
    try:
        assert created.arrived.wait(timeout=5), writer.error
        assert not _actor_gate_is_free(agent)
        member = team_services.add_member(actor=supervisor, team=quality, user=agent)
        edit = Held(
            lambda: team_services.set_lead(actor=supervisor, membership=member, is_lead=True)
        )
        try:
            created.go.set()
            state = writer.waiting_or_ended(seconds=5)
            query = ""
            if state == "waiting":
                assert _blocked_by(writer.pid, edit.session.pid)
                query = _statement(writer.pid)
        finally:
            assert edit.release() == "done", edit.session.error
    finally:
        created.go.set()
        writer.join()
    assert writer.outcome == "done", writer.error
    assert RequestEntry.objects.filter(request_id=writer.result, body=note).count() == 1
    assert state == "ended", query


def test_demo_seed_repair_leaves_the_external_system_available_to_an_earlier_import(
    settings, monkeypatch
):
    """An existing demo can need its starter system restored before the organization phase."""
    import io

    from django.core.management import call_command

    from accounts.models import User
    from links import services as link_services
    from links.models import ExternalRecordLink, ExternalSystem

    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    settings.EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic review only")
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    options = {"stdout": io.StringIO(), "stderr": io.StringIO()}
    call_command("seed_demo", **options)
    system = ExternalSystem.objects.get(key="lis")
    link_services.update_system(
        actor=User.objects.get(username="demo_admin"),
        system_id=system.pk,
        expected_revision=system.revision,
        name="Example Regional LIS",
        reason="Changed demonstration display name",
    )
    identifier = "ORD-TEST-000892"
    args = {
        "actor": User.objects.get(username="demo_account_manager"),
        "filename": "external-ids.csv",
        "data": (
            "account_code,system,record_type,identifier_scheme,identifier,role\n"
            f"EXCLIN,lis,order,,{identifier},related\n"
        ).encode(),
    }
    preview = importing.preview_file(importing.EXTERNAL_IDS, **args)
    assert preview.ok and preview.counts["create"] == 1, [r.errors for r in preview.rows]

    before_system = Gate()
    refused = []

    def probe_system(execute, sql, params, many, context):
        if (
            'FROM "links_externalsystem"' in sql
            and sql.endswith("FOR UPDATE")
            and not before_system.arrived.is_set()
        ):
            before_system.stop()
            try:
                return execute(sql + " NOWAIT", params, many, context)
            except DatabaseError as exc:
                refused.append(getattr(exc.__cause__, "sqlstate", None))
                raise
        return execute(sql, params, many, context)

    def confirm():
        with connection.execute_wrapper(probe_system):
            return importing.commit_file(
                importing.EXTERNAL_IDS, **args, reason="", state_token=preview.state_token
            )

    commit = Session(confirm).start()
    seed = None
    statement = ""
    try:
        assert before_system.arrived.wait(timeout=6), commit.error
        seed = Session(lambda: call_command("seed_demo", **options)).start()
        state = seed.waiting_or_ended(seconds=5)
        assert state in {"waiting", "ended"}, seed.error
        if state == "waiting":
            assert _blocked_by(seed.pid, commit.pid)
            statement = _statement(seed.pid)
    finally:
        before_system.go.set()
        commit.join()
        if seed is not None:
            seed.join()
    assert seed.outcome == "done", seed.error
    system.refresh_from_db()
    assert system.name == "Example LIS"
    saved = ExternalRecordLink.objects.filter(identifier=identifier).count()
    assert (refused, commit.outcome, saved) == ([], "done", 1), (statement, refused, commit.error)


@pytest.mark.parametrize("phase", ["organizations", "links"])
def test_seed_phase_holds_its_manager_before_waiting_for_the_directory(make_user, phase):
    """A manager's full account save queues behind even an otherwise idle seed phase."""

    from accounts.models import User
    from clients.seed import _seed_clients
    from links.seed import seed_records

    manager = make_user("demo_account_manager", "account_manager")
    Client.objects.create(name="Example Independent Office", account_number="EXOFFICE")

    def run_phase():
        with transaction.atomic():
            if phase == "organizations":
                _seed_clients(manager)
            else:
                seed_records(None)

    def save_account():
        person = User.objects.get(pk=manager.pk)
        person.first_name = "Example"
        person.save()

    holder = Held(directory.hold_directory)
    seed = Session(run_phase).start()
    account = None
    try:
        seed_state = seed.waiting_or_ended(seconds=5)
        if seed_state == "waiting":
            assert _blocked_by(seed.pid, holder.session.pid)
        account = Session(save_account).start()
        account_state = account.waiting_or_ended(seconds=5)
        account_statement = ""
        if account_state == "waiting":
            assert _blocked_by(account.pid, seed.pid)
            account_statement = _statement(account.pid)
    finally:
        assert holder.release() == "done", holder.session.error
        seed.join()
        if account is not None:
            account.join()
    assert seed.outcome == account.outcome == "done", (seed.error, account.error)
    manager.refresh_from_db()
    assert manager.first_name == "Example"
    assert (seed_state, account_state) == ("waiting", "waiting")
    assert "pg_advisory_xact_lock(" in account_statement


@pytest.mark.parametrize("other_site", [False, True], ids=["last-choice", "another-choice-remains"])
def test_contact_page_refuses_a_newly_chosen_site_retired_after_opening(
    other_site, client, agent, make_user, clinic, clinic_site
):

    manager = make_user("confirmation_site_manager", "account_manager")
    if other_site:
        ClientSite.objects.create(client=clinic, code="EAST", name="East office")
    client.force_login(agent)
    page = reverse("clients:contact-create", args=[clinic.pk])
    offered = client.get(page).context["form"].fields["sites"].queryset
    assert offered.filter(pk=clinic_site.pk).exists()
    retired = Session(
        lambda: directory.update_site(
            actor=manager,
            site_id=clinic_site.pk,
            expected_revision=clinic_site.revision,
            is_active=False,
            reason="Office closed",
        )
    ).start()
    assert retired.join() == "done", retired.error
    response = client.post(
        page, {"name": "Example Coordinator", "phone": "555-0104", "sites": [clinic_site.pk]}
    )
    contacts = ClientContact.objects.filter(client=clinic, name="Example Coordinator")
    scopes = [list(row.sites.values_list("pk", flat=True)) for row in contacts]
    events = AuditEvent.objects.filter(action="client_contact.added", actor=agent).count()
    assert (response.status_code, scopes, events) == (200, [], 0)
    assert response.context["form"].errors


def test_contact_service_rechecks_a_chosen_site_as_it_was_deactivated(
    agent, make_user, clinic, clinic_site
):

    manager = make_user("confirmation_deactivating_manager", "account_manager")
    retired = Held(
        lambda: directory.update_site(
            actor=manager,
            site_id=clinic_site.pk,
            expected_revision=clinic_site.revision,
            is_active=False,
            reason="Office closed",
        )
    )
    contact = Session(
        lambda: directory.add_contact(
            actor=agent,
            client_id=clinic.pk,
            sites=[clinic_site],
            name="Example Coordinator",
            phone="555-0104",
        )
    ).start()
    try:
        state = contact.waiting_or_ended(seconds=4)
        statement = _statement(contact.pid)
        if state == "waiting":
            assert _blocked_by(contact.pid, retired.session.pid), statement
    finally:
        assert retired.release() == "done", retired.session.error
        contact.join()
    contacts = ClientContact.objects.filter(client=clinic, name="Example Coordinator").count()
    events = AuditEvent.objects.filter(action="client_contact.added", actor=agent).count()
    assert (contact.outcome, contacts, events) == ("refused", 0, 0), (statement, contact.error)
    assert "inactive" in str(contact.error)
    assert (state, "FOR SHARE" in statement) == ("waiting", True), statement


def test_contact_site_lock_leaves_ticket_and_portal_references_free_and_site_edits_waiting(
    agent, make_user, clinic, clinic_site, clinic_contact, clinic_portal, sample_category
):
    from clients.models import PortalAccess

    from .test_contract import new_request

    manager = make_user("confirmation_reference_manager", "account_manager")
    clinic_contact.sites.add(clinic_site)
    # Retain a site already selected. Updating only the contact phone creates no new
    # contact-site foreign key, so any wait for this site is due to the explicit shared hold.
    held = Held(
        lambda: directory.update_contact(
            actor=agent,
            contact_id=clinic_contact.pk,
            expected_revision=clinic_contact.revision,
            sites=[clinic_site],
            phone="555-0188",
            reason="New contact phone",
        )
    )

    def reference_site():
        directory.set_portal_access(
            actor=manager,
            user_id=clinic_portal.pk,
            expected_client_id=clinic.pk,
            sites=[clinic_site],
        )
        return new_request(manager, sample_category, client=clinic, site=clinic_site)

    references = Session(reference_site).start()
    change = None
    try:
        state = references.waiting_or_ended(seconds=4)
        assert state == "ended" and references.outcome == "done", (state, references.error)
        change = Session(
            lambda: directory.update_site(
                actor=manager,
                site_id=clinic_site.pk,
                expected_revision=clinic_site.revision,
                phone="555-0119",
                reason="New office number",
            )
        ).start()
        state = change.waiting_or_ended(seconds=4)
        behind = _blocked_by(change.pid, held.session.pid) if state == "waiting" else False
        statement = _statement(change.pid) if behind else ""
    finally:
        assert held.release() == "done", held.session.error
        references.join()
        if change is not None:
            change.join()
    assert references.result.site_id == clinic_site.pk
    assert list(PortalAccess.objects.get(user=clinic_portal).sites.all()) == [clinic_site]
    assert change.outcome == "done", change.error
    assert (state, behind, "clients_clientsite" in statement) == ("waiting", True, True), statement


def test_sites_file_can_finish_its_second_row_while_a_new_contact_waits_for_its_first(
    agent, make_user, clinic, clinic_site
):

    manager = make_user("confirmation_sites_importer", "account_manager")
    args = dict(
        actor=manager,
        filename="sites.csv",
        data=(
            b"account_code,site_code,name\nEXCLIN,MAIN,Main reception\nEXCLIN,EAST,East reception\n"
        ),
    )
    preview = importing.preview_file(importing.SITES, **args)
    assert preview.ok and preview.counts["update"] == preview.counts["create"] == 1
    before_second = Gate()
    probes = []

    def probe_parent(execute, sql, params, many, context):
        if 'FROM "clients_client"' in sql and sql.endswith("FOR UPDATE"):
            # This is the ordinary organization lock in create_site on row two. Stop
            # before it, and then request its actual mode without introducing a two-way wait.
            before_second.stop()
            try:
                return execute(sql + " NOWAIT", params, many, context)
            except DatabaseError as exc:
                probes.append(getattr(exc.__cause__, "sqlstate", None))
                raise
        return execute(sql, params, many, context)

    def import_sites():
        with connection.execute_wrapper(probe_parent):
            return importing.commit_file(
                importing.SITES, **args, state_token=preview.state_token, reason="Updated offices"
            )

    commit = Session(import_sites).start()
    contact = None
    try:
        assert before_second.arrived.wait(timeout=6), commit.error
        contact = Session(
            lambda: directory.add_contact(
                actor=agent,
                client_id=clinic.pk,
                sites=[clinic_site],
                name="Example Coordinator",
                phone="555-0104",
            )
        ).start()
        state = contact.waiting_or_ended(seconds=4)
        waiting_on_import = state == "waiting" and _blocked_by(contact.pid, commit.pid)
        statement = _statement(contact.pid) if waiting_on_import else ""
    finally:
        before_second.go.set()
        commit.join()
        if contact is not None:
            contact.join()
    assert contact.outcome == "done", contact.error
    assert ClientContact.objects.get(client=clinic, name="Example Coordinator").sites.contains(
        clinic_site
    )
    clinic_site.refresh_from_db()
    branch = ClientSite.objects.filter(client=clinic, code="EAST").exists()
    summaries = AuditEvent.objects.filter(action=importing.SUMMARY_ACTION).count()
    assert (probes, commit.outcome, clinic_site.name, branch, summaries) == (
        [],
        "done",
        "Main reception",
        True,
        1,
    ), (statement, probes, commit.error)


@pytest.mark.parametrize("change", ["leave group", "new group"])
def test_parent_changes_take_turns_with_an_unrelated_import_as_the_upload_page_says(
    change, agent, make_user, clinic, water
):
    parent = Client.objects.create(name="Existing group")
    next_parent = Client.objects.create(name="New group") if change == "new group" else None
    manager = make_user("confirmation_group_editor", "account_manager")
    member = directory.update_client(
        actor=manager,
        client_id=clinic.pk,
        expected_revision=clinic.revision,
        parent=parent,
        reason="Initial grouping",
    )
    args = dict(
        actor=agent,
        filename="sites.csv",
        data=b"account_code,site_code,name\nEXWATER,EAST,East office\n",
    )
    preview = importing.preview_file(importing.SITES, **args)
    assert preview.ok
    first = Held(
        lambda: importing.commit_file(
            importing.SITES, **args, state_token=preview.state_token, reason=""
        )
    )
    edit = Session(
        lambda: directory.update_client(
            actor=manager,
            client_id=clinic.pk,
            expected_revision=member.revision,
            parent=next_parent,
            reason="Changed group",
        )
    ).start()
    try:
        state, sql = _observe_wait(edit, first.session)
    finally:
        assert first.release() == "done"
        edit.join()
    assert edit.outcome == "done", edit.error
    clinic.refresh_from_db()
    assert clinic.parent == next_parent
    assert ClientSite.objects.filter(client=water, code="EAST").exists()
    if change == "leave group":
        # Adapted (C8): the upload help and the roadmap now say that taking an organization out
        # of its group does not wait for an import; placing it under a parent does.
        assert state == "ended", sql
        return
    assert (state, "pg_advisory_xact_lock" in sql) == ("waiting", True), sql


@pytest.mark.parametrize("change", ["parent", "code case"])
def test_external_identifier_confirmation_keeps_an_unchanged_organization_binding(
    change, make_user, agent, clinic
):
    from links.models import ExternalSystem, LinkAttachment

    ExternalSystem.objects.get_or_create(
        key="lis", defaults={"name": "Laboratory system", "kind": "lis"}
    )
    manager = make_user("confirmation_external_importer", "account_manager")
    args = dict(
        actor=manager,
        filename="orders.csv",
        data=b"account_code,system,record_type,identifier_scheme,identifier,role\nEXCLIN,lis,order,,ORD-TEST-000897,related\n",
    )
    spec = importing.EXTERNAL_IDS
    preview = importing.preview_file(spec, **args)
    assert preview.ok and preview.counts["create"] == 1
    update = (
        {"parent": Client.objects.create(name="Regional group")}
        if change == "parent"
        else {"account_number": "exclin"}
    )
    first = Held(
        lambda: directory.update_client(
            actor=agent,
            client_id=clinic.pk,
            expected_revision=clinic.revision,
            reason="Current directory entry",
            **update,
        )
    )
    confirmation = Session(
        lambda: importing.commit_file(spec, **args, state_token=preview.state_token, reason="")
    ).start()
    try:
        state, sql = _observe_wait(confirmation, first.session)
    finally:
        assert first.release() == "done"
        confirmation.join()
    assert state == "waiting" and "pg_advisory_xact_lock" in sql, sql
    assert confirmation.outcome == "done", confirmation.error
    assert LinkAttachment.objects.get(link__identifier="ORD-TEST-000897").client_id == clinic.pk
    assert AuditEvent.objects.filter(action=importing.SUMMARY_ACTION, actor=manager).count() == 1
