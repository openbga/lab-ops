"""Adopted from the independent review of feat/grant-end-dates, verification round (report
.local/review-history/grant-ends-verify.md; originals archived beside it). Changes on adoption: the
zone "UTC" is written "Etc/UTC"; the cases of the two documented limits (a portal access saved by
other code; an altered hidden shown day) are strict xfails; the case of a database that applied
the round-one migration is left out (no published database applied it: the migration was changed
before it was merged).

Verification of the fixes for time-limited staff and portal access."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from queue import Queue
from time import monotonic, sleep

import pytest
from django.db import connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client as HttpClient
from django.test import override_settings
from django.test.html import parse_html
from django.urls import reverse
from django.utils import timezone

from accounts import grant_ends
from accounts.acting import ACCESS_GATE, INT4
from accounts.admin import CHANGED_SINCE_DRAWN, end_field, shown_field
from accounts.models import CapabilityEnd, User
from accounts.tests.test_confirm_identity import page_data
from audit_review import access_reviews
from audit_review.models import AccessReview, AccessReviewItem, AccessScope, Decision, GrantKind
from audit_review.tests import conftest as va_fixtures
from audit_review.tests.test_access_reviews import patient_permission, posted
from clients.models import Client as Organization
from clients.models import ClientSite, PortalAccess
from clients.portal import current_access
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
    response_elements,
)
from tests.test_review_grant_ends import portal_parties as portal_parties

# Account forms


@pytest.mark.django_db
@pytest.mark.parametrize("extra_days", [0, 1], ids=["anniversary", "day-after"])
def test_account_new_day_is_limited_to_the_hundredth_calendar_anniversary(monkeypatch, extra_days):
    today = date(2028, 2, 29)
    monkeypatch.setattr(grant_ends, "today", lambda: today)
    day = date(2128, 2, 29) + timedelta(days=extra_days)
    account = acct_user("calendar-limit", is_active=False)
    http = acct_http("calendar-owner")
    url, form = acct_form(http, account)
    field = end_field(ACCT_REVIEW)
    response = http.post(
        url,
        page_data(form, user_permissions=[acct_perm(ACCT_REVIEW).pk], **{field: day}),
    )
    if not extra_days:
        assert response.status_code == 302
        assert CapabilityEnd.objects.get(user=account).ends_at == grant_ends.end_of(day)
        assert acct_events(account).count() == 1
    else:
        saved_end = (
            CapabilityEnd.objects.filter(user=account).values_list("ends_at", flat=True).first()
        )
        assert (response.status_code, saved_end, acct_events(account).count()) == (200, None, 0)
        assert grant_ends.TOO_LATE in response.context["adminform"].form.errors[field]


# The two documented limits these strict xfails record (docs/architecture.md).
MODEL_SAVE_LIMIT = (
    "Known limit: a portal access's end is changed only by the Set access form and the portal "
    "access services, which take the gate and advance the revision; other code saving the model "
    "does neither."
)
ALTERED_SHOWN_DAY_LIMIT = (
    "Known limit: a form whose hidden shown day was altered only keeps the stored end; nothing is "
    "stored that the fingerprint or revision did not check."
)


def verify_posted_hidden(form, name):
    """An enabled HTML hidden input contributes an empty string when it has no value attribute."""
    widget = parse_html(str(form[name]))
    attrs = dict(widget.attributes)
    assert widget.name == "input" and attrs.get("type") == "hidden" and attrs.get("name") == name
    assert "disabled" not in attrs
    return attrs.get("value", "")


@pytest.mark.django_db
@pytest.mark.parametrize("legacy", [False, True], ids=["current-form", "pre-fix-form"])
def test_keeping_an_ended_day_survives_an_unrelated_error_and_browser_resubmission(legacy):
    account = acct_user("old-form-retry", is_active=False)
    http = acct_http("old-form-owner")
    yesterday = grant_ends.today() - timedelta(days=3)
    end = acct_end(account, ACCT_REVIEW, grant_ends.end_of(yesterday))
    original = end.ends_at
    field = end_field(ACCT_REVIEW)
    hidden = shown_field(ACCT_REVIEW)
    url, form = acct_form(http, account)
    submitted = page_data(form, email="invalid")
    if legacy:
        submitted.pop(hidden)
    refused = http.post(url, submitted)
    assert refused.status_code == 200
    redrawn = refused.context["adminform"].form
    assert set(redrawn.errors) == {"email"}
    assert str(redrawn[field].value()) == yesterday.isoformat()
    assert redrawn.fields[field].help_text.startswith("Ended")
    assert not acct_events(account).exists()
    corrected = page_data(redrawn, email="corrected@example.invalid")
    corrected[hidden] = verify_posted_hidden(redrawn, hidden)
    result = http.post(url, corrected)
    assert result.status_code == 302, result.context["adminform"].form.errors.as_data()
    account.refresh_from_db()
    end.refresh_from_db()
    assert account.email == "corrected@example.invalid"
    assert end.ends_at == original
    event = acct_events(account).get()
    assert event.before["capability_ends"] == event.after["capability_ends"]


@pytest.mark.django_db
def test_two_capabilities_keep_and_change_their_own_days_across_a_retry_and_zone_change():
    account = acct_user("retry-two-days", is_active=False)
    http = acct_http("retry-owner")
    past = grant_ends.today() - timedelta(days=5)
    future = grant_ends.today() + timedelta(days=30)
    with override_settings(TIME_ZONE="Etc/UTC"):
        old_review = acct_end(account, ACCT_REVIEW, grant_ends.end_of(past))
        old_patient = acct_end(account, ACCT_PATIENT, grant_ends.end_of(future))
        expected_review = old_review.ends_at
        url, form = acct_form(http, account)
        next_day = future + timedelta(days=7)
        data = page_data(form, email="invalid", **{end_field(ACCT_PATIENT): next_day})
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        refused = http.post(url, data)
        assert refused.status_code == 200
        redrawn = refused.context["adminform"].form
        assert set(redrawn.errors) == {"email"}
        assert verify_posted_hidden(redrawn, shown_field(ACCT_REVIEW)) == past.isoformat()
        assert verify_posted_hidden(redrawn, shown_field(ACCT_PATIENT)) == future.isoformat()
        assert str(redrawn[end_field(ACCT_PATIENT)].value()) == next_day.isoformat()
        assert redrawn.fields[end_field(ACCT_REVIEW)].help_text.startswith("Ended")
        assert not redrawn.fields[end_field(ACCT_PATIENT)].help_text.startswith("Ended")
        assert not acct_events(account).exists()
        corrected = page_data(redrawn, email="corrected@two-days.invalid")
        assert http.post(url, corrected).status_code == 302
        expected_patient = grant_ends.end_of(next_day)
    old_review.refresh_from_db()
    old_patient.refresh_from_db()
    assert old_review.ends_at == expected_review
    assert old_patient.ends_at == expected_patient
    event = acct_events(account).get()
    assert event.after["capability_ends"] == {
        f"accounts.{ACCT_REVIEW}": grant_ends.iso(expected_review),
        f"accounts.{ACCT_PATIENT}": grant_ends.iso(expected_patient),
    }


@pytest.mark.django_db
@pytest.mark.parametrize("metadata", ["missing", "blank", "invalid"])
def test_old_account_form_distinguishes_missing_empty_and_invalid_shown_day(metadata):
    account = acct_user("verify-metadata", is_active=False)
    http = acct_http("verify-metadata-owner")
    day = grant_ends.today() - timedelta(days=3)
    stored = acct_end(account, ACCT_PATIENT, grant_ends.end_of(day))
    original = stored.ends_at
    url, form = acct_form(http, account)
    field = end_field(ACCT_PATIENT)
    hidden = shown_field(ACCT_PATIENT)
    data = page_data(form, first_name="Updated")
    if metadata == "missing":
        data.pop(hidden)
    else:
        data[hidden] = "" if metadata == "blank" else "2028-02-30"
    result = http.post(url, data)
    if metadata == "missing":
        assert result.status_code == 302
        assert acct_events(account).get().after["first_name"] == "Updated"
    else:
        assert result.status_code == 200
        errors = result.context["adminform"].form.errors
        assert hidden in errors if metadata == "invalid" else grant_ends.PAST_DAY in errors[field]
        assert not acct_events(account).exists()
    stored.refresh_from_db()
    assert stored.ends_at == original


@pytest.mark.django_db
def test_stale_account_drawn_value_takes_precedence_over_valid_shown_day_metadata():
    account = acct_user("stale-meta", is_active=False)
    older = acct_http("stale-meta-first")
    newer = acct_http("stale-meta-second")
    day = grant_ends.today() - timedelta(days=10)
    acct_end(account, ACCT_REVIEW, grant_ends.end_of(day))
    url, stale_form = acct_form(older, account)
    _, later_form = acct_form(newer, account)
    assert newer.post(url, page_data(later_form, **{end_field(ACCT_REVIEW): ""})).status_code == 302
    rejected = older.post(url, page_data(stale_form, first_name="Unrelated"))
    assert rejected.status_code == 200
    assert CHANGED_SINCE_DRAWN in rejected.context["adminform"].form.non_field_errors()
    assert not CapabilityEnd.objects.filter(user=account).exists()
    account.refresh_from_db()
    assert account.first_name == ""
    assert acct_events(account).count() == 1


@pytest.mark.django_db
def test_account_audit_cannot_report_an_end_without_a_stored_moment():
    account = acct_user("audit-no-end", is_active=False)
    account.user_permissions.add(acct_perm(ACCT_PATIENT))
    http = acct_http("audit-no-end-owner")
    url, form = acct_form(http, account)
    # Inconsistent date metadata on an inactive account. Whether the form is accepted
    # or refused, any account-change event has to describe the ends that were actually saved.
    proposed = grant_ends.today() + timedelta(days=5)
    response = http.post(
        url,
        page_data(
            form,
            first_name="Jordan",
            **{end_field(ACCT_PATIENT): proposed, shown_field(ACCT_PATIENT): proposed},
        ),
    )
    if response.status_code == 200:
        assert response.context["adminform"].form.errors
        assert not acct_events(account).exists()
        return
    assert response.status_code == 302
    ends = CapabilityEnd.objects.filter(user=account).values_list("permission__codename", "ends_at")
    expected = {f"accounts.{code}": grant_ends.iso(moment) for code, moment in ends}
    event = acct_events(account).get()
    assert event.after.get("capability_ends", {}) == expected


@pytest.mark.parametrize(
    ("zone", "expected_day"),
    [("America/New_York", date(2028, 2, 29)), ("Asia/Tokyo", date(2028, 3, 1))],
)
def test_last_day_validator_reads_today_in_the_laboratorys_zone(monkeypatch, zone, expected_day):
    instant = datetime(2028, 3, 1, 0, 30, tzinfo=UTC)
    monkeypatch.setattr(grant_ends.timezone, "now", lambda: instant)
    with override_settings(TIME_ZONE=zone):
        assert grant_ends.today() == expected_day
        assert grant_ends.check_new_day(expected_day, None) is None
        assert (
            grant_ends.check_new_day(expected_day - timedelta(days=1), None) == grant_ends.PAST_DAY
        )


@pytest.mark.django_db
@pytest.mark.xfail(strict=True, reason=ALTERED_SHOWN_DAY_LIMIT)
def test_account_form_rejects_a_new_past_day_when_hidden_metadata_disagrees_with_the_drawing():
    account = acct_user("past-input", is_active=False)
    http = acct_http("past-input-owner")
    day = grant_ends.today() - timedelta(days=14)
    acct_end(account, ACCT_REVIEW, grant_ends.end_of(day))
    _, form = acct_form(http, account)
    field = end_field(ACCT_REVIEW)
    hidden = shown_field(ACCT_REVIEW)
    assert form.initial[hidden] == form.initial[field] == day
    proposed = day + timedelta(days=5)
    # Neither past date can restore the grant. Validate only, without saving anything.
    bound = type(form)(page_data(form, **{field: proposed, hidden: proposed}), instance=account)
    assert not bound.is_valid(), "A different, already-past day passed form validation"
    assert field in bound.errors or hidden in bound.errors or bound.non_field_errors()


# Set access


def vportal_data(response):
    """Include empty hidden controls in the POST just as a browser does."""
    form = response.context["form"]
    data = page_data(form)
    for element in response_elements(response):
        attrs = dict(element.attributes)
        if element.name == "input" and attrs.get("name") == "shown_last_day":
            data["shown_last_day"] = attrs.get("value", "")
    return data


@pytest.mark.parametrize("excess", [0, 1, 100])
def test_set_access_uses_the_calendar_anniversary_for_new_dates(
    portal_parties, client, monkeypatch, excess
):
    _, manager, account = portal_parties
    # Across a nonleap century, using 366 days per year overstates the limit.
    monkeypatch.setattr(grant_ends, "today", lambda: date(2030, 3, 1))
    limit = date(2130, 3, 1)
    chosen = limit + timedelta(days=excess)
    client.force_login(manager)
    url = portal_edit_url(portal_parties)
    data = vportal_data(client.get(url))
    response = client.post(url, {**data, "last_day": chosen.isoformat()})
    if not excess:
        assert response.status_code == 302
        assert PortalAccess.objects.get(user=account).ends_at == grant_ends.end_of(chosen)
    else:
        observed = (
            PortalAccess.objects.filter(user=account).values_list("ends_at", flat=True).first()
        )
        assert (response.status_code, observed) == (200, None)
        assert response.context["form"].errors["last_day"] == [grant_ends.TOO_LATE]
        assert not PortalAccess.objects.filter(user=account).exists()


@pytest.mark.parametrize("legacy", [False, True], ids=["current", "pre-fix"])
def test_set_access_can_correct_just_the_reason_on_a_form_keeping_an_ended_day(
    portal_parties, client, legacy
):
    org, manager, _ = portal_parties
    stored_day = grant_ends.today() - timedelta(days=7)
    row = portal_set(portal_parties, None)
    row.ends_at = grant_ends.end_of(stored_day)
    with transaction.atomic():
        row.save(update_fields=["ends_at"])
    contact = org.contacts.create(name="Review contact", phone="555")
    client.force_login(manager)
    url = portal_edit_url(portal_parties)
    data = vportal_data(client.get(url))
    if legacy:
        del data["shown_last_day"]
    first = client.post(url, {**data, "contact": contact.pk, "reason": ""})
    assert first.status_code == 200 and set(first.context["form"].errors) == {"reason"}
    retry = vportal_data(first)
    assert retry["last_day"] == stored_day.isoformat()
    response = client.post(url, {**retry, "reason": "Correcting the contact"})
    assert response.status_code == 302, response.context["form"].errors
    row.refresh_from_db()
    assert row.contact == contact and row.ends_at == grant_ends.end_of(stored_day)


@pytest.mark.xfail(strict=True, reason=ALTERED_SHOWN_DAY_LIMIT)
def test_set_access_keeps_an_expired_row_unchanged_when_the_posted_past_day_was_never_shown(
    portal_parties, client
):
    org, manager, _ = portal_parties
    row = portal_set(portal_parties, None)
    old_day = grant_ends.today() - timedelta(days=15)
    row.ends_at = grant_ends.end_of(old_day)
    with transaction.atomic():
        row.save(update_fields=["ends_at"])
    contact = org.contacts.create(name="Pending correction", phone="555")
    client.force_login(manager)
    url = portal_edit_url(portal_parties)
    data = vportal_data(client.get(url))
    assert data["shown_last_day"] == old_day.isoformat()
    other_day = (old_day - timedelta(days=10)).isoformat()
    before = AuditEvent.objects.filter(action="portal_access.set").count()
    response = client.post(
        url,
        {
            **data,
            "contact": contact.pk,
            "last_day": other_day,
            "shown_last_day": other_day,
            "reason": "Synthetic correction",
        },
    )
    row.refresh_from_db()
    assert row.ends_at == grant_ends.end_of(old_day)
    event_count = AuditEvent.objects.filter(action="portal_access.set").count()
    assert (response.status_code, row.contact_id, event_count - before) == (200, None, 0)
    assert response.context["form"].errors


@pytest.mark.parametrize(
    "writer",
    ["form", pytest.param("model", marks=pytest.mark.xfail(strict=True, reason=MODEL_SAVE_LIMIT))],
)
def test_set_access_rechecks_another_edit_of_the_stored_end_before_moving_it(
    portal_parties, client, staff, writer
):
    _, manager, _ = portal_parties
    today = grant_ends.today()
    initial = today + timedelta(days=40)
    second_day = today + timedelta(days=25)
    form_choice = today + timedelta(days=12)
    row = portal_set(portal_parties, initial)
    client.force_login(manager)
    url = portal_edit_url(portal_parties)
    old = vportal_data(client.get(url))
    if writer == "form":
        second = HttpClient()
        second.force_login(staff("other_grant_manager", "account_manager"))
        other_form = vportal_data(second.get(url))
        assert (
            second.post(
                url,
                {**other_form, "last_day": second_day.isoformat(), "reason": "Revised schedule"},
            ).status_code
            == 302
        )
    else:
        with transaction.atomic():
            row.ends_at = grant_ends.end_of(second_day)
            row.save(update_fields=["ends_at"])
    row.refresh_from_db()
    assert row.ends_at == grant_ends.end_of(second_day)
    events = AuditEvent.objects.filter(action="portal_access.set")
    before_count = events.count()
    response = client.post(
        url, {**old, "last_day": form_choice.isoformat(), "reason": "Edit from the original form"}
    )
    row.refresh_from_db()
    assert (
        response.status_code,
        grant_ends.last_day_of(row.ends_at),
        events.count() - before_count,
    ) == (200, second_day, 0)
    assert response.context["form"].errors


def test_set_access_form_after_a_move_away_and_back_cannot_restore_the_old_choice(
    portal_parties, client
):
    org, manager, account = portal_parties
    row = portal_set(portal_parties, grant_ends.today() + timedelta(days=25))
    client.force_login(manager)
    url = portal_edit_url(portal_parties)
    data = vportal_data(client.get(url))
    other = Organization.objects.create(name="Temporary clinic", account_number="TEMP")
    account.changed_by = manager
    account.client = other
    account.save(update_fields=["client"])
    assert client.post(url, {**data, "reason": "An edit while away"}).status_code == 404
    account.client = org
    account.save(update_fields=["client"])
    response = client.post(url, {**data, "reason": "An edit after the return"})
    assert response.status_code == 200 and response.context["form"].errors
    row.refresh_from_db()
    assert row.client == org and row.ends_at is None and not row.whole_organization


@pytest.mark.parametrize("legacy", [True, False], ids=["omitted", "blank"])
def test_first_date_for_another_organization_is_interpreted_in_the_current_zone(
    portal_parties, client, legacy
):
    org, manager, account = portal_parties
    former = Organization.objects.create(name="Former clinic", account_number="OLD")
    when = grant_ends.today() + timedelta(days=15)
    with override_settings(TIME_ZONE="America/New_York"):
        old_end = grant_ends.end_of(when)
        row = PortalAccess.objects.create(
            user=account, client=former, whole_organization=True, ends_at=old_end
        )
    client.force_login(manager)
    url = portal_edit_url(portal_parties)
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        chosen = grant_ends.last_day_of(old_end)
        data = vportal_data(client.get(url))
        assert data.get("last_day") in (None, "") and data["shown_last_day"] == ""
        if legacy:
            del data["shown_last_day"]
        response = client.post(
            url, {**data, "last_day": chosen.isoformat(), "reason": "Set this clinic's day"}
        )
        assert response.status_code == 302
        row.refresh_from_db()
        assert row.client_id == org.pk
        assert row.ends_at == grant_ends.end_of(chosen) and row.ends_at != old_end


# Portal access model saves


@pytest.fixture
def receiver_account(transactional_db):
    organization = Organization.objects.create(name="Receiver example")
    return User.objects.create_user(
        "receiver-portal", "receiver@example.invalid", client=organization
    )


def receiver_gate_held(account_id):
    """Which of the account's two-key PostgreSQL gate locks this transaction owns."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = pg_backend_pid() "
            "AND locktype = 'advisory' AND classid = %s AND objid = %s AND objsubid = 2 "
            "AND mode = 'ExclusiveLock' AND granted)",
            [ACCESS_GATE, account_id % INT4],
        )
        return cursor.fetchone()[0]


def receiver_new_access(account, **values):
    return PortalAccess.objects.create(
        user=account, client_id=account.client_id, whole_organization=True, **values
    )


@pytest.mark.parametrize("supplied_pk", [False, True], ids=["assigned-pk", "explicit-pk"])
def test_receiver_new_access_gives_only_and_takes_no_gate(receiver_account, supplied_pk):
    end = timezone.now() + timedelta(days=10)
    identity = {"pk": 23000001} if supplied_pk else {}
    with transaction.atomic():
        access = receiver_new_access(receiver_account, ends_at=end, **identity)
        assert not receiver_gate_held(receiver_account.pk)
    assert PortalAccess.objects.get(pk=access.pk).ends_at == end


@pytest.mark.parametrize("change", ["set", "move", "clear"])
@pytest.mark.xfail(strict=True, reason=MODEL_SAVE_LIMIT)
def test_receiver_changing_only_the_end_holds_the_gate_to_commit(receiver_account, change):
    first = timezone.now() + timedelta(days=10)
    before = None if change == "set" else first
    after = None if change == "clear" else first + timedelta(days=5)
    access = receiver_new_access(receiver_account, ends_at=before)
    with transaction.atomic():
        assert not receiver_gate_held(receiver_account.pk)
        access.ends_at = after
        access.save(update_fields=["ends_at"])
        assert receiver_gate_held(receiver_account.pk)
    assert not receiver_gate_held(receiver_account.pk)
    assert PortalAccess.objects.get(pk=access.pk).ends_at == after


def test_receiver_unchanged_omitted_deferred_and_bulk_fields_use_the_documented_paths(
    receiver_account,
):
    first = timezone.now() + timedelta(days=10)
    later = first + timedelta(days=5)
    access = receiver_new_access(receiver_account, ends_at=first)
    with transaction.atomic():
        access.save(update_fields=["ends_at"])
        access.ends_at = later
        access.whole_organization = False
        access.save(update_fields=["whole_organization"])
        assert not receiver_gate_held(receiver_account.pk)
        access.refresh_from_db()
        assert not access.whole_organization and access.ends_at == first
        partial = PortalAccess.objects.only("whole_organization").get(pk=access.pk)
        partial.whole_organization = True
        partial.save()
        assert not receiver_gate_held(receiver_account.pk)
        access.refresh_from_db()
        assert access.whole_organization and access.ends_at == first
        PortalAccess.objects.filter(pk=access.pk).update(ends_at=later)
        assert not receiver_gate_held(receiver_account.pk)
    access.refresh_from_db()
    assert access.ends_at == later
    with transaction.atomic():
        access.delete()
        assert receiver_gate_held(receiver_account.pk)
    assert not PortalAccess.objects.filter(user=receiver_account).exists()


def test_receiver_model_save_of_new_end_in_autocommit(receiver_account):
    access = receiver_new_access(receiver_account)
    access.ends_at = timezone.now() + timedelta(days=15)
    assert connection.get_autocommit()
    access.save(update_fields=["ends_at"])
    assert PortalAccess.objects.get(pk=access.pk).ends_at == access.ends_at


@pytest.mark.xfail(strict=True, reason=MODEL_SAVE_LIMIT)
def test_receiver_concurrent_future_date_saves_each_hold_the_gate_when_they_change_the_row(
    receiver_account,
):
    """Inspect both writers' gates before their changes to future dates commit."""
    first = timezone.now() + timedelta(days=20)
    later = first + timedelta(days=20)
    access = receiver_new_access(receiver_account, ends_at=first)
    backends = Queue()

    def store_first_date():
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '10s'")
                cursor.execute("SELECT pg_backend_pid()")
                backends.put(cursor.fetchone()[0])
                row = PortalAccess.objects.get(pk=access.pk)
                row.ends_at = first
                row.save(update_fields=["ends_at"])
                return receiver_gate_held(receiver_account.pk)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as worker:
        with transaction.atomic(), connection.cursor() as cursor:
            access.ends_at = later
            access.save(update_fields=["ends_at"])
            assert receiver_gate_held(receiver_account.pk)
            future = worker.submit(store_first_date)
            writer = backends.get(timeout=5)
            deadline = monotonic() + 5
            while monotonic() < deadline:
                cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [writer])
                if cursor.fetchone()[0]:
                    break
                sleep(0.01)
            else:
                pytest.fail("The second save did not reach the first transaction's lock")
        second_held_gate = future.result(timeout=10)
    assert PortalAccess.objects.get(pk=access.pk).ends_at == first
    assert second_held_gate, (
        "The second transaction changed the stored date without holding the gate"
    )


@pytest.mark.xfail(strict=True, reason=MODEL_SAVE_LIMIT)
def test_receiver_partial_save_holds_gate_of_persisted_account(receiver_account):
    """A partial save writes the scheduled day only, irrespective of other pending model edits."""
    pending_owner = User.objects.create_user(
        "receiver-pending", "pending@example.invalid", client_id=receiver_account.client_id
    )
    access = receiver_new_access(receiver_account)
    end = timezone.now() + timedelta(days=12)
    access.user = pending_owner
    access.ends_at = end
    with transaction.atomic():
        access.save(update_fields=["ends_at"])
        stored = PortalAccess.objects.get(pk=access.pk)
        assert stored.ends_at == end
        assert stored.user_id == receiver_account.pk
        assert receiver_gate_held(stored.user_id)


# Review decisions and signed content


va_make_user = va_fixtures.make_user
va_clinic = va_fixtures.clinic


@pytest.fixture
def va_reviewer(va_make_user):
    person = va_make_user("verify_signer", "supervisor")
    person.user_permissions.add(va_fixtures.review_permission())
    return person


def va_grant_edit(http, account, day):
    if account.client_id is None:
        url = reverse("admin:accounts_user_change", args=[account.pk])
        form = http.get(url).context["adminform"].form
        data = page_data(
            form,
            confirm_password=va_fixtures.PASSWORD,
            **{end_field(patient_permission().codename): day},
        )
    else:
        url = reverse("clients:portal-access-edit", args=[account.client_id, account.pk])
        form = http.get(url).context["form"]
        data = {
            "scope": "organization",
            "shown_last_day": form["shown_last_day"].value() or "",
            "last_day": day,
            "expected_revision": form["expected_revision"].value(),
            "reason": "Verify the reviewer's copy of this grant.",
        }
    response = http.post(url, data)
    assert response.status_code == 302


@pytest.mark.django_db
@pytest.mark.parametrize("review_scope", [AccessScope.STAFF, AccessScope.PORTAL])
def test_redrawn_review_retains_only_the_decisions_on_unchanged_grants(
    client, va_reviewer, va_make_user, va_clinic, review_scope
):
    client.force_login(va_reviewer)
    manager = va_make_user("verify_editor", "account_manager", is_staff=True, is_superuser=True)
    manager_client = HttpClient()
    manager_client.force_login(manager)
    targets = {}
    day = grant_ends.today() + timedelta(days=8)
    initial_days = {
        "no_end": None,
        "same_day": day,
        "clear": day,
        "renew": day - timedelta(days=15),
        "stable": None,
        "stable_ending": day,
    }
    permission = patient_permission()
    with override_settings(TIME_ZONE="Etc/UTC"):
        for name, initial_day in initial_days.items():
            end = grant_ends.end_of(initial_day)
            if review_scope == AccessScope.PORTAL:
                account = va_make_user("verify_" + name, client=va_clinic)
                PortalAccess.objects.create(
                    user=account, client=va_clinic, whole_organization=True, ends_at=end
                )
            else:
                account = va_make_user("verify_" + name)
                account.user_permissions.add(permission)
                if end:
                    CapabilityEnd.objects.create(user=account, permission=permission, ends_at=end)
            targets[name] = account

    url = reverse("access_review:review-create")
    with override_settings(TIME_ZONE="America/New_York"):
        original = client.get(url, {"scope": review_scope})
        assert original.status_code == 200
        old = {
            g.account_id: g
            for g in original.context["reading"].grants
            if g.kind in (GrantKind.PORTAL, GrantKind.CAPABILITY)
        }
        if review_scope == AccessScope.STAFF:
            assert targets["renew"].pk not in old
        else:
            assert old[targets["renew"].pk].detail == "Only the tickets it raised"
        payload = posted(original, decide=Decision.REMOVE, findings="Recorded before the edit.")
        payload[old[targets["stable"].pk].field_name] = Decision.KEEP
        va_grant_edit(manager_client, targets["no_end"], day)
        va_grant_edit(manager_client, targets["clear"], "")
        va_grant_edit(manager_client, targets["renew"], day)
        # The first time this date was saved it ended at midnight UTC. Save the same printed
        # date in the deployment's new zone; its end is now several hours later.
        va_grant_edit(manager_client, targets["same_day"], "")
        va_grant_edit(manager_client, targets["same_day"], day)
        refused = client.post(url, payload)
        assert refused.status_code == 200 and not AccessReview.objects.exists()
        assert not AuditEvent.objects.filter(action=access_reviews.SIGNED).exists()
        form = refused.context["form"]
        assert access_reviews.STALE in form.non_field_errors()
        assert form["findings"].value() == payload["findings"]
        assert not form.password.value()
        new = {
            g.account_id: g
            for g in refused.context["reading"].grants
            if g.kind in (GrantKind.CAPABILITY, GrantKind.PORTAL)
        }
        for name in ("no_end", "same_day", "clear", "renew"):
            account = targets[name]
            assert form[new[account.pk].field_name].value() is None
        assert form.changed == {
            new[targets[name].pk].field_name for name in ("no_end", "same_day", "clear", "renew")
        }
        for name in ("stable", "stable_ending"):
            pk = targets[name].pk
            assert old[pk].field_name == new[pk].field_name
            assert form[new[pk].field_name].value() == payload[old[pk].field_name]
        compared = targets["same_day"].pk
        assert old[compared].detail == new[compared].detail
        assert old[compared].ends != new[compared].ends
        assert old[compared].field_name != new[compared].field_name
        assert new[targets["no_end"].pk].ends and not new[targets["clear"].pk].ends
        assert refused.context["undecided"] == 4
        # The person chooses Keep on the four changed grants and leaves every other choice intact.
        accepted = posted(refused, findings=payload["findings"])
        for g in refused.context["reading"].grants:
            accepted[g.field_name] = form[g.field_name].value() or Decision.KEEP
        result = client.post(url, accepted)
        assert result.status_code == 302
        stored = AccessReview.objects.get()
        assert access_reviews.intact(stored) and stored.findings == payload["findings"]
        decisions = {
            i.account_id: i.decision
            for i in stored.items.all()
            if i.kind in (GrantKind.CAPABILITY, GrantKind.PORTAL)
        }
        for name in initial_days:
            expected = Decision.REMOVE if name == "stable_ending" else Decision.KEEP
            assert decisions[targets[name].pk] == expected


def va_legacy_shown(grant):
    # The exact serialization used by fefa909 and 689675d to draw the decision inputs.
    return [
        grant.account_id,
        grant.account_name,
        grant.account_username,
        grant.organization_name,
        grant.kind,
        grant.code,
        grant.label,
        grant.detail,
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("end_added", [False, True], ids=["no-grant-change", "one-grant-changed"])
def test_review_open_during_upgrade_keeps_decisions_for_grants_that_did_not_change(
    client, va_reviewer, va_make_user, monkeypatch, end_added
):
    subject = va_make_user("upgrade_holder", "technical")
    subject.user_permissions.add(patient_permission())
    client.force_login(va_reviewer)
    url = reverse("access_review:review-create")
    # Render a normal form with the previous release's grant shape; process its unmodified HTTP
    # fields on the current release, as happens when a review is open through deployment.
    with monkeypatch.context() as legacy:
        legacy.setattr(access_reviews.Grant, "shown", va_legacy_shown)
        old = client.get(url)
        assert old.status_code == 200
        sent = posted(old, decide=Decision.REMOVE)
    if end_added:
        CapabilityEnd.objects.create(
            user=subject,
            permission=patient_permission(),
            ends_at=grant_ends.end_of(grant_ends.today() + timedelta(days=8)),
        )
    result = client.post(url, sent)
    if not end_added:
        assert result.status_code == 302, (
            "The upgrade discarded decisions without any grant change."
        )
        assert AccessReview.objects.get().removals == len(old.context["reading"].grants)
        return
    assert result.status_code == 200 and not AccessReview.objects.exists()
    form = result.context["form"]
    assert access_reviews.STALE in form.non_field_errors()
    changed = next(
        g
        for g in result.context["reading"].grants
        if g.account_id == subject.pk and g.kind == GrantKind.CAPABILITY
    )
    assert form.changed == {changed.field_name}
    for grant in result.context["reading"].grants:
        assert form[grant.field_name].value() == (None if grant == changed else Decision.REMOVE)


@pytest.mark.django_db(transaction=True)
def test_review_keeps_a_ticket_raiser_only_decision_when_the_unused_access_row_expires(
    client, va_reviewer, va_clinic, va_make_user
):
    account = va_make_user("no_sites_left", client=va_clinic)
    site = ClientSite.objects.create(client=va_clinic, name="Synthetic site", code="VX")
    client.force_login(va_reviewer)
    url = reverse("access_review:review-create")
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT transaction_timestamp() + interval '1 microsecond'")
            boundary = cursor.fetchone()[0]
        access = PortalAccess.objects.create(user=account, client=va_clinic, ends_at=boundary)
        access.sites.add(site)
        assert current_access(account) is not None
        access.sites.remove(site)
        assert current_access(account) is None
        original = client.get(url, {"scope": AccessScope.PORTAL})
        (original_grant,) = original.context["reading"].grants
        assert original_grant.detail == "Only the tickets it raised"
        sent = posted(original, decide=Decision.REMOVE)
    assert current_access(account) is None
    now = client.get(url, {"scope": AccessScope.PORTAL})
    (current_grant,) = now.context["reading"].grants
    assert (current_grant.key, current_grant.detail) == (original_grant.key, original_grant.detail)
    assert PortalAccess.objects.get(user=account).ends_at == boundary
    result = client.post(url, sent)
    assert result.status_code == 302, "The account held exactly the same portal grant."
    assert AccessReview.objects.get().items.get().decision == Decision.REMOVE


@pytest.mark.django_db
def test_pre_fix_review_checksum_still_validates_on_the_review_page(client, va_reviewer):
    # Immutable synthetic fixture with the SHA-256 produced by 689675d for these fields.
    review = AccessReview.objects.create(
        number="AC-000048",
        scope=AccessScope.STAFF,
        accounts=1,
        grants=1,
        removals=1,
        findings="Temporary assignment ended.",
        follow_up="",
        signed_by_id=48173,
        signer_name="Renée Example",
        signer_username="synthetic_checker",
        signer_roles="Supervisor",
        signed_at=datetime.fromisoformat("2026-05-08T10:15:30.543210+00:00"),
        checksum="b5b339250fad483b8efa821465a5149f22d0335130202171d10f3bcb3b5fe3e2",
    )
    AccessReviewItem.objects.create(
        review=review,
        position=1,
        account_id=48174,
        account_name="Kai Example",
        account_username="synthetic_holder",
        organization_name="",
        kind=GrantKind.CAPABILITY,
        code="view_patient_information",
        label="Can view patient information",
        detail="Given directly, last day 2026-05-17",
        decision=Decision.REMOVE,
    )
    client.force_login(va_reviewer)
    result = client.get(reverse("access_review:review", args=[review.pk]))
    assert result.status_code == 200 and result.context["intact"]
    assert b"Matches the access review as stored." in result.content
    assert review.checksum.encode() in result.content


# Organization moves and trigger migrations


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path,whole",
    [
        ("admin", False),
        ("full-save", True),
        ("client-id-save", False),
        ("queryset", True),
        ("bulk-update", False),
        ("sql", False),
    ],
)
def test_moving_away_and_back_clears_the_end_and_records_only_attributed_moves(
    client_org, portal_user, client, path, whole
):
    """A round trip crosses the trigger twice. The first history entry needs the old grant."""
    person = portal_user(client_org)
    operator = User.objects.create_user(
        "directory-owner", email="directory-owner@example.invalid", is_staff=True, is_superuser=True
    )
    destination = Organization.objects.create(name="Destination clinic")
    site = client_org.sites.create(code="BR", name="Branch")
    contact = client_org.contacts.create(name="Synthetic contact", phone="555")
    moment = grant_ends.end_of(grant_ends.today() + timedelta(days=10))
    access = PortalAccess.objects.create(
        user=person, client=client_org, contact=contact, whole_organization=whole, ends_at=moment
    )
    if not whole:
        access.sites.add(site)

    def move(new_org):
        if path == "admin":
            client.force_login(operator)
            url = reverse("admin:accounts_user_change", args=[person.pk])
            form = client.get(url).context["adminform"].form
            assert client.post(url, page_data(form, client=new_org.pk)).status_code == 302
        elif path in {"full-save", "client-id-save"}:
            person.client = new_org
            person.changed_by = operator
            person.save(update_fields=["client_id"] if path == "client-id-save" else None)
        elif path == "queryset":
            assert User.objects.filter(pk=person.pk).update(client_id=new_org.pk) == 1
        elif path == "bulk-update":
            person.client = new_org
            assert User.objects.bulk_update([person], ["client"]) == 1
        else:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE accounts_user SET client_id = %s WHERE id = %s", [new_org.pk, person.pk]
                )
                assert cursor.rowcount == 1

    for revision, new_org in enumerate((destination, client_org), start=access.revision + 1):
        move(new_org)
        access.refresh_from_db()
        person.refresh_from_db()
        assert access.revision == revision
        assert access.client_id == person.client_id == new_org.pk
        assert access.ends_at is None and access.contact_id is None
        assert not access.whole_organization and not access.sites.exists()
        assert current_access(person) is None

    events = AuditEvent.objects.filter(action="portal_access.reset", object_id=str(access.pk))
    if path in {"admin", "full-save", "client-id-save"}:
        assert events.count() == 2
        first = events.get(after__client=destination.pk)
        assert first.actor_id == operator.pk
        assert first.before == {
            "client": client_org.pk,
            "contact": contact.pk,
            "whole_organization": whole,
            "sites": [] if whole else [site.pk],
            "ends_at": grant_ends.iso(moment),
        }
        assert first.after == {
            "client": destination.pk,
            "contact": None,
            "whole_organization": False,
            "sites": [],
            "ends_at": None,
            "user": person.pk,
        }
    else:
        assert not events.exists()


@pytest.mark.django_db
def test_trigger_preserves_same_organization_access_and_rolls_back_a_move(client_org, portal_user):
    person = portal_user(client_org)
    destination = Organization.objects.create(name="Transfer office")
    moment = grant_ends.end_of(grant_ends.today())
    access = PortalAccess.objects.create(
        user=person, client=client_org, whole_organization=True, ends_at=moment
    )
    previous_revision = access.revision
    User.objects.filter(pk=person.pk).update(client=client_org)
    access.refresh_from_db()
    assert access.ends_at == moment and access.revision == previous_revision

    with transaction.atomic():
        User.objects.filter(pk=person.pk).update(client=destination)
        access.refresh_from_db()
        assert access.ends_at is None and access.revision == previous_revision + 1
        transaction.set_rollback(True)
    access.refresh_from_db()
    person.refresh_from_db()
    assert current_access(person).ends_at == moment
    assert access.revision == previous_revision and access.client_id == client_org.pk


@pytest.mark.django_db(transaction=True)
def test_moving_after_migration_rollback_and_reapplication_uses_each_versions_trigger():
    """Execute both installed trigger versions on real rows, including after the column is gone."""
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    previous = [("clients", "0007_account_move_empties_portal_access")]
    candidate = [("clients", "0008_grant_ends")]

    org = Organization.objects.create(name="Before rollback")
    other_org = Organization.objects.create(name="After rollback")
    person = User.objects.create_user("migration-portal", "migration@example.invalid", client=org)
    site = org.sites.create(code="N", name="North")
    access = PortalAccess.objects.create(
        user=person, client=org, ends_at=grant_ends.end_of(grant_ends.today()), revision=17
    )
    access.sites.add(site)
    try:
        executor.migrate(previous)
        old_models = executor.loader.project_state(previous).apps
        old_user = old_models.get_model("accounts", "User")
        old_access = old_models.get_model("clients", "PortalAccess")
        old_user.objects.filter(pk=person.pk).update(client_id=other_org.pk)
        moved = old_access.objects.get(pk=access.pk)
        assert moved.client_id == other_org.pk and moved.revision == 18
        assert not moved.sites.exists() and not moved.whole_organization

        MigrationExecutor(connection).migrate(candidate)
        access.refresh_from_db()
        assert access.ends_at is None and access.revision == 18
        with transaction.atomic():
            access.whole_organization = True
            access.ends_at = grant_ends.end_of(grant_ends.today() + timedelta(days=2))
            access.save(update_fields=["whole_organization", "ends_at"])
        User.objects.filter(pk=person.pk).update(client_id=org.pk)
        access.refresh_from_db()
        assert access.ends_at is None and not access.whole_organization
        assert access.client_id == org.pk and access.revision == 19
    finally:
        MigrationExecutor(connection).migrate(leaves)
