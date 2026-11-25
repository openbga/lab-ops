"""Changed-line regression checks for the People cutoff and deferred preference actions."""

import csv
import inspect
import io
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
from django.db import connections, transaction
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from audit_review import sources
from audit_review.tests.conftest import agent as agent
from audit_review.tests.conftest import make_user as make_user
from audit_review.tests.conftest import reviewer as reviewer
from core import access
from core.models import AccessRecord, AuditEvent

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
US = timedelta(microseconds=1)


def add_change(account_id):
    return AuditEvent.objects.create(
        actor_id=account_id,
        action="account.updated",
        object_type="accounts.user",
        object_id=str(account_id),
    )


def add_at(account_id, moment, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(timezone, "now", lambda: moment)
        return add_change(account_id)


def marker_from(response):
    return int(parse_qs(urlsplit(response.context["export_url"]).query)["upto_changes"][0])


def changes_in_csv(client, address):
    response = client.get(address)
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    return sum(int(row["Changes made"]) for row in rows)


CLOCKS_DIFFER = pytest.mark.xfail(
    strict=True,
    reason="documented limit (second confirmation): changes are timed by the clock of the server "
    "that saved them; where it runs ahead of this one, a change begun after the first page can "
    "be counted on a later page, its export or its list, as with a clock running behind",
)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "sample_offset_ms",
    [
        9,
        pytest.param(10, marks=CLOCKS_DIFFER),
        pytest.param(11, marks=CLOCKS_DIFFER),
    ],
    ids=["still-ahead", "at-event-time", "past-event-time"],
)
def test_post_snapshot_writer_stays_out_when_an_earlier_fast_timestamp_becomes_current(
    client, agent, reviewer, monkeypatch, sample_offset_ms
):
    from audit_review import views

    client.force_login(reviewer)
    start = timezone.now() - timedelta(minutes=1)
    ahead = start + timedelta(milliseconds=10)
    add_at(agent.pk, start - timedelta(seconds=1), monkeypatch)
    add_at(reviewer.pk, ahead, monkeypatch)  # already committed, from a different server

    # The reader and the new writer share the same advancing clock. At the snapshot it
    # reads start, at the write it reads start + 5 ms, and at the marker query 9, 10 or 11 ms.
    now = start
    original = views.reading_account

    def writer():
        try:
            with transaction.atomic():
                return add_change(agent.pk).occurred_at
        finally:
            connections.close_all()

    def snapshot(request):
        nonlocal now
        account = original(request)
        now = start + timedelta(milliseconds=5)
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(writer).result(timeout=10) == now
        now = start + timedelta(milliseconds=sample_offset_ms)
        return account

    params = {
        "person": agent.pk,
        "date_from": start.date() - timedelta(days=1),
        "date_to": start.date() + timedelta(days=1),
    }
    path = reverse("audit_review:people")
    with monkeypatch.context() as patch:
        patch.setattr(timezone, "now", lambda: now)
        patch.setattr(views, "reading_account", snapshot)
        first = client.get(path, params)
    assert first.status_code == 200
    [person] = first.context["page_obj"]
    cell = next(cell for cell in person.cells if cell.label == "Changes made")
    assert cell.value == 1
    assert client.get(path, params).context["summary"].totals["changes"] == 2
    csv_count = changes_in_csv(client, first.context["export_url"])
    drilldown_count = client.get(cell.url).context["total"]
    assert (csv_count, drilldown_count) == (cell.value, cell.value)


@pytest.mark.parametrize(
    "moment",
    [
        datetime.min.replace(tzinfo=UTC),
        datetime(1600, 4, 2, 12, microsecond=1, tzinfo=UTC),
        EPOCH - US,
        EPOCH,
        EPOCH + US,
        datetime(2024, 11, 3, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=0),
        datetime(2024, 11, 3, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=1),
        datetime(2600, 4, 2, 12, microsecond=1, tzinfo=UTC),
        datetime.max.replace(tzinfo=UTC),
    ],
    ids=[
        "min",
        "1600",
        "epoch-minus-one",
        "epoch",
        "epoch-plus-one",
        "first-fold",
        "second-fold",
        "2600",
        "max",
    ],
)
def test_change_marker_round_trips_each_microsecond(moment):
    instant = moment.astimezone(UTC)
    delta = instant - EPOCH
    expected = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    marker = sources.to_marker(moment)
    assert type(marker) is int and marker == expected
    assert sources.from_marker(marker) == instant


@pytest.mark.parametrize("include_epoch", [False, True])
def test_epoch_floor_keeps_historical_rows_and_excludes_the_next_microsecond(
    client, reviewer, monkeypatch, include_epoch
):
    client.force_login(reviewer)
    assert not AuditEvent.objects.exists()
    add_at(reviewer.pk, EPOCH - US, monkeypatch)
    if include_epoch:
        add_at(reviewer.pk, EPOCH, monkeypatch)
    dates = {"date_from": "1969-12-30", "date_to": "1970-01-02"}
    path = reverse("audit_review:people")
    first = client.get(path, dates)
    assert marker_from(first) == 0
    [subject] = first.context["page_obj"]
    cell = next(cell for cell in subject.cells if cell.label == "Changes made")
    assert cell.value == 1 + int(include_epoch)
    add_at(reviewer.pk, EPOCH + US, monkeypatch)
    assert client.get(cell.url).context["total"] == cell.value
    assert changes_in_csv(client, first.context["export_url"]) == cell.value
    fresh = client.get(path, dates)
    assert marker_from(fresh) == 1 and fresh.context["summary"].totals["changes"] == cell.value + 1


def test_fresh_reading_admits_committed_future_events_as_each_timestamp_arrives(
    client, reviewer, monkeypatch
):
    client.force_login(reviewer)
    start = timezone.now() - timedelta(minutes=2)
    assert not AuditEvent.objects.exists()
    instants = [start + 10 * US, start + 10 * US, start + 20 * US]
    for moment in instants:
        add_at(reviewer.pk, moment, monkeypatch)
    dates = {
        "date_from": start.date() - timedelta(days=1),
        "date_to": start.date() + timedelta(days=1),
    }
    pinned = []
    for offset, count in [(9, 0), (10, 2), (19, 2), (20, 3)]:
        with monkeypatch.context() as patch:
            patch.setattr(timezone, "now", lambda instant=start + offset * US: instant)
            reply = client.get(reverse("audit_review:people"), dates)
        assert reply.context["summary"].totals["changes"] == count
        assert marker_from(reply) == (sources.to_marker(instants[count - 1]) if count else 0)
        pinned.append((reply.context["export_url"], count))
    for address, expected in pinned:
        assert changes_in_csv(client, address) == expected


@pytest.mark.parametrize(
    ("method", "status", "records", "actions"),
    [
        ("GET", 200, 1, 2),
        ("GET", 302, 1, 2),
        ("GET", 399, 1, 2),
        ("GET", 400, 1, 0),
        ("GET", 422, 1, 0),
        ("GET", 499, 1, 0),
        ("GET", 500, 0, 0),
        ("HEAD", 200, 0, 0),
    ],
)
def test_deferred_actions_follow_final_status_while_records_follow_the_carried_response(
    rf, reviewer, method, status, records, actions
):
    request = rf.generic(method, "/")
    request.user = reviewer
    ticket_id = 4482
    rows = AccessRecord.objects.filter(account_id=reviewer.pk, ticket_id=ticket_id)
    observed = []

    def view(req):
        access.note(req, "ticket", ticket_id=ticket_id)
        return HttpResponse("Synthetic ticket")

    def prepare(req):
        reply = access.AccessBindMiddleware(view)(req)
        assert access.carries(reply)
        access.after_written(reply, lambda: observed.append(("first", rows.count())))
        access.after_written(reply, lambda: observed.append(("second", rows.count())))
        reply.status_code = status
        return reply

    reply = access.AccessRecordMiddleware(prepare)(request)
    assert reply.status_code == status
    assert not access.carries(reply)
    assert rows.count() == records
    assert observed == [("first", 1), ("second", 1)][:actions]


def test_after_written_documents_that_a_written_4xx_record_skips_the_action(rf, reviewer):
    request = rf.get("/")
    request.user = reviewer
    ran = []

    def view(req):
        access.note(req, "ticket", ticket_id=714)
        return HttpResponse("Synthetic validation form", status=422)

    def register(req):
        response = access.AccessBindMiddleware(view)(req)
        access.after_written(response, lambda: ran.append(True))
        return response

    reply = access.AccessRecordMiddleware(register)(request)
    assert reply.status_code == 422
    assert AccessRecord.objects.filter(account=reviewer, ticket_id=714).exists()
    assert not ran
    # The helper's callers need both conditions: a successful write and a qualifying response.
    contract = inspect.getdoc(access.after_written)
    assert re.search(
        r"\b(?:4xx|400|client error|successful (?:response|answer)|success status)\b",
        contract,
        re.I,
    ), contract


def test_changes_list_and_export_keep_the_exact_boundary_microsecond(client, reviewer, monkeypatch):
    from unittest.mock import Mock

    client.force_login(reviewer)
    moment = datetime(2600, 8, 12, 12, microsecond=1, tzinfo=UTC)
    for instant in (moment - US, moment, moment, moment + US):
        add_at(reviewer.pk, instant, monkeypatch)

    # Set only the Changes list's application clock. Authentication uses its ordinary clock.
    clock = Mock(wraps=timezone)
    monkeypatch.setattr(sources, "timezone", clock)
    clock.now.return_value = moment
    path = reverse("audit_review:changes")
    first = client.get(path, {"person": reviewer.pk})
    assert first.context["total"] == 3
    clock.now.return_value = moment + US
    assert client.get(path, {"person": reviewer.pk}).context["total"] == 4
    exported = client.get(first.context["export_url"])
    assert exported.status_code == 200
    assert len(list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))) == 3
