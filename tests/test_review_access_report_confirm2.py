"""Access review confirmation with historical setup records and synthetic accounts."""

import csv
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.urls import reverse
from django.utils import timezone
from test_review_access_report_verify import (
    group_create,
    group_save,
    in_own_connection,
    signed_in,
    wait_for_other_writer,
)

from accounts import admin as account_admin
from accounts import setup
from accounts.models import User
from accounts.tests.test_confirm_identity import account_change
from audit_review.tests.conftest import make_user as make_user
from audit_review.tests.conftest import review_permission
from audit_review.tests.conftest import reviewer as reviewer
from core.models import AuditEvent
from nonconformance.seed import happened_at

# Adapted in the second confirmation round: a group removed outside the application (which never
# removes one) and replaced under the same name is a documented limit of Access review
# (audit_review/access_report.py; docs/architecture.md, Access review); kept as strict expected
# failures.
OUT_OF_BAND = pytest.mark.xfail(
    strict=True,
    reason="Known limit: a group removed outside the application and replaced under the same "
    "name is not told apart from it",
)

pytestmark = pytest.mark.django_db
PASSWORD = "Five separate books on the highest shelf 94"


def read_role(http, account, *, all_roles=False):
    url = reverse("access_review:accounts")
    sheet = reverse("access_review:accounts-export")
    filters = {} if all_roles else {"role": "technical"}
    result = http.get(url, filters)
    export = http.get(sheet, filters)
    assert result.status_code == export.status_code == 200
    row = next(r for r in result.context["page_obj"] if r.username == account.username)
    records = csv.DictReader(io.StringIO(export.content.decode("utf-8-sig")))
    exported = next(r for r in records if r["Username"] == account.username)
    role = next(r for r in row.roles if r.text == "Technical staff")
    assert role.note in exported["Roles"]
    return role


def create_owner():
    from core.management.commands.seed_demo import DEMO_LAB_NAME

    setup.setup_lab(
        lab_name=DEMO_LAB_NAME,
        username="bootstrap-owner",
        email="bootstrap@example.invalid",
        password=PASSWORD,
        roles=["technical"],
    )
    return User.objects.get(username="bootstrap-owner")


def historical_setup(monkeypatch):
    # Use the real service, state and event payloads. Only the append order is the earlier
    # writer's: account first, then lab, groups and the rest, all in the same transaction.
    events = []
    write = setup.record_event
    with transaction.atomic():
        with monkeypatch.context() as patches:
            patches.setattr(setup, "record_event", lambda **event: events.append(event))
            owner = create_owner()
        account_events = [e for e in events if e["action"] == "setup.account_created"]
        assert len(account_events) == 1
        for event in account_events + [e for e in events if e not in account_events]:
            write(**event)
    return owner


@pytest.mark.parametrize("old_order", [True, False], ids=["historical", "installed-writer"])
@pytest.mark.parametrize("reused", [False, True], ids=["first-group", "reused-name"])
def test_bootstrap_grant_survives_upgrade_when_an_earlier_group_had_the_same_role_name(
    old_order, reused, make_user, reviewer, monkeypatch
):
    retired_id = None
    if reused:
        first = signed_in(make_user("retired-editor", is_staff=True, is_superuser=True))
        group = group_create(first, "technical")
        retired_id = group.pk
        group.delete()
    owner = historical_setup(monkeypatch) if old_order else create_owner()
    role = owner.groups.get()
    assert role.pk != retired_id and role.name == "technical"
    granted = AuditEvent.objects.get(action="setup.account_created", object_id=str(owner.pk))
    birth = AuditEvent.objects.get(action="setup.group_created", object_id=str(role.pk))
    assert granted.actor == birth.actor == owner and granted.after["groups"] == [role.name]
    if old_order:
        assert granted.occurred_at < birth.occurred_at
    credit = read_role(signed_in(reviewer), owner)
    assert "(bootstrap-owner)" in credit.note, credit.note


@pytest.mark.parametrize("group_evidence", ["seen", "renamed", "replacement-recorded-first"])
@pytest.mark.parametrize(
    "returned",
    [
        pytest.param(False, id="replacement-held"),
        pytest.param(True, id="old-group-returned", marks=OUT_OF_BAND),
    ],
)
def test_a_recorded_grant_at_a_vacated_name_does_not_belong_to_its_previous_owner(
    group_evidence, returned, make_user
):
    from audit_review import access_report

    editor = signed_in(make_user("label-editor", is_staff=True, is_superuser=True))
    giver = signed_in(make_user("membership-editor", is_staff=True, is_superuser=True))
    cap = review_permission()
    prior = Group.objects.create(name="Night desk")
    group_save(editor, prior, "Night desk", cap)
    # A maintenance rename with no audit event. The following real admin save identifies
    # this very same group under its current name, before the account receives its grant.
    prior.name = "Transit desk"
    prior.save(update_fields=["name"])
    current_name = "Day desk" if group_evidence == "renamed" else "Transit desk"
    group_save(editor, prior, current_name)
    replacement = Group.objects.create(name="Night desk")
    assert replacement.pk != prior.pk
    if group_evidence == "replacement-recorded-first":
        group_save(editor, replacement, replacement.name, cap)
    member = make_user("night-staff")
    assert account_change(giver, member, groups=[replacement.pk]).status_code == 302
    if group_evidence != "replacement-recorded-first":
        group_save(editor, replacement, replacement.name, cap)
    names = list(
        AuditEvent.objects.filter(action="group.updated", object_id=str(prior.pk))
        .order_by("occurred_at", "id")
        .values_list("after__name", flat=True)
    )
    assert names == ["Night desk", current_name]
    if returned:
        # The account no longer holds the group its editor granted. The original group
        # returns to this name on its own page; the new membership has no recorded giver.
        replacement.delete()
        group_save(editor, prior, "Night desk", cap)
        member.groups.add(prior)
    [permission] = access_report.describe([member])[0].capabilities
    assert "granted to the group by label-editor" in permission.note
    expected = (
        "group given: not in the change history" if returned else "group given by membership-editor"
    )
    assert expected in permission.note.lower(), permission.note


@pytest.mark.parametrize("previous_owner", [False, True], ids=["new-name", "reused-name"])
def test_seed_reports_the_demo_administrator_when_its_example_predates_the_current_role(
    previous_owner, make_user, reviewer, settings, monkeypatch
):
    from audit_review.seed import UNUSED_USERNAME

    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    monkeypatch.setenv("DEMO_PASSWORD", PASSWORD)
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    if previous_owner:
        http = signed_in(make_user("previous-editor", is_staff=True, is_superuser=True))
        with happened_at(timezone.now() - timedelta(days=300)):
            old = group_create(http, "technical")
        old_id = old.pk
        old.delete()
    owner = create_owner()
    technical = owner.groups.get()
    call_command("seed_demo", stdout=io.StringIO(), stderr=io.StringIO())
    target = User.objects.get(username=UNUSED_USERNAME)
    given = AuditEvent.objects.get(action="account.created", object_id=str(target.pk))
    birth = AuditEvent.objects.get(action="setup.group_created", object_id=str(technical.pk))
    assert target.groups.get() == technical and given.actor.username == "demo_admin"
    assert given.after["groups"] == ["technical"] and given.occurred_at < birth.occurred_at
    if previous_owner:
        first = AuditEvent.objects.get(action="group.created", object_id=str(old_id))
        assert old_id != technical.pk and first.occurred_at < given.occurred_at
    credit = read_role(signed_in(reviewer), target)
    assert "(demo_admin)" in credit.note, credit.note


@pytest.mark.django_db(transaction=True)
def test_a_direct_grant_survives_a_stale_unchanged_role_label_without_reassigning_the_membership(
    make_user, monkeypatch
):
    from audit_review import access_report

    first = signed_in(make_user("first-giver", is_staff=True, is_superuser=True))
    second = signed_in(make_user("direct-giver", is_staff=True, is_superuser=True))
    group = group_create(first, "Day shift", review_permission())
    person = make_user("shift-member")
    assert account_change(first, person, groups=[group.pk]).status_code == 302
    write = account_admin.record_event
    backend = Queue()

    def rename():
        connection.ensure_connection()
        backend.put(connection.connection.info.backend_pid)
        group_save(first, group, "Early shift", review_permission())

    with ThreadPoolExecutor(max_workers=1) as pool:
        writers = []

        def record(**fields):
            if fields["action"] == "account.updated" and fields["obj"].pk == person.pk:
                pending = pool.submit(in_own_connection, rename)
                writers.append(pending)
                wait_for_other_writer(pending, backend.get(timeout=10))
            return write(**fields)

        monkeypatch.setattr(account_admin, "record_event", record)
        assert (
            account_change(second, person, user_permissions=[review_permission().pk]).status_code
            == 302
        )
        writers[0].result(timeout=10)
    event = AuditEvent.objects.get(action="account.updated", actor__username="direct-giver")
    renamed = AuditEvent.objects.get(action="group.updated", object_id=str(group.pk))
    assert event.before["groups"] == event.after["groups"]
    if renamed.occurred_at < event.occurred_at:
        assert event.after["groups"] in (["Day shift"], ["Early shift"])
    notes = [i.note for i in access_report.describe([person])[0].capabilities]
    assert any("direct-giver" in n and "first-giver" not in n for n in notes), notes
    assert any("Early shift" in n and "group given by first-giver" in n for n in notes), notes


@pytest.mark.parametrize("failure", ["account-primary-key", "membership"])
def test_demo_seed_propagates_unrelated_integrity_errors_and_commits_no_partial_example(
    failure, settings, monkeypatch
):
    from django.db.models.signals import m2m_changed

    from audit_review.seed import UNUSED_EMAIL, UNUSED_USERNAME

    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    monkeypatch.setenv("DEMO_PASSWORD", PASSWORD)
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    call_command("seed_demo", stdout=io.StringIO(), stderr=io.StringIO())
    example = User.objects.get(username=UNUSED_USERNAME)
    example.delete()
    count = AuditEvent.objects.filter(action="account.created").count()
    if failure == "account-primary-key":
        # A restored account with an explicit ID ahead of the sequence. Neither the
        # example's username nor its email is occupied on the refresh.
        with connection.cursor() as c:
            c.execute("SELECT nextval(pg_get_serial_sequence('accounts_user', 'id')) + 1")
            occupied = c.fetchone()[0]
        User.objects.create_user("restored-clerk", "restored@example.invalid", pk=occupied)
    else:
        occupied = None

    def refuse_membership(sender, instance, action, **kwargs):
        if failure == "membership" and action == "pre_add" and instance.username == UNUSED_USERNAME:
            # A non-null violation inside the membership operation, after account.save().
            sender.objects.create(user_id=instance.pk, group_id=None)

    through = User.groups.through
    m2m_changed.connect(refuse_membership, sender=through)
    observed = []

    def track_database_failure(execute, sql, params, many, ctx):
        try:
            return execute(sql, params, many, ctx)
        except IntegrityError as exc:
            detail = exc.__cause__.diag
            observed.append((detail.constraint_name, detail.column_name))
            raise

    error = None
    stdout = io.StringIO()
    try:
        with connection.execute_wrapper(track_database_failure):
            call_command("seed_demo", stdout=stdout, stderr=io.StringIO())
    except IntegrityError as exc:
        error = exc
    finally:
        m2m_changed.disconnect(refuse_membership, sender=through)
    assert not User.objects.filter(username=UNUSED_USERNAME).exists()
    assert not User.objects.filter(email=UNUSED_EMAIL).exists()
    assert AuditEvent.objects.filter(action="account.created").count() == count
    if occupied is not None:
        assert User.objects.get(pk=occupied).username == "restored-clerk"
        assert ("accounts_user_pkey", None) in observed, observed
    else:
        assert (None, "group_id") in observed, observed
    assert error is not None, stdout.getvalue()
    if failure == "account-primary-key":
        assert error.__cause__.diag.constraint_name == "accounts_user_pkey"
    else:
        assert error.__cause__.diag.column_name == "group_id"


@pytest.mark.django_db(transaction=True)
def test_setup_credit_is_independent_of_filters_with_an_edit_between_the_two_relevant_events(
    make_user, reviewer, monkeypatch
):
    editor = signed_in(make_user("other-editor", is_superuser=True, is_staff=True))
    other = make_user("other-person")
    original = setup.record_event
    kinds = []

    def record(**fields):
        event = original(**fields)
        if fields["action"] == "setup.account_created" or (
            fields["action"] == "setup.group_created" and fields["after"]["name"] == "technical"
        ):
            kinds.append(event)
            if len(kinds) == 1:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    answer = pool.submit(
                        in_own_connection, lambda: account_change(editor, other, last_name="Other")
                    ).result(timeout=10)
                    assert answer.status_code == 302
        return event

    monkeypatch.setattr(setup, "record_event", record)
    owner = create_owner()
    unrelated = AuditEvent.objects.get(action="account.updated", object_id=str(other.pk))
    assert len(kinds) == 2 and kinds[0].occurred_at < unrelated.occurred_at < kinds[1].occurred_at
    http = signed_in(reviewer)
    one = read_role(http, owner)
    all_accounts = read_role(http, owner, all_roles=True)
    assert "(bootstrap-owner)" in one.note and one == all_accounts, (one.note, all_accounts.note)


def test_refusal_of_the_last_setup_event_rolls_back_prior_group_events_and_the_administrator(
    make_user, monkeypatch
):
    from core.models import Lab

    saved = Group.objects.create(name="Existing team")
    make_user("existing-owner")
    original = setup.record_event
    written = []

    def record(**fields):
        if fields["action"] == "setup.account_created":
            return original(**{**fields, "reason": None})
        written.append(original(**fields).pk)

    monkeypatch.setattr(setup, "record_event", record)
    with pytest.raises(IntegrityError) as failure:
        create_owner()
    assert failure.value.__cause__.diag.column_name == "reason"
    assert not AuditEvent.objects.filter(pk__in=written).exists()
    assert list(Group.objects.values_list("pk", flat=True)) == [saved.pk]
    assert not Lab.objects.exists()
    assert not User.objects.filter(username="bootstrap-owner").exists()
    assert User.objects.filter(username="existing-owner").exists()


@pytest.mark.parametrize("account_sorts_first", [True, False], ids=["account-first", "group-first"])
def test_equal_audit_timestamps_preserve_the_giver_of_the_role_created_in_the_same_command(
    account_sorts_first, make_user, reviewer, monkeypatch
):
    from uuid import UUID

    editor = signed_in(make_user("retired-editor", is_staff=True, is_superuser=True))
    retired = group_create(editor, "technical")
    retired_id = retired.pk
    retired.delete()
    moment = timezone.now()
    write = setup.record_event
    create = AuditEvent.objects.create
    low = UUID("10000000-0000-4000-8000-000000000001")
    high = UUID("f0000000-0000-4000-8000-000000000001")

    def stamp(**fields):
        if fields["action"] == "setup.account_created":
            fields["id"] = low if account_sorts_first else high
        elif fields["action"] == "setup.group_created" and fields["after"]["name"] == "technical":
            fields["id"] = high if account_sorts_first else low
        return create(**fields)

    def record(**fields):
        with monkeypatch.context() as fixed:
            fixed.setattr(timezone, "now", lambda: moment)
            fixed.setattr(AuditEvent.objects, "create", stamp)
            return write(**fields)

    monkeypatch.setattr(setup, "record_event", record)
    owner = create_owner()
    assert owner.groups.get().pk != retired_id
    pair = AuditEvent.objects.filter(pk__in=[low, high]).order_by("occurred_at", "id")
    assert list(pair.values_list("occurred_at", flat=True)) == [moment, moment]
    assert (pair[0].action == "setup.account_created") == account_sorts_first
    credit = read_role(signed_in(reviewer), owner)
    assert "(bootstrap-owner)" in credit.note, credit.note
