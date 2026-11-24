"""The Scheduled jobs page and its settings form: admin only, the scheduler notices, the jobs, the
settings and the email notices that need attention. Synthetic data."""

import re
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent
from desk.models import EntryKind
from desk.notifications import PHRASES
from desk.services import create_request
from jobs import views
from jobs.models import JobOutcome, JobSettings, JobState, Notice, NoticeStatus
from jobs.services import STALE

from .conftest import DUMMY, LOCMEM

pytestmark = pytest.mark.django_db

PASSWORD = "test-password-729!"
OVERVIEW = "jobs:overview"
EDIT = "jobs:settings-edit"
SUBJECT = "Synthetic subject that must stay private"
BODY = "Synthetic first entry that must stay private."


@pytest.fixture
def admin(make_user):
    return make_user("jobs_admin", "admin", first_name="Avery", last_name="Admin")


@pytest.fixture
def signed_in(client, admin):
    client.force_login(admin)
    return client


@pytest.fixture
def ticket(agent, category):
    return create_request(
        actor=agent,
        category=category,
        subject=SUBJECT,
        first_entry_kind=EntryKind.NOTE,
        first_entry_body=BODY,
        channel="phone",
    )


def main(response):
    """The page's own content: <main>, without the help drawer, which documents every state."""
    content = response.content.decode()
    return content[content.index("<main") : content.index("</main>")]


def page_text(response):
    return " ".join(re.sub(r"<[^>]+>", " ", main(response)).split())


def post(client, value, revision=1):
    return client.post(
        reverse(EDIT), {"close_resolved_after_days": value, "expected_revision": revision}
    )


# Access ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", [OVERVIEW, EDIT])
def test_admin_opens_both_pages(signed_in, name):
    response = signed_in.get(reverse(name))
    assert response.status_code == 200
    title = "Scheduled jobs" if name == OVERVIEW else "Edit scheduled job settings"
    assert re.search(rf"<h1\b[^>]*>\s*{title}\s*</h1>", response.content.decode())


@pytest.mark.parametrize("name", [OVERVIEW, EDIT])
@pytest.mark.parametrize("role", ["supervisor", "client_services"])
def test_other_staff_roles_are_refused(client, make_user, role, name):
    client.force_login(make_user(f"other_{role}", role))
    assert client.get(reverse(name)).status_code == 403
    assert post(client, "30").status_code == 403
    assert not JobSettings.objects.exists()


@pytest.mark.parametrize("name", [OVERVIEW, EDIT])
def test_a_portal_account_is_refused(client, clinic_portal, name):
    client.force_login(clinic_portal)
    assert client.get(reverse(name)).status_code == 403
    assert post(client, "30").status_code == 403


@pytest.mark.parametrize("name", [OVERVIEW, EDIT])
def test_anonymous_goes_to_sign_in(client, name):
    response = client.get(reverse(name))
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


def test_the_admin_menu_marks_scheduled_jobs(signed_in):
    content = signed_in.get(reverse(EDIT)).content.decode()
    current = re.findall(
        r'<a class="vr-navlink"[^>]*aria-current="page"[^>]*>(.*?)</a>', content, re.S
    )
    assert [" ".join(re.sub(r"<[^>]+>", " ", item).split()) for item in current] == [
        "Scheduled jobs"
    ]


# Settings ----------------------------------------------------------------------------------


def test_the_settings_panel_shows_never_then_the_period(signed_in, admin):
    text = page_text(signed_in.get(reverse(OVERVIEW)))
    assert "Close resolved tickets after" in text
    assert "Never (tickets stay resolved until someone closes them)" in text
    JobSettings.objects.create(pk=1, close_resolved_after_days=30)
    assert "30 days" in page_text(signed_in.get(reverse(OVERVIEW)))


def test_the_form_shows_the_current_value_and_revision(signed_in):
    JobSettings.objects.create(pk=1, close_resolved_after_days=14, revision=4)
    content = signed_in.get(reverse(EDIT)).content.decode()
    assert re.search(r'name="close_resolved_after_days" value="14"', content)
    assert re.search(r'name="expected_revision" value="4"', content)
    assert 'id="id_close_resolved_after_days_unit">days<' in content
    assert 'type="number"' not in content


def test_saving_changes_the_setting_with_an_audit_event(signed_in, admin):
    response = post(signed_in, "30")
    assert response.status_code == 302 and response["Location"] == reverse(OVERVIEW)
    row = JobSettings.objects.get(pk=1)
    assert row.close_resolved_after_days == 30 and row.revision == 2
    event = AuditEvent.objects.get(action="jobs.settings_changed")
    assert event.actor_id == admin.pk
    assert event.after == {"close_resolved_after_days": 30}
    page = signed_in.get(reverse(OVERVIEW))
    assert "Settings saved." in page_text(page) and "30 days" in page_text(page)


def test_an_empty_value_turns_closing_off(signed_in):
    JobSettings.objects.create(pk=1, close_resolved_after_days=30, revision=2)
    assert post(signed_in, "", revision=2).status_code == 302
    assert JobSettings.objects.get(pk=1).close_resolved_after_days is None


def test_saving_the_same_value_says_no_changes(signed_in):
    response = signed_in.post(
        reverse(EDIT), {"close_resolved_after_days": "", "expected_revision": 1}, follow=True
    )
    assert "No changes to save." in page_text(response)
    assert not AuditEvent.objects.filter(action="jobs.settings_changed").exists()


def test_a_stale_revision_is_refused_and_the_input_kept(signed_in):
    assert post(signed_in, "30").status_code == 302
    response = post(signed_in, "14", revision=1)
    assert response.status_code == 200
    assert STALE in page_text(response)
    assert 'value="14"' in response.content.decode()
    assert JobSettings.objects.get(pk=1).close_resolved_after_days == 30


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("0", "Enter a number of days from 1 to 365, or leave it empty."),
        ("366", "Enter a number of days from 1 to 365, or leave it empty."),
        ("abc", "Enter a whole number of days."),
        ("7.5", "Enter a whole number of days."),
    ],
)
def test_invalid_values_show_an_error_and_keep_the_input(signed_in, value, message):
    response = post(signed_in, value)
    assert response.status_code == 200
    assert message in page_text(response)
    assert f'value="{value}"' in response.content.decode()
    assert 'aria-invalid="true"' in response.content.decode()
    assert not JobSettings.objects.filter(close_resolved_after_days__isnull=False).exists()
    assert not AuditEvent.objects.filter(action="jobs.settings_changed").exists()


# Scheduler and jobs ------------------------------------------------------------------------

NEVER = "The scheduled jobs have never run on this deployment."
STALE_NOTICE = "No scheduled job has run in the last 30 minutes."


def state(name, started, **extra):
    return JobState.objects.create(name=name, last_started_at=started, **extra)


def test_never_run_notice_and_every_job_listed(signed_in):
    text = page_text(signed_in.get(reverse(OVERVIEW)))
    assert NEVER in text and "python manage.py run_scheduled_jobs" in text
    assert STALE_NOTICE not in text
    for label in ("Close resolved tickets", "Service target breaches", "Send email notices"):
        assert label in text
    assert text.count("Never") >= 3


def test_stale_notice_when_no_run_in_the_last_30_minutes(signed_in):
    state(
        "send_notices",
        timezone.now() - timedelta(hours=2),
        last_outcome=JobOutcome.SUCCEEDED,
    )
    text = page_text(signed_in.get(reverse(OVERVIEW)))
    assert STALE_NOTICE in text and NEVER not in text


def test_no_notice_while_running_and_outcomes_in_words(signed_in):
    now = timezone.now()
    state(
        "close_resolved_tickets",
        now,
        last_outcome=JobOutcome.SUCCEEDED,
        last_counts={"closed": 3, "kept_open": 1},
    )
    state(
        "service_target_breaches",
        now,
        last_outcome=JobOutcome.FAILED,
        last_error="OperationalError",
        consecutive_failures=2,
    )
    state("send_notices", now, last_outcome=JobOutcome.RUNNING)
    text = page_text(signed_in.get(reverse(OVERVIEW)))
    assert NEVER not in text and STALE_NOTICE not in text
    assert "Succeeded" in text
    assert "3 tickets closed, 1 ticket kept open until its complaint review" in text
    assert "Failed OperationalError, 2 runs in a row" in text
    assert "Running" in text


def test_counts_in_words():
    assert views.counts_text({}) == "Nothing to do"
    assert views.counts_text({"closed": 0}) == "Nothing to do"
    assert views.counts_text({"sent": 1, "retrying": 2}) == "1 notice sent, 2 notices to retry"
    assert views.counts_text({"breaches": 1, "notices": 2, "busy": 1}) == (
        "1 target passed, 2 notices recorded, 1 left for the next run"
    )


# Email notices -----------------------------------------------------------------------------


def notice(ticket, recipient, **extra):
    values = {
        "recipient": recipient,
        "topic": "desk.request",
        "object_id": ticket.pk,
        "reference": ticket.number,
        "events": ["assigned"],
        "next_attempt_at": timezone.now(),
    }
    values.update(extra)
    return Notice.objects.create(**values)


def test_event_labels_cover_every_notice_event():
    from accounts.security_notices import PRECEDENCE

    assert set(views.EVENT_LABELS) == set(PHRASES) | set(PRECEDENCE)


def test_nothing_needs_attention_is_an_empty_state(signed_in):
    text = page_text(signed_in.get(reverse(OVERVIEW)))
    assert "Nothing needs attention" in text


def test_a_failed_notice_shows_its_ticket_to_an_admin_without_ticket_text(signed_in, ticket, agent):
    agent.first_name, agent.last_name = "Casey", "Agent"
    agent.save()
    notice(
        ticket,
        agent,
        status=NoticeStatus.FAILED,
        attempts=8,
        last_error="SMTPRecipientsRefused",
        events=["first_response_breached"],
    )
    notice(ticket, agent, attempts=2, events=["client_reply"])
    response = signed_in.get(reverse(OVERVIEW))
    content = response.content.decode()
    text = page_text(response)
    url = reverse("desk:request-detail", args=[ticket.pk])
    assert f'href="{url}">{ticket.number}</a>' in content
    assert "Casey Agent" in text
    assert "First-response target passed" in text and "Client reply" in text
    assert "SMTPRecipientsRefused" in text
    assert "Waiting to retry" in text
    assert "Not available to you" not in text
    assert SUBJECT not in content and BODY not in content


def test_a_notice_about_a_ticket_the_reader_may_not_open_is_not_named(
    signed_in, ticket, agent, monkeypatch
):
    agent.first_name, agent.last_name = "Casey", "Agent"
    agent.save()
    row = notice(ticket, agent, status=NoticeStatus.FAILED, attempts=8, last_error="OSError")
    row.ticket_id = None
    monkeypatch.setattr(views.selectors, "notices_needing_attention", lambda viewer: [row])
    content = signed_in.get(reverse(OVERVIEW)).content.decode()
    assert "Not available to you" in content
    assert ticket.number not in content and "Casey Agent" not in content
    assert reverse("desk:request-detail", args=[ticket.pk]) not in content


def test_counts_and_email_off(signed_in, settings, ticket, agent):
    notice(ticket, agent)
    notice(ticket, agent, attempts=1)
    notice(ticket, agent, status=NoticeStatus.FAILED, attempts=8)
    notice(ticket, agent, status=NoticeStatus.SENT, attempts=1, sent_at=timezone.now())
    settings.EMAIL_BACKEND = DUMMY
    text = page_text(signed_in.get(reverse(OVERVIEW)))
    assert "Waiting to send 2 (1 to retry)" in text
    assert "Failed 1" in text and "Sent in the last 7 days 1" in text
    assert "Not sent in the last 7 days 0" in text
    assert "Email is off on this deployment. Notices are recorded but not sent." in text
    settings.EMAIL_BACKEND = LOCMEM
    assert "Email is off" not in page_text(signed_in.get(reverse(OVERVIEW)))


def test_a_notice_about_sign_in_factors_names_no_ticket_and_no_account(signed_in, agent):
    """A failed notice to a person about their own sign-in factors (accounts.security_notices) is
    listed as an account notice, without a ticket link and without whose account it was."""
    agent.first_name, agent.last_name = "Casey", "Agent"
    agent.save()
    Notice.objects.create(
        recipient=agent,
        topic="accounts.security",
        object_id=agent.pk,
        reference="Account",
        events=["authenticator_added"],
        data={"by": "self"},
        status=NoticeStatus.FAILED,
        attempts=8,
        last_error="OSError",
        next_attempt_at=timezone.now(),
    )
    response = signed_in.get(reverse(OVERVIEW))
    text = page_text(response)
    assert "Account notice" in text and "Authenticator added" in text
    assert "Not available to you" not in text
    assert "Casey Agent" not in text and agent.username not in text
