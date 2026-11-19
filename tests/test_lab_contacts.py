"""The laboratory's security and privacy contacts: only an administrator records them, with a
reason, against the revision the form showed; every change is audited and a decision is made again
under the laboratory row lock."""

import threading
import time

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction

from core import lab_profile
from core.models import AuditEvent, Lab, LabContact

pytestmark = pytest.mark.django_db

ACTION = "lab.contact_changed"
VALUES = {
    "name": "Example Security Office",
    "title": "Security lead",
    "email": "security@example.invalid",
    "phone": "555-0100",
}


@pytest.fixture
def lab():
    return Lab.objects.create(name="Example Reference Laboratory")


@pytest.fixture
def admin(staff):
    return staff("contact-admin", "admin")


def save(actor, kind="security", values=VALUES, revision=0, reason="Recorded for the file"):
    return lab_profile.save_contact(
        actor=actor, kind=kind, values=values, expected_revision=revision, reason=reason
    )


def nothing_written():
    assert not LabContact.objects.exists()
    assert not AuditEvent.objects.filter(action=ACTION).exists()


def test_contacts_lists_security_then_privacy_and_blanks_for_unrecorded(lab):
    rows = lab_profile.contacts()
    assert [row.kind for row in rows] == ["security", "privacy"]
    for row in rows:
        assert row.pk is None and row.revision == 0
        assert lab_profile.contact_values(row) == {
            "name": "",
            "title": "",
            "email": "",
            "phone": "",
        }
        assert not row.is_recorded


def test_contacts_returns_recorded_rows_in_order(lab, admin):
    save(admin, kind="privacy", values={"name": "Privacy Office"})
    rows = lab_profile.contacts()
    assert [row.kind for row in rows] == ["security", "privacy"]
    assert rows[0].pk is None and rows[0].revision == 0
    assert rows[1].pk is not None and rows[1].name == "Privacy Office" and rows[1].revision == 1


def test_admin_records_a_contact_stripped_with_revisions_and_audit(lab, admin):
    contact, changed = save(
        admin,
        values={
            "name": "  Example   Security\t Office ",
            "title": " Security   lead ",
            "email": " security@example.invalid ",
            "phone": " 555   0100 ",
        },
        reason="  First entry  ",
    )
    assert changed and contact.revision == 1
    contact.refresh_from_db()
    assert lab_profile.contact_values(contact) == {
        "name": "Example Security Office",
        "title": "Security lead",
        "email": "security@example.invalid",
        "phone": "555 0100",
    }
    first = AuditEvent.objects.get(action=ACTION)
    assert first.object_type == "core.labcontact"
    assert first.object_id == str(contact.pk)
    assert first.actor == admin
    assert first.reason == "First entry"
    assert first.before == {"kind": "security", "name": "", "title": "", "email": "", "phone": ""}
    assert first.after == {"kind": "security", **lab_profile.contact_values(contact)}

    contact, changed = save(
        admin, values=VALUES | {"phone": "555-0111"}, revision=1, reason="New number"
    )
    assert changed and contact.revision == 2
    events = AuditEvent.objects.filter(action=ACTION).order_by("occurred_at", "pk")
    assert events.count() == 2
    latest = events.last()
    assert latest.reason == "New number" and latest.actor == admin
    stored = {"name": VALUES["name"], "title": "Security lead", "email": VALUES["email"]}
    assert latest.before == {"kind": "security", **stored, "phone": "555 0100"}
    assert latest.after == {"kind": "security", **stored, "phone": "555-0111"}
    assert LabContact.objects.get(kind="security").revision == 2


@pytest.mark.parametrize("role", ["supervisor", "client_services", "account_manager", "technical"])
def test_operational_roles_are_refused(lab, staff, role):
    with pytest.raises(PermissionDenied):
        save(staff(f"contact-{role}", role))
    nothing_written()


def test_client_portal_account_is_refused(lab, portal_user, client_org):
    user = portal_user(client_org)
    user.groups.add(Group.objects.get_or_create(name="admin")[0])
    with pytest.raises(PermissionDenied):
        save(user)
    nothing_written()


def test_superuser_without_the_admin_role_is_refused(lab, staff):
    user = staff("contact-super", "technical")
    user.is_superuser = True
    user.save()
    with pytest.raises(PermissionDenied):
        save(user)
    nothing_written()


def test_inactive_admin_is_refused(lab, admin):
    admin.is_active = False
    admin.save()
    with pytest.raises(PermissionDenied):
        save(admin)
    nothing_written()


@pytest.mark.parametrize("reason", ["", "   ", "\n\t", None])
def test_blank_reason_is_refused(lab, admin, reason):
    with pytest.raises(ValidationError) as caught:
        save(admin, reason=reason)
    assert "reason" in caught.value.message_dict
    nothing_written()


def test_unknown_kind_is_refused(lab, admin):
    with pytest.raises(ValueError):
        save(admin, kind="billing")
    nothing_written()


@pytest.mark.parametrize("revision", [None, 1, 5, -1])
def test_a_never_recorded_contact_needs_revision_zero(lab, admin, revision):
    with pytest.raises(ValidationError):
        save(admin, revision=revision)
    nothing_written()


@pytest.mark.parametrize("revision", [None, 0, 2, 7])
def test_a_stale_or_missing_revision_is_refused(lab, admin, revision):
    save(admin)
    with pytest.raises(ValidationError):
        save(admin, values=VALUES | {"phone": "555-0199"}, revision=revision)
    assert LabContact.objects.get().phone == "555-0100"
    assert AuditEvent.objects.filter(action=ACTION).count() == 1


def test_name_is_required_when_other_fields_are_given(lab, admin):
    with pytest.raises(ValidationError) as caught:
        save(admin, values={"name": "   ", "title": "Lead", "email": "", "phone": ""})
    assert "name" in caught.value.message_dict
    nothing_written()


@pytest.mark.parametrize("field", ["title", "email", "phone"])
def test_each_other_field_needs_a_name(lab, admin, field):
    value = "a@example.invalid" if field == "email" else "x"
    with pytest.raises(ValidationError) as caught:
        save(admin, values={field: value})
    assert "name" in caught.value.message_dict
    nothing_written()


def test_an_invalid_email_is_refused(lab, admin):
    with pytest.raises(ValidationError):
        save(admin, values=VALUES | {"email": "not-an-email"})
    nothing_written()


def test_no_laboratory_row_is_refused(admin):
    assert not Lab.objects.exists()
    with pytest.raises(ValidationError):
        save(admin)
    nothing_written()


def test_the_same_values_change_nothing(lab, admin):
    contact, _ = save(admin)
    again, changed = save(
        admin, values={name: f"  {value}  " for name, value in VALUES.items()}, revision=1
    )
    assert changed is False
    assert again.revision == 1
    assert LabContact.objects.get().revision == 1
    assert AuditEvent.objects.filter(action=ACTION).count() == 1
    assert contact.pk == again.pk


def test_blank_submission_of_a_never_recorded_contact_creates_no_row(lab, admin):
    contact, changed = save(admin, values={"name": " ", "title": "", "email": "", "phone": ""})
    assert changed is False
    assert contact.revision == 0 and contact.pk is None
    nothing_written()
    contact, changed = save(admin, values={})
    assert changed is False
    nothing_written()


def test_clearing_every_field_is_allowed_and_audited(lab, admin):
    save(admin)
    contact, changed = save(admin, values={}, revision=1, reason="Person left; to be named again")
    assert changed and contact.revision == 2
    assert not contact.is_recorded
    event = AuditEvent.objects.filter(action=ACTION).order_by("-occurred_at", "-pk").first()
    assert event.before == {"kind": "security", **VALUES}
    assert event.after == {"kind": "security", "name": "", "title": "", "email": "", "phone": ""}
    assert event.reason == "Person left; to be named again"
    stored = lab_profile.contacts()[0]
    assert stored.pk is not None and not stored.is_recorded and stored.revision == 2


def test_history_includes_contact_changes(lab, admin):
    save(admin)
    assert ACTION in lab_profile.PROFILE_ACTIONS
    entries = list(lab_profile.history())
    assert [entry.action for entry in entries] == [ACTION]
    assert entries[0].actor == admin


# Decisions under concurrency: another session decides while save_contact waits for the
# laboratory row lock. Each test needs real commits, so the database is flushed afterwards like
# the other transactional tests.


def _wait_until_blocked(waiting=1, seconds=10):
    """Whether `waiting` other sessions are waiting for a lock within `seconds`."""
    deadline = time.monotonic() + seconds
    with connection.cursor() as cursor:
        while time.monotonic() < deadline:
            # pg_stat_activity is read once per transaction: without this, a session that
            # connected after the first reading in this transaction is never seen.
            cursor.execute("SELECT pg_stat_clear_snapshot()")
            cursor.execute(
                "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE NOT l.granted AND a.datname = current_database()"
            )
            if cursor.fetchone()[0] >= waiting:
                return True
            time.sleep(0.05)
    return False


def _in_thread(outcomes, name, **call):
    def run():
        try:
            outcomes[name] = ("ok", lab_profile.save_contact(**call))
        except Exception as error:  # reported by the assertions
            outcomes[name] = ("error", error)
        finally:
            connection.close()

    return threading.Thread(target=run)


@pytest.mark.django_db(transaction=True)
def test_an_admin_who_loses_the_role_while_waiting_is_refused(staff):
    Lab.objects.create(name="Example Reference Laboratory")
    admin = staff("contact-racing-admin", "admin")
    outcomes = {}
    worker = _in_thread(
        outcomes,
        "save",
        actor=admin,
        kind="security",
        values=VALUES,
        expected_revision=0,
        reason="Recorded for the file",
    )
    with transaction.atomic():
        Lab.objects.select_for_update().get(pk=1)
        worker.start()
        blocked = _wait_until_blocked()
        admin.groups.clear()
    worker.join(timeout=30)
    assert blocked, "save_contact did not wait for the laboratory row lock"
    status, error = outcomes["save"]
    assert status == "error" and isinstance(error, PermissionDenied)
    nothing_written()


@pytest.mark.django_db(transaction=True)
def test_two_first_saves_of_one_contact_let_exactly_one_through(staff):
    Lab.objects.create(name="Example Reference Laboratory")
    admin = staff("contact-first-admin", "admin")
    other = staff("contact-second-admin", "admin")
    outcomes = {}
    workers = [
        _in_thread(
            outcomes,
            actor.username,
            actor=actor,
            kind="security",
            values=VALUES | {"name": f"Office of {actor.username}"},
            expected_revision=0,
            reason="First save",
        )
        for actor in (admin, other)
    ]
    with transaction.atomic():
        Lab.objects.select_for_update().get(pk=1)
        for worker in workers:
            worker.start()
        blocked = _wait_until_blocked(waiting=2)
    for worker in workers:
        worker.join(timeout=30)
    assert blocked, "Both saves should wait for the laboratory row lock"
    assert len(outcomes) == 2
    winners = [name for name, (status, _) in outcomes.items() if status == "ok"]
    losers = [error for status, error in outcomes.values() if status == "error"]
    assert len(winners) == 1 and len(losers) == 1
    assert type(losers[0]) is ValidationError
    contact = LabContact.objects.get(kind="security")
    assert contact.revision == 1 and contact.name == f"Office of {winners[0]}"
    events = AuditEvent.objects.filter(action=ACTION)
    assert events.count() == 1
    assert events.get().actor.username == winners[0]
