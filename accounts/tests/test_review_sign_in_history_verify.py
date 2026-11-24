"""Round-one verification of account history and its operator-facing contracts.

Adopted from the verification of review round 1
(.local/review-history/sign-in-history-round-1-verification.md; the original file is kept beside
it). Cases changed while adopting are marked "Adopted:" with the reason."""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from queue import Queue

import pytest
from allauth.account.internal.flows.login import record_authentication
from allauth.account.internal.stagekit import clear_login
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.db import OperationalError, connection, transaction
from django.urls import reverse

from accounts import compromised, session_list, sessions, signin_history
from accounts.acting import hold_actor
from accounts.models import AccountSession, SignInEvent, SignInFactor, User
from accounts.tests.test_login import mailed_code, request_code
from accounts.tests.test_sign_in_history import session_row, sign_in
from core import security_settings, session_limits
from tests.test_review_session_limits_verify import acting_as, age
from tests.test_review_session_list import on_connection, wait_for_block

pytestmark = pytest.mark.django_db
Kind = SignInEvent.Kind
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def fresh_limits():
    cache.clear()
    yield
    cache.clear()


def write_limit(user, request=None, factor=SignInFactor.PASSWORD):
    signin_history.record(user, Kind.LOCKED_OUT, request, factor=factor)


@pytest.mark.django_db(transaction=True)
def test_overlapping_lockout_entries_share_one_minute(make_user, rf):
    user = make_user("verify-minute")
    request = rf.get("/", REMOTE_ADDR="192.0.2.44")
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            write_limit(user.pk, request)
            other = pool.submit(on_connection, lambda: write_limit(user.pk, request), pids)
            # A writer that serializes with this transaction can complete after our commit.
            pid = pids.get(timeout=8)
            deadline = time.monotonic() + 6
            with connection.cursor() as cur:
                while not other.done():
                    cur.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
                    if cur.fetchone()[0]:
                        break
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
        other.result(timeout=10)
    assert SignInEvent.objects.filter(user=user, kind=Kind.LOCKED_OUT).count() == 1


def test_distinct_buckets_and_missing_address(make_user, rf):
    user = make_user("verify-buckets")
    other = make_user("verify-buckets-other")
    known_address = rf.get("/", REMOTE_ADDR="192.0.2.56")
    for _ in range(2):
        write_limit(user)
        write_limit(user, known_address)
        write_limit(user, factor=SignInFactor.SECOND_FACTOR)
        write_limit(other)
    assert SignInEvent.objects.filter(user=user, kind=Kind.LOCKED_OUT).count() == 3
    assert SignInEvent.objects.filter(user=other, kind=Kind.LOCKED_OUT).count() == 1


def test_lockout_happens_after_the_checked_failure_in_one_transaction(make_user):
    user = make_user("verify-event-time")
    with transaction.atomic():
        signin_history.record(user, Kind.FAILED, None, factor=SignInFactor.PASSWORD)
        write_limit(user)
    failed, limited = SignInEvent.objects.filter(user=user).order_by("id")
    assert limited.occurred_at >= failed.occurred_at


def between_transaction_and_statement():
    with connection.cursor() as cur:
        cur.execute("SELECT transaction_timestamp(), statement_timestamp()")
        start, current = cur.fetchone()
    assert current > start
    return start + (current - start) / 2


@pytest.mark.parametrize("window", ["minute", "ninety-days"])
def test_elapsed_window_uses_time_of_decision(make_user, window):
    user = make_user("verify-window")
    with transaction.atomic():
        period = timedelta(seconds=60) if window == "minute" else timedelta(days=90)
        previous = SignInEvent.objects.create(
            user=user,
            kind=Kind.LOCKED_OUT,
            factor=SignInFactor.PASSWORD,
            occurred_at=between_transaction_and_statement() - period,
        )
        if window == "minute":
            write_limit(user)
            assert SignInEvent.objects.filter(user=user, kind=Kind.LOCKED_OUT).count() == 2
        else:
            assert previous.pk not in [e.id for e in signin_history.recent(user).entries]


@pytest.mark.parametrize(
    "module,reader,route",
    [
        (signin_history, "recent", "account-sign-ins"),
        (session_list, "reading", "account-sessions"),
    ],
)
@pytest.mark.xfail(
    strict=True,
    reason="Accepted, display only (predates): the account menu is drawn from the account as the "
    "request read it; the page body from its own reading. The pages the menu offers refuse a "
    "shared demonstration account (accounts.middleware), so nothing changes.",
)
def test_account_menu_agrees_with_shared_reading(
    client, make_user, monkeypatch, module, reader, route
):
    user = make_user("verify-published", "technical")
    client.force_login(user)
    original = getattr(module, reader)

    def after_publication(*args, **kwargs):
        User.objects.filter(pk=user.pk).update(credentials_locked=True)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, reader, after_publication)
    page = client.get(reverse(route))
    assert page.status_code == 200 and page.context["locked"]
    assert f'href="{reverse("account_change_password")}"' not in page.content.decode()


def test_history_fact_tracks_the_form_that_records_current_password(client, make_user, settings):
    user = make_user("verify-form")
    settings.ACCOUNT_FORMS = {
        **settings.ACCOUNT_FORMS,
        "change_password": "allauth.account.forms.ChangePasswordForm",
    }
    client.force_login(user)
    response = client.post(
        reverse("account_change_password"),
        {
            "oldpassword": "incorrect synthetic value",
            "password1": "new synthetic passphrase 471",
            "password2": "new synthetic passphrase 471",
        },
    )
    assert response.context["form"].has_error("oldpassword", code="enter_current_password")
    assert not SignInEvent.objects.filter(user=user, kind=Kind.REAUTHENTICATION_FAILED).exists()
    fact = next(
        f
        for group in security_settings.in_force()
        for f in group.facts
        if f.key == "sign-in-history"
    )
    assert fact.value == security_settings.CHANGED


@pytest.mark.parametrize("next_owner", ["same", "other"])
def test_factor_after_an_allauth_record_list_is_replaced(make_user, rf, next_owner):
    first = make_user("verify-first")
    second = first if next_owner == "same" else make_user("verify-second")
    request = rf.get("/")
    request.session = {}
    record_authentication(request, first, "password")
    assert signin_history.sign_in_factor(request, first) == SignInFactor.PASSWORD
    if next_owner == "same":
        clear_login(request)
    record_authentication(request, second, "code")
    assert signin_history.sign_in_factor(request, second) == SignInFactor.CODE


def test_django_replacement_after_upgrade_does_not_reuse_legacy_proof(client, make_user):
    user = make_user("verify-upgrade")
    sign_in(client, user.username)
    assert session_row(client).factor == SignInFactor.PASSWORD
    # Adopted: no place is kept in the session any more; the factor is read when allauth
    # completes a sign-in, so Django's own login() records none whatever the session holds.
    client.force_login(user)
    assert session_row(client).factor == ""


def test_next_accounts_password_record_survives_django_session_rotation(client, make_user):
    first = make_user("verify-previous")
    second = make_user("verify-next")
    sign_in(client, first.username)
    first.is_active = False
    first.save(update_fields=["is_active"])
    response = sign_in(client, second.username)
    assert response.status_code == 302
    row = session_row(client)
    assert row.user_id == second.pk and row.ended_at is None
    assert row.factor == SignInFactor.PASSWORD


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("occasion", ["same-browser", "expired-in-other-browser", "refused"])
def test_replacement_waits_for_acting_session_before_holding_assignment_account(
    make_user, rf, occasion
):
    user = make_user("verify-assignee", "technical")
    marked = occasion == "refused"
    if marked:
        owner = make_user("verify-owner", is_superuser=True, is_staff=True)
        compromised.mark_compromised(
            actor=owner,
            account_id=user.pk,
            reason="Synthetic test",
            drawn=compromised.drawn_password(user),
        )
    # An open session, as after this account signed in with an emailed code.
    older = AccountSession.objects.create(user=user, factor=SignInFactor.CODE)
    request = rf.get("/")
    cleanup = occasion == "expired-in-other-browser"
    request.session = {} if cleanup else {sessions.SESSION_KEY: str(older.pk)}
    if cleanup:
        admin = make_user("verify-limit-owner", "admin")
        age(older, started=timedelta(hours=1), active=timedelta(minutes=10))

    def shorten_limit():
        session_limits.save_limits(
            actor=admin,
            audience="staff",
            new={"inactivity_minutes": 5, "maximum_hours": 12},
            expected_revision=0,
            reason="Synthetic limit change",
        )

    record_authentication(request, user, "password")
    pids = Queue()
    available = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic(), acting_as(older):
            assert hold_actor(user).pk == user.pk
            if cleanup:
                pool.submit(on_connection, shorten_limit, Queue()).result(timeout=10)
            replacement = pool.submit(
                on_connection, lambda: sessions.start_session(User, request, user), pids
            )
            wait_for_block(replacement, pids)
            # Inspect the account lock with NOWAIT. No reverse wait or deadlock is created.
            try:
                with transaction.atomic():
                    User.objects.select_for_update(no_key=True, nowait=True).only("id").get(
                        pk=user.pk
                    )
            except OperationalError as exc:
                assert exc.__cause__.sqlstate == "55P03"
                available = False
        replacement.result(timeout=10)
    row = AccountSession.objects.get(pk=request.session[sessions.SESSION_KEY])
    assert bool(row.ended_at) == marked
    assert available, "A session holder that next assigns to itself still needs this account lock"


def test_history_summary_qualifies_ambiguous_names():
    guide = json.loads((ROOT / "docs/pages/accounts/sign-in-history.json").read_text())
    assert not any(
        "An attempt for a username" in item and "is not listed for anyone" in item
        for item in guide["good_to_know"]
    )


def test_shared_help_allows_contacting_the_laboratory():
    guide = json.loads((ROOT / "docs/pages/accounts/sign-in-history.json").read_text())
    for task in guide["tasks"]:
        for step in task["steps"]:
            assert not ("tell your laboratory" in step and "cannot do either" in step)


def test_signed_in_login_form_files_failures_against_its_named_account(client, make_user, settings):
    # allauth supports drawing the sign-in form in a signed-in browser; this project
    # exercises the same setting in test_review_required_second_factor.
    settings.ACCOUNT_AUTHENTICATED_LOGIN_REDIRECTS = False
    settings.ACCOUNT_RATE_LIMITS = {"login_failed": "1/m/key"}
    signed_in = make_user("verify-already-signed-in")
    named = make_user("verify-login-form-account")
    client.force_login(signed_in)
    for _ in range(2):
        assert sign_in(client, named.username, "incorrect synthetic value").status_code == 200
    history = list(
        SignInEvent.objects.exclude(kind=Kind.SIGNED_IN)
        .order_by("id")
        .values_list("user_id", "kind")
    )
    assert history == [(named.pk, Kind.FAILED), (named.pk, Kind.LOCKED_OUT)]


@pytest.mark.parametrize("surface", ["account", "admin"])
def test_operations_describes_which_limit_counts_current_passwords(
    client, make_user, settings, surface
):
    settings.ACCOUNT_RATE_LIMITS = {"login_failed": "1/m/key"}
    user = make_user("verify-password-volume", is_staff=True, is_superuser=True)
    client.force_login(user)
    if surface == "account":
        url = reverse("account_change_password")
        old, new, repeated = "oldpassword", "password1", "password2"
    else:
        url = reverse("admin:password_change")
        old, new, repeated = "old_password", "new_password1", "new_password2"
    data = {
        old: "incorrect synthetic current value",
        new: "new synthetic value 471",
        repeated: "new synthetic value 471",
    }
    for _ in range(2):
        response = client.post(url, data)
        assert response.status_code == 200 and response.context["form"].has_error(old)
    checked = SignInEvent.objects.filter(user=user, kind=Kind.REAUTHENTICATION_FAILED).count()
    operations = " ".join((ROOT / "docs/operations.md").read_text().split())
    if checked > 1:
        assert "failed attempts that are checked stay bounded by that limit" not in operations


@pytest.mark.parametrize("route", ["accounts/login", "accounts/login-code"])
def test_sign_in_help_limits_its_storage_claim_to_history_entries(
    client, make_user, settings, route
):
    user = make_user("verify-spelling")
    if route == "accounts/login":
        typed = user.username.upper()
        sign_in(client, typed)
        claim = "Neither the password nor what was typed in Login is kept"
    else:
        settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
        typed = user.email
        requested = request_code(client, typed)
        client.post(requested.url, {"code": mailed_code()})
        claim = "Neither the code nor the email address typed is kept"
    session = Session.objects.get(session_key=client.session.session_key).get_decoded()
    assert session["_auth_user_id"] == str(user.pk)
    assert typed in str(session["account_authentication_methods"])
    help_text = (ROOT / f"docs/pages/{route}.json").read_text()
    assert claim not in help_text


def test_identified_code_failure_agrees_with_changelog(client, make_user, settings):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    namesake = make_user("verify-overlap@example.invalid")
    recipient = make_user("verify-code-recipient")
    recipient.email = namesake.username
    recipient.save(update_fields=["email"])
    assert signin_history.account_named({"email": recipient.email}) is None
    requested = request_code(client, recipient.email)
    response = client.post(requested.url, {"code": "unused synthetic value"})
    assert response.status_code == 200
    assert list(SignInEvent.objects.values_list("user_id", "kind", "factor")) == [
        (recipient.pk, Kind.FAILED, SignInFactor.CODE)
    ]
    change = (ROOT / "changelog.d/2026-11-23-sign-in-history.md").read_text()
    blanket_claim = (
        "Failed passwords, codes and lockouts are recorded only for the one account "
        "the username or email address names"
    )
    assert blanket_claim not in change


def test_empty_shared_reading_keeps_the_account_flag(make_user):
    user = make_user("verify-empty-shared", credentials_locked=True)
    history = signin_history.recent(user)
    opened, _now, locked = session_list.reading(user)
    assert history.locked and locked
    assert history.entries == [] and opened == []


def test_late_refusal_is_logged_only_at_commit(
    make_user, rf, django_capture_on_commit_callbacks, caplog
):
    user = make_user("verify-deactivated")
    user.is_active = False
    user.save(update_fields=["is_active"])
    request = rf.get("/", REMOTE_ADDR="192.0.2.76", HTTP_USER_AGENT="Review browser")
    request.session = {}
    record_authentication(request, user, "code")
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        with django_capture_on_commit_callbacks(execute=True):
            sessions.start_session(User, request, user)
            assert not [r for r in caplog.records if r.name == "lab_ops.signin"]
    assert [r.getMessage().split()[0] for r in caplog.records if r.name == "lab_ops.signin"] == [
        "signout.deactivated"
    ]
    row = AccountSession.objects.get(pk=request.session[sessions.SESSION_KEY])
    event = SignInEvent.objects.get(user=user)
    assert event.kind == Kind.REFUSED and event.session == row.pk
    assert event.ending == row.ending == AccountSession.Ending.DEACTIVATED
    # Adopted: a direct call of the receiver did not go through allauth's completion of the
    # sign-in (ClosedSignupAdapter.login), which reads the factor, so none is recorded.
    assert event.factor == ""
    assert event.address == "192.0.2.76" and event.user_agent == "Review browser"
    assert event.occurred_at == row.started_at
    user.refresh_from_db()
    assert user.last_login is None


def test_fresh_password_after_allauth_clears_a_refused_login(client, make_user, settings):
    settings.ACCOUNT_AUTHENTICATED_LOGIN_REDIRECTS = False
    user = make_user("verify-returning")
    inactive = make_user("verify-inactive", is_active=False)
    sign_in(client, user.username)
    first = session_row(client)
    assert first.factor == SignInFactor.PASSWORD
    assert sign_in(client, inactive.username).url == reverse("account_inactive")
    assert sign_in(client, user.username).status_code == 302
    later = session_row(client)
    assert later.user_id == user.pk and later.pk != first.pk
    assert later.factor == SignInFactor.PASSWORD


def test_factor_readers_agree_on_the_current_sign_in(make_user, rf):
    """Compare the two descriptions of proof using only synthetic method metadata."""
    user = make_user("verify-factor-readers")
    request = rf.get("/")
    request.session = {}
    record_authentication(request, user, "code")
    assert signin_history.sign_in_factor(request, user) == SignInFactor.CODE
    record_authentication(request, user, "password")
    history_factor = signin_history.sign_in_factor(request, user)
    assert history_factor == SignInFactor.PASSWORD
    assert not compromised.proved_beyond_password(request, user)


def test_operations_qualifies_which_log_events_survive_an_upgrade(
    client, make_user, settings, caplog
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    user = make_user("verify-log-contract")
    requested = request_code(client, user.email)
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        page = client.post(requested.url, {"code": " "})
    assert page.context["form"].has_error("code", code="required")
    # Adopted: the sign-in log keeps recording every attempt allauth counts against the code,
    # an empty one included (tests/test_review_security_round1_confirm3.py pins it), so the
    # operations page must not say the log lines are unchanged.
    assert [r.getMessage().split()[0] for r in caplog.records if r.name == "lab_ops.signin"] == [
        "signin.failed"
    ]
    operations = " ".join((ROOT / "docs/operations.md").read_text().split())
    assert "The sign-in log lines are unchanged." not in operations


def test_inactive_sessionless_callback_keeps_its_refusal(make_user, rf):
    from django.contrib.auth.signals import user_logged_in

    user = make_user("verify-sessionless")
    earlier_read = User.objects.get(pk=user.pk)
    user.is_active = False
    user.save(update_fields=["is_active"])
    request = rf.get("/")
    assert not hasattr(request, "session")
    user_logged_in.send(sender=User, request=request, user=earlier_read)
    assert request.vr_sign_in_refused and not AccountSession.objects.filter(user=user).exists()
    assert list(SignInEvent.objects.filter(user=user).values_list("kind", "ending", "session")) == [
        (Kind.REFUSED, AccountSession.Ending.DEACTIVATED, None)
    ]


def test_ambiguous_address_refusal_belongs_to_the_accepted_code(client, make_user, settings):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    namesake = make_user("verify-code-refusal@example.invalid")
    recipient = make_user("verify-code-refusal")
    recipient.email = namesake.username
    recipient.save(update_fields=["email"])
    assert signin_history.account_named({"email": recipient.email}) is None
    confirmation = request_code(client, recipient.email)
    code = mailed_code()
    recipient.is_active = False
    recipient.save(update_fields=["is_active"])
    response = client.post(confirmation.url, {"code": code})
    assert response.url == reverse("account_inactive")
    assert list(SignInEvent.objects.values_list("user_id", "kind", "ending", "session")) == [
        (recipient.pk, Kind.REFUSED, AccountSession.Ending.DEACTIVATED, None)
    ]


@pytest.mark.django_db(transaction=True)
def test_committed_lockouts_resume_after_a_minute_in_the_same_bucket(make_user, rf):
    from django.db.models.functions import Now

    user = make_user("verify-elapsed")
    address = "192.0.2.72"
    SignInEvent.objects.create(
        user=user,
        kind=Kind.LOCKED_OUT,
        factor=SignInFactor.PASSWORD,
        address=address,
        occurred_at=Now() - timedelta(seconds=61),
    )
    request = rf.get("/", REMOTE_ADDR=address)
    write_limit(user, request)
    write_limit(user, request)
    assert SignInEvent.objects.filter(user=user, kind=Kind.LOCKED_OUT, address=address).count() == 2


def test_history_fact_reports_an_overridden_mfa_form(settings):
    settings.MFA_FORMS = {}
    fact = next(
        f
        for group in security_settings.in_force()
        for f in group.facts
        if f.key == "sign-in-history"
    )
    assert fact.value == security_settings.CHANGED and "MFA_FORMS" in fact.note
