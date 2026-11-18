"""The scheduled jobs' account cannot sign in, even after someone activates it and gives it a
password in the admin console. Synthetic data."""

import pytest
from django.urls import reverse

from jobs.actor import system_account

pytestmark = pytest.mark.django_db

PASSWORD = "Synthetic-passphrase-for-tests-only-42"


def _activate_with_password(user):
    user.is_active = True
    user.set_password(PASSWORD)
    user.save()


def test_activated_system_account_is_refused_at_sign_in(client):
    account = system_account()
    _activate_with_password(account)
    response = client.post(
        reverse("account_login"), {"login": account.username, "password": PASSWORD}
    )
    assert "_auth_user_id" not in client.session
    assert response.status_code == 302
    assert response["Location"] == reverse("account_inactive")


def test_a_person_with_a_similar_name_still_signs_in(client, make_user):
    person = make_user("scheduled-jobs-person", "client_services")
    person.set_password(PASSWORD)
    person.save()
    system_account()
    client.post(reverse("account_login"), {"login": person.username, "password": PASSWORD})
    assert client.session.get("_auth_user_id") == str(person.pk)
