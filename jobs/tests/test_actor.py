"""The scheduled jobs' own account: created once, never able to sign in, never taken from a
person's account. Synthetic data."""

import pytest
from django.contrib.auth import authenticate
from django.urls import reverse

from accounts.models import User
from accounts.roles import effective_roles
from core.models import AuditEvent
from jobs import actor
from jobs.models import JobSettings

pytestmark = pytest.mark.django_db


def test_created_once_and_recorded_in_the_settings():
    first = actor.system_account()
    second = actor.system_account()
    assert first.pk == second.pk
    assert JobSettings.objects.get(pk=1).system_account_id == first.pk
    assert User.objects.filter(username=first.username).count() == 1


def test_account_cannot_be_used_to_work():
    user = actor.system_account()
    assert user.is_active is False
    assert user.has_usable_password() is False
    assert user.groups.count() == 0
    assert user.client_id is None
    assert user.email.endswith(".invalid")
    assert effective_roles(user) == set()
    assert not user.is_superuser and not user.is_staff


def test_account_cannot_authenticate():
    user = actor.system_account()
    for password in ("", "!", "password", user.password):
        assert authenticate(username=user.username, password=password) is None
        assert authenticate(email=user.email, password=password) is None


def test_account_stays_unable_to_authenticate_even_with_a_password_set():
    user = actor.system_account()
    user.set_password("test-password-729!")
    user.save()
    assert authenticate(username=user.username, password="test-password-729!") is None


def test_sign_in_page_refuses_the_account(client):
    user = actor.system_account()
    for login in (user.username, user.email):
        response = client.post(
            reverse("account_login"), {"login": login, "password": "test-password-729!"}
        )
        assert response.status_code == 200
        assert "_auth_user_id" not in client.session


@pytest.mark.parametrize("name", ["scheduled-jobs", "Scheduled-Jobs"])
def test_a_persons_account_with_the_same_name_is_not_taken(make_user, name):
    person = make_user(name, "client_services")
    system = actor.system_account()
    assert system.pk != person.pk
    assert system.username != person.username
    assert system.username.lower() != person.username.lower()
    person.refresh_from_db()
    assert person.is_active and person.has_usable_password()
    assert actor.is_system_account(person) is False
    assert actor.is_system_account(system) is True


def test_a_persons_account_with_the_same_address_is_not_taken(make_user):
    person = make_user("someone", "client_services")
    User.objects.filter(pk=person.pk).update(email=f"scheduled-jobs@{actor.EMAIL_DOMAIN}")
    system = actor.system_account()
    assert system.pk != person.pk
    assert system.email.lower() != f"scheduled-jobs@{actor.EMAIL_DOMAIN}"
    assert system.email.endswith(".invalid")


def test_is_system_account_is_false_for_everyone_else(make_user, agent):
    assert actor.is_system_account(agent) is False  # before any system account exists
    actor.system_account()
    assert actor.is_system_account(agent) is False
    assert actor.is_system_account(make_user("another", "admin")) is False
    assert actor.is_system_account(None) is False
    assert actor.is_system_account(User(username="unsaved")) is False


def test_creation_is_audited_once():
    user = actor.system_account()
    actor.system_account()
    events = AuditEvent.objects.filter(action="jobs.system_account_created")
    assert events.count() == 1
    event = events.get()
    assert event.actor_id == user.pk
    assert event.object_type == "accounts.user" and event.object_id == str(user.pk)
    assert event.after["is_active"] is False
