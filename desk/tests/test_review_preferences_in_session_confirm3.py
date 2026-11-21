"""Third, narrow confirmation (b33190c, compared with 06845dc) of "preferences kept in the
session", using synthetic accounts and catalogs.

Adopted from the reviewer's file (archived in the review history as the round-1 confirm3
original) with these changes, each explained at the test: E6, E7 and E8 assert the documented
bounds, and E10 the corrected architecture wording.
"""

from pathlib import Path

import pytest
from django.db import connection
from django.urls import reverse

from core import changelog, preferences
from core.models import UserPreference

from .test_contract import new_request

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.django_db(transaction=True)


def _seen_marks():
    return UserPreference.objects.filter(key=preferences.WHATS_NEW_KEY)


@pytest.mark.parametrize("transition", ["staff", "portal", "signed-out", "same-account"])
def test_deferred_first_mark_only_for_an_unchanged_drawing_account(
    client, agent, category, make_user, clinic_portal, monkeypatch, transition
):
    from django.contrib.auth import get_user_model, logout

    from desk import views

    ticket = new_request(agent, category)
    client.force_login(agent)
    client.raise_request_exception = False
    other_staff = make_user("collection-staff", "technical")
    original = views.render
    drawn = []

    def finish_render(request, *args, **kwargs):
        page = original(request, *args, **kwargs)
        assert b"vr-topbar-btn vr-whats-new" in page.content and connection.in_atomic_block
        drawn.append(request.user.pk)
        if transition == "signed-out":
            logout(request)
        else:
            request.user = {
                "portal": clinic_portal,
                "staff": other_staff,
                "same-account": get_user_model().objects.get(pk=agent.pk),
            }[transition]
        return page

    monkeypatch.setattr(views, "render", finish_render)
    page = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    assert drawn == [agent.pk]
    expected = [agent.pk] if transition == "same-account" else []
    assert (page.status_code, list(_seen_marks().values_list("user_id", flat=True))) == (
        200,
        expected,
    )


def test_first_mark_rechecks_an_account_disabled_before_the_ticket_reading_commits(
    client, agent, category, monkeypatch
):
    from django.contrib.auth import get_user_model

    from desk import views

    from .test_preferences_in_session import committed

    ticket = new_request(agent, category)
    client.force_login(agent)
    render = views.render
    changed = []

    def deactivate_after_render(*args, **kwargs):
        page = render(*args, **kwargs)
        assert b"vr-topbar-btn vr-whats-new" in page.content and connection.in_atomic_block
        changed.append(
            committed(lambda: get_user_model().objects.filter(pk=agent.pk).update(is_active=False))
        )
        return page

    monkeypatch.setattr(views, "render", deactivate_after_render)
    response = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    assert changed == [1] and response.status_code == 200
    later = client.get(reverse("whats-new"))
    assert later.status_code == 302 and reverse("account_login") in later.url
    # Adapted (E8, documented bound in docs/architecture.md): a mark drawn for an account
    # deactivated meanwhile still records, for that account only; it gives no access.
    assert _seen_marks().filter(user=agent).exists()
    assert not _seen_marks().exclude(user=agent).exists()


@pytest.mark.parametrize(
    "debug,method", [(False, "get"), (True, "get"), (False, "head"), (True, "head")]
)
def test_redirect_first_mark_requires_the_staff_shell_and_a_body_method(
    client, agent, settings, debug, method
):
    settings.DEBUG = debug
    client.force_login(agent)
    url = reverse("lookup", args=["clients"])
    response = getattr(client, method)(url.rstrip("/"))
    assert response.status_code in (301, 308) and response.url == url
    assert not response.content
    assert _seen_marks().filter(user=agent).exists() == (not debug and method == "get")


def test_published_failed_render_bound_on_a_ticket_with_atomic_requests_off(
    client, agent, category, settings, monkeypatch
):
    """Check the literal keep-on-render-failure rule in docs/architecture.md."""
    from desk import views

    settings.DEBUG = False
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", False)
    ticket = new_request(agent, category)
    client.force_login(agent)
    client.raise_request_exception = False
    render = views.render
    drew_top_bar = []

    def fail_after_render(*args, **kwargs):
        page = render(*args, **kwargs)
        drew_top_bar.append(b"vr-topbar-btn vr-whats-new" in page.content)
        raise RuntimeError("Synthetic ticket render failure")

    monkeypatch.setattr(views, "render", fail_after_render)
    response = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    assert drew_top_bar == [True] and response.status_code == 500
    # Adapted (E10): the corrected wording says a page drawn inside its own reading (a ticket
    # page) discards the mark when it fails, as it does.
    assert not _seen_marks().filter(user=agent).exists()
    note = (ROOT / "docs/architecture.md").read_text()
    assert "drawn inside its" in note and "own reading (a ticket page), which discards it" in note


def test_first_visit_to_whats_new_with_account_removed_at_save(
    client, agent, monkeypatch, settings
):
    from django.contrib.auth import get_user_model

    from .test_preferences_in_session import committed

    settings.DEBUG = False
    client.force_login(agent)
    client.raise_request_exception = False
    original = preferences.save_combined
    pk = agent.pk
    removed = []

    def remove_before_mark(*args, **kwargs):
        assert not connection.in_atomic_block
        removed.append(committed(lambda: get_user_model().objects.filter(pk=pk).delete()[0]))
        return original(*args, **kwargs)

    monkeypatch.setattr(preferences, "save_combined", remove_before_mark)
    response = client.get(reverse("whats-new"))
    assert len(removed) == 1 and removed[0] >= 1
    assert not _seen_marks().filter(user_id=pk).exists()
    cause = response.exc_info[1].__cause__ if response.exc_info else None
    assert (response.status_code < 500, getattr(cause, "sqlstate", None)) == (True, None)


@pytest.mark.parametrize("status", [204, 403, 500, 502, 503, 504])
def test_display_notice_matches_confirmed_refused_and_uncertain_responses(status):
    from django.http import HttpResponse

    from .test_review_preferences_in_session_confirm import _notice_for_response

    notices = _notice_for_response(HttpResponse(status=status), "received")
    if status == 204:
        assert not notices
    elif status == 403:
        assert len(notices) == 1 and "could not be saved" in notices[0]
    else:
        assert len(notices) == 1 and "server did not confirm it" in notices[0]


def _queue_with_saved_sort(client, agent, params=None):

    UserPreference.objects.create(
        user=agent, key="page:desk:request-list", value={"sort": "subject", "per_page": 50}
    )
    client.force_login(agent)
    page = client.get(reverse("desk:request-list"), params or {})
    assert page.status_code == 200
    assert (page.context["sort"], page.context["per_page"]) == ("subject", 50)
    from .test_review_preferences_in_session_confirm import _list_controls

    return page, _list_controls(page.content.decode())


@pytest.mark.parametrize("filtered", [False, True], ids=["bare-default-queue", "filtered-queue"])
def test_ticket_detail_steps_through_the_displayed_queue_order(client, agent, category, filtered):
    from urllib.parse import urlsplit

    from desk.models import ServiceRequest

    tickets = [
        new_request(agent, category, subject=f"Collection {letter}") for letter in ("A", "B", "C")
    ]
    ServiceRequest.objects.filter(pk__in=[ticket.pk for ticket in tickets]).update(assignee=agent)
    params = {"view": "all", "q": "Collection"} if filtered else {}
    listed, controls = _queue_with_saved_sort(client, agent, params)
    assert [ticket.pk for ticket in listed.context["page_obj"]] == [ticket.pk for ticket in tickets]
    paths = [reverse("desk:request-detail", args=[ticket.pk]) for ticket in tickets]
    middle = next(a["href"] for a in controls.links if urlsplit(a["href"]).path == paths[1])
    detail = client.get(middle)
    assert detail.status_code == 200
    neighbours = [urlsplit(detail.context[key]).path for key in ("previous_url", "next_url")]
    assert neighbours == [paths[0], paths[2]]


def test_save_view_keeps_the_sort_shown_on_the_ticket_list(client, agent):
    from urllib.parse import urlsplit

    from desk.models import SavedView

    from .test_review_preferences_in_session_confirm import _list_controls

    _, controls = _queue_with_saved_sort(client, agent, {"view": "all", "q": "Collection"})
    path = reverse("desk:view-create")
    create = client.get(
        next(link["href"] for link in controls.links if urlsplit(link["href"]).path == path)
    )
    assert create.status_code == 200
    fields = next(
        dict(form["hidden"])
        for form in _list_controls(create.content.decode()).forms
        if "list" in dict(form["hidden"])
    )
    saved = client.post(path, {**fields, "name": "Collection order", "shared_with": "private"})
    assert saved.status_code == 302
    view = SavedView.objects.get(owner=agent, name="Collection order")
    assert view.filters["q"] == "Collection"
    assert view.sort == "subject"


def test_bulk_validation_redraw_uses_the_queues_saved_sort_and_size(client, agent, category):
    from desk.models import ServiceRequest

    ticket = new_request(agent, category, subject="Collection hours")
    ServiceRequest.objects.filter(pk=ticket.pk).update(assignee=agent)
    _, controls = _queue_with_saved_sort(client, agent)
    action = next(
        form["attrs"]["action"] for form in controls.forms if form["attrs"].get("id") == "bulk-form"
    )
    redraw = client.post(action, {"requests": f"{ticket.pk}:{ticket.revision}"})
    assert redraw.status_code == 200 and redraw.context["bulk_form"].errors
    assert (redraw.context["sort"], redraw.context["per_page"]) == ("subject", 50)


@pytest.mark.parametrize("replacement", [range(2, 4), ("2", "3")], ids=["range", "tuple"])
def test_sort_heading_replacement_matches_querystring_for_iterables(rf, replacement):
    from django.template import RequestContext, Template

    request = rf.get("/collections/?site=1&site=4")
    variables = {"request": request, "sites": replacement}

    def render(tag):
        context = RequestContext(request, variables)
        return Template("{% load ui %}{% " + tag + " site=sites as url %}{{ url|safe }}").render(
            context
        )

    assert render("grid_query") == render("querystring")


@pytest.mark.parametrize("replacements", [0, 4, 5], ids=["stable", "one-spare-pass", "final-pass"])
def test_a_successful_last_mark_repair_survives_the_transaction(rf, agent, replacements):

    from .test_preferences_in_session import committed, edition

    key = preferences.WHATS_NEW_KEY
    desired = changelog.seen_marker([edition(20)])
    row = UserPreference.objects.create(user=agent, key=key, value={"former": 0})
    rows = UserPreference.objects.filter(pk=row.pk)
    request = rf.get("/")
    request.user = agent
    # An unreadable row already uses a place, so this only repairs it.
    attempts = []

    def replace_before_update(execute, sql, params, many, context):
        if 'UPDATE "core_userpreference"' not in sql:
            return execute(sql, params, many, context)
        if len(attempts) < replacements:
            generation = len(attempts) + 1
            assert committed(lambda: rows.update(value={"former": generation})) == 1
        result = execute(sql, params, many, context)
        attempts.append(context["cursor"].rowcount)
        return result

    with connection.execute_wrapper(replace_before_update):
        try:
            preferences.save_if_absent(request, key, desired)
            result = "kept"
        except ValueError:
            result = "refused"
    row.refresh_from_db()
    if 1 in attempts:
        # PostgreSQL matched and updated the still-current row. No other writer
        # changed it after that successful repair.
        assert (result, row.value, preferences.for_request(request).get(key)) == (
            "kept",
            desired,
            desired,
        )
    else:
        # A bounded implementation may stop earlier if every repair really lost.
        assert attempts and row.value == {"former": len(attempts)}
        assert key not in preferences.for_request(request)


def test_continuous_first_mark_conflicts_are_refused_without_a_cached_success(rf, agent):
    from django.db import IntegrityError

    from .test_preferences_in_session import committed, edition

    key = preferences.WHATS_NEW_KEY
    desired = changelog.seen_marker([edition(20)])
    rows = UserPreference.objects.filter(user=agent, key=key)
    request = rf.get("/")
    request.user = agent
    attempts = []

    def clash(execute, sql, params, many, context):
        if 'INSERT INTO "core_userpreference"' not in sql:
            return execute(sql, params, many, context)
        assert len(attempts) < 12, "the first mark never gave up despite continued conflicts"
        pk = committed(
            lambda: UserPreference.objects.create(user=agent, key=key, value={"former": True}).pk
        )
        attempts.append(pk)
        try:
            return execute(sql, params, many, context)
        except IntegrityError as exc:
            assert exc.__cause__.diag.constraint_name == "unique_user_preference"
            assert committed(lambda: rows.filter(pk=pk).delete()[0]) == 1
            raise

    with connection.execute_wrapper(clash), pytest.raises(ValueError, match="try again"):
        preferences.save_if_absent(request, key, desired)
    assert attempts and not rows.exists() and key not in preferences.for_request(request)
    preferences.save_if_absent(request, key, desired)
    assert rows.get().value == desired


def test_first_mark_keeps_a_readable_replacement_that_wins_conditional_repair(rf, agent):

    from .test_preferences_in_session import committed, edition

    key = preferences.WHATS_NEW_KEY
    desired = changelog.seen_marker([edition(19)])
    seen_elsewhere = changelog.seen_marker([edition(20), edition(19)])
    rows = UserPreference.objects.filter(user=agent, key=key)
    rows.create(user=agent, key=key, value={"former": True})
    request = rf.get("/")
    request.user = agent
    changed = []

    def other_visit(execute, sql, params, many, context):
        if not changed and 'UPDATE "core_userpreference"' in sql:
            changed.append(committed(lambda: rows.update(value=seen_elsewhere)))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(other_visit):
        preferences.save_if_absent(request, key, desired)
    assert changed == [1]
    assert rows.get().value == preferences.for_request(request)[key] == seen_elsewhere


@pytest.mark.parametrize("atomic_view", [False, True], ids=["ordinary-view", "atomic-requests"])
def test_removed_account_first_mark_at_the_django_view_commit_boundary(
    client, agent, monkeypatch, settings, atomic_view
):
    from django.contrib.auth import get_user_model

    from .test_preferences_in_session import committed

    settings.DEBUG = False
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", atomic_view)
    assert not connection.in_atomic_block
    client.force_login(agent)
    client.raise_request_exception = False
    original = preferences.save_if_absent
    pk = agent.pk
    observed = []

    def remove_at_mark(*args, **kwargs):
        observed.append(connection.in_atomic_block)
        assert committed(lambda: get_user_model().objects.filter(pk=pk).delete()[0]) >= 1
        return original(*args, **kwargs)

    monkeypatch.setattr(preferences, "save_if_absent", remove_at_mark)
    page = client.get(reverse("dashboard"))
    assert observed == [atomic_view]
    assert not get_user_model().objects.filter(pk=pk).exists()
    assert not UserPreference.objects.filter(user_id=pk).exists()
    cause = page.exc_info[1].__cause__ if page.exc_info else None
    expected = (500, "23503") if atomic_view else (200, None)
    assert (page.status_code, getattr(cause, "sqlstate", None)) == expected


def _write_collection_release(path, published):
    path.write_text(
        f"---\ndate: {published}\ntype: changed\nimpact: low\ntitle: Collection hours\n"
        "---\nThe collection desk opens earlier on Tuesdays.\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("file_day", "on_both_catalogs"),
    [("2026-08-01", False), ("2026-08-01", True), ("2026-11-19", False)],
)
def test_current_publication_stays_acknowledged_across_catalogs(
    client, agent, tmp_path, file_day, on_both_catalogs
):
    from django.test import Client

    from .test_review_preferences_in_session_verify import (
        release_badge,
        release_entry,
        release_page,
    )

    _write_collection_release(tmp_path / f"{file_day}-collection.md", "2026-11-19")
    collection = changelog.read_entries(tmp_path)
    courier = [release_entry("2026-11-20", "courier")]
    client.force_login(agent)
    other = Client()
    other.force_login(agent)
    release_page(client, collection)
    assert release_badge(other, collection) == 0
    release_page(other, collection + courier if on_both_catalogs else courier)
    assert release_badge(client, collection + courier) == 0


@pytest.mark.parametrize("old_entries_still_listed", [True, False])
def test_expired_publications_leave_room_for_seen_changes(client, agent, old_entries_still_listed):
    from dataclasses import replace
    from datetime import date

    from django.test import Client

    from .test_review_preferences_in_session_verify import (
        release_badge,
        release_entry,
        release_page,
    )

    archived = [
        replace(release_entry("2026-11-10", f"route-{i}"), date=date(2026, 8, 1))
        for i in range(399)
    ]
    newest = [release_entry("2026-11-20", "collection")]
    recent = [release_entry("2026-11-19", "courier")]
    client.force_login(agent)
    other = Client()
    other.force_login(agent)
    release_page(client, archived)
    release_page(client, newest + archived if old_entries_still_listed else newest)
    release_page(other, recent)
    # Adapted (E6, documented bound): ids of entries this process does not know are kept, up to
    # 400, because only a known entry's published date can show it expired.
    assert release_badge(client, newest + recent) == (0 if old_entries_still_listed else 1)


@pytest.mark.parametrize("older_count", [398, 399])
def test_overflow_preserves_more_recent_publications_across_catalogs(client, agent, older_count):
    from django.test import Client

    from .test_review_preferences_in_session_verify import (
        release_badge,
        release_entry,
        release_page,
    )

    older = [release_entry("2026-11-10", f"collection-{i}") for i in range(older_count)]
    latest = release_entry("2026-11-20", "sample-storage")
    recent = release_entry("2026-11-19", "courier-collection")
    client.force_login(agent)
    other = Client()
    other.force_login(agent)
    release_page(client, [latest, *older])
    assert release_page(other, [recent, *older]).context["new_count"] == 1
    # Adapted (E7, documented bound of the 400 ids): past 400 entries inside the window, the
    # newer mark's ids come first, so one entry of the other visit can stay unread.
    assert release_badge(client, [recent]) == (1 if len(older) >= 399 else 0)


@pytest.mark.parametrize(
    ("day", "slug", "allowed"),
    [
        ("2026-11-20", "a" * 80, True),
        ("2026-11-20", "a" * 78 + "-b", True),
        ("2026-11-20", "a" * 79 + "-b", False),
        ("2026-11-20", "two--words", False),
        ("2024-02-29", "collection", True),
        ("2026-02-29", "collection", False),
    ],
)
def test_release_command_checks_filename_grammar(tmp_path, monkeypatch, day, slug, allowed):
    from dataclasses import replace
    from io import StringIO

    from django.core.management import call_command
    from django.core.management.base import CommandError

    folder = tmp_path / "entries"
    folder.mkdir()
    source = folder / "2026-11-20-collection.md"
    target = folder / f"{day}-{slug}.md"
    _write_collection_release(source, "2026-11-20")
    entry = replace(changelog.parse_entry(source), id=target.stem)
    source.rename(target)
    generated = tmp_path / "CHANGELOG.md"
    generated.write_text(changelog.render_markdown([entry]), encoding="utf-8")
    monkeypatch.setattr(changelog, "ENTRIES_DIR", folder)
    monkeypatch.setattr(changelog, "CHANGELOG", generated)
    if allowed:
        call_command("changelog", "--check", stdout=StringIO())
    else:
        with pytest.raises(CommandError):
            call_command("changelog", "--check", stdout=StringIO())
