"""Confirmation of the verification resolution (e8e185f, compared with 1cadba6) of "preferences
kept in the session". All records are synthetic.

Adopted from the reviewer's file (archived in the review history as the round-1 confirm original)
with these changes, each explained at the test: the missing-slash redirect (C4) and lists whose
valid filters match no rows (C12) assert the documented behaviour.
"""

import json
import subprocess
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from django.db import connection, transaction
from django.urls import reverse

from core import changelog, preferences
from core.models import UserPreference

from .test_preferences_in_session import post

pytestmark = pytest.mark.django_db(transaction=True)
ROOT = Path(__file__).resolve().parents[2]


def _release(day, name):
    return changelog.Entry(
        id=f"{day}-{name}",
        date=date.fromisoformat(day),
        type="changed",
        impact="low",
        title=name.replace("-", " ").title(),
        summary="The collection schedule changed.",
    )


def _visit_catalog(client, monkeypatch, entries):
    monkeypatch.setattr(changelog, "entries", lambda: entries)
    response = client.get(reverse("whats-new"))
    assert response.status_code == 200
    return response.context["new_count"]


def test_a_visit_to_an_old_catalog_keeps_marks_still_inside_the_current_window(
    client, agent, monkeypatch
):
    old = [_release("2026-08-01", f"schedule-{number}") for number in range(400)]
    recent = [_release("2026-11-20", "collection")]
    client.force_login(agent)
    _visit_catalog(client, monkeypatch, recent)
    _visit_catalog(client, monkeypatch, old)
    # With November as the newest seen date, only one distinct entry is within 60 days.
    assert _visit_catalog(client, monkeypatch, recent) == 0


def test_duplicate_ids_in_an_accepted_stored_mark_take_one_place(client, agent, monkeypatch):
    prior, other, current = [
        _release("2026-11-20", name) for name in ("routes", "hours", "collection")
    ]
    value = {"date": "2026-11-20", "ids": [prior.id] * 399 + [other.id]}
    # This shape can be read from an older database. A visit may also repair it as malformed.
    UserPreference.objects.create(user=agent, key=preferences.WHATS_NEW_KEY, value=value)
    client.force_login(agent)
    _visit_catalog(client, monkeypatch, [current])
    rows = UserPreference.objects.get(user=agent, key=preferences.WHATS_NEW_KEY)
    expected = {prior.id, other.id, current.id} if changelog.valid_marker(value) else {current.id}
    assert rows.value["ids"] == sorted(expected, reverse=True)


def test_busy_release_leaves_the_oldest_published_entries_unread(tmp_path):
    old = [_release("2026-11-10", f"schedule-{i}") for i in range(400)]
    # The catalog parser assigns the filename to id and the front matter date to date.
    entry_path = tmp_path / "2026-10-31-collection.md"
    entry_path.write_text(
        "---\ndate: 2026-11-20\ntype: changed\nimpact: low\ntitle: Collection hours\n"
        "---\nCollection hours changed.\n"
    )
    recent = changelog.read_entries(tmp_path)[0]
    entries = old + [recent]
    remaining = changelog.unread(entries, changelog.seen_marker(entries))
    assert len(remaining) <= 1
    assert all(entry.date == old[0].date for entry in remaining)


def test_whats_new_help_explains_when_a_visit_leaves_entries_unread(client, agent, monkeypatch):
    client.force_login(agent)
    entries = [_release("2026-11-20", f"schedule-{i}") for i in range(401)]
    _visit_catalog(client, monkeypatch, entries)
    remaining = _visit_catalog(client, monkeypatch, entries)
    help_text = (ROOT / "docs/pages/core/whats-new.json").read_text()
    assert remaining == 0 or "400" in help_text


def test_two_overlapping_catalogs_keep_a_canonical_set(client, agent, monkeypatch):
    client.force_login(agent)
    entries = [_release("2026-11-20", f"schedule-{number}") for number in range(395)]
    _visit_catalog(client, monkeypatch, entries[100:])
    _visit_catalog(client, monkeypatch, entries[:200])
    marker = UserPreference.objects.get(user=agent, key=preferences.WHATS_NEW_KEY)
    assert marker.value == changelog.seen_marker(entries)
    assert _visit_catalog(client, monkeypatch, entries) == 0


def _notice_for_response(response, delivery):
    """Execute the checked-out Fetch handler, with the status/body of the Django response."""
    program = """
const fs = require("node:fs");
const [script, status, delivery] = process.argv.slice(1);
const messages = [];
globalThis.window = globalThis;
globalThis.document = {
  body: {hasAttribute: () => true, getAttribute: () => "{}"},
  getElementById: () => null,
  dispatchEvent: e => messages.push(e.detail.message)
};
globalThis.fetch = () => delivery === "response-lost" ? Promise.reject(new Error("offline")) :
  Promise.resolve({ok: Number(status) === 204, status: Number(status), redirected: false,
    text: () => delivery === "body-lost" ? Promise.reject(new Error("offline")) :
    Promise.resolve(fs.readFileSync(0, "utf8"))});
globalThis.localStorage = {};
require(script);
vrPreferences.set("nav.collapsed", true).then(() => console.log(JSON.stringify(messages)));
"""
    result = subprocess.run(
        [
            "node",
            "-e",
            program,
            ROOT / "static/js/preferences.js",
            str(response.status_code),
            delivery,
        ],
        input=response.content,
        capture_output=True,
        check=True,
        timeout=10,
    )
    return json.loads(result.stdout)


def test_a_lost_response_does_not_tell_the_user_a_committed_setting_is_page_only(client, agent):
    client.force_login(agent)
    saved = post(client, "nav.collapsed", True)
    assert saved.status_code == 204
    messages = _notice_for_response(saved, "response-lost")
    assert UserPreference.objects.get(user=agent, key="nav.collapsed").value is True
    from .test_preferences_in_session import page_prefs

    assert page_prefs(client, reverse("dashboard"))["nav.collapsed"] is True
    assert len(messages) == 1 and "page only" not in messages[0]


@pytest.mark.parametrize("delivery", ["normal", "body-lost"])
def test_the_notice_for_a_named_server_refusal_matches_the_release_note(client, agent, delivery):
    client.force_login(agent)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        refused = post(client, "nav.collapsed", True)
    assert refused.status_code == 400 and b"READ COMMITTED" in refused.content
    messages = _notice_for_response(refused, delivery)
    assert len(messages) == 1
    if delivery == "body-lost":
        assert "could not be saved" in messages[0] and "as many saved" not in messages[0]
    else:
        note = (ROOT / "changelog.d/2026-11-20-preferences-in-every-tab.md").read_text()
        assert "notice saying why" not in note or "transaction" in messages[0].lower()


def test_a_confirmed_save_emits_no_refusal_notice(client, agent):
    client.force_login(agent)
    response = post(client, "nav.collapsed", True)
    assert response.status_code == 204
    assert _notice_for_response(response, "normal") == []


def shell_marker_for(person):
    return UserPreference.objects.filter(user_id=person.pk, key=preferences.WHATS_NEW_KEY)


@pytest.mark.parametrize(
    "surface,expected_status,has_shell",
    [
        ("dashboard", 200, True),
        ("refused-post", 400, True),
        ("forbidden", 403, True),
        ("missing", 404, True),
        ("server-error", 500, False),
        ("rendered-error", 500, True),
        ("redirect", 302, False),
    ],
)
def test_marker_follows_the_shell_sent_in_the_response(
    client, agent, category, monkeypatch, settings, surface, expected_status, has_shell
):
    from core import views

    from .test_contract import new_request

    settings.DEBUG = False
    client.force_login(agent)
    client.raise_request_exception = False
    if surface == "refused-post":
        ticket = new_request(agent, category)
        response = client.post(reverse("desk:request-entry", args=[ticket.pk]), {})
    elif surface == "forbidden":
        response = client.get(reverse("security-settings"))
    elif surface == "missing":
        response = client.get("/unregistered-confirmation-page/")
    elif surface == "redirect":
        response = client.get(reverse("desk:request-list"), {"view": "former"})
    else:
        render = views.render

        def dashboard_render(*args, **kwargs):
            if surface == "server-error":
                raise RuntimeError("Synthetic rendering failure")
            if surface == "rendered-error":
                kwargs["status"] = 500
            return render(*args, **kwargs)

        monkeypatch.setattr(views, "render", dashboard_render)
        response = client.get(reverse("dashboard"))
    assert response.status_code == expected_status
    assert (b"vr-topbar-btn vr-whats-new" in response.content) == has_shell
    expected = [changelog.seen_marker(changelog.entries())] if has_shell else []
    assert list(shell_marker_for(agent).values_list("value", flat=True)) == expected


@pytest.mark.parametrize("address", ["missing-slash", "head"])
def test_request_without_a_visible_page_does_not_initialize_the_badge(
    client, agent, settings, address
):
    settings.DEBUG = False
    client.force_login(agent)
    url = reverse("clients:client-list")
    if address == "missing-slash":
        assert url.endswith("/")
        response = client.get(url.rstrip("/"))
        assert response.status_code in (301, 308) and response.url == url
        # Adapted (C4, documented, predates): the 404 page drawn before the redirect replaces it
        # records the first mark, the same mark the page redirected to records: nothing the
        # person sees differs.
        assert not response.content
        marked = shell_marker_for(agent).get().value
        assert marked == changelog.seen_marker(changelog.entries())
        return
    response = client.head(url)
    assert response.status_code == 200
    assert not response.content
    assert not shell_marker_for(agent).exists()


@pytest.mark.parametrize("transition", ["staff", "portal", "signout"])
def test_first_shell_mark_stays_with_the_account_that_saw_it(
    client, agent, make_user, clinic_portal, monkeypatch, transition
):
    from django.contrib.auth import logout

    from core import views

    other = clinic_portal if transition == "portal" else make_user("other-observer", "technical")
    client.force_login(agent)
    client.raise_request_exception = False
    render = views.render
    rendered = []

    def change_after_render(request, *args, **kwargs):
        response = render(request, *args, **kwargs)
        assert b"vr-topbar-btn vr-whats-new" in response.content
        rendered.append(request.user.pk)
        if transition == "signout":
            logout(request)
        else:
            request.user = other
        return response

    monkeypatch.setattr(views, "render", change_after_render)
    response = client.get(reverse("dashboard"))
    assert rendered == [agent.pk]
    assert (response.status_code, shell_marker_for(other).exists()) == (200, False)


def test_rolled_back_ticket_render_does_not_initialize_the_badge(
    client, agent, category, monkeypatch, settings
):
    from django.db import connection

    from desk import views

    from .test_contract import new_request

    settings.DEBUG = False
    ticket = new_request(agent, category)
    client.force_login(agent)
    client.raise_request_exception = False
    render = views.render
    rendered = []

    def fail_at_end_of_shell(*args, **kwargs):
        response = render(*args, **kwargs)
        assert b"vr-topbar-btn vr-whats-new" in response.content
        rendered.append(connection.in_atomic_block)
        raise RuntimeError("Synthetic rendering failure inside the ticket snapshot")

    monkeypatch.setattr(views, "render", fail_at_end_of_shell)
    response = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    assert rendered == [True] and response.status_code == 500
    assert b"vr-topbar-btn vr-whats-new" not in response.content
    assert not shell_marker_for(agent).exists()


@pytest.mark.parametrize("with_size", [False, True], ids=["plain-list", "list-choice"])
def test_disappearing_account_does_not_turn_the_rendered_list_into_an_error(
    client, agent, clinic, monkeypatch, with_size
):
    from django.contrib.auth import get_user_model

    from core import lists

    from .test_preferences_in_session import committed

    client.force_login(agent)
    client.raise_request_exception = False
    render = lists.render
    removed = []
    pk = agent.pk

    def remove_after_render(*args, **kwargs):
        response = render(*args, **kwargs)
        assert b"vr-topbar-btn vr-whats-new" in response.content
        removed.append(committed(lambda: get_user_model().objects.filter(pk=pk).delete()[0]))
        return response

    monkeypatch.setattr(lists, "render", remove_after_render)
    response = client.get(reverse("clients:client-list"), {"per_page": 50} if with_size else {})
    assert len(removed) == 1 and removed[0] >= 1
    assert not get_user_model().objects.filter(pk=pk).exists()
    assert not UserPreference.objects.filter(user_id=pk).exists()
    cause = response.exc_info[1].__cause__ if response.exc_info else None
    assert (response.status_code, getattr(cause, "sqlstate", None)) == (200, None)


@pytest.mark.parametrize("context", ["ordinary", "snapshot", "rolled-back-snapshot"])
def test_mark_drawn_without_the_preference_middleware(rf, agent, context):
    from django.template import Context, Template

    from core.concurrency import one_reading

    request = rf.get("/")
    request.user = agent
    template = Template("{% load whats_new %}{% whats_new_button %}")

    def draw():
        assert "vr-topbar-btn vr-whats-new" in template.render(Context({"request": request}))

    if context == "ordinary":
        draw()
    elif context == "snapshot":
        with one_reading():
            draw()
            assert not shell_marker_for(agent).exists()
    else:
        with pytest.raises(RuntimeError, match="Synthetic rollback"), one_reading():
            draw()
            raise RuntimeError("Synthetic rollback")
    expected = (
        [] if context == "rolled-back-snapshot" else [changelog.seen_marker(changelog.entries())]
    )
    assert list(shell_marker_for(agent).values_list("value", flat=True)) == expected


def test_two_first_shells_preserve_the_first_committed_marker(client, agent, monkeypatch):
    from contextvars import ContextVar

    from django.test import Client

    from .test_preferences_in_session import edition
    from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by

    other = Client()
    for browser in (client, other):
        browser.force_login(agent)
    initial = [edition(19)]
    expanded = [edition(20), *initial]
    catalog = ContextVar("visible_catalog", default=expanded)
    monkeypatch.setattr(changelog, "entries", catalog.get)

    def first_page(browser, entries):
        catalog.set(entries)
        response = browser.get(reverse("dashboard"))
        assert response.status_code == 200 and b"vr-topbar-btn vr-whats-new" in response.content

    holder = Held(lambda: first_page(client, initial))
    waiting = Session(lambda: first_page(other, expanded)).start()
    try:
        blocked = waiting.waiting_or_ended() == "waiting" and _blocked_by(
            waiting.pid, holder.session.pid
        )
    finally:
        released = holder.release()
        finished = waiting.join()
    assert (blocked, released, finished) == (True, "done", "done")
    assert list(shell_marker_for(agent).values_list("value", flat=True)) == [
        changelog.seen_marker(initial)
    ]
    assert b'data-vr-unread="1"' in other.get(reverse("dashboard")).content


@pytest.mark.parametrize("method", ["sidebar", "combine", "first-mark"])
def test_capacity_count_and_identity_describe_the_same_state(client, agent, method):
    from types import SimpleNamespace

    from django.db import connection

    from core import changelog, preferences
    from core.models import UserPreference

    from .test_preferences_in_session import committed, fill, post

    rows = UserPreference.objects.filter(user=agent)
    mark = changelog.seen_marker(changelog.entries())
    key, previous, desired = {
        "sidebar": ("nav.collapsed", True, False),
        "combine": ("page:clients:client-list", {"sort": "name"}, {"per_page": 50}),
        "first-mark": (preferences.WHATS_NEW_KEY, mark, mark),
    }[method]
    fill(agent, preferences.MAX_PREFERENCES - 1)
    client.force_login(agent)
    observed = []
    target = rows.filter(key=key)

    def while_counting(execute, sql, params, many, context):
        if observed or "COUNT(" not in sql or '"core_userpreference"' not in sql:
            return execute(sql, params, many, context)

        # The only 300-row state contains this key and permits replacement. Both
        # 299-row states have no such key and permit addition.
        def add_last():
            target.create(user=agent, key=key, value=previous)
            return rows.count()

        observed.append(committed(add_last))
        result = execute(sql, params, many, context)

        def withdraw_last():
            assert target.filter(value=previous).delete()[0] == 1
            return rows.count()

        observed.append(committed(withdraw_last))
        return result

    request = SimpleNamespace(user=agent)
    with connection.execute_wrapper(while_counting):
        try:
            if method == "sidebar":
                status = post(client, key, desired).status_code
            elif method == "combine":
                preferences.save_combined(
                    request, key, lambda stored: {**(stored or {}), **desired}
                )
                status = 204
            else:
                preferences.save_if_absent(request, key, desired)
                status = 204
        except ValueError:
            status = 400
    assert observed == [300, 299]
    assert status == 204
    stored = list(target.values_list("value", flat=True))
    if method == "first-mark":
        # The first page can keep the mark during the brief time it really existed.
        assert stored in ([desired], [])
    else:
        assert stored == [desired]


@pytest.mark.parametrize(
    "isolation", ["READ UNCOMMITTED", "READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"]
)
@pytest.mark.parametrize(
    "operation", ["addition", "replacement", "removal", "combine", "repair-first-mark"]
)
def test_writes_follow_the_transactions_effective_read_isolation(agent, isolation, operation):
    from types import SimpleNamespace

    from django.db import connection, transaction

    from core import changelog, preferences
    from core.models import UserPreference

    from .test_preferences_in_session import committed

    visible = UserPreference.objects.filter(user=agent, key="page:collection")
    visible.create(user=agent, key="page:collection", value={"per_page": 25})
    key = "nav.collapsed"
    previous, desired = True, False
    if operation == "addition":
        previous = None
    elif operation == "removal":
        desired = None
    elif operation == "combine":
        key, previous = "page:clients:client-list", {"sort": "name"}
        desired = {**previous, "per_page": 50}
    elif operation == "repair-first-mark":
        key = preferences.WHATS_NEW_KEY
        previous, desired = {"date": "former"}, changelog.seen_marker(changelog.entries())
    target = UserPreference.objects.filter(user=agent, key=key)
    if previous is not None:
        target.create(user=agent, key=key, value=previous)
    request = SimpleNamespace(user=agent)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
        assert visible.get().value == {"per_page": 25}
        assert committed(lambda: visible.update(value={"per_page": 100})) == 1
        sees_later_commit = visible.get().value == {"per_page": 100}
        try:
            if operation == "combine":
                preferences.save_combined(request, key, lambda stored: {**stored, "per_page": 50})
            elif operation == "repair-first-mark":
                preferences.save_if_absent(request, key, desired)
            else:
                preferences.save(request, key, desired)
            saved = True
        except ValueError as exc:
            assert "READ COMMITTED" in str(exc)
            saved = False
    assert sees_later_commit == (isolation in ("READ UNCOMMITTED", "READ COMMITTED"))
    assert saved == sees_later_commit
    expected = desired if saved else previous
    assert list(target.values_list("value", flat=True)) == ([] if expected is None else [expected])


@pytest.mark.parametrize("method", ["save", "combine"])
@pytest.mark.parametrize("conflicts", [2, 3])
def test_repeated_insert_then_withdrawal_is_bounded_and_does_not_cache_a_failed_save(
    agent, method, conflicts
):
    from types import SimpleNamespace

    from django.db import IntegrityError, connection

    from core import preferences
    from core.models import UserPreference

    from .test_preferences_in_session import committed

    request = SimpleNamespace(user=agent)
    key, desired = "page:clients:client-list", {"per_page": 50}
    prior = {"sort": "name"}
    target = UserPreference.objects.filter(user=agent, key=key)
    attempts = []

    def other_writer(execute, sql, params, many, context):
        if 'INSERT INTO "core_userpreference"' not in sql or len(attempts) >= conflicts:
            return execute(sql, params, many, context)
        attempts.append(committed(lambda: target.create(user=agent, key=key, value=prior).pk))
        try:
            return execute(sql, params, many, context)
        except IntegrityError as exc:
            assert exc.__cause__.diag.constraint_name == "unique_user_preference"
            assert committed(lambda: target.filter(value=prior).delete()[0]) == 1
            raise

    with connection.execute_wrapper(other_writer):
        try:
            if method == "save":
                preferences.save(request, key, desired)
            else:
                preferences.save_combined(
                    request, key, lambda current: {**(current or {}), **desired}
                )
            outcome = "saved"
        except ValueError as exc:
            outcome = str(exc)
    assert len(attempts) == conflicts
    if conflicts == 2:
        assert outcome == "saved"
        assert preferences.for_request(request)[key] == target.get().value == desired
    else:
        assert "try again" in outcome
        assert key not in preferences.for_request(request) and not target.exists()
        preferences.save(request, key, desired)
        assert target.get().value == desired


def test_a_first_page_repairs_an_unreadable_mark_withdrawn_before_repair(client, agent):
    from unittest.mock import patch

    from django.db import connection
    from django.urls import reverse

    from core import changelog, preferences
    from core.models import UserPreference

    from .test_preferences_in_session import committed

    entries = changelog.entries()
    before = entries[1:]
    malformed = {"date": "former"}
    key = preferences.WHATS_NEW_KEY
    target = UserPreference.objects.filter(user=agent, key=key)
    target.create(user=agent, key=key, value=malformed)
    client.force_login(agent)
    removed = []

    def withdraw_unreadable_mark(execute, sql, params, many, context):
        if not removed and 'UPDATE "core_userpreference"' in sql:
            # The comparison ensures no valid mark existed before the removal.
            removed.append(committed(lambda: target.filter(value=malformed).delete()[0]))
        return execute(sql, params, many, context)

    with (
        connection.execute_wrapper(withdraw_unreadable_mark),
        patch.object(changelog, "entries", return_value=before),
    ):
        assert client.get(reverse("dashboard")).status_code == 200
    assert removed == [1]
    after_first_page = list(target.values_list("value", flat=True))
    with patch.object(changelog, "entries", return_value=entries):
        following = client.get(reverse("dashboard"))
    assert following.status_code == 200
    assert (after_first_page, b'data-vr-unread="1"' in following.content) == (
        [changelog.seen_marker(before)],
        True,
    )


def _list_controls(html):
    from html.parser import HTMLParser

    class Controls(HTMLParser):
        def __init__(self):
            super().__init__()
            self.forms, self.links, self.current = [], [], None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "form":
                self.current = {"attrs": attrs, "hidden": [], "selects": []}
                self.forms.append(self.current)
            elif tag == "a" and "href" in attrs:
                self.links.append(attrs)
            elif self.current is not None and tag == "select":
                self.current["selects"].append(attrs.get("name"))
            elif self.current is not None and tag == "input" and attrs.get("type") == "hidden":
                self.current["hidden"].append((attrs["name"], attrs.get("value", "")))

        def handle_endtag(self, tag):
            if tag == "form":
                self.current = None

    parsed = Controls()
    parsed.feed(html)
    return parsed


def _size_form(controls):
    forms = [form for form in controls.forms if "per_page" in form["selects"]]
    assert len(forms) == 1
    return forms[0]


@pytest.mark.parametrize("control", ["next", "sort", "filter"])
def test_list_controls_keep_another_browsers_later_choices(client, agent, control):
    from urllib.parse import urljoin

    from django.test import Client as Browser
    from django.urls import reverse

    from clients.models import Client
    from core.models import UserPreference

    Client.objects.bulk_create([Client(name=f"Collection {n:03}") for n in range(26)])
    client.force_login(agent)
    other = Browser()
    other.force_login(agent)
    url = reverse("clients:client-list")
    row = UserPreference.objects.create(
        user=agent, key="page:clients:client-list", value={"sort": "name", "per_page": 25}
    )
    opened = client.get(url)
    assert opened.status_code == 200 and opened.context["page_obj"].has_next()
    controls = _list_controls(opened.content.decode())
    assert other.get(url, {"sort": "-name", "per_page": 50}).status_code == 200
    row.refresh_from_db()
    assert row.value == {"sort": "-name", "per_page": 50}
    expected = dict(row.value)

    if control == "filter":
        form = next(
            form
            for form in controls.forms
            if "vr-grid-toolbar" in form["attrs"].get("class", "").split()
        )
        assert form["attrs"]["action"] == url
        current = client.get(form["attrs"]["action"], {**dict(form["hidden"]), "q": "Collection"})
        assert current.context["filter_form"].is_valid()
    else:
        if control == "next":
            link = next(link for link in controls.links if link.get("rel") == "next")
        else:
            link = next(link for link in controls.links if "sort=account_number" in link["href"])
            expected["sort"] = "account_number"
        current = client.get(urljoin(url, link["href"]))
    assert current.status_code == 200
    row.refresh_from_db()
    assert row.value == expected


@pytest.mark.parametrize(
    "route,sort", [("clients:client-list", "-name"), ("announcement-list", "-title")]
)
def test_zero_matching_rows_leave_the_stored_list_choice_alone(
    client, make_user, clinic, route, sort
):
    from django.urls import reverse
    from django.utils import timezone

    from core.models import Announcement, UserPreference

    manager = make_user("coordinator", "admin")
    Announcement.objects.create(
        title="Collection", body="Collection times", starts_at=timezone.now(), show_to_staff=True
    )
    client.force_login(manager)
    row = UserPreference.objects.create(user=manager, key=f"page:{route}", value={"per_page": 25})
    response = client.get(
        reverse(route), {"q": "No matching collection here", "per_page": 100, "sort": sort}
    )
    assert response.status_code == 200 and response.context["filter_form"].is_valid()
    assert response.context["page_obj"].paginator.count == 0
    assert b'class="vr-table-footer vr-pagination"' not in response.content
    row.refresh_from_db()
    # Adapted (C12): the documented rule is the list's valid filters, with or without matching
    # rows (docs/architecture.md, core.grids.shown); the wording that promised "rows" is fixed.
    assert row.value == {"per_page": 100, "sort": sort}


def test_nine_populated_lists_carry_explicit_filters_in_the_rows_form(
    client, make_user, clinic, clinic_portal, category
):
    from urllib.parse import urlencode

    from django.urls import reverse
    from django.utils import timezone

    from clients.models import PortalAccess
    from core.models import Announcement, LabModule
    from desk.models import SavedView
    from links.models import ExternalSystem
    from nonconformance.models import CorrectiveAction, Nonconformance
    from service_levels.models import BusinessCalendar, ServiceTarget

    from .test_contract import new_request

    manager = make_user("collection-manager", "admin")
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    LabModule.objects.update_or_create(key="nonconformance", defaults={"is_enabled": True})
    new_request(manager, category, subject="Example collection", client=clinic)
    saved = SavedView.objects.create(
        owner=manager, name="Example collections", filters={"view": "all", "q": "Example"}
    )
    nc = Nonconformance.objects.create(
        title="Example collection",
        description="Collection",
        source="internal",
        severity="minor",
        raised_by=manager,
    )
    CorrectiveAction.objects.create(
        nonconformance=nc,
        description="Example collection",
        assignee=manager,
        due_on=timezone.localdate(),
        created_by=manager,
    )
    BusinessCalendar.objects.create(name="Example calendar")
    ServiceTarget.objects.create(name="Example target", resolution_minutes=60)
    ExternalSystem.objects.create(key="example", name="Example system")
    Announcement.objects.create(
        title="Example collection",
        body="Collection times",
        starts_at=timezone.now(),
        show_to_staff=True,
    )

    routes = [
        ("clients:client-list", "-name", {"active": "yes"}),
        ("desk:request-list", "-subject", {"view": "all", "saved": str(saved.pk)}),
        ("nonconformance:list", "-title", {"severity": "minor"}),
        ("nonconformance:action-list", "-description", {"show": "any"}),
        ("service_levels:target-list", "-resolution", {"active": "yes"}),
        ("service_levels:calendar-list", "-name", {"active": "yes"}),
        ("links:system-list", "-kind", {"active": "yes"}),
        ("announcement-list", "-title", {"audience": "staff"}),
        ("portal:request-list", "-subject", {"status": "active"}),
    ]
    for route, sort, filters in routes:
        client.force_login(clinic_portal if route.startswith("portal:") else manager)
        query = {"q": "Example", **filters, "sort": sort, "per_page": "25", "page": "2"}
        page = client.get(reverse(route), query)
        assert page.status_code == 200 and page.context["filter_form"].is_valid(), route
        assert page.context["page_obj"].paginator.count > 0, route
        params = _size_form(_list_controls(page.content.decode()))["hidden"]
        assert sorted(params) == sorted(
            (key, value) for key, value in query.items() if key not in ("per_page", "page")
        ), route
        resized = client.get(reverse(route) + "?" + urlencode([*params, ("per_page", "50")]))
        assert resized.status_code == 200 and resized.context["page_obj"].number == 1, route
        assert resized.context["page_obj"].paginator.per_page == 50, route
        assert (
            resized.context["page_obj"].paginator.count == page.context["page_obj"].paginator.count
        ), route


def test_bulk_error_redraw_keeps_explicit_saved_view_filters_without_writing_choices(
    client, agent, category
):
    from urllib.parse import urlencode

    from django.urls import reverse

    from core.models import UserPreference
    from desk.models import SavedView

    from .test_contract import new_request

    client.force_login(agent)
    new_request(agent, category, subject="Collection hours")
    saved = SavedView.objects.create(
        owner=agent, name="Collections", filters={"view": "all", "q": "Collection"}
    )
    row = UserPreference.objects.create(
        user=agent, key="page:desk:request-list", value={"per_page": 50, "sort": "-updated_at"}
    )
    query = {
        "view": "all",
        "saved": str(saved.pk),
        "q": "Collection",
        "sort": "subject",
        "per_page": "25",
    }
    page = client.post(reverse("desk:request-list") + "?" + urlencode(query), {})
    assert page.status_code == 200 and page.context["bulk_form"].errors
    assert page.context["sort"] == "subject" and page.context["page_obj"].paginator.per_page == 25
    retained = dict(_size_form(_list_controls(page.content.decode()))["hidden"])
    assert retained == {key: value for key, value in query.items() if key != "per_page"}
    row.refresh_from_db()
    assert row.value == {"per_page": 50, "sort": "-updated_at"}


def test_related_rows_form_keeps_other_pages_and_repeated_filter_values(rf):
    from django.core.paginator import Paginator
    from django.template import Context, Template

    request = rf.get(
        "/collections/?page=3&related_page=2&reviews_page=5&place=east&place=west&q=day&per_page=25&sort=name"
    )
    request.vr_saved_params = {"sort"}
    page = Paginator(list(range(60)), 25).page(2)
    rendered = Template(
        "{% load ui %}"
        '{% pagination page_obj=page page_parameter="related_page" noun="collections" %}'
    ).render(Context({"request": request, "page": page}))
    fields = _size_form(_list_controls(rendered))["hidden"]
    assert fields == [
        ("page", "3"),
        ("reviews_page", "5"),
        ("place", "east"),
        ("place", "west"),
        ("q", "day"),
    ]


def test_reusable_single_page_pager_renders_without_a_request():
    from django.core.paginator import Paginator
    from django.template import Context, Template

    page = Paginator(["Collection"], 25).page(1)
    html = Template('{% load ui %}{% pagination page_obj=page noun="collections" %}').render(
        Context({"page": page})
    )
    assert _size_form(_list_controls(html))["hidden"] == []


def test_driver_configured_repeatable_read_does_not_let_two_writers_take_the_last_place(rf, agent):
    import json
    from threading import Event

    from django.db import connection, transaction
    from psycopg import IsolationLevel

    from core import preferences
    from core.models import UserPreference

    from .test_preferences_in_session import fill
    from .test_review_assignment_lock_order_verify3 import Session, _blocked_by

    fill(agent, preferences.MAX_PREFERENCES - 1)
    ready, inserted, release = Event(), Event(), Event()

    def post_with_driver_isolation(key, value, first):
        # Django applies DATABASES[...]["OPTIONS"]["isolation_level"] this way on
        # each psycopg connection. An autocommit statement and atomic() need not
        # execute with the same isolation. Restore the session's original setting.
        assert connection.get_autocommit()
        driver = connection.connection
        original = driver.isolation_level
        driver.isolation_level = IsolationLevel.REPEATABLE_READ
        try:
            with connection.cursor() as cursor:
                cursor.execute("SHOW transaction_isolation")
                outside = cursor.fetchone()[0]
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SHOW transaction_isolation")
                inside = cursor.fetchone()[0]
            assert (outside, inside) == ("read committed", "repeatable read")

            def pause_after_insert(execute, sql, params, many, context):
                result = execute(sql, params, many, context)
                if first and 'INSERT INTO "core_userpreference"' in sql:
                    inserted.set()
                    ready.set()
                    assert release.wait(timeout=15)
                return result

            request = rf.post(
                "/preferences/",
                json.dumps({"key": key, "value": value}),
                content_type="application/json",
            )
            request.user = agent
            with connection.execute_wrapper(pause_after_insert):
                return preferences.preference(request).status_code
        finally:
            driver.isolation_level = original
            if first:
                ready.set()

    first = Session(lambda: post_with_driver_isolation("nav.collapsed", True, True)).start()
    second = None
    try:
        assert ready.wait(timeout=12)
        second = Session(
            lambda: post_with_driver_isolation(
                "grid:clients.clients:v1", {"order": ["name"], "hidden": []}, False
            )
        ).start()
        state = second.waiting_or_ended()
        if state == "waiting":
            assert inserted.is_set() and _blocked_by(second.pid, first.pid)
        else:
            assert state == "ended"
    finally:
        release.set()
        first.join()
        if second:
            second.join()
    assert (first.outcome, second.outcome) == ("done", "done"), (first.error, second.error)
    assert (first.result, second.result, UserPreference.objects.filter(user=agent).count()) == (
        400,
        400,
        preferences.MAX_PREFERENCES - 1,
    )


def test_already_seen_visit_survives_driver_configured_repeatable_read(client, agent):

    from django.db import connection
    from django.urls import reverse
    from psycopg import IsolationLevel

    from core import changelog, preferences
    from core.models import UserPreference

    from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by

    catalog = changelog.entries()
    added = replace(catalog[0], id=f"{catalog[0].date.isoformat()}-courier-confirmation")
    expanded = changelog.seen_marker([*catalog, added])
    row = UserPreference.objects.create(
        user=agent, key=preferences.WHATS_NEW_KEY, value=changelog.seen_marker(catalog)
    )
    client.force_login(agent)
    client.raise_request_exception = False

    def reopen():
        assert connection.get_autocommit()
        driver = connection.connection
        original = driver.isolation_level
        driver.isolation_level = IsolationLevel.REPEATABLE_READ
        try:
            response = client.get(reverse("whats-new"))
            cause = response.exc_info[1].__cause__ if response.exc_info else None
            return response.status_code, getattr(cause, "sqlstate", None)
        finally:
            driver.isolation_level = original

    holding = Held(lambda: UserPreference.objects.filter(pk=row.pk).update(value=expanded))
    opening = Session(reopen).start()
    try:
        state = opening.waiting_or_ended()
        if state == "waiting":
            assert _blocked_by(opening.pid, holding.session.pid)
        else:
            assert state == "ended"
    finally:
        assert holding.release() == "done"
        opening.join()
    row.refresh_from_db()
    assert row.value == expanded
    assert (opening.outcome, opening.result) == ("done", (200, None)), opening.error


def test_fixed_timeline_pager_with_an_existing_older_page_preference(
    client, agent, category, clinic
):
    from urllib.parse import parse_qs, urljoin, urlsplit

    from django.urls import reverse

    from core.models import UserPreference
    from desk.models import RequestEntry

    from .test_contract import new_request

    ticket = new_request(agent, category, client=clinic)
    RequestEntry.objects.bulk_create(
        [
            RequestEntry(request=ticket, kind="note", author=agent, body=f"Collection note {n}")
            for n in range(25)
        ]
    )
    client.force_login(agent)
    UserPreference.objects.create(
        user=agent, key="page:clients:client-timeline", value={"per_page": 50}
    )
    url = reverse("clients:client-timeline", args=[clinic.pk])
    opened = client.get(url, {"kind": "note"})
    assert opened.status_code == 200 and opened.context["page_obj"].paginator.count == 26
    assert len(opened.context["page_obj"]) == 25
    controls = _list_controls(opened.content.decode())
    next_page = next(link for link in controls.links if link.get("rel") == "next")
    assert parse_qs(urlsplit(next_page["href"]).query)["kind"] == ["note"]
    following = client.get(urljoin(url, next_page["href"]))
    assert following.status_code == 200 and len(following.context["page_obj"]) == 1
    assert not any("per_page" in form["selects"] for form in controls.forms)
    # The adopted test's raw name match also detects the toolbar's hidden input.
    field_in_toolbar = any(("per_page", "50") in form["hidden"] for form in controls.forms)
    assert (b'name="per_page"' in opened.content) == field_in_toolbar


def test_warmed_request_cost_includes_the_isolation_query(client, agent, clinic):
    from django.test.utils import CaptureQueriesContext

    from .test_preferences_in_session import fill

    UserPreference.objects.create(
        user=agent, key=preferences.WHATS_NEW_KEY, value=changelog.seen_marker(changelog.entries())
    )
    UserPreference.objects.create(
        user=agent, key="page:clients:client-list", value={"per_page": 50}
    )
    client.force_login(agent)

    def measure():
        counts = []
        for route, params in (
            ("clients:client-list", {}),
            ("clients:client-list", {"per_page": 50}),
            ("whats-new", {}),
        ):
            with CaptureQueriesContext(connection) as queries:
                page = client.get(reverse(route), params)
            assert page.status_code == 200
            sql = [query["sql"] for query in queries]
            counts.append(
                (
                    len(sql),
                    sum('"core_userpreference"' in s for s in sql),
                    sum("current_setting('transaction_isolation')" in s for s in sql),
                )
            )
        return counts

    measure()
    small = measure()
    fill(agent, preferences.MAX_PREFERENCES - 2)
    assert measure() == small
    print(
        "Total, preference table, isolation query for bare list, explicit unchanged size, visit:",
        small,
    )
