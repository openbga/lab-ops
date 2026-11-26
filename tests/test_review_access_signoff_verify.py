"""Regression checks for access sign-off verification, using synthetic records."""

import csv
import hashlib
import io
import json
import re
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.urls import reverse
from django.utils import timezone

from accounts.acting import ACCESS_GATE, INT4
from accounts.models import User
from accounts.tests.test_admin import add_user
from audit_review import access_report, access_reviews, seed
from audit_review.access_review_forms import AccessReviewForm
from audit_review.models import AccessReview, AccessReviewItem, AccessScope, Decision, WorkspaceUse
from audit_review.tests.conftest import PASSWORD, review_permission
from audit_review.tests.conftest import agent as agent
from audit_review.tests.conftest import clinic as clinic
from audit_review.tests.conftest import make_user as make_user
from audit_review.tests.conftest import reviewer as reviewer
from audit_review.tests.conftest import water as water
from audit_review.tests.test_access_report import account_change, group_change, http_for
from audit_review.tests.test_access_reviews import form_page, patient_permission, posted, sign
from clients.models import Client, ClientSite, PortalAccess
from clients.services import update_client
from core.changelog import entries
from core.models import AuditEvent
from nonconformance.seed import happened_at
from tests.test_review_access_signoff import (
    on_database_connection,
    signing_arguments,
    waits_for_this_transaction,
)

# Signing time, organization labels and access gates


def test_signing_time_follows_the_completed_read_of_the_signers_roles(reviewer, monkeypatch):
    reviewer.groups.add(Group.objects.get_or_create(name="technical")[0])
    read_roles = access_reviews.roles_label
    observed = []

    def observe_roles(signer):
        labels = read_roles(signer)
        observed.append((timezone.now(), labels))
        return labels

    monkeypatch.setattr(access_reviews, "roles_label", observe_roles)
    review = sign(reviewer)
    review.refresh_from_db()
    assert len(observed) == 1
    read_at, labels = observed[0]
    assert labels == review.signer_roles == "Supervisor, Technical staff"
    assert read_at <= review.signed_at <= timezone.now()
    event = AuditEvent.objects.get(action=access_reviews.SIGNED, object_id=str(review.pk))
    assert event.after["signer_roles"] == labels
    assert event.after["signed_at"] == review.signed_at.isoformat()
    assert access_reviews.intact(review)


@pytest.mark.parametrize("one_organization", [True, False], ids=["one", "every"])
def test_multiple_portal_decisions_keep_their_own_organization_and_the_correct_heading(
    client, reviewer, make_user, clinic, water, one_organization
):
    manager = make_user("directory-editor", "account_manager")
    make_user("first-clinic-contact", client=clinic)
    make_user("second-clinic-contact", client=clinic)
    make_user("water-contact", client=water)
    old_name = clinic.name
    updated = update_client(
        actor=manager,
        client_id=clinic.pk,
        expected_revision=clinic.revision,
        reason="New directory name",
        name="Regional Sample Clinic",
    )
    # Callers may still have a selection with the previous name. Its id selects the scope.
    selected = clinic if one_organization else None
    review = sign(reviewer, AccessScope.PORTAL, organization=selected)
    expected = {"first-clinic-contact": updated.name, "second-clinic-contact": updated.name}
    if not one_organization:
        expected["water-contact"] = water.name
    heading = updated.name if one_organization else ""
    assert (review.organization_id, review.organization_name) == (
        clinic.pk if one_organization else None,
        heading,
    )
    assert dict(review.items.values_list("account_username", "organization_name")) == expected
    event = AuditEvent.objects.get(action=access_reviews.SIGNED)
    assert event.after["organization"] == heading
    client.force_login(reviewer)
    response = client.get(reverse("access_review:review-export", args=[review.pk]))
    assert response.status_code == 200
    rows = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")))
    assert {r["Username"]: r["Client organization"] for r in rows} == expected
    assert old_name.encode() not in response.content
    assert access_reviews.intact(review)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "population", ["unchanged", "newly-eligible-staff", "newly-in-scope-portal"]
)
def test_every_account_in_the_accepted_inventory_has_a_gate_until_commit(
    reviewer, make_user, clinic, water, monkeypatch, population
):
    """Exercise the service contract for two different enumerations of the same scope.

    Supply two fixed database readings at the account-selector boundary. Inspect the actual
    PostgreSQL locks without modifying any account during signing. The final form may be
    refused, or each account that the service accepts must have its gate.
    """
    portal = population == "newly-in-scope-portal"
    scope = AccessScope.PORTAL if portal else AccessScope.STAFF
    organization = clinic if portal else None
    subject = make_user("directory-subject", client=water if portal else None)
    group = Group.objects.create(name="Sample directory group")
    if population == "unchanged":
        subject.groups.add(group)
    scope_accounts = access_reviews.scope_accounts
    first_population = list(scope_accounts(scope, organization).values_list("pk", flat=True))
    if portal:
        subject.client = clinic
        subject.changed_by = reviewer
        subject.save(update_fields=["client"])
    elif population == "newly-eligible-staff":
        subject.groups.add(group)
    args = signing_arguments(reviewer, scope, organization)
    assert any(k.startswith(f"{subject.pk}:") for k in args["decisions"])

    # The form and final read agree. Only discovery has the earlier population.
    first_call = True

    def scope_at_read(requested_scope, requested_organization=None):
        nonlocal first_call
        rows = scope_accounts(requested_scope, requested_organization)
        if first_call:
            first_call = False
            return rows.filter(pk__in=first_population)
        return rows

    monkeypatch.setattr(access_reviews, "scope_accounts", scope_at_read)
    with transaction.atomic():
        try:
            review = access_reviews.sign_access_review(**args)
        except ValidationError as exc:
            assert access_reviews.STALE in exc.messages
            assert not AccessReview.objects.exists()
            return
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT objid FROM pg_locks WHERE pid = pg_backend_pid() "
                "AND locktype = 'advisory' AND classid = %s AND objsubid = 2 AND granted",
                [ACCESS_GATE],
            )
            held_keys = {row[0] for row in cursor.fetchall()}
        required = {pk % INT4 for pk in review.items.values_list("account_id", flat=True)}
        required.add(reviewer.pk % INT4)
        assert not required - held_keys, f"Missing recorded account gate(s): {required - held_keys}"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("operation", ["addition", "replacement"])
def test_group_edits_wait_when_a_capability_is_removed_in_the_same_save(
    reviewer, make_user, operation
):
    owner = http_for(make_user("directory-owner", is_superuser=True, is_staff=True))
    members = [make_user("morning"), make_user("afternoon")]
    group = Group.objects.create(name="Sample coordination")
    for member in members:
        member.groups.add(group)
    replaces = operation == "replacement"
    if replaces:
        group.permissions.add(review_permission())
    desired = patient_permission()

    def save_group():
        return group_change(owner, group, permissions=[desired.pk]).status_code

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            review = sign(reviewer)
            pid = Future()
            saved = pool.submit(on_database_connection, pid, save_group)
            waited = waits_for_this_transaction(pid.result(5), saved)
        assert saved.result(10) == 302

    assert set(group.permissions.values_list("pk", flat=True)) == {desired.pk}
    previous = review.items.filter(kind="group", code=str(group.pk))
    expected_detail = "Gives Can review access and audit records" if replaces else ""
    assert dict(previous.values_list("account_id", "detail")) == {
        member.pk: expected_detail for member in members
    }
    assert access_reviews.intact(review)
    assert waited == replaces


# Grant eligibility and compatibility with the earlier signing format


@pytest.mark.parametrize("change", ["rename", "replace-with-namesake"])
def test_site_digest_distinguishes_site_identity_from_displayed_words(
    client, reviewer, make_user, clinic, change
):
    member = make_user("scoped", client=clinic)
    access = PortalAccess.objects.create(user=member, client=clinic)
    east = ClientSite.objects.create(client=clinic, name="East")
    west = ClientSite.objects.create(client=clinic, name="West")
    access.sites.add(west, east)
    client.force_login(reviewer)
    query = f"?scope=portal&organization={clinic.pk}"
    initial = form_page(client, query)
    (first,) = initial.context["reading"].grants
    assert first.detail == "Tickets of East, West, and those it raised"
    assert re.fullmatch(rf"{clinic.pk}:sites:[0-9a-f]{{32}}", first.code)
    access.sites.clear()
    access.sites.add(east, west)
    reread = access_reviews.reading(AccessScope.PORTAL, clinic)
    assert reread.grants == [first]
    assert reread.fingerprint == initial.context["reading"].fingerprint
    if change == "rename":
        east.name = "Yard"
        east.save(update_fields=["name"])
    else:
        namesake = ClientSite.objects.create(client=clinic, name=east.name)
        access.sites.set([west, namesake])

    response = client.post(reverse("access_review:review-create"), posted(initial, Decision.REMOVE))
    assert response.status_code == 200
    assert not AccessReview.objects.exists()
    (now,) = response.context["reading"].grants
    if change == "rename":
        assert now.code == first.code
        assert now.detail == "Tickets of West, Yard, and those it raised"
    else:
        assert now.code != first.code
        assert now.detail == first.detail
    assert response.context["form"].changed == {now.field_name}
    assert response.context["undecided"] == 1
    result = client.post(reverse("access_review:review-create"), posted(response, Decision.REMOVE))
    assert result.status_code == 302
    review = AccessReview.objects.get()
    item = review.items.get()
    assert (item.code, item.detail, item.decision) == (now.code, now.detail, Decision.REMOVE)
    access.sites.clear()
    _assert_site_removal(client, review, item, True)
    member.is_active = False
    member.save(update_fields=["is_active"])
    _assert_site_removal(client, review, item, False)


def _assert_site_removal(client, review, item, held):
    detail = client.get(reverse("access_review:review", args=[review.pk]))
    assert detail.status_code == 200 and detail.context["intact"]
    assert detail.context["removals"] == [(item, held)]
    assert item.detail in detail.content.decode()
    response = client.get(reverse("access_review:review-export", args=[review.pk]))
    assert response.status_code == 200
    (row,) = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert (row["Client organization"], row["Detail"], row["Held now"]) == (
        item.organization_name,
        item.detail,
        "Yes" if held else "No",
    )


def _old_form_hash(values):
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_form_open_at_upgrade_redraws_legacy_site_code_and_retains_the_other_decision(
    client, reviewer, make_user, clinic
):
    sited = make_user("a-sited", client=clinic)
    own = make_user("b-own", client=clinic)
    access = PortalAccess.objects.create(user=sited, client=clinic)
    site = ClientSite.objects.create(client=clinic, name="Hill")
    access.sites.add(site)
    # 6f65048 used the raw site ids in the grant code, and hashed just the ordered grant rows.
    old_rows = [
        [
            sited.pk,
            sited.username,
            sited.username,
            clinic.name,
            "portal",
            f"{clinic.pk}:{site.pk}",
            "Portal account",
            "Tickets of Hill, and those it raised",
        ],
        [
            own.pk,
            own.username,
            own.username,
            clinic.name,
            "portal",
            f"{clinic.pk}:none",
            "Portal account",
            "Only the tickets it raised",
        ],
    ]
    old_names = [f"d-{_old_form_hash(row)[:24]}" for row in old_rows]
    client.force_login(reviewer)
    initial = form_page(client, f"?scope=portal&organization={clinic.pk}")
    body = {k: v for k, v in posted(initial).items() if not k.startswith("d-")}
    body.update(dict.fromkeys(old_names, Decision.REMOVE))
    body.update(
        expected=_old_form_hash(old_rows), shown=",".join(old_names), findings="Retire old access"
    )
    url = reverse("access_review:review-create")
    response = client.post(url, body)
    assert response.status_code == 200 and not AccessReview.objects.exists()
    form = response.context["form"]
    current_site, current_own = form.grants
    assert (current_site.account_id, current_own.account_id) == (sited.pk, own.pk)
    assert form.changed == {current_site.field_name}
    assert form[current_site.field_name].value() is None
    assert form[current_own.field_name].value() == Decision.REMOVE
    assert form["findings"].value() == "Retire old access"
    assert form["expected"].value() != body["expected"]
    submitted = posted(response, findings=form["findings"].value())
    submitted[current_site.field_name] = Decision.KEEP
    submitted[current_own.field_name] = Decision.REMOVE
    assert client.post(url, submitted).status_code == 302
    assert list(
        AccessReview.objects.get().items.order_by("position").values_list("account_id", "decision")
    ) == [(sited.pk, Decision.KEEP), (own.pk, Decision.REMOVE)]


def test_legacy_portal_site_row_keeps_checksum_snapshot_and_current_removal_status(
    client, reviewer, water
):
    # Fixed example in the 6f65048 signing format, including its SHA-256 and comma-separated ids.
    organization = Client.objects.create(pk=8007, name="Planning Clinic", account_number="PLAN")
    member = User(
        pk=8011,
        username="rowan",
        first_name="Rowan",
        email="rowan@example.invalid",
        client=organization,
    )
    User.objects.bulk_create([member])
    review = AccessReview.objects.create(
        pk=81,
        number="AC-000081",
        scope="portal",
        organization=organization,
        organization_name="Planning Clinic",
        accounts=1,
        grants=1,
        removals=1,
        signed_by_id=8123,
        signer_name="Prior Reviewer",
        signer_username="prior",
        signer_roles="Supervisor",
        signed_at=datetime(2026, 11, 24, 14, 5, tzinfo=UTC),
        checksum="6e316652ee9cae6287267514ec026dfdf9b345edb3934f80211c15083c66f861",
    )
    old_detail = "Tickets of Cedar, Oak, and those it raised"
    item = AccessReviewItem.objects.create(
        review=review,
        account=member,
        position=1,
        account_name="Rowan",
        account_username="rowan",
        organization_name="Planning Clinic",
        kind="portal",
        code="8007:12,19",
        label="Portal account",
        detail=old_detail,
        decision=Decision.REMOVE,
    )
    client.force_login(reviewer)
    _assert_site_removal(client, review, item, True)
    member.client = water
    member.changed_by = reviewer
    member.save(update_fields=["client"])
    _assert_site_removal(client, review, item, False)
    assert AccessReviewItem.objects.get(pk=item.pk).code == "8007:12,19"


def test_mixed_grants_count_one_person_each_and_only_reviewable_permissions_count(
    reviewer, make_user, clinic
):
    console = User.objects.bulk_create(
        [
            User(username=f"console-{i}", email=f"console-{i}@example.invalid", is_staff=True)
            for i in range(496)
        ]
    )
    super_only = make_user("owner-flag", is_superuser=True)
    group_only = make_user("member")
    direct_only = make_user("direct")
    empty = make_user("unrelated")
    make_user("inactive-console", is_active=False, is_staff=True)
    portal = make_user("portal", client=clinic)
    shadow = Permission.objects.create(
        content_type=ContentType.objects.get_by_natural_key("clients", "client"),
        codename="review_audit_records",
        name="Synthetic unrelated permission",
    )
    ordinary = Permission.objects.get(content_type__app_label="accounts", codename="view_user")
    empty.user_permissions.add(shadow, ordinary)
    group = Group.objects.create(name="Custom group")
    group.permissions.add(shadow)
    group_only.groups.add(group)
    direct_only.user_permissions.add(patient_permission())
    many = console[0]
    many.groups.add(group, Group.objects.create(name="Second group"))
    many.user_permissions.add(patient_permission(), review_permission(), ordinary)
    expected = {u.pk for u in [*console, super_only, group_only, direct_only, reviewer]}
    assert len(expected) == 500
    actual = list(access_reviews.scope_accounts(AccessScope.STAFF).values_list("pk", flat=True))
    assert len(actual) == 500 and set(actual) == expected
    reading = access_reviews.reading(AccessScope.STAFF)
    assert not reading.too_many and reading.accounts == 500
    assert [(g.kind, g.detail) for g in reading.grants if g.account_id == group_only.pk] == [
        ("group", "")
    ]
    assert {g.kind for g in reading.grants if g.account_id == super_only.pk} == {"console"}
    review = sign(reviewer)
    assert (review.accounts, review.grants) == (500, 505)
    assert set(review.items.values_list("account_id", flat=True)) == expected
    assert [g.account_id for g in access_reviews.reading(AccessScope.PORTAL).grants] == [portal.pk]
    empty.user_permissions.add(review_permission())
    assert access_reviews.reading(AccessScope.STAFF).too_many
    with pytest.raises(ValidationError) as refused:
        sign(reviewer)
    assert access_reviews.TOO_MANY in refused.value.messages
    assert AccessReview.objects.count() == 1
    empty.user_permissions.remove(review_permission())
    assert access_reviews.reading(AccessScope.STAFF).fingerprint == reading.fingerprint


# Scope, decisions and redraws


def _commit_after_password_check(monkeypatch, operation):
    """Commit a normal data change on another connection between the view and signing service."""
    original = AccessReviewForm.check_password

    def commit_change():
        try:
            operation()
        finally:
            connections.close_all()

    def check_and_change(form):
        assert original(form)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(commit_change).result(timeout=15)
        return True

    monkeypatch.setattr(AccessReviewForm, "check_password", check_and_change)


@pytest.mark.parametrize("originally_scoped", [True, False], ids=["one-to-all", "all-to-one"])
def test_scope_change_with_the_same_grants_requires_a_redraw_before_signing(
    client, reviewer, make_user, clinic, originally_scoped
):
    make_user("only-contact", client=clinic)
    client.force_login(reviewer)
    url = reverse("access_review:review-create")
    every = "?scope=portal"
    one = f"{every}&organization={clinic.pk}"
    original, changed = (one, every) if originally_scoped else (every, one)
    form_to_send = posted(form_page(client, original), decide=Decision.REMOVE)
    current = posted(form_page(client, changed))
    assert form_to_send["shown"] == current["shown"]
    form_to_send["organization"] = current["organization"]
    refused = client.post(url, form_to_send)
    assert refused.status_code == 200
    assert not AccessReview.objects.exists()
    form = refused.context["form"]
    assert form.non_field_errors() == [access_reviews.STALE]
    assert not form.changed
    assert form[form_to_send["shown"]].value() == Decision.REMOVE
    assert form["confirm_password"].value() is None
    assert refused.context["undecided"] == 0
    retry = posted(refused)
    assert all(
        retry[name] == current[name] for name in ("expected", "shown", "scope", "organization")
    )
    retry[retry["shown"]] = form[retry["shown"]].value()
    assert client.post(url, retry).status_code == 302
    review = AccessReview.objects.get()
    assert review.scope == AccessScope.PORTAL
    assert review.organization_id == (None if originally_scoped else clinic.pk)
    assert review.items.get().decision == Decision.REMOVE


def test_invalid_portal_choice_redraws_staff_with_fresh_decisions_and_can_be_signed(
    client, reviewer, make_user, clinic
):
    make_user("contact", client=clinic)
    client.force_login(reviewer)
    url = reverse("access_review:review-create")
    post = posted(
        form_page(client, f"?scope=portal&organization={clinic.pk}"), decide=Decision.REMOVE
    )
    post["scope"] = AccessScope.STAFF  # the still-selected organization is invalid for staff
    refused = client.post(url, post)
    assert refused.status_code == 200 and not AccessReview.objects.exists()
    assert refused.context["choice"].has_error("organization")
    assert b"were not accepted, so it now lists the staff accounts" in refused.content
    form = refused.context["form"]
    fields = {grant.field_name for grant in form.grants}
    assert len(fields) == 2 and form.changed == fields
    assert all(form[name].value() is None for name in fields)
    assert form["confirm_password"].value() is None
    assert b"2 grants still need a decision." in refused.content
    retry, staff = posted(refused), posted(form_page(client))
    assert all(
        retry[name] == staff[name] for name in ("expected", "shown", "scope", "organization")
    )
    assert client.post(url, retry).status_code == 302
    review = AccessReview.objects.get()
    assert (review.scope, review.organization_id, review.removals) == (AccessScope.STAFF, None, 0)
    assert set(review.items.values_list("account_id", flat=True)) == {reviewer.pk}


def test_a_stale_form_counts_new_and_previously_undecided_grants(client, reviewer, agent):
    client.force_login(reviewer)
    opened = form_page(client)
    data = posted(opened)
    grants = opened.context["reading"].grants
    keep, omitted = [grant for grant in grants if grant.account_id == reviewer.pk]
    agent_grant = next(grant for grant in grants if grant.account_id == agent.pk)
    data[keep.field_name] = Decision.REMOVE
    del data[omitted.field_name]
    data[agent_grant.field_name] = "Undecided"
    group = Group.objects.create(name="Specimen receiving")
    agent.groups.add(group)
    refused = client.post(reverse("access_review:review-create"), data)
    assert refused.status_code == 200 and not AccessReview.objects.exists()
    form = refused.context["form"]
    added = next(grant for grant in form.grants if grant.code == str(group.pk))
    assert form.changed == {added.field_name}
    assert form[keep.field_name].value() == Decision.REMOVE
    assert form[omitted.field_name].value() is None and form[added.field_name].value() is None
    assert b"3 grants still need a decision." in refused.content
    assert refused.context["undecided"] == 3


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("limit", ["accounts", "grants"])
def test_scope_growing_over_the_limit_during_post_shows_the_limit_without_signing_controls(
    client, reviewer, make_user, clinic, monkeypatch, limit
):
    if limit == "accounts":
        make_user("first-contact", client=clinic)
        query = f"?scope=portal&organization={clinic.pk}"
        User.objects.bulk_create(
            [
                User(username=f"contact-{n}", email=f"contact-{n}@example.invalid", client=clinic)
                for n in range(499)
            ]
        )

        def increase():
            User.objects.create_user(
                "another-contact", "another-contact@example.invalid", client=clinic
            )

        listed = 500
    else:
        # The reviewer holds two grants. The other 898 belong to one staff account.
        target = make_user("directory-staff")
        query = "?scope=staff"
        groups = Group.objects.bulk_create([Group(name=f"Grant group {n}") for n in range(898)])
        target.groups.add(*groups)

        def increase():
            target.groups.add(Group.objects.create(name="Another grant group"))

        listed = 900
    client.force_login(reviewer)
    url = reverse("access_review:review-create")
    opened = form_page(client, query)
    assert len(opened.context["reading"].grants) == listed
    _commit_after_password_check(monkeypatch, increase)
    refused = client.post(url, posted(opened))
    assert refused.status_code == 200 and not AccessReview.objects.exists()
    assert not AuditEvent.objects.filter(action=access_reviews.SIGNED).exists()
    fresh = form_page(client, query)
    assert fresh.context["reading"].too_many
    assert b'name="confirm_password"' not in fresh.content
    assert b'name="confirm_password"' not in refused.content, (
        "The POST still displayed a signing form"
    )
    assert b"Too many to review at once" in refused.content


@pytest.mark.django_db(transaction=True)
def test_scope_heading_and_grants_use_the_same_organization_name_on_a_stale_redraw(
    client, reviewer, make_user, clinic, monkeypatch
):
    make_user("contact", client=clinic)
    manager = make_user("manager", "account_manager")
    client.force_login(reviewer)
    query = f"?scope=portal&organization={clinic.pk}"
    data = posted(form_page(client, query), decide=Decision.REMOVE)
    revised_name = "Revised Example Clinic"

    def rename():
        update_client(
            actor=manager,
            client_id=clinic.pk,
            expected_revision=clinic.revision,
            reason="Updated directory name",
            name=revised_name,
        )

    _commit_after_password_check(monkeypatch, rename)
    refused = client.post(reverse("access_review:review-create"), data)
    assert refused.status_code == 200 and not AccessReview.objects.exists()
    form = refused.context["form"]
    assert form.non_field_errors() == [access_reviews.STALE]
    assert {grant.organization_name for grant in form.grants} == {revised_name}
    assert form.changed == {grant.field_name for grant in form.grants}
    assert refused.context["undecided"] == 1
    summary = re.search(r"<p[^>]*>(1 account and 1 grant of .*?)</p>", refused.content.decode())
    assert summary and revised_name in summary.group(1), (
        summary.group(1) if summary else "No scope summary"
    )


# The demonstration account


@pytest.mark.django_db
def test_demo_keeps_technical_role_on_old_passwordless_account_created_in_admin(
    make_user, client, monkeypatch
):
    owner = make_user("independent-owner", "admin", is_superuser=True, is_staff=True)
    technical = Group.objects.get_or_create(name="technical")[0]
    client.force_login(owner)
    response = add_user(
        client,
        username=seed.UNUSED_USERNAME,
        email="another-technician@example.invalid",
        usable_password="false",
        password1="",
        password2="",
        groups=[technical.pk],
        confirm_password=PASSWORD,
    )
    assert response.status_code == 302
    account = User.objects.get(username=seed.UNUSED_USERNAME)
    assert not account.has_usable_password() and account.last_login is None
    creation = AuditEvent.objects.get(
        action="account.created", object_type="accounts.user", object_id=str(account.pk)
    )
    assert creation.actor == owner and creation.reason == "Django admin"

    # This install already has the administrator's account when the example first runs.
    monkeypatch.setenv("DEMO_PASSWORD", "Synthetic demonstration password 754")
    with happened_at(account.date_joined + timedelta(days=130)):
        call_command("seed_demo", stdout=StringIO(), stderr=StringIO())
    review = AccessReview.objects.get()
    role = review.items.get(account=account, kind="group", code=str(technical.pk))
    assert role.decision == Decision.KEEP
    assert review.removals == 0
    assert review.follow_up == ""
    assert seed.UNUSED_USERNAME not in review.findings


def _original_demo_technician(reviewer, make_user):
    make_user("demo_admin", "admin", is_superuser=True, is_staff=True)
    Group.objects.get_or_create(name="technical")
    seed._unused(reviewer)
    account = User.objects.get(username=seed.UNUSED_USERNAME)
    creation = AuditEvent.objects.get(action="account.created", object_id=str(account.pk))
    assert creation.reason == "Demonstration seed: a technician who no longer signs in"
    return account


@pytest.mark.django_db
@pytest.mark.parametrize("activity", ["last_login", "date_joined"])
def test_demo_removal_and_unused_report_use_the_same_exclusive_cutoff(
    reviewer, make_user, monkeypatch, activity
):
    account = _original_demo_technician(reviewer, make_user)
    if activity == "date_joined":
        account.last_login = None
        account.save(update_fields=["last_login"])
    grants = access_reviews.reading(AccessScope.STAFF).grants
    last_activity = getattr(account, activity)
    at_boundary = last_activity + timedelta(days=seed.UNUSED_DAYS)
    monkeypatch.setattr(timezone, "now", lambda: at_boundary)
    assert not access_report.unused_accounts(seed.UNUSED_DAYS, {}).filter(pk=account.pk).exists()
    assert seed._example_role(grants) == set()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("days_at_command", "password_enabled", "expected"),
    [(92, False, Decision.KEEP), (94, False, Decision.REMOVE), (110, True, Decision.KEEP)],
)
def test_example_checks_password_and_inactivity_at_backdated_reading(
    reviewer, make_user, monkeypatch, days_at_command, password_enabled, expected
):
    account = _original_demo_technician(reviewer, make_user)
    if password_enabled:
        account.set_password("Synthetic reactivated example password 432")
        account.save(update_fields=["password"])
    command_time = account.last_login + timedelta(days=days_at_command)
    monkeypatch.setattr(timezone, "now", lambda: command_time)
    seed._access_review(reviewer)
    review = AccessReview.objects.get()
    assert (review.signed_at - account.last_login).days == days_at_command - 3
    role = review.items.get(account=account)
    assert (role.label, role.decision) == ("Technical staff role", expected)
    assert review.removals == int(expected == Decision.REMOVE)
    assert access_reviews.intact(review)


# Signed names, release notes and page help


def test_signed_usernames_are_preserved_separately_from_contact_email(client, reviewer, make_user):
    subject = make_user("second-account", "technical", first_name="Taylor")
    signer_handle = "signer-handle@login.example.invalid"
    member_handle = "technician-handle@login.example.invalid"
    for account, username in ((reviewer, signer_handle), (subject, member_handle)):
        account.username = username
        account.full_clean()
        account.save(update_fields=["username"])
        assert account.email != account.username

    client.force_login(reviewer)
    create = reverse("access_review:review-create")
    form = client.get(create)
    assert form.status_code == 200
    assert b"no email address field is copied" in form.content
    assert signer_handle.encode() in form.content and member_handle.encode() in form.content
    response = client.post(create, posted(form, decide="remove"))
    assert response.status_code == 302
    record = AccessReview.objects.get()
    item = record.items.get(account_id=subject.pk)
    assert item.account_username == member_handle
    assert record.signer_username == signer_handle
    change = AuditEvent.objects.get(action=access_reviews.SIGNED)
    assert change.after["signer_username"] == signer_handle
    assert member_handle in {removal["account"] for removal in change.after["removed"]}

    original_contacts = [reviewer.email, subject.email]
    for account, suffix in ((reviewer, "signer"), (subject, "member")):
        account.username = f"changed-{suffix}"
        account.email = f"new-contact-{suffix}@example.invalid"
        account.full_clean()
        account.save(update_fields=["username", "email"])
    detail = client.get(response.url)
    listing = client.get(reverse("access_review:reviews"))
    decision_file = client.get(reverse("access_review:review-export", args=[record.pk]))
    list_file = client.get(reverse("access_review:reviews-export"))
    changes = client.get(reverse("audit_review:changes"))
    uses = client.get(reverse("audit_review:uses"))
    answers = [detail, listing, decision_file, list_file, changes, uses]
    assert all(answer.status_code == 200 for answer in answers)
    assert access_reviews.intact(record)
    assert signer_handle.encode() in detail.content and member_handle.encode() in detail.content
    signed_rows = list(csv.DictReader(io.StringIO(list_file.content.decode("utf-8-sig"))))
    assert [(r["Access review"], r["Username"]) for r in signed_rows] == [
        (record.number, signer_handle)
    ]
    grant_rows = list(csv.DictReader(io.StringIO(decision_file.content.decode("utf-8-sig"))))
    assert {r["Username"] for r in grant_rows} == {signer_handle, member_handle}
    assert record.number.encode() in changes.content and record.number.encode() in uses.content
    aggregate = [form.content.decode(), *(answer.content.decode("utf-8-sig") for answer in answers)]
    aggregate.append(json.dumps(change.after))
    aggregate.append(json.dumps(list(WorkspaceUse.objects.values_list("filters", flat=True))))
    for email in [*original_contacts, reviewer.email, subject.email]:
        assert all(email not in surface for surface in aggregate)


def test_decision_title_and_summary_agree_across_both_release_note_surfaces(client, reviewer):
    title = "Record a signed access review with a keep or remove decision for each grant"
    entry = next(e for e in entries() if e.id == "2026-11-26-access-review-signoff")
    assert entry.title == title
    assert (
        "decide Keep or Remove" in entry.summary
        and "which removals are still held" in entry.summary
    )
    client.force_login(reviewer)
    notes = client.get(reverse("whats-new"))
    assert notes.status_code == 200
    markdown = (Path(settings.BASE_DIR) / "CHANGELOG.md").read_text()
    for surface in (notes.content.decode(), markdown):
        assert entry.title in surface and entry.summary in surface


@pytest.mark.django_db(transaction=True)
def test_signoff_page_and_help_describe_when_directory_labels_were_read(
    client, reviewer, clinic, make_user, monkeypatch
):
    make_user("one-contact", client=clinic)
    manager = make_user("directory-editor", "account_manager")
    client.force_login(reviewer)
    create = reverse("access_review:review-create")
    form = client.get(create, {"scope": "portal", "organization": clinic.pk})
    assert form.status_code == 200 and clinic.name.encode() in form.content
    old_name, new_name = clinic.name, "Renamed clinic for review verification"

    def rename():
        update_client(
            actor=manager,
            client_id=clinic.pk,
            expected_revision=clinic.revision,
            reason="Routine name correction",
            name=new_name,
        )
        return timezone.now()

    original_roles = access_reviews.roles_label
    pid = Future()
    with ThreadPoolExecutor(max_workers=1) as pool:

        def rename_after_reading(signer):
            nonlocal directory_save
            directory_save = pool.submit(on_database_connection, pid, rename)
            waits_for_this_transaction(pid.result(5), directory_save)
            return original_roles(signer)

        directory_save = None
        monkeypatch.setattr(access_reviews, "roles_label", rename_after_reading)
        result = client.post(create, posted(form))
        assert directory_save is not None
        renamed_at = directory_save.result(10)

    if result.status_code == 200:
        assert not AccessReview.objects.exists()
        assert new_name.encode() in result.content
        return
    assert result.status_code == 302
    review = AccessReview.objects.get()
    clinic.refresh_from_db()
    assert clinic.name == new_name
    if renamed_at > review.signed_at or review.organization_name == new_name:
        # A later implementation may hold the directory name or read it again at signing.
        return
    assert review.organization_name == review.items.get().organization_name == old_name
    detail = client.get(result.url)
    assert detail.status_code == 200 and old_name.encode() in detail.content
    promises = (
        "The grants were read as they stood at the moment of signing.",
        "The grants were read as they stood at that moment.",
    )
    unsupported = [p for p in promises if p.encode() in detail.content]
    assert not unsupported, (
        f"The rename committed before signed_at; the page still says {unsupported}"
    )


def test_adding_a_first_group_in_the_admin_requires_a_fresh_decision(client, reviewer, make_user):
    target = make_user("new-group-member")
    group = Group.objects.create(name="Sample catalog")
    admin = http_for(make_user("catalog-owner", is_staff=True, is_superuser=True))
    client.force_login(reviewer)
    url = reverse("access_review:review-create")
    opened = form_page(client)
    assert all(g.account_id != target.pk for g in opened.context["reading"].grants)
    assert account_change(admin, target, groups=[group.pk]).status_code == 302
    redrawn = client.post(url, posted(opened))
    assert redrawn.status_code == 200 and not AccessReview.objects.exists()
    form = redrawn.context["form"]
    (grant,) = [g for g in form.grants if g.account_id == target.pk]
    assert form.changed == {grant.field_name}
    assert form[grant.field_name].value() is None
    assert redrawn.context["undecided"] == 1
    accepted = client.post(url, posted(redrawn))
    assert accepted.status_code == 302
    signed = AccessReview.objects.get()
    assert signed.items.get(account_id=target.pk).code == str(group.pk)
    assert access_reviews.intact(signed)
