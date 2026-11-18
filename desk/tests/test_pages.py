"""Ticket pages through the test client: the new-request form (D1), the request page and its
composer (D2, D3), the action forms (D4 to D6, D9), the list with its system views, filters, sort,
bulk actions and query budget (D10), Find (D12), the dashboard queues (D14) and the history
page (D15). D7 and D8 are in test_restricted_and_complaints."""

from datetime import timedelta

import pytest
from django.db import connection
from django.http import QueryDict
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent
from desk import services
from desk.lists import ordered_requests
from desk.models import ComplaintDecision, EntryKind, ServiceRequest, Status
from taxonomy.models import RequestCategory
from taxonomy.terms import drawn
from teams.models import ensure_default_team

from .test_contract import new_request, seen, selection, set_status

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------------- helpers


@pytest.fixture
def team(db):
    return ensure_default_team()


@pytest.fixture
def member(agent, join, team):
    """`agent`, a member of the default team."""
    join(agent, team)
    return agent


@pytest.fixture
def lead(make_user):
    return make_user("lead", "supervisor")


@pytest.fixture
def tech(make_user, join, team):
    user = make_user("tech", "technical")
    join(user, team)
    return user


# The composer's notice; the page help says "a reply is recorded but reaches nobody" in lower case.
NOBODY_NOTICE = "A reply is recorded but reaches nobody."


def login(client, user):
    client.force_login(user)
    return client


def messages_of(response):
    return [str(message) for message in response.context["messages"]]


def detail_url(request):
    return reverse("desk:request-detail", args=[request.pk])


def post_entry(client, request, **data):
    values = {
        "kind": "note",
        "body": "A line of text",
        "expected_revision": request.revision,
        "list": "",
    }
    values.update(data)
    return client.post(reverse("desk:request-entry", args=[request.pk]), values)


def subjects(response):
    return {req.subject for req in response.context["page_obj"]}


def reply_to(actor, request, body="A reply"):
    request.refresh_from_db()
    return services.add_entry(
        actor=actor,
        request=request,
        kind=EntryKind.REPLY,
        body=body,
        expected_revision=request.revision,
    )


# ------------------------------------------------------------------------------------ D1


class TestNewRequest:
    def post(self, browser, chosen, **extra):
        data = {
            "category": chosen.pk,
            "category_terms": drawn([chosen]),
            "channel": "phone",
            "subject": "Result not received",
            "first_entry_kind": "note",
            "body": "Caller asks about order ORD-TEST-000042.",
        }
        data.update(extra)
        return browser.post(reverse("desk:request-create"), data)

    def test_page_opens_for_staff_only(self, client, agent, clinic_portal, category):
        assert client.get(reverse("desk:request-create")).status_code == 302  # signed out
        login(client, agent)
        assert client.get(reverse("desk:request-create")).status_code == 200
        login(client, clinic_portal)
        assert client.get(reverse("desk:request-create")).status_code == 403
        assert self.post(client, category).status_code == 403
        assert not ServiceRequest.objects.exists()

    def test_post_creates_and_opens_the_request(self, client, agent, category):
        login(client, agent)
        response = self.post(client, category, reported_by="Night-shift caller")
        request = ServiceRequest.objects.get()
        assert response.status_code == 302
        assert response["Location"] == detail_url(request)
        assert request.created_by == agent and request.reported_by == "Night-shift caller"
        assert request.entries.filter(kind=EntryKind.NOTE).count() == 1
        assert AuditEvent.objects.filter(action="request.created").count() == 1

    def test_category_proposes_team_and_priority_and_staff_may_override(
        self, client, agent, sample_category, accessioning
    ):
        login(client, agent)
        self.post(client, sample_category)
        proposed = ServiceRequest.objects.get()
        assert proposed.team == accessioning and proposed.priority == "high"
        self.post(client, sample_category, priority="low", subject="Second")
        assert ServiceRequest.objects.get(subject="Second").priority == "low"

    def test_assign_to_overrides_the_categorys_team(
        self, client, agent, sample_category, quality, team, tech
    ):
        login(client, agent)
        self.post(client, sample_category, assign_to=f"{team.pk}:{tech.pk}")
        request = ServiceRequest.objects.get()
        assert (request.team, request.assignee) == (team, tech)

    def test_a_group_cannot_be_chosen(self, client, agent, group):
        login(client, agent)
        response = self.post(client, group)
        assert response.status_code == 200
        assert "category" in response.context["form"].errors
        assert not ServiceRequest.objects.exists()

    def test_an_unknown_or_inactive_category_cannot_be_chosen(self, client, agent, category):
        login(client, agent)
        assert self.post(client, category, category=999999).status_code == 200
        category.is_active = False
        category.save()
        assert self.post(client, category).status_code == 200
        assert not ServiceRequest.objects.exists()

    def test_anonymous_request_needs_no_organization(self, client, agent, category):
        login(client, agent)
        response = self.post(client, category, reported_by="Unknown caller")
        assert response.status_code == 302
        request = ServiceRequest.objects.get()
        assert (request.client, request.site, request.contact) == (None, None, None)

    def test_site_or_contact_fills_in_the_organization(
        self, client, agent, category, clinic, clinic_site, clinic_contact
    ):
        login(client, agent)
        self.post(client, category, site=clinic_site.pk, subject="By site")
        by_site = ServiceRequest.objects.get(subject="By site")
        assert by_site.client == clinic and by_site.site == clinic_site
        self.post(client, category, contact=clinic_contact.pk, subject="By contact")
        by_contact = ServiceRequest.objects.get(subject="By contact")
        assert by_contact.client == clinic and by_contact.contact == clinic_contact

    def test_site_of_another_organization_is_refused(
        self, client, agent, category, water, clinic_site, clinic_contact
    ):
        login(client, agent)
        response = self.post(client, category, client=water.pk, site=clinic_site.pk)
        assert response.status_code == 200
        assert "site" in response.context["form"].errors
        response = self.post(client, category, client=water.pk, contact=clinic_contact.pk)
        assert "contact" in response.context["form"].errors
        assert not ServiceRequest.objects.exists()

    def test_errors_keep_the_input(self, client, agent, category):
        login(client, agent)
        response = self.post(client, category, subject="", body="Kept text ORD-TEST-7")
        assert response.status_code == 200
        assert "subject" in response.context["form"].errors
        assert "Kept text ORD-TEST-7" in response.content.decode()

    def test_guidance_partial_returns_the_categorys_guidance(
        self, client, agent, sample_category, group, clinic_portal
    ):
        sample_category.guidance = "Ask for the accession number and the collection date."
        sample_category.save()
        login(client, agent)
        url = reverse("desk:category-guidance")
        body = client.get(url, {"category": sample_category.pk}).content.decode()
        assert "Ask for the accession number and the collection date." in body
        assert "Goes to Accessioning" in body and "high priority" in body
        assert client.get(url, {"category": group.pk}).content.strip() == b""  # a group has none
        login(client, clinic_portal)
        assert client.get(url, {"category": sample_category.pk}).status_code == 403

    def test_guidance_partial_survives_a_malformed_category(self, client, agent):
        login(client, agent)
        response = client.get(reverse("desk:category-guidance"), {"category": "nonsense"})
        assert response.status_code < 500

    def test_phone_channel_reaches_the_clock_through_the_hook(
        self, client, agent, category, monkeypatch
    ):
        seen = []
        monkeypatch.setattr(
            "service_levels.hooks.request_created", lambda request: seen.append(request.channel)
        )
        login(client, agent)
        self.post(client, category, channel="phone")
        assert seen == ["phone"]


# ------------------------------------------------------------------------------------ D2


class TestRequestPage:
    def test_entries_are_labelled_in_words(self, client, member, make_user, category):
        request = new_request(member, category)
        reply = reply_to(member, request, "Reply text")
        request.refresh_from_db()
        services.add_entry(
            actor=member,
            request=request,
            kind=EntryKind.CONTACT,
            body="Rang the office",
            expected_revision=request.revision,
            contact={
                "direction": "to_client",
                "channel": "phone",
                "with_whom": "Office manager",
                "outcome": "spoke_with",
            },
        )
        login(client, member)
        page = client.get(detail_url(request)).content.decode()
        assert "Reply, visible to the client" in page
        assert "Internal note" in page
        assert "Logged contact, internal" in page
        assert "To the client by phone, Office manager" in page and "Spoke with" in page
        # staff still see a withdrawn reply, labelled
        supervisor = make_user("sup", "supervisor")
        services.withdraw_reply(
            actor=supervisor, entry=reply, expected_revision=seen(reply), reason="Wrong request"
        )
        page = client.get(detail_url(request)).content.decode()
        assert "Reply, withdrawn from client view" in page
        assert "Withdrawn by" in page and "Wrong request" in page

    def test_events_read_as_phrases(self, client, member, tech, quality, category, sample_category):
        request = new_request(member, category)
        services.assign(
            actor=member, request=request, expected_revision=1, team=quality, reason="Not ours"
        )
        request.refresh_from_db()
        services.change_status(
            actor=member,
            request=request,
            expected_revision=request.revision,
            status=Status.WAITING_CLIENT,
        )
        request.refresh_from_db()
        services.update_properties(
            actor=member,
            request=request,
            expected_revision=request.revision,
            priority="urgent",
            category=sample_category,
        )
        request.refresh_from_db()
        services.set_complaint(
            actor=member,
            request=request,
            expected_revision=request.revision,
            is_complaint=True,
            reason="Second time",
        )
        login(client, member)
        response = client.get(detail_url(request))
        phrases = [e.phrase for e in response.context["entries"] if e.kind == EntryKind.EVENT]
        assert "Logged (Phone)" in phrases
        assert "Moved to Quality" in phrases
        assert "Status: Open → Waiting on client" in phrases
        assert "Changed priority urgent and category changed" in phrases
        assert "Marked as a complaint" in phrases
        page = response.content.decode()
        assert "Moved to Quality" in page and "Status: Open" in page

    def test_assignment_events_name_the_person(self, client, member, tech, team, category):
        request = new_request(member, category)
        services.assign(
            actor=member, request=request, expected_revision=1, team=team, assignee=tech
        )
        login(client, member)
        entries = client.get(detail_url(request)).context["entries"]
        phrases = [e.phrase for e in entries if e.kind == EntryKind.EVENT]
        assert "Assigned to tech" in phrases

    def test_entries_are_listed_newest_first(self, client, member, category):
        request = new_request(member, category)
        reply_to(member, request, "Newest words")
        login(client, member)
        entries = client.get(detail_url(request)).context["entries"]
        assert entries[0].body == "Newest words"
        assert entries[-1].event_action == "created"

    def test_previous_and_next_follow_the_lists_filters_and_sort(self, client, member, category):
        made = [new_request(member, category, subject=f"R{n}") for n in range(1, 6)]
        r1, r2, r3, r4, r5 = made
        for waiting in (r2, r3, r5):
            set_status(waiting, Status.WAITING_CLIENT)
        login(client, member)
        query = "view=all&status=waiting_client&sort=number"
        response = client.get(detail_url(r3), {"list": query})
        assert response.context["previous_url"].startswith(detail_url(r2))
        assert response.context["next_url"].startswith(detail_url(r5))  # r4 is not waiting
        assert "Previous in list" in response.content.decode()
        reverse_sorted = client.get(
            detail_url(r3), {"list": "view=all&status=waiting_client&sort=-number"}
        )
        assert reverse_sorted.context["previous_url"].startswith(detail_url(r5))
        assert reverse_sorted.context["next_url"].startswith(detail_url(r2))
        first = client.get(detail_url(r2), {"list": query})
        assert first.context["previous_url"] == ""
        last = client.get(detail_url(r5), {"list": query})
        assert last.context["next_url"] == ""
        outside = client.get(detail_url(r4), {"list": query})  # not in that list at all
        assert outside.context["previous_url"] == outside.context["next_url"] == ""
        without = client.get(detail_url(r3))
        assert without.context["previous_url"] == without.context["next_url"] == ""
        assert r1.pk  # created but filtered out of every list above

    def test_neighbours_follow_a_priority_sort(self, member, category):
        low = new_request(member, category, subject="low", priority="low")
        urgent = new_request(member, category, subject="urgent", priority="urgent")
        normal = new_request(member, category, subject="normal", priority="normal")
        high = new_request(member, category, subject="high", priority="high")
        order = lambda sort: list(  # noqa: E731
            ordered_requests(member, QueryDict(f"view=all&sort={sort}")).values_list(
                "pk", flat=True
            )
        )
        assert order("-priority") == [urgent.pk, high.pk, normal.pk, low.pk]
        assert order("priority") == [low.pk, normal.pk, high.pk, urgent.pk]

    def test_an_invalid_filter_gives_no_neighbours(self, member, category):
        new_request(member, category)
        assert not ordered_requests(member, QueryDict("view=all&status=bogus")).exists()
        assert not ordered_requests(member, QueryDict("view=all&per_page=9")).exists()


# ------------------------------------------------------------------------------------ D3


class TestComposer:
    def test_reply_note_and_contact_create_the_right_kinds(self, client, member, category):
        request = new_request(member, category)
        login(client, member)
        expected = {
            "reply": "Reply sent.",
            "note": "Note added.",
            "contact": "Contact logged.",
        }
        for kind, text in expected.items():
            request.refresh_from_db()
            extra = (
                {
                    "contact_direction": "from_client",
                    "contact_channel": "email",
                    "contact_with": "Pat",
                    "contact_outcome": "message_delivered",
                }
                if kind == "contact"
                else {}
            )
            response = post_entry(client, request, kind=kind, body=f"A {kind}", **extra)
            assert response.status_code == 302 and response["Location"] == detail_url(request)
            entry = request.entries.exclude(kind=EntryKind.EVENT).latest("pk")
            assert (entry.kind, entry.body, entry.author) == (kind, f"A {kind}", member)
            follow = client.get(response["Location"])
            assert text in messages_of(follow)
        contact = request.entries.get(kind=EntryKind.CONTACT)
        assert (contact.contact_direction, contact.contact_with) == ("from_client", "Pat")

    def test_contact_fields_are_required_only_for_a_contact(self, client, member, category):
        request = new_request(member, category)
        login(client, member)
        assert post_entry(client, request, kind="note").status_code == 302
        request.refresh_from_db()
        before = request.entries.count()
        response = post_entry(
            client,
            request,
            kind="contact",
            body="Typed but not logged",
            contact_direction="to_client",
            contact_channel="phone",
            contact_with="Someone",
        )
        assert response.status_code == 400
        assert "contact_outcome" in response.context["composer"].errors
        assert "Typed but not logged" in response.content.decode()  # the input is kept
        assert request.entries.count() == before
        request.refresh_from_db()
        assert request.revision == 2

    def test_contact_details_on_a_note_are_ignored_by_the_form(self, client, member, category):
        request = new_request(member, category)
        login(client, member)
        response = post_entry(client, request, kind="note", contact_outcome="spoke_with")
        assert response.status_code == 302
        assert request.entries.get(body="A line of text").contact_outcome == ""

    def test_empty_text_is_refused_and_the_page_reopens(self, client, member, category):
        request = new_request(member, category)
        login(client, member)
        response = post_entry(client, request, body="")
        assert response.status_code == 400
        assert "body" in response.context["composer"].errors

    def test_reaches_nobody_notice(self, client, member, category, clinic, clinic_portal):
        by_staff = new_request(member, category, client=clinic)  # no portal account can see it
        login(client, member)
        page = client.get(detail_url(by_staff))
        assert page.context["audience"] == []
        assert NOBODY_NOTICE in page.content.decode()
        assert page.context["composer"].initial["kind"] == EntryKind.NOTE
        raised = services.create_request(
            actor=clinic_portal,
            category=category,
            subject="Where is it?",
            first_entry_kind=EntryKind.REPLY,
            first_entry_body="Order ORD-TEST-5",
            channel="portal",
        )
        page = client.get(detail_url(raised))
        assert page.context["audience"] == [clinic_portal]
        assert NOBODY_NOTICE not in page.content.decode()
        assert page.context["composer"].initial["kind"] == EntryKind.REPLY
        assert "1 portal account" in page.content.decode()

    def test_an_inactive_portal_account_does_not_count_as_audience(
        self, client, member, category, clinic_portal
    ):
        raised = services.create_request(
            actor=clinic_portal,
            category=category,
            subject="Where is it?",
            first_entry_kind=EntryKind.REPLY,
            first_entry_body="Order ORD-TEST-5",
            channel="portal",
        )
        clinic_portal.is_active = False
        clinic_portal.save()
        login(client, member)
        assert client.get(detail_url(raised)).context["audience"] == []

    def test_status_change_in_the_same_submit(self, client, member, category):
        request = new_request(member, category)
        login(client, member)
        response = post_entry(
            client, request, kind="reply", body="Please send the date", new_status="waiting_client"
        )
        assert response.status_code == 302
        request.refresh_from_db()
        assert request.status == Status.WAITING_CLIENT
        assert request.entries.filter(event_action="status_changed").count() == 1
        assert AuditEvent.objects.filter(action="request.status_changed").count() == 1

    def test_resolving_is_not_offered_in_the_composer(self, client, member, category):
        request = new_request(member, category)
        login(client, member)
        response = post_entry(client, request, new_status="resolved")
        assert response.status_code == 400
        assert "new_status" in response.context["composer"].errors
        request.refresh_from_db()
        assert request.status == Status.OPEN

    def test_a_resolved_request_is_reopened_with_status_not_the_composer(
        self, client, member, category
    ):
        request = set_status(new_request(member, category), Status.RESOLVED)
        login(client, member)
        response = post_entry(client, request, new_status="open")
        assert response.status_code == 400
        request.refresh_from_db()
        assert request.status == Status.RESOLVED
        assert post_entry(client, request).status_code == 302  # a plain note is still fine

    def test_stale_revision_shows_an_error_and_adds_nothing(self, client, member, category):
        request = new_request(member, category)
        stale = request.revision
        reply_to(member, request, "Someone else answered first")
        request.refresh_from_db()
        count = request.entries.count()
        login(client, member)
        response = post_entry(
            client, request, kind="note", body="My unsent words", expected_revision=stale
        )
        assert response.status_code == 400
        assert any("changed" in error for error in response.context["composer"].non_field_errors())
        page = response.content.decode()
        assert "My unsent words" in page and "This ticket changed" in page
        assert request.entries.count() == count

    def test_portal_account_and_signed_out_visitors_cannot_post(
        self, client, member, category, clinic_portal
    ):
        request = new_request(member, category)
        assert post_entry(client, request).status_code == 302  # to sign in
        login(client, clinic_portal)
        assert post_entry(client, request).status_code == 403
        assert request.entries.count() == 2


# ---------------------------------------------------------------------------- D4, D5, D6


class TestAssignAndTakePages:
    def test_take_by_a_member_and_refusal_for_a_non_member(
        self, client, agent, tech, category, team
    ):
        request = new_request(agent, category)
        login(client, agent)  # agent belongs to no team
        response = client.post(
            reverse("desk:request-take", args=[request.pk]),
            {"expected_revision": 1},
            follow=True,
        )
        assert any("Only members of" in m for m in messages_of(response))
        request.refresh_from_db()
        assert request.assignee is None
        login(client, tech)
        response = client.post(
            reverse("desk:request-take", args=[request.pk]),
            {"expected_revision": 1},
            follow=True,
        )
        assert "Assigned to you." in messages_of(response)
        request.refresh_from_db()
        assert request.assignee == tech
        assert AuditEvent.objects.filter(action="request.assigned", actor=tech).count() == 1

    def test_take_with_a_stale_revision_reports_it(self, client, tech, agent, category):
        request = new_request(agent, category)
        reply_to(agent, request)
        login(client, tech)
        response = client.post(
            reverse("desk:request-take", args=[request.pk]), {"expected_revision": 1}, follow=True
        )
        assert any("changed" in m for m in messages_of(response))

    def test_take_button_is_for_members_and_hidden_on_closed_requests(
        self, client, agent, tech, category
    ):
        request = new_request(agent, category)
        login(client, agent)
        take = f'action="{reverse("desk:request-take", args=[request.pk])}"'  # the page help
        assert take not in client.get(detail_url(request)).content.decode()  # also says "Take it"
        login(client, tech)
        assert take in client.get(detail_url(request)).content.decode()
        set_status(request, Status.CLOSED)
        assert take not in client.get(detail_url(request)).content.decode()

    def test_assign_page_assigns_with_a_reason(self, client, agent, tech, team, category):
        request = new_request(agent, category)
        login(client, agent)
        url = reverse("desk:request-assign", args=[request.pk])
        assert client.get(url).status_code == 200
        response = client.post(
            url,
            {"assign_to": f"{team.pk}:{tech.pk}", "expected_revision": 1, "reason": "Covering"},
        )
        assert response.status_code == 302
        request.refresh_from_db()
        assert request.assignee == tech
        assert AuditEvent.objects.get(action="request.assigned").reason == "Covering"

    def test_assign_page_only_takes_members_of_the_chosen_team(
        self, client, agent, tech, quality, category
    ):
        request = new_request(agent, category)
        login(client, agent)
        response = client.post(
            reverse("desk:request-assign", args=[request.pk]),
            {"assign_to": f"{quality.pk}:{tech.pk}", "expected_revision": 1},
        )
        assert response.status_code == 200
        assert "assign_to" in response.context["form"].errors
        request.refresh_from_db()
        assert request.assignee is None

    def test_moving_to_another_team_through_the_page_clears_the_person(
        self, client, agent, tech, quality, category
    ):
        request = new_request(agent, category, assignee=tech)
        login(client, agent)
        response = client.post(
            reverse("desk:request-assign", args=[request.pk]),
            {"assign_to": f"{quality.pk}:", "expected_revision": 1},
        )
        assert response.status_code == 302
        request.refresh_from_db()
        assert (request.team, request.assignee) == (quality, None)

    def test_moving_a_request_out_of_reach_redirects_to_the_list(
        self, client, agent, make_user, privacy_category, quality, team, join
    ):
        colleague = make_user("quality_member", "technical")
        join(colleague, quality)
        request = new_request(agent, privacy_category)  # restricted: the quality team's
        login(client, colleague)
        response = client.post(
            reverse("desk:request-assign", args=[request.pk]),
            {"assign_to": f"{team.pk}:", "expected_revision": 1},
            follow=True,
        )
        assert response.status_code == 200  # the list, not a 404 about the request just changed
        assert response.redirect_chain[-1][0].startswith(reverse("desk:request-list"))
        assert messages_of(response)
        request.refresh_from_db()
        assert request.team == team

    def test_editing_a_request_into_a_restricted_category_redirects_to_the_list(
        self, client, agent, category, privacy_category
    ):
        request = new_request(agent, category)  # agent belongs to no team
        login(client, agent)
        response = client.post(
            reverse("desk:request-properties", args=[request.pk]),
            {"category": privacy_category.pk, "priority": "normal", "expected_revision": 1},
            follow=True,
        )
        assert response.status_code == 200
        assert response.redirect_chain[-1][0].startswith(reverse("desk:request-list"))
        assert messages_of(response)
        request.refresh_from_db()
        assert request.category == privacy_category

    def test_stale_assignment_keeps_the_page(self, client, agent, tech, team, category):
        request = new_request(agent, category)
        reply_to(agent, request)
        login(client, agent)
        response = client.post(
            reverse("desk:request-assign", args=[request.pk]),
            {"assign_to": f"{team.pk}:{tech.pk}", "expected_revision": 1},
        )
        assert response.status_code == 200
        assert any("changed" in e for e in response.context["form"].non_field_errors())

    def test_portal_account_gets_403(self, client, agent, category, clinic_portal):
        request = new_request(agent, category)
        login(client, clinic_portal)
        assert client.get(reverse("desk:request-assign", args=[request.pk])).status_code == 403
        assert client.get(detail_url(request)).status_code == 403


class TestStatusPage:
    def url(self, request):
        return reverse("desk:request-status", args=[request.pk])

    def test_resolve_needs_a_summary(self, client, agent, category):
        request = new_request(agent, category)
        login(client, agent)
        response = client.post(
            self.url(request), {"status": "resolved", "summary": "", "expected_revision": 1}
        )
        assert response.status_code == 200
        assert "summary" in response.context["form"].errors
        response = client.post(
            self.url(request),
            {"status": "resolved", "summary": "Re-sent the report", "expected_revision": 1},
        )
        assert response.status_code == 302
        request.refresh_from_db()
        assert request.status == Status.RESOLVED
        assert request.resolution_summary == "Re-sent the report"

    def test_reopen_needs_a_reason(self, client, agent, category):
        request = new_request(agent, category)
        services.change_status(
            actor=agent,
            request=request,
            expected_revision=1,
            status=Status.RESOLVED,
            summary="Done",
        )
        request.refresh_from_db()
        login(client, agent)
        data = {"status": "open", "expected_revision": request.revision}
        response = client.post(self.url(request), data)
        assert response.status_code == 200 and "reason" in response.context["form"].errors
        response = client.post(self.url(request), {**data, "reason": "Client disagrees"})
        assert response.status_code == 302
        request.refresh_from_db()
        assert request.status == Status.OPEN

    def test_closed_cannot_change(self, client, agent, category):
        request = set_status(new_request(agent, category), Status.CLOSED)
        login(client, agent)
        assert client.get(self.url(request)).status_code == 403
        response = client.post(
            self.url(request), {"status": "open", "reason": "Why", "expected_revision": 1}
        )
        assert response.status_code == 403
        request.refresh_from_db()
        assert request.status == Status.CLOSED

    def test_status_choices_depend_on_the_current_status(self, client, agent, category):
        request = new_request(agent, category)
        login(client, agent)
        choices = [
            value
            for value, _ in client.get(self.url(request)).context["form"].fields["status"].choices
        ]
        assert choices == ["waiting_client", "waiting_third_party", "resolved"]
        set_status(request, Status.RESOLVED)
        choices = [
            value
            for value, _ in client.get(self.url(request)).context["form"].fields["status"].choices
        ]
        assert "closed" in choices and "open" in choices and "resolved" not in choices

    def test_close_from_the_page(self, client, agent, category):
        request = set_status(new_request(agent, category), Status.RESOLVED)
        login(client, agent)
        response = client.post(
            self.url(request), {"status": "closed", "expected_revision": request.revision}
        )
        assert response.status_code == 302
        request.refresh_from_db()
        assert request.status == Status.CLOSED


class TestPropertiesPage:
    def url(self, request):
        return reverse("desk:request-properties", args=[request.pk])

    def test_change_is_saved_and_reaches_the_clock(
        self, client, agent, category, sample_category, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(
            "service_levels.hooks.request_changed",
            lambda request, **kwargs: calls.append(kwargs["previous"]),
        )
        request = new_request(agent, category)
        login(client, agent)
        response = client.post(
            self.url(request),
            {
                "category": sample_category.pk,
                "priority": "urgent",
                "expected_revision": 1,
                "reason": "Escalated",
            },
        )
        assert response.status_code == 302
        request.refresh_from_db()
        assert (request.category, request.priority) == (sample_category, "urgent")
        assert calls == [{"priority": "normal", "category_id": category.pk}]
        record = AuditEvent.objects.get(action="request.updated")
        assert record.after == {"priority": "urgent", "category_id": sample_category.pk}
        assert record.reason == "Escalated"

    def test_nothing_changed_keeps_the_page(self, client, agent, category):
        request = new_request(agent, category)
        login(client, agent)
        response = client.post(
            self.url(request),
            {"category": category.pk, "priority": "normal", "expected_revision": 1},
        )
        assert response.status_code == 200
        assert any("Change the priority" in e for e in response.context["form"].non_field_errors())

    def test_complaint_category_marks_the_request(self, client, agent, group, category):
        complaint = RequestCategory.objects.create(
            key="complaint", name="Complaint", parent=group, is_complaint=True
        )
        request = new_request(agent, category)
        login(client, agent)
        client.post(
            self.url(request),
            {"category": complaint.pk, "priority": "normal", "expected_revision": 1},
        )
        request.refresh_from_db()
        assert request.is_complaint
        assert "Complaint" in client.get(detail_url(request)).content.decode()


# ------------------------------------------------------------------------------------ D9


class TestWithdrawPage:
    @pytest.fixture
    def reply(self, agent, category):
        return reply_to(agent, new_request(agent, category), "Posted on the wrong request")

    def url(self, reply):
        return reverse("desk:entry-withdraw", args=[reply.request_id, reply.pk])

    def test_only_a_supervisor(self, client, agent, make_user, reply):
        for user in (agent, make_user("tech3", "technical")):
            login(client, user)
            assert client.get(self.url(reply)).status_code == 403
            assert client.post(self.url(reply), {"reason": "Wrong"}).status_code == 403
        reply.refresh_from_db()
        assert reply.withdrawn_at is None
        # the link is not offered to them either
        assert self.url(reply) not in client.get(detail_url(reply.request)).content.decode()

    def test_reason_is_required_then_withdrawn(self, client, lead, reply):
        login(client, lead)
        assert self.url(reply) in client.get(detail_url(reply.request)).content.decode()
        seen_revision = seen(reply)  # the revision the open dialog carries
        response = client.post(self.url(reply), {"reason": "", "expected_revision": seen_revision})
        assert response.status_code == 200 and "reason" in response.context["form"].errors
        response = client.post(
            self.url(reply), {"reason": "Wrong request", "expected_revision": seen_revision}
        )
        assert response.status_code == 302
        reply.refresh_from_db()
        assert reply.withdrawn_by == lead
        assert AuditEvent.objects.filter(action="entry.withdrawn").count() == 1

    def test_a_second_withdrawal_changes_nothing(self, client, lead, reply):
        login(client, lead)
        client.post(self.url(reply), {"reason": "Wrong request", "expected_revision": seen(reply)})
        response = client.post(
            self.url(reply), {"reason": "Again", "expected_revision": seen(reply)}
        )
        assert response.status_code == 302
        reply.refresh_from_db()
        assert reply.withdrawal_reason == "Wrong request"
        assert AuditEvent.objects.filter(action="entry.withdrawn").count() == 1

    def test_a_note_cannot_be_withdrawn(self, client, lead, agent, category):
        request = new_request(agent, category)
        note = request.entries.get(kind=EntryKind.NOTE)
        login(client, lead)
        assert client.get(self.url(note)).status_code == 404


# ----------------------------------------------------------------------------- D10: lists


@pytest.fixture
def desk(agent, tech, lead, team, quality, category, sample_category, clinic, join):
    """Requests in every system view, for `agent` (a member of the default team). The subject
    of each names it."""
    join(agent, team)

    def make(name, status=None, **extra):
        request = new_request(agent, extra.pop("category", category), subject=name, **extra)
        if status:
            set_status(request, status)
        return request

    data = {
        "mine_open": make("mine_open", assignee=agent, priority="urgent", client=clinic),
        "mine_wait": make("mine_wait", Status.WAITING_CLIENT, assignee=agent),
        "mine_resolved": make("mine_resolved", Status.RESOLVED, assignee=agent),
        "unassigned": make("unassigned", priority="high"),
        "complaint": make("complaint"),
        "wait_third": make("wait_third", Status.WAITING_THIRD_PARTY, assignee=tech),
        "resolved": make("resolved", Status.RESOLVED, assignee=tech),
        "closed_recent": make(
            "closed_recent", Status.CLOSED, category=sample_category, priority="normal"
        ),
        "closed_old": make("closed_old", Status.CLOSED, priority="low"),
        "elsewhere": make("elsewhere", team=quality),
    }
    ServiceRequest.objects.filter(pk=data["closed_old"].pk).update(
        closed_at=timezone.now() - timedelta(days=30)
    )
    complaint = data["complaint"]
    services.set_complaint(
        actor=agent,
        request=complaint,
        expected_revision=complaint.revision,
        is_complaint=True,
        reason="Reported twice",
    )
    return data


ALL = {
    "mine_open",
    "mine_wait",
    "mine_resolved",
    "unassigned",
    "complaint",
    "wait_third",
    "resolved",
    "closed_recent",
    "closed_old",
    "elsewhere",
}
SYSTEM_VIEW_CONTENT = {
    "my-open": {"mine_open", "mine_wait"},
    "unassigned": {"unassigned", "complaint"},
    "my-teams": {"mine_open", "mine_wait", "unassigned", "complaint", "wait_third"},
    "waiting-client": {"mine_wait"},
    "waiting-third-party": {"wait_third"},
    "resolved": {"mine_resolved", "resolved"},
    "recently-closed": {"closed_recent"},
    "complaints-review": {"complaint"},
    "all": ALL,
}


class TestRequestList:
    def get(self, browser, **params):
        return browser.get(reverse("desk:request-list"), params)

    @pytest.mark.parametrize("view", sorted(SYSTEM_VIEW_CONTENT))
    def test_each_system_view_shows_the_right_requests(self, client, agent, desk, view):
        login(client, agent)
        response = self.get(client, view=view)
        assert response.status_code == 200
        assert subjects(response) == SYSTEM_VIEW_CONTENT[view]

    def test_view_picker_counts_match_the_views(self, client, agent, desk):
        login(client, agent)
        response = self.get(client)
        counts = {v["label"]: v["count"] for v in response.context["views"]}
        assert counts["My open"] == 2
        assert counts["Unassigned in my teams"] == 2
        assert counts["All tickets"] == len(ALL)
        assert response.context["current_view"] == "my-open"  # the default view

    def test_unknown_view_opens_the_default_view(self, client, agent, desk):
        login(client, agent)
        response = self.get(client, view="nonsense", status="open")
        assert response.status_code == 302
        assert "view=" not in response.url and "status=open" in response.url

    def test_overdue_view_opens(self, client, agent, desk):
        login(client, agent)
        assert self.get(client, view="overdue").status_code == 200  # clock rules arrive with C

    def test_recently_closed_leaves_out_old_closures(self, client, agent, desk):
        login(client, agent)
        assert "closed_old" not in subjects(self.get(client, view="recently-closed"))
        assert "closed_old" in subjects(self.get(client, view="all", status="closed"))

    def test_status_filter(self, client, agent, desk):
        login(client, agent)
        assert subjects(self.get(client, view="all", status="waiting_client")) == {"mine_wait"}
        assert subjects(self.get(client, view="all", status="closed")) == {
            "closed_recent",
            "closed_old",
        }
        active = subjects(self.get(client, view="all", status="active"))
        assert active == ALL - {"mine_resolved", "resolved", "closed_recent", "closed_old"}

    def test_assignee_filter(self, client, agent, desk):
        login(client, agent)
        assert subjects(self.get(client, view="all", assignee="me")) == {
            "mine_open",
            "mine_wait",
            "mine_resolved",
        }
        assigned = {"mine_open", "mine_wait", "mine_resolved", "wait_third", "resolved"}
        assert subjects(self.get(client, view="all", assignee="none")) == ALL - assigned

    def test_person_team_category_priority_and_organization_filters(
        self, client, agent, desk, tech, quality, sample_category, clinic
    ):
        login(client, agent)
        assert subjects(self.get(client, view="all", person=tech.pk)) == {"wait_third", "resolved"}
        assert subjects(self.get(client, view="all", team=quality.pk)) == {"elsewhere"}
        assert subjects(self.get(client, view="all", category=sample_category.pk)) == {
            "closed_recent"
        }
        assert subjects(self.get(client, view="all", priority="urgent")) == {"mine_open"}
        assert subjects(self.get(client, view="all", priority="low")) == {"closed_old"}
        assert subjects(self.get(client, view="all", client=clinic.pk)) == {"mine_open"}

    def test_created_date_filter(self, client, agent, desk):
        login(client, agent)
        tomorrow = (timezone.now() + timedelta(days=2)).date().isoformat()
        assert subjects(self.get(client, view="all", created_from=tomorrow)) == set()
        today = timezone.localdate().isoformat()
        assert subjects(self.get(client, view="all", created_from=today)) == ALL

    def test_search_by_number_subject_and_organization(self, client, agent, desk):
        login(client, agent)
        assert subjects(self.get(client, view="all", q="mine_")) == {
            "mine_open",
            "mine_wait",
            "mine_resolved",
        }
        assert subjects(self.get(client, view="all", q="Example Clinic")) == {"mine_open"}
        number = desk["elsewhere"].number
        assert subjects(self.get(client, view="all", q=number)) == {"elsewhere"}

    def test_complaint_filters(self, client, agent, desk, lead, make_user):
        login(client, agent)
        assert subjects(self.get(client, view="all", complaint="yes")) == {"complaint"}
        assert subjects(self.get(client, view="all", complaint="no")) == ALL - {"complaint"}
        complaint = desk["complaint"]
        complaint.refresh_from_db()
        second = make_user("lead2", "supervisor")
        login(client, lead)
        assert subjects(self.get(client, view="all", complaint="review")) == set()  # undecided
        services.decide_complaint(
            actor=lead,
            request=complaint,
            expected_revision=complaint.revision,
            decision=ComplaintDecision.INVESTIGATE,
            rationale="Repeated delay.",
        )
        assert subjects(self.get(client, view="all", complaint="review")) == set()  # lead decided
        login(client, second)
        assert subjects(self.get(client, view="all", complaint="review")) == {"complaint"}

    def test_server_side_sort(self, client, agent, desk):
        login(client, agent)

        def order(sort):
            response = self.get(client, view="all", sort=sort)
            return [req.subject for req in response.context["page_obj"]]

        by_subject = order("subject")
        assert by_subject == sorted(ALL)
        assert order("-subject") == sorted(ALL, reverse=True)
        by_priority = order("-priority")
        assert by_priority[:2] == ["mine_open", "unassigned"]  # urgent, high
        assert by_priority[-1] == "closed_old"  # low
        ascending = order("priority")
        assert ascending[0] == "closed_old" and ascending[-2:] == ["unassigned", "mine_open"]
        by_number = order("number")
        assert by_number == [r.subject for r in sorted(desk.values(), key=lambda r: r.pk)]
        assert order("nonsense") == order("-updated_at")  # an unknown sort uses the default

    def test_invalid_filters_show_no_rows_and_a_bad_page_size_is_refused(self, client, agent, desk):
        login(client, agent)
        response = self.get(client, view="all", status="bogus")
        assert response.status_code == 200 and subjects(response) == set()
        assert self.get(client, view="all", per_page=7).status_code == 400

    def test_pagination_is_server_side(self, client, agent, category):
        made = [new_request(agent, category, subject=f"Row {n:02d}") for n in range(30)]
        login(client, agent)
        first = self.get(client, view="all", sort="number")
        second = self.get(client, view="all", sort="number", page=2)
        assert first.context["page_obj"].paginator.count == 30
        assert [r.pk for r in first.context["page_obj"]] == [r.pk for r in made[:25]]
        assert [r.pk for r in second.context["page_obj"]] == [r.pk for r in made[25:]]
        page = self.get(client, view="all", per_page=50)
        assert len(page.context["page_obj"]) == 30

    def test_portal_account_gets_403(self, client, clinic_portal):
        login(client, clinic_portal)
        assert self.get(client).status_code == 403

    def test_opens_with_nothing_logged(self, client, agent):
        login(client, agent)
        response = self.get(client, view="all")
        assert response.status_code == 200 and subjects(response) == set()


class TestBulkPages:
    def post(self, client, ids, **data):
        return client.post(
            reverse("desk:request-bulk"),
            {
                # As the list posts them: "<id>:<revision>".
                "requests": [f"{pk}:{revision}" for pk, revision in selection(ids).items()],
                "next": reverse("desk:request-list"),
                **data,
            },
            follow=True,
        )

    def test_bulk_assign_is_audited_for_each_request(self, client, agent, tech, team, category):
        requests = [new_request(agent, category, subject=f"B{n}") for n in range(3)]
        login(client, agent)
        response = self.post(client, requests, action="assign", assign_to=f"{team.pk}:{tech.pk}")
        assert "3 tickets changed." in messages_of(response)
        for request in requests:
            request.refresh_from_db()
            assert request.assignee == tech
            assert (
                AuditEvent.objects.filter(
                    action="request.assigned", object_id=str(request.pk)
                ).count()
                == 1
            )

    def test_bulk_status_is_audited_for_each_request(self, client, agent, category):
        requests = [new_request(agent, category, subject=f"B{n}") for n in range(2)]
        login(client, agent)
        response = self.post(client, requests, action="status", status="waiting_third_party")
        assert "2 tickets changed." in messages_of(response)
        assert (
            AuditEvent.objects.filter(
                action="request.status_changed", after={"status": "waiting_third_party"}
            ).count()
            == 2
        )

    def test_bulk_close_only_resolved_all_or_nothing(self, client, agent, category):
        done = set_status(new_request(agent, category, subject="Done"), Status.RESOLVED)
        pending = new_request(agent, category, subject="Pending")
        login(client, agent)
        response = self.post(client, [done, pending], action="close")
        assert any(pending.number in m for m in messages_of(response))
        done.refresh_from_db()
        assert done.status == Status.RESOLVED
        response = self.post(client, [done], action="close")
        assert "1 ticket changed." in messages_of(response)
        done.refresh_from_db()
        assert done.status == Status.CLOSED

    def test_bulk_needs_a_selection_and_a_choice(self, client, agent, category):
        request = new_request(agent, category)
        login(client, agent)
        assert any("at least one" in m for m in messages_of(self.post(client, [], action="close")))
        response = self.post(client, [request], action="status", status="")
        assert any("Choose a status" in m for m in messages_of(response))
        response = self.post(client, [request], action="assign", assign_to="")
        assert any("Choose a team" in m for m in messages_of(response))

    def test_next_must_stay_on_this_site(self, client, agent, category):
        request = new_request(agent, category)
        login(client, agent)
        response = client.post(
            reverse("desk:request-bulk"),
            {
                "requests": [f"{request.pk}:{request.revision}"],
                "action": "close",
                "next": "https://elsewhere.example/",
            },
        )
        assert response["Location"] == reverse("desk:request-list")

    def test_bulk_is_post_only_and_staff_only(self, client, agent, clinic_portal):
        login(client, agent)
        assert client.get(reverse("desk:request-bulk")).status_code == 405
        login(client, clinic_portal)
        assert client.post(reverse("desk:request-bulk"), {"action": "close"}).status_code == 403


class TestListQueryBudget:
    """The list page's queries do not grow with its rows (no N+1). Measured 2026-11-17: 46
    queries for 10 rows and for 30 rows (10 of them are the view picker's counts); BUDGET leaves
    headroom for a few more."""

    BUDGET = 60

    def seed(self, agent, tech, team, category, clinic, clinic_site, clinic_contact, count):
        for n in range(count):
            new_request(
                agent,
                category,
                subject=f"Budget {n}",
                client=clinic,
                site=clinic_site,
                contact=clinic_contact,
                assignee=tech if n % 2 else None,
                priority=("low", "normal", "high", "urgent")[n % 4],
            )

    def load(self, client, assert_max_queries):
        with assert_max_queries(self.BUDGET), CaptureQueriesContext(connection) as queries:
            response = client.get(reverse("desk:request-list"), {"view": "all"})
        assert response.status_code == 200
        return len(queries), response

    def test_same_queries_for_10_and_30_rows(
        self,
        client,
        agent,
        tech,
        team,
        category,
        clinic,
        clinic_site,
        clinic_contact,
        django_assert_max_num_queries,
    ):
        login(client, agent)
        args = (agent, tech, team, category, clinic, clinic_site, clinic_contact)
        client.get(reverse("desk:request-list"), {"view": "all"})  # warm the per-process caches
        self.seed(*args, 10)
        few, response = self.load(client, django_assert_max_num_queries)
        assert len(response.context["page_obj"]) == 10
        self.seed(*args, 20)
        many, response = self.load(client, django_assert_max_num_queries)
        assert len(response.context["page_obj"]) == 25  # one full page of 30 requests
        assert response.context["page_obj"].paginator.count == 30
        assert many == few, f"{few} queries for 10 rows but {many} for 30: an N+1"
        print(f"request list queries: {few} (10 rows), {many} (30 rows)")


# ----------------------------------------------------------------------------------- D12


class TestFind:
    url = reverse("desk:request-find")

    def test_exact_number_opens_the_request(self, client, agent, category):
        request = new_request(agent, category)
        login(client, agent)
        for term in (request.number, request.number.lower(), str(request.pk), f"{request.pk:06d}"):
            response = client.get(self.url, {"q": term})
            assert response.status_code == 302, term
            assert response["Location"] == detail_url(request)
        assert client.get(self.url, {"q": "REQ-999999"}).status_code == 200  # no such request

    def test_organization_and_contact_names(
        self, client, agent, category, clinic, water, clinic_contact
    ):
        by_contact = new_request(agent, category, client=clinic, contact=clinic_contact)
        by_water = new_request(agent, category, client=water)
        anonymous = new_request(agent, category, reported_by="Unknown caller")
        login(client, agent)

        def found(term):
            return {r.pk for r in client.get(self.url, {"q": term}).context["results"]}

        assert found("Example Clinic") == {by_contact.pk}
        assert found("Pat Exam") == {by_contact.pk}
        assert found("water authority") == {by_water.pk}
        assert found("Unknown caller") == {anonymous.pk}
        assert found("nobody by this name") == set()

    def test_subject_and_reference(self, client, agent, category):
        request = new_request(agent, category, subject="Missing report", client_reference="ORD-9")
        login(client, agent)
        assert [r.pk for r in client.get(self.url, {"q": "missing rep"}).context["results"]] == [
            request.pk
        ]
        assert [r.pk for r in client.get(self.url, {"q": "ORD-9"}).context["results"]] == [
            request.pk
        ]

    def test_identifier_results_come_from_links_search(self, client, agent, category, monkeypatch):
        linked = new_request(agent, category, subject="Nothing in common")
        other = new_request(agent, category, subject="Another")
        asked = []

        def fake(user, term):
            asked.append((user, term))
            return [linked.pk] if term == "SMP-TEST-77" else []

        monkeypatch.setattr("desk.views.request_ids_for_identifier", fake)
        login(client, agent)
        response = client.get(self.url, {"q": "SMP-TEST-77"})
        assert [r.pk for r in response.context["results"]] == [linked.pk]
        assert response.context["identifier_ids"] == {linked.pk}
        assert "A linked record" in response.content.decode()
        assert asked == [(agent, "SMP-TEST-77")]
        assert other.pk not in {r.pk for r in response.context["results"]}

    def test_empty_search_and_access(self, client, agent, clinic_portal):
        assert client.get(self.url, {"q": "x"}).status_code == 302
        login(client, agent)
        response = client.get(self.url)
        assert response.status_code == 200 and list(response.context["results"]) == []
        login(client, clinic_portal)
        assert client.get(self.url, {"q": "x"}).status_code == 403


# ----------------------------------------------------------------------------------- D14


class TestDashboardQueues:
    DESK_AREA = "desk"

    def queues(self, response):
        return [q for q in response.context["queues"] if q["area"] == self.DESK_AREA]

    def test_each_count_equals_the_list_it_links_to(self, client, agent, desk):
        login(client, agent)
        queues = self.queues(client.get(reverse("dashboard")))
        labels = {q["label"]: q["count"] for q in queues}
        assert labels["My open tickets"] == 2
        assert labels["Unassigned in my teams"] == 2
        assert len(queues) >= 3
        for queue in queues:
            listing = client.get(queue["url"])
            assert listing.status_code == 200, queue["url"]
            assert listing.context["page_obj"].paginator.count == queue["count"], queue["label"]

    def test_counts_follow_the_data(self, client, agent, desk, category):
        login(client, agent)
        before = {q["label"]: q["count"] for q in self.queues(client.get(reverse("dashboard")))}
        new_request(agent, category, subject="One more", assignee=agent)
        after = {q["label"]: q["count"] for q in self.queues(client.get(reverse("dashboard")))}
        assert after["My open tickets"] == before["My open tickets"] + 1
        assert after["Unassigned in my teams"] == before["Unassigned in my teams"]

    def test_complaint_queue_is_for_supervisors(self, client, agent, lead, desk, make_user):
        login(client, agent)
        assert "Complaints awaiting my review" not in [
            q["label"] for q in self.queues(client.get(reverse("dashboard")))
        ]
        complaint = desk["complaint"]
        complaint.refresh_from_db()
        services.decide_complaint(
            actor=lead,
            request=complaint,
            expected_revision=complaint.revision,
            decision=ComplaintDecision.INVESTIGATE,
            rationale="Repeated delay.",
        )
        reviewer = make_user("lead2", "supervisor")
        for user, expected in ((lead, 0), (reviewer, 1)):
            login(client, user)
            queues = {q["label"]: q for q in self.queues(client.get(reverse("dashboard")))}
            queue = queues["Complaints awaiting my review"]
            assert queue["count"] == expected
            assert client.get(queue["url"]).context["page_obj"].paginator.count == expected

    def test_supervisor_counts_match_their_lists_too(self, client, lead, desk):
        login(client, lead)
        for queue in self.queues(client.get(reverse("dashboard"))):
            listing = client.get(queue["url"])
            assert listing.context["page_obj"].paginator.count == queue["count"], queue["label"]


# ----------------------------------------------------------------------------------- D15


class TestHistoryPage:
    @pytest.fixture
    def worked(self, agent, lead, make_user, category, team, join):
        """A complaint that has been assigned, decided, resolved, reviewed and closed."""
        join(agent, team)
        reviewer = make_user("reviewer", "supervisor")
        request = new_request(agent, category, assignee=agent)
        services.set_complaint(
            actor=agent, request=request, expected_revision=1, is_complaint=True, reason="Late"
        )
        for step in (
            lambda r: services.decide_complaint(
                actor=agent,
                request=r,
                expected_revision=r.revision,
                decision=ComplaintDecision.INVESTIGATE,
                rationale="Look into it.",
            ),
            lambda r: services.change_status(
                actor=agent,
                request=r,
                expected_revision=r.revision,
                status=Status.RESOLVED,
                summary="Fixed.",
            ),
            lambda r: services.review_complaint(
                actor=reviewer, request=r, expected_revision=r.revision, note="Checked."
            ),
            lambda r: services.change_status(
                actor=reviewer, request=r, expected_revision=r.revision, status=Status.CLOSED
            ),
        ):
            request.refresh_from_db()
            step(request)
        request.refresh_from_db()
        return request

    def test_supervisor_reads_the_audit_trail(self, client, lead, worked, category, agent):
        unrelated = new_request(agent, category, subject="Unrelated")
        login(client, lead)
        response = client.get(reverse("desk:request-history", args=[worked.pk]))
        assert response.status_code == 200
        actions = [event.action for event in response.context["events"]]
        assert b"Ticket created" in response.content
        for expected in (
            "request.created",
            "entry.added",
            "request.complaint_marked",
            "complaint.decided",
            "request.status_changed",
            "complaint.reviewed",
        ):
            assert expected in actions, expected
        assert actions.count("request.status_changed") == 2
        keys = {(e.object_type, e.object_id) for e in response.context["events"]}
        assert ("desk.servicerequest", str(unrelated.pk)) not in keys
        assert {t for t, _ in keys} == {
            "desk.servicerequest",
            "desk.requestentry",
            "desk.complaintreview",
        }
        assert worked.number in response.content.decode()

    def test_history_is_in_time_order(self, client, lead, worked):
        login(client, lead)
        events = list(
            client.get(reverse("desk:request-history", args=[worked.pk])).context["events"]
        )
        assert events == sorted(events, key=lambda e: e.occurred_at)
        assert events[0].action == "request.created"

    def test_admin_may_read_it(self, client, make_user, worked):
        login(client, make_user("boss", "admin"))
        assert client.get(reverse("desk:request-history", args=[worked.pk])).status_code == 200

    def test_other_roles_get_403(self, client, worked, make_user, agent, clinic_portal):
        url = reverse("desk:request-history", args=[worked.pk])
        for user in (agent, make_user("am", "account_manager"), make_user("tc", "technical")):
            login(client, user)
            assert client.get(url).status_code == 403, user.username
        login(client, clinic_portal)
        assert client.get(url).status_code == 403

    def test_link_is_offered_to_supervisors_only(self, client, lead, agent, worked):
        login(client, lead)
        link = f'href="{reverse("desk:request-history", args=[worked.pk])}'
        assert link in client.get(detail_url(worked)).content.decode()
        login(client, agent)
        assert link not in client.get(detail_url(worked)).content.decode()

    def test_restricted_request_outside_scope_is_a_404(
        self, client, agent, make_user, privacy_category, quality, join
    ):
        restricted = new_request(agent, privacy_category)
        url = reverse("desk:request-history", args=[restricted.pk])
        outsider = make_user("outsider", "technical")
        login(client, outsider)
        assert client.get(url).status_code == 404
        member = make_user("quality_member", "technical")
        join(member, quality)
        login(client, member)
        assert client.get(url).status_code == 403  # may read the request, not its audit trail
        login(client, make_user("sup2", "supervisor"))
        assert client.get(url).status_code == 200

    def test_unknown_request_is_a_404(self, client, lead):
        login(client, lead)
        assert client.get(reverse("desk:request-history", args=[999999])).status_code == 404
