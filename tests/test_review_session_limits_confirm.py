"""Confirmation cases for the session timing, records and form-response contract."""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from queue import Queue
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib import messages
from django.contrib.auth import SESSION_KEY as AUTH
from django.db import connection, connections, transaction
from django.db.models.functions import Now
from django.test import Client, RequestFactory
from django.urls import reverse

from accounts import sessions
from accounts.models import AccountSession, User
from core.models import AuditEvent, LabModule, SessionLimit
from tests.conftest import PASSWORD

pytestmark = pytest.mark.django_db
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)
HEADERS = {
    "page": {},
    "partial": {"HX-Request": "true"},
    "dialog": {"X-VR-Form": "1"},
    "script": {"Sec-Fetch-Dest": "empty"},
}


@pytest.fixture
def account(staff):
    return staff("confirm-operator", "admin")


def login(account):
    browser = Client()
    browser.force_login(account)
    return browser, AccountSession.objects.get(pk=browser.session[sessions.SESSION_KEY])


def clock(row, start, idle):
    AccountSession.objects.filter(pk=row.pk).update(
        started_at=Now() - start, last_active_at=Now() - idle
    )
    row.refresh_from_db()


def state(row, account):
    uid, start, last, _, now, mins, hrs = sessions._read(str(row.pk), account)
    return sessions.SessionState(str(row.pk), uid, start, last, now, mins * MIN, hrs * HOUR)


def during_wait(row, operation, change, *, long_wait=False):
    """Commit the independent change only after PostgreSQL identifies the waiting backend."""
    pids = Queue()

    def worker():
        try:
            with connection.cursor() as cur:
                cur.execute("SET lock_timeout = '25s'" if long_wait else "SET lock_timeout = '7s'")
                cur.execute("SELECT pg_backend_pid()")
                pids.put(cur.fetchone()[0])
            return operation()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic(), connection.cursor() as cur:
            AccountSession.objects.select_for_update().get(pk=row.pk)
            job = pool.submit(worker)
            pid = pids.get(timeout=4)
            until = time.monotonic() + 4
            while time.monotonic() < until:
                cur.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
                if cur.fetchone()[0]:
                    break
                if job.done():
                    pytest.fail(f"Expected a lock wait, observed {job.result()!r}")
                time.sleep(0.01)
            else:
                pytest.fail("No wait on the fixture's session row")
            change()
        return job.result(timeout=5)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["lengthen", "shorten", "refresh"])
def test_expiry_ledger_observes_the_values_committed_before_lock_acquisition(account, change):
    _, row = login(account)
    policy = SessionLimit.objects.create(audience="staff", inactivity_minutes=10, maximum_hours=12)
    clock(row, HOUR, 15 * MIN)
    prior = state(row, account)

    def update():
        if change == "refresh":
            AccountSession.objects.filter(pk=row.pk).update(last_active_at=Now())
        else:
            SessionLimit.objects.filter(pk=policy.pk).update(
                inactivity_minutes=30 if change == "lengthen" else 5
            )

    ended = during_wait(row, lambda: sessions._end_expired(prior, "inactivity"), update)
    row.refresh_from_db()
    expected = (
        (1, "inactivity", prior.last_active_at + 5 * MIN) if change == "shorten" else (0, "", None)
    )
    assert (ended, row.ending, row.ended_at) == expected


@pytest.mark.django_db(transaction=True)
def test_guard_honors_a_longer_limit_committed_before_its_hold(account):
    _, row = login(account)
    clock(row, HOUR, 15 * MIN)
    policy = SessionLimit.objects.create(audience="staff", inactivity_minutes=5, maximum_hours=12)

    def guard():
        token = sessions._current.set((account.pk, str(row.pk)))
        try:
            with transaction.atomic():
                return sessions.hold_acting_session(account.pk)
        finally:
            sessions._current.reset(token)

    assert during_wait(
        row, guard, lambda: SessionLimit.objects.filter(pk=policy.pk).update(inactivity_minutes=30)
    )


def test_settlement_open_state_has_time_remaining_at_its_returned_clock(account):
    _, row = login(account)
    clock(row, HOUR, 21 * MIN)
    policy = SessionLimit.objects.create(audience="staff", inactivity_minutes=5, maximum_hours=12)
    reading = state(row, account)
    # At admission the row was idle 19 minutes. At the second reading it is idle 21.
    # The new 20-minute deadline lies strictly between those two clocks.
    earlier = replace(reading, now=reading.now - 2 * MIN)
    SessionLimit.objects.filter(pk=policy.pk).update(inactivity_minutes=20)
    outcome, detail = sessions._settle_expiry(earlier)
    if outcome == "open":
        assert detail.last_active_at + detail.inactivity > detail.now
    else:
        row.refresh_from_db()
        assert (outcome, row.ending) == ("expired", "inactivity")


@pytest.mark.parametrize("intervening", ["new-duration", "explicit-sign-out"])
def test_expiry_notice_describes_the_ending_that_won(account, monkeypatch, intervening):
    browser, row = login(account)
    clock(row, HOUR, 35 * MIN)
    read = sessions._read

    def read_then_change(session_id, user):
        result = read(session_id, user)
        if intervening == "new-duration":
            SessionLimit.objects.create(audience="staff", inactivity_minutes=20, maximum_hours=12)
        else:
            sessions.end_sessions(account, "signed_out", session_id=row.pk)
        return result

    monkeypatch.setattr(sessions, "_read", read_then_change)
    answer = browser.get(reverse("account-session"))
    assert answer.json()["signed_in"] is False
    row.refresh_from_db()
    notices = [str(m) for m in messages.get_messages(answer.wsgi_request)]
    if intervening == "new-duration":
        assert row.ending == "inactivity"
        assert row.ended_at == row.last_active_at + 20 * MIN
        assert notices == [sessions.ENDED_MESSAGES["inactivity"].format(inactivity="20 minutes")]
    else:
        assert row.ending == "signed_out"
        assert notices == [sessions.ENDED_MESSAGE]


def test_successful_stay_returns_the_policy_used_to_record_activity(account, monkeypatch):
    browser, row = login(account)
    clock(row, HOUR, MIN)
    policy = SessionLimit.objects.create(audience="staff", inactivity_minutes=10, maximum_hours=12)
    touch = sessions._touch

    def lengthen_before_recording(reading):
        SessionLimit.objects.filter(pk=policy.pk).update(inactivity_minutes=25)
        return touch(reading)

    monkeypatch.setattr(sessions, "_touch", lengthen_before_recording)
    answer = browser.post(reverse("account-session-continue")).json()
    control = browser.get(reverse("account-session")).json()
    assert control["inactivity_limit_seconds"] == 1500
    assert (
        answer["signed_in"]
        and answer["inactivity_limit_seconds"] == control["inactivity_limit_seconds"]
    )
    assert abs(answer["inactivity_seconds"] - control["inactivity_seconds"]) <= 2


@pytest.mark.parametrize("transport", HEADERS)
@pytest.mark.parametrize("form", ["limits", "queue", "legacy-bulk", "nonconformance"])
def test_service_refusal_reaches_the_person_and_ends_browser_session(
    account, monkeypatch, transport, form
):
    browser, _ = login(account)
    calls = []

    def refuse(pk):
        calls.append(pk)
        return False

    monkeypatch.setattr(sessions, "hold_acting_session", refuse)
    parent = reverse("desk:request-list") + "?view=my-open"
    if form == "limits":
        target = reverse("session-limits-edit", args=["staff"])
        data = {
            "expected_revision": 0,
            "reason": "Synthetic shift",
            "maximum_hours": 8,
            "inactivity_minutes": 18,
        }
    elif form == "nonconformance":
        LabModule.objects.create(key="nonconformance", is_enabled=True, revision=1)
        target = reverse("nonconformance:create")
        data = {
            "title": "Synthetic review",
            "description": "Example",
            "severity": "minor",
            "source": "internal",
        }
    else:
        target = reverse("desk:request-list" if form == "queue" else "desk:request-bulk")
        data = {"action": "close", "requests": ["1:1"]}
    answer = browser.post(
        target, data, headers={"Referer": "http://testserver" + parent, **HEADERS[transport]}
    )
    assert (
        calls == [account.pk]
        and not AuditEvent.objects.exists()
        and not SessionLimit.objects.exists()
    )
    header = "Location" if transport == "page" else "HX-Redirect"
    assert (answer.status_code, answer.get(header), AUTH in browser.session) == (
        302 if transport == "page" else 401,
        sessions.sign_in_url(parent),
        False,
    )
    assert "no-store" in answer["Cache-Control"]


def test_notice_from_the_transactional_expiry_guard_records_the_expired_row(account, caplog):
    browser, row = login(account)
    clock(row, HOUR, 31 * MIN)
    req = RequestFactory().post("/")
    req.user, req.session = account, browser.session
    req.account_session = state(row, account)
    token = sessions._current.set((account.pk, str(row.pk)))
    try:
        with transaction.atomic():
            assert sessions.hold_acting_session(account.pk) is False
    finally:
        sessions._current.reset(token)
    # Invoke the exception handler alone. No business view or mutation is needed.
    middleware = sessions.SessionLimitMiddleware(lambda r: None)
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        answer = middleware.process_exception(req, sessions.SessionEnded())
    assert answer.status_code == 302
    row.refresh_from_db()
    assert (row.ending, row.ended_at) == ("inactivity", row.last_active_at + 30 * MIN)
    assert any("signout.inactivity" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("transport", HEADERS)
@pytest.mark.parametrize(
    "destination",
    [
        "own-known-page",
        "own-unknown-page",
        "desk-known-page",
        "desk-unknown-page",
        "admin-explicit-next",
    ],
)
def test_anonymous_post_preserves_the_correct_next(account, transport, destination):
    browser = Client()
    parent = reverse("desk:request-list") + "?view=my-open"
    target = reverse("admin:auth_group_add") + "?section=one"
    headers = dict(HEADERS[transport])
    if "unknown" not in destination:
        headers["Referer"] = "http://testserver" + parent
    if destination.startswith("desk-"):
        target = reverse("desk:request-create") + "?section=one"
    if destination == "admin-explicit-next":
        target = reverse("admin:login") + "?next=/admin/auth/"
    answer = browser.post(target, headers=headers)
    redirect = answer.get("HX-Redirect") or answer["Location"]
    url = urlsplit(redirect)
    expected = {
        "admin-explicit-next": "/admin/auth/",
        "own-unknown-page": target,
        "own-known-page": parent,
        "desk-known-page": parent,
        "desk-unknown-page": target,
    }[destination]
    assert parse_qs(url.query)["next"] == [expected]
    if destination in ("own-known-page", "desk-known-page"):
        assert answer.status_code == (302 if transport == "page" else 401)
    else:
        assert answer.status_code == (204 if transport == "dialog" else 302)
    assert AUTH not in browser.session


@pytest.mark.parametrize("cookie", ["none", "anonymous", "signed-in"])
@pytest.mark.parametrize("path", ["/", "/static/no-file-with-this-name.js"])
def test_early_host_redirect_is_a_redirect_even_under_the_static_prefix(
    account, settings, cookie, path
):
    settings.PREPEND_WWW = True
    settings.ALLOWED_HOSTS = ["testserver", "www.testserver"]
    browser = Client()
    if cookie == "signed-in":
        browser.force_login(account)
    elif cookie == "anonymous":
        session = browser.session
        session["example"] = 1
        session.save()
    answer = browser.get(path, secure=True)
    assert answer.status_code == 301
    assert ("no-store" in answer.get("Cache-Control", "")) is (cookie != "none")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("rollback", [True, False])
def test_cleanup_logs_aggregate_only_the_endings_that_commit(account, caplog, rollback):
    cases = [login(account)[1] for _ in range(3)]
    for row in cases[:2]:
        clock(row, HOUR, 40 * MIN)
    clock(cases[2], 13 * HOUR, MIN)
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        with transaction.atomic():
            assert sessions.close_expired(account) == 3
            assert not any(r.getMessage().startswith("signout.") for r in caplog.records)
            transaction.set_rollback(rollback)
    found = [r.getMessage() for r in caplog.records if r.getMessage().startswith("signout.")]
    assert sorted(found) == (
        []
        if rollback
        else [
            f"signout.inactivity user={account.username} sessions=2",
            f"signout.maximum user={account.username} sessions=1",
        ]
    )
    assert AccountSession.objects.filter(ended_at__isnull=True).count() == (3 if rollback else 0)


def test_final_live_reading_after_two_declined_touches_keeps_browser_and_row_consistent(
    account, monkeypatch
):
    browser, row = login(account)
    clock(row, HOUR, 7 * MIN)
    policy = SessionLimit.objects.create(audience="staff", inactivity_minutes=10, maximum_hours=12)
    original = sessions._touch
    values = SessionLimit.objects.filter(pk=policy.pk)
    touches = []

    def record(state):
        if len(touches) < 2:
            values.update(inactivity_minutes=5)
            observed = original(state)
            assert observed is False
            touches.append(observed)
            values.update(inactivity_minutes=10)
            return observed
        return original(state)

    monkeypatch.setattr(sessions, "_touch", record)
    answer = browser.post(reverse("account-session-continue"))
    row.refresh_from_db()
    assert row.ended_at is None
    assert answer.json()["signed_in"] and AUTH in browser.session


@pytest.mark.django_db(transaction=True)
def test_stay_waits_for_a_held_change_even_beyond_the_browser_timeout(account):
    browser, row = login(account)
    clock(row, HOUR, 8 * MIN)

    def stay():
        return browser.post(reverse("account-session-continue")).json()

    def finish_edit():
        # Use the same self-assignment row lock a real edit may need before it finishes.
        from desk.services import _held_assignee

        assert _held_assignee(account).account.pk == account.pk
        time.sleep(15.2)

    began = time.monotonic()
    answer = during_wait(row, stay, finish_edit, long_wait=True)
    assert time.monotonic() - began >= 15
    assert answer["signed_in"] and answer["inactivity_seconds"] > 29 * 60
    row.refresh_from_db()
    assert row.last_active_at > row.started_at + 55 * MIN


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "route",
    [
        "account_change_password",
        "account_set_password",
        "admin:password_change",
        "admin:auth_user_password_change",
    ],
)
def test_own_password_form_keeps_exactly_its_session_at_commit(route):
    password = "Synthetic-confirmation-shift-418!"
    owner = User.objects.create_superuser("confirm-owner", "confirm@example.invalid", PASSWORD)
    if route == "account_set_password":
        owner.set_unusable_password()
        owner.save(update_fields=["password"])
    browser, own = login(owner)
    _, sibling = login(owner)
    data = {"password1": password, "password2": password}
    args = []
    if route == "admin:password_change":
        data = {"old_password": PASSWORD, "new_password1": password, "new_password2": password}
    elif route == "admin:auth_user_password_change":
        args = [owner.pk]
        data["usable_password"] = "true"
    elif route == "account_change_password":
        data["oldpassword"] = PASSWORD

    open_at_commit = []

    def observe(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if sql.startswith('UPDATE "accounts_user" SET "password"'):
            transaction.on_commit(
                lambda: open_at_commit.append(
                    list(
                        AccountSession.objects.filter(
                            user=owner, ended_at__isnull=True
                        ).values_list("pk", flat=True)
                    )
                )
            )
        return result

    with connection.execute_wrapper(observe):
        answer = browser.post(reverse(route, args=args), data)
    owner.refresh_from_db()
    sibling.refresh_from_db()
    assert answer.status_code == 302 and owner.check_password(password)
    assert open_at_commit == [[own.pk]] and sibling.ending == "new_password"


def test_email_reset_opened_by_different_account_closes_only_the_target_password_sessions(
    account, staff, mailoutbox
):
    target = staff("confirm-reset", "technical")
    _, target_a = login(target)
    _, target_b = login(target)
    browser, original = login(account)
    _, sibling = login(account)
    assert (
        Client().post(reverse("account_reset_password"), {"email": target.email}).status_code == 302
    )
    link = re.search(r"https?://[^\s<>]+/password/reset/key/[^\s<>]+", mailoutbox[0].body)[0]
    form = browser.get(urlsplit(link).path, follow=True)
    newer = "Synthetic-reset-two-accounts-842!"
    result = browser.post(form.wsgi_request.path, {"password1": newer, "password2": newer})
    assert result.status_code == 302
    target.refresh_from_db()
    assert target.check_password(newer)
    for row in (target_a, target_b, original, sibling):
        row.refresh_from_db()
    assert [r.ending for r in (target_a, target_b, original, sibling)] == [
        "new_password",
        "new_password",
        "signed_out",
        "",
    ]


def test_anonymous_cookie_after_session_decision_and_real_static_file_keep_cache_policy():
    from django.templatetags.static import static

    browser = Client()
    session = browser.session
    session["example"] = True
    session.save()
    health = browser.get("/health/")
    assert health.status_code == 200 and health.json() == {"status": "ok"}
    assert "no-store" not in health.get("Cache-Control", "")
    asset = browser.get(static("js/app.js"))
    assert asset.status_code == 200 and "max-age" in asset["Cache-Control"]
    assert "no-store" not in asset["Cache-Control"]
