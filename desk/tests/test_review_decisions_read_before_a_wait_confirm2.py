"""Second confirmation of account, directory, portal and scheduled-job behavior."""

from datetime import timedelta

import pytest
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from accounts import admin as account_admin
from accounts import setup
from attachments.models import Attachment
from clients.models import PortalAccess
from core.models import AuditEvent
from desk import services as portal_services
from desk.models import EntryKind, RequestEntry, Status
from jobs import actor as job_actor
from jobs.models import JobSettings
from jobs.services import update_settings
from service_levels import jobs as clock_jobs
from service_levels.models import RequestClock, ServiceTarget
from teams.models import Team

from .test_contract import new_request, set_status
from .test_review_acting_account_until_commit import Gate, _admin_change_data, _note, gated
from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by
from .test_review_decisions_read_before_a_wait_verify import account_url, rename_group

pytestmark = pytest.mark.django_db(transaction=True)


def _while_change_is_open(holder, work):
    second = Session(work).start()
    try:
        state = second.waiting_or_ended(seconds=5)
        assert state in {"ended", "waiting"}, second.error
        if state == "waiting":
            assert _blocked_by(second.pid, holder.session.pid)
    finally:
        assert holder.release() == "done", holder.session.error
        second.join()
    return state, second


def _setup_events(person):
    return AuditEvent.objects.filter(action="setup.account_roles_added", object_id=str(person.pk))


@pytest.mark.parametrize("role", ["technical", "admin"], ids=["addition", "already-held"])
def test_setup_adding_a_role_does_not_wait_for_the_persons_ordinary_ticket_note(
    make_user, category, role
):
    person = make_user("confirm_note_operator", "admin")
    setup.setup_lab(lab_name="Example laboratory", username=person.username)
    ticket = new_request(person, category)
    note = Held(lambda: _note(person, ticket, "The office confirmed the reference."))
    state, change = _while_change_is_open(
        note, lambda: setup.setup_lab(username=person.username, roles=[role])
    )
    assert change.outcome == "done", change.error
    assert person.groups.filter(name=role).exists()
    assert _setup_events(person).count() == int(role == "technical")
    # Adapted (D3, resolved in accounts/setup.py and the architecture): giving a role the account
    # lacks takes the account's access gate, as the account page does, so it waits for the
    # person's change under way; it does so before taking the lab's row (see the profile case),
    # and a role already held waits for nothing.
    expected = "waiting" if role == "technical" else "ended"
    assert state == expected, "setup and the operator's note did not take turns as documented"


def test_setup_role_event_uses_one_account_state_across_an_administrator_edit(client, make_user):
    person = make_user("confirm_setup_snapshot", "admin")
    setup.setup_lab(lab_name="Example laboratory", username=person.username)
    extra = Group.objects.create(name="Rota")
    editor = make_user("confirm_profile_editor", is_staff=True, is_superuser=True)
    client.force_login(editor)
    page = _admin_change_data(client, person, last_name="Lake")
    page["groups"].append(str(extra.pk))

    def edit():
        assert client.post(account_url(person), page).status_code == 302

    state, addition = _while_change_is_open(
        Held(edit), lambda: setup.setup_lab(username=person.username, roles=["technical"])
    )
    assert addition.outcome == "done", addition.error
    event = _setup_events(person).get()
    person.refresh_from_db()
    assert person.last_name == "Lake" and person.groups.filter(pk=extra.pk).exists()
    assert "technical" not in event.before["groups"] and "technical" in event.after["groups"]
    # The surname and the rota membership were changed in one transaction.
    for snapshot in (event.before, event.after):
        assert (snapshot["last_name"], "Rota" in snapshot["groups"]) in {
            ("", False),
            ("Lake", True),
        }, (state, snapshot["last_name"], snapshot["groups"])


def test_setup_rechecks_an_inactive_operator_after_its_no_change_request_waits(client, make_user):
    from django.core.exceptions import ValidationError

    person = make_user("confirm_unchanged_roles", "admin")
    setup.setup_lab(lab_name="Example laboratory", username=person.username)
    editor = make_user("confirm_inactive_editor", is_staff=True, is_superuser=True)
    client.force_login(editor)
    data = _admin_change_data(client, person, is_active=False)

    def deactivate():
        assert client.post(account_url(person), data).status_code == 302

    # The requested role is already present. The only result at issue is the command's
    # answer to a no-change request once the preceding account edit commits.
    state, repeat = _while_change_is_open(
        Held(deactivate), lambda: setup.setup_lab(username=person.username, roles=["admin"])
    )
    person.refresh_from_db()
    assert not person.is_active and set(person.groups.values_list("name", flat=True)) == {"admin"}
    assert not _setup_events(person).exists()
    if state == "waiting":
        assert isinstance(repeat.error, ValidationError), (state, repeat.outcome)
        assert "inactive" in str(repeat.error).lower()
    else:
        assert repeat.outcome == "done", repeat.error


@pytest.mark.parametrize("role", ["admin", "technical"], ids=["already-held", "addition"])
def test_group_editor_and_setup_each_get_credit_only_for_their_own_changes(client, make_user, role):
    person = make_user("confirm_rename_operator", "admin")
    setup.setup_lab(lab_name="Example laboratory", username=person.username)
    group = Group.objects.create(name="East office rota")
    person.groups.add(group)
    editor = make_user("confirm_rota_editor", is_staff=True, is_superuser=True)
    client.force_login(editor)
    adding = Session(lambda: setup.setup_lab(username=person.username, roles=[role]))
    rename = Session(lambda: rename_group(client, group, "West office rota"))
    read = Gate()
    # Adapted: setup now reads the account once, after deciding whether to hold it, and calls
    # account_snapshot only when it adds a role; pause it at that decision instead.
    with gated(setup, "_hold_for_roles", {adding.thread: read}, before=False):
        adding.start()
        try:
            assert read.arrived.wait(timeout=5), adding.error
            rename.start()
            assert rename.waiting_or_ended(seconds=5) in {"waiting", "ended"}
        finally:
            read.go.set()
            adding.join()
            if rename.thread.ident is not None:
                rename.join()
    assert (adding.outcome, rename.outcome) == ("done", "done"), (adding.error, rename.error)
    group_event = AuditEvent.objects.get(actor=editor, action="group.updated")
    assert (group_event.before["name"], group_event.after["name"]) == (
        "East office rota",
        "West office rota",
    )
    assert person.groups.filter(name=role).exists()
    if role == "admin":
        assert not _setup_events(person).exists(), "the existing membership was credited as new"
        assert not adding.result.changed
    else:
        event = _setup_events(person).get()
        assert set(event.after["groups"]) - set(event.before["groups"]) == {"technical"}
        assert not set(event.before["groups"]) - set(event.after["groups"])


def test_an_account_rejects_a_different_group_id_even_when_the_displayed_name_is_reused(
    client, make_user
):
    editor = make_user("confirm_desk_editor", is_staff=True, is_superuser=True)
    other_editor = make_user("confirm_memberships_editor", is_staff=True, is_superuser=True)
    account = make_user("confirm_rotas_member")
    original = Group.objects.create(name="East desk")
    account.groups.add(original)
    client.force_login(editor)
    saved = _admin_change_data(client, account, first_name="Taylor")
    other = HttpClient()
    other.force_login(other_editor)
    rename_group(other, original, "Former east desk")
    replacement = Group.objects.create(name="East desk")
    replacement_form = _admin_change_data(other, account, groups=[str(replacement.pk)])
    assert other.post(account_url(account), replacement_form).status_code == 302
    event = AuditEvent.objects.get(action="account.updated", actor=other_editor)
    assert (event.before["groups"], event.after["groups"]) == (["Former east desk"], ["East desk"])
    stale = client.post(account_url(account), saved)
    assert stale.status_code == 200
    assert account_admin.CHANGED_SINCE_DRAWN in stale.context["adminform"].form.non_field_errors()
    account.refresh_from_db()
    assert account.first_name == "" and list(account.groups.values_list("pk", flat=True)) == [
        replacement.pk
    ]


def test_invalid_organization_edit_compares_the_validated_revision_number(client, agent, clinic):
    """The integer used by a successful save has the same value on a field-error response."""
    from django.urls import reverse

    from clients import services as directory
    from clients.tests.test_views import edit_data

    client.force_login(agent)
    url = reverse("clients:client-edit", args=[clinic.pk])
    page = client.get(url)
    revision = int(page.context["form"]["expected_revision"].value())
    data = edit_data(
        clinic, email="not an email", phone="555-0164", expected_revision=f"{revision:03d}"
    )
    refused = client.post(url, data)
    assert refused.status_code == 200
    form = refused.context["form"]
    assert form.errors["email"]
    assert form.cleaned_data["expected_revision"] == revision
    assert form["phone"].value() == "555-0164"
    clinic.refresh_from_db()
    assert clinic.revision == revision and clinic.phone != "555-0164"
    # Correct the email alone. The service accepts exactly the same revision text.
    corrected = client.post(url, {**data, "email": "office@example.invalid"})
    assert corrected.status_code == 302
    clinic.refresh_from_db()
    assert (clinic.revision, clinic.phone) == (revision + 1, "555-0164")
    assert directory.CHANGED not in form.non_field_errors(), form.errors


@pytest.mark.parametrize("action", ["create", "edit"])
def test_fieldless_organization_submission_shows_required_fields_with_either_csrf_transport(
    agent, clinic, settings, action
):
    from django.test import Client as HttpClient
    from django.urls import reverse

    from clients.models import Client
    from core.models import AuditEvent

    http = HttpClient(enforce_csrf_checks=True, raise_request_exception=False)
    http.force_login(agent)
    url = reverse(f"clients:client-{action}", args=[clinic.pk] if action == "edit" else [])
    assert http.get(url).status_code == 200
    token = http.cookies[settings.CSRF_COOKIE_NAME].value
    counts = (Client.objects.count(), AuditEvent.objects.count(), clinic.revision)
    body_token = http.post(url, {"csrfmiddlewaretoken": token})
    header_token = http.post(url, {}, HTTP_X_CSRFTOKEN=token)
    assert body_token.status_code == 200
    assert body_token.context["form"].has_error("name", "required")
    clinic.refresh_from_db()
    assert (Client.objects.count(), AuditEvent.objects.count(), clinic.revision) == counts
    assert header_token.status_code == 200
    assert header_token.context["form"].has_error("name", "required")
    assert b"This field is required" in header_token.content


@pytest.mark.parametrize("which_organization", ["edited", "another"])
def test_invalid_organization_field_feedback_uses_the_revision_after_validation(
    client, agent, make_user, clinic, water, which_organization
):
    from django.urls import reverse

    from clients import services as directory
    from clients.forms import ClientEditForm
    from clients.tests.test_views import edit_data
    from desk.tests.test_review_acting_account_until_commit import Gate, gated
    from desk.tests.test_review_assignment_lock_order_verify3 import Session

    client.force_login(agent)
    url = reverse("clients:client-edit", args=[clinic.pk])
    page = client.get(url)
    data = edit_data(
        clinic,
        email="invalid email",
        notes="Keep these notes",
        expected_revision=page.context["form"]["expected_revision"].value(),
    )
    colleague = make_user("directory_colleague", "account_manager")
    edited = clinic if which_organization == "edited" else water
    original_revision = edited.revision
    save = Session(lambda: client.post(url, data))
    checked = Gate()
    with gated(ClientEditForm, "_post_clean", {save.thread: checked}, before=False):
        save.start()
        try:
            assert checked.arrived.wait(timeout=5), save.error
            directory.update_client(
                actor=colleague,
                client_id=edited.pk,
                expected_revision=original_revision,
                reason="Phone correction",
                phone="555-0165",
            )
        finally:
            checked.go.set()
            save.join()
    assert save.outcome == "done", save.error
    assert save.result.status_code == 200
    form = save.result.context["form"]
    assert form.errors["email"] and form["notes"].value() == data["notes"]
    assert list(form.non_field_errors()).count(directory.CHANGED) == int(
        which_organization == "edited"
    )
    clinic.refresh_from_db()
    edited.refresh_from_db()
    assert clinic.notes != data["notes"]
    assert (edited.phone, edited.revision) == ("555-0165", original_revision + 1)


@pytest.mark.parametrize("existing", [False, True], ids=["absent settings", "existing settings"])
def test_a_deferred_job_account_leaves_its_callers_transaction_usable(existing, make_user):
    admin = make_user("confirm_outer_admin", "admin")
    if existing:
        JobSettings.objects.create()
    assert job_actor.system_account_id() is None
    saving = Held(
        lambda: update_settings(actor=admin, expected_revision=1, close_resolved_after_days=14)
    )

    def try_account():
        with transaction.atomic():
            team = Team.objects.create(slug="confirmation-work", name="Confirmation work")
            actor = job_actor.system_account(nowait=True)
            # A failed inner NOWAIT must not poison the surrounding transaction.
            team.name = "Confirmation saved"
            team.save(update_fields=["name"])
            return actor

    state, attempt = _while_change_is_open(saving, try_account)
    assert (state, attempt.outcome, attempt.result) == ("ended", "done", None), attempt.error
    assert Team.objects.get(slug="confirmation-work").name == "Confirmation saved"
    assert JobSettings.objects.get().system_account_id is None
    assert not AuditEvent.objects.filter(action="jobs.system_account_created").exists()
    actor = job_actor.system_account(nowait=True)
    assert actor.pk == job_actor.system_account().pk
    assert AuditEvent.objects.filter(action="jobs.system_account_created").count() == 1


def test_busy_breach_count_spans_pages_and_counts_each_eligible_ticket_once(
    agent, category, accessioning, make_user, monkeypatch, settings
):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    monkeypatch.setattr(clock_jobs, "BREACH_BATCH", 1)
    ServiceTarget.objects.create(
        name="Confirmation targets", first_response_minutes=20, resolution_minutes=40
    )
    both, one, future, closed = [new_request(agent, category, team=accessioning) for _ in range(4)]
    now = timezone.now()
    past = now - timedelta(minutes=5)
    later = now + timedelta(minutes=10)
    RequestClock.objects.filter(request__in=[both, closed]).update(
        first_response_due_at=past, resolution_due_at=past, first_response_met_at=None
    )
    RequestClock.objects.filter(request=one).update(
        first_response_due_at=later, resolution_due_at=past
    )
    RequestClock.objects.filter(request=future).update(
        first_response_due_at=later, resolution_due_at=later
    )
    set_status(closed, Status.CLOSED)
    admin = make_user("confirm_queue_admin", "admin")
    JobSettings.objects.create()
    saving = Held(
        lambda: update_settings(actor=admin, expected_revision=1, close_resolved_after_days=8)
    )
    state, attempt = _while_change_is_open(saving, lambda: clock_jobs.notice_breaches(now=now))
    assert (state, attempt.outcome) == ("ended", "done"), attempt.error
    assert attempt.result == {"busy": 2, "breaches": 0, "notices": 0}
    assert clock_jobs.notice_breaches(now=now) == {"breaches": 3, "notices": 0}
    assert clock_jobs.notice_breaches(now=now) == {"breaches": 0, "notices": 0}


@pytest.fixture
def confirm_two_portal(client, agent, category, clinic, clinic_portal):
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    ticket = new_request(agent, category, client=clinic)
    client.force_login(clinic_portal)
    page = client.get(reverse("portal:request-detail", args=[ticket.number]))
    return (
        ticket,
        page.context["reply_url"],
        page.context["reply_form"]["expected_revision"].value(),
    )


def _confirm_two_list_ticket(client, response, ticket):
    """Follow the Tickets breadcrumb, then the link displayed for this ticket."""
    listing = response.context["list_url"]
    assert f'href="{listing}"'.encode() in response.content
    result = client.get(listing)
    assert result.status_code == 200
    detail = reverse("portal:request-detail", args=[ticket.number])
    assert f'href="{detail}">{ticket.number}</a>'.encode() in result.content
    return detail


def _confirm_two_staff_update(agent, ticket, text):
    ticket.refresh_from_db()
    return portal_services.add_entry(
        actor=agent,
        request=ticket,
        kind=EntryKind.REPLY,
        body=text,
        expected_revision=ticket.revision,
    )


def _confirm_two_fresh_send(client, page, text, files=None):
    fields = {
        "body": text,
        "expected_revision": page.context["reply_form"]["expected_revision"].value(),
    }
    if files is not None:
        fields["files"] = files
    return client.post(page.context["reply_url"], fields)


def _confirm_two_upload():
    return SimpleUploadedFile("label-copy.txt", b"Please check ORD-TEST-000703.", "text/plain")


@pytest.mark.parametrize("reselect", [True, False], ids=["reselect-file", "text-only"])
def test_portal_recovers_draft_via_tickets_and_uses_only_the_new_file_selection(
    client, agent, confirm_two_portal, reselect
):
    ticket, url, revision = confirm_two_portal
    _confirm_two_staff_update(agent, ticket, "We checked the first label.")
    text = "Please check the second label for ORD-TEST-000704."
    refused = client.post(
        url, {"body": text, "expected_revision": revision, "files": _confirm_two_upload()}
    )
    assert refused.status_code == 200
    bound = refused.context["reply_form"]
    assert bound["files"].value() == ["label-copy.txt"] and bound.non_field_errors()
    copied = bound["body"].value()
    fresh = client.get(_confirm_two_list_ticket(client, refused, ticket))
    assert fresh.status_code == 200 and b"We checked the first label." in fresh.content
    assert not fresh.context["reply_form"]["body"].value()
    assert not fresh.context["reply_form"]["files"].value()
    assert (
        b'name="files-previous"' not in fresh.content and b'name="files-omit"' not in fresh.content
    )
    response = _confirm_two_fresh_send(
        client, fresh, copied, _confirm_two_upload() if reselect else None
    )
    assert response.status_code == 302
    saved = RequestEntry.objects.get(request=ticket, body=text)
    assert list(saved.attachments.values_list("original_name", flat=True)) == (
        ["label-copy.txt"] if reselect else []
    )


def test_portal_refusal_message_names_a_working_way_to_reopen_the_ticket(
    client, agent, confirm_two_portal
):
    ticket, url, revision = confirm_two_portal
    _confirm_two_staff_update(agent, ticket, "The courier has arrived.")
    draft = "Please collect ORD-TEST-000705."
    data = {"body": draft, "expected_revision": revision}
    refused = client.post(url, data)
    assert refused.status_code == 200
    errors = " ".join(refused.context["reply_form"].non_field_errors()).lower()
    assert "changed" in errors
    if "reload" in errors and "tickets" not in errors:
        # Follow the visible form error. Confirming a reload repeats the POST that drew it.
        ready = client.post(refused.wsgi_request.get_full_path(), data, follow=True)
    else:
        ready = client.get(_confirm_two_list_ticket(client, refused, ticket))
    assert b"The courier has arrived." in ready.content
    sent = _confirm_two_fresh_send(client, ready, draft)
    assert (
        sent.status_code == 302
        and RequestEntry.objects.filter(request=ticket, body=draft).count() == 1
    ), (errors, ready.wsgi_request.method, ready.context["reply_form"]["expected_revision"].value())


def test_lost_reply_acknowledgment_and_documented_recovery_do_not_duplicate_delivery(
    client, confirm_two_portal
):
    ticket, url, revision = confirm_two_portal
    body = "Here is the corrected label for ORD-TEST-000706."
    original = {"body": body, "expected_revision": revision}
    sent = client.post(url, {**original, "files": _confirm_two_upload()})
    assert sent.status_code == 302
    # The previous response did not reach the user. Repeating its POST shows the stale-reply help.
    replay = client.post(url, {**original, "files": _confirm_two_upload()})
    assert replay.status_code == 200
    fields = replay.context["reply_form"]
    assert fields.non_field_errors()
    copies = RequestEntry.objects.filter(request=ticket, body=body)
    assert copies.count() == 1
    help_text = next(
        item["description"]
        for section in replay.context["page_documentation"]["sections"]
        for item in section.get("items", [])
        if item["label"] == "Ticket changed meanwhile"
    ).lower()
    # Follow the recovery path described by this revision's Help. A deliberate new reply
    # with identical content is legitimate. The lost-acknowledgment instruction should
    # tell someone who sees their original delivery when to stop.
    if "open the ticket again" in help_text:
        ready = client.get(_confirm_two_list_ticket(client, replay, ticket))
    else:
        ready = replay
    assert body.encode() in ready.content
    _confirm_two_fresh_send(client, ready, fields["body"].value(), _confirm_two_upload())
    if copies.count() > 1:
        assert Attachment.objects.filter(entry__in=copies).count() == 2
        stop_instructions = (
            "do not send",
            "don't send",
            "only send",
            "only resend",
            "no need to send",
            "do not resend",
            "don't resend",
            "already there, stop",
            "already there, you can stop",
        )
        assert any(phrase in help_text for phrase in stop_instructions), help_text


def test_account_event_uses_the_checked_names_for_both_sides_of_a_membership_switch(
    client, make_user
):
    member = make_user("confirm_two_names")
    original = Group.objects.create(name="Early counter")
    replacement = Group.objects.create(name="Late counter")
    member.groups.add(original)
    editor = make_user("confirm_account_names", is_staff=True, is_superuser=True)
    peer = make_user("confirm_label_names", is_staff=True, is_superuser=True)
    client.force_login(editor)
    second = HttpClient()
    second.force_login(peer)
    page = _admin_change_data(client, member, groups=[str(replacement.pk)])
    save = Session(lambda: client.post(account_url(member), page))
    checked = Gate()

    def change_labels():
        with transaction.atomic():
            rename_group(second, original, "First counter")
            rename_group(second, replacement, "Last counter")

    rename = Session(change_labels)
    with gated(account_admin.AccountAdmin, "save_model", {save.thread: checked}):
        save.start()
        try:
            assert checked.arrived.wait(5), save.error
            rename.start()
            assert rename.waiting_or_ended(seconds=5) in {"waiting", "ended"}
        finally:
            checked.go.set()
            save.join()
            if rename.thread.ident is not None:
                rename.join()
    assert (save.outcome, rename.outcome) == ("done", "done"), (save.error, rename.error)
    assert save.result.status_code == 302
    assert list(member.groups.values_list("name", flat=True)) == ["Last counter"]
    event = AuditEvent.objects.get(actor=editor, action="account.updated")
    assert (event.before["groups"], event.after["groups"]) == (["Early counter"], ["Late counter"])
    assert AuditEvent.objects.filter(actor=peer, action="group.updated").count() == 2


def test_blocking_settings_editor_recovers_from_a_rolled_back_first_actor_creation(make_user):
    admin = make_user("confirm_settings_editor", "admin")

    def abandoned():
        actor = job_actor.system_account()
        assert JobSettings.objects.get().system_account_id == actor.pk
        transaction.set_rollback(True)

    first = Held(abandoned)
    waiting = Session(
        lambda: update_settings(actor=admin, expected_revision=1, close_resolved_after_days=12)
    ).start()

    def check_busy():
        with transaction.atomic():
            job_actor.locked_settings(nowait=True)

    checking = Session(check_busy).start()
    try:
        edit_state = waiting.waiting_or_ended(seconds=5)
        probe_state = checking.waiting_or_ended(seconds=5)
        if edit_state == "waiting":
            assert _blocked_by(waiting.pid, first.session.pid)
    finally:
        assert first.release() == "done", first.session.error
        waiting.join()
        checking.join()
    assert (edit_state, waiting.outcome, waiting.result) == ("waiting", "done", True), waiting.error
    row = JobSettings.objects.get()
    assert row.close_resolved_after_days == 12 and row.system_account_id is None
    assert AuditEvent.objects.filter(action="jobs.settings_changed", actor=admin).count() == 1
    assert not AuditEvent.objects.filter(action="jobs.system_account_created").exists()
    assert job_actor.system_account(nowait=True).pk == job_actor.system_account().pk
    assert AuditEvent.objects.filter(action="jobs.system_account_created").count() == 1
    assert (probe_state, type(checking.error).__name__) == ("ended", "SettingsBusy")


def test_a_redundant_setup_request_does_not_delay_an_unrelated_laboratory_profile_edit(
    make_user, category
):
    from core.lab_profile import save_identity

    operator = make_user("confirm_setup_desk", "admin")
    colleague = make_user("confirm_office_profile", "admin")
    setup.setup_lab(lab_name="Example laboratory", username=operator.username)
    ticket = new_request(operator, category)
    note = Held(lambda: _note(operator, ticket, "Received the office update."))
    redundant = Session(
        lambda: setup.setup_lab(username=operator.username, roles=["admin"])
    ).start()
    edit = Session(lambda: save_identity(actor=colleague, values={"city": "Mapleton"}))
    try:
        setup_state = redundant.waiting_or_ended(seconds=5)
        if setup_state == "waiting":
            assert _blocked_by(redundant.pid, note.session.pid)
        edit.start()
        edit_state = edit.waiting_or_ended(seconds=5)
        if edit_state == "waiting":
            assert _blocked_by(edit.pid, redundant.pid)
    finally:
        assert note.release() == "done", note.session.error
        redundant.join()
        if edit.thread.ident is not None:
            edit.join()
    assert (redundant.outcome, edit.outcome) == ("done", "done"), (redundant.error, edit.error)
    assert not redundant.result.changed and edit.result[1]
    assert _setup_events(operator).count() == 0
    assert edit_state == "ended", (setup_state, edit_state)


@pytest.mark.parametrize("first", ["setup", "account page"])
def test_two_writers_of_one_membership_report_each_others_committed_result(
    client, make_user, first
):
    person = make_user("confirm_two_writers", "admin")
    setup.setup_lab(lab_name="Example laboratory", username=person.username)
    editor = make_user("confirm_other_writer", is_staff=True, is_superuser=True)
    client.force_login(editor)
    data = _admin_change_data(client, person)
    data["groups"].append(str(Group.objects.get(name="technical").pk))

    def from_setup():
        return setup.setup_lab(username=person.username, roles=["technical"])

    def from_page():
        return client.post(account_url(person), data)

    if first == "setup":
        state, later = _while_change_is_open(Held(from_setup), from_page)
        assert (state, later.outcome) == ("waiting", "done"), later.error
        assert later.result.status_code == 200
        assert (
            account_admin.CHANGED_SINCE_DRAWN
            in later.result.context["adminform"].form.non_field_errors()
        )
        expected_action, expected_actor = "setup.account_roles_added", person.pk
    else:

        def edited():
            assert from_page().status_code == 302

        state, later = _while_change_is_open(Held(edited), from_setup)
        assert (state, later.outcome) == ("waiting", "done"), later.error
        assert not later.result.changed
        expected_action, expected_actor = "account.updated", editor.pk
    assert person.groups.filter(name="technical").exists()
    event = AuditEvent.objects.filter(
        action__in=["setup.account_roles_added", "account.updated"], object_id=str(person.pk)
    ).get()
    assert (event.action, event.actor_id) == (expected_action, expected_actor)
    assert set(event.after["groups"]) - set(event.before["groups"]) == {"technical"}


def test_setup_command_reports_only_new_roles_in_a_mixed_request(make_user):
    from io import StringIO

    from django.core.management import call_command

    person = make_user("confirm_grant_report", "admin")
    setup.setup_lab(lab_name="Example laboratory", username=person.username)
    output = StringIO()
    call_command(
        "setup_lab",
        "--admin-username",
        person.username,
        "--role",
        "admin",
        "--role",
        "technical",
        stdout=output,
    )
    event = _setup_events(person).get()
    added = set(event.after["groups"]) - set(event.before["groups"])
    assert added == {"technical"}
    line = next(line for line in output.getvalue().splitlines() if line.startswith("Added roles"))
    reported = set(line.split(": ", 1)[1].rstrip(".").split(", "))
    assert reported == added, line
