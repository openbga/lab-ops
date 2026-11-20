"""Independent review, round 1, of the follow-up on deadlocks with a ticket's assignee and with an
organization's group (candidate 7342f6f, base 833d571). All records are synthetic.

Adopted: R1 (an edit that changes no parent or code does not take the directory lock), R2 (the
seed takes the directory lock before the tables), R3 (an import reads account codes after a
renumbering), R4 (an organization change holds its actor), R6 (adapted: a full save of the
account locks its row as FOR UPDATE does, so the reply waits at COMMIT for the deactivation,
which waits for nothing it holds), R8 (a duplicate code on a creation is a form error), and the
reviewer's checks found sound (a second file waits before its first organization query, release
skips a ticket closed meanwhile, the outbox and the scheduled settings against a deactivation,
direct posts by every audience). R5, R7, R9, R10 and R11 predate the change and are the
acceptance cases of the roadmap item "Review follow-up: decisions read before a wait"; their
tests stay in the original."""

import io
from threading import Event
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import connection, transaction
from django.urls import reverse

from accounts.models import User
from clients import services as directory
from clients.models import Client, ClientContact, ClientSite, PortalAccess
from clients.tests.test_views import edit_data
from core.models import AuditEvent
from dataimport import importing
from desk import services
from desk.models import EntryKind, RequestEntry, Status
from jobs import outbox
from jobs.models import JobSettings, Notice, NoticeStatus, NotSentReason
from jobs.services import update_settings
from links.models import ExternalRecordLink, ExternalSystem

from .test_contract import new_request, set_status
from .test_review_acting_account_until_commit import Gate
from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by, _statement

pytestmark = pytest.mark.django_db(transaction=True)


DIRECTORY_FILES = {
    "organizations": (
        "account_code,name,parent_account_code\nEXBRANCH,Example Branch,EXCLIN\n",
        Client,
    ),
    "sites": ("account_code,site_code,name\nEXCLIN,EAST,Example East\n", ClientSite),
    "contacts": (
        "account_code,email,name\nEXCLIN,desk@example.invalid,Example Desk\n",
        ClientContact,
    ),
    "external-ids": (
        "account_code,system,record_type,identifier_scheme,identifier,role\n"
        "EXCLIN,lis,order,,ORD-TEST-000832,related\n",
        ExternalRecordLink,
    ),
}


def observe(worker, holder):
    """Sample the blocker while both sessions still own their transactions."""
    state = worker.waiting_or_ended(seconds=5)
    if state == "waiting":
        assert _blocked_by(worker.pid, holder.pid)
        return state, _statement(worker.pid)
    return state, ""


def test_editing_only_the_phone_of_a_group_member_is_independent(client, agent, clinic):
    group = Client.objects.create(name="Example Group")
    clinic.parent = group
    clinic.save(update_fields=["parent"])
    elsewhere = Client.objects.create(name="Example North")
    other_group = Client.objects.create(name="Example North Group")
    url = reverse("clients:client-edit", args=[clinic.pk])
    client.force_login(agent)
    form = client.get(url).context["form"]
    assert form["parent"].value() == group.pk
    data = edit_data(clinic, parent=form["parent"].value(), phone="555-0177")

    other = Held(
        lambda: directory.update_client(
            actor=agent,
            client_id=elsewhere.pk,
            expected_revision=elsewhere.revision,
            parent=other_group,
            reason="Joined the north group",
        )
    )
    edit = Session(lambda: client.post(url, data)).start()
    try:
        state, statement = observe(edit, other.session)
    finally:
        assert other.release() == "done"
        edit.join()
    assert edit.outcome == "done", edit.error
    assert edit.result.status_code == 302
    clinic.refresh_from_db()
    assert (clinic.phone, clinic.revision, clinic.parent_id) == ("555-0177", 2, group.pk)
    assert AuditEvent.objects.filter(action="client.updated", object_id=str(clinic.pk)).count() == 1
    assert state == "ended", statement


def test_two_creations_under_a_parent_return_the_duplicate_code_on_the_form(client, agent, clinic):
    """The two submissions pass their form validation while the first insert is uncommitted."""
    client.force_login(agent)
    client.raise_request_exception = False
    url = reverse("clients:client-create")
    code = "EXBRANCH"
    other = Held(
        lambda: directory.create_client(
            actor=agent,
            name="Example East",
            parent=clinic,
            account_number=code,
        )
    )
    data = {"name": "Example West", "parent": clinic.pk, "account_number": code}
    edit = Session(lambda: client.post(url, data)).start()
    try:
        state, _ = observe(edit, other.session)
    finally:
        assert other.release() == "done"
        edit.join()
    assert state == "waiting"
    assert edit.outcome == "done", edit.error
    assert list(Client.objects.filter(account_number=code).values_list("name", flat=True)) == [
        "Example East"
    ]
    assert AuditEvent.objects.filter(action="client.created").count() == 1
    database_error = edit.result.exc_info[1].__cause__ if edit.result.exc_info else None
    assert edit.result.status_code == 200, getattr(database_error, "sqlstate", None)
    assert b"already uses this account code" in edit.result.content
    assert edit.result.context["form"]["name"].value() == "Example West"


@pytest.mark.parametrize("slug", ["sites", "contacts", "external-ids"])
def test_import_confirmation_rechecks_the_account_code_after_waiting_for_its_row(
    make_user, agent, clinic, slug
):
    manager = make_user("file_manager", "account_manager")
    ExternalSystem.objects.get_or_create(
        key="lis", defaults={"name": "Example Laboratory System", "kind": "lis"}
    )
    csv, model = DIRECTORY_FILES[slug]
    spec = importing.BY_SLUG[slug]
    args = dict(actor=manager, data=csv.encode(), filename="directory.csv")
    before = model.objects.count()
    preview = importing.preview_file(spec, **args)
    assert preview.ok and preview.counts["create"] == 1, [row.errors for row in preview.rows]
    renumber = Held(
        lambda: directory.update_client(
            actor=agent,
            client_id=clinic.pk,
            expected_revision=clinic.revision,
            reason="Renumbered clinic",
            account_number="EXMOVED",
        )
    )
    commit = Session(
        lambda: importing.commit_file(spec, **args, state_token=preview.state_token, reason="")
    ).start()
    try:
        state, _ = observe(commit, renumber.session)
    finally:
        assert renumber.release() == "done"
        commit.join()
    assert state == "waiting"
    assert not Client.objects.filter(account_number="EXCLIN").exists()
    assert (
        commit.outcome,
        model.objects.count() - before,
        AuditEvent.objects.filter(action=importing.SUMMARY_ACTION).count(),
    ) == ("refused", 0, 0), commit.error


@pytest.mark.parametrize("partial", [False, True], ids=["full-save", "active-field-only"])
def test_staff_reply_can_commit_while_its_portal_recipient_deactivation_remains_open(
    settings, agent, clinic, clinic_portal, clinic_contact, sample_category, partial
):
    """A staff reply has recorded a notice to the portal account when that account's deactivation
    saves and keeps its transaction open.

    Adapted (R6) to the documented outcome (desk.signals, accounts.models.User.save): the
    deactivation's own lock is FOR NO KEY UPDATE, which the reply's check of the notice's
    recipient at COMMIT does not wait for; a full save's update then locks the row as FOR UPDATE
    does (it names the unique username and email, and the table has an update trigger), so after
    a full save the reply waits at COMMIT until the deactivation commits. The deactivation waits
    for nothing the reply holds, so both are saved."""
    # The notice is still written with a real deferred FK; no delivery callback can cause a wait.
    settings.EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
    PortalAccess.objects.create(
        user=clinic_portal, client=clinic, contact=clinic_contact, whole_organization=True
    )
    ticket = new_request(agent, sample_category, client=clinic, contact=clinic_contact)
    body = "An update on order ORD-TEST-000831."
    recording = Gate()

    def reply():
        with transaction.atomic():
            services.add_entry(
                actor=agent,
                request=ticket,
                expected_revision=ticket.revision,
                kind=EntryKind.REPLY,
                body=body,
            )
            assert Notice.objects.filter(recipient=clinic_portal, events=["reply"]).count() == 1
            recording.stop()

    def deactivate():
        account = User.objects.get(pk=clinic_portal.pk)
        account.is_active = False
        account.changed_by = agent
        account.save(update_fields=["is_active"] if partial else None)

    reply_session = Session(reply).start()
    try:
        assert recording.arrived.wait(timeout=5), reply_session.error
        other = Held(deactivate)
        try:
            recording.go.set()
            state, statement = observe(reply_session, other.session)
        finally:
            assert other.release() == "done"
    finally:
        recording.go.set()
        reply_session.join()
    assert reply_session.outcome == "done", reply_session.error
    assert RequestEntry.objects.filter(request=ticket, body=body).count() == 1
    assert Notice.objects.filter(recipient=clinic_portal, events=["reply"]).count() == 1
    if partial:
        assert state == "ended", statement
    else:
        assert (state, statement) == ("waiting", "COMMIT")


def test_seed_leaves_write_access_to_an_organization_preview_that_started_first(
    make_user, settings, monkeypatch
):
    """Probe the preview's normal INSERT table-lock mode without creating a wait cycle."""
    manager = make_user("seed_review_manager", "account_manager")
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    settings.EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic review only")
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    ready = Gate()
    real_apply = importing._apply

    def check_write_access(*args):
        ready.stop()
        with connection.cursor() as cursor:
            cursor.execute("LOCK TABLE clients_client IN ROW EXCLUSIVE MODE NOWAIT")
        return real_apply(*args)

    monkeypatch.setattr(importing, "_apply", check_write_access)
    preview = Session(
        lambda: importing.preview_file(
            importing.ORGANIZATIONS,
            actor=manager,
            filename="branch.csv",
            data=b"account_code,name\nEXNORTH,Example North Branch\n",
        )
    ).start()
    seed = None
    try:
        assert ready.arrived.wait(timeout=5), preview.error
        seed = Session(
            lambda: call_command("seed_demo", stdout=io.StringIO(), stderr=io.StringIO())
        ).start()
        state, statement = observe(seed, preview)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT mode FROM pg_locks WHERE pid = %s AND granted "
                "AND relation = 'clients_client'::regclass ORDER BY mode",
                [seed.pid],
            )
            modes = [mode for (mode,) in cursor.fetchall()]
    finally:
        ready.go.set()
        preview.join()
        if seed is not None:
            seed.join()
    assert seed.outcome == "done", seed.error
    assert Client.objects.filter(account_number="EXGEN", parent__account_number="EXHS").exists()
    assert not Client.objects.filter(account_number="EXNORTH").exists()
    assert preview.outcome == "done", (state, statement, modes, preview.outcome)
    assert preview.result.ok and preview.result.counts["create"] == 1


def test_an_outbox_attempt_defers_then_suppresses_a_notice_to_a_deactivated_recipient(
    agent, clinic, clinic_contact, clinic_portal, sample_category, settings
):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    PortalAccess.objects.create(
        user=clinic_portal, client=clinic, contact=clinic_contact, whole_organization=True
    )
    ticket = new_request(agent, sample_category, client=clinic, contact=clinic_contact)
    # Hold delivery until the ordinary reply transaction has committed. Its first attempt
    # is then invoked explicitly so the test can observe the busy outcome.
    with patch.object(outbox, "send_after_commit"):
        services.add_entry(
            actor=agent,
            request=ticket,
            expected_revision=ticket.revision,
            kind=EntryKind.REPLY,
            body="An update on order ORD-TEST-000840.",
        )
    notice = Notice.objects.get(recipient=clinic_portal, events=["reply"])

    def deactivate():
        account = User.objects.get(pk=clinic_portal.pk)
        account.is_active = False
        account.changed_by = agent
        account.save(update_fields=["is_active"])

    other = Held(deactivate)
    try:
        assert outbox.attempt(notice.pk) == "busy"
        notice.refresh_from_db()
        assert (notice.status, notice.attempts) == (NoticeStatus.WAITING, 0)
    finally:
        assert other.release() == "done"
    assert outbox.attempt(notice.pk, now=notice.next_attempt_at) == "not_sent"
    notice.refresh_from_db()
    assert notice.not_sent_reason == NotSentReason.NO_LONGER_APPLIES


@pytest.mark.parametrize("action", ["create", "edit"])
def test_group_save_holds_or_refreshes_its_manager_role_when_it_has_to_wait(
    make_user, agent, clinic, action
):
    """Both roles permit the write. Observe which membership reading authorizes it."""
    manager = make_user("group_editor", "client_services")
    old_role = Group.objects.get(name="client_services")
    new_role = Group.objects.get_or_create(name="account_manager")[0]
    parent = Client.objects.create(name="Example Parent")
    member = Client.objects.create(name="Example Branch")
    switched = Event()
    role_reads = []

    def switch_permitted_roles():
        with transaction.atomic():
            manager.groups.add(new_role)
            manager.groups.remove(old_role)
        switched.set()

    def observe_authorization(execute, sql, params, many, context):
        if switched.is_set() and "accounts_user_groups" in sql.lower():
            role_reads.append(sql)
        return execute(sql, params, many, context)

    def save():
        with connection.execute_wrapper(observe_authorization):
            if action == "create":
                return directory.create_client(actor=manager, parent=clinic, name="Example West")
            return directory.update_client(
                actor=manager,
                client_id=member.pk,
                expected_revision=member.revision,
                parent=clinic,
                reason="Joined the clinic",
            )

    first = Held(
        lambda: directory.update_client(
            actor=agent,
            client_id=clinic.pk,
            expected_revision=clinic.revision,
            parent=parent,
            reason="Joined the parent",
        )
    )
    editor = Session(save).start()
    switch = None
    try:
        state, _ = observe(editor, first.session)
        assert state == "waiting"
        switch = Session(switch_permitted_roles).start()
        switch_state, _ = observe(switch, editor)
    finally:
        assert first.release() == "done"
        editor.join()
        if switch is not None:
            switch.join()
    assert (editor.outcome, switch.outcome) == ("done", "done"), (editor.error, switch.error)
    assert editor.result.parent_id == clinic.pk
    assert switch_state == "waiting" or (switch_state == "ended" and role_reads), (
        "The role switch committed during the wait and the save made no further membership read."
    )


def test_release_skips_a_ticket_closed_while_deactivation_waited(
    agent, make_user, join, accessioning, category
):
    person = make_user("release_technician", "technical")
    join(person, accessioning)
    assigned = dict(team=accessioning, assignee=person)
    closing = set_status(new_request(agent, category, **assigned), Status.RESOLVED)
    still_open = new_request(agent, category, **assigned)
    before = [closing.revision, still_open.revision]

    def deactivate():
        account = User.objects.get(pk=person.pk)
        account.is_active = False
        account.changed_by = agent
        account.save()
        return account.released_assignments

    closer = Held(
        lambda: services.change_status(
            actor=agent, request=closing, expected_revision=closing.revision, status=Status.CLOSED
        )
    )
    save = Session(deactivate).start()
    try:
        state, _ = observe(save, closer.session)
    finally:
        assert closer.release() == "done"
        save.join()
    assert state == "waiting"
    assert (save.outcome, save.result) == ("done", 1), save.error
    closing.refresh_from_db()
    still_open.refresh_from_db()
    assert (closing.status, closing.assignee_id, closing.revision) == (
        Status.CLOSED,
        person.pk,
        before[0] + 1,
    )
    assert (still_open.assignee_id, still_open.revision) == (None, before[1] + 1)
    assert list(
        AuditEvent.objects.filter(
            action="request.assigned", reason="Account deactivated", actor=agent
        ).values_list("object_id", flat=True)
    ) == [str(still_open.pk)]


def test_scheduled_settings_wait_for_and_reread_a_deactivating_admin(make_user):
    admin = make_user("settings_admin", "admin")
    owner = make_user("settings_owner", "admin")
    row = JobSettings.objects.get_or_create(pk=1)[0]

    def deactivate():
        account = User.objects.get(pk=admin.pk)
        account.is_active = False
        account.changed_by = owner
        account.save(update_fields=["is_active"])

    other = Held(deactivate)
    save = Session(
        lambda: update_settings(
            actor=admin, expected_revision=row.revision, close_resolved_after_days=3
        )
    ).start()
    try:
        state, statement = observe(save, other.session)
    finally:
        assert other.release() == "done"
        save.join()
    assert state == "waiting" and "FOR SHARE" in statement
    assert save.outcome == "denied", save.error
    assert JobSettings.objects.get(pk=1).revision == row.revision
    assert not AuditEvent.objects.filter(action="jobs.settings_changed").exists()


@pytest.mark.parametrize("phase", ["preview", "commit"])
@pytest.mark.parametrize("slug", sorted(DIRECTORY_FILES))
def test_a_second_file_waits_before_its_first_organization_query(
    make_user, agent, clinic, phase, slug
):
    manager = make_user("second_importer", "account_manager")
    ExternalSystem.objects.get_or_create(
        key="lis", defaults={"name": "Example Laboratory System", "kind": "lis"}
    )
    first_args = dict(
        actor=agent,
        filename="other.csv",
        data=b"account_code,name\nEXOTHER,Example Separate Organization\n",
    )
    first_preview = importing.preview_file(importing.ORGANIZATIONS, **first_args)
    target = importing.BY_SLUG[slug]
    csv, model = DIRECTORY_FILES[slug]
    args = dict(actor=manager, data=csv.encode(), filename="second.csv")
    token = importing.preview_file(target, **args)
    assert first_preview.ok and token.ok
    count = model.objects.count()
    reads = []

    def observe_org_sql(execute, sql, params, many, context):
        if '"clients_client"' in sql.lower():
            reads.append(sql)
        return execute(sql, params, many, context)

    def second_file():
        with connection.execute_wrapper(observe_org_sql):
            if phase == "preview":
                return importing.preview_file(target, **args)
            return importing.commit_file(target, **args, reason="", state_token=token.state_token)

    first = Held(
        lambda: importing.commit_file(
            importing.ORGANIZATIONS, **first_args, reason="", state_token=first_preview.state_token
        )
    )
    second = Session(second_file).start()
    try:
        state, statement = observe(second, first.session)
        reads_before_release = len(reads)
    finally:
        assert first.release() == "done"
        second.join()
    assert second.outcome == "done", second.error
    assert second.result.ok and second.result.counts["create"] == 1
    assert model.objects.count() == count + (model is Client) + (phase == "commit")
    assert reads  # this case really uses the directory
    assert (state, reads_before_release) == ("waiting", 0), statement


@pytest.mark.parametrize(
    ("audience", "expected"),
    [
        ("no role", [403, 403, 403, 403]),
        ("outside team", [403, 403, 403, 403]),
        ("portal", [302, 403, 403, 404]),
        ("inactive", [302, 302, 302, 302]),
    ],
)
def test_direct_posts_refuse_before_a_busy_private_record(
    client, make_user, agent, clinic, privacy_category, audience, expected
):
    target = make_user("subject_account", "technical")
    subject = "Example restricted subject"
    ticket = new_request(agent, privacy_category, client=clinic, subject=subject)
    if audience == "portal":
        viewer = make_user("outside_reader", client=clinic)
        PortalAccess.objects.create(user=viewer, client=clinic, whole_organization=True)
    elif audience == "no role":
        viewer = make_user("outside_reader", is_staff=True)
    else:
        viewer = make_user("outside_reader", "technical", is_staff=True)
    client.force_login(viewer)
    if audience == "inactive":
        viewer.is_active = False
        viewer.changed_by = agent
        viewer.save(update_fields=["is_active"])

    def reserve_records():
        User.objects.select_for_update().get(pk=target.pk)
        Client.objects.select_for_update().get(pk=clinic.pk)
        type(ticket).objects.select_for_update().get(pk=ticket.pk)

    def visit():
        responses = [
            client.post(reverse("admin:accounts_user_change", args=[target.pk]), {}),
            client.post(reverse("clients:client-edit", args=[clinic.pk]), {}),
            client.post(reverse("dataimport:upload", args=["organizations"]), {"step": "preview"}),
            client.post(
                reverse("portal:request-reply", args=[ticket.number]),
                {
                    "body": "An update on order ORD-TEST-000842.",
                    "expected_revision": ticket.revision,
                },
            ),
        ]
        assert all(subject.encode() not in response.content for response in responses)
        return [response.status_code for response in responses]

    other = Held(reserve_records)
    posts = Session(visit).start()
    try:
        state, statement = observe(posts, other.session)
    finally:
        assert other.release() == "done"
        posts.join()
    assert state == "ended", statement
    assert posts.outcome == "done", posts.error
    assert posts.result == expected
    assert not RequestEntry.objects.filter(body="An update on order ORD-TEST-000842.").exists()
