"""Adopted from the independent review of feat/grant-end-dates, confirmation round (report
.local/review-history/grant-ends-confirm.md; original archived beside it). Change on adoption: the
two cases of an emptied hidden shown day are strict xfails of the documented limit (a form cannot
tell an altered shown day; docs/architecture.md).

Confirmation of the grant-end changes at aaaea1c, using synthetic application records."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from io import StringIO
from threading import Event, local

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.test import Client as HttpClient
from django.test import override_settings
from django.urls import reverse

from accounts import acting, grant_ends
from accounts.admin import CHANGED_SINCE_DRAWN, capability_names, end_field, shown_field
from accounts.models import CapabilityEnd, User
from accounts.roles import CAPABILITIES
from accounts.tests.test_confirm_identity import page_data
from audit_review import access_reviews
from audit_review.access_report import direct_with_ends
from audit_review.models import AccessReview, AccessScope, Decision, GrantKind
from audit_review.tests.test_access_reviews import patient_permission, posted
from clients.models import Client as Organization
from clients.models import ClientSite, PortalAccess
from clients.services import reset_portal_access, set_portal_access
from core.models import AuditEvent
from tests import test_review_grant_ends_verify as verify_review
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
from tests.test_review_grant_ends_verify import (
    ALTERED_SHOWN_DAY_LIMIT,
    receiver_gate_held,
    verify_posted_hidden,
    vportal_data,
)

# Changed markers and inventory reads

EMPTIED_SHOWN_DAY = pytest.mark.xfail(strict=True, reason=ALTERED_SHOWN_DAY_LIMIT)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "blank_marker",
    [False, pytest.param(True, marks=EMPTIED_SHOWN_DAY)],
    ids=["as-rendered", "blank"],
)
def test_name_edit_preserves_the_displayed_capability_end_after_zone_change(blank_marker):
    person = acct_user("confirm-dates", is_active=False)
    http = acct_http("confirm-owner")
    day = grant_ends.today() + timedelta(days=21)
    marker = shown_field(ACCT_REVIEW)
    with override_settings(TIME_ZONE="Etc/UTC"):
        stored = acct_end(person, ACCT_REVIEW, grant_ends.end_of(day))
        original = stored.ends_at
        url, form = acct_form(http, person)
        data = page_data(form, first_name="Avery")
        assert data[end_field(ACCT_REVIEW)] == data[marker] == day
    if blank_marker:
        data[marker] = ""
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        response = http.post(url, data)
    stored.refresh_from_db()
    person.refresh_from_db()
    assert not person.is_active
    assert stored.ends_at == original
    if not blank_marker:
        assert response.status_code == 302
    if response.status_code == 302:
        event = acct_events(person).get()
        assert person.first_name == "Avery"
        assert event.before["capability_ends"] == event.after["capability_ends"]
    else:
        assert response.status_code == 200 and response.context["adminform"].form.errors
        assert not acct_events(person).exists()


@pytest.mark.parametrize(
    "blank_marker",
    [False, pytest.param(True, marks=EMPTIED_SHOWN_DAY)],
    ids=["as-rendered", "blank"],
)
def test_contact_edit_preserves_the_displayed_portal_end_after_zone_change(
    portal_parties, client, blank_marker
):
    organization, manager, person = portal_parties
    person.is_active = False
    person.save(update_fields=["is_active"])
    contact = organization.contacts.create(name="Scheduling office", phone="555")
    client.force_login(manager)
    url = portal_edit_url(portal_parties)
    day = grant_ends.today() + timedelta(days=21)
    with override_settings(TIME_ZONE="Etc/UTC"):
        access = portal_set(portal_parties, day)
        original = access.ends_at
        data = vportal_data(client.get(url))
        assert str(data["last_day"]) == data["shown_last_day"] == day.isoformat()
    data.update(contact=contact.pk, reason="Correct the directory contact")
    if blank_marker:
        data["shown_last_day"] = ""
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        response = client.post(url, data)
    previous_revision = access.revision
    access.refresh_from_db()
    assert access.ends_at == original
    if not blank_marker:
        assert response.status_code == 302
    if response.status_code == 302:
        assert access.revision == previous_revision + 1
        assert access.contact_id == contact.pk
        event = AuditEvent.objects.get(action="portal_access.set", reason=data["reason"])
        assert event.before["ends_at"] == event.after["ends_at"]
    else:
        assert response.status_code == 200 and response.context["form"].errors
        assert access.contact_id is None and access.revision == previous_revision


@pytest.mark.django_db
def test_prefetched_admin_labels_agree_with_direct_grant_inventory(django_assert_num_queries):
    http = acct_http("confirm-reader")
    first = acct_user("ended-and-undated")
    second = acct_user("ending-only")
    cutoff = grant_ends.today()
    expired = acct_end(first, ACCT_PATIENT, grant_ends.end_of(cutoff - timedelta(days=7)))
    first.user_permissions.add(acct_perm(ACCT_REVIEW))
    future = acct_end(second, ACCT_PATIENT, grant_ends.end_of(cutoff + timedelta(days=7)))

    response = http.get(reverse("admin:accounts_user_changelist"))
    assert response.status_code == 200
    listed = {u.pk: u for u in response.context["cl"].result_list}
    with django_assert_num_queries(0):
        assert capability_names(listed[first.pk]) == [CAPABILITIES[ACCT_REVIEW]]
        assert capability_names(listed[second.pk]) == [CAPABILITIES[ACCT_PATIENT]]

    grants = User.user_permissions.through.objects.filter(user_id__in=[first.pk, second.pk])
    with django_assert_num_queries(1):
        rows = set(direct_with_ends(grants))
    assert rows == {
        (first.pk, ACCT_PATIENT, expired.ends_at, True),
        (first.pk, ACCT_REVIEW, None, False),
        (second.pk, ACCT_PATIENT, future.ends_at, False),
    }


# Account edits and calendar boundaries


@pytest.mark.parametrize(
    ("today", "last"),
    [
        (date(2000, 2, 29), date(2100, 2, 28)),
        (date(2096, 2, 29), date(2196, 2, 29)),
        (date(2100, 3, 1), date(2200, 3, 1)),
        (date(2026, 12, 31), date(2126, 12, 31)),
    ],
)
def test_calendar_limit_accepts_last_permitted_day_and_refuses_its_successor(
    monkeypatch, today, last
):
    monkeypatch.setattr(grant_ends, "today", lambda: today)
    assert grant_ends.latest_day(today) == last
    assert grant_ends.check_new_day(last, None) is None
    assert grant_ends.check_new_day(last + timedelta(days=1), None) == grant_ends.TOO_LATE
    assert grant_ends.check_new_day(date.max, None) == grant_ends.TOO_LATE
    assert grant_ends.check_new_day(date.max, date.max) is None


@pytest.mark.django_db
def test_invalid_century_day_leaves_both_dates_and_audit_unchanged_until_corrected(monkeypatch):
    monkeypatch.setattr(grant_ends, "today", lambda: date(2000, 2, 29))
    subject = acct_user("century-audit", is_active=False)
    http = acct_http("century-audit-owner")
    previous = grant_ends.end_of(date(2090, 1, 1))
    review = acct_end(subject, ACCT_REVIEW, previous)
    subject.user_permissions.add(acct_perm(ACCT_PATIENT))
    url, form = acct_form(http, subject)
    choice = date(2100, 2, 28)
    request_data = page_data(
        form,
        **{end_field(ACCT_REVIEW): choice, end_field(ACCT_PATIENT): choice + timedelta(days=1)},
    )
    refused = http.post(url, request_data)
    assert refused.status_code == 200
    redrawn = refused.context["adminform"].form
    assert set(redrawn.errors) == {end_field(ACCT_PATIENT)}
    assert grant_ends.TOO_LATE in redrawn.errors[end_field(ACCT_PATIENT)]
    assert not acct_events(subject).exists()
    review.refresh_from_db()
    assert review.ends_at == previous
    assert CapabilityEnd.objects.filter(user=subject).count() == 1
    corrected = page_data(redrawn, **{end_field(ACCT_PATIENT): ""})
    assert http.post(url, corrected).status_code == 302
    event = acct_events(subject).get()
    assert event.before["capability_ends"] == {f"accounts.{ACCT_REVIEW}": grant_ends.iso(previous)}
    assert event.after["capability_ends"] == {
        f"accounts.{ACCT_REVIEW}": grant_ends.iso(grant_ends.end_of(choice))
    }


@pytest.mark.django_db
def test_mixed_none_and_legacy_shown_days_survive_two_refusals_and_a_zone_change():
    subject = acct_user("mixed-indicators", is_active=False)
    http = acct_http("mixed-indicators-owner")
    past = grant_ends.today() - timedelta(days=12)
    with override_settings(TIME_ZONE="Etc/UTC"):
        ended = acct_end(subject, ACCT_REVIEW, grant_ends.end_of(past))
        original = ended.ends_at
        subject.user_permissions.add(acct_perm(ACCT_PATIENT))
        url, form = acct_form(http, subject)
        data = page_data(form, email="invalid", **{shown_field(ACCT_PATIENT): ""})
        data.pop(shown_field(ACCT_REVIEW))
        refused = http.post(url, data)
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        assert grant_ends.last_day_of(original) != past
        for attempt in range(2):
            assert refused.status_code == 200
            redrawn = refused.context["adminform"].form
            assert set(redrawn.errors) == {"email"}
            hidden = {
                shown_field(code): verify_posted_hidden(redrawn, shown_field(code))
                for code in (ACCT_PATIENT, ACCT_REVIEW)
            }
            assert hidden == {
                shown_field(ACCT_PATIENT): "",
                shown_field(ACCT_REVIEW): past.isoformat(),
            }
            if attempt == 0:
                refused = http.post(url, page_data(redrawn, **hidden))
        assert not acct_events(subject).exists()
        response = http.post(url, page_data(redrawn, email="fixed@example.invalid", **hidden))
    assert response.status_code == 302
    ended.refresh_from_db()
    assert ended.ends_at == original
    ends = {f"accounts.{ACCT_REVIEW}": grant_ends.iso(original)}
    event = acct_events(subject).get()
    assert event.before["capability_ends"] == event.after["capability_ends"] == ends


@pytest.mark.django_db
def test_inconsistent_metadata_for_undated_grant_cannot_create_a_change_event_by_itself():
    subject = acct_user("audit-no-change", is_active=False)
    subject.user_permissions.add(acct_perm(ACCT_REVIEW))
    http = acct_http("audit-no-change-owner")
    url, form = acct_form(http, subject)
    sample = grant_ends.today() - timedelta(days=14)
    data = page_data(form, **{shown_field(ACCT_REVIEW): sample, end_field(ACCT_REVIEW): sample})
    response = http.post(url, data)
    assert response.status_code == 302
    assert not CapabilityEnd.objects.filter(user=subject).exists()
    assert not acct_events(subject).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("legacy", [False, True], ids=["current-form", "pre-fix-form"])
def test_account_save_compares_its_chosen_day_with_the_held_account(monkeypatch, legacy):
    subject = acct_user("legacy-held", is_active=False)
    legacy_editor = acct_http("legacy-chosen-owner")
    correcting_editor = acct_http("legacy-correction-owner")
    original_day = grant_ends.today() + timedelta(days=21)
    chosen_day = original_day + timedelta(days=10)
    end = acct_end(subject, ACCT_REVIEW, grant_ends.end_of(original_day))
    field = end_field(ACCT_REVIEW)
    url, original_form = acct_form(legacy_editor, subject)
    data = page_data(original_form, first_name="Casey", **{field: chosen_day})
    if legacy:
        data.pop(shown_field(ACCT_REVIEW))

    # Another editor first saves the chosen day, then restores the original day before
    # the first request compares the fingerprint with the held account.
    _, current = acct_form(correcting_editor, subject)
    assert correcting_editor.post(url, page_data(current, **{field: chosen_day})).status_code == 302
    _, resetting = acct_form(correcting_editor, subject)
    assert resetting.initial[field] == chosen_day
    restore = page_data(resetting, **{field: original_day})

    before_gate = Event()
    restored = Event()
    submitting = local()
    lock = acting.close_access

    def synchronize(ids):
        ids = list(ids)
        if (
            getattr(submitting, "original", False)
            and subject.pk in ids
            and not before_gate.is_set()
        ):
            before_gate.set()
            assert restored.wait(15)
        return lock(ids)

    monkeypatch.setattr(acting, "close_access", synchronize)

    def submit():
        submitting.original = True
        try:
            return legacy_editor.post(url, data)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        submitted = pool.submit(submit)
        try:
            assert before_gate.wait(15)
            assert correcting_editor.post(url, restore).status_code == 302
            end.refresh_from_db()
            assert end.ends_at == grant_ends.end_of(original_day)
        finally:
            restored.set()
        response = submitted.result(timeout=15)

    subject.refresh_from_db()
    end.refresh_from_db()
    if response.status_code == 200:
        assert CHANGED_SINCE_DRAWN in response.context["adminform"].form.non_field_errors()
        assert subject.first_name == "" and end.ends_at == grant_ends.end_of(original_day)
        assert acct_events(subject).count() == 2
    else:
        assert response.status_code == 302
        assert subject.first_name == "Casey"
        event = acct_events(subject).get(actor__username="legacy-chosen-owner")
        assert end.ends_at == grant_ends.end_of(chosen_day), (
            f"The accepted save recorded {event.after['capability_ends']} and omitted the edit"
        )
        assert event.after["capability_ends"] == {
            f"accounts.{ACCT_REVIEW}": grant_ends.iso(end.ends_at)
        }


# Portal form retries and resets


@pytest.mark.parametrize("keep", [True, False], ids=["keep", "choose-another-day"])
def test_portal_date_after_two_refusals_and_a_zone_change(portal_parties, client, keep):
    org, manager, _ = portal_parties
    contact = org.contacts.create(name="Routing contact", phone="555")
    client.force_login(manager)
    url = portal_edit_url(portal_parties)
    with override_settings(TIME_ZONE="America/New_York"):
        chosen = grant_ends.today() + timedelta(days=10)
        row = portal_set(portal_parties, chosen)
        original_end = row.ends_at
        legacy = vportal_data(client.get(url))
        del legacy["shown_last_day"]
        refused = client.post(url, {**legacy, "contact": contact.pk, "reason": ""})
        assert set(refused.context["form"].errors) == {"reason"}
        data = vportal_data(refused)
        assert data["shown_last_day"] == chosen.isoformat()
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        zone_day = grant_ends.last_day_of(original_end)
        assert zone_day != chosen
        data["last_day"] = (grant_ends.today() - timedelta(days=1)).isoformat()
        data["reason"] = "Update the contact"
        bad_day = client.post(url, data)
        assert set(bad_day.context["form"].errors) == {"last_day"}
        assert grant_ends.PORTAL_PAST_DAY in bad_day.context["form"].errors["last_day"]
        retry = vportal_data(bad_day)
        assert retry["shown_last_day"] == chosen.isoformat()
        assert AuditEvent.objects.filter(action="portal_access.set").count() == 1
        # The date Tokyo displays for this saved instant is itself a different choice from
        # the day this page offered in New York.
        last_day = chosen if keep else zone_day
        result = client.post(url, {**retry, "last_day": last_day.isoformat()})
        assert result.status_code == 302
        row.refresh_from_db()
        expected = original_end if keep else grant_ends.end_of(zone_day)
        assert row.contact == contact and row.ends_at == expected
        saved = AuditEvent.objects.get(
            action="portal_access.set", object_id=str(row.pk), reason="Update the contact"
        )
        assert saved.before["ends_at"] == grant_ends.iso(original_end)
        assert saved.after["ends_at"] == grant_ends.iso(expected)


@pytest.mark.parametrize("after_regrant", [False, True], ids=["after-reset", "after-regrant"])
def test_old_portal_forms_stay_stale_across_a_reset_and_regrant(
    portal_parties, client, staff, after_regrant
):
    org, manager, account = portal_parties
    first_day = grant_ends.today() + timedelta(days=15)
    row = portal_set(portal_parties, first_day)
    client.force_login(manager)
    url = portal_edit_url(portal_parties)
    data = vportal_data(client.get(url))
    # A legacy form redrawn for an unrelated error still belongs to the opening revision.
    del data["shown_last_day"]
    refused = client.post(url, {**data, "reason": ""})
    assert set(refused.context["form"].errors) == {"reason"}
    old = vportal_data(refused)
    first_revision = row.revision
    actor = staff("alternate_directory_editor", "account_manager")
    reset_portal_access(actor=actor, user_id=account.pk, expected_client_id=org.pk)
    row.refresh_from_db()
    assert row.revision == first_revision + 1 and row.ends_at is None
    resets = AuditEvent.objects.filter(action="portal_access.reset", object_id=str(row.pk))
    assert resets.count() == 1 and resets.get().before["ends_at"] == grant_ends.iso(
        grant_ends.end_of(first_day)
    )
    assert resets.get().after["ends_at"] is None
    # A second reset sees the empty scope; it does not manufacture an extra write.
    assert reset_portal_access(actor=actor, user_id=account.pk, expected_client_id=org.pk) is None
    row.refresh_from_db()
    assert row.revision == first_revision + 1 and resets.count() == 1
    reviewer = HttpClient()
    reviewer.force_login(actor)
    if after_regrant:
        reopened = vportal_data(reviewer.get(url))
        assert reopened["shown_last_day"] == ""
        assert int(reopened["expected_revision"]) == first_revision + 1
        response = reviewer.post(
            url,
            {**reopened, "last_day": first_day.isoformat(), "reason": "Restore the agreed date"},
        )
        assert response.status_code == 302
    row.refresh_from_db()
    expected = grant_ends.end_of(first_day) if after_regrant else None
    assert row.ends_at == expected and row.revision == first_revision + 1 + int(after_regrant)
    count = AuditEvent.objects.filter(action="portal_access.set", object_id=str(row.pk)).count()
    retry = client.post(url, {**old, "last_day": "", "reason": "Clear on the original form"})
    assert retry.status_code == 200
    assert "Reload and review" in str(retry.context["form"].non_field_errors())
    row.refresh_from_db()
    assert row.ends_at == expected and row.revision == first_revision + 1 + int(after_regrant)
    assert (
        AuditEvent.objects.filter(action="portal_access.set", object_id=str(row.pk)).count()
        == count
    )


@pytest.mark.parametrize("offset", [0, 1], ids=["february-28", "march-1"])
def test_new_portal_access_from_a_leap_day_uses_february_end_in_a_nonleap_century(
    portal_parties, client, monkeypatch, offset
):
    _, manager, account = portal_parties
    monkeypatch.setattr(grant_ends, "today", lambda: date(2000, 2, 29))
    chosen = date(2100, 2, 28) + timedelta(days=offset)
    client.force_login(manager)
    url = portal_edit_url(portal_parties)
    data = vportal_data(client.get(url))
    response = client.post(url, {**data, "last_day": chosen.isoformat()})
    if offset:
        assert response.status_code == 200
        assert response.context["form"].errors["last_day"] == [grant_ends.TOO_LATE]
        assert not PortalAccess.objects.filter(user=account).exists()
        assert not AuditEvent.objects.filter(action="portal_access.set").exists()
    else:
        assert response.status_code == 302
        assert PortalAccess.objects.get(user=account).ends_at == grant_ends.end_of(chosen)


def test_legacy_redraw_cannot_treat_the_previous_organizations_day_as_a_kept_day(
    portal_parties, client
):
    org, manager, account = portal_parties
    former = Organization.objects.create(name="Former clinic", account_number="FORMERC")
    past_day = grant_ends.today() - timedelta(days=12)
    former_end = grant_ends.end_of(past_day)
    row = PortalAccess.objects.create(
        user=account, client=former, whole_organization=True, ends_at=former_end
    )
    client.force_login(manager)
    url = portal_edit_url(portal_parties)
    data = vportal_data(client.get(url))
    assert data.pop("shown_last_day") == "" and not data.get("last_day")
    data.update(last_day=past_day.isoformat(), reason="")
    missing_reason = client.post(url, data)
    assert set(missing_reason.context["form"].errors) == {"reason"}
    retry = vportal_data(missing_reason)
    assert retry["shown_last_day"] == ""
    refusal = client.post(url, {**retry, "reason": "Decide access for the current clinic"})
    assert refusal.status_code == 200
    assert refusal.context["form"].errors["last_day"] == [grant_ends.PORTAL_PAST_DAY]
    row.refresh_from_db()
    assert row.client_id == former.pk and row.ends_at == former_end
    correction = vportal_data(refusal)
    future = grant_ends.today() + timedelta(days=3)
    assert client.post(url, {**correction, "last_day": future.isoformat()}).status_code == 302
    row.refresh_from_db()
    assert row.client_id == org.pk and row.ends_at == grant_ends.end_of(future)


# Access review signing


va_make_user = verify_review.va_make_user
va_clinic = verify_review.va_clinic
va_reviewer = verify_review.va_reviewer


def ca_portal_rows(response):
    assert response.status_code == 200
    return {grant.account_username: grant for grant in response.context["reading"].grants}


@pytest.mark.django_db
def test_legacy_undated_portal_review_signs_with_the_same_decisions_and_fingerprint(
    client, va_reviewer, va_make_user, va_clinic, monkeypatch
):
    absent = va_make_user("ca_absent", client=va_clinic)
    whole = va_make_user("ca_whole", client=va_clinic)
    sited = va_make_user("ca_listed", client=va_clinic)
    PortalAccess.objects.create(user=whole, client=va_clinic, whole_organization=True)
    access = PortalAccess.objects.create(user=sited, client=va_clinic)
    access.sites.add(ClientSite.objects.create(client=va_clinic, name="North collection", code="N"))
    client.force_login(va_reviewer)
    url = reverse("access_review:review-create")
    query = {"scope": AccessScope.PORTAL, "organization": va_clinic.pk}
    with monkeypatch.context() as legacy:
        legacy.setattr(access_reviews.Grant, "shown", verify_review.va_legacy_shown)
        opened = client.get(url, query)
        rows = ca_portal_rows(opened)
        original_fields = {name: row.field_name for name, row in rows.items()}
        data = posted(opened, decide=Decision.KEEP, findings="One account needs follow-up.")
    data[original_fields[sited.username]] = Decision.REMOVE
    current = client.get(url, query)
    assert posted(current)["expected"] == data["expected"]
    assert {
        name: row.field_name for name, row in ca_portal_rows(current).items()
    } == original_fields
    signed = client.post(url, data)
    assert signed.status_code == 302
    review = AccessReview.objects.get()
    assert access_reviews.intact(review)
    assert review.organization_id == va_clinic.pk
    assert dict(review.items.values_list("account_id", "decision")) == {
        absent.pk: Decision.KEEP,
        whole.pk: Decision.KEEP,
        sited.pk: Decision.REMOVE,
    }


@pytest.mark.django_db
def test_review_upgrade_keeps_an_undated_decision_beside_new_and_moved_dated_grants(
    client, va_reviewer, va_make_user, monkeypatch
):
    client.force_login(va_reviewer)
    dated = va_make_user("ca_dated", "technical")
    stable = va_make_user("ca_stable", "technical")
    owner = va_make_user("ca_owner", is_superuser=True, is_staff=True)
    owner_http = HttpClient()
    owner_http.force_login(owner)
    permission = patient_permission()
    for holder in (dated, stable):
        holder.user_permissions.add(permission)
    url = reverse("access_review:review-create")
    day = grant_ends.today() + timedelta(days=12)
    with override_settings(TIME_ZONE="Etc/UTC"):
        end = CapabilityEnd.objects.create(
            user=dated, permission=permission, ends_at=grant_ends.end_of(day)
        )
        first_instant = end.ends_at
        # This version displayed the last day but did not serialize its instant. Undated grants
        # had exactly the same field names as on the published version before the feature.
        with monkeypatch.context() as legacy:
            legacy.setattr(access_reviews.Grant, "shown", verify_review.va_legacy_shown)
            initial = client.get(url)
            assert initial.status_code == 200
            previous = next(
                g
                for g in initial.context["reading"].grants
                if g.account_id == dated.pk and g.kind == GrantKind.CAPABILITY
            )
            assert previous.detail == f"Given directly, last day {day}"
            old_fields = {g.key: g.field_name for g in initial.context["reading"].grants}
            data = posted(initial, decide=Decision.REMOVE, findings="Review in progress.")
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        verify_review.va_grant_edit(owner_http, dated, day)
        end.refresh_from_db()
        assert first_instant - end.ends_at == timedelta(hours=9)
        # A new reviewer decision is also needed for a previously absent grant.
        new_account = va_make_user("ca_grant_added")
        new_account.user_permissions.add(permission)
        refused = client.post(url, data)
        assert refused.status_code == 200 and not AccessReview.objects.exists()
        form = refused.context["form"]
        assert access_reviews.STALE in form.non_field_errors()
        assert form["findings"].value() == data["findings"]
        changed = set()
        decisions = posted(refused, findings=data["findings"])
        for grant in refused.context["reading"].grants:
            if grant.kind == GrantKind.CAPABILITY and grant.account_id in (
                dated.pk,
                new_account.pk,
            ):
                if grant.account_id == dated.pk:
                    assert (
                        grant.detail == previous.detail
                        and grant.field_name != old_fields[grant.key]
                    )
                changed.add(grant.field_name)
                assert form[grant.field_name].value() is None
                decisions[grant.field_name] = Decision.KEEP
            else:
                assert grant.field_name == old_fields[grant.key]
                assert form[grant.field_name].value() == data[grant.field_name]
                decisions[grant.field_name] = form[grant.field_name].value()
        assert form.changed == changed and refused.context["undecided"] == len(changed) == 2
        signed = client.post(url, decisions)
    assert signed.status_code == 302
    review = AccessReview.objects.get()
    assert access_reviews.intact(review) and review.findings == data["findings"]
    assert set(review.items.filter(decision=Decision.KEEP).values_list("account_id", "kind")) == {
        (dated.pk, GrantKind.CAPABILITY),
        (new_account.pk, GrantKind.CAPABILITY),
    }


@pytest.mark.django_db(transaction=True)
def test_one_review_refreshes_only_the_effective_portal_grants_that_actually_expired(
    client, va_reviewer, va_make_user, va_clinic
):
    url = reverse("access_review:review-create")
    client.force_login(va_reviewer)
    site = ClientSite.objects.create(client=va_clinic, name="South site", code="S")
    # Only the first two people lose a held scope when this transaction's clock advances.
    states = [
        ("org_to_own", "organization", "boundary"),
        ("sites_to_own", "sites", "boundary"),
        ("always_own", "empty", "boundary"),
        ("before_own", "sites", "past"),
        ("without_row", "absent", None),
        ("empty_undated", "empty", None),
        ("keep_sites", "sites", "future"),
        ("keep_all", "organization", None),
    ]
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT transaction_timestamp() + interval '1 microsecond'")
            boundary = cursor.fetchone()[0]
        moments = {
            None: None,
            "boundary": boundary,
            "past": boundary - timedelta(days=2),
            "future": boundary + timedelta(days=2),
        }
        for name, scope, moment in states:
            user = va_make_user(name, client=va_clinic)
            if scope == "absent":
                continue
            stored = PortalAccess.objects.create(
                user=user,
                client=va_clinic,
                whole_organization=scope == "organization",
                ends_at=moments[moment],
            )
            if scope == "sites":
                stored.sites.add(site)
        old_form = client.get(url, {"scope": AccessScope.PORTAL, "organization": va_clinic.pk})
        before = ca_portal_rows(old_form)
        payload = posted(old_form, decide=Decision.REMOVE)
        payload[before["keep_all"].field_name] = Decision.KEEP
    refreshed = client.post(url, payload)
    after = ca_portal_rows(refreshed)
    form = refreshed.context["form"]
    assert access_reviews.STALE in form.non_field_errors() and not AccessReview.objects.exists()
    changed = {"org_to_own", "sites_to_own"}
    assert form.changed == {after[name].field_name for name in changed}
    assert refreshed.context["undecided"] == len(changed)
    for name, _scope, _ in states:
        if name in changed:
            assert "last day" in before[name].detail and before[name].ends
            assert after[name].detail == "Only the tickets it raised" and not after[name].ends
            assert form[after[name].field_name].value() is None
        else:
            assert before[name].field_name == after[name].field_name
            assert form[after[name].field_name].value() == payload[before[name].field_name]
    assert (
        before["always_own"].detail == "Only the tickets it raised"
        and not before["always_own"].ends
    )
    assert after["keep_sites"].ends == grant_ends.iso(moments["future"])


# Portal writers and demo data


@pytest.fixture
def path_parties(transactional_db):
    organization = Organization.objects.create(name="Confirmation Directory")
    manager = User.objects.create_user("confirm-manager", "manager@example.invalid")
    manager.groups.add(Group.objects.get_or_create(name="client_services")[0])
    person = User.objects.create_user(
        "confirm-member", "member@example.invalid", client=organization
    )
    return organization, manager, person


@pytest.mark.parametrize("operation", ["form", "reset", "move", "model-save", "bulk-move"])
def test_portal_end_revision_and_gate_follow_the_writer_and_roll_back_together(
    path_parties,
    client,
    operation,
):
    organization, manager, person = path_parties
    first = grant_ends.today() + timedelta(days=20)
    later = first + timedelta(days=10)
    access = portal_set(path_parties, first)
    events = AuditEvent.objects.filter(object_type="clients.portalaccess", object_id=str(access.pk))
    previous_events = set(events.values_list("pk", flat=True))
    other = Organization.objects.create(name="Confirmation Second Directory")
    if operation == "form":
        client.force_login(manager)
        url = portal_edit_url(path_parties)
        form = client.get(url).context["form"]
    with transaction.atomic():
        assert not receiver_gate_held(person.pk)
        if operation == "form":
            response = client.post(
                url,
                {
                    "scope": form["scope"].value(),
                    "reason": "Confirm the booking dates",
                    "expected_revision": form["expected_revision"].value(),
                    "shown_last_day": form["shown_last_day"].value(),
                    "last_day": later.isoformat(),
                },
            )
            assert response.status_code == 302
        elif operation == "reset":
            reset_portal_access(
                actor=manager, user_id=person.pk, expected_client_id=organization.pk
            )
        elif operation == "move":
            person.changed_by = manager
            person.client = other
            person.save(update_fields=["client"])
        elif operation == "model-save":
            access.ends_at = grant_ends.end_of(later)
            access.save(update_fields=["ends_at"])
        else:
            User.objects.filter(pk=person.pk).update(client=other)

        native = operation in {"form", "reset", "move"}
        clears = operation in {"move", "bulk-move", "reset"}
        stored = PortalAccess.objects.get(pk=access.pk)
        assert receiver_gate_held(person.pk) is native
        assert stored.ends_at == (None if clears else grant_ends.end_of(later))
        assert stored.revision == access.revision + int(operation != "model-save")
        assert stored.client_id == (
            other.pk if operation in {"move", "bulk-move"} else organization.pk
        )
        assert events.count() == len(previous_events) + int(native)
        if native:
            latest = events.exclude(pk__in=previous_events).get()
            assert latest.actor_id == manager.pk
            assert latest.before["ends_at"] == grant_ends.iso(grant_ends.end_of(first))
            assert latest.after["ends_at"] == grant_ends.iso(stored.ends_at)
        transaction.set_rollback(True)
    restored = PortalAccess.objects.get(pk=access.pk)
    assert restored.ends_at == grant_ends.end_of(first) and restored.revision == access.revision
    assert (
        restored.client_id == organization.pk
        and set(events.values_list("pk", flat=True)) == previous_events
    )


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
def test_demo_seed_creates_its_dated_portal_grant_through_the_gated_service(
    transactional_db, monkeypatch
):
    observed = []

    def record_service_use(**kwargs):
        grant = set_portal_access(**kwargs)
        if kwargs.get("last_day"):
            observed.append((grant.pk, grant.revision, receiver_gate_held(grant.user_id)))
        return grant

    monkeypatch.setattr("clients.seed.set_portal_access", record_service_use)
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-portal-confirmation-password")
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    output = StringIO()
    call_command("seed_demo", stdout=output, stderr=output)
    seeded = PortalAccess.objects.get(user__username="demo_client_water")
    assert observed == [(seeded.pk, 1, True)]
    assert seeded.ends_at == grant_ends.end_of(grant_ends.today() + timedelta(days=90))
    event = AuditEvent.objects.get(action="portal_access.set", object_id=str(seeded.pk))
    assert event.actor.username == "demo_account_manager"
    assert event.after["ends_at"] == grant_ends.iso(seeded.ends_at)
