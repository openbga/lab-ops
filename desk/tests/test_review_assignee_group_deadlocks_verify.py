"""Verification of review round 1's fixes (candidate 628d014, earlier 7342f6f). Synthetic records,
independent PostgreSQL connections and bounded waits. The reset probe stops at the erase
boundary. The seed probe requests the import's second row without waiting.

Adopted: V1 (a second hold in an import locks nothing new), V3 (a site code change takes the
directory lock), V4 (adapted: a rename takes the directory lock, so the file waits for it
there), V5 (a stored code with spaces counts as a code change), V6 (a duplicate met at the save
is a form error), V8 and V9 (the seed's phases take the directory lock first), and the
reviewer's checks of the fixes (the reset, the account page, a new root organization, the
guard's positive case). Left in the original: V2 (the import help now promises a refusal only
for a code no longer in use), V7's bypassing writes (documented as not detected) and the
demonstration reset's known exception.
"""

import io

import pytest
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.urls import reverse

from accounts.models import User
from clients import services as directory
from clients.models import Client, ClientContact, ClientSite
from core.models import AuditEvent
from dataimport import importing
from links.models import ExternalRecordLink

from .test_review_acting_account_until_commit import Gate, gated
from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by, _statement

pytestmark = pytest.mark.django_db(transaction=True)


def observe(worker, holder):
    state = worker.waiting_or_ended(seconds=5)
    if state == "waiting":
        assert _blocked_by(worker.pid, holder.pid), _statement(worker.pid)
        return state, _statement(worker.pid)
    return state, ""


@pytest.fixture
def demo_options(settings, monkeypatch):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    settings.EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic review only")
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    return {"stdout": io.StringIO(), "stderr": io.StringIO()}


def test_existing_demo_seed_waits_before_locking_the_organizations_of_its_missing_links(
    demo_options, monkeypatch
):
    """An existing demonstration can still need both of the example organization links."""
    from links import seed as link_seed

    # Make a real demo with just the optional organization links still outstanding.
    with monkeypatch.context() as setup:
        setup.setattr(link_seed, "seed_records", lambda ctx: None)
        call_command("seed_demo", **demo_options)
    manager = User.objects.get(username="demo_account_manager")
    args = {
        "actor": manager,
        "filename": "links.csv",
        "data": (
            b"account_code,system,record_type,identifier_scheme,identifier,role\n"
            b"EXCLIN,lis,order,,ORD-TEST-000861,related\n"
            b"EXWATER,lis,order,,ORD-TEST-000862,related\n"
        ),
    }
    preview = importing.preview_file(importing.EXTERNAL_IDS, **args)
    assert preview.ok and preview.counts["create"] == 2, [r.errors for r in preview.rows]
    first = Gate()
    locked = []
    probe_errors = []

    def check_second_lock(execute, sql, params, many, context):
        if 'FROM "clients_client"' not in sql or not sql.endswith("FOR UPDATE") or len(locked) >= 2:
            return execute(sql, params, many, context)
        locked.append(params[0])
        if len(locked) == 1:
            result = execute(sql, params, many, context)
            first.stop()
            return result
        try:
            return execute(sql + " NOWAIT", params, many, context)
        except DatabaseError as exc:
            probe_errors.append(
                getattr(exc, "sqlstate", None) or getattr(exc.__cause__, "sqlstate", None)
            )
            raise

    def confirm():
        with connection.execute_wrapper(check_second_lock):
            return importing.commit_file(
                importing.EXTERNAL_IDS, **args, reason="", state_token=preview.state_token
            )

    commit = Session(confirm).start()
    seed = None
    try:
        assert first.arrived.wait(timeout=8), commit.error
        seed = Session(lambda: call_command("seed_demo", **demo_options)).start()
        state, statement = observe(seed, commit)
    finally:
        first.go.set()
        commit.join()
        if seed is not None:
            seed.join()
    assert seed.outcome == "done", seed.error
    expected_ids = [Client.objects.get(account_number=code).pk for code in ("EXCLIN", "EXWATER")]
    assert locked == expected_ids
    count = ExternalRecordLink.objects.filter(
        identifier__in=["ORD-TEST-000861", "ORD-TEST-000862"]
    ).count()
    assert not probe_errors, (probe_errors, commit.outcome, count)
    assert (state, "pg_advisory_xact_lock" in statement, commit.outcome, count) == (
        "waiting",
        True,
        "done",
        2,
    ), (statement, commit.error, count)


def test_demo_organization_seed_rechecks_empty_directory_after_an_earlier_import(agent, make_user):
    """The seed's create-once decision must agree with the import it waits for."""
    from clients.seed import _seed_clients
    from core.management.commands.seed_demo import DEMO_LAB_NAME
    from core.models import Lab

    Lab.objects.create(pk=1, name=DEMO_LAB_NAME)
    manager = make_user("demo_account_manager", "account_manager")
    args = dict(
        actor=agent, data=b"account_code,name\nEXFIRST,Example First\n", filename="first.csv"
    )
    preview = importing.preview_file(importing.ORGANIZATIONS, **args)
    assert preview.ok
    first = Held(
        lambda: importing.commit_file(
            importing.ORGANIZATIONS, **args, reason="", state_token=preview.state_token
        )
    )

    def seed_clients():
        with transaction.atomic():
            return list(_seed_clients(manager))

    second = Session(seed_clients).start()
    try:
        state, statement = observe(second, first.session)
    finally:
        assert first.release() == "done"
        second.join()
    assert second.outcome == "done", second.error
    assert state == "waiting" and "pg_advisory_xact_lock" in statement, statement
    assert (
        second.result,
        list(Client.objects.order_by("pk").values_list("account_number", flat=True)),
    ) == ([], ["EXFIRST"])


def test_reset_takes_directory_before_table_locks(demo_options, monkeypatch):
    """Run the reset's real seal, migration and lock checks. Stop before erase."""
    from core import demo
    from core.management.commands.reset_demo import Command
    from core.models import Lab

    class ReachedErase(Exception):
        pass

    def stop_before_erase(_command):
        raise ReachedErase

    def check_reset():
        with pytest.raises(ReachedErase):
            call_command("reset_demo", interactive=False, **demo_options)

    call_command("seed_demo", **demo_options)
    assert demo.is_sealed(Lab.objects.get(pk=1))
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    monkeypatch.setattr(Command, "_erase", stop_before_erase)
    manager = User.objects.get(username="demo_account_manager")
    # A child creation uses the same advisory key on both revisions and holds a write table lock.
    first = Held(
        lambda: directory.create_client(
            actor=manager,
            name="Example Outpost",
            parent=Client.objects.get(account_number="EXCLIN"),
        )
    )
    reset = Session(check_reset).start()
    try:
        state, statement = observe(reset, first.session)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_locks WHERE pid = %s AND granted "
                "AND mode = 'ShareRowExclusiveLock'",
                [reset.pid],
            )
            already_held = cursor.fetchone()[0]
    finally:
        assert first.release() == "done"
        reset.join()
    assert reset.outcome == "done", reset.error
    assert Client.objects.filter(name="Example Outpost").exists()
    assert (state, "pg_advisory_xact_lock" in statement, already_held) == ("waiting", True, 0), (
        statement,
        already_held,
    )


@pytest.mark.parametrize("field", ["unchanged parent"])
def test_stale_parent_or_code_write_without_a_revision_is_refused(agent, clinic, field):
    """Exercise the unversioned writer covered in the resolution's R1 claim."""
    group = Client.objects.create(name="Example Original Group")
    other = Client.objects.create(name="Example Updated Group")
    third = Client.objects.create(name="Example Third Group")
    Client.objects.filter(pk=clinic.pk).update(parent=group)
    if field == "new code":
        maintenance, submitted = {"account_number": "EXCHANGED"}, {"account_number": "EXTHIRD"}
    else:
        maintenance = {"parent": other}
        submitted = {
            "parent": {"new parent": third, "clear parent": None, "unchanged parent": group}[field]
        }
    first = Held(lambda: Client.objects.filter(pk=clinic.pk).update(**maintenance))
    edit = Session(
        lambda: directory.update_client(
            actor=agent,
            client_id=clinic.pk,
            expected_revision=clinic.revision,
            reason="Reviewed contact information",
            phone="555-0181",
            **submitted,
        )
    ).start()
    try:
        state, statement = observe(edit, first.session)
    finally:
        assert first.release() == "done"
        edit.join()
    assert state == "waiting" and '"clients_client"' in statement, statement
    clinic.refresh_from_db()
    events = AuditEvent.objects.filter(action="client.updated", object_id=str(clinic.pk)).count()
    assert (edit.outcome, clinic.revision, events) == ("refused", 1, 0), (
        field,
        clinic.parent_id,
        clinic.account_number,
        edit.error,
    )
    assert "changed" in str(edit.error).lower()
    assert clinic.account_number == maintenance.get("account_number", "EXCLIN")
    assert clinic.parent_id == maintenance.get("parent", group).pk


def test_phone_edit_keeps_an_imports_account_code_unambiguous_until_commit(
    agent, clinic, make_user
):
    """A phone-only edit can strip a stored code that the caller omitted."""
    padded_code = " exclin "
    annex = Client.objects.create(name="Example Annex", account_number=padded_code)
    spec = importing.BY_SLUG["sites"]
    args = {
        "actor": agent,
        "data": b"account_code,site_code,name\nEXCLIN,EAST,Example East\n",
        "filename": "sites.csv",
    }
    preview = importing.preview_file(spec, **args)
    assert preview.ok and preview.counts["create"] == 1
    manager = make_user("verify_annex_editor", "account_manager")
    commit = Session(
        lambda: importing.commit_file(spec, **args, state_token=preview.state_token, reason="")
    )
    parsed = Gate()
    edit = Session(
        lambda: directory.update_client(
            actor=manager,
            client_id=annex.pk,
            expected_revision=annex.revision,
            phone="555-0134",
            reason="Corrected the annex phone",
        )
    )
    with gated(importing, "_apply", {commit.thread: parsed}):
        commit.start()
        try:
            assert parsed.arrived.wait(timeout=5), commit.error
            edit.start()
            state, _ = observe(edit, commit)
            bindings_before_write = list(
                Client.objects.filter(account_number__iexact="EXCLIN")
                .order_by("pk")
                .values_list("pk", flat=True)
            )
        finally:
            parsed.go.set()
            commit.join()
            if edit.connected.is_set():
                edit.join()
    assert state in {"waiting", "ended"} and commit.outcome in {"done", "refused"}, commit.error
    assert edit.outcome == "done", edit.error
    annex.refresh_from_db()
    assert (annex.phone, annex.revision) == ("555-0134", 2)
    sites = list(ClientSite.objects.filter(code="EAST").values_list("client_id", flat=True))
    summaries = AuditEvent.objects.filter(action=importing.SUMMARY_ACTION).count()
    if len(bindings_before_write) == 1:
        assert (commit.outcome, sites, summaries) == ("done", [clinic.pk], 1)
    else:
        fresh = importing.preview_file(spec, **args)
        assert not fresh.ok and "matches more than one organization" in str(fresh.rows[0].errors)
        assert (commit.outcome, sites, summaries) == ("refused", [], 0), bindings_before_write


def test_form_returns_a_duplicate_code_error_even_if_the_other_writer_then_releases_it(
    client, agent
):
    """The database can report a duplicate that disappears before validation reads again."""
    client.force_login(agent)
    client.raise_request_exception = False
    url = reverse("clients:client-create")
    code = "EXTRANSIENT"
    duplicate = Gate()

    def observe_insert(execute, sql, params, many, context):
        try:
            return execute(sql, params, many, context)
        except IntegrityError as exc:
            cause = exc.__cause__
            if (
                getattr(cause, "sqlstate", None) == "23505"
                and cause.diag.constraint_name == "client_account_number"
            ):
                duplicate.stop()
            raise

    def submit():
        with connection.execute_wrapper(observe_insert):
            return client.post(url, {"name": "Example Annex", "account_number": code})

    holder = Held(lambda: Client.objects.create(name="Example Clinic", account_number=code))
    edit = Session(submit).start()
    try:
        try:
            state = edit.waiting_or_ended(seconds=4)
            assert state == "waiting" and _blocked_by(edit.pid, holder.session.pid), edit.error
        finally:
            assert holder.release() == "done", holder.session.error
        assert duplicate.arrived.wait(timeout=5), edit.error
        assert Client.objects.filter(account_number=code).update(account_number="EXREPLACED") == 1
    finally:
        duplicate.go.set()
        edit.join()
    assert edit.outcome == "done", edit.error
    assert not Client.objects.filter(name="Example Annex").exists()
    assert not AuditEvent.objects.filter(action="client.created").exists()
    response = edit.result
    assert response.status_code == 200, response.exc_info and str(response.exc_info[1])
    form = response.context["form"]
    assert form["account_number"].value() == code
    assert "already uses this account code" in str(form.errors)


@pytest.mark.parametrize("action", ["create", "update"])
def test_organization_import_does_not_wait_for_a_team_membership_added_after_its_actor_hold(
    action, make_user, quality, clinic
):
    """The import already holds its manager and the directory. A later team grant and an
    unrelated edit of its lead flag should leave the organization's write free to finish."""
    from teams import services as team_services

    manager = make_user("verify_membership_importer", "account_manager")
    supervisor = make_user("verify_membership_supervisor", "supervisor")
    code = "EXNORTH" if action == "create" else clinic.account_number
    name = "Example North Directory"
    spec = importing.ORGANIZATIONS
    args = dict(
        actor=manager,
        data=f"account_code,name\n{code},{name}\n".encode(),
        filename="organizations.csv",
    )
    preview = importing.preview_file(spec, **args)
    assert preview.ok and preview.counts[action] == 1
    commit = Session(
        lambda: importing.commit_file(
            spec, **args, state_token=preview.state_token, reason="Confirmed organization name"
        )
    )
    parsed = Gate()
    with gated(importing, "_apply", {commit.thread: parsed}):
        commit.start()
        try:
            assert parsed.arrived.wait(timeout=5), commit.error
            membership = team_services.add_member(actor=supervisor, team=quality, user=manager)
            lead_edit = Held(
                lambda: team_services.set_lead(
                    actor=supervisor, membership=membership, is_lead=True
                )
            )
            try:
                parsed.go.set()
                state = commit.waiting_or_ended(seconds=5)
                statement = ""
                if state == "waiting":
                    assert _blocked_by(commit.pid, lead_edit.session.pid)
                    statement = _statement(commit.pid)
            finally:
                assert lead_edit.release() == "done", lead_edit.session.error
        finally:
            parsed.go.set()
            commit.join()
    assert commit.outcome == "done", commit.error
    assert commit.result.counts[action] == 1
    assert Client.objects.get(account_number=code).name == name
    assert AuditEvent.objects.filter(action=importing.SUMMARY_ACTION, actor=manager).count() == 1
    membership.refresh_from_db()
    assert membership.is_lead
    assert state == "ended", statement


def test_account_page_edit_waits_at_the_managers_gate_for_their_organization_edit(
    client, make_user, clinic
):
    from .test_review_acting_account_until_commit import _admin_change_data

    owner = make_user("verify_account_owner", "admin", is_staff=True, is_superuser=True)
    manager = make_user("verify_organization_manager", "account_manager")
    client.force_login(owner)
    url = reverse("admin:accounts_user_change", args=[manager.pk])
    data = _admin_change_data(client, manager, first_name="Example")
    edited = Held(
        lambda: directory.update_client(
            actor=manager,
            client_id=clinic.pk,
            expected_revision=clinic.revision,
            phone="555-0134",
            reason="Changed directory phone number",
        )
    )
    account_page = Session(lambda: client.post(url, data)).start()
    try:
        state = account_page.waiting_or_ended(seconds=5)
        behind_manager = _blocked_by(account_page.pid, edited.session.pid)
        statement = _statement(account_page.pid)
    finally:
        assert edited.release() == "done", edited.session.error
        account_page.join()
    assert account_page.outcome == "done", account_page.error
    assert account_page.result.status_code == 302
    clinic.refresh_from_db()
    manager.refresh_from_db()
    assert (clinic.phone, manager.first_name) == ("555-0134", "Example")
    assert AuditEvent.objects.filter(action="client.updated", actor=manager).count() == 1
    assert (state, behind_manager) == ("waiting", True), statement
    assert "pg_advisory_xact_lock(" in statement, statement


def test_contact_file_rechecks_site_code_after_waiting_to_create_the_contact(
    make_user, agent, clinic, clinic_site
):
    manager = make_user("verify_contact_importer", "account_manager")
    email = "contact@example.invalid"
    old_code = clinic_site.code
    options = dict(
        actor=manager,
        data=(
            "account_code,email,name,site_codes\n"
            f"{clinic.account_number},{email},Example Contact,{old_code}\n"
        ).encode(),
        filename="contacts.csv",
    )
    preview = importing.preview_file(importing.CONTACTS, **options)
    assert preview.ok and preview.counts["create"] == 1, [row.errors for row in preview.rows]

    def edit_directory():
        directory.update_client(
            actor=agent,
            client_id=clinic.pk,
            expected_revision=clinic.revision,
            phone="555-0182",
            reason="Updated the directory phone",
        )
        directory.update_site(
            actor=agent,
            site_id=clinic_site.pk,
            expected_revision=clinic_site.revision,
            code="WEST",
            reason="Renamed the main office",
        )

    edit = Held(edit_directory)
    commit = Session(
        lambda: importing.commit_file(
            importing.CONTACTS, **options, state_token=preview.state_token, reason=""
        )
    ).start()
    try:
        state, statement = observe(commit, edit.session)
    finally:
        assert edit.release() == "done", edit.session.error
        commit.join()
    assert state == "waiting", statement
    clinic_site.refresh_from_db()
    assert (clinic_site.code, clinic_site.revision) == ("WEST", 2)
    assert not ClientSite.objects.filter(client=clinic, code=old_code).exists()
    contacts = ClientContact.objects.filter(client=clinic, email=email)
    assert (
        commit.outcome,
        contacts.count(),
        list(contacts.values_list("sites__code", flat=True)),
        AuditEvent.objects.filter(actor=manager, action="client_contact.added").count(),
        AuditEvent.objects.filter(actor=manager, action=importing.SUMMARY_ACTION).count(),
    ) == ("refused", 0, [], 0, 0), commit.error


def test_new_root_waits_for_an_import_until_its_commit(make_user, agent, clinic):
    manager = make_user("verify_root_manager", "account_manager")
    args = dict(
        actor=agent,
        data=b"account_code,site_code,name\nEXCLIN,EAST,Example East\n",
        filename="sites.csv",
    )
    preview = importing.preview_file(importing.SITES, **args)
    assert preview.ok
    first = Held(
        lambda: importing.commit_file(
            importing.SITES, **args, state_token=preview.state_token, reason=""
        )
    )
    second = Session(
        lambda: directory.create_client(
            actor=manager, name="Example Other", account_number="EXROOT"
        )
    ).start()
    try:
        state, statement = observe(second, first.session)
    finally:
        assert first.release() == "done"
        second.join()
    assert second.outcome == "done", second.error
    assert Client.objects.get(account_number="EXROOT").parent_id is None
    assert ClientSite.objects.get(code="EAST").client_id == clinic.pk
    assert (state, "pg_advisory_xact_lock" in statement) == ("waiting", True), statement


def test_organization_file_rechecks_a_new_name_after_waiting_for_a_later_rows_parent(
    agent, make_user, clinic
):
    """A rename under way when an organizations file is confirmed. The file names a new
    organization with the name being given, and a later row under the renamed organization.
    Intended: the file is refused (the name is taken), nothing of it is saved."""
    manager = make_user("verify_name_importer", "account_manager")
    name = "Example Northern Unit"
    args = dict(
        actor=manager,
        filename="organizations.csv",
        data=(
            f"account_code,name,parent_account_code\nEXNEW,{name},\n"
            "EXBRANCH,Example New Branch,EXCLIN\n"
        ).encode(),
    )
    preview = importing.preview_file(importing.ORGANIZATIONS, **args)
    assert preview.ok and preview.counts["create"] == 2
    edit = Held(
        lambda: directory.update_client(
            actor=agent,
            client_id=clinic.pk,
            expected_revision=clinic.revision,
            name=name,
            reason="Current office name",
        )
    )
    commit = Session(
        lambda: importing.commit_file(
            importing.ORGANIZATIONS, **args, reason="", state_token=preview.state_token
        )
    ).start()
    try:
        state, statement = observe(commit, edit.session)
    finally:
        assert edit.release() == "done"
        commit.join()
    # Adapted: a rename takes the directory lock since review round 1's verification (V4), so
    # the file waits for it before reading anything.
    assert state == "waiting" and "pg_advisory_xact_lock" in statement, statement
    duplicates = list(
        Client.objects.filter(name=name)
        .order_by("account_number")
        .values_list("account_number", flat=True)
    )
    summaries = AuditEvent.objects.filter(action=importing.SUMMARY_ACTION).count()
    branch = Client.objects.filter(account_number="EXBRANCH").exists()
    assert (commit.outcome, duplicates, branch, summaries) == (
        "refused",
        [clinic.account_number],
        False,
        0,
    ), commit.error
