"""Fourth confirmation of review round 1 (candidate 2eceea1, earlier b982d47). Synthetic records.
Adopted from the reviewer; its four findings were resolved in the next commit
(assignee-group-deadlocks-round-1-confirmation-4-resolution.md in the review history). Left in
the original, resolved by correcting the documents instead: a first seed reusing readings of
accounts it creates (the architecture now says the seed reuses those that exist when it begins),
and an import locking the ancestors of an organization it names (the upload help now says so)."""

import io

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction

from accounts import acting
from accounts.models import User
from clients import services
from clients.models import Client
from core import demo
from core.management.commands.reset_demo import Command as ResetCommand
from core.models import AuditEvent
from dataimport import importing

from .test_review_assignment_lock_order_verify3 import Session

pytestmark = pytest.mark.django_db(transaction=True)


def _output():
    return {"stdout": io.StringIO(), "stderr": io.StringIO()}


@pytest.mark.parametrize("invalid", ["missing password", "short admin password"])
def test_invalid_seed_configuration_is_reported_before_locking(make_user, monkeypatch, invalid):
    make_user("demo_account_manager", "account_manager")
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    setting = "DEMO_PASSWORD"
    if invalid == "short admin password":
        monkeypatch.setenv("DEMO_PASSWORD", "Tern harbor 4182 lantern")
        monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "short")
        setting = "DEMO_ADMIN_PASSWORD"

    acquired = []

    def observe(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if "pg_advisory_xact_lock" in sql:
            acquired.append(tuple(params))
        return result

    with connection.execute_wrapper(observe), pytest.raises(CommandError, match=setting):
        call_command("seed_demo", **_output())
    assert acquired == [], acquired


@pytest.mark.parametrize("answer", ["cancel", "reset"])
def test_reset_prompt_releases_preflight_and_rechecks_after_confirmation(monkeypatch, answer):
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    trace = []
    require = ResetCommand._require_demo_laboratory
    seed_lock = demo.hold_seed_lock
    table_locks = demo.lock_application_tables

    def decision(command):
        trace.append(("decision", connection.get_autocommit()))
        return require(command)

    def prompt(_message):
        trace.append(("prompt", connection.get_autocommit()))
        if answer == "reset":
            writer = Session(
                lambda: Client.objects.create(name="Another clinic", account_number="EXOTHER")
            ).start()
            assert writer.join(seconds=5) == "done", writer.error
        return answer

    def acquire_seed():
        trace.append(("seed", connection.get_autocommit()))
        seed_lock()

    def acquire_tables():
        trace.append(("tables", connection.get_autocommit()))
        table_locks()

    monkeypatch.setattr("builtins.input", prompt)
    monkeypatch.setattr(ResetCommand, "_require_demo_laboratory", decision)
    monkeypatch.setattr(ResetCommand, "_erase", lambda _command: pytest.fail("Unexpected erase"))
    monkeypatch.setattr(demo, "hold_seed_lock", acquire_seed)
    monkeypatch.setattr(demo, "lock_application_tables", acquire_tables)
    with pytest.raises(CommandError, match="cancelled" if answer == "cancel" else "not empty"):
        call_command("reset_demo", interactive=True, **_output())
    expected = [("decision", True), ("prompt", True)]
    if answer == "reset":
        assert Client.objects.filter(account_number="EXOTHER").exists()
        expected.extend([("seed", False), ("tables", False), ("decision", False)])
    assert trace == expected


@pytest.mark.parametrize("email", ["", "new@example.invalid"])
def test_an_earlier_name_only_preview_is_refused_after_a_rename(agent, clinic, email):
    contact = services.add_contact(
        actor=agent, client_id=clinic.pk, name="Office", phone="555-0145"
    )
    args = dict(
        actor=agent,
        filename="coordination.csv",
        data=f"account_code,email,name,phone\nEXCLIN,{email},Desk,555-0146\n".encode(),
    )
    before = importing.preview_file(importing.CONTACTS, **args)
    assert before.ok and before.counts["create"] == 1
    services.update_contact(
        actor=agent,
        contact_id=contact.pk,
        expected_revision=contact.revision,
        reason="Rename office",
        name="Desk",
    )
    with pytest.raises(ValidationError, match="different from the preview"):
        importing.commit_file(importing.CONTACTS, **args, state_token=before.state_token, reason="")
    contact.refresh_from_db()
    assert (contact.name, contact.phone, contact.email) == ("Desk", "555-0145", "")
    assert not AuditEvent.objects.filter(action=importing.SUMMARY_ACTION).exists()


def test_multi_account_scope_restores_the_prior_readings_after_an_exception(agent, make_user):
    other = make_user("other_manager", "account_manager")
    missing = User(pk=max(agent.pk, other.pk) + 1000)

    class Finished(Exception):
        pass

    with transaction.atomic(), acting.holding(agent) as outer:
        readings = acting.hold_actors([missing, other, agent])
        assert {reader.pk for reader in readings} == {agent.pk, other.pk}
        with pytest.raises(Finished), acting.scope(readings):
            from_scope = acting.hold_actor(other)
            assert from_scope is next(reader for reader in readings if reader.pk == other.pk)
            assert acting.hold_actor(agent) is outer
            point = transaction.savepoint()
            transaction.savepoint_rollback(point)
            transaction.savepoint_commit(point)
            assert acting.hold_actor(other) is from_scope
            assert acting.hold_actor(missing) is None
            raise Finished
        assert acting.hold_actor(other) is not from_scope
        assert acting.hold_actor(agent) is outer


def test_scope_requires_an_active_transaction_at_entry(agent):
    with transaction.atomic():
        readings = acting.hold_actors([agent])
    assert connection.get_autocommit()
    with pytest.raises(RuntimeError), acting.scope(readings):
        pass


@pytest.mark.parametrize(
    ("stored", "submitted", "should_hold"),
    [
        ("Reception", " Reception ", False),
        (" Reception ", None, True),
        ("Reception", "Dispatch", True),
    ],
    ids=["same-after-trim", "normalize-omitted-name", "rename-with-email"],
)
def test_contact_directory_decision_matches_name_as_saved(
    agent, clinic_contact, stored, submitted, should_hold
):
    from clients.models import ClientContact

    ClientContact.objects.filter(pk=clinic_contact.pk).update(name=stored)
    fields = {"phone": "555-0130"}
    if submitted is not None:
        fields["name"] = submitted
    with transaction.atomic():
        updated = services.update_contact(
            actor=agent,
            contact_id=clinic_contact.pk,
            expected_revision=clinic_contact.revision,
            reason="Update reception details",
            **fields,
        )
        assert updated.email == clinic_contact.email
        assert updated.name == (submitted or stored).strip()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = pg_backend_pid() "
                "AND locktype = 'advisory' AND granted AND mode = 'ExclusiveLock' "
                "AND objsubid = 2 AND classid::bigint = %s AND objid::bigint = %s)",
                services.DIRECTORY_LOCK,
            )
            assert cursor.fetchone()[0] == should_hold
