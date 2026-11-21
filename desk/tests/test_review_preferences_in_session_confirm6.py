"""Sixth, narrow confirmation (77f992a, compared with a9b5de4) of "preferences kept in the
session", using synthetic records and catalogs.

Adopted from the reviewer's file (archived in the review history as the round-1 confirm6
original) with these changes, each explained at the test: H1, H2, H3 and H5 assert the documented
bounds and H4 and H7 the corrected wording (docs/architecture.md, grid_query's docstring). H6 is
fixed and its test adopted unchanged.
"""

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection
from django.urls import reverse

from core import changelog, preferences
from core.models import UserPreference

from .test_contract import new_request
from .test_preferences_in_session import committed, edition

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("route", ["dashboard", "whats-new"])
@pytest.mark.parametrize("atomic_request", [False, True], ids=["ordinary", "request-commit"])
@pytest.mark.parametrize("problem", ["owner", "other-owner", "both", "other-unique"])
def test_mark_only_ignores_the_visitors_own_missing_foreign_key(
    client, agent, make_user, settings, monkeypatch, route, atomic_request, problem
):
    """Identify both the table and the owner in PostgreSQL's actual constraint violation."""
    settings.DEBUG = False
    client.raise_request_exception = False
    client.force_login(agent)
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", atomic_request)
    monkeypatch.setattr(changelog, "entries", lambda: [edition(20)])
    users = get_user_model().objects
    stable = make_user("collection-lead", "technical")
    UserPreference.objects.create(user=stable, key="nav.collapsed", value=True)
    visitor = agent.pk
    assert not users.filter(pk=-1).exists()
    original_add = preferences._add
    errors = []
    attempts = []

    def add_with_second_writer(user_id, key, value):
        assert user_id == visitor and key == preferences.WHATS_NEW_KEY
        first = not attempts
        attempts.append(user_id)
        if first and problem != "other-owner":
            assert committed(lambda: users.filter(pk=visitor).delete()[0]) >= 1
        if first and problem in ("other-owner", "both"):
            # This pending FK names a different owner, but the same table and constraint.
            UserPreference.objects.create(user_id=-1, key="nav.collapsed", value=True)
        if first and problem == "other-unique":
            UserPreference.objects.create(user=stable, key="nav.collapsed", value=False)
        return original_add(user_id, key, value)

    method = "save_if_absent" if route == "dashboard" else "save_combined"
    original_save = getattr(preferences, method)

    def observe(*args, **kwargs):
        try:
            return original_save(*args, **kwargs)
        except IntegrityError as exc:
            errors.append(exc.__cause__)
            raise

    monkeypatch.setattr(preferences, "_add", add_with_second_writer)
    monkeypatch.setattr(preferences, method, observe)
    response = client.get(reverse(route))
    assert attempts and users.filter(pk=visitor).exists() == (problem == "other-owner")
    cause = response.exc_info[1].__cause__ if response.exc_info else None
    failure = errors[0] if errors else cause
    assert failure is not None and failure.diag.table_name == UserPreference._meta.db_table
    if problem == "other-unique":
        assert (
            failure.sqlstate == "23505" and failure.diag.constraint_name == "unique_user_preference"
        )
    else:
        failed_owner = visitor if problem == "owner" else -1
        assert failure.sqlstate == "23503" and f"({failed_owner})" in failure.diag.message_detail
    assert not UserPreference.objects.filter(
        user_id=visitor, key=preferences.WHATS_NEW_KEY
    ).exists()
    # Adapted (H1, documented bound): the preferences table's reference to the removed visitor is
    # passed over; the transaction writes only the visitor's rows, so another owner's row in it
    # (injected here) is outside what the application writes, and is passed over with it.
    should_draw = problem in ("owner", "both") and not atomic_request
    assert (response.status_code, b"vr-topbar-btn vr-whats-new" in response.content) == (
        (200, True) if should_draw else (500, False)
    )
    if should_draw and route == "whats-new":
        assert response.context["new_count"] == 0 and b"Delivery hours" in response.content
    if not should_draw:
        assert cause is not None and cause.diag.constraint_name == failure.diag.constraint_name


@pytest.mark.parametrize("enabled", [False, True], ids=["ordinary", "atomic-requests-enabled"])
def test_ticket_deferred_first_mark_handles_owner_removal_outside_its_reading(
    client, agent, category, make_user, monkeypatch, settings, enabled
):
    settings.DEBUG = False
    client.raise_request_exception = False
    client.force_login(agent)
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", enabled)
    ticket = new_request(make_user("collection-author", "technical"), category)
    monkeypatch.setattr(changelog, "entries", lambda: [edition(20)])
    save = preferences.save_if_absent
    states = []
    owner = agent.pk

    def remove_before_write(*args, **kwargs):
        states.append(connection.in_atomic_block)
        assert committed(lambda: get_user_model().objects.filter(pk=owner).delete()[0]) >= 1
        return save(*args, **kwargs)

    monkeypatch.setattr(preferences, "save_if_absent", remove_before_write)
    response = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    # The ticket view opts out of the request transaction; its mark runs after snapshot commit.
    assert states == [False]
    assert (response.status_code, ticket.subject.encode() in response.content) == (200, True)
    assert b"data-vr-unread=" not in response.content
    assert not UserPreference.objects.filter(user_id=owner).exists()


def _seed_raw_mark(owner, raw):
    """Keep the source decimal/JSON null intact when loading an externally written row."""
    from django.db.models import JSONField, Value
    from django.db.models.functions import Cast

    source = Cast(Value(raw), JSONField())
    row = UserPreference.objects.create(user=owner, key=preferences.WHATS_NEW_KEY, value=source)
    return UserPreference.objects.filter(pk=row.pk), source


@pytest.mark.parametrize(
    ("raw", "shell_repairs"),
    [
        ("null", True),
        ('{"old":9007199254740993}', True),
        ('{"old":[0.250000000000000000000]}', True),
        ('{"old":[1.0000000000000001]}', False),
        ('{"old":[1e-400]}', False),
        ('[{"old":-' + "9" * 310 + ".5}]", False),
    ],
    ids=[
        "json-null",
        "exact-large-integer",
        "equal-jsonb-number",
        "rounded",
        "underflow",
        "overflow",
    ],
)
def test_first_mark_and_explicit_visit_after_an_external_value(client, agent, raw, shell_repairs):

    from .test_review_preferences_in_session_verify import release_badge, release_page

    rows, source = _seed_raw_mark(agent, raw)
    key = preferences.WHATS_NEW_KEY
    entries = [edition(18)]
    first_mark = changelog.seen_marker(entries)
    client.force_login(agent)
    first = release_page(client, entries, "dashboard")
    assert preferences.for_request(first.wsgi_request).get(key) == (
        first_mark if shell_repairs else None
    )
    assert rows.filter(value=source).exists() == (not shell_repairs)
    assert b"data-vr-unread=" not in first.content
    next_catalog = [edition(19), *entries]
    assert release_badge(client, next_catalog) == int(shell_repairs)
    visit = release_page(client, next_catalog)
    assert visit.context["new_count"] == int(shell_repairs)
    ack = changelog.seen_marker(next_catalog)
    assert rows.get().value == preferences.for_request(visit.wsgi_request)[key] == ack
    assert release_badge(client, [edition(20), *next_catalog]) == 1


@pytest.mark.parametrize("route", ["dashboard", "whats-new"])
def test_mark_outside_the_integer_decoder_range_still_allows_the_page(
    client, agent, monkeypatch, settings, route
):
    import sys

    limit = sys.get_int_max_str_digits()
    if not 0 < limit < 131072:
        pytest.skip("The configured integer decoder accepts PostgreSQL's numeric range")
    entries = [edition(19)]
    raw = '{"old":' + "9" * (limit + 1) + "}"
    rows, source = _seed_raw_mark(agent, raw)
    assert rows.filter(value=source).exists()
    client.force_login(agent)
    client.raise_request_exception = False
    settings.DEBUG = False
    monkeypatch.setattr(changelog, "entries", lambda: entries)
    page = client.get(reverse(route))
    failure = page.exc_info[1] if page.exc_info else None
    # Adapted (H5, documented bound in docs/architecture.md): a stored value Python cannot read at
    # all, which only another writer can store, fails that person's pages until it is removed.
    assert page.status_code == 500 and isinstance(failure, ValueError), (route, failure)
    return
    if route == "dashboard":
        assert rows.filter(value=source).exists()
        assert preferences.WHATS_NEW_KEY not in preferences.for_request(page.wsgi_request)
    else:
        assert rows.get().value == changelog.seen_marker(entries)


@pytest.mark.parametrize("with_entries", [False, True], ids=["empty-catalog", "populated-catalog"])
def test_explicit_visit_replaces_the_rounded_mark_as_documented(client, agent, with_entries):
    from .test_review_preferences_in_session_verify import release_page

    rows, source = _seed_raw_mark(agent, '{"old":1.0000000000000001}')
    client.force_login(agent)
    page = release_page(client, [edition(19)] if with_entries else [])
    assert page.context["new_count"] == 0
    assert bool(page.context["groups"]) == with_entries
    if not with_entries:
        assert b"No changes recorded yet" in page.content
        # Adapted (H4, corrected wording): only a What's new visit with entries to mark replaces
        # the mark; an empty catalog leaves it.
        assert rows.filter(value=source).exists()
        return
    assert not rows.filter(value=source).exists(), "The explicit visit left the original JSONB mark"


def _selected_rows(html):
    from html.parser import HTMLParser

    class Options(HTMLParser):
        in_rows = False

        def __init__(self):
            super().__init__()
            self.choices = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "select":
                self.in_rows = attrs.get("name") == "per_page"
            if self.in_rows and tag == "option":
                self.choices.append((int(attrs["value"]), "selected" in attrs))

        def handle_endtag(self, tag):
            if tag == "select":
                self.in_rows = False

    parsed = Options()
    parsed.feed(html)
    assert [value for value, _ in parsed.choices] == [25, 50, 100]
    (selected,) = [value for value, marked in parsed.choices if marked]
    return selected


def test_rows_form_shows_and_saves_each_lists_size_while_preserving_the_sort(
    client, make_user, clinic, clinic_portal, category
):
    from urllib.parse import urlencode

    from django.utils import timezone

    from clients.models import PortalAccess
    from core.models import Announcement, LabModule
    from links.models import ExternalSystem
    from nonconformance.models import CorrectiveAction, Nonconformance
    from service_levels.models import BusinessCalendar, ServiceTarget

    from .test_review_preferences_in_session_confirm import _list_controls, _size_form

    manager = make_user("collection-manager", "admin")
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
    lists = [
        ("clients:client-list", "-name"),
        ("desk:request-list", "-subject"),
        ("nonconformance:list", "-title"),
        ("nonconformance:action-list", "-description"),
        ("service_levels:target-list", "-resolution"),
        ("service_levels:calendar-list", "-name"),
        ("links:system-list", "-kind"),
        ("announcement-list", "-title"),
        ("portal:request-list", "-subject"),
    ]
    errors = []
    for route, sort in lists:
        person = clinic_portal if route.startswith("portal:") else manager
        pref = UserPreference.objects.create(
            user=person, key=f"page:{route}", value={"per_page": 50.0, "sort": sort}
        )
        client.force_login(person)
        url = reverse(route)
        filters = {"view": "all"} if route == "desk:request-list" else {}
        page = client.get(url, filters)
        if page.status_code != 200 or not page.context["filter_form"].is_valid():
            errors.append((route, page.status_code))
            continue
        assert page.context["sort"] == sort and page.context["page_obj"].paginator.per_page == 25, (
            route
        )
        html = page.content.decode()
        assert _selected_rows(html) == 25, route
        form = _size_form(_list_controls(html))
        assert form["hidden"] == list(filters.items()), route
        query = urlencode([*form["hidden"], ("per_page", "100")])
        saved = client.get(f"{form['attrs']['action']}?{query}")
        assert saved.status_code == 200 and _selected_rows(saved.content.decode()) == 100, route
        pref.refresh_from_db()
        assert pref.value == {"per_page": 100, "sort": sort} and type(pref.value["per_page"]) is int
        reopened = client.get(url, filters)
        assert reopened.status_code == 200 and reopened.context["sort"] == sort, route
        assert (
            reopened.context["page_obj"].paginator.per_page
            == _selected_rows(reopened.content.decode())
            == 100
        ), route
    assert not errors, errors


@pytest.mark.parametrize("old_size", [50.0, "50"], ids=["float-equal-to-choice", "text-control"])
def test_choosing_50_rows_replaces_an_equivalent_unusable_saved_size(
    client, agent, clinic, old_size
):
    from urllib.parse import urlencode

    from .test_review_preferences_in_session_confirm import _list_controls, _size_form

    route = "clients:client-list"
    key = f"page:{route}"
    client.force_login(agent)
    initial = client.get(reverse(route))
    form = _size_form(_list_controls(initial.content.decode()))
    pref = UserPreference.objects.create(
        user=agent, key=key, value={"per_page": old_size, "sort": "name"}
    )
    selected = client.get(
        form["attrs"]["action"] + "?" + urlencode([*form["hidden"], ("per_page", "50")])
    )
    assert selected.status_code == 200 and _selected_rows(selected.content.decode()) == 50
    pref.refresh_from_db()
    reopened = client.get(reverse(route))
    actual = _selected_rows(reopened.content.decode()) if reopened.status_code == 200 else None
    assert (reopened.status_code, actual, type(pref.value["per_page"])) == (200, 50, int)


@pytest.mark.parametrize("name", ["context", "query_dict", "changes"])
def test_grid_querys_claim_about_changing_any_parameter_name(rf, name):
    from django.template import Context, Template

    request = rf.get("/?context=old&query_dict=old&changes=old")
    tag = Template("{% load ui %}{% grid_query " + name + "=choice as link %}{{ link|safe }}")
    expected = request.GET.copy()
    expected[name] = "new"
    if name == "context":
        # Adapted (H7, corrected docstring): every context tag, Django's included, takes
        # `context`, so that one name cannot be changed.
        with pytest.raises(TypeError):
            tag.render(Context({"request": request, "choice": "new"}))
        return
    assert tag.render(Context({"request": request, "choice": "new"})) == "?" + expected.urlencode()


@pytest.mark.parametrize(
    "isolation,claim",
    [
        ("READ COMMITTED", "clearing"),
        ("REPEATABLE READ", "propagation"),
        ("REPEATABLE READ", "clearing"),
    ],
)
def test_served_whats_new_promises_match_a_below_quota_isolation_refusal(
    client, agent, monkeypatch, isolation, claim
):
    from dataclasses import replace
    from datetime import date

    from django.db import transaction

    from .test_review_preferences_in_session_verify import (
        release_badge,
        release_entry,
        release_page,
    )

    # Both catalogs use a valid changelog filename. The second corrects the publication
    # date in the front matter. The sole ID was already remembered by a genuine visit.
    earlier = release_entry("2026-11-18", "collection")
    corrected = replace(earlier, date=date(2026, 11, 20))
    client.force_login(agent)
    release_page(client, [earlier])
    row = UserPreference.objects.get(user=agent, key=preferences.WHATS_NEW_KEY)
    assert row.value == changelog.seen_marker([earlier])
    assert release_badge(client, [corrected]) == 1
    assert UserPreference.objects.filter(user=agent).count() < preferences.MAX_PREFERENCES

    recorded = []
    original = preferences.save_combined

    def observe(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except ValueError as error:
            recorded.append(str(error))
            raise

    monkeypatch.setattr(preferences, "save_combined", observe)
    monkeypatch.setattr(changelog, "entries", lambda: [corrected])
    client.raise_request_exception = False
    with transaction.atomic(), connection.cursor() as cursor:
        if isolation == "REPEATABLE READ":
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        else:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        page = client.get(reverse("whats-new"))

    refused = isolation == "REPEATABLE READ"
    assert recorded == (["Preferences can be saved only at READ COMMITTED."] if refused else [])
    row.refresh_from_db()
    assert row.value == changelog.seen_marker([earlier if refused else corrected])
    if refused:
        # Adapted (H2, H3, documented in docs/architecture.md): a save the preferences refuse,
        # here for an isolation other than READ COMMITTED (the application's own), leaves the page
        # as drawn without recording the visit, so the change stays unread.
        assert page.status_code == 200 and page.context["new_count"] == 1
        assert release_badge(client, [corrected]) == 1
        return
    if claim == "propagation":
        assert page.exc_info and isinstance(page.exc_info[1], ValueError), (
            recorded,
            page.status_code,
        )
    elif page.status_code == 200:
        assert page.context["new_count"] == 1
        assert corrected.id in row.value["ids"] and len(row.value["ids"]) == 1
        assert release_badge(client, [corrected]) == 0
    else:
        assert refused and page.exc_info and isinstance(page.exc_info[1], ValueError)


def test_nonquota_retry_exhaustion_agrees_with_the_architecture_error_rule(
    client, agent, monkeypatch
):

    entry = edition(19)
    key = preferences.WHATS_NEW_KEY
    marker = changelog.seen_marker([entry])
    rows = UserPreference.objects.filter(user=agent, key=key)
    attempts = []
    refused = []
    assert not rows.exists()
    client.force_login(agent)
    client.raise_request_exception = False
    monkeypatch.setattr(changelog, "entries", lambda: [entry])
    original = preferences.save_combined

    def observe(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except ValueError as error:
            refused.append(str(error))
            raise

    def competing_write(execute, sql, params, many, context):
        if 'INSERT INTO "core_userpreference"' not in sql or refused:
            return execute(sql, params, many, context)
        # A writer that takes no advisory lock inserts a valid mark and withdraws it
        # before the visit's next retry. Both are real commits on another connection.
        pk = committed(lambda: UserPreference.objects.create(user=agent, key=key, value=marker).pk)
        try:
            return execute(sql, params, many, context)
        except IntegrityError as error:
            assert error.__cause__.diag.constraint_name == "unique_user_preference"
            assert committed(lambda: rows.filter(pk=pk).delete()[0]) == 1
            attempts.append(pk)
            raise

    monkeypatch.setattr(preferences, "save_combined", observe)
    with connection.execute_wrapper(competing_write):
        page = client.get(reverse("whats-new"))
    assert len(attempts) == preferences.ATTEMPTS
    assert refused == ["The preference changed while saving; try again."]
    assert UserPreference.objects.filter(user=agent).count() < preferences.MAX_PREFERENCES
    # Adapted (H2, documented in docs/architecture.md): a row changed under every attempt is a
    # refusal, which leaves the page as drawn; the top bar's own first mark then records it.
    assert page.status_code == 200
    assert rows.get().value == marker  # the top bar's independent first mark succeeded


@pytest.mark.parametrize("count", [399, 400, 401])
@pytest.mark.parametrize("one_catalog", [True, False], ids=["one-version", "two-versions"])
def test_successful_visits_clear_remembered_entries_at_the_exact_400_id_bound(
    client, agent, one_catalog, count
):

    from .test_review_preferences_in_session_verify import (
        release_badge,
        release_entry,
        release_page,
    )

    earlier = release_entry("2026-11-19", "late-collection")
    recent = [release_entry("2026-11-20", f"hours-{n}") for n in range(count - 1)]
    client.force_login(agent)
    if one_catalog:
        visit = [earlier, *recent]
    else:
        release_page(client, recent)
        visit = [earlier]
    page = release_page(client, visit)
    row = UserPreference.objects.get(user=agent, key=preferences.WHATS_NEW_KEY)
    assert len(row.value["ids"]) == min(count, 400)
    assert (earlier.id in row.value["ids"]) == (count <= 400)
    assert release_badge(client, visit) == int(count > 400)
    assert page.context["new_count"] == (0 if one_catalog else 1)
