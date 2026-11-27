"""Regression review of time-limited staff capabilities and portal access.

Adopted from the independent review of feat/grant-end-dates, round 1 (report
.local/review-history/grant-ends-round-1.md; originals archived beside it). Changes on adoption:
the zone "UTC" is written "Etc/UTC" (CI's runner may lack legacy zone names), and the portal
zone-change case posts the hidden shown_last_day the Set access page sends."""

import csv
import io
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from queue import Queue
from time import monotonic, sleep

import pytest
from allauth.mfa.models import Authenticator
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import OperationalError, close_old_connections, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client as HttpClient
from django.test import override_settings
from django.test.html import Element, parse_html
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags

from accounts import grant_ends
from accounts.acting import ACCESS_GATE, hold_actor
from accounts.admin import CHANGED_SINCE_DRAWN, NOT_DIRECT, end_field
from accounts.models import CapabilityEnd, User
from accounts.roles import (
    CAPABILITIES,
    REVIEW_CAPABILITY,
    ROLES,
    has_capability,
    require_capability,
    roles_and_capabilities,
)
from accounts.tests.conftest import PASSWORD as STAFF_PASSWORD
from accounts.tests.test_confirm_identity import page_data
from accounts.tests.test_second_factor_enrolment import session_row, sign_in
from audit_review import access_reviews
from audit_review.models import AccessReview, AccessScope, Decision, WorkspaceUse
from audit_review.tests import conftest as ar_fixtures
from audit_review.tests.conftest import PASSWORD as AR_PASSWORD
from audit_review.tests.conftest import review_permission
from audit_review.tests.test_access_reviews import patient_permission, posted, sign
from audit_review.tests.test_reviews import sign as ar_sign_audit_review
from clients.models import Client as Organization
from clients.models import PortalAccess
from clients.portal import current_access, portal_contacts
from clients.services import set_portal_access
from core import second_factor
from core.models import AuditEvent, SecondFactorRule
from core.security_settings import patient_capability_holders, review_capability_holders
from desk.notifications import client_recipients, prepare_request_notice
from desk.portal_scope import portal_requests, portal_sites, site_required
from desk.services import create_request
from desk.tests.test_portal import raise_staff
from jobs.actor import system_account
from jobs.models import Notice
from links import services as staff_link_services
from links.models import ExternalRecordLink, ExternalSystem
from request_forms.models import FieldKind, FieldValue, RequestTemplate
from request_forms.services import correct_values
from request_forms.tests.conftest import definition_of
from request_forms.tests.test_values import staff_raise
from taxonomy.models import RequestCategory

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

pytestmark = pytest.mark.django_db


# Audiences, upgrades and the demonstration


def matrix_user(name, *roles, **values):
    user = User.objects.create_user(name, f"{name}@example.invalid", **values)
    for role in roles:
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


@pytest.mark.parametrize(
    "audience",
    [
        "anonymous",
        *ROLES,
        "roleless",
        "roleless-direct",
        "console-only",
        "owner-only",
        "owner-with-role",
        "portal",
        "deactivated",
        "direct-reviewer",
        "ended-reviewer",
        "group-reviewer",
    ],
)
def test_documented_addresses_for_each_account_kind(client, audience):
    organization = Organization.objects.create(name="Synthetic matrix laboratory client")
    portal = matrix_user("portal-reader", client=organization)
    permission = Permission.objects.get(
        content_type__app_label="accounts", codename=REVIEW_CAPABILITY
    )
    signer = matrix_user("matrix-signer", "supervisor")
    signer.user_permissions.add(permission)
    review = sign(signer, findings="Synthetic matrix decisions")
    owner = audience in {"owner-only", "owner-with-role"}
    role = {
        "owner-with-role": "admin",
        "direct-reviewer": "technical",
        "ended-reviewer": "technical",
        "group-reviewer": "technical",
        "deactivated": "admin",
    }.get(audience, audience if audience in ROLES else None)
    if audience != "anonymous":
        actor = matrix_user(
            "matrix-actor",
            *([role] if role else []),
            client=organization if audience == "portal" else None,
            is_staff=owner or audience in {"admin", "console-only"},
            is_superuser=owner,
        )
        if audience in {
            "roleless-direct",
            "direct-reviewer",
            "ended-reviewer",
            "deactivated",
            "group-reviewer",
        }:
            actor.user_permissions.add(permission)
            ended = audience in {"group-reviewer", "ended-reviewer"}
            CapabilityEnd.objects.create(
                user=actor,
                permission=permission,
                ends_at=grant_ends.end_of(grant_ends.today() + timedelta(days=-2 if ended else 2)),
            )
        if audience == "group-reviewer":
            committee = Group.objects.create(name="Synthetic committee")
            actor.groups.add(committee)
            committee.permissions.add(permission)
        client.force_login(actor)
        if audience == "deactivated":
            actor.is_active = False
            actor.save(update_fields=["is_active"])

    login_required = audience in {"anonymous", "deactivated"}
    denied = 302 if login_required else 403
    manager = role in {"admin", "client_services", "account_manager"} and not login_required
    reviewer_allowed = audience in {"group-reviewer", "direct-reviewer"}
    staff_reader = role is not None and not login_required

    # A non-console account is redirected through the admin's sign-in view before it is refused.
    change = client.get(reverse("admin:accounts_user_change", args=[signer.pk]), follow=True)
    if login_required:
        assert change.wsgi_request.resolver_match.url_name == "account_login"
    else:
        assert change.status_code == (200 if owner else 403)
        if owner:
            assert "last_day_review_audit_records" in change.context["adminform"].form.fields

    assert client.get(reverse("clients:client-detail", args=[organization.pk])).status_code == (
        200 if staff_reader else denied
    )
    access_url = reverse("clients:portal-access-edit", args=[organization.pk, portal.pk])
    assert client.get(access_url).status_code == (200 if manager else denied)
    other = Organization.objects.create(name="Other synthetic account")
    wrong_address = reverse("clients:portal-access-edit", args=[other.pk, portal.pk])
    assert client.get(wrong_address).status_code == (404 if manager else denied)
    response = client.post(access_url, {"scope": "organization", "last_day": grant_ends.today()})
    assert response.status_code == (302 if manager else denied)
    assert PortalAccess.objects.filter(user=portal).exists() == manager

    for name, args in [
        ("access_review:accounts", []),
        ("access_review:accounts-export", []),
        ("access_review:review-create", []),
        ("access_review:review", [review.pk]),
        ("audit_review:changes", []),
    ]:
        url = reverse(name, args=args)
        assert client.get(url).status_code == (200 if reviewer_allowed else denied), url


@pytest.mark.django_db(transaction=True)
def test_upgrade_and_reverse_keep_the_preexisting_grants_and_site_assignment():
    previous = [
        ("accounts", "0010_review_capability"),
        ("clients", "0007_account_move_empties_portal_access"),
    ]
    candidate = [("accounts", "0011_grant_ends"), ("clients", "0008_grant_ends")]
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(previous)
        old = executor.loader.project_state(previous).apps
        old_users = old.get_model("accounts", "User").objects
        old_access = old.get_model("clients", "PortalAccess").objects
        organization = old.get_model("clients", "Client").objects.create(name="Pre-upgrade client")
        site = old.get_model("clients", "ClientSite").objects.create(
            client=organization, name="Pre-upgrade site", code="PRE"
        )
        holder = old_users.create(username="before-upgrade", email="pre@example.invalid")
        portal = old_users.create(
            username="before-upgrade-portal",
            email="pre-portal@example.invalid",
            client=organization,
        )
        permission = old.get_model("auth", "Permission").objects.get(
            content_type__app_label="accounts", codename="view_patient_information"
        )
        holder.user_permissions.add(permission)
        access = old_access.create(user=portal, client=organization, revision=7)
        access.sites.add(site)

        MigrationExecutor(connection).migrate(candidate)
        current_holder = User.objects.get(pk=holder.pk)
        current_portal = User.objects.get(pk=portal.pk)
        assert not CapabilityEnd.objects.filter(user=current_holder).exists()
        assert has_capability(current_holder, permission.codename)
        effective = current_access(current_portal)
        assert effective.pk == access.pk and effective.ends_at is None and effective.revision == 7
        assert list(effective.sites.values_list("pk", flat=True)) == [site.pk]

        # Rolling back populated columns must still preserve the older grant and site rows.
        with transaction.atomic():
            moment = grant_ends.end_of(grant_ends.today())
            CapabilityEnd.objects.create(
                user=current_holder, permission_id=permission.pk, ends_at=moment
            )
            effective.ends_at = moment
            effective.save(update_fields=["ends_at"])

        MigrationExecutor(connection).migrate(previous)
        assert "accounts_capabilityend" not in connection.introspection.table_names()
        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(cursor, "clients_portalaccess")
        assert "ends_at" not in {column.name for column in columns}
        assert list(holder.user_permissions.values_list("pk", flat=True)) == [permission.pk]
        access.refresh_from_db()
        assert (access.client_id, access.user_id, access.revision) == (
            organization.pk,
            portal.pk,
            7,
        )
        assert list(access.sites.values_list("pk", flat=True)) == [site.pk]
    finally:
        MigrationExecutor(connection).migrate(leaves)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
def test_seed_repeat_keeps_configured_ends_and_reset_restores_the_two_examples(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-grant-demo-password")
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    output = io.StringIO()

    def assert_fresh_examples():
        expected = grant_ends.end_of(grant_ends.today() + timedelta(days=90))
        end = CapabilityEnd.objects.select_related("user", "permission").get()
        assert (end.user.username, end.permission.codename, end.ends_at) == (
            "demo_supervisor",
            "view_patient_information",
            expected,
        )
        portal = PortalAccess.objects.get(ends_at__isnull=False)
        assert portal.user.username == "demo_client_water" and portal.ends_at == expected
        assert not portal.whole_organization and list(
            portal.sites.values_list("code", flat=True)
        ) == ["NORTH"]
        event = AuditEvent.objects.get(action="account.capability_end_set")
        assert (event.actor.username, event.object_id, event.object_type) == (
            "demo_admin",
            str(end.user_id),
            "accounts.user",
        )
        assert event.before == {"capability": end.permission.codename, "ends_at": None}
        assert event.after == {
            "capability": end.permission.codename,
            "ends_at": grant_ends.iso(expected),
        }
        portal_event = AuditEvent.objects.get(action="portal_access.set", object_id=str(portal.pk))
        assert portal_event.before["ends_at"] is None and portal_event.after[
            "ends_at"
        ] == grant_ends.iso(expected)
        return end, portal

    call_command("seed_demo", stdout=output, stderr=output)
    end, access = assert_fresh_examples()
    # An administrator removed both time limits after seeding. Rerunning the seed keeps that choice.
    end.delete()
    actor = User.objects.get(username="demo_account_manager")
    set_portal_access(
        actor=actor,
        user_id=access.user_id,
        expected_client_id=access.client_id,
        expected_revision=access.revision,
        sites=list(access.sites.all()),
        contact=access.contact,
        last_day=None,
        reason="Synthetic site access continues",
    )
    event_count = AuditEvent.objects.count()
    call_command("seed_demo", stdout=output, stderr=output)
    assert not CapabilityEnd.objects.exists()
    access.refresh_from_db()
    assert access.ends_at is None and AuditEvent.objects.count() == event_count

    call_command("reset_demo", interactive=False, stdout=output, stderr=output)
    assert_fresh_examples()


# Account administration, lifecycle and clock


ACCT_PATIENT = "view_patient_information"
ACCT_REVIEW = "review_audit_records"


def acct_user(name, **kwargs):
    return User.objects.create_user(name, f"{name}@example.invalid", **kwargs)


def acct_http(name):
    http = HttpClient()
    http.force_login(acct_user(name, is_superuser=True, is_staff=True))
    return http


def acct_perm(code):
    return Permission.objects.get(content_type__app_label="accounts", codename=code)


def acct_form(http, user):
    url = reverse("admin:accounts_user_change", args=[user.pk])
    response = http.get(url)
    assert response.status_code == 200
    return url, response.context["adminform"].form


def acct_end(user, code, moment):
    permission = acct_perm(code)
    user.user_permissions.add(permission)
    with transaction.atomic():
        return CapabilityEnd.objects.create(user=user, permission=permission, ends_at=moment)


def acct_events(user):
    return AuditEvent.objects.filter(action="account.updated", object_id=str(user.pk))


def test_account_edit_keeps_the_moment_when_zone_changes_while_form_is_open():
    user = acct_user("zone-edit")
    http = acct_http("zone-owner")
    day = grant_ends.today() + timedelta(days=60)
    with override_settings(TIME_ZONE="Etc/UTC"):
        original = grant_ends.end_of(day)
        stored = acct_end(user, ACCT_PATIENT, original)
        url, form = acct_form(http, user)
        assert form.initial[end_field(ACCT_PATIENT)] == day
        unrelated_edit = page_data(form, first_name="Jordan")
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        assert grant_ends.last_day_of(original) == day + timedelta(days=1)
        result = http.post(url, unrelated_edit)
    assert result.status_code in (200, 302)
    stored.refresh_from_db()
    assert stored.ends_at == original
    user.refresh_from_db()
    if result.status_code == 302:
        assert user.first_name == "Jordan"
    else:
        assert result.context["adminform"].form.errors and not user.first_name


def test_account_last_day_at_the_date_fields_upper_bound_gets_a_form_response():
    user = acct_user("date-limit")
    http = acct_http("date-owner")
    url, form = acct_form(http, user)
    field = end_field(ACCT_PATIENT)
    http.raise_request_exception = False
    result = http.post(
        url, page_data(form, user_permissions=[acct_perm(ACCT_PATIENT).pk], **{field: date.max})
    )
    assert result.status_code in (200, 302)
    if result.status_code == 302:
        _, reopened = acct_form(http, user)
        assert reopened[field].value() == date.max
        assert has_capability(user, ACCT_PATIENT)
    else:
        assert field in result.context["adminform"].form.errors
        assert not user.user_permissions.exists()
        assert not acct_events(user).exists()


def test_account_sets_both_last_days_and_preserves_the_other_when_swapping_grants():
    user = acct_user("two-ends")
    http = acct_http("two-ends-owner")
    day = grant_ends.today()
    patient = acct_perm(ACCT_PATIENT)
    review = acct_perm(ACCT_REVIEW)
    next_week = day + timedelta(days=7)
    url, form = acct_form(http, user)
    assert (
        http.post(
            url,
            page_data(
                form,
                user_permissions=[patient.pk, review.pk],
                **{end_field(ACCT_PATIENT): day, end_field(ACCT_REVIEW): next_week},
            ),
        ).status_code
        == 302
    )
    first = acct_events(user).get()
    expected = {
        f"accounts.{ACCT_PATIENT}": grant_ends.iso(grant_ends.end_of(day)),
        f"accounts.{ACCT_REVIEW}": grant_ends.iso(grant_ends.end_of(next_week)),
    }
    assert first.before["capability_ends"] == {}
    assert first.after["capability_ends"] == expected
    _, form = acct_form(http, user)
    assert form.initial[end_field(ACCT_PATIENT)] == day
    assert form.initial[end_field(ACCT_REVIEW)] == next_week
    assert (
        http.post(
            url, page_data(form, user_permissions=[review.pk], **{end_field(ACCT_PATIENT): ""})
        ).status_code
        == 302
    )
    kept = {f"accounts.{ACCT_REVIEW}": expected[f"accounts.{ACCT_REVIEW}"]}
    removed = acct_events(user).exclude(pk=first.pk).get()
    assert removed.before["capability_ends"] == expected
    assert removed.after["capability_ends"] == kept
    _, form = acct_form(http, user)
    assert (
        http.post(
            url, page_data(form, user_permissions=[patient.pk], **{end_field(ACCT_REVIEW): ""})
        ).status_code
        == 302
    )
    assert list(user.user_permissions.values_list("pk", flat=True)) == [patient.pk]
    assert not CapabilityEnd.objects.filter(user=user).exists()
    replaced = acct_events(user).exclude(pk__in=[first.pk, removed.pk]).get()
    assert replaced.before["capability_ends"] == kept
    assert replaced.after["capability_ends"] == {}


@pytest.mark.parametrize(
    "operation", ["set-end", "move-end", "clear-end", "add-grant", "remove-grant"]
)
def test_prior_account_form_detects_independent_grant_edits_by_another_superuser(operation):
    user = acct_user("two-editors")
    pat = acct_perm(ACCT_PATIENT)
    day = grant_ends.today() + timedelta(days=7)
    if operation in {"move-end", "clear-end"}:
        acct_end(user, ACCT_PATIENT, grant_ends.end_of(day))
    elif operation != "add-grant":
        user.user_permissions.add(pat)
    a = acct_http("first-editor")
    b = acct_http("second-editor")
    url, older = acct_form(a, user)
    _, newer = acct_form(b, user)
    changes = {
        "set-end": {end_field(ACCT_PATIENT): day},
        "move-end": {end_field(ACCT_PATIENT): day + timedelta(days=1)},
        "clear-end": {end_field(ACCT_PATIENT): ""},
        "add-grant": {"user_permissions": [pat.pk]},
        "remove-grant": {"user_permissions": []},
    }[operation]
    assert b.post(url, page_data(newer, **changes)).status_code == 302
    refused = a.post(url, page_data(older, first_name="Prior"))
    assert refused.status_code == 200
    assert CHANGED_SINCE_DRAWN in refused.context["adminform"].form.non_field_errors()
    user.refresh_from_db()
    assert user.first_name == ""
    assert acct_events(user).count() == 1


def test_account_form_rejects_a_malformed_date_and_changed_fingerprint():
    user = acct_user("bad-form")
    http = acct_http("bad-form-owner")
    url, form = acct_form(http, user)
    field = end_field(ACCT_REVIEW)
    refused = http.post(url, page_data(form, **{field: "2030-02-30"}))
    assert field in refused.context["adminform"].form.errors
    refused = http.post(url, page_data(form, drawn="invalid", first_name="Edited"))
    assert CHANGED_SINCE_DRAWN in refused.context["adminform"].form.non_field_errors()
    user.refresh_from_db()
    assert user.first_name == ""
    assert not acct_events(user).exists()


def test_account_creation_and_jobs_page_have_no_editable_end_and_portal_page_refuses_one():
    http = acct_http("account-types-owner")
    add = http.get(reverse("admin:accounts_user_add"))
    assert add.status_code == 200
    reserved = system_account()
    jobs_url = reverse("admin:accounts_user_change", args=[reserved.pk])
    read_only = http.get(jobs_url)
    assert read_only.status_code == 200
    assert not read_only.context["has_change_permission"]
    for http_result in (add, read_only):
        for code in (ACCT_PATIENT, ACCT_REVIEW):
            assert f'name="{end_field(code)}"' not in http_result.content.decode()
    assert http.post(jobs_url, {end_field(ACCT_PATIENT): grant_ends.today()}).status_code == 403
    org = Organization.objects.create(name="Example test organization")
    user = acct_user("portal-end-attempt", client=org)
    url, form = acct_form(http, user)
    result = http.post(url, page_data(form, **{end_field(ACCT_PATIENT): grant_ends.today()}))
    assert result.status_code == 200
    assert NOT_DIRECT in result.context["adminform"].form.errors[end_field(ACCT_PATIENT)]
    assert not CapabilityEnd.objects.filter(user__in=[reserved, user]).exists()
    assert not acct_events(user).exists()


@pytest.mark.parametrize("reverse_relation", [False, True], ids=["user", "permission"])
@pytest.mark.parametrize("operation", ["remove", "clear", "set", "reset-and-add"])
def test_relation_changes_remove_only_the_affected_grant_ends(reverse_relation, operation):
    user = acct_user("lifecycle")
    other = acct_user("lifecycle-other")
    patient = acct_perm(ACCT_PATIENT)
    review = acct_perm(ACCT_REVIEW)
    end = grant_ends.end_of(grant_ends.today())
    for who in (user, other):
        for code in (ACCT_PATIENT, ACCT_REVIEW):
            acct_end(who, code, end)
    relation = patient.user_set if reverse_relation else user.user_permissions
    target = user if reverse_relation else patient
    retained = other if reverse_relation else review
    remaining = {(user.pk, review.pk), (other.pk, patient.pk), (other.pk, review.pk)}
    if operation == "clear":
        relation.clear()
        remaining.remove((other.pk, patient.pk) if reverse_relation else (user.pk, review.pk))
    elif operation == "set":
        relation.set([retained])
    elif operation == "reset-and-add":
        relation.set([target], clear=True)
        remaining.remove((other.pk, patient.pk) if reverse_relation else (user.pk, review.pk))
    else:
        relation.remove(target)
    assert set(CapabilityEnd.objects.values_list("user_id", "permission_id")) == remaining
    relation.add(target)
    assert not CapabilityEnd.objects.filter(user=user, permission=patient).exists()
    assert has_capability(user, ACCT_PATIENT)


def test_deleting_an_unreferenced_account_removes_only_its_ends():
    first = acct_user("delete-first")
    other = acct_user("delete-other")
    moment = grant_ends.end_of(grant_ends.today())
    acct_end(first, ACCT_PATIENT, moment)
    retained = acct_end(other, ACCT_PATIENT, moment)
    first.delete()
    assert list(CapabilityEnd.objects.values_list("pk", flat=True)) == [retained.pk]


@pytest.mark.django_db(transaction=True)
def test_end_at_transaction_timestamp_is_over_and_an_earlier_transaction_keeps_its_reading():
    user = acct_user("clock-boundary")
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT transaction_timestamp()")
        start = cursor.fetchone()[0]
        acct_end(user, ACCT_PATIENT, start)
        acct_end(user, ACCT_REVIEW, start + timedelta(microseconds=1))
        cursor.execute("SELECT clock_timestamp()")
        assert cursor.fetchone()[0] > start + timedelta(microseconds=1)
        assert not has_capability(user, ACCT_PATIENT)
        assert has_capability(user, ACCT_REVIEW)
    assert not has_capability(user, ACCT_REVIEW)


@pytest.mark.parametrize(
    ("day", "utc_hour"),
    [
        (date(2027, 3, 13), 5),
        (date(2027, 3, 14), 4),
        (date(2027, 11, 6), 4),
        (date(2027, 11, 7), 5),
    ],
)
@override_settings(TIME_ZONE="America/New_York")
def test_dst_boundary_uses_the_offset_of_the_following_midnight(day, utc_hour):
    tomorrow = day + timedelta(days=1)
    utc_end = datetime(tomorrow.year, tomorrow.month, tomorrow.day, utc_hour, tzinfo=UTC)
    assert grant_ends.end_of(day) == utc_end
    assert grant_ends.last_day_of(utc_end) == day


@pytest.mark.django_db(transaction=True)
def test_setting_a_capability_end_waits_for_the_holders_transaction():
    user = acct_user("gated-holder")
    permission = acct_perm(ACCT_PATIENT)
    user.user_permissions.add(permission)
    moment = grant_ends.end_of(grant_ends.today())
    writer_pid = Queue()

    def write_end():
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                writer_pid.put(cursor.fetchone()[0])
                CapabilityEnd.objects.create(user=user, permission=permission, ends_at=moment)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as worker:
        with transaction.atomic(), connection.cursor() as cursor:
            hold_actor(user)
            cursor.execute("SELECT pg_backend_pid()")
            reader_pid = cursor.fetchone()[0]
            written = worker.submit(write_end)
            pid = writer_pid.get(timeout=10)
            deadline = monotonic() + 5
            while monotonic() < deadline:
                cursor.execute("SELECT pg_blocking_pids(%s)", [pid])
                if reader_pid in cursor.fetchone()[0]:
                    break
                if written.done():
                    written.result()
                    pytest.fail("The writer committed while the holder's change was in progress")
                sleep(0.01)
            else:
                pytest.fail("PostgreSQL did not report the holder blocking the writer")
            assert not CapabilityEnd.objects.filter(user=user).exists()
        written.result(timeout=10)
    assert CapabilityEnd.objects.get(user=user).ends_at == moment


@pytest.mark.django_db(transaction=True)
def test_simultaneous_account_end_edits_commit_exactly_one_date_and_one_audit_event():
    user = acct_user("racing-edits")
    user.user_permissions.add(acct_perm(ACCT_PATIENT))
    field = end_field(ACCT_PATIENT)
    first_day = grant_ends.today() + timedelta(days=7)
    forms = []
    for index in range(2):
        http = acct_http(f"race-owner-{index}")
        url, form = acct_form(http, user)
        day = first_day + timedelta(days=index)
        forms.append((http, url, page_data(form, **{field: day}), day))

    def post(item):
        http, url, data, day = item
        try:
            response = http.post(url, data)
            if response.status_code == 200:
                assert CHANGED_SINCE_DRAWN in response.context["adminform"].form.non_field_errors()
            return response.status_code, day
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        # Hold the gate until PostgreSQL shows both POSTs waiting for this account. The
        # requests then compete to compare and write the two dates from the same prior form.
        with transaction.atomic(), connection.cursor() as cursor:
            hold_actor(user)
            pending = [pool.submit(post, item) for item in forms]
            deadline = monotonic() + 10
            while monotonic() < deadline:
                cursor.execute(
                    """
                    SELECT count(*) FROM pg_locks
                    WHERE locktype = 'advisory' AND NOT granted AND classid = %s AND objid = %s
                    AND objsubid = 2 AND database = (
                        SELECT oid FROM pg_database WHERE datname = current_database()
                    )
                    """,
                    [ACCESS_GATE, user.pk],
                )
                if cursor.fetchone()[0] == 2:
                    break
                for result in pending:
                    if result.done():
                        result.result()
                        pytest.fail("A POST finished before both account saves reached the gate")
                sleep(0.01)
            else:
                pytest.fail("Both concurrent account saves did not reach the gate")
        results = [result.result(timeout=15) for result in pending]
    assert sorted(status for status, day in results) == [200, 302]
    winner = next(day for status, day in results if status == 302)
    stored = CapabilityEnd.objects.get(user=user)
    assert stored.ends_at == grant_ends.end_of(winner)
    event = acct_events(user).get()
    assert event.before["capability_ends"] == {}
    assert event.after["capability_ends"] == {
        f"accounts.{ACCT_PATIENT}": grant_ends.iso(stored.ends_at)
    }


# Staff capability checks


STAFF_PATIENT = "view_patient_information"
STAFF_MARK = "REVIEW-TEST-482671"
STAFF_LINK = "PT-TEST-55390217"


def staff_person(name, role="supervisor"):
    person = User.objects.create_user(name, f"{name}@example.invalid", STAFF_PASSWORD)
    person.groups.add(Group.objects.get_or_create(name=role)[0])
    return person


def staff_grant(person, codename, days):
    permission = Permission.objects.get(content_type__app_label="accounts", codename=codename)
    person.user_permissions.add(permission)
    return CapabilityEnd.objects.create(
        user=person, permission=permission, ends_at=timezone.now() + timedelta(days=days)
    )


@pytest.fixture
def staff_template(db):
    group = RequestCategory.objects.create(key="review-group", name="Sample group")
    category = RequestCategory.objects.create(
        key="review-leaf", name="Sample category", parent=group
    )
    template = RequestTemplate.objects.create(
        key="review-form", name="Sample form", category=category
    )
    template.fields.create(
        key="patient",
        label="Synthetic patient reference",
        kind=FieldKind.SHORT_TEXT,
        holds_patient_information=True,
    )
    return template


@pytest.mark.parametrize("effective", [True, False], ids=["ending", "ended"])
def test_patient_capability_controls_intake_correction_and_reveal(
    client, staff_template, effective, caplog
):
    recorder = staff_person("review-recorder")
    staff_grant(recorder, STAFF_PATIENT, 1)
    ticket = staff_raise(recorder, staff_template, {"patient": STAFF_MARK})
    value = FieldValue.objects.get(filled_form__request=ticket, key="patient")
    reader = staff_person("review-reader")
    staff_grant(reader, STAFF_PATIENT, 1 if effective else -1)
    client.force_login(reader)
    intake = client.get(reverse("desk:category-guidance"), {"category": staff_template.category_id})
    correction = client.get(reverse("request_details:edit", args=[ticket.number]))
    detail = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    reveal = reverse("request_details:reveal", args=[value.pk])
    for response in (intake, correction, detail):
        assert response.status_code == 200
        assert STAFF_MARK not in response.content.decode()
    assert ('name="field-patient"' in intake.content.decode()) is effective
    assert ('name="field-patient"' in correction.content.decode()) is effective
    assert (reveal in detail.content.decode()) is effective
    shown = client.post(reveal, HTTP_HX_REQUEST="true")
    assert shown.status_code == (200 if effective else 404)
    assert (STAFF_MARK in shown.content.decode()) is effective
    assert "no-store" in shown["Cache-Control"]
    events = AuditEvent.objects.filter(actor=reader, action="request_form.value_revealed")
    assert events.count() == int(effective)

    with nullcontext() if effective else pytest.raises(PermissionDenied):
        staff_raise(reader, staff_template, {"patient": STAFF_MARK})
    with nullcontext() if effective else pytest.raises(PermissionDenied):
        correct_values(
            actor=reader,
            request=ticket,
            expected_definition=definition_of(ticket),
            values={"patient": STAFF_MARK + "-2"},
            expected={"patient": value.pk},
            reason="",
            reason_code="typing",
        )
    saved = FieldValue.objects.get(filled_form__request=ticket, key="patient", superseded_at=None)
    assert saved.value == (STAFF_MARK + "-2" if effective else STAFF_MARK)
    assert STAFF_MARK not in str(list(AuditEvent.objects.values("before", "after", "reason")))
    assert STAFF_MARK not in caplog.text


@pytest.mark.parametrize("effective", [True, False], ids=["ending", "ended"])
def test_person_link_pages_and_services_observe_the_last_day(
    client, staff_template, effective, caplog
):
    recorder = staff_person("review-link-recorder")
    staff_grant(recorder, STAFF_PATIENT, 1)
    ticket = staff_raise(recorder, staff_template, {})
    other_ticket = staff_raise(recorder, staff_template, {})
    system = ExternalSystem.objects.create(key="review-lis", name="Synthetic LIS", kind="lis")
    attachment, _ = staff_link_services.add_link(
        actor=recorder,
        request_id=ticket.pk,
        system=system,
        record_type="patient",
        identifier=STAFF_LINK,
    )
    reader = staff_person("review-link-reader")
    staff_grant(reader, STAFF_PATIENT, 1 if effective else -1)
    client.force_login(reader)
    add = client.get(reverse("link_records:request-link-add", args=[other_ticket.pk]))
    assert add.status_code == 200
    assert ('value="patient"' in add.content.decode()) is effective
    search_url = reverse("link_records:person-search")
    search = client.get(search_url)
    assert search.status_code == 200
    assert ('name="key"' in search.content.decode()) is effective
    results = client.post(search_url, {"key": STAFF_LINK})
    assert results.status_code == (200 if effective else 403)
    assert (ticket.number in results.content.decode()) is effective
    assert STAFF_LINK not in results.content.decode()

    for route, pk in (
        ("link-detail", attachment.link_id),
        ("link-edit", attachment.link_id),
        ("attachment-remove", attachment.pk),
    ):
        response = client.get(reverse(f"link_records:{route}", args=[pk]))
        assert response.status_code == (200 if effective else 404), route
        assert STAFF_LINK not in response.content.decode(), route
    detail = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    assert detail.status_code == 200
    assert (attachment.link.display_label in detail.content.decode()) is effective
    assert STAFF_LINK not in detail.content.decode()
    shown = client.post(reverse("link_records:attachment-reveal", args=[attachment.pk]))
    assert shown.status_code == (200 if effective else 404)
    assert (STAFF_LINK in shown.content.decode()) is effective
    assert "no-store" in shown["Cache-Control"]

    with nullcontext() if effective else pytest.raises(PermissionDenied):
        staff_link_services.add_link(
            actor=reader,
            request_id=other_ticket.pk,
            system=system,
            record_type="patient",
            identifier=STAFF_LINK,
        )
    with nullcontext() if effective else pytest.raises(ExternalRecordLink.DoesNotExist):
        staff_link_services.update_link(
            actor=reader,
            link_id=attachment.link_id,
            expected_revision=attachment.link.revision,
            display_label="Patient alias Finch",
            reason_code="entered_in_error",
        )
    with nullcontext() if effective else pytest.raises(PermissionDenied):
        staff_link_services.remove_link(
            actor=reader, attachment_id=attachment.pk, reason_code="other"
        )
    attachment.refresh_from_db()
    assert (attachment.removed_at is not None) is effective
    assert AuditEvent.objects.filter(
        actor=reader, action="external_link.person_revealed"
    ).count() == int(effective)
    assert STAFF_LINK not in str(list(AuditEvent.objects.values("before", "after", "reason")))
    assert STAFF_LINK not in caplog.text


@pytest.mark.parametrize(
    "direct_days,through_group,effective",
    [
        (1, False, True),
        (-1, False, False),
        (-1, True, True),
    ],
)
def test_staff_sidebar_admin_list_and_security_counts_agree(
    client, direct_days, through_group, effective
):
    reader = staff_person("review-matrix-reader", "technical")
    review = staff_grant(reader, REVIEW_CAPABILITY, direct_days)
    patient = staff_grant(reader, STAFF_PATIENT, direct_days)
    if through_group:
        grant_group = Group.objects.create(name="review-explicit-grants")
        grant_group.permissions.add(review.permission, patient.permission)
        reader.groups.add(grant_group)

    expected = {REVIEW_CAPABILITY, STAFF_PATIENT} if effective else set()
    for code in (STAFF_PATIENT, REVIEW_CAPABILITY):
        assert has_capability(reader, code) is effective
        with nullcontext() if effective else pytest.raises(PermissionDenied):
            require_capability(reader, code)
    assert roles_and_capabilities(reader)[1] == expected
    assert (patient_capability_holders(), review_capability_holders()) == (
        int(effective),
        int(effective),
    )
    client.force_login(reader)
    dashboard = client.get(reverse("dashboard"))
    assert dashboard.status_code == 200
    assert dashboard.context["user_capabilities"] == expected
    for route in ("audit_review:changes", "access_review:accounts"):
        assert (f'href="{reverse(route)}"' in dashboard.content.decode()) is effective
    owner = User.objects.create_superuser(
        "review-console", "review-console@example.invalid", STAFF_PASSWORD
    )
    client.force_login(owner)
    result = client.get(reverse("admin:accounts_user_changelist"))
    assert result.status_code == 200
    listing = result.context["cl"]
    rendered = listing.model_admin.capabilities(
        next(u for u in listing.result_list if u.pk == reader.pk)
    )
    # The two capabilities this reader was given (emergency access, added since, it was not).
    for code in (STAFF_PATIENT, REVIEW_CAPABILITY):
        assert (CAPABILITIES[code] in rendered) is effective


def test_enrolment_session_rechecks_ended_grant_and_records_new_sign_in(client):
    cache.clear()
    reader = staff_person("review-enrolment", "technical")
    end = staff_grant(reader, STAFF_PATIENT, 1)
    SecondFactorRule.objects.update_or_create(pk=1, defaults={"patient_information": True})
    assert second_factor.requires(reader)
    assert second_factor.counts() == (1, 1)
    sign_in(client, reader)
    assert session_row(client).second_factor_required
    assert client.get("/").url == reverse("account-second-factor")
    end.ends_at = timezone.now() - timedelta(days=1)
    end.save(update_fields=["ends_at"])
    assert not second_factor.requires(reader)
    assert second_factor.counts() == (0, 0)
    assert client.get("/").status_code == 200
    client.logout()
    sign_in(client, reader)
    assert not session_row(client).second_factor_required
    assert client.get("/").status_code == 200


# Portal access


@pytest.fixture
def portal_parties(client_org, staff, portal_user):
    return client_org, staff("grant_manager", "client_services"), portal_user(client_org)


def portal_set(parties, last_day, **scope):
    organization, manager, account = parties
    stored = PortalAccess.objects.filter(user=account).first()
    return set_portal_access(
        actor=manager,
        user_id=account.pk,
        expected_client_id=organization.pk,
        expected_revision=stored.revision if stored else None,
        reason="Directory update" if stored else "",
        last_day=last_day,
        **{"whole_organization": True, **scope},
    )


def portal_edit_url(parties):
    organization, _, account = parties
    return reverse("clients:portal-access-edit", args=[organization.pk, account.pk])


@pytest.mark.parametrize("scope", ["organization", "sites"])
def test_portal_end_only_change_checks_revision_and_records_previous_and_new_moments(
    portal_parties, client, scope
):
    organization, manager, account = portal_parties
    site = organization.sites.create(code="EAST", name="East office")
    day = grant_ends.today() + timedelta(days=15)
    access = portal_set(
        portal_parties, day, whole_organization=scope == "organization", sites=[site]
    )
    url = portal_edit_url(portal_parties)
    second = HttpClient()
    for session in (client, second):
        session.force_login(manager)
    first_form = client.get(url).context["form"]
    second_form = second.get(url).context["form"]
    assert first_form["last_day"].value() == second_form["last_day"].value() == day
    assert first_form["scope"].value() == scope
    assert set(first_form["sites"].value()) == ({site.pk} if scope == "sites" else set())
    chosen = day + timedelta(days=1)
    data = {
        "scope": scope,
        "sites": first_form["sites"].value(),
        "last_day": chosen.isoformat(),
        "expected_revision": first_form["expected_revision"].value(),
        "reason": "Revised date only",
    }
    assert client.post(url, data).status_code == 302
    event = AuditEvent.objects.get(action="portal_access.set", reason=data["reason"])
    facts = {
        "client": organization.pk,
        "contact": None,
        "whole_organization": scope == "organization",
        "sites": [site.pk] if scope == "sites" else [],
    }
    assert event.actor_id == manager.pk
    assert event.before == {**facts, "ends_at": grant_ends.iso(grant_ends.end_of(day))}
    assert event.after == {
        **facts,
        "ends_at": grant_ends.iso(grant_ends.end_of(chosen)),
        "user": account.pk,
    }
    old_revision = second_form["expected_revision"].value()
    refused = second.post(url, {**data, "last_day": "", "expected_revision": old_revision})
    assert refused.status_code == 200
    assert "Reload and review" in str(refused.context["form"].non_field_errors())
    assert refused.context["form"]["last_day"].value() == ""
    access.refresh_from_db()
    assert access.revision == old_revision + 1 and access.ends_at == grant_ends.end_of(chosen)
    assert (
        second.post(
            url,
            {
                **data,
                "expected_revision": access.revision,
                "last_day": "",
                "reason": "Clear the last day",
            },
        ).status_code
        == 302
    )
    cleared = AuditEvent.objects.get(
        action="portal_access.set", object_id=str(access.pk), reason="Clear the last day"
    )
    assert cleared.before["ends_at"] == event.after["ends_at"] and cleared.after["ends_at"] is None
    assert current_access(account).ends_at is None


def test_portal_move_resets_moment_audits_it_and_refuses_open_form(portal_parties, client):
    organization, manager, account = portal_parties
    site = organization.sites.create(code="OLD", name="Old office")
    contact = organization.contacts.create(name="Old office contact", phone="555")
    day = grant_ends.today() + timedelta(days=5)
    access = portal_set(
        portal_parties, day, contact=contact, whole_organization=False, sites=[site]
    )
    destination = Organization.objects.create(name="New office", account_number="NEW")
    client.force_login(manager)
    url = portal_edit_url(portal_parties)
    form = client.get(url).context["form"]
    account.client = destination
    account.changed_by = manager
    account.save(update_fields=["client"])
    refused = client.post(
        url,
        {
            "scope": "sites",
            "sites": [site.pk],
            "last_day": day.isoformat(),
            "expected_revision": form["expected_revision"].value(),
            "reason": "Existing form",
        },
    )
    assert refused.status_code == 404
    access.refresh_from_db()
    assert access.client_id == destination.pk and access.contact_id is None
    assert not access.whole_organization and not access.sites.exists() and access.ends_at is None
    event = AuditEvent.objects.get(action="portal_access.reset", object_id=str(access.pk))
    assert event.actor_id == manager.pk
    assert event.before == {
        "client": organization.pk,
        "contact": contact.pk,
        "sites": [site.pk],
        "whole_organization": False,
        "ends_at": grant_ends.iso(grant_ends.end_of(day)),
    }
    assert event.after == {
        "client": destination.pk,
        "contact": None,
        "sites": [],
        "whole_organization": False,
        "ends_at": None,
        "user": account.pk,
    }


def test_portal_move_trigger_clears_the_last_day_with_the_scope(portal_parties):
    organization, _, account = portal_parties
    site = organization.sites.create(code="MOVE", name="Old site")
    access = portal_set(portal_parties, grant_ends.today(), whole_organization=False, sites=[site])
    destination = Organization.objects.create(name="Next clinic", account_number="NEXT")
    User.objects.filter(pk=account.pk).update(client=destination)
    account.refresh_from_db()
    access.refresh_from_db()
    assert access.client_id == destination.pk
    assert not access.whole_organization and not access.sites.exists()
    assert current_access(account) is None
    assert access.ends_at is None


def test_portal_edit_after_a_zone_change_preserves_the_displayed_day_left_untouched(
    portal_parties, client
):
    organization, manager, _ = portal_parties
    contact = organization.contacts.create(name="Second contact", phone="555")
    url = portal_edit_url(portal_parties)
    with override_settings(TIME_ZONE="America/New_York"):
        day = grant_ends.today() + timedelta(days=15)
        access = portal_set(portal_parties, day)
        saved_end = access.ends_at
        client.force_login(manager)
        form = client.get(url).context["form"]
        assert form["last_day"].value() == day
    with override_settings(TIME_ZONE="Europe/Paris"):
        response = client.post(
            url,
            {
                "scope": form["scope"].value(),
                "last_day": day.isoformat(),
                "contact": contact.pk,
                "shown_last_day": form["shown_last_day"].value(),  # posted by the page
                "expected_revision": form["expected_revision"].value(),
                "reason": "Link the contact",
            },
        )
        assert response.status_code in (200, 302)
    access.refresh_from_db()
    assert access.ends_at == saved_end
    if response.status_code == 302:
        assert access.contact == contact
    else:
        assert response.context["form"].errors and access.contact is None


def test_portal_last_day_at_the_date_fields_upper_bound_gets_a_form_response(
    portal_parties, client
):
    _, manager, _ = portal_parties
    url = portal_edit_url(portal_parties)
    client.force_login(manager)
    form = client.get(url).context["form"]
    client.raise_request_exception = False
    response = client.post(
        url,
        {
            "scope": "organization",
            "expected_revision": form["expected_revision"].value(),
            "last_day": date.max.isoformat(),
        },
    )
    assert response.status_code in (200, 302)
    if response.status_code == 200:
        assert response.context["form"].has_error("last_day")
    else:
        assert client.get(url).context["form"]["last_day"].value() == date.max


@pytest.mark.parametrize("scope", ["organization", "sites"])
def test_portal_end_controls_tickets_replies_intake_contacts_and_notice_delivery(
    portal_parties, client, scope
):
    organization, manager, account = portal_parties
    site = organization.sites.create(code="LISTED", name="Listed office")
    other = organization.sites.create(code="OTHER", name="Other office")
    linked = organization.contacts.create(name="Listed contact", phone="555")
    linked.sites.add(site)
    elsewhere = organization.contacts.create(name="Other contact", phone="555")
    elsewhere.sites.add(other)
    all_sites = organization.contacts.create(name="Main contact", phone="555")
    group = RequestCategory.objects.create(key="review-group", name="Review group")
    category = RequestCategory.objects.create(
        key="review-topic", name="Review topic", parent=group, client_may_raise=True
    )
    access = portal_set(
        portal_parties,
        grant_ends.today() + timedelta(days=10),
        contact=linked,
        whole_organization=scope == "organization",
        sites=[site],
    )
    own = create_request(
        actor=account,
        channel="portal",
        subject="Own synthetic query",
        first_entry_kind="reply",
        first_entry_body="Synthetic text",
        category=category,
        site=site,
    )
    shared = raise_staff(
        manager, category, organization, site, "Scoped synthetic query", contact=linked
    )
    assert {account} == set(client_recipients(shared))
    waiting = Notice.objects.get(recipient=account, object_id=shared.pk, topic="desk.request")
    assert prepare_request_notice(waiting) is not None
    assert set(portal_contacts(account)) == (
        {all_sites, linked, elsewhere} if scope == "organization" else {all_sites, linked}
    )
    assert set(portal_sites(account)) == ({site, other} if scope == "organization" else {site})
    assert site_required(account)
    client.force_login(account)
    intake = reverse("portal:request-create")
    fields = client.get(intake).context["form"].fields
    assert fields["site"].required
    with pytest.raises(ValidationError, match="Choose the site"):
        create_request(
            actor=account,
            category=category,
            channel="portal",
            subject="Missing site",
            first_entry_kind="reply",
            first_entry_body="Synthetic intake",
        )
    listing = reverse("portal:request-list")
    detail = reverse("portal:request-detail", args=[shared.number])
    reply = reverse("portal:request-reply", args=[shared.number])
    assert {own, shared} == set(portal_requests(account))
    assert shared.number in client.get(listing).content.decode()
    assert client.get(detail).status_code == 200
    assert (
        client.post(
            reply, {"body": "Synthetic before end", "expected_revision": shared.revision}
        ).status_code
        == 302
    )
    shared.refresh_from_db()
    # Position the fixture exactly at the transaction's start. Equality is the first denied instant.
    with connection.cursor() as cursor:
        cursor.execute("SELECT transaction_timestamp()")
        boundary = cursor.fetchone()[0]
    PortalAccess.objects.filter(pk=access.pk).update(ends_at=boundary)
    assert current_access(account) is None
    assert list(portal_contacts(account)) == [all_sites]
    assert not portal_sites(account).exists() and not site_required(account)
    assert "site" not in client.get(intake).context["form"].fields
    assert set(portal_requests(account)) == {own}
    response = client.get(listing)
    assert (
        own.number in response.content.decode() and shared.number not in response.content.decode()
    )
    assert client.get(detail).status_code == 404
    assert (
        client.post(
            reply, {"body": "Synthetic after end", "expected_revision": shared.revision}
        ).status_code
        == 404
    )
    assert (
        client.post(
            reverse("portal:request-reply", args=[own.number]),
            {"body": "Own after end", "expected_revision": own.revision},
        ).status_code
        == 302
    )
    assert set(client_recipients(own)) == {account} and not client_recipients(shared)
    assert prepare_request_notice(waiting) is None
    new_ticket = create_request(
        actor=account,
        category=category,
        channel="portal",
        subject="Organization-only query",
        first_entry_kind="reply",
        first_entry_body="Synthetic intake",
    )
    assert new_ticket.site_id is None and new_ticket.client_id == organization.pk


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "writer",
    [
        "service",
        pytest.param("model-save", marks=pytest.mark.xfail(strict=True, reason=MODEL_SAVE_LIMIT)),
    ],
)
def test_portal_setting_a_future_end_waits_for_the_accounts_open_change(portal_parties, writer):
    _, _, account = portal_parties
    access = portal_set(portal_parties, None)
    day = grant_ends.today() + timedelta(days=10)
    backend = Queue()

    def set_end():
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '10s'")
                cursor.execute("SELECT pg_backend_pid()")
                backend.put(cursor.fetchone()[0])
                if writer == "service":
                    portal_set(portal_parties, day)
                else:
                    row = PortalAccess.objects.get(pk=access.pk)
                    row.ends_at = grant_ends.end_of(day)
                    row.save(update_fields=["ends_at"])
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic(), connection.cursor() as cursor:
            assert hold_actor(account).pk == account.pk
            future = pool.submit(set_end)
            pid = backend.get(timeout=5)
            deadline = monotonic() + 5
            blocked = False
            while monotonic() < deadline and not future.done():
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s "
                    "AND locktype = 'advisory' AND NOT granted)",
                    [pid],
                )
                if cursor.fetchone()[0]:
                    blocked = True
                    break
                sleep(0.01)
        future.result(timeout=10)
    access.refresh_from_db()
    assert access.ends_at == grant_ends.end_of(day)
    assert blocked, "The writer committed without waiting for the account's access gate"


def test_portal_grant_for_a_former_organization_has_no_scope_or_initial_day(portal_parties, client):
    organization, manager, account = portal_parties
    former = Organization.objects.create(name="Prior clinic", account_number="FORMER")
    old_site = former.sites.create(name="Former branch", code="FORMER")
    old_contact = former.contacts.create(name="Former contact", phone="555")
    # A stored pre-existing record for a different organization, a state supported by the form.
    record = PortalAccess.objects.create(
        user=account,
        client=former,
        contact=old_contact,
        ends_at=grant_ends.end_of(grant_ends.today()),
    )
    record.sites.add(old_site)
    assert current_access(account) is None and not portal_sites(account).exists()
    client.force_login(manager)
    url = portal_edit_url(portal_parties)
    form = client.get(url).context["form"]
    assert form["last_day"].value() is None and form["contact"].value() is None
    assert form["sites"].value() == []
    assert form["expected_revision"].value() == record.revision
    chosen = grant_ends.today() + timedelta(days=2)
    assert (
        client.post(
            url,
            {
                "scope": "organization",
                "expected_revision": record.revision,
                "last_day": chosen.isoformat(),
                "reason": "Decide access for the current organization",
            },
        ).status_code
        == 302
    )
    record.refresh_from_db()
    assert record.client_id == organization.pk and record.contact_id is None
    assert not record.sites.exists() and record.ends_at == grant_ends.end_of(chosen)
    assert current_access(account).whole_organization


def test_portal_form_accepts_an_ended_day_only_if_kept_and_renews_with_a_new_day(
    portal_parties, client
):
    organization, manager, account = portal_parties
    site = organization.sites.create(name="Branch", code="BRANCH")
    access = portal_set(portal_parties, grant_ends.today())
    past = grant_ends.today() - timedelta(days=4)
    past_end = grant_ends.end_of(past)
    PortalAccess.objects.filter(pk=access.pk).update(ends_at=past_end)
    client.force_login(manager)
    url = portal_edit_url(portal_parties)
    drawn = client.get(url).context["form"]
    assert drawn["last_day"].value() == past
    data = {
        "scope": "sites",
        "sites": [site.pk],
        "expected_revision": drawn["expected_revision"].value(),
        "reason": "Change the served site",
    }
    refused_day = past + timedelta(days=1)
    refused = client.post(url, {**data, "last_day": refused_day.isoformat()})
    assert refused.status_code == 200
    assert "Choose today or a later day" in str(refused.context["form"].errors["last_day"])
    access.refresh_from_db()
    assert access.ends_at == past_end and access.whole_organization
    assert client.post(url, {**data, "last_day": past.isoformat()}).status_code == 302
    access.refresh_from_db()
    assert access.ends_at == past_end and set(access.sites.all()) == {site}
    assert current_access(account) is None
    new_day = grant_ends.today()
    fresh = client.get(url).context["form"]
    assert (
        client.post(
            url,
            {
                **data,
                "expected_revision": fresh["expected_revision"].value(),
                "last_day": new_day.isoformat(),
                "reason": "Give access again",
            },
        ).status_code
        == 302
    )
    assert set(portal_sites(account)) == {site}
    assert current_access(account).ends_at == grant_ends.end_of(new_day)


# Access reports and signed reviews


ar_category = ar_fixtures.category
ar_clinic = ar_fixtures.clinic
ar_make_user = ar_fixtures.make_user


@pytest.fixture
def ar_reviewer(ar_make_user):
    reviewer = ar_make_user("review_signer", "supervisor")
    reviewer.user_permissions.add(review_permission())
    return reviewer


def ar_end(account, moment):
    permission = patient_permission()
    account.user_permissions.add(permission)
    return CapabilityEnd.objects.create(user=account, permission=permission, ends_at=moment)


def ar_csv(response):
    assert response.status_code == 200 and "text/csv" in response["Content-Type"]
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))


def test_report_filter_csv_and_review_keep_group_and_direct_ends_distinct(
    client, ar_make_user, ar_reviewer, ar_clinic, ar_category, caplog
):
    today = grant_ends.today()
    last_day = today + timedelta(days=9)
    ended_day = today - timedelta(days=4)
    ending = ar_make_user("ending_individual", "technical")
    ended = ar_make_user("ended_individual", "technical")
    both = ar_make_user("ended_and_group", "technical")
    for account, chosen in [(ending, last_day), (ended, ended_day), (both, ended_day)]:
        ar_end(account, grant_ends.end_of(chosen))
    group = Group.objects.create(name="Specimen reviewers")
    group.permissions.add(patient_permission())
    both.groups.add(group)

    private_subject, private_note = "Synthetic private subject 813", "Synthetic private note 912"
    private_factor = "FAKEFACTOR731"
    ticket = create_request(
        actor=ar_reviewer,
        category=ar_category,
        subject=private_subject,
        first_entry_kind="note",
        first_entry_body=private_note,
        channel="phone",
        client=ar_clinic,
    )
    Authenticator.objects.create(
        user=ending, type=Authenticator.Type.TOTP, data={"secret": private_factor}
    )
    client.force_login(ar_reviewer)
    caplog.clear()
    caplog.set_level(logging.INFO)
    listing = client.get(reverse("access_review:accounts"))
    exported = client.get(reverse("access_review:accounts-export"))
    rows = {row["Username"]: row["Capabilities"] for row in ar_csv(exported)}
    assert f"Directly, last day {last_day}" in rows[ending.username]
    for account in (ended, both):
        assert "Can view patient information (ended)" in rows[account.username]
        assert f"ended after {ended_day}, gives nothing" in rows[account.username]
    assert group.name in rows[both.username]
    assert listing.status_code == 200 and f"last day {last_day}".encode() in listing.content

    filters = {"capability": "view_patient_information"}
    filtered = client.get(reverse("access_review:accounts"), filters)
    filtered_csv = client.get(reverse("access_review:accounts-export"), filters)
    assert {row.username for row in filtered.context["page_obj"]} == {
        ending.username,
        both.username,
    }
    assert {row["Username"] for row in ar_csv(filtered_csv)} == {ending.username, both.username}

    form_url = reverse("access_review:review-create")
    form_response = client.get(form_url)
    grants = form_response.context["reading"].grants
    assert [g.detail for g in grants if g.account_id == ending.pk and g.kind == "capability"] == [
        f"Given directly, last day {last_day}"
    ]
    assert not any(g.kind == "capability" and g.account_id in (both.pk, ended.pk) for g in grants)
    assert any(
        g.account_id == both.pk
        and g.kind == "group"
        and group.name in g.label
        and "patient information" in g.detail
        for g in grants
    )
    submitted = client.post(form_url, posted(form_response, decide=Decision.REMOVE))
    assert submitted.status_code == 302
    review = AccessReview.objects.get()
    detail = client.get(submitted["Location"])
    decisions = client.get(reverse("access_review:review-export", args=[review.pk]))
    assert any(
        row["Username"] == ending.username
        and row["Held now"] == "Yes"
        and row["Detail"] == f"Given directly, last day {last_day}"
        for row in ar_csv(decisions)
    )
    assert detail.status_code == 200 and f"last day {last_day}".encode() in detail.content
    assert any(
        record.name == "lab_ops.request" and "access-review/" in record.getMessage()
        for record in caplog.records
    )
    signed_event = AuditEvent.objects.get(action=access_reviews.SIGNED)
    artifacts = [
        r.content.decode()
        for r in (listing, exported, filtered, filtered_csv, form_response, detail, decisions)
    ]
    artifacts.extend(
        (
            str((signed_event.before, signed_event.after, signed_event.reason)),
            str(list(WorkspaceUse.objects.values("filters", "request_ref"))),
            caplog.text,
        )
    )
    for private in (
        ticket.number,
        private_subject,
        private_note,
        private_factor,
        AR_PASSWORD,
        ending.password,
        ar_reviewer.password,
        ending.email,
    ):
        assert all(private not in artifact for artifact in artifacts)


def ar_end_just_after_transaction_start():
    # A deterministic boundary separating this reading from every subsequent transaction.
    with connection.cursor() as cursor:
        cursor.execute("SELECT transaction_timestamp() + interval '1 microsecond'")
        return cursor.fetchone()[0]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("grant_scope", [AccessScope.STAFF, AccessScope.PORTAL])
def test_end_passes_between_review_form_and_signing_without_editing_the_grant(
    client, ar_make_user, ar_clinic, ar_reviewer, grant_scope
):
    subject = ar_make_user(
        "clock_subject", **({"client": ar_clinic} if grant_scope == AccessScope.PORTAL else {})
    )
    client.force_login(ar_reviewer)
    url = reverse("access_review:review-create")
    with transaction.atomic():
        boundary = ar_end_just_after_transaction_start()
        if grant_scope == AccessScope.PORTAL:
            stored = PortalAccess.objects.create(
                user=subject, client=ar_clinic, whole_organization=True, ends_at=boundary
            )
        else:
            stored = ar_end(subject, boundary)
        original = client.get(url, {"scope": grant_scope})
        assert original.status_code == 200
        before = [g for g in original.context["reading"].grants if g.account_id == subject.pk]
        assert len(before) == 1 and "last day" in before[0].detail
        with connection.cursor() as cursor:
            cursor.execute("SELECT clock_timestamp() >= %s", [boundary])
            assert cursor.fetchone() == (True,)
    refused = client.post(url, posted(original))
    assert refused.status_code == 200
    assert access_reviews.STALE in refused.context["form"].non_field_errors()
    assert (
        not AccessReview.objects.exists()
        and not AuditEvent.objects.filter(action=access_reviews.SIGNED).exists()
    )
    after = [g for g in refused.context["reading"].grants if g.account_id == subject.pk]
    if grant_scope == AccessScope.PORTAL:
        assert [g.detail for g in after] == ["Only the tickets it raised"]
        assert after[0].field_name in refused.context["form"].changed
    else:
        assert not after
    stored.refresh_from_db()
    assert stored.ends_at == boundary


@pytest.mark.django_db(transaction=True)
def test_signed_direct_removal_shows_current_held_status_without_altering_signed_words(
    client, ar_make_user, ar_reviewer
):
    holder = ar_make_user("temporary_holder", "technical")
    code = patient_permission().codename
    client.force_login(ar_reviewer)
    with transaction.atomic():
        boundary = ar_end_just_after_transaction_start()
        stored = ar_end(holder, boundary)
        review = sign(ar_reviewer, remove={f"{holder.pk}:capability:{code}"})
        item = review.items.get(account_id=holder.pk, code=code)
        assert access_reviews.still_held([item]) == {item.pk: True}
    response = client.get(reverse("access_review:review", args=[review.pk]))
    assert response.context["removals"] == [(item, False)]
    assert response.context["intact"] and item.detail.encode() in response.content
    rows = ar_csv(client.get(reverse("access_review:review-export", args=[review.pk])))
    (removed,) = (row for row in rows if row["Decision"] == "Remove")
    assert removed["Held now"] == "No" and removed["Detail"] == item.detail
    stored.refresh_from_db()
    assert stored.ends_at == boundary


@pytest.mark.django_db(transaction=True)
def test_assigning_an_end_waits_for_a_review_that_has_signed_but_not_committed(
    ar_make_user, ar_reviewer
):
    holder = ar_make_user("held_for_signing", "technical")
    permission = patient_permission()
    holder.user_permissions.add(permission)
    end = grant_ends.end_of(grant_ends.today() + timedelta(days=12))

    def store_end():
        close_old_connections()
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '350ms'")
                CapabilityEnd.objects.create(
                    user_id=holder.pk, permission_id=permission.pk, ends_at=end
                )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as workers:
        with transaction.atomic():
            review = sign(ar_reviewer)
            with pytest.raises(OperationalError) as waiting:
                workers.submit(store_end).result(timeout=10)
            assert waiting.value.__cause__.sqlstate == "55P03"
        workers.submit(store_end).result(timeout=10)
    assert CapabilityEnd.objects.get(user=holder, permission=permission).ends_at == end
    item = review.items.get(account_id=holder.pk, code=permission.codename)
    assert item.detail == "Given directly" and access_reviews.intact(review)


def test_review_refuses_a_moved_portal_end_when_its_printed_date_matches_across_zones(
    client, ar_make_user, ar_reviewer, ar_clinic
):
    portal = ar_make_user("zone_reviewed", client=ar_clinic)
    manager = ar_make_user("zone_manager", "account_manager")
    manager_client = HttpClient()
    manager_client.force_login(manager)
    client.force_login(ar_reviewer)
    edit = reverse("clients:portal-access-edit", args=[ar_clinic.pk, portal.pk])
    review_url = reverse("access_review:review-create")
    with override_settings(TIME_ZONE="Etc/UTC"):
        day = grant_ends.today() + timedelta(days=15)
        first = manager_client.post(
            edit, {"scope": "organization", "last_day": day.isoformat(), "expected_revision": 0}
        )
        assert first.status_code == 302
        before = PortalAccess.objects.get(user=portal).ends_at
        form = client.get(review_url, {"scope": AccessScope.PORTAL, "organization": ar_clinic.pk})
        assert form.status_code == 200
        assert f"last day {day}".encode() in form.content
        payload = posted(form)
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        current = manager_client.get(edit).context["form"].initial
        assert current["last_day"] == day + timedelta(days=1)
        changed = manager_client.post(
            edit,
            {
                "scope": "organization",
                "last_day": day.isoformat(),
                "reason": "Move the last day earlier",
                "expected_revision": current["expected_revision"],
            },
        )
        assert changed.status_code == 302
        after = PortalAccess.objects.get(user=portal).ends_at
        assert before - after == timedelta(hours=9)
        response = client.post(review_url, payload)
    assert not AccessReview.objects.exists(), "Signed despite a nine-hour change to the end."
    assert response.status_code == 200
    assert access_reviews.STALE in response.context["form"].non_field_errors()


@pytest.mark.parametrize("end_state", ["ending", "ended", "ended with group"])
def test_audit_workspace_and_signing_apply_the_reviewers_current_grant(
    client, ar_make_user, ar_reviewer, end_state
):
    old_review = ar_sign_audit_review(ar_reviewer)
    visitor = ar_make_user("workspace_visitor", "technical")
    permission = review_permission()
    visitor.user_permissions.add(permission)
    day = grant_ends.today() + timedelta(days=7 if end_state == "ending" else -7)
    CapabilityEnd.objects.create(
        user=visitor, permission=permission, ends_at=grant_ends.end_of(day)
    )
    if end_state == "ended with group":
        team = Group.objects.create(name="Auditors")
        team.permissions.add(permission)
        visitor.groups.add(team)
    client.force_login(visitor)
    can_review = end_state != "ended"
    paths = [
        reverse("audit_review:" + endpoint)
        for endpoint in (
            "changes",
            "access",
            "sign-ins",
            "people",
            "uses",
            "changes-export",
            "access-export",
            "sign-ins-export",
            "people-export",
            "uses-export",
            "reviews",
            "reviews-export",
            "review-create",
            "review-interval",
        )
    ] + [
        reverse("audit_review:" + endpoint, args=[old_review.pk])
        for endpoint in ("review", "review-note")
    ]
    for path in paths:
        assert client.get(path).status_code == (200 if can_review else 403), path
    for signing in (ar_sign_audit_review, sign):
        if can_review:
            assert signing(visitor).signed_by_id == visitor.pk
        else:
            with pytest.raises(PermissionDenied):
                signing(visitor)


def test_review_refuses_a_moved_direct_end_when_its_printed_date_matches_across_zones(
    client, ar_make_user, ar_reviewer
):
    account = ar_make_user("reviewed_in_two_zones", "technical")
    admin = acct_http("review-date-owner")
    client.force_login(ar_reviewer)
    review_url = reverse("access_review:review-create")
    day = grant_ends.today() + timedelta(days=20)
    with override_settings(TIME_ZONE="Etc/UTC"):
        end = acct_end(account, ACCT_PATIENT, grant_ends.end_of(day))
        before = end.ends_at
        response = client.get(review_url)
        grant = next(g for g in response.context["reading"].grants if g.code == ACCT_PATIENT)
        assert grant.detail == f"Given directly, last day {day}"
        decisions = posted(response)
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        url, form = acct_form(admin, account)
        field = end_field(ACCT_PATIENT)
        assert form[field].value() == day + timedelta(days=1)
        assert admin.post(url, page_data(form, **{field: day})).status_code == 302
        end.refresh_from_db()
        assert before - end.ends_at == timedelta(hours=9)
        response = client.post(review_url, decisions)
    assert not AccessReview.objects.exists(), "The review accepted a changed capability end."
    assert response.status_code == 200
    assert access_reviews.STALE in response.context["form"].non_field_errors()


def response_elements(response):
    """Find notices and controls separately from the prose in the help drawer."""

    def descendants(node):
        for child in node.children:
            if isinstance(child, Element):
                yield child
                yield from descendants(child)

    return descendants(parse_html(response.content.decode()))


@pytest.mark.parametrize("ended", [False, True], ids=["ending", "ended"])
def test_set_access_renders_the_status_notice_only_after_the_last_day(
    portal_parties, client, ended
):
    _, manager, _ = portal_parties
    access = portal_set(portal_parties, grant_ends.today() + timedelta(days=2))
    if ended:
        access.ends_at = grant_ends.end_of(grant_ends.today() - timedelta(days=2))
        access.save(update_fields=["ends_at"])
    client.force_login(manager)
    response = client.get(portal_edit_url(portal_parties))
    assert response.status_code == 200
    notices = [
        node
        for node in response_elements(response)
        if dict(node.attributes).get("role") in {"alert", "status"}
    ]
    assert any("Access ended" in node for node in notices) == ended


def test_past_portal_day_error_only_suggests_an_action_the_manager_can_take(portal_parties, client):
    _, manager, _ = portal_parties
    access = portal_set(portal_parties, grant_ends.today())
    client.force_login(manager)
    response = client.post(
        portal_edit_url(portal_parties),
        {
            "scope": "organization",
            "last_day": grant_ends.today() - timedelta(days=2),
            "expected_revision": access.revision,
            "reason": "Synthetic scope change",
        },
    )
    assert response.status_code == 200
    form = response.context["form"]
    assert form.has_error("last_day")
    # If the remedy instructs the operator to remove access, the operator needs an action for it.
    errors = " ".join(form.errors["last_day"]).lower()
    if "remove it instead" in errors:
        controls = [
            " ".join(strip_tags(str(node)).lower().split())
            for node in response_elements(response)
            if node.name in {"button", "a", "label", "option"}
        ]
        assert any(
            label.startswith(
                ("remove access", "revoke access", "end access", "only tickets it raised")
            )
            for label in controls
        ), f"Set access offers {list(form.fields['scope'].choices)}, but advises: {errors}"


@pytest.mark.parametrize("when", ["past", "future"])
def test_last_day_chosen_for_a_new_organization_is_validated_as_a_new_day(
    portal_parties, client, when
):
    _, manager, person = portal_parties
    former = Organization.objects.create(name="Synthetic former organization")
    # The page supports older records whose granting organization differs from the account's.
    # A day belonging to such a record was never offered as this organization's existing choice.
    day = grant_ends.today() + timedelta(days=-7 if when == "past" else 7)
    with override_settings(TIME_ZONE="Etc/UTC"):
        old = PortalAccess.objects.create(
            user=person, client=former, whole_organization=True, ends_at=grant_ends.end_of(day)
        )
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        chosen = grant_ends.last_day_of(old.ends_at)
        assert chosen == day + timedelta(days=1)
        client.force_login(manager)
        url = portal_edit_url(portal_parties)
        form = client.get(url).context["form"]
        assert form["last_day"].value() is None
        response = client.post(
            url,
            {
                "scope": "organization",
                "last_day": chosen.isoformat(),
                "expected_revision": form["expected_revision"].value(),
                "reason": "First decision for this organization",
            },
        )
        old.refresh_from_db()
        if when == "past":
            assert response.status_code == 200
            assert response.context["form"].has_error("last_day")
            assert old.client_id == former.pk
        else:
            assert response.status_code == 302 and old.client_id == person.client_id
            assert old.ends_at == grant_ends.end_of(chosen)
