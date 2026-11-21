"""Fourth, narrow confirmation (5653e6a, compared with b33190c) of "preferences kept in the
session", using synthetic accounts, tickets and catalogs.

Adopted from the reviewer's file (archived in the review history as the round-1 confirm4
original) with these changes, each explained at the test: F4, F5 and F7 assert the documented
bounds (docs/architecture.md and the What's new help). F1, F2 and F3, which predate this item, were
expected to fail until the follow-up "a ticket's way back to the queue" was built, and now pass.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection
from django.template import RequestContext, Template
from django.urls import reverse

from core import changelog, preferences
from core.models import AuditEvent, LabModule, UserPreference

from .test_preferences_in_session import committed, edition

pytestmark = pytest.mark.django_db(transaction=True)

ROOT = Path(__file__).resolve().parents[2]


def _catalog(monkeypatch):
    old = replace(edition(18), title="Earlier collection hours")
    current = replace(edition(20), title="Revised collection hours")
    monkeypatch.setattr(changelog, "entries", lambda: [current, old])
    return old, current


@pytest.mark.parametrize("atomic_view", [False, True], ids=["ordinary", "atomic-requests"])
@pytest.mark.parametrize("prior", [False, True], ids=["first-visit", "returning"])
def test_whats_new_can_finish_drawing_after_the_mark_owner_is_removed(
    client, agent, settings, monkeypatch, atomic_view, prior
):
    """A departing account's acknowledgment is treated as unsaved (E9)."""
    from core import views

    old, current = _catalog(monkeypatch)
    account_id = agent.pk
    marks = UserPreference.objects.filter(user_id=account_id, key=preferences.WHATS_NEW_KEY)
    if prior:
        marks.create(user=agent, key=preferences.WHATS_NEW_KEY, value=changelog.seen_marker([old]))
    client.force_login(agent)
    client.raise_request_exception = False
    settings.DEBUG = False
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", atomic_view)
    original = preferences.save_combined
    deletions = []
    drawn = []
    render = views.render

    def observe_render(request, name, context):
        page = render(request, name, context)
        if name == "core/whats_new.html":
            drawn.append(
                (
                    context["new_count"],
                    current.title.encode() in page.content,
                    b'data-vr-unread="1"' in page.content,
                )
            )
        return page

    monkeypatch.setattr(views, "render", observe_render)

    def delete_then_record(*args, **kwargs):
        assert connection.in_atomic_block == atomic_view
        deletions.append(
            committed(lambda: get_user_model().objects.filter(pk=account_id).delete()[0])
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(preferences, "save_combined", delete_then_record)
    response = client.get(reverse("whats-new"))
    assert len(deletions) == 1 and deletions[0] and not marks.exists()
    assert not get_user_model().objects.filter(pk=account_id).exists()
    assert client.get(reverse("whats-new")).status_code == 302
    cause = response.exc_info[1].__cause__ if response.exc_info else None
    if atomic_view:
        # Adapted (F7, documented bound in docs/architecture.md): under ATOMIC_REQUESTS, which
        # this project does not use, the removal shows only when the request commits, after the
        # page was drawn, and that page fails. Nothing is left behind.
        assert drawn == [(int(prior), True, False)]
        assert (response.status_code, getattr(cause, "sqlstate", None)) == (500, "23503")
        return
    if drawn:
        # A request-wide transaction can still fail after rendering completes.
        assert drawn == [(int(prior), True, prior and not atomic_view)]
    assert (response.status_code, getattr(cause, "sqlstate", None)) == (200, None), drawn
    assert drawn
    assert response.context["new_count"] == int(prior)
    rows = response.context["groups"][0]["sections"][0]["entries"]
    assert {r["entry"].id: r["new"] for r in rows} == {old.id: False, current.id: prior}
    assert all(entry.title.encode() in response.content for entry in (old, current))
    assert (b'data-vr-unread="1"' in response.content) == prior


@pytest.mark.parametrize("kind,sqlstate", [("foreign-key", "23503"), ("unique", "23505")])
def test_whats_new_reraises_integrity_failures_unrelated_to_the_visit_account(
    client, agent, settings, monkeypatch, kind, sqlstate
):
    """Inject a real constraint failure in the acknowledgment transaction while its owner exists."""
    _catalog(monkeypatch)
    client.force_login(agent)
    client.raise_request_exception = False
    settings.DEBUG = False
    LabModule.objects.create(key="review-collection")
    original_add = preferences._add
    original_save = preferences.save_combined
    inserted = []
    errors = []

    def other_write(*args, **kwargs):
        added = original_add(*args, **kwargs)
        if not inserted:
            inserted.append(added)
            if kind == "foreign-key":
                assert not get_user_model().objects.filter(pk=-1).exists()
                AuditEvent.objects.create(
                    actor_id=-1, action="review", object_type="review", object_id="1"
                )
            else:
                LabModule.objects.create(key="review-collection")
        return added

    def observe_save(*args, **kwargs):
        try:
            return original_save(*args, **kwargs)
        except IntegrityError as exc:
            errors.append((exc.__cause__.sqlstate, exc.__cause__.diag.table_name))
            raise

    monkeypatch.setattr(preferences, "_add", other_write)
    monkeypatch.setattr(preferences, "save_combined", observe_save)
    response = client.get(reverse("whats-new"))
    table = (AuditEvent if kind == "foreign-key" else LabModule)._meta.db_table
    assert inserted == [True] and errors == [(sqlstate, table)]
    assert get_user_model().objects.filter(pk=agent.pk).exists()
    failure = response.exc_info[1].__cause__ if response.exc_info else None
    assert (response.status_code, getattr(failure, "sqlstate", None)) == (500, sqlstate)


def _tag_value(kind):
    values = {"East wing": 1, "West & north": 2}
    if kind == "queryset":
        return LabModule.objects.order_by("key").values_list("key", flat=True)
    if kind == "generator":
        return (k for k in values)
    if kind == "dict-view":
        return values.keys()
    if kind == "byte-items":
        return [b"East wing", b"West & north"]
    if kind == "scalar-no-iteration":

        class SiteCode:
            __iter__ = None

            def __str__(self):
                return "East wing"

        return SiteCode()
    if kind == "list":
        return list(values)
    if kind == "tuple":
        return tuple(values)
    if kind == "string":
        return "East & west"
    if kind == "empty-iterator":
        return iter(())
    assert kind == "none"
    return None


def _run_tag(request, tag, kind):
    context = RequestContext(request, {"request": request, "choice": _tag_value(kind)})
    return Template("{% load ui %}{% " + tag + " site=choice as link %}{{ link|safe }}").render(
        context
    )


@pytest.mark.parametrize(
    "kind",
    [
        "string",
        "list",
        "tuple",
        "queryset",
        "generator",
        "dict-view",
        "none",
        "empty-iterator",
        "byte-items",
        "scalar-no-iteration",
    ],
)
def test_grid_query_repeats_and_encodes_values_like_djangos_tag(rf, kind):
    LabModule.objects.bulk_create([LabModule(key="review-east"), LabModule(key="review-west")])
    chosen = "/?site=old&site=older&q=one&q=two"
    actual = rf.get(chosen + "&sort=name&per_page=50")
    actual.vr_saved_params = {"sort", "per_page"}
    expected = _run_tag(rf.get(chosen), "querystring", kind)
    assert _run_tag(actual, "grid_query", kind) == expected


def test_grid_query_omitting_last_unset_value_matches_djangos_empty_link(rf):
    request = rf.get("/?sort=name")
    request.vr_saved_params = {"sort"}
    assert _run_tag(request, "grid_query", "none") == _run_tag(rf.get("/"), "querystring", "none")


def _queue_controls(response):
    from .test_review_preferences_in_session_confirm import _list_controls

    assert response.status_code == 200
    return _list_controls(response.content.decode())


@pytest.mark.parametrize("filtered", [False, True], ids=["bare", "chosen-filter"])
def test_first_time_staff_can_step_through_the_default_queue(client, agent, category, filtered):
    from urllib.parse import urlsplit

    from desk.models import ServiceRequest

    from .test_contract import new_request

    tickets = [new_request(agent, category, subject=f"Collection {n}") for n in range(3)]
    ServiceRequest.objects.filter(pk__in=[t.pk for t in tickets]).update(assignee=agent)
    client.force_login(agent)
    listed = client.get(reverse("desk:request-list"), {"q": "Collection"} if filtered else {})
    controls = _queue_controls(listed)
    paths = [
        reverse("desk:request-detail", args=[ticket.pk]) for ticket in listed.context["page_obj"]
    ]
    assert len(paths) == 3
    link = next(a["href"] for a in controls.links if urlsplit(a["href"]).path == paths[1])
    detail = client.get(link)
    assert detail.status_code == 200
    assert [urlsplit(detail.context[k]).path for k in ("previous_url", "next_url")] == [
        paths[0],
        paths[2],
    ]


@pytest.mark.parametrize("explicit_sort", [False, True], ids=["default-sort", "chosen-sort"])
def test_named_view_reopens_in_the_order_shown_when_saved(client, agent, explicit_sort):
    from urllib.parse import urlsplit

    from desk.lists import DEFAULT_SORT

    client.force_login(agent)
    url = reverse("desk:request-list")
    filters = {"view": "all", "q": "Collection"}
    shown = client.get(url, {**filters, **({"sort": DEFAULT_SORT} if explicit_sort else {})})
    assert shown.context["sort"] == DEFAULT_SORT
    save_path = reverse("desk:view-create")
    form = client.get(
        next(
            a["href"] for a in _queue_controls(shown).links if urlsplit(a["href"]).path == save_path
        )
    )
    fields = next(
        dict(f["hidden"]) for f in _queue_controls(form).forms if "list" in dict(f["hidden"])
    )
    saved = client.post(save_path, {**fields, "name": "Collections", "shared_with": "private"})
    assert saved.status_code == 302
    assert client.get(url, {**filters, "sort": "subject"}).context["sort"] == "subject"
    reopened = client.get(saved.url)
    assert reopened.status_code == 200 and reopened.context["q"] == "Collection"
    assert reopened.context["sort"] == DEFAULT_SORT


@pytest.mark.parametrize("action", ["bulk", "cancel-save-view"])
@pytest.mark.parametrize(
    "chosen",
    [
        {},
        {"sort": "subject"},
        {"sort": "subject", "per_page": 50},
    ],
    ids=["inherited", "mixed", "chosen"],
)
def test_queue_actions_only_resend_the_persons_choices(client, agent, category, chosen, action):
    from urllib.parse import urlsplit

    from django.test import Client

    from desk.models import Status

    from .test_contract import new_request

    url = reverse("desk:request-list")
    old = {"sort": "subject", "per_page": 50}
    newer = {"sort": "-subject", "per_page": 100}
    pref = UserPreference.objects.create(user=agent, key="page:desk:request-list", value=old)
    ticket = new_request(agent, category, subject="Collection hours")
    client.force_login(agent)
    other = Client()
    other.force_login(agent)
    filters = {"view": "all", "q": "Collection"}
    queue = client.get(url, {**filters, **chosen})
    assert [item.pk for item in queue.context["page_obj"]] == [ticket.pk]
    controls = _queue_controls(queue)
    if action == "bulk":
        target = next(
            f["attrs"]["action"] for f in controls.forms if f["attrs"].get("id") == "bulk-form"
        )
    else:
        save_path = reverse("desk:view-create")
        create = client.get(
            next(a["href"] for a in controls.links if urlsplit(a["href"]).path == save_path)
        )
        target = create.context["cancel_url"]
        assert any(a["href"] == target for a in _queue_controls(create).links)
    assert other.get(url, {**filters, **newer}).status_code == 200
    pref.refresh_from_db()
    assert pref.value == newer
    if action == "bulk":
        submit = client.post(
            target,
            {
                "action": "status",
                "status": Status.WAITING_CLIENT,
                "requests": [f"{ticket.pk}:{ticket.revision}"],
            },
        )
        assert submit.status_code == 302
        ticket.refresh_from_db()
        assert ticket.status == Status.WAITING_CLIENT
        target = submit.url
    back = client.get(target)
    assert back.status_code == 200 and back.context["q"] == "Collection"
    pref.refresh_from_db()
    expected = {**newer, **chosen}
    assert (back.context["sort"], back.context["per_page"], pref.value) == (
        expected["sort"],
        expected["per_page"],
        expected,
    )


@pytest.mark.parametrize(
    "route", ["desk:request-list", "clients:client-list", "portal:request-list"]
)
@pytest.mark.parametrize(
    "source", ["settings", "filter-only", "filter-and-settings", "explicit-settings"]
)
def test_record_breadcrumb_keeps_only_grid_settings_chosen_on_the_address(
    client, agent, category, clinic, clinic_portal, route, source
):
    from django.test import Client

    from clients.models import PortalAccess
    from desk.models import ServiceRequest

    from .test_contract import new_request

    ticket = new_request(agent, category, client=clinic, subject="Example collection")
    ServiceRequest.objects.filter(pk=ticket.pk).update(assignee=agent)
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    person = clinic_portal if route.startswith("portal:") else agent
    sort = "name" if route.startswith("clients:") else "subject"
    old = {"sort": sort, "per_page": 50}
    newer = {"sort": "-" + sort, "per_page": 100}
    rows = UserPreference.objects.filter(user=person, key=f"page:{route}")
    if source != "filter-only":
        rows.create(user=person, key=f"page:{route}", value=old)
    url = reverse(route)
    chosen = old if source == "explicit-settings" else {}
    params = {} if source == "settings" else {"q": "Example", **chosen}
    client.force_login(person)
    other = Client()
    other.force_login(person)
    listing = client.get(url, params)
    link = next(
        a["href"]
        for a in _queue_controls(listing).links
        if "vr-row-link" in a.get("class", "").split()
    )
    record = client.get(link)
    back = record.context["list_url"]
    assert any(a["href"] == back for a in _queue_controls(record).links)
    assert other.get(url, newer).status_code == 200
    assert rows.get().value == newer
    returned = client.get(back)
    assert returned.status_code == 200 and returned.context["q"] == params.get("q", "")
    expected = {**newer, **chosen}
    assert (returned.context["sort"], returned.context["per_page"], rows.get().value) == (
        expected["sort"],
        expected["per_page"],
        expected,
    )


@pytest.mark.parametrize("other_write", ["none", "unreadable", "readable", "removed"])
def test_shell_repair_tracks_the_committed_row_and_next_badge(client, agent, other_write):

    from .test_review_preferences_in_session_verify import release_badge, release_page

    key = preferences.WHATS_NEW_KEY
    rows = UserPreference.objects.filter(user=agent, key=key)
    previous = {"format": "former"}
    rows.create(user=agent, key=key, value=previous)
    earlier = [edition(18)]
    displayed = [edition(19), *earlier]
    other_mark = changelog.seen_marker(earlier)
    client.force_login(agent)
    changes = []
    results = []

    def interleave_repair(execute, sql, params, many, context):
        if 'UPDATE "core_userpreference"' not in sql:
            return execute(sql, params, many, context)
        if not results and other_write != "none":

            def change():
                target = rows.filter(value=previous)
                if other_write == "removed":
                    return target.delete()[0]
                replacement = other_mark if other_write == "readable" else {"format": "older"}
                return target.update(value=replacement)

            changes.append(committed(change))
        result = execute(sql, params, many, context)
        results.append(context["cursor"].rowcount)
        return result

    with connection.execute_wrapper(interleave_repair):
        first = release_page(client, displayed, "dashboard")
    assert changes == ([] if other_write == "none" else [1])
    assert (
        results == {"none": [1], "unreadable": [0, 1], "readable": [0], "removed": [0]}[other_write]
    )
    assert b"vr-topbar-btn vr-whats-new" in first.content
    assert b"data-vr-unread=" not in first.content  # no readable mark at this page's first read
    kept = other_mark if other_write == "readable" else changelog.seen_marker(displayed)
    assert preferences.for_request(first.wsgi_request)[key] == rows.get().value == kept
    expected = 2 if other_write == "readable" else 1
    assert release_badge(client, [edition(20), *displayed]) == expected


@pytest.mark.parametrize("legacy_number", ["1.25", "1.0000000000000001"])
def test_a_stable_unreadable_jsonb_number_can_be_repaired_and_show_the_next_release(
    client, agent, legacy_number
):
    from django.db.models import JSONField, Value
    from django.db.models.functions import Cast

    from .test_review_preferences_in_session_verify import release_badge, release_page

    key = preferences.WHATS_NEW_KEY
    # This is legal JSONB already in the database. Casting the original JSON preserves
    # the old number exactly through setup. There is no writer competing with the repair.
    raw = '{"former":' + legacy_number + "}"
    row = UserPreference.objects.create(user=agent, key=key, value=Cast(Value(raw), JSONField()))
    client.force_login(agent)
    entries = [edition(19)]
    first = release_page(client, entries, "dashboard")
    row.refresh_from_db()
    held = preferences.for_request(first.wsgi_request).get(key)
    following = release_badge(client, [edition(20), *entries])
    desired = changelog.seen_marker(entries)
    if legacy_number == "1.0000000000000001":
        # Adapted (F4, documented bound in docs/architecture.md): a stored value that does not
        # read back exactly as written, a JSON number beyond double precision that the
        # application never writes, is not repaired; it counts as none on every page.
        assert preferences.clean(key, row.value) is None
        assert (held, following) == (None, 0)
        return
    assert (row.value, held, following) == (desired, desired, 1)


def test_marker_uses_known_publication_age_and_retains_absent_ids_until_they_return(client, agent):
    from datetime import date, timedelta

    from .test_review_preferences_in_session_verify import release_entry, release_page

    newest = date(2026, 11, 20)

    def entry(days, name):
        return release_entry((newest - timedelta(days=days)).isoformat(), name)

    out = entry(61, "archived")
    boundary = entry(60, "collection-hours")
    inside = entry(59, "late-collection")
    elsewhere = entry(61, "courier-route")
    current = entry(0, "turnaround")
    client.force_login(agent)
    release_page(client, [out, boundary, inside, elsewhere])
    row = UserPreference.objects.get(user=agent, key=preferences.WHATS_NEW_KEY)
    release_page(client, [current, out, boundary, inside])
    row.refresh_from_db()
    assert row.value == {
        "date": newest.isoformat(),
        "ids": sorted([current.id, inside.id, elsewhere.id], reverse=True),
    }
    release_page(client, [current, out, boundary, inside, elsewhere])
    row.refresh_from_db()
    assert row.value == changelog.seen_marker([current, inside])


@pytest.mark.parametrize(
    ("stored_date", "visit_date", "visit_wins"),
    [
        ("2026-11-20", "2026-11-19", False),
        ("2026-11-19", "2026-11-20", True),
        ("2026-11-20", "2026-11-20", True),
    ],
)
def test_catalog_union_prioritizes_the_newer_marker_and_the_visit_on_a_tie(
    stored_date, visit_date, visit_wins
):

    from .test_review_preferences_in_session_verify import release_entry

    shared = [release_entry("2026-11-10", f"collection-{i}") for i in range(399)]
    before = release_entry(stored_date, "hours")
    after = release_entry(visit_date, "routes")
    first = changelog.seen_marker([before, *shared])
    visit = [after, *shared]
    combined = changelog.combined_marker(first, changelog.seen_marker(visit), visit)
    winner = after if visit_wins else before
    assert combined == {
        "date": max(stored_date, visit_date),
        "ids": sorted([winner.id, *(item.id for item in shared)], reverse=True),
    }


@pytest.mark.parametrize("archived_count", [398, 399])
def test_operator_help_clearing_rule_for_repeated_visits_to_a_one_entry_catalog(
    client, agent, archived_count
):
    """What's new help promises clearing, with up to 400 changes from the latest 60 days."""
    from .test_review_preferences_in_session_verify import (
        release_badge,
        release_entry,
        release_page,
    )

    archived = [release_entry("2026-08-01", f"courier-{i}") for i in range(archived_count)]
    current = release_entry("2026-11-20", "collection")
    late = release_entry("2026-11-19", "hours")
    client.force_login(agent)
    release_page(client, archived)
    release_page(client, [current])
    # From this point all visits use the same single-entry catalog. Only two distinct entries
    # in any of the three releases fall inside the 60 days ending on November 20.
    assert release_page(client, [late]).context["new_count"] == 1
    release_page(client, [late])
    if archived_count == 399:
        # Adapted (F5, documented bound): the 399 ids another catalog listed, whose dates this one
        # does not know, keep their places ahead of this visit's (docs/architecture.md), and the
        # help now says so next to the 400 places.
        assert release_badge(client, [late]) == 1
        help_page = json.loads((ROOT / "docs/pages/core/whats-new.json").read_text())
        unread = next(s for s in help_page["sections"] if s["title"] == "Unread changes and access")
        assert "only another version of the application lists" in unread["items"][0]["description"]
        return
    assert release_badge(client, [late]) == 0


def _publish_calendar_entry(folder, filename_date, published):
    path = folder / f"{filename_date}-collection.md"
    path.write_text(
        f"---\ndate: {published}\ntype: changed\nimpact: low\ntitle: Collection hours\n"
        "---\nThe collection desk opens earlier on Tuesdays.\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("filename_date", "published", "invalid_field"),
    [
        ("1900-02-29", "2000-02-29", "file name"),
        ("2026-04-31", "2026-04-30", "file name"),
        ("0000-01-01", "2026-04-30", "file name"),
        ("2000-02-29", "1900-02-29", "key 'date'"),
        ("2000-02-29", "2026-11-20", None),
    ],
)
def test_release_check_distinguishes_filename_and_front_matter_calendar_dates(
    tmp_path, monkeypatch, filename_date, published, invalid_field
):
    from io import StringIO

    from django.core.management import call_command
    from django.core.management.base import CommandError

    folder = tmp_path / "entries"
    folder.mkdir()
    path = _publish_calendar_entry(folder, filename_date, published)
    monkeypatch.setattr(changelog, "ENTRIES_DIR", folder)
    monkeypatch.setattr(changelog, "CHANGELOG", tmp_path / "CHANGELOG.md")
    if invalid_field:
        with pytest.raises(CommandError) as raised:
            call_command("changelog", "--check", stdout=StringIO())
        assert path.name in str(raised.value) and invalid_field in str(raised.value)
    else:
        call_command("changelog", stdout=StringIO())
        generated = changelog.CHANGELOG.read_bytes()
        call_command("changelog", "--check", stdout=StringIO())
        assert changelog.CHANGELOG.read_bytes() == generated


@pytest.mark.parametrize("date_text", ["0001-01-01", "0001-03-01", "0001-03-02", "9999-12-31"])
def test_release_date_accepted_by_changelog_check_can_be_displayed(
    client, agent, tmp_path, monkeypatch, date_text
):
    from io import StringIO

    from django.core.management import call_command

    folder = tmp_path / "entries"
    folder.mkdir()
    _publish_calendar_entry(folder, date_text, date_text)
    monkeypatch.setattr(changelog, "ENTRIES_DIR", folder)
    monkeypatch.setattr(changelog, "CHANGELOG", tmp_path / "CHANGELOG.md")
    call_command("changelog", stdout=StringIO())
    call_command("changelog", "--check", stdout=StringIO())
    catalog = changelog.read_entries(folder)
    monkeypatch.setattr(changelog, "entries", lambda: catalog)
    client.force_login(agent)
    client.raise_request_exception = False
    page = client.get(reverse("whats-new"))
    failure = page.exc_info[0].__name__ if page.exc_info else None
    assert (page.status_code, failure) == (200, None)
    assert page.context["groups"][0]["count"] == 1
