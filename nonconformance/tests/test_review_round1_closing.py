"""Closing checks for the C-01 to C-09 resolutions, using synthetic lab records."""

import pytest
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, HttpResponseNotFound
from django.test import Client, override_settings
from django.urls import include, path, reverse

from accounts.models import User
from clients.models import PortalAccess
from core import modules
from core.models import AuditEvent, LabModule
from desk import services as desk
from desk import views as desk_views
from desk.models import ServiceRequest
from nonconformance import services

from . import test_review_round1 as first

pytestmark = pytest.mark.django_db
owned = first.owned

# A second root with the same optional module and a distinguishable ordinary 404.
urlpatterns = [path("quality/", include("nonconformance.urls"))]


def handler404(request, exception):
    return HttpResponseNotFound("Alternate desk error page")


def test_disabled_module_uses_the_active_roots_error_handler(client, settings, monkeypatch):
    settings.DEBUG = False
    monkeypatch.setattr(modules, "_URLCONFS", {})
    url = reverse("nonconformance:list")
    assert client.get(url).status_code == 404  # warm this deployment's filtered URLconf
    with override_settings(ROOT_URLCONF=__name__):
        control = client.get("/unknown-closing-page/")
        off = client.get(url)
    assert control.status_code == off.status_code == 404
    assert control.content == b"Alternate desk error page"
    assert off.content == control.content


@pytest.mark.django_db(transaction=True)
def test_dialog_when_switch_turns_off_at_save_uses_the_disabled_page(
    client, settings, owned, agent, admin, module_on
):
    settings.DEBUG = False
    client.force_login(agent)
    url = reverse("nonconformance:investigation", args=[owned.pk])
    draft = first.form_values(client.get(url))
    headers = {"X-VR-Form": "1"}
    original_revision = owned.revision

    def disable():
        modules.set_enabled(
            actor=admin,
            key="nonconformance",
            enabled=False,
            expected_revision=module_on.revision,
            reason="Pause the exercise",
        )

    with first.at_read(
        lambda sql: "FOR SHARE" in sql and "core_labmodule" in sql, disable, before=True
    ) as committed:
        refused = client.post(url, draft, headers=headers)
    assert committed == [True]
    already_off = client.post(url, draft, headers=headers)
    owned.refresh_from_db()
    assert owned.revision == original_revision
    assert refused.status_code == already_off.status_code == 404
    assert b">Nonconformances<" not in refused.content
    markers = (b"data-vr-form-page", b"Record investigation")
    assert not any(marker in already_off.content for marker in markers)
    assert [marker in refused.content for marker in markers] == [False, False]


def test_production_https_redirect_on_enabled_module_disallows_storage(client, settings, module_on):
    settings.DEBUG = False
    settings.SECURE_SSL_REDIRECT = True
    path = reverse("nonconformance:list")
    response = client.get(path, {"q": "Calibration"})
    assert response.status_code == 301
    assert response["Location"].startswith("https://") and "Calibration" in response["Location"]
    assert "no-store" in response.get("Cache-Control", "")


def test_request_switch_reading_is_released_on_exception(rf, module_on):
    def fail_during_render(request):
        assert modules.enabled_for(request, "nonconformance")
        LabModule.objects.filter(pk=module_on.pk).update(is_enabled=False)
        assert modules.is_enabled("nonconformance")
        raise RuntimeError("Synthetic render failure")

    middleware = modules.ModuleAddressMiddleware(fail_during_render)
    with pytest.raises(RuntimeError, match="Synthetic render failure"):
        middleware(rf.get(reverse("nonconformance:list")))
    assert not modules.is_enabled("nonconformance")


@pytest.mark.django_db(transaction=True)
def test_overlapping_requests_keep_separate_switch_readings(
    client, settings, agent, admin, module_on
):
    settings.DEBUG = False
    client.force_login(agent)
    url = reverse("nonconformance:list")
    second_status = []

    def switch_and_request():
        modules.set_enabled(
            actor=admin,
            key="nonconformance",
            enabled=False,
            expected_revision=module_on.revision,
            reason="End the exercise",
        )
        second_status.append(Client().get(url).status_code)

    with first.at_read(lambda sql: 'FROM "core_labmodule"' in sql, switch_and_request) as committed:
        first_page = client.get(url)
    assert committed == [True]
    assert first_page.status_code == 200 and second_status == [404]
    assert client.get(url).status_code == 404


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["account", "grant"])
def test_portal_reply_rechecks_a_previously_readable_ticket(portal, plain_ticket, change):
    access, _ = PortalAccess.objects.update_or_create(
        user=portal, defaults={"client": portal.client, "whole_organization": True}
    )
    assert desk.portal_requests(portal).filter(pk=plain_ticket.pk).exists()
    before = (plain_ticket.revision, plain_ticket.entries.count(), AuditEvent.objects.count())

    def withdraw():
        if change == "account":
            User.objects.filter(pk=portal.pk).update(is_active=False)
        else:
            PortalAccess.objects.filter(pk=access.pk).update(whole_organization=False)

    with first.at_read(
        lambda sql: "desk_servicerequest" in sql and "FOR UPDATE" in sql, withdraw
    ) as committed:
        with pytest.raises(ServiceRequest.DoesNotExist):
            desk.add_entry(
                actor=portal,
                request=plain_ticket,
                expected_revision=plain_ticket.revision,
                kind="reply",
                body="",
            )
    assert committed == [True]
    plain_ticket.refresh_from_db()
    assert (
        plain_ticket.revision,
        plain_ticket.entries.count(),
        AuditEvent.objects.count(),
    ) == before


@pytest.mark.parametrize("route", ["desk:request-list", "desk:request-bulk"])
def test_bulk_refusal_on_second_row_rolls_back_first_and_displays_message(
    client, monkeypatch, agent, plain_ticket, complaint, quality_team, route
):
    client.force_login(agent)
    rows = sorted([plain_ticket, complaint], key=lambda row: row.pk)
    previous = [(row.team_id, row.assignee_id, row.revision) for row in rows]
    audit_count = AuditEvent.objects.count()
    settle = desk.settle
    saw_first_change = []

    def refuse_second(actor, ticket):
        current = settle(actor, ticket)
        if ticket.pk == rows[1].pk:
            saw_first_change.append(
                ServiceRequest.objects.filter(pk=rows[0].pk, team=quality_team).exists()
            )
            raise ServiceRequest.DoesNotExist
        return current

    monkeypatch.setattr(desk, "settle", refuse_second)
    response = client.post(
        reverse(route),
        {
            "action": "assign",
            "assign_to": f"{quality_team.pk}:",
            "requests": [f"{row.pk}:{row.revision}" for row in rows],
        },
        follow=True,
    )
    assert saw_first_change == [True]
    assert response.status_code == 200
    assert b"A selected ticket is no longer open to you." in response.content
    for row in rows:
        row.refresh_from_db()
    assert [(row.team_id, row.assignee_id, row.revision) for row in rows] == previous
    assert AuditEvent.objects.count() == audit_count


def test_bulk_global_staff_refusal_exits_before_list_redraw(
    client, agent, plain_ticket, monkeypatch
):
    client.force_login(agent)
    refused = []
    redraws = []

    def no_current_staff(actor):
        refused.append(actor.pk)
        raise PermissionDenied("Your account no longer has a staff role.")

    def empty_renderer(*args, **kwargs):
        redraws.append(True)
        return HttpResponse(status=204)

    # Model the service decision on an ordinary no-op assignment. The test account stays active
    # and the renderer returns empty content without evaluating the ticket list.
    monkeypatch.setattr(desk, "current_staff", no_current_staff)
    monkeypatch.setattr(desk_views, "list_response", empty_renderer)
    before = (plain_ticket.revision, AuditEvent.objects.count())
    response = client.post(
        reverse("desk:request-list"),
        {
            "action": "assign",
            "assign_to": f"{plain_ticket.team_id}:",
            "requests": [f"{plain_ticket.pk}:{plain_ticket.revision}"],
        },
    )
    plain_ticket.refresh_from_db()
    assert refused and refused[0] == agent.pk
    assert (plain_ticket.revision, AuditEvent.objects.count()) == before
    assert (response.status_code, redraws) == (403, [])


@pytest.mark.parametrize("reason", ["Source correction", ""], ids=["complete", "missing-reason"])
def test_stale_correction_explains_the_revision_refusal_and_preserves_draft(
    client, owned, agent, lead, reason
):
    client.force_login(agent)
    url = reverse("nonconformance:edit", args=[owned.pk])
    draft = first.form_values(client.get(url))
    services.record_investigation(
        actor=lead,
        nc=owned,
        expected_revision=owned.revision,
        containment="Calibration under review",
        root_cause="",
    )
    owned.refresh_from_db()
    before = owned.revision, AuditEvent.objects.count()
    response = client.post(url, {**draft, "title": "Revised synthetic title", "reason": reason})
    assert response.status_code == 200
    form = response.context["form"]
    if not reason:
        assert "reason" in form.errors
    assert form["title"].value() == "Revised synthetic title"
    assert (
        int(form["expected_revision"].value()) == int(draft["expected_revision"]) < owned.revision
    )
    owned.refresh_from_db()
    assert (owned.revision, AuditEvent.objects.count()) == before
    assert services.STALE in form.non_field_errors()
