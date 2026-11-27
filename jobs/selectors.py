"""Reads for the Scheduled jobs page (administrators): each job's last run, the settings, and the
email notices that need attention. Notices about a ticket name it only to a reader who may open
that ticket (desk.selectors.staff_requests)."""

from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .models import JobOutcome, JobSettings, JobState, Notice, NoticeStatus
from .registry import JOBS

# A run every few minutes is expected; past this, the page warns that the jobs are not running.
STALE_AFTER = timedelta(minutes=30)
# How far back the page counts sent and unsent notices.
RECENT = timedelta(days=7)
# Most notices the attention table lists.
ATTENTION_LIMIT = 25
# Notice topics whose object is a ticket (desk.models.ServiceRequest).
TICKET_TOPICS = frozenset({"desk.request", "desk.breach"})
# Notices of emergency access (desk.emergency): their object is the use, which names a ticket.
EMERGENCY_TOPIC = "desk.emergency"


def current_settings():
    """The settings row, or an unsaved one with the defaults (never created by reading)."""
    return JobSettings.objects.filter(pk=1).first() or JobSettings()


@dataclass
class JobRow:
    job: object  # jobs.registry.Job
    state: JobState | None

    @property
    def never_run(self):
        return self.state is None or self.state.last_started_at is None

    @property
    def failed(self):
        return self.state is not None and self.state.last_outcome == JobOutcome.FAILED


def job_rows():
    states = {state.name: state for state in JobState.objects.all()}
    return [JobRow(job, states.get(job.name)) for job in JOBS]


def scheduler_status(rows, now=None):
    """The scheduler's state and the latest start time: "never" when no job has started,
    "stale" when the latest start is older than STALE_AFTER, else "running"."""
    now = now or timezone.now()
    starts = [row.state.last_started_at for row in rows if not row.never_run]
    if not starts:
        return "never", None
    latest = max(starts)
    return ("stale" if now - latest > STALE_AFTER else "running"), latest


def notice_counts(now=None):
    """Waiting and failed notices (all), and sent and not sent in the last RECENT."""
    now = now or timezone.now()
    since = now - RECENT
    return Notice.objects.aggregate(
        waiting=Count("pk", filter=Q(status=NoticeStatus.WAITING)),
        retrying=Count("pk", filter=Q(status=NoticeStatus.WAITING, attempts__gt=0)),
        failed=Count("pk", filter=Q(status=NoticeStatus.FAILED)),
        sent=Count("pk", filter=Q(status=NoticeStatus.SENT, created_at__gte=since)),
        not_sent=Count("pk", filter=Q(status=NoticeStatus.NOT_SENT, created_at__gte=since)),
    )


def notices_needing_attention(viewer):
    """Failed notices and those waiting to be retried, newest first, at most ATTENTION_LIMIT.
    Each gets `.ticket_id` (the ticket's id when `viewer` may open it, else None), so the page
    names a ticket, and who the notice was for, only to someone who may read the ticket."""
    from desk.models import EmergencyAccess
    from desk.selectors import staff_requests

    notices = list(
        Notice.objects.filter(
            Q(status=NoticeStatus.FAILED) | Q(status=NoticeStatus.WAITING, attempts__gt=0)
        )
        .select_related("recipient")
        .order_by("-created_at", "-pk")[:ATTENTION_LIMIT]
    )
    # A notice of emergency access names the use, which names its ticket (desk.emergency).
    uses = {n.object_id for n in notices if n.topic == EMERGENCY_TOPIC}
    use_tickets = dict(EmergencyAccess.objects.filter(pk__in=uses).values_list("pk", "ticket_id"))

    def ticket_of(notice):
        if notice.topic in TICKET_TOPICS:
            return notice.object_id
        return use_tickets.get(notice.object_id) if notice.topic == EMERGENCY_TOPIC else None

    ticket_ids = {ticket_of(n) for n in notices} - {None}
    readable = set(staff_requests(viewer).filter(pk__in=ticket_ids).values_list("pk", flat=True))
    for notice in notices:
        ticket_id = ticket_of(notice)
        notice.ticket_id = ticket_id if ticket_id in readable else None
    return notices
