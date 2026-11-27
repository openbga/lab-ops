"""Adopted from the independent review of feat/grant-end-dates, third confirmation round (report
.local/review-history/grant-ends-confirm3.md; original archived beside it). No change on adoption.

Third confirmation of account and portal date redraws and demo grant schedules."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from io import StringIO
from threading import Event, local
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import connection, connections
from django.test import override_settings
from django.utils import timezone

from accounts import acting, grant_ends
from accounts.admin import CHANGED_SINCE_DRAWN, NOT_DIRECT, UNSENT, end_field, shown_field
from accounts.models import CapabilityEnd
from accounts.tests.conftest import PASSWORD
from accounts.tests.test_confirm_identity import http_for, page_data
from audit_review.models import AccessReview, AccessScope
from clients import seed as client_seed
from clients.models import PortalAccess
from core.models import AuditEvent
from tests.test_review_grant_ends import (
    ACCT_PATIENT,
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
from tests.test_review_grant_ends import portal_parties as portal_parties
from tests.test_review_grant_ends_verify import verify_posted_hidden, vportal_data


def rendered_account_markers(response):
    form = response.context["adminform"].form
    fields = [shown_field(ACCT_PATIENT), shown_field(ACCT_REVIEW)]
    for name in fields:
        assert str(form[name]) in response.content.decode()
    return {name: verify_posted_hidden(form, name) for name in fields}


@pytest.mark.django_db
@pytest.mark.parametrize("legacy", [False, True], ids=["shown", "omitted"])
def test_last_console_owner_refusal_keeps_an_expired_date_for_the_corrected_form(legacy):
    owner = acct_user("sole-console-owner", is_staff=True, is_superuser=True)
    http = http_for(owner)
    field, marker = end_field(ACCT_PATIENT), shown_field(ACCT_PATIENT)
    past = grant_ends.today() - timedelta(days=10)
    with override_settings(TIME_ZONE="Etc/UTC"):
        end = acct_end(owner, ACCT_PATIENT, grant_ends.end_of(past))
        original_moment = end.ends_at
        url, drawing = acct_form(http, owner)
        submitted = page_data(drawing, first_name="Casey")
        submitted.pop("is_staff")
        if legacy:
            submitted.pop(marker)
        refused = http.post(url, submitted)
    assert refused.status_code == 200
    form = refused.context["adminform"].form
    assert "Keep at least one active superuser" in str(form.non_field_errors())
    assert form.held_reading["capability_ends"][ACCT_PATIENT] == grant_ends.iso(original_moment)
    owner.refresh_from_db()
    assert owner.is_staff and owner.is_active and owner.is_superuser
    assert not acct_events(owner).exists()
    shown = rendered_account_markers(refused)
    assert str(form[field].value()) == past.isoformat()
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        assert grant_ends.last_day_of(original_moment) != past
        # Correct the refused checkbox. The already-expired date was never edited.
        retry = http.post(url, page_data(form, is_staff="on", **shown))
    end.refresh_from_db()
    owner.refresh_from_db()
    assert end.ends_at == original_moment
    assert owner.is_staff and owner.is_active and owner.is_superuser
    assert retry.status_code == 302, (
        shown[marker],
        retry.context["adminform"].form.errors.as_data(),
    )
    assert owner.first_name == "Casey"
    assert acct_events(owner).count() == 1


@pytest.mark.django_db
@pytest.mark.confirmation
def test_confirmation_then_validation_replaces_only_unsent_markers_with_a_held_day():
    subject = acct_user("confirmation-retry-subject", is_active=False)
    admin = acct_user(
        "confirmation-retry-owner", password=PASSWORD, is_staff=True, is_superuser=True
    )
    http = http_for(admin)
    day = grant_ends.today() + timedelta(days=14)
    with override_settings(TIME_ZONE="Etc/UTC"):
        end = acct_end(subject, ACCT_REVIEW, grant_ends.end_of(day))
        original_moment = end.ends_at
        subject.user_permissions.add(acct_perm(ACCT_PATIENT))
        url, drawing = acct_form(http, subject)
        data = page_data(drawing, email="invalid")
        for code in (ACCT_REVIEW, ACCT_PATIENT):
            data.pop(shown_field(code), None)
        # A first refusal occurs before the account is held.
        refused = http.post(url, data)
        form = refused.context["adminform"].form
        assert {"email", "confirm_password"} <= set(form.errors)
        assert not hasattr(form, "held_reading")
        markers = rendered_account_markers(refused)
        assert markers == {shown_field(code): UNSENT for code in (ACCT_REVIEW, ACCT_PATIENT)}
        assert not acct_events(subject).exists()
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        held_day = grant_ends.last_day_of(original_moment)
        assert held_day != day
        # Keep the expiration as it reads here and confirm. The email is still invalid.
        data = page_data(
            form, confirm_password=PASSWORD, **{end_field(ACCT_REVIEW): held_day, **markers}
        )
        refused = http.post(url, data)
        form = refused.context["adminform"].form
        assert set(form.errors) == {"email"}
        markers = rendered_account_markers(refused)
        assert markers == {
            shown_field(ACCT_REVIEW): held_day.isoformat(),
            shown_field(ACCT_PATIENT): "",
        }
        assert not acct_events(subject).exists()
    with override_settings(TIME_ZONE="America/New_York"):
        assert grant_ends.last_day_of(original_moment) != held_day
        assert (
            http.post(
                url, page_data(form, email="corrected@example.invalid", **markers)
            ).status_code
            == 302
        )
    end.refresh_from_db()
    assert end.ends_at == original_moment
    event = acct_events(subject).get()
    assert event.after["email"] == "corrected@example.invalid"
    assert (
        event.before["capability_ends"]
        == event.after["capability_ends"]
        == {f"accounts.{ACCT_REVIEW}": grant_ends.iso(original_moment)}
    )


@pytest.mark.django_db
def test_unsent_on_an_ungranted_capability_still_requires_a_direct_grant_and_allows_correction():
    subject = acct_user("ungranted-token-subject", is_active=False)
    http = acct_http("ungranted-token-owner")
    subject.user_permissions.add(acct_perm(ACCT_REVIEW))
    url, drawing = acct_form(http, subject)
    data = page_data(
        drawing,
        first_name="Robin",
        **{
            shown_field(ACCT_PATIENT): UNSENT,
            end_field(ACCT_PATIENT): grant_ends.today() + timedelta(days=8),
            shown_field(ACCT_REVIEW): UNSENT,
        },
    )
    refused = http.post(url, data)
    assert refused.status_code == 200
    form = refused.context["adminform"].form
    assert set(form.errors) == {end_field(ACCT_PATIENT)}
    assert NOT_DIRECT in form.errors[end_field(ACCT_PATIENT)]
    assert not acct_events(subject).exists()
    markers = rendered_account_markers(refused)
    assert markers == {shown_field(code): "" for code in (ACCT_PATIENT, ACCT_REVIEW)}
    assert (
        http.post(url, page_data(form, **{end_field(ACCT_PATIENT): "", **markers})).status_code
        == 302
    )
    assert not CapabilityEnd.objects.filter(user=subject).exists()
    event = acct_events(subject).get()
    assert "capability_ends" not in event.before and "capability_ends" not in event.after


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("legacy", [True, False], ids=["omitted", "shown"])
def test_two_admins_restore_the_drawn_end_between_a_stale_refusal_and_a_field_error(
    monkeypatch, legacy
):
    account = acct_user("restored-after-stale", is_active=False)
    editor = acct_http("restored-after-stale-editor")
    coworker = acct_http("restored-after-stale-coworker")
    field, marker = end_field(ACCT_REVIEW), shown_field(ACCT_REVIEW)
    first_day = grant_ends.today() + timedelta(days=10)
    chosen = first_day + timedelta(days=20)
    with override_settings(TIME_ZONE="Etc/UTC"):
        end = acct_end(account, ACCT_REVIEW, grant_ends.end_of(first_day))
        url, drawing = acct_form(editor, account)
        request_data = page_data(drawing, email="invalid", **{field: chosen})
        if legacy:
            request_data.pop(marker)

        _, other = acct_form(coworker, account)
        assert coworker.post(url, page_data(other, **{field: chosen})).status_code == 302
        refused = editor.post(url, request_data)
        first = refused.context["adminform"].form
        assert CHANGED_SINCE_DRAWN in first.non_field_errors()
        assert not hasattr(first, "held_reading")
        markers = rendered_account_markers(refused)
        assert markers[marker] == (UNSENT if legacy else first_day.isoformat())
        retry = page_data(first, **markers)
        _, other = acct_form(coworker, account)
        undo = page_data(other, **{field: first_day})

        reached, resume = Event(), Event()
        worker = local()
        lock = acting.close_access

        def wait_for_undo(ids):
            ids = list(ids)
            if getattr(worker, "retry", False) and account.pk in ids and not reached.is_set():
                reached.set()
                assert resume.wait(15)
            return lock(ids)

        def second_request():
            worker.retry = True
            try:
                return editor.post(url, retry)
            finally:
                connections.close_all()

        monkeypatch.setattr(acting, "close_access", wait_for_undo)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(second_request)
            try:
                assert reached.wait(15)
                assert coworker.post(url, undo).status_code == 302
            finally:
                resume.set()
            refused = future.result(timeout=15)
        second = refused.context["adminform"].form
        assert set(second.errors) == {"email"}
        assert rendered_account_markers(refused)[marker] == first_day.isoformat()
        assert str(second[field].value()) == chosen.isoformat()
        assert acct_events(account).count() == 2

    with override_settings(TIME_ZONE="Asia/Tokyo"):
        expected = grant_ends.end_of(chosen)
        assert (
            editor.post(
                url,
                page_data(
                    second, email="restored@example.invalid", **rendered_account_markers(refused)
                ),
            ).status_code
            == 302
        )
    account.refresh_from_db()
    end.refresh_from_db()
    assert account.email == "restored@example.invalid"
    assert end.ends_at == expected
    event = acct_events(account).get(actor__username="restored-after-stale-editor")
    assert event.after["capability_ends"] == {f"accounts.{ACCT_REVIEW}": grant_ends.iso(expected)}


@pytest.mark.django_db
@pytest.mark.parametrize("membership", ["unassigned", "through-group"])
def test_unsent_for_an_indirect_or_absent_capability_keeps_a_date_on_its_own_grant(membership):
    person = acct_user("directory-date", is_active=False)
    http = acct_http("directory-owner")
    direct = acct_perm(ACCT_REVIEW)
    person.user_permissions.add(direct)
    if membership == "through-group":
        group = Group.objects.create(name="Directory staff")
        group.permissions.add(acct_perm(ACCT_PATIENT))
        person.groups.add(group)
    day = grant_ends.today() + timedelta(days=18)
    direct_field, other_field = end_field(ACCT_REVIEW), end_field(ACCT_PATIENT)
    direct_marker, other_marker = shown_field(ACCT_REVIEW), shown_field(ACCT_PATIENT)
    url, form = acct_form(http, person)
    submission = page_data(form, first_name="Taylor")
    submission.update({direct_field: day, other_field: day, other_marker: UNSENT})
    submission.pop(direct_marker, None)
    first = http.post(url, submission)
    assert first.status_code == 200
    drawn = first.context["adminform"].form
    assert drawn.errors == {other_field: [NOT_DIRECT]}
    assert (
        verify_posted_hidden(drawn, direct_marker)
        == verify_posted_hidden(drawn, other_marker)
        == ""
    )
    assert not CapabilityEnd.objects.filter(user=person).exists()
    assert not acct_events(person).exists()

    with override_settings(TIME_ZONE="Pacific/Auckland"):
        fixed = page_data(drawn, **{other_field: ""})
        fixed[direct_marker] = verify_posted_hidden(drawn, direct_marker)
        fixed[other_marker] = verify_posted_hidden(drawn, other_marker)
        assert http.post(url, fixed).status_code == 302
        end = CapabilityEnd.objects.get(user=person)
        assert (end.permission_id, end.ends_at) == (direct.pk, grant_ends.end_of(day))
    person.refresh_from_db()
    assert person.first_name == "Taylor" and not person.is_active
    assert set(person.user_permissions.values_list("pk", flat=True)) == {direct.pk}
    event = acct_events(person).get()
    assert event.after["capability_ends"] == {
        f"accounts.{ACCT_REVIEW}": grant_ends.iso(end.ends_at)
    }


@pytest.mark.parametrize("marker", ["from-page", "omitted"])
@pytest.mark.parametrize("dated", [False, True], ids=["no-end", "with-end"])
def test_set_access_preserves_the_reading_through_service_and_field_refusals(
    portal_parties, client, marker, dated
):
    org, manager, account = portal_parties
    contact = org.contacts.create(name="Scheduling desk", phone="555")
    events = AuditEvent.objects.filter(action="portal_access.set")
    client.force_login(manager)
    url = portal_edit_url(portal_parties)
    with override_settings(TIME_ZONE="Etc/UTC"):
        day = grant_ends.today() + timedelta(days=15) if dated else None
        original = portal_set(portal_parties, day)
        data = vportal_data(client.get(url))
        if marker == "omitted":
            del data["shown_last_day"]
        # A valid, unchanged submission is refused inside the service, after its revision check.
        first = client.post(url, {**data, "reason": "Check the existing schedule"})
        assert first.status_code == 200
        assert first.context["form"].non_field_errors() == [
            "Change at least one detail before saving."
        ]
        retry = vportal_data(first)
        expected_marker = day.isoformat() if dated else ""
        assert retry["shown_last_day"] == expected_marker
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        second = client.post(url, {**retry, "contact": contact.pk, "reason": ""})
        assert second.status_code == 200 and set(second.context["form"].errors) == {"reason"}
        corrected = vportal_data(second)
        assert corrected["shown_last_day"] == expected_marker
        assert int(corrected["expected_revision"]) == original.revision
        assert events.count() == 1
        assert (
            client.post(url, {**corrected, "reason": "Correct the directory contact"}).status_code
            == 302
        )
    expected_end = original.ends_at
    previous_revision = original.revision
    original.refresh_from_db()
    assert original.ends_at == expected_end and original.revision == previous_revision + 1
    assert original.contact_id == contact.pk and original.user_id == account.pk
    event = events.get(reason="Correct the directory contact")
    assert event.before["ends_at"] == event.after["ends_at"] == grant_ends.iso(expected_end)


SEED_ZONE = "America/New_York"


def database_seed_day():
    with connection.cursor() as cursor:
        cursor.execute("SELECT (transaction_timestamp() AT TIME ZONE %s)::date", [SEED_ZONE])
        return cursor.fetchone()[0]


def set_demo_passwords(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-third-confirmation-password")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "synthetic-unlisted-third-confirmation-password")


def audited_seed_dates():
    staff = CapabilityEnd.objects.get(user__username="demo_supervisor")
    portal = PortalAccess.objects.get(user__username="demo_client_water")
    assert AuditEvent.objects.get(action="account.capability_end_set").after[
        "ends_at"
    ] == grant_ends.iso(staff.ends_at)
    assert AuditEvent.objects.get(action="portal_access.set", object_id=str(portal.pk)).after[
        "ends_at"
    ] == grant_ends.iso(portal.ends_at)
    return grant_ends.last_day_of(portal.ends_at), grant_ends.last_day_of(staff.ends_at)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "app_days_behind", [4, 0], ids=["database-still-later", "midnight-overtakes-database"]
)
@override_settings(
    TIME_ZONE=SEED_ZONE, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)
def test_one_seed_uses_one_90_day_date_when_the_application_crosses_midnight(
    monkeypatch, app_days_behind
):
    set_demo_passwords(monkeypatch)
    db_day = database_seed_day()
    app_day = db_day - timedelta(days=app_days_behind)
    midnight = datetime.combine(app_day + timedelta(days=1), time.min, ZoneInfo(SEED_ZONE))
    clock = [midnight - timedelta(seconds=1)]
    clients_seed_records = client_seed.seed_records

    def seed_clients_and_advance_clock(ctx):
        clients_seed_records(ctx)
        assert PortalAccess.objects.filter(
            user__username="demo_client_water", ends_at__isnull=False
        ).exists()
        assert not CapabilityEnd.objects.filter(user__username="demo_supervisor").exists()
        clock[0] = midnight + timedelta(seconds=1)

    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    monkeypatch.setattr(client_seed, "seed_records", seed_clients_and_advance_clock)
    assert grant_ends.database_today() == db_day
    assert grant_ends.today() == app_day
    stream = StringIO()
    call_command("seed_demo", stdout=stream, stderr=stream)
    assert grant_ends.today() == app_day + timedelta(days=1)
    assert database_seed_day() == db_day
    portal_day, staff_day = audited_seed_dates()
    assert portal_day == staff_day, (
        f"One seed recorded portal {portal_day} and supervisor {staff_day}."
    )
    assert staff_day in {
        max(app_day, db_day) + timedelta(days=90),
        max(app_day + timedelta(days=1), db_day) + timedelta(days=90),
    }


@pytest.mark.django_db
@override_settings(
    TIME_ZONE=SEED_ZONE, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)
def test_seed_accepts_the_db_clock_example_on_the_inclusive_calendar_century_limit(monkeypatch):
    set_demo_passwords(monkeypatch)
    db_day = database_seed_day()
    expected = db_day + timedelta(days=90)
    # The first of the target month exists in either century. When the target day is 29
    # February in only one of them, the offset lands on 1 March of the earlier century.
    old_day = expected.replace(day=1, year=expected.year - 100) + timedelta(days=expected.day - 1)
    application = datetime.combine(old_day, time(12), ZoneInfo(SEED_ZONE))
    with monkeypatch.context() as clock:
        clock.setattr(timezone, "now", lambda: application)
        assert grant_ends.today() == old_day and grant_ends.later_today() == db_day
        assert grant_ends.latest_day(old_day) >= expected
        assert grant_ends.latest_day(old_day - timedelta(days=1)) < expected
        output = StringIO()
        call_command("seed_demo", announce_accounts=True, stdout=output, stderr=output)
    portal_day, staff_day = audited_seed_dates()
    assert portal_day == staff_day == expected
    signed = AccessReview.objects.get(scope=AccessScope.STAFF)
    dated = signed.items.get(account_username="demo_supervisor", code="view_patient_information")
    assert dated.detail == f"Given directly, last day {expected}"
