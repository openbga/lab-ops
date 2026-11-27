import logging

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.totp.internal.auth import (
    TOTP,
    generate_totp_secret,
)
from django.core.cache import cache
from django.urls import reverse

from accounts.tests.test_confirm_identity import code_for as code_with_time_left

from .conftest import PASSWORD
from .test_login import mailed_code, request_code

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def signin_records(caplog):
    cache.clear()
    caplog.set_level(logging.INFO, logger="lab_ops.signin")
    yield caplog
    cache.clear()


def records(caplog):
    return [record for record in caplog.records if record.name == "lab_ops.signin"]


def messages(caplog):
    return [record.getMessage() for record in records(caplog)]


@pytest.mark.parametrize("use_email", [False, True])
def test_sign_in_and_out_log_once_with_both_request_ids(
    client, make_user, signin_records, use_email
):
    user = make_user("signin_staff", "technical")
    response = client.post(
        reverse("account_login"),
        {"login": user.email if use_email else user.username, "password": PASSWORD},
        HTTP_RNDR_ID="8ebfa3c3-8929-4885",
    )
    assert response.status_code == 302
    client.get(reverse("account_logout"))  # Showing confirmation is not a sign-out.
    signed_out = client.post(reverse("account_logout"), HTTP_RNDR_ID="b-2")
    client.post(reverse("account_logout"))  # Anonymous sign-outs do not invent an event.

    assert messages(signin_records) == [
        "signin.success user=signin_staff",
        "signout user=signin_staff",
    ]
    success, signout = records(signin_records)
    assert success.request_id == response["X-Request-ID"]
    assert success.render_request_id == "8ebfa3c3-8929-4885"
    assert signout.request_id == signed_out["X-Request-ID"]
    assert signout.render_request_id == "b-2"


def test_password_failures_name_only_stored_accounts(client, make_user, signin_records):
    user = make_user("signin_supervisor", "supervisor")
    EmailAddress.objects.create(user=user, email="alias@example.invalid", verified=True)
    for identifier in ("SIGNIN_SUPERVISOR", user.email.upper(), "alias@example.invalid"):
        response = client.post(
            reverse("account_login"), {"login": identifier, "password": "wrong-Secret-1"}
        )
        assert response.status_code == 200
    client.post(
        reverse("account_login"),
        {"login": "Typed-Secret-99", "password": "wrong-Secret-2"},
        HTTP_RNDR_ID="x y=forged",
    )
    assert messages(signin_records) == ["signin.failed user=signin_supervisor"] * 3 + [
        "signin.failed user=<unknown>"
    ]
    assert records(signin_records)[-1].render_request_id == "-"
    assert all("Secret" not in line for line in messages(signin_records))


def test_throttled_password_attempt_is_also_logged(client, make_user, settings, signin_records):
    settings.ACCOUNT_RATE_LIMITS = {"login_failed": "1/m/key"}
    user = make_user("signin_throttled", "technical")
    for password in ("wrong-Secret-1", PASSWORD):
        response = client.post(
            reverse("account_login"), {"login": user.username, "password": password}
        )
        assert response.status_code == 200
    assert "_auth_user_id" not in client.session
    assert messages(signin_records) == ["signin.failed user=signin_throttled"] * 2


def test_mfa_failure_and_completed_sign_in_are_logged(client, make_user, signin_records):
    user = make_user("signin_mfa", "technical")
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    response = client.post(reverse("account_login"), {"login": user.username, "password": PASSWORD})
    assert response.url == reverse("mfa_authenticate")
    assert not messages(signin_records)  # A correct password alone has not signed in.

    client.post(response.url, {"code": "invalid-Secret-code"})
    assert messages(signin_records) == ["signin.failed user=signin_mfa"]
    response = client.post(response.url, {"code": code_with_time_left(secret)})
    assert client.session["_auth_user_id"] == str(user.pk)
    assert messages(signin_records) == [
        "signin.failed user=signin_mfa",
        "signin.success user=signin_mfa",
    ]


def test_email_code_failures_and_completed_sign_in_are_logged(
    client, make_user, settings, signin_records
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    user = make_user("signin_email_code", "technical")
    request_code(client, user.email)
    code = mailed_code()
    assert not messages(signin_records)
    client.post(reverse("account_confirm_login_code"), {"code": "invalid-Secret-code"})
    client.post(reverse("account_confirm_login_code"), {"code": code})
    assert client.session["_auth_user_id"] == str(user.pk)
    assert messages(signin_records) == [
        "signin.failed user=signin_email_code",
        "signin.success user=signin_email_code",
    ]


def test_stored_username_cannot_forge_log_fields(client, make_user, signin_records):
    user = make_user("synthetic\nforged=event", "technical")
    client.force_login(user)
    assert messages(signin_records) == ["signin.success user=synthetic%0Aforged%3Devent"]


def test_admin_sign_out_uses_the_same_log(client, make_user, signin_records):
    user = make_user("signin_admin", "admin", is_staff=True, is_superuser=True)
    client.post(reverse("account_login"), {"login": user.username, "password": PASSWORD})
    client.post(reverse("admin:logout"))
    assert messages(signin_records) == [
        "signin.success user=signin_admin",
        "signout user=signin_admin",
    ]
