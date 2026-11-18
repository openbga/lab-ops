"""Deactivating an account returns its assignments to the queue in the same transaction."""

import pytest
from django.db import connection

from accounts.models import User
from desk.models import ServiceRequest
from teams.models import TeamMembership

from .test_portal import raise_staff


@pytest.fixture
def assigned(agent, category, clinic, accessioning):
    TeamMembership.objects.create(team=accessioning, user=agent)
    return raise_staff(agent, category, clinic, team=accessioning, assignee=agent)


@pytest.mark.django_db(transaction=True)
def test_deactivation_rolls_back_as_one_operation_on_release_failure(agent, assigned, monkeypatch):
    from desk import services as desk_services

    assert not connection.in_atomic_block  # a plain autocommit save, no caller transaction

    def interrupted(**kwargs):
        raise RuntimeError("Synthetic interruption during assignment release")

    monkeypatch.setattr(desk_services, "release_assignments", interrupted)
    agent.is_active = False
    with pytest.raises(RuntimeError, match="Synthetic interruption"):
        agent.save(update_fields=["is_active"])
    agent.refresh_from_db()
    assigned.refresh_from_db()
    assert agent.is_active and assigned.assignee_id == agent.pk  # neither happened


@pytest.mark.django_db(transaction=True)
def test_a_retried_half_done_deactivation_still_releases(agent, assigned):
    # A path that bypassed save() left the account inactive and its work assigned.
    User.objects.filter(pk=agent.pk).update(is_active=False)
    agent.refresh_from_db()
    assert ServiceRequest.objects.get(pk=assigned.pk).assignee_id == agent.pk
    agent.save(update_fields=["is_active"])  # the retry
    assert ServiceRequest.objects.get(pk=assigned.pk).assignee_id is None
    assert agent.released_assignments == 1


@pytest.mark.django_db
def test_saving_an_inactive_account_without_work_releases_nothing(agent):
    User.objects.filter(pk=agent.pk).update(is_active=False)
    agent.refresh_from_db()
    agent.first_name = "Renamed"
    agent.save()
    assert not hasattr(agent, "released_assignments")
