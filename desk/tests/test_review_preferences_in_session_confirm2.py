"""Second, narrow confirmation (06845dc, compared with e8e185f) of "preferences kept in the
session". All records are synthetic.

Adopted from the reviewer's file (archived in the review history as the round-1 confirm2
original) with these changes, each explained at the test: D3 (the missing-slash redirect at 299),
D4 (a page failing after drawing the top bar) and D7's outer-commit case assert the documented
bounds.
"""

import json
import subprocess
from pathlib import Path

import pytest
from django.db import connection
from django.urls import reverse

from core import changelog, preferences
from core.models import UserPreference

from .test_contract import new_request
from .test_preferences_in_session import edition, fill, post

pytestmark = pytest.mark.django_db(transaction=True)


def _marks(user):
    return UserPreference.objects.filter(user=user, key=preferences.WHATS_NEW_KEY)


@pytest.mark.parametrize("existing", [False, True], ids=["first-mark", "unread-entry"])
def test_head_of_whats_new_neither_initializes_nor_acknowledges_changes(
    client, agent, monkeypatch, existing
):
    old, current = edition(18), edition(20)
    monkeypatch.setattr(changelog, "entries", lambda: [current, old])
    retained = []
    if existing:
        retained.append(changelog.seen_marker([old]))
        _marks(agent).create(user=agent, key=preferences.WHATS_NEW_KEY, value=retained[0])
    client.force_login(agent)
    response = client.head(reverse("whats-new"))
    assert response.status_code == 200 and not response.content
    assert list(_marks(agent).values_list("value", flat=True)) == retained


@pytest.mark.parametrize("canonicalize", [False, True], ids=["canonical", "without-slash"])
def test_lookup_followed_by_an_explicit_save_can_use_the_last_place(
    client, agent, settings, canonicalize
):
    settings.DEBUG = False
    client.force_login(agent)
    fill(agent, preferences.MAX_PREFERENCES - 1)
    url = reverse("lookup", args=["clients"])
    if canonicalize:
        redirect = client.get(url.rstrip("/"))
        assert redirect.status_code in (301, 308) and redirect.url == url and not redirect.content
    lookup = client.get(url)
    assert lookup.status_code == 200 and "json" in lookup["Content-Type"]
    result = post(client, "nav.collapsed", True)
    # Adapted (D3, documented bound in docs/architecture.md): an address without its trailing
    # slash draws a 404 page, with the top bar and its first mark, before the redirect replaces
    # it, so at 299 that mark takes the last place.
    expected = (400, True) if canonicalize else (204, False)
    assert (result.status_code, _marks(agent).exists()) == expected


@pytest.mark.parametrize("transition", ["staff", "portal", "signout"])
def test_snapshot_first_mark_uses_the_account_that_drew_the_ticket(
    client, agent, category, clinic_portal, make_user, monkeypatch, transition
):
    from django.contrib.auth import logout

    from desk import views

    other = clinic_portal if transition == "portal" else make_user("day-shift", "technical")
    ticket = new_request(agent, category)
    client.force_login(agent)
    client.raise_request_exception = False
    viewed = []
    render = views.render

    def switch_at_end_of_render(request, *args, **kwargs):
        response = render(request, *args, **kwargs)
        assert b"vr-topbar-btn vr-whats-new" in response.content and connection.in_atomic_block
        viewed.append(request.user.pk)
        if transition == "signout":
            logout(request)
        else:
            request.user = other
        return response

    monkeypatch.setattr(views, "render", switch_at_end_of_render)
    response = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    assert viewed == [agent.pk]
    assert (response.status_code, _marks(other).exists()) == (200, False)


@pytest.mark.parametrize("atomic_request", [False, True])
def test_discarded_dashboard_shell_cannot_acknowledge_the_catalog(
    client, agent, monkeypatch, settings, atomic_request
):
    from core import views

    settings.DEBUG = False
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", atomic_request)
    client.force_login(agent)
    client.raise_request_exception = False
    render = views.render
    rendered = []

    def fail_after_drawing(*args, **kwargs):
        response = render(*args, **kwargs)
        rendered.append(b"vr-topbar-btn vr-whats-new" in response.content)
        raise RuntimeError("Synthetic dashboard rendering failure")

    monkeypatch.setattr(views, "render", fail_after_drawing)
    response = client.get(reverse("dashboard"))
    assert rendered == [True] and response.status_code == 500
    assert b"vr-topbar-btn vr-whats-new" not in response.content
    # Adapted (D4, documented bound in docs/architecture.md): a page that fails after drawing
    # the top bar keeps the first mark it recorded, unless ATOMIC_REQUESTS rolls it back.
    assert _marks(agent).exists() == (not atomic_request)


@pytest.mark.parametrize(
    "audience,initial,bare_size", [("staff", 298, 50), ("staff", 299, 25), ("portal", 299, 50)]
)
def test_first_list_at_the_quota_boundary_accounts_for_the_actual_marker(
    client, agent, clinic_portal, audience, initial, bare_size
):
    staff = audience == "staff"
    user = agent if staff else clinic_portal
    route = "clients:client-list" if staff else "portal:request-list"
    client.force_login(user)
    fill(user, initial)
    page = client.get(reverse(route), {"per_page": 50})
    assert page.status_code == 200 and page.context["page_obj"].paginator.per_page == 50
    assert client.get(reverse(route)).context["page_obj"].paginator.per_page == bare_size
    assert _marks(user).exists() == staff
    saved = UserPreference.objects.filter(user=user, key=f"page:{route}").exists()
    assert saved == (bare_size == 50)
    assert UserPreference.objects.filter(user=user).count() == preferences.MAX_PREFERENCES


def _browser_message(status, body, delivery):
    program = """
const fs = require("node:fs");
const [script, statusText, way] = process.argv.slice(1);
const status = Number(statusText);
globalThis.window = globalThis;
const events = [];
globalThis.document = {
  body: {hasAttribute: () => true, getAttribute: () => "{}"},
  getElementById: () => null, dispatchEvent: e => events.push([e.type, e.detail.message])
};
globalThis.localStorage = {};
globalThis.fetch = () => way === "no-reply" ? Promise.reject(new Error("offline")) :
  Promise.resolve({ok: status >= 200 && status < 300, status, redirected: way === "redirect",
    text: () => way === "unreadable" ? Promise.reject(new Error("interrupted")) :
      Promise.resolve(fs.readFileSync(0, "utf8"))});
require(script);
vrPreferences.set("nav.collapsed", true).then(() => console.log(JSON.stringify(events)));
"""
    result = subprocess.run(
        [
            "node",
            "-e",
            program,
            Path(__file__).resolve().parents[2] / "static/js/preferences.js",
            str(status),
            delivery,
        ],
        input=body,
        capture_output=True,
        check=True,
        timeout=10,
    )
    return json.loads(result.stdout)


def test_server_error_after_a_committed_save_does_not_claim_it_was_refused(
    client, agent, monkeypatch, settings
):
    from django.contrib.sessions.middleware import SessionMiddleware

    from .test_preferences_in_session import page_prefs

    settings.DEBUG = False
    client.force_login(agent)
    client.raise_request_exception = False
    finish = SessionMiddleware.process_response
    completed = []

    def fail_finishing_response(middleware, request, response):
        if request.path == reverse("preference") and response.status_code == 204:
            completed.append(
                UserPreference.objects.filter(user=agent, key="nav.collapsed", value=True).exists()
            )
            raise RuntimeError("Synthetic response finalization failure")
        return finish(middleware, request, response)

    monkeypatch.setattr(SessionMiddleware, "process_response", fail_finishing_response)
    reply = post(client, "nav.collapsed", True)
    assert completed == [True] and reply.status_code == 500
    assert page_prefs(client, reverse("dashboard"))["nav.collapsed"] is True
    events = _browser_message(reply.status_code, reply.content, "normal")
    assert len(events) == 1 and events[0][0] == "vr:request-error"
    assert "page only" not in events[0][1] and "could not be saved" not in events[0][1]


@pytest.mark.parametrize(
    "outcome", ["full", "invalid", "ended-session", "no-reply", "unreadable", "saved"]
)
def test_preference_notices_distinguish_refusal_from_no_reply(client, agent, outcome):
    client.force_login(agent)
    if outcome == "full":
        fill(agent, preferences.MAX_PREFERENCES)
    if outcome == "ended-session":
        client.logout()
    reply = post(
        client, "nav.collapsed", "collection" if outcome in ("invalid", "unreadable") else True
    )
    delivery = outcome
    if outcome == "ended-session":
        assert reply.status_code == 302
        reply = client.get(reply.url)
        delivery = "redirect"
    assert (
        reply.status_code
        == {
            "full": 400,
            "invalid": 400,
            "ended-session": 200,
            "no-reply": 204,
            "unreadable": 400,
            "saved": 204,
        }[outcome]
    )
    events = _browser_message(reply.status_code, reply.content, delivery)
    if outcome == "saved":
        assert not events
        return
    assert len(events) == 1 and events[0][0] == "vr:request-error"
    message = events[0][1]
    assert ("as many saved" in message) == (outcome == "full")
    assert ("page only" in message) == (outcome != "no-reply")
    if outcome == "no-reply":
        assert "may not have been saved" in message


def test_explicit_addition_cost_does_not_grow_with_the_account(client, agent, make_user):
    from django.test.utils import CaptureQueriesContext

    other = make_user("evening", "technical")
    fill(other, preferences.MAX_PREFERENCES - 1)
    costs = []
    for person in (agent, other):
        client.force_login(person)
        with CaptureQueriesContext(connection) as queries:
            reply = post(client, "nav.collapsed", True)
        assert reply.status_code == 204
        sql = [q["sql"] for q in queries]
        preference_sql = [q for q in sql if '"core_userpreference"' in q]
        counts = [q for q in preference_sql if "COUNT(" in q]
        assert (
            len(counts) == 1
            and sum("current_setting('transaction_isolation')" in q for q in sql) == 1
        )
        costs.append((len(sql), len(preference_sql)))
    assert costs[0] == costs[1]
    print("Addition SQL totals and preference-table statements at 0 and 299 settings:", costs)


@pytest.mark.parametrize("arrival", ["before-lock", "before-insert", "during-repair"])
def test_first_mark_repairs_the_value_that_is_present_when_saving(client, agent, arrival):
    from unittest.mock import patch

    from django.db import connection
    from django.urls import reverse

    from core import changelog, preferences
    from core.models import UserPreference

    from .test_preferences_in_session import committed, edition

    key = preferences.WHATS_NEW_KEY
    target = UserPreference.objects.filter(user=agent, key=key)
    previous = {"format": "older"}
    unreadable = {"format": "earlier"}
    if arrival == "during-repair":
        target.create(user=agent, key=key, value=previous)
    initial = [edition(19)]
    newer = [edition(20), *initial]
    admitted = []
    client.force_login(agent)

    def write_legacy_mark():
        if arrival == "during-repair":
            return target.filter(value=previous).update(value=unreadable)
        target.create(user=agent, key=key, value=unreadable)
        return 1

    def concurrent_write(execute, sql, params, many, context):
        watch = {
            "before-lock": "pg_advisory_xact_lock",
            "before-insert": 'INSERT INTO "core_userpreference"',
            "during-repair": 'UPDATE "core_userpreference"',
        }[arrival]
        if not admitted and watch in sql:
            admitted.append(committed(write_legacy_mark))
        return execute(sql, params, many, context)

    with (
        connection.execute_wrapper(concurrent_write),
        patch.object(changelog, "entries", return_value=initial),
    ):
        assert client.get(reverse("dashboard")).status_code == 200
    assert admitted == [1]
    value = target.get().value
    with patch.object(changelog, "entries", return_value=newer):
        following = client.get(reverse("dashboard"))
    assert following.status_code == 200
    assert (value, b'data-vr-unread="1"' in following.content) == (
        changelog.seen_marker(initial),
        True,
    )


@pytest.mark.parametrize("withdrawals", [2, 3])
def test_first_mark_never_returns_success_after_only_unreadable_conflicts(rf, agent, withdrawals):
    from django.db import IntegrityError, connection

    from core import changelog, preferences
    from core.models import UserPreference

    from .test_preferences_in_session import committed, edition

    key = preferences.WHATS_NEW_KEY
    desired = changelog.seen_marker([edition(19)])
    old = {"format": "former"}
    target = UserPreference.objects.filter(user=agent, key=key)
    attempts = []
    request = rf.get("/")
    request.user = agent

    def conflict(execute, sql, params, many, context):
        if 'INSERT INTO "core_userpreference"' not in sql or len(attempts) == withdrawals:
            return execute(sql, params, many, context)
        attempts.append(committed(lambda: target.create(user=agent, key=key, value=old).pk))
        try:
            return execute(sql, params, many, context)
        except IntegrityError as error:
            assert error.__cause__.diag.constraint_name == "unique_user_preference"
            assert committed(lambda: target.filter(value=old).delete()[0]) == 1
            raise

    with connection.execute_wrapper(conflict):
        try:
            preferences.save_if_absent(request, key, desired)
            outcome = "saved"
        except ValueError:
            outcome = "refused"
    # At no time did the concurrent transactions produce a valid first mark to keep.
    values = list(target.values_list("value", flat=True))
    if outcome == "refused":
        assert len(attempts) == 3 and values == [] and key not in preferences.for_request(request)
    else:
        assert (values, preferences.for_request(request).get(key)) == ([desired], desired)


@pytest.mark.parametrize("stage", ["before-first-mark", "outer-commit"])
def test_departed_list_account_does_not_fail_the_write_or_the_outer_commit(
    client, agent, clinic, monkeypatch, stage
):
    from contextlib import nullcontext

    from django.contrib.auth import get_user_model
    from django.db import IntegrityError, transaction
    from django.urls import reverse

    from core import changelog, preferences
    from core.models import UserPreference

    from .test_preferences_in_session import committed

    if stage == "outer-commit":
        UserPreference.objects.create(
            user=agent,
            key=preferences.WHATS_NEW_KEY,
            value=changelog.seen_marker(changelog.entries()),
        )
    client.force_login(agent)
    client.raise_request_exception = False
    pk = agent.pk
    removed = []
    method = "save_combined" if stage == "outer-commit" else "save_if_absent"
    original = getattr(preferences, method)

    def remove_then_save(*args, **kwargs):
        removed.append(committed(lambda: get_user_model().objects.filter(pk=pk).delete()[0]))
        return original(*args, **kwargs)

    monkeypatch.setattr(preferences, method, remove_then_save)
    if stage == "outer-commit":
        # Adapted for the automatic sign-out change: the session's activity statement runs in
        # its own transaction before the view in production (accounts.sessions); inside this
        # test's outer transaction it would hold the session row the deletion removes with the
        # account, and the deletion would wait for this thread.
        from accounts import sessions

        monkeypatch.setattr(sessions, "_touch", lambda state: True)
    status, failure = None, None
    try:
        with transaction.atomic() if stage == "outer-commit" else nullcontext():
            page = client.get(
                reverse("clients:client-list"), {"per_page": 50} if stage == "outer-commit" else {}
            )
            status = page.status_code
            if page.exc_info:
                failure = getattr(page.exc_info[1].__cause__, "sqlstate", None)
    except IntegrityError as error:
        failure = error.__cause__.sqlstate
    assert len(removed) == 1 and removed[0] >= 1
    assert not UserPreference.objects.filter(user_id=pk).exists()
    # Adapted (D7): the first mark and the list's own commit treat a removed account as "not
    # saved". Documented bound (account deletion is disabled in the application's admin): inside
    # a transaction around the whole request the database checks the account only at its outer
    # commit, which fails.
    expected = (200, "23503") if stage == "outer-commit" else (200, None)
    assert (status, failure) == expected


@pytest.mark.parametrize(
    "isolation,accepted", [("READ_UNCOMMITTED", True), ("REPEATABLE_READ", False)]
)
def test_driver_setting_guards_existing_rows_and_first_marks(rf, agent, isolation, accepted):
    from django.db import connection
    from psycopg import IsolationLevel

    from core import changelog, preferences
    from core.models import UserPreference

    from .test_preferences_in_session import edition

    request = rf.get("/")
    request.user = agent
    key = "nav.collapsed"
    mark = changelog.seen_marker([edition(19)])
    rows = UserPreference.objects.filter(user=agent)
    rows.create(user=agent, key=key, value=True)
    assert connection.get_autocommit()
    driver = connection.connection
    former = driver.isolation_level
    results = []
    try:
        driver.isolation_level = getattr(IsolationLevel, isolation)
        for write in (
            lambda: preferences.save(request, key, False),
            lambda: preferences.save(request, key, None),
            lambda: preferences.save_if_absent(request, preferences.WHATS_NEW_KEY, mark),
        ):
            try:
                write()
                results.append(True)
            except ValueError:
                results.append(False)
    finally:
        driver.isolation_level = former
    assert results == [accepted] * 3
    expected = {preferences.WHATS_NEW_KEY: mark} if accepted else {key: True}
    assert dict(rows.values_list("key", "value")) == expected


def test_full_account_cannot_borrow_the_same_key_stored_by_a_different_account(
    client, agent, make_user
):
    from core import preferences
    from core.models import UserPreference

    from .test_preferences_in_session import fill, post

    another = make_user("collection-observer", "technical")
    key = "nav.collapsed"
    UserPreference.objects.create(user=another, key=key, value=True)
    fill(agent, preferences.MAX_PREFERENCES)
    client.force_login(agent)
    assert post(client, key, False).status_code == 400
    assert not UserPreference.objects.filter(user=agent, key=key).exists()
    assert UserPreference.objects.filter(user=agent, key="page:list1").delete()[0] == 1
    assert post(client, key, False).status_code == 204
    assert UserPreference.objects.get(user=another, key=key).value is True
    assert UserPreference.objects.get(user=agent, key=key).value is False


@pytest.mark.parametrize(
    "old_count,latest_in_visiting_catalog",
    [(398, "2026-11-19"), (399, "2026-11-19"), (399, "2026-11-20"), (399, "2026-11-21")],
)
def test_expired_entries_leave_room_for_a_late_release_visit(
    client, agent, old_count, latest_in_visiting_catalog
):
    from django.test import Client

    from .test_review_preferences_in_session_verify import (
        release_badge,
        release_entry,
        release_page,
    )

    client.force_login(agent)
    other = Client()
    other.force_login(agent)
    august = [release_entry("2026-08-01", f"hours-{i}") for i in range(old_count)]
    november = [release_entry("2026-11-20", "collection")]
    late = [release_entry(latest_in_visiting_catalog, "courier")]

    # The entire stored mark arises on ordinary page visits. Even in the larger
    # case, only the two November entries are now inside the remembered window.
    release_page(client, august)
    release_page(client, november)
    assert release_badge(other, late) == 1
    assert release_page(other, late).context["new_count"] == 1
    # Adapted in the third confirmation (D8 returned to the documented 400-id bound, E5/E6):
    # ids of entries this visit does not know are kept, so 399 August ids plus a November one
    # fill the mark before an earlier-dated visit.
    crowded = old_count == 399 and latest_in_visiting_catalog == "2026-11-19"
    assert release_badge(client, [*november, *late]) == (1 if crowded else 0)


@pytest.mark.parametrize("other_entries", [1, 400])
@pytest.mark.parametrize("long_name", [False, True], ids=["80-character-slug", "81-character-slug"])
def test_every_accepted_release_file_can_be_acknowledged(
    client, agent, tmp_path, other_entries, long_name
):
    from core import changelog

    from .test_review_preferences_in_session_verify import (
        release_badge,
        release_entry,
        release_page,
    )

    if long_name:
        slug = "remember-the-selected-service-level-calendar-when-editing-the-client-organization"
    else:
        slug = "remember-the-selected-service-level-calendar-when-editing-client-portal-accounts"
    assert len(slug) == 80 + int(long_name)
    file = tmp_path / f"2026-10-01-{slug}.md"
    file.write_text(
        "---\ndate: 2026-11-20\ntype: changed\nimpact: low\ntitle: Collection hours\n"
        "---\nCollection hours changed.\n"
    )
    try:
        parsed = changelog.read_entries(tmp_path)
    except changelog.ChangelogError:
        # Refusing an unsupported filename at release validation also protects the reader.
        assert long_name
        return
    assert len(parsed) == 1 and parsed[0].id == file.stem
    catalog = [release_entry("2026-11-10", f"schedule-{i}") for i in range(other_entries)] + parsed
    client.force_login(agent)
    release_page(client, [release_entry("2026-09-01", "baseline")])
    assert release_page(client, catalog).context["new_count"] == len(catalog)
    assert release_badge(client, catalog) <= max(0, len(catalog) - changelog.SEEN_IDS_MAX)


def test_equal_publication_dates_give_a_stable_bounded_marker():
    from core import changelog

    from .test_review_preferences_in_session_verify import release_entry

    entries = [release_entry("2026-11-20", f"hours-{i}") for i in range(402)]
    forward = changelog.seen_marker(entries)
    backward = changelog.seen_marker(entries[::-1])
    assert forward == backward
    assert forward["ids"] == sorted(set(forward["ids"]), reverse=True)
    assert len(forward["ids"]) == changelog.SEEN_IDS_MAX
    assert len(changelog.unread(entries, forward)) == 2


def _confirmation_controls(html):
    from html.parser import HTMLParser

    class Controls(HTMLParser):
        def __init__(self):
            super().__init__()
            self.forms = []
            self.links = []
            self.form = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "form":
                self.form = (attrs, {})
                self.forms.append(self.form)
            elif tag == "input" and self.form and attrs.get("type") == "hidden":
                self.form[1][attrs["name"]] = attrs.get("value", "")
            elif tag == "a" and "href" in attrs:
                self.links.append(attrs)

        def handle_endtag(self, tag):
            if tag == "form":
                self.form = None

    parser = Controls()
    parser.feed(html)
    return parser


@pytest.mark.parametrize("saved_view", [False, True], ids=["system-view", "saved-view"])
def test_applying_a_ticket_filter_preserves_the_other_browsers_settings(
    client, agent, category, saved_view
):
    from django.test import Client as Browser
    from django.urls import reverse

    from core.models import UserPreference
    from desk.models import SavedView

    from .test_contract import new_request

    new_request(agent, category, subject="Collection hours")
    row = UserPreference.objects.create(
        user=agent, key="page:desk:request-list", value={"per_page": 25, "sort": "subject"}
    )
    client.force_login(agent)
    other = Browser()
    other.force_login(agent)
    url = reverse("desk:request-list")
    explicit = {"view": "all"}
    if saved_view:
        view = SavedView.objects.create(owner=agent, name="Collections", filters={"view": "all"})
        explicit["saved"] = str(view.pk)
    page = client.get(url, explicit)
    assert page.status_code == 200 and page.context["page_obj"].paginator.count
    form, fields = next(
        item
        for item in _confirmation_controls(page.content.decode()).forms
        if "vr-grid-toolbar" in item[0].get("class", "").split()
    )
    assert all(fields[name] == value for name, value in explicit.items())
    chosen = {"per_page": 50, "sort": "-subject"}
    assert other.get(url, {**explicit, **chosen}).status_code == 200
    row.refresh_from_db()
    assert row.value == chosen
    result = client.get(form["action"], {**fields, "q": "Collection"})
    assert result.status_code == 200 and result.context["filter_form"].is_valid()
    assert result.context["page_obj"].paginator.count
    row.refresh_from_db()
    assert (result.context["sort"], result.context["page_obj"].paginator.per_page, row.value) == (
        "-subject",
        50,
        chosen,
    )


@pytest.mark.parametrize(
    "route", ["clients:client-list", "desk:request-list", "portal:request-list"]
)
def test_returning_from_a_list_record_keeps_the_other_browsers_later_choice(
    client, agent, category, clinic, clinic_portal, route
):
    from urllib.parse import parse_qs, urlsplit

    from django.test import Client as Browser
    from django.urls import reverse

    from clients.models import PortalAccess
    from core.models import UserPreference

    from .test_contract import new_request

    new_request(agent, category, client=clinic, subject="Example collection")
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    person = clinic_portal if route.startswith("portal:") else agent
    sort = "name" if route.startswith("clients:") else "subject"
    row = UserPreference.objects.create(
        user=person, key=f"page:{route}", value={"sort": sort, "per_page": 25}
    )
    client.force_login(person)
    other = Browser()
    other.force_login(person)
    url = reverse(route)
    params = {"q": "Example"}
    if route.startswith("desk:"):
        params["view"] = "all"
    listed = client.get(url, params)
    assert listed.status_code == 200 and listed.context["page_obj"].paginator.count
    link = next(
        a
        for a in _confirmation_controls(listed.content.decode()).links
        if "vr-row-link" in a.get("class", "").split()
    )
    record = client.get(link["href"])
    assert record.status_code == 200
    back = record.context["list_url"]
    assert any(a["href"] == back for a in _confirmation_controls(record.content.decode()).links)
    assert parse_qs(urlsplit(back).query)["q"] == ["Example"]
    chosen = {"sort": "-" + sort, "per_page": 50}
    assert other.get(url, {**params, **chosen}).status_code == 200
    row.refresh_from_db()
    assert row.value == chosen
    returning = client.get(back)
    assert returning.status_code == 200 and returning.context["filter_form"].is_valid()
    row.refresh_from_db()
    assert (
        returning.context["sort"],
        returning.context["page_obj"].paginator.per_page,
        row.value,
    ) == ("-" + sort, 50, chosen)


def test_following_the_unknown_queue_redirect_does_not_restore_an_inherited_sort(client, agent):
    from django.test import Client as Browser
    from django.urls import reverse

    from core.models import UserPreference

    url = reverse("desk:request-list")
    client.force_login(agent)
    other = Browser()
    other.force_login(agent)
    row = UserPreference.objects.create(
        user=agent, key="page:desk:request-list", value={"sort": "subject", "per_page": 25}
    )
    redirect = client.get(url, {"view": "former"})
    assert redirect.status_code == 302
    changed = {"sort": "-subject", "per_page": 50}
    assert other.get(url, changed).status_code == 200
    row.refresh_from_db()
    assert row.value == changed
    assert client.get(redirect["Location"]).status_code == 200
    row.refresh_from_db()
    assert row.value == changed


@pytest.mark.parametrize("standalone", [False, True])
def test_multi_page_navigation_and_sort_only_change_the_intended_query(rf, standalone):
    from urllib.parse import parse_qs, urlsplit

    from django.core.paginator import Paginator
    from django.template import Context, RequestContext, Template

    request = rf.get(
        "/examples/?page=3&sites_page=2&notes_page=5&sort=name&per_page=25"
        "&site=East&site=West&q=Day+%26+Night"
    )
    request.vr_saved_params = {"sort", "per_page"}
    page = Paginator(range(100), 25).page(2)
    values = {"items": page} if standalone else {"items": page, "request": request}
    context = Context(values) if standalone else RequestContext(request, values)
    html = Template(
        "{% load ui %}"
        '{% sort_header label="Name" asc="name" desc="-name" sort="name" %}'
        '{% pagination page_obj=items page_parameter="sites_page" noun="sites" %}'
    ).render(context)
    links = _confirmation_controls(html).links
    sorting = next(a["href"] for a in links if a.get("class") == "vr-sort")
    next_page = next(a["href"] for a in links if a.get("rel") == "next")
    sort_query = parse_qs(urlsplit(sorting).query)
    next_query = parse_qs(urlsplit(next_page).query)
    if standalone:
        assert sort_query == {"sort": ["-name"]}
        assert next_query == {"sites_page": ["3"]}
    else:
        shared = {"notes_page": ["5"], "q": ["Day & Night"], "site": ["East", "West"]}
        assert next_query == {**shared, "sites_page": ["3"], "page": ["3"]}
        assert sort_query == {**shared, "sites_page": ["2"], "sort": ["-name"]}


@pytest.mark.parametrize("explicit", [False, True], ids=["inherited", "in-address"])
def test_filter_toolbar_distinguishes_chosen_settings_on_each_grid(
    client, make_user, clinic_portal, explicit
):
    from django.urls import reverse

    from core.models import LabModule, UserPreference

    LabModule.objects.update_or_create(key="nonconformance", defaults={"is_enabled": True})
    manager = make_user("queue-manager", "admin")
    routes = (
        ("clients:client-list", "-name"),
        ("desk:request-list", "-subject"),
        ("nonconformance:list", "-title"),
        ("nonconformance:action-list", "-description"),
        ("service_levels:target-list", "-resolution"),
        ("service_levels:calendar-list", "-name"),
        ("links:system-list", "-kind"),
        ("announcement-list", "-title"),
        ("portal:request-list", "-subject"),
    )
    unexpected = {}
    for route, sort in routes:
        user = clinic_portal if route.startswith("portal:") else manager
        UserPreference.objects.create(
            user=user, key=f"page:{route}", value={"sort": sort, "per_page": 50}
        )
        client.force_login(user)
        expected = {"per_page": "50", "sort": sort} if explicit else {}
        page = client.get(reverse(route), expected)
        assert page.status_code == 200 and page.context["filter_form"].is_valid(), route
        _, fields = next(
            item
            for item in _confirmation_controls(page.content.decode()).forms
            if "vr-grid-toolbar" in item[0].get("class", "").split()
        )
        carried = {k: fields[k] for k in ("per_page", "sort") if k in fields}
        if carried != expected:
            unexpected[route] = carried
    assert unexpected == {}


def test_sort_link_query_replaces_a_repeated_filter_with_its_selected_values(rf):
    from urllib.parse import parse_qs

    from django.template import RequestContext, Template

    from core.templatetags import ui

    request = rf.get("/collections/?site=North&site=South&q=Collection")
    context = RequestContext(request, {"sites": ["East", "West"]})
    # The previous template used Django's querystring; the replacement explicitly
    # documents its change-parameter semantics against that tag.
    name = "grid_query" if hasattr(ui, "grid_query") else "querystring"
    template = Template(
        "{% load ui %}{% " + name + " site=sites sort='name' as link %}{{ link|safe }}"
    )
    query = template.render(context)
    assert parse_qs(query.removeprefix("?")) == {
        "site": ["East", "West"],
        "q": ["Collection"],
        "sort": ["name"],
    }
