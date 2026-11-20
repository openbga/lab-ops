"""Verify the round-one resolutions through account, directory, job and ticket workflows."""

import logging
from datetime import timedelta
from html import escape
from unittest.mock import patch

import pytest
from allauth.account.adapter import get_adapter
from django.contrib.auth.models import Group, Permission, update_last_login
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, OperationalError, connection, transaction
from django.forms import models as model_forms
from django.test import Client as HttpClient
from django.test.html import parse_html
from django.urls import reverse
from django.utils import timezone

from accounts import admin as account_admin
from accounts.models import User
from accounts.setup import setup_lab
from attachments.models import Attachment, AttachmentBlob
from clients import services as directory
from clients.models import Client, PortalAccess
from clients.tests.test_views import edit_data
from core.models import AuditEvent
from dataimport import importing
from desk import notifications, services
from desk.models import EntryKind, RequestEntry, Status
from jobs import outbox
from jobs.models import JobSettings, Notice, NoticeStatus, NotSentReason
from jobs.services import update_settings
from service_levels import jobs as clock_jobs
from service_levels.models import RequestClock, ServiceTarget

from .conftest import PASSWORD
from .test_contract import new_request, set_status
from .test_portal import main as main_content
from .test_review_acting_account_until_commit import Gate, _admin_change_data, gated
from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by, _statement

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def verify_editor(make_user):
    return make_user("verify_account_editor", is_staff=True, is_superuser=True)


def account_url(account):
    return reverse("admin:accounts_user_change", args=[account.pk])


def rename_group(http, group, title):
    url = reverse("admin:auth_group_change", args=[group.pk])
    response = http.post(url, {"name": title, "permissions": []})
    assert response.status_code == 302


def test_account_drawn_with_a_group_name_that_changed_prompts_to_reopen(
    client, verify_editor, make_user
):
    account = make_user("verify_group_label")
    group = Group.objects.create(name="Thursday planning")
    account.groups.add(group)
    client.force_login(verify_editor)
    assert group.name.encode() in client.get(account_url(account)).content
    page = _admin_change_data(client, account, first_name="Riley")
    assert page["groups"] == [str(group.pk)]
    # The group administration supports renaming custom groups. This one grants no permissions.
    rename_group(client, group, "Friday planning")
    response = client.post(account_url(account), page)
    account.refresh_from_db()
    assert list(account.groups.values_list("name", flat=True)) == ["Friday planning"]
    assert (response.status_code, account.first_name) == (200, "")
    assert (
        account_admin.CHANGED_SINCE_DRAWN in response.context["adminform"].form.non_field_errors()
    )


def test_a_group_rename_is_attributed_only_to_its_editor(client, verify_editor, make_user):
    account = make_user("verify_attribution")
    group = Group.objects.create(name="Thursday meeting")
    account.groups.add(group)
    client.force_login(verify_editor)
    page = _admin_change_data(client, account, last_name="Updated")
    colleague = make_user("verify_label_editor", is_staff=True, is_superuser=True)
    other_client = HttpClient()
    other_client.force_login(colleague)
    save = Session(lambda: client.post(account_url(account), page))
    rename = Session(lambda: rename_group(other_client, group, "Friday meeting"))
    before_relations = Gate()
    with gated(account_admin.AccountAdmin, "save_related", {save.thread: before_relations}):
        save.start()
        try:
            assert before_relations.arrived.wait(5), save.error
            rename.start()
            state = rename.waiting_or_ended(seconds=5)
            assert state in {"ended", "waiting"}
            if state == "waiting":
                assert _blocked_by(rename.pid, save.pid)
        finally:
            before_relations.go.set()
            save.join()
            if rename.thread.ident is not None:
                rename.join()
    assert (save.outcome, rename.outcome) == ("done", "done"), (save.error, rename.error)
    group_event = AuditEvent.objects.get(action="group.updated", actor=colleague)
    assert (group_event.before["name"], group_event.after["name"]) == (
        "Thursday meeting",
        "Friday meeting",
    )
    if save.result.status_code == 200:
        assert save.result.context["adminform"].form.non_field_errors()
        assert not AuditEvent.objects.filter(action="account.updated", actor=verify_editor).exists()
    else:
        assert save.result.status_code == 302
        event = AuditEvent.objects.get(action="account.updated", actor=verify_editor)
        assert (event.before["last_name"], event.after["last_name"]) == ("", "Updated")
        assert event.before["groups"] == event.after["groups"], (
            event.before["groups"],
            event.after["groups"],
        )


def test_two_requests_to_add_the_same_membership_record_who_added_it(
    client, verify_editor, make_user
):
    # The admin role already includes technical work. Adding the explicit membership only
    # changes the recorded group list for this account.
    person = make_user("verify_setup_operator", "admin", is_staff=True, is_superuser=True)
    setup_lab(lab_name="Example laboratory", username=person.username)
    group = Group.objects.get(name="technical")
    client.force_login(verify_editor)
    data = _admin_change_data(client, person)
    data["groups"].append(str(group.pk))
    save = Session(lambda: client.post(account_url(person), data))
    setup = Session(lambda: setup_lab(username=person.username, roles=["technical"]))
    valid = Gate()
    with gated(account_admin.AccountAdmin, "save_model", {save.thread: valid}):
        save.start()
        try:
            assert valid.arrived.wait(5), save.error
            setup.start()
            state = setup.waiting_or_ended(seconds=5)
            assert state in {"waiting", "ended"}
            if state == "waiting":
                assert _blocked_by(setup.pid, save.pid)
        finally:
            valid.go.set()
            save.join()
            if setup.thread.ident is not None:
                setup.join()
    assert (save.outcome, setup.outcome) == ("done", "done"), (save.error, setup.error)
    assert person.groups.filter(pk=group.pk).exists()
    events = AuditEvent.objects.filter(
        action__in=["account.updated", "setup.account_roles_added"], object_id=str(person.pk)
    )
    additions = [
        (row.action, row.actor_id)
        for row in events
        if "technical" in set(row.after["groups"]) - set(row.before["groups"])
    ]
    assert len(additions) == 1, additions
    if state == "ended":
        assert additions == [("setup.account_roles_added", person.pk)]


def test_save_and_continue_shows_the_groups_and_permissions_the_page_changed(
    client, verify_editor, make_user
):
    person = make_user("verify_multiple_choices")
    old_group, new_group = (
        Group.objects.create(name=name) for name in ("Early office", "Late office")
    )
    old_permission, new_permission = (
        Permission.objects.get(content_type__app_label="auth", codename=name)
        for name in ("view_group", "view_permission")
    )
    person.groups.add(old_group)
    person.user_permissions.add(old_permission)
    client.force_login(verify_editor)
    data = _admin_change_data(
        client,
        person,
        groups=[str(new_group.pk)],
        user_permissions=[str(new_permission.pk)],
        _continue="Save and continue editing",
    )
    response = client.post(account_url(person), data, follow=True)
    assert response.redirect_chain == [(account_url(person), 302)]
    form = response.context["adminform"].form
    assert {str(pk) for pk in form["groups"].value()} == {str(new_group.pk)}
    assert {str(pk) for pk in form["user_permissions"].value()} == {str(new_permission.pk)}
    before, after = (
        AuditEvent.objects.filter(action="account.updated").values_list("before", "after").get()
    )
    assert (before["groups"], after["groups"]) == ([old_group.name], [new_group.name])
    assert (before["permissions"], after["permissions"]) == (
        ["auth.view_group"],
        ["auth.view_permission"],
    )
    assert "password" not in before and "password" not in after
    assert form["drawn"].value() == account_admin.drawn_state(User.objects.get(pk=person.pk))


def test_a_flag_changed_during_the_get_keeps_its_value(client, verify_editor, make_user):
    person = make_user("verify_drawn_flag")
    client.force_login(verify_editor)
    draw = Session(lambda: _admin_change_data(client, person, last_name="Proposed"))
    initial = Gate()
    change = None
    with gated(model_forms, "model_to_dict", {draw.thread: initial}, before=False):
        draw.start()
        try:
            assert initial.arrived.wait(5), draw.error
            change = Session(
                lambda: User.objects.filter(pk=person.pk).update(is_staff=True)
            ).start()
            state = change.waiting_or_ended(seconds=5)
            assert state in {"ended", "waiting"}
        finally:
            initial.go.set()
            draw.join()
            if change is not None:
                change.join()
    assert (draw.outcome, change.outcome) == ("done", "done"), (draw.error, change.error)
    assert "is_staff" not in draw.result
    response = client.post(account_url(person), draw.result)
    person.refresh_from_db()
    assert response.status_code == 200 and person.is_staff and person.last_name == ""
    assert (
        account_admin.CHANGED_SINCE_DRAWN in response.context["adminform"].form.non_field_errors()
    )


def test_retrying_an_abandoned_move_then_completing_it_records_only_the_real_move(
    agent, clinic_portal, clinic, water
):
    access = PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    clinic_portal.changed_by = agent
    clinic_portal.client = water
    clinic_portal.email = agent.email
    with pytest.raises(IntegrityError):
        clinic_portal.save(update_fields=["email", "client_id"])
    clinic_portal.refresh_from_db()
    clinic_portal.first_name = "Avery"
    clinic_portal.save(update_fields=["first_name"])
    access.refresh_from_db()
    assert access.client_id == clinic.pk and access.whole_organization
    clinic_portal.client = water
    clinic_portal.save(update_fields=["client"])
    access.refresh_from_db()
    assert access.client_id == water.pk and not access.whole_organization
    event = AuditEvent.objects.get(action="portal_access.reset")
    assert (event.before["client"], event.after["client"]) == (clinic.pk, water.pk)
    assert event.before["whole_organization"] and not event.after["whole_organization"]


def test_an_owner_replaced_and_deactivated_while_page_open_gets_the_reload_message(
    client, agent, make_user, clinic
):

    previous = make_user("previous_relationship_owner", "technical")
    successor = make_user("next_relationship_owner", "technical")
    manager = make_user("other_relationship_manager", "account_manager")
    clinic = directory.update_client(
        actor=agent,
        client_id=clinic.pk,
        expected_revision=clinic.revision,
        account_owner=previous,
        reason="Appointed",
    )
    client.force_login(agent)
    url = reverse("clients:client-edit", args=[clinic.pk])
    page = client.get(url)
    assert page.status_code == 200
    form = page.context["form"]
    submitted = edit_data(
        clinic,
        phone="555-0124",
        account_owner=form["account_owner"].value(),
        expected_revision=form["expected_revision"].value(),
    )

    def reassign():
        with transaction.atomic():
            directory.update_client(
                actor=manager,
                client_id=clinic.pk,
                expected_revision=clinic.revision,
                account_owner=successor,
                reason="Handover",
            )
            previous.is_active = False
            previous.changed_by = manager
            previous.save(update_fields=["is_active"])

    change = Session(reassign).start()
    assert change.join() == "done", change.error
    response = client.post(url, submitted)
    assert response.status_code == 200
    refused = response.context["form"]
    clinic.refresh_from_db()
    assert clinic.account_owner_id == successor.pk and clinic.phone != submitted["phone"]
    assert refused["phone"].value() == submitted["phone"]
    assert directory.CHANGED in refused.non_field_errors(), refused.errors


@pytest.mark.parametrize("phase", ["preview", "confirmation"])
def test_needed_owner_role_is_already_held_when_import_applies_the_rows(
    make_user, agent, clinic, phase
):
    """The owner's original technical role stays held. An unrelated group added later is
    incidental to the appointment and its removal should not delay applying the file."""

    owner = make_user("retained_staff_role", "technical")
    additional = Group.objects.create(name="Department newsletter")
    opts = dict(
        actor=agent,
        filename="offices.csv",
        data=(
            b"account_code,name,parent_account_code,account_owner\n"
            b"ORD-TEST-000211,Example first office,EXCLIN,\n"
            b"ORD-TEST-000212,Example second office,,retained_staff_role\n"
        ),
    )

    def preview():
        return importing.preview_file(importing.ORGANIZATIONS, **opts)

    def confirm():
        return importing.commit_file(
            importing.ORGANIZATIONS, **opts, reason="Appointed", state_token=token
        )

    token = preview().state_token if phase == "confirmation" else None
    worker = Session(confirm if phase == "confirmation" else preview)
    parsed = Gate()
    with gated(importing, "parse", {worker.thread: parsed}, before=False):
        worker.start()
        removal = None
        try:
            assert parsed.arrived.wait(timeout=5), worker.error
            with pytest.raises(OperationalError) as busy, transaction.atomic():
                Client.objects.select_for_update(nowait=True).get(pk=clinic.pk)
            assert busy.value.__cause__.sqlstate == "55P03"
            # The original technical role stays in place. Change an unrelated group's
            # membership after the import already holds the parent.
            owner.groups.add(additional)
            removal = Held(lambda: owner.groups.remove(additional))
            parsed.go.set()
            state = worker.waiting_or_ended(seconds=5)
            if state == "waiting":
                assert _blocked_by(worker.pid, removal.session.pid)
                statement = _statement(worker.pid)
            else:
                statement = ""
        finally:
            parsed.go.set()
            if removal is not None:
                removal.release()
            worker.join()
    assert worker.outcome == "done", worker.error
    assert removal.session.outcome == "done", removal.session.error
    assert worker.result.ok, [row.errors for row in worker.result.rows]
    assert not owner.groups.filter(pk=additional.pk).exists()
    assert Client.objects.filter(
        account_number="ORD-TEST-000212", account_owner=owner
    ).exists() == (phase == "confirmation")
    assert state == "ended", statement


def test_import_confirmation_refuses_an_owner_username_now_held_by_another_account(
    agent, make_user
):

    first = make_user("regional_contact", "technical")
    second = make_user("incoming_contact", "technical")
    opts = dict(
        actor=agent,
        filename="regional-office.csv",
        data=(
            b"account_code,name,account_owner\n"
            b"ORD-TEST-000213,Example regional office,regional_contact\n"
        ),
    )
    old = importing.preview_file(importing.ORGANIZATIONS, **opts)
    assert old.ok and old.rows[0].bound["account_owner"] == first.pk

    def transfer_username():
        with transaction.atomic():
            first.username = "former_contact"
            first.save(update_fields=["username"])
            second.username = "regional_contact"
            second.save(update_fields=["username"])

    change = Session(transfer_username).start()
    assert change.join() == "done", change.error
    with pytest.raises(ValidationError, match="different from the preview"):
        importing.commit_file(
            importing.ORGANIZATIONS, **opts, reason="Assign", state_token=old.state_token
        )
    assert not Client.objects.filter(account_number="ORD-TEST-000213").exists()
    fresh = importing.preview_file(importing.ORGANIZATIONS, **opts)
    assert fresh.ok and fresh.rows[0].bound["account_owner"] == second.pk
    importing.commit_file(
        importing.ORGANIZATIONS, **opts, reason="Assign", state_token=fresh.state_token
    )
    assert Client.objects.get(account_number="ORD-TEST-000213").account_owner_id == second.pk


@pytest.mark.parametrize("role", ["technical", "roleless", "portal"])
def test_form_with_an_inactive_owner_still_requires_directory_management(
    client, agent, make_user, clinic, role
):

    owner = make_user("former_directory_holder", "technical")
    clinic = directory.update_client(
        actor=agent,
        client_id=clinic.pk,
        expected_revision=clinic.revision,
        account_owner=owner,
        reason="Appointed",
    )
    owner.is_active = False
    owner.changed_by = agent
    owner.save(update_fields=["is_active"])
    roles = ("technical",) if role == "technical" else ()
    person = make_user("directory_reader", *roles, client=clinic if role == "portal" else None)
    client.force_login(person)
    url = reverse("clients:client-edit", args=[clinic.pk])
    for response in (client.get(url), client.post(url, edit_data(clinic, phone="555-0127"))):
        assert response.status_code == 403
        assert owner.username.encode() not in response.content
    clinic.refresh_from_db()
    assert clinic.account_owner_id == owner.pk and clinic.phone != "555-0127"


def test_replacement_owner_lookup_offers_only_active_accounts_with_a_laboratory_role(
    client, agent, make_user, clinic
):

    former = make_user("relationship_previous", "technical")
    eligible = make_user("relationship_current", "technical")
    roleless = make_user("relationship_without_role")
    clinic = directory.update_client(
        actor=agent,
        client_id=clinic.pk,
        expected_revision=clinic.revision,
        reason="Appointed",
        account_owner=former,
    )
    former.is_active = False
    former.changed_by = agent
    former.save(update_fields=["is_active"])
    client.force_login(agent)
    url = reverse("clients:client-edit", args=[clinic.pk])
    page = client.get(url)
    assert page.status_code == 200
    select = parse_html(str(page.context["form"]["account_owner"]))
    lookup = dict(select.attributes)["data-vr-lookup"]
    response = client.get(lookup, {"q": "relationship_"})
    assert response.status_code == 200
    offered = {str(option["value"]) for option in response.json()["results"]}
    rejected = client.post(url, edit_data(clinic, account_owner=roleless.pk))
    assert rejected.status_code == 200 and rejected.context["form"].errors["account_owner"]
    clinic.refresh_from_db()
    assert clinic.account_owner_id == former.pk
    assert offered == {str(eligible.pk)}, response.json()["results"]


@pytest.fixture
def verify_breach(agent, accessioning, category, make_user, join, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    lead = make_user("verify_breach_lead", "technical")
    join(lead, accessioning, lead=True)
    ServiceTarget.objects.create(name="Verify target", resolution_minutes=60)
    ticket = new_request(agent, category, team=accessioning)
    # Delivery and recording both see an actually overdue ticket.
    RequestClock.objects.filter(request=ticket).update(
        resolution_due_at=timezone.now() - timedelta(minutes=6)
    )
    return ticket, lead


def test_first_breach_run_finishes_or_defers_while_the_auto_close_period_is_being_saved(
    verify_breach, make_user
):
    ticket, _lead = verify_breach
    admin = make_user("verify_period_admin", "admin")
    settings_row, _created = JobSettings.objects.get_or_create(pk=1)
    assert settings_row.system_account_id is None
    saving = Held(
        lambda: update_settings(
            actor=admin, expected_revision=settings_row.revision, close_resolved_after_days=12
        )
    )
    job = Session(clock_jobs.notice_breaches).start()
    try:
        state = job.waiting_or_ended(seconds=5)
        if state == "waiting":
            assert _blocked_by(job.pid, saving.session.pid)
    finally:
        assert saving.release() == "done", saving.session.error
        job.join()
    assert job.outcome == "done", job.error
    if RequestClock.objects.get(request=ticket).resolution_breach_noticed_at is None:
        assert clock_jobs.notice_breaches()["breaches"] == 1
    assert state == "ended", "first-ever breach sweep waited for the auto-close setting"


@pytest.mark.parametrize("change", ["notice preference", "own password"])
def test_account_changes_while_the_person_takes_a_ticket(
    change, client, agent, accessioning, category, make_user, join
):
    person = make_user("verify_take_member", "technical")
    join(person, accessioning)
    ticket = new_request(agent, category, team=accessioning)
    client.force_login(person)
    password = "violet woodland sparrow"

    def update():
        if change == "notice preference":
            return client.post(reverse("email-notices"), {})
        get_adapter().set_password(User.objects.get(pk=person.pk), password)

    taking = Held(
        lambda: services.take(actor=person, request=ticket, expected_revision=ticket.revision)
    )
    later = Session(update).start()
    try:
        state = later.waiting_or_ended(seconds=5)
        if state == "waiting":
            assert _blocked_by(later.pid, taking.session.pid)
    finally:
        assert taking.release() == "done", taking.session.error
        later.join()
    assert later.outcome == "done", later.error
    ticket.refresh_from_db()
    assert ticket.assignee_id == person.pk
    if change == "notice preference":
        assert later.result.status_code == 302
        assert not notifications.wants_notices(person)
    else:
        person.refresh_from_db()
        assert person.check_password(password)
    # Adapted (V8, resolved in docs/architecture.md): neither waits for the access gate, but each
    # writes the account's row (the setting page locks it, the password is an update of it) and
    # so waits a moment for the assignment, which holds that row; then it goes through.
    assert state == "waiting", f"{change} did not meet the assignment's row lock"


def test_breach_is_recorded_while_recipient_sign_in_finishes_and_delivers_on_retry(verify_breach):
    ticket, lead = verify_breach
    signing_in = Held(lambda: update_last_login(None, user=User.objects.get(pk=lead.pk)))
    job = Session(clock_jobs.notice_breaches).start()
    try:
        state = job.waiting_or_ended(seconds=5)
    finally:
        assert signing_in.release() == "done", signing_in.session.error
        job.join()
    assert state == "ended" and job.outcome == "done", job.error
    assert job.result == {"breaches": 1, "notices": 1}
    row = Notice.objects.get(topic=notifications.BREACH_TOPIC, object_id=ticket.pk)
    assert (row.recipient_id, row.status, row.attempts) == (lead.pk, NoticeStatus.WAITING, 0)
    assert not mail.outbox
    assert outbox.attempt(row.pk, now=row.next_attempt_at) == "sent"
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == [lead.email] and ticket.number in message.body
    assert ticket.subject not in message.body and "ORD-TEST-000042" not in message.body


def test_breach_delivery_defers_for_pending_opt_out_and_suppresses_on_retry(verify_breach, client):
    ticket, lead = verify_breach
    client.force_login(lead)
    with patch.object(outbox, "send_now", return_value={}):
        assert clock_jobs.notice_breaches()["notices"] == 1
    row = Notice.objects.get(topic=notifications.BREACH_TOPIC, object_id=ticket.pk)
    assert row.status == NoticeStatus.WAITING

    def opt_out():
        assert client.post(reverse("email-notices"), {}).status_code == 302

    saving = Held(opt_out)
    attempt = Session(lambda: outbox.attempt(row.pk)).start()
    try:
        state = attempt.waiting_or_ended(seconds=5)
    finally:
        assert saving.release() == "done", saving.session.error
        attempt.join()
    assert (state, attempt.outcome, attempt.result) == ("ended", "done", "busy"), attempt.error
    row.refresh_from_db()
    assert row.status == NoticeStatus.WAITING and row.attempts == 0
    assert outbox.attempt(row.pk, now=row.next_attempt_at) == "not_sent"
    row.refresh_from_db()
    assert row.not_sent_reason == NotSentReason.NO_LONGER_APPLIES and not mail.outbox


def test_checking_notice_setting_locks_outside_a_transaction_cannot_report_success(agent):
    hold = getattr(notifications, "try_hold_notice_settings", None)
    if hold is None:
        pytest.skip("notice setting lock was introduced in the resolution")
    assert connection.get_autocommit()
    try:
        held = hold([agent.pk])
    except RuntimeError:
        return  # a transactional caller is required, just as for the exclusive helper
    if not held:
        return

    def independent_setting_change():
        with transaction.atomic():
            notifications.lock_notice_setting(agent.pk)

    writer = Session(independent_setting_change).start()
    assert writer.join() == "done", writer.error
    pytest.fail("the helper reported success but an independent exclusive writer went through")


@pytest.fixture
def verify_portal_ticket(client, agent, sample_category, clinic, clinic_portal):
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    ticket = new_request(agent, sample_category, client=clinic)
    client.force_login(clinic_portal)
    detail_url = reverse("portal:request-detail", args=[ticket.number])
    page = client.get(detail_url)
    assert page.status_code == 200
    revision = page.context["reply_form"]["expected_revision"].value()
    return ticket, detail_url, page.context["reply_url"], revision


def test_open_ticket_stale_reply_can_follow_the_reload_and_send_instructions(
    client, agent, verify_portal_ticket
):
    """Follow the open-ticket Help: confirm the reload, then submit the form it renders."""
    ticket, detail_url, url, drawn_revision = verify_portal_ticket
    services.add_entry(
        actor=agent,
        request=ticket,
        kind=EntryKind.REPLY,
        body="We have checked the label.",
        expected_revision=drawn_revision,
    )
    body = "The label on ORD-TEST-000651 now matches."
    data = {"body": body, "expected_revision": drawn_revision}
    refused = client.post(url, data)
    assert refused.status_code == 200
    assert refused.context["reply_form"].errors
    refreshed = client.post(refused.wsgi_request.get_full_path(), data, follow=True)
    assert refreshed.status_code == 200
    form = refreshed.context["reply_form"]
    assert form["body"].value() == body
    sent = client.post(
        refreshed.context["reply_url"],
        {"body": form["body"].value(), "expected_revision": form["expected_revision"].value()},
    )
    # Opening the ticket separately also shows what can actually be recovered there.
    fresh_form = client.get(detail_url).context["reply_form"]
    assert RequestEntry.objects.filter(request=ticket, body=body).count() == 1, {
        "status_after_sending": sent.status_code,
        "original_revision": drawn_revision,
        "revision_after_reload": form["expected_revision"].value(),
        "fresh_get_revision": fresh_form["expected_revision"].value(),
        "fresh_get_draft": fresh_form["body"].value(),
    }


@pytest.mark.parametrize("audience", ["portal", "staff"])
def test_refused_ticket_composer_shows_its_own_help_and_literal_input(
    client, agent, verify_portal_ticket, audience
):
    ticket, _, reply_url, revision = verify_portal_ticket
    body = "Verify <tube> & label for ORD-TEST-000652."
    if audience == "staff":
        client.force_login(agent)
        url = reverse("desk:request-entry", args=[ticket.pk])
        form_name, page_id, expected_status = "composer", "desk-request-detail", 400
        data = {"kind": EntryKind.NOTE}
    else:
        url = reply_url
        form_name, page_id, expected_status = "reply_form", "portal-request-detail", 200
        data = {}
    # A previously displayed revision no longer matches; the ticket remains open.
    services.add_entry(
        actor=agent,
        request=ticket,
        kind=EntryKind.NOTE,
        body="Checked the label.",
        expected_revision=revision,
    )
    response = client.post(url, {**data, "body": body, "expected_revision": revision})
    assert response.status_code == expected_status
    assert response.context[form_name].errors
    assert escape(body).encode() in response.content and body.encode() not in response.content
    assert not RequestEntry.objects.filter(request=ticket, body=body).exists()
    assert "private" in response.headers["Cache-Control"]
    if audience == "portal":
        assert "no-store" in response.headers["Cache-Control"]
    assert f'data-vr-documentation-id="{page_id}"'.encode() in response.content


def test_closed_reply_with_whitespace_and_files_is_response_only(
    client, verify_portal_ticket, caplog, mailoutbox
):
    ticket, detail_url, url, revision = verify_portal_ticket
    set_status(ticket, Status.CLOSED)
    models = (Attachment, AttachmentBlob, RequestEntry, AuditEvent, Notice)
    before = [model.objects.count() for model in models]
    letters = len(mailoutbox)
    caplog.set_level(logging.INFO, logger="lab_ops.request")
    filename = "order-ORD-TEST-000653.txt"
    file_contents = b"ORD-TEST-000654"
    response = client.post(
        url,
        {
            "body": " \n ",
            "expected_revision": revision,
            "files": SimpleUploadedFile(filename, file_contents, "text/plain"),
        },
    )
    assert response.status_code == 200
    assert [model.objects.count() for model in models] == before
    assert len(mailoutbox) == letters
    assert "no-store" in response.headers["Cache-Control"]
    assert any(record.name == "lab_ops.request" for record in caplog.records)
    for marker in (filename, file_contents.decode()):
        assert marker not in caplog.text and marker not in response.wsgi_request.get_full_path()
        assert marker.encode() not in response.content
        assert marker not in str(dict(client.session))
    main = main_content(response)
    assert "Reply not sent" in main and "files included" in main
    assert 'id="id_body"' not in main and "New follow-up ticket" in main
    reopened = client.get(detail_url)
    assert reopened.status_code == 200 and "Reply not sent" not in main_content(reopened)


def test_waiting_opt_out_defers_another_breach_while_an_earlier_notice_is_sent(
    verify_breach, client, agent, category, accessioning
):
    """Three ordinary transactions: a delivery, the lead's preference save, and a new breach."""
    first, lead = verify_breach
    client.force_login(lead)
    with patch.object(outbox, "send_now", return_value={}):
        assert clock_jobs.notice_breaches()["notices"] == 1
    first_notice = Notice.objects.get(object_id=first.pk, topic=notifications.BREACH_TOPIC)
    next_ticket = new_request(agent, category, team=accessioning)
    RequestClock.objects.filter(request=next_ticket).update(
        resolution_due_at=timezone.now() - timedelta(minutes=3)
    )

    sending = Session(lambda: outbox.attempt(first_notice.pk))
    saving = Session(lambda: client.post(reverse("email-notices"), {}))
    sweep = Session(clock_jobs.notice_breaches)
    prepared = Gate()
    with gated(outbox.EmailMessage, "send", {sending.thread: prepared}):
        sending.start()
        try:
            assert prepared.arrived.wait(5), sending.error
            saving.start()
            assert saving.waiting_or_ended(seconds=5) == "waiting"
            assert _blocked_by(saving.pid, sending.pid)
            sweep.start()
            sweep_state = sweep.waiting_or_ended(seconds=5)
        finally:
            prepared.go.set()
            sending.join()
            if saving.thread.ident is not None:
                saving.join()
            if sweep.thread.ident is not None:
                sweep.join()
    assert (sending.outcome, saving.outcome, sweep.outcome) == ("done", "done", "done")
    assert sending.result == "sent" and saving.result.status_code == 302
    assert not notifications.wants_notices(lead)
    assert (sweep_state, sweep.result) == ("ended", {"breaches": 0, "notices": 0, "busy": 1})
    assert clock_jobs.notice_breaches() == {"breaches": 1, "notices": 0}
    assert not Notice.objects.filter(topic=notifications.BREACH_TOPIC, object_id=next_ticket.pk)
    assert len(mail.outbox) == 1 and first.number in mail.outbox[0].body


@pytest.mark.parametrize("setting", ["own password", "email notices"])
def test_person_finishes_the_account_change_while_their_note_is_still_open(
    setting, client, agent, category, accessioning, make_user, join, settings
):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    person = make_user("verify_note_writer", "technical")
    join(person, accessioning)
    ticket = new_request(agent, category, team=accessioning)
    client.force_login(person)
    phrase = "three synthetic orchard baskets"

    def update():
        if setting == "email notices":
            return client.post(reverse("email-notices"), {})
        return client.post(
            reverse("account_change_password"),
            {"oldpassword": PASSWORD, "password1": phrase, "password2": phrase},
        )

    note = Held(
        lambda: services.add_entry(
            actor=person,
            request=ticket,
            kind=EntryKind.NOTE,
            body="Verified the label.",
            expected_revision=ticket.revision,
        )
    )
    change = Session(update).start()
    try:
        state = change.waiting_or_ended(seconds=5)
        if state == "waiting":
            assert _blocked_by(change.pid, note.session.pid)
    finally:
        assert note.release() == "done", note.session.error
        change.join()
    assert (change.outcome, state) == ("done", "ended"), change.error
    assert change.result.status_code == 302
    if setting == "email notices":
        assert not notifications.wants_notices(person)
    else:
        person.refresh_from_db()
        assert person.check_password(phrase)
