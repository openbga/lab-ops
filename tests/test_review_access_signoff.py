"""Independent regression checks for signed access reviews. All records are synthetic."""

import codecs
import csv
import io
import json
import logging
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from io import StringIO

import pytest
from allauth.mfa.models import Authenticator
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import DatabaseError, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from accounts.acting import close_access
from accounts.models import SignInEvent, User
from accounts.roles import ROLES
from audit_review import access_review_views, access_reviews, seed
from audit_review.access_review_forms import AccessReviewForm, ScopeForm
from audit_review.models import (
    AccessReview,
    AccessReviewItem,
    AccessScope,
    Decision,
    Review,
    Source,
    UseKind,
    WorkspaceUse,
)
from audit_review.review_forms import NO_PASSWORD
from audit_review.tests.conftest import PASSWORD, review_permission
from audit_review.tests.conftest import agent as agent
from audit_review.tests.conftest import category as category
from audit_review.tests.conftest import clinic as clinic
from audit_review.tests.conftest import make_ticket as make_ticket
from audit_review.tests.conftest import make_user as make_user
from audit_review.tests.conftest import reviewer as reviewer
from audit_review.tests.conftest import water as water
from audit_review.tests.test_access_report import account_change, group_change, http_for
from audit_review.tests.test_access_reviews import form_page, key, patient_permission, posted, sign
from audit_review.tests.test_reviews import sign as sign_activity_review
from clients.models import ClientSite, PortalAccess
from clients.services import set_portal_access, update_client, update_site
from core.models import AuditEvent
from jobs.actor import system_account
from request_forms.models import Audience, FieldKind, FilledForm, RequestTemplate

# Authorization and concurrent signings


def review_addresses(review_id):
    return [
        reverse("access_review:reviews"),
        reverse("access_review:reviews-export"),
        reverse("access_review:review-create"),
        reverse("access_review:review", args=[review_id]),
        reverse("access_review:review-export", args=[review_id]),
    ]


@pytest.mark.parametrize(
    "audience",
    [
        "anonymous",
        *ROLES,
        "owner",
        "owner-with-role",
        "permission-only",
        "portal-with-permission",
        "deactivated",
        "group-reviewer",
        "admin-reviewer",
    ],
)
def test_each_address_checks_the_role_and_grant_before_disclosing_a_review(
    reviewer, make_user, clinic, audience
):
    record = sign(reviewer, findings="Quarterly synthetic sample")
    http = HttpClient()
    if audience != "anonymous":
        roles = {
            "owner-with-role": ["admin"],
            "deactivated": ["technical"],
            "group-reviewer": ["technical"],
            "admin-reviewer": ["admin"],
        }.get(audience, [audience] if audience in ROLES else [])
        owner = audience in {"owner", "owner-with-role"}
        actor = make_user(
            "second-person",
            *roles,
            is_staff=owner,
            is_superuser=owner,
            client=clinic if audience == "portal-with-permission" else None,
        )
        if audience in {
            "permission-only",
            "portal-with-permission",
            "deactivated",
            "admin-reviewer",
        }:
            actor.user_permissions.add(review_permission())
        elif audience == "group-reviewer":
            committee = Group.objects.create(name="Review committee")
            committee.permissions.add(review_permission())
            actor.groups.add(committee)
        http.force_login(actor)
        if audience == "deactivated":
            actor.is_active = False
            actor.save(update_fields=["is_active"])

    urls = review_addresses(record.pk)
    data = posted(form_page(http_for(reviewer)))
    before = WorkspaceUse.objects.filter(source=Source.ACCESS_REVIEWS).count()
    allowed = audience in {"group-reviewer", "admin-reviewer"}
    denied = 302 if audience in {"anonymous", "deactivated"} else 403
    for url in urls:
        result = http.get(url)
        assert result.status_code == (200 if allowed else denied), url
        if not allowed:
            assert record.number.encode() not in result.content, url
            assert b"Quarterly synthetic sample" not in result.content, url
            assert b"Rae Viewer" not in result.content, url
    for url in review_addresses(record.pk + 100)[-2:]:
        assert http.get(url).status_code == (404 if allowed else denied), url
    result = http.post(reverse("access_review:review-create"), data)
    if allowed:
        assert result.status_code == 302
        assert AccessReview.objects.exclude(pk=record.pk).get().signed_by_id == actor.pk
    else:
        assert result.status_code == denied
        assert AccessReview.objects.count() == 1
        assert WorkspaceUse.objects.filter(source=Source.ACCESS_REVIEWS).count() == before


@pytest.mark.parametrize("removed", ["groups", "user_permissions"])
def test_admin_removing_review_access_between_requests_refuses_the_open_form(
    reviewer, make_user, removed
):
    operator = http_for(reviewer)
    owner = http_for(make_user("owner", is_superuser=True, is_staff=True))
    data = posted(form_page(operator))
    account_change(owner, reviewer, **{removed: []})
    response = operator.post(reverse("access_review:review-create"), data)
    assert response.status_code == 403
    assert not AccessReview.objects.exists()
    assert not AuditEvent.objects.filter(action=access_reviews.SIGNED).exists()
    assert WorkspaceUse.objects.filter(source=Source.ACCESS_REVIEWS).count() == 1


def signing_arguments(actor, scope=AccessScope.STAFF, organization=None):
    inventory = access_reviews.reading(scope, organization)
    return {
        "actor": actor,
        "scope": scope,
        "organization": organization,
        "decisions": {g.key: Decision.KEEP for g in inventory.grants},
        "expected": inventory.fingerprint,
        "password_hash": actor.password,
    }


def on_database_connection(pid, operation):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '12s'")
            cursor.execute("SELECT pg_backend_pid()")
            pid.set_result(cursor.fetchone()[0])
        return operation()
    finally:
        connections.close_all()


def waits_for_this_transaction(pid, work):
    """Observe PostgreSQL's actual lock dependency or a completed operation."""
    deadline = time.monotonic() + 8
    with connection.cursor() as cursor:
        while time.monotonic() < deadline:
            cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
            if cursor.fetchone()[0]:
                return True
            if work.done():
                work.result()  # propagate any worker error
                return False
            time.sleep(0.02)
    pytest.fail("Neither a completed database operation nor its expected lock wait was observed")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("removed", ["role", "capability"])
def test_signer_queued_behind_an_access_change_uses_the_committed_role_and_grant(reviewer, removed):
    args = signing_arguments(reviewer)
    pid = Future()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            close_access([reviewer.pk])
            worker = pool.submit(
                on_database_connection, pid, lambda: access_reviews.sign_access_review(**args)
            )
            assert waits_for_this_transaction(pid.result(5), worker)
            if removed == "role":
                reviewer.groups.clear()
            else:
                reviewer.user_permissions.remove(review_permission())
        with pytest.raises(PermissionDenied):
            worker.result(10)
    assert not AccessReview.objects.exists()
    assert not AuditEvent.objects.filter(action=access_reviews.SIGNED).exists()


# Adapted in round 1: a capability added to a group on its page takes no member's gate, so it does
# not wait for a signing (audit_review.access_reviews, "Known limit, one rule").
GROUP_ADDITION_GOES_ON = pytest.mark.xfail(
    strict=True, reason="Documented limit: a capability added to a group does not wait"
)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "edit", ["account-name", pytest.param("add-group-capability", marks=GROUP_ADDITION_GOES_ON)]
)
def test_admin_save_and_review_commit_in_the_order_they_obtain_access(reviewer, make_user, edit):
    member = make_user("lab-member", "technical")
    owner = http_for(make_user("owner", is_superuser=True, is_staff=True))
    team = member.groups.get()
    grant = patient_permission()
    pid = Future()

    def edit_in_admin():
        if edit == "account-name":
            return account_change(owner, member, first_name="Mira").status_code
        return group_change(owner, team, permissions=[grant.pk]).status_code

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            review = sign(reviewer)
            worker = pool.submit(on_database_connection, pid, edit_in_admin)
            waited = waits_for_this_transaction(pid.result(5), worker)
        assert worker.result(10) == 302
    assert AccessReview.objects.get().pk == review.pk
    if edit == "account-name":
        member.refresh_from_db()
        assert member.first_name == "Mira"
    else:
        assert team.permissions.filter(pk=grant.pk).exists()
    assert waited, "The admin save committed while the transaction signing the review was open"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "change",
    [
        "username",
        "membership",
        "direct-grant",
        "remove-direct",
        "console",
        "deactivate",
        "remove-inherited",
    ],
)
def test_reviewed_staff_access_changes_wait_for_the_signed_inventory(reviewer, make_user, change):
    member = make_user("lab-member", "technical")
    owner = http_for(make_user("owner", is_superuser=True, is_staff=True))
    role = member.groups.get()
    grant = patient_permission()
    if change == "remove-direct":
        member.user_permissions.add(grant)
    if change == "remove-inherited":
        role.permissions.add(grant)
    admin_values = {
        "username": {"username": "renamed-member"},
        "membership": {"groups": [reviewer.groups.get().pk]},
        "direct-grant": {"user_permissions": [grant.pk]},
        "remove-direct": {"user_permissions": []},
        "console": {"is_staff": "on"},
        "deactivate": {"is_active": ""},
    }

    def save_change():
        if change == "remove-inherited":
            return group_change(owner, role, permissions=[])
        return account_change(owner, member, **admin_values[change])

    before = access_reviews.reading(AccessScope.STAFF).fingerprint
    pid = Future()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            signed = sign(reviewer)
            worker = pool.submit(on_database_connection, pid, save_change)
            waited = waits_for_this_transaction(pid.result(5), worker)
        assert worker.result(10).status_code == 302
    assert access_reviews.intact(signed)
    assert before != access_reviews.reading(AccessScope.STAFF).fingerprint
    assert waited


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["organization", "sites"])
def test_portal_move_or_change_of_sites_waits_for_its_signed_inventory(
    reviewer, make_user, clinic, water, change
):
    contact = make_user("site-contact", client=clinic)
    owner = http_for(make_user("owner", is_superuser=True, is_staff=True))
    manager = make_user("manager", "account_manager")
    site = ClientSite.objects.create(client=clinic, code="S", name="South")
    access = PortalAccess.objects.create(client=clinic, user=contact, whole_organization=True)
    pid = Future()

    def save_change():
        if change == "organization":
            assert account_change(owner, contact, client=water.pk).status_code == 302
        else:
            set_portal_access(
                actor=manager,
                user_id=contact.pk,
                expected_client_id=clinic.pk,
                expected_revision=access.revision,
                sites=[site],
                reason="Review local site",
            )

    before = access_reviews.reading(AccessScope.PORTAL, clinic).fingerprint
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            signed = sign(reviewer, AccessScope.PORTAL, clinic)
            worker = pool.submit(on_database_connection, pid, save_change)
            waited = waits_for_this_transaction(pid.result(5), worker)
        worker.result(10)
    assert access_reviews.intact(signed)
    assert before != access_reviews.reading(AccessScope.PORTAL, clinic).fingerprint
    assert waited


@pytest.mark.django_db(transaction=True)
def test_two_signatures_receive_their_numbers_in_commit_order(reviewer, make_user):
    colleague = make_user("colleague", "technical")
    colleague.user_permissions.add(review_permission())
    args = signing_arguments(colleague)
    pid = Future()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            first = sign(reviewer)
            worker = pool.submit(
                on_database_connection, pid, lambda: access_reviews.sign_access_review(**args)
            )
            assert waits_for_this_transaction(pid.result(5), worker)
        second = worker.result(10)
    assert first.pk < second.pk
    assert [first.number, second.number] == [f"AC-{pk:06d}" for pk in (first.pk, second.pk)]
    assert list(AccessReview.objects.order_by("pk").values_list("signed_by_id", flat=True)) == [
        reviewer.pk,
        colleague.pk,
    ]
    assert AuditEvent.objects.filter(action=access_reviews.SIGNED).count() == 2


@pytest.mark.django_db(transaction=True)
def test_scoped_signature_uses_the_same_organization_name_as_its_items(
    client, reviewer, make_user, clinic, monkeypatch
):
    make_user("portal", client=clinic)
    manager = make_user("manager", "account_manager")
    client.force_login(reviewer)
    response = form_page(client, f"?scope=portal&organization={clinic.pk}")
    data = posted(response)
    original_name = clinic.name
    directory = update_client(
        actor=manager,
        client_id=clinic.pk,
        expected_revision=clinic.revision,
        reason="Directory maintenance",
        name="Sample Clinical Services",
    )
    chosen = ScopeForm.chosen

    def select_while_directory_is_corrected(form):
        result = chosen(form)
        assert result[1].name == directory.name

        # The directory correction commits on its own connection after the POST resolved
        # the choice, before it loaded the account list. The operator's original words now match.
        def correct_directory():
            return update_client(
                actor=manager,
                client_id=clinic.pk,
                expected_revision=directory.revision,
                reason="Restore registered name",
                name=original_name,
            )

        with ThreadPoolExecutor(max_workers=1) as pool:
            record = pool.submit(on_database_connection, Future(), correct_directory).result(10)
        assert record.name == original_name
        return result

    monkeypatch.setattr(ScopeForm, "chosen", select_while_directory_is_corrected)
    saved = client.post(reverse("access_review:review-create"), data)
    if saved.status_code == 200:
        assert saved.context["form"].non_field_errors()
        assert not AccessReview.objects.exists()
        return
    assert saved.status_code == 302
    review = AccessReview.objects.get()
    assert review.items.get().organization_name == original_name
    assert review.organization_name == original_name


@pytest.mark.django_db(transaction=True)
def test_portal_site_words_were_in_force_at_the_recorded_signing_time(
    reviewer, make_user, clinic, monkeypatch
):
    contact = make_user("portal", client=clinic)
    manager = make_user("manager", "account_manager")
    site = ClientSite.objects.create(client=clinic, code="ST", name="North station")
    access = PortalAccess.objects.create(user=contact, client=clinic)
    access.sites.add(site)
    args = signing_arguments(reviewer, AccessScope.PORTAL, clinic)
    revised = update_site(
        actor=manager,
        site_id=site.pk,
        expected_revision=site.revision,
        reason="Directory maintenance",
        name="North campus",
    )
    # The reviewer saw North station. By the time signing starts, the site's label is
    # North campus. The directory editor restores the original label during the final reading.
    read_grants = access_reviews.read_grants
    with ThreadPoolExecutor(max_workers=1) as pool:

        def correct_site():
            earliest = timezone.now()
            update_site(
                actor=manager,
                site_id=site.pk,
                expected_revision=revised.revision,
                reason="Restore registered site label",
                name=site.name,
            )
            return earliest

        def read_during_directory_edit(accounts):
            nonlocal change
            pid = Future()
            change = pool.submit(on_database_connection, pid, correct_site)
            # A reader that locks the site may legitimately finish before the directory edit.
            waits_for_this_transaction(pid.result(5), change)
            return read_grants(accounts)

        change = None
        monkeypatch.setattr(access_reviews, "read_grants", read_during_directory_edit)
        try:
            review = access_reviews.sign_access_review(**args)
        except ValidationError as exc:
            assert access_reviews.STALE in exc.messages
            assert not AccessReview.objects.exists()
            return
        assert change is not None
        restored_no_earlier_than = change.result(10)
    assert site.name in review.items.get().detail
    assert review.signed_at >= restored_no_earlier_than, (
        "The signed site name was restored only after the time on the signature"
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("directory_edit", ["new-account", "activation", "org-name", "site-name"])
def test_final_inventory_sees_directory_changes_committed_after_the_access_gates(
    reviewer, make_user, clinic, monkeypatch, directory_edit
):
    contact = make_user("contact", client=clinic)
    inactive = make_user("on-leave", client=clinic, is_active=False)
    manager = make_user("manager", "account_manager")
    site = ClientSite.objects.create(client=clinic, code="N", name="North")
    access = PortalAccess.objects.create(user=contact, client=clinic)
    access.sites.add(site)
    args = signing_arguments(reviewer, AccessScope.PORTAL, clinic)
    gates = access_reviews.share_access

    def directory_change():
        if directory_edit == "org-name":
            return update_client(
                actor=manager,
                client_id=clinic.pk,
                expected_revision=clinic.revision,
                reason="Directory maintenance",
                name="New clinic name",
            )
        if directory_edit == "site-name":
            return update_site(
                actor=manager,
                site_id=site.pk,
                expected_revision=site.revision,
                reason="Directory maintenance",
                name="New site name",
            )
        if directory_edit == "activation":
            inactive.is_active = True
            return inactive.save(update_fields=["is_active"])
        return make_user("new-contact", client=clinic)

    def take_gates(ids):
        gates(ids)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(on_database_connection, Future(), directory_change).result(10)

    monkeypatch.setattr(access_reviews, "share_access", take_gates)
    with pytest.raises(ValidationError) as refused:
        access_reviews.sign_access_review(**args)
    assert access_reviews.STALE in refused.value.messages
    assert not AccessReview.objects.exists()
    assert not AuditEvent.objects.filter(action=access_reviews.SIGNED).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("later", ["created", "activated"])
def test_portal_account_entering_scope_after_the_final_reading_belongs_to_the_next_review(
    reviewer, make_user, clinic, later
):
    original = make_user("in-scope", client=clinic)
    dormant = make_user("returning", client=clinic, is_active=False)

    def enter_scope():
        if later == "created":
            return make_user("arriving", client=clinic).pk
        dormant.is_active = True
        dormant.save(update_fields=["is_active"])
        return dormant.pk

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            old = sign(reviewer, AccessScope.PORTAL, clinic)
            pid = Future()
            entered = pool.submit(on_database_connection, pid, enter_scope)
            waits_for_this_transaction(pid.result(5), entered)
        newcomer = entered.result(10)

    assert list(old.items.values_list("account_id", flat=True)) == [original.pk]
    fresh = sign(reviewer, AccessScope.PORTAL, clinic)
    assert set(fresh.items.values_list("account_id", flat=True)) == {original.pk, newcomer}


def test_review_items_and_signature_roll_back_together_if_change_history_cannot_be_written(
    reviewer, monkeypatch
):
    class HistoryUnavailable(Exception):
        pass

    def history_unavailable(**kwargs):
        assert AccessReview.objects.filter(pk=kwargs["obj"].pk).exists()
        assert kwargs["obj"].items.exists()
        raise HistoryUnavailable

    monkeypatch.setattr(access_reviews, "record_event", history_unavailable)
    with pytest.raises(HistoryUnavailable):
        sign(reviewer)
    assert not AccessReview.objects.exists()
    assert not AccessReviewItem.objects.exists()
    assert not AuditEvent.objects.filter(action=access_reviews.SIGNED).exists()


# Inventory, limits and live removal status


def test_staff_inventory_uses_display_name_then_username_and_stored_grants(reviewer, make_user):
    first = make_user(
        "cedar", "technical", "supervisor", first_name="Birch", is_staff=True, is_superuser=True
    )
    make_user("amber", "technical", first_name="Birch")
    make_user("alder", is_staff=True)
    make_user("no-grants")
    make_user("former", "technical", is_active=False)
    jobs = system_account()
    User.objects.filter(pk=jobs.pk).update(is_active=True, is_staff=True)
    technical = Group.objects.get(name="technical")
    technical.permissions.add(patient_permission())
    night = Group.objects.create(name="night shift")
    early = Group.objects.create(name="Early shift")
    night.permissions.add(patient_permission(), review_permission())
    first.groups.add(night, early)
    first.user_permissions.add(patient_permission(), review_permission())
    reading = access_reviews.reading(AccessScope.STAFF)
    assert reading.accounts == 4
    assert list(dict.fromkeys(g.account_username for g in reading.grants)) == [
        "alder",
        "amber",
        "cedar",
        "reviewer",
    ]
    assert [(g.label, g.detail) for g in reading.grants if g.account_id == first.pk] == [
        ("Admin console", ""),
        ("Superuser", ""),
        ("Supervisor role", ""),
        ("Technical staff role", "Gives Can view patient information"),
        ("Group “Early shift”", ""),
        (
            "Group “night shift”",
            "Gives Can view patient information and Can review access and audit records",
        ),
        ("Can view patient information", "Given directly"),
        ("Can review access and audit records", "Given directly"),
    ]


def test_portal_inventory_includes_inactive_granted_sites_and_resets_on_move(
    reviewer, make_user, clinic, water
):
    organization = make_user("organization", client=clinic)
    limited = make_user("sites", client=clinic)
    make_user("own", client=water)
    make_user("deactivated", client=clinic, is_active=False)
    PortalAccess.objects.create(user=organization, client=clinic, whole_organization=True)
    access = PortalAccess.objects.create(user=limited, client=clinic)
    active = ClientSite.objects.create(client=clinic, name="Birch", code="B")
    former = ClientSite.objects.create(client=clinic, name="Alder", code="A")
    access.sites.add(active, former)
    former.is_active = False
    former.save(update_fields=["is_active"])

    def portal_rows(org=None):
        reading = access_reviews.reading(AccessScope.PORTAL, org)
        assert reading.accounts == len(reading.grants)
        return [(g.account_username, g.organization_name, g.detail) for g in reading.grants]

    whole_row = ("organization", clinic.name, "Its organization's tickets")
    site_row = ("sites", clinic.name, "Tickets of Alder, Birch, and those it raised")
    own_row = ("own", water.name, "Only the tickets it raised")
    assert portal_rows() == [whole_row, own_row, site_row]
    assert portal_rows(clinic) == [whole_row, site_row]
    assert portal_rows(water) == [own_row]
    limited.client = water
    limited.changed_by = reviewer
    limited.save(update_fields=["client"])
    assert portal_rows(clinic) == [whole_row]
    assert portal_rows(water) == [own_row, ("sites", water.name, "Only the tickets it raised")]


def test_grantless_staff_are_left_out_before_applying_the_account_limit(client, reviewer):
    User.objects.bulk_create(
        [User(username=f"empty-{i}", email=f"empty-{i}@example.invalid") for i in range(500)]
    )
    client.force_login(reviewer)
    response = form_page(client)
    assert not response.context["reading"].too_many, "Only the reviewer holds grants"
    assert response.context["reading"].accounts == 1
    result = client.post(response.wsgi_request.path, posted(response))
    assert result.status_code == 302
    assert AccessReview.objects.get().accounts == 1


@pytest.mark.parametrize("limit", ["accounts", "grants"])
def test_review_can_sign_at_each_published_limit_and_refuses_one_more(reviewer, limit):
    if limit == "accounts":
        people = [
            User(username=f"holder-{i}", email=f"holder-{i}@example.invalid", is_staff=True)
            for i in range(499)
        ]
        User.objects.bulk_create(people)
        expected = (500, 501)
    else:
        groups = Group.objects.bulk_create([Group(name=f"Group {i}") for i in range(898)])
        reviewer.groups.add(*groups)
        expected = (1, 900)
    review = sign(reviewer)
    assert (review.accounts, review.grants) == expected
    assert review.items.count() == expected[1]
    if limit == "accounts":
        User.objects.create(username="last", email="last@example.invalid", is_staff=True)
    else:
        reviewer.groups.add(Group.objects.create(name="Last group"))
    current = access_reviews.reading(AccessScope.STAFF)
    assert current.too_many and not current.grants


def test_single_portal_grant_with_many_sites_can_be_signed(client, reviewer, make_user, clinic):
    member = make_user("regional", client=clinic)
    access = PortalAccess.objects.create(user=member, client=clinic)
    sites = ClientSite.objects.bulk_create(
        [ClientSite(client=clinic, name=f"Site {n:03}", code=f"S{n}") for n in range(90)]
    )
    access.sites.add(*sites)
    client.force_login(reviewer)
    response = form_page(client, f"?scope=portal&organization={clinic.pk}")
    grants = response.context["reading"].grants
    assert len(grants) == 1 and grants[0].kind == "portal"
    assert all(site.name in grants[0].detail for site in sites)
    result = client.post(response.wsgi_request.path, posted(response))
    assert result.status_code == 302
    item = AccessReview.objects.get().items.get()
    assert item.detail == grants[0].detail and item.decision == Decision.KEEP


def _grant_export(client, route, prefix, *args):
    response = client.get(reverse(f"access_review:{route}", args=args))
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert re.fullmatch(
        rf'attachment; filename="{re.escape(prefix)}-\d{{8}}-\d{{6}}\.csv"',
        response["Content-Disposition"],
    )
    assert response.content.startswith(codecs.BOM_UTF8)
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


def test_both_exports_include_documented_columns_and_quote_formula_like_text(
    client, reviewer, make_user
):
    person = make_user("unit", is_staff=True, first_name="+Équipe")
    review = sign(
        reviewer,
        remove={key(person, "console", "is_staff")},
        findings="=1+2",
        follow_up="@unit, check\nFriday",
    )
    client.force_login(reviewer)
    list_header, row = _grant_export(client, "reviews-export", "access-review-reviews")
    assert list_header == [
        "Access review",
        "Accounts reviewed",
        "Accounts",
        "Grants",
        "Decided to remove",
        "Findings",
        "Follow-up",
        "Signed by",
        "Username",
        "Roles",
        "Meaning",
        "Signed at",
        "SHA-256 checksum",
    ]
    signed = datetime.fromisoformat(row[11])
    assert signed.utcoffset() is not None
    assert signed == review.signed_at.replace(microsecond=0)
    assert row == [
        review.number,
        "Staff accounts",
        "2",
        "3",
        "1",
        "'=1+2",
        "'@unit, check\nFriday",
        "Rae Viewer",
        "reviewer",
        "Supervisor",
        "Reviewed",
        row[11],
        review.checksum,
    ]
    header, *items = _grant_export(
        client, "review-export", f"access-review-{review.number}", review.pk
    )
    assert header == [
        "Access review",
        "Person",
        "Username",
        "Client organization",
        "Grant",
        "Detail",
        "Decision",
        "Held now",
        "Signed at",
    ]
    assert items == [
        [review.number, "'+Équipe", "unit", "", "Admin console", "", "Remove", "Yes", row[11]],
        [review.number, "Rae Viewer", "reviewer", "", "Supervisor role", "", "Keep", "", row[11]],
        [
            review.number,
            "Rae Viewer",
            "reviewer",
            "",
            "Can review access and audit records",
            "Given directly",
            "Keep",
            "",
            row[11],
        ],
    ]


def _assert_live_removals(client, review, expected):
    response = client.get(reverse("access_review:review", args=[review.pk]))
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    assert response.context["intact"]
    assert {item.label: held for item, held in response.context["removals"]} == expected
    header, *rows = _grant_export(
        client, "review-export", f"access-review-{review.number}", review.pk
    )
    index = header.index("Held now")
    removed = {r[4]: r[index] for r in rows if r[6] == "Remove"}
    assert removed == {label: "Yes" if held else "No" for label, held in expected.items()}


def test_review_and_export_recheck_each_grant_and_keep_snapshots_after_deletion(
    client, reviewer, make_user
):
    worker = make_user("assessor", "technical", is_staff=True, is_superuser=True)
    group = Group.objects.create(name="Release")
    group.permissions.add(patient_permission())
    worker.groups.add(group)
    worker.user_permissions.add(patient_permission())
    expected = {
        "Admin console": True,
        "Superuser": True,
        "Technical staff role": True,
        "Group “Release”": True,
        "Can view patient information": True,
    }
    grants = access_reviews.reading(AccessScope.STAFF).grants
    review = sign(reviewer, remove=[g.key for g in grants if g.account_id == worker.pk])
    client.force_login(reviewer)
    _assert_live_removals(client, review, expected)
    worker.is_superuser = False
    worker.save(update_fields=["is_superuser"])
    worker.groups.remove(Group.objects.get(name="technical"))
    worker.user_permissions.remove(patient_permission())
    expected.update(
        {"Superuser": False, "Technical staff role": False, "Can view patient information": False}
    )
    _assert_live_removals(client, review, expected)
    group.delete()
    expected["Group “Release”"] = False
    _assert_live_removals(client, review, expected)
    worker.is_active = False
    worker.save(update_fields=["is_active"])
    expected["Admin console"] = False
    _assert_live_removals(client, review, expected)
    worker.delete()
    _assert_live_removals(client, review, expected)


def test_removed_portal_account_on_review_and_export_tracks_organization_not_sites(
    client, reviewer, make_user, clinic, water
):
    member = make_user("visiting", client=clinic)
    grant = access_reviews.reading(AccessScope.PORTAL, clinic).grants[0]
    review = sign(reviewer, AccessScope.PORTAL, clinic, remove=[grant.key])
    client.force_login(reviewer)
    _assert_live_removals(client, review, {"Portal account": True})
    PortalAccess.objects.create(user=member, client=clinic, whole_organization=True)
    _assert_live_removals(client, review, {"Portal account": True})
    member.client = water
    member.changed_by = reviewer
    member.save(update_fields=["client"])
    _assert_live_removals(client, review, {"Portal account": False})


def test_detail_explains_a_stored_checksum_mismatch(client, reviewer):
    # Start with a mismatched fixture without changing history or disabling a trigger.
    signed = sign(reviewer)
    mismatch = AccessReview.objects.get(pk=signed.pk)
    mismatch.pk = None
    mismatch.number = "AC-987654"
    mismatch.checksum = "0" * 64
    mismatch.save(force_insert=True)
    for item in signed.items.all():
        item.pk = None
        item.review = mismatch
        item.save(force_insert=True)
    client.force_login(reviewer)
    response = client.get(reverse("access_review:review", args=[mismatch.pk]))
    assert response.status_code == 200 and not response.context["intact"]
    assert b"Does not match the access review as stored." in response.content
    assert b"Compare it with an earlier export or a backup." in response.content


# Form submission at the grant limit


def test_form_accepts_900_decisions_then_shows_limit_at_901(client, reviewer):
    extra = Group.objects.bulk_create([Group(name=f"Bench {i:03}") for i in range(898)])
    reviewer.groups.add(*extra)
    client.force_login(reviewer)
    initial = form_page(client)
    body = posted(initial)
    assert sum(k.startswith("d-") for k in body) == 900
    result = client.post(reverse("access_review:review-create"), body)
    assert result.status_code == 302
    assert AccessReview.objects.get().items.count() == 900
    reviewer.groups.add(Group.objects.create(name="One more bench"))
    full = form_page(client)
    assert full.context["reading"].too_many
    assert b'name="confirm_password"' not in full.content


# Input validation, redraws and the signed content


def _assert_no_signoff():
    assert not AccessReview.objects.exists()
    assert not AuditEvent.objects.filter(action=access_reviews.SIGNED).exists()


def _assert_choices_match_drawn_form(client, response):
    """Compare the hidden fields on the redraw to a separate GET of the current staff form."""
    current = posted(form_page(client))
    redrawn = posted(response)
    assert redrawn["expected"] == current["expected"]
    assert redrawn["shown"] == current["shown"]
    assert set(redrawn["shown"].split(",")) == {name for name in redrawn if name.startswith("d-")}


def test_two_changes_return_fresh_decisions_and_text_before_signing(client, reviewer, agent):
    client.force_login(reviewer)
    url = reverse("access_review:review-create")
    get = form_page(client)
    initial = posted(
        get, decide=Decision.REMOVE, findings="Check memberships.", follow_up="Ask owner."
    )
    original_fields = initial["shown"].split(",")
    group = Group.objects.create(name="Morning shift")
    agent.groups.add(group)

    first = client.post(url, initial)
    assert first.status_code == 200
    form = first.context["form"]
    assert form.non_field_errors() == [access_reviews.STALE]
    assert form["confirm_password"].value() is None
    assert form["findings"].value() == initial["findings"]
    assert form["follow_up"].value() == initial["follow_up"]
    assert all(form[name].value() == Decision.REMOVE for name in original_fields)
    new = next(grant.field_name for grant in form.grants if grant.label == "Group “Morning shift”")
    assert form.changed == {new}
    assert form[new].value() is None
    _assert_choices_match_drawn_form(client, first)
    _assert_no_signoff()

    sending = posted(first, findings=initial["findings"], follow_up=initial["follow_up"])
    sending.update({name: Decision.REMOVE for name in original_fields})
    group.name = "Afternoon shift"
    group.save(update_fields=["name"])
    second = client.post(url, sending)
    assert second.status_code == 200
    form = second.context["form"]
    assert form.non_field_errors() == [access_reviews.STALE]
    assert form["confirm_password"].value() is None
    changed = next(grant.field_name for grant in form.grants if "Afternoon shift" in grant.label)
    assert form.changed == {changed} and form[changed].value() is None
    assert new not in form.fields
    assert all(form[name].value() == Decision.REMOVE for name in original_fields)
    _assert_choices_match_drawn_form(client, second)
    _assert_no_signoff()

    corrected = posted(
        second, findings=form["findings"].value(), follow_up=form["follow_up"].value()
    )
    corrected.update({name: form[name].value() for name in original_fields})
    result = client.post(url, corrected)
    assert result.status_code == 302
    review = AccessReview.objects.get()
    assert result.url == reverse("access_review:review", args=[review.pk])
    assert (review.accounts, review.grants, review.removals) == (2, 4, 3)
    assert review.items.get(label="Group “Afternoon shift”").decision == Decision.KEEP
    assert (review.findings, review.follow_up) == (initial["findings"], initial["follow_up"])


@pytest.mark.parametrize("stage", ["first display", "stale redraw"])
def test_grants_summary_counts_choices_still_needed(client, reviewer, agent, stage):
    client.force_login(reviewer)
    response = form_page(client)
    if stage == "stale redraw":
        data = posted(response)
        agent.groups.add(Group.objects.create(name="Second shift"))
        response = client.post(reverse("access_review:review-create"), data)
        assert response.status_code == 200
    form = response.context["form"]
    missing = sum(form[grant.field_name].value() is None for grant in form.grants)
    assert missing == (3 if stage == "first display" else 1)
    message = (
        "1 grant still needs a decision." if missing == 1 else "3 grants still need a decision."
    )
    assert message.encode() in response.content


def test_drawn_scope_distinguishes_one_organization_from_every_organization(
    client, reviewer, make_user, clinic
):
    make_user("one-portal-account", client=clinic)
    client.force_login(reviewer)
    all_portal = posted(form_page(client, "?scope=portal"))
    one_org = posted(form_page(client, f"?scope=portal&organization={clinic.pk}"))
    assert all_portal["shown"] == one_org["shown"]  # same grants, different scope shown at signing
    assert all_portal["organization"] == "" and one_org["organization"] == str(clinic.pk)
    assert all_portal["expected"] != one_org["expected"]


@pytest.mark.parametrize("change", ["scope", "organization", "expected"])
def test_changed_scope_or_digest_with_different_grants_requires_a_new_signature(
    client, reviewer, make_user, clinic, water, change
):
    make_user("clinic-contact", client=clinic)
    make_user("water-contact", client=water)
    client.force_login(reviewer)
    query = f"?scope=portal&organization={clinic.pk}" if change == "organization" else ""
    data = posted(form_page(client, query), confirm_password="synthetic incorrect")
    if change == "scope":
        data["scope"] = "portal"
    if change == "organization":
        data["organization"] = str(water.pk)
    if change == "expected":
        data["expected"] = "0" * 64
    response = client.post(reverse("access_review:review-create"), data)
    assert response.status_code == 200
    assert response.context["form"].non_field_errors() == [access_reviews.STALE]
    assert response.context["form"]["confirm_password"].value() is None
    assert not SignInEvent.objects.filter(kind=SignInEvent.Kind.REAUTHENTICATION_FAILED).exists()
    _assert_no_signoff()


@pytest.mark.parametrize(
    "reason", ["missing", "invalid", "other field", "empty password", "unusable"]
)
def test_invalid_choices_or_missing_password_do_not_sign_or_cost_a_password_attempt(
    client, reviewer, reason
):
    if reason == "unusable":
        reviewer.set_unusable_password()
        reviewer.save(update_fields=["password"])
    client.force_login(reviewer)
    data = posted(form_page(client), confirm_password="synthetic incorrect")
    name = data["shown"].split(",")[0]
    if reason == "empty password":
        data["confirm_password"] = ""
    elif reason == "missing":
        del data[name]
    elif reason == "other field":
        data["d-field-from-another-form"] = data.pop(name)
    elif reason == "invalid":
        data[name] = "defer"
    response = client.post(reverse("access_review:review-create"), data)
    assert response.status_code == 200
    form = response.context["form"]
    if reason == "unusable":
        assert form["confirm_password"].errors == [NO_PASSWORD]
    elif reason == "empty password":
        assert form.has_error("confirm_password", code="required")
    else:
        assert form.has_error(name, code="invalid_choice" if reason == "invalid" else "required")
    assert not SignInEvent.objects.filter(kind=SignInEvent.Kind.REAUTHENTICATION_FAILED).exists()
    _assert_no_signoff()


def test_only_the_current_grants_enter_the_review_if_an_unrelated_control_is_submitted(
    client, reviewer
):
    client.force_login(reviewer)
    drawn = form_page(client)
    grants = drawn.context["reading"].grants
    data = posted(drawn)
    data["d-unrelated"] = Decision.REMOVE
    response = client.post(reverse("access_review:review-create"), data)
    if response.status_code == 200:
        _assert_no_signoff()
        return
    assert response.status_code == 302
    signed = AccessReview.objects.get()
    assert list(signed.items.order_by("position").values_list("kind", "code", "decision")) == [
        (grant.kind, grant.code, Decision.KEEP) for grant in grants
    ]
    assert signed.removals == 0


def test_shown_metadata_does_not_supply_a_missing_or_invalid_decision(client, reviewer):
    client.force_login(reviewer)
    data = posted(form_page(client))
    first, other = data["shown"].split(",")
    data["shown"] = other
    del data[first]
    data[other] = "undecided"
    response = client.post(reverse("access_review:review-create"), data)
    assert response.status_code == 200
    form = response.context["form"]
    assert form.has_error(first, "required") and form.has_error(other, "invalid_choice")
    _assert_no_signoff()


@pytest.mark.django_db(transaction=True)
def test_password_changed_in_another_session_after_the_check_refuses_signing(
    client, reviewer, monkeypatch
):
    client.force_login(reviewer)
    other = HttpClient()
    other.force_login(reviewer)
    data = posted(form_page(client))
    check = AccessReviewForm.check_password
    new_password = "sunlit maple river stone 578"

    def change_after_check(form):
        assert check(form)
        assert form.checked_hash == reviewer.password
        changed = other.post(
            reverse("account_change_password"),
            {"oldpassword": PASSWORD, "password1": new_password, "password2": new_password},
        )
        assert changed.status_code == 302
        reviewer.refresh_from_db()
        assert reviewer.check_password(new_password)
        return True

    monkeypatch.setattr(AccessReviewForm, "check_password", change_after_check)
    response = client.post(reverse("access_review:review-create"), data)
    _assert_no_signoff()
    reviewer.refresh_from_db()
    assert reviewer.check_password(new_password)
    assert response.status_code == 302 and response.url == reverse("account_login")


def test_checksum_changes_for_each_signed_field_and_the_item_order(reviewer, make_user, clinic):
    for name in ["first-contact", "second-contact"]:
        make_user(name, client=clinic)
    review = sign(reviewer, scope=AccessScope.PORTAL, organization=clinic)
    items = list(review.items.order_by("position"))
    assert access_reviews.intact(review, items)
    assert not access_reviews.intact(review, list(reversed(items)))
    review_fields = (
        "number",
        "scope",
        "organization_id",
        "organization_name",
        "accounts",
        "grants",
        "removals",
        "findings",
        "follow_up",
        "signed_by_id",
        "signer_name",
        "signer_username",
        "signer_roles",
        "meaning",
        "signed_at",
    )
    item_fields = (
        "position",
        "account_id",
        "account_name",
        "account_username",
        "organization_name",
        "kind",
        "code",
        "label",
        "detail",
        "decision",
    )
    for record, fields in [(review, review_fields), (items[0], item_fields)]:
        for field in fields:
            original = getattr(record, field)
            if field == "signed_at":
                replacement = original + timedelta(microseconds=1)
            elif isinstance(original, int):
                replacement = original + 1
            else:
                replacement = f"{original} (changed)"
            setattr(record, field, replacement)
            assert not access_reviews.intact(review, items), field
            setattr(record, field, original)
            assert access_reviews.intact(review, items), field


# Workspace use, history and documentation


def _grant_use(response, reviewer, kind, filters, rows):
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    # A separate row for this HTTP request, with its own request id (also on redrawn forms).
    use = WorkspaceUse.objects.get(request_ref=response["X-Request-ID"])
    assert (use.account_id, use.source, use.kind, use.filters, use.rows) == (
        reviewer.pk,
        Source.ACCESS_REVIEWS,
        kind,
        filters,
        rows,
    )


def test_review_uses_and_change_history_match_the_responses(client, reviewer, clinic, make_user):
    portal = make_user("signoff-portal", client=clinic)
    client.force_login(reviewer)
    form_url = reverse("access_review:review-create")
    scope = {"scope": "portal", "organization": str(clinic.pk)}
    form_filters = {"form": "new", **scope}
    form_response = client.get(form_url, {**scope, "unused": "Unaccepted synthetic text"})
    _grant_use(form_response, reviewer, UseKind.VIEWED, form_filters, 1)
    data = posted(
        form_response, decide="remove", findings="Access has ended.", follow_up="Verify Friday."
    )
    failed = client.post(form_url, {**data, "confirm_password": ""})
    assert b"Enter your password" in failed.content
    _grant_use(failed, reviewer, UseKind.VIEWED, form_filters, 1)
    saved = client.post(form_url, data)
    assert saved.status_code == 302
    assert not WorkspaceUse.objects.filter(request_ref=saved["X-Request-ID"]).exists()
    review = AccessReview.objects.get()
    assert review.request_ref == saved["X-Request-ID"]
    change = AuditEvent.objects.get(request_ref=review.request_ref, action="access_review.signed")
    assert (change.object_type, change.object_id, change.actor_id) == (
        "audit_review.accessreview",
        str(review.pk),
        reviewer.pk,
    )
    assert change.before == {} and change.reason == ""
    assert change.after == {
        "number": review.number,
        "scope": "portal",
        "organization": clinic.name,
        "accounts": 1,
        "grants": 1,
        "removals": 1,
        "removed": [
            {
                "account": portal.username,
                "grant": "Portal account",
                "detail": "Only the tickets it raised",
            }
        ],
        "findings": "Access has ended.",
        "follow_up": "Verify Friday.",
        "signer_name": "Rae Viewer",
        "signer_username": reviewer.username,
        "signer_roles": "Supervisor",
        "meaning": "reviewed",
        "signed_at": review.signed_at.isoformat(),
        "checksum": review.checksum,
    }
    portal.refresh_from_db()
    assert portal.is_active and portal.client_id == clinic.pk
    record = {"access_review": str(review.pk)}
    for name, ids, kind, filters in (
        ("reviews", [], UseKind.VIEWED, {"page": "1"}),
        ("review", [review.pk], UseKind.VIEWED, record),
        ("reviews-export", [], UseKind.EXPORTED, {}),
        ("review-export", [review.pk], UseKind.EXPORTED, record),
    ):
        response = client.get(reverse(f"access_review:{name}", args=ids))
        _grant_use(response, reviewer, kind, filters, 1)
    uses = client.get(reverse("audit_review:uses"))
    assert uses.status_code == 200
    assert (
        b"Organization: Example Clinic; Accounts: Client portal accounts; Record an access review"
        in uses.content
    )
    assert f"Access review {review.number}".encode() in uses.content
    changes = client.get(reverse("audit_review:changes"))
    assert changes.status_code == 200
    assert b"Access review signed" in changes.content
    assert f"Access review {review.number}".encode() in changes.content
    assert b"Verify Friday." not in changes.content
    assert WorkspaceUse.objects.filter(source=Source.ACCESS_REVIEWS).count() == 6


def test_heads_redirects_and_unknown_reviews_leave_no_review_uses(client, reviewer, monkeypatch):
    review = sign(reviewer)
    create = reverse("access_review:review-create")
    assert client.get(create).status_code == 302
    client.force_login(reviewer)
    for name, ids in (
        ("review-create", []),
        ("reviews", []),
        ("reviews-export", []),
        ("review", [review.pk]),
        ("review-export", [review.pk]),
    ):
        response = client.head(reverse(f"access_review:{name}", args=ids))
        assert not response.content
        assert "no-store" in response["Cache-Control"]
    for name in ("review", "review-export"):
        assert client.get(reverse(f"access_review:{name}", args=[review.pk + 1])).status_code == 404
    monkeypatch.setattr(access_review_views, "EXPORT_LIMIT", 0)
    oversized = client.get(reverse("access_review:reviews-export"))
    assert oversized.status_code == 302 and "upto=" in oversized.url
    assert not WorkspaceUse.objects.filter(source=Source.ACCESS_REVIEWS).exists()


def test_review_page_or_file_is_withheld_if_its_workspace_use_fails(
    client,
    reviewer,
    monkeypatch,
    settings,
):
    review = sign(reviewer)
    client.force_login(reviewer)
    client.raise_request_exception = False
    settings.DEBUG = False

    def unavailable(*args, **kwargs):
        raise RuntimeError("Synthetic workspace write unavailable")

    monkeypatch.setattr(WorkspaceUse.objects, "bulk_create", unavailable)
    for name, ids in (
        ("review-create", []),
        ("reviews", []),
        ("reviews-export", []),
        ("review", [review.pk]),
        ("review-export", [review.pk]),
    ):
        response = client.get(reverse(f"access_review:{name}", args=ids))
        assert response.status_code == 500
        assert review.number.encode() not in response.content
        assert "Content-Disposition" not in response
        assert "no-store" in response["Cache-Control"]
    assert not WorkspaceUse.objects.filter(source=Source.ACCESS_REVIEWS).exists()


def test_private_source_fields_stay_out_of_access_review_responses_history_and_logs(
    client,
    reviewer,
    agent,
    make_ticket,
    clinic,
    caplog,
):
    ticket = make_ticket(clinic, "Synthetic review ticket 154")
    patient_value = "Synthetic patient marker 27451"
    template = RequestTemplate.objects.create(
        key="capture", name="Capture", category=ticket.category
    )
    field = template.fields.create(
        key="marker",
        label="Identifier",
        kind=FieldKind.SHORT_TEXT,
        audience=Audience.STAFF,
        holds_patient_information=True,
    )
    filled = FilledForm.objects.create(
        request=ticket,
        template=template,
        template_key=template.key,
        template_name=template.name,
        created_by=agent,
    )
    filled.values.create(
        field=field,
        key=field.key,
        label=field.label,
        kind=field.kind,
        audience=field.audience,
        holds_patient_information=True,
        position=1,
        value=patient_value,
        entered_by=agent,
    )
    totp, recovery = "SYNTHETIC-TOTP-154", "SYNTHETIC-RECOVERY-154"
    Authenticator.objects.create(user=agent, type="totp", data={"secret": totp})
    Authenticator.objects.create(user=agent, type="recovery_codes", data={"codes": [recovery]})
    client.force_login(reviewer)
    caplog.set_level(logging.INFO)
    caplog.clear()
    form_url = reverse("access_review:review-create")
    shown = client.get(form_url)
    assert shown.status_code == 200 and agent.username.encode() in shown.content
    form = posted(shown, decide="remove")
    wrong = "Synthetic-rejected-password-154"
    redrawn = client.post(form_url, {**form, "confirm_password": wrong})
    assert redrawn.status_code == 200 and b"Incorrect password" in redrawn.content
    saved = client.post(form_url, form)
    assert saved.status_code == 302
    review = AccessReview.objects.get()
    answers = [shown, redrawn, saved]
    for name, ids in (
        ("reviews", []),
        ("review", [review.pk]),
        ("reviews-export", []),
        ("review-export", [review.pk]),
    ):
        response = client.get(reverse(f"access_review:{name}", args=ids))
        assert response.status_code == 200 and review.number.encode() in response.content
        answers.append(response)
    assert all("no-store" in answer["Cache-Control"] for answer in answers)
    observed = [answer.content.decode("utf-8-sig") for answer in answers]
    observed += [
        json.dumps(
            list(
                WorkspaceUse.objects.filter(source=Source.ACCESS_REVIEWS).values("filters", "rows")
            )
        ),
        json.dumps(
            list(
                AuditEvent.objects.filter(action="access_review.signed").values(
                    "before", "after", "reason"
                )
            )
        ),
    ]
    assert len([r for r in caplog.records if r.name == "lab_ops.request"]) == len(answers)
    observed.extend(record.getMessage() for record in caplog.records)
    for private in (
        ticket.subject,
        ticket.number,
        patient_value,
        totp,
        recovery,
        wrong,
        PASSWORD,
        reviewer.email,
        agent.email,
        reviewer.password,
        agent.password,
    ):
        assert all(private not in part for part in observed)


def test_list_page_and_export_links_preserve_the_first_page_after_another_signing(
    client,
    reviewer,
):
    import csv
    import io
    import re
    from html import unescape
    from urllib.parse import urljoin

    list_url = reverse("access_review:reviews")
    client.force_login(reviewer)
    empty = client.get(list_url)
    assert b"No access review recorded yet" in empty.content
    assert reverse("access_review:reviews-export").encode() not in empty.content
    _grant_use(empty, reviewer, UseKind.VIEWED, {"page": "1"}, 0)
    signed = [sign(reviewer).number for _ in range(51)]
    first = client.get(list_url)
    _grant_use(first, reviewer, UseKind.VIEWED, {"page": "1"}, 51)
    assert [r.number for r in first.context["page_obj"]] == signed[:0:-1]
    match = re.search(r'href="([^"]+)" rel="next"', first.content.decode())
    assert match is not None
    next_url = urljoin(list_url, unescape(match[1]))
    export_url = first.context["export_url"]
    newest = sign(reviewer)
    second = client.get(next_url)
    _grant_use(second, reviewer, UseKind.VIEWED, {"page": "2"}, 51)
    assert f'href="/access-review/reviews/{newest.pk}/"'.encode() not in second.content
    assert [r.number for r in second.context["page_obj"]] == signed[:1]
    exported = client.get(export_url)
    _grant_use(exported, reviewer, UseKind.EXPORTED, {}, 51)
    rows = csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig")))
    assert [row["Access review"] for row in rows] == list(reversed(signed))
    updated = client.get(list_url)
    _grant_use(updated, reviewer, UseKind.VIEWED, {"page": "1"}, 52)
    assert updated.context["page_obj"][0].pk == newest.pk


def test_release_note_title_describes_the_result_of_signing_a_removal(
    client, reviewer, clinic, make_user
):
    portal = make_user("release-note-subject", client=clinic)
    client.force_login(reviewer)
    create = reverse("access_review:review-create")
    form = client.get(create, {"scope": "portal", "organization": clinic.pk})
    response = client.post(create, posted(form, decide="remove"))
    assert response.status_code == 302
    review = AccessReview.objects.get()
    assert review.removals == 1 and b"Still held" in client.get(response.url).content
    portal.refresh_from_db()
    assert portal.is_active and portal.client_id == clinic.pk
    notes = client.get(reverse("whats-new"))
    assert notes.status_code == 200 and b"On the new Reviews tab of Access review" in notes.content
    assert b"signed access review that keeps or removes each grant" not in notes.content


def test_email_address_promise_accounts_for_a_valid_email_as_username(client, reviewer, make_user):
    # The record is expected to preserve usernames. Form help should describe precisely which
    # account fields are omitted when the account's username is also its email address.
    subject = make_user("staff-review", "technical")
    subject.username = subject.email
    subject.full_clean()
    subject.save(update_fields=["username"])
    client.force_login(reviewer)
    create = reverse("access_review:review-create")
    form = client.get(create)
    saved = client.post(create, posted(form, decide="remove"))
    assert saved.status_code == 302
    record = AccessReview.objects.get()
    assert record.items.get(account_id=subject.pk).account_username == subject.email
    change = AuditEvent.objects.get(action="access_review.signed")
    assert subject.email in [r["account"] for r in change.after["removed"]]
    assert b"email addresses are never part of the record or the change" not in form.content


# Migration and demonstration data


def _move_review_schema(name):
    executor = MigrationExecutor(connection)
    target = [("audit_review", name)]
    executor.migrate(target)
    return executor.loader.project_state(target).apps


def _restore_review_schema():
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes("audit_review"))


@pytest.mark.django_db(transaction=True)
def test_upgrade_and_empty_reverse_preserve_previous_review_and_all_earlier_use_sources(reviewer):
    previous, new = "0004_access_report", "0005_access_reviews"
    try:
        old_apps = _move_review_schema(previous)
        old_uses = old_apps.get_model("audit_review", "WorkspaceUse")
        earlier = (
            "access",
            "changes",
            "sign_ins",
            "people",
            "uses",
            "reviews",
            "accounts",
            "unused",
        )
        for source in earlier:
            old_uses.objects.create(
                account_id=reviewer.pk,
                source=source,
                kind="viewed",
                filters={"page": "1"},
                rows=2,
                request_ref="old-release-page",
            )
        signed = sign_activity_review(reviewer, findings="Synthetic earlier review")
        original_review = Review.objects.values().get(pk=signed.pk)

        def use_rows():
            # xmin and ctid distinguish untouched rows from copies with the same column values.
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT u.*, u.xmin::text, u.ctid::text FROM audit_review_workspaceuse u "
                    "ORDER BY u.id"
                )
                return cursor.fetchall()

        original_uses = use_rows()
        _move_review_schema(new)
        assert not AccessReview.objects.exists()
        assert use_rows() == original_uses
        assert Review.objects.values().get(pk=signed.pk) == original_review
        _move_review_schema(previous)
        assert "audit_review_accessreview" not in connection.introspection.table_names()
        assert "audit_review_accessreviewitem" not in connection.introspection.table_names()
        assert use_rows() == original_uses
        assert Review.objects.values().get(pk=signed.pk) == original_review
    finally:
        _restore_review_schema()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("history", ["review", "use", "both"])
def test_migration_refuses_to_reverse_with_signoff_history_and_preserves_it(reviewer, history):
    if history in {"review", "both"}:
        signed = sign(reviewer)
        decisions = list(signed.items.values())
    if history in {"use", "both"}:
        use = WorkspaceUse.objects.create(
            account=reviewer, source="grant_review", kind="viewed", rows=0
        )
    try:
        with pytest.raises(DatabaseError, match="Access reviews or their uses are recorded"):
            _move_review_schema("0004_access_report")
        assert ("audit_review", "0005_access_reviews") in MigrationExecutor(
            connection
        ).loader.applied_migrations
        if history in {"review", "both"}:
            assert AccessReview.objects.get().checksum == signed.checksum
            assert list(AccessReview.objects.get().items.values()) == decisions
        if history in {"use", "both"}:
            assert WorkspaceUse.objects.get().pk == use.pk
    finally:
        _restore_review_schema()


def _run_signoff_demo(monkeypatch, command="seed_demo"):
    monkeypatch.setenv("DEMO_PASSWORD", "Synthetic demonstration password 754")
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    options = {}
    if command == "reset_demo":
        monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
        options["interactive"] = False
    call_command(command, stdout=StringIO(), stderr=StringIO(), **options)


@pytest.mark.django_db(transaction=True)
def test_seed_repeat_and_reset_preserve_one_documented_example_or_recreate_it(monkeypatch):
    def example():
        review = AccessReview.objects.get()
        assert review.scope == AccessScope.STAFF
        assert review.signer_username == "demo_supervisor"
        assert access_reviews.intact(review)
        assert abs(timezone.now() - review.signed_at - timedelta(days=3)) < timedelta(minutes=2)
        removal = review.items.get(decision=Decision.REMOVE)
        assert (removal.account_username, removal.label) == (
            "former_technician",
            "Technical staff role",
        )
        assert access_reviews.still_held([removal]) == {removal.pk: True}
        assert review.items.filter(decision=Decision.KEEP).count() == review.grants - 1
        assert AuditEvent.objects.get(action=access_reviews.SIGNED).object_id == str(review.pk)
        return review

    _run_signoff_demo(monkeypatch)
    original = example()
    _run_signoff_demo(monkeypatch)
    repeated = example()
    assert (repeated.pk, repeated.checksum) == (original.pk, original.checksum)
    sign(original.signed_by, findings="Synthetic prior sign-off to erase")
    assert AccessReview.objects.count() == 2
    _run_signoff_demo(monkeypatch, "reset_demo")
    recreated = example()
    assert recreated.signed_at > original.signed_at
    assert not AccessReview.objects.filter(findings="Synthetic prior sign-off to erase").exists()


@pytest.mark.django_db
def test_demo_address_collision_yields_no_former_technician_and_no_removals(monkeypatch):
    User.objects.create_user("different-person", email=seed.UNUSED_EMAIL)
    _run_signoff_demo(monkeypatch)
    assert not User.objects.filter(username=seed.UNUSED_USERNAME).exists()
    review = AccessReview.objects.get()
    assert review.removals == 0
    assert review.grants > 0
    assert review.grants == review.items.filter(decision=Decision.KEEP).count()
    assert review.follow_up == ""
    assert seed.UNUSED_USERNAME not in review.findings
    assert access_reviews.intact(review)


@pytest.mark.django_db
def test_demo_signs_no_removal_for_unrelated_account_using_examples_username(monkeypatch):
    existing = User.objects.create_user(
        seed.UNUSED_USERNAME,
        email="different-owner@example.invalid",
        last_login=timezone.now(),
    )
    group = Group.objects.create(name="Independently assigned")
    existing.groups.add(group)
    _run_signoff_demo(monkeypatch)
    review = AccessReview.objects.get()
    decision = review.items.get(account=existing, kind="group", code=str(group.pk))
    assert decision.decision == Decision.KEEP
    assert review.removals == 0
    assert seed.UNUSED_USERNAME not in review.findings


@pytest.mark.django_db
def test_demo_keeps_an_additional_grant_on_previously_seeded_technician(monkeypatch):
    # The previous release already populated the demonstration. Its only missing seed step was
    # the signed access review introduced in this release.
    with monkeypatch.context() as previous_release:
        previous_release.setattr(seed, "_access_review", lambda reviewer: None)
        _run_signoff_demo(previous_release)
    assert not AccessReview.objects.exists()
    account = User.objects.get(username=seed.UNUSED_USERNAME)
    technical = account.groups.get(name="technical")
    added = Group.objects.create(name="Specimen routing")
    account.groups.add(added)
    _run_signoff_demo(monkeypatch)
    review = AccessReview.objects.get()
    assert dict(review.items.filter(account=account).values_list("code", "decision")) == {
        str(technical.pk): Decision.REMOVE,
        str(added.pk): Decision.KEEP,
    }
