"""The service clocks' scheduled job (jobs.registry): tell the people responsible once when a
request passes its first-response or resolution target.

A clock is breached when it is running (not met, and for resolution not paused) and its due time
has passed; at the due time itself it is still on time, as service_levels.rules counts it. For
each breached clock not yet noticed in this run of it, the job records a notice for each person
desk.notifications.breach_recipients names (the assignee and the team's leads, or the whole team
when it has neither), marks the clock noticed and records an audit event, in one transaction with
the request and then its clock locked (the services' order). A request or clock someone is
changing at that moment is left for the next run. The request itself does not change, so
nobody's open page of it goes stale.

A run of a clock ends when it is met or resolved, or when a recalculation moves its due time
(service_levels.rules): its mark is cleared, a later breach is noticed again, and a notice of the
earlier run that is still waiting no longer applies (desk.notifications.prepare_breach_notice
compares the mark it was recorded with). Clocks already past due when this release was installed
were marked by migration 0003 so the first run does not send a notice for every old breach.
"""

from collections import Counter
from datetime import UTC

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.audit import record_event
from jobs import outbox
from jobs.actor import system_account

from .models import RequestClock
from .rules import STOPPED_STATUSES

# Most requests one run looks at; the rest wait for the next run.
BREACH_BATCH = 500
CLOCKS = ("first_response", "resolution")


def breached_filter(now):
    """Clocks running past their due time and not noticed yet in this run of them."""
    first = Q(
        first_response_met_at__isnull=True,
        first_response_due_at__lt=now,
        first_response_breach_noticed_at__isnull=True,
    )
    resolution = Q(
        resolution_met_at__isnull=True,
        paused_since__isnull=True,
        resolution_due_at__lt=now,
        resolution_breach_noticed_at__isnull=True,
    )
    return (first | resolution) & ~Q(request__status__in=STOPPED_STATUSES)


def notice_breaches(now=None):
    from desk import notifications
    from desk.models import ServiceRequest

    now = now or timezone.now()
    request_ids = list(
        RequestClock.objects.filter(breached_filter(now))
        .order_by("pk")
        .values_list("request_id", flat=True)[:BREACH_BATCH]
    )
    counts = Counter({"breaches": 0, "notices": 0})
    if not request_ids:
        return dict(counts)
    actor = system_account()
    for request_id in request_ids:
        with transaction.atomic():
            request = (
                ServiceRequest.objects.select_for_update(of=("self",), skip_locked=True)
                .filter(pk=request_id)
                .exclude(status__in=STOPPED_STATUSES)
                .first()
            )
            if request is None:  # being changed right now, or resolved since: next run
                counts["busy"] += 1
                continue
            clock = (
                RequestClock.objects.select_for_update(skip_locked=True)
                .filter(request=request)
                .first()
            )
            if clock is None:  # being changed right now: next run
                counts["busy"] += 1
                continue
            changed, recorded = [], []
            for which in CLOCKS:
                noticed_field = f"{which}_breach_noticed_at"
                if getattr(clock, noticed_field) is not None:
                    continue
                if not notifications.breach_still_running(clock, which, now):
                    continue
                due = getattr(clock, f"{which}_due_at")
                notices = [
                    outbox.record(
                        recipient=user,
                        topic=notifications.BREACH_TOPIC,
                        object_id=request.pk,
                        reference=request.number,
                        events=[notifications.BREACH_EVENTS[which]],
                        # The run this notice belongs to: sending checks the mark is unchanged.
                        data={
                            "clock": which,
                            "role": role,
                            "noticed_at": now.astimezone(UTC).isoformat(),
                        },
                    )
                    for user, role in notifications.breach_recipients(request)
                ]
                setattr(clock, noticed_field, now)
                changed.append(noticed_field)
                recorded += [notice.pk for notice in notices]
                record_event(
                    actor=actor,
                    action="clock.breach_noticed",
                    obj=clock,
                    after={
                        "request_id": request.pk,
                        "clock": which,
                        "due_at": due.isoformat(),
                        "notices": len(notices),
                    },
                )
                counts["breaches"] += 1
                counts["notices"] += len(notices)
            if changed:
                clock.save(update_fields=changed)
                outbox.send_after_commit(recorded)
    return dict(counts)
