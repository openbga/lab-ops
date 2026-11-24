"""Independent sign-in history regressions with synthetic accounts and PostgreSQL.

Adopted from review round 1 (.local/review-history/sign-in-history-round-1.md; the original file is
kept beside it). Cases changed while adopting are marked "Adopted:" with the reason."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
from allauth.account.internal.flows import code_verification
from allauth.account.models import EmailAddress
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.db import connection, connections
from django.db.models.functions import Now
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts import sessions
from accounts.models import AccountSession, User
from accounts.roles import ROLES
from accounts.tests.conftest import PASSWORD
from accounts.tests.test_login import mailed_code, request_code
from accounts.tests.test_second_factor_enrolment import code_for, require, sign_in

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def fresh_review_limits(db):
    cache.clear()
    yield
    cache.clear()


def event_fields(user):
    from accounts.models import SignInEvent

    return list(
        SignInEvent.objects.filter(user=user).order_by("id").values_list("kind", "factor", "ending")
    )


@pytest.mark.xfail(
    strict=True,
    reason="Accepted, predates: the sign-in log records every attempt allauth counts against an "
    "emailed code, an empty one included (tests/test_review_security_round1_confirm3.py pins it); "
    "the sign-in history records only a code entered.",
)
def test_empty_email_confirmation_is_required_input_not_a_rejected_code(
    client, make_user, settings, caplog
):
    """The operational log identifies wrong codes. A required-field error checked no code."""
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    user = make_user("review-missing-code")
    requested = request_code(client, user.email)
    assert requested.url == reverse("account_confirm_login_code")
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        rejected = client.post(requested.url, {"code": ""})
    assert rejected.context["form"].has_error("code", code="required")
    assert [r.getMessage() for r in caplog.records if r.name == "lab_ops.signin"] == []


def test_empty_email_confirmation_does_not_claim_a_code_was_rejected(client, make_user, settings):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    user = make_user("review-empty-history")
    requested = request_code(client, user.email)
    rejected = client.post(requested.url, {"code": ""})
    assert rejected.context["form"].has_error("code", code="required")
    assert event_fields(user) == []


def test_request_resend_and_expired_email_code_leave_no_history(
    client, make_user, settings, monkeypatch
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    user = make_user("review-expired-history")
    clock = SimpleNamespace(time=lambda: 1000)
    monkeypatch.setattr(code_verification, "time", clock)
    step = request_code(client, user.email).url
    assert client.post(step, {"action": "resend"}).url == step
    code = mailed_code()
    clock.time = lambda: 1000 + settings.ACCOUNT_LOGIN_BY_CODE_TIMEOUT + 1
    assert client.post(step, {"code": code}).url == reverse("account_request_login_code")
    assert "_auth_user_id" not in client.session
    assert event_fields(user) == []


@pytest.mark.xfail(
    strict=True,
    reason="Accepted, predates: allauth keeps the submitted spelling in the browser's saved "
    "session until it signs out; the documents now say only that no history entry holds it.",
)
def test_session_storage_does_not_keep_the_submitted_spelling(client, make_user):
    """Check the brief's database-wide wording against this browser's own saved session."""
    user = make_user("review-session-spelling")
    submitted = user.username.upper()
    response = client.post(reverse("account_login"), {"login": submitted, "password": PASSWORD})
    assert response.status_code == 302 and client.session["_auth_user_id"] == str(user.pk)
    stored = Session.objects.get(session_key=client.session.session_key).get_decoded()
    found = submitted in str(stored)
    assert not found, "the saved session contains the submitted spelling"


def test_history_page_and_log_omit_form_values(client, make_user, caplog, settings):
    from accounts.models import SignInEvent

    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    user = make_user("review-canonical-name")
    submitted = user.username.upper()
    incorrect_password = "a synthetic incorrect passphrase"
    incorrect_code = "synthetic-wrong-code"
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        client.post(reverse("account_login"), {"login": submitted, "password": incorrect_password})
        step = request_code(client, user.email.upper()).url
        client.post(step, {"code": incorrect_code})
        code = mailed_code()
        assert client.post(step, {"code": code}).status_code == 302
        assert client.session["_auth_user_id"] == str(user.pk)
    assert event_fields(user) == [
        ("failed", "password", ""),
        ("failed", "code", ""),
        ("signed_in", "code", ""),
    ]
    body = client.get(reverse("account-sign-ins")).content.decode()
    entries = list(SignInEvent.objects.filter(user=user).values())
    log_lines = [r.getMessage() for r in caplog.records if r.name == "lab_ops.signin"]
    for value in (
        submitted,
        user.email.upper(),
        incorrect_password,
        incorrect_code,
        code,
        PASSWORD,
    ):
        assert value not in body and value not in str(entries) and value not in str(log_lines)


def test_inactive_refusal_is_recorded_for_the_account_whose_password_was_accepted(
    client, make_user
):
    """Adopted: the password accepted names the account whatever else the name typed matches;
    the documents now say so (a failed attempt still needs a name that matches one account)."""
    first = make_user("review-ambiguous@example.invalid")
    second = make_user("review-email-owner", is_active=False)
    second.email = first.username
    second.set_password("independent review fixture password")
    second.save(update_fields=["email", "password"])
    response = client.post(
        reverse("account_login"),
        {
            "login": first.username,
            "password": "independent review fixture password",
        },
    )
    assert response.url == reverse("account_inactive") and "_auth_user_id" not in client.session
    assert event_fields(first) == []
    assert event_fields(second) == [("refused", "", "deactivated")]


def test_reauthentication_records_a_password_the_signed_in_form_did_not_accept(client, make_user):
    """An email-shaped username and another person's email can legally coincide."""
    user = make_user("review-same@example.invalid")
    other = make_user("review-other-owner")
    other.email = user.username
    other.set_password("synthetic separate account passphrase")
    other.save(update_fields=["email", "password"])
    client.force_login(user)
    response = client.post(
        reverse("account_reauthenticate"),
        {
            "password": "synthetic separate account passphrase",
        },
    )
    assert response.status_code == 200
    assert response.context["form"].has_error("password", code="incorrect_password")
    assert client.session["_auth_user_id"] == str(user.pk)
    assert event_fields(other) == []
    assert event_fields(user) == [("signed_in", "", ""), ("reauth_failed", "password", "")]


def test_current_password_rejection_on_change_password_is_recorded(client, make_user):
    user = make_user("review-current-password")
    client.force_login(user)
    response = client.post(
        reverse("account_change_password"),
        {
            "oldpassword": "unused fixture password",
            "password1": "new synthetic passphrase 471",
            "password2": "new synthetic passphrase 471",
        },
    )
    assert response.status_code == 200
    assert response.context["form"].has_error("oldpassword", code="enter_current_password")
    assert event_fields(user) == [("signed_in", "", ""), ("reauth_failed", "password", "")]


def test_inactive_jobs_account_records_its_refusal_without_starting_a_session(client):
    from jobs.actor import system_account

    user = system_account()
    assert not user.is_active
    user.set_password(PASSWORD)
    user.save(update_fields=["password"])
    response = sign_in(client, user)
    assert response.url == reverse("account_inactive")
    assert (
        "_auth_user_id" not in client.session
        and not AccountSession.objects.filter(user=user).exists()
    )
    assert [kind for kind, _, _ in event_fields(user)] == ["refused"]


@pytest.mark.parametrize("step", ["password", "second_factor"])
def test_reauthentication_lockout_records_the_step_checked(client, make_user, settings, step):
    user = make_user("review-reauth-limits", "technical")
    client.force_login(user)
    if step == "second_factor":
        secret = generate_totp_secret()
        TOTP.activate(user, secret)
        url = reverse("mfa_reauthenticate")
        first, second = {"code": "not-a-numeric-code"}, {"code": code_for(secret)}
    else:
        url = reverse("account_reauthenticate")
        first, second = {"password": "incorrect fixture"}, {"password": PASSWORD}
    settings.ACCOUNT_RATE_LIMITS = {"login_failed": "1/m/key"}
    assert client.post(url, first).status_code == client.post(url, second).status_code == 200
    assert event_fields(user) == [
        ("signed_in", "", ""),
        ("reauth_failed", step, ""),
        ("locked_out", step, ""),
    ]


def test_password_history_writes_with_separate_form_and_request_limits(client, make_user, settings):
    """A small counter example with both limits visible in the same four requests.

    Adopted: a refusal by the limit is recorded at most once a minute for each account, step and
    address (signin_history.LOCKOUT_EVERY), so the second refusal adds no row."""
    settings.ACCOUNT_RATE_LIMITS = {"login": "3/m/ip", "login_failed": "1/m/key"}
    user = make_user("review-accounting")
    statuses = []
    for _ in range(4):
        response = client.post(
            reverse("account_login"), {"login": user.username, "password": "wrong"}
        )
        statuses.append(response.status_code)
    assert statuses == [200, 200, 200, 429]
    assert event_fields(user) == [
        ("failed", "password", ""),
        ("locked_out", "password", ""),
    ]


@pytest.mark.parametrize("role", [*ROLES, "superuser", "portal", "no-role"])
def test_each_audience_reads_only_its_own_entries(client, make_user, role):
    from accounts.models import SignInEvent
    from clients.models import Client as Organization

    if role == "portal":
        user = make_user("review-reader", client=Organization.objects.create(name="Review Clinic"))
    elif role == "superuser":
        user = make_user("review-reader", is_superuser=True, is_staff=True)
    elif role == "no-role":
        user = make_user("review-reader")
    else:
        user = make_user("review-reader", role)
    other = make_user("review-other-reader")
    SignInEvent.objects.create(user=other, kind="failed", factor="password", address="192.0.2.71")
    client.force_login(user)
    page = client.get(reverse("account-sign-ins"))
    assert page.status_code == 200 and "no-store" in page["Cache-Control"]
    assert [e.id for e in page.context["entries"]] == [SignInEvent.objects.get(user=user).pk]
    assert b"192.0.2.71" not in page.content


@pytest.mark.parametrize("gate", ["second-factor", "new-password"])
def test_held_sessions_return_to_the_required_step(client, make_user, gate):
    from accounts import compromised

    user = make_user("review-held-person", "technical")
    if gate == "second-factor":
        require(staff=True)
        assert sign_in(client, user).status_code == 302
        destination = reverse("account-second-factor")
    else:
        owner = make_user("review-mark-owner", is_superuser=True, is_staff=True)
        secret = generate_totp_secret()
        TOTP.activate(user, secret)
        compromised.mark_compromised(
            actor=owner,
            account_id=user.pk,
            reason="Synthetic review mark",
            drawn=compromised.drawn_password(user),
        )
        challenge = sign_in(client, user)
        assert challenge.url == reverse("mfa_authenticate")
        assert client.post(challenge.url, {"code": code_for(secret)}).status_code == 302
        destination = reverse("account-new-password")
    history = reverse("account-sign-ins")
    assert client.get(history).url == destination
    assert f'href="{history}"' not in client.get(destination).content.decode()


@pytest.mark.parametrize("route", ["account-sign-ins", "account-sessions"])
@pytest.mark.django_db(transaction=True)
def test_shared_notice_reflects_the_account_when_the_page_is_read(
    client, make_user, monkeypatch, route
):
    """Change one synthetic account before the event query. Check only the policy notice."""
    from accounts import session_list

    if route == "account-sign-ins":
        from accounts import signin_history

        module, reader = signin_history, "recent"
    else:
        module, reader = session_list, "reading"
    user = make_user("review-published", "technical")
    client.force_login(user)
    original = getattr(module, reader)

    def publish():
        try:
            return User.objects.filter(pk=user.pk).update(credentials_locked=True)
        finally:
            connections.close_all()

    def read_after_publish(*args, **kwargs):
        with ThreadPoolExecutor(max_workers=1) as worker:
            assert worker.submit(publish).result(timeout=10) == 1
        return original(*args, **kwargs)

    monkeypatch.setattr(module, reader, read_after_publish)
    page = client.get(reverse(route))
    assert page.status_code == 200
    assert page.context["locked"], "the new shared-account setting should govern this reading"


def test_history_window_independently_of_the_entry_limit(client, make_user):
    from accounts.models import SignInEvent

    user = make_user("review-cutoff", "technical")
    client.force_login(user)
    now = timezone.now()
    recent = SignInEvent.objects.create(
        user=user,
        kind="failed",
        factor="code",
        occurred_at=now - timedelta(days=90) + timedelta(minutes=1),
    )
    SignInEvent.objects.create(
        user=user, kind="failed", factor="code", occurred_at=now - timedelta(days=90, minutes=1)
    )
    page = client.get(reverse("account-sign-ins"))
    own_start = SignInEvent.objects.get(user=user, kind="signed_in")
    assert [e.id for e in page.context["entries"]] == [own_start.pk, recent.pk]
    assert page.context["attention_count"] == 1


def test_bounded_page_queries_and_warning_at_full_capacity(client, make_user):
    from accounts.models import SignInEvent

    user = make_user("review-full-history", "technical")
    client.force_login(user)
    now = timezone.now()
    SignInEvent.objects.bulk_create(
        [
            SignInEvent(user=user, kind="failed", factor="password", occurred_at=now)
            for _ in range(103)
        ]
    )
    url = reverse("account-sign-ins")
    client.get(url)
    with CaptureQueriesContext(connection) as queries:
        page = client.get(url)
    assert len(queries) <= 25
    ids = list(
        SignInEvent.objects.filter(user=user, kind="failed")
        .order_by("-id")
        .values_list("id", flat=True)[:100]
    )
    assert [e.id for e in page.context["entries"]] == ids
    assert page.context["attention_count"] == 100
    assert b"100 attempts listed below were not let through" in page.content


def test_empty_state_for_a_reading_without_events(rf, make_user):
    from accounts.session_views import sign_in_history_page

    user = make_user("review-pre-upgrade", "technical")
    request = rf.get("/account/sign-ins/")
    request.user = user
    from django.contrib.sessions.middleware import SessionMiddleware

    SessionMiddleware(lambda r: None).process_request(request)
    page = sign_in_history_page(request)
    assert page.status_code == 200
    assert b"Nothing recorded yet" in page.content and b"<table" not in page.content


def test_browser_description_is_classified_before_display(make_user):
    from accounts.models import SignInEvent

    user = make_user("review-browser-label", "technical")
    description = "Review & Research <Synthetic-Device> Firefox/140.0 Windows NT"
    http = Client(HTTP_USER_AGENT=description, REMOTE_ADDR="192.0.2.40")
    assert sign_in(http, user).status_code == 302
    assert SignInEvent.objects.get(user=user).user_agent == description
    page = http.get(reverse("account-sign-ins"))
    assert page.context["entries"][0].browser == "Firefox on Windows"
    assert b"Synthetic-Device" not in page.content


def test_last_resend_from_a_second_tab_is_not_an_emailed_code_failure(client, make_user, settings):
    """Two tabs show the last resend button before either uses it."""
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    user = make_user("review-two-tabs")
    step = request_code(client, user.email).url
    assert client.post(step, {"action": "resend"}).url == step
    for _tab in range(2):
        assert client.get(step).context["can_resend"]
    assert client.post(step, {"action": "resend"}).url == step
    assert not client.get(step).context["can_resend"]
    client.post(step, {"action": "resend"})
    assert event_fields(user) == []


def test_history_marks_only_the_sign_in_of_the_session_identified_as_current(client, make_user):
    from accounts import signin_history

    user = make_user("review-marker")
    client.force_login(user)
    row = AccountSession.objects.get(user=user)
    client.post(reverse("account_logout"))
    # The reading carries the session id obtained before this session finished.
    # Adopted: recent() returns a Reading, whose entries are the list.
    entries = signin_history.recent(user, current=row.pk).entries
    assert [(e.kind, e.this_session) for e in entries] == [
        ("signed_out", False),
        ("signed_in", True),
    ]


def test_help_accounts_for_a_code_refused_after_deactivation(client, make_user, settings):
    from accounts.models import SignInEvent

    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    user = make_user("review-pending-email")
    step = request_code(client, user.email).url
    code = mailed_code()
    user.is_active = False
    user.save(update_fields=["is_active"])
    assert client.post(step, {"code": code}).url == reverse("account_inactive")
    assert "_auth_user_id" not in client.session
    refused = SignInEvent.objects.get(user=user)
    assert (refused.kind, refused.ending, refused.session) == ("refused", "deactivated", None)

    reader = make_user("review-guide-reader")
    client.force_login(reader)
    guide = client.get(reverse("account-sign-ins")).context["page_documentation"]
    description = next(
        item["description"]
        for section in guide["sections"]
        for item in section.get("items", [])
        if item["label"] == "Sign-in refused"
    )
    assert (
        "the password was right" not in description.lower() or "emailed code" in description.lower()
    )


def test_default_password_limit_counts_checked_attempts_and_refusals(client, make_user):
    user = make_user("review-default-limits")
    for _ in range(6):
        assert (
            client.post(
                reverse("account_login"), {"login": user.username, "password": "wrong"}
            ).status_code
            == 200
        )
    assert event_fields(user) == [("failed", "password", "")] * 5 + [("locked_out", "password", "")]


def assert_review_session_start(http, user, factor):
    from accounts.models import AccountSession, SignInEvent
    from accounts.sessions import SESSION_KEY

    assert http.session["_auth_user_id"] == str(user.pk)
    row = AccountSession.objects.get(pk=http.session[SESSION_KEY])
    start = SignInEvent.objects.get(session=row.pk, kind="signed_in")
    assert start.user_id == row.user_id == user.pk
    assert (start.occurred_at, start.factor, start.ending) == (row.started_at, factor, "")
    assert row.factor == factor
    assert start.user_agent == row.user_agent == "Review browser"
    assert start.address == row.address == "192.0.2.37"
    return row


@pytest.mark.parametrize("second", ["totp", "recovery"])
def test_emailed_code_and_second_factor_have_one_start_after_both_steps(
    make_user, settings, second
):
    from accounts.models import SignInEvent

    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    user = make_user("review-email-second", "technical")
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    recoveries = RecoveryCodes.activate(user).get_unused_codes()
    http = Client(HTTP_USER_AGENT="Review browser", REMOTE_ADDR="192.0.2.37")
    requested = request_code(http, user.email)
    assert requested.url == reverse("account_confirm_login_code")
    stage = http.post(requested.url, {"code": mailed_code()})
    assert stage.url == reverse("mfa_authenticate")
    assert not SignInEvent.objects.filter(user=user).exists()

    code = code_for(secret) if second == "totp" else recoveries[0]
    assert http.post(stage.url, {"code": code}).status_code == 302
    assert_review_session_start(http, user, "code_" + second)
    assert SignInEvent.objects.filter(user=user).count() == 1


def test_admin_sign_in_and_sign_out_keep_factors_across_two_visits(make_user):
    from allauth.mfa.models import Authenticator

    from accounts.models import SignInEvent
    from accounts.tests.conftest import PASSWORD

    user = make_user("review-admin-factor", "admin", is_staff=True, is_superuser=True)
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    http = Client(HTTP_USER_AGENT="Review browser", REMOTE_ADDR="192.0.2.37")
    required = http.get(reverse("admin:index"))
    app_sign_in = http.get(required.url)
    assert app_sign_in.url.startswith(reverse("account_login"))
    challenge = http.post(app_sign_in.url, {"login": user.username, "password": PASSWORD})
    assert challenge.url == reverse("mfa_authenticate")
    assert not SignInEvent.objects.filter(user=user).exists()
    complete = http.post(challenge.url, {"code": code_for(secret)})
    assert http.get(complete.url).status_code == 200
    assert complete.url == reverse("admin:index")
    first = assert_review_session_start(http, user, "password_totp")

    assert http.post(reverse("admin:logout")).status_code == 200
    first.refresh_from_db()
    Authenticator.objects.filter(user=user).delete()
    assert sign_in(http, user).status_code == 302
    after_logout = assert_review_session_start(http, user, "password")
    assert first.pk != after_logout.pk
    assert list(
        SignInEvent.objects.filter(user=user)
        .order_by("id")
        .values_list("kind", "session", "factor", "ending", "occurred_at")
    ) == [
        ("signed_in", first.pk, "password_totp", "", first.started_at),
        ("signed_out", first.pk, "", "signed_out", first.ended_at),
        ("signed_in", after_logout.pk, "password", "", after_logout.started_at),
    ]


def test_a_replacement_django_session_with_no_steps_has_an_unknown_factor(make_user):
    from accounts.models import AccountSession, SignInEvent
    from accounts.sessions import SESSION_KEY

    user = make_user("review-new-session-proof", "technical")
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    http = Client(HTTP_USER_AGENT="Review browser", REMOTE_ADDR="192.0.2.37")
    challenge = sign_in(http, user)
    assert challenge.url == reverse("mfa_authenticate")
    assert http.post(challenge.url, {"code": code_for(secret)}).status_code == 302
    first = assert_review_session_start(http, user, "password_totp")

    # A Django sign-in without an allauth step, as in the shipped forced-sign-in test.
    http.force_login(user)
    second = AccountSession.objects.get(pk=http.session[SESSION_KEY])
    assert first.pk != second.pk
    assert list(
        SignInEvent.objects.filter(user=user)
        .order_by("id")
        .values_list("kind", "session", "factor", "ending")
    ) == [
        ("signed_in", first.pk, "password_totp", ""),
        ("signed_out", first.pk, "", "signed_in_again"),
        ("signed_in", second.pk, "", ""),
    ]


@pytest.mark.django_db(transaction=True)
def test_upgrade_keeps_preexisting_sessions_and_accepts_the_old_model():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from django.utils import timezone

    from accounts.models import AccountSession, SignInEvent, User

    previous = [("accounts", "0008_compromised_password")]
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    old_session = executor.loader.project_state(previous).apps.get_model(
        "accounts", "AccountSession"
    )
    executor.migrate(previous)
    try:
        user = User.objects.create_user("review-history-upgrade")
        old_fields = {"user_id": user.pk, "address": "192.0.2.18", "user_agent": "Synthetic Tablet"}
        opened = old_session.objects.create(**old_fields)
        ended = old_session.objects.create(
            **old_fields, ending="signed_out", ended_at=timezone.now()
        )
        before = list(old_session.objects.filter(user_id=user.pk).order_by("id").values())
    finally:
        MigrationExecutor(connection).migrate(latest)

    after = list(AccountSession.objects.filter(user_id=user.pk).order_by("id").values())
    assert [record.pop("factor") for record in after] == ["", ""]
    assert after == before
    histories = SignInEvent.objects.filter(user_id=user.pk)
    assert not histories.exists()

    # A process still running 0008 writes all the columns it knows; PostgreSQL supplies factor.
    mixed = old_session.objects.create(**old_fields)
    finish = timezone.now()
    assert (
        old_session.objects.filter(pk__in=(opened.pk, mixed.pk)).update(
            ended_at=finish, ending="signed_out"
        )
        == 2
    )
    columns = ("session", "kind", "occurred_at", "factor", "ending", "user_agent", "address")
    observed = set(histories.values_list(*columns))
    browser = (old_fields["user_agent"], old_fields["address"])
    assert observed == {
        (mixed.pk, "signed_in", mixed.started_at, "", "", *browser),
        (mixed.pk, "signed_out", finish, "", "signed_out", *browser),
        (opened.pk, "signed_out", finish, "", "signed_out", *browser),
    }
    assert histories.count() == len(observed)
    assert not histories.filter(session=ended.pk).exists()

    MigrationExecutor(connection).migrate(previous)
    try:
        assert SignInEvent._meta.db_table not in connection.introspection.table_names()
        assert set(old_session.objects.filter(user_id=user.pk).values_list("pk", flat=True)) == {
            opened.pk,
            ended.pk,
            mixed.pk,
        }
        while_reversed = old_session.objects.create(**old_fields)
    finally:
        MigrationExecutor(connection).migrate(latest)
    assert not histories.exists()
    old_session.objects.filter(pk=while_reversed.pk).update(ended_at=finish, ending="signed_out")
    assert list(histories.values_list("session", "kind")) == [(while_reversed.pk, "signed_out")]


def test_session_transition_and_its_history_roll_back_together():
    from django.db import transaction
    from django.utils import timezone

    from accounts.models import AccountSession, SignInEvent, User

    user = User.objects.create_user("review-history-rollback")
    history = SignInEvent.objects.filter(user=user)
    with transaction.atomic():
        aborted = AccountSession.objects.create(user=user, factor="password")
        assert history.get(session=aborted.pk).kind == "signed_in"
        transaction.set_rollback(True)
    assert not AccountSession.objects.filter(user=user).exists() and not history.exists()

    persisted = AccountSession.objects.create(user=user, factor="password")
    with transaction.atomic():
        AccountSession.objects.filter(pk=persisted.pk).update(
            ending="administrator", ended_at=timezone.now()
        )
        assert history.get(session=persisted.pk, kind="signed_out").ending == "administrator"
        transaction.set_rollback(True)
    persisted.refresh_from_db()
    assert persisted.ended_at is None and persisted.ending == ""
    assert list(history.values_list("session", "kind")) == [(persisted.pk, "signed_in")]


def test_demo_seed_preserves_sign_in_history_and_reset_restarts_it(monkeypatch):
    from io import StringIO

    from django.core.management import call_command
    from django.test import Client
    from django.urls import reverse

    from accounts.models import AccountSession, SignInEvent, User

    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-review-demo-password")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "synthetic-review-owner-password")
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    options = {"announce_accounts": True, "stdout": StringIO()}
    call_command("seed_demo", **options)
    account = User.objects.get(username="demo_technical")
    browser = Client()
    browser.force_login(account)
    browser.post(reverse("account_logout"))

    captured = list(SignInEvent.objects.order_by("id").values())
    assert [row["kind"] for row in captured] == ["signed_in", "signed_out"]
    call_command("seed_demo", **options)
    assert list(SignInEvent.objects.order_by("id").values()) == captured
    call_command("reset_demo", interactive=False, **options)
    assert not SignInEvent.objects.exists()
    assert not AccountSession.objects.exists()
    restored = User.objects.get(username="demo_technical")
    assert restored.credentials_locked
    Client().force_login(restored)
    assert SignInEvent.objects.get().kind == "signed_in"


def _together(left, right):
    """Release two real connections together at the entry to their normal operations."""
    ready = Barrier(2)

    def worker(operation):
        try:
            conn = connections["default"]
            with conn.cursor() as cursor:
                cursor.execute("SET lock_timeout = '8s'")
            ready.wait(timeout=10)
            return operation()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        workers = [pool.submit(worker, operation) for operation in (left, right)]
        return [future.result(timeout=25) for future in workers]


def _history_password_login(browser, account):
    return browser.post(reverse("account_login"), {"login": account.username, "password": PASSWORD})


def _session_events(row):
    from accounts.models import SignInEvent

    return list(
        SignInEvent.objects.filter(user_id=row.user_id, session=row.pk)
        .order_by("id")
        .values_list("kind", "factor", "ending", "occurred_at", "user_agent", "address")
    )


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_browser_sign_ins_each_keep_their_own_start(make_user):
    from accounts.models import SignInEvent, SignInFactor

    account = make_user("history-colleague", "technical")
    EmailAddress.objects.create(user=account, email=account.email, verified=True, primary=True)
    first = Client(HTTP_USER_AGENT="Firefox/120", REMOTE_ADDR="192.0.2.31")
    second = Client(HTTP_USER_AGENT="Chrome/120", REMOTE_ADDR="192.0.2.32")

    results = _together(
        lambda: _history_password_login(first, account),
        lambda: _history_password_login(second, account),
    )
    assert [r.status_code for r in results] == [302, 302]
    assert AccountSession.objects.filter(user=account, ended_at__isnull=True).count() == 2
    assert SignInEvent.objects.filter(user=account).count() == 2
    for browser, agent, address in (
        (first, "Firefox/120", "192.0.2.31"),
        (second, "Chrome/120", "192.0.2.32"),
    ):
        row = AccountSession.objects.get(pk=browser.session[sessions.SESSION_KEY])
        assert _session_events(row) == [
            (SignInEvent.Kind.SIGNED_IN, SignInFactor.PASSWORD, "", row.started_at, agent, address)
        ]


@pytest.mark.django_db(transaction=True)
def test_concurrent_sign_in_and_deactivation_account_for_each_session(make_user):
    from accounts.models import SignInEvent

    account = make_user("history-retiring", "technical")
    EmailAddress.objects.create(user=account, email=account.email, verified=True, primary=True)
    earlier = Client()
    earlier.force_login(account)
    earlier_id = earlier.session[sessions.SESSION_KEY]
    incoming = Client(REMOTE_ADDR="192.0.2.37")

    def deactivate():
        account.is_active = False
        account.save(update_fields=["is_active"])

    response, _ = _together(lambda: _history_password_login(incoming, account), deactivate)
    assert response.status_code == 302
    account.refresh_from_db()
    assert not account.is_active
    rows = list(AccountSession.objects.filter(user=account))
    assert len(rows) in {1, 2}
    assert all(row.ending == AccountSession.Ending.DEACTIVATED for row in rows)
    for row in rows:
        events = _session_events(row)
        assert [(kind, ending, at) for kind, _factor, ending, at, _ua, _ip in events] == [
            (SignInEvent.Kind.SIGNED_IN, "", row.started_at),
            (SignInEvent.Kind.SIGNED_OUT, AccountSession.Ending.DEACTIVATED, row.ended_at),
        ]
    if len(rows) == 1:
        assert str(rows[0].pk) == earlier_id
        refused = SignInEvent.objects.get(user=account, session__isnull=True)
        assert (refused.kind, refused.ending, refused.address) == (
            SignInEvent.Kind.REFUSED,
            AccountSession.Ending.DEACTIVATED,
            "192.0.2.37",
        )
    else:
        assert not SignInEvent.objects.filter(user=account, session__isnull=True).exists()


@pytest.mark.django_db(transaction=True)
def test_simultaneous_sign_out_and_admin_ending_leave_one_ending(make_user):
    from accounts.models import SignInEvent

    account = make_user("history-signing-out", "technical")
    owner = make_user("history-finishing-sessions", is_staff=True, is_superuser=True)
    member, admin = Client(), Client()
    member.force_login(account)
    admin.force_login(owner)
    row = AccountSession.objects.get(pk=member.session[sessions.SESSION_KEY])
    admin_url = reverse("admin:accounts_user_sessions", args=[account.pk])

    logout, end = _together(
        lambda: member.post(reverse("account_logout")),
        lambda: admin.post(admin_url, {"reason": "Synthetic shift finished"}),
    )
    assert (logout.status_code, end.status_code) == (302, 302)
    row.refresh_from_db()
    assert row.ending in {AccountSession.Ending.SIGNED_OUT, AccountSession.Ending.ADMINISTRATOR}
    assert row.ended_at is not None
    assert _session_events(row) == [
        (SignInEvent.Kind.SIGNED_IN, "", "", row.started_at, row.user_agent, row.address),
        (SignInEvent.Kind.SIGNED_OUT, "", row.ending, row.ended_at, row.user_agent, row.address),
    ]
    assert AccountSession.objects.get(pk=admin.session[sessions.SESSION_KEY]).ended_at is None


def test_longest_session_ending_uses_the_deadline_in_history(make_user):
    from accounts.models import SignInEvent
    from core.models import SessionLimit

    user = make_user("history-overall-limit", "technical")
    SessionLimit.objects.create(audience="staff", inactivity_minutes=30, maximum_hours=12)
    browser = Client()
    browser.force_login(user)
    row = AccountSession.objects.get(pk=browser.session[sessions.SESSION_KEY])
    AccountSession.objects.filter(pk=row.pk).update(started_at=Now() - timedelta(hours=13))
    assert browser.get(reverse("account-session")).json()["signed_in"] is False
    row.refresh_from_db()
    expected = row.started_at + timedelta(hours=12)
    assert (row.ended_at, row.ending) == (expected, AccountSession.Ending.MAXIMUM)
    assert _session_events(row)[1:] == [
        (SignInEvent.Kind.SIGNED_OUT, "", row.ending, expected, row.user_agent, row.address),
    ]


@pytest.mark.django_db(transaction=True)
def test_history_keeps_the_ending_when_a_refused_changes_limit_was_lengthened(make_user):
    from accounts.models import SignInEvent
    from tests.test_review_session_limits_confirm3 import finish, refused_request

    user = make_user("history-changed-limit", "technical")
    request, row, _ = refused_request(user)
    finish(request)
    row.refresh_from_db()
    assert row.ending == AccountSession.Ending.CHANGE_REFUSED
    assert _session_events(row)[1:] == [
        (SignInEvent.Kind.SIGNED_OUT, "", row.ending, row.ended_at, row.user_agent, row.address),
    ]


def test_shared_html_omits_other_visitors_browser_and_address_everywhere(make_user):
    user = make_user("review-shared-output", "technical", credentials_locked=True)
    first = Client(
        HTTP_USER_AGENT="Synthetic Earlier Device OPR/12 Linux", REMOTE_ADDR="192.0.2.142"
    )
    second = Client(
        HTTP_USER_AGENT="Synthetic Current Device Edg/12 Android", REMOTE_ADDR="192.0.2.143"
    )
    assert sign_in(first, user).status_code == 302
    assert sign_in(second, user).status_code == 302
    page = second.get(reverse("account-sign-ins"))
    assert [e.this_session for e in page.context["entries"]] == [True, False]
    assert b"Edge on Android" in page.content and b"192.0.2.143" in page.content
    for withheld in (b"Opera on Linux", b"Synthetic Earlier Device", b"192.0.2.142"):
        assert withheld not in page.content


@pytest.mark.django_db(transaction=True)
def test_session_recorder_leaves_no_open_row_after_an_account_was_deactivated(make_user, rf):
    """Check the recorder's database invariant with a stale account object and no client cookie."""
    user = make_user("review-deactivated-recorder")
    earlier_read = User.objects.get(pk=user.pk)

    def deactivate():
        try:
            current = User.objects.get(pk=user.pk)
            current.is_active = False
            current.save(update_fields=["is_active"])
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as worker:
        worker.submit(deactivate).result(timeout=10)
    request = rf.get("/")
    request.session = {}
    sessions.start_session(User, request, earlier_read)
    assert not User.objects.get(pk=user.pk).is_active
    assert not AccountSession.objects.filter(user=user, ended_at__isnull=True).exists()


def test_security_settings_describes_where_a_named_password_failure_is_stored(client, make_user):
    from django.apps import apps

    admin = make_user("review-settings-reader", "admin")
    response = client.post(reverse("account_login"), {"login": admin.username, "password": "wrong"})
    assert response.status_code == 200
    client.force_login(admin)
    page = client.get(reverse("security-settings"))
    assert page.status_code == 200
    fact = next(
        fact
        for group in page.context["groups"]
        for fact in group.facts
        if fact.key == "sign-in-records"
    )
    event_model = apps.all_models["accounts"].get("signinevent")
    if event_model is not None:
        assert event_model.objects.filter(user=admin, kind="failed", factor="password").count() == 1
        assert "failed sign-ins are not stored" not in fact.note.lower()
    else:
        assert "failed sign-ins are not stored" in fact.note.lower()
