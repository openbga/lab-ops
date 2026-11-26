"""Access review confirmation with synthetic records written by the application."""

import csv
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue
from threading import Event
from uuid import UUID

import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.urls import reverse
from django.utils import timezone
from test_review_access_report_verify import group_create, group_save, in_own_connection, signed_in

from accounts import admin as account_admin
from accounts import setup
from accounts.models import User
from accounts.tests.test_confirm_identity import account_change
from audit_review.tests.conftest import make_user as make_user
from audit_review.tests.conftest import review_permission
from audit_review.tests.conftest import reviewer as reviewer
from core.models import AuditEvent

pytestmark = pytest.mark.django_db
PASSWORD = "Four polished stones on the library stairs 97"


def account_rows(http, person, filters=None):
    filters = filters or {}
    page = http.get(reverse("access_review:accounts"), filters)
    sheet = http.get(reverse("access_review:accounts-export"), filters)
    assert page.status_code == sheet.status_code == 200
    shown = next(row for row in page.context["page_obj"] if row.username == person.username)
    records = csv.DictReader(io.StringIO(sheet.content.decode("utf-8-sig")))
    return shown, next(record for record in records if record["Username"] == person.username)


# Adapted in the third confirmation round: changes are replayed in the order of their recorded time
# (audit_review/access_report.py; docs/architecture.md, Access review), a documented limit when
# two are recorded at the same moment or by a clock that runs behind; kept as strict expected
# failures.
OUT_OF_ORDER = pytest.mark.xfail(
    strict=True,
    reason="Known limit: changes recorded at the same moment, or by a clock behind, can be "
    "replayed in another order than they were made",
)


@pytest.mark.parametrize(
    "clock",
    [
        "ordered",
        "tie-in-order",
        pytest.param("tie-reversed", marks=OUT_OF_ORDER),
        pytest.param("clock-behind", marks=OUT_OF_ORDER),
    ],
)
def test_two_live_groups_keep_their_respective_givers_when_one_takes_the_others_former_name(
    clock, make_user, reviewer, monkeypatch
):
    first = signed_in(make_user("first-editor", is_staff=True, is_superuser=True))
    second = signed_in(make_user("second-editor", is_staff=True, is_superuser=True))
    cap = review_permission()
    north = group_create(first, "North desk", cap)
    south = group_create(second, "South desk", cap)
    member = make_user("desk-member")
    assert account_change(first, member, groups=[north.pk]).status_code == 302
    assert account_change(second, member, groups=[north.pk, south.pk]).status_code == 302

    # Only the clocks and UUIDs of the two audit writes are controlled. The first
    # group's real admin POST commits and vacates the name before the second POST.
    moment = timezone.now() + timedelta(seconds=1)
    low = UUID("10000000-0000-4000-8000-000000000003")
    high = UUID("f0000000-0000-4000-8000-000000000003")
    write = account_admin.record_event
    insert = AuditEvent.objects.create

    def record(**fields):
        if fields["action"] != "group.updated":
            return write(**fields)
        earlier = fields["obj"].pk == north.pk
        offset = 0
        if not earlier:
            offset = {"ordered": 1, "clock-behind": -1}.get(clock, 0)
        uid = low if earlier == (clock != "tie-reversed") else high
        with monkeypatch.context() as patched:
            patched.setattr(timezone, "now", lambda: moment + timedelta(milliseconds=offset))
            patched.setattr(AuditEvent.objects, "create", lambda **values: insert(id=uid, **values))
            return write(**fields)

    monkeypatch.setattr(account_admin, "record_event", record)
    group_save(first, north, "Archive desk", cap)
    group_save(second, south, "North desk", cap)
    assert dict(member.groups.values_list("pk", "name")) == {
        north.pk: "Archive desk",
        south.pk: "North desk",
    }
    events = list(AuditEvent.objects.filter(pk__in=[low, high]).order_by("occurred_at", "id"))
    assert len(events) == 2
    assert (events[0].object_id == str(north.pk)) == (clock in ("ordered", "tie-in-order"))
    shown, exported = account_rows(signed_in(reviewer), member)
    expected = {"Archive desk": "first-editor", "North desk": "second-editor"}
    notes = {}
    for name, giver in expected.items():
        [note] = [grant.note for grant in shown.capabilities if f"“{name}”" in grant.note]
        assert note in exported["Capabilities"]
        assert f"granted to the group by {giver}" in note
        credit = note.split("; group given", 1)[1].strip(": ")
        notes[name] = credit.removeprefix("by ").split(" (", 1)[0]
    assert notes == expected, (
        f"Archive desk = {notes['Archive desk']}, North desk = {notes['North desk']}"
    )


def test_membership_removal_regrant_and_case_only_renames_stay_with_each_live_group(
    make_user, reviewer
):
    first = signed_in(make_user("original-editor", is_staff=True, is_superuser=True))
    second = signed_in(make_user("later-editor", is_staff=True, is_superuser=True))
    member = make_user("night-member")
    cap = review_permission()
    old = group_create(first, "Night", cap)
    assert account_change(first, member, groups=[old.pk]).status_code == 302
    group_save(first, old, "NIGHT", cap)
    other = group_create(second, "Night", cap)
    assert account_change(second, member, groups=[other.pk]).status_code == 302
    assert account_change(second, member, groups=[old.pk, other.pk]).status_code == 302
    group_save(first, old, "Late", cap)
    group_save(second, other, "NIGHT", cap)
    group_save(first, old, "Night", cap)
    assert member.groups.count() == 2
    shown, exported = account_rows(signed_in(reviewer), member)
    for name, capability_giver in [("Night", "original-editor"), ("NIGHT", "later-editor")]:
        [grant] = [g for g in shown.capabilities if f"“{name}”" in g.note]
        assert f"granted to the group by {capability_giver}" in grant.note
        assert (
            "group given by later-editor" in grant.note and grant.note in exported["Capabilities"]
        )


def test_new_setup_account_precedes_its_groups_and_an_idempotent_rerun_adds_no_events():
    setup.setup_lab(
        lab_name="Example Laboratory",
        username="setup-reader",
        email="reader@example.invalid",
        roles=["technical", "admin"],
        password=PASSWORD,
    )
    owner = User.objects.get(username="setup-reader")
    events = list(AuditEvent.objects.filter(actor=owner).order_by("occurred_at", "id"))
    account = next(e for e in events if e.action == "setup.account_created")
    groups = [e for e in events if e.action == "setup.group_created"]
    assert set(owner.groups.values_list("name", flat=True)) == {"technical", "admin"}
    assert account.after["groups"] == ["admin", "technical"] and PASSWORD not in str(account.after)
    assert {e.after["name"] for e in groups} >= {"technical", "admin"}
    setup.setup_lab(username=owner.username, roles=["technical", "admin"])
    assert AuditEvent.objects.filter(actor=owner).count() == len(events)
    assert events[0] == account


@pytest.mark.parametrize("source", ["setup", "group-page", "renamed-on-page"])
def test_demo_seed_credits_the_backdated_membership_of_an_existing_canonical_role(
    source, make_user, reviewer, settings, monkeypatch
):
    from audit_review import seed
    from core.management.commands.seed_demo import DEMO_LAB_NAME

    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    monkeypatch.setenv("DEMO_PASSWORD", PASSWORD)
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    admin = signed_in(make_user("role-editor", is_staff=True, is_superuser=True))
    if source == "group-page":
        group_create(admin, "technical")
    elif source == "renamed-on-page":
        custom = group_create(admin, "Staff preparing orders")
        group_save(admin, custom, "technical")
    setup.setup_lab(lab_name=DEMO_LAB_NAME, username="role-editor", roles=["technical"])
    call_command("seed_demo", stdout=io.StringIO(), stderr=io.StringIO())
    member = User.objects.get(username=seed.UNUSED_USERNAME)
    group = member.groups.get(name="technical")
    birth = AuditEvent.objects.filter(object_type="auth.group", object_id=str(group.pk))
    event = AuditEvent.objects.get(action="account.created", object_id=str(member.pk))
    assert event.actor.username == "demo_admin" and event.after["groups"] == ["technical"]
    assert birth.exists() and all(e.occurred_at > event.occurred_at for e in birth)
    shown, exported = account_rows(signed_in(reviewer), member, {"role": "technical"})
    [role] = shown.roles
    assert "(demo_admin)" in role.note and role.note in exported["Roles"], role.note


@pytest.mark.parametrize("error_kind", ["database-not-null", "error-without-driver-diagnostics"])
def test_seed_preserves_an_unrelated_insert_error_and_rolls_back_the_run(
    error_kind, monkeypatch, settings
):
    from audit_review import seed
    from core.models import Lab

    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    monkeypatch.setenv("DEMO_PASSWORD", PASSWORD)
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    original = User.save
    injected = IntegrityError("Synthetic storage failure")
    attempts = []

    def save(person, *args, **kwargs):
        if person.username == seed.UNUSED_USERNAME and person._state.adding:
            attempts.append(person.username)
            if error_kind == "error-without-driver-diagnostics":
                raise injected
            person.last_name = None
        return original(person, *args, **kwargs)

    monkeypatch.setattr(User, "save", save)
    outcome = None
    try:
        call_command("seed_demo", stdout=io.StringIO(), stderr=io.StringIO())
    except IntegrityError as error:
        outcome = error
    assert attempts == [seed.UNUSED_USERNAME]
    assert outcome is not None, "seed_demo returned successfully despite a failed account insert"
    if error_kind == "database-not-null":
        assert (
            outcome.__cause__.sqlstate == "23502"
            and outcome.__cause__.diag.column_name == "last_name"
        )
    else:
        assert outcome is injected
    assert (
        not Lab.objects.exists() and not User.objects.filter(username__startswith="demo_").exists()
    )
    assert not AuditEvent.objects.filter(action="account.created").exists()


def test_migrated_database_names_both_account_collision_constraints():
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, User._meta.db_table)
    for column in ("username", "email"):
        [name] = [n for n, c in constraints.items() if c["unique"] and c["columns"] == [column]]
        assert name == f"accounts_user_{column}_key"


@pytest.mark.parametrize("field", ["username", "email"])
@pytest.mark.parametrize("roll_back", [True, False], ids=["roll-back", "commit"])
@pytest.mark.django_db(transaction=True)
def test_seed_follows_the_outcome_of_another_accounts_pending_identifier_insert(
    field, roll_back, make_user, reviewer
):
    from audit_review import seed

    admin = make_user("demo_admin")
    setup.setup_lab(lab_name="Example Laboratory", username=admin.username, roles=["technical"])
    new_name = seed.UNUSED_USERNAME if field == "username" else "incoming"
    new_email = seed.UNUSED_EMAIL if field == "email" else "incoming@example.invalid"
    pending = Queue()
    finish = Event()
    violations = []

    def other_writer():
        with transaction.atomic():
            account = User.objects.create_user(new_name, new_email)
            pending.put(account.pk)
            assert finish.wait(timeout=10)
            transaction.set_rollback(roll_back)

    def observe(execute, sql, params, many, ctx):
        if sql.startswith('INSERT INTO "accounts_user"'):
            # The seed has checked availability while the other row was uncommitted.
            finish.set()
        try:
            return execute(sql, params, many, ctx)
        except IntegrityError as exc:
            violations.append(exc.__cause__.diag.constraint_name)
            raise

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(in_own_connection, other_writer)
        try:
            other_id = pending.get(timeout=10)
            with connection.execute_wrapper(observe):
                seed._unused(reviewer)
        finally:
            finish.set()
        future.result(timeout=10)
    event = AuditEvent.objects.filter(action="account.created", actor=admin)
    if roll_back:
        example = User.objects.get(username=seed.UNUSED_USERNAME, email=seed.UNUSED_EMAIL)
        assert example.pk != other_id and example.groups.get().name == "technical"
        assert event.get().object_id == str(example.pk) and not violations
    else:
        assert User.objects.get(pk=other_id).email == new_email
        assert not User.objects.filter(
            username=seed.UNUSED_USERNAME, email=seed.UNUSED_EMAIL
        ).exists()
        assert not event.exists() and violations == [f"accounts_user_{field}_key"]


def test_role_group_page_preserves_the_group_when_its_owner_requests_deletion(make_user):
    owner = signed_in(make_user("role-owner", is_superuser=True, is_staff=True))
    group = group_create(owner, "West desk")
    member = make_user("west-member")
    assert account_change(owner, member, groups=[group.pk]).status_code == 302
    url = reverse("admin:auth_group_delete", args=[group.pk])
    assert owner.get(url).status_code == 403
    assert owner.post(url, {"post": "yes"}).status_code == 403
    assert member.groups.get(pk=group.pk).name == "West desk"
    assert list(
        AuditEvent.objects.filter(object_type="auth.group", object_id=str(group.pk)).values_list(
            "action", flat=True
        )
    ) == ["group.created"]
