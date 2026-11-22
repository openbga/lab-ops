"""Narrow confirmation of the sign-in and password access-gate changes."""

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

import pytest
from django.core.cache import cache
from django.db import OperationalError, connection, connections, transaction
from django.test import Client
from django.urls import reverse

from accounts import acting, sessions
from accounts.models import AccountSession, User
from accounts.session_list import SESSION_ENDINGS
from accounts.setup import setup_lab
from clients.models import Client as Organization
from core.models import AuditEvent
from desk.tests.test_review_acting_account_until_commit import _admin_deactivation
from tests.conftest import PASSWORD
from tests.test_review_session_limits_confirm import login
from tests.test_review_session_limits_verify import acting_as
from tests.test_review_session_list_verify import in_worker
from tests.test_session_limits import NEW_PASSWORD

pytestmark = pytest.mark.django_db(transaction=True)
ENDING = AccountSession.Ending


@pytest.fixture
def person(staff):
    cache.clear()
    return staff("second-confirmation", "technical")


@pytest.fixture
def owner():
    return User.objects.create_superuser("confirmation-owner", "owner@example.invalid", PASSWORD)


def submit_login(account):
    browser = Client()
    result = browser.post(
        reverse("account_login"), {"login": account.username, "password": PASSWORD}
    )
    assert result.status_code == 302
    return AccountSession.objects.get(pk=browser.session[sessions.SESSION_KEY])


def upgrade_hash(settings):
    settings.PASSWORD_HASHERS = [
        "tests.test_session_limits.StrongerHasher",
        "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    ]


def save_password(account):
    copy = User.objects.get(pk=account.pk)
    copy.set_password(NEW_PASSWORD)
    copy.save(update_fields=["password"])


def holds_ending_gate(account):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() "
            "AND locktype='advisory' AND granted AND classid=%s AND objid=%s "
            "AND mode='ExclusiveLock')",
            [SESSION_ENDINGS, account.pk % acting.INT4],
        )
        return cursor.fetchone()[0]


def account_write_held(account):
    """Detect an existing conflicting row lock using a separate, non-waiting transaction."""

    def probe():
        try:
            with transaction.atomic():
                User.objects.select_for_update(no_key=True, nowait=True).only("pk").get(
                    pk=account.pk
                )
            return False
        except OperationalError as exc:
            if exc.__cause__.sqlstate != "55P03":
                raise
            return True
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(probe).result(timeout=5)


@pytest.mark.parametrize("per_request", [False, True], ids=["default", "atomic-request"])
@pytest.mark.parametrize("rehash", [False, True], ids=["current-hash", "upgrade"])
def test_sign_in_and_password_save_agree_on_ending_lock_vs_account_write_order(
    person, settings, monkeypatch, per_request, rehash
):
    """Observe two successful forms of account update separately; compare their lock order."""
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", per_request)
    if rehash:
        upgrade_hash(settings)
    held_on_first_acquisition = []
    last_login_writes = []

    def at_sign_in(execute, sql, params, many, context):
        if "pg_advisory_xact_lock(" in sql and list(params) == [
            SESSION_ENDINGS,
            person.pk % acting.INT4,
        ]:
            if not holds_ending_gate(person):
                held_on_first_acquisition.append(account_write_held(person))
        if sql.startswith('UPDATE "accounts_user" SET "last_login"'):
            last_login_writes.append(True)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(at_sign_in):
        row = submit_login(person)
    person.refresh_from_db()
    assert last_login_writes == [True] and person.last_login is not None
    assert person.password.startswith("pbkdf2_stronger$") == rehash

    gate_at_password_write = []

    def at_password(execute, sql, params, many, context):
        if sql.startswith('UPDATE "accounts_user" SET "password"'):
            gate_at_password_write.append(holds_ending_gate(person))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(at_password):
        save_password(person)
    row.refresh_from_db()
    assert row.ending == ENDING.NEW_PASSWORD and gate_at_password_write == [True]
    assert not any(held_on_first_acquisition), (
        "Sign-in already held the account write lock when it first requested the ending lock; "
        "the password save held the ending lock before writing the same account."
    )


def observed_wait(worker, pid, holder):
    """Return the lock requested behind the holder or queued writer, or None on completion."""
    deadline = time.monotonic() + 5
    with connection.cursor() as cursor:
        while not worker.done():
            cursor.execute(
                "SELECT locktype, classid, mode FROM pg_locks WHERE pid=%s AND NOT granted "
                "AND %s=ANY(pg_blocking_pids(%s))",
                [pid, holder, pid],
            )
            wait = cursor.fetchone()
            if wait:
                return wait
            if time.monotonic() >= deadline:
                pytest.fail("Operation neither completed nor waited on the expected connection")
            time.sleep(0.01)
    return None


def overlap(hold, operation):
    """Finish a legitimate change while a second operation either proceeds or waits for it."""
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            hold()
            future = pool.submit(in_worker, operation, pids)
            waited = observed_wait(
                future, pids.get(timeout=5), connection.connection.info.backend_pid
            )
        result = future.result(timeout=6)
    return waited, result


@pytest.mark.parametrize("password_first", [False, True], ids=["access-first", "password-first"])
@pytest.mark.parametrize("access", ["admin-deactivation", "portal-move", "setup-role"])
def test_password_serializes_with_access_changes_at_the_access_gate(
    person, owner, access, portal_user, client_org, password_first
):
    account = portal_user(client_org) if access == "portal-move" else person
    _, prior = login(account)
    if access == "admin-deactivation":
        deactivate = _admin_deactivation(owner, account)

        def change():
            assert deactivate().status_code == 302
    elif access == "portal-move":
        destination = Organization.objects.create(
            name="New Example Clinic", account_number="SLCONF2"
        )

        def change():
            account.client = destination
            account.changed_by = owner
            account.save(update_fields=["client"])
    else:

        def change():
            assert setup_lab(
                lab_name="Example Lab", username=account.username, roles=["supervisor"]
            ).changed

    def password():
        save_password(account)

    first, second = (password, change) if password_first else (change, password)
    waited, _ = overlap(first, second)
    account.refresh_from_db()
    prior.refresh_from_db()
    assert account.check_password(NEW_PASSWORD)
    if access == "admin-deactivation":
        assert not account.is_active
        assert prior.ending == (ENDING.NEW_PASSWORD if password_first else ENDING.DEACTIVATED)
    elif access == "portal-move":
        assert account.client_id == destination.pk and prior.ending == ENDING.NEW_PASSWORD
    else:
        assert (
            account.groups.filter(name="supervisor").exists()
            and prior.ending == ENDING.NEW_PASSWORD
        )
        assert AuditEvent.objects.filter(
            action="setup.account_roles_added", object_id=str(account.pk)
        ).exists()
    assert waited == (
        "advisory",
        acting.ACCESS_GATE,
        "ExclusiveLock" if password_first else "ShareLock",
    )


@pytest.mark.parametrize("operation", ["ordinary-login", "hash-upgrade-login", "password"])
def test_access_gate_documentation_matches_setup_role_change(person, settings, operation):
    if operation == "hash-upgrade-login":
        upgrade_hash(settings)

    def setup():
        setup_lab(lab_name="Example Lab", username=person.username, roles=["supervisor"])

    def update():
        return save_password(person) if operation == "password" else submit_login(person)

    waited, row = overlap(setup, update)
    person.refresh_from_db()
    assert person.groups.filter(name="supervisor").exists()
    if operation == "password":
        assert person.check_password(NEW_PASSWORD)
    else:
        assert row.user_id == person.pk and row.ended_at is None
        assert person.password.startswith("pbkdf2_stronger$") == (operation == "hash-upgrade-login")
    if operation == "ordinary-login":
        assert waited is None
    elif waited == ("advisory", acting.ACCESS_GATE, "ShareLock"):
        if operation == "password":
            assert (
                "a password change saves the password only and waits for no gate"
                not in " ".join(acting.__doc__.split())
            )
        else:
            architecture = (
                Path(__file__).resolve().parents[1] / "docs/architecture.md"
            ).read_text()
            paragraph = next(
                (p for p in architecture.split("\n\n") if "Updates that take no access away" in p),
                "",
            )
            claim = " ".join(paragraph.split()).partition("Updates that take no access away")[2]
            if "such as a sign-in" in claim and "do not wait for the gate" in claim:
                assert "hash" in claim.partition("The account page carries")[0], (
                    "A sign-in that upgraded the hash waited for the access gate."
                )
    else:
        assert waited is None


def test_password_change_keeps_work_in_its_own_session_moving(person):
    browser, current = login(person)
    _, peer = login(person)

    def held_change():
        assert acting.hold_actor(person).pk == person.pk

    def submit():
        return browser.post(
            reverse("account_change_password"),
            {"oldpassword": PASSWORD, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
        )

    with acting_as(current):
        waited, response = overlap(held_change, submit)
    current.refresh_from_db()
    peer.refresh_from_db()
    person.refresh_from_db()
    assert response.status_code == 302 and person.check_password(NEW_PASSWORD)
    assert current.ended_at is None and peer.ending == ENDING.NEW_PASSWORD
    assert waited is None


@pytest.mark.parametrize("operation", ["personal-ending", "admin-ending"])
def test_new_sign_in_takes_its_turn_with_an_ending(person, owner, operation):
    browser, original = login(person)
    _, other = login(person)
    admin, _ = login(owner)

    def end():
        if operation == "personal-ending":
            response = browser.post("/account/sessions/others/sign-out/")
        else:
            response = admin.post(
                reverse("admin:accounts_user_sessions", args=[person.pk]),
                {"reason": "Synthetic review"},
            )
        assert response.status_code == 302

    waited, fresh = overlap(end, lambda: submit_login(person))
    original.refresh_from_db()
    other.refresh_from_db()
    fresh.refresh_from_db()
    expected = ENDING.ADMINISTRATOR if operation == "admin-ending" else ENDING.SIGNED_OUT_ELSEWHERE
    assert original.ending == (ENDING.ADMINISTRATOR if operation == "admin-ending" else "")
    assert other.ending == expected and fresh.ended_at is None
    event = AuditEvent.objects.get(action="account.sessions_ended")
    assert set(event.after["sessions"]) == (
        {str(other.pk), str(original.pk)} if operation == "admin-ending" else {str(other.pk)}
    )
    assert waited == ("advisory", SESSION_ENDINGS, "ExclusiveLock")


@pytest.mark.parametrize("operation", ["sign-in", "password"])
def test_stay_and_account_change_agree_on_activity_and_ending(person, operation):
    browser, row = login(person)

    def stay():
        assert browser.post(reverse("account-session-continue")).json()["signed_in"]

    def update():
        return submit_login(person) if operation == "sign-in" else save_password(person)

    original = row.last_active_at
    waited, fresh = overlap(stay, update)
    row.refresh_from_db()
    assert row.last_active_at > original
    if operation == "sign-in":
        assert waited is None and fresh.ended_at is None and row.ended_at is None
    else:
        assert waited and waited[0] in {"transactionid", "tuple"}
        assert row.ending == ENDING.NEW_PASSWORD and row.ended_at >= row.last_active_at


def test_password_can_follow_a_queued_access_change_without_a_cycle(person):
    browser, current = login(person)
    _, peer = login(person)
    role_pids, password_pids = Queue(), Queue()

    def grant_role():
        return setup_lab(lab_name="Example Lab", username=person.username, roles=["supervisor"])

    def password():
        return browser.post(
            reverse("account_change_password"),
            {
                "oldpassword": PASSWORD,
                "password1": NEW_PASSWORD,
                "password2": NEW_PASSWORD,
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        with transaction.atomic(), acting_as(current):
            assert acting.hold_actor(person).pk == person.pk
            role = pool.submit(in_worker, grant_role, role_pids)
            role_pid = role_pids.get(timeout=5)
            role_wait = observed_wait(role, role_pid, connection.connection.info.backend_pid)
            assert role_wait == ("advisory", acting.ACCESS_GATE, "ExclusiveLock")
            change = pool.submit(in_worker, password, password_pids)
            password_wait = observed_wait(change, password_pids.get(timeout=5), role_pid)
        assert role.result(timeout=6).changed
        assert change.result(timeout=6).status_code == 302
    person.refresh_from_db()
    current.refresh_from_db()
    peer.refresh_from_db()
    assert person.check_password(NEW_PASSWORD) and person.groups.filter(name="supervisor").exists()
    assert current.ended_at is None and peer.ending == ENDING.NEW_PASSWORD
    assert password_wait == ("advisory", acting.ACCESS_GATE, "ShareLock")
