"""Where you are signed in (accounts.session_list, accounts.session_views) and ending an account's
sessions from the account administration (accounts.admin.AccountAdmin.end_sessions_view).

Each test browser is its own session. Times are moved by changing the session rows; the limits
are compared with the database's clock."""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from django.contrib.admin.models import LogEntry
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, connections, transaction
from django.db.models.functions import Now
from django.test import Client as BrowserClient
from django.urls import reverse

from accounts import session_list, sessions
from accounts.acting import hold_actor
from accounts.models import AccountSession, User
from core.models import AuditEvent

pytestmark = pytest.mark.django_db

Ending = AccountSession.Ending
PAGE = "/account/sessions/"
FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"
SAFARI_PHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"
)


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def agent(staff):
    return staff("list-agent", "client_services")


@pytest.fixture
def other_agent(staff):
    return staff("list-other", "client_services")


@pytest.fixture
def portal(client_org, portal_user):
    return portal_user(client_org, "list-portal")


@pytest.fixture
def owner(db):
    return User.objects.create_superuser("list-owner", "list-owner@example.invalid", "x-pass-81!")


def signed_in(user, HTTP_USER_AGENT=None, REMOTE_ADDR=None):  # noqa: N803 - request keys
    """A browser signed in to the account, its session recorded as from this browser and address
    (force_login signs in with a request of its own, without the browser's headers; what a real
    sign-in records is tested on start_session itself)."""
    browser = BrowserClient()
    browser.force_login(user)
    changes = {}
    if HTTP_USER_AGENT is not None:
        changes["user_agent"] = HTTP_USER_AGENT
    if REMOTE_ADDR is not None:
        changes["address"] = REMOTE_ADDR
    if changes:
        AccountSession.objects.filter(pk=browser.session[sessions.SESSION_KEY]).update(**changes)
    return browser


def row_of(browser):
    return AccountSession.objects.get(pk=browser.session[sessions.SESSION_KEY])


def messages_of(response):
    return [str(message) for message in response.context["messages"]]


def follow(browser, response):
    assert response.status_code == 302, response.status_code
    return browser.get(response["Location"])


# What sign-in records


def test_sign_in_records_the_browser_and_the_address(agent, rf):
    request = rf.get("/", HTTP_USER_AGENT=FIREFOX, REMOTE_ADDR="198.51.100.7")
    request.session = BrowserClient().session
    sessions.start_session(None, request, agent)
    row = AccountSession.objects.get(user=agent)
    assert row.user_agent == FIREFOX
    assert row.address == "198.51.100.7"


def test_sign_in_takes_the_address_from_the_trusted_proxy_count(agent, rf, settings):
    settings.ALLAUTH_TRUSTED_PROXY_COUNT = 1
    request = rf.get("/", REMOTE_ADDR="10.0.0.2", HTTP_X_FORWARDED_FOR="203.0.113.9")
    request.session = BrowserClient().session
    sessions.start_session(None, request, agent)
    assert AccountSession.objects.get(user=agent).address == "203.0.113.9"


def test_sign_in_ignores_a_forwarded_address_when_no_proxy_is_trusted(agent, rf):
    request = rf.get("/", REMOTE_ADDR="198.51.100.8", HTTP_X_FORWARDED_FOR="203.0.113.10")
    request.session = BrowserClient().session
    sessions.start_session(None, request, agent)
    assert AccountSession.objects.get(user=agent).address == "198.51.100.8"


@pytest.mark.parametrize(
    "meta",
    [
        {},  # no address at all
        {"REMOTE_ADDR": "not-an-address"},
        {"REMOTE_ADDR": "10.0.0.2", "HTTP_X_FORWARDED_FOR": ""},
    ],
)
def test_sign_in_without_a_usable_address_records_none_and_still_signs_in(agent, rf, meta):
    request = rf.get("/")
    request.META.pop("REMOTE_ADDR", None)
    request.META.update(meta)
    request.session = BrowserClient().session
    sessions.start_session(None, request, agent)
    row = AccountSession.objects.get(user=agent)
    assert row.address in (None, "10.0.0.2")
    if meta.get("REMOTE_ADDR") != "10.0.0.2":
        assert row.address is None


@pytest.mark.parametrize(
    ("given", "recorded"),
    [("fe80::1%eth0", "fe80::1"), ("2001:db8:0:0::7", "2001:db8::7"), ("192.0.2.5", "192.0.2.5")],
)
def test_an_address_is_recorded_as_the_column_takes_it(agent, rf, given, recorded):
    request = rf.get("/", REMOTE_ADDR=given)
    request.session = BrowserClient().session
    sessions.start_session(None, request, agent)
    assert AccountSession.objects.get(user=agent).address == recorded


def test_a_proxy_count_the_request_does_not_match_records_no_address(agent, rf, settings):
    settings.ALLAUTH_TRUSTED_PROXY_COUNT = 3
    request = rf.get("/", REMOTE_ADDR="10.0.0.2", HTTP_X_FORWARDED_FOR="203.0.113.9")
    request.session = BrowserClient().session
    sessions.start_session(None, request, agent)
    assert AccountSession.objects.get(user=agent).address is None


def test_the_browser_is_kept_printable_and_cut_to_its_column(agent, rf):
    request = rf.get("/", HTTP_USER_AGENT="Agent\x00\x07/1 " + "x" * 400)
    request.session = BrowserClient().session
    sessions.start_session(None, request, agent)
    agent_text = AccountSession.objects.get(user=agent).user_agent
    assert "\x00" not in agent_text and "\x07" not in agent_text
    assert agent_text.startswith("Agent/1 ")
    assert len(agent_text) == sessions.USER_AGENT_LENGTH


def test_a_session_from_before_this_release_has_no_browser_or_address(agent):
    row = AccountSession.objects.create(user=agent)
    row.refresh_from_db()
    assert row.user_agent == "" and row.address is None


@pytest.mark.parametrize(
    ("agent_text", "label"),
    [
        (FIREFOX, "Firefox on Windows"),
        (SAFARI_PHONE, "Safari on iPhone"),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like "
            "Gecko) Chrome/142.0.0.0 Safari/537.36",
            "Chrome on macOS",
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0",
            "Edge on Windows",
        ),
        (
            "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/142.0.0.0 Mobile Safari/537.36",
            "Chrome on Android",
        ),
        ("curl/8.7.1", "Unknown browser"),
        ("", "Unknown browser"),
    ],
)
def test_browser_labels(agent_text, label):
    assert session_list.browser_label(agent_text) == label


# The page


def test_the_page_lists_the_accounts_open_sessions_this_browser_first(agent):
    first = signed_in(agent, HTTP_USER_AGENT=FIREFOX, REMOTE_ADDR="198.51.100.1")
    second = signed_in(agent, HTTP_USER_AGENT=SAFARI_PHONE, REMOTE_ADDR="198.51.100.2")
    response = second.get(PAGE)
    assert response.status_code == 200
    assert response.context["this_session"].id == str(row_of(second).pk)
    assert [row.id for row in response.context["other_sessions"]] == [str(row_of(first).pk)]
    text = response.content.decode()
    assert "Safari on iPhone" in text and "Firefox on Windows" in text
    assert "198.51.100.1" in text and "198.51.100.2" in text
    assert text.index("Safari on iPhone") < text.index("Firefox on Windows")
    assert (
        response["Cache-Control"].startswith("max-age=0") or "no-store" in response["Cache-Control"]
    )


def test_the_page_leaves_out_ended_and_expired_sessions_and_other_accounts(agent, other_agent):
    me = signed_in(agent)
    ended = signed_in(agent)
    expired = signed_in(agent)
    foreign = signed_in(other_agent, REMOTE_ADDR="192.0.2.99")
    AccountSession.objects.filter(pk=row_of(ended).pk).update(
        ended_at=Now(), ending=Ending.SIGNED_OUT
    )
    AccountSession.objects.filter(pk=row_of(expired).pk).update(
        last_active_at=Now() - timedelta(hours=2)
    )
    response = me.get(PAGE)
    listed = {row.id for row in response.context["other_sessions"]}
    assert listed == set()
    assert response.context["this_session"].id == str(row_of(me).pk)
    assert "192.0.2.99" not in response.content.decode()
    assert row_of(foreign).ended_at is None


def test_a_client_portal_account_has_the_page_too(portal):
    first = signed_in(portal)
    second = signed_in(portal)
    response = second.get(PAGE)
    assert response.status_code == 200
    assert [row.id for row in response.context["other_sessions"]] == [str(row_of(first).pk)]


def test_the_user_menu_links_to_the_page(agent, portal):
    for user in (agent, portal):
        assert 'href="/account/sessions/"' in signed_in(user).get(PAGE).content.decode()


def test_signed_out_visitors_are_sent_to_sign_in():
    for method, url in (
        ("get", PAGE),
        ("post", "/account/sessions/others/sign-out/"),
    ):
        response = getattr(BrowserClient(), method)(url)
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("account_login"))


def test_sign_out_addresses_accept_only_post(agent):
    browser = signed_in(agent)
    other = signed_in(agent)
    target = row_of(other).pk
    assert browser.get(f"/account/sessions/{target}/sign-out/").status_code == 405
    assert browser.get("/account/sessions/others/sign-out/").status_code == 405
    assert browser.post(PAGE).status_code == 405
    assert row_of(other).ended_at is None


# Signing out one other session


def test_signing_out_another_session_ends_it_and_its_browser_is_told(agent):
    me = signed_in(agent)
    other = signed_in(agent, REMOTE_ADDR="198.51.100.4", HTTP_USER_AGENT=FIREFOX)
    target = row_of(other)
    response = me.post(f"/account/sessions/{target.pk}/sign-out/")
    page = follow(me, response)
    assert "That session was signed out." in " ".join(messages_of(page))
    target.refresh_from_db()
    assert target.ending == Ending.SIGNED_OUT_ELSEWHERE and target.ended_at is not None
    assert row_of(me).ended_at is None
    landed = other.get("/")
    assert landed.status_code == 302 and landed["Location"].startswith(reverse("account_login"))
    notices = [str(m) for m in other.get(reverse("account_login")).context["messages"]]
    assert notices == [sessions.ENDED_BY_MESSAGES[Ending.SIGNED_OUT_ELSEWHERE]]


def test_the_ending_is_audited_without_the_browser_or_the_address(agent):
    me = signed_in(agent)
    other = signed_in(agent, REMOTE_ADDR="198.51.100.5", HTTP_USER_AGENT=FIREFOX)
    target = row_of(other)
    me.post(f"/account/sessions/{target.pk}/sign-out/")
    event = AuditEvent.objects.get(action=session_list.ACTION)
    assert event.actor == agent and event.object_id == str(agent.pk)
    assert event.after == {
        "ending": Ending.SIGNED_OUT_ELSEWHERE,
        "sessions": [str(target.pk)],
        "count": 1,
    }
    stored = f"{event.before} {event.after} {event.reason}"
    assert "198.51.100.5" not in stored and "Firefox" not in stored


def test_the_sign_in_log_records_the_ending_once_it_commits(
    agent, caplog, django_capture_on_commit_callbacks
):
    me = signed_in(agent)
    other = signed_in(agent, REMOTE_ADDR="198.51.100.6")
    with (
        caplog.at_level(logging.INFO, logger="lab_ops.signin"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        me.post(f"/account/sessions/{row_of(other).pk}/sign-out/")
    lines = [r.getMessage() for r in caplog.records if r.name == "lab_ops.signin"]
    assert "signout.signed_out_elsewhere user=list-agent sessions=1" in lines
    assert not any("198.51.100.6" in line for line in lines)


def test_a_session_of_another_account_is_refused_like_one_that_ended(agent, other_agent):
    me = signed_in(agent)
    foreign = signed_in(other_agent)
    target = row_of(foreign)
    page = follow(me, me.post(f"/account/sessions/{target.pk}/sign-out/"))
    assert messages_of(page) == [session_list.NOT_OPEN]
    target.refresh_from_db()
    assert target.ended_at is None and target.ending == ""
    assert not AuditEvent.objects.filter(action=session_list.ACTION).exists()
    assert foreign.get("/").status_code == 200


def test_a_session_that_ended_meanwhile_changes_nothing(agent):
    me = signed_in(agent)
    other = signed_in(agent)
    target = row_of(other)
    other.post(reverse("account_logout"))  # signed out there after this page was opened
    page = follow(me, me.post(f"/account/sessions/{target.pk}/sign-out/"))
    assert messages_of(page) == [session_list.NOT_OPEN]
    target.refresh_from_db()
    assert target.ending == Ending.SIGNED_OUT
    assert not AuditEvent.objects.filter(action=session_list.ACTION).exists()


def test_a_session_past_a_limit_ends_as_expired_not_as_signed_out(agent):
    me = signed_in(agent)
    other = signed_in(agent)
    target = row_of(other)
    AccountSession.objects.filter(pk=target.pk).update(last_active_at=Now() - timedelta(hours=1))
    page = follow(me, me.post(f"/account/sessions/{target.pk}/sign-out/"))
    assert messages_of(page) == [session_list.NOT_OPEN]
    target.refresh_from_db()
    assert target.ending == Ending.INACTIVITY
    assert not AuditEvent.objects.filter(action=session_list.ACTION).exists()


def test_the_session_in_use_is_not_signed_out_here(agent):
    me = signed_in(agent)
    mine = row_of(me)
    page = follow(me, me.post(f"/account/sessions/{mine.pk}/sign-out/"))
    assert "This is the session you are using now." in messages_of(page)[0]
    mine.refresh_from_db()
    assert mine.ended_at is None


def test_an_unknown_session_changes_nothing(agent):
    me = signed_in(agent)
    page = follow(me, me.post("/account/sessions/6f1c1a52-1b5c-4c3e-9a49-0a6d1e2f3b4c/sign-out/"))
    assert messages_of(page) == [session_list.NOT_OPEN]
    assert me.post("/account/sessions/not-a-session/sign-out/").status_code == 404


def test_a_browser_whose_session_ended_cannot_sign_out_others(agent):
    me = signed_in(agent)
    other = signed_in(agent)
    third = signed_in(agent)
    sessions.end_sessions(agent, Ending.SIGNED_OUT, session_id=row_of(me).pk)
    response = me.post(f"/account/sessions/{row_of(other).pk}/sign-out/")
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("account_login"))
    assert row_of(other).ended_at is None and row_of(third).ended_at is None


# Signing out every other session


def test_signing_out_all_others_keeps_this_one_and_names_those_opened_later(agent):
    me = signed_in(agent)
    shown = signed_in(agent)
    drawn = me.get(PAGE)
    later = signed_in(agent)  # signed in after the page was drawn
    response = me.post("/account/sessions/others/sign-out/", {"shown": drawn.context["shown"]})
    page = follow(me, response)
    assert messages_of(page) == [
        "Signed out of 2 other sessions. 1 of them was signed in after you opened this page."
    ]
    for browser in (shown, later):
        assert row_of(browser).ending == Ending.SIGNED_OUT_ELSEWHERE
    assert row_of(me).ended_at is None
    event = AuditEvent.objects.get(action=session_list.ACTION)
    assert sorted(event.after["sessions"]) == sorted([str(row_of(shown).pk), str(row_of(later).pk)])


def test_signing_out_all_others_with_none_open_changes_nothing(agent, other_agent):
    me = signed_in(agent)
    foreign = signed_in(other_agent)
    page = follow(me, me.post("/account/sessions/others/sign-out/", {"shown": ""}))
    assert messages_of(page) == ["No other session was open, so nothing changed."]
    assert row_of(foreign).ended_at is None and row_of(me).ended_at is None
    assert not AuditEvent.objects.filter(action=session_list.ACTION).exists()


def test_tampered_shown_ids_only_change_the_wording(agent, other_agent):
    me = signed_in(agent)
    other = signed_in(agent)
    foreign = signed_in(other_agent)
    page = follow(
        me,
        me.post(
            "/account/sessions/others/sign-out/",
            {"shown": f"{row_of(foreign).pk},{row_of(other).pk},junk"},
        ),
    )
    assert messages_of(page) == ["Signed out of 1 other session."]
    assert row_of(foreign).ended_at is None


# Shared demonstration accounts


def test_a_shared_demonstration_account_sees_only_its_own_session(agent):
    User.objects.filter(pk=agent.pk).update(credentials_locked=True)
    other = signed_in(agent, REMOTE_ADDR="198.51.100.30", HTTP_USER_AGENT=FIREFOX)
    me = signed_in(agent, REMOTE_ADDR="198.51.100.31")
    response = me.get(PAGE)
    text = response.content.decode()
    table = text[text.index("<table") : text.index("</table>")]
    assert response.context["other_sessions"] == []
    assert response.context["other_count"] == 1
    assert "198.51.100.31" in table and "Firefox" not in table
    assert "198.51.100.30" not in text
    assert 'data-bs-target="#sign-out' not in text and "/sign-out/" not in text
    assert row_of(other).ended_at is None


def test_a_shared_demonstration_account_cannot_sign_out_other_visitors(agent):
    User.objects.filter(pk=agent.pk).update(credentials_locked=True)
    other = signed_in(agent)
    me = signed_in(agent)
    for url in (
        f"/account/sessions/{row_of(other).pk}/sign-out/",
        "/account/sessions/others/sign-out/",
    ):
        page = follow(me, me.post(url, {"shown": ""}))
        assert messages_of(page) == [session_list.LOCKED_REFUSAL]
    assert row_of(other).ended_at is None
    assert not AuditEvent.objects.filter(action=session_list.ACTION).exists()


def test_an_account_locked_after_the_page_opened_is_refused_as_held(agent):
    me = signed_in(agent)
    other = signed_in(agent)
    me.get(PAGE)
    User.objects.filter(pk=agent.pk).update(credentials_locked=True)
    page = follow(me, me.post("/account/sessions/others/sign-out/", {"shown": ""}))
    assert messages_of(page) == [session_list.LOCKED_REFUSAL]
    assert row_of(other).ended_at is None


# A change under way and two sessions ending each other


@pytest.mark.django_db(transaction=True)
def test_a_change_under_way_from_the_session_being_ended_finishes_first(agent):
    me = signed_in(agent)
    other = signed_in(agent)
    target = row_of(other)
    started = threading.Event()
    release = threading.Event()

    def change_from_other():
        token = sessions._current.set((agent.pk, str(target.pk)))
        try:
            with transaction.atomic():
                hold_actor(agent)  # holds the other session's row FOR SHARE
                started.set()
                release.wait(5)
            return "committed"
        finally:
            sessions._current.reset(token)
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        change = pool.submit(change_from_other)
        assert started.wait(5)
        token = sessions._current.set((agent.pk, str(row_of(me).pk)))
        try:
            ending = pool.submit(
                lambda: _in_thread(
                    lambda: session_list.sign_out_other(
                        actor=agent, current=row_of(me).pk, session_id=target.pk
                    ),
                    (agent.pk, str(row_of(me).pk)),
                )
            )
            time.sleep(0.3)
            assert not ending.done()  # waits for the change under way
            release.set()
            assert change.result(5) == "committed"
            assert ending.result(5) == [str(target.pk)]
        finally:
            sessions._current.reset(token)
    target.refresh_from_db()
    assert target.ending == Ending.SIGNED_OUT_ELSEWHERE
    # A change started from that session afterwards is refused.
    token = sessions._current.set((agent.pk, str(target.pk)))
    try:
        with pytest.raises(sessions.SessionEnded), transaction.atomic():
            hold_actor(agent)
    finally:
        sessions._current.reset(token)


def _in_thread(operation, current):
    token = sessions._current.set(current)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '8s'")
        return operation()
    finally:
        sessions._current.reset(token)
        connections.close_all()


@pytest.mark.django_db(transaction=True)
def test_two_sessions_signing_each_other_out_take_turns(agent, monkeypatch):
    """Each would hold its own session and wait for the other's: the session-ending lock makes
    the second wait before it holds anything, then find its own session ended."""
    first = row_of(signed_in(agent))
    second = row_of(signed_in(agent))
    original = session_list.end_session_ids
    second_holds = threading.Event()
    first_holds = threading.Event()

    def pausing(user, ending, **kwargs):
        if kwargs.get("keep") == str(first.pk):
            first_holds.set()
            # Give the second its chance to hold its own session before this one goes on.
            second_holds.wait(1.5)
        else:
            second_holds.set()
        return original(user, ending, **kwargs)

    monkeypatch.setattr(session_list, "end_session_ids", pausing)

    def by_first():
        return session_list.sign_out_other(actor=agent, current=first.pk, session_id=second.pk)

    def by_second():
        assert first_holds.wait(5)
        return session_list.sign_out_other(actor=agent, current=second.pk, session_id=first.pk)

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(_in_thread, by_first, (agent.pk, str(first.pk)))
        two = pool.submit(_in_thread, by_second, (agent.pk, str(second.pk)))
        assert one.result(10) == [str(second.pk)]
        with pytest.raises(sessions.SessionEnded):
            two.result(10)
    # The second never held its own session while the first held its own.
    assert not second_holds.is_set()
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.ended_at is None
    assert second.ending == Ending.SIGNED_OUT_ELSEWHERE


# The account administration


def admin_browser(owner):
    return signed_in(owner)


def test_the_account_page_counts_open_sessions_and_links_to_end_them(owner, agent):
    signed_in(agent)
    signed_in(agent)
    page = admin_browser(owner).get(reverse("admin:accounts_user_change", args=[agent.pk]))
    text = page.content.decode()
    assert "2 open sessions" in text
    assert reverse("admin:accounts_user_sessions", args=[agent.pk]) in text


def test_ending_an_accounts_sessions_needs_a_reason(owner, agent):
    target = signed_in(agent)
    browser = admin_browser(owner)
    url = reverse("admin:accounts_user_sessions", args=[agent.pk])
    page = browser.get(url)
    assert page.status_code == 200 and "1 session is open now" in page.content.decode()
    refused = browser.post(url, {"reason": "   "})
    assert refused.status_code == 200 and refused.context["form"].errors
    assert row_of(target).ended_at is None


def test_a_superuser_ends_an_accounts_sessions_with_a_reason(owner, agent):
    one = signed_in(agent)
    two = signed_in(agent, REMOTE_ADDR="198.51.100.40")
    browser = admin_browser(owner)
    url = reverse("admin:accounts_user_sessions", args=[agent.pk])
    response = browser.post(url, {"reason": "Laptop reported lost"})
    assert response.status_code == 302
    assert response["Location"] == reverse("admin:accounts_user_change", args=[agent.pk])
    for b in (one, two):
        assert row_of(b).ending == Ending.ADMINISTRATOR
    event = AuditEvent.objects.get(action=session_list.ACTION)
    assert event.actor == owner and event.object_id == str(agent.pk)
    assert event.reason == "Laptop reported lost"
    assert event.after["count"] == 2 and "198.51.100.40" not in str(event.after)
    assert LogEntry.objects.filter(object_id=str(agent.pk), change_message="Ended 2 sessions.")
    assert row_of(browser).ended_at is None
    landed = one.get("/")
    assert landed["Location"].startswith(reverse("account_login"))
    notices = [str(m) for m in one.get(reverse("account_login")).context["messages"]]
    assert notices == [sessions.ENDED_BY_MESSAGES[Ending.ADMINISTRATOR]]
    # The account can sign in again: ending sessions takes no access away.
    assert signed_in(agent).get("/").status_code == 200


def test_on_their_own_account_a_superuser_keeps_the_session_in_use(owner):
    other = signed_in(owner)
    browser = admin_browser(owner)
    url = reverse("admin:accounts_user_sessions", args=[owner.pk])
    assert "1 other session is open now" in browser.get(url).content.decode()
    browser.post(url, {"reason": "Signed in on a shared computer"})
    assert row_of(other).ending == Ending.ADMINISTRATOR
    assert row_of(browser).ended_at is None
    assert browser.get("/admin/").status_code == 200


def test_ending_sessions_when_none_are_open_records_nothing(owner, agent):
    browser = admin_browser(owner)
    url = reverse("admin:accounts_user_sessions", args=[agent.pk])
    response = browser.post(url, {"reason": "Leaving"})
    assert response.status_code == 302
    assert not AuditEvent.objects.filter(action=session_list.ACTION).exists()


@pytest.mark.parametrize("role", ["admin", "supervisor", "client_services"])
def test_only_a_superuser_reaches_end_sessions(staff, agent, role):
    target = signed_in(agent)
    lab_admin = staff(f"list-role-{role}", role)
    User.objects.filter(pk=lab_admin.pk).update(is_staff=True)
    lab_admin.refresh_from_db()
    browser = signed_in(lab_admin)
    url = reverse("admin:accounts_user_sessions", args=[agent.pk])
    for response in (browser.get(url), browser.post(url, {"reason": "x"})):
        assert response.status_code in (302, 403)
        if response.status_code == 302:
            assert "/sessions/" not in response["Location"]
    assert row_of(target).ended_at is None


def test_a_portal_account_cannot_reach_end_sessions(portal, agent):
    target = signed_in(agent)
    browser = signed_in(portal)
    url = reverse("admin:accounts_user_sessions", args=[agent.pk])
    response = browser.post(url, {"reason": "x"})
    assert response.status_code in (302, 403)
    assert row_of(target).ended_at is None


def test_a_superuser_whose_status_was_removed_meanwhile_is_refused(owner, agent):
    target = signed_in(agent)
    with pytest.raises(PermissionDenied):
        User.objects.filter(pk=owner.pk).update(is_superuser=False)
        session_list.end_account_sessions(actor=owner, account_id=agent.pk, reason="Leaving")
    assert row_of(target).ended_at is None


def test_the_scheduled_jobs_account_has_no_end_sessions(owner):
    from jobs.actor import system_account

    jobs = system_account()
    browser = admin_browser(owner)
    response = browser.get(reverse("admin:accounts_user_sessions", args=[jobs.pk]))
    assert response.status_code == 403
    with pytest.raises(PermissionDenied):
        session_list.end_account_sessions(actor=owner, account_id=jobs.pk, reason="x")


def test_an_unknown_account_is_not_found(owner):
    browser = admin_browser(owner)
    response = browser.get(reverse("admin:accounts_user_sessions", args=[987654]))
    assert response.status_code in (302, 404)
    with pytest.raises(User.DoesNotExist):
        session_list.end_account_sessions(actor=owner, account_id=987654, reason="x")


def test_the_reason_is_required_by_the_service_too(owner, agent):
    with pytest.raises(ValidationError):
        session_list.end_account_sessions(actor=owner, account_id=agent.pk, reason=" ")
