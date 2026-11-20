"""Decisions read before a wait (roadmap: "Review follow-up: decisions read before a wait").

Five changes decided from what they read before waiting for another change, and then saved. Each
now decides from what it holds:

- R5 the account page refuses a save when the account changed after the page was drawn, and
  writes only the columns it edits;
- R7 an organization's new owner is held and checked as it stands, before the organization's
  row, also by an import naming owners on several rows;
- R9 the breach notice job holds the access of the people it may tell before reading who they
  are;
- R10 a portal reply refused because the ticket was closed shows the typed text again;
- R11 finishing a sign-in never compares the account's organization with a copy read earlier.

The first test of each group is the independent review's acceptance case (round 1 of the
assignee and group deadlocks review), adopted as written; the others are its neighbours.
"""

from datetime import timedelta

import pytest
from django.contrib.auth.models import Group, update_last_login
from django.db import transaction
from django.test import Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from accounts.admin import CHANGED_SINCE_DRAWN, drawn_state
from accounts.models import User
from clients import services as directory
from clients.models import Client, PortalAccess
from clients.tests.test_views import edit_data
from core.models import AuditEvent
from dataimport import importing
from desk import notifications, services
from desk.models import RequestEntry, ServiceRequest, Status
from jobs.models import Notice
from service_levels import jobs as clock_jobs
from service_levels.models import RequestClock, ServiceTarget
from teams.services import remove_member

from .test_contract import new_request, set_status
from .test_review_acting_account_until_commit import Gate, _admin_change_data, gated
from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by, _statement

pytestmark = pytest.mark.django_db(transaction=True)


def observe(worker, holder):
    """Sample the blocker while both sessions still own their transactions."""
    state = worker.waiting_or_ended(seconds=5)
    if state == "waiting":
        assert _blocked_by(worker.pid, holder.pid)
        return state, _statement(worker.pid)
    return state, ""


def deactivate(user, by):
    def work():
        account = User.objects.get(pk=user.pk)
        account.is_active = False
        account.changed_by = by
        account.save(update_fields=["is_active"])

    return work


# R5: the account page saves only what it was drawn with ------------------------------------------


@pytest.fixture
def owner(make_user):
    return make_user("review_admin", "admin", is_staff=True, is_superuser=True)


ACCOUNT_URL = "admin:accounts_user_change"


@pytest.mark.parametrize("source", ["account page", "email field only"])
def test_account_deactivation_does_not_replace_a_more_recent_email_with_the_drawn_value(
    client, make_user, source
):
    owner = make_user("review_admin", "admin", is_staff=True, is_superuser=True)
    account = make_user("review_technician", "technical")
    client.force_login(owner)
    form = _admin_change_data(client, account, is_active=False)
    url = reverse("admin:accounts_user_change", args=[account.pk])
    current_email = "new-office@example.invalid"

    editor = make_user("directory_admin", "admin", is_staff=True, is_superuser=True)
    if source == "account page":
        http = HttpClient()
        http.force_login(editor)
        correction = _admin_change_data(http, account, email=current_email)

    def correct_email():
        if source == "account page":
            assert http.post(url, correction).status_code == 302
            return
        current = User.objects.get(pk=account.pk)
        current.email = current_email
        current.changed_by = editor
        current.save(update_fields=["email"])

    first = Held(correct_email)
    save = Session(lambda: client.post(url, form)).start()
    try:
        state, _ = observe(save, first.session)
    finally:
        assert first.release() == "done"
        save.join()
    assert save.outcome == "done", save.error
    assert state == "waiting"
    account.refresh_from_db()
    saved_events = list(
        AuditEvent.objects.filter(
            action="account.updated", actor=owner, object_id=str(account.pk)
        ).values_list("before__email", "after__email")
    )
    assert account.email == current_email, (save.result.status_code, account.email, saved_events)
    if save.result.status_code == 200:  # a stale submission may also be refused as a whole
        assert account.is_active
        assert save.result.context["adminform"].form.errors
    else:
        assert save.result.status_code == 302 and not account.is_active


def _post(http, account, data):
    return http.post(reverse(ACCOUNT_URL, args=[account.pk]), data)


def _refused(response):
    assert response.status_code == 200
    errors = response.context["adminform"].form.non_field_errors()
    assert CHANGED_SINCE_DRAWN in errors, errors
    assert CHANGED_SINCE_DRAWN.encode() in response.content


@pytest.mark.parametrize(
    "change",
    ["email", "name", "role added", "role removed", "capability", "deactivated", "superuser"],
)
def test_a_page_drawn_before_any_change_it_shows_is_refused_and_records_nothing(
    client, owner, make_user, change
):
    account = make_user("drawn_technician", "technical")
    client.force_login(owner)
    form = _admin_change_data(client, account, first_name="Alex")
    current = User.objects.get(pk=account.pk)
    if change == "email":
        current.email = "corrected@example.invalid"
        current.save(update_fields=["email"])
    elif change == "name":
        current.last_name = "Example"
        current.save(update_fields=["last_name"])
    elif change == "role added":
        current.groups.add(Group.objects.get_or_create(name="supervisor")[0])
    elif change == "role removed":
        with transaction.atomic():
            current.groups.clear()
    elif change == "capability":
        from django.contrib.auth.models import Permission

        current.user_permissions.add(Permission.objects.get(codename="view_patient_information"))
    elif change == "deactivated":
        deactivate(account, owner)()
    else:
        current.is_superuser = True
        current.save(update_fields=["is_superuser"])
    before = User.objects.get(pk=account.pk)
    events = AuditEvent.objects.filter(action="account.updated").count()
    _refused(_post(client, account, form))
    after = User.objects.get(pk=account.pk)
    assert (after.first_name, after.email, after.is_active) == (
        before.first_name,
        before.email,
        before.is_active,
    )
    assert AuditEvent.objects.filter(action="account.updated").count() == events


def test_a_refused_page_stays_refused_and_a_page_opened_again_saves(client, owner, make_user):
    account = make_user("drawn_again", "technical")
    client.force_login(owner)
    stale = _admin_change_data(client, account, is_active=False)
    current = User.objects.get(pk=account.pk)
    current.email = "corrected-again@example.invalid"
    current.save(update_fields=["email"])
    refused = _post(client, account, stale)
    _refused(refused)
    # Sent again as shown, the page still carries the state it was drawn with.
    again = refused.context["adminform"].form["drawn"].value()
    assert again == stale["drawn"] != drawn_state(User.objects.get(pk=account.pk))
    _refused(_post(client, account, {**stale, "drawn": again}))
    fresh = _admin_change_data(client, account, is_active=False)
    assert _post(client, account, fresh).status_code == 302
    account.refresh_from_db()
    assert (account.is_active, account.email) == (False, "corrected-again@example.invalid")
    before, after = (
        AuditEvent.objects.filter(action="account.updated").values_list("before", "after").get()
    )
    assert before["email"] == after["email"] == "corrected-again@example.invalid"
    assert (before["is_active"], after["is_active"]) == (True, False)


def test_a_post_without_the_drawn_state_is_refused(client, owner, make_user):
    account = make_user("drawn_missing", "technical")
    client.force_login(owner)
    data = _admin_change_data(client, account, first_name="Sam")
    del data["drawn"]
    _refused(_post(client, account, data))
    assert User.objects.get(pk=account.pk).first_name == ""


def test_a_password_and_sign_in_recorded_while_the_page_was_open_are_kept(client, owner, make_user):
    """Neither is on the page: the save neither refuses for them nor writes them back."""
    account = make_user("drawn_password", "technical")
    client.force_login(owner)
    form = _admin_change_data(client, account, first_name="Robin")
    current = User.objects.get(pk=account.pk)
    current.set_password("a-new-synthetic-passphrase")
    current.save()
    update_last_login(None, user=User.objects.get(pk=account.pk))
    signed_in = User.objects.get(pk=account.pk).last_login
    assert _post(client, account, form).status_code == 302
    account.refresh_from_db()
    assert account.first_name == "Robin"
    assert account.check_password("a-new-synthetic-passphrase")
    assert account.last_login == signed_in


def test_a_password_change_under_way_is_kept_by_a_deactivation_that_waited(
    client, owner, make_user
):
    account = make_user("drawn_waiting_password", "technical")
    client.force_login(owner)
    form = _admin_change_data(client, account, is_active=False)

    def change_password():
        current = User.objects.get(pk=account.pk)
        current.set_password("another-synthetic-passphrase")
        current.save()  # a full save, as the password pages make: the access gate first

    first = Held(change_password)
    save = Session(lambda: _post(client, account, form)).start()
    try:
        state, _ = observe(save, first.session)
    finally:
        assert first.release() == "done"
        save.join()
    assert state == "waiting"
    assert save.outcome == "done", save.error
    assert save.result.status_code == 302
    account.refresh_from_db()
    assert not account.is_active
    assert account.check_password("another-synthetic-passphrase")


def test_last_login_and_date_joined_are_shown_and_never_saved_from_the_page(
    client, owner, make_user
):
    account = make_user("drawn_dates", "technical")
    update_last_login(None, user=account)
    account.refresh_from_db()
    client.force_login(owner)
    page = client.get(reverse(ACCOUNT_URL, args=[account.pk]))
    form = page.context["adminform"].form
    assert "last_login" not in form.fields and "date_joined" not in form.fields
    data = _admin_change_data(client, account, first_name="Kai")
    data.update(
        last_login_0="2001-01-01",
        last_login_1="00:00:00",
        date_joined_0="2001-01-01",
        date_joined_1="00:00:00",
    )
    assert _post(client, account, data).status_code == 302
    stored = User.objects.get(pk=account.pk)
    assert (stored.first_name, stored.last_login, stored.date_joined) == (
        "Kai",
        account.last_login,
        account.date_joined,
    )


def test_an_unchanged_page_still_saves_and_records_its_change(client, owner, make_user):
    account = make_user("drawn_fine", "technical")
    client.force_login(owner)
    data = _admin_change_data(client, account, last_name="Example")
    assert _post(client, account, data).status_code == 302
    event = AuditEvent.objects.get(action="account.updated", object_id=str(account.pk))
    assert (event.before["last_name"], event.after["last_name"]) == ("", "Example")


def test_two_owners_stepping_down_from_pages_drawn_at_once_keep_one_owner(client, make_user):
    first = make_user("owner_one", "admin", is_staff=True, is_superuser=True)
    second = make_user("owner_two", "admin", is_staff=True, is_superuser=True)
    one, two = HttpClient(), HttpClient()
    one.force_login(first)
    two.force_login(second)
    first_page = _admin_change_data(one, first, is_superuser=False)
    second_page = _admin_change_data(two, second, is_superuser=False)
    assert _post(one, first, first_page).status_code == 302
    response = _post(two, second, second_page)
    assert response.status_code == 200
    assert "Keep at least one active superuser" in str(
        response.context["adminform"].form.non_field_errors()
    )
    assert User.objects.get(pk=second.pk).is_superuser


@pytest.mark.parametrize("path", ["own password page", "account password page"])
def test_a_password_set_from_a_copy_read_before_a_deactivation_keeps_it(make_user, agent, path):
    """Both password pages set the password on the account as read when the request began. They
    save the password only, so a deactivation committed meanwhile stays."""
    from allauth.account.adapter import get_adapter

    from accounts.admin import AccountPasswordForm

    person = make_user("password_person", "technical")
    stale = User.objects.get(pk=person.pk)
    deactivate(person, agent)()
    password = "a-fresh-synthetic-passphrase"
    if path == "own password page":
        get_adapter().set_password(stale, password)
    else:
        form = AccountPasswordForm(
            stale, {"usable_password": "true", "password1": password, "password2": password}
        )
        assert form.is_valid(), form.errors
        form.save()
    stored = User.objects.get(pk=person.pk)
    assert not stored.is_active
    assert stored.check_password(password)


# R7: an organization's new owner as it stands ----------------------------------------------------


@pytest.mark.parametrize("action", ["create", "edit"])
def test_setting_an_owner_uses_their_eligibility_after_a_group_change(
    client, make_user, agent, clinic, action
):
    """Another manager finishes a group change and the chosen owner leaves before this save."""
    owner = make_user("directory_owner", "technical")
    other_group = Client.objects.create(name="Example Regional Group")
    member = Client.objects.create(name="Example Member")
    create = action == "create"
    if create:
        url = reverse("clients:client-create")
        data = {"name": "Example New Member", "account_number": "EXNEW"}
    else:
        url = reverse("clients:client-edit", args=[member.pk])
        data = edit_data(member, phone="555-0178")
    data.update(parent=clinic.pk, account_owner=owner.pk)
    client.force_login(agent)
    # On both revisions this operation holds the parent row our form will need.
    other = Held(
        lambda: directory.update_client(
            actor=agent,
            client_id=clinic.pk,
            parent=other_group,
            expected_revision=clinic.revision,
            reason="Joined the regional group",
        )
    )

    def deactivate_owner():
        current = User.objects.get(pk=owner.pk)
        current.is_active = False
        current.changed_by = agent
        current.save(update_fields=["is_active"])

    edit = Session(lambda: client.post(url, data)).start()
    departure = None
    try:
        state, _ = observe(edit, other.session)
        assert state == "waiting"
        departure = Session(deactivate_owner).start()
        departure_state, _ = observe(departure, edit)
    finally:
        assert other.release() == "done"
        edit.join()
        if departure is not None:
            departure.join()
    assert edit.outcome == departure.outcome == "done", (edit.error, departure.error)
    written = Client.objects.filter(account_owner=owner).count()
    if departure_state == "waiting":  # the appointment was serialized before deactivation
        assert (edit.result.status_code, written) == (302, 1)
        return
    assert departure_state == "ended"
    assert (edit.result.status_code, written) == (200, 0)
    form = edit.result.context["form"]
    assert "active laboratory staff account" in str(form.errors["account_owner"])
    assert form["name"].value() == data["name"]


def _appoint(action, actor, owner, organization=None):
    if action == "create":
        return lambda: directory.create_client(
            actor=actor, name="Example Owned Clinic", account_owner=owner
        )
    # A new name too, so that the save takes the directory lock first, as a group change does.
    return lambda: directory.update_client(
        actor=actor,
        client_id=organization.pk,
        expected_revision=organization.revision,
        reason="New account owner",
        name=f"{organization.name} (owned)",
        account_owner=owner,
    )


@pytest.mark.parametrize("action", ["create", "edit"])
def test_an_owner_whose_role_is_removed_while_the_save_waits_is_refused(
    make_user, agent, clinic, action
):
    owner = make_user("role_owner", "technical")
    member = Client.objects.create(name="Example Waiting Member")
    held_directory = Held(directory.hold_directory)  # the directory lock this save waits for

    def remove_role():
        with transaction.atomic():
            User.objects.get(pk=owner.pk).groups.clear()

    save = Session(_appoint(action, agent, owner, member)).start()
    try:
        state, _ = observe(save, held_directory.session)
        assert state == "waiting"
        removal = Session(remove_role).start()
        removal_state, _ = observe(removal, save)
    finally:
        assert held_directory.release() == "done"
        save.join()
        removal.join()
    assert removal.outcome == "done", removal.error
    assert removal_state == "ended"  # nothing held the owner yet
    assert save.outcome == "refused", save.error
    assert "active laboratory staff account" in str(save.error)
    assert not Client.objects.filter(account_owner=owner).exists()


@pytest.mark.parametrize("action", ["create", "edit"])
def test_a_deactivation_after_the_owner_is_held_waits_and_the_owner_is_saved(
    make_user, agent, clinic, action
):
    owner = make_user("held_owner", "technical")
    member = Client.objects.create(name="Example Held Member")
    holding, gates = Gate(), {}
    with gated(directory, "_held_owner", gates, before=False):
        save = Session(_appoint(action, agent, owner, member))
        gates[save.thread] = holding
        save.start()
        assert holding.arrived.wait(timeout=5), save.error
        departure = Session(deactivate(owner, agent)).start()
        try:
            state, statement = observe(departure, save)
        finally:
            holding.go.set()
            save.join()
            departure.join()
    assert state == "waiting", statement
    assert (save.outcome, departure.outcome) == ("done", "done"), (save.error, departure.error)
    assert Client.objects.filter(account_owner=owner).count() == 1
    assert not User.objects.get(pk=owner.pk).is_active


def test_keeping_an_owner_who_has_left_saves_other_details(agent, make_user, clinic):
    """The owner is checked when set or changed, not on every save of the organization."""
    owner = make_user("former_owner", "technical")
    clinic = directory.update_client(
        actor=agent,
        client_id=clinic.pk,
        expected_revision=clinic.revision,
        reason="Owner",
        account_owner=owner,
    )
    deactivate(owner, agent)()
    saved = directory.update_client(
        actor=agent,
        client_id=clinic.pk,
        expected_revision=clinic.revision,
        reason="New phone",
        account_owner=User.objects.get(pk=owner.pk),
        phone="555-0179",
    )
    assert (saved.phone, saved.account_owner_id) == ("555-0179", owner.pk)


def test_an_owner_written_around_the_services_meanwhile_is_refused_as_changed(
    agent, make_user, clinic
):
    """The save keeps its owner, so it holds none; a write that bypassed the services (no new
    revision) changes the stored owner while the save waits. Saving would then set an owner it
    did not hold."""
    first = make_user("first_owner", "technical")
    second = make_user("second_owner", "technical")
    clinic = directory.update_client(
        actor=agent,
        client_id=clinic.pk,
        expected_revision=clinic.revision,
        reason="Owner",
        account_owner=first,
    )
    waiting, gates = Gate(), {}
    with gated(directory, "hold_directory", gates, before=True):
        save = Session(
            lambda: directory.update_client(
                actor=agent,
                client_id=clinic.pk,
                expected_revision=clinic.revision,
                reason="Renamed",
                name="Example Renamed Clinic",
                account_owner=User.objects.get(pk=first.pk),
            )
        )
        gates[save.thread] = waiting
        save.start()
        try:
            assert waiting.arrived.wait(timeout=5), save.error
            Client.objects.filter(pk=clinic.pk).update(account_owner=second)  # no new revision
        finally:
            waiting.go.set()
            save.join()
    assert save.outcome == "refused", save.error
    assert directory.CHANGED in str(save.error)
    assert Client.objects.get(pk=clinic.pk).account_owner_id == second.pk


@pytest.mark.parametrize("first", ["ticket", "owner"])
def test_a_ticket_assigned_to_the_new_owner_and_the_appointment_never_deadlock(
    agent, make_user, join, accessioning, sample_category, clinic, first
):
    """A ticket created naming the owner as its assignee holds the account and then checks the
    organization at its COMMIT; the appointment holds the account before the organization."""
    owner = make_user("assigned_owner", "technical")
    join(owner, accessioning)
    appoint = _appoint("edit", agent, owner, clinic)

    def create_ticket():
        return new_request(agent, sample_category, client=clinic, assignee=owner)

    holder, other = (create_ticket, appoint) if first == "ticket" else (appoint, create_ticket)
    held = Held(holder)
    second = Session(other).start()
    try:
        state, _ = observe(second, held.session)
    finally:
        assert held.release() == "done"
        second.join()
    assert second.outcome == "done", second.error
    assert state in ("waiting", "ended")
    assert Client.objects.get(pk=clinic.pk).account_owner_id == owner.pk
    assert ServiceRequest.objects.filter(assignee=owner, client=clinic).count() == 1


def _owners_file(*rows):
    lines = ["account_code,name,account_owner", *(",".join(row) for row in rows)]
    return ("\n".join(lines) + "\n").encode()


def test_an_import_refuses_an_owner_deactivated_while_it_waited(agent, make_user, clinic):
    manager = make_user("owner_importer", "account_manager")
    owner = make_user("imported_owner", "technical")
    args = dict(
        actor=manager,
        filename="owners.csv",
        data=_owners_file(("EXCLIN", clinic.name, owner.username)),
    )
    preview = importing.preview_file(importing.ORGANIZATIONS, **args)
    assert preview.ok, [row.errors for row in preview.rows]
    held_directory = Held(directory.hold_directory)
    commit = Session(
        lambda: importing.commit_file(
            importing.ORGANIZATIONS, **args, reason="Owners", state_token=preview.state_token
        )
    ).start()
    try:
        state, _ = observe(commit, held_directory.session)
        assert state == "waiting"
        departure = Session(deactivate(owner, agent)).start()
        departure_state, _ = observe(departure, commit)
    finally:
        assert held_directory.release() == "done"
        commit.join()
        departure.join()
    assert departure_state == "ended"
    assert commit.outcome == "refused", commit.error
    assert Client.objects.get(pk=clinic.pk).account_owner_id is None


def test_an_import_holds_every_owner_before_its_first_organization(
    agent, make_user, join, accessioning, sample_category, clinic, water
):
    """Row one locks an organization; row two names an owner a new ticket is assigned to,
    whose COMMIT checks that organization. Held first, the import never meets it in a cycle."""
    manager = make_user("cycle_importer", "account_manager")
    owner = make_user("cycle_owner", "technical")
    join(owner, accessioning)
    clinic.account_number, water.account_number = "EXONE", "EXTWO"
    Client.objects.filter(pk=clinic.pk).update(account_number="EXONE")
    Client.objects.filter(pk=water.pk).update(account_number="EXTWO")
    args = dict(
        actor=manager,
        filename="two-owners.csv",
        data=_owners_file(
            ("EXONE", "Example Renamed Clinic", ""), ("EXTWO", water.name, owner.username)
        ),
    )
    preview = importing.preview_file(importing.ORGANIZATIONS, **args)
    assert preview.ok, [row.errors for row in preview.rows]
    held_rows, gates = Gate(), {}
    with gated(importing, "parse", gates, before=False):
        commit = Session(
            lambda: importing.commit_file(
                importing.ORGANIZATIONS, **args, reason="Owners", state_token=preview.state_token
            )
        )
        gates[commit.thread] = held_rows
        commit.start()
        assert held_rows.arrived.wait(timeout=5), commit.error
        ticket = Session(
            lambda: new_request(agent, sample_category, client=clinic, assignee=owner)
        ).start()
        try:
            state, statement = observe(ticket, commit)
        finally:
            held_rows.go.set()
            commit.join()
            ticket.join()
    assert state == "waiting", statement  # for the owner the file holds, not at its COMMIT
    assert (commit.outcome, ticket.outcome) == ("done", "done"), (commit.error, ticket.error)
    assert Client.objects.get(pk=water.pk).account_owner_id == owner.pk
    assert Client.objects.get(pk=clinic.pk).name == "Example Renamed Clinic"


# R9: the breach job holds whom it may tell before reading them ----------------------------------


def test_a_breach_records_only_the_recipients_read_under_the_job_locks(
    make_user, agent, category, accessioning, join, settings
):
    lead = make_user("clock_lead", "technical")
    join(lead, accessioning, lead=True)
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    ServiceTarget.objects.create(name="Review target", resolution_minutes=60)
    ticket = new_request(agent, category, team=accessioning)
    now = timezone.now() + timedelta(hours=2)
    job = Session(lambda: clock_jobs.notice_breaches(now=now))
    selected = Gate()

    def deactivate():
        account = User.objects.get(pk=lead.pk)
        account.is_active = False
        account.changed_by = agent
        account.save(update_fields=["is_active"])

    with gated(notifications, "breach_recipients", {job.thread: selected}, before=False):
        job.start()
        other = None
        try:
            assert selected.arrived.wait(timeout=5), job.error
            other = Session(deactivate).start()
            state, _ = observe(other, job)
        finally:
            selected.go.set()
            job.join()
            if other is not None:
                other.join()
    assert job.outcome == other.outcome == "done", (job.error, other.error)
    notices = Notice.objects.filter(recipient=lead, topic=notifications.BREACH_TOPIC)
    if state == "ended":  # if deactivation went first, the job must see its result
        assert not notices.exists(), list(notices.values_list("status", "not_sent_reason"))
        assert job.result["notices"] == 0
    else:
        assert state == "waiting"
        assert job.result["notices"] == 1
    assert RequestClock.objects.get(request=ticket).resolution_breach_noticed_at


@pytest.fixture
def breach(make_user, agent, category, accessioning, join, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    lead = make_user("breach_lead", "technical")
    join(lead, accessioning, lead=True)
    ServiceTarget.objects.create(name="Neighbour target", resolution_minutes=60)
    ticket = new_request(agent, category, team=accessioning)
    return lead, ticket, timezone.now() + timedelta(hours=2)


def _lead_notices(lead):
    return Notice.objects.filter(recipient=lead, topic=notifications.BREACH_TOPIC)


@pytest.mark.parametrize("change", ["deactivation", "role removed", "left the team"])
def test_an_access_change_under_way_leaves_the_breach_for_the_next_run(
    breach, agent, accessioning, make_user, change
):
    lead, ticket, now = breach
    supervisor = make_user("breach_supervisor", "supervisor")

    def take_access():
        if change == "deactivation":
            deactivate(lead, agent)()
        elif change == "role removed":
            User.objects.get(pk=lead.pk).groups.clear()
        else:
            remove_member(actor=supervisor, membership=accessioning.memberships.get(user=lead))

    other = Held(take_access)
    try:
        assert clock_jobs.notice_breaches(now=now) == {"breaches": 0, "notices": 0, "busy": 1}
    finally:
        assert other.release() == "done", other.session.error
    outcome = clock_jobs.notice_breaches(now=now)
    assert outcome["breaches"] == 1
    assert not _lead_notices(lead).exists()
    assert RequestClock.objects.get(request=ticket).resolution_breach_noticed_at


def test_a_lead_deactivated_before_the_run_is_not_told(breach, agent):
    lead, _ticket, now = breach
    deactivate(lead, agent)()
    assert clock_jobs.notice_breaches(now=now)["breaches"] == 1
    assert not _lead_notices(lead).exists()


def test_signing_in_or_an_assignment_does_not_hold_up_the_breach_job(
    breach, agent, accessioning, category
):
    """The job holds the access gates only: other changes of the account go on."""
    lead, ticket, now = breach
    other_ticket = new_request(agent, category, team=accessioning)
    job = Session(lambda: clock_jobs.notice_breaches(now=now))
    selected = Gate()
    with gated(notifications, "breach_recipients", {job.thread: selected}, before=True):
        job.start()
        try:
            assert selected.arrived.wait(timeout=5), job.error
            sign_in = Session(lambda: update_last_login(None, user=User.objects.get(pk=lead.pk)))
            assign = Session(
                lambda: services.assign(
                    actor=agent,
                    request=other_ticket,
                    expected_revision=other_ticket.revision,
                    team=accessioning,
                    assignee=lead,
                )
            )
            states = []
            for session in (sign_in, assign):
                session.start()
                states.append(observe(session, job)[0])
                session.join()
        finally:
            selected.go.set()
            job.join()
    assert states == ["ended", "ended"]
    assert (sign_in.outcome, assign.outcome, job.outcome) == ("done", "done", "done"), (
        sign_in.error,
        assign.error,
        job.error,
    )
    assert _lead_notices(lead).filter(object_id=ticket.pk).count() == 1


# R10: a reply refused on a closed ticket comes back ----------------------------------------------


def test_closing_an_assigned_ticket_while_the_portal_reply_waits_preserves_the_draft(
    client, make_user, agent, sample_category, accessioning, clinic, clinic_portal, join
):
    assignee = make_user("closing_assignee", "technical")
    join(assignee, accessioning)
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    ticket = set_status(
        new_request(agent, sample_category, client=clinic, team=accessioning, assignee=assignee),
        Status.RESOLVED,
    )
    body = "An update on order ORD-TEST-000839."
    client.force_login(clinic_portal)
    detail = client.get(reverse("portal:request-detail", args=[ticket.number]))
    form = detail.context["reply_form"]
    data = {"body": body, "expected_revision": form["expected_revision"].value()}
    before = ticket.revision
    closer = Held(
        lambda: services.change_status(
            actor=agent, request=ticket, expected_revision=before, status=Status.CLOSED
        )
    )
    reply = Session(lambda: client.post(detail.context["reply_url"], data, follow=True)).start()
    try:
        state, _ = observe(reply, closer.session)
    finally:
        assert closer.release() == "done"
        reply.join()
    assert reply.outcome == "done", reply.error
    assert state == "waiting"
    ticket.refresh_from_db()
    assert (ticket.status, ticket.revision) == (Status.CLOSED, before + 1)
    assert not RequestEntry.objects.filter(request=ticket, body=body).exists()
    assert reply.result.status_code == 200
    assert body.encode() in reply.result.content


@pytest.fixture
def portal_ticket(agent, sample_category, accessioning, clinic, clinic_portal):
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    ticket = set_status(
        new_request(agent, sample_category, client=clinic, team=accessioning), Status.RESOLVED
    )
    return ticket


def _draw_reply(client, clinic_portal, ticket, body):
    client.force_login(clinic_portal)
    detail = client.get(reverse("portal:request-detail", args=[ticket.number]))
    form = detail.context["reply_form"]
    return detail.context["reply_url"], {
        "body": body,
        "expected_revision": form["expected_revision"].value(),
    }


def test_a_reply_to_a_ticket_closed_before_it_was_sent_shows_the_text_again(
    client, agent, clinic_portal, portal_ticket
):
    body = "Please add order ORD-TEST-000850 to this ticket."
    url, data = _draw_reply(client, clinic_portal, portal_ticket, body)
    services.change_status(
        actor=agent,
        request=portal_ticket,
        expected_revision=portal_ticket.revision,
        status=Status.CLOSED,
    )
    response = client.post(url, data)
    assert response.status_code == 200
    assert not RequestEntry.objects.filter(request=portal_ticket, body=body).exists()
    page = response.content.decode()
    assert "Reply not sent" in page and "so nothing was sent" in page
    assert body in page
    assert response.context["reply_form"] is None
    unsent = response.context["unsent_reply"]
    assert unsent["body"].value() == body
    assert "readonly" in str(unsent["body"])
    assert "no-store" in response.headers.get("Cache-Control", "")  # the text may hold PHI
    follow_up = reverse("portal:request-create") + f"?follow_up={portal_ticket.number}"
    assert page.count(f'href="{follow_up}"') == 2  # the page action and the panel's own


def test_a_closed_ticket_shows_no_unsent_panel_without_a_refused_reply(
    client, agent, clinic_portal, portal_ticket
):
    url, data = _draw_reply(client, clinic_portal, portal_ticket, "   ")
    services.change_status(
        actor=agent,
        request=portal_ticket,
        expected_revision=portal_ticket.revision,
        status=Status.CLOSED,
    )
    for response in (
        client.post(url, data),
        client.get(reverse("portal:request-detail", args=[portal_ticket.number])),
    ):
        assert response.status_code == 200
        assert response.context["unsent_reply"] is None
        assert b"so nothing was sent" not in response.content  # the panel's notice (help aside)


def test_a_reply_refused_on_a_ticket_still_open_keeps_its_form(
    client, agent, clinic_portal, portal_ticket
):
    body = "Order ORD-TEST-000851 is also affected."
    url, data = _draw_reply(client, clinic_portal, portal_ticket, body)
    services.change_status(
        actor=agent,
        request=portal_ticket,
        expected_revision=portal_ticket.revision,
        status=Status.WAITING_CLIENT,
        reason="More detail needed",
    )
    response = client.post(url, data)
    assert response.status_code == 200
    assert response.context["unsent_reply"] is None
    form = response.context["reply_form"]
    assert form.errors and form["body"].value() == body
    assert not RequestEntry.objects.filter(request=portal_ticket, body=body).exists()


def test_another_organization_cannot_post_to_the_closed_ticket(
    client, make_user, agent, water, portal_ticket
):
    outsider = make_user("water_reader", client=water)
    PortalAccess.objects.create(user=outsider, client=water, whole_organization=True)
    services.change_status(
        actor=agent,
        request=portal_ticket,
        expected_revision=portal_ticket.revision,
        status=Status.CLOSED,
    )
    client.force_login(outsider)
    response = client.post(
        reverse("portal:request-reply", args=[portal_ticket.number]),
        {"body": "Synthetic text", "expected_revision": portal_ticket.revision + 1},
    )
    assert response.status_code == 404
    assert portal_ticket.subject.encode() not in response.content


# R11: finishing a sign-in ------------------------------------------------------------------------


def test_sign_in_timestamp_survives_a_concurrent_portal_move(agent, clinic_portal, clinic, water):
    access = PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    read = Gate()

    def finish_sign_in():
        user = User.objects.get(pk=clinic_portal.pk)
        read.stop()
        update_last_login(None, user=user)

    sign_in = Session(finish_sign_in).start()
    try:
        assert read.arrived.wait(timeout=5), sign_in.error
        clinic_portal.client = water
        clinic_portal.changed_by = agent
        clinic_portal.save(update_fields=["client"])
    finally:
        read.go.set()
        sign_in.join()
    clinic_portal.refresh_from_db()
    access.refresh_from_db()
    assert clinic_portal.client_id == access.client_id == water.pk
    assert not access.whole_organization
    assert AuditEvent.objects.filter(action="portal_access.reset").count() == 1
    assert sign_in.outcome == "done", sign_in.error
    assert clinic_portal.last_login is not None


def test_a_sign_in_during_a_portal_move_waits_and_keeps_the_move(
    agent, clinic_portal, clinic, water
):
    access = PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    stale = User.objects.get(pk=clinic_portal.pk)

    def move():
        account = User.objects.get(pk=clinic_portal.pk)
        account.client = water
        account.changed_by = agent
        account.save(update_fields=["client"])

    moving = Held(move)
    sign_in = Session(lambda: update_last_login(None, user=stale)).start()
    try:
        state, _ = observe(sign_in, moving.session)
    finally:
        assert moving.release() == "done"
        sign_in.join()
    assert state == "waiting"
    assert sign_in.outcome == "done", sign_in.error
    stored = User.objects.get(pk=clinic_portal.pk)
    access.refresh_from_db()
    assert (stored.client_id, access.client_id, stored.last_login is not None) == (
        water.pk,
        water.pk,
        True,
    )


def test_a_save_that_writes_the_organization_from_a_stale_copy_is_still_checked(
    clinic_portal, clinic, water
):
    """A save naming the organization compares it, as before: unattributed, it is refused."""
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    stale = User.objects.get(pk=clinic_portal.pk)
    stale.client = water
    with pytest.raises(Exception, match="has portal access"):
        stale.save(update_fields=["client", "last_login"])
    assert User.objects.get(pk=clinic_portal.pk).client_id == clinic.pk


def test_the_account_page_draws_a_fingerprint_without_secrets(client, owner, make_user):
    account = make_user("fingerprint_reader", "technical")
    client.force_login(owner)
    page = client.get(reverse(ACCOUNT_URL, args=[account.pk])).content.decode()
    token = drawn_state(account)
    assert f'name="drawn" value="{token}"' in page
    assert len(token) == 64 and account.password not in page
