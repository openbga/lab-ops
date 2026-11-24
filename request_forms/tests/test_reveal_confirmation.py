"""Confirm it is you before the first reveal of patient information in a session
(core.reveal_confirmation), where the laboratory turns it on: the reveal of a patient-information
form value asks a session that has not confirmed, nothing is read or recorded until it has, and the
confirmation page takes the password (and the authenticator code where the account has one) and
returns to the page the reveal was sent from. The person-level key's reveal is covered in
links/tests/test_person_links.py; the rule's page in tests/test_reveal_confirmation_rule.py."""

import time
from urllib.parse import parse_qs, urlsplit

import pytest
from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.mfa.totp.internal.auth import (
    TOTP,
    format_hotp_value,
    generate_totp_secret,
    hotp_value,
    yield_hotp_counters_from_time,
)
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts import confirmation
from core import reveal_confirmation
from core.models import AuditEvent, RevealConfirmationRule
from request_forms.models import FieldValue

from .conftest import PASSWORD, PATIENT_SENTINEL
from .test_pages import login, raised

pytestmark = [pytest.mark.django_db, pytest.mark.confirmation]

ACTION = "request_form.value_revealed"
CONFIRM = reverse("account-confirm-reveal")
USER_KEY = "account_authentication_methods_user"


@pytest.fixture(autouse=True)
def fresh_limits():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def asked():
    return RevealConfirmationRule.objects.create(pk=1, first_reveal=True, revision=1)


@pytest.fixture
def value(portal_user, pickup):
    request = raised(portal_user, pickup, {"count": 1, "patient": PATIENT_SENTINEL})
    return request, FieldValue.objects.get(key="patient")


def reveal_url(row):
    return reverse("request_details:reveal", args=[row.pk])


def ticket_url(request):
    return reverse("desk:request-detail", args=[request.pk])


def code_for(secret):
    left = 30 - time.time() % 30
    if left < 10:
        time.sleep(left + 0.2)
    return format_hotp_value(hotp_value(secret, next(yield_hotp_counters_from_time())))


def reauthenticated(client, user, *methods, ago=0):
    """Re-authentication records in the browser's session as allauth writes them, and the digest
    of the account's password as a password re-authentication leaves it (accounts.confirmation)."""
    from allauth.mfa.models import Authenticator

    session = client.session
    app = Authenticator.objects.filter(user=user, type="totp").first()
    entries = list(session.get(AUTHENTICATION_METHODS_SESSION_KEY, []))
    for method in methods:
        entry = {"method": method, "at": time.time() - ago, "reauthenticated": True}
        if method == "mfa":
            entry.update(type="totp", id=app.pk)
        entries.append(entry)
    session[AUTHENTICATION_METHODS_SESSION_KEY] = entries
    session[USER_KEY] = str(user.pk)
    state = confirmation.read(user)
    session[confirmation.PASSWORD_PROOF_KEY] = confirmation._password_proof(state.password_hash)
    session.save()


def test_off_reveals_without_asking(client, capable, value):
    _, row = value
    login(client, capable)
    response = client.post(reveal_url(row), HTTP_HX_REQUEST="true")
    assert response.status_code == 200 and PATIENT_SENTINEL in response.content.decode()
    assert reveal_confirmation.SESSION_KEY not in client.session


def test_on_asks_first_and_reads_nothing(client, capable, value, asked):
    request, row = value
    login(client, capable)
    on_screen = "http://testserver" + ticket_url(request) + "?sort=-number"
    response = client.post(reveal_url(row), HTTP_HX_REQUEST="true", HTTP_HX_CURRENT_URL=on_screen)
    assert response.status_code == 204 and not response.content
    assert "no-store" in response["Cache-Control"]
    target = urlsplit(response["HX-Redirect"])
    assert target.path == CONFIRM
    assert parse_qs(target.query)["next"] == [ticket_url(request) + "?sort=-number"]
    assert not AuditEvent.objects.filter(action=ACTION).exists()
    # Without JavaScript: a redirect, back to the referring page.
    plain = client.post(reveal_url(row), HTTP_REFERER="http://testserver" + ticket_url(request))
    assert plain.status_code == 302 and PATIENT_SENTINEL not in plain.content.decode()
    assert parse_qs(urlsplit(plain["Location"]).query)["next"] == [ticket_url(request)]
    assert not AuditEvent.objects.filter(action=ACTION).exists()


def test_page_on_another_site_is_not_where_it_returns(client, capable, value, asked):
    _, row = value
    login(client, capable)
    response = client.post(
        reveal_url(row), HTTP_HX_REQUEST="true", HTTP_HX_CURRENT_URL="https://elsewhere.example/x"
    )
    assert response["HX-Redirect"] == CONFIRM
    page = client.get(CONFIRM, {"next": "https://elsewhere.example/x"})
    assert page.status_code == 200
    assert 'value="https://elsewhere.example/x"' not in page.content.decode()
    for target in ("//elsewhere.example/x", "https://elsewhere.example/x", "javascript:alert(1)"):
        done = client.post(CONFIRM, {"next": target, confirmation.PASSWORD_FIELD: PASSWORD})
        assert done.status_code == 302 and done["Location"] == reverse("dashboard"), target
        session = client.session
        session.pop(reveal_confirmation.SESSION_KEY, None)
        session.save()


def test_password_confirms_for_the_session(client, capable, value, asked):
    request, row = value
    login(client, capable)
    back = ticket_url(request)
    page = client.get(CONFIRM, {"next": back})
    body = page.content.decode()
    assert page.status_code == 200 and "no-store" in page["Cache-Control"]
    assert f'name="{confirmation.PASSWORD_FIELD}"' in body
    assert f'name="{confirmation.CODE_FIELD}"' not in body  # no authenticator app
    empty = client.post(CONFIRM, {"next": back})
    assert "Enter your password to confirm it is you." in empty.content.decode()
    form = empty.content.decode().split('<form class="vr-form', 1)[1].split("</form>", 1)[0]
    assert "save this change" not in form
    wrong = client.post(CONFIRM, {"next": back, confirmation.PASSWORD_FIELD: "not-it-4471"})
    assert wrong.status_code == 200 and "Incorrect password." in wrong.content.decode()
    assert reveal_confirmation.SESSION_KEY not in client.session
    assert client.post(reveal_url(row), HTTP_HX_REQUEST="true").status_code == 204
    done = client.post(CONFIRM, {"next": back, confirmation.PASSWORD_FIELD: PASSWORD})
    assert done.status_code == 302 and done["Location"] == back
    assert client.session[reveal_confirmation.SESSION_KEY][0] == capable.pk
    response = client.post(reveal_url(row), HTTP_HX_REQUEST="true")
    assert response.status_code == 200 and PATIENT_SENTINEL in response.content.decode()
    assert AuditEvent.objects.filter(action=ACTION, actor=capable).count() == 1
    # Already confirmed: the page goes straight back.
    again = client.get(CONFIRM, {"next": back})
    assert again.status_code == 302 and again["Location"] == back


def test_confirmed_session_is_not_asked_again_after_the_window(client, capable, value, asked):
    _, row = value
    login(client, capable)
    reauthenticated(client, capable, "password", ago=confirmation.window() + 60)
    assert client.post(reveal_url(row), HTTP_HX_REQUEST="true").status_code == 204
    client.post(CONFIRM, {confirmation.PASSWORD_FIELD: PASSWORD})
    session = client.session
    session[AUTHENTICATION_METHODS_SESSION_KEY] = [
        {**entry, "at": time.time() - confirmation.window() - 60}
        for entry in session[AUTHENTICATION_METHODS_SESSION_KEY]
    ]
    session.save()
    assert not confirmation.current(type("R", (), {"session": client.session, "user": capable}))
    assert client.post(reveal_url(row), HTTP_HX_REQUEST="true").status_code == 200


def test_recent_confirmation_for_another_change_counts(client, capable, value, asked):
    _, row = value
    login(client, capable)
    reauthenticated(client, capable, "password")
    response = client.post(reveal_url(row), HTTP_HX_REQUEST="true")
    assert response.status_code == 200 and PATIENT_SENTINEL in response.content.decode()
    assert client.session[reveal_confirmation.SESSION_KEY][0] == capable.pk


def test_expired_confirmation_and_the_sign_in_do_not_count(client, capable, value, asked):
    _, row = value
    login(client, capable)  # the sign-in itself is not a re-authentication
    assert client.post(reveal_url(row), HTTP_HX_REQUEST="true").status_code == 204
    reauthenticated(client, capable, "password", ago=confirmation.window() + 1)
    assert client.post(reveal_url(row), HTTP_HX_REQUEST="true").status_code == 204
    assert not AuditEvent.objects.filter(action=ACTION).exists()


def test_authenticator_app_asks_for_a_code_too(client, capable, value, asked):
    request, row = value
    secret = generate_totp_secret()
    TOTP.activate(capable, secret)
    login(client, capable)
    back = ticket_url(request)
    body = client.get(CONFIRM, {"next": back}).content.decode()
    assert f'name="{confirmation.CODE_FIELD}"' in body
    password_only = client.post(CONFIRM, {"next": back, confirmation.PASSWORD_FIELD: PASSWORD})
    assert password_only.status_code == 200
    assert "or a recovery code, to confirm it is you." in password_only.content.decode()
    assert reveal_confirmation.SESSION_KEY not in client.session
    # A password re-authentication alone is not a confirmation for an account with an app.
    reauthenticated(client, capable, "password")
    assert client.post(reveal_url(row), HTTP_HX_REQUEST="true").status_code == 204
    done = client.post(
        CONFIRM,
        {
            "next": back,
            confirmation.PASSWORD_FIELD: PASSWORD,
            confirmation.CODE_FIELD: code_for(secret),
        },
    )
    assert done.status_code == 302 and done["Location"] == back
    assert client.post(reveal_url(row), HTTP_HX_REQUEST="true").status_code == 200


def test_session_mark_of_another_account_does_not_count(client, capable, agent, value, asked):
    _, row = value
    login(client, capable)
    session = client.session
    session[reveal_confirmation.SESSION_KEY] = [agent.pk, session["_account_session"]]
    session.save()
    assert client.post(reveal_url(row), HTTP_HX_REQUEST="true").status_code == 204
    for stored in (capable.pk, True, [capable.pk, "an-earlier-sign-in"], [capable.pk]):
        session = client.session
        session[reveal_confirmation.SESSION_KEY] = stored  # an earlier format or sign-in
        session.save()
        assert client.post(reveal_url(row), HTTP_HX_REQUEST="true").status_code == 204, stored


def test_sign_in_again_asks_again(client, capable, value, asked):
    _, row = value
    login(client, capable)
    client.post(CONFIRM, {confirmation.PASSWORD_FIELD: PASSWORD})
    assert client.post(reveal_url(row), HTTP_HX_REQUEST="true").status_code == 200
    client.post(reverse("account_logout"))
    login(client, capable)
    assert reveal_confirmation.SESSION_KEY not in client.session
    assert client.post(reveal_url(row), HTTP_HX_REQUEST="true").status_code == 204


def test_rule_turned_off_reveals_again(client, capable, value, asked):
    _, row = value
    login(client, capable)
    assert client.post(reveal_url(row), HTTP_HX_REQUEST="true").status_code == 204
    RevealConfirmationRule.objects.filter(pk=1).update(first_reveal=False)
    assert client.post(reveal_url(row), HTTP_HX_REQUEST="true").status_code == 200
    page = client.get(CONFIRM, {"next": "/requests/"})
    assert page.status_code == 302 and page["Location"] == "/requests/"


def test_who_may_open_the_confirmation_page(client, capable, portal_user, value, asked):
    assert client.get(CONFIRM).status_code == 302  # signed out: to sign-in
    login(client, portal_user)
    assert client.get(CONFIRM).status_code == 403
    assert client.post(CONFIRM, {confirmation.PASSWORD_FIELD: PASSWORD}).status_code == 403
    assert reveal_confirmation.SESSION_KEY not in client.session


def test_without_the_capability_nothing_is_revealed_after_confirming(client, agent, value, asked):
    _, row = value
    login(client, agent)
    # Asked first whatever the address names, so the answer says nothing about the value.
    assert client.post(reveal_url(row), HTTP_HX_REQUEST="true").status_code == 204
    assert client.post(reveal_url(row) + "0", HTTP_HX_REQUEST="true").status_code in (204, 404)
    client.post(CONFIRM, {confirmation.PASSWORD_FIELD: PASSWORD})
    assert client.post(reveal_url(row), HTTP_HX_REQUEST="true").status_code == 404
    assert not AuditEvent.objects.filter(action=ACTION).exists()


def test_account_without_a_password_cannot_confirm(client, capable, value, asked):
    _, row = value
    capable.set_unusable_password()
    capable.save()
    client.force_login(capable)
    page = client.get(CONFIRM)
    body = page.content.decode()
    assert "no password to confirm it is you" in body
    assert f'name="{confirmation.PASSWORD_FIELD}"' not in body
    assert 'type="submit"' not in body.split("vr-form", 1)[1].split("</form>")[0]
    client.post(CONFIRM, {})
    assert reveal_confirmation.SESSION_KEY not in client.session
    assert client.post(reveal_url(row), HTTP_HX_REQUEST="true").status_code == 204


def test_failed_confirmation_counts_toward_the_limit_and_is_not_kept(client, capable, asked):
    from accounts.models import SignInEvent

    login(client, capable)
    response = client.post(CONFIRM, {confirmation.PASSWORD_FIELD: "wrong-guess-5521"})
    assert response.status_code == 200
    assert "wrong-guess-5521" not in response.content.decode()
    failed = SignInEvent.objects.filter(user=capable).order_by("-pk").first()
    assert failed is not None and "wrong-guess-5521" not in str(failed.__dict__)


def test_the_rule_is_read_after_the_acting_account_is_held(client, capable, value, asked):
    """The reveal takes the rule's lock shared after the acting account's gate (accounts.acting:
    the account comes before every other lock), and before the value is read."""
    _, row = value
    login(client, capable)
    client.post(CONFIRM, {confirmation.PASSWORD_FIELD: PASSWORD})
    with CaptureQueriesContext(connection) as queries:
        assert client.post(reveal_url(row), HTTP_HX_REQUEST="true").status_code == 200
    sql = [query["sql"] for query in queries.captured_queries]
    gate = next(i for i, s in enumerate(sql) if "pg_advisory_xact_lock_shared(19553" in s)
    rule = next(
        i
        for i, s in enumerate(sql)
        if "pg_advisory_xact_lock_shared({}, {})".format(*reveal_confirmation.RULE_LOCK) in s
    )
    value_read = next(i for i, s in enumerate(sql) if "request_forms_fieldvalue" in s)
    assert gate < rule < value_read
