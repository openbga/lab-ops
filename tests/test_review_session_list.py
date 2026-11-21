"""Independent regression checks for the session list. All accounts and addresses are synthetic.

Written by the independent reviewer (round 1 at 86e3571; the original is kept outside the
repository with the review report). Adopted with these changes, each marked "Adopted:" below: the
bulk confirmation posts the page's reading time (drawn_at), which replaced the listed ids, so two
tests read the listed sessions instead; the
End sessions page no longer shows the latest activity at all; and a password form whose proxy
settings give no usable address is refused before sign-in, as it was before this change, so that
test asserts nothing is recorded instead of a sign-in."""

import ipaddress
import json
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from queue import Queue
from urllib.parse import urlsplit

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth import SESSION_KEY as AUTH_KEY
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.functions import Now
from django.test import Client
from django.urls import reverse

from accounts import sessions
from accounts.adapter import ClosedSignupAdapter
from accounts.models import AccountSession, User
from accounts.roles import ROLES
from core import security_settings, session_limits
from core.models import AuditEvent, SessionLimit
from tests.conftest import PASSWORD

pytestmark = pytest.mark.django_db
PAGE = "/account/sessions/"
ALL = PAGE + "others/sign-out/"
ACTION = "account.sessions_ended"
Ending = AccountSession.Ending


@pytest.fixture
def person(staff):
    return staff("review-person", "technical")


@pytest.fixture
def owner():
    return User.objects.create_superuser("review-owner", "owner@example.invalid", PASSWORD)


def login(user):
    client = Client()
    client.force_login(user)
    return client


def row(client):
    return AccountSession.objects.get(pk=client.session[sessions.SESSION_KEY])


def single(session):
    return f"{PAGE}{session.pk}/sign-out/"


def admin_url(user):
    return reverse("admin:accounts_user_sessions", args=[user.pk])


def notices(page):
    return [str(message) for message in page.context["messages"]]


def no_store(response):
    assert "no-store" in response.headers.get("Cache-Control", "").split(", ")


def set_minutes(admin, minutes):
    current = session_limits.limits()[0]
    session_limits.save_limits(
        actor=admin,
        audience=SessionLimit.Audience.STAFF,
        expected_revision=current.revision,
        new={**session_limits.values(current), "inactivity_minutes": minutes},
        reason="Synthetic shift schedule",
    )


def age(session, minutes):
    AccountSession.objects.filter(pk=session.pk).update(
        started_at=Now() - timedelta(minutes=minutes + 1),
        last_active_at=Now() - timedelta(minutes=minutes),
    )


@pytest.mark.parametrize("audience", [*ROLES, "no-role", "portal", "owner", "shared", "anonymous"])
def test_direct_pages_and_actions_for_each_audience(
    audience, staff, client_org, portal_user, owner
):
    if audience == "portal":
        user = portal_user(client_org)
    elif audience == "owner":
        user = owner
    elif audience == "no-role":
        user = User.objects.create_user("review-no-role", is_staff=True)
    elif audience in ROLES:
        user = staff(f"review-{audience}", audience)
        user.is_staff = True  # Admission to the admin site is independent of a laboratory role.
        user.save(update_fields=["is_staff"])
    else:
        user = User.objects.create_user("review-personal", credentials_locked=audience == "shared")

    other = row(login(user))
    stranger = User.objects.create_user("review-stranger", "stranger@example.invalid")
    private = row(login(stranger))
    AccountSession.objects.filter(pk=private.pk).update(
        user_agent="FxiOS/123", address="192.0.2.83"
    )
    AccountSession.objects.filter(pk=other.pk).update(address="192.0.2.82")
    browser = Client() if audience == "anonymous" else login(user)
    page = browser.get(PAGE)
    if audience == "anonymous":
        for response in (page, browser.post(single(other)), browser.post(ALL)):
            assert response.status_code == 302 and reverse("account_login") in response.url
    else:
        assert page.status_code == 200
        no_store(page)
        assert str(private.pk).encode() not in page.content and b"192.0.2.83" not in page.content
        assert (b"192.0.2.82" in page.content) == (audience != "shared")
        if audience == "shared":
            assert str(other.pk).encode() not in page.content
            assert b"1 other session is open now" in page.content
        for response in (browser.get(single(other)), browser.post(PAGE), browser.get(ALL)):
            assert response.status_code == 405
            no_store(response)
    url = admin_url(stranger)
    if audience == "owner":
        for response in (
            browser.get(reverse("admin:accounts_user_change", args=[stranger.pk])),
            browser.get(url),
        ):
            assert response.status_code == 200
            assert (
                b"1 open session" in response.content
                or b"1 session is open now" in response.content
            )
            for value in (b"192.0.2.83", str(private.pk).encode(), b"FxiOS/123"):
                assert value not in response.content
            no_store(response)
    else:
        for response in (browser.get(url), browser.post(url, {"reason": "Review"})):
            assert response.status_code in (302, 403)
            if audience != "anonymous":
                no_store(response)
    assert not AccountSession.objects.filter(pk__in=[private.pk, other.pk], ended_at__isnull=False)
    assert not AuditEvent.objects.filter(action=ACTION).exists()
    if audience != "anonymous":
        no_store(browser.post(PAGE + "not-a-session/sign-out/"))
        for index in range(2):
            if index:
                other = row(login(user))
            response = browser.post(ALL if index else single(other))
            no_store(response)
            assert response.status_code == 302 and response.url == PAGE
            other.refresh_from_db()
            assert bool(other.ended_at) == (audience != "shared")
            assert row(browser).ended_at is None
            assert AuditEvent.objects.filter(action=ACTION).count() == (
                0 if audience == "shared" else index + 1
            )
        private.refresh_from_db()
        assert private.ended_at is None


@pytest.mark.parametrize("change", ["ended", "expired", "replaced", "deactivated", "shared"])
def test_change_between_page_and_submit(person, change):
    browser, other = login(person), login(person)
    target = row(other)
    drawn = browser.get(PAGE)
    # Adopted: the listed sessions, now that the page sends its reading time instead of ids.
    assert str(target.pk) in [s.id for s in drawn.context["other_sessions"]]
    if change == "replaced":
        other.force_login(person)
        assert row(other).pk != target.pk
    elif change == "ended":
        other.logout()
    elif change == "expired":
        age(row(browser), 40)
    elif change == "shared":
        person.credentials_locked = True
        person.save(update_fields=["credentials_locked"])
    else:
        person.is_active = False
        person.save(update_fields=["is_active"])
    response = browser.post(single(target), HTTP_REFERER="http://testserver" + PAGE)
    target.refresh_from_db()
    assert not AuditEvent.objects.filter(action=ACTION).exists()
    if change in ("expired", "deactivated"):
        assert response.status_code == 302 and reverse("account_login") in response.url
        assert not browser.get(PAGE).wsgi_request.user.is_authenticated
        assert target.ending == (Ending.DEACTIVATED if change == "deactivated" else "")
    else:
        no_store(response)
        assert response.url == PAGE
        assert (
            target.ending
            == {"replaced": Ending.SIGNED_IN_AGAIN, "ended": Ending.SIGNED_OUT, "shared": ""}[
                change
            ]
        )
        if change == "replaced":
            assert not row(other).ended_at


def test_bulk_confirmation_counts_sign_ins_since_page_not_rows_restored_by_new_limit(person, staff):
    policy_admin = staff("policy-admin", "admin")
    browser, idle, visible = (login(person) for _ in range(3))
    age(row(idle), 35)
    drawn = browser.get(PAGE)
    # Adopted: the page lists only the visible session and sends its reading time.
    assert [s.id for s in drawn.context["other_sessions"]] == [str(row(visible).pk)]
    set_minutes(policy_admin, 60)
    later = login(person)
    page = browser.post(ALL, {"drawn_at": drawn.context["drawn_at"]}, follow=True)
    event = AuditEvent.objects.get(action=ACTION)
    assert set(event.after["sessions"]) == {str(row(b).pk) for b in (idle, visible, later)}
    assert notices(page) == [
        "Signed out of 3 other sessions. 1 of them was signed in after you opened this page."
    ]


def test_owner_noop_confirmation_acknowledges_session_still_open(owner):
    browser = login(owner)
    response = browser.post(admin_url(owner), {"reason": "Finished"}, follow=True)
    assert not row(browser).ended_at and not AuditEvent.objects.filter(action=ACTION).exists()
    assert not LogEntry.objects.filter(object_id=str(owner.pk)).exists()
    (message,) = notices(response)
    assert "other" in message.lower(), message


def test_shared_account_with_no_other_visitors_shows_the_count(person):
    person.credentials_locked = True
    person.save(update_fields=["credentials_locked"])
    page = login(person).get(PAGE)
    assert page.context["other_count"] == 0
    notice = (
        page.content.decode().split("Other visitors use this account too.", 1)[1].split("</", 1)[0]
    )
    assert "no other session" in notice.lower() or "0 other sessions" in notice.lower(), notice


def test_sign_in_records_fact_agrees_with_available_page(person):
    page = login(person).get(PAGE)
    facts = {f.key: f for group in security_settings.in_force() for f in group.facts}
    # This also runs at the base, where the absence of a page was accurate.
    exists = page.status_code == 200
    assert ("no page lists the sessions" in facts["sign-in-records"].note.lower()) != exists


def test_ending_fact_identifies_the_session_kept_on_owners_own_account(owner):
    browser = login(owner)
    url = admin_url(owner)
    assert browser.post(url, {"reason": "Finished"}).status_code == 302
    assert row(browser).ended_at is None
    fact = next(
        f for group in security_settings.in_force() for f in group.facts if f.key == "end-sessions"
    )
    assert "every open session of the account ends" not in fact.note.lower() or (
        "except" in fact.note.lower() or "own account" in fact.note.lower()
    ), fact.note


class ExampleAddressAdapter(ClosedSignupAdapter):
    def get_client_ip(self, request):
        return "192.0.2.140"


def test_address_fact_tracks_the_account_adapter(settings, rf):
    settings.ACCOUNT_ADAPTER = __name__ + ".ExampleAddressAdapter"
    assert sessions.reported_by(rf.get("/", REMOTE_ADDR="192.0.2.7"))[1] == "192.0.2.140"
    fact = next(
        f for group in security_settings.in_force() for f in group.facts if f.key == "own-sessions"
    )
    assert fact.value == security_settings.CHANGED and "ACCOUNT_ADAPTER" in fact.note


def test_safeguards_distinguishes_retained_ended_rows_from_a_page_showing_them(person):
    target = row(login(person))
    sessions.end_sessions(person, Ending.SIGNED_OUT, session_id=target.pk)
    target.refresh_from_db()
    assert target.ending == Ending.SIGNED_OUT and target.ended_at is not None
    reference = (Path(__file__).resolve().parents[1] / "docs/safeguards.md").read_text()
    assert "past sessions are not shown or stored as a history" not in reference


@pytest.mark.parametrize("case", ["direct", "chain", "header", "zone", "mapped"])
def test_password_form_records_address_and_label(person, settings, case):
    cache.clear()
    settings.ALLAUTH_TRUSTED_PROXY_COUNT = 2 if case in ("chain", "header") else 0
    settings.ALLAUTH_TRUSTED_CLIENT_IP_HEADER = "X-Review-Client" if case == "header" else None
    source, expected = {
        "direct": ("192.0.2.44", "192.0.2.44"),
        "chain": ("192.0.2.44", "192.0.2.7"),
        "header": ("192.0.2.44", "2001:db8::15"),
        "zone": ("fe80::7%en0", "fe80::7"),
        "mapped": ("::ffff:192.0.2.16", "::ffff:c000:210"),
    }[case]
    browser = Client()
    response = browser.post(
        reverse("account_login"),
        {"login": person.username, "password": PASSWORD},
        HTTP_USER_AGENT="Firefox/128 (Windows) <review-mark>" + "x" * 300,
        REMOTE_ADDR=source,
        HTTP_X_FORWARDED_FOR="192.0.2.7, 192.0.2.9",
        HTTP_X_REVIEW_CLIENT="[2001:db8::15]:443",
    )
    assert response.status_code == 302 and browser.session[AUTH_KEY] == str(person.pk)
    session = row(browser)
    assert ipaddress.ip_address(session.address) == ipaddress.ip_address(expected)
    assert len(session.user_agent) == 300
    page = browser.get(PAGE)
    assert b"Firefox on Windows" in page.content and b"<review-mark>" not in page.content


@pytest.mark.parametrize("case", ["missing-header", "short-chain"])
def test_password_form_with_no_usable_proxy_address(person, settings, case):
    """The real, rate-limited password form, when the proxy settings give no usable address.

    Adopted: the sign-in limits need the address and refuse the request before anyone is signed
    in (403 for a missing trusted header, a server error for a forwarding chain shorter than the
    trusted proxy count), as before this change; nothing is half recorded."""
    cache.clear()
    settings.ALLAUTH_TRUSTED_PROXY_COUNT = 2
    settings.ALLAUTH_TRUSTED_CLIENT_IP_HEADER = (
        "X-Review-Client" if case == "missing-header" else None
    )
    browser = Client(raise_request_exception=False)
    response = browser.post(
        reverse("account_login"),
        {"login": person.username, "password": PASSWORD},
        HTTP_X_FORWARDED_FOR="192.0.2.4",
    )
    assert response.status_code in (403, 500)
    assert browser.session.get(AUTH_KEY) is None
    assert not AccountSession.objects.filter(user=person).exists()


@pytest.mark.django_db(transaction=True)
def test_admin_save_and_rollback_match_rows_log_history_and_mail(person, owner, caplog, mailoutbox):
    browser = login(owner)
    first = row(login(person))
    url = admin_url(person)
    assert browser.get(url).context["open_count"] == 1
    later = row(login(person))
    assert (
        browser.post(url, {"reason": "x" * 1001}).context["form"].has_error("reason", "max_length")
    )
    ids = sorted([str(first.pk), str(later.pk)])

    def lines():
        return [r.getMessage() for r in caplog.records if r.name == "lab_ops.signin"]

    reason = "Synthetic device replacement"
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        with transaction.atomic():
            assert browser.post(url, {"reason": reason}).status_code == 302
            assert AuditEvent.objects.filter(action=ACTION).count() == 1
            transaction.set_rollback(True)
        assert not AuditEvent.objects.filter(action=ACTION).exists()
        assert not LogEntry.objects.filter(object_id=str(person.pk)).exists()
        assert all(s.ended_at is None for s in AccountSession.objects.filter(pk__in=ids))
        assert not lines()
        with transaction.atomic():
            assert browser.post(url, {"reason": reason}).status_code == 302
            assert not lines()
    assert lines() == [f"signout.administrator user={person.username} sessions=2"]
    event = AuditEvent.objects.get(action=ACTION)
    assert (event.actor_id, event.object_type, event.object_id, event.reason) == (
        owner.pk,
        "accounts.user",
        str(person.pk),
        reason,
    )
    assert event.before == {} and event.after == {
        "sessions": ids,
        "count": 2,
        "ending": "administrator",
    }
    entry = LogEntry.objects.get(object_id=str(person.pk))
    assert entry.user_id == owner.pk and entry.change_message == "Ended 2 sessions."
    assert not mailoutbox


def backend_pid():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def wait_for_block(worker, pids):
    """Wait for PostgreSQL to confirm the specific blocker on the other connection."""
    pid = pids.get(timeout=10)
    deadline = time.monotonic() + 6
    with connection.cursor() as cursor:
        while time.monotonic() < deadline:
            if worker.done():
                pytest.fail(f"Worker completed before the lock wait: {worker.result()}")
            cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
            if cursor.fetchone()[0]:
                return
            time.sleep(0.02)
    pytest.fail("PostgreSQL did not report the expected blocking connection")


def on_connection(operation, pids):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '10s'")
        pids.put(backend_pid())
        return operation()
    finally:
        connections.close_all()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("action", ["single", "all", "admin", "deactivate"])
@pytest.mark.parametrize("initial,final", [(30, 5), (5, 30)], ids=["shorten", "lengthen"])
def test_ending_classifies_target_by_limit_after_row_wait(
    person, owner, staff, action, initial, final
):
    policy_admin = staff("review-policy", "admin")
    me = login(person)
    target = row(login(person))
    operator = login(owner)
    set_minutes(policy_admin, initial)
    age(target, 10)

    def save():
        if action == "deactivate":  # Also available on the base.
            person.is_active = False
            person.save(update_fields=["is_active"])
            return
        if action == "admin":
            response = operator.post(admin_url(person), {"reason": "Synthetic device replacement"})
        else:
            response = me.post(single(target) if action == "single" else ALL)
        assert response.status_code == 302

    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM accounts_accountsession WHERE id = %s FOR SHARE", [target.pk]
                )
            worker = pool.submit(on_connection, save, pids)
            wait_for_block(worker, pids)
            set_minutes(policy_admin, final)
        worker.result(10)
    target.refresh_from_db()
    if final == 5:
        expected = Ending.INACTIVITY
    else:
        expected = {
            "single": "signed_out_elsewhere",
            "all": "signed_out_elsewhere",
            "admin": "administrator",
            "deactivate": "deactivated",
        }[action]
    named = [
        pk for event in AuditEvent.objects.filter(action=ACTION) for pk in event.after["sessions"]
    ]
    expected_mentions = int(final == 30 and action != "deactivate")
    assert (target.ending, named.count(str(target.pk))) == (expected, expected_mentions)
    if final == 5:
        assert target.ended_at == target.last_active_at + timedelta(minutes=5)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("pair", ["two-admins", "admin-first", "self-first"])
def test_overlapping_endings_have_one_owner_per_ended_row(owner, person, pair):
    first = login(owner)
    if pair == "two-admins":
        person = User.objects.create_superuser("second-owner", "second@example.invalid", PASSWORD)
    second = login(person)
    spare = row(login(person))
    first_id, second_id = row(first).pk, row(second).pk

    def admin_save():
        return first.post(admin_url(person), {"reason": "Synthetic replacement"})

    def other_save():
        if pair == "two-admins":
            return second.post(admin_url(owner), {"reason": "Synthetic replacement"})
        return second.post(ALL)

    start, follow = (other_save, admin_save) if pair == "self-first" else (admin_save, other_save)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            assert start().status_code == 302
            pending = pool.submit(on_connection, follow, pids)
            wait_for_block(pending, pids)
        response = pending.result(10)
    assert response.status_code == 302
    assert AccountSession.objects.get(pk=first_id).ended_at is None
    assert (reverse("account_login") in response.url) == (pair != "self-first")
    events = list(AuditEvent.objects.filter(action=ACTION))
    assert len(events) == (2 if pair == "self-first" else 1)
    assert sorted(pk for e in events for pk in e.after["sessions"]) == sorted(
        map(str, [second_id, spare.pk])
    )
    for event in events:
        assert event.after["count"] == len(event.after["sessions"])
        assert (
            AccountSession.objects.filter(
                pk__in=event.after["sessions"], ending=event.after["ending"]
            ).count()
            == event.after["count"]
        )


def test_demo_reseed_preserves_session_and_reset_clears_it(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-shared-password")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "synthetic-owner-password")
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    call_command("seed_demo", announce_accounts=True, verbosity=0)
    account = User.objects.get(username="demo_technical")
    visitor = login(account)
    target = row(visitor)
    AccountSession.objects.filter(pk=target.pk).update(
        user_agent="Firefox/127", address="192.0.2.3"
    )
    call_command("seed_demo", announce_accounts=True, verbosity=0)
    target.refresh_from_db()
    assert target.address == "192.0.2.3" and target.user_agent == "Firefox/127"
    call_command("reset_demo", announce_accounts=True, interactive=False, verbosity=0)
    assert not AccountSession.objects.exists()
    assert visitor.get(PAGE).status_code == 302
    assert row(login(User.objects.get(username="demo_technical"))).ended_at is None


@pytest.mark.django_db(transaction=True)
def test_base_sign_in_before_and_after_migration(person, settings):
    """Run the base checkout’s actual sign-in receiver against this test database."""
    base = os.environ.get("SESSION_REVIEW_BASE")
    if not base:
        pytest.skip("Set SESSION_REVIEW_BASE to the 0413969 checkout with its environment")
    name = connection.settings_dict["NAME"]
    assert name.startswith("test_") and name == os.environ["TEST_DATABASE_NAME"]
    env = dict(os.environ)
    env.pop("TEST_DATABASE_NAME")
    env["DATABASE_URL"] = urlsplit(env["DATABASE_URL"])._replace(path="/" + name).geturl()
    script = """
import django, json, sys
django.setup()
from django.test import Client
from accounts.models import User
from django.conf import settings
assert settings.DATABASES['default']['NAME'] == sys.argv[1]
c = Client()
c.force_login(User.objects.get(pk=sys.argv[2]))
print(json.dumps([c.cookies[settings.SESSION_COOKIE_NAME].value, c.session['_account_session']]))
"""
    env["DJANGO_SETTINGS_MODULE"] = "config.settings"

    def old_signin():
        result = subprocess.run(
            [str(Path(base) / ".venv/bin/python"), "-c", script, name, str(person.pk)],
            env=env,
            cwd=base,
            text=True,
            capture_output=True,
            check=True,
            timeout=20,
        )
        return json.loads(result.stdout.splitlines()[-1])

    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([("accounts", "0003_account_session")])
        cookie, before = old_signin()
    finally:
        MigrationExecutor(connection).migrate(leaves)
    _, after = old_signin()
    visitor = Client()
    visitor.cookies[settings.SESSION_COOKIE_NAME] = cookie
    page = visitor.get(PAGE)
    assert page.status_code == 200 and page.context["this_session"].id == before
    assert [s.id for s in page.context["other_sessions"]] == [after]
    assert page.content.count(b"Not recorded</span>") == 2
    assert set(AccountSession.objects.filter(user=person).values_list("user_agent", "address")) == {
        ("", None)
    }


def test_admin_disclosure_matches_count_only_personal_page_help(owner, person):
    row(login(person))
    page = login(owner).get(admin_url(person))
    # Adopted: the page shows the count only, as the personal page's help says.
    assert page.context["open_count"] == 1 and "last_activity" not in page.context
    assert b"1 session is open now." in page.content and b"last active" not in page.content


def test_offboarding_instructions_distinguish_password_change_from_deactivation(
    owner, person, settings, mailoutbox
):
    cache.clear()
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    person.set_password("synthetic-new-device-password")
    person.save(update_fields=["password"])
    sign_in = Client().get(reverse("account_login"))
    assert person.is_active and b"Email me a sign-in code" in sign_in.content
    code_request = Client().post(reverse("account_request_login_code"), {"email": person.email})
    assert code_request.url == reverse("account_confirm_login_code")
    assert [m.to for m in mailoutbox] == [[person.email]]
    form = login(owner).get(admin_url(person))
    assert (
        b"to stop that, also clear Active on the account's page or set a new password"
        not in form.content
    )


@pytest.mark.django_db(transaction=True)
def test_ticket_note_commits_before_sign_out_and_next_note_is_refused(staff):
    from desk.services import create_request
    from taxonomy.models import RequestCategory

    agent = staff("review-note", "client_services")
    group = RequestCategory.objects.create(key="group", name="Example group")
    category = RequestCategory.objects.create(key="question", name="Example question", parent=group)
    ticket = create_request(
        actor=agent,
        category=category,
        channel="fax",
        subject="Example",
        first_entry_kind="note",
        first_entry_body="Initial note",
    )
    editor, closer = login(agent), login(agent)
    target = row(editor)
    url = reverse("desk:request-entry", args=[ticket.pk])

    def note(body):
        ticket.refresh_from_db()
        return editor.post(
            url, {"kind": "note", "body": body, "expected_revision": ticket.revision}
        )

    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            assert note("First review note").status_code == 302
            worker = pool.submit(on_connection, lambda: closer.post(single(target)), pids)
            wait_for_block(worker, pids)
        assert worker.result(10).status_code == 302
    assert ticket.entries.filter(author=agent, body="First review note").count() == 1
    response = note("Second review note")
    assert response.status_code == 302 and reverse("account_login") in response.url
    assert not ticket.entries.filter(body="Second review note").exists()
    assert AUTH_KEY not in editor.session


@pytest.mark.parametrize("flag", ["is_superuser", "is_staff"])
def test_admin_form_rechecks_status_when_submitted(owner, person, flag):
    browser = login(owner)
    target = row(login(person))
    url = admin_url(person)
    assert browser.get(url).status_code == 200
    setattr(owner, flag, False)
    owner.save(update_fields=[flag])
    response = browser.post(url, {"reason": "Synthetic retirement"})
    assert response.status_code in (302, 403)
    target.refresh_from_db()
    assert target.ended_at is None
    assert not AuditEvent.objects.filter(action=ACTION).exists()
    assert not LogEntry.objects.filter(object_id=str(person.pk)).exists()


def test_missing_forwarding_chain_fact_explains_which_address_was_recorded(person, settings):
    settings.ALLAUTH_TRUSTED_PROXY_COUNT = 2
    settings.ALLAUTH_TRUSTED_CLIENT_IP_HEADER = None
    cache.clear()
    browser = Client()
    response = browser.post(
        reverse("account_login"),
        {"login": person.username, "password": PASSWORD},
        REMOTE_ADDR="192.0.2.44",
    )
    assert response.status_code == 302 and row(browser).address == "192.0.2.44"
    note = next(
        f.note
        for group in security_settings.in_force()
        for f in group.facts
        if f.key == "own-sessions"
    )
    assert any(term in note.lower() for term in ("connecting", "peer", "remote_addr")), note


def test_missing_csrf_token_does_not_end_any_session(owner, person):
    staff_browser, admin_browser = (
        Client(enforce_csrf_checks=True),
        Client(enforce_csrf_checks=True),
    )
    staff_browser.force_login(person)
    admin_browser.force_login(owner)
    target = row(login(person))
    for browser, path in (
        (staff_browser, single(target)),
        (staff_browser, ALL),
        (admin_browser, admin_url(person)),
    ):
        response = browser.post(path, {"reason": "Synthetic replacement"})
        assert response.status_code == 403
        no_store(response)
    assert not AuditEvent.objects.filter(action=ACTION).exists()
    assert not LogEntry.objects.filter(object_id=str(person.pk)).exists()
    target.refresh_from_db()
    assert target.ended_at is None


@pytest.mark.django_db(transaction=True)
def test_target_reaching_its_deadline_during_an_ending_keeps_the_limit_ending(owner, person):
    target = row(login(person))
    operator = login(owner)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic(), connection.cursor() as cursor:
            # Ordinary activity stopped a little under 30 minutes ago. Only the test clock
            # setup changes the timestamps. Release the row without updating it.
            cursor.execute(
                "UPDATE accounts_accountsession SET "
                "started_at = clock_timestamp() - interval '1 hour', "
                "last_active_at = clock_timestamp() - interval '30 minutes' + interval '3 seconds' "
                "WHERE id = %s RETURNING last_active_at + interval '30 minutes'",
                [target.pk],
            )
            deadline = cursor.fetchone()[0]
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM accounts_accountsession WHERE id = %s FOR SHARE", [target.pk]
            )
            worker = pool.submit(
                on_connection, lambda: operator.post(admin_url(person), {"reason": "Review"}), pids
            )
            wait_for_block(worker, pids)
            cursor.execute("SELECT clock_timestamp() < %s", [deadline])
            assert cursor.fetchone()[0], (
                "the target should still be inside the limit at the start of the wait"
            )
            cursor.execute(
                "SELECT pg_sleep(GREATEST(0, EXTRACT(EPOCH FROM (%s - clock_timestamp()))))",
                [deadline],
            )
        assert worker.result(10).status_code == 302
    target.refresh_from_db()
    assert (target.ending, target.ended_at, AuditEvent.objects.filter(action=ACTION).count()) == (
        Ending.INACTIVITY,
        deadline,
        0,
    )
