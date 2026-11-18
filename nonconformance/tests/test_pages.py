"""The module's pages: who reaches them, the module switch, one reading per page and save (a stale
form is refused with the input kept), restricted tickets, and the ticket page with the module on
and off. Synthetic data only."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import LabModule
from desk.models import ComplaintReview
from nonconformance import services
from nonconformance.models import Nonconformance, Status, TicketLink

pytestmark = pytest.mark.django_db


def fresh(obj):
    return type(obj).objects.get(pk=obj.pk)


def text(response):
    return response.content.decode()


def main(response):
    """The page's own content (the <main> element), without the help drawer and scripts."""
    page = text(response)
    return page[page.index("<main") : page.index("</main>")]


@pytest.fixture
def record(module_on, agent):
    return services.raise_nonconformance(
        actor=agent,
        title="Label printer misprints",
        description="Labels on ORD-TEST-000101 were unreadable.",
        source="internal",
        severity="minor",
        area="Accessioning",
    )


@pytest.fixture
def owned(record, lead, agent):
    return services.set_owner(actor=lead, nc=record, expected_revision=record.revision, owner=agent)


def record_urls(nc):
    pk = nc.pk
    return [
        reverse("nonconformance:list"),
        reverse("nonconformance:action-list"),
        reverse("nonconformance:create"),
        reverse("nonconformance:detail", args=[pk]),
        reverse("nonconformance:status", args=[pk]),
        reverse("nonconformance:investigation", args=[pk]),
        reverse("nonconformance:ticket-link", args=[pk]),
    ]


class TestAccess:
    def test_staff_roles_read_and_raise(self, client, record, tech):
        client.force_login(tech)
        for url in (
            reverse("nonconformance:list"),
            reverse("nonconformance:action-list"),
            reverse("nonconformance:create"),
            reverse("nonconformance:detail", args=[record.pk]),
        ):
            assert client.get(url).status_code == 200, url

    @pytest.mark.parametrize("who", ["no_role", "portal"])
    def test_accounts_without_a_staff_role_are_refused(self, client, record, request, who):
        client.force_login(request.getfixturevalue(who))
        for url in record_urls(record):
            assert client.get(url).status_code == 403, url
        assert client.post(reverse("nonconformance:create"), {"title": "x"}).status_code == 403
        assert Nonconformance.objects.count() == 1

    def test_signed_out_visitors_go_to_sign_in(self, client, record):
        response = client.get(reverse("nonconformance:detail", args=[record.pk]))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("account_login"))

    def test_module_off_answers_404_everywhere(self, client, record, lead, complaint):
        LabModule.objects.filter(key="nonconformance").update(is_enabled=False)
        client.force_login(lead)
        for url in [
            *record_urls(record),
            reverse("nonconformance:from-ticket", args=[complaint.pk]),
        ]:
            assert client.get(url).status_code == 404, url
        posted = client.post(
            reverse("nonconformance:investigation", args=[record.pk]),
            {"expected_revision": record.revision, "containment": "Stopped", "root_cause": ""},
        )
        assert posted.status_code == 404
        assert fresh(record).containment == ""

    def test_work_forms_are_for_the_owner_or_a_supervisor(self, client, owned, tech, agent, lead):
        status_url = reverse("nonconformance:status", args=[owned.pk])
        client.force_login(tech)
        assert client.get(status_url).status_code == 403
        assert client.get(reverse("nonconformance:close", args=[owned.pk])).status_code == 403
        page = main(client.get(reverse("nonconformance:detail", args=[owned.pk])))
        assert "Change status" not in page and "Add action" not in page
        assert "Its owner or a supervisor works this nonconformance." in page
        client.force_login(agent)  # the owner
        assert client.get(status_url).status_code == 200
        assert client.get(reverse("nonconformance:owner", args=[owned.pk])).status_code == 403
        client.force_login(lead)
        assert client.get(reverse("nonconformance:owner", args=[owned.pk])).status_code == 200

    def test_close_is_disabled_with_its_reason(self, client, owned, lead):
        client.force_login(lead)
        page = text(client.get(reverse("nonconformance:detail", args=[owned.pk])))
        assert "Not ready to close: Move it to Verification first." in page
        assert 'disabled aria-describedby="close-unavailable"' in page


class TestUnavailableInThisState:
    def test_forms_the_state_rules_out_return_to_the_record(self, client, owned, agent, lead):
        record_url = reverse("nonconformance:detail", args=[owned.pk])
        action = services.add_action(
            actor=agent,
            nc=owned,
            expected_revision=owned.revision,
            description="Replace the print head.",
            assignee=agent,
            due_on=timezone.localdate(),
        )
        owned = fresh(owned)
        services.finish_action(
            actor=agent,
            nc=owned,
            expected_revision=owned.revision,
            action=action,
            done=True,
            note="Replaced.",
        )
        client.force_login(lead)
        for name, args, reason in (
            ("reopen", [owned.pk], "This nonconformance is not closed."),
            ("close", [owned.pk], "Move it to Verification first."),
            ("effectiveness", [owned.pk], "Move it to Verification"),
            ("action-done", [owned.pk, action.pk], "This action is already finished."),
            ("action-edit", [owned.pk, action.pk], "This action is already finished."),
            ("action-cancel", [owned.pk, action.pk], "This action is already finished."),
        ):
            response = client.get(reverse(f"nonconformance:{name}", args=args), follow=True)
            assert response.redirect_chain[-1][0] == record_url, name
            assert reason in text(response), name


class TestOneReading:
    def test_stale_form_is_refused_and_the_input_kept(self, client, owned, agent, lead):
        client.force_login(agent)
        url = reverse("nonconformance:investigation", args=[owned.pk])
        page = text(client.get(url))
        assert f'name="expected_revision" value="{owned.revision}"' in page
        # Someone else changes the record after the form was drawn.
        services.change_status(
            actor=lead, nc=owned, expected_revision=owned.revision, status=Status.INVESTIGATING
        )
        response = client.post(
            url,
            {
                "expected_revision": owned.revision,
                "containment": "Printer taken out of use.",
                "root_cause": "",
                "similar_cases": "",
            },
        )
        assert response.status_code == 200
        assert services.STALE in text(response)
        assert "Printer taken out of use." in text(response)
        assert f'name="expected_revision" value="{owned.revision}"' in text(response)
        assert fresh(owned).containment == ""

    def test_a_current_form_saves_with_a_fixed_message(self, client, owned, agent):
        client.force_login(agent)
        response = client.post(
            reverse("nonconformance:investigation", args=[owned.pk]),
            {
                "expected_revision": owned.revision,
                "containment": "Printer taken out of use.",
                "root_cause": "Worn print head.",
                "similar_cases": "Second printer checked: fine.",
            },
            follow=True,
        )
        assert "Investigation saved." in text(response)
        saved = fresh(owned)
        assert saved.similar_cases == "Second printer checked: fine."
        assert "Second printer checked: fine." in text(response)

    def test_a_service_refusal_shows_on_the_form(self, client, owned, agent):
        client.force_login(agent)
        response = client.post(
            reverse("nonconformance:action-add", args=[owned.pk]),
            {
                "expected_revision": owned.revision,
                "description": "Replace the print head.",
                "assignee": agent.pk,
                "due_on": (timezone.localdate() - timedelta(days=1)).isoformat(),
            },
        )
        assert response.status_code == 200
        assert "Choose today or a later date." in text(response)
        assert "Replace the print head." in text(response)
        assert not owned.actions.exists()


class TestTickets:
    def test_restricted_ticket_is_neither_shown_nor_linkable(
        self, client, owned, agent, lead, restricted_ticket
    ):
        services.link_ticket(
            actor=lead, nc=owned, expected_revision=owned.revision, request=restricted_ticket
        )
        owned = fresh(owned)
        client.force_login(lead)
        assert restricted_ticket.number in text(
            client.get(reverse("nonconformance:detail", args=[owned.pk]))
        )
        client.force_login(agent)  # the owner, not a member of the restricted team
        page = text(client.get(reverse("nonconformance:detail", args=[owned.pk])))
        assert restricted_ticket.number not in page and "Privacy matter" not in page
        link = TicketLink.objects.get(request=restricted_ticket)
        remove = reverse("nonconformance:ticket-unlink", args=[owned.pk, link.pk])
        assert client.get(remove).status_code == 404
        TicketLink.objects.filter(pk=link.pk).delete()
        response = client.post(
            reverse("nonconformance:ticket-link", args=[owned.pk]),
            {"expected_revision": owned.revision, "ticket": restricted_ticket.number},
        )
        assert "No ticket with that number you can open." in text(response)
        assert not TicketLink.objects.filter(request=restricted_ticket).exists()
        unknown = client.post(
            reverse("nonconformance:ticket-link", args=[owned.pk]),
            {"expected_revision": owned.revision, "ticket": "REQ-999999"},
        )
        assert "No ticket with that number you can open." in text(unknown)

    def test_a_readable_ticket_links_by_number(self, client, owned, agent, plain_ticket):
        client.force_login(agent)
        digits = plain_ticket.number.removeprefix("REQ-").lstrip("0")
        response = client.post(
            reverse("nonconformance:ticket-link", args=[owned.pk]),
            {"expected_revision": owned.revision, "ticket": f"req-{digits}"},
            follow=True,
        )
        assert "Ticket linked." in text(response)
        assert TicketLink.objects.filter(nonconformance=owned, request=plain_ticket).exists()

    def test_restricted_complaint_cannot_be_opened_by_an_outsider(
        self, client, module_on, agent, restricted_ticket
    ):
        client.force_login(agent)
        url = reverse("nonconformance:from-ticket", args=[restricted_ticket.pk])
        assert client.get(url).status_code == 404


class TestTicketPage:
    def test_module_off_shows_nothing_of_it(self, client, agent, complaint):
        LabModule.objects.create(key="nonconformance", is_enabled=False)
        client.force_login(agent)
        response = client.get(reverse("desk:request-detail", args=[complaint.pk]))
        assert "onconformance" not in main(response)
        assert 'href="/quality/' not in text(response)
        form = main(client.get(reverse("desk:complaint-record", args=[complaint.pk])))
        assert 'name="corrective_action_reference"' in form

    def test_module_on_opens_one_from_the_complaint(self, client, module_on, agent, complaint):
        client.force_login(agent)
        ticket_url = reverse("desk:request-detail", args=[complaint.pk])
        open_url = reverse("nonconformance:from-ticket", args=[complaint.pk])
        assert open_url in text(client.get(ticket_url))
        record_form = reverse("desk:complaint-record", args=[complaint.pk])
        # Until a record is opened from it, the complaint can still take a free-text reference.
        assert 'name="corrective_action_reference"' in main(client.get(record_form))
        page = text(client.get(open_url))
        assert "Results late three times" not in page  # nothing is copied from the ticket
        response = client.post(
            open_url,
            {
                "expected_revision": fresh(complaint).revision,
                "title": "Late results",
                "description": "Results reached the clinic late.",
                "severity": "major",
                "area": "",
                "containment": "",
            },
            follow=True,
        )
        nc = Nonconformance.objects.get(source_request=complaint)
        assert f"Nonconformance {nc.number} opened." in text(response)
        ticket_page = text(client.get(ticket_url))
        assert nc.number in ticket_page and open_url not in ticket_page
        assert client.get(open_url).status_code == 302  # one per complaint
        # Its number takes the reference's place.
        assert 'name="corrective_action_reference"' not in main(client.get(record_form))

    def test_a_stale_ticket_revision_is_refused(self, client, module_on, agent, complaint):
        client.force_login(agent)
        response = client.post(
            reverse("nonconformance:from-ticket", args=[complaint.pk]),
            {
                "expected_revision": complaint.revision - 1,
                "title": "Late results",
                "description": "Results reached the clinic late.",
                "severity": "major",
            },
        )
        assert response.status_code == 200
        assert "This ticket changed." in text(response)
        assert "Late results" in text(response)
        assert not Nonconformance.objects.exists()

    def test_a_reviewed_complaint_offers_no_opening(
        self, client, module_on, agent, complaint, lead
    ):
        ComplaintReview.objects.create(
            request=complaint,
            decision="investigate",
            rationale="Synthetic.",
            decided_by=agent,
            decided_at=timezone.now(),
            reviewed_by=lead,
            reviewed_at=timezone.now(),
        )
        client.force_login(agent)
        page = text(client.get(reverse("desk:request-detail", args=[complaint.pk])))
        assert reverse("nonconformance:from-ticket", args=[complaint.pk]) not in page


class TestModuleSwitch:
    def test_admin_turns_it_on_with_a_reason(self, client, admin):
        client.force_login(admin)
        profile = text(client.get(reverse("lab-profile")))
        assert "Nonconformance and corrective action" in profile
        url = reverse("lab-module", args=["nonconformance"])
        page = text(client.get(url))
        assert 'name="enabled" value="1"' in page and 'name="expected_revision" value="0"' in page
        refused = client.post(url, {"expected_revision": 0, "enabled": "1", "reason": ""})
        assert refused.status_code == 200 and not LabModule.objects.exists()
        response = client.post(
            url, {"expected_revision": 0, "enabled": "1", "reason": "Quality manual v3."}
        )
        assert response.status_code == 302
        assert LabModule.objects.get(key="nonconformance").is_enabled

    def test_a_switch_in_between_is_refused_not_inverted(self, client, admin):
        LabModule.objects.create(key="nonconformance", is_enabled=True, revision=1)
        client.force_login(admin)
        url = reverse("lab-module", args=["nonconformance"])
        assert 'name="enabled" value="0"' in text(client.get(url))
        response = client.post(url, {"expected_revision": 0, "enabled": "1", "reason": "Old page."})
        assert response.status_code == 200
        assert "switched after you opened the page" in text(response)
        assert "Old page." in text(response)
        assert LabModule.objects.get(key="nonconformance").revision == 1

    def test_others_are_refused_and_unknown_modules_are_404(self, client, admin, lead):
        client.force_login(lead)
        url = reverse("lab-module", args=["nonconformance"])
        assert client.get(url).status_code == 403
        assert (
            client.post(url, {"expected_revision": 0, "enabled": "1", "reason": "x"}).status_code
            == 403
        )
        assert "Turn on" not in main(client.get(reverse("lab-profile")))
        client.force_login(admin)
        assert client.get(reverse("lab-module", args=["no-such-module"])).status_code == 404
