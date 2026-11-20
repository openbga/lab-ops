"""Verification of the first ticket-page and jobs-account review, on synthetic records."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from django.test import Client as Browser
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from attachments import services as files
from attachments.models import Attachment, AttachmentBlob
from clients.models import ClientSite, PortalAccess
from core import changelog, concurrency, preferences
from core.models import AuditEvent, UserPreference
from core.templatetags.whats_new import whats_new_button
from desk import services as desk
from desk.models import ComplaintReview, EntryKind, ServiceRequest, Status
from jobs import actor as job_actor
from jobs.actor import system_account
from jobs.models import Notice
from links import services as links
from links.models import ExternalSystem, LinkAttachment
from links.seed import seed_request_link
from request_forms.models import FieldKind, RequestTemplate
from request_forms.tests.test_values import current, portal_raise
from service_levels.jobs import notice_breaches
from service_levels.models import RequestClock, ServiceTarget

from .test_auto_close import resolve
from .test_contract import new_request
from .test_review_assignment_lock_order_verify3 import (
    Held,
    Session,
    _blocked_by,
    _deactivate_by,
    paused,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def local_mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


def committed(work):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        reader = cursor.fetchone()[0]
    writer = Session(work).start()
    assert writer.join() == "done", writer.error
    assert reader != writer.pid
    return writer.result


def wait_for_writer(held, work):
    holder = Held(held)
    other = Session(work).start()
    try:
        state = other.waiting_or_ended()
        if state == "waiting":
            assert _blocked_by(other.pid, holder.session.pid)
    finally:
        holder.release()
        other.join()
    assert holder.session.outcome == "done", holder.session.error
    return state, other


@pytest.fixture
def ticket(agent, category, clinic, clinic_portal):
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    return new_request(agent, category, client=clinic)


@pytest.fixture
def system():
    return ExternalSystem.objects.create(key="delivery", name="Delivery records", kind="other")


def revision(ticket):
    return ServiceRequest.objects.values_list("revision", flat=True).get(pk=ticket.pk)


def add(agent, ticket, system, identifier="ORD-TEST-000941", **kwargs):
    attachment, _ = links.add_link(
        actor=agent,
        request_id=ticket.pk,
        system=system,
        record_type="order",
        identifier=identifier,
        **kwargs,
    )
    return attachment


def detail(ticket):
    return reverse("desk:request-detail", args=[ticket.pk])


def shown_url(attachment):
    return reverse("link_records:attachment-shown", args=[attachment.pk])


def form_hidden(page):
    form = page.context["form"]
    return {field.name: field.value() or "" for field in form.hidden_fields()}


def messages_for(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


@pytest.mark.parametrize("form_name", ["add", "remove", "correct"])
def test_open_forms_after_a_link_is_added(
    form_name, client, agent, category, clinic_portal, system
):
    template = RequestTemplate.objects.create(key="delivery", name="Delivery", category=category)
    template.fields.create(key="route", label="Route", kind=FieldKind.SHORT_TEXT)
    ticket = portal_raise(clinic_portal, template, {"route": "North"})
    existing = add(agent, ticket, system)
    client.force_login(agent)
    routes = {
        "add": reverse("link_records:request-link-add", args=[ticket.pk]),
        "remove": reverse("link_records:attachment-remove", args=[existing.pk]),
        "correct": reverse("request_details:edit", args=[ticket.number]),
    }
    url = routes[form_name]
    page = client.get(url)
    assert page.status_code == 200
    data = form_hidden(page)
    if form_name == "add":
        data.update(
            system=system.pk, record_type="order", identifier="ORD-TEST-000942", role="related"
        )
    elif form_name == "remove":
        data["reason"] = "Delivery complete"
    else:
        data.update({"field-route": "West", "reason": "Delivery moved"})
    committed(lambda: add(agent, ticket, system, "ORD-TEST-000943"))
    before = revision(ticket)
    reply = client.post(url, data)
    assert reply.status_code < 500
    existing.refresh_from_db()
    # Adapted (verification, finding V1): these pages check what they change, not the whole
    # ticket (docs/pages/links/request-link-add.json "History", attachment-remove.json "Remove
    # link", request_forms request-details-edit.json): after an unrelated link is added, adding
    # another, removing the unchanged one and correcting a field each save, as one change.
    assert reply.status_code == 302, reply.content[:2000]
    saved = {
        "add": LinkAttachment.objects.filter(link__identifier="ORD-TEST-000942").exists(),
        "remove": existing.removed_at is not None,
        "correct": current(ticket)["route"].value == "West",
    }
    assert saved[form_name] and revision(ticket) == before + 1, saved


@pytest.mark.parametrize("change", ["ordinary", "removed", "closed"])
def test_show_from_a_changed_ticket_explains_the_stale_page(client, agent, ticket, system, change):
    ticket = resolve(agent, ticket, days_ago=1)
    attachment = add(agent, ticket, system)
    client.force_login(agent)
    page = client.get(detail(ticket))
    initial = page.context["req"].revision
    assert shown_url(attachment).encode() in page.content
    if change == "removed":
        committed(
            lambda: links.remove_link(
                actor=agent, attachment_id=attachment.pk, reason="Delivery complete"
            )
        )
    elif change == "closed":
        committed(
            lambda: desk.change_status(
                actor=agent, request=ticket, expected_revision=initial, status=Status.CLOSED
            )
        )
    else:
        committed(
            lambda: desk.add_entry(
                actor=agent,
                request=ticket,
                expected_revision=initial,
                kind=EntryKind.NOTE,
                body="Collection confirmed",
            )
        )
    before = revision(ticket)
    result = client.post(shown_url(attachment), {"shown": "1", "expected_revision": initial})
    attachment.refresh_from_db()
    assert (
        result.status_code == 302 and not attachment.shown_to_client and revision(ticket) == before
    )
    assert desk.STALE in messages_for(result)


@pytest.mark.parametrize("choice", [None, "unknown"])
def test_missing_or_unrecognized_visibility_choice_keeps_the_current_setting(
    client, agent, ticket, system, choice
):
    attachment = add(agent, ticket, system, shown_to_client=True)
    client.force_login(agent)
    page = client.get(detail(ticket))
    start = page.context["req"].revision
    fields = {"expected_revision": start}
    if choice is not None:
        fields["shown"] = choice
    response = client.post(shown_url(attachment), fields)
    assert response.status_code < 500
    attachment.refresh_from_db()
    assert (attachment.shown_to_client, revision(ticket)) == (True, start)


@pytest.mark.parametrize("action", ["assign", "status", "complaint", "bulk"])
def test_ticket_revision_forms_refuse_after_a_link_change(
    client, ticket, make_user, agent, system, action, accessioning, join
):
    lead = make_user("dispatch_lead", "supervisor")
    join(lead, accessioning)
    ticket = desk.set_complaint(
        actor=lead,
        request=ticket,
        expected_revision=revision(ticket),
        is_complaint=True,
        reason="Review collection",
    )
    client.force_login(lead)
    before = revision(ticket)
    if action == "bulk":
        url = reverse("desk:request-list") + "?view=all"
        listing = client.get(url)
        selection = f"{ticket.pk}:{before}"
        assert selection.encode() in listing.content
        data = {"action": "status", "status": "waiting_client", "requests": [selection]}
    else:
        route = {
            "assign": "request-assign",
            "status": "request-status",
            "complaint": "complaint-record",
        }[action]
        url = reverse(f"desk:{route}", args=[ticket.pk])
        page = client.get(url)
        assert page.status_code == 200
        data = form_hidden(page)
        assert int(data["expected_revision"]) == before
        data.update(
            {
                "assign": {"assign_to": f"{accessioning.pk}:{lead.pk}", "reason": "Collect parcel"},
                "status": {"status": "waiting_client"},
                "complaint": {"findings": "Collection checked"},
            }[action]
        )
    committed(lambda: add(agent, ticket, system))
    after_link = revision(ticket)
    response = client.post(url, data, follow=True)
    assert response.status_code < 500
    ticket.refresh_from_db()
    assert ticket.status == Status.OPEN and ticket.assignee_id is None
    assert not ComplaintReview.objects.filter(
        request=ticket, findings="Collection checked"
    ).exists()
    assert ticket.revision == after_link == before + 1
    assert b"changed" in response.content.lower()


@pytest.mark.parametrize("audience", ["staff", "portal"])
@pytest.mark.parametrize("atomic", [False, True], ids=["default", "atomic-requests"])
@pytest.mark.parametrize("outcome", ["accepted", "failure-on-second-file"])
def test_multi_file_post_commits_the_entire_change(
    client, ticket, clinic_portal, agent, audience, atomic, outcome, monkeypatch
):
    client.force_login(agent if audience == "staff" else clinic_portal)
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", atomic)
    url = (
        reverse("desk:request-entry", args=[ticket.pk])
        if audience == "staff"
        else reverse("portal:request-reply", args=[ticket.number])
    )
    before = (
        ticket.entries.count(),
        Attachment.objects.count(),
        AttachmentBlob.objects.count(),
        AuditEvent.objects.count(),
        Notice.objects.count(),
        revision(ticket),
    )
    calls = []
    original = files.record_event

    def audit(**kwargs):
        result = original(**kwargs)
        if kwargs["action"] == "attachment.added":
            calls.append(kwargs["obj"].pk)
            if outcome != "accepted" and len(calls) == 2:
                raise ValidationError("The second delivery slip could not be stored.")
        return result

    with patch.object(files, "record_event", audit):
        reply = client.post(
            url,
            {
                "kind": "reply",
                "body": "Enclosed delivery slips",
                "expected_revision": before[-1],
                "files": [
                    SimpleUploadedFile(f"{name}.txt", name.encode()) for name in ("north", "west")
                ],
            },
        )
    assert len(calls) == 2
    if outcome == "accepted":
        assert reply.status_code == 302
        entry = ticket.entries.get(body="Enclosed delivery slips")
        assert entry.attachments.count() == 2 and AttachmentBlob.objects.count() == before[2] + 2
        assert (
            AuditEvent.objects.filter(
                action="attachment.added", object_id__in=[str(pk) for pk in calls]
            ).count()
            == 2
        )
        assert revision(ticket) == before[-1] + 1
    else:
        assert reply.status_code == (400 if audience == "staff" else 200)
        assert (
            b"second delivery slip" in reply.content and b"Enclosed delivery slips" in reply.content
        )
        assert (
            ticket.entries.count(),
            Attachment.objects.count(),
            AttachmentBlob.objects.count(),
            AuditEvent.objects.count(),
            Notice.objects.count(),
            revision(ticket),
        ) == before


@pytest.mark.parametrize("initial", ["absent", "malformed"])
def test_first_shell_at_the_preference_limit(client, agent, ticket, initial, monkeypatch):
    monkeypatch.setattr(preferences, "MAX_PREFERENCES", 1)
    preference = preferences.WHATS_NEW_KEY
    if initial == "absent":
        UserPreference.objects.create(user=agent, key="nav.collapsed", value=True)
    else:
        UserPreference.objects.create(user=agent, key=preference, value="old format")
    client.force_login(agent)
    assert client.get(detail(ticket)).status_code == 200
    stored = UserPreference.objects.get(user=agent)
    assert (stored.key, stored.value) == (
        (preference, changelog.seen_marker(changelog.entries()))
        if initial == "malformed"
        else ("nav.collapsed", True)
    )


@pytest.mark.parametrize("hold_link", [True, False], ids=["link-first", "assignment-first"])
def test_link_and_assignment_serialize_on_the_ticket(
    agent, make_user, join, ticket, system, hold_link
):
    assignee = make_user("delivery_assignee", "client_services")
    join(assignee, ticket.team)
    before = revision(ticket)

    def assign():
        return desk.assign(
            actor=agent,
            request=ticket,
            team=ticket.team,
            assignee=assignee,
            expected_revision=before,
        )

    def attach():
        return add(agent, ticket, system)

    state, other = wait_for_writer(attach if hold_link else assign, assign if hold_link else attach)
    assert state == "waiting"
    ticket.refresh_from_db()
    assert ticket.link_attachments.count() == 1
    if hold_link:
        assert other.outcome == "refused" and desk.STALE in other.error.messages
        assert ticket.assignee_id is None and ticket.revision == before + 1
    else:
        assert other.outcome == "done", other.error
        assert ticket.assignee_id == assignee.pk and ticket.revision == before + 2


def test_link_commits_before_its_actors_deactivation(ticket, agent, make_user, system):
    owner = make_user("delivery_owner", "admin", is_superuser=True, is_staff=True)
    before = revision(ticket)
    state, save = wait_for_writer(lambda: add(agent, ticket, system), _deactivate_by(agent, owner))
    assert state == "waiting" and save.outcome == "done", save.error
    agent.refresh_from_db()
    assert not agent.is_active
    assert ticket.link_attachments.get().added_by_id == agent.pk
    assert AuditEvent.objects.filter(action="external_link.attached", actor=agent).count() == 1
    assert revision(ticket) == before + 1


def test_breach_skips_ticket_held_by_link_writer(agent, category, system):
    ServiceTarget.objects.create(name="Collection time", resolution_minutes=20)
    ticket = new_request(agent, category)
    RequestClock.objects.filter(request=ticket).update(
        resolution_due_at=timezone.now() - timedelta(minutes=2)
    )
    system_account()
    state, sweep = wait_for_writer(lambda: add(agent, ticket, system), notice_breaches)
    assert state == "ended" and sweep.outcome == "done", sweep.error
    assert sweep.result["busy"] == 1 and sweep.result["breaches"] == 0
    assert notice_breaches()["breaches"] == 1 and notice_breaches()["breaches"] == 0


def test_visibility_waiter_rechecks_the_page_after_the_other_post_commits(
    client, agent, ticket, system
):
    first = add(agent, ticket, system)
    second = add(agent, ticket, system, "ORD-TEST-000944")
    client.force_login(agent)
    before = client.get(detail(ticket)).context["req"].revision
    other_tab = Browser()
    other_tab.force_login(agent)
    data = {"expected_revision": before, "shown": "1"}
    state, second_post = wait_for_writer(
        lambda: client.post(shown_url(first), data), lambda: other_tab.post(shown_url(second), data)
    )
    assert state == "waiting" and second_post.outcome == "done", second_post.error
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.shown_to_client and not second.shown_to_client
    assert desk.STALE in messages_for(second_post.result) and revision(ticket) == before + 1


def test_seeded_person_reference_advances_only_on_permitted_changes(
    client, make_user, agent, ticket, system, caplog
):
    capable = make_user("delivery_reviewer", "client_services")
    capable.user_permissions.add(Permission.objects.get(codename="view_patient_information"))
    before = revision(ticket)
    identifier = "ORD-TEST-000946"
    item, created = seed_request_link(
        ticket, actor=capable, system_key=system.key, record_type="patient", identifier=identifier
    )
    assert created
    added = revision(ticket)
    client.force_login(agent)
    assert (
        client.post(shown_url(item), {"shown": "1", "expected_revision": added}).status_code == 404
    )
    client.force_login(capable)
    reply = client.post(shown_url(item), {"shown": "1", "expected_revision": added})
    assert reply.status_code == 302
    assert not LinkAttachment.objects.get(pk=item.pk).shown_to_client
    assert revision(ticket) == added
    remove_url = reverse("link_records:attachment-remove", args=[item.pk])
    page = client.get(remove_url)
    assert page.status_code == 200
    removed = client.post(remove_url, {**form_hidden(page), "reason_code": "entered_in_error"})
    assert removed.status_code == 302
    item.refresh_from_db()
    assert item.removed_at and item.removed_by_id == capable.pk
    audit = AuditEvent.objects.filter(action__startswith="external_link.")
    assert audit.filter(action="external_link.detached", actor=capable).exists()
    assert identifier not in str(list(audit.values("before", "after", "reason")))
    assert identifier not in caplog.text + str(messages_for(reply)) + str(messages_for(removed))
    assert (added, revision(ticket)) == (before + 1, before + 2)


@pytest.mark.parametrize("audience", ["nonmember", "member", "roleless", "client", "inactive"])
def test_show_on_a_restricted_ticket_follows_the_posters_access(
    client, make_user, join, agent, quality, privacy_category, clinic_portal, system, audience
):
    join(agent, quality)
    ticket = new_request(agent, privacy_category)
    item = add(agent, ticket, system)
    viewer = (
        clinic_portal
        if audience == "client"
        else make_user("delivery_viewer", *([] if audience == "roleless" else ["client_services"]))
    )
    if audience == "member":
        join(viewer, quality)
    client.force_login(viewer)
    if audience == "inactive":
        viewer.is_active = False
        viewer.save()
    before = revision(ticket)
    response = client.post(shown_url(item), {"shown": "1", "expected_revision": before})
    item.refresh_from_db()
    if audience == "member":
        assert response.status_code == 302 and item.shown_to_client
        assert revision(ticket) == before + 1
    else:
        assert (
            response.status_code
            == {"nonmember": 404, "roleless": 403, "client": 403, "inactive": 302}[audience]
        )
        assert not item.shown_to_client and revision(ticket) == before
        assert item.link.identifier not in response.content.decode() + str(messages_for(response))


# Adapted (verification, finding V6): "suffixed" forced the draw to equal what the person
# registers; the roadmap now states the guarantee for the old name, as the architecture does.
@pytest.mark.parametrize("case", ["exact", "capitals"])
@pytest.mark.parametrize("namespace", ["legacy"])
def test_roadmap_concurrent_registration_guarantee(monkeypatch, namespace, case):
    draws = iter(["aabbccdd", "bbccddee"])
    monkeypatch.setattr(
        job_actor, "secrets", SimpleNamespace(token_hex=lambda size: next(draws)), raising=False
    )
    chosen = "scheduled-jobs" if namespace == "legacy" else "scheduled-jobs-aabbccdd"
    registered = chosen if case == "exact" else chosen.upper()
    state, job = wait_for_writer(
        lambda: User.objects.create_user(registered, "dispatch@example.invalid"), system_account
    )
    assert job.outcome == "done", job.error
    person = User.objects.get(username=registered)
    assert AuditEvent.objects.get(action="jobs.system_account_created").actor_id == job.result.pk
    assert person.pk != job.result.pk
    assert (state, job.result.username.casefold() != registered.casefold()) == ("ended", True)


def test_nested_reading_discards_rolled_back_mark_and_leaves_same_backend_ready(agent, make_user):
    other = make_user("delivery_reader", "client_services")

    def render(user):
        request = RequestFactory().get("/")
        request.user = user
        return whats_new_button({"request": request})

    def backend():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid(), current_setting('transaction_isolation')")
            return cursor.fetchone()

    initial = backend()
    marks = UserPreference.objects.filter(key=preferences.WHATS_NEW_KEY)
    with concurrency.one_reading():
        with pytest.raises(ValueError, match="cancel reading"):
            with transaction.atomic(), concurrency.one_reading():
                render(agent)
                raise ValueError("cancel reading")
        render(other)
        assert not marks.exists()
    assert list(marks.values_list("user_id", flat=True)) == [other.pk]
    assert backend() == initial and connection.get_autocommit()


def test_malformed_first_mark_keeps_newer_explicit_repair(client, agent, ticket):
    marker = UserPreference.objects.create(
        user=agent, key=preferences.WHATS_NEW_KEY, value="old format"
    )
    client.force_login(agent)
    second = Browser()
    second.force_login(agent)
    entries = changelog.entries()
    older = entries[1:]
    assert changelog.seen_marker(entries) != changelog.seen_marker(older)
    visits = []

    def explicit_visit():
        def visit_and_read_mark():
            response = second.get(reverse("whats-new"))
            return response.status_code, UserPreference.objects.get(pk=marker.pk).value

        with patch.object(changelog, "entries", return_value=entries):
            status, stored = committed(visit_and_read_mark)
        visits.append(status)
        assert stored == changelog.seen_marker(entries)

    with (
        patch.object(changelog, "entries", return_value=older),
        paused(changelog, "seen_marker", explicit_visit, before=False),
    ):
        assert client.get(detail(ticket)).status_code == 200
    marker.refresh_from_db()
    assert visits == [200] and marker.value == changelog.seen_marker(entries)


def test_open_removal_form_cannot_remove_a_reference_just_shown_to_the_client(
    client, agent, ticket, system
):
    item = add(agent, ticket, system)
    client.force_login(agent)
    url = reverse("link_records:attachment-remove", args=[item.pk])
    page = client.get(url)
    assert page.status_code == 200
    fields = {**form_hidden(page), "reason": "Retire draft reference"}
    committed(lambda: links.set_shown_to_client(actor=agent, attachment_id=item.pk, shown=True))
    before = revision(ticket)
    response = client.post(url, fields)
    item.refresh_from_db()
    assert response.status_code < 500 and item.shown_to_client
    assert item.removed_at is None and revision(ticket) == before


def test_unissued_positive_revision_leaves_reference_unchanged(client, agent, ticket, system):
    item = add(agent, ticket, system)
    client.force_login(agent)
    before = client.get(detail(ticket)).context["req"].revision
    response = client.post(shown_url(item), {"expected_revision": before + 91, "shown": "1"})
    item.refresh_from_db()
    assert not item.shown_to_client and revision(ticket) == before
    assert desk.STALE in messages_for(response)


@pytest.mark.parametrize(
    "action,account,expected",
    [
        ("entry", "portal", 403),
        ("reply", "staff", 403),
        ("reply", "other_organization", 404),
        ("reply", "other_site", 404),
    ],
)
def test_direct_entry_and_reply_addresses_enforce_audience(
    client,
    make_user,
    agent,
    category,
    clinic,
    clinic_site,
    clinic_portal,
    water,
    action,
    account,
    expected,
    caplog,
):
    ticket = new_request(agent, category, client=clinic, site=clinic_site)
    if account == "staff":
        viewer = agent
    elif account == "other_organization":
        viewer = make_user("other_dispatch", client=water)
        PortalAccess.objects.create(user=viewer, client=water, whole_organization=True)
    else:
        viewer = clinic_portal
        if account == "other_site":
            other_site = ClientSite.objects.create(
                client=clinic, code="EAST", name="East collection"
            )
            access = PortalAccess.objects.create(user=viewer, client=clinic)
            access.sites.add(other_site)
        else:
            PortalAccess.objects.create(user=viewer, client=clinic, whole_organization=True)
    client.force_login(viewer)
    before = (ticket.entries.count(), revision(ticket), AuditEvent.objects.count())
    body = "Blue delivery basket"
    if action == "entry":
        url = reverse("desk:request-entry", args=[ticket.pk])
    else:
        url = reverse("portal:request-reply", args=[ticket.number])
    response = client.post(url, {"kind": "reply", "expected_revision": before[1], "body": body})
    assert response.status_code == expected
    assert (ticket.entries.count(), revision(ticket), AuditEvent.objects.count()) == before
    for text in (body, ticket.subject):
        assert text not in response.content.decode() + str(messages_for(response)) + caplog.text
