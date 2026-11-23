"""Third confirmation of review round 1 (candidate b982d47, earlier c5af6c0): locks of normal
operations on synthetic PostgreSQL records. Adopted from the reviewer; its findings were
resolved in the next commit (assignee-group-deadlocks-round-1-confirmation-3-resolution.md in the
review history). Adapted: an organization file holds the parent it names (the help's wording
was corrected instead). Left in the original: the check of the earlier seed-order test, which
was replaced by this file's stronger one."""

import io

import pytest
from django.core.management import call_command
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from accounts.acting import ACCESS_GATE, INT4, close_access
from accounts.models import User
from clients import services as directory
from clients.models import Client, ClientContact
from core import demo
from core.management.commands import seed_demo
from core.management.commands.reset_demo import Command as ResetCommand
from core.models import AuditEvent, Lab
from dataimport import importing
from request_forms.services import create_template
from teams.models import TeamMembership

from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by, _statement

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def demo_options(settings, monkeypatch):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    settings.EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
    monkeypatch.setenv("DEMO_PASSWORD", "Tern harbor 4182 lantern")
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    return {"stdout": io.StringIO(), "stderr": io.StringIO()}


def _directory_is_held():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = pg_backend_pid() AND granted "
            "AND locktype = 'advisory' AND mode = 'ExclusiveLock' AND objsubid = 2 "
            "AND classid::bigint = %s AND objid::bigint = %s)",
            directory.DIRECTORY_LOCK,
        )
        return cursor.fetchone()[0]


@pytest.mark.parametrize(
    "file_email", ["", "coord@example.invalid"], ids=["name", "email-fallback"]
)
def test_rename_that_changes_a_files_contact_match_takes_directory(agent, clinic, file_email):
    contact = directory.add_contact(
        actor=agent, client_id=clinic.pk, name="Receiving", phone="555-0109"
    )
    args = dict(
        actor=agent,
        filename="contacts.csv",
        data=f"account_code,email,name,phone\nEXCLIN,{file_email},Coordinator,555-0193\n".encode(),
    )
    before = importing.preview_file(importing.CONTACTS, **args)
    assert before.ok and before.rows[0].action == "create"
    with transaction.atomic():
        directory.update_contact(
            actor=agent,
            contact_id=contact.pk,
            expected_revision=contact.revision,
            reason="Update contact name",
            name="Coordinator",
        )
        held = _directory_is_held()
    after = importing.preview_file(importing.CONTACTS, **args)
    assert after.ok and after.rows[0].action == "update" and after.rows[0].existing.pk == contact.pk
    assert after.state_token != before.state_token
    assert held, "The rename changed the contact selected by the file without holding the directory"


@pytest.mark.parametrize("edit", ["create", "change", "clear", "normalize", "unchanged"])
def test_contact_directory_lock_tracks_the_email_saved(agent, clinic, clinic_contact, edit):
    if edit == "normalize":
        ClientContact.objects.filter(pk=clinic_contact.pk).update(email=f" {clinic_contact.email} ")
    with transaction.atomic():
        if edit == "create":
            directory.add_contact(
                actor=agent, client_id=clinic.pk, name="Coordinator", phone="555-0193"
            )
        else:
            fields = {"phone": "555-0152"}
            if edit == "unchanged":
                fields["email"] = clinic_contact.email
            elif edit in ("change", "clear"):
                fields["email"] = "new@example.invalid" if edit == "change" else ""
            directory.update_contact(
                actor=agent,
                contact_id=clinic_contact.pk,
                expected_revision=clinic_contact.revision,
                reason="Update contact details",
                **fields,
            )
        assert _directory_is_held() == (edit != "unchanged")


def test_an_organization_file_holds_the_parent_it_names_until_it_ends(agent, clinic):
    """Adapted (the upload help now says other changes to an organization wait only for an
    import whose file names that organization): a file that names an existing parent holds it
    until the import's transaction ends, even when no row changes."""

    Client.objects.create(name="Example branch", account_number="EXBRANCH", parent=clinic)
    args = dict(
        actor=agent,
        filename="organizations.csv",
        data=b"account_code,parent_account_code\nEXBRANCH,EXCLIN\n",
    )
    preview = importing.preview_file(importing.ORGANIZATIONS, **args)
    assert preview.ok and preview.counts["unchanged"] == 1

    def probe_parent():
        with transaction.atomic():
            Client.objects.select_for_update(nowait=True).get(pk=clinic.pk)

    with transaction.atomic():
        result = importing.commit_file(
            importing.ORGANIZATIONS, **args, state_token=preview.state_token, reason=""
        )
        assert result.ok and result.counts["unchanged"] == 1
        probe = Session(probe_parent).start()
        outcome = probe.join(seconds=5)
    assert not AuditEvent.objects.filter(action=importing.SUMMARY_ACTION).exists()
    assert outcome == "55P03", (outcome, probe.error)


def test_repeat_seed_finishes_holding_its_existing_manager_before_the_directory(demo_options):

    call_command("seed_demo", **demo_options)
    manager = User.objects.get(username="demo_account_manager")
    directory_held = False
    reads = []

    def record_order(execute, sql, params, many, context):
        nonlocal directory_held
        result = execute(sql, params, many, context)
        if "pg_advisory_xact_lock(" in sql and tuple(params) == directory.DIRECTORY_LOCK:
            directory_held = True
        if (
            TeamMembership._meta.db_table in sql
            and "FOR KEY SHARE" in sql
            and tuple(params or ()) == (manager.pk,)
        ):
            reads.append("after directory" if directory_held else "before directory")
        return result

    with connection.execute_wrapper(record_order):
        call_command("seed_demo", **demo_options)
    assert directory_held
    assert reads == ["before directory"], reads


def _scope_row_probe(account):
    """Observe the row from another backend without waiting or writing."""

    def probe():
        with transaction.atomic():
            User.objects.select_for_update(nowait=True).only("pk").get(pk=account.pk)

    session = Session(probe).start()
    outcome = session.join(seconds=5)
    assert outcome in {"done", "55P03"}, session.error
    return outcome


@pytest.mark.parametrize("nested_exit", ["release", "rollback"])
def test_nested_holds_follow_their_own_savepoint_and_restore_the_outer_scope(
    agent, make_user, nested_exit
):

    from accounts.acting import hold_actor, holding

    other = make_user("other_scoped_reader", "client_services")

    class Discard(Exception):
        pass

    with transaction.atomic():
        with holding(agent) as outer:
            try:
                with transaction.atomic(), holding(other) as inner, holding(agent) as same:
                    assert same is outer
                    assert hold_actor(other) is inner
                    assert _scope_row_probe(agent) == _scope_row_probe(other) == "55P03"
                    if nested_exit == "rollback":
                        raise Discard
            except Discard:
                pass
            assert hold_actor(agent) is outer
            assert _scope_row_probe(agent) == "55P03"
            assert _scope_row_probe(other) == ("done" if nested_exit == "rollback" else "55P03")
            assert hold_actor(other) is not inner
            assert _scope_row_probe(other) == "55P03"
        assert hold_actor(agent) is not outer
        assert _scope_row_probe(agent) == "55P03"
    assert _scope_row_probe(agent) == _scope_row_probe(other) == "done"


def test_service_reuses_only_its_own_actors_scope(agent, make_user, clinic):

    from accounts.acting import holding

    other = make_user("other_scoped_manager", "account_manager")

    def add(person, name):
        with CaptureQueriesContext(connection) as queries:
            saved = directory.add_contact(
                actor=person, client_id=clinic.pk, name=name, phone="555-0139"
            )
        reheld = any("pg_advisory_xact_lock_shared(" in q["sql"] for q in queries)
        return saved, reheld

    with transaction.atomic(), holding(agent):
        same, same_reheld = add(agent, "Case coordinator")
        different, other_held = add(other, "Receiving coordinator")
        assert (same_reheld, other_held) == (False, True)
        assert _scope_row_probe(agent) == _scope_row_probe(other) == "55P03"
    assert set(
        AuditEvent.objects.filter(
            action="client_contact.added", object_id__in=[str(same.pk), str(different.pk)]
        ).values_list("object_id", "actor_id")
    ) == {(str(same.pk), agent.pk), (str(different.pk), other.pk)}


@pytest.mark.parametrize("kind", ["sites", "contacts"])
@pytest.mark.parametrize("operation", ["update", "create"])
def test_form_reference_while_directory_file_is_awaiting_commit(
    kind, operation, make_user, agent, clinic, clinic_site, clinic_contact, category
):
    """A form references the organization after the import's last write but before commit."""

    admin = make_user("confirmation3_forms_admin", "admin")
    if kind == "sites":
        code = "MAIN" if operation == "update" else "NEW"
        csv = f"account_code,site_code,name\nEXCLIN,{code},Reception\n"
    else:
        email = clinic_contact.email if operation == "update" else "new@example.invalid"
        csv = f"account_code,email,name\nEXCLIN,{email},Sam Example\n"
    spec = importing.BY_SLUG[kind]
    args = dict(actor=agent, data=csv.encode(), filename="directory.csv")
    preview = importing.preview_file(spec, **args)
    assert preview.ok and preview.counts[operation] == 1
    held = Held(
        lambda: importing.commit_file(
            spec, **args, reason="Directory change", state_token=preview.state_token
        )
    )
    form = Session(
        lambda: create_template(
            actor=admin, category=category, key="reception", name="Reception", clients=[clinic]
        )
    ).start()
    try:
        state = form.waiting_or_ended(seconds=4)
        if state == "waiting":
            assert _blocked_by(form.pid, held.session.pid)
            assert _statement(form.pid).strip().upper() == "COMMIT"
    finally:
        file_result = held.release()
        form.join()
    assert file_result == "done", held.session.error
    assert form.outcome == "done", form.error
    assert form.result.clients.contains(clinic)
    # Update-only files hold NO KEY UPDATE throughout. Creating the child still explicitly
    # locks its parent FOR UPDATE in the per-row directory service, until transaction end.
    assert state in {"ended", "waiting"}
    if operation == "update":
        assert state == "ended"


def test_reset_releases_preflight_reads_before_waiting_for_another_demo_command(
    demo_options, monkeypatch
):
    """Inspect the reset's relation locks at admission and stop before any erase."""

    class AdmissionReached(Exception):
        pass

    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    call_command("seed_demo", **demo_options)
    assert demo.is_sealed(Lab.objects.get(pk=1))

    granted = []

    def observe_admission():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT c.relname, l.mode FROM pg_locks l "
                "JOIN pg_class c ON c.oid = l.relation "
                "WHERE l.pid = pg_backend_pid() AND l.granted AND c.relname = ANY(%s) "
                "ORDER BY c.relname, l.mode",
                [[Lab._meta.db_table, AuditEvent._meta.db_table]],
            )
            granted.extend(cursor.fetchall())
        raise AdmissionReached

    # On the previous revision admission goes through the directory/table lock helper.
    admission = "hold_seed_lock" if hasattr(demo, "hold_seed_lock") else "lock_application_tables"
    monkeypatch.setattr(demo, admission, observe_admission)
    monkeypatch.setattr(ResetCommand, "_erase", lambda _command: pytest.fail("past admission"))
    with pytest.raises(AdmissionReached):
        call_command("reset_demo", interactive=False, **demo_options)
    assert not granted, granted


def test_seed_prefix_takes_every_folded_gate_in_the_access_services_order(make_user, monkeypatch):

    first = make_user("demo_admin", "admin", pk=400)
    folded = make_user("demo_account_manager", "account_manager", pk=INT4 + 17)
    monkeypatch.setenv("DEMO_PASSWORD", "Tern harbor 4182 lantern")
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)

    class ReachedDirectory(Exception):
        pass

    def stop_before_directory():
        raise ReachedDirectory

    acquired = []

    def record_gate(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if "pg_advisory_xact_lock" in sql and params[0] == ACCESS_GATE:
            acquired.append(params[1])
        return result

    monkeypatch.setattr(seed_demo, "hold_directory", stop_before_directory)
    with connection.execute_wrapper(record_gate):
        with pytest.raises(ReachedDirectory):
            call_command("seed_demo", stdout=io.StringIO(), stderr=io.StringIO())
        during_seed = acquired.copy()
        acquired.clear()
        with transaction.atomic():
            close_access([first.pk, folded.pk])
    assert during_seed == acquired == [17, 400]
