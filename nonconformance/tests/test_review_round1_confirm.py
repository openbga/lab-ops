"""Confirmation of the f221ff5 review resolutions on ordinary synthetic lab records."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse

from core import modules
from core.models import AuditEvent, LabModule
from desk import services as desk
from desk.models import ServiceRequest
from nonconformance import selectors, services

from . import test_review_round1 as first
from . import test_review_round1_final as final
from . import test_review_round1_verify as verification

pytestmark = pytest.mark.django_db
owned = first.owned
linked = first.linked
nc_url = first.nc_url


def test_removed_link_admits_only_a_revision_when_the_link_existed(
    client, owned, linked, agent, lead
):
    client.force_login(agent)
    url = nc_url("ticket-unlink", owned.pk, linked.pk)
    drawn = first.form_values(client.get(url))
    services.unlink_ticket(
        actor=lead,
        nc=owned,
        expected_revision=owned.revision,
        link=linked,
        reason="Removed at review",
    )
    owned.refresh_from_db()
    after_removal = first.form_revision(client.get(nc_url("investigation", owned.pk)))
    services.record_investigation(
        actor=agent,
        nc=owned,
        expected_revision=after_removal,
        containment="Labels held",
        root_cause="Synthetic finding",
    )
    owned.refresh_from_db()
    assert int(drawn["expected_revision"]) < after_removal < owned.revision
    before = AuditEvent.objects.count()
    original = client.post(url, {**drawn, "reason": "Draft from before removal"})
    later = client.post(url, {"expected_revision": after_removal, "reason": "After removal"})
    assert b"Draft from before removal" in original.content
    # Resolved contract (C-04, docs/architecture.md): a form posted with an earlier revision of
    # its record is shown again, to anyone who may still read it, with what was typed and the
    # reason it cannot be saved; nothing is written. The ticket is readable by this reader.
    assert later.status_code == 200 and b"After removal" in later.content
    assert b"no longer linked" in later.content
    assert AuditEvent.objects.count() == before


@pytest.mark.parametrize("route", ["desk:complaint-record", "nonconformance:from-ticket"])
def test_post_from_after_unmark_is_not_an_outstanding_complaint_form(
    client, module_on, complaint, lead, route
):
    client.force_login(lead)
    unmarked = desk.set_complaint(
        actor=lead,
        request=complaint,
        expected_revision=complaint.revision,
        is_complaint=False,
        reason="Routine enquiry",
    )
    desk.add_entry(
        actor=lead,
        request=unmarked,
        expected_revision=unmarked.revision,
        kind="note",
        body="Follow-up on ordinary enquiry",
    )
    url = reverse(route, args=[complaint.pk])
    before = AuditEvent.objects.count()
    assert client.get(url).status_code == 404
    page = client.post(url, {"expected_revision": unmarked.revision})
    assert AuditEvent.objects.count() == before
    # Resolved contract (C-04, docs/architecture.md): an earlier revision is redrawn as stale
    # for a reader who may still read the ticket, writing nothing; a current one is 404.
    assert page.status_code == 200
    current = client.post(
        url, {"expected_revision": complaint.__class__.objects.get(pk=complaint.pk).revision}
    )
    assert current.status_code == 404 and AuditEvent.objects.count() == before


@pytest.mark.parametrize("method", ["get", "post"])
def test_slashless_disabled_module_is_an_ordinary_unknown_address(method, settings, module_on):
    settings.DEBUG = False
    client = Client(enforce_csrf_checks=True)
    LabModule.objects.filter(pk=module_on.pk).update(is_enabled=False)
    path = nc_url("list").rstrip("/")
    submit = getattr(client, method)
    off, unknown = submit(path), submit("/unknown-confirmation-address")
    assert (off.status_code, off.get("Location")) == (unknown.status_code, unknown.get("Location"))


def test_disabled_edit_dialog_gets_the_unknown_address_error_page(
    client, settings, module_on, agent
):
    settings.DEBUG = False
    client.force_login(agent)
    LabModule.objects.filter(pk=module_on.pk).update(is_enabled=False)
    headers = {"X-VR-Form": "1"}
    off = client.get(nc_url("create"), headers=headers)
    unknown = client.get("/unknown-confirmation-address/", headers=headers)
    assert off.status_code == unknown.status_code == 404
    markup = b"data-vr-form-page"
    help_title = b"New nonconformance"
    assert (markup in off.content, help_title in off.content) == (
        markup in unknown.content,
        help_title in unknown.content,
    )
    assert [t.name for t in off.templates] == [t.name for t in unknown.templates]


@pytest.mark.django_db(transaction=True)
def test_quality_get_and_its_error_menu_agree_when_the_switch_changes(
    client, module_on, agent, admin, settings
):
    settings.DEBUG = False
    client.force_login(agent)
    url = nc_url("list")

    def turn_off():
        modules.set_enabled(
            actor=admin,
            key="nonconformance",
            enabled=False,
            expected_revision=module_on.revision,
            reason="Pause quality work",
        )

    with first.at_read(lambda sql: 'FROM "core_labmodule"' in sql, turn_off) as committed:
        page = client.get(url)
    assert page.status_code in {200, 404}
    if committed == [True]:
        hrefs = {tag.get("href") for tag in first.PageTags(page).attributes("a")}
        assert (url in hrefs) == (page.status_code == 200)


@pytest.mark.parametrize("name", ["request-list", "request-bulk"])
def test_bulk_form_handles_a_final_unavailable_ticket(
    client, agent, plain_ticket, monkeypatch, name
):
    client.force_login(agent)
    client.raise_request_exception = False
    before = plain_ticket.revision, AuditEvent.objects.count()
    # Model a scope refusal on an ordinary, readable ticket. The assignment is also a no-op.
    with final.scope_expires_at(monkeypatch, desk, "teams_team"):
        page = client.post(
            reverse("desk:" + name),
            {
                "action": "assign",
                "assign_to": f"{plain_ticket.team_id}:",
                "requests": [f"{plain_ticket.pk}:{plain_ticket.revision}"],
            },
        )
    plain_ticket.refresh_from_db()
    assert (plain_ticket.revision, AuditEvent.objects.count()) == before
    assert page.status_code in {200, 302, 400, 403, 404, 409}


def test_portal_reply_validates_final_ticket_scope_before_empty_body(
    plain_ticket, portal, monkeypatch
):
    from clients.models import PortalAccess

    PortalAccess.objects.update_or_create(
        user=portal, defaults={"client": portal.client, "whole_organization": True}
    )
    assert desk.portal_requests(portal).filter(pk=plain_ticket.pk).exists()
    lock = desk._locked_request

    def lock_and_change_policy(actor, request_id, **kwargs):
        result = lock(actor, request_id, **kwargs)
        # Only the test policy changes. The real portal grant and ticket remain ordinary and valid.
        monkeypatch.setattr(desk, "portal_requests", lambda user: ServiceRequest.objects.none())
        return result

    monkeypatch.setattr(desk, "_locked_request", lock_and_change_policy)
    before = plain_ticket.revision, AuditEvent.objects.count()
    with pytest.raises((ServiceRequest.DoesNotExist, PermissionDenied, ValidationError)) as refused:
        desk.add_entry(
            actor=portal,
            request=plain_ticket,
            expected_revision=plain_ticket.revision,
            kind="reply",
            body="",
        )
    plain_ticket.refresh_from_db()
    assert (plain_ticket.revision, AuditEvent.objects.count()) == before
    assert isinstance(refused.value, (ServiceRequest.DoesNotExist, PermissionDenied)), str(
        refused.value
    )


def test_refused_ticket_entry_keeps_ticket_cache_protection(
    client, linked, owned, plain_ticket, agent
):
    client.force_login(agent)
    body = "Draft delivery note"
    page = client.post(
        reverse("desk:request-entry", args=[plain_ticket.pk]),
        {
            "kind": "note",
            "body": body,
            "expected_revision": plain_ticket.revision,
        },
    )
    assert (
        page.status_code == 400
        and body.encode() in page.content
        and owned.title.encode() in page.content
    )
    assert not plain_ticket.entries.filter(body=body).exists()
    assert {"private", "no-store"} & set(page.get("Cache-Control", "").lower().split(", "))


def test_source_correction_preserves_complaint_and_audits_the_change(
    client, module_on, complaint, agent, lead
):
    nc = verification.opened(complaint, agent)
    complaint.refresh_from_db()
    ticket_version = complaint.revision
    client.force_login(lead)
    url = nc_url("edit", nc.pk)
    body = first.form_values(client.get(url))
    response = client.post(url, {**body, "source": "internal", "reason": "Correct classification"})
    assert response.status_code == 302
    event = AuditEvent.objects.get(action="nonconformance.updated", object_id=str(nc.pk))
    assert (event.before["source"], event.after["source"], event.actor_id) == (
        "complaint",
        "internal",
        lead.pk,
    )
    nc.refresh_from_db()
    complaint.refresh_from_db()
    assert nc.source_request_id == complaint.pk and complaint.revision == ticket_version
    link = selectors.linked_tickets(lead, nc).get(request=complaint)
    with pytest.raises(ValidationError, match="stays linked"):
        services.unlink_ticket(
            actor=lead, nc=nc, link=link, expected_revision=nc.revision, reason="Test"
        )


def test_quality_url_resolution_is_independent_of_the_switch(module_on):
    for enabled in (False, True):
        LabModule.objects.filter(pk=module_on.pk).update(is_enabled=enabled)
        with CaptureQueriesContext(connection) as queries:
            path = nc_url("close", 5)
            assert resolve(path).view_name == "nonconformance:close"
        assert not queries.captured_queries


def test_enabled_module_request_limit_refusal_has_its_normal_privacy_header(module_on, settings):
    # Lower the middleware limit so a short, ordinary synthetic form reaches its early refusal.
    settings.DEBUG = False
    settings.MAX_REQUEST_BODY_BYTES = 4
    page = Client().post(nc_url("create"), {"title": "Example"})
    assert page.status_code == 413 and b"Example" not in page.content
    assert "no-store" in page.get("Cache-Control", "")


def test_reclassifying_a_complaint_source_requires_a_recorded_reason(
    client, module_on, complaint, agent, lead
):
    nc = verification.opened(complaint, agent)
    client.force_login(lead)
    url = nc_url("edit", nc.pk)
    body = first.form_values(client.get(url))
    page = client.post(url, {**body, "source": "internal", "reason": ""})
    nc.refresh_from_db()
    edits = AuditEvent.objects.filter(action="nonconformance.updated", object_id=str(nc.pk))
    if nc.source == "internal":
        event = edits.get()
        assert event.reason.strip(), "The changed classification has no recorded reason"
    else:
        assert nc.source == "complaint" and not edits.exists() and page.status_code == 200


@pytest.mark.django_db(transaction=True)
def test_refused_team_still_rechecks_the_operator_before_business_validation(
    plain_ticket, quality_team, lead
):
    # An inactive destination makes this operation a no-op regardless of the actor's state.
    from accounts.models import User

    quality_team.is_active = False
    quality_team.save(update_fields=["is_active"])
    before = plain_ticket.revision, AuditEvent.objects.count()

    def deactivate():
        actor = User.objects.get(pk=lead.pk)
        actor.is_active = False
        actor.save(update_fields=["is_active"])

    with first.at_read(final.row_lock("teams_team"), deactivate, before=True) as committed:
        with pytest.raises((PermissionDenied, ValidationError)) as refused:
            desk.assign(
                actor=lead,
                request=plain_ticket,
                expected_revision=plain_ticket.revision,
                team=quality_team,
            )
    plain_ticket.refresh_from_db()
    assert before == (plain_ticket.revision, AuditEvent.objects.count())
    if committed == [True]:
        assert isinstance(refused.value, PermissionDenied), str(refused.value)
