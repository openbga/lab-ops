"""Confirmation of the session-ending transaction, ordering, and form contracts."""

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta

import pytest
from django.db import OperationalError, connection, connections, transaction
from django.test import Client
from django.urls import reverse

from accounts import sessions
from accounts.acting import ACCESS_GATE, INT4
from accounts.models import AccountSession, User
from accounts.session_list import SESSION_ENDINGS
from clients.models import Client as Organization
from core.models import AuditEvent, SessionLimit
from tests.conftest import PASSWORD
from tests.test_review_session_limits_confirm import clock, during_wait, login
from tests.test_session_limits import NEW_PASSWORD

pytestmark = pytest.mark.django_db(transaction=True)
ENDING = AccountSession.Ending
ACTION = "account.sessions_ended"
BULK = "/account/sessions/others/sign-out/"
HOUR = timedelta(hours=1)
MIN = timedelta(minutes=1)


@pytest.fixture
def person(staff):
    return staff("session-confirmation-operator", "technical")


@pytest.fixture
def owner():
    return User.objects.create_superuser(
        "session-confirmation-owner", "owner@example.invalid", PASSWORD
    )


def ending_logs(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "lab_ops.signin" and r.getMessage().startswith("signout.")
    ]


@pytest.mark.parametrize("helper", ["end_session_rows", "close_expired"])
@pytest.mark.parametrize("nested", [False, True], ids=["standalone", "caller-catches"])
def test_failed_ending_rolls_back_its_write_and_its_callers_work(
    person, monkeypatch, caplog, helper, nested
):
    _, row = login(person)
    clock(row, HOUR, 40 * MIN)
    original_name = person.first_name
    real = sessions._end_locked

    class InterruptedEnding(Exception):
        pass

    def interrupt_after_write(*args, **kwargs):
        result = real(*args, **kwargs)
        assert result and AccountSession.objects.get(pk=row.pk).ending == ENDING.INACTIVITY
        raise InterruptedEnding

    monkeypatch.setattr(sessions, "_end_locked", interrupt_after_write)

    def attempt():
        with pytest.raises(InterruptedEnding):
            if helper == "end_session_rows":
                sessions.end_session_rows(person, ENDING.ADMINISTRATOR)
            else:
                sessions.close_expired(person)

    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        if nested:
            with transaction.atomic():
                User.objects.filter(pk=person.pk).update(first_name="Part of the same change")
                attempt()
        else:
            attempt()
    row.refresh_from_db()
    person.refresh_from_db()
    assert (row.ended_at, row.ending, person.first_name, ending_logs(caplog)) == (
        None,
        "",
        original_name,
        [],
    )


@pytest.mark.parametrize("spelling", ["uuid", "hex"])
def test_targeted_ending_identifies_a_session_by_its_uuid(person, spelling):
    _, target = login(person)
    _, peer = login(person)
    value = target.pk if spelling == "uuid" else target.pk.hex
    count = sessions.end_sessions(person, ENDING.SIGNED_OUT, session_id=value)
    target.refresh_from_db()
    peer.refresh_from_db()
    assert (count, target.ending, peer.ending) == (1, ENDING.SIGNED_OUT, "")


@pytest.mark.parametrize("action", ["single", "sign-in-again"])
def test_combined_selection_rereads_revived_peer_and_counts_only_target(
    person, monkeypatch, caplog, action
):
    actor, current = login(person)
    target_browser, target = login(person)
    _, abandoned = login(person)
    _, live = login(person)
    clock(abandoned, HOUR, 40 * MIN)
    selections = []
    original = sessions._lock_open

    def observe(user, **kwargs):
        ids = original(user, **kwargs)
        selections.append(ids)
        return ids

    monkeypatch.setattr(sessions, "_lock_open", observe)

    def submit():
        if action == "single":
            assert actor.post(f"/account/sessions/{target.pk}/sign-out/").status_code == 302
        else:
            target_browser.force_login(person)

    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        during_wait(
            target,
            submit,
            lambda: SessionLimit.objects.create(
                audience="staff", inactivity_minutes=60, maximum_hours=12
            ),
        )
    assert selections == [sorted(map(str, (abandoned.pk, target.pk)))]
    for row in (current, target, abandoned, live):
        row.refresh_from_db()
    assert current.ended_at is None and abandoned.ended_at is None and live.ended_at is None
    if action == "single":
        assert target.ending == ENDING.SIGNED_OUT_ELSEWHERE
        assert AuditEvent.objects.get(action=ACTION).after["sessions"] == [str(target.pk)]
        assert ending_logs(caplog) == [
            f"signout.signed_out_elsewhere user={person.username} sessions=1"
        ]
    else:
        assert target.ending == ENDING.SIGNED_IN_AGAIN
        assert (
            AccountSession.objects.get(pk=target_browser.session[sessions.SESSION_KEY]).ended_at
            is None
        )
        assert not AuditEvent.objects.filter(action=ACTION).exists() and ending_logs(caplog) == []


@dataclass
class Locks:
    rows: set
    gates: dict

    def excludes(self, other):
        return any(
            key in other.gates and "ExclusiveLock" in (mode, other.gates[key])
            for key, mode in self.gates.items()
        )


def observe_locks(person, named_rows):
    """Observe one successful operation from another connection using non-waiting reads."""
    pid = connection.connection.info.backend_pid

    def read():
        try:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT classid, mode FROM pg_locks WHERE pid=%s AND locktype='advisory' "
                    "AND granted AND objid=%s AND classid IN (%s,%s)",
                    [pid, person.pk % INT4, ACCESS_GATE, SESSION_ENDINGS],
                )
                gates = dict(cur.fetchall())
            held = set()
            for name, row in named_rows.items():
                try:
                    with transaction.atomic():
                        type(row).objects.select_for_update(nowait=True, no_key=True).only(
                            "pk"
                        ).get(pk=row.pk)
                except OperationalError as exc:
                    if exc.__cause__.sqlstate != "55P03":
                        raise
                    held.add(name)
            return Locks(held, gates)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(read).result(timeout=5)


def test_repeat_sign_in_and_own_bulk_share_a_gate_or_take_their_rows_in_the_same_order(
    person, monkeypatch
):
    """Compare serial executions, using a live higher ID and idle lower ID for each."""
    original = sessions._lock_open

    def run(action):
        (_, lower), (actor, higher) = sorted(
            (login(person), login(person)), key=lambda pair: pair[1].pk
        )
        clock(lower, HOUR, 40 * MIN)
        named = {"lower": lower, "higher": higher}
        labels = {str(row.pk): name for name, row in named.items()}
        order, first = [], []

        def observe(user, **kwargs):
            if not first:
                before = observe_locks(person, named)
                first.append(before)
                assert len(before.rows) <= 1
                order.extend(before.rows)
            selected = original(user, **kwargs)
            for pk in selected:
                if pk in labels and labels[pk] not in order:
                    order.append(labels[pk])
            return selected

        with monkeypatch.context() as patch:
            patch.setattr(sessions, "_lock_open", observe)
            if action == "bulk":
                assert actor.post(BULK).status_code == 302
            else:
                actor.force_login(person)
        lower.refresh_from_db()
        higher.refresh_from_db()
        assert lower.ending == ENDING.INACTIVITY
        assert higher.ending == ("" if action == "bulk" else ENDING.SIGNED_IN_AGAIN)
        assert set(order) == {"lower", "higher"}
        return order, first[0]

    bulk, bulk_locks = run("bulk")
    repeat, repeat_locks = run("repeat")
    assert bulk == repeat or bulk_locks.excludes(repeat_locks), (
        bulk,
        bulk_locks,
        repeat,
        repeat_locks,
    )


def password_input(route, person, browser, owner, settings):
    if route == "admin-target":
        operator, _ = login(owner)
        url = reverse("admin:auth_user_password_change", args=[person.pk])
        return (
            operator,
            url,
            {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD, "usable_password": "true"},
        )
    if route == "admin-own":
        return (
            browser,
            reverse("admin:password_change"),
            {
                "old_password": PASSWORD,
                "new_password1": NEW_PASSWORD,
                "new_password2": NEW_PASSWORD,
            },
        )
    if route == "own":
        return (
            browser,
            reverse("account_change_password"),
            {"oldpassword": PASSWORD, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
        )
    settings.PASSWORD_HASHERS = [
        "tests.test_session_limits.StrongerHasher",
        "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    ]
    return Client(), reverse("account_login"), {"login": person.username, "password": PASSWORD}


@pytest.mark.parametrize("route", ["admin-target", "admin-own", "own", "hash-upgrade"])
def test_password_uses_the_ending_gate_before_account_or_session_rows(
    person, owner, settings, monkeypatch, route
):
    account = owner if route == "admin-own" else person
    browser, current = login(account)
    _, peer = login(account)
    client, url, data = password_input(route, account, browser, owner, settings)
    before = []
    selection = []
    original = sessions._lock_open

    def on_selection(user, **kwargs):
        if user.pk == account.pk and not selection:
            selection.append(
                observe_locks(account, {"account": account, "peer": peer, "current": current})
            )
        return original(user, **kwargs)

    monkeypatch.setattr(sessions, "_lock_open", on_selection)

    def record(execute, sql, params, many, context):
        if 'UPDATE "accounts_user" SET "password"' in sql:
            before.append(observe_locks(account, {"account": account, "peer": peer}))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(record):
        response = client.post(url, data)
    current.refresh_from_db()
    peer.refresh_from_db()
    account.refresh_from_db()
    assert response.status_code == 302, response.context["form"].errors
    assert peer.ending == ENDING.NEW_PASSWORD
    assert current.ending == ("" if route in ("own", "admin-own") else ENDING.NEW_PASSWORD)
    assert account.check_password(PASSWORD if route == "hash-upgrade" else NEW_PASSWORD)
    assert len(selection) == 1 and selection[0].rows == set()
    assert selection[0].gates.get(SESSION_ENDINGS) == "ExclusiveLock"
    assert len(before) == 1 and before[0].rows == {"peer"}
    assert before[0].gates.get(SESSION_ENDINGS) == "ExclusiveLock"


def test_password_only_and_mixed_access_saves_have_compatible_account_and_session_lock_order(
    portal_user, client_org, owner, settings, monkeypatch
):
    person = portal_user(client_org, "session-confirmation-portal")
    destination = Organization.objects.create(
        name="Other Example Clinic", account_number="CNFOTHER"
    )
    # Inspect the locks of each successful save separately.
    real = sessions._lock_open

    def run(mode):
        browser, peer = login(person)
        named = {"account": person, "peer": peer}
        observations = {}
        if mode == "password-only":
            client, url, data = password_input("admin-target", person, browser, owner, settings)

        def at_selection(user, **kwargs):
            observations.setdefault("selection", observe_locks(person, named))
            return real(user, **kwargs)

        def at_write(execute, sql, params, many, context):
            if 'UPDATE "accounts_user" SET "password"' in sql:
                observations["write"] = observe_locks(person, named)
            return execute(sql, params, many, context)

        with monkeypatch.context() as patch, connection.execute_wrapper(at_write):
            patch.setattr(sessions, "_lock_open", at_selection)
            if mode == "password-only":
                assert client.post(url, data).status_code == 302
            else:
                person.set_password("synthetic-combined-change-7925")
                person.client = destination
                person.changed_by = owner
                person.save(update_fields=["password", "client"])
        peer.refresh_from_db()
        assert peer.ending == ENDING.NEW_PASSWORD
        return observations

    plain, mixed = run("password-only"), run("with-access")
    person.refresh_from_db()
    assert person.client_id == destination.pk
    assert "peer" in plain["write"].rows and "peer" in mixed["write"].rows
    assert ("account" in plain["selection"].rows) == (
        "account" in mixed["selection"].rows
    ) or plain["selection"].excludes(mixed["selection"]), (plain, mixed)


@pytest.mark.parametrize(
    "value", ["2025-13-01T12:00:00Z", "2026-11-21T12:61:00Z", "not a time", ""]
)
def test_invalid_reading_changes_only_confirmation_wording(person, value):
    actor, current = login(person)
    _, target = login(person)
    response = actor.post(BULK, {"drawn_at": value}, follow=True)
    current.refresh_from_db()
    target.refresh_from_db()
    assert response.status_code == 200 and current.ended_at is None
    assert target.ending == ENDING.SIGNED_OUT_ELSEWHERE
    assert AuditEvent.objects.get(action=ACTION).after["sessions"] == [str(target.pk)]
    assert [str(m) for m in response.context["messages"]] == ["Signed out of 1 other session."]


def test_own_account_confirmation_and_destination_agree_on_kept_browser(owner):
    _, peer = login(owner)
    browser, current = login(owner)
    response = browser.post(
        reverse("admin:accounts_user_sessions", args=[owner.pk]),
        {"reason": "Synthetic confirmation"},
        follow=True,
    )
    peer.refresh_from_db()
    current.refresh_from_db()
    assert (
        response.status_code == 200
        and peer.ending == ENDING.ADMINISTRATOR
        and current.ended_at is None
    )
    assert any("This one stays signed in" in str(m) for m in response.context["messages"])
    sections = dict(response.context["adminform"].fieldsets)
    description = sections["Sessions"]["description"].lower()
    claim = "signs every browser of the account out"
    if claim in description:
        assert any(
            qualification in description
            for qualification in ("except", "other than", "own account")
        ), description


@pytest.mark.parametrize("action", ["single", "bulk", "admin-own", "admin-target"])
def test_expiry_only_confirmation_agrees_with_rows_and_logs(person, owner, caplog, action):
    account = owner if action == "admin-own" else person
    _, expired = login(account)
    if action == "admin-target":
        actor, current = login(owner)
    else:
        actor, current = login(account)
    clock(expired, HOUR, 40 * MIN)
    url = {
        "single": f"/account/sessions/{expired.pk}/sign-out/",
        "bulk": BULK,
        "admin-own": reverse("admin:accounts_user_sessions", args=[account.pk]),
        "admin-target": reverse("admin:accounts_user_sessions", args=[account.pk]),
    }[action]
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        response = actor.post(url, {"reason": "Synthetic check"}, follow=True)
    expected = {
        "single": "That session had already ended, so it was not signed out.",
        "bulk": "No other session was open, so none was signed out.",
        "admin-own": (
            "None of your other sessions was open, so none was ended. "
            "The session you are using stays signed in."
        ),
        "admin-target": f"No session of {account.username} was open, so none was ended.",
    }[action]
    expired.refresh_from_db()
    current.refresh_from_db()
    assert (
        expired.ending == ENDING.INACTIVITY
        and expired.ended_at == expired.last_active_at + 30 * MIN
    )
    assert current.ended_at is None and not AuditEvent.objects.filter(action=ACTION).exists()
    assert ending_logs(caplog) == [f"signout.inactivity user={account.username} sessions=1"]
    assert response.status_code == 200 and [str(m) for m in response.context["messages"]] == [
        expected
    ]


def test_menu_sign_out_reads_the_new_limit_after_its_initial_row_wait(person, caplog):
    browser, row = login(person)
    clock(row, 2 * HOUR, MIN)
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        response = during_wait(
            row,
            lambda: browser.post(reverse("account_logout")),
            lambda: SessionLimit.objects.create(
                audience="staff", inactivity_minutes=30, maximum_hours=1
            ),
        )
    row.refresh_from_db()
    assert response.status_code == 302
    assert (row.ending, row.ended_at) == (ENDING.MAXIMUM, row.started_at + HOUR)
    assert ending_logs(caplog) == [f"signout.maximum user={person.username} sessions=1"]
