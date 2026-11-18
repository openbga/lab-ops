"""D13 teams: exactly one default team that stays active, membership for staff accounts only,
the lead flag, who may create, change and staff a team (service and page), and the audit events."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from core.models import AuditEvent
from desk import services as desk_services
from desk.forms import assign_choices
from desk.models import EntryKind, ServiceRequest
from teams import services
from teams.models import Team, TeamMembership

pytestmark = pytest.mark.django_db


def audit(action, team):
    return list(
        AuditEvent.objects.filter(action=action, object_id=str(team.pk)).order_by("occurred_at")
    )


def defaults():
    return list(Team.objects.filter(is_default=True))


def update(actor, team, reason="Reorganised", **data):
    return services.update_team(
        actor=actor, team_id=team.pk, expected_revision=team.revision, reason=reason, **data
    )


class TestCreateTeam:
    def test_admin_creates_with_audit(self, admin):
        team = services.create_team(actor=admin, name="  Lab operations ", description="Benches")
        assert (team.name, team.slug, team.is_active, team.is_default) == (
            "Lab operations",
            "lab-operations",
            True,
            False,
        )
        (record,) = audit("team.created", team)
        assert record.actor == admin and record.after["name"] == "Lab operations"

    @pytest.mark.parametrize(
        "role", ["supervisor", "client_services", "account_manager", "technical"]
    )
    def test_only_admin_creates(self, make_user, role):
        with pytest.raises(PermissionDenied):
            services.create_team(actor=make_user(f"u_{role}", role), name="Nope")
        assert not Team.objects.filter(name="Nope").exists()

    def test_portal_account_cannot_create(self, portal_account):
        with pytest.raises(PermissionDenied):
            services.create_team(actor=portal_account, name="Nope")

    def test_names_are_unique_ignoring_case_and_slugs_are_made_unique(self, admin):
        services.create_team(actor=admin, name="Lab ops")
        with pytest.raises(ValidationError):
            services.create_team(actor=admin, name="LAB OPS")
        other = services.create_team(actor=admin, name="Lab-ops")  # same slug, other name
        assert other.slug == "lab-ops-2"

    def test_a_name_is_required(self, admin):
        with pytest.raises(ValidationError):
            services.create_team(actor=admin, name="   ")

    def test_new_default_team_moves_the_default(self, admin, default_team):
        team = services.create_team(actor=admin, name="Front desk", is_default=True)
        default_team.refresh_from_db()
        assert defaults() == [team]
        assert not default_team.is_default


class TestUpdateTeam:
    def test_admin_changes_with_reason_and_audit(self, admin, quality):
        update(admin, quality, name="Quality assurance", description="QA")
        quality.refresh_from_db()
        assert (quality.name, quality.revision) == ("Quality assurance", 2)
        (record,) = audit("team.updated", quality)
        assert record.reason == "Reorganised"
        assert record.before["name"] == "Quality" and record.after["name"] == "Quality assurance"
        assert quality.slug == "quality"  # the stable key never follows a rename

    @pytest.mark.parametrize("role", ["supervisor", "client_services", "technical"])
    def test_only_admin_changes(self, make_user, quality, role):
        with pytest.raises(PermissionDenied):
            update(make_user(f"u_{role}", role), quality, name="Renamed")
        quality.refresh_from_db()
        assert quality.name == "Quality"

    def test_a_reason_is_required(self, admin, quality):
        for blank in ("", "  "):
            with pytest.raises(ValidationError) as error:
                update(admin, quality, reason=blank, name="Renamed")
            assert "reason" in error.value.message_dict

    def test_stale_edit_and_no_change_are_refused(self, admin, quality):
        stale = quality.revision
        update(admin, quality, name="Renamed")
        with pytest.raises(ValidationError, match="changed"):
            services.update_team(
                actor=admin, team_id=quality.pk, expected_revision=stale, reason="x", name="Again"
            )
        quality.refresh_from_db()
        with pytest.raises(ValidationError, match="Change at least one"):
            update(admin, quality, name="Renamed")

    def test_the_default_cannot_be_unset(self, admin, default_team):
        with pytest.raises(ValidationError) as error:
            update(admin, default_team, is_default=False)
        assert "is_default" in error.value.message_dict
        assert defaults() == [default_team]

    def test_the_default_must_stay_active(self, admin, default_team):
        with pytest.raises(ValidationError) as error:
            update(admin, default_team, is_active=False)
        assert "is_active" in error.value.message_dict
        default_team.refresh_from_db()
        assert default_team.is_active

    def test_making_another_team_the_default_moves_it(self, admin, default_team, quality):
        update(admin, quality, is_default=True)
        quality.refresh_from_db()
        default_team.refresh_from_db()
        assert defaults() == [quality]
        assert not default_team.is_default
        assert audit("team.updated", quality)[0].after["is_default"] is True

    def test_a_team_can_be_deactivated_once_it_is_not_the_default(
        self, admin, default_team, quality
    ):
        update(admin, quality, is_active=False)
        quality.refresh_from_db()
        assert not quality.is_active
        update(admin, quality, is_active=True, reason="Back in use")
        quality.refresh_from_db()
        assert quality.is_active

    def test_the_database_keeps_exactly_one_active_default(self, default_team, quality):
        assert defaults() == [default_team]
        with pytest.raises(IntegrityError), transaction.atomic():
            Team.objects.filter(pk=quality.pk).update(is_default=True)  # a second default
        with pytest.raises(IntegrityError), transaction.atomic():
            Team.objects.filter(pk=default_team.pk).update(is_active=False)  # an inactive default
        assert defaults() == [default_team]

    def test_deactivated_team_is_not_offered_and_takes_no_new_requests(
        self, admin, agent, quality, category
    ):
        update(admin, quality, is_active=False)
        quality.refresh_from_db()
        offered = [label for label, _ in assign_choices()]
        assert "Quality" not in offered
        with pytest.raises(ValidationError) as error:
            desk_services.create_request(
                actor=agent,
                category=category,
                subject="Late",
                first_entry_kind=EntryKind.NOTE,
                first_entry_body="Text",
                channel="phone",
                team=quality,
            )
        assert "team" in error.value.message_dict


class TestMembership:
    @pytest.mark.parametrize("actor_name", ["admin", "supervisor"])
    def test_admins_and_supervisors_add_members(self, request, actor_name, agent, quality):
        actor = request.getfixturevalue(actor_name)
        membership = services.add_member(actor=actor, team=quality, user=agent, is_lead=True)
        assert membership.is_lead
        (record,) = audit("team.member_added", quality)
        assert record.actor == actor
        assert record.after == {"user_id": agent.pk, "is_lead": True}

    @pytest.mark.parametrize("role", ["client_services", "account_manager", "technical"])
    def test_other_roles_cannot_manage_members(self, make_user, agent, quality, join, role):
        actor = make_user(f"u_{role}", role)
        member = join(agent, quality)
        with pytest.raises(PermissionDenied):
            services.add_member(actor=actor, team=quality, user=actor)
        with pytest.raises(PermissionDenied):
            services.remove_member(actor=actor, membership=member)
        with pytest.raises(PermissionDenied):
            services.set_lead(actor=actor, membership=member, is_lead=True)
        assert TeamMembership.objects.count() == 1
        assert not AuditEvent.objects.filter(action__startswith="team.member").exists()

    def test_membership_is_for_staff_accounts_only(self, admin, make_user, portal_account, quality):
        with pytest.raises(ValidationError) as error:
            services.add_member(actor=admin, team=quality, user=portal_account)
        assert "user" in error.value.message_dict
        no_role = make_user("no_role")  # an account with no staff role
        with pytest.raises(ValidationError):
            services.add_member(actor=admin, team=quality, user=no_role)
        inactive = make_user("inactive_staff", "technical")
        inactive.is_active = False
        inactive.save()
        with pytest.raises(ValidationError):
            services.add_member(actor=admin, team=quality, user=inactive)
        assert not TeamMembership.objects.exists()

    def test_a_person_joins_a_team_once(self, admin, agent, quality):
        services.add_member(actor=admin, team=quality, user=agent)
        with pytest.raises(ValidationError, match="already belongs"):
            services.add_member(actor=admin, team=quality, user=agent)
        assert len(audit("team.member_added", quality)) == 1

    def test_one_person_may_belong_to_several_teams(self, admin, agent, quality, default_team):
        services.add_member(actor=admin, team=quality, user=agent)
        services.add_member(actor=admin, team=default_team, user=agent)
        assert agent.team_memberships.count() == 2

    def test_lead_flag_toggles_with_audit(self, supervisor, agent, quality, join):
        membership = join(agent, quality)
        services.set_lead(actor=supervisor, membership=membership, is_lead=True)
        membership.refresh_from_db()
        assert membership.is_lead
        with pytest.raises(ValidationError, match="Nothing to change"):
            services.set_lead(actor=supervisor, membership=membership, is_lead=True)
        services.set_lead(actor=supervisor, membership=membership, is_lead=False)
        membership.refresh_from_db()
        assert not membership.is_lead
        records = audit("team.lead_changed", quality)
        assert [r.after["is_lead"] for r in records] == [True, False]

    def test_removing_a_member_is_audited_and_returns_their_requests_to_the_queue(
        self, supervisor, agent, quality, join, category
    ):
        membership = join(agent, quality, lead=True)
        request = desk_services.create_request(
            actor=agent,
            category=category,
            subject="Kept",
            first_entry_kind=EntryKind.NOTE,
            first_entry_body="Text",
            channel="phone",
            team=quality,
            assignee=agent,
        )
        services.remove_member(actor=supervisor, membership=membership, reason="Moved to billing")
        assert not TeamMembership.objects.exists()
        (record,) = audit("team.member_removed", quality)
        assert record.actor == supervisor and record.reason == "Moved to billing"
        assert record.before == {"user_id": agent.pk, "is_lead": True}
        assert record.after == {"requests_released": 1}
        # Their open requests in the team go back to its queue, with an event.
        released = ServiceRequest.objects.get(pk=request.pk)
        assert released.assignee is None and released.team == quality
        event = released.entries.filter(event_action="assigned").last()
        assert event.event_data["old_assignee_id"] == agent.pk
        assert event.event_data["assignee_id"] is None

    def test_a_removed_member_can_no_longer_be_assigned_requests(
        self, supervisor, agent, quality, join, category
    ):
        membership = join(agent, quality)
        services.remove_member(actor=supervisor, membership=membership)
        with pytest.raises(ValidationError):
            desk_services.create_request(
                actor=agent,
                category=category,
                subject="Not any more",
                first_entry_kind=EntryKind.NOTE,
                first_entry_body="Text",
                channel="phone",
                team=quality,
                assignee=agent,
            )


class TestPages:
    def login(self, client, user):
        client.force_login(user)
        return client

    def test_list_is_for_staff_with_correct_counts(
        self, client, agent, technician, quality, default_team, join, category
    ):
        join(agent, quality)
        join(technician, quality)
        for n in range(3):
            desk_services.create_request(
                actor=agent,
                category=category,
                subject=f"Queued {n}",
                first_entry_kind=EntryKind.NOTE,
                first_entry_body="Text",
                channel="phone",
                team=quality,
            )
        self.login(client, technician)
        response = client.get(reverse("teams:team-list"))
        assert response.status_code == 200
        counts = {t.name: (t.member_count, t.open_count) for t in response.context["teams"]}
        assert counts["Quality"] == (2, 3)  # joins do not multiply the counts
        assert response.context["can_manage_teams"] is False

    def test_list_and_detail_refuse_portal_accounts_and_visitors(
        self, client, portal_account, quality
    ):
        assert client.get(reverse("teams:team-list")).status_code == 302
        self.login(client, portal_account)
        assert client.get(reverse("teams:team-list")).status_code == 403
        assert client.get(reverse("teams:team-detail", args=[quality.pk])).status_code == 403

    def test_detail_shows_members_and_history(self, client, supervisor, agent, quality, join):
        join(agent, quality, lead=True)
        services.add_member(actor=supervisor, team=quality, user=supervisor)
        self.login(client, supervisor)
        response = client.get(reverse("teams:team-detail", args=[quality.pk]))
        assert response.status_code == 200
        assert [m.user for m in response.context["members"]][0] == agent  # the lead first
        assert [e.action for e in response.context["history"]] == ["team.member_added"]
        assert response.context["can_manage_members"] and not response.context["can_manage_teams"]

    def test_admin_and_supervisor_add_members_others_get_403(
        self, client, admin, supervisor, agent, technician, quality
    ):
        url = reverse("teams:team-detail", args=[quality.pk])
        self.login(client, technician)
        page = client.get(url)
        assert page.status_code == 200 and page.context["member_form"] is None
        assert client.post(url, {"user": agent.pk}).status_code == 403
        assert not TeamMembership.objects.exists()
        for manager in (admin, supervisor):
            TeamMembership.objects.all().delete()
            self.login(client, manager)
            response = client.post(url, {"user": agent.pk, "is_lead": "on"})
            assert response.status_code == 302
            assert TeamMembership.objects.get().is_lead

    def test_adding_a_portal_account_through_the_page_is_refused(
        self, client, supervisor, portal_account, quality
    ):
        self.login(client, supervisor)
        response = client.post(
            reverse("teams:team-detail", args=[quality.pk]), {"user": portal_account.pk}
        )
        assert response.status_code == 200
        assert "user" in response.context["member_form"].errors
        assert not TeamMembership.objects.exists()

    def test_adding_the_same_person_twice_shows_the_error(
        self, client, supervisor, agent, quality, join
    ):
        join(agent, quality)
        self.login(client, supervisor)
        response = client.post(reverse("teams:team-detail", args=[quality.pk]), {"user": agent.pk})
        assert response.status_code == 200
        assert "user" in response.context["member_form"].errors

    def test_lead_and_remove_buttons(self, client, supervisor, technician, agent, quality, join):
        membership = join(agent, quality)
        lead_url = reverse("teams:member-lead", args=[quality.pk, membership.pk])
        remove_url = reverse("teams:member-remove", args=[quality.pk, membership.pk])
        self.login(client, technician)
        assert client.post(lead_url, {"is_lead": "1"}).status_code == 403
        assert client.post(remove_url).status_code == 403
        membership.refresh_from_db()
        assert not membership.is_lead
        self.login(client, supervisor)
        assert client.get(remove_url).status_code == 405
        assert client.post(lead_url, {"is_lead": "1"}).status_code == 302
        membership.refresh_from_db()
        assert membership.is_lead
        response = client.post(remove_url, follow=True)
        assert response.status_code == 200
        assert not TeamMembership.objects.exists()

    def test_a_membership_of_another_team_answers_404(
        self, client, supervisor, agent, quality, default_team, join
    ):
        membership = join(agent, quality)
        self.login(client, supervisor)
        url = reverse("teams:member-remove", args=[default_team.pk, membership.pk])
        assert client.post(url).status_code == 404
        assert TeamMembership.objects.count() == 1

    def test_only_admin_opens_create_and_edit(
        self, client, admin, supervisor, agent, quality, portal_account
    ):
        urls = (reverse("teams:team-create"), reverse("teams:team-edit", args=[quality.pk]))
        for user in (supervisor, agent, portal_account):
            self.login(client, user)
            for url in urls:
                assert client.get(url).status_code == 403, (user.username, url)
            assert client.post(urls[0], {"name": "Sneaky"}).status_code == 403
            data = {"name": "Renamed", "reason": "x", "expected_revision": 1, "is_active": "on"}
            assert client.post(urls[1], data).status_code == 403
        assert not Team.objects.filter(name__in=["Sneaky", "Renamed"]).exists()
        self.login(client, admin)
        for url in urls:
            assert client.get(url).status_code == 200

    def test_create_page_renders_its_fields_and_creates(self, client, admin):
        self.login(client, admin)
        page = client.get(reverse("teams:team-create")).content.decode()
        for field in ("name", "description", "is_default"):
            assert f'name="{field}"' in page, field
        response = client.post(
            reverse("teams:team-create"), {"name": "Sample handling", "description": "Bench 4"}
        )
        team = Team.objects.get(name="Sample handling")
        assert response.status_code == 302
        assert response["Location"] == reverse("teams:team-detail", args=[team.pk])
        assert audit("team.created", team)

    def test_create_page_refuses_a_duplicate_name(self, client, admin, quality):
        self.login(client, admin)
        response = client.post(reverse("teams:team-create"), {"name": "quality"})
        assert response.status_code == 200
        assert Team.objects.filter(name__iexact="quality").count() == 1

    def test_create_as_the_default_team_moves_the_default(self, client, admin, default_team):
        self.login(client, admin)
        response = client.post(
            reverse("teams:team-create"), {"name": "Front desk", "is_default": "on"}
        )
        assert response.status_code == 302
        assert [t.name for t in defaults()] == ["Front desk"]

    def test_edit_page_saves_with_a_reason_and_keeps_input_on_errors(self, client, admin, quality):
        self.login(client, admin)
        url = reverse("teams:team-edit", args=[quality.pk])
        page = client.get(url).content.decode()
        for field in ("name", "description", "is_active", "reason"):
            assert f'name="{field}"' in page, field
        base = {"name": "Quality assurance", "is_active": "on", "expected_revision": 1}
        missing = client.post(url, base)
        assert missing.status_code == 200 and "reason" in missing.context["form"].errors
        assert "Quality assurance" in missing.content.decode()  # input kept
        response = client.post(url, {**base, "reason": "Renamed by the lab director"})
        assert response.status_code == 302
        quality.refresh_from_db()
        assert quality.name == "Quality assurance"
        assert audit("team.updated", quality)[0].reason == "Renamed by the lab director"

    def test_stale_edit_keeps_the_page(self, client, admin, quality):
        update(admin, quality, name="Renamed elsewhere")
        self.login(client, admin)
        response = client.post(
            reverse("teams:team-edit", args=[quality.pk]),
            {"name": "My rename", "is_active": "on", "expected_revision": 1, "reason": "x"},
        )
        assert response.status_code == 200
        assert any("changed" in e for e in response.context["form"].non_field_errors())
        quality.refresh_from_db()
        assert quality.name == "Renamed elsewhere"

    def test_edit_page_refuses_unsetting_or_deactivating_the_default(
        self, client, admin, default_team
    ):
        self.login(client, admin)
        url = reverse("teams:team-edit", args=[default_team.pk])
        unset = client.post(
            url,
            {"name": default_team.name, "is_active": "on", "expected_revision": 1, "reason": "x"},
        )
        assert unset.status_code == 200 and "is_default" in unset.context["form"].errors
        deactivate = client.post(
            url,
            {
                "name": default_team.name,
                "is_default": "on",
                "expected_revision": 1,
                "reason": "x",
            },
        )
        assert deactivate.status_code == 200 and deactivate.context["form"].errors
        assert defaults() == [default_team]
        default_team.refresh_from_db()
        assert default_team.is_active

    def test_edit_page_makes_another_team_the_default(self, client, admin, default_team, quality):
        self.login(client, admin)
        response = client.post(
            reverse("teams:team-edit", args=[quality.pk]),
            {
                "name": "Quality",
                "is_default": "on",
                "is_active": "on",
                "expected_revision": 1,
                "reason": "Quality now answers first",
            },
        )
        assert response.status_code == 302
        assert defaults() == [Team.objects.get(pk=quality.pk)]

    def test_unknown_team_answers_404(self, client, admin):
        self.login(client, admin)
        assert client.get(reverse("teams:team-detail", args=[999999])).status_code == 404
        assert client.get(reverse("teams:team-edit", args=[999999])).status_code == 404
