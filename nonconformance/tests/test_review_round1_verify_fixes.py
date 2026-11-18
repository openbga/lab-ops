"""The cases next to each finding of the round 1 verification
(.local/review-history/nonconformance-round-1-verification.md): every later lock of a change
rechecks the acting account, every refusal path keeps or drops what it should, and the desk's
complaint pages follow the same rules. Synthetic data only."""

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from core.modules import ModuleOff
from desk import services as desk_services
from nonconformance import services

from . import test_review_round1 as previous

pytestmark = pytest.mark.django_db

owned = previous.owned
action = previous.action
linked = previous.linked
decided = previous.decided
at_read = previous.at_read


def fresh(obj):
    return type(obj).objects.get(pk=obj.pk)


def deactivate(user):
    return lambda: User.objects.filter(pk=user.pk).update(is_active=False)


def lock_of(table):
    return lambda sql: f'FROM "{table}"' in sql and "FOR" in sql and "UPDATE" in sql


# V-01: each later lock of a change is followed by a fresh reading of the acting account.


@pytest.mark.django_db(transaction=True)
def test_linking_rechecks_the_actor_after_the_ticket_lock(owned, agent, plain_ticket):
    with at_read(lock_of("desk_servicerequest"), deactivate(agent), before=True) as concurrent:
        with pytest.raises(PermissionDenied):
            services.link_ticket(
                actor=agent, nc=owned, expected_revision=owned.revision, request=plain_ticket
            )
    assert concurrent == [True]
    assert fresh(owned).revision == owned.revision


@pytest.mark.django_db(transaction=True)
def test_choosing_an_owner_rechecks_the_supervisor_after_the_person_lock(owned, lead, tech):
    def owner_lock(sql):
        return 'FROM "accounts_user"' in sql and "FOR NO KEY UPDATE" in sql

    with at_read(owner_lock, deactivate(lead), before=True) as concurrent:
        with pytest.raises(PermissionDenied):
            services.set_owner(actor=lead, nc=owned, expected_revision=owned.revision, owner=tech)
    assert concurrent == [True]
    assert fresh(owned).owner_id != tech.pk


@pytest.mark.django_db(transaction=True)
def test_editing_an_action_rechecks_the_actor_after_the_action_lock(owned, action, agent):
    nc = fresh(owned)
    with at_read(
        lock_of("nonconformance_correctiveaction"), deactivate(agent), before=True
    ) as concurrent:
        with pytest.raises(PermissionDenied):
            services.update_action(
                actor=agent,
                nc=nc,
                expected_revision=nc.revision,
                action=action,
                description="Changed",
                assignee=action.assignee,
                due_on=action.due_on,
                reason="Example",
            )
    assert concurrent == [True]
    assert fresh(action).description == action.description


# V-02: every desk staff change reads the actor again after the ticket lock.


@pytest.mark.django_db(transaction=True)
def test_a_complaint_decision_rechecks_the_actor_after_the_ticket_lock(complaint, agent):
    with at_read(lock_of("desk_servicerequest"), deactivate(agent), before=True) as concurrent:
        with pytest.raises(PermissionDenied):
            desk_services.decide_complaint(
                actor=agent,
                request=complaint,
                expected_revision=complaint.revision,
                decision="investigate",
                rationale="Example",
            )
    assert concurrent == [True]
    assert fresh(complaint).revision == complaint.revision


def test_any_desk_staff_change_refuses_an_account_deactivated_since_the_request_began(
    plain_ticket, agent
):
    User.objects.filter(pk=agent.pk).update(is_active=False)
    with pytest.raises(PermissionDenied):
        desk_services.set_complaint(
            actor=agent,
            request=plain_ticket,
            expected_revision=plain_ticket.revision,
            is_complaint=True,
            reason="Example",
        )
    assert not fresh(plain_ticket).is_complaint


# V-03: a refusal of the reader as staff answers 403 on every form; a preview is drawn only while
# its ticket can still be opened.


@pytest.mark.parametrize("name", ["status", "impact", "action-add", "ticket-link"])
def test_a_reader_no_longer_staff_gets_no_form_back(
    client, owned, agent, plain_ticket, monkeypatch, name
):
    client.force_login(agent)
    url = reverse(f"nonconformance:{name}", args=[owned.pk])
    revision = fresh(owned).revision

    def refuse(**kwargs):
        User.objects.filter(pk=agent.pk).update(is_active=False)
        raise PermissionDenied("Your account no longer has a staff role.")

    for service in ("change_status", "record_impact", "add_action", "link_ticket"):
        monkeypatch.setattr(services, service, refuse)
    response = client.post(
        url,
        {
            "expected_revision": revision,
            "status": "investigating",
            "impact": "none",
            "impact_assessment": "Example",
            "description": "Example",
            "assignee": agent.pk,
            "due_on": timezone.localdate().isoformat(),
            "ticket": plain_ticket.number,
        },
    )
    assert response.status_code in (302, 403)  # the session itself may have ended
    assert owned.title.encode() not in response.content


# V-04: only a lower whole-number revision is an earlier reading.


@pytest.mark.parametrize("name", ["owner", "close", "reopen"])
@pytest.mark.parametrize("revision", ["", "x", "9999", "-1"])
def test_supervisor_forms_refuse_a_non_supervisor_whatever_revision_is_posted(
    client, owned, tech, name, revision
):
    client.force_login(tech)
    response = client.post(
        reverse(f"nonconformance:{name}", args=[owned.pk]), {"expected_revision": revision}
    )
    assert response.status_code == 403


def test_a_supervisor_form_from_an_earlier_reading_keeps_the_draft(client, owned, tech, lead):
    client.force_login(tech)
    stale = owned.revision
    services.set_owner(actor=lead, nc=owned, expected_revision=owned.revision, owner=tech)
    response = client.post(
        reverse("nonconformance:reopen", args=[owned.pk]),
        {"expected_revision": stale, "reason": "Example draft"},
    )
    assert response.status_code == 200 and b"Example draft" in response.content


# V-08: the module switched off during a save answers 404 on every form.


@pytest.mark.parametrize(
    ("name", "service"),
    [("investigation", "record_investigation"), ("status", "change_status")],
)
def test_the_module_switched_off_during_a_save_is_not_found(
    client, owned, agent, monkeypatch, name, service
):
    client.force_login(agent)

    def off(**kwargs):
        raise ModuleOff("This module is not in use.")

    monkeypatch.setattr(services, service, off)
    response = client.post(
        reverse(f"nonconformance:{name}", args=[owned.pk]),
        {"expected_revision": owned.revision, "containment": "C", "status": "investigating"},
    )
    assert response.status_code == 404
    assert "no-store" in response["Cache-Control"]


# V-17: the ticket page, like the review, shows how the outcome was communicated without a date.


def test_the_ticket_page_shows_the_communication_method_without_a_date(client, decided, agent):
    desk_services.update_complaint(
        actor=agent,
        request=decided,
        expected_revision=decided.revision,
        outcome_communicated_how="Example switchboard call",
    )
    client.force_login(agent)
    page = client.get(reverse("desk:request-detail", args=[decided.pk])).content.decode()
    fact = page[page.index("Outcome communicated") :][:400]
    assert "Example switchboard call" in fact and "Not yet" not in fact
