"""Confirmation cases for the two fixes after 670ae2b, using synthetic users and PostgreSQL.

Adopted from the confirmation of review round 1
(.local/review-history/sign-in-history-round-1-confirmation.md; the original file is kept beside
it). Cases changed while adopting are marked "Adopted:" with the reason."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from queue import Queue

import pytest
from django.contrib.auth import login as django_login
from django.core.cache import cache
from django.db import OperationalError, connection, transaction
from django.urls import reverse

from accounts import acting, compromised, sessions, signin_history
from accounts.models import AccountSession, SignInEvent, SignInFactor, User
from accounts.tests.test_login import mailed_code, request_code
from accounts.tests.test_review_compromised_password_confirm import mark, real_signin
from accounts.tests.test_sign_in_history import session_row, sign_in
from core.models import SecondFactorRule
from tests.test_review_session_limits_verify import age
from tests.test_review_session_list import on_connection, wait_for_block

pytestmark = pytest.mark.django_db
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def fresh_counts():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("earlier", ["replacement", "expired"])
def test_deactivation_can_finish_ending_sessions_while_signin_waits(make_user, rf, earlier):
    """Observe the compatibility of the two real lock orders. Never enqueue a reverse wait."""
    user = make_user("confirm-deactivation")
    old = AccountSession.objects.create(user=user)
    request = rf.get("/")
    request.session = {}
    if earlier == "replacement":
        request.session[sessions.SESSION_KEY] = str(old.pk)
    else:
        age(old, started=timedelta(hours=2), active=timedelta(hours=1))

    pids = Queue()
    session_available = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            # The prefix of User.save(update_fields=["is_active"]), before _end_sessions.
            acting.close_access([user.pk])
            User.objects.select_for_update(no_key=True).get(pk=user.pk)
            signing_in = pool.submit(
                on_connection, lambda: sessions.start_session(User, request, user), pids
            )
            wait_for_block(signing_in, pids)
            try:
                with transaction.atomic():
                    AccountSession.objects.select_for_update(no_key=True, nowait=True).get(
                        pk=old.pk
                    )
            except OperationalError as exc:
                assert exc.__cause__.sqlstate == "55P03"
                session_available = False
        signing_in.result(timeout=10)
    assert session_available, "Deactivation needs the session row before it can release the account"


@pytest.mark.parametrize(
    "method,needs_enrolment",
    [("email-code", False), ("email-code", True), ("totp", False), ("recovery", False)],
)
def test_marked_account_keeps_verified_recovery_after_changing_accounts(
    client, make_user, settings, method, needs_enrolment
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    first = make_user("confirm-prior")
    recipient = make_user("confirm-returning", "technical")
    owner = make_user("confirm-owner", is_superuser=True, is_staff=True)
    mark(owner, recipient)
    if needs_enrolment:
        SecondFactorRule.objects.update_or_create(pk=1, defaults={"roles": ["technical"]})
    sign_in(client, first.username)
    first.is_active = False
    first.save(update_fields=["is_active"])
    expected = {
        "email-code": SignInFactor.CODE,
        "totp": SignInFactor.PASSWORD_AUTHENTICATOR,
        "recovery": SignInFactor.PASSWORD_RECOVERY_CODE,
    }[method]
    assert real_signin(client, recipient, method).status_code == 302
    row = session_row(client)
    assert row.user_id == recipient.pk
    assert row.ended_at is None, "The completed recovery sign-in should open the session"
    route = "account-second-factor" if needs_enrolment else "account-new-password"
    target = reverse(route)
    response = client.get(target)
    row.refresh_from_db()
    assert (row.factor, row.ending, response.status_code) == (expected, "", 200)


@pytest.mark.parametrize("another_account", [False, True], ids=["same", "next-account"])
def test_factor_captured_by_allauth_is_for_one_login_only(client, make_user, another_account):
    first = make_user("confirm-adapter")
    second = make_user("confirm-django") if another_account else first
    completed = sign_in(client, first.username)
    assert session_row(client).factor == SignInFactor.PASSWORD
    # A second, independent Django login in the same request, without a new allauth step.
    request = completed.wsgi_request
    django_login(request, second, backend="django.contrib.auth.backends.ModelBackend")
    row = AccountSession.objects.get(pk=request.session[sessions.SESSION_KEY])
    assert row.user_id == second.pk
    assert row.factor == ""
    assert SignInEvent.objects.get(session=row.pk, kind=SignInEvent.Kind.SIGNED_IN).factor == ""


def test_failure_only_factor_is_not_a_completed_second_factor():
    # This enum value is the unspecified type of a rejected code. It is a history detail.
    assert not compromised.beyond_password(SignInFactor.SECOND_FACTOR)


@pytest.mark.django_db(transaction=True)
@pytest.mark.xfail(
    strict=True,
    reason="Accepted: the application's transactions run at PostgreSQL's default read committed "
    "isolation (Django's default), where the advisory lock serializes the lockout entries; a "
    "transaction a caller opens at repeatable read sees its own snapshot.",
)
def test_lockout_window_with_an_existing_repeatable_read_snapshot(make_user):
    """Two sequential writes, with a reader that already has a PostgreSQL snapshot."""
    user = make_user("confirm-snapshot")

    def write():
        signin_history.record(
            user.pk, SignInEvent.Kind.LOCKED_OUT, None, factor=SignInFactor.PASSWORD
        )

    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        assert not SignInEvent.objects.filter(user=user).exists()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(on_connection, write, Queue()).result(timeout=10)
        write()
    assert SignInEvent.objects.filter(user=user, kind=SignInEvent.Kind.LOCKED_OUT).count() == 1


def sign_in_log(caplog):
    return [r.getMessage().split()[0] for r in caplog.records if r.name == "lab_ops.signin"]


def test_pending_email_deactivation_obeys_operational_logging_note(
    client, make_user, settings, caplog
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    user = make_user("confirm-email-inactive")
    challenge = request_code(client, user.email)
    code = mailed_code()
    user.is_active = False
    user.save(update_fields=["is_active"])
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        assert client.post(challenge.url, {"code": code}).url == reverse("account_inactive")
    refused = SignInEvent.objects.get(user=user)
    assert (refused.kind, refused.ending, refused.session) == (
        SignInEvent.Kind.REFUSED,
        AccountSession.Ending.DEACTIVATED,
        None,
    )
    # Adopted: the documents now say the line is logged for a sign-in whose session starts
    # ended; allauth's own refusal of an inactive account starts none and adds no line.
    assert "signout.deactivated" not in sign_in_log(caplog)
    operations = " ".join((ROOT / "docs/operations.md").read_text().split())
    assert "a refusal before any session starts adds no line" in operations


def test_sessionless_deactivation_obeys_operational_logging_note(
    rf, make_user, caplog, django_capture_on_commit_callbacks
):
    user = make_user("confirm-no-session")
    user.is_active = False
    user.save(update_fields=["is_active"])
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        with django_capture_on_commit_callbacks(execute=True):
            sessions.start_session(User, rf.get("/"), user)
            assert sign_in_log(caplog) == []
    event = SignInEvent.objects.get(user=user)
    assert (event.kind, event.ending, event.session) == (
        SignInEvent.Kind.REFUSED,
        AccountSession.Ending.DEACTIVATED,
        None,
    )
    # Adopted: without a browser session no session starts, so no line (see the case above).
    assert sign_in_log(caplog) == []


def test_operations_distinguishes_logged_password_reauthentication(client, make_user, caplog):
    user = make_user("confirm-reauth-log")
    client.force_login(user)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        response = client.post(
            reverse("account_reauthenticate"), {"password": "incorrect synthetic value"}
        )
    assert response.status_code == 200 and response.context["form"].has_error("password")
    assert sign_in_log(caplog) == ["signin.failed"]
    operations = " ".join((ROOT / "docs/operations.md").read_text().split())
    assert (
        "the earlier release's sign-in log has those at sign-in, not those on re-authentication."
        not in operations
    )


def test_operations_identifies_the_emailed_code_attempt_limit(client, make_user, settings):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    settings.ACCOUNT_RATE_LIMITS = {"login_failed": "1/m/key"}
    user = make_user("confirm-code-limit")
    challenge = request_code(client, user.email)
    for _ in range(2):
        response = client.post(challenge.url, {"code": "unused synthetic value"})
        assert response.status_code == 200
    assert list(SignInEvent.objects.filter(user=user).values_list("kind", flat=True)) == [
        SignInEvent.Kind.FAILED,
        SignInEvent.Kind.FAILED,
    ]
    operations = " ".join((ROOT / "docs/operations.md").read_text().split())
    assert "at sign-in the failed sign-in limit bounds how many are checked" not in operations


@pytest.mark.django_db(transaction=True)
def test_lockout_after_rollback_and_boundary_at_the_time_of_the_next_write(make_user):
    user = make_user("confirm-rolled-back")
    with transaction.atomic():
        signin_history.record(user, SignInEvent.Kind.LOCKED_OUT, None, factor=SignInFactor.CODE)
        transaction.set_rollback(True)
    assert not SignInEvent.objects.filter(user=user).exists()
    for _ in range(2):
        signin_history.record(user.pk, SignInEvent.Kind.LOCKED_OUT, None, factor=SignInFactor.CODE)
    assert SignInEvent.objects.filter(user=user, kind=SignInEvent.Kind.LOCKED_OUT).count() == 1


def test_login_code_help_qualifies_the_log_by_known_account(client, settings, caplog):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    step = request_code(client, "confirm-no-such-account@example.invalid")
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        response = client.post(step.url, {"code": ""})
    assert response.wsgi_request._login_stage.state["failed_attempts"] == 1
    assert sign_in_log(caplog) == [] and not SignInEvent.objects.exists()
    guide = (ROOT / "docs/pages/accounts/login-code.json").read_text()
    assert "the application log, which records every attempt counted against the code" not in guide


def test_changelog_distinguishes_named_login_from_known_reauthentication(client, make_user):
    user = make_user("confirm-overlap@example.invalid")
    other = make_user("confirm-overlap")
    other.email = user.username
    other.save(update_fields=["email"])
    assert signin_history.account_named({"username": user.username}) is None
    client.force_login(user)
    response = client.post(
        reverse("account_reauthenticate"), {"password": "incorrect synthetic value"}
    )
    assert response.context["form"].has_error("password")
    assert SignInEvent.objects.get(kind=SignInEvent.Kind.REAUTHENTICATION_FAILED).user_id == user.pk
    change = (ROOT / "changelog.d/2026-11-23-sign-in-history.md").read_text()
    blanket = (
        "A wrong password, or one the failed sign-in limit refused, is recorded only for the one "
        "account the username or email address names"
    )
    assert blanket not in change


def test_supported_successful_factors_match_compromised_password_rule():
    # The six labels that can result from a completed allauth sign-in.
    factors = {
        SignInFactor.PASSWORD: False,
        SignInFactor.CODE: True,
        SignInFactor.PASSWORD_AUTHENTICATOR: True,
        SignInFactor.PASSWORD_RECOVERY_CODE: True,
        SignInFactor.CODE_AUTHENTICATOR: True,
        SignInFactor.CODE_RECOVERY_CODE: True,
        "": False,
    }
    assert {f: compromised.beyond_password(f) for f in factors} == factors


def test_sessionless_refusal_rolls_back_with_the_outer_transaction(make_user, rf):
    user = make_user("confirm-rolled-refusal", is_active=False)
    request = rf.get("/", REMOTE_ADDR="192.0.2.83", HTTP_USER_AGENT="Confirmation browser")
    with transaction.atomic():
        sessions.start_session(User, request, user)
        event = SignInEvent.objects.get(user=user)
        assert (event.session, event.ending, event.address, event.user_agent) == (
            None,
            AccountSession.Ending.DEACTIVATED,
            "192.0.2.83",
            "Confirmation browser",
        )
        transaction.set_rollback(True)
    assert not SignInEvent.objects.filter(user=user).exists()


def test_failed_session_insert_restores_earlier_session_and_its_history(make_user, rf, monkeypatch):
    user = make_user("confirm-rollback-replacement")
    earlier = AccountSession.objects.create(user=user)
    request = rf.get("/")
    request.session = {sessions.SESSION_KEY: str(earlier.pk)}

    class WriteInterrupted(Exception):
        pass

    def stop_before_create(*args, **kwargs):
        assert (
            AccountSession.objects.get(pk=earlier.pk).ending
            == AccountSession.Ending.SIGNED_IN_AGAIN
        )
        raise WriteInterrupted

    monkeypatch.setattr(AccountSession.objects, "create", stop_before_create)
    with pytest.raises(WriteInterrupted):
        sessions.start_session(User, request, user)
    earlier.refresh_from_db()
    user.refresh_from_db()
    assert earlier.ended_at is None and user.last_login is None
    assert list(SignInEvent.objects.filter(user=user).values_list("kind", flat=True)) == [
        SignInEvent.Kind.SIGNED_IN
    ]


def test_login_after_password_reset_does_not_borrow_the_previous_signin(
    client, make_user, settings
):
    from accounts.tests.test_review_password_rules import reset_form

    settings.ACCOUNT_LOGIN_ON_PASSWORD_RESET = True
    user = make_user("confirm-reset")
    assert sign_in(client, user.username).status_code == 302
    previous = session_row(client)
    assert previous.factor == SignInFactor.PASSWORD
    user.refresh_from_db()
    form_url = reset_form(client, user)
    fresh = "Linen curtains beside a quiet orchard"
    assert client.post(form_url, {"password1": fresh, "password2": fresh}).status_code == 302
    later = session_row(client)
    assert later.pk != previous.pk and later.user_id == user.pk
    assert later.factor != SignInFactor.PASSWORD, "The reset link did not check a sign-in password"


def test_ordinary_check_after_an_interrupted_reauthentication_uses_its_named_account(
    make_user, rf, monkeypatch
):
    from allauth.account.adapter import DefaultAccountAdapter
    from django.core.exceptions import ValidationError

    from accounts.adapter import ClosedSignupAdapter

    signed_in = make_user("confirm-existing-reader")
    named = make_user("confirm-next-reader")
    request = rf.post("/")
    request.user = signed_in
    adapter = ClosedSignupAdapter()

    def interrupted_check(self, user, password):
        raise ValidationError("Synthetic interrupted check")

    monkeypatch.setattr(DefaultAccountAdapter, "reauthenticate", interrupted_check)
    with pytest.raises(ValidationError, match="Synthetic interrupted check"):
        adapter.reauthenticate(signed_in, "sample")
    adapter.authentication_failed(request, username=named.username)
    assert list(SignInEvent.objects.values_list("user_id", "kind", "factor")) == [
        (named.pk, SignInEvent.Kind.FAILED, SignInFactor.PASSWORD)
    ]
