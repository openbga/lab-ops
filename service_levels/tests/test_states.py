"""The SQL clock state agrees with the Python state (S3), over every combination of clock shapes.

One request; its clock row is rewritten for each case and read back through
annotate_clock_state, then compared with rules.request_state on the same values.
"""

from datetime import datetime, timedelta
from itertools import product

import pytest

from desk.models import ServiceRequest
from service_levels.models import RequestClock
from service_levels.rules import STATES, request_state
from service_levels.selectors import annotate_clock_state, filter_clock_state

from .conftest import NY

NOW = datetime(2026, 11, 16, 12, tzinfo=NY)
M = timedelta(minutes=1)
H = timedelta(hours=1)

# (due_at, met_at, missed, minutes) for the first-response clock.
FIRST = {
    "none": (None, None, False, None),
    "far": (NOW + 5 * H, None, False, 60),
    "soon": (NOW + 30 * M, None, False, 60),
    "edge": (NOW + H, None, False, 60),  # exactly the threshold left: on track
    "due_now": (NOW, None, False, 60),
    "overdue": (NOW - M, None, False, 60),
    "met": (NOW + H, NOW - H, False, 60),
    "met_late": (NOW - 2 * H, NOW - H, True, 60),
    "missed_moved": (NOW + 5 * H, None, True, 240),
    "no_minutes": (NOW + 50 * M, None, False, None),
}
# (due_at, met_at, missed, minutes, paused_since) for the resolution clock.
RESOLUTION = {
    "none": (None, None, False, None, None),
    "far": (NOW + 30 * H, None, False, 600, None),
    "soon_fifth": (NOW + 90 * M, None, False, 600, None),  # 2 h threshold
    "not_soon_fifth": (NOW + 3 * H, None, False, 600, None),
    "overdue": (NOW - H, None, True, 600, None),
    "paused": (NOW - H, None, False, 600, NOW - 2 * H),
    "paused_missed": (NOW - H, None, True, 600, NOW - 2 * H),
    "met": (NOW + H, NOW - H, False, 600, None),
    "met_late": (NOW - 2 * H, NOW - H, True, 600, None),
    "missed_moved": (NOW + 30 * H, None, True, 600, None),
}
NOWS = [NOW, NOW + 2 * H, NOW - 3 * H]


@pytest.fixture
def request_row(frozen, new_request):
    return new_request()


def _write(request, first, resolution):
    fr_due, fr_met, fr_missed, fr_minutes = FIRST[first]
    re_due, re_met, re_missed, re_minutes, paused = RESOLUTION[resolution]
    RequestClock.objects.update_or_create(
        request=request,
        defaults={
            "first_response_due_at": fr_due,
            "first_response_met_at": fr_met,
            "first_response_missed": fr_missed,
            "first_response_minutes": fr_minutes,
            "resolution_due_at": re_due,
            "resolution_met_at": re_met,
            "resolution_missed": re_missed,
            "resolution_minutes": re_minutes,
            "paused_since": paused,
        },
    )
    return RequestClock.objects.get(request=request)


def test_sql_state_agrees_with_python_for_every_case(request_row):
    seen = set()
    for first, resolution in product(FIRST, RESOLUTION):
        clock = _write(request_row, first, resolution)
        for now in NOWS:
            expected = request_state(clock, now)
            row = annotate_clock_state(ServiceRequest.objects.filter(pk=request_row.pk), now).get()
            assert row.clock_state == expected, (first, resolution, now)
            seen.add(expected)
            dues = [
                due
                for due, met, paused in (
                    (clock.first_response_due_at, clock.first_response_met_at, None),
                    (clock.resolution_due_at, clock.resolution_met_at, clock.paused_since),
                )
                if due is not None and met is None and paused is None
            ]
            assert row.clock_due_at == (min(dues) if dues else None), (first, resolution)
    assert seen == set(STATES)  # the table reaches every state


def test_request_without_a_clock_is_none(request_row):
    row = annotate_clock_state(ServiceRequest.objects.filter(pk=request_row.pk), NOW).get()
    assert row.clock_state == "none" and row.clock_due_at is None


def test_filter_by_state(request_row, new_request):
    other = new_request()
    _write(request_row, "overdue", "none")
    _write(other, "met", "met")
    queryset = ServiceRequest.objects.all()
    assert list(filter_clock_state(queryset, "overdue", NOW).values_list("pk", flat=True)) == [
        request_row.pk
    ]
    assert list(filter_clock_state(queryset, "met", NOW).values_list("pk", flat=True)) == [other.pk]
    with pytest.raises(ValueError):
        filter_clock_state(queryset, "late", NOW)


def test_annotation_costs_one_query(request_row, new_request, django_assert_num_queries):
    for _ in range(30):
        _write(new_request(), "soon", "paused")
    with django_assert_num_queries(1):
        states = list(
            annotate_clock_state(ServiceRequest.objects.all(), NOW).values_list(
                "clock_state", flat=True
            )
        )
    assert states.count("due_soon") == 30
