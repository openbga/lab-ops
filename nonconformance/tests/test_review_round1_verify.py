"""Independent verification of f926108. All data is synthetic."""

import json
import logging
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.http import HttpResponse
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from core import modules
from core.models import AuditEvent, LabModule
from desk import services as desk
from desk.models import ComplaintReview
from nonconformance import selectors, services, summary, views
from nonconformance.models import Nonconformance, TicketLink
from teams.models import TeamMembership

from . import test_review_round1 as previous

pytestmark = pytest.mark.django_db

# Keep the same fixture identities and bounded, independent PostgreSQL worker as round 1.
owned = previous.owned
action = previous.action
linked = previous.linked
decided = previous.decided
at_read = previous.at_read
nc_url = previous.nc_url
form_values = previous.form_values


def opened(complaint, actor):
    return services.open_from_complaint(
        actor=actor,
        request=complaint,
        expected_revision=complaint.revision,
        title="Example delivery review",
        description="Synthetic observation",
        severity="minor",
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["deactivate", "remove_role"])
def test_finished_action_still_checks_the_actor_after_the_action_lock(owned, action, tech, change):
    # The second finish is always a no-op. Which refusal it gets identifies the guard used.
    services.finish_action(
        actor=tech,
        nc=owned,
        expected_revision=owned.revision,
        action=action,
        done=True,
        note="Finished once",
    )
    owned.refresh_from_db()
    before = (owned.revision, AuditEvent.objects.count())

    def revoke():
        if change == "deactivate":
            current = User.objects.get(pk=tech.pk)
            current.is_active = False
            current.save(update_fields=["is_active"])
        else:
            User.objects.get(pk=tech.pk).groups.clear()

    def child_lock(sql):
        return 'FROM "nonconformance_correctiveaction"' in sql and "FOR UPDATE" in sql

    with at_read(child_lock, revoke, before=True) as committed:
        with pytest.raises((PermissionDenied, ValidationError)) as refused:
            services.finish_action(
                actor=tech,
                nc=owned,
                expected_revision=owned.revision,
                action=action,
                done=True,
                note="Already finished",
            )
    owned.refresh_from_db()
    assert (owned.revision, AuditEvent.objects.count()) == before
    if committed == [True]:
        assert isinstance(refused.value, PermissionDenied), str(refused.value)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("method", ["record", "review"])
def test_complaint_no_op_checks_actor_after_ticket_lock(decided, lead, method):
    actor = lead
    before = (decided.revision, AuditEvent.objects.count())

    def revoke():
        current = User.objects.get(pk=actor.pk)
        current.is_active = False
        current.save(update_fields=["is_active"])

    def ticket_lock(sql):
        return 'FROM "desk_servicerequest"' in sql and "FOR UPDATE" in sql

    with at_read(ticket_lock, revoke, before=True) as committed:
        with pytest.raises((PermissionDenied, ValidationError)) as refused:
            if method == "record":
                desk.update_complaint(
                    actor=actor,
                    request=decided,
                    expected_revision=decided.revision,
                    corrective_action_reference="CA-SYN-0042",
                )
            else:
                desk.review_complaint(
                    actor=actor, request=decided, expected_revision=decided.revision, note=""
                )
    decided.refresh_from_db()
    assert (decided.revision, AuditEvent.objects.count()) == before
    if committed == [True]:
        assert isinstance(refused.value, PermissionDenied), str(refused.value)


@pytest.mark.django_db(transaction=True)
def test_switch_rechecks_active_account_after_switch_lock(admin, module_on):
    def deactivate():
        current = User.objects.get(pk=admin.pk)
        current.is_active = False
        current.save(update_fields=["is_active"])

    with at_read(
        lambda sql: 'FROM "core_labmodule"' in sql and "FOR UPDATE" in sql,
        deactivate,
        before=True,
    ) as committed:
        with pytest.raises((PermissionDenied, ValidationError)) as refused:
            modules.set_enabled(
                actor=admin,
                key="nonconformance",
                enabled=True,
                expected_revision=module_on.revision,
                reason="No state change",
            )
    if committed == [True]:
        assert isinstance(refused.value, PermissionDenied)
    assert LabModule.objects.get(pk=module_on.pk).revision == module_on.revision


@pytest.mark.parametrize("route", ["investigation", "from-ticket"])
@pytest.mark.parametrize("version", ["missing", "invalid", "future", "empty_post"])
def test_only_an_earlier_version_can_keep_an_ineligible_posters_draft(
    client, owned, complaint, tech, route, version
):
    # These posts contain no mutation data. Missing and future versions could not have been drawn.
    client.force_login(tech)
    target = complaint if route == "from-ticket" else owned
    data = {} if version == "empty_post" else {"description": "Synthetic draft"}
    if version in {"invalid", "future"}:
        data["expected_revision"] = "invalid" if version == "invalid" else target.revision + 1
    client.raise_request_exception = False
    before = (Nonconformance.objects.count(), AuditEvent.objects.count())
    reply = client.post(nc_url(route, target.pk), data)
    assert (Nonconformance.objects.count(), AuditEvent.objects.count()) == before
    assert reply.status_code == 403


@pytest.mark.parametrize("route", ["investigation", "from-ticket"])
def test_a_genuinely_stale_ineligible_post_has_no_effect(
    client, owned, complaint, agent, lead, tech, route
):
    target = complaint if route == "from-ticket" else owned
    client.force_login(agent)
    url = nc_url(route, target.pk)
    drawn = form_values(client.get(url))
    if route == "from-ticket":
        desk.assign(
            actor=lead,
            request=target,
            expected_revision=target.revision,
            team=target.team,
            assignee=None,
            reason="Returned to queue",
        )
        drawn.update(title="Sample draft", description="Example narrative", severity="minor")
    else:
        services.set_owner(actor=lead, nc=target, expected_revision=target.revision, owner=tech)
        drawn["containment"] = "Example narrative"
    before = (Nonconformance.objects.count(), AuditEvent.objects.count())
    page = client.post(url, drawn)
    assert (Nonconformance.objects.count(), AuditEvent.objects.count()) == before
    assert page.status_code == 200 and b"Example narrative" in page.content


def test_global_permission_refusal_does_not_redraw_a_staff_form(client, owned, agent, monkeypatch):
    # Isolate exception handling at the service boundary. Stop before rendering any data.
    client.force_login(agent)

    def refuse(**kwargs):
        User.objects.filter(pk=agent.pk).update(is_active=False)
        raise PermissionDenied("Your account no longer has a staff role.")

    renderer = Mock(return_value=HttpResponse(status=204))
    monkeypatch.setattr(services, "record_investigation", refuse)
    monkeypatch.setattr(views, "_render_form", renderer)
    response = client.post(
        nc_url("investigation", owned.pk),
        {"expected_revision": owned.revision, "containment": "Example draft"},
    )
    assert not renderer.called and response.status_code in {403, 404}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("operation", ["ticket-link", "ticket-unlink"])
def test_switch_off_during_save_still_hides_module_page(
    client, owned, linked, complaint, admin, agent, operation
):
    client.force_login(agent)
    if operation == "ticket-link":
        url = nc_url(operation, owned.pk)
        data = {"ticket": complaint.number}
    else:
        url = nc_url(operation, owned.pk, linked.pk)
        data = {"reason": "Remove synthetic link"}
    before = (owned.revision, TicketLink.objects.filter(removed_at__isnull=True).count())

    def disable():
        modules.set_enabled(
            actor=admin,
            key="nonconformance",
            enabled=False,
            expected_revision=1,
            reason="Pause module",
        )

    with at_read(
        lambda sql: "FOR SHARE" in sql and "core_labmodule" in sql, disable, before=True
    ) as committed:
        page = client.post(url, {**data, "expected_revision": owned.revision})
    if committed == [True]:
        owned.refresh_from_db()
        assert (
            owned.revision,
            TicketLink.objects.filter(removed_at__isnull=True).count(),
        ) == before
        assert page.status_code == 404


@pytest.mark.parametrize("state", ["module_off", "missing_record", "role_denied"])
def test_module_error_responses_follow_the_never_store_contract(
    client, owned, agent, no_role, state
):
    if state == "module_off":
        LabModule.objects.update(is_enabled=False)
    client.force_login(no_role if state == "role_denied" else agent)
    pk = owned.pk + 1000 if state == "missing_record" else owned.pk
    response = client.get(nc_url("detail", pk))
    assert response.status_code == (403 if state == "role_denied" else 404)
    if state == "module_off":
        # Resolved contract (V-09/V-10): while the module is off its addresses do not resolve at
        # all, so the answer is the application's ordinary Not found page, the same as for any
        # unknown address, with nothing of the module in it.
        unknown = client.get("/quality-no-such-address/")
        assert [t.name for t in response.templates][:1] == [t.name for t in unknown.templates][:1]
        assert response.get("Cache-Control") == unknown.get("Cache-Control")
        for marker in (owned.title, owned.number):
            assert marker.encode() not in response.content
    else:
        assert "no-store" in response.get("Cache-Control", "")


def test_disabled_address_is_absent_with_production_csrf_middleware(owned, agent):
    client = Client(enforce_csrf_checks=True)
    client.force_login(agent)
    LabModule.objects.update(is_enabled=False)
    # Submission after the module was disabled and the CSRF cookie expired.
    page = client.post(nc_url("investigation", owned.pk), {"expected_revision": owned.revision})
    assert page.status_code == 404


def test_reference_draft_survives_a_complaint_opening(client, module_on, decided, agent, lead):
    client.force_login(agent)
    url = reverse("desk:complaint-record", args=[decided.pk])
    original = form_values(client.get(url))
    opened(decided, lead)
    draft = "CA-EXAMPLE-UNSAVED"
    page = client.post(
        url,
        {**original, "corrective_action_reference": draft, "findings": "Synthetic draft findings"},
    )
    saved = ComplaintReview.objects.get(request=decided)
    assert saved.corrective_action_reference == "CA-SYN-0042" and not saved.findings
    assert page.status_code == 200 and b"Synthetic draft findings" in page.content
    assert draft.encode() in page.content  # read-only display for recovery is sufficient


def test_new_reference_and_findings_are_refused_as_one_edit(
    client, module_on, decided, agent, lead
):
    opened(decided, lead)
    decided.refresh_from_db()
    client.force_login(agent)
    url = reverse("desk:complaint-record", args=[decided.pk])
    response = client.post(
        url,
        {
            "expected_revision": decided.revision,
            "corrective_action_reference": "CA-EXAMPLE-CHANGE",
            "findings": "Findings accompanying the changed reference",
        },
    )
    review = ComplaintReview.objects.get(request=decided)
    assert review.corrective_action_reference == "CA-SYN-0042"
    assert (response.status_code, review.findings) == (200, "")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("route", ["complaint-review", "complaint-record"])
def test_opening_and_ticket_revision_are_read_together(
    client, module_on, decided, lead, agent, route
):
    client.force_login(lead)

    def open_in_other_connection():
        opened(decided, agent)

    with at_read(lambda sql: 'FROM "desk_servicerequest"' in sql, open_in_other_connection):
        page = client.get(reverse("desk:" + route, args=[decided.pk]))
    assert page.status_code == 200
    latest = Nonconformance.objects.filter(source_request=decided).first()
    if route == "complaint-review":
        saw_opening = latest is not None and latest.number.encode() in page.content
    else:
        saw_opening = "corrective_action_reference" not in page.context["form"].fields
    observed = (previous.form_revision(page), saw_opening)
    assert observed in [(decided.revision, False), (decided.revision + 1, True)]


@pytest.mark.django_db(transaction=True)
def test_link_panel_and_complaint_fact_share_the_switch_read(
    client, module_on, decided, agent, admin
):
    nc = opened(decided, agent)
    client.force_login(agent)

    def disable():
        modules.set_enabled(
            actor=admin,
            key="nonconformance",
            enabled=False,
            expected_revision=1,
            reason="Pause module",
        )

    with at_read(lambda sql: 'FROM "core_labmodule"' in sql, disable):
        page = client.get(reverse("desk:request-detail", args=[decided.pk]))
    ctx = page.context
    assert ctx["nonconformance_opened"] == nc and page.status_code == 200
    assert (ctx["nonconformance_module"], list(ctx["nonconformances"])) in [
        (True, [nc]),
        (False, []),
    ]


def test_stale_from_ticket_keeps_draft_if_ticket_remains_readable_but_is_unmarked(
    client, module_on, complaint, agent, lead
):
    client.force_login(agent)
    url = nc_url("from-ticket", complaint.pk)
    drawn = form_values(client.get(url))
    desk.set_complaint(
        actor=lead,
        request=complaint,
        expected_revision=complaint.revision,
        is_complaint=False,
        reason="Classified as another enquiry",
    )
    page = client.post(
        url,
        {**drawn, "title": "Example saved draft", "description": "Example", "severity": "minor"},
    )
    assert not Nonconformance.objects.exists()
    assert b"Example saved draft" in page.content


def test_a_removed_link_keeps_the_second_operators_reason(client, owned, linked, agent, lead):
    client.force_login(agent)
    url = nc_url("ticket-unlink", owned.pk, linked.pk)
    old = form_values(client.get(url))
    services.unlink_ticket(
        actor=lead,
        nc=owned,
        expected_revision=owned.revision,
        link=linked,
        reason="First operator removed link",
    )
    draft = "Second operator's reason"
    page = client.post(url, {**old, "reason": draft})
    assert AuditEvent.objects.filter(action="nonconformance.ticket_unlinked").count() == 1
    assert page.status_code == 200 and page.context["form"]["reason"].value() == draft


@pytest.mark.parametrize("route", ["list", "action-list"])
def test_search_words_in_the_address_are_a_documented_exception_kept_out_of_the_log(
    client, owned, action, lead, route, caplog
):
    """Resolved contract (V-12): lists search by GET, so the words typed into a search go into
    the address, as on every list of the application; the product scope and the list's help say
    so, and the request log records the route only."""
    docs = Path(__file__).resolve().parents[2] / "docs"
    assert "search box go into the page's address" in (docs / "product.md").read_text().replace(
        "\n", " "
    )
    page_help = "list.json" if route == "list" else "action-list.json"
    assert (
        "Search words go into the page address"
        in (docs / "pages" / "nonconformance" / page_help).read_text()
    )
    client.force_login(lead)
    term = owned.title if route == "list" else action.description
    with caplog.at_level(logging.INFO, logger="lab_ops.request"):
        assert client.get(nc_url(route), {"q": term}).status_code == 200
    assert "route=" in caplog.text and term not in caplog.text


def test_summary_uses_the_same_calendar_window_for_area_and_source(owned, lead):
    zone = ZoneInfo("America/New_York")
    following = services.raise_nonconformance(
        actor=lead,
        title="Next month",
        description="Example",
        source="internal",
        severity="minor",
        area="Dispatch",
    )
    Nonconformance.objects.filter(pk=owned.pk).update(raised_at=datetime(2026, 3, 1, tzinfo=zone))
    Nonconformance.objects.filter(pk=following.pk).update(
        raised_at=datetime(2026, 4, 1, tzinfo=zone)
    )
    records = selectors.staff_nonconformances(lead)
    with timezone.override(zone), CaptureQueriesContext(connection) as queries:
        result = summary.counts(records, today=date(2026, 3, 31), months=1)
    assert len(queries) == 1
    assert result["months"][0]["total"] == result["total"] == 1
    assert sum(row["count"] for row in result["areas"]) == result["total"]


@pytest.mark.parametrize("operation", ["add", "edit"])
def test_cleared_check_audits_the_revision_that_contains_the_change(owned, agent, tech, operation):
    nc = services.change_status(
        actor=agent, nc=owned, expected_revision=owned.revision, status="verification"
    )
    values = {"assignee": tech, "due_on": timezone.localdate(), "description": "Check labels"}
    if operation == "edit":
        item = services.add_action(actor=agent, nc=nc, expected_revision=nc.revision, **values)
        nc.refresh_from_db()
    checked = services.record_effectiveness(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        result="no_action_needed",
        note="Checked once",
    )
    if operation == "add":
        services.add_action(actor=agent, nc=checked, expected_revision=checked.revision, **values)
    else:
        services.update_action(
            actor=agent,
            nc=checked,
            expected_revision=checked.revision,
            action=item,
            reason="Expanded follow-up",
            **{**values, "description": "Check labels and dates"},
        )
    cleared = AuditEvent.objects.get(action="nonconformance.effectiveness_cleared")
    nc.refresh_from_db()
    assert cleared.before["revision"] == checked.revision
    assert cleared.before["effectiveness_note"] == "Checked once"
    assert cleared.after["effectiveness"] == nc.effectiveness == ""
    assert cleared.after["revision"] == nc.revision == checked.revision + 1


def test_history_visibility_follows_membership_after_unlink(
    client, owned, lead, tech, restricted_ticket
):
    link = services.link_ticket(
        actor=lead, nc=owned, expected_revision=owned.revision, request=restricted_ticket
    )
    owned.refresh_from_db()
    services.unlink_ticket(
        actor=lead,
        nc=owned,
        expected_revision=owned.revision,
        link=link,
        reason="Synthetic archive decision",
    )
    client.force_login(tech)
    url = nc_url("detail", owned.pk)

    def visible():
        page = client.get(url)
        return {row["title"] for row in page.context["history"]}

    events = {"Ticket linked", "Linked ticket removed"}
    assert visible().isdisjoint(events)
    membership = TeamMembership.objects.create(team=restricted_ticket.team, user=tech)
    assert events <= visible()
    membership.delete()
    assert visible().isdisjoint(events)


def test_edit_help_does_not_identify_an_out_of_scope_ticket_as_the_origin(
    client, module_on, restricted_ticket, lead, tech
):
    # Staff may edit an NC they own. Examine only the form's help for its public Source field.
    complaint = desk.set_complaint(
        actor=lead,
        request=restricted_ticket,
        expected_revision=restricted_ticket.revision,
        is_complaint=True,
        reason="Synthetic classification",
    )
    nc = opened(complaint, lead)
    services.set_owner(actor=lead, nc=nc, expected_revision=nc.revision, owner=tech)
    client.force_login(tech)
    page = client.get(nc_url("edit", nc.pk))
    assert page.status_code == 200
    assert not selectors.linked_tickets(tech, nc).exists()
    assert "Opened from a complaint" not in page.context["form"].fields["source"].help_text


def test_stale_unlink_refusal_discards_preview_if_scope_has_changed(
    client, owned, linked, agent, monkeypatch
):
    # Model simultaneous revision and scope loss at the service boundary. Capture only whether
    # the template would be given a preview. The underlying synthetic ticket stays unrestricted.
    client.force_login(agent)
    original_scope = selectors.linked_tickets
    still_visible = True

    def visible_links(user, record, **kwargs):
        links = original_scope(user, record, **kwargs)
        return links if still_visible else links.none()

    def stale(**kwargs):
        nonlocal still_visible
        still_visible = False
        raise ValidationError(services.STALE)

    includes_preview = []

    def inspect_redraw(request, nc, form, title, submit_label, destructive, notes, template, extra):
        # The preview is decided when the form is drawn (a callable), after the refusal.
        drawn = extra() if callable(extra) else extra
        includes_preview.append(bool((drawn or {}).get("link")))
        return HttpResponse(status=200)

    monkeypatch.setattr(selectors, "linked_tickets", visible_links)
    monkeypatch.setattr(services, "unlink_ticket", stale)
    monkeypatch.setattr(views, "_render_form", inspect_redraw)
    client.post(
        nc_url("ticket-unlink", owned.pk, linked.pk),
        {"expected_revision": owned.revision, "reason": "Synthetic draft reason"},
    )
    assert not any(includes_preview)


def test_review_shows_saved_communication_method_even_without_date(client, decided, agent, lead):
    method = "Example switchboard call"
    desk.update_complaint(
        actor=agent,
        request=decided,
        expected_revision=decided.revision,
        outcome_communicated_how=method,
    )
    review = ComplaintReview.objects.get(request=decided)
    assert review.outcome_communicated_how == method and review.outcome_communicated_on is None
    client.force_login(lead)
    page = client.get(reverse("desk:complaint-review", args=[decided.pk]))
    assert page.status_code == 200 and method.encode() in page.content


def test_independent_re_review_retains_the_previous_signed_note(decided, agent, lead, lead2):
    first_note = "Example first reviewer conclusion"
    first = desk.review_complaint(
        actor=lead, request=decided, expected_revision=decided.revision, note=first_note
    )
    decided.refresh_from_db()
    TeamMembership.objects.get_or_create(team=decided.team, user=lead)
    assigned = desk.assign(
        actor=agent,
        request=decided,
        expected_revision=decided.revision,
        team=decided.team,
        assignee=lead,
        reason="Taking over follow-up",
    )
    final = desk.review_complaint(
        actor=lead2,
        request=assigned,
        expected_revision=assigned.revision,
        note="Example second reviewer conclusion",
    )
    assert final.pk == first.pk and final.reviewed_by == lead2
    history = AuditEvent.objects.filter(
        object_type=final._meta.label_lower, object_id=str(final.pk)
    )
    # The two approvals are attributable. Their evidence belongs to complaint history.
    assert set(history.filter(action="complaint.reviewed").values_list("actor_id", flat=True)) == {
        lead.pk,
        lead2.pk,
    }
    assert any(first_note in json.dumps(row) for row in history.values("before", "after", "reason"))


@pytest.mark.parametrize(
    ("page_id", "obsolete"),
    [
        (
            "desk/complaint-record",
            "If your laboratory uses the optional nonconformance module, this field is not shown",
        ),
        ("desk/complaint-review", "A review cannot be undone or repeated"),
        (
            "nonconformance/from-ticket",
            "Once the complaint has been reviewed, opening one here is refused",
        ),
    ],
)
def test_operator_help_has_retired_the_superseded_instruction(page_id, obsolete):
    page_help = Path(__file__).resolve().parents[2] / "docs" / "pages" / (page_id + ".json")
    assert obsolete not in json.dumps(json.loads(page_help.read_text()))


@pytest.mark.django_db(transaction=True)
def test_independence_notice_and_record_controls_agree_during_assignment(
    client, module_on, decided, agent, lead, lead2
):
    desk.review_complaint(
        actor=lead,
        request=decided,
        expected_revision=decided.revision,
        note="Example independent review",
    )
    decided.refresh_from_db()
    TeamMembership.objects.get_or_create(team=decided.team, user=lead)
    client.force_login(lead2)

    def reviewer_takes_ticket():
        desk.assign(
            actor=agent,
            request=decided,
            expected_revision=decided.revision,
            team=decided.team,
            assignee=lead,
            reason="Assigned to follow up",
        )

    def assignee_history(sql):
        return 'SELECT "desk_requestentry"."event_data"' in sql and "event_action" in sql

    with at_read(assignee_history, reviewer_takes_ticket):
        page = client.get(reverse("desk:request-detail", args=[decided.pk]))
    assert page.status_code == 200
    counts = page.context["review_counts"]
    can_work = page.context["can_record_complaint"]
    assert (counts, can_work) in {(True, False), (False, True)}
    assert (b"Review no longer counts" in page.content) == can_work


def test_related_ticket_entries_and_audit_events_match_the_exact_ticket_revision(
    module_on, complaint, plain_ticket, lead
):
    before = {ticket.pk: ticket.revision for ticket in (complaint, plain_ticket)}
    nc = opened(complaint, lead)
    link = services.link_ticket(
        actor=lead, nc=nc, expected_revision=nc.revision, request=plain_ticket
    )
    nc.refresh_from_db()
    services.unlink_ticket(
        actor=lead,
        nc=nc,
        expected_revision=nc.revision,
        link=link,
        reason="Synthetic reason confined to NC history",
    )
    for ticket, changes in (
        (complaint, ["nonconformance_opened"]),
        (plain_ticket, ["nonconformance_linked", "nonconformance_unlinked"]),
    ):
        ticket.refresh_from_db()
        assert ticket.revision == before[ticket.pk] + len(changes)
        for change in changes:
            entry = ticket.entries.get(event_action=change)
            audit = AuditEvent.objects.get(
                object_type=ticket._meta.label_lower,
                object_id=str(ticket.pk),
                action="request." + change,
            )
            assert (
                entry.event_data == audit.after == {"nonconformance_id": nc.pk, "number": nc.number}
            )
            assert entry.author_id == audit.actor_id == lead.pk
            assert not audit.before and not audit.reason


@pytest.mark.parametrize("operation", ["complaint-record", "complaint-review"])
def test_reassigned_complaint_keeps_already_drawn_draft_for_a_current_ticket_reader(
    client, module_on, decided, lead, agent, operation
):
    writer = lead if operation == "complaint-review" else agent
    client.force_login(writer)
    url = reverse("desk:" + operation, args=[decided.pk])
    body = form_values(client.get(url))
    TeamMembership.objects.get_or_create(team=decided.team, user=lead)
    assigned = desk.assign(
        actor=lead,
        request=decided,
        expected_revision=decided.revision,
        team=decided.team,
        assignee=lead,
        reason="Taking the ticket",
    )
    field = "note" if operation == "complaint-review" else "findings"
    draft = "Example review in progress"
    body[field] = draft
    page = client.post(url, body)
    decided.refresh_from_db()
    assert assigned.revision == decided.revision
    saved = ComplaintReview.objects.get(request=decided)
    assert not saved.findings and not saved.review_note
    assert page.status_code == 200 and draft.encode() in page.content
