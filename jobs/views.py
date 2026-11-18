"""The Scheduled jobs page and its settings form (administrators only; the service checks again).

The overview reads jobs.selectors: each job's last run, the settings, and the email notices that
need attention. A notice names its ticket, and who it was for, only when the reader may open the
ticket (selectors.notices_needing_attention)."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.urls import reverse

from accounts.roles import require_role

from . import selectors
from .forms import JobSettingsForm
from .models import JobOutcome, NoticeStatus
from .outbox import mail_enabled
from .services import update_settings

TITLE = "Scheduled jobs"
SETTINGS_TITLE = "Edit scheduled job settings"

# What a notice was about, by event code (the keys of desk.notifications.PHRASES).
EVENT_LABELS = {
    "reply": "Reply",
    "waiting_client": "Waiting on client",
    "resolved": "Resolved",
    "assigned": "Assigned",
    "client_reply": "Client reply",
    "first_response_breached": "First-response target passed",
    "resolution_breached": "Resolution target passed",
}

# A job's last-run counts in words: key -> (one, many). Keys not listed are left out.
COUNT_WORDS = {
    "closed": ("ticket closed", "tickets closed"),
    "kept_open": (
        "ticket kept open until its complaint review",
        "tickets kept open until their complaint review",
    ),
    "breaches": ("target passed", "targets passed"),
    "notices": ("notice recorded", "notices recorded"),
    "sent": ("notice sent", "notices sent"),
    "retrying": ("notice to retry", "notices to retry"),
    "failed": ("notice failed", "notices failed"),
    "not_sent": ("notice not sent", "notices not sent"),
    "busy": ("left for the next run", "left for the next run"),
}

OUTCOMES = {
    JobOutcome.SUCCEEDED: ("Succeeded", "success"),
    JobOutcome.FAILED: ("Failed", "danger"),
    JobOutcome.RUNNING: ("Running", "progress"),
}


def require_admin(user):
    require_role(user, "admin")


def counts_text(counts):
    """'3 tickets closed, 1 left for the next run', or 'Nothing to do'."""
    parts = []
    for key, (one, many) in COUNT_WORDS.items():
        value = int((counts or {}).get(key) or 0)
        if value:
            parts.append(f"{value} {one if value == 1 else many}")
    return ", ".join(parts) or "Nothing to do"


def _job(row):
    state = row.state
    outcome = OUTCOMES.get(state.last_outcome) if state and not row.never_run else None
    return {
        "label": row.job.label,
        "summary": row.job.summary,
        "started_at": None if row.never_run else state.last_started_at,
        "outcome": outcome[0] if outcome else "",
        "tone": outcome[1] if outcome else "",
        "error": state.last_error if row.failed else "",
        "failures": state.consecutive_failures if row.failed else 0,
        "did": counts_text(state.last_counts)
        if state and state.last_outcome == JobOutcome.SUCCEEDED
        else "",
    }


def _notice(notice):
    named = notice.ticket_id is not None
    recipient = notice.recipient
    return {
        "created_at": notice.created_at,
        "ticket_id": notice.ticket_id,
        "reference": notice.reference if named else "",
        "recipient": (recipient.get_full_name() or recipient.username) if named else "",
        "events": ", ".join(EVENT_LABELS.get(code, "Other") for code in notice.events),
        "failed": notice.status == NoticeStatus.FAILED,
        "attempts": notice.attempts,
        "last_error": notice.last_error,
        "next_attempt_at": notice.next_attempt_at
        if notice.status == NoticeStatus.WAITING
        else None,
    }


@login_required
def overview(request):
    require_admin(request.user)
    rows = selectors.job_rows()
    status, latest = selectors.scheduler_status(rows)
    return render(
        request,
        "jobs/overview.html",
        {
            "page_title": TITLE,
            "scheduler": status,
            "latest_start": latest,
            "jobs": [_job(row) for row in rows],
            "settings": selectors.current_settings(),
            "counts": selectors.notice_counts(),
            "notices": [_notice(n) for n in selectors.notices_needing_attention(request.user)],
            "mail_enabled": mail_enabled(),
        },
    )


@login_required
def settings_edit(request):
    require_admin(request.user)
    current = selectors.current_settings()
    overview_url = reverse("jobs:overview")
    form = JobSettingsForm(
        request.POST or None,
        initial={
            "close_resolved_after_days": current.close_resolved_after_days,
            "expected_revision": current.revision,
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            changed = update_settings(
                actor=request.user,
                expected_revision=form.cleaned_data["expected_revision"],
                close_resolved_after_days=form.cleaned_data["close_resolved_after_days"],
            )
        except ValidationError as exc:
            if hasattr(exc, "message_dict"):
                for name, errors in exc.message_dict.items():
                    form.add_error(name if name in form.fields else None, errors)
            else:
                form.add_error(None, exc)
        else:
            messages.success(request, "Settings saved." if changed else "No changes to save.")
            return redirect(overview_url)
    return render(
        request,
        "jobs/settings_form.html",
        {
            "page_title": SETTINGS_TITLE,
            "form": form,
            "crumbs": [(TITLE, overview_url), (SETTINGS_TITLE, None)],
            "cancel_url": overview_url,
        },
    )
