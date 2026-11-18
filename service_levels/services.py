"""Changes to business calendars, holidays and service targets (S1, S2). Admin only.

Each change locks the record, rechecks its revision, and records an attributed audit event in
the same transaction. Edits apply to requests created or recalculated afterwards; running clocks
keep the values they copied (`clocks.py`).
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from accounts.roles import require_role
from core.audit import record_event, snapshot

from .models import BusinessCalendar, Holiday, ServiceTarget

SETUP_ROLE = "admin"
CALENDAR_FIELDS = ("name", "hours", "is_active")
TARGET_FIELDS = (
    "name",
    "category",
    "priority",
    "first_response_minutes",
    "resolution_minutes",
    "calendar",
    "pause_on_third_party",
    "is_active",
)


def require_setup(user):
    require_role(user, SETUP_ROLE)


def _reason(reason):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required."})
    return reason


def _stale(what):
    return ValidationError(f"This {what} changed after you opened it. Reload and review it.")


def _save(obj, message, field=None):
    try:
        with transaction.atomic():
            obj.save()
    except IntegrityError as error:
        raise ValidationError({field: message} if field else message) from error


# Calendars ---------------------------------------------------------------------------------


@transaction.atomic
def create_calendar(*, actor, reason="", **data):
    require_setup(actor)
    obj = BusinessCalendar(**{k: v for k, v in data.items() if k in CALENDAR_FIELDS})
    obj.name = (obj.name or "").strip()
    obj.full_clean()
    _save(obj, "Another calendar has this name.", "name")
    record_event(
        actor=actor,
        action="calendar.created",
        obj=obj,
        after=snapshot(obj),
        reason=(reason or "").strip(),
    )
    return obj


@transaction.atomic
def update_calendar(*, actor, calendar_id, expected_revision, reason, **data):
    require_setup(actor)
    reason = _reason(reason)
    obj = BusinessCalendar.objects.select_for_update().get(pk=calendar_id)
    if obj.revision != expected_revision:
        raise _stale("calendar")
    before = snapshot(obj)
    for name in CALENDAR_FIELDS:
        if name in data:
            setattr(obj, name, data[name])
    obj.name = (obj.name or "").strip()
    obj.full_clean()
    if snapshot(obj) == before:
        raise ValidationError("Change at least one detail before saving.")
    if not obj.is_active and obj.targets.filter(is_active=True).exists():
        raise ValidationError(
            {"is_active": "Active service targets use this calendar. Change them first."}
        )
    obj.revision += 1
    _save(obj, "Another calendar has this name.", "name")
    record_event(
        actor=actor,
        action="calendar.updated",
        obj=obj,
        before=before,
        after=snapshot(obj),
        reason=reason,
    )
    return obj


@transaction.atomic
def add_holiday(*, actor, calendar_id, date, name):
    require_setup(actor)
    calendar = BusinessCalendar.objects.select_for_update().get(pk=calendar_id)
    holiday = Holiday(calendar=calendar, date=date, name=(name or "").strip())
    holiday.full_clean(validate_constraints=False)
    _save(holiday, "This calendar already has a holiday on that date.", "date")
    record_event(actor=actor, action="holiday.added", obj=holiday, after=snapshot(holiday))
    return holiday


@transaction.atomic
def remove_holiday(*, actor, holiday_id, reason):
    require_setup(actor)
    reason = _reason(reason)
    holiday = Holiday.objects.select_for_update().get(pk=holiday_id)
    BusinessCalendar.objects.select_for_update().get(pk=holiday.calendar_id)
    before = snapshot(holiday)
    record_event(actor=actor, action="holiday.removed", obj=holiday, before=before, reason=reason)
    holiday.delete()


# Targets -----------------------------------------------------------------------------------

DUPLICATE_SCOPE = (
    "An active target already covers this category and priority. Change that one, or "
    "deactivate it first."
)


def _check_target(obj, before=None):
    if obj.first_response_minutes is None and obj.resolution_minutes is None:
        raise ValidationError("Give a first-response time, a resolution time, or both.")
    before = before or {}
    if obj.category is not None and not obj.category.is_active:
        if before.get("category") != obj.category_id:
            raise ValidationError({"category": "Choose an active category or group."})
    if obj.calendar is not None and not obj.calendar.is_active:
        if before.get("calendar") != obj.calendar_id:
            raise ValidationError({"calendar": "Choose an active calendar."})
    if obj.is_active:
        clash = ServiceTarget.objects.filter(
            category_id=obj.category_id, priority=obj.priority, is_active=True
        ).exclude(pk=obj.pk)
        if clash.exists():
            raise ValidationError(DUPLICATE_SCOPE)


@transaction.atomic
def create_target(*, actor, reason="", **data):
    require_setup(actor)
    obj = ServiceTarget(**{k: v for k, v in data.items() if k in TARGET_FIELDS})
    obj.name = (obj.name or "").strip()
    obj.priority = obj.priority or ""
    obj.full_clean(validate_constraints=False)
    _check_target(obj)
    _save(obj, DUPLICATE_SCOPE)
    record_event(
        actor=actor,
        action="target.created",
        obj=obj,
        after=snapshot(obj),
        reason=(reason or "").strip(),
    )
    return obj


@transaction.atomic
def update_target(*, actor, target_id, expected_revision, reason, **data):
    require_setup(actor)
    reason = _reason(reason)
    obj = ServiceTarget.objects.select_for_update().get(pk=target_id)
    if obj.revision != expected_revision:
        raise _stale("service target")
    before = snapshot(obj)
    for name in TARGET_FIELDS:
        if name in data:
            setattr(obj, name, data[name])
    obj.name = (obj.name or "").strip()
    obj.priority = obj.priority or ""
    obj.full_clean(validate_constraints=False)
    if snapshot(obj) == before:
        raise ValidationError("Change at least one detail before saving.")
    _check_target(obj, before)
    obj.revision += 1
    _save(obj, DUPLICATE_SCOPE)
    record_event(
        actor=actor,
        action="target.updated",
        obj=obj,
        before=before,
        after=snapshot(obj),
        reason=reason,
    )
    return obj
