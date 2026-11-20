"""Third confirmation of setup role changes, organization forms and portal replies."""

import pytest
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from accounts import setup
from attachments.models import Attachment
from clients.models import PortalAccess
from core.lab_profile import save_identity
from core.models import AuditEvent
from desk import services as third_portal_services
from desk.models import EntryKind, RequestEntry, Status

from .test_contract import new_request
from .test_portal import main as third_portal_main
from .test_review_acting_account_until_commit import Gate, _admin_change_data, _note, gated
from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by
from .test_review_decisions_read_before_a_wait_verify import account_url, rename_group

pytestmark = pytest.mark.django_db(transaction=True)


def test_setup_and_the_account_editor_credit_one_restoration_after_a_lab_wait(client, make_user):
    person = make_user("setup_rota", "admin", "technical")
    setup.setup_lab(lab_name="Example laboratory", username=person.username)
    editor = make_user("rota_editor", is_staff=True, is_superuser=True)
    profile_editor = make_user("profile_editor", "admin")
    client.force_login(editor)
    admin = Group.objects.get(name="admin")
    technical = Group.objects.get(name="technical")
    url = account_url(person)
    remove = _admin_change_data(client, person, groups=[str(admin.pk)])
    setup_request = Session(lambda: setup.setup_lab(username=person.username, roles=["technical"]))
    insertion = Gate()
    with gated(type(person.groups), "add", {setup_request.thread: insertion}):
        profile = Held(lambda: save_identity(actor=profile_editor, values={"city": "Mapleton"}))
        setup_request.start()
        restore = None
        try:
            assert setup_request.waiting_or_ended() == "waiting"
            assert _blocked_by(setup_request.pid, profile.session.pid)
            assert client.post(url, remove).status_code == 302
            data = _admin_change_data(client, person, groups=[str(admin.pk), str(technical.pk)])
            assert profile.release() == "done", profile.session.error
            assert insertion.arrived.wait(timeout=5), setup_request.error
            restore = Session(lambda: client.post(url, data)).start()
            assert restore.waiting_or_ended() in {"waiting", "ended"}
        finally:
            insertion.go.set()
            profile.release()
            setup_request.join()
            if restore is not None:
                restore.join()
    assert (setup_request.outcome, restore.outcome) == ("done", "done")
    assert person.groups.filter(pk=technical.pk).exists()
    assert restore.result.status_code in {200, 302}
    credited = [
        (event.action, event.actor_id)
        for event in AuditEvent.objects.filter(
            object_id=str(person.pk), action__in=["account.updated", "setup.account_roles_added"]
        )
        if "technical" in set(event.after["groups"]) - set(event.before["groups"])
    ]
    assert len(credited) == 1, credited
    owner = (
        ("account.updated", editor.pk)
        if restore.result.status_code == 302
        else ("setup.account_roles_added", person.pk)
    )
    assert credited == [owner]


def test_setup_waits_for_the_note_when_a_held_role_is_removed_during_the_lab_wait(
    client, make_user, category
):
    person = make_user("setup_note_writer", "admin", "technical")
    setup.setup_lab(lab_name="Example laboratory", username=person.username)
    ticket = new_request(person, category)
    profile_editor = make_user("city_editor", "admin")
    editor = make_user("role_editor", is_staff=True, is_superuser=True)
    client.force_login(editor)
    data = _admin_change_data(client, person, groups=[str(Group.objects.get(name="admin").pk)])
    profile = Held(lambda: save_identity(actor=profile_editor, values={"city": "Mapleton"}))
    request = Session(
        lambda: setup.setup_lab(username=person.username, roles=["technical"])
    ).start()
    note = None
    try:
        assert request.waiting_or_ended() == "waiting"
        assert _blocked_by(request.pid, profile.session.pid)
        assert client.post(account_url(person), data).status_code == 302
        note = Held(lambda: _note(person, ticket, "The reference was confirmed."))
        assert profile.release() == "done", profile.session.error
        state = request.waiting_or_ended()
        blocked_by_note = state == "waiting" and _blocked_by(request.pid, note.session.pid)
    finally:
        profile.release()
        if note is not None:
            note.release()
        request.join()
    assert request.outcome == "done", request.error
    assert person.groups.filter(name="technical").exists()
    assert blocked_by_note, f"setup was {state} while its operator's note was still open"


def test_setup_does_not_claim_a_group_rename_between_its_role_read_and_insert(client, make_user):
    person = make_user("setup_with_rota", "admin")
    setup.setup_lab(lab_name="Example laboratory", username=person.username)
    group = Group.objects.create(name="First rota")
    person.groups.add(group)
    editor = make_user("rota_owner", is_staff=True, is_superuser=True)
    client.force_login(editor)
    addition = Session(lambda: setup.setup_lab(username=person.username, roles=["technical"]))
    rename = Session(lambda: rename_group(client, group, "Second rota"))
    insertion = Gate()
    with gated(type(person.groups), "add", {addition.thread: insertion}):
        addition.start()
        try:
            assert insertion.arrived.wait(timeout=5), addition.error
            rename.start()
            assert rename.waiting_or_ended() in {"waiting", "ended"}
        finally:
            insertion.go.set()
            addition.join()
            if rename.thread.ident is not None:
                rename.join()
    assert (addition.outcome, rename.outcome) == ("done", "done"), (addition.error, rename.error)
    change = AuditEvent.objects.get(actor=editor, action="group.updated")
    assert (change.before["name"], change.after["name"]) == ("First rota", "Second rota")
    event = AuditEvent.objects.get(actor=person, action="setup.account_roles_added")
    before, after = set(event.before["groups"]), set(event.after["groups"])
    assert (after - before, before - after) == ({"technical"}, set())


def test_a_whole_number_revision_accepted_by_the_form_is_current_on_an_invalid_edit(
    client, agent, clinic
):
    from django.urls import reverse

    from clients import services as directory
    from clients.tests.test_views import edit_data

    client.force_login(agent)
    url = reverse("clients:client-edit", args=[clinic.pk])
    drawn = client.get(url).context["form"]["expected_revision"].value()
    same_revision = f"{drawn}.00"
    data = edit_data(
        clinic, expected_revision=same_revision, email="not an email", notes="New notes"
    )
    refused = client.post(url, data)
    assert refused.status_code == 200
    form = refused.context["form"]
    assert (
        form.has_error("email", "invalid")
        and form.cleaned_data["expected_revision"] == clinic.revision
    )
    clinic.refresh_from_db()
    assert clinic.revision == int(drawn) and clinic.notes != data["notes"]
    # Fix just the email. The same posted revision is valid for the real save.
    assert client.post(url, {**data, "email": "new@example.invalid"}).status_code == 302
    clinic.refresh_from_db()
    assert (clinic.revision, clinic.notes) == (int(drawn) + 1, data["notes"])
    assert directory.CHANGED not in form.non_field_errors(), form.errors


@pytest.mark.parametrize("revision_input", ["fieldless", "not a number"])
def test_an_edit_with_no_readable_revision_does_not_claim_a_colleague_changed_the_organization(
    agent, clinic, settings, revision_input
):
    from django.test import Client as HttpClient
    from django.urls import reverse

    from clients import services as directory
    from clients.tests.test_views import edit_data
    from core.models import AuditEvent

    http = HttpClient(enforce_csrf_checks=True)
    http.force_login(agent)
    url = reverse("clients:client-edit", args=[clinic.pk])
    page = http.get(url)
    assert page.status_code == 200
    revision = int(page.context["form"]["expected_revision"].value())
    event_count = AuditEvent.objects.count()
    data = (
        {} if revision_input == "fieldless" else edit_data(clinic, expected_revision="not a number")
    )
    refused = http.post(url, data, HTTP_X_CSRFTOKEN=http.cookies[settings.CSRF_COOKIE_NAME].value)
    assert refused.status_code == 200
    form = refused.context["form"]
    if revision_input == "not a number":
        assert form.has_error("expected_revision", "invalid")
    clinic.refresh_from_db()
    assert (clinic.revision, AuditEvent.objects.count()) == (revision, event_count)
    assert directory.CHANGED not in form.non_field_errors(), form.errors


@pytest.mark.parametrize("action", ["edit", "create"])
def test_returning_to_the_organization_form_after_a_field_error_restores_its_initial_state(
    client, agent, clinic, action
):
    from django.urls import reverse

    from clients.models import Client
    from core.models import AuditEvent

    client.force_login(agent)
    url = reverse(f"clients:client-{action}", args=[clinic.pk] if action == "edit" else [])
    first = client.get(url)
    assert first.status_code == 200
    baseline = first.context["form"]
    events = AuditEvent.objects.count()
    count = Client.objects.count()
    data = {"email": "not an address", "notes": "Unsubmitted notes"}
    if action == "edit":
        data["expected_revision"] = baseline["expected_revision"].value()
    refused = client.post(url, data)
    assert refused.status_code == 200
    bound = refused.context["form"]
    assert bound.has_error("name", "required") and bound.has_error("email", "invalid")
    assert bound["notes"].value() == data["notes"]
    again = client.get(url)
    assert again.status_code == 200
    fresh = again.context["form"]
    assert not fresh.is_bound and not fresh.errors
    for name in ("name", "notes", "email"):
        assert fresh[name].value() == baseline[name].value()
    if action == "edit":
        assert fresh["expected_revision"].value() == baseline["expected_revision"].value()
    assert (Client.objects.count(), AuditEvent.objects.count()) == (count, events)


@pytest.fixture
def third_portal_case(client, agent, category, clinic, clinic_portal):
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    ticket = new_request(agent, category, client=clinic)
    client.force_login(clinic_portal)
    detail = reverse("portal:request-detail", args=[ticket.number])
    page = client.get(detail)
    return (
        ticket,
        detail,
        page.context["reply_url"],
        page.context["reply_form"]["expected_revision"].value(),
    )


def _third_portal_file():
    return SimpleUploadedFile("replacement-label.txt", b"Label for ORD-TEST-000713", "text/plain")


def _third_portal_staff_reply(agent, ticket):
    third_portal_services.add_entry(
        actor=agent,
        request=ticket,
        kind=EntryKind.REPLY,
        body="The package arrived.",
        expected_revision=ticket.revision,
    )


def test_open_ticket_recovery_with_the_displayed_file_reminder_delivers_the_attachment(
    client, agent, third_portal_case
):
    """Follow the recovery page for a genuinely undelivered reply with a file."""
    ticket, detail, url, revision = third_portal_case
    _third_portal_staff_reply(agent, ticket)
    body = "Please check this copy of ORD-TEST-000714."
    refused = client.post(
        url, {"body": body, "expected_revision": revision, "files": _third_portal_file()}
    )
    form = refused.context["reply_form"]
    assert refused.status_code == 200 and form.non_field_errors()
    assert form["files"].value() == ["replacement-label.txt"]
    assert not RequestEntry.objects.filter(request=ticket, body=body).exists()
    inline = " ".join(form.non_field_errors()).lower()
    copied = form["body"].value()
    listing = refused.context["list_url"]
    assert f'href="{listing}"'.encode() in refused.content
    assert f'href="{detail}">{ticket.number}</a>'.encode() in client.get(listing).content
    fresh = client.get(detail)
    assert not fresh.context["reply_form"]["files"].value()
    assert b"replacement-label.txt" not in fresh.content
    assert body.encode() not in fresh.content
    fields = {
        "body": copied,
        "expected_revision": fresh.context["reply_form"]["expected_revision"].value(),
    }
    assert "file" in inline or b"Choose files again" in refused.content
    fields["files"] = _third_portal_file()
    sent = client.post(fresh.context["reply_url"], fields)
    assert sent.status_code == 302
    delivered = RequestEntry.objects.get(request=ticket, body=body)
    assert list(delivered.attachments.values_list("original_name", flat=True)) == [
        "replacement-label.txt"
    ], inline


def test_closed_ticket_lost_acknowledgment_gives_the_stop_condition_before_a_follow_up(
    client, agent, category, third_portal_case
):
    """Follow the refusal notice after the original delivery succeeded and the lab closed it."""
    ticket, _, url, revision = third_portal_case
    body = "Here is the corrected label for ORD-TEST-000715."
    data = {"body": body, "expected_revision": revision}
    assert client.post(url, {**data, "files": _third_portal_file()}).status_code == 302
    ticket.refresh_from_db()
    ticket = third_portal_services.change_status(
        actor=agent,
        request=ticket,
        expected_revision=ticket.revision,
        status=Status.RESOLVED,
        summary="The label was received",
    )
    third_portal_services.change_status(
        actor=agent,
        request=ticket,
        expected_revision=ticket.revision,
        status=Status.CLOSED,
    )
    replay = client.post(url, {**data, "files": _third_portal_file()})
    assert replay.status_code == 200 and replay.context["is_closed"]
    assert any(
        entry.get("body") == body and entry["from_client"]
        for entry in replay.context["conversation"]
    )
    entries = RequestEntry.objects.filter(body=body)
    assert entries.count() == 1 and Attachment.objects.filter(entry__in=entries).count() == 1
    panel = third_portal_main(replay).partition("Reply not sent")[2].lower()
    if "follow-up ticket" in panel and not any(
        term in panel
        for term in (
            "already sent",
            "already there",
            "already shows",
            "already in",
            "do not send",
            "do not resend",
        )
    ):
        # The warning says to create a follow-up. Use its link, form token and copied text.
        follow = reverse("portal:request-create", query={"follow_up": ticket.number})
        assert f'href="{follow}"'.encode() in replay.content
        page = client.get(follow)
        fields = {
            "category": category.pk,
            "category_terms": page.context["category_terms"],
            "subject": "Replacement label",
            "description": replay.context["unsent_reply"]["body"].value(),
            "follow_up": page.context["form"]["follow_up"].value(),
            "files": _third_portal_file(),
        }
        assert client.post(follow, fields).status_code == 302
    assert (entries.count(), Attachment.objects.filter(entry__in=entries).count()) == (1, 1)


def test_file_validation_then_a_stale_refusal_keeps_the_two_explanations_distinct(
    client, agent, third_portal_case
):
    ticket, _, url, revision = third_portal_case
    data = {"body": "See the paper label for ORD-TEST-000716.", "expected_revision": revision}
    invalid = client.post(url, {**data, "files": SimpleUploadedFile("replacement-label.txt", b"")})
    assert invalid.status_code == 200
    form = invalid.context["reply_form"]
    assert "files" in form.errors and not form.non_field_errors()
    assert form["body"].value() == data["body"]
    _third_portal_staff_reply(agent, ticket)
    corrected = client.post(
        url, {**data, "files-previous": form["files"].value(), "files": _third_portal_file()}
    )
    assert corrected.status_code == 200
    corrected_form = corrected.context["reply_form"]
    warnings = " ".join(corrected_form.non_field_errors()).lower()
    assert "files" not in corrected_form.errors
    assert corrected_form["files"].value() == ["replacement-label.txt"]
    assert "tickets" in warnings and "do not send it again" in warnings
    assert not RequestEntry.objects.filter(request=ticket, body=data["body"]).exists()
    assert not Attachment.objects.filter(request=ticket).exists()
