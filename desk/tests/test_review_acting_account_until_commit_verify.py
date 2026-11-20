"""Verification of review round 1: the acting account held until commit (candidate 3c82dcf, round 1
candidate 1743376, base cc8d7fa). All records are synthetic.

Adopted: V2 (the import page says why a commit was refused; adapted in the follow-up on
deadlocks with an organization's group, see its docstring) and V4 (updates that take no access
away). V4's password change is a full save of the account, which takes the access gate by design
(docs/architecture.md: "a full save of the account ... waits for the person's change under way"):
its case asserts that wait. V1 and V3 predated the change and became the acceptance cases of the
roadmap follow-up on deadlocks with a ticket's assignee and with an organization's group
(test_assignee_and_group_deadlocks.py); V5 is the documented order of a multi-member access
change (covered in test_acting_account_until_commit_round1_neighbours.py).

Each test states intended behaviour (docs/architecture.md "Services and hooks", the docstrings of
accounts/acting.py and dataimport/importing.py, the import page's help, docs/roadmap.md). The races
run every party in its own PostgreSQL session (a thread with its own connection) with a bounded
lock_timeout; a party is held at a named point until the others wait for a lock (seen with
pg_blocking_pids) or have finished. The file uses no helper only the candidate has, so it runs on
the base too.
"""

import dataclasses
import threading
import time
from contextlib import nullcontext
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from django.test import Client
from django.urls import reverse

from accounts.models import User
from clients.models import Client as Organization
from desk import services
from desk.models import EntryKind
from links import services as link_services
from links.models import ExternalRecordLink, ExternalSystem

from .test_contract import new_request
from .test_review_assignment_lock_order_verify3 import (
    Session,
    _blocked_by,
    _fresh,
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
    return make_user("verify_owner", "admin")


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


# V1. The import and an organization's group change: whichever waited first is ended --------------


def _group_and_member():
    group = Organization.objects.create(name="Example Health Group", account_number="EXGROUP")
    member = Organization.objects.create(name="Example Member Clinic", account_number="EXMEMBER")
    assert group.pk < member.pk
    return group, member


GROUP_FIRST = (
    HEADER
    + "EXGROUP,lis,order,,ORD-TEST-000701,related,,\r\n"
    + "EXMEMBER,lis,order,,ORD-TEST-000702,related,,\r\n"
)


def _paused_after_the_first_organization(gates):
    """The external identifier record type, stopped (in the gated session) once it holds the
    file's first organization and before it locks the second: inside the candidate's lock step
    (RecordType.hold) when there is one, after the first row otherwise (the base)."""
    from dataimport import importing

    record_type = importing.EXTERNAL_IDS
    if "hold" not in {field.name for field in dataclasses.fields(record_type)}:
        return record_type, gated(link_services, "add_link", gates, before=False)
    real = record_type.hold

    def hold(rows):
        first = next(
            (
                row.new_fields["client_id"]
                for row in rows
                if not row.errors and row.action == "create" and row.existing is None
            ),
            None,
        )
        list(Organization.objects.select_for_update().filter(pk=first).values_list("pk"))
        gate = gates.get(threading.current_thread())
        if gate is not None:
            gate.stop()
        return real(rows)

    return dataclasses.replace(record_type, hold=hold), nullcontext()


# V2. The import page after a commit refused at its lock step -------------------------------------


def test_an_import_refused_at_its_lock_step_says_why_on_the_import_page(
    make_user, lis, monkeypatch
):
    """The import page when the database ends the import's lock step to break a deadlock: the
    import, not the other change, is ended, and the file is refused with nothing imported.

    Adapted in the follow-up on deadlocks with an organization's group: putting an organization
    into a group no longer meets an import in a deadlock (both take the organizations' groups
    lock before their first organization), and no change of the application locks two of a
    file's organizations the other way round. A test session stands in for one. It holds the
    member; the import locks the group and stops; the stand-in asks for the group and waits past
    the database's deadlock_timeout, so its own deadlock check finds no cycle yet; the import
    then asks for the member, and its check finds the cycle and ends it.

    Intended (the import page's help; dataimport.importing.LOCKS_REFUSED, the message written
    for this case; AGENTS.md 5 "handle validation"): the page the person gets back says why:
    another change to the file's organizations or systems was being saved at the same moment."""
    from dataimport import importing

    group, member = _group_and_member()
    importer = make_user("verify_page_importer", "account_manager")
    http = Client()
    http.force_login(importer)
    url = reverse("dataimport:upload", args=["external-ids"])
    upload = SimpleUploadedFile("ids.csv", GROUP_FIRST.encode(), content_type="text/csv")
    drawn = http.post(url, {"step": "preview", "file": upload})
    assert drawn.status_code == 200
    initial = drawn.context["commit_form"].initial
    data = {
        "step": "commit",
        "filename": initial["filename"],
        "payload": initial["payload"],
        "state_token": initial["state_token"],
        "reason": "",
    }
    gates = {}
    record_type, _around = _paused_after_the_first_organization(gates)
    monkeypatch.setitem(importing.BY_SLUG, "external-ids", record_type)
    holding, go = threading.Event(), threading.Event()

    def stand_in():
        with transaction.atomic():
            list(Organization.objects.select_for_update().filter(pk=member.pk).values_list("pk"))
            holding.set()
            go.wait(timeout=20)
            list(Organization.objects.select_for_update().filter(pk=group.pk).values_list("pk"))

    with connection.cursor() as cursor:
        cursor.execute("SHOW deadlock_timeout")
        deadlock_timeout = cursor.fetchone()[0]
    other = Session(stand_in).start()
    assert holding.wait(timeout=10), other.error
    page = Session(lambda: http.post(url, data))
    gates[page.thread] = Gate()
    page.start()
    assert gates[page.thread].arrived.wait(timeout=10), page.error  # holds the group
    go.set()
    other_state = other.waiting_or_ended(seconds=3)  # for the group
    time.sleep(_seconds(deadlock_timeout) + 0.5)
    gates[page.thread].go.set()  # the import asks for the member
    page.join()
    other.join()
    assert other_state == "waiting", other_state
    assert page.outcome == "done", repr(page.error)
    assert other.outcome == "done", repr(other.error)
    assert page.result.status_code == 200
    assert ExternalRecordLink.objects.filter(identifier__startswith="ORD-TEST-00070").count() == 0
    shown = [str(message) for message in page.result.context["messages"]]
    preview = page.result.context["preview"]
    detail = {"messages": shown, "preview errors": preview.file_errors if preview else None}
    said = " ".join(shown + (preview.file_errors if preview else []))
    assert "being saved at the same moment" in said, detail


def _seconds(setting):
    """A duration setting as PostgreSQL shows it (1s, 500ms) in seconds."""
    if setting.endswith("ms"):
        return int(setting[:-2]) / 1000
    if setting.endswith("min"):
        return int(setting[:-3]) * 60
    return int(setting.rstrip("s"))


# V3. A6 in its other order: the client's reply waits first ---------------------------------------


# V4. Updates that take no access away and the person's change under way --------------------------


@pytest.mark.parametrize(
    "update", ["password change", "email notices", "capability added to the role"]
)
def test_an_update_that_takes_no_access_away_does_not_wait_for_the_persons_change(
    update, make_user, join, agent, sample_category, accessioning
):
    """A technician's note is under way (it has held their account and reached its ticket). In
    another tab the technician changes their password (allauth saves the account in full) or turns
    their email notices off; or an owner gives the technician role the patient-information
    capability on the role group page.

    Intended (docs/architecture.md "Updates that take no access away, such as a sign-in or the
    email notice preference, do not wait; a full save of the account (its page, a password
    change) takes the gate and waits for the person's change under way"; accounts/acting.py
    "Additions wait for nothing"): the notice preference and the capability added to the role do
    not wait for the note; the password change, a full save, waits for it and then goes
    through."""
    from allauth.account.adapter import get_adapter
    from django.contrib.auth.models import Group, Permission

    person = make_user(f"verify_tab_{update.split()[0]}", "technical")
    join(person, accessioning)
    ticket = new_request(agent, sample_category, subject="Under way")

    if update == "password change":

        def work():
            get_adapter().set_password(User.objects.get(pk=person.pk), "violet harbour lantern")

    elif update == "email notices":
        http = Client()
        http.force_login(person)

        def work():
            return http.post(reverse("email-notices"), {})

    else:
        root = make_user("verify_tab_root", "admin", is_staff=True, is_superuser=True)
        technical = Group.objects.get(name="technical")
        permission = Permission.objects.get(
            codename="view_patient_information", content_type__app_label="accounts"
        )
        http = Client()
        http.force_login(root)
        url = reverse("admin:auth_group_change", args=[technical.pk])

        def work():
            return http.post(url, {"name": "technical", "permissions": [str(permission.pk)]})

    first = Session(lambda: _note(person, ticket, "Note under way ORD-TEST-000721."))
    gates = {first.thread: Gate()}
    with gated(services, "_locked_request", gates):
        first.start()
        assert gates[first.thread].arrived.wait(timeout=10), first.error
        session = Session(work).start()
        state = session.waiting_or_ended(seconds=3)
        behind_note = session.pid is not None and _blocked_by(session.pid, first.pid)
        gates[first.thread].go.set()
        first.join()
        session.join()
    assert first.outcome == "done", repr(first.error)
    assert session.outcome == "done", repr(session.error)
    if update == "password change":  # a full save: waits for the note, then goes through
        assert (state, behind_note) == ("waiting", True), state
    else:
        assert state == "ended", (state, "behind the note" if behind_note else "")


# V5. A capability removed from a role, and a member it has not reached yet -----------------------
