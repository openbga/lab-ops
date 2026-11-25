"""Independent People review at 88b7185. All accounts and events are synthetic."""

import codecs
import csv
import io
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from html.parser import HTMLParser
from threading import Event
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

import pytest
from django.db import DatabaseError, connections, transaction
from django.test import Client as ClientSession
from django.urls import reverse
from django.utils import timezone

from accounts.models import SignInEvent, User
from audit_review.models import UseKind, WorkspaceUse
from audit_review.tests.conftest import agent as agent
from audit_review.tests.conftest import category as category
from audit_review.tests.conftest import clinic as clinic
from audit_review.tests.conftest import make_ticket as make_ticket
from audit_review.tests.conftest import make_user as make_user
from audit_review.tests.conftest import review_permission
from audit_review.tests.conftest import reviewer as reviewer
from audit_review.tests.conftest import water as water
from core.models import AccessKind, AccessRecord, AuditEvent
from desk.tests.test_account_move import admin_change
from jobs.actor import system_account
from service_levels.models import BusinessCalendar


def _review_route(route):
    return reverse(f"audit_review:{route}")


@pytest.mark.parametrize(
    "audience",
    [
        "anonymous",
        "admin",
        "client_services",
        "supervisor",
        "technical",
        "account_manager",
        "superuser",
        "grant_only",
        "portal_with_grant",
        "inactive",
        "role_group_with_grant",
    ],
)
def test_direct_page_and_export_audiences(client, make_user, clinic, audience):
    calendar = BusinessCalendar.objects.create(
        name="Synthetic review calendar", hours={"mon": [["09:00", "17:00"]]}
    )
    subject = make_user("review-only-subject")
    if audience != "anonymous":
        roles = {
            "superuser": (),
            "grant_only": (),
            "portal_with_grant": ("supervisor",),
            "inactive": ("technical",),
            "role_group_with_grant": ("technical",),
        }.get(audience, (audience,))
        account = make_user(
            "review-observer",
            *roles,
            client=clinic if audience == "portal_with_grant" else None,
            is_staff=audience in {"superuser", "grant_only"},
            is_superuser=audience == "superuser",
            is_active=audience != "inactive",
        )
        if audience in {"grant_only", "portal_with_grant", "inactive"}:
            account.user_permissions.add(review_permission())
        if audience == "role_group_with_grant":
            account.groups.get(name="technical").permissions.add(review_permission())
        client.force_login(account)

    common = {"date_from": "2024-02-01", "date_to": "2024-02-29", "person": subject.pk}
    sources = {
        "people": {"activity": "outside", "calendar": calendar.pk},
        "changes": {"outside_hours": calendar.pk},
        "access": {"outside_hours": calendar.pk, "action": "patient_information"},
        "sign-ins": {"outside_hours": calendar.pk, "action": "failed_or_refused"},
    }
    for route, filters in sources.items():
        for suffix in ("", "-export"):
            response = client.get(_review_route(route + suffix), common | filters)
            if audience == "role_group_with_grant":
                assert response.status_code == 200
                if not suffix:
                    assert not response.context["filter_form"].errors
            else:
                expected = 302 if audience in {"anonymous", "inactive"} else 403
                assert response.status_code == expected, (route, audience)
                assert subject.username.encode() not in response.content
                assert calendar.name.encode() not in response.content
                if expected == 302:
                    assert "/accounts/login/" in response["Location"]
                calendar_field = "calendar" if route == "people" else "outside_hours"
                missing = client.get(
                    _review_route(route + suffix),
                    common | filters | {"person": 999999, calendar_field: 999999},
                )
                assert missing.status_code == response.status_code
                assert "Select a valid choice" not in missing.content.decode()
                if expected == 302:
                    assert urlsplit(missing["Location"]).path == urlsplit(response["Location"]).path
    assert WorkspaceUse.objects.count() == (8 if audience == "role_group_with_grant" else 0)


@pytest.mark.parametrize("removed", ["groups", "user_permissions"])
def test_open_tab_links_recheck_the_grant_and_role(client, reviewer, make_user, removed):
    administration = ClientSession()
    administration.force_login(make_user("review-owner", "admin", is_staff=True, is_superuser=True))
    client.force_login(reviewer)
    first = client.get(_review_route("people"), {"person": reviewer.pk})
    [person] = first.context["page_obj"]
    sign_ins = person.cells[0]
    assert sign_ins.url and sign_ins.value == 1

    assert admin_change(administration, reviewer, **{removed: []}).status_code == 302
    for address in (sign_ins.url, first.context["export_url"]):
        assert client.get(address).status_code == 403
    assert list(WorkspaceUse.objects.values_list("source", flat=True)) == ["people"]


@pytest.mark.parametrize("route", ["people", "people-export"])
def test_people_write_failure_withholds_the_response(client, reviewer, monkeypatch, route):
    def failed_write(*args, **kwargs):
        raise DatabaseError("Synthetic workspace recorder failure")

    client.raise_request_exception = False
    client.force_login(reviewer)
    monkeypatch.setattr(WorkspaceUse.objects, "bulk_create", failed_write)
    response = client.get(_review_route(route))
    assert response.status_code == 500
    assert "Content-Disposition" not in response
    assert b"Everyone listed" not in response.content
    assert not WorkspaceUse.objects.exists()


def _unavailable_response(get_response):
    def unavailable(request):
        response = get_response(request)
        response.status_code = 503
        return response

    return unavailable


def test_server_errors_are_not_recorded_and_non_get_methods_cannot_export(
    client, reviewer, settings
):
    client.force_login(reviewer)
    for route in ("people", "people-export"):
        address = _review_route(route)
        for method in (
            client.post,
            client.head,
            client.put,
            client.patch,
            client.delete,
            client.options,
        ):
            assert method(address).status_code == 405
    middleware = list(settings.MIDDLEWARE)
    middleware.insert(
        middleware.index("core.access.AccessRecordMiddleware") + 1,
        f"{__name__}._unavailable_response",
    )
    settings.MIDDLEWARE = middleware
    another_client = ClientSession()
    another_client.force_login(reviewer)
    for route in ("people", "people-export"):
        assert another_client.get(_review_route(route)).status_code == 503
    assert not WorkspaceUse.objects.exists()


def test_people_use_matches_each_answer_and_displays_names(
    client, reviewer, make_user, clinic, settings
):
    person = make_user(
        "history-subject", first_name="Synthetic", last_name="Subject", client=clinic
    )
    calendar = BusinessCalendar.objects.create(
        name="Thursday hours", hours={"thu": [["08:00", "12:00"]]}
    )
    moment = datetime(2024, 9, 5, 17, tzinfo=ZoneInfo(settings.TIME_ZONE))
    SignInEvent.objects.create(user=person, occurred_at=moment, kind="failed")
    query = {
        "date_from": "2024-09-05",
        "date_to": "2024-09-05",
        "calendar": str(calendar.pk),
        "activity": "outside",
        "person": str(person.pk),
        "organization": str(clinic.pk),
        "sort": "-failed",
    }
    client.force_login(reviewer)
    response = client.get(_review_route("people"), query)
    exported = client.get(response.context["export_url"])
    assert exported.status_code == 200
    ids = [answer["X-Request-ID"] for answer in (response, exported)]
    assert len(set(ids)) == 2 and all(ids)
    history = list(WorkspaceUse.objects.order_by("pk"))
    assert [
        (use.account_id, use.kind, use.source, use.filters, use.rows, use.request_ref)
        for use in history
    ] == [
        (reviewer.pk, UseKind.VIEWED, "people", {**query, "page": "1"}, 1, ids[0]),
        (reviewer.pk, UseKind.EXPORTED, "people", query, 1, ids[1]),
    ]
    uses = client.get(_review_route("uses"))
    assert uses.status_code == 200
    for row in uses.context["page_obj"]:
        assert row.cells[0].note == "People"
        for label in (
            calendar.name,
            person.get_full_name(),
            clinic.name,
            "Only activity outside business hours",
            "Sorted by failed or refused sign-ins, most first",
        ):
            assert label in row.cells[1].text
    invalid = client.get(_review_route("people"), {"date_from": "not-a-date"})
    assert invalid.status_code == 200 and invalid.context["total"] == 0
    assert WorkspaceUse.objects.get(request_ref=invalid["X-Request-ID"]).filters == {
        "invalid": True
    }
    count = WorkspaceUse.objects.count()
    redirected = client.get(_review_route("people-export"), {"date_from": "not-a-date"})
    assert redirected.status_code == 302 and WorkspaceUse.objects.count() == count


def test_workspace_use_names_all_activity_when_it_was_the_default(client, reviewer):
    BusinessCalendar.objects.create(name="Default hours", hours={"mon": [["09:00", "17:00"]]})
    client.force_login(reviewer)
    first = client.get(_review_route("people"))
    assert first.context["period"]["outside_only"] is False
    [row] = client.get(_review_route("uses")).context["page_obj"]
    assert "All activity" in row.cells[1].text


def test_people_sort_is_saved_for_the_same_reviewer_across_browsers(client, reviewer):
    second = ClientSession()
    for session in (client, second):
        session.force_login(reviewer)
    assert client.get(_review_route("people"), {"sort": "-sign_ins"}).context["sort"] == "-sign_ins"
    assert second.get(_review_route("people")).context["sort"] == "-sign_ins"


def test_export_from_default_calendar_keeps_the_calendar_that_was_offered(
    client, reviewer, settings
):
    chosen = BusinessCalendar.objects.create(
        name="Original Monday", hours={"mon": [["08:00", "17:00"]]}
    )
    monday = datetime(2024, 9, 2, 10, tzinfo=ZoneInfo(settings.TIME_ZONE))
    SignInEvent.objects.create(user=reviewer, kind="failed", occurred_at=monday)
    client.force_login(reviewer)
    first = client.get(
        _review_route("people"), {"date_from": "2024-09-02", "date_to": "2024-09-02"}
    )
    assert first.context["period"]["calendar"] == chosen.name
    assert first.context["summary"].totals["outside"] == 0

    # The original calendar, its holidays, the people and all the counted records stay the same.
    BusinessCalendar.objects.create(name="Additional Thursday", hours={"thu": [["09:00", "12:00"]]})
    response = client.get(first.context["export_url"])
    assert response.status_code == 200
    header, row = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert (header[-1], row[-1]) == (f"Outside the hours of {chosen.name}", "0")


@pytest.mark.parametrize(
    ("zone", "first", "last"),
    [
        ("America/New_York", "2024-03-09", "2024-03-11"),
        ("America/New_York", "2024-11-02", "2024-11-04"),
        ("CET", "2024-10-26", "2024-10-28"),
        ("EET", "2024-03-30", "2024-04-01"),
        ("EST", "2024-07-06", "2024-07-08"),
        ("Pacific/Apia", "2011-12-29", "2012-01-02"),
    ],
)
def test_people_hours_drilldowns_match_service_clock_intervals(
    client, reviewer, agent, settings, zone, first, last
):
    from datetime import UTC, date, datetime, time, timedelta
    from zoneinfo import ZoneInfo

    from django.urls import reverse

    from accounts.models import SignInEvent
    from service_levels.business_time import WEEKDAYS, open_intervals
    from service_levels.models import BusinessCalendar, Holiday

    settings.TIME_ZONE = zone
    tz = ZoneInfo(zone)
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    shift = [["00:00", "01:30"], ["02:15", "04:00"], ["22:00", "24:00"]]
    calendar = BusinessCalendar.objects.create(
        name="Split shifts", hours={weekday: shift for weekday in WEEKDAYS}
    )
    Holiday.objects.create(calendar=calendar, date=end, name="Closed")
    # The first and last date boundaries in these periods are unambiguous.
    lower = datetime.combine(start, time.min, tzinfo=tz).astimezone(UTC)
    upper = datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)
    intervals = list(open_intervals(calendar.as_calendar(), start - timedelta(days=1), end))
    step = timedelta(microseconds=1)
    points = {lower - step, lower, upper - step, upper}
    for begin, finish in intervals:
        for boundary in (begin, finish):
            if lower <= boundary < upper:
                points.update([boundary - step, boundary, boundary + step])
    SignInEvent.objects.bulk_create(
        [SignInEvent(user_id=agent.pk, kind="signed_in", occurred_at=p) for p in points]
    )
    client.force_login(reviewer)
    period = {p for p in points if lower <= p < upper}
    outside = {p for p in period if not any(a <= p < b for a, b in intervals)}
    for activity, expected in (("", period), ("outside", outside)):
        response = client.get(
            reverse("audit_review:people"),
            {
                "date_from": first,
                "date_to": last,
                "person": agent.pk,
                "calendar": calendar.pk,
                "activity": activity,
            },
        )
        [person] = response.context["page_obj"]
        count = person.cells[0]
        assert count.value == len(expected)
        assert person.outside == len(outside)
        linked = client.get(count.url)
        assert linked.context["total"] == len(expected)
        rows = list(linked.context["page_obj"])
        if len(rows) < len(expected):
            rows += list(client.get(count.url + "&page=2").context["page_obj"])
        assert {row.occurred_at for row in rows} == expected


@pytest.mark.parametrize("destination", ["outside-count", "export"])
def test_last_calendar_retirement_preserves_requested_outside_filter(
    client, reviewer, agent, settings, destination
):
    import csv
    import io
    from datetime import UTC, datetime

    from django.urls import reverse

    from accounts.models import SignInEvent
    from audit_review.models import WorkspaceUse
    from service_levels.models import BusinessCalendar

    settings.TIME_ZONE = "UTC"
    chosen = BusinessCalendar.objects.create(
        name="Monday shift", hours={"mon": [["09:00", "17:00"]]}
    )
    client.force_login(reviewer)
    for hour in (8, 12):
        SignInEvent.objects.create(
            user=agent, kind="signed_in", occurred_at=datetime(2024, 7, 1, hour, tzinfo=UTC)
        )
    review = client.get(
        reverse("audit_review:people"),
        {"person": agent.pk, "date_from": "2024-07-01", "date_to": "2024-07-01"},
    )
    [person] = review.context["page_obj"]
    assert person.outside == 1
    outside_url = person.cells[-1].url
    filtered = client.get(outside_url)
    [person] = filtered.context["page_obj"]
    assert person.counts["sign_ins"] == 1
    # The active calendar may change between drawing a page and using its links.
    chosen.is_active = False
    chosen.save(update_fields=["is_active"])
    if destination == "export":
        previous = WorkspaceUse.objects.count()
        answer = client.get(filtered.context["export_url"])
        rows = list(csv.DictReader(io.StringIO(answer.content.decode("utf-8-sig"))))
        assert (answer.status_code, [r.get("Sign-ins") for r in rows]) == (302, [])
        assert WorkspaceUse.objects.count() == previous
    else:
        answer = client.get(outside_url)
        assert [p.counts["sign_ins"] for p in answer.context["page_obj"]] == []
        assert answer.context["filter_form"].errors
        assert WorkspaceUse.objects.latest("pk").filters == {"invalid": True}


@pytest.mark.parametrize(
    ("zone", "first", "last", "weekday", "instant"),
    [
        ("UTC", "9999-12-31", "9999-12-31", "fri", "9999-12-31T12:00:00+00:00"),
        ("Asia/Tokyo", "0001-01-01", "0001-01-08", "mon", "0001-01-08T02:00:00+00:00"),
    ],
)
def test_representable_calendar_intervals_at_date_limits_are_open(
    db, settings, zone, first, last, weekday, instant
):
    from datetime import date, datetime

    from accounts.models import SignInEvent
    from audit_review.hours import Outside, open_hours
    from service_levels.models import BusinessCalendar

    settings.TIME_ZONE = zone
    calendar = BusinessCalendar.objects.create(
        name="Edge hours", hours={weekday: [["00:00", "16:00"]]}
    )
    from service_levels.business_time import open_intervals

    moment = datetime.fromisoformat(instant)
    local_day = moment.astimezone(calendar.as_calendar().tz).date()
    begin, finish = next(open_intervals(calendar.as_calendar(), local_day, local_day))
    assert begin <= moment < finish
    event = SignInEvent.objects.create(user_id=199, kind="failed", occurred_at=moment)
    intervals = open_hours(calendar, date.fromisoformat(first), date.fromisoformat(last))
    assert (
        not SignInEvent.objects.filter(pk=event.pk)
        .annotate(is_outside=Outside("occurred_at", intervals))
        .values_list("is_outside", flat=True)
        .get()
    )


def test_people_and_lists_recheck_calendar_edits_and_name_their_filter(
    client, reviewer, agent, settings
):
    from datetime import UTC, date, datetime

    from django.urls import reverse

    from accounts.models import SignInEvent
    from audit_review.models import WorkspaceUse
    from service_levels.models import BusinessCalendar, Holiday

    settings.TIME_ZONE = "UTC"
    monday = date(2024, 7, 1)
    chosen = BusinessCalendar.objects.create(
        name="Monday shift", hours={"mon": [["09:00", "17:00"]]}
    )
    client.force_login(reviewer)
    for hour in (8, 12):
        SignInEvent.objects.create(
            user=agent, kind="signed_in", occurred_at=datetime(2024, 7, 1, hour, tzinfo=UTC)
        )
    response = client.get(
        reverse("audit_review:people"),
        {"person": agent.pk, "date_from": monday, "date_to": monday, "activity": "outside"},
    )
    [person] = response.context["page_obj"]
    assert person.cells[0].value == 1
    Holiday.objects.create(calendar=chosen, date=monday, name="Closed for the day")
    listing = client.get(person.cells[0].url)
    assert listing.context["total"] == 2
    [chip] = [chip for chip in listing.context["active_filters"] if chip["name"] == "outside_hours"]
    assert chip["value"] == chosen.name
    use = WorkspaceUse.objects.get(request_ref=listing.headers["X-Request-ID"])
    assert use.filters["outside_hours"] == str(chosen.pk)
    uses = client.get(reverse("audit_review:uses"))
    assert any(
        f"Outside the hours of: {chosen.name}" in row.cells[1].text
        for row in uses.context["page_obj"]
    )
    chosen.is_active = False
    chosen.save(update_fields=["is_active"])
    invalid = client.get(person.cells[0].url)
    assert "outside_hours" in invalid.context["filter_form"].errors
    assert invalid.context["total"] == 0


def test_people_workspace_use_regression_is_independent_of_test_run_date(
    client, reviewer, agent, monkeypatch
):
    from datetime import date

    from django.utils import timezone

    from audit_review.tests import test_people
    from service_levels.models import BusinessCalendar

    calendar = BusinessCalendar.objects.create(
        name="Weekday hours", hours=test_people.WEEKDAYS_9_TO_5
    )
    # Exercise the candidate's actual request and assertion one day after its date window expires.
    monkeypatch.setattr(timezone, "localdate", lambda: date(2027, 1, 6))
    test_people.test_the_uses_list_describes_a_people_use_by_name(client, reviewer, agent, calendar)


@pytest.mark.parametrize("name", ["changes", "access", "sign-ins"])
def test_outside_hours_list_requires_both_dates_and_accepts_366_days(client, reviewer, name):
    from django.urls import reverse

    from audit_review.models import WorkspaceUse
    from service_levels.models import BusinessCalendar

    chosen = BusinessCalendar.objects.create(
        name="First shift", hours={"wed": [["09:00", "12:00"]]}
    )
    client.force_login(reviewer)
    path = reverse(f"audit_review:{name}")
    for period, error in (
        ({"date_to": "2024-12-31"}, "outside_hours"),
        ({"date_from": "2024-01-01"}, "outside_hours"),
        ({"date_from": "2024-01-01", "date_to": "2025-01-01"}, "date_to"),
        ({"date_from": "2024-01-01", "date_to": "2024-12-31"}, None),
    ):
        response = client.get(path, {"outside_hours": chosen.pk, **period})
        form = response.context["filter_form"]
        use = WorkspaceUse.objects.get(request_ref=response.headers["X-Request-ID"])
        if error:
            assert error in form.errors
            assert response.context["total"] == 0
            assert use.filters == {"invalid": True}
        else:
            assert form.is_valid()
            assert (use.filters["date_from"], use.filters["date_to"]) == (
                "2024-01-01",
                "2024-12-31",
            )
            assert use.filters["outside_hours"] == str(chosen.pk)


@pytest.mark.django_db(transaction=True)
def test_people_constraint_upgrade_with_an_old_writer_and_both_rollback_cases():
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue
    from threading import Event
    from time import monotonic, sleep

    from django.db import IntegrityError, connection, connections, transaction
    from django.db.migrations.executor import MigrationExecutor

    previous = ("audit_review", "0001_initial")
    current = ("audit_review", "0002_people_summary")

    def move(target):
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        return executor.loader.project_state([target]).apps.get_model(
            "audit_review", "WorkspaceUse"
        )

    def physical_rows():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, source, xmin::text, ctid::text FROM audit_review_workspaceuse "
                "WHERE request_ref = 'before-upgrade' ORDER BY id"
            )
            return cursor.fetchall()

    old_model = move(previous)
    try:
        for source in ("changes", "access", "sign_ins", "uses"):
            old_model.objects.create(
                account_id=41, kind="viewed", rows=1, source=source, request_ref="before-upgrade"
            )
        before = physical_rows()
        assert len(before) == 4
        allow_insert = Event()
        writer_pids = Queue()

        def previous_release():
            worker_db = connections["default"]
            try:
                with worker_db.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    writer_pids.put(cursor.fetchone()[0])
                assert allow_insert.wait(10)
                row = old_model.objects.create(
                    account_id=41, kind="exported", rows=2, source="uses"
                )
                return row.pk
            finally:
                worker_db.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            old_insert = pool.submit(previous_release)
            old_pid = writer_pids.get(timeout=10)
            saw_wait = []

            def run_during_ddl(execute, sql, params, many, wrapper_context):
                if "ADD CONSTRAINT" in sql and "workspace_use_source" in sql:
                    allow_insert.set()
                    with connection.cursor() as cursor:
                        deadline = monotonic() + 8
                        while monotonic() < deadline:
                            cursor.execute(
                                "SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [old_pid]
                            )
                            if cursor.fetchone()[0]:
                                saw_wait.append(True)
                                break
                            sleep(0.01)
                return execute(sql, params, many, wrapper_context)

            try:
                with connection.execute_wrapper(run_during_ddl):
                    current_model = move(current)
            finally:
                allow_insert.set()
            assert old_insert.result(timeout=10)
            assert saw_wait, "The old writer must overlap the actual constraint replacement"

        assert physical_rows() == before
        assert sorted(current_model.objects.values_list("source", flat=True)) == [
            "access",
            "changes",
            "sign_ins",
            "uses",
            "uses",
        ]
        old_model = move(previous)
        assert physical_rows() == before
        with pytest.raises(IntegrityError), transaction.atomic():
            old_model.objects.create(account_id=41, kind="viewed", rows=0, source="people")
        current_model = move(current)
        use = current_model.objects.create(account_id=41, kind="viewed", rows=0, source="people")
        with pytest.raises(IntegrityError):
            move(previous)
        assert current in MigrationExecutor(connection).loader.applied_migrations
        assert current_model.objects.get(pk=use.pk).source == "people"
        assert physical_rows() == before
        with pytest.raises(IntegrityError), transaction.atomic():
            current_model.objects.create(account_id=41, kind="viewed", rows=0, source="invalid")
    finally:
        move(current)


@pytest.mark.django_db(transaction=True)
def test_reseeding_and_resetting_a_sealed_demo_with_a_people_use(monkeypatch, settings):
    from io import StringIO

    from django.core.management import call_command

    from accounts.models import User
    from audit_review.models import WorkspaceUse

    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    monkeypatch.setenv("DEMO_PASSWORD", "River bend under autumn clouds 31")
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    output = StringIO()
    call_command("seed_demo", stdout=output)
    reader = User.objects.get(username="demo_supervisor")
    use = WorkspaceUse.objects.create(
        account_id=reader.pk, kind="viewed", rows=1, source="people", request_ref="review-demo"
    )
    call_command("seed_demo", stdout=output)
    assert WorkspaceUse.objects.filter(
        pk=use.pk, source="people", request_ref="review-demo"
    ).exists()
    call_command("reset_demo", interactive=False, stdout=output)
    assert not WorkspaceUse.objects.filter(request_ref="review-demo").exists()
    assert User.objects.get(username="demo_supervisor").is_active


def test_year_of_people_uses_one_grouped_query_per_source_at_both_sizes(client, reviewer):
    from datetime import timedelta
    from time import perf_counter

    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from django.urls import reverse
    from django.utils import timezone

    from accounts.models import SignInEvent, User
    from core.models import AccessRecord, AuditEvent
    from service_levels.models import BusinessCalendar

    schedule = {
        day: [["07:00", "12:00"], ["13:00", "18:00"]]
        for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    }
    calendar = BusinessCalendar.objects.create(name="Two shifts", hours=schedule)
    client.force_login(reviewer)
    today = timezone.localdate()
    query = {"date_from": today - timedelta(days=365), "date_to": today, "calendar": calendar.pk}
    address = reverse("audit_review:people")
    counts = []
    for first, stop in ((0, 1), (1, 128)):
        accounts = User.objects.bulk_create(
            [
                User(username=f"perf-{n:03}", email=f"perf-{n:03}@example.invalid")
                for n in range(first, stop)
            ]
        )
        reads, events, changes = [], [], []
        now = timezone.now()
        for account in accounts:
            for repeat in range(20):
                moment = now - timedelta(days=repeat * 18, minutes=repeat)
                reads.append(
                    AccessRecord(
                        occurred_at=moment,
                        account_id=account.pk,
                        kind="ticket",
                        capacity="laboratory",
                        ticket_id=101,
                    )
                )
                events.append(SignInEvent(occurred_at=moment, user=account, kind="failed"))
                changes.append(
                    AuditEvent(
                        actor=account,
                        action="request.updated",
                        object_type="desk.servicerequest",
                        object_id="101",
                    )
                )
        AccessRecord.objects.bulk_create(reads)
        SignInEvent.objects.bulk_create(events)
        AuditEvent.objects.bulk_create(changes)
        client.get(address, query)  # warm request, including session updates and template loading
        before = perf_counter()
        with CaptureQueriesContext(connection) as queries:
            response = client.get(address, query)
        elapsed = perf_counter() - before
        assert response.status_code == 200 and response.context["total"] == stop + 1
        assert response.context["summary"].totals["tickets"] == stop * 20
        assert response.context["summary"].totals["failed"] == stop * 20
        assert response.context["summary"].totals["changes"] == stop * 20
        grouped = [entry["sql"] for entry in queries if "GROUP BY" in entry["sql"]]
        for model in (AccessRecord, SignInEvent, AuditEvent):
            name = model._meta.db_table
            assert sum(f'FROM "{name}"' in sql for sql in grouped) == 1
        counts.append(len(queries))
        print(
            f"366 days, {stop} measured accounts, {stop * 60} records: "
            f"{len(queries)} SQL statements, {elapsed:.3f}s"
        )
    assert counts[0] == counts[1]


@pytest.mark.parametrize("source", ("changes", "access", "sign-ins", "uses"))
def test_earlier_list_query_counts_on_an_unfiltered_get(client, reviewer, source):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from django.urls import reverse

    from service_levels.models import BusinessCalendar

    BusinessCalendar.objects.create(name="Review weekday", hours={"mon": [["09:00", "17:00"]]})
    client.force_login(reviewer)
    address = reverse(f"audit_review:{source}")
    client.get(address)
    with CaptureQueriesContext(connection) as queries:
        response = client.get(address)
    assert response.status_code == 200
    print(
        source,
        len(queries),
        [
            entry["sql"][:93]
            for entry in queries
            if 'FROM "service_levels_businesscalendar"' in entry["sql"]
        ],
    )

    # Same warmed GET and synthetic database on 1d785a7, before the calendar filter shipped.
    base_queries = {"changes": 16, "access": 16, "sign-ins": 18, "uses": 18}
    # adapted (accepted, round 1): the lists with "Outside the hours of" read the active
    # calendars for its choices, one statement a page whatever the rows; Workspace use has none.
    added = {"changes": 1, "access": 1, "sign-ins": 1, "uses": 0}
    assert len(queries) <= base_queries[source] + added[source]


class _PeopleLinks(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.links = []
        self.path = response.wsgi_request.path
        self.feed(response.content.decode())

    def handle_starttag(self, tag, attrs):
        if tag == "a" and (href := dict(attrs).get("href")):
            self.links.append(urljoin(self.path, href))

    def with_parameter(self, name, value):
        return next(
            link for link in self.links if parse_qs(urlsplit(link).query).get(name) == [value]
        )


def _marker_csv(response):
    assert response.status_code == 200
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))


def _marker_record_all(account_id):
    AuditEvent.objects.create(
        actor_id=account_id,
        action="account.updated",
        object_type="accounts.user",
        object_id=str(account_id),
    )
    AccessRecord.objects.create(
        account_id=account_id, kind="ticket", capacity="laboratory", ticket_id=1
    )
    SignInEvent.objects.create(user_id=account_id, kind="failed")


def _marker_vector(person):
    return tuple(person.counts[key] for key in ("changes", "tickets", "failed"))


@pytest.mark.parametrize("route", ["people", "people-export"])
@pytest.mark.django_db(transaction=True)
def test_first_people_reading_agrees_across_a_single_committed_batch(
    client, reviewer, agent, monkeypatch, route
):
    """A committed batch cannot appear in just two of three totals at any database instant."""
    from audit_review import views

    client.force_login(reviewer)
    _marker_record_all(agent.pk)

    def commit_batch():
        try:
            with transaction.atomic():
                _marker_record_all(agent.pk)
        finally:
            connections.close_all()

    original = views.reading_account

    def commit_before_first_snapshot_query(request):
        with ThreadPoolExecutor(max_workers=1) as writer:
            writer.submit(commit_batch).result(timeout=10)
        return original(request)

    monkeypatch.setattr(views, "reading_account", commit_before_first_snapshot_query)
    response = client.get(reverse(f"audit_review:{route}"), {"person": agent.pk})
    assert response.status_code == 200
    if route == "people":
        [person] = response.context["page_obj"]
        counts = _marker_vector(person)
    else:
        [record] = _marker_csv(response)
        counts = tuple(
            int(record[key])
            for key in ("Changes made", "Tickets opened", "Failed or refused sign-ins")
        )
    assert counts in {(1, 1, 1), (2, 2, 2)}, counts


@pytest.mark.xfail(
    strict=True,
    reason="documented limit (round 1): a record still being saved when the first page opened, "
    "whose number or time comes before the reading's, is counted once it is saved, as on the lists",
)
@pytest.mark.django_db(transaction=True)
def test_people_export_and_drilldowns_keep_counts_when_pending_rows_commit(client, reviewer, agent):
    """Exercise the unconditional snapshot promise in the People page help."""

    client.force_login(reviewer)
    _marker_record_all(agent.pk)
    allocated, allow_commit = Event(), Event()

    def delayed_commit():
        try:
            with transaction.atomic():
                _marker_record_all(agent.pk)
                allocated.set()
                assert allow_commit.wait(15)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as writer:
        pending = writer.submit(delayed_commit)
        try:
            assert allocated.wait(10)
            _marker_record_all(agent.pk)
            first = client.get(reverse("audit_review:people"), {"person": agent.pk})
            [person] = first.context["page_obj"]
            assert _marker_vector(person) == (2, 2, 2)
        finally:
            allow_commit.set()
        pending.result(timeout=10)
    [row] = _marker_csv(client.get(first.context["export_url"]))
    exported = tuple(
        int(row[key]) for key in ("Changes made", "Tickets opened", "Failed or refused sign-ins")
    )
    details = tuple(client.get(person.cells[i].url).context["total"] for i in (5, 2, 1))
    assert exported == details == _marker_vector(person), (
        exported,
        details,
        _marker_vector(person),
    )


@pytest.mark.django_db
@pytest.mark.parametrize("destination", ["page", "sort", "export"])
def test_people_links_keep_the_first_default_dates_across_midnight(
    client, reviewer, monkeypatch, destination
):
    client.force_login(reviewer)
    last = timezone.localdate()
    first = last - timedelta(days=29)
    first_noon = timezone.make_aware(datetime.combine(first, time(12)))
    subjects = User.objects.bulk_create(
        [User(username=f"period-{i:02}", email=f"period-{i}@example.invalid") for i in range(51)]
    )
    SignInEvent.objects.bulk_create(
        [
            SignInEvent(user_id=subject.pk, kind="failed", occurred_at=first_noon)
            for subject in subjects
        ]
    )
    response = client.get(reverse("audit_review:people"))
    assert response.context["summary"].totals["failed"] == len(subjects)
    links = _PeopleLinks(response)
    address = {
        "page": links.with_parameter("page", "2"),
        "sort": links.with_parameter("sort", "-failed"),
        "export": response.context["export_url"],
    }[destination]

    monkeypatch.setattr(timezone, "localdate", lambda: last + timedelta(days=1))
    followed = client.get(address)
    if destination == "export":
        data = _marker_csv(followed)
        applied = {(r["From"], r["To"]) for r in data}
        failed = sum(int(r["Failed or refused sign-ins"]) for r in data)
    else:
        assert followed.status_code == 200
        period = followed.context["period"]
        applied = {(period["date_from"].isoformat(), period["date_to"].isoformat())}
        failed = followed.context["summary"].totals["failed"]
    assert (applied, failed) == ({(first.isoformat(), last.isoformat())}, len(subjects))


@pytest.mark.django_db
def test_people_export_preserves_a_valid_from_only_366_day_reading(client, reviewer, monkeypatch):
    client.force_login(reviewer)
    today = timezone.localdate()
    response = client.get(
        reverse("audit_review:people"), {"date_from": today - timedelta(days=365)}
    )
    assert response.context["total"] == 1
    monkeypatch.setattr(timezone, "localdate", lambda: today + timedelta(days=1))
    followed = client.get(response.context["export_url"])
    assert followed.status_code == 200
    assert [row["To"] for row in _marker_csv(followed)] == [today.isoformat()]


@pytest.mark.django_db
def test_people_pager_sort_and_export_reuse_all_three_inclusive_markers(client, reviewer):
    client.force_login(reviewer)
    subjects = User.objects.bulk_create(
        [User(username=f"reader-{i:02}", email=f"reader-{i}@example.invalid") for i in range(52)]
    )
    for subject in subjects:
        _marker_record_all(subject.pk)
    path = reverse("audit_review:people")
    first = client.get(path)
    totals = first.context["summary"].totals
    assert tuple(totals[key] for key in ("changes", "tickets", "failed")) == (52, 52, 52)
    links = _PeopleLinks(first)
    export = first.context["export_url"]
    sort = links.with_parameter("sort", "-failed")
    next_page = links.with_parameter("page", "2")
    for link in (next_page, sort, export):
        args = parse_qs(urlsplit(link).query)
        for marker in ("upto_changes", "upto_access", "upto_sign_ins"):
            assert args[marker] == parse_qs(urlsplit(export).query)[marker]
    _marker_record_all(subjects[-1].pk)
    second = client.get(next_page)
    assert second.context["page_obj"].number == 2
    second_use = WorkspaceUse.objects.get(request_ref=second["X-Request-ID"])
    assert (second_use.filters["page"], second_use.rows) == ("2", 53)
    use_view = client.get(reverse("audit_review:uses"))
    assert any(
        row.cells[0].note == "People" and "Page 2" in row.cells[1].text
        for row in use_view.context["page_obj"]
    )
    assert second.context["summary"].totals == client.get(sort).context["summary"].totals == totals
    [subject] = [p for p in second.context["page_obj"] if p.account_id == subjects[-1].pk]
    assert _marker_vector(subject) == (1, 1, 1)
    assert [client.get(subject.cells[i].url).context["total"] for i in (1, 2, 5)] == [1, 1, 1]
    [line] = [
        row for row in _marker_csv(client.get(export)) if row["Username"] == subjects[-1].username
    ]
    assert [line[k] for k in ("Failed or refused sign-ins", "Tickets opened", "Changes made")] == [
        "1"
    ] * 3
    assert client.get(path).context["summary"].totals["changes"] == 53


@pytest.mark.django_db
def test_people_markers_ignore_malformed_values_clamp_the_future_and_accept_zero(
    client, reviewer, agent
):
    from audit_review import sources
    from audit_review.models import Source

    client.force_login(reviewer)
    _marker_record_all(agent.pk)
    path = reverse("audit_review:people")
    keys = ["upto_changes", "upto_access", "upto_sign_ins"]
    for supplied in ("-1", "9" * 80, "１２", "²", "9" * 19, "0"):
        before = sources.newest(Source.CHANGES)
        params = dict.fromkeys(keys, supplied) | {"person": agent.pk}
        response = client.get(path, params)
        assert response.status_code == 200
        parsed = parse_qs(urlsplit(response.context["export_url"]).query)
        changes, access_mark, sign_mark = (int(parsed[k][0]) for k in keys)
        if supplied == "0":
            assert (changes, access_mark, sign_mark) == (0, 0, 0)
            assert response.context["total"] == 0
        else:
            # adapted (verification round): the Changes marker is the newest change the reading
            # holds, never later than now (people.newest_change), no longer the moment now.
            from audit_review import people

            assert changes == people.newest_change() <= sources.newest(Source.CHANGES)
            assert before > 0
            assert (access_mark, sign_mark) == (
                sources.newest(Source.ACCESS),
                sources.newest(Source.SIGN_INS),
            )
            [person] = response.context["page_obj"]
            assert _marker_vector(person) == (1, 1, 1)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("route", ["people", "people-export"])
def test_people_authorization_and_visible_counts_share_the_open_snapshot(
    client, reviewer, agent, monkeypatch, route
):
    from audit_review import views

    client.force_login(reviewer)
    _marker_record_all(agent.pk)

    def withdraw_and_record():
        try:
            with transaction.atomic():
                User.objects.get(pk=reviewer.pk).user_permissions.clear()
                _marker_record_all(agent.pk)
        finally:
            connections.close_all()

    original = views.require_reviewer

    def after_authorization(account):
        original(account)
        with ThreadPoolExecutor(max_workers=1) as writer:
            writer.submit(withdraw_and_record).result(timeout=10)

    with monkeypatch.context() as patch:
        patch.setattr(views, "require_reviewer", after_authorization)
        path = reverse(f"audit_review:{route}")
        response = client.get(path, {"person": agent.pk})
    assert response.status_code == 200
    if route == "people":
        [person] = response.context["page_obj"]
        assert _marker_vector(person) == (1, 1, 1)
    else:
        [row] = _marker_csv(response)
        assert [
            row[key] for key in ("Changes made", "Tickets opened", "Failed or refused sign-ins")
        ] == ["1", "1", "1"]
    assert client.get(path, {"person": agent.pk}).status_code == 403


_COUNT_HEADINGS = (
    "Sign-ins",
    "Failed or refused sign-ins",
    "Tickets opened",
    "Files downloaded",
    "Patient information shown",
    "Changes made",
)


def _count_csv(response):
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert "no-store" in response["Cache-Control"]
    assert response.content.startswith(codecs.BOM_UTF8)
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"), newline="")))


def test_count_links_return_the_seeded_rows_for_each_measure(
    client, reviewer, make_user, clinic, water, make_ticket
):
    from audit_review.tests.test_people import change, read, sign_in

    query = {"date_from": "2018-02-05", "date_to": "2018-02-05"}
    opened = timezone.make_aware(datetime(2018, 2, 5, 9))
    closed = opened + timedelta(hours=8)
    cal = BusinessCalendar.objects.create(
        name="Counting hours", hours={"mon": [["09:00", "17:00"]]}
    )
    query["calendar"] = cal.pk
    target = make_user("synthetic_counted", client=clinic)
    decoy = make_user("synthetic_decoy", client=water)
    quiet = make_user("synthetic_quiet")
    ticket = make_ticket(water)
    read(quiet, ticket, closed, AccessKind.COMPLAINT)
    sign_in(quiet, closed, "signed_out")
    expected = {}
    event_types = [
        ("sign-ins", "Sign-ins", "signed_in"),
        ("sign-ins", "Failed or refused sign-ins", "failed"),
        ("sign-ins", "Failed or refused sign-ins", "refused"),
        ("sign-ins", "Failed or refused sign-ins", "locked_out"),
        ("sign-ins", "Failed or refused sign-ins", "reauth_failed"),
        ("access", "Tickets opened", AccessKind.TICKET),
        ("access", "Files downloaded", AccessKind.FILE),
        ("access", "Patient information shown", AccessKind.PATIENT_ANSWER),
        ("access", "Patient information shown", AccessKind.PERSON_KEY),
        ("changes", "Changes made", None),
    ]
    for outside, moment in [(False, opened), (True, closed)]:
        for sequence, (source, label, kind) in enumerate(event_types):
            when = moment + timedelta(seconds=sequence)
            expected.setdefault((label, outside), set()).add(when)
            for account in (target, decoy):
                if source == "sign-ins":
                    sign_in(account, when, kind)
                elif source == "access":
                    read(account, ticket, when, kind)
                else:
                    change(account, ticket, when)
    for kind in (
        AccessKind.HISTORY,
        AccessKind.ACCESS,
        AccessKind.FORM_DETAILS,
        AccessKind.TIMELINE,
        AccessKind.COMPLAINT,
        AccessKind.REPEAT,
    ):
        read(target, ticket, closed, kind)
    sign_in(target, closed, "signed_out")
    for delta in (-timedelta(days=1), timedelta(days=1)):
        sign_in(target, opened + delta, "signed_in")
        read(target, ticket, closed + delta, AccessKind.PATIENT_ANSWER)
        change(target, ticket, opened + delta)
    client.force_login(reviewer)
    tab = reverse("audit_review:people")
    overview = client.get(tab, query)
    assert {r.username for r in overview.context["page_obj"]} == {target.username, decoy.username}
    overview = client.get(tab, {**query, "organization": clinic.pk})
    [all_row] = overview.context["page_obj"]
    assert all_row.username == target.username
    outside_cell = all_row.cells[-1]
    assert outside_cell.label == "Outside business hours" and outside_cell.value == len(event_types)
    outside_view = client.get(outside_cell.url)
    assert not outside_view.context["filter_form"].errors
    [outside_row] = outside_view.context["page_obj"]
    assert outside_row.username == target.username
    for only_outside, row in ((False, all_row), (True, outside_row)):
        assert [cell.label for cell in row.cells[:-1]] == list(_COUNT_HEADINGS)
        for cell in row.cells[:-1]:
            moments = expected[(cell.label, True)]
            if not only_outside:
                moments = moments | expected[(cell.label, False)]
            assert cell.value == len(moments)
            address = next(source for source, label, _ in event_types if label == cell.label)
            assert urlsplit(cell.url).path == reverse(f"audit_review:{address}")
            result = client.get(cell.url)
            assert not result.context["filter_form"].errors
            assert result.context["total"] == len(moments)
            assert {(r.username, r.occurred_at) for r in result.context["page_obj"]} == {
                (target.username, moment) for moment in moments
            }
    assert outside_row.outside == sum(cell.value for cell in outside_row.cells[:-1])
    target.client = water
    target.first_name = "Moved"
    target.save(update_fields=["client", "first_name"])
    assert client.get(tab, {**query, "organization": clinic.pk}).context["total"] == 0
    [moved] = client.get(tab, {**query, "organization": water.pk, "person": target.pk}).context[
        "page_obj"
    ]
    assert (moved.name, moved.organization) == ("Moved", water.name)


def test_scheduled_job_changes_drill_down_and_deleted_accounts_remain_unlinked(
    client, reviewer, make_user
):
    from audit_review.tests.test_people import change, sign_in
    from core.models import AccessRecord

    day = "2018-02-05"
    when = timezone.make_aware(datetime(2018, 2, 5, 7))
    runner = system_account()
    change(runner, runner, when)
    removed = make_user("synthetic_departed")
    removed_id = removed.pk
    sign_in(removed, when, "failed")
    AccessRecord.objects.create(
        account=removed, ticket_id=4, capacity="laboratory", kind="ticket", occurred_at=when
    )
    removed.delete()
    cal = BusinessCalendar.objects.create(name="Jobs hours", hours={"mon": [["09:00", "17:00"]]})
    client.force_login(reviewer)
    tab = client.get(
        reverse("audit_review:people"), {"date_from": day, "date_to": day, "calendar": cal.pk}
    )
    rows = {row.account_id: row for row in tab.context["page_obj"]}
    assert set(rows) == {runner.pk, removed_id}
    gone = rows[removed_id]
    assert (gone.name, gone.username) == (f"Account {removed_id}", "no longer exists")
    assert [cell.value for cell in gone.cells] == [0, 1, 1, 0, 0, 0, 2]
    assert all(not cell.url for cell in gone.cells)
    assert rows[runner.pk].name == "Scheduled jobs"
    [event] = client.get(rows[runner.pk].cells[5].url).context["page_obj"]
    assert (event.person, event.username, event.occurred_at) == (
        "Scheduled jobs",
        runner.username,
        when,
    )


@pytest.mark.parametrize("only_outside", [False, True])
def test_full_csv_matches_order_totals_headings_and_text_cells_on_both_pages(
    client, reviewer, clinic, only_outside
):
    from accounts.models import User

    when = timezone.make_aware(datetime(2018, 2, 5, 8))
    day = "2018-02-05"
    cal = BusinessCalendar.objects.create(name='=weekday, "É"', hours={"mon": [["09:00", "17:00"]]})
    clinic.name = ' +labo, "Été"'
    clinic.save(update_fields=["name"])
    names = [(f"person{i:02d}", f"Person {i:02d}") for i in range(53)] + [("@review", "=Émilie")]
    accounts = User.objects.bulk_create(
        User(username=name, first_name=display, email=f"member{i}@example.invalid", client=clinic)
        for i, (name, display) in enumerate(names)
    )
    expected = {}
    events = []
    for i, account in enumerate(accounts):
        outside = i % 4 + 1
        expected[account.username] = (outside if only_outside else outside + 1, outside)
        events.extend(
            SignInEvent(user_id=account.pk, kind="signed_in", occurred_at=when)
            for _ in range(outside)
        )
        events.append(
            SignInEvent(user_id=account.pk, kind="signed_in", occurred_at=when + timedelta(hours=2))
        )
    SignInEvent.objects.bulk_create(events)
    client.force_login(reviewer)
    url = reverse("audit_review:people")
    query = {"date_from": day, "date_to": day, "calendar": cal.pk, "sort": "-sign_ins"}
    if only_outside:
        query["activity"] = "outside"
    first = client.get(url, query)
    second = client.get(url, {**query, "page": "2"})
    pages = [first.context["page_obj"], second.context["page_obj"]]
    assert [len(p) for p in pages] == [50, 4]
    shown = [row for p in pages for row in p]
    want_order = sorted(names, key=lambda item: (-expected[item[0]][0], item[1].casefold()))
    assert [person.username for person in shown] == [name for name, display in want_order]
    for result in (first, second):
        summary = result.context["summary"]
        assert summary.measure_totals == [sum(v[0] for v in expected.values()), 0, 0, 0, 0, 0]
        assert summary.totals["outside"] == sum(v[1] for v in expected.values())
    response = client.get(second.context["export_url"])
    assert re.fullmatch(
        r'attachment; filename="audit-review-people-20180205-20180205-\d{8}-\d{6}\.csv"',
        response["Content-Disposition"],
    )
    header, *lines = _count_csv(response)
    suffix = " (outside business hours)" if only_outside else ""
    assert header == [
        "From",
        "To",
        "Person",
        "Username",
        "Client organization",
        *(x + suffix for x in _COUNT_HEADINGS),
        f"Outside the hours of {cal.name}",
    ]
    assert len(lines) == len(shown)
    for row, line in zip(shown, lines, strict=True):
        name = "'=Émilie" if row.username == "@review" else row.name
        username = "'@review" if row.username == "@review" else row.username
        signed_in, outside = expected[row.username]
        assert [c.value for c in row.cells] == [signed_in, 0, 0, 0, 0, 0, outside]
        assert line == [
            day,
            day,
            name,
            username,
            "'" + clinic.name,
            str(signed_in),
            "0",
            "0",
            "0",
            "0",
            "0",
            str(outside),
        ]


def test_people_export_has_no_list_row_limit(client, reviewer):
    when = timezone.make_aware(datetime(2018, 2, 5, 8))
    retired_ids = range(2_000_000, 2_010_001)
    SignInEvent.objects.bulk_create(
        SignInEvent(user_id=account_id, kind="signed_in", occurred_at=when)
        for account_id in retired_ids
    )
    client.force_login(reviewer)
    response = client.get(
        reverse("audit_review:people-export"),
        {"date_from": "2018-02-05", "date_to": "2018-02-05", "calendar": "none"},
    )
    header, *lines = _count_csv(response)
    assert header[-1] == "Changes made"
    assert len(lines) == len(retired_ids) > 10_000
    assert {row[2] for row in lines} == {f"Account {n}" for n in retired_ids}
    assert all(row[5:] == ["1", "0", "0", "0", "0", "0"] for row in lines)


def test_sort_by_each_count_uses_its_own_values_and_orders_ties_by_name_then_username(
    client, reviewer, clinic, make_ticket, make_user
):
    from audit_review.tests.test_people import change, read, sign_in

    ticket = make_ticket(clinic)
    when = timezone.make_aware(datetime(2018, 2, 5, 7))
    cal = BusinessCalendar.objects.create(name="Sort hours", hours={"mon": [["09:00", "17:00"]]})
    columns = ("sign_ins", "failed", "tickets", "downloads", "shown", "changes", "outside")
    data = [
        ("user3", "Rae", [1, 2, 1, 3, 1, 1]),
        ("user2", "Rae", [1, 1, 3, 2, 2, 2]),
        ("user1", "Abe", [2, 1, 2, 1, 2, 3]),
    ]
    expected = {}
    for username, display, values in data:
        account = make_user(username, first_name=display)
        for key, num in zip(columns[:-1], values, strict=True):
            for _ in range(num):
                if key == "changes":
                    change(account, ticket, when)
                elif key in ("sign_ins", "failed"):
                    sign_in(account, when, "signed_in" if key == "sign_ins" else "failed")
                else:
                    read(
                        account,
                        ticket,
                        when,
                        {
                            "tickets": AccessKind.TICKET,
                            "downloads": AccessKind.FILE,
                            "shown": AccessKind.PATIENT_ANSWER,
                        }[key],
                    )
        expected[username] = dict(zip(columns, [*values, sum(values)], strict=True))
    client.force_login(reviewer)
    query = {"date_from": "2018-02-05", "date_to": "2018-02-05", "calendar": cal.pk}
    tab = reverse("audit_review:people")
    names = [username for username, _, _ in sorted(data, key=lambda row: (row[1], row[0]))]
    for direction in ("", "-"):
        for key in ("person", *columns):
            response = client.get(tab, {**query, "sort": direction + key})
            if key == "person":
                ordered = names[::-1] if direction else names
            else:
                ordered = sorted(
                    names, key=lambda name: expected[name][key], reverse=bool(direction)
                )
            assert [r.username for r in response.context["page_obj"]] == ordered, direction + key
