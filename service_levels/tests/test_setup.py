"""Business calendars, holidays and service targets (S1, S2): services, constraints and pages."""

from datetime import date

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from core.models import AuditEvent
from service_levels.forms import CalendarForm, TargetForm, parse_day_text
from service_levels.models import BusinessCalendar, ServiceTarget
from service_levels.services import (
    add_holiday,
    create_calendar,
    create_target,
    remove_holiday,
    update_calendar,
    update_target,
)

from .conftest import PASSWORD

HOURS = {"mon": [["08:00", "17:00"]]}


# S1 ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "intervals"),
    [
        ("07:00-19:00", ((420, 1140),)),
        ("7-19", ((420, 1140),)),
        ("0700-1900", ((420, 1140),)),
        ("08:00 to 12:00, 13:00–17:00", ((480, 720), (780, 1020))),
        ("22:00-24:00", ((1320, 1440),)),
        ("", ()),
    ],
)
def test_typed_hours(text, intervals):
    assert parse_day_text(text) == intervals


@pytest.mark.parametrize(
    "text",
    [
        "17:00-08:00",
        "08:00-12:00, 11:00-14:00",
        "13:00-17:00, 08:00-12:00",
        "8am-5pm",
        "08:00",
        "25-26",
    ],
)
def test_calendar_form_refuses_bad_hours_on_the_day(db, text):
    form = CalendarForm({"name": "Lab hours", "hours_tue": text})
    assert not form.is_valid()
    assert "hours_tue" in form.errors


def test_calendar_needs_one_opening_interval(db):
    form = CalendarForm({"name": "Closed"})
    assert not form.is_valid()
    assert "at least one opening interval" in str(form.errors)
    with pytest.raises(ValidationError):
        BusinessCalendar(name="Closed", hours={}).full_clean()


def test_calendar_create_and_update_are_admin_only_audited_and_revision_checked(admin, agent):
    with pytest.raises(PermissionDenied):
        create_calendar(actor=agent, name="Lab hours", hours=HOURS)
    calendar = create_calendar(actor=admin, name="Lab hours", hours=HOURS)
    assert AuditEvent.objects.filter(action="calendar.created", object_id=str(calendar.pk)).exists()
    update_calendar(
        actor=admin,
        calendar_id=calendar.pk,
        expected_revision=1,
        reason="Saturday mornings",
        hours={**HOURS, "sat": [["08:00", "12:00"]]},
    )
    with pytest.raises(ValidationError, match="changed after you opened it"):
        update_calendar(
            actor=admin, calendar_id=calendar.pk, expected_revision=1, reason="Stale", name="X"
        )
    with pytest.raises(ValidationError):
        update_calendar(
            actor=admin, calendar_id=calendar.pk, expected_revision=2, reason="", name="X"
        )
    event = AuditEvent.objects.get(action="calendar.updated")
    assert event.reason == "Saturday mornings" and "sat" in event.after["hours"]


def test_holidays_belong_to_one_calendar_and_are_unique_per_date(admin):
    first = create_calendar(actor=admin, name="Main", hours=HOURS)
    second = create_calendar(actor=admin, name="Night", hours=HOURS)
    add_holiday(actor=admin, calendar_id=first.pk, date=date(2026, 12, 25), name="Example holiday")
    add_holiday(actor=admin, calendar_id=second.pk, date=date(2026, 12, 25), name="Example holiday")
    with pytest.raises(ValidationError, match="already has a holiday"):
        add_holiday(actor=admin, calendar_id=first.pk, date=date(2026, 12, 25), name="Again")
    assert first.as_calendar().holidays == frozenset({date(2026, 12, 25)})
    holiday = first.holidays.get()
    with pytest.raises(ValidationError):
        remove_holiday(actor=admin, holiday_id=holiday.pk, reason="")
    remove_holiday(actor=admin, holiday_id=holiday.pk, reason="Entered on the wrong calendar")
    assert not first.holidays.exists() and second.holidays.exists()
    assert AuditEvent.objects.filter(action="holiday.removed").get().before["date"] == "2026-12-25"


def test_calendar_in_use_cannot_be_deactivated(admin):
    calendar = create_calendar(actor=admin, name="Main", hours=HOURS)
    create_target(actor=admin, name="All", resolution_minutes=60, calendar=calendar)
    with pytest.raises(ValidationError, match="Active service targets use this calendar"):
        update_calendar(
            actor=admin,
            calendar_id=calendar.pk,
            expected_revision=1,
            reason="Retire",
            is_active=False,
        )


# S2 ----------------------------------------------------------------------------------------


def test_target_needs_a_time(admin, db):
    with pytest.raises(ValidationError, match="first-response time, a resolution time"):
        create_target(actor=admin, name="Nothing")
    with pytest.raises(IntegrityError), transaction.atomic():
        ServiceTarget.objects.create(name="Nothing")
    form = TargetForm(
        {"name": "Nothing", "first_response_unit": "hours", "resolution_unit": "hours"}
    )
    assert not form.is_valid()


@pytest.mark.parametrize(
    "scope",
    [
        {},  # any category, any priority
        {"priority": "high"},
        {"category": "group"},
        {"category": "category", "priority": "urgent"},
    ],
)
def test_one_active_target_per_scope_including_any_values(admin, group, category, scope):
    values = {"group": group, "category": category}
    data = {k: values.get(v, v) for k, v in scope.items()}
    first = create_target(actor=admin, name="First", resolution_minutes=60, **data)
    with pytest.raises(ValidationError, match="already covers"):
        create_target(actor=admin, name="Second", resolution_minutes=30, **data)
    with pytest.raises(IntegrityError), transaction.atomic():
        ServiceTarget.objects.create(name="Raw", resolution_minutes=30, **data)
    # An inactive target does not block a new one.
    update_target(
        actor=admin, target_id=first.pk, expected_revision=1, reason="Replaced", is_active=False
    )
    create_target(actor=admin, name="Second", resolution_minutes=30, **data)


def test_target_form_converts_hours_and_minutes(db, category):
    form = TargetForm(
        {
            "name": "Urgent",
            "priority": "urgent",
            "first_response_time": "30",
            "first_response_unit": "minutes",
            "resolution_time": "4",
            "resolution_unit": "hours",
        }
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["first_response_minutes"] == 30
    assert form.cleaned_data["resolution_minutes"] == 240
    bad = TargetForm(
        {
            "name": "x",
            "resolution_time": "1.5",
            "resolution_unit": "hours",
            "first_response_unit": "hours",
        }
    )
    assert not bad.is_valid() and "resolution_time" in bad.errors


def test_target_update_is_admin_only_audited_with_reason_and_refuses_stale(admin, agent):
    with pytest.raises(PermissionDenied):
        create_target(actor=agent, name="x", resolution_minutes=1)
    target = create_target(actor=admin, name="All", resolution_minutes=60)
    with pytest.raises(PermissionDenied):
        update_target(actor=agent, target_id=target.pk, expected_revision=1, reason="x")
    update_target(
        actor=admin,
        target_id=target.pk,
        expected_revision=1,
        reason="Faster",
        resolution_minutes=30,
    )
    with pytest.raises(ValidationError):
        update_target(
            actor=admin,
            target_id=target.pk,
            expected_revision=1,
            reason="Stale",
            resolution_minutes=20,
        )
    assert AuditEvent.objects.get(action="target.updated").reason == "Faster"


# Pages -------------------------------------------------------------------------------------

PAGES = [
    "service_levels:target-list",
    "service_levels:target-create",
    "service_levels:calendar-list",
    "service_levels:calendar-create",
]


@pytest.mark.parametrize("name", PAGES)
def test_setup_pages_are_admin_only(client, admin, agent, name):
    client.login(username=agent.username, password=PASSWORD)
    assert client.get(reverse(name)).status_code == 403
    client.login(username=admin.username, password=PASSWORD)
    assert client.get(reverse(name)).status_code == 200


def test_calendar_pages_render_and_save(client, admin):
    client.login(username=admin.username, password=PASSWORD)
    response = client.post(
        reverse("service_levels:calendar-create"),
        {"name": "Laboratory hours", "hours_mon": "7-19", "hours_sat": "08:00-12:00"},
    )
    calendar = BusinessCalendar.objects.get()
    assert response.status_code == 302
    assert calendar.hours == {"mon": [["07:00", "19:00"]], "sat": [["08:00", "12:00"]]}
    page = client.get(reverse("service_levels:calendar-detail", args=[calendar.pk]))
    assert b"07:00-19:00" in page.content
    response = client.post(
        reverse("service_levels:holiday-create", args=[calendar.pk]),
        {"date": "2026-12-25", "name": "Example holiday"},
    )
    assert response.status_code == 302 and calendar.holidays.count() == 1
    response = client.post(
        reverse("service_levels:target-create"),
        {
            "name": "Urgent",
            "priority": "urgent",
            "first_response_time": "30",
            "first_response_unit": "minutes",
            "resolution_time": "4",
            "resolution_unit": "hours",
            "calendar": calendar.pk,
        },
    )
    assert response.status_code == 302, response.content
    assert client.get(reverse("service_levels:target-list")).status_code == 200
