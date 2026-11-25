"""Independent verification of the People fixes. All history and users are synthetic."""

import csv
import io
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from urllib.parse import parse_qs, urljoin, urlsplit

import pytest
from django.db import DatabaseError, connections, transaction
from django.test import Client as Browser
from django.urls import reverse
from django.utils import timezone

from accounts.models import SignInEvent, User
from audit_review.models import WorkspaceUse
from audit_review.tests.conftest import agent as agent
from audit_review.tests.conftest import make_user as make_user
from audit_review.tests.conftest import review_permission
from audit_review.tests.conftest import reviewer as reviewer
from core.models import AccessRecord, AuditEvent, UserPreference
from service_levels.models import BusinessCalendar


def people_url(export=False):
    return reverse("audit_review:people-export" if export else "audit_review:people")


def uses_for(response):
    return WorkspaceUse.objects.get(request_ref=response.headers["X-Request-ID"])


def csv_rows(response):
    assert response.status_code == 200
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))


def marks_in(address):
    query = parse_qs(urlsplit(address).query)
    return {key: vals for key, vals in query.items() if key.startswith("upto_")}


@pytest.fixture
def shifts():
    hours = {"mon": [["08:00", "16:00"]]}
    return [
        BusinessCalendar.objects.create(name=name, hours=hours, is_active=active)
        for name, active in [("Early", True), ("Late", True), ("Retired", False)]
    ]


@pytest.mark.parametrize(
    ("query", "first", "last", "chosen", "error"),
    [
        (
            {"reading_from": "typo", "reading_to": "2024-02-30", "reading_calendar": "garbled"},
            "2024-03-02",
            "2024-03-31",
            "Early",
            None,
        ),
        (
            {"reading_calendar": "99999999999999999999999"},
            "2024-03-02",
            "2024-03-31",
            "Early",
            None,
        ),
        (
            {"reading_from": "2024-01-01", "reading_to": "2024-12-31", "reading_calendar": "Late"},
            "2024-01-01",
            "2024-12-31",
            "Late",
            None,
        ),
        (
            {"reading_to": "2024-03-01", "reading_calendar": "none"},
            "2024-02-01",
            "2024-03-01",
            "none",
            None,
        ),
        ({"reading_calendar": "Retired"}, None, None, None, "calendar"),
        ({"reading_calendar": "999999"}, None, None, None, "calendar"),
        ({"reading_calendar": "none", "activity": "outside"}, None, None, None, "activity"),
        ({"reading_from": "2024-01-01", "reading_to": "2025-01-01"}, None, None, None, "date_to"),
        ({"reading_from": "2024-03-04", "reading_to": "2024-03-03"}, None, None, None, "date_to"),
        (
            {
                "reading_calendar": "Retired",
                "calendar": "Late",
                "reading_from": "2025-01-01",
                "reading_to": "2023-01-01",
                "date_from": "2024-03-01",
                "date_to": "2024-03-05",
            },
            "2024-03-01",
            "2024-03-05",
            "Late",
            None,
        ),
        (
            {
                "reading_calendar": "Early",
                "calendar": "none",
                "reading_from": "2024-02-01",
                "date_to": "2024-02-29",
            },
            "2024-02-01",
            "2024-02-29",
            "none",
            None,
        ),
        (
            {
                "reading_calendar": "none",
                "calendar": "Late",
                "activity": "outside",
                "reading_to": "2024-03-10",
                "date_from": "2024-03-05",
            },
            "2024-03-05",
            "2024-03-10",
            "Late",
            None,
        ),
        ({"reading_to": "2024-03-10", "date_to": "not-a-date"}, None, None, None, "date_to"),
        ({"reading_calendar": "Early", "calendar": "Retired"}, None, None, None, "calendar"),
    ],
)
def test_carried_defaults_obey_filter_validation_and_history(
    client, reviewer, shifts, monkeypatch, query, first, last, chosen, error
):
    monkeypatch.setattr(timezone, "localdate", lambda: date(2024, 3, 31))
    by_name = {s.name: str(s.pk) for s in shifts}
    supplied = {k: by_name.get(v, v) for k, v in query.items()}
    client.force_login(reviewer)
    response = client.get(people_url(), supplied)
    assert response.status_code == 200
    form = response.context["filter_form"]
    history = uses_for(response)
    exported = client.get(people_url(export=True), supplied)
    if error:
        assert error in form.errors and response.context["total"] == 0
        assert history.filters == {"invalid": True}
        assert exported.status_code == 302
        redirected = parse_qs(urlsplit(exported.url).query)
        assert {k: redirected[k] for k in supplied} == {k: [v] for k, v in supplied.items()}
        assert not WorkspaceUse.objects.filter(
            request_ref=exported.headers["X-Request-ID"]
        ).exists()
        assert error in client.get(exported.url).context["filter_form"].errors
        return
    assert not form.errors
    expected = {
        "date_from": first,
        "date_to": last,
        "calendar": by_name.get(chosen, chosen),
        "activity": supplied.get("activity", "all"),
    }
    assert history.filters == {**expected, "page": "1"}
    assert exported.status_code == 200 and uses_for(exported).filters == expected
    chips = {c["name"] for c in response.context["active_filters"]}
    assert chips == {k for k in supplied if not k.startswith("reading_")}
    pinned = parse_qs(urlsplit(response.context["export_url"]).query)
    assert [pinned[k] for k in ("reading_from", "reading_to", "reading_calendar")] == [
        [first],
        [last],
        [expected["calendar"]],
    ]


def test_short_carried_period_is_described_as_the_actual_period(client, reviewer):
    client.force_login(reviewer)
    result = client.get(people_url(), {"reading_from": "2024-02-12", "reading_to": "2024-02-12"})
    if result.context["filter_form"].errors:
        assert result.context["summary"] is None
        return
    period = result.context["period"]
    assert period["date_from"] == period["date_to"] == date(2024, 2, 12)
    intro = re.search(r'<p class="vr-page-intro">(.*?)</p>', result.content.decode()).group(1)
    assert "last 30 days" not in intro


def test_pager_csv_chip_and_outside_count_apply_their_own_sort_and_reading(
    client, reviewer, shifts, monkeypatch
):
    from tests.test_review_audit_review_people import _PeopleLinks

    other = Browser()
    for session in (client, other):
        session.force_login(reviewer)
    monday = date(2024, 3, 4)
    subjects = User.objects.bulk_create(
        [User(username=f"verify-{i:02}", email=f"verify-{i:02}@example.invalid") for i in range(51)]
    )
    when = timezone.make_aware(timezone.datetime(2024, 3, 4, 7))
    SignInEvent.objects.bulk_create(
        [SignInEvent(user=u, kind="failed", occurred_at=when) for u in subjects]
    )
    monkeypatch.setattr(timezone, "localdate", lambda: monday)
    client.get(people_url(), {"sort": "-failed"})
    first = client.get(people_url(), {"activity": "outside"})
    next_url = _PeopleLinks(first).with_parameter("page", "2")
    export = first.context["export_url"]
    [chip] = first.context["active_filters"]
    assert chip["name"] == "activity" and not parse_qs(urlsplit(chip["remove_url"]).query)
    assert "sort" not in parse_qs(urlsplit(next_url).query)
    # adapted (verification round): the export carries the page's order as reading_sort
    assert parse_qs(urlsplit(export).query)["reading_sort"] == ["-failed"]
    assert other.get(people_url(), {"sort": "-person"}).context["sort"] == "-person"
    monkeypatch.setattr(timezone, "localdate", lambda: monday + timedelta(days=30))
    second = client.get(next_url)
    assert second.context["sort"] == "-person" and second.context["total"] == 51
    assert second.context["period"]["date_to"] == monday
    fresh = client.get(urljoin(people_url(), chip["remove_url"]))
    assert fresh.context["period"]["date_to"] == monday + timedelta(days=30)
    file = client.get(export)
    assert len(csv_rows(file)) == 51
    assert uses_for(file).filters["sort"] == "-failed"
    assert other.get(people_url()).context["sort"] == "-person"

    all_activity = client.get(
        people_url(),
        {
            "reading_to": monday.isoformat(),
            "reading_from": monday.isoformat(),
            "reading_calendar": str(shifts[1].pk),
            "person": subjects[0].pk,
        },
    )
    [person] = all_activity.context["page_obj"]
    address = person.cells[-1].url
    assert marks_in(address) == marks_in(all_activity.context["export_url"])
    outside = client.get(address)
    assert uses_for(outside).filters == {
        "date_from": monday.isoformat(),
        "date_to": monday.isoformat(),
        "person": str(subjects[0].pk),
        "calendar": str(shifts[1].pk),
        "activity": "outside",
        "sort": "-person",
        "page": "1",
    }
    [record] = csv_rows(client.get(outside.context["export_url"]))
    assert record["Failed or refused sign-ins (outside business hours)"] == "1"


def record_batch(subject):
    AuditEvent.objects.create(
        actor_id=subject,
        action="account.updated",
        object_type="accounts.user",
        object_id=str(subject),
    )
    SignInEvent.objects.create(user_id=subject, kind="failed")
    AccessRecord.objects.create(
        account_id=subject, kind="ticket", capacity="laboratory", ticket_id=1
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "phase", ["before_snapshot", "after_snapshot", "after_clock", "after_marks"]
)
def test_links_exclude_a_batch_started_after_the_first_snapshot(
    client, reviewer, agent, monkeypatch, phase
):
    from audit_review import views

    record_batch(agent.pk)
    client.force_login(reviewer)
    before_read = views.reading_account
    # adapted (verification round): the Changes marker is read by people.newest_change
    newest_change = views.people.newest_change
    summarize = views.people.summarize

    def writer():
        try:
            with transaction.atomic():
                record_batch(agent.pk)
        finally:
            connections.close_all()

    def commit():
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(writer).result(timeout=10)

    def at_snapshot(request):
        if phase == "before_snapshot":
            commit()
        result = before_read(request)
        if phase == "after_snapshot":
            commit()
        return result

    def at_clock():
        marker = newest_change()
        if phase == "after_clock":
            commit()
        return marker

    def after_marks(*args):
        if phase == "after_marks":
            commit()
        return summarize(*args)

    with monkeypatch.context() as patch:
        patch.setattr(views, "reading_account", at_snapshot)
        patch.setattr(views.people, "newest_change", at_clock)
        patch.setattr(views.people, "summarize", after_marks)
        first = client.get(people_url(), {"person": agent.pk})
    expected = 2 if phase == "before_snapshot" else 1
    [person] = first.context["page_obj"]
    cells = [person.cells[i] for i in (5, 2, 1)]
    assert [c.value for c in cells] == [expected] * 3
    [row] = csv_rows(client.get(first.context["export_url"]))
    exported = [int(row[c.label]) for c in cells]
    listed = [client.get(c.url).context["total"] for c in cells]
    assert (exported, listed) == ([expected] * 3, [expected] * 3)
    assert client.get(people_url(), {"person": agent.pk}).context["summary"].totals["changes"] == 2


def test_refused_filters_and_retracted_grants_do_not_save_a_new_sort(client, reviewer):
    client.force_login(reviewer)
    chosen = {"sort": "-tickets"}
    client.get(people_url(), chosen)
    key = "page:audit_review:people"
    assert UserPreference.objects.get(user=reviewer, key=key).value == chosen
    for params in (
        {"reading_from": "2024-03-05", "reading_to": "2024-03-01"},
        {"calendar": "99999"},
        {"date_from": "not-a-day"},
    ):
        refused = client.get(people_url(), {**params, "sort": "-sign_ins"})
        assert refused.context["filter_form"].errors
        assert uses_for(refused).filters == {"invalid": True}
        assert UserPreference.objects.get(user=reviewer, key=key).value == chosen
    first = client.get(people_url())
    reviewer.user_permissions.remove(review_permission())
    for path in (first.context["export_url"], people_url() + "?sort=-failed"):
        response = client.get(path)
        assert response.status_code == 403
        assert not WorkspaceUse.objects.filter(
            request_ref=response.headers["X-Request-ID"]
        ).exists()
    assert UserPreference.objects.get(user=reviewer, key=key).value == chosen


def test_failed_use_does_not_save_the_sort_of_the_withheld_page(client, reviewer, monkeypatch):
    def fail(*args, **kwargs):
        raise DatabaseError("Synthetic use-record write failure")

    client.force_login(reviewer)
    client.raise_request_exception = False
    UserPreference.objects.create(
        user=reviewer, key="page:audit_review:people", value={"sort": "-tickets"}
    )
    with monkeypatch.context() as patch:
        patch.setattr(WorkspaceUse.objects, "bulk_create", fail)
        response = client.get(people_url(), {"sort": "-failed"})
    assert response.status_code == 500
    assert not WorkspaceUse.objects.filter(request_ref=response.headers["X-Request-ID"]).exists()
    next_visit = client.get(people_url())
    assert next_visit.context["sort"] == "-tickets"


def test_unjudged_table_does_not_save_a_sort_its_headers_do_not_offer(client, reviewer, shifts):
    client.force_login(reviewer)
    UserPreference.objects.create(
        user=reviewer, key="page:audit_review:people", value={"sort": "-sign_ins"}
    )
    unjudged = client.get(people_url(), {"calendar": "none", "sort": "-outside"})
    assert unjudged.status_code == 200 and not unjudged.context["summary"].judged
    assert 'data-vr-col="outside"' not in unjudged.content.decode()
    assert client.get(people_url()).context["sort"] == "-sign_ins"


@pytest.mark.parametrize("explicit", [False, True])
def test_outside_count_preserves_the_difference_between_selected_filters_and_defaults(
    client, reviewer, agent, shifts, monkeypatch, explicit
):
    monday = date(2024, 3, 4)
    monkeypatch.setattr(timezone, "localdate", lambda: monday)
    when = timezone.make_aware(timezone.datetime(2024, 3, 4, 7))
    SignInEvent.objects.create(user=agent, kind="failed", occurred_at=when)
    client.force_login(reviewer)
    selected = {"calendar": str(shifts[0].pk), "date_from": "2024-03-01", "date_to": "2024-03-04"}
    first = client.get(people_url(), selected if explicit else {})
    [subject] = [person for person in first.context["page_obj"] if person.account_id == agent.pk]
    address = subject.cells[-1].url
    assert address and subject.outside == 1
    monkeypatch.setattr(timezone, "localdate", lambda: monday + timedelta(days=30))
    followed = client.get(address)
    assert followed.context["period"]["date_to"] == monday
    assert followed.context["summary"].totals["outside"] == 1
    expected = {"person", "activity"} | (set(selected) if explicit else set())
    assert {chip["name"] for chip in followed.context["active_filters"]} == expected


def test_split_final_day_keeps_an_earlier_representable_opening(db, settings):
    from datetime import UTC, datetime

    from audit_review.hours import Outside, open_hours

    settings.TIME_ZONE = "UTC"
    cal = BusinessCalendar.objects.create(
        name="Final Friday", hours={"fri": [["08:00", "12:00"], ["20:00", "24:00"]]}
    )
    # The last midnight cannot be expressed as a Python datetime. The first shift can.
    events = SignInEvent.objects.bulk_create(
        [
            SignInEvent(
                user_id=1001, kind="failed", occurred_at=datetime(9999, 12, 31, h, tzinfo=UTC)
            )
            for h in (8, 11, 12)
        ]
    )
    intervals = open_hours(cal, date.max, date.max)
    outside = (
        SignInEvent.objects.filter(pk__in=[e.pk for e in events])
        .order_by("occurred_at")
        .annotate(outside=Outside("occurred_at", intervals))
        .values_list("outside", flat=True)
    )
    assert list(outside) == [False, False, True]


def test_correcting_a_refused_export_does_not_restore_another_tabs_old_sort(
    client, reviewer, shifts
):
    other = Browser()
    for session in (client, other):
        session.force_login(reviewer)
    UserPreference.objects.create(
        user=reviewer, key="page:audit_review:people", value={"sort": "-failed"}
    )
    first = client.get(people_url())
    assert first.context["sort"] == "-failed" and "sort" in first.wsgi_request.vr_saved_params
    export = first.context["export_url"]
    other.get(people_url(), {"sort": "-changes"})
    early, later, _ = shifts
    early.is_active = False
    early.save(update_fields=["is_active"])

    refused = client.get(export)
    assert refused.status_code == 302
    offered = parse_qs(urlsplit(export).query)
    back = parse_qs(urlsplit(refused.url).query)
    assert {
        key: values for key, values in back.items() if key.startswith(("reading_", "upto_"))
    } == {key: values for key, values in offered.items() if key.startswith(("reading_", "upto_"))}
    assert not WorkspaceUse.objects.filter(request_ref=refused.headers["X-Request-ID"]).exists()
    correction = client.get(refused.url)
    assert "calendar" in correction.context["filter_form"].errors
    # The reviewer changes only Business hours. Submit the hidden inputs the actual toolbar kept.
    html = correction.content.decode()
    form = re.search(r'<form[^>]+aria-label="Filter people"[^>]*>(.*?)</form>', html, re.S).group(1)
    hidden = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form))
    fixed = client.get(people_url(), {**hidden, "calendar": later.pk})
    assert not fixed.context["filter_form"].errors
    assert (fixed.context["sort"], other.get(people_url()).context["sort"]) == (
        "-changes",
        "-changes",
    )


@pytest.mark.parametrize("route", ["people", "changes"])
def test_help_explains_a_later_change_from_a_slow_application_clock(
    client, reviewer, agent, settings, monkeypatch, route
):
    import json

    client.force_login(reviewer)
    record_batch(agent.pk)
    first = client.get(reverse(f"audit_review:{route}"), {"person": agent.pk})
    if route == "people":
        [person] = first.context["page_obj"]
        shown = person.counts["changes"]
    else:
        shown = first.context["total"]
    assert shown == 1
    # A different writer starts only after the HTTP response. Its application clock is slow.
    other_clock = timezone.now() - timedelta(seconds=5)
    with monkeypatch.context() as patch:
        patch.setattr(timezone, "now", lambda: other_clock)
        AuditEvent.objects.create(
            actor=agent,
            action="account.updated",
            object_type="accounts.user",
            object_id=str(agent.pk),
        )
    exported = csv_rows(client.get(first.context["export_url"]))
    if route == "people":
        count = int(exported[0]["Changes made"])
        assert client.get(person.cells[5].url).context["total"] == count
    else:
        count = len(exported)
    if count != shown:
        filename = "people" if route == "people" else "workspace"
        help_page = json.loads(
            (settings.BASE_DIR / f"docs/pages/audit_review/{filename}.json").read_text()
        )
        explanation = first.content.decode() + " " + json.dumps(help_page, ensure_ascii=False)
        assert re.search(r"clock.{0,80}(behind|slow|skew)|slow.{0,30}clock", explanation, re.I), (
            shown,
            count,
            filename,
        )


def test_unjudged_reading_survives_the_first_calendar_and_fresh_apply_uses_it(client, reviewer):
    client.force_login(reviewer)
    first = client.get(people_url())
    assert not first.context["summary"].judged
    export = first.context["export_url"]
    assert parse_qs(urlsplit(export).query)["reading_calendar"] == ["none"]
    added = BusinessCalendar.objects.create(name="First shift", hours={"mon": [["09:00", "12:00"]]})
    file = client.get(export)
    [user] = csv_rows(file)
    assert not any(col.startswith("Outside the hours") for col in user)
    assert uses_for(file).filters["calendar"] == "none"
    fresh = client.get(people_url())
    assert fresh.context["period"]["calendar"] == added.name
    assert uses_for(fresh).filters["calendar"] == str(added.pk)
