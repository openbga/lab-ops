"""Verification of the round-one resolutions. All clocks, accounts and records are test data.

Written by the independent reviewer (round 1 verification,
.local/review-history/session-limits-round-1-verification.md); adopted unchanged except where a
test says it was adapted."""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import timedelta
from queue import Queue
from threading import Event
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth import update_session_auth_hash
from django.db import connection, connections, transaction
from django.db.models.functions import Now
from django.http import HttpResponse
from django.test import Client, RequestFactory
from django.urls import reverse

from accounts import sessions
from accounts.acting import hold_actor, holding
from accounts.models import AccountSession, User
from core.models import AuditEvent, SessionLimit
from tests.conftest import PASSWORD

pytestmark = pytest.mark.django_db
NEW = "synthetic-session-verification-918!"
SECOND = "synthetic-final-verification-317!"
LIMITS_DATA = {
    "inactivity_minutes": 17,
    "maximum_hours": 9,
    "expected_revision": 0,
    "reason": "Synthetic reception hours",
}


@pytest.fixture
def admin(staff):
    return staff("verify-operator", "admin")


def sign_in(user):
    browser = Client()
    browser.force_login(user)
    return browser


def row_of(browser):
    return AccountSession.objects.get(pk=browser.session[sessions.SESSION_KEY])


@contextmanager
def acting_as(row):
    token = sessions._current.set((row.user_id, str(row.pk)))
    try:
        yield
    finally:
        sessions._current.reset(token)


def age(row, *, started, active):
    AccountSession.objects.filter(pk=row.pk).update(
        started_at=Now() - started, last_active_at=Now() - active
    )
    row.refresh_from_db()


def test_request_and_cleanup_agree_on_the_first_deadline(admin):
    # Both sessions have exactly the same start and last activity. Inactivity came first.
    checked, abandoned = sign_in(admin), sign_in(admin)
    a, b = row_of(checked), row_of(abandoned)
    age(a, started=timedelta(hours=14), active=timedelta(hours=3))
    AccountSession.objects.filter(pk=b.pk).update(
        started_at=a.started_at, last_active_at=a.last_active_at
    )
    deadline = a.last_active_at + timedelta(minutes=30)
    assert checked.get(reverse("account-session")).json()["signed_in"] is False
    sign_in(admin)
    a.refresh_from_db()
    b.refresh_from_db()
    assert [(r.ending, r.ended_at) for r in (a, b)] == [("inactivity", deadline)] * 2


@pytest.mark.parametrize("occasion", ["next-login", "password-change"])
def test_reconciliation_logs_each_abandoned_ending_once(
    admin, caplog, occasion, django_capture_on_commit_callbacks
):
    expired = row_of(sign_in(admin))
    age(expired, started=timedelta(hours=2), active=timedelta(minutes=40))
    with (
        caplog.at_level(logging.INFO, logger="lab_ops.signin"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        if occasion == "next-login":
            sign_in(admin)
        else:
            admin.set_password(NEW)
            admin.save(update_fields=["password"])
        sign_in(admin)  # already counted
    expired.refresh_from_db()
    assert expired.ending == "inactivity"
    messages = [r.getMessage() for r in caplog.records if r.name == "lab_ops.signin"]
    assert messages.count(f"signout.inactivity user={admin.username} sessions=1") == 1


@pytest.mark.django_db(transaction=True)
def test_cleanup_does_not_apply_limits_replaced_after_its_read(admin):
    """Adapted after verification (finding 5): close_expired now reads the limits in the same
    statement that ends rows, so there is no earlier reading to replace. A limit lengthened by
    another connection and committed before the cleanup runs governs it."""
    row = row_of(sign_in(admin))
    age(row, started=timedelta(hours=1), active=timedelta(minutes=15))
    policy = SessionLimit.objects.create(audience="staff", inactivity_minutes=5, maximum_hours=12)

    def extend():
        try:
            assert SessionLimit.objects.filter(pk=policy.pk).update(inactivity_minutes=30) == 1
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(extend).result(timeout=5)
    count = sessions.close_expired(admin)
    row.refresh_from_db()
    assert (count, row.ended_at, row.ending) == (0, None, "")


@pytest.mark.django_db(transaction=True)
def test_expiry_record_respects_activity_committed_since_the_reading(admin):
    # The old snapshot and current row are intentionally different; only the ledger is exercised.
    row = row_of(sign_in(admin))
    age(row, started=timedelta(hours=1), active=timedelta(minutes=35))
    read = sessions._read(str(row.pk), admin)
    _, start, last, _, now, minutes, hours = read
    snapshot = sessions.SessionState(
        str(row.pk), admin.pk, start, last, now, timedelta(minutes=minutes), timedelta(hours=hours)
    )

    def store_recent_activity():
        try:
            assert (
                AccountSession.objects.filter(pk=row.pk).update(
                    last_active_at=now - timedelta(minutes=10)
                )
                == 1
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(store_recent_activity).result(timeout=5)
    changed = sessions._end_expired(snapshot, AccountSession.Ending.INACTIVITY)
    row.refresh_from_db()
    assert (changed, row.ended_at, row.ending) == (0, None, "")


@pytest.mark.parametrize("deadline", ["inactivity", "maximum", "ended-row", "open"])
def test_actor_guard_checks_the_session_at_its_own_reading(admin, deadline):
    # A decision-only test: no view or business mutation is called.
    row = row_of(sign_in(admin))
    if deadline == "inactivity":
        age(row, started=timedelta(hours=2), active=timedelta(minutes=31))
    elif deadline == "maximum":
        age(row, started=timedelta(hours=13), active=timedelta(seconds=1))
    elif deadline == "ended-row":
        sessions.end_sessions(admin, AccountSession.Ending.SIGNED_OUT, session_id=row.pk)
    with acting_as(row), transaction.atomic():
        allowed = sessions.hold_acting_session(admin.pk)
    assert allowed is (deadline == "open")


def test_missing_activity_row_is_reported_as_gone(admin):
    # There is no competing connection and no protected request follows this predicate.
    browser = sign_in(admin)
    row = row_of(browser)
    read = sessions._read(str(row.pk), admin)
    uid, start, last, _, now, minutes, hours = read
    state = sessions.SessionState(
        str(row.pk), uid, start, last, now, timedelta(minutes=minutes), timedelta(hours=hours)
    )
    row.delete()
    assert sessions._touch(state) is False


def test_stay_accepts_the_longer_limit_seen_at_the_final_check(admin, monkeypatch):
    browser = sign_in(admin)
    row = row_of(browser)
    policy = SessionLimit.objects.create(audience="staff", inactivity_minutes=5, maximum_hours=12)
    age(row, started=timedelta(hours=1), active=timedelta(minutes=4))
    touch = sessions._touch
    calls = []

    def cross_boundary_then_extend(state):
        # Adapted: after the final reading finds the session within the longer limit, the
        # activity is recorded once more; this stand-in applies to the first statement only.
        calls.append(state)
        if len(calls) > 1:
            return touch(state)
        age(row, started=timedelta(hours=1), active=timedelta(minutes=6))
        assert touch(state) is False
        SessionLimit.objects.filter(pk=policy.pk).update(inactivity_minutes=15)
        return False

    monkeypatch.setattr(sessions, "_touch", cross_boundary_then_extend)
    response = browser.post(reverse("account-session-continue"))
    row.refresh_from_db()
    assert row.ended_at is None
    assert response.json()["signed_in"] is True


@pytest.mark.django_db(transaction=True)
def test_successful_continue_refreshes_idle_time_while_another_tab_holds_a_change(admin):
    browser = sign_in(admin)
    row = row_of(browser)
    age(row, started=timedelta(hours=1), active=timedelta(minutes=25))
    before = row.last_active_at
    reached_activity = Event()

    def observe(execute, sql, params, many, context):
        if "set last_active_at" in sql.lower():
            reached_activity.set()
        return execute(sql, params, many, context)

    def keep_working():
        try:
            with connection.execute_wrapper(observe):
                return browser.post(reverse("account-session-continue")).json()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with acting_as(row), transaction.atomic():
            hold_actor(admin)
            # Hold the same PostgreSQL lock on both versions, including the version
            # where hold_actor did not yet take this row itself.
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM accounts_accountsession WHERE id = %s FOR SHARE", [row.pk]
                )
            future = pool.submit(keep_working)
            assert reached_activity.wait(timeout=5)
            wait(
                [future], timeout=0.6
            )  # the former blocking implementation may finish after release
        answer = future.result(timeout=5)
    row.refresh_from_db()
    assert answer["signed_in"] is True
    assert row.last_active_at > before and answer["inactivity_seconds"] > 29 * 60, answer


def test_database_clock_moving_back_does_not_decrease_recorded_activity(admin):
    browser = sign_in(admin)
    row = row_of(browser)
    AccountSession.objects.filter(pk=row.pk).update(last_active_at=Now() + timedelta(seconds=2))
    row.refresh_from_db()
    latest = row.last_active_at
    assert browser.post(reverse("account-session-continue")).json()["signed_in"]
    row.refresh_from_db()
    assert row.last_active_at >= latest


@pytest.mark.parametrize(
    "target", ["404", "module-off", "locked-credentials", "continue-get", "status"]
)
def test_request_that_never_performs_an_activity_keeps_the_timestamp(admin, target):
    admin.credentials_locked = True
    admin.save(update_fields=["credentials_locked"])
    browser = sign_in(admin)
    row = row_of(browser)
    age(row, started=timedelta(hours=1), active=timedelta(minutes=5))
    before = row.last_active_at
    path, expected = {
        "404": ("/verify-missing-page/", 404),
        "module-off": ("/quality/nonconformances/", 404),
        "locked-credentials": (reverse("account_email"), 302),
        "continue-get": (reverse("account-session-continue"), 405),
        "status": (reverse("account-session"), 200),
    }[target]
    response = browser.get(path)
    assert response.status_code == expected
    if target == "locked-credentials":
        assert response.url == reverse("dashboard")
    row.refresh_from_db()
    assert row.last_active_at == before


@pytest.mark.parametrize("route", ["session-limits-edit", "desk:request-create"])
@pytest.mark.parametrize("transport", ["page", "dialog"])
def test_session_ending_at_save_has_a_sign_in_response(
    admin, client_org, monkeypatch, route, transport
):
    from desk.models import ServiceRequest
    from taxonomy.models import RequestCategory
    from taxonomy.terms import drawn

    browser = sign_in(admin)
    parent = (
        reverse("security-settings")
        if route == "session-limits-edit"
        else reverse("desk:request-list")
    )
    if route == "session-limits-edit":
        url, data = reverse(route, args=["staff"]), LIMITS_DATA
    else:
        group = RequestCategory.objects.create(key="verify", name="Verify")
        category = RequestCategory.objects.create(key="collection", name="Collection", parent=group)
        url = reverse(route)
        data = {
            "category": category.pk,
            "category_terms": drawn([category]),
            "client": client_org.pk,
            "channel": "fax",
            "subject": "Example verification",
            "first_entry_kind": "note",
            "body": "Example note",
        }
    # Simulate the guard's refusal on an otherwise valid form. No session or access race is needed.
    calls = []

    def refuse(user_pk):
        calls.append(user_pk)
        return False

    monkeypatch.setattr(sessions, "hold_acting_session", refuse)
    headers = {"Referer": "http://testserver" + parent}
    if transport == "dialog":
        headers["X-VR-Form"] = "1"
    response = browser.post(url, data, headers=headers)
    assert (
        calls == [admin.pk]
        and not SessionLimit.objects.exists()
        and not ServiceRequest.objects.exists()
    )
    assert not AuditEvent.objects.exists()
    header = "HX-Redirect" if transport == "dialog" else "Location"
    expected = 401 if transport == "dialog" else 302
    assert (response.status_code, response.get(header)) == (
        expected,
        reverse("account_login") + "?next=" + parent,
    )


@pytest.mark.parametrize(
    "via", ["ticket", "client", "import-preview", "import-commit", "limits", "nested-holding"]
)
def test_actor_guard_refusal_reaches_each_service_before_writing(admin, monkeypatch, via):
    from clients.models import Client as Organization
    from clients.services import create_client
    from core.session_limits import save_limits
    from dataimport.importing import ORGANIZATIONS, commit_file, preview_file
    from desk.services import create_request
    from taxonomy.models import RequestCategory

    csv = {"actor": admin, "data": b"account_code,name\nVER,Example\n", "filename": "example.csv"}
    if via == "import-commit":
        preview = preview_file(ORGANIZATIONS, **csv)
        assert preview.ok
    if via == "ticket":
        group = RequestCategory.objects.create(key="verify", name="Verify")
        category = RequestCategory.objects.create(key="collection", name="Collection", parent=group)

    monkeypatch.setattr(sessions, "hold_acting_session", lambda user_pk: False)
    with pytest.raises(sessions.SessionEnded), transaction.atomic():
        if via == "ticket":
            create_request(
                actor=admin,
                category=category,
                channel="fax",
                subject="Example",
                first_entry_kind="note",
                first_entry_body="Example",
            )
        elif via == "client":
            create_client(actor=admin, name="Example", account_number="VER")
        elif via == "import-preview":
            preview_file(ORGANIZATIONS, **csv)
        elif via == "import-commit":
            commit_file(ORGANIZATIONS, **csv, reason="Example", state_token=preview.state_token)
        elif via == "limits":
            save_limits(
                actor=admin,
                audience="staff",
                new=LIMITS_DATA,
                expected_revision=0,
                reason="Example",
            )
        else:
            with holding(admin):
                pytest.fail("Refused account entered a held block")
    assert not Organization.objects.exists() and not AuditEvent.objects.exists()


@pytest.mark.parametrize("transport", ["page", "dialog", "htmx", "script"])
@pytest.mark.parametrize("console", [False, True], ids=["desk", "console"])
def test_anonymous_action_returns_to_its_parent_page(transport, console):
    browser = Client()
    parent = reverse("admin:index") if console else reverse("desk:request-list")
    target = reverse("admin:auth_group_add") if console else reverse("desk:request-create")
    headers = {"Referer": "http://testserver" + parent}
    if transport == "dialog":
        headers["X-VR-Form"] = "1"
    if transport == "htmx":
        headers["HX-Request"] = "true"
    if transport == "script":
        headers["Sec-Fetch-Dest"] = "empty"
    response = (
        browser.post(target, headers=headers)
        if transport == "page"
        else browser.get(target, headers=headers)
    )
    code, header = (302, "Location") if transport == "page" else (401, "HX-Redirect")
    assert (response.status_code, response.get(header)) == (
        code,
        reverse("account_login") + "?next=" + parent,
    )
    assert "no-store" in response["Cache-Control"]


def test_early_canonical_host_redirect_for_a_signed_in_browser_has_no_store(admin, settings):
    # A real HTTPS request with a valid cookie. CommonMiddleware answers before dispatch.
    settings.PREPEND_WWW = True
    settings.ALLOWED_HOSTS = ["testserver", "www.testserver"]
    browser = sign_in(admin)
    response = browser.get("/", secure=True)
    assert response.status_code == 301 and response.url == "https://www.testserver/"
    assert "no-store" in response.get("Cache-Control", "")


@pytest.mark.django_db(transaction=True)
def test_signed_in_email_reset_ends_its_rows_at_the_password_commit(admin, mailoutbox):
    current, sibling = sign_in(admin), sign_in(admin)
    ids = [row_of(browser).pk for browser in (current, sibling)]
    requester = Client()
    assert (
        requester.post(reverse("account_reset_password"), {"email": admin.email}).status_code == 302
    )
    link = re.search(r"https?://[^\s<>]+/password/reset/key/[^\s<>]+", mailoutbox[0].body)[0]
    form = current.get(urlsplit(link).path)
    assert form.status_code == 302
    open_at_commit = []

    def observe(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if sql.startswith('UPDATE "accounts_user" SET "password"'):
            transaction.on_commit(
                lambda: open_at_commit.append(
                    AccountSession.objects.filter(pk__in=ids, ended_at__isnull=True).count()
                )
            )
        return result

    with connection.execute_wrapper(observe):
        response = current.post(form.url, {"password1": NEW, "password2": NEW})
    assert response.status_code == 302
    admin.refresh_from_db()
    assert admin.check_password(NEW)
    assert open_at_commit == [0]
    assert AccountSession.objects.filter(pk__in=ids, ending="new_password").count() == 2


@pytest.mark.parametrize("action", ["rollback", "two-changes", "rollback-second"])
def test_settling_uses_the_final_committed_password(admin, action):
    browser, other = sign_in(admin), sign_in(admin)
    own, sibling = row_of(browser), row_of(other)
    request = RequestFactory().get("/")
    request.session, request.user = browser.session, admin
    request.COOKIES = browser.cookies.copy()

    def change(req):
        with transaction.atomic():
            admin.set_password(NEW)
            admin.save(update_fields=["password"])
            if action == "rollback":
                transaction.set_rollback(True)
            else:
                update_session_auth_hash(req, admin)
        if action != "rollback":
            with transaction.atomic():
                admin.set_password(SECOND)
                admin.save(update_fields=["password"])
                if action == "rollback-second":
                    transaction.set_rollback(True)
                else:
                    update_session_auth_hash(req, admin)
        return HttpResponse()

    assert sessions.SessionLimitMiddleware(change)(request).status_code == 200
    admin.refresh_from_db()
    assert admin.check_password(
        {"rollback": PASSWORD, "rollback-second": NEW, "two-changes": SECOND}[action]
    )
    own.refresh_from_db()
    sibling.refresh_from_db()
    assert own.ended_at is None
    assert sibling.ending == ("" if action == "rollback" else "new_password")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("ending", ["signed_out", "new_password", "deactivated"])
def test_account_ending_waits_for_the_held_edit_and_self_assignment_completes(admin, ending):
    """A session-ending operation and an ordinary assignee lock on two PostgreSQL connections."""
    from desk.services import _held_assignee

    editor = sign_in(admin)
    editor_row = row_of(editor)
    sibling = sign_in(admin)
    sibling_row = row_of(sibling)
    worker_pid = Queue()

    def observe_pid(execute, sql, params, many, context):
        if worker_pid.empty():
            worker_pid.put(connection.connection.info.backend_pid)
        return execute(sql, params, many, context)

    def end():
        try:
            with connection.execute_wrapper(observe_pid):
                if ending == "deactivated":
                    other_copy = User.objects.get(pk=admin.pk)
                    other_copy.is_active = False
                    other_copy.save(update_fields=["is_active"])
                elif ending == "new_password":
                    r = sibling.post(
                        reverse("account_change_password"),
                        {"oldpassword": PASSWORD, "password1": NEW, "password2": NEW},
                    )
                    assert r.status_code == 302
                else:
                    tab = Client()
                    tab.cookies = editor.cookies.copy()
                    assert tab.post(reverse("account_logout")).status_code == 302
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with acting_as(editor_row), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '3s'")
            hold_actor(admin)
            work = pool.submit(end)
            pid = worker_pid.get(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
                if cursor.fetchone()[0]:
                    break
                if work.done():
                    work.result()
                    pytest.fail("The account ending finished before the held edit")
                time.sleep(0.01)
            else:
                pytest.fail("The account ending did not reach the held edit")
            assert _held_assignee(admin).account.pk == admin.pk
            assert row_of(editor).ended_at is None
        work.result(timeout=6)
    editor_row.refresh_from_db()
    sibling_row.refresh_from_db()
    assert editor_row.ending == ending
    assert sibling_row.ending == ("deactivated" if ending == "deactivated" else "")


@pytest.mark.django_db(transaction=True)
def test_a_second_edit_of_the_same_session_can_hold_the_actor_concurrently(admin):
    row = row_of(sign_in(admin))

    def second():
        try:
            with acting_as(row), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '2s'")
                with holding(admin) as current:
                    return hold_actor(current).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with acting_as(row), transaction.atomic():
            hold_actor(admin)
            assert pool.submit(second).result(timeout=4) == admin.pk


def test_cleanup_keeps_the_earliest_deadline_and_uses_maximum_on_equality(admin):
    # Compare the complementary case to the one where inactivity came first.
    policy = SessionLimit.objects.create(audience="staff", inactivity_minutes=60, maximum_hours=1)
    for last_active_after_start in (timedelta(0), timedelta(minutes=15)):
        browser = sign_in(admin)
        row = row_of(browser)
        age(row, started=timedelta(hours=2), active=timedelta(hours=2) - last_active_after_start)
        if last_active_after_start == timedelta(0):
            AccountSession.objects.filter(pk=row.pk).update(last_active_at=row.started_at)
        assert sessions.close_expired(admin) == 1
        row.refresh_from_db()
        assert row.ending == "maximum" and row.ended_at == row.started_at + timedelta(
            hours=policy.maximum_hours
        )


def test_ordinary_csrf_refusal_preserves_activity_and_preference(admin):
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(admin)
    row = row_of(browser)
    age(row, started=timedelta(hours=1), active=timedelta(minutes=7))
    before = row.last_active_at
    response = browser.post(
        reverse("preference"),
        {"key": "nav.collapsed", "value": True},
        content_type="application/json",
    )
    assert response.status_code == 403 and "no-store" in response.get("Cache-Control", "")
    assert not admin.preferences.filter(key="nav.collapsed").exists()
    row.refresh_from_db()
    assert row.last_active_at == before


def test_opened_before_expiry_grid_saves_preferences_only_for_a_live_session(admin):
    browser = sign_in(admin)
    row = row_of(browser)
    age(row, started=timedelta(hours=3), active=timedelta(hours=1))
    address = reverse("desk:request-list") + "?per_page=50"
    response = browser.get(address)
    assert response.status_code == 302
    assert urlsplit(response.url).path == reverse("account_login")
    assert parse_qs(urlsplit(response.url).query)["next"] == [address]
    assert not admin.preferences.filter(key="page:desk:request-list").exists()


def test_standalone_admin_login_and_normal_sign_in_and_out_preserve_return(admin):
    browser = Client()
    console = reverse("admin:index")
    response = browser.get(console, follow=True)
    assert response.status_code == 200
    sign_in_url = response.redirect_chain[-1][0]
    assert urlsplit(sign_in_url).path == reverse("account_login")
    assert parse_qs(urlsplit(sign_in_url).query)["next"] == [console]
    form = reverse("account_login") + "?next=/requests/"
    response = browser.post(form, {"login": admin.username, "password": PASSWORD})
    assert response.status_code == 302 and response.url == "/requests/"
    assert browser.get(reverse("account_logout")).status_code == 200
    row = row_of(browser)
    assert browser.post(reverse("account_logout")).status_code == 302
    row.refresh_from_db()
    assert row.ending == "signed_out"


@pytest.mark.django_db(transaction=True)
def test_ending_in_progress_skips_activity_and_the_actor_guard_waits_for_commit(admin):
    row = row_of(sign_in(admin))
    uid, start, last, _, now, minutes, hours = sessions._read(str(row.pk), admin)
    state = sessions.SessionState(
        str(row.pk), uid, start, last, now, timedelta(minutes=minutes), timedelta(hours=hours)
    )
    skipped = Queue()

    def read_only_decisions():
        try:
            skipped.put((sessions._touch(state), connection.connection.info.backend_pid))
            with acting_as(row), transaction.atomic():
                return sessions.hold_acting_session(admin.pk)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            assert sessions.end_sessions(admin, "signed_out", session_id=row.pk) == 1
            decisions = pool.submit(read_only_decisions)
            result, pid = skipped.get(timeout=4)
            assert result is None
            with connection.cursor() as cursor:
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
                    if cursor.fetchone()[0]:
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("The guard did not wait for the pending ending")
        assert decisions.result(timeout=5) is False
    row.refresh_from_db()
    assert row.ending == "signed_out" and row.last_active_at == last


@pytest.mark.parametrize("early", ["module-off", "body-limit"])
def test_early_refusals_keep_their_own_no_store_header(admin, early):
    browser = sign_in(admin)
    if early == "module-off":
        response = browser.get("/quality/nonconformances/")
        assert response.status_code == 404
    else:
        response = browser.generic("POST", "/", CONTENT_LENGTH="999999999")
        assert response.status_code == 413
    assert "no-store" in response.get("Cache-Control", "")
