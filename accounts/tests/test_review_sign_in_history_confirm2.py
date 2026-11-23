"""Second confirmation of sign-in history, with synthetic accounts and ordinary sign-in flows.

Adopted from the second confirmation of review round 1
(.local/review-history/sign-in-history-round-1-confirmation-2.md; the original file is kept beside
it). Cases changed while adopting are marked "Adopted:" with the reason."""

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

import pytest
from allauth.account.internal.flows.login import record_authentication
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.contrib.auth import login as django_login
from django.core.cache import cache
from django.db import OperationalError, transaction
from django.urls import reverse

from accounts import compromised, sessions, signin_history
from accounts.adapter import ClosedSignupAdapter
from accounts.models import AccountSession, SignInEvent, SignInFactor, User
from accounts.tests.test_login import request_code
from accounts.tests.test_review_compromised_password_confirm import mark
from accounts.tests.test_review_password_rules import reset_form
from accounts.tests.test_sign_in_history import code_for, session_row, sign_in
from tests.test_review_session_list import on_connection, wait_for_block

pytestmark = pytest.mark.django_db
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def fresh_limits(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    cache.clear()
    yield
    cache.clear()


def test_replacement_signin_reads_its_own_factor(client, make_user):
    user = make_user("confirm2-replacement")
    assert sign_in(client, user.username).status_code == 302
    previous = session_row(client)
    assert previous.factor == SignInFactor.PASSWORD
    request = client.get(reverse("account-sign-ins")).wsgi_request
    assert request.account_session.id == str(previous.pk)
    # Exercise the adapter/reader boundary on a single request, as a post-login observer
    # would see it. Both sign-ins belong to the same synthetic, unmarked account.
    record_authentication(request, user, "code", email=user.email)
    ClosedSignupAdapter().login(request, user)
    current = AccountSession.objects.get(pk=request.session[sessions.SESSION_KEY])
    previous.refresh_from_db()
    assert current.pk != previous.pk and previous.ending == AccountSession.Ending.SIGNED_IN_AGAIN
    assert current.factor == SignInFactor.CODE and current.ended_at is None
    assert compromised.proved_beyond_password(request, user)


@pytest.mark.xfail(
    strict=True,
    reason="Accepted, predates: needs ACCOUNT_LOGIN_ON_PASSWORD_RESET, which this release leaves "
    "off. A reset link that signs in is not one of the recorded factors, so a second factor after "
    "it is not counted, and a mark made between the reset and the code refuses the sign-in.",
)
def test_verified_authenticator_after_reset_can_open_recovery(client, make_user, settings):
    settings.ACCOUNT_LOGIN_ON_PASSWORD_RESET = True
    user = make_user("confirm2-reset")
    owner = make_user("confirm2-owner", is_staff=True, is_superuser=True)
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    reset = reset_form(client, user)
    fresh = "Linen curtains beside a quiet orchard"
    next_step = client.post(reset, {"password1": fresh, "password2": fresh})
    assert next_step.url == reverse("mfa_authenticate")
    user.refresh_from_db()
    mark(owner, user)
    completed = client.post(next_step.url, {"code": code_for(secret)})
    assert completed.status_code == 302
    current = session_row(client)
    assert current.ended_at is None, current.ending
    assert client.get(reverse("account-new-password")).status_code == 200


def test_passwordless_primary_does_not_inherit_previous_password(make_user, rf):
    user = make_user("confirm2-passwordless")
    request = rf.get("/")
    request.session = {}
    record_authentication(request, user, "password")
    # allauth.mfa.base.internal.flows.post_authentication distinguishes a standalone passkey
    # from a second factor with this flag. History has no label for a standalone passkey.
    record_authentication(request, user, "mfa", type="webauthn", passwordless=True)
    assert signin_history.sign_in_factor(request, user) == ""


def test_failed_adapter_login_leaves_no_factor_for_an_independent_login(
    client, make_user, rf, monkeypatch
):
    user = make_user("confirm2-interrupted")
    request = rf.get("/")
    request.session = client.session
    record_authentication(request, user, "password")

    def unavailable():
        raise OperationalError("Synthetic interrupted session rotation")

    with monkeypatch.context() as patch:
        patch.setattr(request.session, "cycle_key", unavailable)
        with pytest.raises(OperationalError, match="interrupted session rotation"):
            ClosedSignupAdapter().login(request, user)
    assert not AccountSession.objects.filter(user=user).exists()
    django_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    row = AccountSession.objects.get(pk=request.session[sessions.SESSION_KEY])
    assert SignInEvent.objects.get(session=row.pk, kind=SignInEvent.Kind.SIGNED_IN).factor == ""


def test_operations_describes_attempts_per_email_challenge(client, make_user, settings, mailoutbox):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    settings.ACCOUNT_LOGIN_BY_CODE_MAX_ATTEMPTS = 3
    user = make_user("confirm2-resend")
    step = request_code(client, user.email).url
    for _ in range(2):
        assert client.post(step, {"code": "incorrect"}).status_code == 200
    before = len(mailoutbox)
    assert client.post(step, {"action": "resend"}).url == step
    assert len(mailoutbox) == before + 1
    assert client.post(step, {"code": "incorrect"}).url == reverse("account_request_login_code")
    assert SignInEvent.objects.filter(user=user, kind=SignInEvent.Kind.FAILED).count() == 3
    guide = " ".join((ROOT / "docs/operations.md").read_text().split())
    assert "each emailed code allows a few attempts of its own" not in guide


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("undo", [False, True], ids=["commit", "rollback"])
def test_waiting_signin_observes_deactivation_outcome(make_user, rf, undo):
    user = make_user("confirm2-waits")
    request = rf.get("/")
    request.session = {}
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            writer = User.objects.get(pk=user.pk)
            writer.is_active = False
            writer.save(update_fields=["is_active"])
            pending = pool.submit(
                on_connection, lambda: sessions.start_session(User, request, user), pids
            )
            wait_for_block(pending, pids)
            transaction.set_rollback(undo)
        pending.result(timeout=10)
    user.refresh_from_db()
    row = AccountSession.objects.get(user=user)
    assert (user.is_active, user.last_login is not None, row.ended_at is None) == (undo,) * 3
    assert row.ending == ("" if undo else AccountSession.Ending.DEACTIVATED)
    event = SignInEvent.objects.get(user=user)
    assert event.kind == (SignInEvent.Kind.SIGNED_IN if undo else SignInEvent.Kind.REFUSED)


def test_rollout_note_accounts_for_a_refused_password_reauthentication(client, make_user, caplog):
    # A username may coincide with another account's email. The reauthentication form still
    # checks that the password was accepted for the account that opened it.
    person = make_user("confirm2-reader@example.invalid")
    other = make_user("confirm2-other")
    other.email = person.username
    other.set_password("Distinct synthetic passphrase for the second fixture")
    other.save(update_fields=["email", "password"])
    client.force_login(person)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        response = client.post(
            reverse("account_reauthenticate"),
            {"password": "Distinct synthetic passphrase for the second fixture"},
        )
    assert response.context["form"].has_error("password", code="incorrect_password")
    assert not [r for r in caplog.records if r.name == "lab_ops.signin"]
    guide = " ".join((ROOT / "docs/operations.md").read_text().split())
    assert "the passwords and codes it refused, on re-authentication too" not in guide


def test_history_volume_note_accounts_for_an_unknown_login(client):
    response = sign_in(client, "confirm2-unknown", "wrong synthetic value")
    assert response.status_code == 200 and response.context["form"].errors
    assert not SignInEvent.objects.exists()
    guide = " ".join((ROOT / "docs/operations.md").read_text().split())
    assert "Wrong passwords and codes that are checked add one entry each" not in guide
