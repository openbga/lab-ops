"""First job runs and ticket details, through their normal entry points on PostgreSQL.

The identity tests supply two possible random draws. Only the random input is controlled.
The page tests let a second PostgreSQL session commit at a named stage of a real request.
"""

from datetime import date, timedelta
from html.parser import HTMLParser
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection, transaction
from django.test import Client as HttpClient
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from clients.models import PortalAccess
from core import changelog, concurrency, preferences
from core.models import AuditEvent, LabModule, UserPreference
from core.templatetags.whats_new import whats_new_button
from desk import portal_views, services, views
from desk.forms import EntryForm
from desk.jobs import close_resolved_tickets
from desk.models import EntryKind, Status
from jobs import actor
from jobs.models import JobSettings
from jobs.outbox import deliver_due
from jobs.services import update_settings
from links import services as link_services
from links.models import ExternalSystem
from nonconformance.services import open_from_complaint
from request_forms.models import FieldKind, RequestTemplate
from request_forms.services import correct_values
from request_forms.templatetags import request_forms as form_tags
from request_forms.tests.conftest import definition_of
from request_forms.tests.test_values import current, portal_raise
from service_levels.jobs import notice_breaches
from service_levels.models import RequestClock, ServiceTarget

from .test_auto_close import resolve, set_period
from .test_contract import new_request
from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by, paused

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def local_mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


def committed(work):
    """The writer must commit before the reader resumes, on a different backend."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        (reader_pid,) = cursor.fetchone()
    writer = Session(work).start()
    state = writer.waiting_or_ended(seconds=5)
    assert writer.join() == "done", writer.error
    assert state == "ended", state
    assert writer.pid != reader_pid
    return writer.result


def during_page(module, name, change, read, *, before=True):
    commits = []
    with paused(module, name, lambda: commits.append(committed(change)), before=before):
        page = read()
    assert len(commits) == 1
    return page


def while_held(change, run):
    holder = Held(change)
    job = Session(run).start()
    try:
        state = job.waiting_or_ended(seconds=5)
        assert state in {"ended", "waiting"}, job.error
        if state == "waiting":
            assert _blocked_by(job.pid, holder.session.pid)
    finally:
        holder.release()
        job.join()
    assert holder.session.outcome == job.outcome == "done", (holder.session.error, job.error)
    return state, job.result


def due_job(kind, agent, category):
    set_period(1)
    if kind == "close":
        ticket = resolve(agent, new_request(agent, category), days_ago=2)
        return close_resolved_tickets, "closed", ticket
    ServiceTarget.objects.create(name="Morning deliveries", resolution_minutes=15)
    ticket = new_request(agent, category)
    RequestClock.objects.filter(request=ticket).update(
        resolution_due_at=timezone.now() - timedelta(minutes=2)
    )
    return notice_breaches, "breaches", ticket


# Adapted (review round 1, findings 3 and 4): "suffixed" forced the random draw to equal what the
# person saves at that moment, which needs the suffix known before it is drawn. The documented
# bound (docs/architecture.md, jobs/actor.py) is that only such a guess can meet the job; the
# legacy spellings here, and test_a_person_saving_the_drawn_name_itself_is_waited_for_once below,
# cover what happens.
@pytest.mark.parametrize("namespace", ["legacy"])
@pytest.mark.parametrize("case", ["exact", "capitals"])
@pytest.mark.parametrize(
    ("kind", "field", "operation"),
    [
        ("breach", "username", "create"),
        ("breach", "email", "edit"),
        ("close", "username", "edit"),
        ("close", "email", "create"),
    ],
)
def test_first_job_during_a_persons_identity_change(
    namespace, case, kind, field, operation, agent, category, monkeypatch
):
    run, counter, ticket = due_job(kind, agent, category)
    suffixes = iter(["aabbccdd", "bbccddee"])
    monkeypatch.setattr(
        actor, "secrets", SimpleNamespace(token_hex=lambda size: next(suffixes)), raising=False
    )
    name = "scheduled-jobs" if namespace == "legacy" else "scheduled-jobs-aabbccdd"
    value = name if field == "username" else f"{name}@lab-ops.invalid"
    if case == "capitals":
        value = value.upper()
    person = User(username="delivery", email="delivery@example.invalid")
    if operation == "edit":
        person.save()

    def save_person():
        setattr(person, field, value)
        person.save(update_fields=[field] if operation == "edit" else None)

    state, first = while_held(save_person, run)
    assert first[counter] + run()[counter] == 1
    person.refresh_from_db()
    system = actor.system_account()
    event = AuditEvent.objects.get(action="jobs.system_account_created")
    assert event.actor_id == system.pk == JobSettings.objects.get().system_account_id
    assert system.pk != person.pk and not system.is_active and person.is_active
    if kind == "close":
        ticket.refresh_from_db()
        assert ticket.status == Status.CLOSED
    else:
        assert RequestClock.objects.get(request=ticket).resolution_breach_noticed_at
    assert state == "ended", "the person's uncommitted account change blocked the job"
    assert getattr(system, field).casefold() != getattr(person, field).casefold()


def test_first_automatic_close_waits_for_a_settings_edit_and_uses_its_period(
    agent, category, make_user
):
    # Adapted (review round 1, finding 5): the automatic close locks the settings row for each
    # close and decides from the period in force (docs/architecture.md, Scheduled jobs; the
    # deferral on a settings save is the breach job's), so it waits for the save and closes in
    # the same run under the new period.
    run, _, ticket = due_job("close", agent, category)
    admin = make_user("period_editor", "admin")
    row = JobSettings.objects.get()
    state, first = while_held(
        lambda: update_settings(
            actor=admin, expected_revision=row.revision, close_resolved_after_days=2
        ),
        run,
    )
    later = run()
    ticket.refresh_from_db()
    assert ticket.status == Status.CLOSED and first["closed"] + later["closed"] == 1
    assert (state, first["closed"], later["closed"]) == ("waiting", 1, 0)


def test_overlapping_first_jobs_share_one_attributed_account(agent, category):
    close, _, _ = due_job("close", agent, category)
    ServiceTarget.objects.create(name="Morning deliveries", resolution_minutes=15)
    due = new_request(agent, category)
    RequestClock.objects.filter(request=due).update(
        resolution_due_at=timezone.now() - timedelta(minutes=2)
    )
    state, first = while_held(close, notice_breaches)
    assert state == "ended" and first["breaches"] == 0
    assert notice_breaches()["breaches"] == 1
    account_id = JobSettings.objects.get().system_account_id
    assert AuditEvent.objects.get(action="jobs.system_account_created").actor_id == account_id
    assert (
        AuditEvent.objects.get(action="request.status_changed", after={"status": "closed"}).actor_id
        == account_id
    )


@pytest.fixture
def ticket(agent, category, clinic, clinic_portal):
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    return new_request(agent, category, client=clinic)


def location(ticket, audience, post=False):
    if audience == "portal":
        return reverse(
            "portal:request-reply" if post else "portal:request-detail", args=[ticket.number]
        )
    return reverse("desk:request-entry" if post else "desk:request-detail", args=[ticket.pk])


def change_status(agent, ticket, status):
    return services.change_status(
        actor=agent, request=ticket, expected_revision=ticket.revision, status=status
    )


@pytest.mark.parametrize("atomic", [False, True], ids=["default", "atomic-requests"])
@pytest.mark.parametrize("audience", ["portal", "staff"])
@pytest.mark.parametrize("method", ["GET", "invalid", "stale"])
def test_status_and_timeline_agree_on_detail_and_refusal(
    client, agent, clinic_portal, ticket, audience, method, atomic, monkeypatch
):
    client.force_login(clinic_portal if audience == "portal" else agent)
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", atomic)
    initial = ticket.revision
    draft = "Please check the delivery."
    url = location(ticket, audience, post=method != "GET")
    if method == "stale":
        change_status(agent, ticket, Status.WAITING_THIRD_PARTY)
        ticket.refresh_from_db()

    def request_page():
        if method == "GET":
            return client.get(url)
        fields = {
            "expected_revision": initial,
            "kind": "note",
            "body": draft if method == "stale" else "",
        }
        return client.post(url, fields)

    module, stage = (
        (portal_views, "_conversation") if audience == "portal" else (views, "with_files")
    )
    before = ticket.status
    page = during_page(
        module, stage, lambda: change_status(agent, ticket, Status.WAITING_CLIENT), request_page
    )
    assert page.status_code == (400 if audience == "staff" and method != "GET" else 200)
    record = page.context["service_request" if audience == "portal" else "req"]
    assert record.status == before
    ticket.refresh_from_db()
    assert ticket.status == Status.WAITING_CLIENT and record.revision + 1 == ticket.revision
    form = page.context["reply_form" if audience == "portal" else "composer"]
    assert int(form["expected_revision"].value()) == initial
    if method == "stale":
        assert draft.encode() in page.content and not ticket.entries.filter(body=draft).exists()
    if audience == "portal":
        assert "no-store" in page["Cache-Control"]
        seen_late = any(
            item["kind"] == "status" and item["status"][0] == "Waiting for you"
            for item in page.context["conversation"]
        )
    else:
        assert "private" in page["Cache-Control"]
        seen_late = any(
            entry.event_data.get("new") == Status.WAITING_CLIENT
            for entry in page.context["entries"]
            if entry.kind == EntryKind.EVENT
        )
    assert not seen_late, "the page shows the new status event beside the old ticket status"


def test_refused_staff_entry_on_just_closed_ticket_only_offers_note(client, agent, ticket):
    ticket = resolve(agent, ticket, days_ago=1)
    client.force_login(agent)
    draft = "Please collect the paperwork."
    page = during_page(
        EntryForm,
        "is_valid",
        lambda: change_status(agent, ticket, Status.CLOSED),
        lambda: client.post(
            location(ticket, "staff", post=True),
            {
                "kind": "reply",
                "body": draft,
                "expected_revision": ticket.revision,
            },
        ),
    )
    assert page.status_code == 400 and page.context["is_closed"]
    assert draft.encode() in page.content and not ticket.entries.filter(body=draft).exists()
    assert int(page.context["composer"]["expected_revision"].value()) == ticket.revision
    assert [key for key, label in page.context["composer"].fields["kind"].choices] == ["note"]


@pytest.mark.parametrize("audience", ["staff", "portal"])
def test_form_details_correction_during_render(client, agent, category, clinic_portal, audience):
    template = RequestTemplate.objects.create(key="delivery", name="Delivery", category=category)
    template.fields.create(key="destination", label="Destination", kind=FieldKind.SHORT_TEXT)
    original = "North delivery desk"
    changed = "West delivery desk"
    ticket = portal_raise(clinic_portal, template, {"destination": original})
    expected = current(ticket)["destination"].pk
    client.force_login(clinic_portal if audience == "portal" else agent)
    url = location(ticket, audience)

    def correct():
        correct_values(
            actor=agent,
            request=ticket,
            expected_definition=definition_of(ticket),
            values={"destination": changed},
            expected={"destination": expected},
            reason="Delivery moved",
        )

    page = during_page(
        form_tags,
        "client_values" if audience == "portal" else "staff_values",
        correct,
        lambda: client.get(url),
    )
    assert page.status_code == 200
    form = page.context["reply_form" if audience == "portal" else "composer"]
    assert int(form["expected_revision"].value()) == ticket.revision
    ticket.refresh_from_db()
    assert ticket.revision == int(form["expected_revision"].value()) + 1
    assert original.encode() in page.content and changed.encode() not in page.content
    assert changed.encode() in client.get(url).content


def reference(agent, ticket, shown):
    system = ExternalSystem.objects.create(key="delivery", name="Delivery desk", kind="other")
    attachment, _ = link_services.add_link(
        actor=agent,
        request_id=ticket.pk,
        system=system,
        record_type="order",
        identifier="ORD-TEST-000901",
        shown_to_client=shown,
    )
    return attachment


@pytest.mark.parametrize("initially_shown", [True, False])
def test_portal_references_use_the_page_snapshot(
    client, agent, clinic_portal, ticket, initially_shown
):
    attachment = reference(agent, ticket, initially_shown)
    client.force_login(clinic_portal)
    url = location(ticket, "portal")
    page = during_page(
        portal_views,
        "_shown_links",
        lambda: link_services.set_shown_to_client(
            actor=agent, attachment_id=attachment.pk, shown=not initially_shown
        ),
        lambda: client.get(url),
    )
    assert (b"ORD-TEST-000901" in page.content) == initially_shown
    assert (b"ORD-TEST-000901" in client.get(url).content) != initially_shown


@pytest.mark.parametrize("action", ["attach", "show", "hide"])
def test_portal_reply_checks_the_revision_of_its_references(
    client, agent, clinic_portal, ticket, action
):
    attachment = reference(agent, ticket, action == "hide") if action != "attach" else None
    client.force_login(clinic_portal)
    url = location(ticket, "portal")
    page = client.get(url)
    assert (b"ORD-TEST-000901" in page.content) == (action == "hide")
    if attachment:
        committed(
            lambda: link_services.set_shown_to_client(
                actor=agent, attachment_id=attachment.pk, shown=action == "show"
            )
        )
    else:
        committed(lambda: reference(agent, ticket, True))
    assert (b"ORD-TEST-000901" in client.get(url).content) == (action != "hide")
    draft = "I checked the references shown."
    posted = client.post(
        page.context["reply_url"],
        {
            "body": draft,
            "expected_revision": page.context["reply_form"]["expected_revision"].value(),
        },
    )
    assert (posted.status_code, ticket.entries.filter(body=draft).count()) == (200, 0)


def test_closed_parent_lists_follow_ups_from_the_page_snapshot(
    client, agent, category, ticket, clinic_portal
):
    ticket = resolve(agent, ticket, days_ago=1)
    ticket = change_status(agent, ticket, Status.CLOSED)
    client.force_login(clinic_portal)
    url = location(ticket, "portal")
    children = []

    def follow_up():
        children.append(
            new_request(
                clinic_portal,
                category,
                follow_up_of=ticket,
                channel="portal",
                first_entry_kind=EntryKind.REPLY,
            )
        )

    page = during_page(portal_views, "_signature", follow_up, lambda: client.get(url))
    assert page.context["is_closed"] and page.context["reply_form"] is None
    number = children[0].number.encode()
    assert number not in page.content and number in client.get(url).content


@pytest.mark.parametrize("rolling_update", [False, True], ids=["same-release", "rolling-release"])
def test_two_first_tabs_initialize_the_seen_marker_once(client, agent, ticket, rolling_update):
    client.force_login(agent)
    tab = HttpClient()
    tab.cookies = client.cookies.copy()
    entries = [
        changelog.Entry(
            id=f"2026-11-{day}-delivery",
            date=date(2026, 11, day),
            type="changed",
            impact="low",
            title="Delivery hours",
            summary="Updated collection hours.",
        )
        for day in (20, 19)
    ]
    saved = UserPreference.objects.filter(user=agent, key=preferences.WHATS_NEW_KEY)
    snapshots = []
    assert not saved.exists()

    def second_tab():
        with patch.object(changelog, "entries", return_value=entries):
            page = tab.get(reverse("whats-new") if rolling_update else location(ticket, "staff"))
            assert page.status_code == 200
        snapshots.append(saved.values_list("value", "updated_at").get())

    with patch.object(
        changelog, "entries", return_value=entries[1:] if rolling_update else entries
    ):
        # Adapted: the first page's mark is written by save_if_absent (the fix), paused there.
        page = during_page(
            preferences,
            "save_if_absent",
            second_tab,
            lambda: client.get(location(ticket, "staff")),
        )
    assert page.status_code == 200 and snapshots[0][0] == changelog.seen_marker(entries)
    assert saved.values_list("value", "updated_at").get() == snapshots[0]


def test_first_shell_started_in_a_commit_callback_saves_in_its_callers_transaction(agent):
    """A callback of the finished reading starts a new, unrelated transaction."""
    request = RequestFactory().get("/")
    request.user = agent
    saved = UserPreference.objects.filter(user=agent, key=preferences.WHATS_NEW_KEY)
    observed = []

    def render_another_shell():
        with transaction.atomic():
            whats_new_button({"request": request})
            observed.append(saved.exists())

    with concurrency.one_reading():
        transaction.on_commit(render_another_shell)
    assert saved.count() == 1
    assert observed == [True]


@pytest.mark.parametrize("audience", ["portal", "staff"])
@pytest.mark.parametrize("atomic", [False, True])
def test_detail_sql_and_connection_after_success(
    client, agent, clinic_portal, ticket, audience, atomic, monkeypatch
):
    """Record the isolation at real write statements, including the later session save."""
    user = clinic_portal if audience == "portal" else agent
    client.force_login(user)
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", atomic)
    writes = []
    isolation = []
    events = AuditEvent.objects.count()

    def observe(execute, sql, params, many, context):
        if sql.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            with connection.cursor() as cursor:
                cursor.execute("SHOW transaction_isolation")
                writes.append((cursor.fetchone()[0], sql))
        return execute(sql, params, many, context)

    def inspect():
        with connection.cursor() as cursor:
            cursor.execute("SHOW transaction_isolation")
            isolation.append(cursor.fetchone()[0])

    module, name = (
        (portal_views, "_conversation") if audience == "portal" else (views, "with_files")
    )
    with connection.execute_wrapper(observe), paused(module, name, inspect):
        page = client.get(location(ticket, audience))
    assert page.status_code == 200 and events == AuditEvent.objects.count()
    assert all(level == "read committed" for level, _ in writes)
    mark = UserPreference.objects.filter(user=user, key=preferences.WHATS_NEW_KEY)
    assert mark.exists() == (audience == "staff")
    assert not connection.in_atomic_block and connection.get_autocommit()
    inspect()
    assert isolation == ["repeatable read", "read committed"]


@pytest.mark.parametrize("outcome", ["403", "404", "exception", "redirect"])
def test_request_finishes_on_the_same_backend_with_default_isolation(
    client, agent, ticket, make_user, outcome
):
    staff_url = location(ticket, "staff")
    if outcome != "redirect":
        client.force_login(make_user("visitor") if outcome == "403" else agent)

    def backend():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid(), current_setting('transaction_isolation')")
            return cursor.fetchone()

    initial = backend()
    if outcome == "exception":

        class DrawingError(Exception):
            pass

        with (
            patch.object(views, "with_files", side_effect=DrawingError),
            pytest.raises(DrawingError),
        ):
            client.get(staff_url)
    else:
        page = client.get(
            reverse("desk:request-detail", args=[ticket.pk + 1]) if outcome == "404" else staff_url
        )
        assert page.status_code == (302 if outcome == "redirect" else int(outcome))
    assert backend() == initial and initial[1] == "read committed"
    assert not connection.in_atomic_block and connection.get_autocommit()
    if hasattr(concurrency, "after_reading"):
        occurred = []
        with transaction.atomic():
            concurrency.after_reading(lambda: occurred.append(True))
            assert occurred == [True]


def test_migrations_and_empty_outbox_need_no_new_identity():
    call_command("makemigrations", check_changes=True, dry_run=True, stdout=StringIO())
    deliver_due()
    assert not JobSettings.objects.exclude(system_account=None).exists()
    assert not AuditEvent.objects.filter(action="jobs.system_account_created").exists()


class PostedFields(HTMLParser):
    """The hidden inputs in the form that submits to the given action."""

    def __init__(self, response, action):
        super().__init__()
        self.action = action
        self.matches = 0
        self.inside = False
        self.fields = {}
        self.feed(response.content.decode())
        assert self.matches == 1

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.inside = attrs.get("action") == self.action
            self.matches += int(self.inside)
        if self.inside and tag == "input" and attrs.get("type") == "hidden":
            self.fields[attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False


def test_inline_reference_control_checks_the_ticket_it_was_drawn_on(client, agent, ticket):
    attachment = reference(agent, ticket, True)
    client.force_login(agent)
    page = client.get(location(ticket, "staff"))
    action = reverse("link_records:attachment-shown", args=[attachment.pk])
    fields = PostedFields(page, action).fields
    assert fields["shown"] == "0"
    # Adapted: attaching and showing the reference advanced the revision (finding 2's fix).
    ticket.refresh_from_db()
    committed(lambda: change_status(agent, ticket, Status.WAITING_CLIENT))
    posted = client.post(action, fields)
    assert posted.status_code < 500
    attachment.refresh_from_db()
    assert (fields.get("expected_revision"), attachment.shown_to_client) == (
        str(ticket.revision),
        True,
    )


def test_complaint_actions_and_take_use_the_same_reading_as_the_timeline(
    client, make_user, category, accessioning, join
):
    lead = make_user("delivery_lead", "supervisor")
    join(lead, accessioning)
    category.is_complaint = True
    category.save(update_fields=["is_complaint"])
    ticket = new_request(lead, category, team=accessioning)
    LabModule.objects.create(key="nonconformance", is_enabled=True)
    client.force_login(lead)
    url = location(ticket, "staff")
    take = reverse("desk:request-take", args=[ticket.pk])
    title = "Loading bay collection"
    opened = []

    def open_record():
        opened.append(
            open_from_complaint(
                actor=lead,
                request=ticket,
                expected_revision=ticket.revision,
                title=title,
                description="Collection delayed",
                severity="minor",
            )
        )

    page = during_page(views, "_nonconformance_context", open_record, lambda: client.get(url))
    fields = PostedFields(page, take).fields
    assert (
        int(fields["expected_revision"])
        == page.context["composer"]["expected_revision"].value()
        == ticket.revision
    )
    assert opened[0].number.encode() not in page.content
    assert reverse("nonconformance:from-ticket", args=[ticket.pk]).encode() in page.content
    assert title.encode() in client.get(url).content
    client.post(take, fields)
    ticket.refresh_from_db()
    assert ticket.assignee_id is None
    for route in (
        "desk:request-assign",
        "desk:request-status",
        "desk:request-properties",
        "desk:complaint-record",
    ):
        action = reverse(route, args=[ticket.pk])
        assert action.encode() in page.content
        dialog = client.get(action)
        assert dialog.status_code == 200
        assert int(dialog.context["form"]["expected_revision"].value()) == ticket.revision


@pytest.mark.parametrize("audience", ["portal", "staff"])
def test_signed_in_display_name_is_reread_before_drawing(
    client, agent, clinic_portal, ticket, audience
):
    user = clinic_portal if audience == "portal" else agent
    client.force_login(user)
    url = location(ticket, audience)
    new_name = "Delivery Reader"

    def change():
        User.objects.filter(pk=user.pk).update(first_name=new_name)

    page = during_page(preferences, "for_request", change, lambda: client.get(url), before=False)
    assert page.status_code == 200
    assert page.context["user"].first_name == new_name and new_name.encode() in page.content


@pytest.mark.parametrize("grant", ["organization", "site", "team", "role"])
@pytest.mark.parametrize("when", ["before", "during"])
def test_reading_boundary_for_ticket_membership(
    client,
    agent,
    clinic,
    clinic_site,
    clinic_portal,
    category,
    privacy_category,
    quality,
    join,
    grant,
    when,
):
    portal = grant in {"organization", "site"}
    if portal:
        access = PortalAccess.objects.create(
            user=clinic_portal, client=clinic, whole_organization=grant == "organization"
        )
        if grant == "site":
            access.sites.add(clinic_site)
        ticket = new_request(agent, category, client=clinic, site=clinic_site)

        def remove():
            if grant == "site":
                access.sites.remove(clinic_site)
            else:
                access.whole_organization = False
                access.save(update_fields=["whole_organization"])
    elif grant == "team":
        membership = join(agent, quality)
        ticket = new_request(agent, privacy_category, team=quality)

        def remove():
            membership.delete()
    else:
        ticket = new_request(agent, category)

        def remove():
            agent.groups.clear()

    client.force_login(clinic_portal if portal else agent)
    audience = "portal" if portal else "staff"
    url = location(ticket, audience)
    expected = 403 if grant == "role" else 404
    assert client.get(url).status_code == 200
    if when == "before":
        page = during_page(
            preferences, "for_request", remove, lambda: client.get(url), before=False
        )
        assert page.status_code == expected
    else:
        module, name = (portal_views, "_conversation") if portal else (views, "with_files")
        page = during_page(module, name, remove, lambda: client.get(url))
        assert page.status_code == 200
    refused = client.get(url)
    assert refused.status_code == expected and ticket.subject.encode() not in refused.content
    before = ticket.entries.count()
    posted = client.post(
        location(ticket, audience, post=True),
        {"kind": "note", "body": "Delivery check", "expected_revision": ticket.revision},
    )
    assert posted.status_code == expected and ticket.subject.encode() not in posted.content
    assert before == ticket.entries.count()


@pytest.mark.parametrize("when", ["before", "during"])
def test_patient_controls_follow_the_account_reading(
    client, agent, category, clinic_portal, when, caplog
):
    """Check both panels' controls using an ordinary word in a synthetic, marked field."""
    template = RequestTemplate.objects.create(key="delivery", name="Delivery", category=category)
    template.fields.create(
        key="label", label="Test label", kind=FieldKind.SHORT_TEXT, holds_patient_information=True
    )
    value = "Violet basket"
    ticket = portal_raise(clinic_portal, template, {"label": value})
    reveal = reverse("request_details:reveal", args=[current(ticket)["label"].pk]).encode()
    grant = Permission.objects.get(
        content_type__app_label="accounts", codename="view_patient_information"
    )
    agent.user_permissions.add(grant)
    client.force_login(agent)
    url = location(ticket, "staff")
    link = reverse("link_records:person-search").encode()
    first = client.get(url)
    assert link in first.content and reveal in first.content

    def remove():
        User.objects.get(pk=agent.pk).user_permissions.remove(grant)

    if when == "before":
        page = during_page(
            preferences, "for_request", remove, lambda: client.get(url), before=False
        )
    else:
        page = during_page(views, "with_files", remove, lambda: client.get(url))
    assert page.status_code == 200
    assert (link in page.content) == (when == "during")
    assert (reveal in page.content) == (when == "during")
    again = client.get(url)
    assert link not in again.content and reveal not in again.content
    for response in (first, page, again):
        assert value.encode() not in response.content and value not in str(dict(response.headers))
    assert value not in caplog.text
    assert not AuditEvent.objects.filter(action="request_form.value_revealed").exists()


@pytest.mark.parametrize("audience", ["portal", "staff"])
def test_ended_sign_in_refuses_detail_and_post(client, agent, ticket, clinic_portal, audience):
    user = clinic_portal if audience == "portal" else agent
    client.force_login(user)
    user.is_active = False
    user.save(update_fields=["is_active"])
    before = ticket.entries.count()
    for page in (
        client.get(location(ticket, audience)),
        client.post(
            location(ticket, audience, post=True),
            {"kind": "note", "body": "Delivery check", "expected_revision": ticket.revision},
        ),
    ):
        assert page.status_code == 302 and ticket.subject.encode() not in page.content
    assert ticket.entries.count() == before


def test_portal_closure_during_get_preserves_the_later_refused_draft_and_file(
    client, agent, clinic_portal, ticket
):
    ticket = resolve(agent, ticket, days_ago=1)
    client.force_login(clinic_portal)
    url = location(ticket, "portal")
    opened = during_page(
        portal_views,
        "_conversation",
        lambda: change_status(agent, ticket, Status.CLOSED),
        lambda: client.get(url),
    )
    assert (
        opened.context["can_reply"] and opened.context["service_request"].status == Status.RESOLVED
    )
    draft = "Delivery receipt enclosed."
    refused = client.post(
        opened.context["reply_url"],
        {
            "expected_revision": opened.context["reply_form"]["expected_revision"].value(),
            "body": draft,
            "files": SimpleUploadedFile("delivery.txt", b"Delivery receipt"),
        },
    )
    assert refused.status_code == 200 and "no-store" in refused["Cache-Control"]
    assert refused.context["is_closed"] and refused.context["reply_form"] is None
    assert refused.context["unsent_reply"]["body"].value() == draft
    assert not ticket.entries.filter(body=draft).exists()
    assert not ticket.entries.filter(attachments__isnull=False).exists()
    assert b"Status: Closed" in refused.content and b"Status: Closed" not in opened.content
