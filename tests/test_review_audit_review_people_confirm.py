"""Confirmation of the People reading and the list preferences written with a response."""

import csv
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from django.db import DatabaseError, connections, transaction
from django.http import HttpResponse
from django.test import Client as WebClient
from django.urls import reverse
from django.utils import timezone

from accounts.models import SignInEvent
from audit_review.models import WorkspaceUse
from audit_review.tests.conftest import agent as agent
from audit_review.tests.conftest import category as category
from audit_review.tests.conftest import clinic as clinic
from audit_review.tests.conftest import make_ticket as make_ticket
from audit_review.tests.conftest import make_user as make_user
from audit_review.tests.conftest import reviewer as reviewer
from core import preferences
from core.models import AccessRecord, AuditEvent, LabModule, UserPreference
from service_levels.models import BusinessCalendar

PEOPLE = "audit_review:people"
EXPORT = "audit_review:people-export"


def query_of(address):
    return {key: values[-1] for key, values in parse_qs(urlsplit(address).query).items()}


def csv_rows(reply):
    assert reply.status_code == 200
    return list(csv.DictReader(io.StringIO(reply.content.decode("utf-8-sig"))))


def new_change(subject):
    return AuditEvent.objects.create(
        actor_id=subject,
        action="account.updated",
        object_type="accounts.user",
        object_id=str(subject),
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("fast_event", [False, True], ids=["ordinary-clock", "fast-clock"])
@pytest.mark.parametrize(
    "phase", ["before_snapshot", "after_snapshot", "before_clock", "after_marks", "after_response"]
)
def test_changes_link_and_csv_keep_snapshot_with_an_ahead_clock(
    client, reviewer, agent, monkeypatch, fast_event, phase
):
    from audit_review import views

    client.force_login(reviewer)
    new_change(agent.pk)
    if fast_event:
        # An unrelated account's earlier, committed change came from a fast application server.
        ahead = timezone.now() + timedelta(hours=1)
        with monkeypatch.context() as patch:
            patch.setattr(timezone, "now", lambda: ahead)
            new_change(reviewer.pk)

    def writer():
        try:
            with transaction.atomic():
                # This writer uses the reader's ordinary application clock.
                new_change(agent.pk)
        finally:
            connections.close_all()

    def commit():
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(writer).result(timeout=10)

    read_account = views.reading_account
    summarize = views.people.summarize

    def acquire(request):
        if phase == "before_snapshot":
            commit()
        result = read_account(request)
        if phase == "after_snapshot":
            commit()
        return result

    def count(*args):
        if phase == "after_marks":
            commit()
        return summarize(*args)

    def sample_clock():
        # newest_change has read the latest timestamp and is about to cap it at the time now.
        commit()
        return timezone.now()

    with monkeypatch.context() as patch:
        if phase == "before_clock":
            patch.setattr(views.people, "timezone", SimpleNamespace(now=sample_clock))
        patch.setattr(views, "reading_account", acquire)
        patch.setattr(views.people, "summarize", count)
        first = client.get(reverse(PEOPLE), {"person": agent.pk})
    if phase == "after_response":
        commit()
    expected = 2 if phase == "before_snapshot" else 1
    [subject] = first.context["page_obj"]
    cell = subject.cells[5]
    assert cell.label == "Changes made" and cell.value == expected
    [exported] = csv_rows(client.get(first.context["export_url"]))
    opened = client.get(cell.url)
    fresh = client.get(reverse(PEOPLE), {"person": agent.pk})
    assert fresh.context["summary"].totals["changes"] == 2
    assert (int(exported["Changes made"]), opened.context["total"]) == (expected, expected)


def test_empty_change_history_stays_empty_in_the_offered_export(client, reviewer):
    client.force_login(reviewer)
    assert not AuditEvent.objects.exists()
    first = client.get(reverse(PEOPLE))
    assert first.context["summary"].totals["changes"] == 0
    assert query_of(first.context["export_url"])["upto_changes"] == "0"
    new_change(reviewer.pk)
    [row] = csv_rows(client.get(first.context["export_url"]))
    assert int(row["Changes made"]) == 0
    assert client.get(reverse(PEOPLE)).context["summary"].totals["changes"] == 1


def test_a_reading_after_historical_changes_keeps_its_marker(client, reviewer, monkeypatch):
    client.force_login(reviewer)
    assert not AuditEvent.objects.exists()
    with monkeypatch.context() as patch:
        patch.setattr(timezone, "now", lambda: datetime(1969, 12, 31, 12, tzinfo=UTC))
        new_change(reviewer.pk)
    first = client.get(reverse(PEOPLE))
    assert first.context["summary"].totals["changes"] == 0
    new_change(reviewer.pk)
    [row] = csv_rows(client.get(first.context["export_url"]))
    assert int(row["Changes made"]) == 0


@pytest.mark.parametrize(
    ("pinned", "regular", "judged", "expected"),
    [
        ("-person", "person", True, "-person"),
        ("failed", "-person", True, "failed"),
        ("unknown", "-person", True, "person"),
        ("--person", "-failed", True, "person"),
        ("-outside", "-person", False, "person"),
        ("outside", "-person", False, "person"),
        ("", "-person", False, "-person"),
        (None, "-person", False, "-person"),
    ],
)
def test_export_sort_is_validated_against_its_reading(
    client, reviewer, agent, pinned, regular, judged, expected
):
    client.force_login(reviewer)
    SignInEvent.objects.create(user=agent, kind="failed")
    BusinessCalendar.objects.create(name="First", hours={"mon": [["09:00", "11:00"]]})
    params = {"sort": regular}
    if pinned is not None:
        params["reading_sort"] = pinned
    if not judged:
        params["calendar"] = "none"
    saved = UserPreference.objects.create(
        user=reviewer, key=f"page:{PEOPLE}", value={"sort": "-tickets"}
    )
    response = client.get(reverse(EXPORT), params)
    rows = csv_rows(response)
    ordered = [row["Username"] for row in rows]
    if expected == "failed":
        assert ordered == [reviewer.username, agent.username]
    else:
        names = sorted(rows, key=lambda row: row["Person"], reverse=expected == "-person")
        assert ordered == [row["Username"] for row in names]
    use = WorkspaceUse.objects.get(request_ref=response["X-Request-ID"])
    assert use.filters.get("sort", "person") == expected
    saved.refresh_from_db()
    assert saved.value == {"sort": "-tickets"}
    assert not UserPreference.objects.filter(user=reviewer, key=f"page:{EXPORT}").exists()


def test_return_from_invalid_direct_export_drops_inherited_export_preferences(client, reviewer):
    one = client
    two = WebClient()
    for session in (one, two):
        session.force_login(reviewer)
    UserPreference.objects.create(
        user=reviewer, key=f"page:{EXPORT}", value={"sort": "failed", "per_page": 50}
    )
    two.get(reverse(PEOPLE), {"sort": "-tickets"})
    requested = {"reading_sort": "-person", "reading_calendar": "99999"}
    reply = one.get(reverse(EXPORT), requested)
    assert reply.status_code == 302
    back = query_of(reply.url)
    assert all(back[k] == v for k, v in requested.items())
    assert "sort" not in back and "per_page" not in back
    assert not WorkspaceUse.objects.filter(request_ref=reply["X-Request-ID"]).exists()
    assert "calendar" in one.get(reply.url).context["filter_form"].errors
    corrected = one.get(reverse(PEOPLE), {**back, "calendar": "none"})
    assert not corrected.context["filter_form"].errors
    assert corrected.context["sort"] == two.get(reverse(PEOPLE)).context["sort"] == "-tickets"
    assert query_of(corrected.context["export_url"])["reading_sort"] == "-tickets"


@pytest.mark.parametrize(
    "explicit",
    [
        (),
        ("date_from",),
        ("date_to", "calendar"),
        ("calendar",),
        ("date_from", "date_to", "calendar"),
    ],
)
def test_outside_link_preserves_each_chosen_field_independently(client, reviewer, agent, explicit):
    client.force_login(reviewer)
    cal = BusinessCalendar.objects.create(name="Monday shift", hours={"mon": [["09:00", "11:00"]]})
    when = timezone.make_aware(datetime(2024, 4, 6, 12))  # Saturday
    SignInEvent.objects.create(user=agent, kind="failed", occurred_at=when)
    fields = {"date_from": "2024-04-06", "date_to": "2024-04-06", "calendar": str(cal.pk)}
    carried = {"date_from": "reading_from", "date_to": "reading_to", "calendar": "reading_calendar"}
    sent = {k if k in explicit else carried[k]: v for k, v in fields.items()}
    first = client.get(reverse(PEOPLE), sent)
    [subject] = first.context["page_obj"]
    assert subject.outside == 1
    address = subject.cells[-1].url
    params = query_of(address)
    assert {k: v for k, v in params.items() if k in fields or k in carried.values()} == sent
    followed = client.get(address)
    assert {c["name"] for c in followed.context["active_filters"]} == {
        *explicit,
        "person",
        "activity",
    }
    assert followed.context["summary"].totals["outside"] == 1
    assert not followed.context["period"]["default"]
    intro = re.search(rb'<p class="vr-page-intro">(.*?)</p>', followed.content).group(1)
    assert b"last 30 days" not in intro


@pytest.mark.parametrize("fail_setting", [False, True])
@pytest.mark.django_db(transaction=True)
def test_preference_save_sees_committed_use_and_failure_still_serves_the_page(
    client, reviewer, monkeypatch, caplog, fail_setting
):
    client.force_login(reviewer)
    key = f"page:{PEOPLE}"
    saved = UserPreference.objects.create(user=reviewer, key=key, value={"sort": "tickets"})
    save = preferences.save_combined
    observed = []

    def committed_use(request_ref):
        try:
            return WorkspaceUse.objects.filter(account=reviewer, request_ref=request_ref).count()
        finally:
            connections.close_all()

    def observe(request, setting, update):
        if setting == key:
            with ThreadPoolExecutor(max_workers=1) as pool:
                observed.append(pool.submit(committed_use, request.request_id).result(timeout=10))
            if fail_setting:
                raise DatabaseError("Synthetic preference write failure")
        return save(request, setting, update)

    monkeypatch.setattr(preferences, "save_combined", observe)
    response = client.get(reverse(PEOPLE), {"sort": "-person"})
    assert response.status_code == 200 and response.context["sort"] == "-person"
    assert (
        WorkspaceUse.objects.get(request_ref=response["X-Request-ID"]).filters["sort"] == "-person"
    )
    assert observed == [1]
    saved.refresh_from_db()
    assert saved.value == {"sort": "tickets" if fail_setting else "-person"}
    assert ("preference.not_saved" in caplog.text) == fail_setting


def final_status(get_response):
    # Simulate a layer between the history writer and the grid middleware. Django can return
    # a different response on an error, or a deployment can change the status on the same one.
    def respond(request):
        response = get_response(request)
        code = int(request.headers["X-Test-Status"])
        if request.headers["X-Test-Replace"] == "yes":
            return HttpResponse(status=code)
        response.status_code = code
        return response

    return respond


@pytest.mark.parametrize(
    ("status", "replace"), [(400, True), (403, False), (503, False), (503, True)]
)
def test_final_error_preserves_the_old_preference(reviewer, settings, status, replace):
    key = f"page:{PEOPLE}"
    saved = UserPreference.objects.create(user=reviewer, key=key, value={"sort": "-changes"})
    stack = list(settings.MIDDLEWARE)
    stack.insert(stack.index("core.access.AccessRecordMiddleware") + 1, f"{__name__}.final_status")
    settings.MIDDLEWARE = stack
    session = WebClient()
    session.force_login(reviewer)
    response = session.get(
        reverse(PEOPLE),
        {"sort": "-failed"},
        headers={"X-Test-Status": str(status), "X-Test-Replace": "yes" if replace else "no"},
    )
    assert response.status_code == status
    if replace or status >= 500:
        assert not WorkspaceUse.objects.filter(request_ref=response["X-Request-ID"]).exists()
    saved.refresh_from_db()
    assert saved.value == {"sort": "-changes"}


def test_ticket_and_timeline_record_access_without_saving_list_settings(
    client, reviewer, clinic, make_ticket
):
    ticket = make_ticket(clinic)
    client.force_login(reviewer)
    for route, pk, kind in [
        ("desk:request-detail", ticket.pk, "ticket"),
        ("clients:client-timeline", clinic.pk, "timeline"),
        ("desk:request-access", ticket.pk, "access"),
    ]:
        url = reverse(route, args=[pk])
        reply = client.get(url, {"per_page": 50, "sort": "-name"})
        assert reply.status_code == 200
        assert list(
            AccessRecord.objects.filter(request_ref=reply["X-Request-ID"]).values_list(
                "kind", "ticket_id"
            )
        ) == [(kind, ticket.pk)]
        head = client.head(url, {"per_page": 100, "sort": "name"})
        assert head.status_code in {200, 405}
        assert not AccessRecord.objects.filter(request_ref=head["X-Request-ID"]).exists()
        assert not UserPreference.objects.filter(user=reviewer, key=f"page:{route}").exists()


@pytest.mark.parametrize(
    ("route", "order"),
    [
        ("clients:client-list", "-account_number"),
        ("desk:request-list", "-subject"),
        ("portal:request-list", "-status"),
        ("service_levels:calendar-list", "-name"),
        ("service_levels:target-list", "-name"),
        ("links:system-list", "-name"),
        ("announcement-list", "-title"),
        ("nonconformance:list", "-status"),
        ("nonconformance:action-list", "-status"),
    ],
)
def test_unrecorded_lists_remember_sort_and_page_size(client, make_user, clinic, route, order):
    if route.startswith("nonconformance:"):
        LabModule.objects.create(key="nonconformance", is_enabled=True, revision=1)
    user = (
        make_user("portal-reader", client=clinic)
        if route.startswith("portal:")
        else make_user("staff-reader", "admin", "supervisor")
    )
    client.force_login(user)
    url = reverse(route)
    sent = client.get(url, {"sort": order, "per_page": "50"})
    assert sent.status_code == 200
    assert not AccessRecord.objects.filter(request_ref=sent["X-Request-ID"]).exists()
    assert not WorkspaceUse.objects.filter(request_ref=sent["X-Request-ID"]).exists()
    saved = UserPreference.objects.get(user=user, key=f"page:{route}")
    assert saved.value == {"sort": order, "per_page": 50}
    head = client.head(url, {"sort": order[1:], "per_page": "100"})
    assert head.status_code in {200, 405}
    next_visit = client.get(url)
    assert next_visit.context["page_obj"].paginator.per_page == 50
    assert next_visit.context["sort"] == order


def test_fast_change_enters_a_fresh_reading_only_after_the_clock_catches_up(
    client, reviewer, monkeypatch
):
    client.force_login(reviewer)
    ordinary_now = timezone.now()
    fast_now = ordinary_now + timedelta(minutes=2)
    with monkeypatch.context() as patch:
        patch.setattr(timezone, "now", lambda: fast_now)
        new_change(reviewer.pk)
    dates = {
        "date_from": (ordinary_now.date() - timedelta(days=1)).isoformat(),
        "date_to": (fast_now.date() + timedelta(days=1)).isoformat(),
    }
    first = client.get(reverse(PEOPLE), dates)
    [row] = first.context["page_obj"]
    assert row.counts["changes"] == 0
    with monkeypatch.context() as patch:
        patch.setattr(timezone, "now", lambda: fast_now + timedelta(seconds=1))
        fresh = client.get(reverse(PEOPLE), dates)
        pinned = client.get(first.context["export_url"])
    assert fresh.context["summary"].totals["changes"] == 1
    assert int(csv_rows(pinned)[0]["Changes made"]) == 0


@pytest.mark.parametrize("year", [1900, 1600])
def test_first_page_counts_the_latest_historical_event_to_the_microsecond(
    client, reviewer, monkeypatch, year
):
    client.force_login(reviewer)
    moment = datetime(year, 4, 2, 12, microsecond=1, tzinfo=UTC)
    with monkeypatch.context() as patch:
        patch.setattr(timezone, "now", lambda: moment)
        event = new_change(reviewer.pk)
    event.refresh_from_db()
    assert event.occurred_at == moment
    reply = client.get(reverse(PEOPLE), {"date_from": moment.date(), "date_to": moment.date()})
    assert reply.status_code == 200
    assert reply.context["summary"].totals["changes"] == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("route", "chosen"),
    [
        (PEOPLE, "-changes"),
        pytest.param(
            "clients:client-list",
            "-name",
            marks=pytest.mark.xfail(
                strict=True,
                reason="follow-up, predates the People tab (confirmation round): a list that "
                "records nothing saves its sort and page size before the session middleware "
                "turns the answer into an error on a session save that fails",
            ),
        ),
    ],
)
def test_grid_setting_when_signout_replaces_the_list_with_a_session_error(
    client, reviewer, rf, monkeypatch, route, chosen
):
    from random import Random

    from django.contrib.messages import INFO
    from django.contrib.messages.middleware import MessageMiddleware
    from django.contrib.messages.storage import default_storage

    client.force_login(reviewer)
    other_tab = WebClient()
    other_tab.cookies = client.cookies.copy()
    saved = UserPreference.objects.create(
        user=reviewer, key=f"page:{route}", value={"sort": "person" if route == PEOPLE else "name"}
    )
    old_value = saved.value.copy()

    # Pending messages too large for the cookie go in the database session. Showing them
    # consumes them and makes Django save that session as the request finishes.
    pending = rf.get("/")
    pending.session = client.session
    messages = default_storage(pending)
    messages.add(INFO, Random(9).randbytes(3500).hex())
    prepared = HttpResponse()
    messages.update(prepared)
    assert pending.session.modified
    pending.session.save()
    client.cookies.update(prepared.cookies)

    finish_messages = MessageMiddleware.process_response

    def sign_out_after_render(layer, request, response):
        response = finish_messages(layer, request, response)
        assert request.session.modified
        other_tab.logout()
        return response

    monkeypatch.setattr(MessageMiddleware, "process_response", sign_out_after_render)
    result = client.get(reverse(route), {"sort": chosen, "per_page": "100"})
    assert result.status_code == 400
    assert not WorkspaceUse.objects.filter(request_ref=result["X-Request-ID"]).exists()
    saved.refresh_from_db()
    assert saved.value == old_value


def test_slow_writer_exception_is_explained_in_both_the_footer_and_page_help(
    client, reviewer, settings, monkeypatch
):
    client.force_login(reviewer)
    first_change = new_change(reviewer.pk)
    start = first_change.occurred_at.date()
    first = client.get(
        reverse(PEOPLE),
        {"date_from": start - timedelta(days=1), "date_to": start + timedelta(days=1)},
    )
    assert first.context["summary"].totals["changes"] == 1
    with monkeypatch.context() as patch:
        patch.setattr(timezone, "now", lambda: first_change.occurred_at - timedelta(seconds=1))
        new_change(reviewer.pk)
    assert int(csv_rows(client.get(first.context["export_url"]))[0]["Changes made"]) == 2
    note = re.search(r"<p[^>]*>Counts are drawn.*?</p>", first.content.decode()).group()
    help_text = json.loads((settings.BASE_DIR / "docs/pages/audit_review/people.json").read_text())
    for source in (note, json.dumps(help_text["sections"])):
        assert re.search(r"clock.{0,60}(behind|slow|skew)|slow.{0,30}clock", source, re.I)
