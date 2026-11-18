"""The cases next to each round 1 review finding (.local/review-history/nonconformance-round-1.md):
the fix holds for every service, field, audience and form that shares the reported path, not only
the one the reviewer proved. Synthetic data only."""

from datetime import timedelta

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from core import modules
from core.models import LabModule
from desk import services as desk_services
from nonconformance import services
from nonconformance.models import Nonconformance, Status, TicketLink

pytestmark = pytest.mark.django_db


def fresh(obj):
    return type(obj).objects.get(pk=obj.pk)


@pytest.fixture
def owned(module_on, lead, agent):
    return services.raise_nonconformance(
        actor=lead,
        owner=agent,
        title="Example label review",
        description="Example batch.",
        source="internal",
        severity="minor",
    )


@pytest.fixture
def action(owned, tech, agent):
    item = services.add_action(
        actor=agent,
        nc=owned,
        expected_revision=owned.revision,
        description="Check the label",
        assignee=tech,
        due_on=timezone.localdate() + timedelta(days=3),
    )
    return item


def stale_account(user, state):
    """`user` as the request holds it, after the account changed in the database."""
    if state == "deactivated":
        User.objects.filter(pk=user.pk).update(is_active=False)
    else:
        user.groups.clear()
    return user  # the in-memory object still says active, with its old groups cached nowhere


# NC-01: every service decides who acts from the account as it stands, not the request's copy.

SERVICES = {
    "raise": lambda nc, agent, complaint, lead: services.raise_nonconformance(
        actor=agent, title="T", description="D", source="internal", severity="minor"
    ),
    "details": lambda nc, agent, complaint, lead: services.update_details(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        title="New title",
        description="D",
        source="internal",
        severity="minor",
    ),
    "investigation": lambda nc, agent, complaint, lead: services.record_investigation(
        actor=agent, nc=nc, expected_revision=nc.revision, containment="C", root_cause="R"
    ),
    "status": lambda nc, agent, complaint, lead: services.change_status(
        actor=agent, nc=nc, expected_revision=nc.revision, status=Status.INVESTIGATING
    ),
    "add_action": lambda nc, agent, complaint, lead: services.add_action(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        description="A",
        assignee=lead,
        due_on=timezone.localdate(),
    ),
    "link": lambda nc, agent, complaint, lead: services.link_ticket(
        actor=agent, nc=nc, expected_revision=nc.revision, request=complaint
    ),
    "open_from_complaint": lambda nc, agent, complaint, lead: services.open_from_complaint(
        actor=agent,
        request=complaint,
        expected_revision=complaint.revision,
        title="T",
        description="D",
        severity="minor",
    ),
}


@pytest.mark.parametrize("state", ["deactivated", "role_removed"])
@pytest.mark.parametrize("name", sorted(SERVICES))
def test_every_service_refuses_an_account_changed_since_the_request_began(
    owned, agent, lead, complaint, state, name
):
    before = (fresh(owned).revision, Nonconformance.objects.count(), TicketLink.objects.count())
    actor = stale_account(agent, state)
    with pytest.raises(PermissionDenied):
        SERVICES[name](fresh(owned), actor, fresh(complaint), lead)
    after = (fresh(owned).revision, Nonconformance.objects.count(), TicketLink.objects.count())
    assert after == before


@pytest.mark.parametrize("state", ["deactivated", "role_removed"])
def test_a_supervisor_changed_since_the_request_began_cannot_close_or_reopen(
    owned, agent, lead, state
):
    nc = services.change_status(
        actor=agent, nc=owned, expected_revision=owned.revision, status=Status.VERIFICATION
    )
    nc = services.record_impact(
        actor=agent, nc=nc, expected_revision=nc.revision, impact="none", impact_assessment="R"
    )
    nc = services.record_effectiveness(
        actor=agent, nc=nc, expected_revision=nc.revision, result="effective", note="Checked"
    )
    actor = stale_account(lead, state)
    with pytest.raises(PermissionDenied):
        services.close(actor=actor, nc=nc, expected_revision=nc.revision, note="Closing")
    assert fresh(nc).status == Status.VERIFICATION


def test_an_assignee_deactivated_since_the_request_began_cannot_finish_the_action(
    owned, action, tech
):
    nc = fresh(owned)
    actor = stale_account(tech, "deactivated")
    with pytest.raises(PermissionDenied):
        services.finish_action(
            actor=actor, nc=nc, expected_revision=nc.revision, action=action, done=True, note="N"
        )


# NC-02: the module switch.


@pytest.mark.parametrize("state", ["deactivated", "role_removed"])
def test_the_switch_refuses_an_administrator_changed_since_the_request_began(
    module_on, admin, state
):
    actor = stale_account(admin, state)
    with pytest.raises(PermissionDenied):
        modules.set_enabled(
            actor=actor, key="nonconformance", enabled=False, expected_revision=1, reason="Pause"
        )
    assert modules.is_enabled("nonconformance")


# NC-10: a group that is not one of the five roles does not make a staff member.


@pytest.mark.parametrize("field", ["owner", "assignee", "client_told_by"])
def test_a_custom_group_alone_does_not_qualify_a_person(owned, lead, agent, no_role, field):
    no_role.groups.add(Group.objects.create(name="Synthetic capability holders"))
    nc = fresh(owned)
    with pytest.raises(ValidationError) as refused:
        if field == "owner":
            services.set_owner(actor=lead, nc=nc, expected_revision=nc.revision, owner=no_role)
        elif field == "assignee":
            services.add_action(
                actor=lead,
                nc=nc,
                expected_revision=nc.revision,
                description="A",
                assignee=no_role,
                due_on=timezone.localdate(),
            )
        else:
            services.record_impact(
                actor=lead,
                nc=nc,
                expected_revision=nc.revision,
                impact="affected",
                impact_assessment="Reviewed",
                client_notice="told",
                client_told_on=timezone.localdate(),
                client_told_how="Phone",
                client_told_by=no_role,
            )
    assert field in refused.value.message_dict


# NC-04: the close form shows what is closed, from the reading whose revision it posts.


def test_the_close_form_shows_the_client_notice_actions_and_check_it_closes(
    client, owned, action, agent, lead2
):
    nc = fresh(owned)
    services.finish_action(
        actor=agent, nc=nc, expected_revision=nc.revision, action=action, done=True, note="Done"
    )
    nc = fresh(owned)
    nc = services.change_status(
        actor=agent, nc=nc, expected_revision=nc.revision, status=Status.VERIFICATION
    )
    nc = services.record_impact(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        impact="affected",
        impact_assessment="Reports RPT-TEST-000901 and 000902 affected",
        client_notice="told",
        client_told_on=timezone.localdate(),
        client_told_how="Phone call",
        client_told_by=agent,
    )
    nc = services.record_effectiveness(
        actor=agent, nc=nc, expected_revision=nc.revision, result="effective", note="Re-checked"
    )
    client.force_login(lead2)
    page = client.get(reverse("nonconformance:close", args=[nc.pk]))
    html = page.content.decode()
    for shown in (
        "Reported results affected",
        "Reports RPT-TEST-000901 and 000902 affected",
        "Client told",
        "Phone call",
        "1 done, 0 cancelled, 0 open",
        "Effective",
        "Re-checked",
    ):
        assert shown in html, shown
    assert f'name="expected_revision" value="{nc.revision}"' in html


# NC-05: the remove form in the real case, a ticket gone out of reach before the form is drawn.


def test_removing_a_link_to_a_ticket_out_of_reach_shows_nothing_of_it(
    client, owned, agent, lead, plain_ticket, restricted_category
):
    link = services.link_ticket(
        actor=lead, nc=owned, expected_revision=owned.revision, request=plain_ticket
    )
    type(plain_ticket).objects.filter(pk=plain_ticket.pk).update(category=restricted_category)
    client.force_login(agent)
    url = reverse("nonconformance:ticket-unlink", args=[owned.pk, link.pk])
    for response in (
        client.get(url),
        client.post(url, {"expected_revision": fresh(owned).revision, "reason": "Example"}),
    ):
        assert response.status_code == 404
        assert plain_ticket.subject.encode() not in response.content
        assert plain_ticket.number.encode() not in response.content
    assert fresh(link).removed_at is None


# NC-06: link history follows who may open the ticket, both ways.


def test_a_linked_restricted_ticket_is_in_the_history_of_those_who_may_open_it_only(
    client, owned, agent, lead, restricted_ticket
):
    services.link_ticket(
        actor=lead, nc=owned, expected_revision=owned.revision, request=restricted_ticket
    )
    url = reverse("nonconformance:detail", args=[owned.pk])
    client.force_login(lead)
    titles = [item["title"] for item in client.get(url).context["history"]]
    assert "Ticket linked" in titles
    client.force_login(agent)
    titles = [item["title"] for item in client.get(url).context["history"]]
    assert "Ticket linked" not in titles and "Raised" in titles


# NC-09: an owner posted by someone who may not choose one is refused, never dropped.


def test_an_owner_posted_by_a_non_supervisor_is_refused_with_the_input_kept(
    client, module_on, agent, tech
):
    client.force_login(agent)
    response = client.post(
        reverse("nonconformance:create"),
        {
            "owner": tech.pk,
            "title": "Example observation",
            "description": "Example batch",
            "source": "internal",
            "severity": "minor",
        },
    )
    assert response.status_code == 200
    assert b"A supervisor chooses the owner." in response.content
    assert b"Example observation" in response.content
    assert not Nonconformance.objects.exists()


# NC-13, NC-14: a stale form keeps its draft; a current one from someone who may not act is 403.


def test_opening_from_a_complaint_no_longer_assigned_keeps_the_draft_when_stale(
    client, module_on, complaint, agent, lead
):
    client.force_login(agent)
    url = reverse("nonconformance:from-ticket", args=[complaint.pk])
    drawn = complaint.revision
    desk_services.assign(
        actor=lead,
        request=complaint,
        expected_revision=complaint.revision,
        team=complaint.team,
        assignee=None,
        reason="Back to the queue",
    )
    data = {"title": "Draft title", "description": "Draft text", "severity": "minor"}
    stale = client.post(url, {**data, "expected_revision": drawn})
    assert stale.status_code == 200 and b"Draft text" in stale.content
    assert b"This ticket changed." in stale.content
    current = client.post(url, {**data, "expected_revision": fresh(complaint).revision})
    assert current.status_code == 403
    assert not Nonconformance.objects.exists()


@pytest.mark.parametrize("name", ["status", "investigation", "impact", "action-add", "ticket-link"])
def test_a_current_form_from_someone_who_may_not_work_the_record_is_refused(
    client, owned, tech, name
):
    client.force_login(tech)
    url = reverse(f"nonconformance:{name}", args=[owned.pk])
    assert client.get(url).status_code == 403
    assert client.post(url, {"expected_revision": fresh(owned).revision}).status_code == 403
    assert fresh(owned).revision == owned.revision


# NC-15: the status form.


def test_posting_the_status_the_record_already_has_says_so(client, owned, agent):
    client.force_login(agent)
    response = client.post(
        reverse("nonconformance:status", args=[owned.pk]),
        {"expected_revision": owned.revision, "status": Status.OPEN},
    )
    assert response.status_code == 200
    assert b"It already has this status." in response.content


# NC-16: with the module off, nothing of it exists for anyone.


def test_with_the_module_off_every_module_address_is_not_found_before_sign_in(client, owned):
    LabModule.objects.update(is_enabled=False)
    for name, args in (
        ("list", ()),
        ("summary", ()),
        ("detail", (owned.pk,)),
        ("close", (owned.pk,)),
    ):
        assert client.get(reverse(f"nonconformance:{name}", args=args)).status_code == 404


# NC-19: forms that show an action or a link read it with the record in one statement.


def test_action_and_link_forms_post_the_revision_of_the_record_they_show(
    client, owned, action, agent, lead, plain_ticket
):
    link = services.link_ticket(
        actor=lead, nc=fresh(owned), expected_revision=fresh(owned).revision, request=plain_ticket
    )
    nc = fresh(owned)
    client.force_login(agent)
    for url in (
        reverse("nonconformance:action-done", args=[nc.pk, action.pk]),
        reverse("nonconformance:action-cancel", args=[nc.pk, action.pk]),
        reverse("nonconformance:action-edit", args=[nc.pk, action.pk]),
        reverse("nonconformance:ticket-unlink", args=[nc.pk, link.pk]),
    ):
        page = client.get(url)
        assert f'name="expected_revision" value="{nc.revision}"' in page.content.decode(), url


def test_an_action_of_another_record_is_not_found(client, owned, action, agent, lead):
    other = services.raise_nonconformance(
        actor=lead, owner=agent, title="Other", description="D", source="other", severity="minor"
    )
    client.force_login(agent)
    url = reverse("nonconformance:action-edit", args=[other.pk, action.pk])
    assert client.get(url).status_code == 404


# NC-21: every complaint record control follows whether the review still counts.


def test_record_controls_return_when_the_review_no_longer_counts(
    client, module_on, complaint, agent, lead
):
    desk_services.decide_complaint(
        actor=agent,
        request=complaint,
        expected_revision=complaint.revision,
        decision="investigate",
        rationale="Example",
    )
    ticket = fresh(complaint)
    desk_services.review_complaint(
        actor=lead, request=ticket, expected_revision=ticket.revision, note="Reviewed"
    )
    url = reverse("desk:request-detail", args=[complaint.pk])
    client.force_login(agent)
    page = client.get(url).content.decode()
    assert reverse("desk:complaint-record", args=[complaint.pk]) not in page
    from teams.models import TeamMembership

    TeamMembership.objects.get_or_create(team=ticket.team, user=lead)
    ticket = fresh(complaint)
    desk_services.assign(
        actor=agent,
        request=ticket,
        expected_revision=ticket.revision,
        team=ticket.team,
        assignee=lead,
        reason="Reviewer takes it over",
    )
    client.force_login(lead)
    page = client.get(url).content.decode()
    for name in ("desk:complaint-decision", "desk:complaint-record"):
        assert reverse(name, args=[complaint.pk]) in page, name
    assert reverse("nonconformance:from-ticket", args=[complaint.pk]) in page


# NC-22: work changed after the effectiveness check.


@pytest.fixture
def checked(owned, agent):
    nc = services.change_status(
        actor=agent, nc=fresh(owned), expected_revision=owned.revision, status="verification"
    )
    nc = services.record_impact(
        actor=agent, nc=nc, expected_revision=nc.revision, impact="none", impact_assessment="R"
    )
    return services.record_effectiveness(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        result="no_action_needed",
        note="Nothing to do",
    )


def test_changing_an_action_clears_the_effectiveness_check(checked, agent, tech):
    action = services.add_action(
        actor=agent,
        nc=checked,
        expected_revision=checked.revision,
        description="A",
        assignee=tech,
        due_on=timezone.localdate(),
    )
    nc = fresh(checked)
    assert nc.effectiveness == ""
    nc = services.record_effectiveness(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        result="no_action_needed",
        note="Again",
    )
    services.update_action(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        action=action,
        description="A, widened",
        assignee=tech,
        due_on=action.due_on,
        reason="Widened",
    )
    assert fresh(checked).effectiveness == ""
    from core.models import AuditEvent

    cleared = AuditEvent.objects.filter(action="nonconformance.effectiveness_cleared")
    assert cleared.count() == 2
    assert all(event.before["effectiveness"] == "no_action_needed" for event in cleared)


def test_finishing_or_cancelling_an_action_keeps_the_check(owned, agent, tech):
    nc = fresh(owned)
    first = services.add_action(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        description="A",
        assignee=tech,
        due_on=timezone.localdate(),
    )
    nc = fresh(owned)
    second = services.add_action(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        description="B",
        assignee=tech,
        due_on=timezone.localdate(),
    )
    nc = services.change_status(
        actor=agent, nc=fresh(owned), expected_revision=fresh(owned).revision, status="verification"
    )
    nc = services.record_effectiveness(
        actor=agent, nc=nc, expected_revision=nc.revision, result="no_action_needed", note="N"
    )
    services.finish_action(
        actor=tech, nc=nc, expected_revision=nc.revision, action=first, done=True, note="Done"
    )
    nc = fresh(owned)
    services.finish_action(
        actor=agent, nc=nc, expected_revision=nc.revision, action=second, done=False, note="No"
    )
    assert fresh(owned).effectiveness == "no_action_needed"


# NC-11: never stored, on every page of the module and the desk's action forms.


@pytest.mark.parametrize(
    "name", ["list", "action-list", "summary", "create", "detail", "edit", "status", "impact"]
)
def test_module_pages_are_never_stored(client, owned, agent, name):
    client.force_login(agent)
    args = (owned.pk,) if name in {"detail", "edit", "status", "impact"} else ()
    response = client.get(reverse(f"nonconformance:{name}", args=args))
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]


def test_desk_action_forms_are_never_stored(client, module_on, complaint, agent):
    client.force_login(agent)
    for name in ("desk:complaint-decision", "desk:complaint-record", "desk:request-assign"):
        response = client.get(reverse(name, args=[complaint.pk]))
        assert response.status_code == 200 and "no-store" in response["Cache-Control"], name
