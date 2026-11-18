"""Ticket services: assignment (D4), status (D5), category and priority (D6), reply withdrawal
(D9) and the bulk actions behind the request list (D10). Each case checks the allowed and the
refused role, the timeline event, the audit event and the stale-edit path."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from core.models import AuditEvent
from desk import services
from desk.models import EntryKind, RequestEntry, Status
from desk.visibility import client_visible
from taxonomy.models import RequestCategory
from teams.models import ensure_default_team

from .test_contract import new_request, seen, selection, set_status

pytestmark = pytest.mark.django_db


@pytest.fixture
def team(db):
    return ensure_default_team()


@pytest.fixture
def lead(make_user):
    return make_user("lead", "supervisor")


@pytest.fixture
def tech(make_user, join, team):
    """A technical staff member who belongs to the default team."""
    user = make_user("tech", "technical")
    join(user, team)
    return user


@pytest.fixture
def notified(monkeypatch):
    """The assignment notifications sent (desk.notifications.assigned)."""
    calls = []
    monkeypatch.setattr("desk.notifications.assigned", lambda *args, **kwargs: calls.append(kwargs))
    return calls


@pytest.fixture
def changed(monkeypatch):
    """The service-clock calls for a change of status, priority or category."""
    calls = []
    monkeypatch.setattr(
        "service_levels.hooks.request_changed",
        lambda request, **kwargs: calls.append((request.pk, kwargs)),
    )
    return calls


def audit(action, obj):
    return list(
        AuditEvent.objects.filter(action=action, object_id=str(obj.pk)).order_by("occurred_at")
    )


def events(request, action):
    return list(request.entries.filter(kind=EntryKind.EVENT, event_action=action))


def resolved(actor, request):
    services.change_status(
        actor=actor,
        request=request,
        expected_revision=request.revision,
        status=Status.RESOLVED,
        summary="Result re-sent.",
    )
    request.refresh_from_db()
    return request


class TestCreateHook:
    def test_phone_request_is_answered_at_intake_through_the_hook(
        self, agent, category, monkeypatch
    ):
        seen = []
        monkeypatch.setattr(
            "service_levels.hooks.request_created", lambda request: seen.append(request.channel)
        )
        new_request(agent, category, channel="phone")
        new_request(agent, category, channel="email")
        assert seen == ["phone", "email"]  # the clock decides from the channel


class TestAssign:
    def test_assignee_must_be_a_member_of_the_team(self, agent, tech, quality, category):
        request = new_request(agent, category)
        with pytest.raises(ValidationError) as error:
            services.assign(
                actor=agent, request=request, expected_revision=1, team=quality, assignee=tech
            )
        assert "assignee" in error.value.message_dict
        request.refresh_from_db()
        assert (request.revision, request.assignee) == (1, None)
        assert not audit("request.assigned", request)

    def test_deactivated_member_cannot_be_assigned(self, agent, tech, team, category):
        tech.is_active = False
        tech.save()
        request = new_request(agent, category)
        with pytest.raises(ValidationError):
            services.assign(
                actor=agent, request=request, expected_revision=1, team=team, assignee=tech
            )

    def test_portal_account_cannot_assign(self, clinic_portal, agent, tech, team, category):
        request = new_request(agent, category)
        with pytest.raises(PermissionDenied):
            services.assign(
                actor=clinic_portal, request=request, expected_revision=1, team=team, assignee=tech
            )

    def test_events_audit_and_notification(self, agent, tech, team, category, notified):
        request = new_request(agent, category)
        services.assign(
            actor=agent,
            request=request,
            expected_revision=1,
            team=team,
            assignee=tech,
            reason="Knows this client",
        )
        request.refresh_from_db()
        assert (request.assignee, request.revision) == (tech, 2)
        (event,) = events(request, "assigned")
        assert event.author == agent
        assert event.event_data["assignee_id"] == tech.pk
        assert event.event_data["old_assignee_id"] is None
        (record,) = audit("request.assigned", request)
        assert record.actor == agent
        assert record.reason == "Knows this client"
        assert record.before == {"team_id": team.pk, "assignee_id": None}
        assert record.after == {"team_id": team.pk, "assignee_id": tech.pk}
        assert notified == [{"assignee": tech, "actor": agent}]

    def test_moving_team_with_no_person_clears_a_non_member(
        self, agent, tech, team, quality, category
    ):
        request = new_request(agent, category, assignee=tech)
        services.assign(actor=agent, request=request, expected_revision=1, team=quality)
        request.refresh_from_db()
        assert request.team == quality
        assert request.assignee is None
        (event,) = events(request, "assigned")
        assert event.event_data["old_assignee_id"] == tech.pk
        assert event.event_data["assignee_id"] is None

    def test_no_notification_to_oneself_or_when_nothing_changes(
        self, agent, join, team, category, notified
    ):
        join(agent, team)
        request = new_request(agent, category)
        services.assign(
            actor=agent, request=request, expected_revision=1, team=team, assignee=agent
        )
        assert notified == []
        request.refresh_from_db()
        with pytest.raises(ValidationError, match="another team or person"):
            services.assign(
                actor=agent,
                request=request,
                expected_revision=request.revision,
                team=team,
                assignee=agent,
            )

    def test_inactive_team_and_closed_request_refused(self, agent, quality, category, team):
        request = new_request(agent, category)
        quality.is_active = False
        quality.save()
        with pytest.raises(ValidationError) as error:
            services.assign(actor=agent, request=request, expected_revision=1, team=quality)
        assert "team" in error.value.message_dict
        set_status(request, Status.CLOSED)
        with pytest.raises(ValidationError, match="closed"):
            services.assign(actor=agent, request=request, expected_revision=1, team=team)

    def test_stale_edit_is_refused(self, agent, tech, team, category):
        request = new_request(agent, category)
        services.add_entry(
            actor=agent, request=request, kind=EntryKind.NOTE, body="Seen", expected_revision=1
        )
        with pytest.raises(ValidationError, match="changed"):
            services.assign(
                actor=agent, request=request, expected_revision=1, team=team, assignee=tech
            )
        request.refresh_from_db()
        assert request.assignee is None


class TestTake:
    def test_member_takes_it(self, tech, agent, category, notified):
        request = new_request(agent, category)
        services.take(actor=tech, request=request, expected_revision=1)
        request.refresh_from_db()
        assert request.assignee == tech
        (event,) = events(request, "assigned")
        assert event.author == tech
        (record,) = audit("request.assigned", request)
        assert record.actor == tech and record.after["assignee_id"] == tech.pk
        assert notified == []  # nobody to tell: the actor took it

    def test_non_member_is_refused(self, agent, category):
        request = new_request(agent, category)
        with pytest.raises(ValidationError, match="Only members"):
            services.take(actor=agent, request=request, expected_revision=1)
        request.refresh_from_db()
        assert request.assignee is None
        assert not audit("request.assigned", request)

    def test_taking_it_again_and_a_stale_edit_are_refused(self, tech, agent, category):
        request = new_request(agent, category)
        services.take(actor=tech, request=request, expected_revision=1)
        with pytest.raises(ValidationError, match="already yours"):
            services.take(actor=tech, request=request, expected_revision=2)
        with pytest.raises(ValidationError, match="changed"):
            services.take(actor=tech, request=request, expected_revision=1)

    def test_taking_notifies_nobody_but_assigning_a_colleague_does(
        self, tech, agent, make_user, join, team, category, notified
    ):
        other = make_user("other", "client_services")
        join(other, team)
        request = new_request(agent, category, assignee=other)
        notified.clear()  # creating it told `other`
        services.take(actor=tech, request=request, expected_revision=1)
        request.refresh_from_db()
        assert request.assignee == tech and notified == []
        services.assign(actor=tech, request=request, expected_revision=2, team=team, assignee=other)
        assert notified == [{"assignee": other, "actor": tech}]


class TestStatus:
    def test_resolve_needs_a_summary(self, agent, category):
        request = new_request(agent, category)
        for blank in ("", "   "):
            with pytest.raises(ValidationError) as error:
                services.change_status(
                    actor=agent,
                    request=request,
                    expected_revision=1,
                    status=Status.RESOLVED,
                    summary=blank,
                )
            assert "summary" in error.value.message_dict
        request.refresh_from_db()
        assert (request.status, request.revision) == (Status.OPEN, 1)

    def test_resolve_records_event_audit_and_calls_the_clock(self, agent, category, changed):
        request = new_request(agent, category)
        services.change_status(
            actor=agent,
            request=request,
            expected_revision=1,
            status=Status.RESOLVED,
            summary="Report re-sent by fax.",
        )
        request.refresh_from_db()
        assert request.status == Status.RESOLVED
        assert request.resolution_summary == "Report re-sent by fax."
        assert request.resolved_at is not None
        (event,) = events(request, "status_changed")
        assert event.event_data == {"old": "open", "new": "resolved"}
        (record,) = audit("request.status_changed", request)
        assert (record.before, record.after) == ({"status": "open"}, {"status": "resolved"})
        assert "Report re-sent" not in str(record.after)  # the summary is not copied to audit
        assert changed == [(request.pk, {"previous": {"status": "open"}})]

    def test_reopen_needs_a_reason(self, agent, category, changed):
        request = resolved(agent, new_request(agent, category))
        changed.clear()
        with pytest.raises(ValidationError) as error:
            services.change_status(
                actor=agent,
                request=request,
                expected_revision=request.revision,
                status=Status.OPEN,
            )
        assert "reason" in error.value.message_dict
        assert changed == []
        services.change_status(
            actor=agent,
            request=request,
            expected_revision=request.revision,
            status=Status.OPEN,
            reason="Client says the report is still wrong",
        )
        request.refresh_from_db()
        assert request.status == Status.OPEN and request.resolved_at is None
        record = audit("request.status_changed", request)[-1]
        assert record.reason == "Client says the report is still wrong"
        assert changed == [(request.pk, {"previous": {"status": "resolved"}})]

    def test_waiting_statuses_need_nothing_more(self, agent, category):
        request = new_request(agent, category)
        for status in (Status.WAITING_CLIENT, Status.WAITING_THIRD_PARTY, Status.OPEN):
            services.change_status(
                actor=agent, request=request, expected_revision=request.revision, status=status
            )
            request.refresh_from_db()
            assert request.status == status

    def test_closed_cannot_change(self, agent, category):
        request = resolved(agent, new_request(agent, category))
        services.change_status(
            actor=agent,
            request=request,
            expected_revision=request.revision,
            status=Status.CLOSED,
        )
        request.refresh_from_db()
        assert request.closed_at is not None
        for status in (Status.OPEN, Status.RESOLVED, Status.WAITING_CLIENT):
            with pytest.raises(ValidationError, match="closed ticket cannot change"):
                services.change_status(
                    actor=agent,
                    request=request,
                    expected_revision=request.revision,
                    status=status,
                    summary="Again",
                    reason="Again",
                )

    def test_closing_follows_resolving_and_the_same_status_is_refused(self, agent, category):
        request = new_request(agent, category)
        with pytest.raises(ValidationError) as error:
            services.change_status(
                actor=agent, request=request, expected_revision=1, status=Status.CLOSED
            )
        assert "status" in error.value.message_dict
        with pytest.raises(ValidationError, match="already has this status"):
            services.change_status(
                actor=agent, request=request, expected_revision=1, status=Status.OPEN
            )
        with pytest.raises(ValidationError):
            services.change_status(
                actor=agent, request=request, expected_revision=1, status="archived"
            )

    def test_stale_edit_and_portal_account_refused(self, agent, clinic_portal, category):
        request = new_request(agent, category)
        with pytest.raises(PermissionDenied):
            services.change_status(
                actor=clinic_portal,
                request=request,
                expected_revision=1,
                status=Status.WAITING_CLIENT,
            )
        services.add_entry(
            actor=agent, request=request, kind=EntryKind.NOTE, body="Seen", expected_revision=1
        )
        with pytest.raises(ValidationError, match="changed"):
            services.change_status(
                actor=agent, request=request, expected_revision=1, status=Status.WAITING_CLIENT
            )

    def test_complaint_closes_only_after_its_review(self, agent, lead, join, team, category):
        join(agent, team)
        request = new_request(agent, category)
        services.set_complaint(
            actor=agent, request=request, expected_revision=1, is_complaint=True, reason="Late"
        )
        request.refresh_from_db()
        request = resolved(agent, request)
        with pytest.raises(ValidationError, match="after a supervisor has reviewed"):
            services.change_status(
                actor=agent,
                request=request,
                expected_revision=request.revision,
                status=Status.CLOSED,
            )


class TestProperties:
    def test_priority_change_calls_the_clock_with_the_previous_value(
        self, agent, category, changed
    ):
        request = new_request(agent, category)
        services.update_properties(
            actor=agent,
            request=request,
            expected_revision=1,
            priority="urgent",
            category=category,
            reason="Clinic waiting on a result",
        )
        request.refresh_from_db()
        assert request.priority == "urgent"
        assert changed == [(request.pk, {"previous": {"priority": "normal"}})]
        (record,) = audit("request.updated", request)
        assert record.before == {"priority": "normal"}
        assert record.after == {"priority": "urgent"}
        assert record.reason == "Clinic waiting on a result"

    def test_category_change_audit_holds_codes_not_text(
        self, agent, category, sample_category, changed
    ):
        request = new_request(agent, category, subject="A very private subject")
        services.update_properties(
            actor=agent,
            request=request,
            expected_revision=1,
            priority=request.priority,
            category=sample_category,
        )
        request.refresh_from_db()
        assert request.category == sample_category
        assert changed == [(request.pk, {"previous": {"category_id": category.pk}})]
        (record,) = audit("request.updated", request)
        assert record.before == {"category_id": category.pk}
        assert record.after == {"category_id": sample_category.pk}
        text = str(record.before) + str(record.after)
        assert sample_category.name not in text and "private subject" not in text
        (event,) = events(request, "updated")
        assert event.event_data["fields"] == ["category_id"]
        assert "private subject" not in str(event.event_data)

    def test_both_changes_in_one_call(self, agent, category, sample_category, changed):
        request = new_request(agent, category)
        services.update_properties(
            actor=agent,
            request=request,
            expected_revision=1,
            priority="low",
            category=sample_category,
        )
        assert changed == [
            (request.pk, {"previous": {"priority": "normal", "category_id": category.pk}})
        ]

    def test_complaint_category_marks_the_request(self, agent, group, category):
        complaint_category = RequestCategory.objects.create(
            key="complaint", name="Complaint", parent=group, is_complaint=True
        )
        request = new_request(agent, category)
        assert not request.is_complaint
        services.update_properties(
            actor=agent,
            request=request,
            expected_revision=1,
            priority=request.priority,
            category=complaint_category,
        )
        request.refresh_from_db()
        assert request.is_complaint
        (event,) = events(request, "complaint_marked")
        assert event.event_data == {"by_category": True}
        # moving back out never unmarks it: only a supervisor does that
        services.update_properties(
            actor=agent,
            request=request,
            expected_revision=request.revision,
            priority=request.priority,
            category=category,
        )
        request.refresh_from_db()
        assert request.is_complaint

    def test_nothing_changed_is_refused(self, agent, category, changed):
        request = new_request(agent, category)
        with pytest.raises(ValidationError, match="Change the priority or the category"):
            services.update_properties(
                actor=agent,
                request=request,
                expected_revision=1,
                priority=request.priority,
                category=category,
            )
        assert changed == [] and not audit("request.updated", request)

    def test_refusals(self, agent, group, category, sample_category):
        request = new_request(agent, category)
        with pytest.raises(ValidationError) as error:
            services.update_properties(
                actor=agent, request=request, expected_revision=1, priority="normal", category=group
            )
        assert "category" in error.value.message_dict
        with pytest.raises(ValidationError) as error:
            services.update_properties(
                actor=agent,
                request=request,
                expected_revision=1,
                priority="asap",
                category=category,
            )
        assert "priority" in error.value.message_dict
        with pytest.raises(ValidationError, match="changed"):
            services.update_properties(
                actor=agent,
                request=request,
                expected_revision=7,
                priority="high",
                category=category,
            )
        set_status(request, Status.CLOSED)
        with pytest.raises(ValidationError, match="closed"):
            services.update_properties(
                actor=agent,
                request=request,
                expected_revision=1,
                priority="high",
                category=category,
            )


class TestWithdraw:
    @pytest.fixture
    def reply(self, agent, category):
        request = new_request(agent, category)
        return services.add_entry(
            actor=agent,
            request=request,
            kind=EntryKind.REPLY,
            body="Sent to the wrong clinic.",
            expected_revision=1,
        )

    def test_only_a_supervisor_withdraws(self, agent, make_user, reply):
        for user in (agent, make_user("tech2", "technical"), make_user("am", "account_manager")):
            with pytest.raises(PermissionDenied):
                services.withdraw_reply(
                    actor=user, entry=reply, expected_revision=seen(reply), reason="Wrong request"
                )
        reply.refresh_from_db()
        assert reply.withdrawn_at is None

    def test_supervisor_withdraws_with_a_reason(self, lead, reply):
        request = reply.request
        assert reply in client_visible(request.entries.all())
        revision = request.revision
        services.withdraw_reply(
            actor=lead,
            entry=reply,
            expected_revision=seen(reply),
            reason="Posted on the wrong request",
        )
        reply.refresh_from_db()
        assert reply.withdrawn_by == lead and reply.withdrawn_at is not None
        assert reply.withdrawal_reason == "Posted on the wrong request"
        assert reply not in client_visible(request.entries.all())
        assert RequestEntry.objects.filter(pk=reply.pk).exists()  # staff still see it
        request.refresh_from_db()
        assert request.revision == revision + 1
        assert events(request, "reply_withdrawn")[0].event_data == {"entry_id": reply.pk}
        (record,) = audit("entry.withdrawn", reply)
        assert record.actor == lead and record.reason == "Posted on the wrong request"

    def test_reason_is_required(self, lead, reply):
        for blank in ("", "  "):
            with pytest.raises(ValidationError) as error:
                services.withdraw_reply(
                    actor=lead, entry=reply, expected_revision=seen(reply), reason=blank
                )
            assert "reason" in error.value.message_dict
        reply.refresh_from_db()
        assert reply.withdrawn_at is None

    def test_second_withdrawal_is_refused(self, lead, reply):
        services.withdraw_reply(
            actor=lead, entry=reply, expected_revision=seen(reply), reason="Wrong request"
        )
        with pytest.raises(ValidationError, match="already been withdrawn"):
            services.withdraw_reply(
                actor=lead, entry=reply, expected_revision=seen(reply), reason="Again"
            )
        assert len(audit("entry.withdrawn", reply)) == 1

    def test_notes_and_contacts_cannot_be_withdrawn(self, agent, lead, category):
        request = new_request(agent, category)
        note = request.entries.get(kind=EntryKind.NOTE)
        with pytest.raises(ValidationError, match="Only a reply"):
            services.withdraw_reply(
                actor=lead, entry=note, expected_revision=seen(note), reason="Not a reply"
            )


class TestBulk:
    def make(self, agent, category, count=3, **extra):
        return [new_request(agent, category, subject=f"Bulk {n}", **extra) for n in range(count)]

    def test_bulk_assign_audits_each_request_and_skips_those_already_there(
        self, agent, tech, team, category, notified
    ):
        first, second, third = self.make(agent, category)
        services.assign(actor=agent, request=third, expected_revision=1, team=team, assignee=tech)
        notified.clear()
        changed_count = services.bulk_assign(
            actor=agent,
            selected=selection([first.pk, second.pk, third.pk]),
            team=team,
            assignee=tech,
            reason="Morning triage",
        )
        assert changed_count == 2
        for request in (first, second):
            request.refresh_from_db()
            assert request.assignee == tech and request.revision == 2
            (record,) = audit("request.assigned", request)
            assert record.reason == "Morning triage" and record.actor == agent
            assert len(events(request, "assigned")) == 1
        assert len(notified) == 2
        third.refresh_from_db()
        assert third.revision == 2  # untouched by the bulk call

    def test_bulk_assign_is_all_or_nothing(self, agent, tech, team, category):
        first, second = self.make(agent, category, 2)
        set_status(second, Status.CLOSED)
        with pytest.raises(ValidationError, match="closed"):
            services.bulk_assign(
                actor=agent, selected=selection([first.pk, second.pk]), team=team, assignee=tech
            )
        first.refresh_from_db()
        assert first.assignee is None and not audit("request.assigned", first)

    def test_bulk_assign_refuses_a_non_member(self, agent, tech, quality, category):
        (request,) = self.make(agent, category, 1)
        with pytest.raises(ValidationError):
            services.bulk_assign(
                actor=agent, selected=selection([request.pk]), team=quality, assignee=tech
            )

    def test_bulk_status_audits_each_request(self, agent, category, changed):
        requests = self.make(agent, category)
        count = services.bulk_status(
            actor=agent, selected=selection([r.pk for r in requests]), status=Status.WAITING_CLIENT
        )
        assert count == 3
        for request in requests:
            request.refresh_from_db()
            assert request.status == Status.WAITING_CLIENT
            (record,) = audit("request.status_changed", request)
            assert record.after == {"status": "waiting_client"}
            assert len(events(request, "status_changed")) == 1
        assert [call[1] for call in changed] == [{"previous": {"status": "open"}}] * 3

    def test_bulk_status_refuses_resolve_close_and_resolved_requests(self, agent, category):
        first, second = self.make(agent, category, 2)
        for status in (Status.RESOLVED, Status.CLOSED):
            with pytest.raises(ValidationError):
                services.bulk_status(actor=agent, selected=selection([first.pk]), status=status)
        resolved(agent, second)
        with pytest.raises(ValidationError, match="one at a time"):
            services.bulk_status(
                actor=agent, selected=selection([first.pk, second.pk]), status=Status.WAITING_CLIENT
            )
        first.refresh_from_db()
        assert first.status == Status.OPEN

    def test_bulk_close_closes_only_resolved_requests(self, agent, category):
        requests = [resolved(agent, request) for request in self.make(agent, category)]
        count = services.bulk_close(actor=agent, selected=selection([r.pk for r in requests]))
        assert count == 3
        for request in requests:
            request.refresh_from_db()
            assert request.status == Status.CLOSED and request.closed_at is not None
            assert audit("request.status_changed", request)[-1].after == {"status": "closed"}
        closed_records = AuditEvent.objects.filter(
            action="request.status_changed", after={"status": "closed"}
        )
        assert closed_records.count() == 3  # one per request, none for the batch

    def test_bulk_close_is_all_or_nothing(self, agent, category):
        done = resolved(agent, new_request(agent, category))
        still_open = new_request(agent, category)
        with pytest.raises(ValidationError, match=still_open.number):
            services.bulk_close(actor=agent, selected=selection([done.pk, still_open.pk]))
        done.refresh_from_db()
        assert done.status == Status.RESOLVED and done.closed_at is None

    def test_bulk_needs_a_selection_and_a_staff_account(self, agent, clinic_portal, category):
        with pytest.raises(ValidationError, match="at least one"):
            services.bulk_close(actor=agent, selected=selection([]))
        (request,) = self.make(agent, category, 1)
        with pytest.raises(PermissionDenied):
            services.bulk_status(
                actor=clinic_portal, selected=selection([request.pk]), status=Status.WAITING_CLIENT
            )

    def test_bulk_close_of_old_resolved_requests_keeps_resolution_time(self, agent, category):
        request = resolved(agent, new_request(agent, category))
        before = request.resolved_at
        services.bulk_close(actor=agent, selected=selection([request.pk]))
        request.refresh_from_db()
        assert request.resolved_at == before
        assert request.closed_at >= before


class TestEntryStatusOnResolved:
    """Staff reopen a resolved request with Status and a reason, not through the composer."""

    def test_composer_cannot_reopen_a_resolved_request(self, agent, category):
        request = resolved(agent, new_request(agent, category))
        for status in (Status.OPEN, Status.WAITING_CLIENT, Status.WAITING_THIRD_PARTY):
            with pytest.raises(ValidationError):
                services.add_entry(
                    actor=agent,
                    request=request,
                    kind=EntryKind.NOTE,
                    body="Reopening quietly",
                    expected_revision=request.revision,
                    new_status=status,
                )
        request.refresh_from_db()
        assert request.status == Status.RESOLVED
        assert not request.entries.filter(body="Reopening quietly").exists()
        assert len(audit("request.status_changed", request)) == 1  # only the resolve

    def test_a_note_on_a_resolved_request_without_a_status_is_fine(self, agent, category):
        request = resolved(agent, new_request(agent, category))
        services.add_entry(
            actor=agent,
            request=request,
            kind=EntryKind.NOTE,
            body="Filed the paperwork",
            expected_revision=request.revision,
        )
        request.refresh_from_db()
        assert request.status == Status.RESOLVED

    def test_status_with_a_reason_still_reopens(self, agent, category):
        request = resolved(agent, new_request(agent, category))
        services.change_status(
            actor=agent,
            request=request,
            expected_revision=request.revision,
            status=Status.OPEN,
            reason="Client called back",
        )
        request.refresh_from_db()
        assert request.status == Status.OPEN
