"""Independent review of "preferences kept in the session", round 1 (candidate 3b7c04e, base
be87eb8). All records are synthetic.

Adopted from the reviewer's file (archived as test_preferences_in_session_round1_original.py),
with these changes, each explained at the test:
- P1, P2 and P3 ran a real be87eb8 process beside the candidate. That needs the base checkout,
  so P1 is replaced by an in-process stand-in for an earlier release's writer (a plain insert held
  open in another session), with a case for each writer. P2 and P3 are documented bounds of a
  rolling deploy (docs/architecture.md, the changelog's technical notes) and stay in the archive.
- P7 asserts the documented bound: accounts whose ids share the lock's key take turns and are
  counted separately.
- P11 asserts what the Session length now says: messages kept in the session save it.
"""

import json
import random
from datetime import timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib import messages
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.models import Session as StoredSession
from django.core.management import call_command
from django.db import connection, transaction
from django.http import HttpResponse
from django.test import Client as Browser
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts import acting
from accounts.models import User
from clients.services import hold_directory
from core import changelog, demo, preferences
from core.concurrency import reading_account
from core.models import AuditEvent, UserPreference
from desk import notifications
from desk.models import ServiceRequest

from .conftest import PASSWORD
from .test_contract import new_request
from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by, paused

pytestmark = pytest.mark.django_db(transaction=True)

LIST = "clients:client-list"
LAYOUT = {"order": ["name"], "hidden": ["state"]}
GRID = "grid:clients.clients:v1"
BASE = Path("/tmp/labops-pis-base")


@pytest.fixture(autouse=True)
def local_mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


def tab(client):
    other = Browser()
    other.cookies = client.cookies.copy()
    return other


def post(client, key, value):
    return client.post(
        reverse("preference"), {"key": key, "value": value}, content_type="application/json"
    )


def stored(user):
    return UserPreference.objects.filter(user=user)


def fill(user, count):
    UserPreference.objects.bulk_create(
        UserPreference(user=user, key=f"page:list{n}", value={"per_page": 25}) for n in range(count)
    )


def committed(work):
    other = Session(work).start()
    assert other.join() == "done", other.outcome
    return other.result


def sidebar(page):
    assert page.status_code == 200
    tag = b'<script id="vr-preferences" type="application/json">'
    return json.loads(page.content.split(tag, 1)[1].split(b"</script>", 1)[0]).get("nav.collapsed")


@pytest.mark.parametrize("first_parameter", ["per_page", "sort"])
def test_two_tabs_changing_different_parts_of_one_list_keep_both(client, agent, first_parameter):
    client.force_login(agent)
    url = reverse(LIST)
    assert client.get(url, {"per_page": 25, "sort": "name"}).status_code == 200
    other = tab(client)
    values = {"sort": "-account_number", "per_page": 50}
    first = {first_parameter: values[first_parameter]}
    second = {key: value for key, value in values.items() if key != first_parameter}

    def save_other():
        assert committed(lambda: other.get(url, second).status_code) == 200
        assert stored(agent).get(key=f"page:{LIST}").value.items() >= second.items()

    with paused(preferences, "for_request", save_other, before=False):
        assert client.get(url, first).status_code == 200
    following = other.get(url)
    assert following.status_code == 200
    actual = (following.context["page_obj"].paginator.per_page, following.wsgi_request.GET["sort"])
    assert (stored(agent).get(key=f"page:{LIST}").value, actual) == (
        values,
        (50, "-account_number"),
    )


def test_a_visit_with_an_earlier_release_list_keeps_already_seen_entries(client, agent):
    """A subsequent visit on a worker with fewer release entries must not revive an unread badge."""
    client.force_login(agent)
    earlier_browser = Browser()
    earlier_browser.force_login(agent)
    entries = changelog.entries()
    latest, older = entries[0], entries[1:]
    assert latest.id in changelog.seen_marker(entries)["ids"]
    assert client.get(reverse("whats-new")).status_code == 200
    with patch.object(changelog, "entries", return_value=older):
        assert committed(lambda: earlier_browser.get(reverse("whats-new")).status_code) == 200
    fresh = Browser()
    fresh.force_login(agent)
    page = fresh.get(reverse("dashboard"))
    assert page.status_code == 200
    unread = changelog.unread(entries, stored(agent).get(key=preferences.WHATS_NEW_KEY).value)
    assert ([e.id for e in unread], b"data-vr-unread" in page.content) == ([], False)


def earlier_release_insert(user, key, value):
    """What an earlier release's writer does: a plain insert, without the additions lock."""
    return lambda: UserPreference.objects.create(user=user, key=key, value=value)


@pytest.mark.parametrize("choice", ["save", "first-mark", "visit", "list"])
def test_a_row_an_earlier_release_adds_meanwhile_is_replaced_kept_or_combined(
    client, agent, choice
):
    """P1, adapted. An earlier release's writer adds the same key without the additions lock,
    held open in another session. This release's addition waits on the unique index, then finds
    the row: an explicit save replaces it, the first page's mark keeps it, a What's new visit and
    a list's page size are combined with it. Never a server error, never a second row."""
    client.force_login(agent)
    client.raise_request_exception = False
    entries = changelog.entries()
    older_mark = changelog.seen_marker(entries[1:])
    key, earlier_value, second, status, expected = {
        "save": (
            "nav.collapsed",
            True,
            lambda: post(client, "nav.collapsed", False).status_code,
            204,
            False,
        ),
        "first-mark": (
            preferences.WHATS_NEW_KEY,
            older_mark,
            lambda: client.get(reverse("dashboard")).status_code,
            200,
            older_mark,
        ),
        "visit": (
            preferences.WHATS_NEW_KEY,
            older_mark,
            lambda: client.get(reverse("whats-new")).status_code,
            200,
            changelog.combined_marker(older_mark, changelog.seen_marker(entries)),
        ),
        "list": (
            f"page:{LIST}",
            {"sort": "-name"},
            lambda: client.get(reverse(LIST), {"per_page": 50}).status_code,
            200,
            {"sort": "-name", "per_page": 50},
        ),
    }[choice]
    holder = Held(earlier_release_insert(agent, key, earlier_value))
    writer = Session(second).start()
    try:
        state = writer.waiting_or_ended()
        blocked = state == "waiting" and _blocked_by(writer.pid, holder.session.pid)
    finally:
        assert holder.release() == "done", holder.session.outcome
        writer.join()
    assert (state, blocked) == ("waiting", True)
    assert (writer.outcome, writer.result) == ("done", status), writer.error
    assert list(stored(agent).filter(key=key).values_list("value", flat=True)) == [expected]


def test_lookup_with_a_page_size_leaves_room_for_a_list_layout(client, agent):
    client.force_login(agent)
    fill(agent, preferences.MAX_PREFERENCES - 1)
    lookup = client.get(reverse("lookup", args=["clients"]), {"per_page": 50})
    assert lookup.status_code == 200 and lookup.json()["results"] == []
    result = post(client, GRID, LAYOUT)
    assert (result.status_code, stored(agent).filter(key="page:lookup").exists()) == (204, False)


def test_unavailable_sort_does_not_discard_the_saved_column(client, agent):
    client.force_login(agent)
    url = reverse(LIST)
    assert client.get(url, {"sort": "-account_number"}).status_code == 200
    old_link = client.get(url, {"sort": "former_column"})
    assert old_link.status_code == 200 and old_link.context["sort"] == "name"
    following = client.get(url)
    assert (stored(agent).get(key=f"page:{LIST}").value, following.context["sort"]) == (
        {"sort": "-account_number"},
        "-account_number",
    )


def test_accounts_sharing_an_additions_lock_key_take_turns_and_count_separately(agent, make_user):
    """P7, adapted to the documented bound (core.preferences.PREFERENCE_ADDITIONS): the lock's
    second key is the id modulo 2**31 - 1, as for the access gate, so accounts whose ids differ by
    that share it. That only makes their additions take turns; each is counted for its own
    account."""
    colleague = make_user("colleague", "technical", pk=agent.pk + acting.INT4)
    fill(colleague, preferences.MAX_PREFERENCES - 1)
    holder = Held(lambda: preferences.save(SimpleNamespace(user=agent), "nav.collapsed", True))
    writer = Session(
        lambda: preferences.save(SimpleNamespace(user=colleague), "nav.collapsed", True)
    ).start()
    try:
        state = writer.waiting_or_ended()
        blocked = state == "waiting" and _blocked_by(writer.pid, holder.session.pid)
    finally:
        assert holder.release() == "done", holder.session.outcome
        writer.join()
    assert (state, blocked, writer.outcome) == ("waiting", True, "done")
    assert (stored(agent).count(), stored(colleague).count()) == (1, preferences.MAX_PREFERENCES)


def test_messages_kept_in_the_session_save_it_as_session_length_says(client, agent, rf):
    """P11, adapted: the Session length fact now names messages too long for their cookie among
    what saves a session, as Django does when a page shows them; the test asserts that."""
    client.force_login(agent)
    assert client.get(reverse("dashboard")).status_code == 200  # warm the base's preferences too
    request = rf.get("/")
    request.session = client.session
    storage = FallbackStorage(request)
    words = "Monday Tuesday Wednesday Thursday Friday Saturday Sunday morning noon evening".split()
    rng = random.Random(42)
    for _ in range(70):
        storage.add(messages.INFO, " ".join(rng.choices(words, k=45)))
    response = HttpResponse()
    storage.update(response)
    assert request.session.get("_messages") and "messages" in response.cookies
    request.session.save()
    client.cookies.update(response.cookies)
    row = StoredSession.objects.get(session_key=request.session.session_key)
    earlier = timezone.now() + timedelta(days=1)
    StoredSession.objects.filter(pk=row.pk).update(expire_date=earlier)
    page = client.get(reverse("dashboard"))
    assert page.status_code == 200 and "_messages" not in client.session
    row.refresh_from_db()
    assert row.expire_date > earlier + timedelta(days=12)
    from core.security_settings import in_force

    stated = next(
        fact.note for group in in_force() for fact in group.facts if fact.key == "session-length"
    )
    assert "messages too long for their cookie" in stated
    assert "changing a display preference do not otherwise save it" in stated


@pytest.mark.parametrize("remember", [False, True])
def test_signin_page_preference_reauthentication_and_signout_cookies(
    client, agent, settings, remember
):
    fields = {"login": agent.username, "password": PASSWORD}
    if remember:
        fields["remember"] = "on"
    cookie = settings.SESSION_COOKIE_NAME
    login = client.post(reverse("account_login"), fields)
    assert login.status_code == 302
    assert bool(login.cookies[cookie]["max-age"]) == remember
    assert client.session["_auth_user_id"] == str(agent.pk)
    # The login message normally fits in its own cookie. Open the shell twice for the base.
    assert client.get(reverse("dashboard")).status_code == 200
    assert cookie not in client.get(reverse("dashboard")).cookies
    again = client.post(reverse("account_reauthenticate"), {"password": PASSWORD})
    assert again.status_code == 302 and cookie in again.cookies
    assert bool(again.cookies[cookie]["max-age"]) == remember
    logout = client.post(reverse("account_logout"))
    assert logout.status_code == 302 and logout.cookies[cookie]["max-age"] == 0
    assert not StoredSession.objects.filter(pk=login.cookies[cookie].value).exists()


def test_preference_cache_tracks_the_account_during_one_request(rf, agent, clinic_portal):
    UserPreference.objects.create(user=agent, key="nav.collapsed", value=True)
    UserPreference.objects.create(user=clinic_portal, key="nav.collapsed", value=False)
    request = rf.get("/")
    request.user = AnonymousUser()
    assert preferences.for_request(request) == {}
    request.user = agent
    assert preferences.for_request(request) == {"nav.collapsed": True}
    reading_account(request)
    assert preferences.for_request(request) == {"nav.collapsed": True}
    request.user = clinic_portal
    assert preferences.for_request(request) == {"nav.collapsed": False}
    request.user = AnonymousUser()
    assert preferences.for_request(request) == {}


def test_account_types_see_their_own_settings(client, agent, clinic_portal, make_user):
    roleless = make_user("newcomer")
    assert post(client, GRID, LAYOUT).status_code == 302
    for user, workspace, news, notices in (
        (agent, "dashboard", 200, 200),
        (roleless, "whats-new", 200, 403),
        (clinic_portal, "portal:home", 403, 403),
    ):
        client.force_login(user)
        assert sidebar(client.get(reverse(workspace))) is None
        assert post(client, "nav.collapsed", True).status_code == 204
        assert sidebar(client.get(reverse(workspace))) is True
        assert client.get(reverse("whats-new")).status_code == news
        assert client.get(reverse("email-notices")).status_code == notices
    client.force_login(agent)
    agent.is_active = False
    agent.save(update_fields=["is_active"])
    assert post(client, "nav.collapsed", False).status_code == 302
    assert stored(agent).get(key="nav.collapsed").value is True


@pytest.mark.parametrize("writer", ["save", "save_if_absent"])
def test_stored_choice_is_replaced_after_an_intervening_removal(rf, agent, writer):
    """save's contract at call time, using a request which has already rendered a control."""
    UserPreference.objects.create(user=agent, key="nav.collapsed", value=True)
    request = rf.post(reverse("preference"))
    request.user = agent
    assert preferences.for_request(request)["nav.collapsed"] is True
    other = Browser()
    other.force_login(agent)
    assert committed(lambda: post(other, "nav.collapsed", None).status_code) == 204
    assert not stored(agent).filter(key="nav.collapsed").exists()
    getattr(preferences, writer)(request, "nav.collapsed", True)
    assert list(stored(agent).filter(key="nav.collapsed").values_list("value", flat=True)) == [True]


def test_unrelated_domain_locks_allow_a_display_preference(client, agent, category):
    ticket = new_request(agent, category)
    client.force_login(agent)

    def domain_work():
        demo.hold_seed_lock()
        acting.close_access([agent.pk])
        hold_directory()
        notifications.lock_notice_setting(agent.pk)
        acting._locked_account(agent.pk)  # FOR KEY SHARE
        User.objects.select_for_update(no_key=True).get(pk=agent.pk)
        ServiceRequest.objects.select_for_update().get(pk=ticket.pk)

    holder = Held(domain_work)
    writer = Session(lambda: post(client, GRID, LAYOUT).status_code).start()
    try:
        state = writer.waiting_or_ended()
    finally:
        assert holder.release() == "done", holder.session.outcome
        writer.join()
    assert (state, writer.outcome, writer.result) == ("ended", "done", 204)


def test_full_account_refusal_and_opt_out_and_pool_reuse(client, agent):
    client.force_login(agent)
    fill(agent, preferences.MAX_PREFERENCES)
    refused = post(client, GRID, LAYOUT)
    assert (refused.status_code, refused.content) == (400, b"Too many saved preferences.")
    assert client.post(reverse("email-notices"), {}).status_code == 302
    assert stored(agent).count() == 301 and not notifications.wants_notices(agent)
    assert client.post(reverse("email-notices"), {"enabled": "on"}).status_code == 302
    assert stored(agent).count() == 300 and notifications.wants_notices(agent)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid() AND locktype = 'advisory'"
        )
        assert cursor.fetchone() == (0,)


def test_rolled_back_addition_releases_lock_on_the_same_connection(agent):
    request = SimpleNamespace(user=agent)
    with transaction.atomic():
        preferences.save(request, GRID, LAYOUT)
        transaction.set_rollback(True)
    assert not stored(agent).exists()
    assert committed(lambda: preferences.save(SimpleNamespace(user=agent), GRID, LAYOUT)) is None
    assert stored(agent).get().value == LAYOUT


def test_page_lookup_download_and_fragment_query_counts_are_bounded(client, agent, category):
    from attachments.tests.conftest import png_file
    from attachments.tests.test_attachments import file_of, reply_with

    ticket = new_request(agent, category)
    attachment = file_of(reply_with(ticket, agent, [png_file()]))
    client.force_login(agent)
    assert client.get(reverse("dashboard")).status_code == 200  # create the initial seen mark
    routes = [
        (reverse("desk:request-detail", args=[ticket.pk]), {}),
        (reverse("lookup", args=["clients"]), {}),
        (reverse("attachments:download", args=[attachment.pk]), {}),
        (reverse("desk:request-create"), {"X-VR-Form": "1"}),
    ]

    def counts():
        results = []
        for url, headers in routes:
            with CaptureQueriesContext(connection) as queries:
                response = client.get(url, headers=headers)
                assert response.status_code == 200
            sql = [q["sql"] for q in queries]
            loads = sum('FROM "core_userpreference"' in q for q in sql)
            assert loads <= 1
            results.append((len(sql), loads))
        return results

    before = counts()
    fill(agent, preferences.MAX_PREFERENCES - 1)
    after = counts()
    print("Queries (total, preference reads), four routes, one row then 300:", before, after)
    assert before == after


def test_atomic_request_and_refused_ticket_draw_save_first_mark(
    client, agent, category, monkeypatch
):
    ticket = new_request(agent, category)
    client.force_login(agent)
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)
    with CaptureQueriesContext(connection) as queries:
        page = client.post(reverse("desk:request-entry", args=[ticket.pk]), {})
    assert page.status_code == 400 and page.wsgi_request.user.pk == agent.pk
    assert stored(agent).get(key=preferences.WHATS_NEW_KEY).value == changelog.seen_marker(
        changelog.entries()
    )
    assert "private" in page["Cache-Control"] and "Cookie" in page["Vary"]
    assert any('INSERT INTO "core_userpreference"' in q["sql"] for q in queries)


def test_refused_preference_leaves_no_value_in_request_log_or_audit(client, agent, caplog):
    client.force_login(agent)
    before = AuditEvent.objects.count()
    layout = {"order": ["courier"], "hidden": []}
    assert post(client, GRID, layout).status_code == 204
    fill(agent, preferences.MAX_PREFERENCES - 1)
    assert post(client, "grid:desk.requests:v1", layout).status_code == 400
    assert "courier" not in caplog.text and GRID not in caplog.text
    assert AuditEvent.objects.count() == before


def test_nondefault_sort_applies_to_this_page_at_capacity(client, agent, clinic, water):
    fill(agent, preferences.MAX_PREFERENCES)
    client.force_login(agent)
    client.raise_request_exception = False
    page = client.get(reverse(LIST), {"sort": "-name", "per_page": 50})
    assert page.status_code == 200
    assert [c.pk for c in page.context["page_obj"]] == [water.pk, clinic.pk]
    assert page.context["per_page"] == 50
    following = client.get(reverse(LIST))
    assert [c.pk for c in following.context["page_obj"]] == [clinic.pk, water.pk]
    assert following.context["per_page"] == 25


def test_a_removal_releases_its_place_at_commit(client, agent):
    fill(agent, preferences.MAX_PREFERENCES - 1)
    UserPreference.objects.create(user=agent, key=GRID, value=LAYOUT)
    client.force_login(agent)
    elsewhere = Browser()
    elsewhere.force_login(agent)

    def remove():
        assert post(client, GRID, None).status_code == 204

    holder = Held(remove)
    try:
        assert committed(lambda: post(elsewhere, "nav.collapsed", True).status_code) == 400
    finally:
        assert holder.release() == "done", holder.session.outcome
    assert post(elsewhere, "nav.collapsed", True).status_code == 204
    assert stored(agent).count() == preferences.MAX_PREFERENCES


def test_opt_out_can_commit_during_an_ordinary_addition(client, agent):
    fill(agent, preferences.MAX_PREFERENCES - 1)
    client.force_login(agent)
    elsewhere = Browser()
    elsewhere.force_login(agent)

    def add():
        assert post(client, GRID, LAYOUT).status_code == 204

    holder = Held(add)
    writer = Session(lambda: elsewhere.post(reverse("email-notices"), {}).status_code).start()
    try:
        state = writer.waiting_or_ended()
    finally:
        assert holder.release() == "done", holder.session.outcome
        writer.join()
    assert (state, writer.outcome, writer.result) == ("ended", "done", 302)
    assert stored(agent).exclude(key=notifications.OPT_OUT_KEY).count() == 300
    assert not notifications.wants_notices(agent)


def test_account_foreign_key_wait_finishes_after_account_commit(client, agent):
    client.force_login(agent)
    holder = Held(lambda: User.objects.select_for_update().get(pk=agent.pk))
    writer = Session(lambda: post(client, GRID, LAYOUT).status_code).start()
    try:
        state = writer.waiting_or_ended()
        blocked = state == "waiting" and _blocked_by(writer.pid, holder.session.pid)
    finally:
        assert holder.release() == "done", holder.session.outcome
        writer.join()
    assert blocked and (writer.outcome, writer.result) == ("done", 204)


def test_refused_new_ticket_marks_first_page_inside_the_request_transaction(
    client, agent, monkeypatch
):
    client.force_login(agent)
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)
    in_transaction = []

    def record():
        in_transaction.append(connection.in_atomic_block)

    with paused(preferences, "save_if_absent", record):
        page = client.post(reverse("desk:request-create"), {"subject": "Collection"})
    assert page.status_code in (200, 400, 422) and page.context["form"].errors
    # Adapted in the verification round: the first mark is now saved once the response is ready
    # (core.preferences.after_response), after the request's transaction, not inside it.
    assert in_transaction == [False]
    assert stored(agent).filter(key=preferences.WHATS_NEW_KEY).exists()


def test_authenticator_setup_is_an_account_page_that_saves_the_session(client, agent, settings):
    from allauth.account.models import EmailAddress

    EmailAddress.objects.update_or_create(
        user=agent, email=agent.email, defaults={"primary": True, "verified": True}
    )
    login = client.post(reverse("account_login"), {"login": agent.username, "password": PASSWORD})
    assert login.status_code == 302
    assert client.get(reverse("dashboard")).status_code == 200
    setup = reverse("mfa_activate_totp")
    challenge = client.get(setup)
    assert challenge.status_code == 302 and challenge.url.startswith(
        reverse("account_reauthenticate")
    )
    checked = client.post(challenge.url, {"password": PASSWORD})
    assert checked.status_code == 302 and checked.url == setup
    row = StoredSession.objects.get(pk=client.session.session_key)
    earlier = timezone.now() + timedelta(days=1)
    StoredSession.objects.filter(pk=row.pk).update(expire_date=earlier)
    response = client.get(reverse("mfa_activate_totp"))
    assert response.status_code == 200 and settings.SESSION_COOKIE_NAME in response.cookies
    row.refresh_from_db()
    assert row.expire_date > earlier + timedelta(days=12)


def test_addition_in_a_repeatable_read_request_keeps_the_limit(client, agent):
    """Deployment case. A request already in REPEATABLE READ retains its earlier snapshot."""
    fill(agent, preferences.MAX_PREFERENCES - 1)
    client.force_login(agent)
    other = Browser()
    other.force_login(agent)

    def add_first():
        assert post(client, GRID, LAYOUT).status_code == 204

    def add_second():
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            return post(other, "nav.collapsed", True).status_code

    holder = Held(add_first)
    writer = Session(add_second).start()
    try:
        state = writer.waiting_or_ended()
        assert state == "ended" or (
            state == "waiting" and _blocked_by(writer.pid, holder.session.pid)
        )
    finally:
        assert holder.release() == "done", holder.session.outcome
        writer.join()
    assert (writer.outcome, writer.result, stored(agent).count()) == ("done", 400, 300)


def test_upgrade_requires_no_migration():
    call_command("makemigrations", "--check", "--dry-run", "--no-input", stdout=StringIO())
