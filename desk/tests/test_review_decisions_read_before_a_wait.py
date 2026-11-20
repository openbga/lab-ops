"""Independent checks of account, directory, clock and reply behavior on synthetic records."""

import logging
from datetime import timedelta
from html import escape
from urllib.parse import urlsplit

import pytest
from allauth.account.utils import user_email
from django.contrib.auth.models import Group, Permission, update_last_login
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, OperationalError, transaction
from django.forms import models as model_forms
from django.test import Client as HttpClient
from django.test.html import parse_html
from django.urls import reverse
from django.utils import timezone

from accounts import admin as account_admin
from accounts.acting import holding
from accounts.models import User
from attachments.models import Attachment, AttachmentBlob
from clients import services as directory
from clients.models import Client, PortalAccess
from clients.seed import DEMO_CLIENTS, _seed_clients
from clients.services import set_portal_access
from clients.tests.test_views import edit_data
from core.models import AuditEvent
from dataimport import importing
from desk import notifications, services
from desk.models import RequestEntry, Status
from jobs.actor import system_account
from jobs.models import JobSettings, Notice, NoticeStatus, NotSentReason
from jobs.services import update_settings
from service_levels import jobs as clock_jobs
from service_levels.models import RequestClock, ServiceTarget
from teams.models import TeamMembership
from teams.services import remove_member

from .conftest import PASSWORD
from .test_contract import new_request, set_status
from .test_review_acting_account_until_commit import Gate, _admin_change_data, gated
from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by, _statement

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def account_editor(make_user):
    return make_user("review_editor", is_superuser=True, is_staff=True)


def account_url(user):
    return reverse("admin:accounts_user_change", args=[user.pk])


def test_group_choices_and_stale_check_describe_the_same_page(client, account_editor, make_user):
    """An inert group is added while Django constructs the initial values for the GET."""
    account = make_user("review_form_reader")
    group = Group.objects.create(name="Example mailing list")
    client.force_login(account_editor)
    read = Session(lambda: _admin_change_data(client, account, last_name="Example"))
    rendered = Gate()
    addition = Session(lambda: account.groups.add(group))
    with gated(model_forms, "model_to_dict", {read.thread: rendered}, before=False):
        read.start()
        try:
            assert rendered.arrived.wait(5), read.error
            addition.start()
            observed = addition.waiting_or_ended(seconds=5)
            assert observed in {"waiting", "ended"}
        finally:
            rendered.go.set()
            read.join()
            if addition.thread.ident is not None:
                addition.join()
    assert (read.outcome, addition.outcome) == ("done", "done"), (read.error, addition.error)
    page = read.result
    posted = client.post(account_url(account), page)
    retained = list(account.groups.values_list("name", flat=True))
    assert group.name in retained, (posted.status_code, page.get("groups"), retained)
    if str(group.pk) not in page.get("groups", []):
        assert posted.status_code == 200
        assert posted.context["adminform"].form.non_field_errors()
        assert not AuditEvent.objects.filter(action="account.updated", object_id=str(account.pk))


def test_group_added_during_an_unrelated_name_save_is_preserved(client, account_editor, make_user):
    """An ordinary membership addition overlaps a name correction. The group has no permissions."""
    account = make_user("review_form_editor")
    group = Group.objects.create(name="Example planning group")
    client.force_login(account_editor)
    page = _admin_change_data(client, account, first_name="Alex")
    save = Session(lambda: client.post(account_url(account), page))
    addition = Session(lambda: account.groups.add(group))
    validated = Gate()
    with gated(account_admin.AccountAdmin, "save_model", {save.thread: validated}):
        save.start()
        try:
            assert validated.arrived.wait(5), save.error
            addition.start()
            state = addition.waiting_or_ended(seconds=5)
            if state == "waiting":
                assert _blocked_by(addition.pid, save.pid)
        finally:
            validated.go.set()
            save.join()
            if addition.thread.ident is not None:
                addition.join()
    assert (save.outcome, addition.outcome) == ("done", "done"), (save.error, addition.error)
    assert state in {"waiting", "ended"}
    assert account.groups.filter(pk=group.pk).exists(), (
        state,
        save.result.status_code,
        list(
            AuditEvent.objects.filter(action="account.updated").values_list(
                "before__groups", "after__groups"
            )
        ),
    )


def test_save_and_continue_after_portal_move_draws_the_new_state(
    client, account_editor, clinic_portal, clinic, water
):
    access = PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    client.force_login(account_editor)
    page = _admin_change_data(client, clinic_portal, client=water.pk, _continue="Save")
    response = client.post(account_url(clinic_portal), page, follow=True)
    assert response.redirect_chain == [(account_url(clinic_portal), 302)]
    access.refresh_from_db()
    assert access.client_id == water.pk and not access.whole_organization
    assert (
        AuditEvent.objects.filter(action="portal_access.reset", actor=account_editor).count() == 1
    )
    assert response.context["adminform"].form["client"].value() == water.pk
    fresh = _admin_change_data(client, clinic_portal, first_name="Sam")
    assert client.post(account_url(clinic_portal), fresh).status_code == 302


def test_overlapping_owner_step_downs_keep_a_current_owner(make_user):
    owners = [make_user(name, is_superuser=True, is_staff=True) for name in ("owner_a", "owner_b")]
    clients = [HttpClient(), HttpClient()]
    pages = []
    for http, owner in zip(clients, owners, strict=True):
        http.force_login(owner)
        pages.append(_admin_change_data(http, owner, is_superuser=False))

    def step_down(index):
        return clients[index].post(account_url(owners[index]), pages[index])

    def first_change():
        assert step_down(0).status_code == 302

    held = Held(first_change)
    second = Session(lambda: step_down(1)).start()
    try:
        assert second.waiting_or_ended(seconds=5) == "waiting"
        assert _blocked_by(second.pid, held.session.pid)
    finally:
        assert held.release() == "done", held.session.error
        second.join()
    assert second.outcome == "done", second.error
    assert second.result.status_code == 200
    assert b"Keep at least one active superuser" in second.result.content
    assert list(User.objects.filter(is_superuser=True, is_active=True, is_staff=True)) == [
        owners[1]
    ]


@pytest.mark.parametrize("route", ["own page", "account password page"])
def test_password_page_keeps_current_session_and_concurrent_name_correction(
    route, make_user, client, settings
):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    user = make_user("review_password_editor", is_superuser=True, is_staff=True)
    other = HttpClient()
    client.force_login(user)
    other.force_login(user)
    replacement = "a synthetic replacement phrase"
    if route == "own page":
        url = reverse("account_change_password")
        data = {"oldpassword": PASSWORD, "password1": replacement, "password2": replacement}
    else:
        url = reverse("admin:auth_user_password_change", args=[user.pk])
        data = {"usable_password": "true", "password1": replacement, "password2": replacement}

    def correct_name():
        current = User.objects.get(pk=user.pk)
        current.last_name = "Corrected"
        current.save(update_fields=["last_name"])

    first = Held(correct_name)
    change = Session(lambda: client.post(url, data)).start()
    try:
        assert change.waiting_or_ended(seconds=7) == "waiting", (change.outcome, change.error)
        assert _blocked_by(change.pid, first.session.pid)
    finally:
        assert first.release() == "done"
        change.join()
    assert change.outcome == "done", change.error
    assert change.result.status_code == 302
    user.refresh_from_db()
    assert user.check_password(replacement)
    assert client.get(account_url(user)).status_code == 200
    assert other.get(account_url(user)).status_code == 302
    assert user.last_name == "Corrected"


def test_email_only_save_from_before_a_portal_move_keeps_the_move(
    clinic_portal, clinic, water, agent
):
    access = PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    stale = User.objects.get(pk=clinic_portal.pk)

    def move():
        current = User.objects.get(pk=clinic_portal.pk)
        current.client, current.changed_by = water, agent
        current.save(update_fields=["client_id"])

    first = Held(move)
    correction = Session(
        lambda: user_email(stale, "new-office@example.invalid", commit=True)
    ).start()
    try:
        assert correction.waiting_or_ended(seconds=5) == "waiting"
        assert _blocked_by(correction.pid, first.session.pid)
    finally:
        assert first.release() == "done"
        correction.join()
    assert correction.outcome == "done", correction.error
    clinic_portal.refresh_from_db()
    access.refresh_from_db()
    assert (clinic_portal.email, clinic_portal.client_id, access.client_id) == (
        "new-office@example.invalid",
        water.pk,
        water.pk,
    )
    assert not access.whole_organization
    assert AuditEvent.objects.filter(action="portal_access.reset").count() == 1


def test_partial_save_after_rolled_back_move_does_not_record_a_move(
    agent, clinic_portal, clinic, water
):
    """A maintenance caller abandons an edit after an ordinary duplicate-email error."""
    access = PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    clinic_portal.changed_by = agent
    clinic_portal.client = water
    clinic_portal.email = agent.email
    with pytest.raises(IntegrityError):
        clinic_portal.save(update_fields=["client", "email"])
    clinic_portal.refresh_from_db()
    assert clinic_portal.client_id == clinic.pk
    assert not AuditEvent.objects.filter(action="portal_access.reset").exists()
    clinic_portal.first_name = "Avery"
    clinic_portal.save(update_fields=["first_name"])
    access.refresh_from_db()
    assert (
        access.whole_organization,
        AuditEvent.objects.filter(action="portal_access.reset").count(),
    ) == (True, 0)


def test_models_match_published_migrations():
    # Django runs this command against pytest's already selected disposable database.
    call_command("makemigrations", check=True, dry_run=True, interactive=False, verbosity=0)


@pytest.fixture
def review_due_ticket(agent, category, accessioning, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    ServiceTarget.objects.create(name="Review target", resolution_minutes=60)
    ticket = new_request(agent, category, team=accessioning)
    # Actually past due, including when the post-commit delivery handler reads the wall clock.
    RequestClock.objects.filter(request=ticket).update(
        resolution_due_at=timezone.now() - timedelta(minutes=5)
    )
    return ticket


@pytest.mark.parametrize("work", ["sign-in timestamp", "assign another ticket"])
def test_other_work_proceeds_while_breach_is_being_recorded(
    work, review_due_ticket, agent, category, accessioning, make_user, join
):
    lead = make_user("review_clock_lead", "technical")
    join(lead, accessioning, lead=True)
    other_ticket = new_request(agent, category, team=accessioning)

    def other_work():
        if work == "sign-in timestamp":
            return update_last_login(None, user=User.objects.get(pk=lead.pk))
        return services.assign(
            actor=agent,
            request=other_ticket,
            team=accessioning,
            assignee=lead,
            expected_revision=other_ticket.revision,
        )

    saving = Gate()
    job = Session(clock_jobs.notice_breaches)
    worker = Session(other_work)
    # Here all recipient locks have been taken. Pause before the audit write and commit.
    with gated(clock_jobs, "record_event", {job.thread: saving}):
        job.start()
        try:
            assert saving.arrived.wait(timeout=8), job.error
            worker.start()
            state = worker.waiting_or_ended(seconds=5)
            if state == "waiting":
                assert _blocked_by(worker.pid, job.pid)
        finally:
            saving.go.set()
            job.join()
            if worker.thread.ident is not None:
                worker.join()
    assert (worker.outcome, job.outcome) == ("done", "done"), (worker.error, job.error)
    assert (
        Notice.objects.filter(
            topic=notifications.BREACH_TOPIC, object_id=review_due_ticket.pk, recipient=lead
        ).count()
        == 1
    )
    assert state == "ended", f"{work} waited for the breach on a different ticket"


def test_breach_recipient_records_reflect_a_notice_preference_saved_during_selection(
    review_due_ticket, accessioning, client, make_user, join
):
    lead = make_user("review_mail_lead", "technical")
    join(lead, accessioning, lead=True)
    client.force_login(lead)
    selected = Gate()
    job = Session(clock_jobs.notice_breaches)
    preference = Session(lambda: client.post(reverse("email-notices"), {}))
    with gated(notifications, "breach_recipients", {job.thread: selected}, before=False):
        job.start()
        try:
            assert selected.arrived.wait(timeout=8), job.error
            preference.start()
            state = preference.waiting_or_ended(seconds=5)
            if state == "waiting":
                assert _blocked_by(preference.pid, job.pid)
        finally:
            selected.go.set()
            job.join()
            if preference.thread.ident is not None:
                preference.join()
    assert (job.outcome, preference.outcome) == ("done", "done"), (job.error, preference.error)
    assert preference.result.status_code == 302
    assert not notifications.wants_notices(lead)
    rows = Notice.objects.filter(
        topic=notifications.BREACH_TOPIC, recipient=lead, object_id=review_due_ticket.pk
    )
    if state == "waiting":
        # Job held the preference first. Recording its earlier choice is consistent.
        assert job.result["notices"] == rows.count() == 1
    else:
        assert state == "ended"
        assert not mail.outbox  # rechecking at send time still honors the opt-out
        assert not rows.exclude(
            status=NoticeStatus.NOT_SENT, not_sent_reason=NotSentReason.NO_LONGER_APPLIES
        ).exists()
        assert rows.count() == job.result["notices"] == 0, "opt-out committed before recording"


def test_breach_on_an_unassigned_team_honors_every_members_access(
    review_due_ticket, accessioning, make_user, join, settings
):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    members = [make_user(f"review_member_{index}", "technical") for index in range(24)]
    for member in members:
        join(member, accessioning)
    # Even an unexpected membership left over for this account must never address mail to it.
    join(system_account(), accessioning)
    removing = Held(lambda: members[-1].groups.clear())
    job = Session(clock_jobs.notice_breaches).start()
    try:
        state = job.waiting_or_ended(seconds=8)
    finally:
        assert removing.release() == "done", removing.session.error
        job.join()
    assert state == "ended" and job.outcome == "done", job.error
    assert job.result == {"busy": 1, "breaches": 0, "notices": 0}
    assert not Notice.objects.filter(topic=notifications.BREACH_TOPIC).exists()
    assert clock_jobs.notice_breaches() == {"breaches": 1, "notices": len(members) - 1}
    addressed = Notice.objects.filter(
        topic=notifications.BREACH_TOPIC, object_id=review_due_ticket.pk
    )
    assert set(addressed.values_list("recipient_id", flat=True)) == {
        user.pk for user in members[:-1]
    }
    assert set(addressed.values_list("data__role", flat=True)) == {"member"}


def test_initialized_breach_sweep_does_not_wait_for_edit_of_unrelated_job_setting(
    review_due_ticket, make_user
):
    system_account()
    admin = make_user("review_sweep_admin", "admin")
    revision = JobSettings.objects.get().revision
    editor = Held(
        lambda: update_settings(
            actor=admin, expected_revision=revision, close_resolved_after_days=12
        )
    )
    job = Session(clock_jobs.notice_breaches).start()
    try:
        state = job.waiting_or_ended(seconds=5)
        if state == "waiting":
            assert _blocked_by(job.pid, editor.session.pid)
    finally:
        assert editor.release() == "done", editor.session.error
        job.join()
    assert job.outcome == "done", job.error
    assert state == "ended", "breach sweep waited for an edit of the auto-close period"
    still_due = (
        RequestClock.objects.get(request=review_due_ticket).resolution_breach_noticed_at is None
    )
    if still_due:
        assert clock_jobs.notice_breaches()["breaches"] == 1
    else:
        assert job.result["breaches"] == 1


@pytest.fixture
def review_closed_ticket(client, agent, sample_category, clinic, clinic_portal):
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    ticket = new_request(agent, sample_category, client=clinic)
    client.force_login(clinic_portal)
    detail = client.get(reverse("portal:request-detail", args=[ticket.number]))
    assert detail.status_code == 200
    set_status(ticket, Status.CLOSED)
    return (
        ticket,
        detail.context["reply_url"],
        detail.context["reply_form"]["expected_revision"].value(),
    )


def test_closed_portal_reply_has_the_ticket_help(client, review_closed_ticket):
    """The Help drawer on a refused reply describes the ticket the client is viewing."""
    ticket, url, revision = review_closed_ticket
    page = client.post(
        url, {"body": "One more tube, please.", "expected_revision": revision}, follow=True
    )
    assert page.status_code == 200 and ticket.number.encode() in page.content
    assert b'data-vr-documentation-id="portal-request-detail"' in page.content, page.context[
        "page_documentation"
    ]["id"]


def test_reloading_the_closed_reply_response_sends_it_again_and_stores_nothing(
    client, review_closed_ticket
):
    """Adapted (F12, resolved in the page help): a reload of the refused reply's response sends
    the reply again, which is refused the same way and shows its own text again; nothing is
    stored, and opening the ticket again does not show it."""
    ticket, url, revision = review_closed_ticket
    body = "One spare label for ORD-TEST-000891."
    data = {"body": body, "expected_revision": revision}
    first = client.post(url, data, follow=True)
    assert first.status_code == 200 and first.wsgi_request.method == "POST"
    reloaded = client.post(first.wsgi_request.get_full_path(), data, follow=True)
    assert reloaded.status_code == 200 and body.encode() in reloaded.content
    assert not ticket.entries.filter(body=body).exists()
    opened = client.get(reverse("portal:request-detail", args=[ticket.number]))
    assert body.encode() not in opened.content


def test_closed_reply_echoes_literal_text_and_discards_the_chosen_file(
    client, review_closed_ticket, make_user, clinic, caplog
):
    ticket, url, revision = review_closed_ticket
    body = "Please label <tube> & rack for ORD-TEST-000892."
    peer = make_user("desk_portal_peer", client=clinic)
    PortalAccess.objects.create(user=peer, client=clinic, whole_organization=True)
    models = (RequestEntry, Attachment, AttachmentBlob, AuditEvent, Notice)
    before = [model.objects.count() for model in models]
    caplog.set_level(logging.INFO, logger="lab_ops.request")
    page = client.post(
        url,
        {
            "body": body,
            "expected_revision": revision,
            "files": SimpleUploadedFile("spare-labels.txt", b"spare labels", "text/plain"),
        },
        follow=True,
    )
    assert [model.objects.count() for model in models] == before
    assert (
        "no-store" in page.headers["Cache-Control"] and "private" in page.headers["Cache-Control"]
    )
    assert page.status_code == 200
    assert escape(body).encode() in page.content
    assert body.encode() not in page.content
    assert b"spare-labels.txt" not in page.content
    assert "ORD-TEST-000892" not in str(dict(client.session))
    assert any(record.name == "lab_ops.request" for record in caplog.records)
    assert "ORD-TEST-000892" not in caplog.text
    assert "ORD-TEST-000892" not in page.wsgi_request.get_full_path()
    assert (
        b"ORD-TEST-000892"
        not in client.get(reverse("portal:request-detail", args=[ticket.number])).content
    )
    client.force_login(peer)
    another_reader = client.get(reverse("portal:request-detail", args=[ticket.number]))
    assert another_reader.status_code == 200
    assert b"ORD-TEST-000892" not in another_reader.content


@pytest.mark.parametrize("scope", ["grant removed", "restricted category"])
def test_closed_ticket_outside_current_portal_scope_refuses_the_reply(
    client, agent, clinic, clinic_site, clinic_portal, sample_category, privacy_category, scope
):
    access = PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    category = sample_category if scope == "grant removed" else privacy_category
    ticket = new_request(agent, category, client=clinic)
    client.force_login(clinic_portal)
    detail = reverse("portal:request-detail", args=[ticket.number])
    if scope == "grant removed":
        assert client.get(detail).status_code == 200
        set_portal_access(
            actor=agent,
            user_id=clinic_portal.pk,
            expected_client_id=clinic.pk,
            expected_revision=access.revision,
            sites=[clinic_site],
            reason="Limit tickets to the main office",
        )
    set_status(ticket, Status.CLOSED)
    body = "Please note order ORD-TEST-000893."
    result = client.post(
        reverse("portal:request-reply", args=[ticket.number]),
        {"body": body, "expected_revision": ticket.revision},
    )
    assert result.status_code == 404
    assert body.encode() not in result.content
    assert ticket.subject.encode() not in result.content
    assert not RequestEntry.objects.filter(request=ticket, body=body).exists()


@pytest.mark.parametrize("audience", ["no role", "technical role", "portal", "deactivated"])
def test_change_pages_check_the_audience_on_direct_requests(
    client, make_user, agent, clinic, sample_category, audience
):
    roles = ["technical"] if audience == "technical role" else []
    person = make_user(
        "desk_review_reader",
        *roles,
        is_staff=audience != "portal",
        client=clinic if audience == "portal" else None,
    )
    ticket = new_request(agent, sample_category, client=clinic)
    client.force_login(person)
    if audience == "deactivated":
        person.is_active = False
        person.changed_by = agent
        person.save(update_fields=["is_active"])
    pages = [
        reverse("admin:accounts_user_change", args=[agent.pk]),
        reverse("admin:auth_user_password_change", args=[agent.pk]),
        reverse("clients:client-create"),
        reverse("clients:client-edit", args=[clinic.pk]),
        reverse("dataimport:upload", args=["organizations"]),
    ]
    for path in pages:
        for send in (client.get, client.post):
            response = send(path, follow=True)
            if audience == "deactivated":
                assert response.status_code == 200 and response.redirect_chain, path
                assert response.wsgi_request.path == reverse("account_login"), path
            else:
                assert response.status_code == 403, (path, audience, response.status_code)
    response = client.post(
        reverse("portal:request-reply", args=[ticket.number]),
        {"body": "One more label.", "expected_revision": ticket.revision},
        follow=True,
    )
    if audience == "deactivated":
        assert response.status_code == 200 and response.wsgi_request.path == reverse(
            "account_login"
        )
    else:
        assert response.status_code == (404 if audience == "portal" else 403)


# Organization forms and imports


@pytest.mark.parametrize("departure", ["before opening", "after opening"])
def test_phone_edit_keeps_the_owner_when_the_owner_has_left(
    client, agent, make_user, clinic, departure
):
    owner = make_user("former_relationship_owner", "technical")
    clinic = directory.update_client(
        actor=agent,
        client_id=clinic.pk,
        expected_revision=clinic.revision,
        account_owner=owner,
        reason="Appointed",
    )
    client.force_login(agent)
    url = reverse("clients:client-edit", args=[clinic.pk])

    def leave():
        owner.is_active = False
        owner.changed_by = agent
        owner.save(update_fields=["is_active"])

    if departure == "before opening":
        leave()
    page = client.get(url)
    assert page.status_code == 200
    # A single-select posts its selected option, falling back to its first option.
    select = parse_html(str(page.context["form"]["account_owner"]))
    options = [
        dict(option.attributes)
        for option in select.children
        if getattr(option, "name", None) == "option"
    ]
    selected = next((opt for opt in options if "selected" in opt), options[0])
    if departure == "after opening":
        leave()
    response = client.post(
        url,
        edit_data(
            clinic,
            organization_type=clinic.organization_type,
            account_owner=selected["value"],
            phone="555-0198",
        ),
    )
    clinic.refresh_from_db()
    assert (response.status_code, clinic.phone, clinic.account_owner_id) == (
        302,
        "555-0198",
        owner.pk,
    )


def test_owner_preview_holds_an_accepted_owner_before_its_parent_rows(agent, make_user, clinic):
    """A staff username comes into use between two rows. If the preview uses it, it must
    have taken its owner's lock before its parent lock, as for any other named owner."""

    person = make_user("earlier_owner_name", "technical")
    filename = "owner-names.csv"
    data = (
        b"account_code,name,parent_account_code,account_owner\n"
        b"EXONE,Example first office,EXCLIN,\n"
        b"EXTWO,Example second office,,current_owner_name\n"
    )
    first_row, all_rows = Gate(), Gate()
    preview = Session(
        lambda: importing.preview_file(
            importing.ORGANIZATIONS, actor=agent, data=data, filename=filename
        )
    )

    def row_is_held(obj):
        try:
            with transaction.atomic():
                type(obj).objects.select_for_update(nowait=True, no_key=True).get(pk=obj.pk)
        except OperationalError as error:
            if getattr(error.__cause__, "sqlstate", None) == "55P03":
                return True
            raise
        return False

    with (
        gated(importing, "_read_row", {preview.thread: first_row}, before=False),
        gated(importing, "parse", {preview.thread: all_rows}, before=False),
    ):
        change = None
        preview.start()
        try:
            assert first_row.arrived.wait(timeout=5), preview.error
            assert row_is_held(clinic)
            # The directory's parent row is already locked. This account had a different
            # username when the file first scanned its owner column.
            owner_held_at_first_row = row_is_held(person)

            def rename():
                person.username = "current_owner_name"
                person.save(update_fields=["username"])

            change = Session(rename).start()
            assert change.waiting_or_ended(seconds=4) in {"waiting", "ended"}
            first_row.go.set()
            assert all_rows.arrived.wait(timeout=5), preview.error
            parent_held, owner_held = row_is_held(clinic), row_is_held(person)
        finally:
            first_row.go.set()
            all_rows.go.set()
            preview.join()
            if change is not None:
                change.join()
    assert change.outcome == "done", change.error
    assert preview.outcome == "done", preview.error
    assert parent_held
    assert not Client.objects.filter(account_number__in=["EXONE", "EXTWO"]).exists()
    assert User.objects.get(pk=person.pk).username == "current_owner_name"
    assert not preview.result.ok or (owner_held_at_first_row and owner_held), (
        preview.result.rows[1].bound,
        owner_held_at_first_row,
        owner_held,
    )


def test_owner_named_twice_is_resolved_consistently_and_preview_rolls_back(agent, make_user):
    owner = make_user("shared_relationship_owner", "technical")
    options = dict(
        actor=agent,
        filename="shared-owner.csv",
        data=(
            b"account_code,name,account_owner\n"
            b"EXONE,Example first office,shared_relationship_owner\n"
            b"EXTWO,Example second office,shared_relationship_owner\n"
        ),
    )
    previous_events = AuditEvent.objects.count()
    preview = importing.preview_file(importing.ORGANIZATIONS, **options)
    assert preview.ok, [row.errors for row in preview.rows]
    assert not Client.objects.filter(account_owner=owner).exists()
    assert AuditEvent.objects.count() == previous_events
    importing.commit_file(
        importing.ORGANIZATIONS, **options, reason="Appointed", state_token=preview.state_token
    )
    assert set(
        Client.objects.filter(account_owner=owner).values_list("account_number", flat=True)
    ) == {"EXONE", "EXTWO"}


def test_organization_seed_uses_its_own_actor_as_the_owner(agent):
    expected = {fields["account_number"] for fields, *_ in DEMO_CLIENTS}
    with transaction.atomic(), holding(agent):
        assert set(_seed_clients(agent)) == expected
    assert (
        set(Client.objects.filter(account_owner=agent).values_list("account_number", flat=True))
        == expected
    )


@pytest.mark.parametrize("first", ["appointment", "team departure"])
def test_team_membership_is_independent_of_organization_ownership(
    agent, make_user, accessioning, join, first
):
    owner = make_user("outgoing_team_member", "technical")
    supervisor = make_user("team_supervisor", "supervisor")
    member = join(owner, accessioning)

    def appointment():
        return directory.create_client(actor=agent, name="Example office", account_owner=owner)

    def departure():
        return remove_member(actor=supervisor, membership=member, reason="Changed teams")

    earlier, later = (
        (appointment, departure) if first == "appointment" else (departure, appointment)
    )
    held = Held(earlier)
    other = Session(later).start()
    try:
        observed = other.waiting_or_ended(seconds=4)
    finally:
        assert held.release() == "done"
        other.join()
    assert observed in {"waiting", "ended"}
    assert other.outcome == "done", other.error
    assert Client.objects.get(name="Example office").account_owner_id == owner.pk
    assert not TeamMembership.objects.filter(team=accessioning, user=owner).exists()


def test_stale_organization_form_requests_reload_when_its_new_owner_also_lost_their_role(
    client, agent, make_user, clinic
):
    owner = make_user("incoming_relationship_owner", "technical")
    colleague = make_user("colleague_in_directory", "account_manager")
    url = reverse("clients:client-edit", args=[clinic.pk])
    client.force_login(agent)
    page = client.get(url)
    assert page.status_code == 200
    data = edit_data(
        clinic,
        name="My new organization name",
        account_owner=owner.pk,
        expected_revision=page.context["form"]["expected_revision"].value(),
    )

    def revise():
        directory.update_client(
            actor=colleague,
            client_id=clinic.pk,
            expected_revision=clinic.revision,
            reason="Renamed",
            name="Current organization name",
        )
        owner.groups.clear()

    held = Held(revise)
    save = Session(lambda: client.post(url, data)).start()
    try:
        state = save.waiting_or_ended(seconds=4)
        blocked = state == "waiting" and _blocked_by(save.pid, held.session.pid)
    finally:
        assert held.release() == "done"
        save.join()
    assert blocked and save.outcome == "done", save.error
    clinic.refresh_from_db()
    assert (clinic.name, clinic.account_owner_id) == ("Current organization name", None)
    assert save.result.status_code == 200
    form = save.result.context["form"]
    assert form["name"].value() == data["name"]
    assert "Reload and review the current record" in str(form.non_field_errors()), form.errors


def test_pre_upgrade_account_post_preserves_account_and_input(client, account_editor, make_user):
    account = make_user("review_legacy_account")
    client.force_login(account_editor)
    page = _admin_change_data(client, account, last_name="Requested name")
    page.pop("drawn", None)  # POST from the old release, on either checkout
    response = client.post(account_url(account), page)
    account.refresh_from_db()
    assert (response.status_code, account.last_name) == (200, "")
    form = response.context["adminform"].form
    assert form.non_field_errors() and form["last_name"].value() == "Requested name"
    assert not AuditEvent.objects.filter(
        action="account.updated", object_id=str(account.pk)
    ).exists()


def test_name_edit_preserves_independent_group_permission_change(client, account_editor, make_user):
    account = make_user("review_group_member")
    group = Group.objects.create(name="Example label")
    account.groups.add(group)
    client.force_login(account_editor)
    page = _admin_change_data(client, account, first_name="Parker")
    permission = Permission.objects.get(content_type__app_label="auth", codename="view_group")
    changed = client.post(
        reverse("admin:auth_group_change", args=[group.pk]),
        {"name": group.name, "permissions": [permission.pk]},
    )
    assert changed.status_code == 302
    assert client.post(account_url(account), page).status_code == 302
    assert group.permissions.filter(pk=permission.pk).exists()
    assert not account.user_permissions.exists()
    assert account.groups.filter(pk=group.pk).exists()


def test_account_edit_preserves_separate_portal_site_selection(
    client, account_editor, clinic_portal, agent, clinic, clinic_site
):
    client.force_login(account_editor)
    page = _admin_change_data(client, clinic_portal, first_name="Avery")
    access = set_portal_access(
        actor=agent, user_id=clinic_portal.pk, expected_client_id=clinic.pk, sites=[clinic_site]
    )
    assert client.post(account_url(clinic_portal), page).status_code == 302
    assert list(access.sites.all()) == [clinic_site]
    assert not AuditEvent.objects.filter(action="portal_access.reset").exists()


def test_password_reset_keeps_concurrent_name_and_expires_other_session(
    client, make_user, settings
):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    user = make_user("review_reset", is_staff=True, is_superuser=True)
    previous = HttpClient()
    previous.force_login(user)
    assert client.post(reverse("account_reset_password"), {"email": user.email}).status_code == 302
    link = next(word for word in mail.outbox[-1].body.split() if "/password/reset/key/" in word)
    landing = client.get(urlsplit(link).path, follow=True)
    assert landing.status_code == 200 and landing.context["form"]
    replacement = "another synthetic replacement phrase"
    data = {"password1": replacement, "password2": replacement}
    first = Held(lambda: User.objects.filter(pk=user.pk).update(first_name="Robin"))
    reset = Session(lambda: client.post(landing.wsgi_request.path, data)).start()
    try:
        assert reset.waiting_or_ended(seconds=5) == "waiting", reset.error
        assert _blocked_by(reset.pid, first.session.pid)
    finally:
        assert first.release() == "done"
        reset.join()
    assert reset.outcome == "done", reset.error
    assert reset.result.status_code == 302
    user.refresh_from_db()
    assert user.check_password(replacement)
    assert previous.get(account_url(user)).status_code == 302
    assert user.first_name == "Robin"


def test_breach_recording_skips_a_busy_audit_actor(review_due_ticket):
    """The audit event names the scheduled account even when nobody is due a notice."""
    actor = system_account()
    held = Held(lambda: User.objects.select_for_update().get(pk=actor.pk))
    sweep = Session(clock_jobs.notice_breaches).start()
    try:
        state = sweep.waiting_or_ended(seconds=5)
        if state == "waiting":
            assert _blocked_by(sweep.pid, held.session.pid)
            statement = _statement(sweep.pid)
        else:
            statement = ""
    finally:
        assert held.release() == "done"
        sweep.join()
    assert sweep.outcome == "done", sweep.error
    assert state == "ended", (state, statement)
    assert sweep.result == {"breaches": 0, "notices": 0, "busy": 1}
    assert clock_jobs.notice_breaches()["breaches"] == 1


def test_closed_reply_with_file_only_explains_the_outcome(client, review_closed_ticket):
    """A file was chosen and submitted from the open page; report its refusal."""
    _, url, revision = review_closed_ticket
    file_count = Attachment.objects.count()
    response = client.post(
        url,
        {
            "body": " ",
            "expected_revision": revision,
            "files": SimpleUploadedFile("labels.txt", b"labels", "text/plain"),
        },
        follow=True,
    )
    assert response.status_code == 200
    assert Attachment.objects.count() == file_count

    def refusal_notices(element):
        classes = dict(element.attributes).get("class", "").split()
        if "alert-warning" in classes or "alert-danger" in classes:
            yield str(element).lower()
        for child in element.children:
            if hasattr(child, "attributes"):
                yield from refusal_notices(child)

    notices = list(refusal_notices(parse_html(response.content.decode())))
    assert any("closed" in notice or "not sent" in notice for notice in notices), (
        "Only the ordinary closed ticket status is displayed after the file submission"
    )


@pytest.mark.parametrize("operation", ["create", "edit", "preview", "confirm"])
def test_owner_role_stays_held_until_each_directory_operation_finishes(
    operation, make_user, agent, clinic
):
    person = make_user("review_incoming_owner", "technical")
    args = dict(
        actor=agent,
        filename="appointments.csv",
        data=f"account_code,name,account_owner\nEXCLIN,{clinic.name},{person.username}\n".encode(),
    )
    if operation == "confirm":
        checked = importing.preview_file(importing.ORGANIZATIONS, **args)
        assert checked.ok

    def appoint():
        if operation == "create":
            return directory.create_client(
                actor=agent, name="Receiving office", account_owner=person
            )
        if operation == "edit":
            return directory.update_client(
                actor=agent,
                client_id=clinic.pk,
                expected_revision=clinic.revision,
                account_owner=person,
                reason="Appointed",
            )
        if operation == "preview":
            return importing.preview_file(importing.ORGANIZATIONS, **args)
        return importing.commit_file(
            importing.ORGANIZATIONS, **args, state_token=checked.state_token, reason="Appointed"
        )

    recorded = Gate()
    appointment = Session(appoint)
    departure = Session(lambda: person.groups.clear())
    with gated(directory, "record_event", {appointment.thread: recorded}, before=False):
        appointment.start()
        try:
            assert recorded.arrived.wait(5), appointment.error
            departure.start()
            state = departure.waiting_or_ended(seconds=5)
            if state == "waiting":
                assert _blocked_by(departure.pid, appointment.pid)
        finally:
            recorded.go.set()
            appointment.join()
            if departure.thread.ident is not None:
                departure.join()
    assert (appointment.outcome, departure.outcome) == ("done", "done")
    assert not person.groups.exists()
    assert Client.objects.filter(account_owner=person).count() == int(operation != "preview")
    assert state == "waiting", f"{operation} had not finished when the owner's role was removed"


@pytest.mark.parametrize("departure", ["role", "team"])
def test_role_and_team_removals_started_after_breach_selection_wait_for_it(
    review_due_ticket, accessioning, make_user, join, departure
):
    lead = make_user("review_departing_lead", "technical")
    membership = join(lead, accessioning, lead=True)
    supervisor = make_user("review_team_editor", "supervisor")

    def remove():
        if departure == "role":
            lead.groups.clear()
        else:
            remove_member(actor=supervisor, membership=membership)

    job = Session(clock_jobs.notice_breaches)
    later = Session(remove)
    chosen = Gate()
    with gated(notifications, "breach_recipients", {job.thread: chosen}, before=False):
        job.start()
        try:
            assert chosen.arrived.wait(5), job.error
            later.start()
            state = later.waiting_or_ended(seconds=5)
            if state == "waiting":
                assert _blocked_by(later.pid, job.pid)
        finally:
            chosen.go.set()
            job.join()
            if later.thread.ident is not None:
                later.join()
    assert (job.outcome, later.outcome) == ("done", "done"), (job.error, later.error)
    assert state == "waiting"
    assert job.result["notices"] == 1
    if departure == "role":
        assert not lead.groups.exists()
    else:
        assert not TeamMembership.objects.filter(pk=membership.pk).exists()
