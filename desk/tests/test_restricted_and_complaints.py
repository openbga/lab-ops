"""Access and complaint rules for reading and changing tickets:

D7  A request in a restricted category is visible only to members of its team and to supervisors:
    list, every system view, Find, direct URLs of the page and of every action form, previous and
    next, counts, the dashboard and bulk actions.
D8  A complaint closes only after a review by a supervisor who did not record the decision and has
    never been the request's assignee: the service, the review page, the list filter and the
    dashboard queue agree.
"""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from desk import services
from desk.lists import view_count, view_queryset
from desk.models import ComplaintDecision, EntryKind, Status
from teams.models import ensure_default_team

from .test_contract import new_request, selection

pytestmark = pytest.mark.django_db


@pytest.fixture
def restricted(agent, privacy_category):
    return new_request(agent, privacy_category, subject="Copy of records RECTEST")


@pytest.fixture
def outsider(make_user):
    """A technical staff member outside the restricted request's team."""
    return make_user("outsider", "technical")


@pytest.fixture
def member(make_user, quality, join):
    user = make_user("member", "technical")
    join(user, quality)
    return user


ACTION_ROUTES = (
    "desk:request-detail",
    "desk:request-assign",
    "desk:request-status",
    "desk:request-properties",
    "desk:complaint-mark",
)


class TestRestrictedCategory:
    def test_direct_urls_answer_404_to_outsiders(self, client, outsider, restricted):
        client.force_login(outsider)
        for route in ACTION_ROUTES:
            assert client.get(reverse(route, args=[restricted.pk])).status_code == 404, route
        posts = ("desk:request-entry", "desk:request-take")
        for route in posts:
            response = client.post(reverse(route, args=[restricted.pk]), {"expected_revision": 1})
            assert response.status_code == 404, route

    def test_member_and_supervisor_open_it(self, client, member, make_user, restricted):
        for user in (member, make_user("lead", "supervisor"), make_user("boss", "admin")):
            client.force_login(user)
            response = client.get(reverse("desk:request-detail", args=[restricted.pk]))
            assert response.status_code == 200

    def test_lists_views_and_counts_leave_it_out(self, client, outsider, member, restricted):
        for key in ("all", "my-teams", "unassigned"):
            assert restricted not in view_queryset(outsider, {"view": key})
        assert view_count(outsider, "all") == 0
        assert view_count(member, "all") == 1
        client.force_login(outsider)
        listing = client.get(reverse("desk:request-list"), {"view": "all"})
        assert restricted.number not in listing.content.decode()
        client.force_login(member)
        listing = client.get(reverse("desk:request-list"), {"view": "all"})
        assert restricted.number in listing.content.decode()

    def test_find_leaves_it_out(self, client, outsider, restricted, monkeypatch):
        # Even when the identifier search names it, Find shows nothing to an outsider.
        monkeypatch.setattr(
            "desk.views.request_ids_for_identifier", lambda user, term: [restricted.pk]
        )
        client.force_login(outsider)
        by_number = client.get(reverse("desk:request-find"), {"q": restricted.number})
        assert by_number.status_code == 200  # no redirect to a request it may not read
        by_text = client.get(reverse("desk:request-find"), {"q": "RECTEST"})
        assert restricted.number not in by_text.content.decode()

    def test_dashboard_counts_leave_it_out(self, client, outsider, restricted, quality, join):
        join(outsider, ensure_default_team())
        client.force_login(outsider)
        page = client.get(reverse("dashboard")).content.decode()
        assert restricted.number not in page
        queues = {
            q["label"]: q["count"] for q in client.get(reverse("dashboard")).context["queues"]
        }
        assert queues.get("Unassigned in my teams") == 0

    def test_bulk_refuses_a_hidden_request(self, outsider, restricted, agent, category):
        visible = new_request(agent, category)
        with pytest.raises(PermissionDenied):
            services.bulk_status(
                actor=outsider,
                selected=selection([visible.pk, restricted.pk]),
                status=Status.WAITING_CLIENT,
            )
        visible.refresh_from_db()
        assert visible.status == Status.OPEN  # all or nothing

    def test_bulk_view_refuses_and_changes_nothing(self, client, outsider, restricted):
        client.force_login(outsider)
        response = client.post(
            reverse("desk:request-bulk"),
            {
                "requests": [f"{restricted.pk}:{restricted.revision}"],
                "action": "status",
                "status": "waiting_client",
            },
        )
        assert response.status_code == 302
        restricted.refresh_from_db()
        assert restricted.status == Status.OPEN

    def test_previous_and_next_skip_it(self, client, outsider, agent, category, restricted):
        first = new_request(agent, category)
        second = new_request(agent, category)
        client.force_login(outsider)
        page = client.get(
            reverse("desk:request-detail", args=[second.pk]), {"list": "view=all&sort=number"}
        )
        assert page.context["previous_url"].startswith(
            reverse("desk:request-detail", args=[first.pk])
        )
        assert page.context["next_url"] == ""

    def test_services_refuse_outsiders(self, outsider, restricted):
        with pytest.raises(Exception) as error:
            services.add_entry(
                actor=outsider,
                request=restricted,
                kind=EntryKind.NOTE,
                body="Should not land",
                expected_revision=restricted.revision,
            )
        assert "No such ticket" in str(error.value)


@pytest.fixture
def complaint_setup(agent, make_user, category, join):
    """A complaint assigned to `agent` (a team member), with two supervisors."""
    team = ensure_default_team()
    join(agent, team)
    first_lead = make_user("first_lead", "supervisor")
    second_lead = make_user("second_lead", "supervisor")
    request = new_request(agent, category, assignee=agent)
    services.set_complaint(
        actor=agent, request=request, expected_revision=1, is_complaint=True, reason="Late twice"
    )
    request.refresh_from_db()
    return request, first_lead, second_lead


def decide(actor, request):
    request.refresh_from_db()
    services.decide_complaint(
        actor=actor,
        request=request,
        expected_revision=request.revision,
        decision=ComplaintDecision.INVESTIGATE,
        rationale="Repeated delay.",
    )
    request.refresh_from_db()
    return request


def review(actor, request):
    request.refresh_from_db()
    return services.review_complaint(
        actor=actor, request=request, expected_revision=request.revision, note="Checked."
    )


def resolve(actor, request):
    request.refresh_from_db()
    services.change_status(
        actor=actor,
        request=request,
        expected_revision=request.revision,
        status=Status.RESOLVED,
        summary="Fixed.",
    )
    request.refresh_from_db()
    return request


def close(actor, request):
    request.refresh_from_db()
    services.change_status(
        actor=actor, request=request, expected_revision=request.revision, status=Status.CLOSED
    )


class TestComplaintReview:
    def test_closes_only_after_review(self, agent, complaint_setup):
        request, first_lead, _ = complaint_setup
        resolve(agent, decide(agent, request))
        with pytest.raises(ValidationError):
            close(agent, request)
        review(first_lead, request)
        close(agent, request)
        request.refresh_from_db()
        assert request.status == Status.CLOSED

    def test_decision_author_cannot_review(self, agent, complaint_setup):
        request, first_lead, second_lead = complaint_setup
        services.assign(
            actor=first_lead,
            request=request,
            expected_revision=request.revision,
            team=request.team,
            assignee=None,
        )
        decide(first_lead, request)
        with pytest.raises(PermissionDenied, match="recorded the decision"):
            review(first_lead, request)
        review(second_lead, request)

    def test_current_and_former_assignees_cannot_review(
        self, agent, make_user, complaint_setup, join
    ):
        request, first_lead, second_lead = complaint_setup
        third_lead = make_user("third_lead", "supervisor")
        join(first_lead, request.team)
        services.assign(
            actor=agent,
            request=request,
            expected_revision=request.revision,
            team=request.team,
            assignee=first_lead,
        )
        decide(second_lead, request)
        with pytest.raises(PermissionDenied, match="assigned"):
            review(first_lead, request)  # the current assignee
        request.refresh_from_db()
        services.assign(
            actor=agent,
            request=request,
            expected_revision=request.revision,
            team=request.team,
            assignee=agent,
        )
        with pytest.raises(PermissionDenied, match="assigned"):
            review(first_lead, request)  # a former assignee
        with pytest.raises(PermissionDenied, match="recorded the decision"):
            review(second_lead, request)
        review(third_lead, request)

    def test_assignee_at_creation_counts(self, make_user, category, join):
        team = ensure_default_team()
        lead = make_user("creator_lead", "supervisor")
        other = make_user("other_lead", "supervisor")
        join(lead, team)
        request = new_request(lead, category, assignee=lead)
        services.set_complaint(
            actor=lead, request=request, expected_revision=1, is_complaint=True, reason="x"
        )
        request.refresh_from_db()
        services.assign(
            actor=lead,
            request=request,
            expected_revision=request.revision,
            team=team,
            assignee=None,
        )
        decide(other, request)
        with pytest.raises(PermissionDenied, match="assigned"):
            review(lead, request)

    def test_non_supervisor_cannot_review(self, agent, make_user, complaint_setup):
        request, first_lead, _ = complaint_setup
        decide(first_lead, request)
        with pytest.raises(PermissionDenied, match="supervisor"):
            review(make_user("tech", "technical"), request)

    def test_review_needs_a_decision_and_happens_once(self, agent, complaint_setup):
        request, first_lead, second_lead = complaint_setup
        with pytest.raises(PermissionDenied, match="decision first"):
            review(first_lead, request)
        decide(agent, request)
        review(first_lead, request)
        with pytest.raises(PermissionDenied, match="has been reviewed"):
            review(second_lead, request)
        with pytest.raises(ValidationError):
            decide(agent, request)  # the record is frozen after the review

    def test_database_refuses_reviewer_equal_to_decider(self, agent, complaint_setup):
        from django.db import IntegrityError, transaction

        from desk.models import ComplaintReview

        request, *_ = complaint_setup
        decide(agent, request)
        with pytest.raises(IntegrityError), transaction.atomic():
            ComplaintReview.objects.filter(request=request).update(
                reviewed_by=agent, reviewed_at=request.created_at
            )

    def test_review_page_refuses_with_the_reason(self, client, agent, complaint_setup):
        request, first_lead, second_lead = complaint_setup
        decide(agent, request)
        client.force_login(first_lead)
        assert client.get(reverse("desk:complaint-review", args=[request.pk])).status_code == 200
        client.force_login(agent)  # the decision author, not a supervisor
        assert client.get(reverse("desk:complaint-review", args=[request.pk])).status_code == 403

    def test_awaiting_my_review_agrees_with_the_rule(self, agent, complaint_setup, client):
        request, first_lead, second_lead = complaint_setup
        request.refresh_from_db()
        # Not decided yet: nobody's queue.
        params = {"view": "complaints-review", "complaint": "review"}
        assert request not in view_queryset(first_lead, params)
        services.assign(
            actor=first_lead,
            request=request,
            expected_revision=request.revision,
            team=request.team,
            assignee=None,
        )
        decide(first_lead, request)
        assert request not in view_queryset(first_lead, params)  # decided it
        assert request in view_queryset(second_lead, params)
        assert request not in view_queryset(agent, params)  # former assignee
        client.force_login(second_lead)
        queues = {
            q["label"]: q["count"] for q in client.get(reverse("dashboard")).context["queues"]
        }
        assert queues["Complaints awaiting my review"] == 1
        client.force_login(agent)
        labels = [q["label"] for q in client.get(reverse("dashboard")).context["queues"]]
        assert "Complaints awaiting my review" not in labels  # not a supervisor

    def test_bulk_close_refuses_an_unreviewed_complaint(self, agent, category, complaint_setup):
        request, first_lead, _ = complaint_setup
        resolve(agent, decide(agent, request))
        plain = resolve(agent, new_request(agent, category))
        with pytest.raises(ValidationError, match=request.number):
            services.bulk_close(actor=agent, selected=selection([plain.pk, request.pk]))
        plain.refresh_from_db()
        assert plain.status == Status.RESOLVED  # all or nothing

    def test_unmark_needs_a_supervisor(self, agent, complaint_setup):
        request, first_lead, _ = complaint_setup
        with pytest.raises(PermissionDenied):
            services.set_complaint(
                actor=agent,
                request=request,
                expected_revision=request.revision,
                is_complaint=False,
                reason="Not a complaint",
            )
        services.set_complaint(
            actor=first_lead,
            request=request,
            expected_revision=request.revision,
            is_complaint=False,
            reason="Feedback, not a complaint",
        )

    def test_only_assignee_or_supervisor_records(self, make_user, complaint_setup):
        request, *_ = complaint_setup
        with pytest.raises(PermissionDenied):
            decide(make_user("bystander", "client_services"), request)
