"""What the scheduled jobs are, and which code words each kind of notice.

Later work adds its periodic job to JOBS and its notices to NOTICE_TOPICS; the one command
(`manage.py run_scheduled_jobs`) runs them all.
"""

from dataclasses import dataclass

from django.utils.module_loading import import_string


@dataclass(frozen=True)
class Job:
    name: str  # stable key, stored in JobState
    label: str  # what administrators see
    summary: str  # one sentence on what a run does
    path: str  # dotted path of `function(now) -> dict of counts`

    @property
    def function(self):
        return import_string(self.path)


# In run order: jobs that record notices come before the one that sends them.
JOBS = (
    Job(
        "close_resolved_tickets",
        "Close resolved tickets",
        "Closes tickets resolved longer ago than the laboratory's period. Off until one is set.",
        "desk.jobs.close_resolved_tickets",
    ),
    Job(
        "service_target_breaches",
        "Service target breaches",
        "Tells the people responsible once when a ticket passes its first-response or "
        "resolution target.",
        "service_levels.jobs.notice_breaches",
    ),
    Job(
        "send_notices",
        "Send email notices",
        "Sends email notices that are waiting and retries those that could not be sent.",
        "jobs.outbox.deliver_due",
    ),
    Job(
        "remove_kept_typing",
        "Remove kept typing",
        "Removes what people were typing when a session limit signed them out, 24 hours after "
        "the sign-out.",
        "accounts.kept.remove_expired",
    ),
)
JOBS_BY_NAME = {job.name: job for job in JOBS}

# topic -> dotted path of `prepare(notice) -> outbox.Prepared | None` (None: no longer applies).
NOTICE_TOPICS = {
    "desk.request": "desk.notifications.prepare_request_notice",
    "desk.breach": "desk.notifications.prepare_breach_notice",
    "accounts.security": "accounts.security_notices.prepare_security_notice",
    "desk.emergency": "desk.emergency.prepare_emergency_notice",
}


def notice_handler(topic):
    return import_string(NOTICE_TOPICS[topic])
