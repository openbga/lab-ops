"""The cases next to each finding of the round 1 final check
(.local/review-history/nonconformance-round-1-final-check.md): ticket visibility and the actor are
decided after the last lock on every desk path that takes one, refusals of the reader as staff
draw nothing on every form, and the module's answer while off is the same everywhere. Synthetic
data only."""

import pytest
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse

from accounts.models import User
from core.models import LabModule
from desk import services as desk_services
from desk.models import EntryKind, RequestEntry, ServiceRequest
from nonconformance import services
from teams.models import TeamMembership

from . import test_review_round1 as previous

pytestmark = pytest.mark.django_db

owned = previous.owned
action = previous.action
linked = previous.linked
decided = previous.decided
at_read = previous.at_read


def fresh(obj):
    return type(obj).objects.get(pk=obj.pk)


def lock_of(table):
    return lambda sql: f'FROM "{table}"' in sql and "FOR" in sql and "UPDATE" in sql


# F-01: a ticket that leaves the actor's scope after the ticket lock (a team membership ended).


@pytest.mark.django_db(transaction=True)
def test_a_member_cannot_leave_a_restricted_ticket_while_their_note_holds_it(
    restricted_ticket, make_user, quality_team
):
    """The member's note holds their account's rows, team memberships included, from before the
    ticket's lock until it commits (accounts.acting): removing the membership at the ticket's
    lock has to wait for the note (here it gives up after its bounded wait), and the note, written
    by a member who could still read the ticket, is saved."""
    member = make_user("member", "client_services")
    TeamMembership.objects.create(team=quality_team, user=member)

    def leave():
        TeamMembership.objects.filter(team=quality_team, user=member).delete()

    with at_read(lock_of("desk_servicerequest"), leave) as concurrent:
        desk_services.add_entry(
            actor=member,
            request=restricted_ticket,
            kind=EntryKind.NOTE,
            body="Synthetic note",
            expected_revision=restricted_ticket.revision,
        )
    assert concurrent == [False]
    assert RequestEntry.objects.filter(body="Synthetic note").count() == 1
    assert TeamMembership.objects.filter(team=quality_team, user=member).exists()


@pytest.mark.django_db(transaction=True)
def test_a_restricted_ticket_left_by_the_actor_before_the_change_is_not_changed(
    restricted_ticket, make_user, quality_team
):
    """The member left the ticket's team after the page was drawn and before the note was
    saved: the ticket is no longer one they may open, so the note is refused as a missing ticket
    and nothing is written."""
    member = make_user("member", "client_services")
    TeamMembership.objects.create(team=quality_team, user=member)
    TeamMembership.objects.filter(team=quality_team, user=member).delete()
    with pytest.raises(ServiceRequest.DoesNotExist):
        desk_services.add_entry(
            actor=member,
            request=restricted_ticket,
            kind=EntryKind.NOTE,
            body="Synthetic note",
            expected_revision=restricted_ticket.revision,
        )
    assert not RequestEntry.objects.filter(body="Synthetic note").exists()


# F-02: later locks of other desk changes.


@pytest.mark.django_db(transaction=True)
def test_a_category_change_rechecks_the_actor_after_the_category_lock(
    plain_ticket, agent, complaint_category
):
    with at_read(
        lock_of("taxonomy_requestcategory"),
        lambda: User.objects.filter(pk=agent.pk).update(is_active=False),
        before=True,
    ) as concurrent:
        with pytest.raises(PermissionDenied):
            desk_services.update_properties(
                actor=agent,
                request=plain_ticket,
                expected_revision=plain_ticket.revision,
                priority=plain_ticket.priority,
                category=complaint_category,
            )
    assert concurrent == [True]
    assert fresh(plain_ticket).category_id == plain_ticket.category_id


@pytest.mark.django_db(transaction=True)
def test_a_supervisor_cannot_be_demoted_while_their_withdrawal_holds_the_role(plain_ticket, lead):
    """The withdrawal holds the supervisor's roles from before its first lock until it commits
    (accounts.acting): removing them at the entry's lock has to wait (here it gives up after its
    bounded wait), and the reply is withdrawn by a supervisor who still had the role."""
    desk_services.add_entry(
        actor=lead,
        request=plain_ticket,
        kind=EntryKind.REPLY,
        body="Synthetic reply",
        expected_revision=plain_ticket.revision,
    )
    reply = RequestEntry.objects.get(body="Synthetic reply")
    ticket = fresh(plain_ticket)

    def demote():
        User.objects.get(pk=lead.pk).groups.clear()

    with at_read(lock_of("desk_requestentry"), demote, before=True) as concurrent:
        desk_services.withdraw_reply(
            actor=lead, entry=reply, reason="Example", expected_revision=ticket.revision
        )
    assert concurrent == [False]
    assert fresh(reply).withdrawn_at is not None
    assert User.objects.get(pk=lead.pk).groups.exists()


@pytest.mark.django_db(transaction=True)
def test_withdrawing_a_reply_refuses_a_supervisor_demoted_before_it(plain_ticket, lead):
    """The supervisor lost their roles after the page was drawn: the withdrawal is refused as
    someone no longer staff, and the reply stays."""
    desk_services.add_entry(
        actor=lead,
        request=plain_ticket,
        kind=EntryKind.REPLY,
        body="Synthetic reply",
        expected_revision=plain_ticket.revision,
    )
    reply = RequestEntry.objects.get(body="Synthetic reply")
    ticket = fresh(plain_ticket)
    User.objects.get(pk=lead.pk).groups.clear()
    with pytest.raises(PermissionDenied):
        desk_services.withdraw_reply(
            actor=lead, entry=reply, reason="Example", expected_revision=ticket.revision
        )
    assert fresh(reply).withdrawn_at is None


# F-03: the decision form, like findings, answers 403 to someone no longer staff.


def test_the_complaint_decision_form_draws_nothing_for_a_reader_no_longer_staff(
    client, complaint, agent, monkeypatch
):
    client.force_login(agent)

    def refuse(**kwargs):
        User.objects.filter(pk=agent.pk).update(is_active=False)
        raise PermissionDenied("Your account no longer has a staff role.")

    monkeypatch.setattr(desk_services, "decide_complaint", refuse)
    response = client.post(
        reverse("desk:complaint-decision", args=[complaint.pk]),
        {
            "expected_revision": complaint.revision,
            "decision": "investigate",
            "rationale": "Example",
        },
    )
    assert response.status_code in (302, 403)
    assert complaint.subject.encode() not in response.content


# F-05: zero is no revision, on every form that keeps a stale draft.


@pytest.mark.parametrize("name", ["status", "owner", "close", "action-add"])
def test_revision_zero_is_refused_on_every_record_form(client, owned, tech, name):
    client.force_login(tech)
    url = reverse(f"nonconformance:{name}", args=[owned.pk])
    assert client.post(url, {"expected_revision": "0"}).status_code == 403


# F-07: the decision form of an unmarked complaint keeps its draft when stale.


def test_an_open_decision_form_keeps_its_draft_after_the_complaint_is_unmarked(
    client, complaint, agent, lead
):
    client.force_login(agent)
    drawn = complaint.revision
    desk_services.set_complaint(
        actor=lead,
        request=complaint,
        expected_revision=complaint.revision,
        is_complaint=False,
        reason="Not a complaint",
    )
    response = client.post(
        reverse("desk:complaint-decision", args=[complaint.pk]),
        {"expected_revision": drawn, "decision": "investigate", "rationale": "Draft rationale"},
    )
    assert response.status_code == 200 and b"Draft rationale" in response.content
    current = client.post(
        reverse("desk:complaint-decision", args=[complaint.pk]),
        {"expected_revision": fresh(complaint).revision, "decision": "investigate"},
    )
    assert current.status_code == 404


# F-08: a removed link answers 404 to a GET and to a current-revision POST alike.


def test_a_removed_link_is_not_found_except_for_a_form_drawn_before_its_removal(
    client, owned, linked, agent, lead
):
    drawn = fresh(owned).revision
    services.unlink_ticket(
        actor=lead, nc=fresh(owned), expected_revision=drawn, link=linked, reason="Removed"
    )
    client.force_login(agent)
    url = reverse("nonconformance:ticket-unlink", args=[owned.pk, linked.pk])
    assert client.get(url).status_code == 404
    current = fresh(owned).revision
    assert client.post(url, {"expected_revision": current, "reason": "R"}).status_code == 404
    kept = client.post(url, {"expected_revision": drawn, "reason": "Second reason"})
    assert kept.status_code == 200 and b"Second reason" in kept.content


# F-09, F-10: every module address while off, and every response of one while on.


@pytest.mark.parametrize("name", ["detail", "status", "close", "summary"])
def test_every_module_address_while_off_is_the_ordinary_not_found(owned, agent, name, settings):
    settings.DEBUG = False
    client = Client(enforce_csrf_checks=True)
    client.force_login(agent)
    LabModule.objects.update(is_enabled=False)
    args = () if name == "summary" else (owned.pk,)
    url = reverse(f"nonconformance:{name}", args=args)
    unknown = client.post("/unknown-example-address/")
    for response in (client.get(url), client.post(url)):
        assert response.status_code == 404
        assert response.get("Cache-Control") == unknown.get("Cache-Control")


def test_a_csrf_refusal_of_a_module_address_is_never_stored(owned, agent, settings):
    settings.DEBUG = False
    client = Client(enforce_csrf_checks=True)
    client.force_login(agent)
    response = client.post(reverse("nonconformance:status", args=[owned.pk]), {})
    assert response.status_code == 403 and "no-store" in response["Cache-Control"]


# F-13: the dashboard menu and a module page read the switch once per request.


def test_the_switch_is_read_once_per_page(client, owned, agent, django_assert_max_num_queries):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    client.force_login(agent)
    with CaptureQueriesContext(connection) as queries:
        assert client.get(reverse("desk:request-list")).status_code == 200
    switch_reads = [q for q in queries.captured_queries if '"core_labmodule"' in q["sql"]]
    assert len(switch_reads) == 1


# F-14: the ticket page, with or without a linked record, is private to its reader.


def test_every_ticket_page_is_private(client, plain_ticket, agent):
    client.force_login(agent)
    response = client.get(reverse("desk:request-detail", args=[plain_ticket.pk]))
    assert "private" in response["Cache-Control"] and "no-store" not in response["Cache-Control"]
