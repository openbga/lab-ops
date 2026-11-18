"""The desk's scheduled job (jobs.registry): close resolved tickets after the laboratory's period.

Off until an administrator sets the period on the Scheduled jobs page (none by default). Each
candidate is closed in its own transaction by desk.services.close_resolved_automatically, which
decides again under its locks; one being changed by someone at that moment waits for the next
run. Every candidate is looked at in each run, oldest resolution first, so tickets that cannot
close (a complaint whose review no longer counts) never hold up the others.
"""

from collections import Counter
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from jobs.actor import system_account
from jobs.models import JobSettings

from .models import ServiceRequest, Status
from .services import close_resolved_automatically

# Candidates are read in pages of this many, oldest resolution first, until none is left.
CLOSE_BATCH = 500


def _candidates(cutoff):
    """Resolved tickets resolved by `cutoff`, leaving out complaints with no review yet, which
    cannot close (the service still decides each one under its locks)."""
    return (
        ServiceRequest.objects.filter(status=Status.RESOLVED, resolved_at__lte=cutoff)
        .exclude(is_complaint=True, complaint_review__reviewed_at__isnull=True)
        .order_by("resolved_at", "pk")
    )


def close_resolved_tickets(now=None):
    now = now or timezone.now()
    days = JobSettings.objects.filter(pk=1).values_list("close_resolved_after_days", flat=True)
    days = days.first()
    counts = Counter({"closed": 0})
    if not days:
        return dict(counts)
    cutoff = now - timedelta(days=days)
    actor = None
    after = None  # (resolved_at, pk) of the last candidate read: every candidate gets its turn
    while True:
        page = _candidates(cutoff)
        if after is not None:
            page = page.filter(
                Q(resolved_at__gt=after[0]) | Q(resolved_at=after[0], pk__gt=after[1])
            )
        rows = list(page.values_list("pk", "resolved_at")[:CLOSE_BATCH])
        if not rows:
            break
        actor = actor or system_account()
        for pk, resolved_at in rows:
            after = (resolved_at, pk)
            try:
                with transaction.atomic():
                    counts[close_resolved_automatically(actor=actor, request_id=pk, now=now)] += 1
            except ValidationError:  # a rule keeps it open: its complaint review no longer counts
                counts["kept_open"] += 1
    counts.pop("not_due", None)
    return dict(counts)
