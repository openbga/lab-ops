"""Category availability and parent ticket access at intake."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from desk import services
from desk.models import EntryKind, ServiceRequest, Status
from desk.tests.test_contract import new_request, set_status
from taxonomy.models import RequestCategory
from taxonomy.terms import drawn

pytestmark = pytest.mark.django_db


def retire_group(category):
    RequestCategory.objects.filter(pk=category.parent_id).update(is_active=False)


class TestInactiveGroup:
    def test_staff_intake_refuses_a_category_of_an_inactive_group(self, agent, category):
        stale = RequestCategory.objects.get(pk=category.pk)  # loaded while the group was active
        retire_group(category)
        with pytest.raises(ValidationError) as error:
            new_request(agent, stale)
        assert "category" in error.value.message_dict
        assert not ServiceRequest.objects.exists()

    def test_portal_intake_refuses_it(self, clinic_portal, category):
        retire_group(category)
        with pytest.raises(ValidationError) as error:
            services.create_request(
                actor=clinic_portal,
                category=category,
                subject="Question",
                first_entry_kind=EntryKind.REPLY,
                first_entry_body="Text",
                channel="portal",
            )
        assert "category" in error.value.message_dict

    def test_recategorizing_refuses_it(self, agent, category, sample_category):
        request = new_request(agent, category)
        retire_group(sample_category)
        with pytest.raises(ValidationError):
            services.update_properties(
                actor=agent,
                request=request,
                expected_revision=request.revision,
                priority=request.priority,
                category=sample_category,
            )

    def test_staff_form_refuses_it(self, client, agent, category, clinic):
        retire_group(category)
        client.force_login(agent)
        response = client.post(
            reverse("desk:request-create"),
            {
                "category": category.pk,
                "category_terms": drawn([category]),
                "channel": "phone",
                "subject": "Retired group",
                "first_entry_kind": "note",
                "body": "Text",
                "client": clinic.pk,
            },
        )
        assert response.status_code == 200 and not ServiceRequest.objects.exists()
        assert response.context["form"].errors


class TestStaffFollowUp:
    @pytest.fixture
    def closed_restricted(self, agent, privacy_category, clinic):
        parent = new_request(agent, privacy_category, client=clinic)
        return set_status(parent, Status.CLOSED)

    def test_outsider_cannot_follow_up_a_restricted_parent(
        self, make_user, category, clinic, closed_restricted
    ):
        outsider = make_user("follow_outsider", "client_services")
        with pytest.raises(PermissionDenied):
            new_request(outsider, category, client=clinic, follow_up_of=closed_restricted)
        assert not ServiceRequest.objects.filter(follow_up_of=closed_restricted).exists()

    def test_refusal_reveals_nothing_about_an_unreadable_parent(
        self, make_user, agent, privacy_category, category, clinic, water
    ):
        outsider = make_user("follow_outsider", "client_services")
        open_parent = new_request(agent, privacy_category, client=clinic)  # not closed
        for organization in (clinic, water):  # neither the status nor the organization shows
            with pytest.raises(PermissionDenied):
                new_request(outsider, category, client=organization, follow_up_of=open_parent)

    def test_team_member_and_supervisor_may(
        self, make_user, quality, join, category, clinic, closed_restricted
    ):
        member = make_user("follow_member", "technical")
        join(member, quality)
        assert new_request(member, category, client=clinic, follow_up_of=closed_restricted)
        lead = make_user("follow_lead", "supervisor")
        assert new_request(lead, category, client=clinic, follow_up_of=closed_restricted)

    def test_readable_parent_still_follows_the_other_rules(self, agent, category, clinic, water):
        parent = set_status(new_request(agent, category, client=clinic), Status.CLOSED)
        with pytest.raises(ValidationError):
            new_request(agent, category, client=water, follow_up_of=parent)
        assert new_request(agent, category, client=clinic, follow_up_of=parent).follow_up_of

    def test_staff_form_has_no_follow_up_path(
        self, client, agent, category, clinic, closed_restricted
    ):
        client.force_login(agent)
        response = client.post(
            reverse("desk:request-create"),
            {
                "category": category.pk,
                "category_terms": drawn([category]),
                "channel": "phone",
                "subject": "Posted follow-up",
                "first_entry_kind": "note",
                "body": "Text",
                "client": clinic.pk,
                "follow_up_of": closed_restricted.pk,
            },
        )
        assert response.status_code == 302
        created = ServiceRequest.objects.get(subject="Posted follow-up")
        assert created.follow_up_of is None
