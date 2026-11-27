"""Adopted from the independent review of feat/grant-end-dates, fourth confirmation round (report
.local/review-history/grant-ends-confirm4.md; original archived beside it). No change on adoption.

Fourth confirmation of grant date redraws and the day used by demo seed commands."""

from datetime import UTC, datetime, time, timedelta
from io import StringIO
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from accounts import grant_ends
from accounts.admin import CHANGED_SINCE_DRAWN, UNSENT, end_field, shown_field
from accounts.models import CapabilityEnd, User
from accounts.tests.conftest import make_user as make_user
from accounts.tests.test_confirm_identity import page_data
from audit_review.models import AccessReview, AccessScope
from clients import seed as client_seed
from clients.models import PortalAccess
from clients.services import set_portal_access
from core.models import Announcement, AuditEvent
from core.seeding import SeedContext
from desk import story
from tests.test_review_grant_ends import (
    ACCT_PATIENT,
    ACCT_REVIEW,
    acct_end,
    acct_events,
    acct_form,
    acct_http,
    acct_perm,
    acct_user,
)
from tests.test_review_grant_ends_confirm3 import (
    SEED_ZONE,
    audited_seed_dates,
    database_seed_day,
    rendered_account_markers,
    set_demo_passwords,
)


@pytest.fixture
def demo_clock_settings(settings, monkeypatch):
    settings.TIME_ZONE = SEED_ZONE
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    set_demo_passwords(monkeypatch)


def local_midnight(day):
    return datetime.combine(day, time.min, ZoneInfo(SEED_ZONE)).astimezone(UTC)


@pytest.mark.django_db
@pytest.mark.parametrize("command", ["seed_demo", "reset_demo"])
def test_fresh_seed_and_reset_use_the_day_captured_before_either_grant_is_written(
    demo_clock_settings, monkeypatch, command
):
    stream = StringIO()
    db_day = database_seed_day()
    if command == "reset_demo":
        monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
        call_command("seed_demo", stdout=stream)
        first_dates = audited_seed_dates()

    start_day = db_day + timedelta(days=6)
    midnight = local_midnight(start_day + timedelta(days=1))
    instants = [midnight - timedelta(seconds=1)]
    monkeypatch.setattr(timezone, "now", lambda: instants[-1])
    setup = client_seed.seed_setup

    def advance_after_context_creation(ctx):
        setup(ctx)
        assert not PortalAccess.objects.exists() and not CapabilityEnd.objects.exists()
        instants.append(midnight + timedelta(seconds=1))

    monkeypatch.setattr(client_seed, "seed_setup", advance_after_context_creation)
    options = {"interactive": False} if command == "reset_demo" else {}
    call_command(command, announce_accounts=True, stdout=stream, **options)
    assert len(instants) == 2 and grant_ends.today() == start_day + timedelta(days=1)
    expected = start_day + timedelta(days=90)
    assert audited_seed_dates() == (expected, expected)
    if command == "reset_demo":
        assert first_dates != (expected, expected)
    review = AccessReview.objects.get(scope=AccessScope.STAFF)
    item = review.items.get(account_username="demo_supervisor", code=ACCT_PATIENT)
    assert item.detail == f"Given directly, last day {expected}"
    assert Announcement.objects.filter(title="Demo accounts").count() == 1


@pytest.mark.django_db
def test_a_second_seed_on_a_later_day_keeps_operator_dates_and_their_history(
    demo_clock_settings, monkeypatch
):
    stream = StringIO()
    day = database_seed_day()
    instant = local_midnight(day) + timedelta(hours=12)
    monkeypatch.setattr(timezone, "now", lambda: instant)
    call_command("seed_demo", stdout=stream)
    assert audited_seed_dates() == (day + timedelta(days=90),) * 2

    supervisor = User.objects.get(username="demo_supervisor")
    editor = acct_http("seed-date-editor")
    url, form = acct_form(editor, supervisor)
    response = editor.post(url, page_data(form, **{end_field(ACCT_PATIENT): ""}))
    assert (
        response.status_code == 302 and not CapabilityEnd.objects.filter(user=supervisor).exists()
    )
    access = PortalAccess.objects.get(user__username="demo_client_water")
    chosen = day + timedelta(days=17)
    saved = set_portal_access(
        actor=User.objects.get(username="demo_account_manager"),
        user_id=access.user_id,
        expected_client_id=access.client_id,
        expected_revision=access.revision,
        contact=access.contact,
        sites=list(access.sites.all()),
        last_day=chosen,
        reason="Shorter demonstration schedule",
    )
    events = AuditEvent.objects.filter(
        action__in=["account.capability_end_set", "portal_access.set", "account.updated"]
    ).order_by("pk")
    prior_events = list(events.values_list("pk", "after"))
    review = AccessReview.objects.get(scope=AccessScope.STAFF)
    signed_item = review.items.get(account_username=supervisor.username, code=ACCT_PATIENT)
    signed_detail = signed_item.detail
    instant += timedelta(days=4)
    call_command("seed_demo", stdout=stream)
    access.refresh_from_db()
    assert access.ends_at == grant_ends.end_of(chosen) and access.revision == saved.revision
    assert supervisor.user_permissions.filter(codename=ACCT_PATIENT).exists()
    assert not CapabilityEnd.objects.filter(user=supervisor).exists()
    assert list(events.values_list("pk", "after")) == prior_events
    signed_item.refresh_from_db()
    assert (
        signed_item.detail
        == signed_detail
        == f"Given directly, last day {day + timedelta(days=90)}"
    )


@pytest.mark.django_db
@pytest.mark.parametrize("supply_day", [False, True], ids=["default-context", "supplied-day"])
def test_standalone_seed_steps_support_both_the_default_context_and_a_supplied_day(
    demo_clock_settings, monkeypatch, make_user, supply_day
):
    db_day = database_seed_day()
    app_day = db_day - timedelta(days=3)
    monkeypatch.setattr(timezone, "now", lambda: local_midnight(app_day) + timedelta(hours=12))
    make_user("demo_account_manager", "account_manager")
    make_user("demo_admin", "admin")
    make_user("demo_supervisor", "supervisor")
    password = "synthetic-portal-fixture-password"
    if supply_day:
        anchor = db_day + timedelta(days=8)
        ctx = SeedContext(password, day=anchor)
        story.grant_capability(day=anchor)
    else:
        anchor = db_day
        ctx = SeedContext(password)
        story.grant_capability()
    client_seed.seed_records(ctx)
    expected = anchor + timedelta(days=90)
    assert audited_seed_dates() == (expected, expected)

    # The optional arguments also work when the initial dates already exist.
    story.end_capability(day=anchor + timedelta(days=1))
    client_seed.seed_records(SeedContext(password, day=anchor + timedelta(days=1)))
    assert audited_seed_dates() == (expected, expected)


@pytest.mark.django_db
@pytest.mark.parametrize("has_review_end", [True, False], ids=["dated", "undated"])
def test_duplicate_email_redraw_keeps_the_supplied_day_and_fills_the_omitted_marker(
    has_review_end,
):
    subject = acct_user("unique-date-subject", is_active=False)
    existing = acct_user("unique-date-existing", is_active=False)
    editor = acct_http("unique-date-owner")
    patient_field, patient_marker = end_field(ACCT_PATIENT), shown_field(ACCT_PATIENT)
    review_field, review_marker = end_field(ACCT_REVIEW), shown_field(ACCT_REVIEW)
    original_day = grant_ends.today() - timedelta(days=9)
    chosen_day = grant_ends.today() + timedelta(days=28)
    with override_settings(TIME_ZONE="Etc/UTC"):
        retained = acct_end(subject, ACCT_PATIENT, grant_ends.end_of(original_day))
        patient_moment = retained.ends_at
        if has_review_end:
            previous_review = acct_end(
                subject, ACCT_REVIEW, grant_ends.end_of(chosen_day - timedelta(days=7))
            ).ends_at
        else:
            subject.user_permissions.add(acct_perm(ACCT_REVIEW))
            previous_review = None
        url, drawing = acct_form(editor, subject)
        request_data = page_data(drawing, email=existing.email, **{review_field: chosen_day})
        if has_review_end:
            request_data.pop(review_marker)
        else:
            request_data[review_marker] = UNSENT
        assert str(request_data[patient_marker]) == original_day.isoformat()
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        # The two controls require different readings: the supplied day is from the original
        # drawing, while an absent marker is the day of the held moment in the zone now in use.
        assert grant_ends.last_day_of(patient_moment) != original_day
        refused = editor.post(url, request_data)
        assert refused.status_code == 200
        form = refused.context["adminform"].form
        assert set(form.errors) == {"email"}
        assert "unique" in {err.code for err in form.errors.as_data()["email"]}
        markers = rendered_account_markers(refused)
        assert markers[patient_marker] == original_day.isoformat()
        assert markers[review_marker] == (
            grant_ends.last_day_of(previous_review).isoformat() if has_review_end else ""
        )
        assert str(form[patient_field].value()) == original_day.isoformat()
        assert str(form[review_field].value()) == chosen_day.isoformat()
    before = {f"accounts.{ACCT_PATIENT}": grant_ends.iso(patient_moment)}
    if previous_review:
        before[f"accounts.{ACCT_REVIEW}"] = grant_ends.iso(previous_review)
    subject.refresh_from_db()
    assert subject.email == "unique-date-subject@example.invalid"
    assert not acct_events(subject).exists()
    with override_settings(TIME_ZONE="America/New_York"):
        corrected_email = "unique-date-corrected@example.invalid"
        assert (
            editor.post(url, page_data(form, email=corrected_email, **markers)).status_code == 302
        )
        expected_review = grant_ends.end_of(chosen_day)
    subject.refresh_from_db()
    retained.refresh_from_db()
    assert subject.email == corrected_email
    assert retained.ends_at == patient_moment
    assert (
        CapabilityEnd.objects.get(user=subject, permission=acct_perm(ACCT_REVIEW)).ends_at
        == expected_review
    )
    event = acct_events(subject).get()
    assert event.before["capability_ends"] == before
    assert event.after["capability_ends"] == {
        f"accounts.{ACCT_PATIENT}": grant_ends.iso(patient_moment),
        f"accounts.{ACCT_REVIEW}": grant_ends.iso(expected_review),
    }


@pytest.mark.django_db
def test_a_redraw_with_a_held_marker_stays_stale_if_another_editor_clears_the_date():
    subject = acct_user("later-stale-subject", is_active=False)
    taken = acct_user("later-stale-other", is_active=False)
    first = acct_http("later-stale-owner")
    second = acct_http("later-stale-coworker")
    field, marker = end_field(ACCT_PATIENT), shown_field(ACCT_PATIENT)
    first_day = grant_ends.today() + timedelta(days=10)
    chosen = first_day + timedelta(days=5)
    with override_settings(TIME_ZONE="Etc/UTC"):
        acct_end(subject, ACCT_PATIENT, grant_ends.end_of(first_day))
        url, drawing = acct_form(first, subject)
        data = page_data(drawing, username=taken.username, **{field: chosen})
        data.pop(marker)
        refused = first.post(url, data)
        held = refused.context["adminform"].form
        assert set(held.errors) == {"username"}
        assert "unique" in {err.code for err in held.errors.as_data()["username"]}
        markers = rendered_account_markers(refused)
        assert markers[marker] == first_day.isoformat()
        assert not acct_events(subject).exists()
        _, coworker_form = acct_form(second, subject)
        assert second.post(url, page_data(coworker_form, **{field: ""})).status_code == 302
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        retry = first.post(url, page_data(held, username=subject.username, **markers))
        assert retry.status_code == 200
        stale = retry.context["adminform"].form
        assert CHANGED_SINCE_DRAWN in stale.non_field_errors()
        assert rendered_account_markers(retry)[marker] == first_day.isoformat()
        assert str(stale[field].value()) == chosen.isoformat()
    assert not CapabilityEnd.objects.filter(user=subject).exists()
    event = acct_events(subject).get()
    assert event.actor.username == "later-stale-coworker"
    assert event.after["capability_ends"] == {}
