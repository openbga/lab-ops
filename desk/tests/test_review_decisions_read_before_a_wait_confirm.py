"""Confirm account, directory, scheduled-job and portal decisions on synthetic records."""

from datetime import timedelta

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from accounts import admin as account_admin
from accounts.models import User
from attachments.models import Attachment
from clients.models import PortalAccess
from clients.services import set_portal_access
from core.models import AuditEvent
from desk import jobs as close_jobs
from desk import portal_views
from desk import services as confirm_desk
from desk.models import EntryKind, RequestEntry, ServiceRequest, Status
from jobs import actor as job_actor
from jobs.models import JobSettings
from jobs.services import update_settings
from jobs.views import counts_text
from service_levels import jobs as clock_jobs
from service_levels.models import RequestClock, ServiceTarget

from .test_contract import new_request, set_status
from .test_review_acting_account_until_commit import Gate, _admin_change_data, gated
from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def confirm_editor(make_user):
    return make_user("confirm_account_editor", is_staff=True, is_superuser=True)


def confirm_account_page(user):
    return reverse("admin:accounts_user_change", args=[user.pk])


def confirm_rename(http, group, name):
    page = http.post(
        reverse("admin:auth_group_change", args=[group.pk]),
        {
            "name": name,
            "permissions": [],
        },
    )
    assert page.status_code == 302


def confirm_during(method_owner, method, operation, other, *, before=True):
    """Let a second normal operation run at this point in the first request."""
    first = Session(operation)
    second = Session(other)
    pause = Gate()
    with gated(method_owner, method, {first.thread: pause}, before=before):
        first.start()
        try:
            assert pause.arrived.wait(5), first.error
            second.start()
            assert second.waiting_or_ended(seconds=5) in {"waiting", "ended"}
        finally:
            pause.go.set()
            first.join()
            if second.thread.ident is not None:
                second.join()
    assert (first.outcome, second.outcome) == ("done", "done"), (first.error, second.error)
    return first.result


@pytest.mark.parametrize("point", ["save_model", "save_related"])
def test_replacing_a_membership_keeps_an_account_event_when_its_old_name_is_reused(
    client, confirm_editor, make_user, point
):
    account = make_user("confirm_membership_switch")
    old = Group.objects.create(name="Morning desk")
    new = Group.objects.create(name="Evening desk")
    account.groups.add(old)
    client.force_login(confirm_editor)
    submitted = _admin_change_data(client, account, groups=[str(new.pk)])
    colleague = make_user("confirm_group_editor", is_staff=True, is_superuser=True)
    other_client = HttpClient()
    other_client.force_login(colleague)

    def reuse_name():
        with transaction.atomic():
            confirm_rename(other_client, old, "Early desk")
            confirm_rename(other_client, new, "Morning desk")

    response = confirm_during(
        account_admin.AccountAdmin,
        point,
        lambda: client.post(confirm_account_page(account), submitted),
        reuse_name,
    )
    assert AuditEvent.objects.filter(action="group.updated", actor=colleague).count() == 2
    if response.status_code == 200:
        assert response.context["adminform"].form.non_field_errors()
        assert list(account.groups.values_list("pk", flat=True)) == [old.pk]
        return
    assert response.status_code == 302
    assert list(account.groups.values_list("pk", flat=True)) == [new.pk]
    events = AuditEvent.objects.filter(
        action="account.updated", object_id=str(account.pk), actor=confirm_editor
    )
    assert events.count() == 1, "membership changed, but its editor has no account audit event"
    event = events.get()
    assert event.before["groups"] != event.after["groups"]


@pytest.mark.parametrize("restore_old_name", [False, True], ids=["label-kept", "label-restored"])
def test_account_fingerprint_agrees_with_the_selected_label_in_the_response(
    client, confirm_editor, make_user, restore_old_name
):
    account = make_user("confirm_rendered_groups")
    group = Group.objects.create(name="Tuesday office")
    account.groups.add(group)
    client.force_login(confirm_editor)
    url = confirm_account_page(account)
    submitted = _admin_change_data(client, account, first_name="Robin")
    colleague = make_user("confirm_render_editor", is_staff=True, is_superuser=True)
    other_client = HttpClient()
    other_client.force_login(colleague)
    page = confirm_during(
        account_admin.AccountChangeForm,
        "__init__",
        lambda: client.get(url),
        lambda: confirm_rename(other_client, group, "Monday office"),
        before=False,
    )
    assert page.status_code == 200
    from django.test.html import parse_html

    def elements(parent, tag):
        for child in parent.children:
            if hasattr(child, "name"):
                if child.name == tag:
                    yield child
                yield from elements(child, tag)

    select = next(
        node
        for node in elements(parse_html(page.content.decode()), "select")
        if dict(node.attributes).get("name") == "groups"
    )
    selected = [node for node in elements(select, "option") if "selected" in dict(node.attributes)]
    assert len(selected) == 1 and dict(selected[0].attributes)["value"] == str(group.pk)
    visible = "".join(selected[0].children).strip()
    assert visible in ("Monday office", "Tuesday office"), visible
    submitted["drawn"] = page.context["adminform"].form["drawn"].value()
    if restore_old_name:
        confirm_rename(other_client, group, "Tuesday office")
    group.refresh_from_db()
    # Adapted (C4, resolved with V3 in the page help and architecture): the page saves which
    # roles the account holds, and a role renamed on its own page changes no account, so the
    # label shown, whichever reading it came from, never refuses the save.
    response = client.post(url, submitted)
    account.refresh_from_db()
    assert (response.status_code, account.first_name) == (302, "Robin"), (visible, group.name)


def test_save_and_continue_audits_the_pages_own_role_and_permission_differences_together(
    client, confirm_editor, make_user
):
    account = make_user("confirm_combined_changes")
    keep, drop, shared, new, separate = [
        Group.objects.create(name=f"Desk {n}") for n in ("one", "two", "three", "four", "five")
    ]
    account.groups.add(keep, drop)
    grants = list(Permission.objects.filter(content_type__app_label="auth").order_by("pk")[:5])
    assert len(grants) == 5
    account.user_permissions.add(*grants[:2])
    client.force_login(confirm_editor)
    submitted = _admin_change_data(
        client,
        account,
        _continue="Save and continue editing",
        groups=[str(g.pk) for g in (keep, shared, new)],
        user_permissions=[str(grants[i].pk) for i in (0, 2, 3)],
    )
    other_editor = make_user("confirm_separate_editor", is_staff=True, is_superuser=True)
    other_client = HttpClient()
    other_client.force_login(other_editor)

    def separate_changes():
        with transaction.atomic():
            confirm_rename(other_client, keep, "Desk renamed")
            account.groups.add(shared, separate)
            account.user_permissions.add(grants[2], grants[4])

    response = confirm_during(
        account_admin.AccountAdmin,
        "save_model",
        lambda: client.post(confirm_account_page(account), submitted, follow=True),
        separate_changes,
    )
    assert response.status_code == 200 and response.redirect_chain
    form = response.context["adminform"].form
    assert set(map(int, form["groups"].value())) == {keep.pk, shared.pk, new.pk, separate.pk}
    assert set(map(int, form["user_permissions"].value())) == {grants[i].pk for i in (0, 2, 3, 4)}
    event = AuditEvent.objects.get(
        action="account.updated", actor=confirm_editor, object_id=str(account.pk)
    )
    assert set(event.before["groups"]) == {keep.name, drop.name}
    assert set(event.after["groups"]) == {keep.name, new.name}
    labels = [f"auth.{grant.codename}" for grant in grants]
    assert set(event.before["permissions"]) == {labels[0], labels[1]}
    assert set(event.after["permissions"]) == {labels[0], labels[3]}
    assert form["drawn"].value() == account_admin.drawn_state(User.objects.get(pk=account.pk))


def test_new_account_audit_and_first_edit_keep_both_selected_custom_groups(client, confirm_editor):
    first = Group.objects.create(name="Desk A")
    second = Group.objects.create(name="Desk B")
    client.force_login(confirm_editor)
    response = client.post(
        reverse("admin:accounts_user_add"),
        {
            "username": "confirm_new_staff",
            "email": "confirm-new@example.invalid",
            "usable_password": "false",
            "groups": [first.pk, second.pk],
            "_continue": "Save",
        },
        follow=True,
    )
    assert response.status_code == 200 and response.redirect_chain
    person = User.objects.get(username="confirm_new_staff")
    event = AuditEvent.objects.get(action="account.created", object_id=str(person.pk))
    assert event.actor_id == confirm_editor.pk and event.before == {}
    assert event.after["groups"] == [first.name, second.name]
    form = response.context["adminform"].form
    assert set(map(int, form["groups"].value())) == {first.pk, second.pk}
    assert form["drawn"].value() == account_admin.drawn_state(person)
    data = _admin_change_data(client, person, first_name="Alex")
    assert client.post(confirm_account_page(person), data).status_code == 302
    changed = AuditEvent.objects.get(action="account.updated", object_id=str(person.pk))
    assert changed.before["groups"] == changed.after["groups"] == [first.name, second.name]


def test_organization_edit_checks_the_revision_when_the_owner_leaves_during_field_validation(
    client, agent, make_user, clinic
):
    from django.db import transaction
    from django.urls import reverse

    from clients import services as directory
    from clients.forms import ClientEditForm
    from clients.tests.test_views import edit_data
    from desk.tests.test_review_acting_account_until_commit import Gate, gated
    from desk.tests.test_review_assignment_lock_order_verify3 import Session

    owner = make_user("prospective_owner", "technical")
    colleague = make_user("other_manager", "account_manager")
    client.force_login(agent)
    url = reverse("clients:client-edit", args=[clinic.pk])
    page = client.get(url)
    assert page.status_code == 200
    drawn = page.context["form"]
    assert drawn.fields["account_owner"].queryset.filter(pk=owner.pk).exists()
    data = edit_data(
        clinic,
        expected_revision=drawn["expected_revision"].value(),
        account_owner=owner.pk,
        notes="Please retain these notes",
    )
    checking = Gate()
    save = Session(lambda: client.post(url, data))
    with gated(ClientEditForm, "is_valid", {save.thread: checking}):
        save.start()
        try:
            assert checking.arrived.wait(timeout=5), save.error
            # The POST has read the organization but has not resolved the chosen owner yet.
            with transaction.atomic():
                directory.update_client(
                    actor=colleague,
                    client_id=clinic.pk,
                    expected_revision=clinic.revision,
                    reason="Phone corrected",
                    phone="555-0128",
                )
                owner.groups.clear()
        finally:
            checking.go.set()
            save.join()
    assert save.outcome == "done", save.error
    assert save.result.status_code == 200
    form = save.result.context["form"]
    clinic.refresh_from_db()
    assert (clinic.phone, clinic.account_owner_id, clinic.notes) == ("555-0128", None, "")
    assert form["notes"].value() == data["notes"]
    assert directory.CHANGED in form.non_field_errors(), form.errors


def test_an_empty_organization_edit_post_returns_the_edit_page(agent, clinic, settings):
    from django.test import Client as HttpClient
    from django.urls import reverse

    from core.models import AuditEvent

    http = HttpClient(enforce_csrf_checks=True, raise_request_exception=False)
    http.force_login(agent)
    url = reverse("clients:client-edit", args=[clinic.pk])
    assert http.get(url).status_code == 200
    original_revision, events = clinic.revision, AuditEvent.objects.count()
    response = http.post(url, {}, HTTP_X_CSRFTOKEN=http.cookies[settings.CSRF_COOKIE_NAME].value)
    clinic.refresh_from_db()
    assert clinic.revision == original_revision and AuditEvent.objects.count() == events
    assert response.status_code == 200


@pytest.mark.parametrize("phase", ["preview", "confirmation"])
def test_failed_import_row_releases_its_writes_but_keeps_the_next_rows_owner_valid(
    agent, make_user, monkeypatch, phase
):
    from django.core.exceptions import ValidationError

    from clients import services as directory
    from clients.models import Client
    from core.models import AuditEvent
    from dataimport import importing

    owner = make_user("shared_office_owner", "technical")
    args = dict(
        actor=agent,
        filename="offices.csv",
        data=(
            b"account_code,name,account_owner\n"
            b"ORD-TEST-000311,Example first office,shared_office_owner\n"
            b"ORD-TEST-000312,Example second office,shared_office_owner\n"
        ),
    )
    preview = importing.preview_file(importing.ORGANIZATIONS, **args)
    assert preview.ok
    original = directory.create_client
    completed_rows = []
    previous_row_absent = []
    initial_events = AuditEvent.objects.count()

    def fail_first_after_writing(**kwargs):
        record = original(**kwargs)
        assert record.account_owner_id == owner.pk
        completed_rows.append(record.account_number)
        if record.account_number == "ORD-TEST-000311":
            raise ValidationError("First row refused")
        previous_row_absent.append(
            not Client.objects.filter(account_number="ORD-TEST-000311").exists()
        )
        return record

    with monkeypatch.context() as changes:
        changes.setattr(directory, "create_client", fail_first_after_writing)
        if phase == "preview":
            refused = importing.preview_file(importing.ORGANIZATIONS, **args)
            assert refused.rows[0].errors == ["First row refused"]
            assert not refused.rows[1].errors
        else:
            with pytest.raises(ValidationError, match="Nothing was imported"):
                importing.commit_file(
                    importing.ORGANIZATIONS,
                    **args,
                    reason="Owners",
                    state_token=preview.state_token,
                )
    assert completed_rows == ["ORD-TEST-000311", "ORD-TEST-000312"]
    assert previous_row_absent == [True]
    assert not Client.objects.filter(account_owner=owner).exists()
    assert AuditEvent.objects.count() == initial_events
    owner.groups.clear()
    # A subsequent normal service call on this same connection must re-read the owner.
    with pytest.raises(ValidationError) as rejected:
        directory.create_client(actor=agent, name="Separate office", account_owner=owner)
    assert "account_owner" in rejected.value.message_dict


def test_owner_create_field_and_lookup_agree_for_staff_and_portal_audiences(
    client, agent, clinic, make_user
):
    from django.test import Client as HttpClient
    from django.test.html import parse_html
    from django.urls import reverse

    from clients.models import Client

    eligible = make_user("selected_owner", "supervisor", "technical", first_name="Lookuptest")
    no_role = make_user("ineligible_owner", first_name="Lookuptest")
    former = make_user("former_owner", "technical", first_name="Lookuptest", is_active=False)
    portal = make_user("portal_account", first_name="Lookuptest", client=clinic)
    administrator = make_user("deployment_owner_only", is_staff=True, is_superuser=True)
    manager = make_user("account_manager_reader", "account_manager")
    lab_admin = make_user("admin_reader", "admin")
    technical = make_user("technical_reader", "technical")
    supervisor = make_user("supervisor_reader", "supervisor")
    create_url = reverse("clients:client-create")
    client.force_login(agent)
    page = client.get(create_url)
    assert page.status_code == 200
    field = page.context["form"]["account_owner"]
    lookup_url = dict(parse_html(str(field)).attributes)["data-vr-lookup"]
    assert set(
        field.field.queryset.filter(first_name="Lookuptest").values_list("pk", flat=True)
    ) == {eligible.pk}
    for viewer in (agent, manager, lab_admin, technical, supervisor):
        http = HttpClient()
        http.force_login(viewer)
        lookup = http.get(lookup_url, {"q": "Lookuptest"})
        assert lookup.status_code == 200, viewer.username
        assert [str(row["value"]) for row in lookup.json()["results"]] == [str(eligible.pk)]
        assert http.get(create_url).status_code == (
            200 if viewer in (agent, manager, lab_admin) else 403
        )
    for viewer in (no_role, former, portal, administrator, None):
        http = HttpClient()
        if viewer is not None:
            http.force_login(viewer)
        for address in (lookup_url, create_url):
            response = http.get(address, {"q": "Lookuptest"})
            assert response.status_code in (302, 403)
            assert eligible.username.encode() not in response.content
    data = {"name": "Example new office", "account_number": "ORD-TEST-000313"}
    for excluded in (no_role, former, portal):
        rejected = client.post(create_url, {**data, "account_owner": excluded.pk})
        assert rejected.status_code == 200 and rejected.context["form"].errors["account_owner"]
        assert not Client.objects.filter(account_number=data["account_number"]).exists()
    assert client.post(create_url, {**data, "account_owner": eligible.pk}).status_code == 302
    assert Client.objects.get(account_number=data["account_number"]).account_owner_id == eligible.pk


@pytest.fixture
def confirm_due_clock(agent, category, accessioning, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    ServiceTarget.objects.create(name="Confirmation target", resolution_minutes=60)

    def make():
        ticket = new_request(agent, category, team=accessioning)
        RequestClock.objects.filter(request=ticket).update(
            resolution_due_at=timezone.now() - timedelta(minutes=2)
        )
        return ticket

    return make


def _confirm_job_meets_writer(writer, job):
    session = Session(job).start()
    try:
        state = session.waiting_or_ended(seconds=5)
        if state == "waiting":
            assert _blocked_by(session.pid, writer.session.pid)
    finally:
        assert writer.release() == "done", writer.session.error
        session.join()
    return state, session


@pytest.mark.parametrize("creator", ["first settings save", "first actor creation"])
def test_first_breach_run_defers_when_settings_are_being_created(
    confirm_due_clock, make_user, creator
):
    ticket = confirm_due_clock()
    assert not JobSettings.objects.exists()
    if creator == "first settings save":
        admin = make_user("confirm_period_admin", "admin")

        def create():
            return update_settings(actor=admin, expected_revision=1, close_resolved_after_days=9)
    else:
        create = job_actor.system_account

    state, job = _confirm_job_meets_writer(Held(create), clock_jobs.notice_breaches)
    assert job.outcome == "done", job.error
    # Whichever run records the clock, it does so once and both creators use the same account.
    counts = clock_jobs.notice_breaches()
    assert counts["breaches"] + job.result["breaches"] == 1
    assert JobSettings.objects.get().system_account_id is not None
    assert AuditEvent.objects.filter(action="jobs.system_account_created").count() == 1
    assert RequestClock.objects.get(request=ticket).resolution_breach_noticed_at
    assert state == "ended", f"the first breach sweep waited for the {creator}"


@pytest.mark.parametrize("occupied", ["username", "email"])
def test_an_account_name_being_registered_cannot_fail_the_first_breach_sweep(
    confirm_due_clock, occupied
):
    ticket = confirm_due_clock()
    settings_row, _ = JobSettings.objects.get_or_create(pk=1)
    assert settings_row.system_account_id is None

    def create_person():
        name = job_actor.USERNAME if occupied == "username" else "confirm_person"
        address = (
            f"{job_actor.USERNAME}@{job_actor.EMAIL_DOMAIN}"
            if occupied == "email"
            else "confirm-person@example.invalid"
        )
        User.objects.create_user(name, email=address, first_name="Morgan")

    state, job = _confirm_job_meets_writer(Held(create_person), clock_jobs.notice_breaches)
    # Check the subsequent run as well. It must find a distinct actor, leave the existing
    # person's details alone, and record the clock exactly once over both attempts.
    again = clock_jobs.notice_breaches()
    person = User.objects.get(first_name="Morgan")
    system = job_actor.system_account()
    assert system.pk != person.pk and person.is_active and not system.is_active
    assert person.username != system.username and person.email != system.email
    assert AuditEvent.objects.filter(action="jobs.system_account_created").count() == 1
    assert RequestClock.objects.get(request=ticket).resolution_breach_noticed_at
    assert job.outcome == "done", (state, job.error)
    assert again["breaches"] + job.result["breaches"] == 1


def test_first_breach_run_reports_how_many_tickets_it_left_for_the_next_run(
    confirm_due_clock, make_user
):
    tickets = [confirm_due_clock(), confirm_due_clock()]
    admin = make_user("confirm_count_admin", "admin")
    row, _ = JobSettings.objects.get_or_create(pk=1)
    assert row.system_account_id is None
    editor = Held(
        lambda: update_settings(
            actor=admin, expected_revision=row.revision, close_resolved_after_days=9
        )
    )
    _state, sweep = _confirm_job_meets_writer(editor, clock_jobs.notice_breaches)
    assert sweep.outcome == "done", sweep.error
    due = RequestClock.objects.filter(
        request__in=tickets, resolution_breach_noticed_at__isnull=True
    ).count()
    assert clock_jobs.notice_breaches()["breaches"] == due
    # The previous revision might process everything in its first run. If this one deferred,
    # the operator should see exactly the number of tickets the second run had to pick up.
    assert sweep.result.get("busy", 0) == due, (counts_text(sweep.result), due)


def test_first_auto_close_honours_the_new_period_after_a_settings_edit(
    agent, category, accessioning, make_user, settings
):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    admin = make_user("confirm_close_admin", "admin")
    status_row = JobSettings.objects.create(close_resolved_after_days=1)
    ticket = set_status(new_request(agent, category, team=accessioning), Status.RESOLVED)
    now = timezone.now()
    ServiceRequest.objects.filter(pk=ticket.pk).update(resolved_at=now - timedelta(days=2))
    editor = Held(
        lambda: update_settings(
            actor=admin, expected_revision=status_row.revision, close_resolved_after_days=7
        )
    )
    state, sweep = _confirm_job_meets_writer(editor, lambda: close_jobs.close_resolved_tickets(now))
    assert sweep.outcome == "done", sweep.error
    assert sweep.result["closed"] == 0
    ticket.refresh_from_db()
    assert ticket.status == Status.RESOLVED
    assert close_jobs.close_resolved_tickets(now + timedelta(days=6))["closed"] == 1
    ticket.refresh_from_db()
    assert ticket.status == Status.CLOSED
    assert AuditEvent.objects.filter(action="jobs.system_account_created").count() == 1
    assert state in {"waiting", "ended"}


@pytest.fixture
def confirmation_portal_reply(client, agent, category, clinic, clinic_portal):
    access = PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    ticket = new_request(agent, category, client=clinic)
    client.force_login(clinic_portal)
    shown = client.get(reverse("portal:request-detail", args=[ticket.number]))
    return (
        ticket,
        access,
        shown.context["reply_url"],
        shown.context["reply_form"]["expected_revision"].value(),
    )


def _confirmation_staff_reply(actor, ticket, body):
    ticket.refresh_from_db()
    return confirm_desk.add_entry(
        actor=actor,
        request=ticket,
        kind=EntryKind.REPLY,
        body=body,
        expected_revision=ticket.revision,
    )


def _confirmation_retry_fields(page):
    form = page.context["reply_form"]
    data = {"body": form["body"].value(), "expected_revision": form["expected_revision"].value()}
    if previous := form["files"].value():
        data["files-previous"] = previous
    return data


def _confirmation_attachment():
    return SimpleUploadedFile("review-label.txt", b"Order ORD-TEST-000741", "text/plain")


def test_replayed_delivered_reply_does_not_offer_to_send_the_same_reply_again(
    client, confirmation_portal_reply
):
    """The sender retries the same upload after losing its response, then follows the refusal."""
    ticket, _, url, revision = confirmation_portal_reply
    body = "Please use the attached label for ORD-TEST-000742."
    original = {"body": body, "expected_revision": revision}
    delivered = client.post(url, {**original, "files": _confirmation_attachment()})
    assert delivered.status_code == 302
    replay = client.post(url, {**original, "files": _confirmation_attachment()})
    entries = RequestEntry.objects.filter(request=ticket, body=body)
    uploads = Attachment.objects.filter(entry__in=entries, original_name="review-label.txt")
    assert (entries.count(), uploads.count()) == (1, 1)
    # If the response offers the kept draft again, use its fields and reselect the original file.
    if replay.status_code == 200 and replay.context["reply_form"] is not None:
        retry = _confirmation_retry_fields(replay)
        if retry["body"] == body:
            client.post(url, {**retry, "files": _confirmation_attachment()})
    assert (entries.count(), uploads.count()) == (1, 1)


def test_file_reselection_does_not_hide_a_second_ticket_change(
    client, agent, confirmation_portal_reply
):
    ticket, _, url, drawn = confirmation_portal_reply
    _confirmation_staff_reply(agent, ticket, "We found the label.")
    body = "Please check the label on ORD-TEST-000743."
    first = client.post(
        url, {"body": body, "expected_revision": drawn, "files": _confirmation_attachment()}
    )
    assert first.status_code == 200 and first.context["reply_form"].non_field_errors()
    fields = _confirmation_retry_fields(first)
    assert fields["files-previous"] == ["review-label.txt"]
    _confirmation_staff_reply(agent, ticket, "Please check the replacement label instead.")
    missing_file = client.post(url, fields)
    assert missing_file.status_code == 200
    assert "files" in missing_file.context["reply_form"].errors
    retried = client.post(
        url, {**_confirmation_retry_fields(missing_file), "files": _confirmation_attachment()}
    )
    warnings = list(missing_file.context["reply_form"].non_field_errors())
    if retried.status_code == 200:
        warnings.extend(retried.context["reply_form"].non_field_errors())
    assert any("ticket changed" in warning.lower() for warning in warnings), {
        "file_retry_status": retried.status_code,
        "saved_replies": RequestEntry.objects.filter(request=ticket, body=body).count(),
        "warnings_about_second_change": warnings,
    }


def test_retry_revision_agrees_with_the_latest_reply_shown_during_redraw(
    client, agent, confirmation_portal_reply
):
    """The conversation and retry revision agree when a staff reply commits during rendering."""
    ticket, _, url, drawn = confirmation_portal_reply
    _confirmation_staff_reply(agent, ticket, "We received the envelope.")
    ticket.refresh_from_db()
    earlier = ticket.revision
    body = "The second envelope is for ORD-TEST-000744."
    late_body = "The second envelope arrived this morning."
    gate, gates = Gate(), {}
    with gated(portal_views, "_conversation", gates):
        refusal = Session(lambda: client.post(url, {"body": body, "expected_revision": drawn}))
        gates[refusal.thread] = gate
        refusal.start()
        writer = None
        try:
            assert gate.arrived.wait(timeout=5), refusal.error
            writer = Session(lambda: _confirmation_staff_reply(agent, ticket, late_body)).start()
            state = writer.waiting_or_ended(seconds=5)
            if state == "waiting":
                assert _blocked_by(writer.pid, refusal.pid)
            else:
                assert state == "ended", writer.error
        finally:
            gate.go.set()
            refusal.join()
            if writer is not None:
                writer.join()
    assert refusal.outcome == writer.outcome == "done", (refusal.error, writer.error)
    response = refusal.result
    assert response.status_code == 200 and response.context["reply_form"].errors
    assert b"We received the envelope." in response.content
    ticket.refresh_from_db()
    # Adapted (C11, resolved with C3 by keeping the revision the page was drawn with): a retry
    # from the refused page is refused, whatever the page showed; the ticket opened again takes
    # it once.
    fields = _confirmation_retry_fields(response)
    assert int(fields["expected_revision"]) == int(drawn) < earlier <= ticket.revision
    assert client.post(url, fields).status_code == 200
    assert not RequestEntry.objects.filter(request=ticket, body=body).exists()


@pytest.mark.parametrize("choice", ["reselect", "leave out"])
def test_stale_reply_files_are_only_saved_as_selected_on_the_retry(
    client, agent, confirmation_portal_reply, choice
):
    ticket, _, url, drawn = confirmation_portal_reply
    _confirmation_staff_reply(agent, ticket, "We see the first shipment.")
    body = "Please include ORD-TEST-000745."
    first = client.post(
        url, {"body": body, "expected_revision": drawn, "files": _confirmation_attachment()}
    )
    assert first.status_code == 200 and first.context["reply_form"].errors
    entries = RequestEntry.objects.filter(request=ticket, body=body)
    assert not entries.exists() and not Attachment.objects.filter(request=ticket).exists()
    fields = _confirmation_retry_fields(first)
    assert fields["files-previous"] == ["review-label.txt"]
    # Adapted (after C3): sent from the ticket opened again, with the files chosen there.
    ticket.refresh_from_db()
    fields["expected_revision"] = ticket.revision
    if choice == "reselect":
        fields["files"] = _confirmation_attachment()
    else:
        fields["files-omit"] = "yes"
    assert client.post(url, fields).status_code == 302
    names = list(
        Attachment.objects.filter(entry=entries.get()).values_list("original_name", flat=True)
    )
    assert names == (["review-label.txt"] if choice == "reselect" else [])


def test_portal_grant_narrowed_between_refusal_and_retry_is_honoured(
    client, agent, clinic, clinic_site, clinic_portal, confirmation_portal_reply
):
    ticket, access, url, drawn = confirmation_portal_reply
    _confirmation_staff_reply(agent, ticket, "We will check the reference.")
    body = "Please include ORD-TEST-000746."
    first = client.post(url, {"body": body, "expected_revision": drawn})
    assert first.status_code == 200 and first.context["reply_form"].errors
    set_portal_access(
        actor=agent,
        user_id=clinic_portal.pk,
        expected_client_id=clinic.pk,
        expected_revision=access.revision,
        sites=[clinic_site],
        reason="Narrow to the main office",
    )
    denied = client.post(url, _confirmation_retry_fields(first))
    assert denied.status_code == 404
    assert ticket.subject.encode() not in denied.content and body.encode() not in denied.content
    assert not RequestEntry.objects.filter(request=ticket, body=body).exists()


def test_account_setup_audits_only_the_roles_it_actually_adds_when_the_page_wins(
    client, confirm_editor, make_user
):
    from accounts import setup

    person = make_user("confirm_setup_operator", "admin", is_staff=True, is_superuser=True)
    setup.setup_lab(lab_name="Example laboratory", username=person.username)
    technical = Group.objects.get(name="technical")
    client.force_login(confirm_editor)
    data = _admin_change_data(client, person)
    data["groups"].append(str(technical.pk))

    def submit_page():
        return client.post(confirm_account_page(person), data).status_code

    # Adapted (C2): setup now takes the account's access gate before reading it, as the account
    # page does, so the two take turns: here setup, already under way, goes first and the page,
    # drawn before setup's role, is refused as changed. Either way one addition is recorded.
    confirm_during(
        setup,
        "account_snapshot",
        lambda: setup.setup_lab(username=person.username, roles=["technical"]),
        submit_page,
        before=False,
    )
    assert person.groups.filter(pk=technical.pk).exists()
    events = AuditEvent.objects.filter(
        action__in=["account.updated", "setup.account_roles_added"], object_id=str(person.pk)
    )
    additions = [
        (e.action, e.actor_id)
        for e in events
        if "technical" in set(e.after["groups"]) - set(e.before["groups"])
    ]
    assert len(additions) == 1, additions


def test_two_sends_of_the_same_retry_revision_deliver_one_reply_and_one_file(
    client, agent, confirmation_portal_reply
):
    ticket, _, url, drawn = confirmation_portal_reply
    _confirmation_staff_reply(agent, ticket, "The manifest is ready.")
    body = "Please check ORD-TEST-000747."
    first = client.post(url, {"body": body, "expected_revision": drawn})
    assert first.status_code == 200 and first.context["reply_form"].errors
    retry = _confirmation_retry_fields(first)
    # Adapted (after C3): sent twice from the ticket opened again, at its current revision.
    ticket.refresh_from_db()
    retry["expected_revision"] = ticket.revision
    assert client.post(url, {**retry, "files": _confirmation_attachment()}).status_code == 302
    client.post(url, {**retry, "files": _confirmation_attachment()})
    entries = RequestEntry.objects.filter(request=ticket, body=body)
    assert entries.count() == 1 and Attachment.objects.filter(entry__in=entries).count() == 1


@pytest.mark.parametrize("setting", ["own password", "email notices"])
def test_a_held_access_gate_by_itself_allows_the_person_to_save_the_setting(
    client, make_user, setting, settings
):
    from accounts.acting import close_access
    from desk.notifications import wants_notices

    from .conftest import PASSWORD

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    person = make_user("confirm_account_writer", "technical")
    client.force_login(person)
    phrase = "three autumn office calendars"

    def save():
        if setting == "email notices":
            return client.post(reverse("email-notices"), {})
        return client.post(
            reverse("account_change_password"),
            {"oldpassword": PASSWORD, "password1": phrase, "password2": phrase},
        )

    gate = Held(lambda: close_access([person.pk]))
    update = Session(save).start()
    try:
        state = update.waiting_or_ended(seconds=5)
    finally:
        assert gate.release() == "done", gate.session.error
        update.join()
    # Adapted (session list, review round 1 confirmation): a password saved alone ends the
    # account's other sessions, rows first and the account's row after, the other way round from
    # an access change, so it takes the access gate shared (accounts.models.User._end_sessions):
    # it waits for an access change under way, as here, and saves once it ends; it still never
    # waits for the person's own change (test_review_acting_account_until_commit_verify.py).
    expected = "waiting" if setting == "own password" else "ended"
    assert (state, update.outcome) == (expected, "done"), update.error
    assert update.result.status_code == 302
    person.refresh_from_db()
    if setting == "own password":
        assert person.check_password(phrase)
    else:
        assert not wants_notices(person)


def test_notice_setting_hold_requires_a_transaction_even_for_an_empty_recipient_list(agent):
    from django.db import connection

    from desk.notifications import lock_notice_setting, try_hold_notice_settings

    assert connection.get_autocommit()
    with pytest.raises(RuntimeError):
        try_hold_notice_settings([None])
    with transaction.atomic():
        assert try_hold_notice_settings([])
    batch = [agent.pk, None, agent.pk]

    def hold_batch():
        assert try_hold_notice_settings(batch)

    held = Held(hold_batch)

    def write():
        with transaction.atomic():
            lock_notice_setting(agent.pk)

    writer = Session(write).start()
    try:
        state = writer.waiting_or_ended(seconds=5)
        if state == "waiting":
            assert _blocked_by(writer.pid, held.session.pid)
    finally:
        assert held.release() == "done", held.session.error
        writer.join()
    assert (state, writer.outcome) == ("waiting", "done"), writer.error
