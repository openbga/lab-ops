"""Access and revision checks across ticket actions, lists and notifications."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from clients.services import set_portal_access
from desk import lists, saved_views, services, views
from desk.models import ComplaintDecision, EntryKind, Status
from desk.selectors import staff_requests
from desk.tests.test_contract import new_request, seen
from links.models import ExternalSystem
from links.services import add_link
from service_levels.models import RequestClock
from taxonomy.services import update_category
from teams import services as team_services
from teams.models import ensure_default_team

pytestmark = pytest.mark.django_db


@pytest.fixture
def lis(db):
    """The starter external system (as links/tests/conftest.py)."""
    return ExternalSystem.objects.get_or_create(
        key="lis", defaults={"name": "Laboratory information system", "kind": "lis"}
    )[0]


def change(record, operation, actor, **values):
    record.refresh_from_db()
    return operation(actor=actor, request=record, expected_revision=record.revision, **values)


def decide(record, actor):
    return change(
        record,
        services.decide_complaint,
        actor,
        decision=ComplaintDecision.INVESTIGATE,
        rationale="Synthetic review rationale",
    )


def test_revised_decision_preserves_author_exclusion(make_user, category):
    first = make_user("decision_one", "supervisor")
    second = make_user("decision_two", "supervisor")
    record = new_request(first, category)
    change(record, services.set_complaint, first, is_complaint=True, reason="Synthetic complaint")
    decide(record, first)
    final = decide(record, second)
    assert record.entries.filter(author=first, event_action="complaint_decided").exists()
    assert services.review_refusal(first, record, final), "An earlier decision author is eligible"


def test_approval_still_independent_after_assignment(make_user, category, join, client):
    """Assigning the reviewer is allowed, but the review then no
    longer counts and closing is refused until another supervisor reviews it."""
    decision = make_user("decider", "supervisor")
    reviewer = make_user("reviewer", "supervisor")
    third = make_user("third_reviewer", "supervisor")
    record = new_request(decision, category)
    join(reviewer, record.team)
    change(
        record, services.set_complaint, decision, is_complaint=True, reason="Synthetic complaint"
    )
    decide(record, decision)
    change(record, services.review_complaint, reviewer, note="Reviewed separately")
    client.force_login(decision)
    response = client.post(
        reverse("desk:request-assign", args=[record.pk]),
        {"assign_to": f"{record.team_id}:{reviewer.pk}", "expected_revision": seen(record)},
        follow=True,
    )
    assert "review no longer counts" in response.content.decode()
    record.refresh_from_db()
    assert record.complaint_review.reviewed_by_id in services.former_assignee_ids(record)
    assert not services.review_counts(record, record.complaint_review)
    change(record, services.change_status, decision, status=Status.RESOLVED, summary="Done")
    with pytest.raises(ValidationError, match="new review"):
        change(record, services.change_status, decision, status=Status.CLOSED)
    # The queue offers it again, and an independent supervisor's new review lets it close.
    assert record in lists.view_queryset(third, {"view": "complaints-review"})
    change(record, services.review_complaint, third, note="Fresh independent review")
    change(record, services.change_status, decision, status=Status.CLOSED)
    record.refresh_from_db()
    assert record.status == Status.CLOSED


def test_complaint_flag_at_intake_uses_current_category(agent, make_user, category):
    owner = make_user("setup_owner", "admin")
    update_category(
        actor=owner,
        category_id=category.pk,
        expected_revision=category.revision,
        reason="Classify new requests as complaints",
        is_complaint=True,
    )
    record = new_request(agent, category)
    record.refresh_from_db()
    assert record.category.is_complaint
    assert record.is_complaint, "The category now requires independent review"


def test_assignment_uses_current_team_state(agent, make_user, category, quality):
    owner = make_user("team_owner", "admin")
    record = new_request(agent, category)
    team_services.update_team(
        actor=owner,
        team_id=quality.pk,
        expected_revision=quality.revision,
        reason="Retire the team",
        is_active=False,
    )
    with pytest.raises(ValidationError):
        change(record, services.assign, agent, team=quality)


def test_team_removal_ends_assignee_only_complaint_access(agent, make_user, category, join, client):
    supervisor = make_user("membership_owner", "supervisor")
    membership = join(agent, ensure_default_team())
    record = new_request(agent, category, assignee=agent)
    change(record, services.set_complaint, agent, is_complaint=True, reason="Review needed")
    team_services.remove_member(actor=supervisor, membership=membership)
    client.force_login(agent)
    response = client.get(reverse("desk:complaint-decision", args=[record.pk]))
    assert response.status_code == 403, "Removed member still qualifies as the complaint's assignee"


def test_default_transfer_invalidates_source_team_form(make_user):
    admin = make_user("routing_owner", "admin")
    old = ensure_default_team()
    original_revision = old.revision
    destination = team_services.create_team(actor=admin, name="New routing", is_default=True)
    old.refresh_from_db()
    assert not old.is_default
    try:
        team_services.update_team(
            actor=admin,
            team_id=old.pk,
            expected_revision=original_revision,
            reason="Clarify description",
            description="Updated description",
            is_default=True,
        )
    except ValidationError:
        pass
    destination.refresh_from_db()
    assert destination.is_default, "A stale description edit reversed the default-team transfer"


def test_bulk_form_detects_intervening_status(agent, make_user, category, client):
    colleague = make_user("colleague", "technical")
    record = new_request(agent, category, assignee=None)
    client.force_login(agent)
    assert client.get(reverse("desk:request-list"), {"view": "all"}).status_code == 200
    change(record, services.change_status, colleague, status=Status.WAITING_THIRD_PARTY)
    response = client.post(
        reverse("desk:request-bulk"),
        {"requests": [record.pk], "action": "status", "status": Status.WAITING_CLIENT},
    )
    assert response.status_code in (200, 302, 400, 409)
    record.refresh_from_db()
    assert record.status == Status.WAITING_THIRD_PARTY, "Stale list replaced the colleague's status"


def test_withdrawal_respects_intervening_request_change(agent, make_user, category, client):
    reviewer = make_user("withdrawer", "supervisor")
    record = new_request(agent, category, first_entry_kind="reply")
    entry = record.entries.get(kind="reply")
    client.force_login(reviewer)
    page = client.get(reverse("desk:entry-withdraw", args=[record.pk, entry.pk]))
    assert page.status_code == 200
    change(record, services.add_entry, agent, kind="note", body="Later case activity")
    # Send only the fields in the open withdrawal form.
    response = client.post(
        reverse("desk:entry-withdraw", args=[record.pk, entry.pk]),
        {"reason": "Synthetic correction"},
    )
    assert response.status_code in (200, 302, 400, 409)
    entry.refresh_from_db()
    assert entry.withdrawn_at is None, "The stale withdrawal was accepted"


def test_public_status_service_requires_a_revision(agent, category):
    record = new_request(agent, category)
    with pytest.raises(ValidationError):
        services.change_status(
            actor=agent, request=record, expected_revision=None, status=Status.WAITING_CLIENT
        )


def test_closed_note_status_conflict_keeps_request_final(agent, category, client):
    record = new_request(agent, category)
    change(record, services.change_status, agent, status=Status.RESOLVED, summary="Completed")
    change(record, services.change_status, agent, status=Status.CLOSED)
    record.refresh_from_db()
    client.force_login(agent)
    client.raise_request_exception = False
    response = client.post(
        reverse("desk:request-entry", args=[record.pk]),
        {
            "kind": EntryKind.NOTE,
            "body": "Synthetic closed-note sentinel",
            "new_status": Status.OPEN,
            "expected_revision": record.revision,
        },
    )
    record.refresh_from_db()
    assert (
        record.status == Status.CLOSED
        and not record.entries.filter(body="Synthetic closed-note sentinel").exists()
    )
    assert response.status_code == 400, (
        "Database rolls back the request, but the caller gets a server error"
    )


def test_composer_error_rechecks_scope_before_loading_timeline(
    client, agent, make_user, category, privacy_category, monkeypatch
):
    supervisor = make_user("rerouter", "supervisor")
    record = new_request(agent, category)
    client.force_login(agent)
    validate = views.EntryForm.is_valid

    def validate_during_change(form):
        change(
            record,
            services.update_properties,
            supervisor,
            priority=record.priority,
            category=privacy_category,
        )
        assert not staff_requests(agent).filter(pk=record.pk).exists()
        return validate(form)

    monkeypatch.setattr(views.EntryForm, "is_valid", validate_during_change)
    # Stop at context assembly, before reading or rendering any out-of-scope entries.
    with patch.object(
        views,
        "_detail_context",
        side_effect=AssertionError("Error renderer requested the newly restricted timeline"),
    ):
        response = client.post(
            reverse("desk:request-entry", args=[record.pk]),
            {"kind": "note", "body": "", "expected_revision": record.revision},
        )
        assert response.status_code == 404


def test_committed_scope_change_blocks_old_form(
    client, agent, make_user, category, privacy_category
):
    supervisor = make_user("new_scope_owner", "supervisor")
    record = new_request(agent, category)
    client.force_login(agent)
    page = client.get(reverse("desk:request-detail", args=[record.pk]))
    revision = page.context["composer"].initial["expected_revision"]
    change(
        record,
        services.update_properties,
        supervisor,
        priority=record.priority,
        category=privacy_category,
    )
    response = client.post(
        reverse("desk:request-entry", args=[record.pk]),
        {"kind": "note", "body": "Synthetic", "expected_revision": revision},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 404
    assert not record.entries.filter(body="Synthetic").exists()


@pytest.mark.parametrize("route", ["teams:member-lead", "teams:member-remove"])
def test_portal_membership_route_authorizes_before_object_lookup(
    client, clinic_portal, quality, route, agent, make_user, join
):
    supervisor = make_user("membership_admin", "supervisor")
    membership = join(agent, quality)
    url = reverse(route, args=[quality.pk, membership.pk])
    client.force_login(clinic_portal)
    assert client.post(url).status_code == 403
    team_services.remove_member(actor=supervisor, membership=membership)
    assert client.post(url).status_code == 403


def test_dispatch_after_real_withdrawal(
    agent,
    category,
    clinic,
    clinic_contact,
    clinic_portal,
    make_user,
    django_capture_on_commit_callbacks,
    settings,
):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    set_portal_access(
        actor=agent,
        user_id=clinic_portal.pk,
        whole_organization=True,
        expected_client_id=clinic.pk,
        contact=clinic_contact,
    )
    reviewer = make_user("notice_reviewer", "supervisor")
    record = new_request(agent, category, client=clinic, contact=clinic_contact)
    with django_capture_on_commit_callbacks(execute=True):
        entry = change(record, services.add_entry, agent, kind="reply", body="Synthetic withdrawn")
        services.withdraw_reply(
            actor=reviewer, entry=entry, expected_revision=seen(entry), reason="Correction"
        )
    assert not mail.outbox
    with django_capture_on_commit_callbacks(execute=True):
        change(
            record,
            services.add_entry,
            agent,
            kind="reply",
            body="Synthetic delivered",
            new_status=Status.WAITING_CLIENT,
        )
    assert len(mail.outbox) == 1 and mail.outbox[0].to == [clinic_portal.email]
    assert "Synthetic" not in mail.outbox[0].body


def test_shared_views_clocks_and_find_use_current_reader(
    client, agent, make_user, category, privacy_category, lis
):
    supervisor = make_user("shared_owner", "supervisor")
    open_row = new_request(agent, category)
    hidden = new_request(supervisor, privacy_category, subject="Synthetic private subject")
    for row in (open_row, hidden):
        RequestClock.objects.update_or_create(
            request=row,
            defaults={
                "resolution_minutes": 60,
                "resolution_due_at": timezone.now() - timedelta(hours=1),
            },
        )
    identifier = "ORD-TEST-SCOPE"
    add_link(
        actor=supervisor,
        request_id=hidden.pk,
        system=lis,
        record_type="order",
        identifier=identifier,
    )
    view = saved_views.create_view(
        actor=supervisor,
        name="Shared overdue",
        shared_with="everyone",
        params={"view": "all", "clock": "overdue", "unsupported": "yes"},
    )
    assert "unsupported" not in view.filters
    client.force_login(agent)
    page = client.get(views._saved_url(view), headers={"HX-Request": "true"})
    assert page.status_code == 200 and open_row.number.encode() in page.content
    assert hidden.number.encode() not in page.content
    for key in lists.SYSTEM_VIEWS:
        assert not lists.view_queryset(agent, {"view": key}).filter(pk=hidden.pk).exists(), key
    dashboard = client.get(reverse("dashboard")).context
    overdue = next(q for q in dashboard["queues"] if q["label"] == "Overdue tickets")
    assert overdue["count"] == 1 and overdue["oldest_at"] == open_row.created_at
    for term in (identifier, hidden.number, hidden.subject):
        result = client.get(reverse("desk:request-find"), {"q": term})
        assert result.status_code == 200 and not list(result.context["results"])


def test_restricted_intake_honours_current_routing(
    agent, privacy_category, make_user, accessioning
):
    owner = make_user("routing_changes", "admin")
    previous = privacy_category.default_team
    update_category(
        actor=owner,
        category_id=privacy_category.pk,
        expected_revision=privacy_category.revision,
        reason="Change who handles future restricted work",
        default_team=accessioning,
    )
    record = new_request(agent, privacy_category)
    record.refresh_from_db()
    assert record.category.default_team_id == accessioning.pk
    assert record.team_id != previous.pk, "New restricted intake went to the former team"


def test_remark_preserves_assignment_history(agent, category, make_user, join):
    from desk.tests.test_restricted_and_complaints import review

    lead = make_user("former_assignee", "supervisor")
    decider = make_user("current_decider", "supervisor")
    team = ensure_default_team()
    join(lead, team)
    record = new_request(agent, category, assignee=lead)
    change(record, services.set_complaint, agent, is_complaint=True, reason="Reported")
    change(record, services.assign, agent, team=team, assignee=None)
    change(record, services.set_complaint, lead, is_complaint=False, reason="Reconsidered")
    change(record, services.set_complaint, agent, is_complaint=True, reason="Reinstated")
    decide(record, decider)
    with pytest.raises(PermissionDenied, match="assigned"):
        review(lead, record)


def test_independence_is_evaluated_per_account(make_user, category):
    # Synthetic provisioning model: one operator has two approved supervisor accounts.
    first = make_user("shift_a", "supervisor", first_name="Example", last_name="Operator")
    second = make_user("shift_b", "supervisor", first_name="Example", last_name="Operator")
    record = new_request(first, category)
    change(record, services.set_complaint, first, is_complaint=True, reason="Reported")
    decision = decide(record, first)
    assert services.review_refusal(first, record, decision)
    assert services.review_refusal(second, record, decision) == ""  # observed account boundary


def test_internal_posts_and_status_notice_have_separate_visibility(
    client,
    agent,
    category,
    clinic,
    clinic_contact,
    clinic_portal,
    django_capture_on_commit_callbacks,
):
    set_portal_access(
        actor=agent,
        user_id=clinic_portal.pk,
        expected_client_id=clinic.pk,
        whole_organization=True,
        contact=clinic_contact,
    )
    record = new_request(agent, category, client=clinic, contact=clinic_contact)
    client.force_login(agent)
    url = reverse("desk:request-entry", args=[record.pk])
    with django_capture_on_commit_callbacks(execute=True):
        for data in (
            {"kind": "note", "body": "Internal note sentinel", "new_status": Status.WAITING_CLIENT},
            {
                "kind": "contact",
                "body": "Internal contact sentinel",
                "contact_channel": "phone",
                "contact_direction": "to_client",
                "contact_outcome": "no_answer",
            },
        ):
            record.refresh_from_db()
            assert (
                client.post(url, {"expected_revision": record.revision, **data}).status_code == 302
            )
    client.force_login(clinic_portal)
    portal_page = client.get(reverse("portal:request-detail", args=[record.number]))
    assert portal_page.status_code == 200 and len(mail.outbox) == 1
    assert "waiting for information" in mail.outbox[0].body
    for text in ("Internal note sentinel", "Internal contact sentinel"):
        assert text.encode() not in portal_page.content and text not in mail.outbox[0].body


def test_csrf_enforced_on_bulk_and_membership(agent, make_user, category, quality, join):
    supervisor = make_user("csrf_supervisor", "supervisor")
    member = join(agent, quality)
    record = new_request(agent, category)
    client = HttpClient(enforce_csrf_checks=True)
    client.force_login(supervisor)
    actions = [
        (
            reverse("desk:request-bulk"),
            {"requests": [record.pk], "action": "status", "status": "waiting_client"},
        ),
        (reverse("teams:member-remove", args=[quality.pk, member.pk]), {}),
    ]
    for url, data in actions:
        assert client.post(url, data).status_code == 403
    record.refresh_from_db()
    assert record.status == Status.OPEN and quality.memberships.filter(pk=member.pk).exists()


def test_withdrawn_combined_reply_keeps_only_status_notice(
    agent,
    make_user,
    category,
    clinic,
    clinic_portal,
    clinic_contact,
    django_capture_on_commit_callbacks,
):
    set_portal_access(
        actor=agent,
        user_id=clinic_portal.pk,
        expected_client_id=clinic.pk,
        whole_organization=True,
        contact=clinic_contact,
    )
    supervisor = make_user("batch_supervisor", "supervisor")
    record = new_request(agent, category, client=clinic, contact=clinic_contact)
    with django_capture_on_commit_callbacks(execute=True):
        reply = change(
            record,
            services.add_entry,
            agent,
            kind="reply",
            body="Withdrawn sentinel",
            new_status=Status.WAITING_CLIENT,
        )
        services.withdraw_reply(
            actor=supervisor, entry=reply, expected_revision=seen(reply), reason="Correction"
        )
    assert len(mail.outbox) == 1 and mail.outbox[0].to == [clinic_portal.email]
    body = mail.outbox[0].body
    assert (
        "waiting for information" in body
        and "has replied" not in body
        and "Withdrawn sentinel" not in body
    )


def test_submit_handles_scope_lost_during_validation(
    client, agent, make_user, category, privacy_category, monkeypatch
):
    supervisor = make_user("scope_owner", "supervisor")
    record = new_request(agent, category)
    validate = views.EntryForm.is_valid

    def validate_during_change(form):
        valid = validate(form)
        assert valid
        change(
            record,
            services.update_properties,
            supervisor,
            priority=record.priority,
            category=privacy_category,
        )
        return valid

    monkeypatch.setattr(views.EntryForm, "is_valid", validate_during_change)
    client.raise_request_exception = False
    client.force_login(agent)
    response = client.post(
        reverse("desk:request-entry", args=[record.pk]),
        {"kind": "note", "body": "Discarded sentinel", "expected_revision": record.revision},
    )
    assert not record.entries.filter(body="Discarded sentinel").exists()
    assert response.status_code == 404


def test_intake_requires_current_active_assignee(agent, make_user, category, join):
    receiver = make_user("retiring_receiver", "technical")
    join(receiver, ensure_default_team())
    # Keep the assignment choice the form validated, then deactivate the stored account.
    updated = type(receiver).objects.get(pk=receiver.pk)
    updated.is_active = False
    updated.save(update_fields=["is_active"])
    with pytest.raises(ValidationError):
        new_request(agent, category, assignee=receiver)
