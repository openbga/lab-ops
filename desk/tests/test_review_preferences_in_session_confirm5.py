"""Fifth, narrow confirmation (a9b5de4, compared with 5653e6a) of "preferences kept in the
session", using synthetic records and catalogs.

Adopted from the reviewer's file (archived in the review history as the round-1 confirm5
original) with one change, explained at the test: G4 asserts the corrected architecture wording
(an imprecise mark counts as none until opening What's new replaces it).
"""

from dataclasses import replace
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection
from django.urls import reverse

from core import changelog, preferences
from core.models import AuditEvent, UserPreference

from .test_preferences_in_session import committed, edition, fill

pytestmark = pytest.mark.django_db(transaction=True)


def _catalog(monkeypatch):
    earlier = replace(edition(17), title="Earlier collection notice")
    newer = replace(edition(20), title="New collection notice")
    monkeypatch.setattr(changelog, "entries", lambda: [newer, earlier])
    return earlier, newer


@pytest.mark.parametrize(
    ("problem", "atomic_request"),
    [
        ("removed", False),
        ("unrelated", False),
        ("both", False),
        ("removed-after-error", False),
        ("removed", True),
        ("unrelated", True),
        ("both", True),
    ],
)
def test_whats_new_reports_the_reference_that_failed(
    client, agent, settings, monkeypatch, problem, atomic_request
):
    """Use PostgreSQL's reported constraint, actual account state and the delivered HTML."""
    settings.DEBUG = False
    client.raise_request_exception = False
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", atomic_request)
    old, current = _catalog(monkeypatch)
    account = get_user_model().objects.filter(pk=agent.pk)
    marks = UserPreference.objects.filter(user=agent, key=preferences.WHATS_NEW_KEY)
    marker = changelog.seen_marker([old])
    UserPreference.objects.create(user=agent, key=preferences.WHATS_NEW_KEY, value=marker)
    client.force_login(agent)
    save = preferences.save_combined
    observed = []
    visited = []

    def save_with_other_write(request, key, combine):
        visited.append(request.user.pk)
        if problem in {"removed", "both"}:
            assert committed(lambda: account.delete()[0]) >= 1

        def merge(stored):
            if problem != "removed":
                # Queue this distinct deferred constraint before the mark's own INSERT.
                AuditEvent.objects.create(
                    actor_id=-1, action="review", object_type="collection", object_id="1"
                )
            return combine(stored)

        try:
            return save(request, key, merge)
        except IntegrityError as exc:
            observed.append((exc.__cause__.sqlstate, exc.__cause__.diag.table_name))
            if problem == "removed-after-error":
                # The mark owner still existed when PostgreSQL reported the other failure.
                assert committed(lambda: account.delete()[0]) >= 1
            raise

    assert not get_user_model().objects.filter(pk=-1).exists()
    monkeypatch.setattr(preferences, "save_combined", save_with_other_write)
    page = client.get(reverse("whats-new"))
    cause = page.exc_info[1].__cause__ if page.exc_info else None
    if atomic_request:
        assert not observed
        if cause:
            observed.append((cause.sqlstate, cause.diag.table_name))
    table = UserPreference._meta.db_table if problem == "removed" else AuditEvent._meta.db_table
    assert visited == [agent.pk] and observed == [("23503", table)]
    assert account.exists() == (problem == "unrelated")
    assert list(marks.values_list("value", flat=True)) == (
        [marker] if problem == "unrelated" else []
    )
    draws_catalog = not atomic_request and problem == "removed"
    assert (
        page.status_code,
        current.title.encode() in page.content,
        b'data-vr-unread="1"' in page.content,
    ) == ((200, True, True) if draws_catalog else (500, False, False))
    if draws_catalog:
        assert page.context["new_count"] == 1
        rows = page.context["groups"][0]["sections"][0]["entries"]
        assert {r["entry"].id: r["new"] for r in rows} == {old.id: False, current.id: True}
    else:
        assert cause is not None and cause.diag.table_name == table
        assert b"Something went wrong" in page.content


@pytest.mark.parametrize("method,empty_catalog", [("head", False), ("get", True)])
def test_request_commit_after_removal_with_no_acknowledgment_to_write(
    client, agent, monkeypatch, settings, method, empty_catalog
):
    """Separate the deferred foreign key from a request that never writes a marker."""
    settings.DEBUG = False
    client.raise_request_exception = False
    client.force_login(agent)
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)
    key = agent.pk
    left = []

    def read_catalog():
        if not left:
            assert connection.in_atomic_block
            left.append(committed(lambda: get_user_model().objects.filter(pk=key).delete()[0]))
        return [] if empty_catalog else [edition(20)]

    monkeypatch.setattr(changelog, "entries", read_catalog)
    page = getattr(client, method)(reverse("whats-new"))
    assert len(left) == 1 and left[0] >= 1
    assert page.status_code == 200 and page.exc_info is None
    assert page.context["new_count"] == 0
    assert len(page.context["groups"]) == int(not empty_catalog)
    if method == "head":
        assert not page.content
    assert not UserPreference.objects.filter(user_id=key).exists()
    assert client.get(reverse("whats-new")).status_code == 302


def test_capacity_refusal_agrees_with_the_published_whats_new_error_rule(
    client, agent, monkeypatch
):
    """Check the new 'any other failure' sentence against an ordinary quota refusal."""
    _, current = _catalog(monkeypatch)
    fill(agent, preferences.MAX_PREFERENCES)
    client.force_login(agent)
    client.raise_request_exception = False
    save = preferences.save_combined
    refusals = []

    def record_refusal(*args, **kwargs):
        try:
            return save(*args, **kwargs)
        except ValueError as exc:
            refusals.append(str(exc))
            raise

    monkeypatch.setattr(preferences, "save_combined", record_refusal)
    page = client.get(reverse("whats-new"))
    assert refusals == ["Too many saved preferences."]
    assert not UserPreference.objects.filter(user=agent, key=preferences.WHATS_NEW_KEY).exists()
    text = (Path(__file__).resolve().parents[2] / "docs/architecture.md").read_text()
    claim = "any other failure to record it is raised"
    if claim in " ".join(text.split()):
        assert page.exc_info and isinstance(page.exc_info[1], ValueError), (claim, page.status_code)
    else:
        assert page.status_code == 200 and current.title.encode() in page.content


def test_whats_new_shell_retry_surfaces_an_unrelated_foreign_key_failure(
    client, agent, settings, monkeypatch
):
    """The explicit quota refusal and the later template retry share the same HTTP request."""
    settings.DEBUG = False
    client.raise_request_exception = False
    _, current = _catalog(monkeypatch)
    fill(agent, preferences.MAX_PREFERENCES)
    client.force_login(agent)
    original_add = preferences._add
    stages = []

    def add_with_other_reference(user_id, key, value):
        if not stages:
            try:
                return original_add(user_id, key, value)
            except ValueError as error:
                stages.append(str(error))
                raise
        assert (
            committed(
                lambda: UserPreference.objects.filter(user=agent, key="page:list0").delete()[0]
            )
            == 1
        )
        added = original_add(user_id, key, value)
        AuditEvent.objects.create(
            actor_id=-1, action="review", object_type="collection", object_id="2"
        )
        return added

    original_first = preferences.save_if_absent

    def observe_shell_retry(*args, **kwargs):
        try:
            return original_first(*args, **kwargs)
        except IntegrityError as exc:
            stages.append((exc.__cause__.sqlstate, exc.__cause__.diag.table_name))
            raise

    assert not get_user_model().objects.filter(pk=-1).exists()
    monkeypatch.setattr(preferences, "_add", add_with_other_reference)
    monkeypatch.setattr(preferences, "save_if_absent", observe_shell_retry)
    page = client.get(reverse("whats-new"))
    assert stages == ["Too many saved preferences.", ("23503", AuditEvent._meta.db_table)]
    assert get_user_model().objects.filter(pk=agent.pk).exists()
    assert not UserPreference.objects.filter(user=agent, key=preferences.WHATS_NEW_KEY).exists()
    assert (page.status_code, current.title.encode() in page.content) == (500, False)


def _legacy_numeric_preference(user, key, json_text):
    from django.db.models import JSONField, Value
    from django.db.models.functions import Cast

    return UserPreference.objects.create(
        user=user, key=key, value=Cast(Value(json_text), JSONField())
    )


@pytest.mark.parametrize("route", ["dashboard", "whats-new"])
def test_unreadable_mark_counts_as_none_after_visiting_the_named_page(client, agent, route):
    """Check the scope of the architecture's 'on every page' precision exception."""

    from .test_review_preferences_in_session_verify import release_badge, release_page

    key = preferences.WHATS_NEW_KEY
    from django.db.models import JSONField, Value
    from django.db.models.functions import Cast

    raw = '{"former":1.0000000000000001}'
    row = _legacy_numeric_preference(agent, key, raw)
    client.force_login(agent)
    first = release_page(client, [edition(19)], route)
    row.refresh_from_db()
    cached = preferences.for_request(first.wsgi_request).get(key)
    count = release_badge(client, [edition(20), edition(19)])
    if route == "dashboard":
        # Compare as JSONB so Python float rounding cannot conceal a changed legacy row.
        assert UserPreference.objects.filter(
            pk=row.pk, value=Cast(Value(raw), JSONField())
        ).exists()
    if route == "whats-new":
        # Adapted (G4, corrected wording in docs/architecture.md): opening What's new replaces
        # the mark, so this release's visit is recorded and the next release's entry counts.
        desired = changelog.seen_marker([edition(19)])
        assert (row.value, cached, count) == (desired, desired, 1)
        return
    assert (preferences.clean(key, row.value), cached, count) == (None, None, 0)


@pytest.mark.parametrize("magnitude", [1, 310], ids=["rounds-to-finite", "rounds-to-infinity"])
def test_unreadable_mark_outside_float_range_keeps_the_staff_shell_usable(
    client, agent, monkeypatch, settings, magnitude
):
    from math import isfinite

    # A decimal point keeps PostgreSQL's exact JSONB representation fractional. Its
    # expanded numeric text is then decoded into a binary float by Django's JSON reader.
    raw = '{"former":' + ("9" * magnitude) + ".0000000000000001}"
    row = _legacy_numeric_preference(agent, preferences.WHATS_NEW_KEY, raw)
    row.refresh_from_db()
    assert isfinite(row.value["former"]) == (magnitude == 1)
    client.force_login(agent)
    client.raise_request_exception = False
    settings.DEBUG = False
    monkeypatch.setattr(changelog, "entries", lambda: [edition(20)])
    page = client.get(reverse("dashboard"))
    cause = page.exc_info[1].__cause__ if page.exc_info else None
    assert (page.status_code, getattr(cause, "sqlstate", None)) == (200, None)
    assert b"vr-topbar-btn vr-whats-new" in page.content and b"data-vr-unread=" not in page.content
    assert preferences.WHATS_NEW_KEY not in preferences.for_request(page.wsgi_request)


@pytest.mark.parametrize(
    ("raw_page_size", "expected"),
    [("50", 50), ("50.25", 25), ("50.0000000000000001", 25)],
    ids=["valid-size", "invalid-size", "rounds-to-a-valid-size"],
)
def test_lossy_legacy_list_size_counts_as_none_when_the_list_opens(
    client, agent, raw_page_size, expected
):

    route = "desk:request-list"
    _legacy_numeric_preference(agent, f"page:{route}", '{"per_page":' + raw_page_size + "}")
    client.force_login(agent)
    page = client.get(reverse(route))
    assert page.status_code == 200, page.content[:160]
    assert page.context["per_page"] == expected


@pytest.mark.parametrize(
    ("latest", "older", "inside"),
    [
        ("0001-01-01", "0001-01-01", True),
        ("0001-03-01", "0001-01-01", True),
        ("0001-03-02", "0001-01-01", False),
        ("2000-04-28", "2000-02-29", True),
        ("2000-04-29", "2000-02-29", False),
        ("1900-03-01", "1900-01-01", True),
        ("9999-12-31", "9999-11-02", True),
        ("9999-12-31", "9999-11-01", False),
        ("9999-12-31", "0001-01-01", False),
    ],
)
def test_release_mark_merge_and_unread_agree_at_calendar_boundaries(latest, older, inside):

    from .test_review_preferences_in_session_verify import release_entry

    current = release_entry(latest, "current")
    known = release_entry(older, "known")
    elsewhere = release_entry(older, "other-release")
    catalog = [current, known]
    marker = changelog.seen_marker(catalog)
    here = {current.id, known.id} if inside else {current.id}
    assert marker == {"date": latest, "ids": sorted(here, reverse=True)}
    stored = {"date": older, "ids": [known.id, elsewhere.id]}
    assert changelog.combined_marker(stored, marker, catalog) == {
        "date": latest,
        "ids": sorted(here | {elsewhere.id}, reverse=True),
    }
    held = {"date": latest, "ids": [current.id]}
    assert changelog.unread(catalog, held) == ([known] if inside else [])
    if latest != "9999-12-31":
        future = release_entry("9999-12-31", "future")
        assert changelog.unread([future], held) == [future]


@pytest.mark.parametrize("help_section", ["good-to-know", "reference"])
def test_published_help_describes_the_count_that_remains_after_a_full_marker_visit(
    client, agent, help_section
):
    """Compare the literal promises in the served help to the stored count and following badge."""
    from html import unescape
    from re import search

    from django.utils.html import strip_tags

    from .test_review_preferences_in_session_verify import (
        release_badge,
        release_entry,
        release_page,
    )

    baseline = [release_entry("2026-11-20", f"courier-{n}") for n in range(400)]
    late = release_entry("2026-11-19", "collection-hours")
    client.force_login(agent)
    release_page(client, baseline)
    seen = UserPreference.objects.get(user=agent, key=preferences.WHATS_NEW_KEY)
    assert len(seen.value["ids"]) == 400
    page = release_page(client, [late])
    seen.refresh_from_db()
    remembered = len(seen.value["ids"])
    remaining = release_badge(client, [late])
    assert (remembered, remaining, page.context["new_count"]) == (400, 1, 1)
    assert late.id not in seen.value["ids"]
    help_html = " ".join(unescape(strip_tags(page.content.decode())).split())
    documentation = page.context["page_documentation"]
    if help_section == "good-to-know":
        promise = "Opening What's new clears your unread count from the next page you open"
        tip = next(t for t in documentation["good_to_know"] if "[[What's new]]" in t)
        if promise in help_html and tip.endswith("including changes in the collapsed list."):
            assert remaining == 0, tip
    else:
        # The stated exception describes a number of entries actually remembered.
        exception = search(r"while more than (\d+) are remembered", help_html)
        if exception:
            assert remembered > int(exception[1]), exception[0]


def test_sort_headings_on_every_list_carry_only_the_chosen_query(
    client, make_user, clinic, clinic_portal, category
):
    from urllib.parse import parse_qs, urlsplit

    from django.utils import timezone

    from clients.models import PortalAccess
    from core.models import Announcement, LabModule
    from links.models import ExternalSystem
    from nonconformance.models import CorrectiveAction, Nonconformance
    from service_levels.models import BusinessCalendar, ServiceTarget

    from .test_contract import new_request
    from .test_review_preferences_in_session_confirm import _list_controls

    manager = make_user("sort-manager", "admin")
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    LabModule.objects.update_or_create(key="nonconformance", defaults={"is_enabled": True})
    new_request(manager, category, subject="Collection review", client=clinic)
    case = Nonconformance.objects.create(
        title="Collection review",
        description="Collection",
        source="internal",
        severity="minor",
        raised_by=manager,
    )
    CorrectiveAction.objects.create(
        nonconformance=case,
        description="Collection review",
        assignee=manager,
        due_on=timezone.localdate(),
        created_by=manager,
    )
    BusinessCalendar.objects.create(name="Collection calendar")
    ServiceTarget.objects.create(name="Collection target", resolution_minutes=60)
    ExternalSystem.objects.create(key="collection", name="Collection")
    Announcement.objects.create(
        title="Collection review",
        body="Collection hours",
        starts_at=timezone.now(),
        show_to_staff=True,
    )

    lists = (
        ("clients:client-list", "name"),
        ("desk:request-list", "subject"),
        ("nonconformance:list", "title"),
        ("nonconformance:action-list", "description"),
        ("service_levels:target-list", "resolution"),
        ("service_levels:calendar-list", "name"),
        ("links:system-list", "kind"),
        ("announcement-list", "title"),
        ("portal:request-list", "subject"),
    )
    filters = {"query_dict": ["East & west", "South wing"], "notes_page": ["2"]}
    for route, column in lists:
        person = clinic_portal if route.startswith("portal:") else manager
        client.force_login(person)
        for mode in ("saved", "chosen", "both"):
            pref = UserPreference.objects.filter(user=person, key=f"page:{route}")
            if mode == "chosen":
                pref.delete()
            else:
                UserPreference.objects.update_or_create(
                    user=person,
                    key=f"page:{route}",
                    defaults={"value": {"sort": "-" + column, "per_page": 50}},
                )
            chosen = {"view": ["all"]} if route == "desk:request-list" else {}
            if mode != "saved":
                chosen.update(filters)
            if mode == "chosen":
                chosen.update(sort=["-" + column], per_page=["100"])
            page = client.get(reverse(route), {**chosen, "page": 1})
            assert page.status_code == 200 and page.context["filter_form"].is_valid(), (route, mode)
            links = [
                a["href"]
                for a in _list_controls(page.content.decode()).links
                if a.get("class") == "vr-sort"
            ]
            assert links and page.context["sort"] == "-" + column, (route, mode)
            alternatives = []
            for link in links:
                params = parse_qs(urlsplit(link).query)
                (target,) = params.pop("sort")
                alternatives.append(target)
                assert params == {k: v for k, v in chosen.items() if k != "sort"}, (
                    route,
                    mode,
                    link,
                )
            assert column in alternatives, (route, mode, alternatives)


@pytest.mark.parametrize(
    "current,first,next_sort",
    [
        ("name", "", "-name"),
        ("-name", "", "name"),
        ("other", "desc", "-name"),
        ("other", "", "name"),
    ],
)
def test_standalone_sort_heading_builds_each_direction_without_a_request(current, first, next_sort):
    from django.template import Context, Template

    from .test_review_preferences_in_session_confirm import _list_controls

    heading = Template(
        "{% load ui %}"
        '{% sort_header label="Name" asc="name" desc="-name" sort=current first=first %}'
    )
    html = heading.render(Context({"current": current, "first": first}))
    assert [link["href"] for link in _list_controls(html).links] == ["?sort=" + next_sort]


@pytest.mark.parametrize("value", [None, "West & north"], ids=["remove", "replace"])
def test_grid_query_can_change_an_address_parameter_named_query_dict(rf, value):
    from urllib.parse import parse_qs

    from django.template import Context, Template

    request = rf.get("/?query_dict=old&q=collection&per_page=50")
    request.vr_saved_params = {"per_page"}
    template = Template("{% load ui %}{% grid_query query_dict=selection as link %}{{ link|safe }}")
    href = template.render(Context({"request": request, "selection": value}))
    expected = {"q": ["collection"]}
    if value is not None:
        expected["query_dict"] = [value]
    assert parse_qs(href.removeprefix("?")) == expected
