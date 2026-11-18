"""The desk's scheduled job (jobs.registry): close resolved tickets after the laboratory's period.

Off until an administrator sets the period on the Scheduled jobs page (none by default). Each
candidate is closed in its own transaction by desk.services.close_resolved_automatically, which
decides again under its locks; one being changed by someone at that moment waits for the next
run.
"""

from collections import Counter
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from jobs.actor import system_account
from jobs.models import JobSettings

from .models import ServiceRequest, Status
from .services import close_resolved_automatically

# Most tickets one run closes; the rest wait for the next run.
CLOSE_BATCH = 500


def close_resolved_tickets(now=None):
    now = now or timezone.now()
    days = JobSettings.objects.filter(pk=1).values_list("close_resolved_after_days", flat=True)
    days = days.first()
    if not days:
        return {"closed": 0}
    candidates = list(
        ServiceRequest.objects.filter(
            status=Status.RESOLVED, resolved_at__lte=now - timedelta(days=days)
        )
        .order_by("resolved_at", "pk")
        .values_list("pk", flat=True)[:CLOSE_BATCH]
    )
    counts = Counter({"closed": 0})
    if not candidates:
        return dict(counts)
    actor = system_account()
    for pk in candidates:
        try:
            with transaction.atomic():
                counts[close_resolved_automatically(actor=actor, request_id=pk, now=now)] += 1
        except ValidationError:  # a rule keeps it open: a complaint whose review is missing
            counts["kept_open"] += 1
    counts.pop("not_due", None)
    return dict(counts)
