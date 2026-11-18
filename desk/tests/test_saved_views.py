"""D11 saved views: private by default, sharing with a team or everyone needs a supervisor, a
shared view never shows its reader more than staff_requests, filters are checked on save and on
use, only the owner changes a view, and the owner orders them."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import QueryDict
from django.urls import reverse

from core.models import AuditEvent
from desk import saved_views
from desk.models import SavedView, Status, ViewSharing
from teams.models import ensure_default_team

from .test_contract import new_request, set_status

pytestmark = pytest.mark.django_db


@pytest.fixture
def team(db):
    return ensure_default_team()


@pytest.fixture
def lead(make_user):
    return make_user("lead", "supervisor")


@pytest.fixture
def tech(make_user):
    return make_user("tech", "technical")


def make_view(actor, name="Waiting", params=None, **extra):
    values = {"name": name, "shared_with": "private", "params": params or {"status": "active"}}
    values.update(extra)
    return saved_views.create_view(actor=actor, **values)


def audit(action, view):
    return list(
        AuditEvent.objects.filter(action=action, object_id=str(view.pk)).order_by("occurred_at")
    )


def listed(browser, user):
    """The saved views the list page offers `user`, in order, as {name: url}."""
    browser.force_login(user)
    response = browser.get(reverse("desk:request-list"))
    return response, {entry["label"]: entry["url"] for entry in response.context["saved_views"]}


def subjects_at(browser, url):
    response = browser.get(url)
    assert response.status_code == 200
    return {req.subject for req in response.context["page_obj"]}


class TestCreate:
    def test_private_by_default_with_audit(self, agent):
        view = make_view(agent, "My waiting", {"status": "waiting_client", "priority": "high"})
        assert view.owner == agent
        assert view.shared_with == ViewSharing.PRIVATE and view.team is None
        assert view.filters == {"status": "waiting_client", "priority": "high"}
        (record,) = audit("saved_view.created", view)
        assert record.actor == agent and record.after["name"] == "My waiting"

    def test_sharing_needs_a_supervisor(self, agent, tech, lead, quality):
        for user in (agent, tech):
            with pytest.raises(PermissionDenied):
                make_view(user, "Everyone", shared_with="everyone")
            with pytest.raises(PermissionDenied):
                make_view(user, "Team", shared_with="team", team=quality)
        assert not SavedView.objects.exists()
        assert make_view(lead, "Everyone", shared_with="everyone").shared_with == "everyone"
        team_view = make_view(lead, "Quality", shared_with="team", team=quality)
        assert team_view.team == quality

    def test_a_team_view_names_its_team_and_others_have_none(self, lead, quality):
        with pytest.raises(ValidationError) as error:
            make_view(lead, "No team", shared_with="team")
        assert "team" in error.value.message_dict
        everyone = make_view(lead, "Everyone", shared_with="everyone", team=quality)
        assert everyone.team is None  # a team on a view for everyone is dropped
        with pytest.raises(ValidationError):
            make_view(lead, "Odd", shared_with="nobody")

    def test_unknown_filter_keys_are_dropped(self, agent):
        view = make_view(
            agent,
            "Clean",
            QueryDict(
                "status=active&bogus=1&page=3&per_page=50&sort=-priority&assignee=&priority=high"
            ),
            sort="-priority",
        )
        assert view.filters == {"status": "active", "priority": "high"}
        assert view.sort == "-priority"

    def test_sort_must_be_a_list_column(self, agent):
        assert make_view(agent, "A", sort="drop table").sort == ""
        assert make_view(agent, "B", sort="-updated_at").sort == "-updated_at"

    def test_the_search_text_is_kept(self, agent):
        assert make_view(agent, "Search", {"q": "ORD-TEST"}).filters == {"q": "ORD-TEST"}

    def test_the_system_view_is_stored_with_the_filters(self, agent):
        view = make_view(agent, "Mine", QueryDict("view=my-open&status=waiting_client"))
        assert view.filters == {"view": "my-open", "status": "waiting_client"}

    def test_invalid_filter_values_are_refused(self, agent):
        for params in ({"status": "bogus"}, {"priority": "asap"}, {"team": "999999"}):
            with pytest.raises(ValidationError, match="Correct the list's filters"):
                make_view(agent, "Bad", params)
        assert not SavedView.objects.exists()

    def test_names_are_unique_per_owner(self, agent, tech):
        make_view(agent, "Same name")
        with pytest.raises(ValidationError):
            make_view(agent, "Same name")
        with pytest.raises(ValidationError):
            make_view(agent, "  ")
        assert make_view(tech, "Same name").owner == tech  # another owner may reuse it

    def test_positions_follow_creation(self, agent):
        views = [make_view(agent, name) for name in ("One", "Two", "Three")]
        assert [v.position for v in views] == [1, 2, 3]

    def test_portal_account_cannot_save_a_view(self, clinic_portal):
        with pytest.raises(PermissionDenied):
            make_view(clinic_portal)
        with pytest.raises(PermissionDenied):
            saved_views.visible_views(clinic_portal)


class TestVisibility:
    def test_visible_views_are_own_everyone_and_member_teams_only(
        self, agent, tech, lead, make_user, quality, team, join
    ):
        join(tech, quality)
        other_lead = make_user("lead2", "supervisor")
        make_view(agent, "Agent private")
        make_view(tech, "Tech private")
        make_view(lead, "For everyone", shared_with="everyone")
        make_view(lead, "For quality", shared_with="team", team=quality)
        make_view(other_lead, "For the default team", shared_with="team", team=team)
        make_view(lead, "Lead private")

        def names(user):
            return set(saved_views.visible_views(user).values_list("name", flat=True))

        assert names(agent) == {"Agent private", "For everyone"}
        assert names(tech) == {"Tech private", "For everyone", "For quality"}
        join(agent, team)
        assert names(agent) == {"Agent private", "For everyone", "For the default team"}
        assert names(lead) == {"For everyone", "For quality", "Lead private"}

    def test_a_team_view_listed_once_for_a_member(self, tech, lead, quality, join):
        join(tech, quality)
        make_view(lead, "For quality", shared_with="team", team=quality)
        assert saved_views.visible_views(tech).count() == 1

    def test_the_list_offers_each_reader_their_views(
        self, client, agent, tech, lead, quality, join
    ):
        join(tech, quality)
        make_view(agent, "Agent private")
        make_view(lead, "For quality", shared_with="team", team=quality)
        _, agent_views = listed(client, agent)
        assert set(agent_views) == {"Agent private"}
        _, tech_views = listed(client, tech)
        assert set(tech_views) == {"For quality"}

    def test_a_shared_view_never_shows_a_reader_more_than_staff_requests(
        self, client, agent, tech, lead, make_user, join, privacy_category, category, quality
    ):
        restricted = new_request(agent, privacy_category, subject="restricted")
        ordinary = new_request(agent, category, subject="ordinary")
        make_view(lead, "Everything", {"status": "active"}, shared_with="everyone")
        # the supervisor sees both through it ...
        client.force_login(lead)
        _, lead_views = listed(client, lead)
        assert subjects_at(client, lead_views["Everything"]) == {"restricted", "ordinary"}
        # ... a technician outside the restricted team sees only what the list gives them
        _, tech_views = listed(client, tech)
        assert subjects_at(client, tech_views["Everything"]) == {"ordinary"}
        # ... and a member of that team sees it through the same view
        join(tech, quality)
        assert subjects_at(client, tech_views["Everything"]) == {"restricted", "ordinary"}
        assert restricted.pk and ordinary.pk

    def test_a_shared_view_applies_its_filters_to_the_reader(
        self, client, agent, lead, category, make_user
    ):
        waiting = set_status(new_request(agent, category, subject="waiting"), Status.WAITING_CLIENT)
        new_request(agent, category, subject="open")
        make_view(lead, "Waiting", {"status": "waiting_client"}, shared_with="everyone")
        _, views = listed(client, agent)
        assert subjects_at(client, views["Waiting"]) == {"waiting"}
        assert waiting.pk

    def test_stored_filters_are_checked_again_when_the_view_is_used(
        self, client, agent, lead, category
    ):
        new_request(agent, category, subject="visible")
        view = make_view(lead, "Tampered", shared_with="everyone")
        SavedView.objects.filter(pk=view.pk).update(filters={"status": "bogus"})
        _, views = listed(client, agent)
        assert (
            subjects_at(client, views["Tampered"]) == set()
        )  # an invalid filter hides, never shows
        SavedView.objects.filter(pk=view.pk).update(filters={"not_a_filter": "x"})
        _, views = listed(client, agent)
        assert subjects_at(client, views["Tampered"]) == {"visible"}


class TestUpdateDeleteMove:
    def test_owner_renames_and_shares_with_audit(self, lead, quality):
        view = make_view(lead, "Old name")
        saved_views.update_view(
            actor=lead,
            view=view,
            expected_revision=1,
            name="New name",
            shared_with="team",
            team=quality,
        )
        view.refresh_from_db()
        assert (view.name, view.shared_with, view.team, view.revision) == (
            "New name",
            "team",
            quality,
            2,
        )
        (record,) = audit("saved_view.updated", view)
        assert record.before["name"] == "Old name" and record.after["name"] == "New name"

    def test_only_the_owner_changes_a_view(self, agent, tech, lead):
        view = make_view(lead, "Shared", shared_with="everyone")
        with pytest.raises(PermissionDenied):
            saved_views.update_view(
                actor=agent,
                view=view,
                expected_revision=1,
                name="Mine now",
                shared_with="private",
            )
        with pytest.raises(PermissionDenied):
            saved_views.delete_view(actor=tech, view=view)
        with pytest.raises(PermissionDenied):
            saved_views.move_view(actor=agent, view=view, direction="up")
        view.refresh_from_db()
        assert (view.name, view.shared_with) == ("Shared", "everyone")

    def test_a_non_supervisor_cannot_share_their_own_view(self, agent, quality):
        view = make_view(agent, "Private")
        for sharing, team in (("everyone", None), ("team", quality)):
            with pytest.raises(PermissionDenied):
                saved_views.update_view(
                    actor=agent,
                    view=view,
                    expected_revision=1,
                    name="Private",
                    shared_with=sharing,
                    team=team,
                )

    def test_stale_edit_and_no_change_are_refused(self, agent):
        view = make_view(agent, "Name")
        with pytest.raises(ValidationError, match="Change the name"):
            saved_views.update_view(
                actor=agent, view=view, expected_revision=1, name="Name", shared_with="private"
            )
        saved_views.update_view(
            actor=agent, view=view, expected_revision=1, name="Renamed", shared_with="private"
        )
        with pytest.raises(ValidationError, match="changed"):
            saved_views.update_view(
                actor=agent, view=view, expected_revision=1, name="Again", shared_with="private"
            )

    def test_rename_cannot_collide_with_another_view_of_the_owner(self, agent):
        make_view(agent, "First")
        second = make_view(agent, "Second")
        with pytest.raises(ValidationError):
            saved_views.update_view(
                actor=agent,
                view=second,
                expected_revision=1,
                name="First",
                shared_with="private",
            )

    def test_delete_is_audited_and_removes_it_for_everyone(self, lead, agent):
        view = make_view(lead, "Doomed", shared_with="everyone")
        saved_views.delete_view(actor=lead, view=view)
        assert not SavedView.objects.filter(pk=view.pk).exists()
        (record,) = AuditEvent.objects.filter(action="saved_view.deleted")
        assert record.actor == lead and record.before["name"] == "Doomed"
        assert not saved_views.visible_views(agent).exists()

    def test_move_up_and_down_swaps_neighbours(self, agent):
        a, b, c = (make_view(agent, name) for name in ("A", "B", "C"))

        def order():
            return [v.name for v in SavedView.objects.filter(owner=agent)]

        assert order() == ["A", "B", "C"]
        saved_views.move_view(actor=agent, view=c, direction="up")
        assert order() == ["A", "C", "B"]
        saved_views.move_view(actor=agent, view=a, direction="down")
        assert order() == ["C", "A", "B"]
        assert len(audit("saved_view.moved", c)) == 1
        assert audit("saved_view.moved", a)[0].after == {"direction": "down"}
        assert b.pk

    def test_moving_past_either_end_changes_nothing(self, agent):
        a, _, c = (make_view(agent, name) for name in ("A", "B", "C"))
        saved_views.move_view(actor=agent, view=a, direction="up")
        saved_views.move_view(actor=agent, view=c, direction="down")
        assert [v.name for v in SavedView.objects.filter(owner=agent)] == ["A", "B", "C"]
        assert not AuditEvent.objects.filter(action="saved_view.moved").exists()

    def test_only_the_owners_own_views_are_reordered(self, agent, tech):
        a = make_view(agent, "A")
        make_view(agent, "B")
        make_view(tech, "T1")
        make_view(tech, "T2")
        saved_views.move_view(actor=agent, view=a, direction="down")
        assert [v.name for v in SavedView.objects.filter(owner=tech)] == ["T1", "T2"]


class TestPages:
    def test_save_view_page_stores_the_lists_filters(self, client, agent):
        client.force_login(agent)
        query = "view=my-open&status=waiting_client&sort=-priority&bogus=1&page=2"
        page = client.get(reverse("desk:view-create"), {"list": query})
        assert page.status_code == 200
        shown = {str(label).lower(): value for label, value in page.context["filters"]}
        assert shown["status"] == "Waiting on client" and shown["view"] == "My open"
        response = client.post(
            reverse("desk:view-create"),
            {"name": "My waiting", "shared_with": "private", "list": query},
        )
        view = SavedView.objects.get()
        assert view.filters == {"view": "my-open", "status": "waiting_client"}
        assert view.sort == "-priority"
        assert response.status_code == 302
        target = QueryDict(response["Location"].split("?", 1)[1])
        assert target["view"] == "my-open" and target["saved"] == str(view.pk)
        assert target["status"] == "waiting_client" and target["sort"] == "-priority"
        assert "bogus" not in target and "page" not in target

    def test_a_saved_view_reopens_on_its_system_view(self, client, agent, join, team, category):
        join(agent, team)
        mine = new_request(agent, category, subject="mine", assignee=agent)
        new_request(agent, category, subject="unassigned")
        client.force_login(agent)
        client.post(
            reverse("desk:view-create"),
            {"name": "Mine again", "shared_with": "private", "list": "view=my-open"},
        )
        _, views = listed(client, agent)
        assert subjects_at(client, views["Mine again"]) == {"mine"}
        everything = client.post(
            reverse("desk:view-create"),
            {"name": "Anything", "shared_with": "private", "list": "status=active"},
        )
        assert "view=all" in everything["Location"]  # no system view stored: all requests
        assert mine.pk

    def test_clear_search_restores_saved_filters_and_named_queue(self, client, agent):
        view = make_view(agent, "Urgent", QueryDict("view=all&priority=urgent"))
        client.force_login(agent)
        url = reverse("desk:request-list")
        result = client.get(url, {"view": "all", "saved": view.pk, "q": "missing"})
        assert result.context["clear_url"] == f"{url}?view=all&priority=urgent&saved={view.pk}"
        assert result.context["filter_form"]["saved"].value() == str(view.pk)
        assert not any(chip["label"] == "Saved" for chip in result.context["active_filters"])
        assert client.get(result.context["clear_url"]).context["refined"] is False
        result = client.get(url, {"view": "unassigned", "q": "missing"})
        assert result.context["clear_url"] == f"{url}?view=unassigned"

    def test_delete_confirmation_checks_the_revision_it_displayed(self, client, agent):
        view = make_view(agent, "Original")
        client.force_login(agent)
        edit = reverse("desk:view-edit", args=[view.pk])
        confirm = client.get(edit, {"confirm": "delete"})
        assert "Delete view Original?" in confirm.content.decode()
        client.post(
            edit, {"name": "New name", "shared_with": "private", "expected_revision": view.revision}
        )
        delete = reverse("desk:view-delete", args=[view.pk])
        assert client.post(delete)["Location"] == edit
        assert SavedView.objects.filter(pk=view.pk).exists()
        response = client.post(delete, {"expected_revision": view.revision})
        assert response["Location"] == edit
        assert SavedView.objects.get(pk=view.pk).name == "New name"

    def test_non_supervisor_is_only_offered_private(self, client, agent, quality):
        client.force_login(agent)
        response = client.post(
            reverse("desk:view-create"),
            {"name": "Mine", "shared_with": "everyone", "list": "status=active"},
        )
        assert response.status_code == 200
        assert "shared_with" in response.context["form"].errors
        assert not SavedView.objects.exists()

    def test_supervisor_shares_with_a_team(self, client, lead, agent, quality, join):
        join(agent, quality)
        client.force_login(lead)
        response = client.post(
            reverse("desk:view-create"),
            {
                "name": "Quality work",
                "shared_with": "team",
                "team": quality.pk,
                "list": "status=active",
            },
        )
        assert response.status_code == 302
        assert SavedView.objects.get().team == quality
        _, views = listed(client, agent)
        assert "Quality work" in views
        missing = client.post(
            reverse("desk:view-create"),
            {"name": "No team", "shared_with": "team", "list": "status=active"},
        )
        assert missing.status_code == 200 and not SavedView.objects.filter(name="No team")

    def test_duplicate_name_keeps_the_page(self, client, agent):
        make_view(agent, "Taken")
        client.force_login(agent)
        response = client.post(
            reverse("desk:view-create"),
            {"name": "Taken", "shared_with": "private", "list": "status=active"},
        )
        assert response.status_code == 200
        assert SavedView.objects.count() == 1

    def test_owner_edits_and_a_stale_edit_keeps_the_page(self, client, agent):
        view = make_view(agent, "Before")
        client.force_login(agent)
        url = reverse("desk:view-edit", args=[view.pk])
        assert client.get(url).status_code == 200
        response = client.post(
            url, {"name": "After", "shared_with": "private", "expected_revision": 1}
        )
        assert response.status_code == 302
        stale = client.post(
            url, {"name": "Later", "shared_with": "private", "expected_revision": 1}
        )
        assert stale.status_code == 200
        assert any("changed" in e for e in stale.context["form"].non_field_errors())
        view.refresh_from_db()
        assert view.name == "After"

    def test_other_staff_get_404_for_edit_delete_and_move(self, client, agent, lead, tech):
        view = make_view(lead, "Lead's view", shared_with="everyone")
        client.force_login(agent)  # sees it in the list, may not change it
        assert client.get(reverse("desk:view-edit", args=[view.pk])).status_code == 404
        data = {"name": "Mine", "shared_with": "private", "expected_revision": 1}
        assert client.post(reverse("desk:view-edit", args=[view.pk]), data).status_code == 404
        assert client.post(reverse("desk:view-delete", args=[view.pk])).status_code == 404
        move = reverse("desk:view-move", args=[view.pk])
        assert client.post(move, {"direction": "up"}).status_code == 404
        view.refresh_from_db()
        assert view.name == "Lead's view"

    def test_delete_and_move_through_the_pages(self, client, agent):
        first, second = make_view(agent, "First"), make_view(agent, "Second")
        client.force_login(agent)
        assert client.get(reverse("desk:view-delete", args=[first.pk])).status_code == 405
        move = reverse("desk:view-move", args=[second.pk])
        assert client.post(move, {"direction": "up"}).status_code == 302
        assert [v.name for v in SavedView.objects.filter(owner=agent)] == ["Second", "First"]
        response = client.post(
            reverse("desk:view-delete", args=[first.pk]),
            {"expected_revision": first.revision},
            follow=True,
        )
        assert response.status_code == 200
        assert [v.name for v in SavedView.objects.all()] == ["Second"]

    def test_move_redirect_stays_on_this_site(self, client, agent):
        view = make_view(agent, "Only")
        client.force_login(agent)
        response = client.post(
            reverse("desk:view-move", args=[view.pk]),
            {"direction": "up", "next": "https://elsewhere.example/"},
        )
        assert "elsewhere.example" not in response["Location"]

    def test_portal_account_gets_403_and_signed_out_visitors_are_sent_to_sign_in(
        self, client, agent, clinic_portal
    ):
        view = make_view(agent, "Mine")
        assert client.get(reverse("desk:view-create")).status_code == 302
        client.force_login(clinic_portal)
        assert client.get(reverse("desk:view-create")).status_code == 403
        assert client.get(reverse("desk:view-edit", args=[view.pk])).status_code == 403
        assert client.post(reverse("desk:view-delete", args=[view.pk])).status_code == 403
