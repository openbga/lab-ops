"""Setup pages for service targets and business calendars (S1, S2). Admin role only; the services
check again. Request clocks render through the slot templates (`_request_clock.html`,
`_clock_badge.html`) on the desk's pages."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from core.lists import list_response
from core.models import AuditEvent

from .forms import (
    CalendarEditForm,
    CalendarForm,
    HolidayForm,
    HolidayRemoveForm,
    TargetEditForm,
    TargetFilter,
    TargetForm,
)
from .models import BusinessCalendar, Holiday, ServiceTarget
from .rules import PRECEDENCE
from .services import (
    add_holiday,
    create_calendar,
    create_target,
    remove_holiday,
    require_setup,
    update_calendar,
    update_target,
)

TARGETS_TITLE = "Service targets"
CALENDARS_TITLE = "Business calendars"
TARGET_COLUMNS = {
    "name": "name",
    "category": "category__name",
    "priority": "priority",
    "first_response": "first_response_minutes",
    "resolution": "resolution_minutes",
    "calendar": "calendar__name",
    "is_active": "is_active",
}
CALENDAR_COLUMNS = {"name": "name", "is_active": "is_active"}


def _errors(form, exc):
    if hasattr(exc, "message_dict"):
        for name, errors in exc.message_dict.items():
            form.add_error(name if name in form.fields else None, errors)
    else:
        form.add_error(None, exc)


def _history(model_label, pk):
    return (
        AuditEvent.objects.filter(object_type=model_label, object_id=str(pk))
        .select_related("actor")
        .order_by("occurred_at")
    )


def _form(request, form, *, title, submit_label, crumbs, cancel_url, obj=None):
    return render(
        request,
        "core/record_form.html",
        {
            "page_title": title,
            "submit_label": submit_label,
            "form": form,
            "object": obj,
            "inactive_notice": "This is inactive and is not used for new tickets.",
            "crumbs": crumbs,
            "cancel_url": cancel_url,
        },
    )


# Targets -----------------------------------------------------------------------------------


@login_required
def target_list(request):
    require_setup(request.user)
    return list_response(
        request,
        queryset=ServiceTarget.objects.select_related("category", "category__parent", "calendar"),
        columns=TARGET_COLUMNS,
        default_sort="name",
        form_class=TargetFilter,
        search_fields=("name", "category__name", "calendar__name"),
        filter_fields={},
        template="service_levels/target_list.html",
        title=TARGETS_TITLE,
        placeholder="Target, category or calendar",
        inline=("active",),
        extra={"precedence": PRECEDENCE},
    )


@login_required
def target_create(request):
    require_setup(request.user)
    form = TargetForm(request.POST or None)
    list_url = reverse("service_levels:target-list")
    if request.method == "POST" and form.is_valid():
        try:
            target = create_target(actor=request.user, **form.service_data())
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, f"Target {target.name} added. New tickets use it.")
            return redirect(list_url)
    return _form(
        request,
        form,
        title="New service target",
        submit_label="Create target",
        crumbs=[(TARGETS_TITLE, list_url), ("New service target", None)],
        cancel_url=list_url,
    )


@login_required
def target_edit(request, pk):
    require_setup(request.user)
    target = get_object_or_404(ServiceTarget, pk=pk)
    name = target.name
    list_url = reverse("service_levels:target-list")
    form = TargetEditForm(
        request.POST or None, instance=target, initial={"expected_revision": target.revision}
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_target(
                actor=request.user,
                target_id=target.pk,
                expected_revision=form.cleaned_data["expected_revision"],
                reason=form.cleaned_data["reason"],
                **form.service_data(),
            )
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Changes recorded with their reason.")
            return redirect(list_url)
    return _form(
        request,
        form,
        title=f"Edit {name}",
        submit_label="Save changes",
        crumbs=[(TARGETS_TITLE, list_url), (name, None), ("Edit", None)],
        cancel_url=list_url,
        obj=target,
    )


# Calendars ---------------------------------------------------------------------------------


@login_required
def calendar_list(request):
    require_setup(request.user)
    queryset = BusinessCalendar.objects.annotate(
        holiday_count=Count("holidays", distinct=True),
        target_count=Count("targets", filter=Q(targets__is_active=True), distinct=True),
    )
    return list_response(
        request,
        queryset=queryset,
        columns=CALENDAR_COLUMNS,
        default_sort="name",
        form_class=TargetFilter,
        search_fields=("name",),
        filter_fields={},
        template="service_levels/calendar_list.html",
        title=CALENDARS_TITLE,
        placeholder="Calendar name",
        inline=("active",),
    )


@login_required
def calendar_detail(request, pk):
    require_setup(request.user)
    calendar = get_object_or_404(BusinessCalendar, pk=pk)
    holidays = list(calendar.holidays.all())
    events = list(_history("service_levels.businesscalendar", calendar.pk))
    events += list(
        AuditEvent.objects.filter(
            object_type="service_levels.holiday",
            action__startswith="holiday.",
            after__calendar=calendar.pk,
        ).select_related("actor")
    )
    events += list(
        AuditEvent.objects.filter(
            object_type="service_levels.holiday",
            action="holiday.removed",
            before__calendar=calendar.pk,
        ).select_related("actor")
    )
    events = sorted({e.pk: e for e in events}.values(), key=lambda e: e.occurred_at)
    return render(
        request,
        "service_levels/calendar_detail.html",
        {
            "page_title": calendar.name,
            "calendar": calendar,
            "weekly_hours": calendar.weekly_hours(),
            "holidays": holidays,
            "targets": calendar.targets.select_related("category").order_by("name"),
            "history": events,
        },
    )


@login_required
def calendar_create(request):
    require_setup(request.user)
    form = CalendarForm(request.POST or None)
    list_url = reverse("service_levels:calendar-list")
    if request.method == "POST" and form.is_valid():
        try:
            calendar = create_calendar(actor=request.user, **form.service_data())
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, f"Calendar {calendar.name} added.")
            return redirect("service_levels:calendar-detail", pk=calendar.pk)
    return _form(
        request,
        form,
        title="New business calendar",
        submit_label="Create calendar",
        crumbs=[(CALENDARS_TITLE, list_url), ("New business calendar", None)],
        cancel_url=list_url,
    )


@login_required
def calendar_edit(request, pk):
    require_setup(request.user)
    calendar = get_object_or_404(BusinessCalendar, pk=pk)
    name = calendar.name
    record_url = reverse("service_levels:calendar-detail", args=[calendar.pk])
    form = CalendarEditForm(
        request.POST or None, instance=calendar, initial={"expected_revision": calendar.revision}
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_calendar(
                actor=request.user,
                calendar_id=calendar.pk,
                expected_revision=form.cleaned_data["expected_revision"],
                reason=form.cleaned_data["reason"],
                **form.service_data(),
            )
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Changes recorded with their reason.")
            return redirect(record_url)
    return _form(
        request,
        form,
        title=f"Edit {name}",
        submit_label="Save changes",
        crumbs=[
            (CALENDARS_TITLE, reverse("service_levels:calendar-list")),
            (name, record_url),
            ("Edit", None),
        ],
        cancel_url=record_url,
        obj=calendar,
    )


@login_required
def holiday_create(request, pk):
    require_setup(request.user)
    calendar = get_object_or_404(BusinessCalendar, pk=pk)
    record_url = reverse("service_levels:calendar-detail", args=[calendar.pk])
    form = HolidayForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            add_holiday(actor=request.user, calendar_id=calendar.pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Holiday added.")
            return redirect(record_url)
    return _form(
        request,
        form,
        title="Add holiday",
        submit_label="Add holiday",
        crumbs=[
            (CALENDARS_TITLE, reverse("service_levels:calendar-list")),
            (calendar.name, record_url),
            ("Add holiday", None),
        ],
        cancel_url=record_url,
    )


@login_required
def holiday_remove(request, pk):
    require_setup(request.user)
    holiday = get_object_or_404(Holiday.objects.select_related("calendar"), pk=pk)
    calendar = holiday.calendar
    record_url = reverse("service_levels:calendar-detail", args=[calendar.pk])
    form = HolidayRemoveForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            remove_holiday(actor=request.user, holiday_id=holiday.pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Holiday removed.")
            return redirect(record_url)
    return render(
        request,
        "service_levels/holiday_remove.html",
        {
            "page_title": "Remove holiday",
            "form": form,
            "holiday": holiday,
            "crumbs": [
                (CALENDARS_TITLE, reverse("service_levels:calendar-list")),
                (calendar.name, record_url),
                ("Remove holiday", None),
            ],
            "cancel_url": record_url,
        },
    )
