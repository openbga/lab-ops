"""Independent verification of the session-ending changes at 6ca70c4.

Written by the independent reviewer (round 1 verification). Adopted with one change, marked
"Adopted:": the person's own bulk ending holds its acting session first by design (every change
holds its acting account and session before anything else), so instead of asserting id order
within that transaction, the own-bulk case asserts that the one ending which locks the same rows
without the access gate or the session-ending lock before, a save of a new password alone, now
waits for the session-ending lock while holding no session row, and both finish."""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from queue import Queue
from threading import Event

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth import SESSION_KEY as AUTH_KEY
from django.core.cache import cache
from django.db import OperationalError, connection, connections, transaction
from django.test import Client
from django.urls import reverse

from accounts import sessions
from accounts.models import AccountSession, User
from core.models import AuditEvent, SessionLimit
from tests.conftest import PASSWORD
from tests.test_review_session_limits_confirm import clock, during_wait, login
from tests.test_review_session_limits_confirm3 import finish, refused_request

pytestmark = pytest.mark.django_db(transaction=True)
ENDING = AccountSession.Ending
ACTION = "account.sessions_ended"
PAGE = "/account/sessions/"
BULK = PAGE + "others/sign-out/"
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)


@pytest.fixture
def person(staff):
    return staff("verification-operator", "technical")


@pytest.fixture
def owner():
    return User.objects.create_superuser("verification-owner", "owner@example.invalid", PASSWORD)


def admin_url(person):
    return reverse("admin:accounts_user_sessions", args=[person.pk])


def close_one(row):
    return f"{PAGE}{row.pk}/sign-out/"


def logs(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "lab_ops.signin" and r.getMessage().startswith("signout.")
    ]


def in_worker(fn, pid=None):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '8s'")
            if pid is not None:
                cursor.execute("SELECT pg_backend_pid()")
                pid.put(cursor.fetchone()[0])
        return fn()
    finally:
        connections.close_all()


def wait_for_lock(worker, pids):
    pid = pids.get(timeout=5)
    deadline = time.monotonic() + 5
    with connection.cursor() as cursor:
        while time.monotonic() < deadline:
            cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
            if cursor.fetchone()[0]:
                return
            if worker.done():
                pytest.fail(f"Operation finished before the expected wait: {worker.result()!r}")
            time.sleep(0.02)
    pytest.fail("PostgreSQL did not report the expected lock wait")


@pytest.mark.parametrize(
    "when",
    [None, "2026-08-15T09:00:00", "2050-08-15T09:00:00Z", "2026-02-29T09:00:00Z"],
    ids=["absent", "naive", "future", "calendar"],
)
def test_optional_page_clock_cannot_prevent_an_ordinary_bulk_save(person, when):
    me, current = login(person)
    _, target = login(person)
    drawn = me.get(PAGE)
    assert str(target.pk) in [s.id for s in drawn.context["other_sessions"]]
    form = {"drawn_at": when} if when is not None else {}
    if "shown" in drawn.context:  # A form actually drawn by the previous revision.
        form["shown"] = drawn.context["shown"]
    me.raise_request_exception = False
    page = me.post(BULK, form, follow=True)
    target.refresh_from_db()
    current.refresh_from_db()
    assert (page.status_code, target.ending, current.ending) == (
        200,
        ENDING.SIGNED_OUT_ELSEWHERE,
        "",
    )
    assert AuditEvent.objects.get(action=ACTION).after["sessions"] == [str(target.pk)]
    assert [str(m) for m in page.context["messages"]] == ["Signed out of 1 other session."]


def test_replacement_in_same_browser_counts_as_one_later_sign_in(person):
    me, current = login(person)
    other, old = login(person)
    page = me.get(PAGE)
    other.force_login(person)
    new = AccountSession.objects.get(pk=other.session[sessions.SESSION_KEY])
    form = (
        {"drawn_at": page.context["drawn_at"]}
        if "drawn_at" in page.context
        else {"shown": page.context["shown"]}
    )
    response = me.post(BULK, form, follow=True)
    old.refresh_from_db()
    new.refresh_from_db()
    current.refresh_from_db()
    assert (old.ending, new.ending, current.ending) == (
        ENDING.SIGNED_IN_AGAIN,
        ENDING.SIGNED_OUT_ELSEWHERE,
        "",
    )
    assert AuditEvent.objects.get(action=ACTION).after["sessions"] == [str(new.pk)]
    assert [str(m) for m in response.context["messages"]] == [
        "Signed out of 1 other session. 1 of them was signed in after you opened this page."
    ]


def held_ids(ids):
    """An independent transaction can take a write lock only after a conflicting hold ends."""
    held = set()
    for pk in ids:
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM accounts_accountsession WHERE id=%s FOR NO KEY UPDATE NOWAIT",
                    [pk],
                )
                assert cursor.fetchone()
        except OperationalError as exc:
            if exc.__cause__.sqlstate != "55P03":
                raise
            held.add(pk)
    return held


@pytest.mark.parametrize("path", ["sign-out", "exception", "cleanup", "sign-in-cleanup"])
def test_target_lock_lasts_until_ending_statement(person, monkeypatch, path):
    if path == "exception":
        request, target, _policy = refused_request(person)
    else:
        browser, target = login(person)
        if "cleanup" in path:
            clock(target, HOUR, 40 * MIN)
    real = sessions._end_locked
    held_at_update = []

    with ThreadPoolExecutor(max_workers=1) as pool:

        def observe(user, ids, ending, **kwargs):
            if str(target.pk) in ids:
                held = pool.submit(in_worker, lambda: held_ids([target.pk])).result(timeout=5)
                held_at_update.append(target.pk in held)
            return real(user, ids, ending, **kwargs)

        monkeypatch.setattr(sessions, "_end_locked", observe)
        if path == "sign-out":
            assert browser.post(reverse("account_logout")).status_code == 302
        elif path == "exception":
            assert finish(request) == [sessions.ENDED_MESSAGE]
        elif path == "cleanup":
            assert sessions.close_expired(person) == 1
        else:
            login(person)
    target.refresh_from_db()
    assert (
        target.ending
        == {
            "sign-out": ENDING.SIGNED_OUT,
            "exception": ENDING.CHANGE_REFUSED,
            "cleanup": ENDING.INACTIVITY,
            "sign-in-cleanup": ENDING.INACTIVITY,
        }[path]
    )
    assert held_at_update == [True], f"{path} lost the row lock before the update"


@pytest.mark.parametrize("action", ["sign-in-again"])  # Adopted: own-bulk is the test below
def test_one_transaction_takes_overlapping_session_rows_in_consistent_order(person, action):
    browsers = sorted((login(person), login(person)), key=lambda item: item[1].pk)
    (_, small), (actor, large) = browsers
    if action == "sign-in-again":
        clock(small, HOUR, 40 * MIN)

    # Observe one successful transaction. No competing ending or deadlock is needed.
    locks = []
    with ThreadPoolExecutor(max_workers=1) as pool:

        def record(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if "accounts_accountsession" in sql and connection.in_atomic_block:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT txid_current()")
                    txid = cursor.fetchone()[0]
                held = pool.submit(in_worker, lambda: held_ids([small.pk, large.pk])).result(5)
                locks.append((txid, held))
            return result

        with connection.execute_wrapper(record):
            if action == "sign-in-again":
                actor.force_login(person)
            else:
                assert actor.post(BULK).status_code == 302
    small.refresh_from_db()
    large.refresh_from_db()
    assert (small.ending, large.ending) == (
        (ENDING.INACTIVITY, ENDING.SIGNED_IN_AGAIN)
        if action == "sign-in-again"
        else (ENDING.SIGNED_OUT_ELSEWHERE, "")
    )
    both = {small.pk, large.pk}
    assert any(held == both for _, held in locks)
    large_first = set()
    for txid, held in locks:
        if held == {large.pk}:
            large_first.add(txid)
        elif held == both:
            assert txid not in large_first, "The higher ID was held alone before both rows"


@pytest.mark.parametrize("other", ["staff", "portal"])
def test_changing_only_the_other_audiences_limits_preserves_manual_ending(
    person, owner, client_org, portal_user, other, caplog
):
    account = portal_user(client_org) if other == "staff" else person
    _, target = login(account)
    browser, _ = login(owner)
    clock(target, HOUR, 10 * MIN)
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        response = during_wait(
            target,
            lambda: browser.post(admin_url(account), {"reason": "Synthetic reassignment"}),
            lambda: SessionLimit.objects.create(
                audience=other, inactivity_minutes=5, maximum_hours=1
            ),
        )
    assert response.status_code == 302
    target.refresh_from_db()
    assert target.ending == ENDING.ADMINISTRATOR
    assert AuditEvent.objects.get(action=ACTION).after == {
        "count": 1,
        "ending": ENDING.ADMINISTRATOR,
        "sessions": [str(target.pk)],
    }
    assert logs(caplog) == [f"signout.administrator user={account.username} sessions=1"]


def test_longest_deadline_passes_during_wait(person, owner, caplog):
    _, target = login(person)
    browser, _ = login(owner)
    clock(target, 12 * HOUR - timedelta(seconds=3), MIN)
    deadline = target.started_at + 12 * HOUR

    def cross_deadline():
        with connection.cursor() as cursor:
            cursor.execute("SELECT clock_timestamp() < %s", [deadline])
            assert cursor.fetchone()[0]
            cursor.execute(
                "SELECT pg_sleep(GREATEST(0, EXTRACT(EPOCH FROM (%s - clock_timestamp()))))",
                [deadline],
            )

    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        response = during_wait(
            target, lambda: browser.post(admin_url(person), {"reason": "Review"}), cross_deadline
        )
    assert response.status_code == 302
    target.refresh_from_db()
    assert (target.ending, target.ended_at) == (ENDING.MAXIMUM, deadline)
    assert not AuditEvent.objects.filter(action=ACTION).exists()
    assert not LogEntry.objects.filter(object_id=str(person.pk)).exists()
    assert logs(caplog) == [f"signout.maximum user={person.username} sessions=1"]


@pytest.mark.parametrize("ending", ["sign-in-cleanup", "administrator"])
def test_ending_reads_activity_committed_by_stay_during_its_wait(person, owner, ending, caplog):
    editor, target = login(person)
    operator, _ = login(owner)
    clock(target, HOUR, 10 * MIN)
    pids = Queue()

    def save():
        # The new policy makes the former activity too old. Stay's update is still uncommitted.
        SessionLimit.objects.create(audience="staff", inactivity_minutes=5, maximum_hours=12)
        if ending == "sign-in-cleanup":
            login(person)
        else:
            assert (
                operator.post(admin_url(person), {"reason": "Synthetic device"}).status_code == 302
            )

    with (
        caplog.at_level(logging.INFO, logger="lab_ops.signin"),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        with transaction.atomic():
            assert editor.post(reverse("account-session-continue")).json()["signed_in"]
            observed = AccountSession.objects.get(pk=target.pk).last_active_at
            assert observed > target.last_active_at
            pending = pool.submit(in_worker, save, pids)
            wait_for_lock(pending, pids)
        pending.result(5)
    target.refresh_from_db()
    assert target.last_active_at == observed
    if ending == "sign-in-cleanup":
        assert target.ended_at is None and logs(caplog) == []
    else:
        assert target.ending == ENDING.ADMINISTRATOR and target.ended_at >= observed
        assert logs(caplog) == [f"signout.administrator user={person.username} sessions=1"]


def test_own_password_change_keeps_current_and_logs_each_cause_once(person, caplog):
    actor, current = login(person)
    _, live = login(person)
    _, abandoned = login(person)
    _, longest = login(person)
    clock(abandoned, HOUR, 40 * MIN)
    clock(longest, 13 * HOUR, MIN)
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        answer = actor.post(
            reverse("account_change_password"),
            {
                "oldpassword": PASSWORD,
                "password1": "synthetic-new-password-481!",
                "password2": "synthetic-new-password-481!",
            },
        )
    assert answer.status_code == 302 and actor.session[AUTH_KEY] == str(person.pk)
    for record in (current, live, abandoned, longest):
        record.refresh_from_db()
    assert current.ending == "" and live.ending == ENDING.NEW_PASSWORD
    assert (abandoned.ending, abandoned.ended_at) == (
        ENDING.INACTIVITY,
        abandoned.last_active_at + 30 * MIN,
    )
    assert (longest.ending, longest.ended_at) == (ENDING.MAXIMUM, longest.started_at + 12 * HOUR)
    assert sorted(logs(caplog)) == sorted(
        f"signout.{cause} user={person.username} sessions=1"
        for cause in ("inactivity", "maximum", "new_password")
    )
    assert not AuditEvent.objects.filter(action=ACTION).exists()


@pytest.mark.parametrize("action", ["administrator", "personal"])
@pytest.mark.parametrize(
    "deactivate_first", [False, True], ids=["ending-first", "deactivation-first"]
)
def test_deactivation_and_another_ending_agree_on_each_row(
    person, owner, caplog, action, deactivate_first
):
    operator, _ = login(owner)
    member, current = login(person)
    _, target = login(person)
    pids = Queue()

    def deactivate():
        person.is_active = False
        person.save(update_fields=["is_active"])

    def other():
        if action == "administrator":
            return operator.post(admin_url(person), {"reason": "Synthetic retirement"})
        return member.post(close_one(target))

    first, second = (deactivate, other) if deactivate_first else (other, deactivate)
    with (
        caplog.at_level(logging.INFO, logger="lab_ops.signin"),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        with transaction.atomic():
            first()
            worker = pool.submit(in_worker, second, pids)
            wait_for_lock(worker, pids)
        worker.result(5)
    target.refresh_from_db()
    current.refresh_from_db()
    expected = ENDING.ADMINISTRATOR if action == "administrator" else ENDING.SIGNED_OUT_ELSEWHERE
    assert target.ending == (ENDING.DEACTIVATED if deactivate_first else expected)
    assert current.ending == (
        ENDING.DEACTIVATED if deactivate_first or action == "personal" else expected
    )
    records = list(AuditEvent.objects.filter(action=ACTION))
    expected_ids = [] if deactivate_first else [str(target.pk)]
    if action == "administrator" and not deactivate_first:
        expected_ids.append(str(current.pk))
    assert sorted(pk for e in records for pk in e.after["sessions"]) == sorted(expected_ids)
    expected_logs = []
    if expected_ids:
        expected_logs.append(
            f"signout.{expected} user={person.username} sessions={len(expected_ids)}"
        )
    if len(expected_ids) < 2:
        expected_logs.append(
            f"signout.deactivated user={person.username} sessions={2 - len(expected_ids)}"
        )
    assert sorted(logs(caplog)) == sorted(expected_logs)


def test_single_ending_settles_another_abandoned_row_as_documented(person, caplog):
    me, _ = login(person)
    _, target = login(person)
    _, abandoned = login(person)
    clock(abandoned, HOUR, 40 * MIN)
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        assert me.post(close_one(target)).status_code == 302
    abandoned.refresh_from_db()
    assert (abandoned.ending, abandoned.ended_at) == (
        ENDING.INACTIVITY,
        abandoned.last_active_at + 30 * MIN,
    )
    assert AuditEvent.objects.get(action=ACTION).after["sessions"] == [str(target.pk)]
    assert sorted(logs(caplog)) == sorted(
        [
            f"signout.inactivity user={person.username} sessions=1",
            f"signout.signed_out_elsewhere user={person.username} sessions=1",
        ]
    )


def test_expiry_only_admin_save_describes_the_record_it_wrote(person, owner, caplog):
    operator, _ = login(owner)
    _, abandoned = login(person)
    clock(abandoned, HOUR, 40 * MIN)
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        result = operator.post(admin_url(person), {"reason": "Synthetic device"}, follow=True)
    abandoned.refresh_from_db()
    assert abandoned.ending == ENDING.INACTIVITY
    assert logs(caplog) == [f"signout.inactivity user={person.username} sessions=1"]
    assert not AuditEvent.objects.filter(action=ACTION).exists()
    (message,) = [str(m) for m in result.context["messages"]]
    assert "nothing changed" not in message.lower(), message


def test_explicit_signout_uses_policy_at_its_last_row_wait(person, caplog):
    browser, target = login(person)
    clock(target, 2 * HOUR, MIN)
    at_write, proceed = Event(), Event()
    pids = Queue()

    def submit():
        def observe(execute, sql, params, many, context):
            if "accounts_accountsession" in sql and "UPDATE " in sql and "ending" in sql:
                at_write.set()
                assert proceed.wait(8)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(observe):
            return browser.post(reverse("account_logout"))

    with (
        caplog.at_level(logging.INFO, logger="lab_ops.signin"),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        pending = pool.submit(in_worker, submit, pids)
        try:
            assert at_write.wait(8)
            with transaction.atomic():
                # With a retained lock, the ending already owns this row. Otherwise our
                # successful lock stays held until commit and the ending must read after it.
                already_held = target.pk in held_ids([target.pk])
                proceed.set()
                if not already_held:
                    wait_for_lock(pending, pids)
                    SessionLimit.objects.create(
                        audience="staff", inactivity_minutes=30, maximum_hours=1
                    )
        finally:
            proceed.set()
        assert pending.result(5).status_code == 302
    target.refresh_from_db()
    if already_held:
        assert target.ending == ENDING.SIGNED_OUT and logs(caplog) == []
    else:
        assert (target.ending, target.ended_at, logs(caplog)) == (
            ENDING.MAXIMUM,
            target.started_at + HOUR,
            [f"signout.maximum user={person.username} sessions=1"],
        )


@pytest.mark.parametrize(("mode", "expected"), [("missing-header", 403), ("short-chain", 500)])
def test_adopted_proxy_cases_match_their_named_outcomes(person, settings, mode, expected):
    cache.clear()
    settings.ALLAUTH_TRUSTED_PROXY_COUNT = 2
    settings.ALLAUTH_TRUSTED_CLIENT_IP_HEADER = (
        "X-Review-Client" if mode == "missing-header" else None
    )
    browser = Client(raise_request_exception=False)
    response = browser.post(
        reverse("account_login"),
        {"login": person.username, "password": PASSWORD},
        HTTP_X_FORWARDED_FOR="192.0.2.4",
    )
    assert response.status_code == expected
    assert (
        AUTH_KEY not in browser.session and not AccountSession.objects.filter(user=person).exists()
    )


def test_general_references_qualify_the_own_account_exception(owner):
    other, old = login(owner)
    browser, current = login(owner)
    assert browser.post(admin_url(owner), {"reason": "Synthetic device"}).status_code == 302
    old.refresh_from_db()
    current.refresh_from_db()
    assert old.ending == ENDING.ADMINISTRATOR and current.ended_at is None
    docs = Path(__file__).resolve().parents[1] / "docs"
    unqualified = []
    for name, claim in [
        ("product.md", "ends every session of an account"),
        ("architecture.md", "ends every row of an account"),
        ("safeguards.md", "end every session of an account"),
    ]:
        for paragraph in (docs / name).read_text().split("\n\n"):
            text = " ".join(paragraph.lower().split())
            start = text.find(claim)
            if start >= 0:
                statement = text[start:].split(". ", 1)[0]
                if not any(word in statement for word in ("except", "own account", "other than")):
                    unqualified.append(name)
    assert not unqualified, f"The administrator's current session survives: {unqualified}"


def test_own_bulk_and_a_password_alone_take_turns_on_the_session_ending_lock(person, monkeypatch):
    """Adopted (own-bulk): the bulk ending holds its acting session, then locks the others; a new
    password saved alone locks every row but takes the account's session-ending lock first, so it
    waits holding no row, and the two finish without a deadlock."""
    from accounts import session_list

    browsers = sorted((login(person), login(person)), key=lambda item: item[1].pk)
    (_, small), (actor, large) = browsers
    real = session_list.end_session_rows
    holding, release = Event(), Event()

    def pausing(user, ending, **kwargs):
        holding.set()  # the acting session (large) is held FOR SHARE by now
        assert release.wait(8)
        return real(user, ending, **kwargs)

    monkeypatch.setattr(session_list, "end_session_rows", pausing)

    def new_password():
        account = User.objects.get(pk=person.pk)
        account.set_password("synthetic-replacement-password-77")
        account.save(update_fields=["password"])

    pids = Queue()
    with ThreadPoolExecutor(max_workers=2) as pool:
        bulk = pool.submit(in_worker, lambda: actor.post(BULK))
        assert holding.wait(8)
        password = pool.submit(in_worker, new_password, pids)
        pid = pids.get(timeout=5)
        deadline = time.monotonic() + 5
        with connection.cursor() as cursor:
            while time.monotonic() < deadline:
                # Blocked by the bulk ending's transaction, on an advisory lock.
                cursor.execute(
                    "SELECT cardinality(pg_blocking_pids(%s)) > 0 AND EXISTS (SELECT 1 FROM "
                    "pg_locks WHERE pid = %s AND NOT granted AND locktype = 'advisory')",
                    [pid, pid],
                )
                if cursor.fetchone()[0]:
                    break
                assert not password.done(), password.result()
                time.sleep(0.02)
            else:
                pytest.fail("The new password did not wait for the session-ending lock")
        # Waiting on the session-ending lock, it holds neither row.
        assert held_ids([small.pk]) == set()
        release.set()
        assert bulk.result(10).status_code == 302
        password.result(10)
    small.refresh_from_db()
    large.refresh_from_db()
    assert small.ending == ENDING.SIGNED_OUT_ELSEWHERE
    assert large.ending == ENDING.NEW_PASSWORD
