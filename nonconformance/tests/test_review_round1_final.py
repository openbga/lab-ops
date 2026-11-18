"""Independent final contract checks for 57460b1. All records and drafts are synthetic."""

from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.http import Http404, HttpResponse
from django.test import Client
from django.urls import resolve, reverse

from accounts.models import User
from core import modules
from core.models import AuditEvent, LabModule
from desk import services as desk
from desk import views as desk_views
from desk.models import ComplaintReview, ServiceRequest
from nonconformance import selectors, services, views
from nonconformance.models import Nonconformance

from . import test_review_round1 as first
from . import test_review_round1_verify as verification

pytestmark = pytest.mark.django_db
owned = first.owned
action = first.action
linked = first.linked
decided = first.decided
nc_url = first.nc_url


def row_lock(table):
    return lambda sql: f'FROM "{table}"' in sql and "UPDATE" in sql and " FOR " in sql


@contextmanager
def scope_expires_at(monkeypatch, service_module, table):
    """Script a policy refusal after the designated lock, using an ordinary readable ticket.

    The actual user's roles, team and ticket access are unchanged. All operations are business
    no-ops too. This checks whether the final decision consults ticket scope at all.
    """
    original = service_module.staff_requests
    reached = False

    def authorized(user):
        qs = original(user)
        return qs.none() if reached else qs

    def observe(execute, sql, params, many, context):
        nonlocal reached
        result = execute(sql, params, many, context)
        if row_lock(table)(sql):
            reached = True
        return result

    monkeypatch.setattr(service_module, "staff_requests", authorized)
    with connection.execute_wrapper(observe):
        yield
    assert reached, f"Expected the {table} lock"


@pytest.mark.parametrize("change", ["link", "unlink", "desk"])
def test_final_guard_checks_current_ticket_scope(
    monkeypatch, owned, linked, plain_ticket, agent, change
):
    # A repeated link, removal without a reason, and unchanged complaint flag are all refusals.
    plain_ticket.refresh_from_db()
    counts = (owned.revision, plain_ticket.revision, AuditEvent.objects.count())
    target = desk if change == "desk" else services
    with scope_expires_at(monkeypatch, target, "desk_servicerequest"):
        with pytest.raises(
            (ValidationError, PermissionDenied, Http404, ServiceRequest.DoesNotExist)
        ) as refused:
            if change == "desk":
                desk.set_complaint(
                    actor=agent,
                    request=plain_ticket,
                    expected_revision=plain_ticket.revision,
                    is_complaint=plain_ticket.is_complaint,
                    reason="Unchanged",
                )
            elif change == "link":
                services.link_ticket(
                    actor=agent, nc=owned, expected_revision=owned.revision, request=plain_ticket
                )
            else:
                services.unlink_ticket(
                    actor=agent, nc=owned, expected_revision=owned.revision, link=linked, reason=""
                )
    owned.refresh_from_db()
    plain_ticket.refresh_from_db()
    assert (owned.revision, plain_ticket.revision, AuditEvent.objects.count()) == counts
    assert isinstance(refused.value, (Http404, PermissionDenied, ServiceRequest.DoesNotExist))


@pytest.mark.django_db(transaction=True)
def test_unchanged_desk_assignment_checks_staff_after_the_team_lock(complaint, lead):
    before = complaint.revision, AuditEvent.objects.count()

    def retire():
        current = User.objects.get(pk=lead.pk)
        current.is_active = False
        current.save(update_fields=["is_active"])

    with first.at_read(row_lock("teams_team"), retire, before=True) as outcome:
        with pytest.raises((PermissionDenied, ValidationError)) as refused:
            desk.assign(
                actor=lead,
                request=complaint,
                expected_revision=complaint.revision,
                team=complaint.team,
                assignee=complaint.assignee,
            )
    complaint.refresh_from_db()
    assert (complaint.revision, AuditEvent.objects.count()) == before
    if outcome == [True]:
        assert isinstance(refused.value, PermissionDenied), str(refused.value)


@pytest.mark.parametrize(
    "operation", ["create", "from-ticket", "complaint-record", "complaint-review"]
)
def test_global_refusal_in_an_individual_form_never_reaches_its_renderer(
    client, module_on, decided, lead, monkeypatch, operation
):
    # Stop before rendering any page data. The service boundary returns a global refusal.
    client.force_login(lead)
    data = {
        "expected_revision": decided.revision,
        "title": "Example draft",
        "description": "Example",
        "source": "internal",
        "severity": "minor",
        "findings": "Example findings",
        "note": "Example note",
    }
    module, name = {
        "create": (services, "raise_nonconformance"),
        "from-ticket": (services, "open_from_complaint"),
        "complaint-record": (desk, "update_complaint"),
        "complaint-review": (desk, "review_complaint"),
    }[operation]

    def refuse(**kwargs):
        User.objects.filter(pk=lead.pk).update(is_active=False)
        raise PermissionDenied("Your account no longer has a staff role.")

    renderer = Mock(return_value=HttpResponse(status=204))
    monkeypatch.setattr(module, name, refuse)
    monkeypatch.setattr(views if module is services else desk_views, "render", renderer)
    if module is desk:
        url = reverse("desk:" + operation, args=[decided.pk])
    else:
        url = nc_url(operation) if operation == "create" else nc_url(operation, decided.pk)
    response = client.post(url, data)
    assert response.status_code in {403, 404} and not renderer.called


@pytest.mark.parametrize(
    "route", ["investigation", "from-ticket", "complaint-record", "complaint-review"]
)
def test_zero_is_not_a_revision_of_a_drawn_record(client, owned, decided, tech, route):
    # NC and ticket records both start at revision 1. No mutation fields are sent.
    client.force_login(tech)
    if route.startswith("complaint-"):
        url = reverse("desk:" + route, args=[decided.pk])
    else:
        url = nc_url(route, decided.pk if route == "from-ticket" else owned.pk)
    assert client.post(url, {"expected_revision": "0"}).status_code == 403


@pytest.mark.parametrize(
    "route,field", [("complaint-record", "findings"), ("complaint-review", "note")]
)
def test_unmarking_a_readable_ticket_preserves_the_open_desk_draft(
    client, decided, lead, route, field
):
    client.force_login(lead)
    url = reverse("desk:" + route, args=[decided.pk])
    body = first.form_values(client.get(url))
    latest = desk.set_complaint(
        actor=lead,
        request=decided,
        expected_revision=decided.revision,
        is_complaint=False,
        reason="Synthetic reclassification",
    )
    body[field] = "Unsaved example"
    response = client.post(url, body)
    saved = ComplaintReview.objects.get(request=decided)
    decided.refresh_from_db()
    assert decided.revision == latest.revision and not saved.findings and not saved.review_note
    assert response.status_code == 200 and body[field].encode() in response.content


@pytest.mark.django_db(transaction=True)
def test_refused_complaint_review_keeps_its_displayed_facts_together(
    client, module_on, decided, agent, lead
):
    client.force_login(lead)
    marker = "Revised synthetic finding"

    def change_both_facts():
        desk.update_complaint(
            actor=agent, request=decided, expected_revision=decided.revision, findings=marker
        )
        fresh = ServiceRequest.objects.get(pk=decided.pk)
        verification.opened(fresh, agent)

    with first.at_read(
        lambda sql: 'FROM "desk_servicerequest"' in sql and "opened_number" in sql,
        change_both_facts,
    ) as committed:
        page = client.post(
            reverse("desk:complaint-review", args=[decided.pk]),
            {"expected_revision": decided.revision, "note": ""},
        )
    assert page.status_code == 200
    if committed == [True]:
        number = Nonconformance.objects.get(source_request=decided).number
        assert (marker.encode() in page.content) == (number.encode() in page.content)


def test_edit_form_does_not_distinguish_a_hidden_origin_from_manual_complaint_source(
    client, module_on, restricted_ticket, lead, tech
):
    # The same authorized NC owner sees two public Complaint sources and no visible ticket links.
    complaint = desk.set_complaint(
        actor=lead,
        request=restricted_ticket,
        expected_revision=restricted_ticket.revision,
        is_complaint=True,
        reason="Synthetic classification",
    )
    sourced = verification.opened(complaint, lead)
    services.set_owner(actor=lead, nc=sourced, expected_revision=sourced.revision, owner=tech)
    manual = services.raise_nonconformance(
        actor=lead,
        owner=tech,
        title="Example",
        description="Example",
        source="complaint",
        severity="minor",
    )
    client.force_login(tech)
    choices = []
    for nc in (sourced, manual):
        assert not selectors.linked_tickets(tech, nc).exists()
        page = client.get(nc_url("edit", nc.pk))
        assert page.status_code == 200
        choices.append(list(page.context["form"].fields["source"].choices))
    assert choices[0] == choices[1]


def test_current_removal_form_cannot_be_reopened_for_a_historical_link(
    client, owned, linked, agent, lead
):
    services.unlink_ticket(
        actor=lead,
        nc=owned,
        expected_revision=owned.revision,
        link=linked,
        reason="Already removed",
    )
    owned.refresh_from_db()
    client.force_login(agent)
    url = nc_url("ticket-unlink", owned.pk, linked.pk)
    assert client.get(url).status_code == 404
    page = client.post(url, {"expected_revision": owned.revision, "reason": "Example"})
    assert AuditEvent.objects.filter(action="nonconformance.ticket_unlinked").count() == 1
    assert page.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_ticket_menu_and_linked_panel_share_switch_state(
    client, linked, plain_ticket, agent, admin
):
    client.force_login(agent)

    def disable():
        modules.set_enabled(
            actor=admin,
            key="nonconformance",
            enabled=False,
            expected_revision=1,
            reason="Pause quality work",
        )

    with first.at_read(lambda sql: 'FROM "core_labmodule"' in sql, disable):
        page = client.get(reverse("desk:request-detail", args=[plain_ticket.pk]))
    assert page.status_code == 200
    hrefs = {a.get("href") for a in first.PageTags(page).attributes("a")}
    assert (nc_url("list") in hrefs) == (b'id="nonconformances-heading"' in page.content)


def test_module_csrf_refusal_while_enabled_uses_its_privacy_headers(
    module_on, owned, agent, settings
):
    settings.DEBUG = False
    client = Client(enforce_csrf_checks=True)
    client.force_login(agent)
    page = client.post(nc_url("investigation", owned.pk), {"expected_revision": owned.revision})
    assert page.status_code == 403 and owned.title.encode() not in page.content
    assert "no-store" in page.get("Cache-Control", "")


def test_linked_ticket_excludes_shared_http_caches_while_allowing_browser_history(
    client, linked, owned, plain_ticket, agent, settings
):
    settings.DEBUG = False
    client.force_login(agent)
    page = client.get(reverse("desk:request-detail", args=[plain_ticket.pk]))
    assert page.status_code == 200 and owned.title.encode() in page.content
    assert "private" in page.get("Cache-Control", "").lower()


@pytest.mark.parametrize("method", ["get", "head", "post", "put", "delete", "options"])
def test_absent_module_and_unknown_address_match_before_csrf(module_on, method, settings):
    settings.DEBUG = False
    client = Client(enforce_csrf_checks=True)
    path = nc_url("list")
    LabModule.objects.filter(pk=module_on.pk).update(is_enabled=False)
    # Resolved contract (F-10): the address still resolves (URL resolution reads no database);
    # core.modules.ModuleAddressMiddleware answers it, before CSRF, exactly like an unknown one.
    request = getattr(client, method)
    off, unknown = request(path), request("/unknown-example-address/")
    assert off.status_code == unknown.status_code == 404
    assert [t.name for t in off.templates] == [t.name for t in unknown.templates]
    assert off.get("Cache-Control") == unknown.get("Cache-Control")
    LabModule.objects.filter(pk=module_on.pk).update(is_enabled=True)
    assert resolve(path).view_name == "nonconformance:list"
    assert client.get(path).status_code in (200, 302)


@pytest.mark.django_db(transaction=True)
def test_finished_action_edit_refuses_a_retired_actor_before_returning_action_state(
    owned, action, agent, tech
):
    item = action
    services.finish_action(
        actor=tech,
        nc=owned,
        expected_revision=owned.revision,
        action=item,
        done=True,
        note="Finished once",
    )
    owned.refresh_from_db()
    before = owned.revision, AuditEvent.objects.count()

    def retire():
        current = User.objects.get(pk=agent.pk)
        current.is_active = False
        current.save(update_fields=["is_active"])

    with first.at_read(row_lock("nonconformance_correctiveaction"), retire, before=True) as outcome:
        with pytest.raises((ValidationError, PermissionDenied)) as refused:
            services.update_action(
                actor=agent,
                nc=owned,
                expected_revision=owned.revision,
                action=item,
                description=item.description,
                assignee=tech,
                due_on=item.due_on,
                reason="Unchanged",
            )
    owned.refresh_from_db()
    assert (owned.revision, AuditEvent.objects.count()) == before
    if outcome == [True]:
        assert isinstance(refused.value, PermissionDenied), str(refused.value)


def test_view_contract_acknowledges_documented_search_exception(client, owned, agent):
    client.force_login(agent)
    url = nc_url("list")
    search = next(
        f for f in first.PageTags(client.get(url)).attributes("form") if f.get("role") == "search"
    )
    assert search.get("method", "get").lower() == "get"
    page = client.get(search.get("action") or url, {"q": owned.title})
    assert page.status_code == 200 and page.wsgi_request.GET["q"] == owned.title
    assert "nothing typed into a record reaches a message, a URL" not in views.__doc__


@pytest.mark.parametrize("operation", ["findings", "review", "open"])
@pytest.mark.django_db(transaction=True)
def test_complaint_lock_rechecks_supervisory_authority(decided, module_on, lead, tech, operation):
    # The supervisor is never the assignee. Another named staff role survives the downgrade.
    supervisor = lead.groups.get(name="supervisor")
    lead.groups.add(tech.groups.get(name="technical"))
    if operation == "open":
        verification.opened(decided, lead)
        decided.refresh_from_db()
    before = (decided.revision, AuditEvent.objects.count())

    def demote():
        User.objects.get(pk=lead.pk).groups.remove(supervisor)

    with first.at_read(row_lock("desk_complaintreview"), demote, before=True) as outcome:
        with pytest.raises((PermissionDenied, ValidationError)) as refused:
            if operation == "findings":
                desk.update_complaint(
                    actor=lead,
                    request=decided,
                    expected_revision=decided.revision,
                    corrective_action_reference="CA-SYN-0042",
                )
            elif operation == "review":
                desk.review_complaint(
                    actor=lead, request=decided, expected_revision=decided.revision, note=""
                )
            else:
                verification.opened(decided, lead)
    decided.refresh_from_db()
    assert before == (decided.revision, AuditEvent.objects.count())
    if outcome == [True]:
        assert isinstance(refused.value, PermissionDenied)


def test_failed_clearing_audit_rolls_back_action_revision_and_effectiveness(
    owned, agent, tech, monkeypatch
):
    from django.utils import timezone

    nc = services.change_status(
        actor=agent, nc=owned, expected_revision=owned.revision, status="verification"
    )
    nc = services.record_effectiveness(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        result="no_action_needed",
        note="Example signed check",
    )
    before = (
        nc.revision,
        nc.effectiveness,
        nc.effectiveness_note,
        nc.actions.count(),
        AuditEvent.objects.count(),
    )
    audit = services.record_event

    def reject_event(**kwargs):
        if kwargs["action"] == "nonconformance.effectiveness_cleared":
            raise RuntimeError("Synthetic audit refusal")
        return audit(**kwargs)

    monkeypatch.setattr(services, "record_event", reject_event)
    with pytest.raises(RuntimeError, match="Synthetic audit refusal"):
        services.add_action(
            actor=agent,
            nc=nc,
            expected_revision=nc.revision,
            assignee=tech,
            due_on=timezone.localdate(),
            description="Example action",
        )
    nc.refresh_from_db()
    assert (
        nc.revision,
        nc.effectiveness,
        nc.effectiveness_note,
        nc.actions.count(),
        AuditEvent.objects.count(),
    ) == before
