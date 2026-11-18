"""Ticket services: request creation, entries, staff and portal scope, visibility, the entry
history trigger and the hooks used by related apps."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.utils import timezone

from core.models import AuditEvent
from desk import services
from desk.models import EntryKind, RequestEntry, ServiceRequest, Status
from desk.selectors import staff_requests
from desk.visibility import client_visible
from teams.models import Team, ensure_default_team

pytestmark = pytest.mark.django_db


def new_request(actor, category, **extra):
    values = {
        "subject": "Result not received",
        "first_entry_kind": EntryKind.NOTE,
        "first_entry_body": "Caller says the result for order ORD-TEST-000042 is missing.",
        "channel": "phone",
    }
    values.update(extra)
    return services.create_request(actor=actor, category=category, **values)


def selection(requests):
    """Bulk selections as the list posts them: each request id with its current revision."""
    ids = [getattr(item, "pk", item) for item in requests]
    return dict(ServiceRequest.objects.filter(pk__in=ids).values_list("pk", "revision"))


def seen(entry_or_request):
    """The request's current revision: what an open page or dialog would carry."""
    request_id = getattr(entry_or_request, "request_id", None) or entry_or_request.pk
    return ServiceRequest.objects.get(pk=request_id).revision


def set_status(request, status):
    """Put a request in a status directly."""
    now = timezone.now()
    ServiceRequest.objects.filter(pk=request.pk).update(
        status=status,
        resolved_at=now if status in (Status.RESOLVED, Status.CLOSED) else None,
        closed_at=now if status == Status.CLOSED else None,
    )
    request.refresh_from_db()
    return request


class TestCreate:
    def test_number_comes_from_the_id(self, agent, category):
        request = new_request(agent, category)
        assert request.number == f"REQ-{request.pk:06d}"

    def test_defaults_from_category_then_default_team(self, agent, category, sample_category):
        plain = new_request(agent, category)
        assert plain.team == ensure_default_team()
        assert plain.priority == "normal"
        routed = new_request(agent, sample_category)
        assert routed.team.slug == "accessioning"
        assert routed.priority == "high"

    def test_timeline_and_audit(self, agent, category):
        request = new_request(agent, category)
        kinds = list(request.entries.values_list("kind", "event_action"))
        assert kinds == [(EntryKind.EVENT, "created"), (EntryKind.NOTE, "")]
        event = AuditEvent.objects.get(action="request.created", object_id=str(request.pk))
        assert event.after["category"] == "missing-result"
        assert "Result not received" not in str(event.after)  # no subject in audit payloads
        assert AuditEvent.objects.filter(action="entry.added").count() == 1

    def test_anonymous_request_without_organization(self, agent, category):
        request = new_request(agent, category, reported_by="Unknown caller, night shift")
        assert request.client is None
        assert request.reported_by == "Unknown caller, night shift"

    def test_group_and_inactive_category_refused(self, agent, group, category):
        with pytest.raises(ValidationError):
            new_request(agent, group)
        category.is_active = False
        category.save()
        with pytest.raises(ValidationError):
            new_request(agent, category)

    def test_site_and_contact_of_another_organization_refused(
        self, agent, category, water, clinic_site, clinic_contact
    ):
        with pytest.raises(ValidationError) as error:
            new_request(agent, category, client=water, site=clinic_site)
        assert "site" in error.value.message_dict
        with pytest.raises(ValidationError) as error:
            new_request(agent, category, client=water, contact=clinic_contact)
        assert "contact" in error.value.message_dict

    def test_site_without_organization_refused(self, agent, category, clinic_site):
        with pytest.raises(ValidationError):
            new_request(agent, category, site=clinic_site)

    def test_inactive_organization_refused(self, agent, category, clinic):
        clinic.is_active = False
        clinic.save()
        with pytest.raises(ValidationError):
            new_request(agent, category, client=clinic)

    def test_assignee_must_be_team_member(self, agent, make_user, category, join):
        other = make_user("other", "technical")
        with pytest.raises(ValidationError) as error:
            new_request(agent, category, assignee=other)
        assert "assignee" in error.value.message_dict
        join(other, ensure_default_team())
        assert new_request(agent, category, assignee=other).assignee == other

    def test_inactive_team_refused(self, agent, category):
        team = Team.objects.create(slug="old", name="Old team", is_active=False)
        with pytest.raises(ValidationError):
            new_request(agent, category, team=team)

    def test_follow_up_needs_a_closed_request_of_the_same_organization(
        self, agent, category, clinic, water
    ):
        first = new_request(agent, category, client=clinic)
        with pytest.raises(ValidationError):
            new_request(agent, category, client=clinic, follow_up_of=first)
        set_status(first, Status.CLOSED)
        with pytest.raises(ValidationError):
            new_request(agent, category, client=water, follow_up_of=first)
        assert new_request(agent, category, client=clinic, follow_up_of=first).follow_up_of

    def test_portal_account_raises_for_its_own_organization(
        self, clinic_portal, category, clinic, clinic_site
    ):
        raise_it = lambda **extra: services.create_request(  # noqa: E731
            actor=clinic_portal,
            category=category,
            subject="Where is my result?",
            first_entry_kind=EntryKind.NOTE,  # ignored: a client's first entry is a reply
            first_entry_body="Order ORD-TEST-000042, collected Monday.",
            channel="phone",  # ignored: the portal is the channel
            priority="high",
            **extra,
        )
        # Without portal access the account raises for its organization with no site.
        with pytest.raises(ValidationError):
            raise_it(site=clinic_site)
        request = raise_it()
        assert request.client == clinic and request.site is None
        assert request.channel == "portal"
        assert request.entries.get(kind=EntryKind.REPLY).author == clinic_portal

    def test_portal_refusals(
        self, clinic_portal, category, privacy_category, water, water_site, agent
    ):
        raise_it = lambda **extra: services.create_request(  # noqa: E731
            actor=clinic_portal,
            category=extra.pop("category", category),
            subject="Question",
            first_entry_kind=EntryKind.REPLY,
            first_entry_body="Text",
            channel="portal",
            **extra,
        )
        with pytest.raises(PermissionDenied):
            raise_it(client=water)
        with pytest.raises(ValidationError):
            raise_it(site=water_site)
        with pytest.raises(ValidationError):
            raise_it(category=privacy_category)  # not offered to clients
        with pytest.raises(ValidationError):
            raise_it(priority="urgent")
        with pytest.raises(PermissionDenied):
            raise_it(team=ensure_default_team())

    def test_non_staff_refused(self, make_user, category):
        with pytest.raises(PermissionDenied):
            new_request(make_user("nobody"), category)


class TestScope:
    def test_restricted_category_hidden_from_non_members(
        self, agent, make_user, privacy_category, category, quality, join
    ):
        restricted = new_request(agent, privacy_category)
        ordinary = new_request(agent, category)
        technical = make_user("tech", "technical")
        assert set(staff_requests(technical)) == {ordinary}
        join(technical, quality)
        assert set(staff_requests(technical)) == {ordinary, restricted}
        for role in ("supervisor", "admin"):
            assert restricted in staff_requests(make_user(f"lead_{role}", role))

    def test_portal_accounts_are_not_staff(self, clinic_portal):
        with pytest.raises(PermissionDenied):
            staff_requests(clinic_portal)

    def test_portal_stub_fails_closed(self, agent, clinic_portal, category, clinic):
        logged = new_request(agent, category, client=clinic)
        with pytest.raises(ServiceRequest.DoesNotExist):
            services.add_entry(
                actor=clinic_portal,
                request=logged,
                kind=EntryKind.REPLY,
                body="Any news?",
                expected_revision=logged.revision,
            )


class TestEntries:
    def test_note_reply_contact(self, agent, category):
        request = new_request(agent, category)
        services.add_entry(
            actor=agent,
            request=request,
            kind=EntryKind.CONTACT,
            body="Called the office; result re-sent.",
            expected_revision=1,
            contact={
                "direction": "to_client",
                "channel": "phone",
                "with_whom": "Office manager",
                "outcome": "spoke_with",
            },
        )
        request.refresh_from_db()
        assert request.revision == 2
        with pytest.raises(ValidationError):
            services.add_entry(
                actor=agent,
                request=request,
                kind=EntryKind.CONTACT,
                body="No outcome",
                expected_revision=2,
                contact={"direction": "to_client", "channel": "phone"},
            )
        with pytest.raises(ValidationError):
            services.add_entry(
                actor=agent,
                request=request,
                kind=EntryKind.NOTE,
                body="Stale",
                expected_revision=1,
            )

    def test_status_with_entry(self, agent, category):
        request = new_request(agent, category)
        services.add_entry(
            actor=agent,
            request=request,
            kind=EntryKind.REPLY,
            body="Please confirm the collection date.",
            expected_revision=1,
            new_status=Status.WAITING_CLIENT,
        )
        request.refresh_from_db()
        assert request.status == Status.WAITING_CLIENT
        assert request.entries.filter(event_action="status_changed").exists()
        with pytest.raises(ValidationError):
            services.add_entry(
                actor=agent,
                request=request,
                kind=EntryKind.NOTE,
                body="Done",
                expected_revision=request.revision,
                new_status=Status.RESOLVED,
            )

    def test_closed_request_takes_notes_only(self, agent, category):
        request = set_status(new_request(agent, category), Status.CLOSED)
        with pytest.raises(ValidationError):
            services.add_entry(
                actor=agent, request=request, kind=EntryKind.REPLY, body="Hi", expected_revision=1
            )
        services.add_entry(
            actor=agent, request=request, kind=EntryKind.NOTE, body="Late info", expected_revision=1
        )

    @pytest.mark.parametrize("status", [Status.WAITING_CLIENT, Status.RESOLVED])
    def test_client_reply_reopens(self, clinic_portal, category, status):
        request = services.create_request(
            actor=clinic_portal,
            category=category,
            subject="Question",
            first_entry_kind=EntryKind.REPLY,
            first_entry_body="Text",
            channel="portal",
        )
        set_status(request, status)
        services.add_entry(
            actor=clinic_portal,
            request=request,
            kind=EntryKind.REPLY,
            body="Still missing.",
            expected_revision=request.revision,
        )
        request.refresh_from_db()
        assert request.status == Status.OPEN
        assert request.resolved_at is None

    def test_client_cannot_write_on_closed_or_add_notes(self, clinic_portal, category):
        request = services.create_request(
            actor=clinic_portal,
            category=category,
            subject="Question",
            first_entry_kind=EntryKind.REPLY,
            first_entry_body="Text",
            channel="portal",
        )
        with pytest.raises(PermissionDenied):
            services.add_entry(
                actor=clinic_portal,
                request=request,
                kind=EntryKind.NOTE,
                body="Note",
                expected_revision=1,
            )
        set_status(request, Status.CLOSED)
        with pytest.raises(ValidationError):
            services.add_entry(
                actor=clinic_portal,
                request=request,
                kind=EntryKind.REPLY,
                body="Again",
                expected_revision=1,
            )

    def test_client_visible_rule(self, agent, category):
        request = new_request(agent, category)
        reply = services.add_entry(
            actor=agent, request=request, kind=EntryKind.REPLY, body="Re-sent", expected_revision=1
        )
        assert list(client_visible(request.entries.all())) == [reply]
        RequestEntry.objects.filter(pk=reply.pk).update(
            withdrawn_at=timezone.now(), withdrawn_by=agent, withdrawal_reason="Wrong request"
        )
        assert not client_visible(request.entries.all()).exists()


class TestHistoryTrigger:
    def test_entries_cannot_change_or_vanish(self, agent, category):
        request = new_request(agent, category)
        note = request.entries.get(kind=EntryKind.NOTE)
        with pytest.raises(DatabaseError, match="Request entries cannot"), transaction.atomic():
            RequestEntry.objects.filter(pk=note.pk).update(body="Rewritten")
        with pytest.raises(DatabaseError, match="Request entries cannot"), transaction.atomic():
            RequestEntry.objects.filter(pk=note.pk).delete()

    def test_reply_withdrawn_once(self, agent, category):
        request = new_request(agent, category)
        reply = services.add_entry(
            actor=agent, request=request, kind=EntryKind.REPLY, body="Hello", expected_revision=1
        )
        withdraw = dict(withdrawn_at=timezone.now(), withdrawn_by=agent, withdrawal_reason="x")
        RequestEntry.objects.filter(pk=reply.pk).update(**withdraw)
        with pytest.raises(DatabaseError, match="Request entries cannot"), transaction.atomic():
            RequestEntry.objects.filter(pk=reply.pk).update(withdrawal_reason="changed")

    def test_withdrawing_a_note_is_refused_by_the_database(self, agent, category):
        request = new_request(agent, category)
        note = request.entries.get(kind=EntryKind.NOTE)
        with pytest.raises(IntegrityError), transaction.atomic():
            RequestEntry.objects.filter(pk=note.pk).update(
                withdrawn_at=timezone.now(), withdrawn_by=agent
            )


class TestHooks:
    def test_hooks_are_called(self, agent, category, monkeypatch, make_user, join):
        calls = []

        def recorder(label):
            return lambda *args, **kwargs: calls.append((label, kwargs))

        for module, names in (
            ("service_levels.hooks", ("request_created", "request_changed", "entry_added")),
            ("desk.notifications", ("entry_added", "status_changed", "assigned")),
        ):
            for name in names:
                label = f"{module.split('.')[0]}.{name}"
                monkeypatch.setattr(f"{module}.{name}", recorder(label))
        helper = make_user("helper", "client_services")
        join(helper, ensure_default_team())
        request = new_request(agent, category, assignee=helper)
        names = [name for name, _ in calls]
        assert names.count("service_levels.request_created") == 1
        assert names.count("service_levels.entry_added") == 2
        assert ("desk.assigned", {"assignee": helper, "actor": agent}) in calls
        calls.clear()
        services.add_entry(
            actor=agent,
            request=request,
            kind=EntryKind.NOTE,
            body="Waiting",
            expected_revision=1,
            new_status=Status.WAITING_THIRD_PARTY,
        )
        assert ("service_levels.request_changed", {"previous": {"status": "open"}}) in calls
        assert any(name == "desk.status_changed" for name, _ in calls)


def test_default_team_is_recreated(db):
    Team.objects.all().delete()
    team = ensure_default_team()
    assert team.is_default and team.slug == "client-services"
    assert ensure_default_team() == team
