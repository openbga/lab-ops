"""Independent review, round 1 (Codex), of the scheduler and outbox at 01ff430: its regression
contracts, adopted as written. All data is synthetic."""

import io
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.forms import modelform_factory
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.admin import AccountChangeForm
from accounts.models import User
from clients.models import Client, ClientSite, PortalAccess
from core import demo
from core.models import AuditEvent, Lab
from desk import jobs as close_job
from desk import notifications, services
from desk.models import ServiceRequest, Status
from desk.tests.test_auto_close import resolve, set_period
from desk.tests.test_contract import new_request
from jobs import outbox, runner, selectors
from jobs.actor import system_account
from jobs.models import JobSettings, JobState, Notice, NoticeStatus, NotSentReason
from jobs.services import update_settings
from service_levels import jobs as clock_job
from service_levels.models import RequestClock, ServiceTarget
from service_levels.rules import resolution_state
from service_levels.tests import conftest as clock_fixtures
from teams.models import TeamMembership, ensure_default_team

from .conftest import DUMMY, LOCMEM

pytestmark = pytest.mark.django_db
frozen = clock_fixtures.frozen


@pytest.fixture(autouse=True)
def review_database(db, settings):
    assert connection.vendor == "postgresql"
    settings.EMAIL_BACKEND = LOCMEM


def in_worker_session(work):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '450ms'")
        return work()
    finally:
        connections.close_all()


def independent(work):
    """Run a single operation on a separate PostgreSQL session with a bounded lock wait."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(in_worker_session, work).result(timeout=15)


def can_lock(row):
    """Take and release a writer-compatible probe. No application data is changed."""
    try:
        with transaction.atomic():
            type(row).objects.select_for_update(no_key=True).get(pk=row.pk)
    except OperationalError as exc:
        if exc.__cause__.sqlstate != "55P03":
            raise
        return False
    return True


def change_status(user, ticket, status):
    ticket.refresh_from_db()
    return services.change_status(
        actor=user,
        request=ticket,
        expected_revision=ticket.revision,
        status=status,
        summary="Synthetic resolution",
        reason="Synthetic status change",
    )


def reply(user, ticket, **kwargs):
    ticket.refresh_from_db()
    return services.add_entry(
        actor=user,
        request=ticket,
        kind="reply",
        body="Synthetic reply",
        expected_revision=ticket.revision,
        **kwargs,
    )


@pytest.fixture
def portal_ticket(clinic_portal, category):
    return new_request(clinic_portal, category, channel="portal", first_entry_kind="reply")


@pytest.fixture
def resolution_ticket(agent, category, accessioning, join, frozen):
    ServiceTarget.objects.create(name="Review target", resolution_minutes=60)
    join(agent, accessioning)
    ticket = new_request(agent, category, team=accessioning, assignee=agent)
    frozen(frozen.now + timedelta(hours=2))
    return ticket


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("admin", 200),
        ("supervisor", 403),
        ("client_services", 403),
        ("roleless", 403),
        ("portal", 403),
        ("anonymous", 302),
        ("scheduler", 302),
    ],
)
def test_direct_jobs_routes(client, role, expected, make_user, clinic_portal):
    if role == "scheduler":
        client.force_login(system_account())
    elif role == "portal":
        client.force_login(clinic_portal)
    elif role != "anonymous":
        roles = () if role == "roleless" else (role,)
        client.force_login(make_user("review_" + role, *roles, is_staff=role == "roleless"))
    data = {"close_resolved_after_days": "7", "expected_revision": "1"}
    for route in ("jobs:overview", "jobs:settings-edit"):
        for method in ("get", "post"):
            url = reverse(route)
            response = client.get(url) if method == "get" else client.post(url, data)
            saved = role == "admin" and route == "jobs:settings-edit" and method == "post"
            assert response.status_code == (302 if saved else expected)
            if expected == 302:
                assert reverse("account_login") in response["Location"]
            elif saved:
                assert response["Location"] == reverse("jobs:overview")
            assert AuditEvent.objects.filter(action="jobs.settings_changed").count() == int(saved)
    assert selectors.current_settings().close_resolved_after_days == (
        7 if role == "admin" else None
    )


def test_ticket_and_recipient_redaction_uses_real_staff_scope(
    agent, make_user, privacy_category, category
):
    supervisor = make_user("review_supervisor", "supervisor")
    restricted = new_request(supervisor, privacy_category)
    ordinary = new_request(agent, category)
    notices = []
    for ticket in (restricted, ordinary):
        notice = outbox.record(
            recipient=agent,
            topic=notifications.TOPIC,
            object_id=ticket.pk,
            reference=ticket.number,
            events=["assigned"],
        )
        Notice.objects.filter(pk=notice.pk).update(status=NoticeStatus.FAILED)
        notices.append(notice)
    from jobs.views import _notice

    public = {row.pk: _notice(row) for row in selectors.notices_needing_attention(agent)}
    assert public[notices[0].pk]["ticket_id"] is None
    assert public[notices[0].pk]["reference"] == public[notices[0].pk]["recipient"] == ""
    assert public[notices[1].pk]["reference"] == ordinary.number
    assert public[notices[1].pk]["recipient"] == agent.username


def test_attention_page_omits_address_shaped_username(client, make_user, agent, category):
    admin = make_user("review_admin", "admin")
    recipient = User.objects.create_user("contact@example.invalid", "contact@example.invalid")
    ticket = new_request(agent, category)
    row = outbox.record(
        recipient=recipient,
        topic=notifications.TOPIC,
        object_id=ticket.pk,
        reference=ticket.number,
        events=["reply"],
    )
    Notice.objects.filter(pk=row.pk).update(status=NoticeStatus.FAILED)
    client.force_login(admin)
    page = client.get(reverse("jobs:overview"))
    assert page.status_code == 200 and ticket.number.encode() in page.content
    assert recipient.email.encode() not in page.content


def test_scheduler_identity_cannot_acquire_an_operational_role():
    actor = system_account()
    group = Group.objects.get_or_create(name="technical")[0]
    # Validate the proposed identity change only. No change is saved.
    identity_form = modelform_factory(
        User, form=AccountChangeForm, fields=("username", "email", "is_active", "groups")
    )
    form = identity_form(
        {"username": actor.username, "email": actor.email, "is_active": True, "groups": [group.pk]},
        instance=actor,
    )
    assert not form.is_valid()


def test_last_human_owner_check_excludes_the_noninteractive_account():
    owner = User.objects.create_superuser("review_owner", "owner@example.invalid", "test-only")
    actor = system_account()
    User.objects.filter(pk=actor.pk).update(is_active=True, is_staff=True, is_superuser=True)
    identity_form = modelform_factory(
        User, form=AccountChangeForm, fields=("username", "email", "is_active", "is_superuser")
    )
    form = identity_form(
        {
            "username": owner.username,
            "email": owner.email,
            "is_active": False,
            "is_superuser": True,
        },
        instance=owner,
    )
    assert not form.is_valid(), "the form would remove the last owner able to sign in"


@pytest.mark.django_db(transaction=True)
def test_concurrent_first_use_creates_one_actor():
    barrier = Barrier(2)

    def create():
        barrier.wait(timeout=10)
        return system_account().pk

    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(in_worker_session, create) for _ in range(2)]
        ids = [call.result(timeout=15) for call in calls]
    assert ids[0] == ids[1] == JobSettings.objects.get().system_account_id
    assert AuditEvent.objects.filter(action="jobs.system_account_created").count() == 1


@pytest.mark.django_db(transaction=True)
def test_settings_authority_is_stable_through_the_write(monkeypatch, make_user):
    admin = make_user("review_admin", "admin")
    membership = User.groups.through.objects.get(user=admin)
    original = JobSettings.save
    observations = []

    def save(row, *args, **kwargs):
        if row.close_resolved_after_days is not None:
            observations.append(independent(lambda: [can_lock(admin), can_lock(membership)]))
        return original(row, *args, **kwargs)

    monkeypatch.setattr(JobSettings, "save", save)
    update_settings(actor=admin, expected_revision=1, close_resolved_after_days=7)
    assert observations == [[False, False]], "account and role membership must survive the write"


@pytest.mark.parametrize("value", [Decimal("1.9"), True], ids=["fractional", "boolean"])
def test_settings_service_refuses_non_day_values(make_user, value):
    admin = make_user("review_admin", "admin")
    with pytest.raises(ValidationError):
        update_settings(actor=admin, expected_revision=1, close_resolved_after_days=value)
    assert not AuditEvent.objects.filter(action="jobs.settings_changed").exists()


@pytest.mark.parametrize("original", ["waiting_client", "resolved", "reply_and_waiting"])
def test_client_retry_uses_only_events_that_still_apply(
    agent, portal_ticket, clinic_portal, original, frozen
):
    if original == "reply_and_waiting":
        reply(agent, portal_ticket, new_status=Status.WAITING_CLIENT)
    else:
        change_status(agent, portal_ticket, original)
    notice = Notice.objects.get(recipient=clinic_portal)
    with patch.object(mail.EmailMessage, "send", side_effect=ConnectionRefusedError):
        assert outbox.attempt(notice.pk) == "retrying"
    reply(clinic_portal, portal_ticket)
    portal_ticket.refresh_from_db()
    assert portal_ticket.status == Status.OPEN
    frozen(frozen.now + timedelta(hours=3))
    notice.refresh_from_db()
    prepared = notifications.prepare_request_notice(notice)
    assert (prepared.event if prepared else None) == (
        "reply" if original == "reply_and_waiting" else None
    )


def test_backend_zero_messages_sent_keeps_notice_for_retry(agent, portal_ticket, monkeypatch):
    reply(agent, portal_ticket)
    row = Notice.objects.get()
    monkeypatch.setattr(
        "django.core.mail.backends.locmem.EmailBackend.send_messages", lambda self, messages: 0
    )
    outcome = outbox.attempt(row.pk)
    row.refresh_from_db()
    assert (outcome, row.status, row.attempts, row.sent_at) == (
        "retrying",
        NoticeStatus.WAITING,
        1,
        None,
    )


@pytest.mark.django_db(transaction=True)
def test_mail_off_survives_loss_of_the_first_callback(agent, portal_ticket):
    # The change commits durably. Its web process never gets to execute its first send.
    with override_settings(EMAIL_BACKEND=DUMMY), patch.object(outbox, "send_now", return_value={}):
        reply(agent, portal_ticket)
    outbox.deliver_due()
    row = Notice.objects.get()
    assert (row.status, row.not_sent_reason, len(mail.outbox)) == (
        NoticeStatus.NOT_SENT,
        NotSentReason.MAIL_OFF,
        0,
    )


@pytest.mark.django_db(transaction=True)
def test_delivery_eligibility_remains_stable_at_transport_handoff(
    monkeypatch, agent, portal_ticket, clinic_portal
):
    observations = []

    def transport(message):
        observations.append(independent(lambda: [can_lock(portal_ticket), can_lock(clinic_portal)]))
        return 1

    monkeypatch.setattr(mail.EmailMessage, "send", transport)
    reply(agent, portal_ticket)
    assert observations == [[False, False]], "ticket and account are both mutable at handoff"


def test_retry_delay_runs_from_each_failure_in_a_long_sweep(agent, portal_ticket, frozen):
    for _ in range(2):
        reply(agent, portal_ticket)
    second = Notice.objects.latest("pk")
    start = frozen.now

    def fail_first_then_second(message):
        if frozen.now == start:
            frozen(start + timedelta(minutes=2))
        else:
            frozen(start + timedelta(minutes=4))
        raise ConnectionRefusedError

    with patch.object(mail.EmailMessage, "send", fail_first_then_second):
        assert outbox.deliver_due()["retrying"] == 2
    second.refresh_from_db()
    assert second.next_attempt_at >= start + timedelta(minutes=2) + outbox.RETRY_DELAYS[0]


@pytest.mark.parametrize("responsible", ["assignee", "lead"])
def test_breach_retry_rechecks_the_fallback_recipient_set(
    agent, category, accessioning, join, make_user, responsible, frozen
):
    ServiceTarget.objects.create(name="Review target", resolution_minutes=1)
    replacement = make_user("review_colleague", "technical")
    join(agent, accessioning)
    membership = join(replacement, accessioning)
    ticket = new_request(agent, category, team=accessioning)
    frozen(frozen.now + timedelta(hours=1))
    assert clock_job.notice_breaches()["notices"] == 2
    row = Notice.objects.get(recipient=agent)
    assert row.data["role"] == "member"
    with patch.object(mail.EmailMessage, "send", side_effect=ConnectionRefusedError):
        assert outbox.attempt(row.pk) == "retrying"
    frozen(frozen.now + timedelta(hours=3))
    if responsible == "assignee":
        services.assign(
            actor=agent,
            request=ticket,
            team=accessioning,
            assignee=replacement,
            expected_revision=ticket.revision,
        )
    else:
        TeamMembership.objects.filter(pk=membership.pk).update(is_lead=True)
    ticket.refresh_from_db()
    assert agent.pk not in {u.pk for u, _ in notifications.breach_recipients(ticket)}
    assert notifications.prepare_breach_notice(row) is None


def test_pending_breach_belongs_to_the_run_that_raised_it(resolution_ticket, agent, frozen):
    assert clock_job.notice_breaches()["notices"] == 1
    row = Notice.objects.get(topic=notifications.BREACH_TOPIC)
    with patch.object(mail.EmailMessage, "send", side_effect=ConnectionRefusedError):
        assert outbox.attempt(row.pk) == "retrying"
    change_status(agent, resolution_ticket, Status.RESOLVED)
    assert notifications.prepare_breach_notice(row) is None
    frozen(frozen.now + timedelta(hours=3))
    change_status(agent, resolution_ticket, Status.OPEN)
    assert clock_job.notice_breaches()["notices"] == 1
    replacement = Notice.objects.exclude(pk=row.pk).get(topic=notifications.BREACH_TOPIC)
    assert notifications.prepare_breach_notice(replacement) is not None
    assert notifications.prepare_breach_notice(row) is None


def test_new_breach_after_recalculated_target_gets_a_notice(resolution_ticket, agent, frozen):
    assert clock_job.notice_breaches()["breaches"] == 1
    ServiceTarget.objects.create(name="Longer target", priority="high", resolution_minutes=240)
    ticket = resolution_ticket
    services.update_properties(
        actor=agent,
        request=ticket,
        category=ticket.category,
        priority="high",
        expected_revision=ticket.revision,
    )
    clock = RequestClock.objects.get(request=ticket)
    assert clock.resolution_due_at > frozen.now
    assert clock_job.notice_breaches()["breaches"] == 0
    frozen(clock.resolution_due_at + timedelta(minutes=1))
    assert clock_job.notice_breaches()["notices"] == 1


def test_target_at_its_exact_due_instant_is_not_breached(resolution_ticket, frozen):
    clock = RequestClock.objects.get(request=resolution_ticket)
    frozen(clock.resolution_due_at)
    assert resolution_state(clock, frozen.now) != "overdue"
    assert clock_job.notice_breaches()["breaches"] == 0


def test_eligible_tickets_progress_beyond_a_batch_of_unreviewed_complaints(
    agent, category, monkeypatch
):
    monkeypatch.setattr(close_job, "CLOSE_BATCH", 1)
    set_period(7)
    blocked = resolve(agent, new_request(agent, category), days_ago=20)
    ServiceRequest.objects.filter(pk=blocked.pk).update(is_complaint=True)
    eligible = resolve(agent, new_request(agent, category), days_ago=10)
    for _ in range(3):
        close_job.close_resolved_tickets()
    blocked.refresh_from_db()
    eligible.refresh_from_db()
    assert (blocked.status, eligible.status) == (Status.RESOLVED, Status.CLOSED)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("later_period", [None, 30])
def test_close_rechecks_period_after_discovery(agent, category, monkeypatch, later_period):
    ticket = resolve(agent, new_request(agent, category), days_ago=10)
    set_period(7)
    robot = system_account()

    def change_before_close():
        independent(lambda: set_period(later_period))
        return robot

    monkeypatch.setattr(close_job, "system_account", change_before_close)
    assert close_job.close_resolved_tickets()["closed"] == 0
    ticket.refresh_from_db()
    assert ticket.status == Status.RESOLVED


@pytest.mark.django_db(transaction=True)
def test_busy_clock_does_not_abort_the_breach_job(resolution_ticket):
    clock = RequestClock.objects.get(request=resolution_ticket)
    with transaction.atomic():
        RequestClock.objects.select_for_update().get(pk=clock.pk)
        try:
            outcome = independent(clock_job.notice_breaches)
        except OperationalError as exc:
            if exc.__cause__.sqlstate != "55P03":
                raise
            outcome = "waited until lock_timeout"
    assert isinstance(outcome, dict) and not outcome["breaches"], outcome
    assert clock_job.notice_breaches()["breaches"] == 1


@pytest.mark.django_db(transaction=True)
def test_state_write_failure_is_isolated_to_one_job():
    first, *others = runner.JOBS
    row = JobState.objects.create(name=first.name)
    with transaction.atomic():
        JobState.objects.select_for_update().get(pk=row.pk)

        def invoke():
            try:
                call_command("run_scheduled_jobs", stdout=io.StringIO())
            except CommandError:
                return "reported failed job"
            except OperationalError as exc:
                if exc.__cause__.sqlstate != "55P03":
                    raise
                return "uncaught state-write timeout"
            return "success"

        result = independent(invoke)
    completed = set(
        JobState.objects.filter(last_outcome="succeeded").values_list("name", flat=True)
    )
    assert (result, completed) == ("reported failed job", {job.name for job in others})


def test_selected_jobs_keep_registry_order_and_run_once():
    output = io.StringIO()
    chosen = ["send_notices", "close_resolved_tickets", "send_notices"]
    call_command("run_scheduled_jobs", jobs=chosen, stdout=output)
    recorded = [line.split(":", 1)[0] for line in output.getvalue().splitlines()]
    assert recorded == [job.name for job in runner.JOBS if job.name in chosen]


@pytest.mark.django_db(transaction=True)
def test_one_slow_backend_cannot_charge_one_timeout_per_recipient(
    agent, category, clinic, clinic_contact, make_user, settings
):
    for name in ("review_first", "review_second"):
        person = make_user(name, client=clinic)
        PortalAccess.objects.create(
            user=person, client=clinic, contact=clinic_contact, whole_organization=True
        )
    ticket = new_request(agent, category, client=clinic, contact=clinic_contact)
    settings.EMAIL_TIMEOUT = 10
    elapsed = 0

    def timeout(message):
        nonlocal elapsed
        elapsed += settings.EMAIL_TIMEOUT
        raise TimeoutError("synthetic transport timeout")

    with patch.object(mail.EmailMessage, "send", timeout):
        reply(agent, ticket)
    assert Notice.objects.count() == 2
    assert elapsed <= settings.EMAIL_TIMEOUT


@pytest.mark.parametrize("change", ["site_removed", "moved_organization", "restricted"])
@pytest.mark.parametrize("phase", ["first", "retry"])
def test_contact_notice_uses_current_portal_scope(
    agent, category, clinic, clinic_contact, clinic_portal, change, phase, frozen
):
    site = ClientSite.objects.create(client=clinic, code="REVIEW", name="Review site")
    grant = PortalAccess.objects.create(user=clinic_portal, client=clinic, contact=clinic_contact)
    grant.sites.add(site)
    ticket = new_request(agent, category, client=clinic, site=site, contact=clinic_contact)
    reply(agent, ticket)
    row = Notice.objects.get()
    assert notifications.prepare_request_notice(row) is not None
    if phase == "retry":
        with patch.object(mail.EmailMessage, "send", side_effect=ConnectionRefusedError):
            assert outbox.attempt(row.pk) == "retrying"
        frozen(frozen.now + timedelta(hours=3))
    if change == "site_removed":
        grant.sites.remove(site)
    elif change == "moved_organization":
        clinic_portal.changed_by = agent
        clinic_portal.client = Client.objects.create(name="Review second organization")
        clinic_portal.save()
    else:
        category.is_restricted = True
        category.save(update_fields=["is_restricted"])
    assert outbox.attempt(row.pk) == "not_sent"
    row.refresh_from_db()
    assert row.not_sent_reason == NotSentReason.NO_LONGER_APPLIES and not mail.outbox


@pytest.mark.parametrize("event", ["assigned", "client_reply"])
@pytest.mark.parametrize("change", ["role_removed", "deactivated", "opted_out", "withdrawn"])
def test_staff_notice_honors_changes_before_a_retry(
    agent, clinic_portal, portal_ticket, make_user, join, event, change, frozen
):
    if change == "withdrawn" and event == "assigned":
        # Withdrawing a reply does not rescind an independent assignment.
        expected = "sent"
    else:
        expected = "not_sent"
    assignee = make_user("review_assignee", "technical")
    join(assignee, portal_ticket.team)
    services.assign(
        actor=agent,
        request=portal_ticket,
        team=portal_ticket.team,
        assignee=assignee,
        expected_revision=portal_ticket.revision,
    )
    entry = reply(clinic_portal, portal_ticket)
    row = Notice.objects.get(recipient=assignee, events=[event])
    with patch.object(mail.EmailMessage, "send", side_effect=ConnectionRefusedError):
        assert outbox.attempt(row.pk) == "retrying"
    frozen(frozen.now + timedelta(hours=3))
    if change == "role_removed":
        assignee.groups.clear()
    elif change == "deactivated":
        assignee.is_active = False
        assignee.save(update_fields=["is_active"])
    elif change == "opted_out":
        from core.models import UserPreference

        UserPreference.objects.create(user=assignee, key=notifications.OPT_OUT_KEY, value=False)
    else:
        portal_ticket.refresh_from_db()
        services.withdraw_reply(
            actor=make_user("review_supervisor", "supervisor"),
            entry=entry,
            reason="Synthetic correction",
            expected_revision=portal_ticket.revision,
        )
    assert outbox.attempt(row.pk) == expected
    assert len(mail.outbox) == int(expected == "sent")


@pytest.mark.django_db(transaction=True)
def test_close_rechecks_reopened_ticket_after_discovery(
    agent, clinic_portal, portal_ticket, monkeypatch
):
    resolve(agent, portal_ticket, days_ago=12)
    set_period(7)
    robot = system_account()

    def reopen():
        independent(lambda: reply(clinic_portal, portal_ticket))
        return robot

    monkeypatch.setattr(close_job, "system_account", reopen)
    assert close_job.close_resolved_tickets()["closed"] == 0
    portal_ticket.refresh_from_db()
    assert portal_ticket.status == Status.OPEN


@pytest.mark.django_db(transaction=True)
def test_close_rechecks_complaint_independence_after_discovery(
    agent, category, make_user, join, monkeypatch
):
    team = ensure_default_team()
    reviewer = make_user("review_supervisor", "supervisor")
    join(agent, team)
    join(reviewer, team)
    ticket = new_request(agent, category, team=team, assignee=agent)
    services.set_complaint(
        actor=agent,
        request=ticket,
        expected_revision=ticket.revision,
        is_complaint=True,
        reason="Synthetic complaint",
    )
    ticket.refresh_from_db()
    services.decide_complaint(
        actor=agent,
        request=ticket,
        expected_revision=ticket.revision,
        decision="investigate",
        rationale="Synthetic decision",
    )
    resolve(agent, ticket, days_ago=12)
    services.review_complaint(
        actor=reviewer, request=ticket, expected_revision=ticket.revision, note="Synthetic review"
    )
    ticket.refresh_from_db()
    assert services.review_counts(ticket, ticket.complaint_review)
    set_period(7)
    robot = system_account()

    def change_assignee():
        independent(
            lambda: services.assign(
                actor=agent,
                request=ticket,
                team=team,
                assignee=reviewer,
                expected_revision=ticket.revision,
            )
        )
        return robot

    monkeypatch.setattr(close_job, "system_account", change_assignee)
    assert close_job.close_resolved_tickets() == {"closed": 0, "kept_open": 1}
    ticket.refresh_from_db()
    assert ticket.status == Status.RESOLVED


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["paused", "resolved", "recalculated", "reassigned"])
def test_breach_job_uses_clock_and_ticket_after_candidate_discovery(
    resolution_ticket, make_user, agent, join, change, monkeypatch
):
    ticket = resolution_ticket
    replacement = make_user("review_colleague", "technical")
    join(replacement, ticket.team)
    ServiceTarget.objects.create(
        name="Later high priority target", priority="high", resolution_minutes=480
    )
    robot = system_account()

    def change_ticket():
        if change == "paused":
            change_status(agent, ticket, Status.WAITING_CLIENT)
        elif change == "resolved":
            change_status(agent, ticket, Status.RESOLVED)
        elif change == "recalculated":
            services.update_properties(
                actor=agent,
                request=ticket,
                category=ticket.category,
                priority="high",
                expected_revision=ticket.revision,
            )
        else:
            services.assign(
                actor=agent,
                request=ticket,
                team=ticket.team,
                assignee=replacement,
                expected_revision=ticket.revision,
            )

    def before_lock():
        independent(change_ticket)
        return robot

    monkeypatch.setattr(clock_job, "system_account", before_lock)
    assert clock_job.notice_breaches()["breaches"] == int(change == "reassigned")
    recipients = list(
        Notice.objects.filter(topic=notifications.BREACH_TOPIC).values_list(
            "recipient_id", flat=True
        )
    )
    assert recipients == ([replacement.pk] if change == "reassigned" else [])


@pytest.mark.django_db(transaction=True)
def test_two_live_senders_make_one_transport_call(monkeypatch, portal_ticket, agent):
    with patch.object(outbox, "send_now", return_value={}):
        reply(agent, portal_ticket)
    row = Notice.objects.get()
    other = []

    def transport(message):
        # The second worker gets a real turn while the first holds the Notice row.
        other.append(independent(lambda: outbox.attempt(row.pk)))
        return 1

    monkeypatch.setattr(mail.EmailMessage, "send", transport)
    assert outbox.attempt(row.pk) == "sent"
    row.refresh_from_db()
    assert (other, row.status, row.attempts) == (["skipped"], NoticeStatus.SENT, 1)


@pytest.mark.django_db(transaction=True)
def test_pre_release_rows_survive_upgrade_and_reversal():
    head = MigrationExecutor(connection).loader.graph.leaf_nodes()
    previous_clock = ("service_levels", "0002_ticket_terminology")
    rollback = [("jobs", None), previous_clock]
    previous = [n for n in head if n[0] not in {"service_levels", "jobs"}] + [previous_clock]
    try:
        MigrationExecutor(connection).migrate(rollback)
        previous_apps = MigrationExecutor(connection).loader.project_state(previous).apps
        team = previous_apps.get_model("teams.Team").objects.create(
            slug="review-historical", name="Review historical team"
        )
        category = previous_apps.get_model("taxonomy.RequestCategory").objects.create(
            key="historical", name="Historical tickets"
        )
        user = previous_apps.get_model("accounts.User").objects.create(
            username="review_history", email="history@example.invalid", password="!"
        )
        ticket = previous_apps.get_model("desk.ServiceRequest").objects.create(
            number="REQ-900001",
            channel="email",
            subject="Historical synthetic ticket",
            category=category,
            team=team,
            created_by=user,
            revision=3,
        )
        past = timezone.now() - timedelta(days=2)
        clock = previous_apps.get_model("service_levels.RequestClock").objects.create(
            request=ticket,
            first_response_due_at=past,
            resolution_due_at=past,
            resolution_met_at=past,
            resolution_missed=True,
        )
        entry = previous_apps.get_model("desk.RequestEntry").objects.create(
            request=ticket,
            author=user,
            kind="note",
            body="Historical synthetic entry",
            author_capacity="laboratory",
        )
        event = previous_apps.get_model("core.AuditEvent").objects.create(
            actor=user,
            action="request.created",
            object_type="desk.servicerequest",
            object_id=str(ticket.pk),
            after={"number": ticket.number},
        )

        def snapshot():
            return [
                type(row).objects.values().get(pk=row.pk)
                for row in (ticket, clock, entry, event, user, team, category)
            ]

        original = snapshot()
        MigrationExecutor(connection).migrate(head)
        current = RequestClock.objects.get(pk=clock.pk)
        assert current.first_response_breach_noticed_at is not None
        assert current.resolution_breach_noticed_at is None
        assert snapshot() == original
        assert not any(model.objects.exists() for model in (JobState, JobSettings, Notice))
        MigrationExecutor(connection).migrate(rollback)
        assert snapshot() == original
        assert not set(connection.introspection.table_names()).intersection(
            {"jobs_jobstate", "jobs_jobsettings", "jobs_notice"}
        )
        with connection.cursor() as cursor:
            fields = connection.introspection.get_table_description(cursor, clock._meta.db_table)
        assert not any(field.name.endswith("_breach_noticed_at") for field in fields)
    finally:
        MigrationExecutor(connection).migrate(head)


@pytest.mark.django_db(transaction=True)
def test_empty_upgrade_and_demo_reset_with_real_commit_callbacks(monkeypatch, settings, tmp_path):
    assert demo.nonempty_tables() == []
    settings.EMAIL_BACKEND = DUMMY
    settings.MEDIA_ROOT = tmp_path
    monkeypatch.setenv("DEMO_PASSWORD", "Synthetic-shared-review-password")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "Synthetic-private-review-password")
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")

    def seeded_notice_counts():
        assert demo.is_sealed(Lab.objects.get())
        notices = Notice.objects.all()
        assert notices.exists()
        assert not notices.exclude(
            status=NoticeStatus.NOT_SENT, not_sent_reason=NotSentReason.MAIL_OFF, attempts=0
        ).exists()
        return notices.count()

    call_command("seed_demo", announce_accounts=True, stdout=io.StringIO())
    initial = seeded_notice_counts()
    actor = system_account()
    JobState.objects.create(name="review_before_reset")
    call_command("reset_demo", interactive=False, announce_accounts=True, stdout=io.StringIO())
    assert seeded_notice_counts() == initial
    assert not User.objects.filter(username=actor.username).exists()
    assert not JobSettings.objects.exists() and not JobState.objects.exists()
    settings.EMAIL_BACKEND = LOCMEM
    assert outbox.deliver_due(now=timezone.now() + timedelta(days=30))["sent"] == 0
    assert mail.outbox == []


@pytest.mark.parametrize("use", ["assignment_picker", "breach_audience"])
def test_reserved_account_is_excluded_from_human_work_even_if_its_profile_drifts(
    agent, accessioning, category, use
):
    from desk.forms import assign_choices

    robot = system_account()
    User.objects.filter(pk=robot.pk).update(is_active=True)
    robot.groups.add(Group.objects.get_or_create(name="technical")[0])
    TeamMembership.objects.create(team=accessioning, user=robot, is_lead=True)
    if use == "assignment_picker":
        offered = {value for _, group in assign_choices() for value, _ in group}
        assert f"{accessioning.pk}:{robot.pk}" not in offered
    else:
        ticket = new_request(agent, category, team=accessioning)
        audience = notifications.breach_recipients(ticket)
        assert robot.pk not in {recipient.pk for recipient, _ in audience}


def test_reopened_clock_is_noticeable_when_its_target_was_temporarily_absent(
    resolution_ticket, agent, frozen
):
    ticket = resolution_ticket
    assert clock_job.notice_breaches()["breaches"] == 1
    original_target = ServiceTarget.objects.get()
    # A category/priority switch recalculates outstanding targets. The new target only
    # measures first response. Returning to normal priority restores resolution timing.
    ServiceTarget.objects.create(
        name="First response only", priority="high", first_response_minutes=60
    )

    def change_priority(value):
        ticket.refresh_from_db()
        services.update_properties(
            actor=agent,
            request=ticket,
            category=ticket.category,
            priority=value,
            expected_revision=ticket.revision,
        )

    change_priority("high")
    assert RequestClock.objects.get(request=ticket).resolution_due_at is None
    change_status(agent, ticket, Status.RESOLVED)
    frozen(frozen.now + timedelta(days=1))
    change_status(agent, ticket, Status.OPEN)
    change_priority("normal")
    clock = RequestClock.objects.get(request=ticket)
    assert clock.target_id == original_target.pk and clock.resolution_due_at < frozen.now
    assert clock_job.notice_breaches()["notices"] == 1


def test_failed_breach_keeps_content_out_of_page_logs_audit_and_email(
    client, agent, category, clinic, clinic_contact, join, make_user, caplog, frozen
):
    ServiceTarget.objects.create(name="Synthetic audit target", resolution_minutes=10)
    join(agent, ensure_default_team())
    ticket = new_request(
        agent,
        category,
        subject="Private review subject",
        first_entry_body="Private review narrative",
        client=clinic,
        contact=clinic_contact,
        assignee=agent,
    )
    frozen(frozen.now + timedelta(hours=1))
    with patch.object(mail.EmailMessage, "send", side_effect=ConnectionError("Private SMTP text")):
        assert clock_job.notice_breaches()["notices"] == 1
        row = Notice.objects.get(topic=notifications.BREACH_TOPIC)
        assert outbox.attempt(row.pk) == "retrying"
    report = Notice.objects.values().get(pk=row.pk)
    events = AuditEvent.objects.filter(action="clock.breach_noticed")
    assert events.count() == 1
    audit = list(events.values("before", "after", "reason"))
    admin = make_user("review_admin", "admin")
    client.force_login(admin)
    response = client.get(reverse("jobs:overview"))
    page = response.content.decode()
    assert response.status_code == 200 and ticket.number in page
    row.refresh_from_db()
    assert outbox.attempt(row.pk, now=row.next_attempt_at) == "sent"
    (sent,) = mail.outbox
    assert sent.to == [agent.email]
    outputs = [str(report), str(audit), page, caplog.text, sent.subject, sent.body]
    for marker in (
        ticket.subject,
        "Private review narrative",
        clinic.name,
        clinic_contact.name,
        clinic_contact.email,
        agent.email,
        "Private SMTP text",
    ):
        assert all(marker not in value for value in outputs), marker


@pytest.mark.django_db(transaction=True)
def test_old_failure_cannot_move_a_newer_senders_retry_back_in_time(agent, portal_ticket):
    with patch.object(outbox, "send_now", return_value={}):
        reply(agent, portal_ticket)
    notice = Notice.objects.get()
    first = notice.next_attempt_at
    later = first + timedelta(hours=2)
    database = connections["default"]
    rollback = database.rollback
    observed = []

    def resume_after_other_worker():
        # A process can be descheduled after releasing the row on rollback and before its
        # recovery transaction. Another sender completes a normal failed attempt meanwhile.
        rollback()
        observed.append(independent(lambda: outbox.attempt(notice.pk, now=later)))

    with (
        patch.object(database, "rollback", resume_after_other_worker),
        patch.object(
            outbox,
            "notice_handler",
            side_effect=[
                RuntimeError("Synthetic preparation error"),
                notifications.prepare_request_notice,
            ],
        ),
        patch.object(mail.EmailMessage, "send", side_effect=TimeoutError),
    ):
        outbox.attempt(notice.pk, now=first)

    notice.refresh_from_db()
    assert observed == ["retrying"]
    assert notice.status == NoticeStatus.WAITING
    assert notice.last_attempt_at >= later
    assert notice.next_attempt_at >= later + outbox.RETRY_DELAYS[0]
