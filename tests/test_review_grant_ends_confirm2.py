"""Adopted from the independent review of feat/grant-end-dates, second confirmation round (report
.local/review-history/grant-ends-confirm2.md; original archived beside it). Changes on adoption:
the seed's days count from the later of today by the application's clock and by the database's
(accounts.grant_ends.later_today), so the expected ends add the days the application's clock is
ahead (`ahead`); the case of an application clock over a century behind is a strict xfail of the
limit later_today states.

Second confirmation of grant end dates, including persisted history and database time."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta
from io import StringIO
from threading import Event, local
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from accounts import acting, grant_ends
from accounts.admin import CHANGED_SINCE_DRAWN, end_field, shown_field
from accounts.models import CapabilityEnd, User
from accounts.roles import has_capability
from accounts.tests.test_confirm_identity import page_data
from audit_review.models import AccessReview, AccessScope
from clients.models import Client as Organization
from clients.models import PortalAccess
from clients.portal import current_access
from clients.services import reset_portal_access
from core.models import AuditEvent, Lab
from tests.test_review_grant_ends import (
    ACCT_REVIEW,
    acct_end,
    acct_events,
    acct_form,
    acct_http,
    acct_perm,
    acct_user,
    portal_edit_url,
    portal_set,
)
from tests.test_review_grant_ends_confirm import path_parties as path_parties
from tests.test_review_grant_ends_verify import (
    receiver_gate_held,
    verify_posted_hidden,
    vportal_data,
)

# Account form retries


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("send_marker", "choose_temporary_day"),
    [(True, True), (False, False), (False, True)],
    ids=["current-form", "legacy-other-choice", "legacy-matching-temporary-date"],
)
def test_correcting_an_email_on_a_redrawn_account_form_keeps_the_requested_date(
    monkeypatch, send_marker, choose_temporary_day
):
    account = acct_user("date-retry-subject", is_active=False)
    editor = acct_http("date-retry-editor")
    coworker = acct_http("date-retry-coworker")
    original_day = grant_ends.today() + timedelta(days=14)
    temporary_day = original_day + timedelta(days=10)
    requested_day = temporary_day if choose_temporary_day else temporary_day + timedelta(days=1)
    saved = acct_end(account, ACCT_REVIEW, grant_ends.end_of(original_day))
    field, marker = end_field(ACCT_REVIEW), shown_field(ACCT_REVIEW)
    url, drawing = acct_form(editor, account)
    assert drawing.initial[field] == drawing.initial[marker] == original_day
    pending = page_data(drawing, email="incomplete", **{field: requested_day})
    if not send_marker:
        pending.pop(marker)

    _, before_edit = acct_form(coworker, account)
    assert coworker.post(url, page_data(before_edit, **{field: temporary_day})).status_code == 302
    _, before_undo = acct_form(coworker, account)
    assert before_undo.initial[field] == temporary_day
    undo = page_data(before_undo, **{field: original_day})

    ready, resume = Event(), Event()
    worker = local()
    take_gate = acting.close_access

    def wait_for_correction(ids):
        ids = list(ids)
        if getattr(worker, "submitting", False) and account.pk in ids and not ready.is_set():
            ready.set()
            assert resume.wait(15)
        return take_gate(ids)

    monkeypatch.setattr(acting, "close_access", wait_for_correction)

    def submit():
        worker.submitting = True
        try:
            return editor.post(url, pending)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        response = executor.submit(submit)
        try:
            assert ready.wait(15)
            assert coworker.post(url, undo).status_code == 302
            saved.refresh_from_db()
            assert saved.ends_at == grant_ends.end_of(original_day)
        finally:
            resume.set()
        refused = response.result(timeout=15)

    assert refused.status_code == 200
    redrawn = refused.context["adminform"].form
    assert acct_events(account).count() == 2
    if CHANGED_SINCE_DRAWN in redrawn.non_field_errors():
        # A form that explicitly asks to be reopened cannot silently discard the edit.
        return
    assert set(redrawn.errors) == {"email"}
    assert str(redrawn[field].value()) == requested_day.isoformat()
    # The user fixes only the email and submits what the error page actually drew.
    assert str(redrawn[marker]) in refused.content.decode()
    rendered_marker = verify_posted_hidden(redrawn, marker)
    retry = page_data(redrawn, email="corrected@example.invalid", **{marker: rendered_marker})
    response = editor.post(url, retry)
    if response.status_code == 200:
        assert CHANGED_SINCE_DRAWN in response.context["adminform"].form.non_field_errors()
        assert acct_events(account).count() == 2
        return
    assert response.status_code == 302
    account.refresh_from_db()
    saved.refresh_from_db()
    assert account.email == "corrected@example.invalid"
    event = acct_events(account).get(actor__username="date-retry-editor")
    assert saved.ends_at == grant_ends.end_of(requested_day), (
        f"Requested {requested_day}; redrawn marker {rendered_marker}; "
        f"accepted edit recorded {event.after['capability_ends']}"
    )
    assert event.before["capability_ends"] == {
        f"accounts.{ACCT_REVIEW}": grant_ends.iso(grant_ends.end_of(original_day))
    }
    assert event.after["capability_ends"] == {
        f"accounts.{ACCT_REVIEW}": grant_ends.iso(saved.ends_at)
    }


@pytest.mark.django_db
@pytest.mark.parametrize("send_marker", [False, True], ids=["legacy", "current"])
def test_a_permanently_changed_account_date_refuses_a_corrected_retry(send_marker):
    account = acct_user("permanent-date-subject", is_active=False)
    first = acct_http("permanent-date-first")
    second = acct_http("permanent-date-second")
    field, marker = end_field(ACCT_REVIEW), shown_field(ACCT_REVIEW)
    original_day = grant_ends.today() + timedelta(days=14)
    current_day = original_day + timedelta(days=7)
    stored = acct_end(account, ACCT_REVIEW, grant_ends.end_of(original_day))
    url, earlier = acct_form(first, account)
    pending = page_data(earlier, email="incomplete", **{field: current_day + timedelta(days=7)})
    if not send_marker:
        pending.pop(marker)
    _, current = acct_form(second, account)
    assert second.post(url, page_data(current, **{field: current_day})).status_code == 302
    refused = first.post(url, pending)
    assert refused.status_code == 200
    form = refused.context["adminform"].form
    assert CHANGED_SINCE_DRAWN in form.non_field_errors()
    assert "email" in form.errors
    corrected = page_data(
        form, email="corrected@example.invalid", **{marker: verify_posted_hidden(form, marker)}
    )
    retry = first.post(url, corrected)
    assert retry.status_code == 200
    assert CHANGED_SINCE_DRAWN in retry.context["adminform"].form.non_field_errors()
    stored.refresh_from_db()
    account.refresh_from_db()
    assert stored.ends_at == grant_ends.end_of(current_day)
    assert account.email == "permanent-date-subject@example.invalid"
    assert acct_events(account).count() == 1


# Portal writes, history and rollback


@pytest.mark.parametrize("has_end", [False, True], ids=["undated", "dated"])
def test_new_portal_events_record_both_end_values_across_reset_and_regrant(path_parties, has_end):
    org, manager, person = path_parties
    destination = Organization.objects.create(name="Destination practice")
    day = grant_ends.database_today() + timedelta(days=15)
    with override_settings(TIME_ZONE="America/New_York"):
        first = portal_set(path_parties, day if has_end else None)
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        # Check both a reset by the service and the attributed reset after the move trigger.
        reset_portal_access(actor=manager, user_id=person.pk, expected_client_id=org.pk)
        assert (
            reset_portal_access(actor=manager, user_id=person.pk, expected_client_id=org.pk) is None
        )
        renewed = portal_set(path_parties, day)
        person.client = destination
        person.changed_by = manager
        person.save(update_fields=["client"])
    events = AuditEvent.objects.filter(object_type="clients.portalaccess", object_id=str(first.pk))
    pairs = [
        (event.action, event.before["ends_at"], event.after["ends_at"], event.actor_id)
        for event in events.order_by("occurred_at", "id")
    ]
    first_end, renewed_end = grant_ends.iso(first.ends_at), grant_ends.iso(renewed.ends_at)
    assert first_end != renewed_end
    assert pairs == [
        ("portal_access.set", None, first_end, manager.pk),
        ("portal_access.reset", first_end, None, manager.pk),
        ("portal_access.set", None, renewed_end, manager.pk),
        ("portal_access.reset", renewed_end, None, manager.pk),
    ]
    first.refresh_from_db()
    assert first.ends_at is None and first.client_id == destination.pk
    assert first.revision == 4


def test_mixed_bulk_account_update_resets_only_rows_that_moved(path_parties):
    org, manager, moving = path_parties
    destination = Organization.objects.create(name="Transfer destination")
    staying = User.objects.create_user(
        "stationary", "stationary@example.invalid", client=destination
    )
    no_grant = User.objects.create_user("without-access", "no-access@example.invalid", client=org)
    day = grant_ends.database_today() + timedelta(days=12)
    moved_grant = portal_set(path_parties, day)
    kept_grant = portal_set((destination, manager, staying), day)
    original_revision, original_end = kept_grant.revision, kept_grant.ends_at
    with transaction.atomic():
        count = User.objects.filter(pk__in=[moving.pk, staying.pk, no_grant.pk]).update(
            client=destination
        )
        assert count == 3
        for account in (moving, staying, no_grant):
            assert not receiver_gate_held(account.pk)
        moved_grant.refresh_from_db()
        kept_grant.refresh_from_db()
        assert moved_grant.ends_at is None and moved_grant.revision == 2
        assert not moved_grant.whole_organization and moved_grant.client_id == destination.pk
        assert kept_grant.ends_at == original_end and kept_grant.revision == original_revision
        assert kept_grant.whole_organization
        assert not PortalAccess.objects.filter(user=no_grant).exists()
    assert not AuditEvent.objects.filter(action="portal_access.reset").exists()


@pytest.mark.django_db(transaction=True)
def test_old_schema_group_and_direct_memberships_survive_reapplying_grant_ends():
    person = acct_user("rollback-reader", is_active=False)
    group = Group.objects.create(name="Synthetic review group")
    perm = acct_perm(ACCT_REVIEW)
    person.groups.add(group)
    group.permissions.add(perm)
    dated = acct_end(person, ACCT_REVIEW, grant_ends.end_of(grant_ends.database_today()))
    assert dated.user_id == person.pk
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    prior = [("accounts", "0010_review_capability")]
    try:
        executor.migrate(prior)
        prior_apps = executor.loader.project_state(prior).apps
        old_user = prior_apps.get_model("accounts", "User")
        old_group = prior_apps.get_model("auth", "Group")
        memberships = old_user.user_permissions.through.objects
        additional = old_user.objects.create(
            username="between-versions", email="second@example.invalid", is_active=False
        )
        memberships.create(user_id=additional.pk, permission_id=perm.pk)
        assert memberships.filter(user_id=person.pk, permission_id=perm.pk).exists()
        assert old_group.permissions.through.objects.filter(
            group_id=group.pk, permission_id=perm.pk
        ).exists()
    finally:
        MigrationExecutor(connection).migrate(leaves)

    assert set(
        User.user_permissions.through.objects.filter(
            permission=perm, user_id__in=[person.pk, additional.pk]
        ).values_list("user_id", flat=True)
    ) == {person.pk, additional.pk}
    assert (
        group.permissions.filter(pk=perm.pk).exists() and person.groups.filter(pk=group.pk).exists()
    )
    assert not CapabilityEnd.objects.filter(
        permission=perm, user_id__in=[person.pk, additional.pk]
    ).exists()


@pytest.mark.parametrize("legacy", [False, True], ids=["as-drawn", "omitted"])
def test_portal_date_retry_after_two_service_edits_checks_the_original_revision(
    path_parties, client, legacy
):
    organization, manager, account = path_parties
    day = grant_ends.database_today() + timedelta(days=19)
    with override_settings(TIME_ZONE="Etc/UTC"):
        access = portal_set(path_parties, day)
        client.force_login(manager)
        url = portal_edit_url(path_parties)
        pending = vportal_data(client.get(url))
        if legacy:
            del pending["shown_last_day"]
        requested = day - timedelta(days=5)
        pending.update(last_day=requested.isoformat(), reason="")
        first = client.post(url, pending)
        assert first.status_code == 200 and set(first.context["form"].errors) == {"reason"}
        corrected = vportal_data(first)
        assert corrected["shown_last_day"] == day.isoformat()
        assert int(corrected["expected_revision"]) == access.revision
        assert corrected["last_day"] == requested.isoformat()

        portal_set(path_parties, requested)
        restored = portal_set(path_parties, day)
        assert restored.ends_at == access.ends_at and restored.revision == access.revision + 2
    events = AuditEvent.objects.filter(action="portal_access.set", object_id=str(access.pk))
    before = events.count()
    response = client.post(url, {**corrected, "reason": "Correcting the schedule"})
    assert response.status_code == 200
    assert any("Reload and review" in e for e in response.context["form"].non_field_errors())
    assert events.count() == before
    restored.refresh_from_db()
    assert restored.revision == access.revision + 2 and restored.ends_at == access.ends_at
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        fresh = vportal_data(client.get(url))
        assert fresh["shown_last_day"] == grant_ends.last_day_of(restored.ends_at).isoformat()
        response = client.post(
            url, {**fresh, "last_day": requested.isoformat(), "reason": "Reopened and checked"}
        )
        assert response.status_code == 302
        restored.refresh_from_db()
        assert restored.revision == access.revision + 3 and restored.ends_at == grant_ends.end_of(
            requested
        )
        assert restored.client_id == organization.pk and events.count() == before + 1


@pytest.mark.parametrize("legacy", [False, True], ids=["as-drawn", "omitted"])
def test_current_organization_last_day_is_a_new_choice_after_a_former_organization_redraw(
    path_parties, client, legacy
):
    organization, manager, account = path_parties
    former = Organization.objects.create(name="Former directory")
    with override_settings(TIME_ZONE="Etc/UTC"):
        day = grant_ends.database_today() + timedelta(days=8)
        original = grant_ends.end_of(day)
        access = PortalAccess.objects.create(user=account, client=former, ends_at=original)
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        client.force_login(manager)
        url = portal_edit_url(path_parties)
        drawn = vportal_data(client.get(url))
        assert drawn.get("last_day") in ("", None) and drawn["shown_last_day"] == ""
        if legacy:
            del drawn["shown_last_day"]
        # This is the first choice for this organization, even if the former date matches.
        chosen = grant_ends.last_day_of(original)
        refused = client.post(url, {**drawn, "last_day": chosen.isoformat(), "reason": ""})
        assert refused.status_code == 200 and set(refused.context["form"].errors) == {"reason"}
        data = vportal_data(refused)
        assert data["shown_last_day"] == "" and data["last_day"] == chosen.isoformat()
        assert (
            client.post(url, {**data, "reason": "First schedule for this organization"}).status_code
            == 302
        )
        access.refresh_from_db()
        assert access.ends_at == grant_ends.end_of(chosen) != original
        assert access.client_id == organization.pk
        entry = AuditEvent.objects.get(action="portal_access.set", object_id=str(access.pk))
        assert entry.before["ends_at"] == grant_ends.iso(original)
        assert entry.after["ends_at"] == grant_ends.iso(access.ends_at)


# Demo seed and clocks

CENTURY_BEHIND_LIMIT = (
    "Known limit (accounts.grant_ends.later_today): with the application's clock about a century "
    "behind the database's, the seed's day is refused as too far ahead."
)


def expected_demo_end_from_database(ahead=0):
    """Use a laboratory whose calendar date differs from UTC at this transaction's start. `ahead`:
    the days the application's clock is ahead of the database's, which the seed's day follows."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT transaction_timestamp()")
        (instant,) = cursor.fetchone()
        for zone in ("Pacific/Kiritimati", "Pacific/Pago_Pago"):
            if instant.astimezone(ZoneInfo(zone)).date() != instant.astimezone(UTC).date():
                break
        cursor.execute(
            "SELECT (transaction_timestamp() AT TIME ZONE %s)::date + 90 + %s,"
            " ((transaction_timestamp() AT TIME ZONE %s)::date + 91 + %s)::timestamp"
            " AT TIME ZONE %s",
            [zone, ahead, zone, ahead, zone],
        )
        last_day, end = cursor.fetchone()
    return instant, zone, last_day, end


@pytest.mark.django_db
@pytest.mark.parametrize("clock_delta", [-400, 90], ids=["app-behind", "last-day-is-app-today"])
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
def test_seeded_ends_use_the_database_calendar_and_agree_on_the_account_report_and_review(
    client, monkeypatch, clock_delta
):
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-confirmation-seed-password")
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    ahead = max(clock_delta, 0)
    instant, zone, last_day, end = expected_demo_end_from_database(ahead)
    app_time = instant + timedelta(days=clock_delta)
    code = "view_patient_information"
    with override_settings(TIME_ZONE=zone):
        with monkeypatch.context() as shifted:
            shifted.setattr(timezone, "now", lambda: app_time)
            assert grant_ends.database_today() == last_day - timedelta(days=90 + ahead)
            assert grant_ends.today() == last_day + timedelta(days=clock_delta - 90 - ahead)
            output = StringIO()
            call_command("seed_demo", stdout=output, stderr=output)

        supervisor = User.objects.get(username="demo_supervisor")
        portal = User.objects.get(username="demo_client_water")
        assert has_capability(supervisor, code)
        direct = CapabilityEnd.objects.get(user=supervisor, permission__codename=code)
        access = current_access(portal)
        assert access is not None and not access.whole_organization
        assert direct.ends_at == access.ends_at == end
        site = access.sites.get()
        assert site.code == "NORTH"
        assert (
            AuditEvent.objects.get(action="account.capability_end_set").after["ends_at"]
            == end.isoformat()
        )
        assert (
            AuditEvent.objects.get(action="portal_access.set", object_id=str(access.pk)).after[
                "ends_at"
            ]
            == end.isoformat()
        )
        seeded_review = AccessReview.objects.get(scope=AccessScope.STAFF)
        item = seeded_review.items.get(account_id=supervisor.pk, code=code)
        assert item.detail == f"Given directly, last day {last_day}"

        owner = User.objects.create_superuser(
            "seed_date_owner", "seed-date-owner@example.invalid", "synthetic-operator-password"
        )
        client.force_login(owner)
        change = client.get(reverse("admin:accounts_user_change", args=[supervisor.pk]))
        assert change.status_code == 200
        assert change.context["adminform"].form[end_field(code)].value() == last_day
        client.force_login(User.objects.get(username="demo_account_manager"))
        set_access = client.get(
            reverse("clients:portal-access-edit", args=[portal.client_id, portal.pk])
        )
        assert set_access.status_code == 200
        assert set_access.context["form"]["last_day"].value() == last_day

        client.force_login(supervisor)
        report = client.get(reverse("access_review:accounts"))
        assert report.status_code == 200
        rows = {row.username: row for row in report.context["page_obj"]}
        patient = next(
            item
            for item in rows[supervisor.username].capabilities
            if item.text == "Can view patient information"
        )
        assert f"Directly, last day {last_day}" in patient.note
        assert (
            rows[portal.username].portal
            == f"Tickets of {site.name}, and those it raised, last day {last_day}"
        )
        for scope, holder, words in (
            (AccessScope.STAFF, supervisor, item.detail),
            (AccessScope.PORTAL, portal, rows[portal.username].portal),
        ):
            review_form = client.get(reverse("access_review:review-create"), {"scope": scope})
            assert review_form.status_code == 200
            held = [
                grant
                for grant in review_form.context["reading"].grants
                if grant.account_id == holder.pk and grant.detail == words
            ]
            assert len(held) == 1 and held[0].ends == end.isoformat()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("command", "offset_days", "validation_message"),
    [
        ("seed_demo", 91, grant_ends.PORTAL_PAST_DAY),
        ("reset_demo", 91, grant_ends.PORTAL_PAST_DAY),
        pytest.param(
            "seed_demo",
            -101 * 365,
            grant_ends.TOO_LATE,
            marks=pytest.mark.xfail(strict=True, reason=CENTURY_BEHIND_LIMIT),
        ),
    ],
    ids=["fresh-ahead", "reset-ahead", "fresh-over-a-century-behind"],
)
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
def test_demo_commands_can_write_the_database_schedule_with_a_shifted_application_clock(
    monkeypatch, command, offset_days, validation_message
):
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-confirmation-seed-password")
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    output = StringIO()
    instant, zone, _last_day, end = expected_demo_end_from_database(max(offset_days, 0))
    with override_settings(TIME_ZONE=zone):
        if command == "reset_demo":
            call_command("seed_demo", stdout=output, stderr=output)
        original_users = set(User.objects.values_list("id", "username"))
        original_events = AuditEvent.objects.count()
        with monkeypatch.context() as shifted:
            shifted.setattr(timezone, "now", lambda: instant + timedelta(days=offset_days))
            try:
                call_command(
                    command,
                    **({"interactive": False} if command == "reset_demo" else {}),
                    stdout=output,
                    stderr=output,
                )
            except ValidationError as error:
                assert error.message_dict == {"last_day": [validation_message]}
                # The command's transaction must still be all-or-nothing, even on a failed reset.
                assert set(User.objects.values_list("id", "username")) == original_users
                assert AuditEvent.objects.count() == original_events
                assert Lab.objects.exists() == (command == "reset_demo")
                raise AssertionError(
                    f"{command} refused the DB-derived demonstration date: {error}"
                ) from error
    assert CapabilityEnd.objects.get(user__username="demo_supervisor").ends_at == end
    assert PortalAccess.objects.get(user__username="demo_client_water").ends_at == end


@pytest.mark.django_db
@pytest.mark.parametrize("days_ahead", [-10, 10], ids=["past", "future"])
def test_blank_day_marker_on_account_form_validates_the_date_in_the_current_zone(days_ahead):
    account = acct_user("marker-calendar", is_active=False)
    http = acct_http("marker-owner")
    field = end_field(ACCT_REVIEW)
    marker = shown_field(ACCT_REVIEW)
    with override_settings(TIME_ZONE="Etc/UTC"):
        day = grant_ends.database_today() + timedelta(days=days_ahead)
        original_end = grant_ends.end_of(day)
        row = acct_end(account, ACCT_REVIEW, original_end)
        url, form = acct_form(http, account)
        data = page_data(form, first_name="Morgan")
        assert data[marker] == data[field] == day
    data[marker] = ""
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        response = http.post(url, data)
        row.refresh_from_db()
        account.refresh_from_db()
        assert not account.is_active
        if days_ahead < 0:
            assert response.status_code == 200
            assert grant_ends.PAST_DAY in response.context["adminform"].form.errors[field]
            assert row.ends_at == original_end and not acct_events(account).exists()
        else:
            assert response.status_code == 302 and account.first_name == "Morgan"
            assert row.ends_at == grant_ends.end_of(day) < original_end
            event = acct_events(account).get()
            assert event.after["capability_ends"][f"accounts.{ACCT_REVIEW}"] == grant_ends.iso(
                row.ends_at
            )
