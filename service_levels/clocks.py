"""Keep each request's clock current. Called from `hooks.py` inside the desk services'
transactions, with the request already locked; nothing here changes the request itself.

The rules are in `rules.py`. Due times are worked out at creation and when the category or
priority changes, from the most specific active target at that moment. A clock copies the
target's times, calendar and third-party pause rule, so a later edit to a target applies to
requests created or recalculated afterwards. The calendar's opening hours and holidays are read
whenever a due time is next worked out (creation, recalculation, resuming after a pause,
reopening), so an edit to a calendar reaches running clocks at that point.
"""

from django.db.models import Q
from django.utils import timezone

from .models import BusinessCalendar, RequestClock, ServiceTarget
from .rules import (
    ANSWERED_AT_INTAKE_CHANNELS,
    TargetValues,
    apply_target,
    entry_meets_first_response,
    meet_first_response,
    select_target,
    sync_status,
)


def applicable_target(category, priority):
    """The most specific active target for a category and priority, or None."""
    scopes = Q(category__isnull=True) | Q(category_id=category.pk)
    if category.parent_id:
        scopes |= Q(category_id=category.parent_id)
    candidates = ServiceTarget.objects.filter(scopes, is_active=True)
    return select_target(
        candidates, category_id=category.pk, group_id=category.parent_id, priority=priority
    )


def _values(target):
    if target is None:
        return None
    return TargetValues(
        target_id=target.pk,
        first_response_minutes=target.first_response_minutes,
        resolution_minutes=target.resolution_minutes,
        calendar_id=target.calendar_id,
        pause_on_third_party=target.pause_on_third_party,
    )


def calendar_value(calendar_id):
    """The pure Calendar for a stored calendar id (None: around the clock)."""
    if calendar_id is None:
        return None
    return BusinessCalendar.objects.get(pk=calendar_id).as_calendar()


def _locked_clock(request):
    return RequestClock.objects.select_for_update().filter(request=request).first()


def _apply(clock, request, target, now):
    calendar = calendar_value(target.calendar_id if target else None)
    apply_target(clock, _values(target), start=request.created_at, now=now, calendar=calendar)


def _sync(clock, request, now):
    sync_status(
        clock,
        request.status,
        start=request.created_at,
        now=now,
        calendar=calendar_value(clock.calendar_id),
    )


def start(request, now=None):
    """Create the clock of a new request, if a target applies."""
    now = now or timezone.now()
    target = applicable_target(request.category, request.priority)
    if target is None:
        return None
    clock = RequestClock(request=request)
    _apply(clock, request, target, now)
    if request.channel in ANSWERED_AT_INTAKE_CHANNELS:
        meet_first_response(clock, request.created_at)
    _sync(clock, request, now)
    clock.save()
    return clock


def recalculate(request, now=None):
    """Category or priority changed: take the target that applies now. Met clocks stay."""
    now = now or timezone.now()
    clock = _locked_clock(request)
    target = applicable_target(request.category, request.priority)
    if clock is None:
        if target is None:
            return None
        # No target applied before: the clock starts now, counted from the request's creation.
        return start(request, now)
    _apply(clock, request, target, now)
    _sync(clock, request, now)
    clock.save()
    return clock


def status_changed(request, now=None):
    now = now or timezone.now()
    clock = _locked_clock(request)
    if clock is None:
        return None
    _sync(clock, request, now)
    clock.save()
    return clock


def _is_first_entry(request, entry):
    """The words a request was opened with are not a response to it."""
    first = request.entries.exclude(kind="event").order_by("created_at", "pk").first()
    return first is not None and first.pk == entry.pk


def entry_added(request, entry):
    meets = entry_meets_first_response(
        kind=entry.kind,
        # The capacity recorded on the entry when written (desk.models.AuthorCapacity), never the
        # author's account today.
        author_is_staff=entry.recorded_capacity() == "laboratory",
        contact_outcome=entry.contact_outcome or "",
    )
    if not meets or _is_first_entry(request, entry):
        return None
    clock = _locked_clock(request)
    if clock is not None and meet_first_response(clock, entry.created_at):
        clock.save()
    return clock
