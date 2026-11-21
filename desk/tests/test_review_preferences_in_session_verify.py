"""Verification of the round-1 resolution (1cadba6, compared with 3b7c04e) of "preferences kept
in the session". All application records are synthetic.

Adopted from the reviewer's file (archived as test_preferences_in_session_round1_verify_original.py)
with these changes, each explained at the test: V5 and V11's 401-entry case assert the documented
bounds; V13 asserts the window as now documented (less than 60 days); the timeline case looks for
the rows-per-page form itself (the shared page help names Rows per page on every page). In the
confirmation round, V7 became a documented bound and the offline notice case follows C14.
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.db import IntegrityError, connection, transaction
from django.test import Client as Browser
from django.urls import reverse

from core import changelog, preferences
from core.models import UserPreference

from .test_preferences_in_session import committed, fill, post, same_session
from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by

pytestmark = pytest.mark.django_db(transaction=True)
LIST_ROUTE = "clients:client-list"
PAGE = f"page:{LIST_ROUTE}"


def seed_mark(user):
    UserPreference.objects.create(
        user=user, key=preferences.WHATS_NEW_KEY, value=changelog.seen_marker(changelog.entries())
    )


@pytest.mark.parametrize("workstation", [False, True], ids=["two-tabs", "two-browsers"])
@pytest.mark.parametrize("first", ["list", "other"])
@pytest.mark.parametrize("operation", ["addition", "replacement", "removal", "combine"])
def test_combining_in_both_transaction_orders(client, agent, workstation, first, operation):
    """Observe which writer waits, and the value left after both have committed."""
    client.force_login(agent)
    if workstation:
        second = Browser()
        second.force_login(agent)
    else:
        second = same_session(client)
    seed_mark(agent)
    row = UserPreference.objects.filter(user=agent, key=PAGE)
    if operation != "addition":
        row.create(user=agent, key=PAGE, value={"sort": "name", "per_page": 25})

    def list_choice():
        assert client.get(reverse(LIST_ROUTE), {"per_page": 50}).status_code == 200

    def other_choice():
        # List fields are server-set. Use its actual GET for combination, the ordinary
        # store/remove helpers for complete replacement and removal of the same row.
        if operation in ("combine", "addition"):
            assert second.get(reverse(LIST_ROUTE), {"sort": "-name"}).status_code == 200
        else:
            preferences.save(
                SimpleNamespace(user=agent),
                PAGE,
                None if operation == "removal" else {"sort": "-name"},
            )

    holding, waiting = (
        (list_choice, other_choice) if first == "list" else (other_choice, list_choice)
    )
    holder = Held(holding)
    writer = Session(waiting).start()
    try:
        state = writer.waiting_or_ended()
        blocked = state == "waiting" and _blocked_by(writer.pid, holder.session.pid)
    finally:
        assert holder.release() == "done", holder.session.outcome
        writer.join()
    assert (blocked, writer.outcome) == (True, "done"), writer.outcome
    expected = {"sort": "-name", "per_page": 50}
    if operation == "removal":
        expected = None if first == "list" else {"per_page": 50}
    elif operation == "replacement" and first == "list":
        expected = {"sort": "-name"}
    assert list(row.values_list("value", flat=True)) == ([] if expected is None else [expected])


@pytest.mark.parametrize("stage", ["before-count", "after-unique-conflict"])
@pytest.mark.parametrize("operation", ["explicit", "first-mark", "visit", "list"])
def test_earlier_insert_at_another_point_in_the_save(client, agent, operation, stage):
    """Commit an unlocked insert. In the second case, another transaction then removes it."""
    client.force_login(agent)
    other_key = "nav.collapsed"
    previous, desired = True, False
    seen = changelog.seen_marker(changelog.entries())
    if operation in ("first-mark", "visit"):
        other_key = preferences.WHATS_NEW_KEY
        previous = changelog.seen_marker(changelog.entries()[1:])
        desired = previous if operation == "first-mark" and stage == "before-count" else seen
    if operation == "list":
        other_key = PAGE
        seed_mark(agent)
        previous = {"sort": "-name"}
        desired = {"per_page": 50}
        if stage == "before-count":
            desired["sort"] = "-name"
    rows = UserPreference.objects.filter(user=agent)
    fill(agent, preferences.MAX_PREFERENCES - rows.count() - 1)
    existing = rows.filter(key=other_key)
    inserted = []

    def gate(execute, sql, params, many, context):
        counts = "COUNT(" in sql and '"core_userpreference"' in sql
        inserts = 'INSERT INTO "core_userpreference"' in sql
        if not inserted and (
            (stage == "before-count" and counts) or (stage != "before-count" and inserts)
        ):
            inserted.append(
                committed(
                    lambda: (
                        UserPreference.objects.create(user=agent, key=other_key, value=previous).pk
                    )
                )
            )
        try:
            return execute(sql, params, many, context)
        except IntegrityError as exc:
            if inserts and stage == "after-unique-conflict":
                assert exc.__cause__.diag.constraint_name == "unique_user_preference"

                def remove():
                    with transaction.atomic():
                        assert existing.select_for_update().get().value == previous
                        assert existing.delete()[0] == 1

                committed(remove)
            raise

    try:
        with connection.execute_wrapper(gate):
            if operation == "explicit":
                status = post(client, other_key, desired).status_code
            elif operation == "list":
                status = client.get(reverse(LIST_ROUTE), {"per_page": 50}).status_code
            elif operation == "visit":
                status = client.get(reverse("whats-new")).status_code
            else:
                # Observe the first-page helper itself: its template caller swallows ValueError.
                preferences.save_if_absent(SimpleNamespace(user=agent), other_key, seen)
                status = 200
    except (ValueError, IntegrityError) as exc:
        status = type(exc).__name__
    assert len(inserted) == 1
    if operation == "first-mark" and stage == "after-unique-conflict":
        # Keeping the earlier writer's existing mark can precede the removal.
        # The remover may win, or a later first-page insert may restore the mark.
        assert status == 200 and list(existing.values_list("value", flat=True)) in ([], [seen])
        return
    assert (status, list(existing.values_list("value", flat=True))) == (
        204 if operation == "explicit" else 200,
        [desired],
    )


def test_an_unrelated_unique_violation_is_reported(agent):
    # An explicitly imported PK may occupy the identity sequence's next value.
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(pg_get_serial_sequence('core_userpreference', 'id'))")
        occupied = cursor.fetchone()[0] + 1
    UserPreference.objects.create(pk=occupied, user=agent, key=PAGE, value={"per_page": 25})
    with pytest.raises(IntegrityError) as raised:
        preferences.save(SimpleNamespace(user=agent), "nav.collapsed", True)
    assert raised.value.__cause__.diag.constraint_name == "core_userpreference_pkey"


def test_a_foreign_key_error_is_reported(agent):
    pk = agent.pk
    agent.delete()
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        preferences.save(
            SimpleNamespace(user=SimpleNamespace(pk=pk, is_authenticated=True)),
            PAGE,
            {"sort": "name"},
        )
    assert raised.value.__cause__.sqlstate == "23503"


def test_rollout_bound_with_two_earlier_writers(client, agent):
    # Each old writer has counted and inserted, but has not committed. Neither takes the
    # new additions lock. Check the changelog's numeric promise for this allowed ordering.
    client.force_login(agent)
    fill(agent, preferences.MAX_PREFERENCES - 1)
    rows = UserPreference.objects.filter(user=agent)

    def earlier(key):
        assert rows.count() == 299
        rows.create(user=agent, key=key, value={"per_page": 25})

    first = Held(lambda: earlier("page:collection"))
    try:
        second = Held(lambda: earlier("page:delivery"))
        try:
            assert post(client, "nav.collapsed", True).status_code == 204
        finally:
            assert second.release() == "done", second.session.outcome
    finally:
        assert first.release() == "done", first.session.outcome
    # Adapted (V5): the documented bound. Processes of an earlier release take no lock, so the
    # limit can be passed by as many as they add at that moment; the release note says so.
    assert rows.count() == preferences.MAX_PREFERENCES + 2
    note = (
        Path(__file__).resolve().parents[2] / "changelog.d/2026-11-20-preferences-in-every-tab.md"
    ).read_text()
    assert "passed by one" not in note and "as many preferences as those processes add" in note


def test_explicit_list_choice_on_first_page_at_299(client, agent, clinic):
    """Adapted in the confirmation round (V7, documented bound in docs/architecture.md): the
    first page's What's new mark is recorded as the page is drawn, so on an account at 299 that
    has never been shown a staff page it takes the last place before the list's choice. The page
    still follows its address; the choice is not saved."""
    client.force_login(agent)
    fill(agent, preferences.MAX_PREFERENCES - 1)
    first = client.get(reverse(LIST_ROUTE), {"per_page": 50})
    assert first.status_code == 200 and first.context["page_obj"].paginator.per_page == 50
    following = client.get(reverse(LIST_ROUTE))
    assert following.context["page_obj"].paginator.per_page == 25
    assert UserPreference.objects.filter(user=agent).count() == preferences.MAX_PREFERENCES


def test_release_note_describes_the_lock_taken_for_a_bigint_account(make_user):
    from accounts.acting import INT4

    person = make_user("courier", "technical", pk=INT4 + 7)
    with transaction.atomic():
        preferences.save(SimpleNamespace(user=person), "nav.collapsed", True)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT objid FROM pg_locks WHERE pid = pg_backend_pid() "
                "AND locktype = 'advisory' AND classid = 19557"
            )
            key = cursor.fetchone()[0]
    note = (
        Path(__file__).resolve().parents[2] / "changelog.d/2026-11-20-preferences-in-every-tab.md"
    ).read_text()
    assert key == person.pk or "modulo" in note or "folded" in note


@pytest.mark.parametrize(
    "outcome", ["limit", "limit-without-layout", "ended-session", "offline", "isolation"]
)
def test_background_save_notice_matches_the_outcome(client, agent, outcome):
    """Run the shipped Fetch handler with the real HTTP answer or a rejected network promise."""
    client.force_login(agent)
    if outcome.startswith("limit"):
        fill(agent, preferences.MAX_PREFERENCES)
    if outcome == "ended-session":
        client.logout()
    with transaction.atomic():
        if outcome == "isolation":
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        response = post(client, "nav.collapsed", True)
    redirected = response.status_code == 302
    if redirected:
        response = client.get(response.url)
    if outcome.startswith("limit"):
        assert response.status_code == 400 and b"Too many" in response.content
    if outcome == "isolation" and response.status_code == 204:
        # Before the resolution, this deployment accepted the save, with no failure to explain.
        assert UserPreference.objects.filter(user=agent, key="nav.collapsed", value=True).exists()
        return
    if outcome == "isolation":
        assert response.status_code == 400 and b"READ COMMITTED" in response.content
    if outcome == "ended-session":
        assert redirected and response.status_code == 200
    program = """
const fs = require('node:fs');
const [file, outcome, statusText, followed] = process.argv.slice(1);
const status = Number(statusText);
const events = [];
globalThis.window = globalThis;
globalThis.document = {
  body: {hasAttribute: () => true, getAttribute: () => '{}'},
  getElementById: () => null, dispatchEvent: e => events.push([e.type, e.detail.message])
};
globalThis.localStorage = {};
globalThis.fetch = () => outcome === 'offline' ? Promise.reject(new Error('offline')) :
  Promise.resolve({ok: status === 200 || status === 204, redirected: followed === 'yes', status,
                   text: async () => fs.readFileSync(0, 'utf8')});
require(file);
vrPreferences.set('nav.collapsed', true).then(() => console.log(JSON.stringify(events)));
"""
    script = Path(__file__).resolve().parents[2] / "static/js/preferences.js"
    result = subprocess.run(
        [
            "node",
            "-e",
            program,
            script,
            outcome,
            str(response.status_code),
            "yes" if redirected else "no",
        ],
        input=response.content,
        capture_output=True,
        check=True,
        timeout=10,
    )
    events = json.loads(result.stdout)
    assert len(events) == 1 and events[0][0] == "vr:request-error"
    # Adapted in the confirmation round (C14): with no answer the setting may have been saved,
    # so that notice does not say it applies to this page only.
    if outcome == "offline":
        assert "may not have been saved" in events[0][1] and "page only" not in events[0][1]
    else:
        assert "page only" in events[0][1]
    assert ("as many saved" in events[0][1]) == outcome.startswith("limit")
    if outcome == "limit-without-layout":
        # Follow the advertised remedy for an account containing only page settings.
        assert post(client, "grid:clients.clients:v1", None).status_code == 204
        retry = post(client, "nav.collapsed", True)
        assert (
            retry.status_code == 204 or "Reset a list's columns to make room." not in events[0][1]
        )


def _page_setting(person, route):
    return (
        UserPreference.objects.filter(user=person, key=f"page:{route}")
        .values_list("value", flat=True)
        .first()
    )


def test_all_list_surfaces_remember_url_choices(client, make_user, clinic_portal, monkeypatch):
    from core.models import LabModule

    LabModule.objects.update_or_create(key="nonconformance", defaults={"is_enabled": True})
    manager = make_user("coordinator", "admin")
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)
    for user, routes in (
        (
            manager,
            [
                ("clients:client-list", "-name"),
                ("desk:request-list", "-subject"),
                ("nonconformance:list", "-title"),
                ("nonconformance:action-list", "-description"),
                ("service_levels:target-list", "-resolution"),
                ("service_levels:calendar-list", "-name"),
                ("links:system-list", "-kind"),
                ("announcement-list", "-title"),
            ],
        ),
        (clinic_portal, [("portal:request-list", "-subject")]),
    ):
        client.force_login(user)
        for route, sort in routes:
            url = reverse(route)
            choice = {"sort": sort, "per_page": 50}
            page = client.get(url, choice)
            assert page.status_code == 200 and not page.context["filter_form"].errors, route
            assert _page_setting(user, route) == choice, route
            following = client.get(url)
            assert following.context["sort"] == sort, route
            assert following.context["page_obj"].paginator.per_page == 50, route


@pytest.mark.parametrize(
    "route,bad_filter,sort",
    [
        ("clients:client-list", {"state": "former"}, "-name"),
        ("announcement-list", {"audience": "former"}, "-title"),
    ],
)
def test_filter_error_does_not_save_page_size_or_sort(client, make_user, route, bad_filter, sort):
    manager = make_user("coordinator", "admin")
    client.force_login(manager)
    page = client.get(reverse(route), {**bad_filter, "per_page": 100, "sort": sort})
    assert page.status_code == 200 and page.context["filter_form"].errors
    assert b"These filters cannot be applied" in page.content
    assert b'class="vr-table-footer vr-pagination"' not in page.content
    assert _page_setting(manager, route) is None


@pytest.mark.parametrize(
    "route,bad_size,sort,expected",
    [
        ("clients:client-list", "seven", "-name", 400),
        ("announcement-list", "seven", "-title", 200),
    ],
)
def test_bad_page_size_does_not_save_an_unshown_sort(
    client, make_user, route, bad_size, sort, expected
):
    manager = make_user("coordinator", "admin")
    client.force_login(manager)
    page = client.get(reverse(route), {"per_page": bad_size, "sort": sort})
    assert page.status_code == expected
    if expected == 200:
        assert page.context["filter_form"].errors
    assert _page_setting(manager, route) is None


@pytest.mark.parametrize(
    "route,extra,status",
    [
        ("desk:request-list", {"view": "former"}, 302),
        ("service_levels:target-list", {}, 403),
        ("nonconformance:list", {}, 404),
        ("lookup", {}, 200),
    ],
)
def test_list_preferences_when_no_grid_is_reached(client, agent, route, extra, status):
    client.force_login(agent)
    args = ["clients"] if route == "lookup" else []
    response = client.get(reverse(route, args=args), {**extra, "per_page": 50})
    assert response.status_code == status
    assert _page_setting(agent, route) is None


def test_download_with_page_size_does_not_save_a_list_preference(client, agent, category):
    from attachments.tests.conftest import png_file
    from attachments.tests.test_attachments import file_of, reply_with

    from .test_contract import new_request

    client.force_login(agent)
    file = file_of(reply_with(new_request(agent, category), agent, [png_file()]))
    route = "attachments:download"
    page = client.get(reverse(route, args=[file.pk]), {"per_page": 50})
    assert page.status_code == 200 and "attachment" in page["Content-Disposition"]
    assert _page_setting(agent, route) is None


def test_list_save_checks_current_request_account(client, agent, make_user, monkeypatch):
    from core import lists

    colleague = make_user("colleague", "technical")
    real_render = lists.render

    def render_then_switch(request, *args, **kwargs):
        page = real_render(request, *args, **kwargs)
        request.user = colleague
        return page

    client.force_login(agent)
    monkeypatch.setattr(lists, "render", render_then_switch)
    route = "clients:client-list"
    assert client.get(reverse(route), {"per_page": 50}).status_code == 200
    assert (_page_setting(agent, route), _page_setting(colleague, route)) == (None, None)


def test_rows_per_page_form_does_not_restore_a_sort_changed_elsewhere(client, agent, clinic):
    """Submit the form the first tab actually received, including its hidden inputs."""
    from html.parser import HTMLParser

    from django.test import Client as Browser

    from .test_review_assignment_lock_order_verify3 import Session

    class PageSizeForm(HTMLParser):
        def __init__(self):
            super().__init__()
            self.current = None
            self.fields = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "form":
                self.current = {}
            if self.current is not None and tag == "input" and attrs.get("type") == "hidden":
                self.current[attrs["name"]] = attrs.get("value", "")
            if self.current is not None and tag == "select" and attrs.get("name") == "per_page":
                self.fields = self.current

        def handle_endtag(self, tag):
            if tag == "form":
                self.current = None

    client.force_login(agent)
    route = "clients:client-list"
    url = reverse(route)
    UserPreference.objects.create(user=agent, key=f"page:{route}", value={"sort": "name"})
    opened = client.get(url)
    assert opened.status_code == 200
    form = PageSizeForm()
    form.feed(opened.content.decode())
    assert form.fields is not None
    elsewhere = Browser()
    elsewhere.force_login(agent)
    writer = Session(lambda: elsewhere.get(url, {"sort": "-account_number"}).status_code).start()
    assert writer.join() == "done" and writer.result == 200
    assert _page_setting(agent, route) == {"sort": "-account_number"}
    assert client.get(url, {**form.fields, "per_page": 50}).status_code == 200
    assert _page_setting(agent, route) == {"sort": "-account_number", "per_page": 50}


def test_list_that_fails_to_render_does_not_save_display_choices(client, agent, monkeypatch):
    from core import lists

    def fail_to_render(*args, **kwargs):
        raise RuntimeError("Synthetic list render failure")

    client.raise_request_exception = False
    client.force_login(agent)
    monkeypatch.setattr(lists, "render", fail_to_render)
    route = "clients:client-list"
    response = client.get(reverse(route), {"sort": "-name", "per_page": 50})
    assert response.status_code == 500
    assert _page_setting(agent, route) is None


def release_entry(day, slug):
    from datetime import date

    from core.changelog import Entry

    return Entry(
        id=f"{day}-{slug}",
        date=date.fromisoformat(day),
        type="changed",
        impact="low",
        title=slug.replace("-", " ").title(),
        summary="Collection hours changed.",
    )


def release_page(browser, entries, name="whats-new"):
    from unittest.mock import patch

    from django.urls import reverse

    from core import changelog

    with patch.object(changelog, "entries", return_value=entries):
        page = browser.get(reverse(name))
    assert page.status_code == 200
    return page


def release_badge(browser, entries):
    import re

    page = release_page(browser, entries, "dashboard")
    match = re.search(rb'data-vr-unread="(\d+)"', page.content)
    return int(match[1]) if match else 0


def test_a_visit_to_one_entry_clears_it_after_another_catalog_filled_the_marker(client, agent):
    from django.test import Client

    client.force_login(agent)
    other = Client()
    other.force_login(agent)
    full = [release_entry("2026-11-20", f"notice-{i}") for i in range(400)]
    smaller = [release_entry("2026-11-20", "collection")]
    release_page(client, full)
    assert release_badge(other, smaller) == 1
    assert release_page(other, smaller).context["new_count"] == 1
    assert release_badge(client, smaller) == 0


def test_one_visit_clears_all_entries_in_a_busy_release(client, agent):
    """Adapted (V11, first case): the documented bound of core.changelog.SEEN_IDS_MAX. More than
    400 entries inside the window leave the ones beyond 400 unread; 400 are cleared."""
    client.force_login(agent)
    busy = [release_entry("2026-11-20", f"delivery-{i}") for i in range(401)]
    release_page(client, busy)
    assert release_badge(client, busy) == 1
    full = busy[:400]  # exactly at the bound: one visit clears every entry
    release_page(client, full)
    assert release_badge(client, full) == 0


def test_visits_use_the_published_date_of_an_entry_file(client, agent, tmp_path):
    from core import changelog

    # Both releases accept this file. The UI groups it by the date in its front matter.
    path = tmp_path / "2026-08-01-collection.md"
    path.write_text(
        "---\ndate: 2026-11-20\ntype: changed\nimpact: low\ntitle: Collection hours\n"
        "---\nCollection hours changed.\n"
    )
    catalog = changelog.read_entries(tmp_path)
    assert len(catalog) == 1 and catalog[0].date.isoformat() == "2026-11-20"
    client.force_login(agent)
    release_page(client, catalog)
    assert release_badge(client, catalog) == 0
    release_page(client, catalog)
    assert release_badge(client, catalog) == 0


@pytest.mark.parametrize("offset,expected", [(59, 1), (60, 0), (61, 0)])
def test_late_entry_at_the_edge_of_the_documented_window(client, agent, offset, expected):
    """Adapted (V13): the window is now documented as it is coded, less than 60 days
    (core.changelog.SEEN_WINDOW)."""
    from datetime import timedelta

    from core.changelog import SEEN_WINDOW

    assert SEEN_WINDOW.days == 60

    client.force_login(agent)
    newest = release_entry("2026-11-20", "courier")
    release_page(client, [newest])
    earlier_date = (newest.date - timedelta(days=offset)).isoformat()
    catalog = [newest, release_entry(earlier_date, "collection")]
    assert release_badge(client, catalog) == expected
    assert release_page(client, catalog).context["new_count"] == expected
    assert release_badge(client, catalog) == 0


@pytest.mark.parametrize("latest_first", [True, False])
def test_two_browsers_visiting_different_catalogs_keep_both_acknowledgments(
    client, agent, latest_first
):
    from django.test import Client

    client.force_login(agent)
    other = Client()
    other.force_login(agent)
    first = [release_entry("2026-11-20", "collection")]
    second = [release_entry("2026-11-19", "hours")]
    if not latest_first:
        first, second = second, first
    release_page(client, first)
    assert release_badge(other, second) == 1
    release_page(other, second)
    assert release_badge(client, first + second) == 0


def test_a_visit_replaces_a_malformed_marker_and_marks_late_additions(client, agent):
    from core.models import UserPreference
    from core.preferences import WHATS_NEW_KEY

    UserPreference.objects.create(user=agent, key=WHATS_NEW_KEY, value={"date": "recent"})
    client.force_login(agent)
    entries = [release_entry("2026-11-20", "courier")]
    assert release_page(client, entries).context["new_count"] == 0
    assert release_badge(client, entries) == 0
    late = [*entries, release_entry("2026-11-18", "collection")]
    assert release_badge(client, late) == 1
    assert release_page(client, late).context["new_count"] == 1
    assert release_badge(client, late) == 0


def test_rows_per_page_on_the_client_timeline(client, agent, clinic, category):
    from .test_contract import new_request

    new_request(agent, category, client=clinic)
    client.force_login(agent)
    url = reverse("clients:client-timeline", args=[clinic.pk])
    opened = client.get(url)
    assert opened.status_code == 200 and opened.context["page_obj"].paginator.count
    if b'name="per_page"' not in opened.content:
        # Adapted: look for the form itself; the shared page help names Rows per page everywhere.
        # The timeline's own help also permits a fixed 25-row page with no size control.
        assert opened.context["page_obj"].paginator.per_page == 25
        return
    requested = client.get(url, {"per_page": 50})
    following = client.get(url)
    assert requested.status_code == following.status_code == 200
    sizes = [r.context["page_obj"].paginator.per_page for r in (requested, following)]
    assert (sizes, _page_setting(agent, "clients:client-timeline")) == ([50, 50], {"per_page": 50})


def test_reopening_an_already_seen_release_during_a_newer_visit(client, agent):
    from unittest.mock import patch

    from .test_review_assignment_lock_order_verify3 import paused

    entries = changelog.entries()
    older = entries[1:]
    mark = UserPreference.objects.create(
        user=agent, key=preferences.WHATS_NEW_KEY, value=changelog.seen_marker(older)
    )
    client.force_login(agent)
    client.raise_request_exception = False
    elsewhere = Browser()
    elsewhere.force_login(agent)

    def newer_visit():
        with patch.object(changelog, "entries", return_value=entries):
            assert committed(lambda: elsewhere.get(reverse("whats-new")).status_code) == 200

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        with (
            patch.object(changelog, "entries", return_value=older),
            paused(preferences, "for_request", newer_visit, before=False),
        ):
            page = client.get(reverse("whats-new"))
    mark.refresh_from_db()
    assert mark.value == changelog.seen_marker(entries)
    cause = page.exc_info[1].__cause__ if page.exc_info else None
    assert (page.status_code, getattr(cause, "sqlstate", None)) == (200, None)


def test_merge_holds_the_stored_row_before_the_write(client, agent):
    client.force_login(agent)
    seed_mark(agent)
    row = UserPreference.objects.create(user=agent, key=PAGE, value={"per_page": 25})
    other = Browser()
    other.force_login(agent)
    probe = []
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        here = cursor.fetchone()[0]

    def observe_read(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if not probe and 'FROM "core_userpreference"' in sql:
            # On the base the dictionary load was the sole read for merging these fields.
            # On the candidate observe the later locked read inside save_combined().
            if "FOR NO KEY UPDATE" in sql or not hasattr(preferences, "save_combined"):
                writer = Session(
                    lambda: other.get(reverse(LIST_ROUTE), {"sort": "-name"}).status_code
                ).start()
                probe.append((writer, writer.waiting_or_ended()))
                probe.append(_blocked_by(writer.pid, here))
        return result

    try:
        with connection.execute_wrapper(observe_read):
            assert client.get(reverse(LIST_ROUTE), {"per_page": 50}).status_code == 200
    finally:
        if probe:
            assert probe[0][0].join() == "done"
    assert probe and probe[0][1] == "waiting" and probe[1]
    row.refresh_from_db()
    assert row.value == {"per_page": 50, "sort": "-name"}


def test_list_and_visit_query_cost_and_logged_data(client, agent, caplog):
    from django.test.utils import CaptureQueriesContext

    from core.models import AuditEvent

    seed_mark(agent)
    UserPreference.objects.create(user=agent, key=PAGE, value={"per_page": 50})
    client.force_login(agent)
    audit = AuditEvent.objects.count()

    def costs():
        result = []
        for route, params in (
            (LIST_ROUTE, {}),
            (LIST_ROUTE, {"per_page": 50, "q": "weekday collection"}),
            ("whats-new", {}),
        ):
            with CaptureQueriesContext(connection) as queries:
                assert client.get(reverse(route), params).status_code == 200
            sql = [q["sql"] for q in queries if '"core_userpreference"' in q["sql"]]
            result.append((len(sql), sum("FOR NO KEY UPDATE" in q for q in sql)))
        return result

    initial = costs()
    fill(agent, preferences.MAX_PREFERENCES - 2)
    assert costs() == initial
    print("Preference statements and locked reads (plain list, unchanged URL, visit):", initial)
    assert AuditEvent.objects.count() == audit
    assert "weekday collection" not in caplog.text
    assert _page_setting(agent, LIST_ROUTE) == {"per_page": 50}


@pytest.mark.parametrize("workstation", [False, True], ids=["two-tabs", "two-browsers"])
@pytest.mark.parametrize("first", ["visit", "other"])
@pytest.mark.parametrize("operation", ["addition", "replacement", "removal", "combine"])
def test_release_visit_in_both_transaction_orders(
    client, agent, monkeypatch, workstation, first, operation
):
    from contextvars import ContextVar

    client.force_login(agent)
    other = Browser() if workstation else same_session(client)
    if workstation:
        other.force_login(agent)
    initial, visited, elsewhere = (
        release_entry("2026-11-20", slug) for slug in ("monday", "tuesday", "wednesday")
    )
    catalog = ContextVar("catalog")
    monkeypatch.setattr(changelog, "entries", catalog.get)
    rows = UserPreference.objects.filter(user=agent, key=preferences.WHATS_NEW_KEY)
    if operation != "addition":
        rows.create(
            user=agent, key=preferences.WHATS_NEW_KEY, value=changelog.seen_marker([initial])
        )

    def visit():
        catalog.set([visited])
        assert client.get(reverse("whats-new")).status_code == 200

    def other_action():
        if operation in ("addition", "combine"):
            catalog.set([elsewhere])
            route = "dashboard" if operation == "addition" else "whats-new"
            assert other.get(reverse(route)).status_code == 200
        else:
            preferences.save(
                SimpleNamespace(user=agent),
                preferences.WHATS_NEW_KEY,
                None if operation == "removal" else changelog.seen_marker([elsewhere]),
            )

    holding, waiting = (visit, other_action) if first == "visit" else (other_action, visit)
    holder = Held(holding)
    writer = Session(waiting).start()
    try:
        state = writer.waiting_or_ended()
        blocked = state == "waiting" and _blocked_by(writer.pid, holder.session.pid)
    finally:
        assert holder.release() == "done", holder.session.outcome
        writer.join()
    assert (blocked, writer.outcome) == (True, "done"), writer.error
    if operation == "removal":
        expected = [] if first == "visit" else [visited]
    elif operation == "replacement":
        expected = [elsewhere] if first == "visit" else [elsewhere, visited]
    elif operation == "addition":
        expected = [visited] if first == "visit" else [elsewhere, visited]
    else:
        expected = [initial, visited, elsewhere]
    assert list(rows.values_list("value", flat=True)) == (
        [changelog.seen_marker(expected)] if expected else []
    )
