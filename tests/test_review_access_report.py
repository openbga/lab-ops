"""Independent regression checks for Access review. The accounts and records are synthetic."""

import codecs
import csv
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from allauth.mfa.models import Authenticator
from django.contrib.auth.models import Group, Permission
from django.db import connection, connections
from django.test import Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from accounts.roles import ROLES
from audit_review.models import WorkspaceUse
from audit_review.tests.conftest import agent as agent
from audit_review.tests.conftest import category as category
from audit_review.tests.conftest import clinic as clinic
from audit_review.tests.conftest import make_ticket as make_ticket
from audit_review.tests.conftest import make_user as make_user
from audit_review.tests.conftest import review_permission
from audit_review.tests.conftest import reviewer as reviewer
from audit_review.tests.conftest import water as water
from core.models import AuditEvent
from teams.models import Team, TeamMembership

ACCOUNTS_URL = "/access-review/accounts/"
UNUSED_URL = "/access-review/unused/"
PERIOD_URL = UNUSED_URL + "period/"
LIST_ADDRESSES = (ACCOUNTS_URL, ACCOUNTS_URL + "export/", UNUSED_URL, UNUSED_URL + "export/")
PERIOD_INPUT = {"days": 29, "expected_revision": 1, "reason": "Synthetic review interval"}


def read_csv(response):
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response.content.startswith(codecs.BOM_UTF8)
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))


@pytest.mark.parametrize(
    "audience",
    [
        "anonymous",
        *ROLES,
        "superuser",
        "only_grant",
        "portal",
        "deactivated",
        "role_group",
        "other_group",
        "direct",
    ],
)
@pytest.mark.django_db
def test_audience_at_each_address_and_in_navigation(audience, reviewer, make_user, clinic):
    from audit_review.access_report import set_period

    set_period(actor=reviewer, days=19, expected_revision=0, reason="Stored synthetic reason")
    subject = make_user(
        "historical-person",
        last_name="UniqueReviewSurname",
        date_joined=timezone.now() - timedelta(days=90),
    )
    http = HttpClient()
    allowed = audience in {"direct", "other_group", "role_group"}
    if audience != "anonymous":
        roles = (audience,) if audience in ROLES else ()
        if allowed or audience == "deactivated":
            roles = ("technical",)
        person = make_user(
            "requester",
            *roles,
            client=clinic if audience == "portal" else None,
            is_superuser=audience == "superuser",
            is_staff=audience == "superuser",
        )
        if audience in {"portal", "only_grant", "deactivated", "direct"}:
            person.user_permissions.add(review_permission())
        if audience in {"role_group", "other_group"}:
            group, _ = Group.objects.get_or_create(
                name="technical" if audience == "role_group" else "Review panel"
            )
            group.permissions.add(review_permission())
            person.groups.add(group)
        http.force_login(person)
        if audience == "deactivated":
            person.is_active = False
            person.save(update_fields=["is_active"])
    menu = http.get(reverse("account-sessions"), follow=True)
    assert (f'href="{ACCOUNTS_URL}"' in menu.content.decode()) == allowed

    for address in (*LIST_ADDRESSES, PERIOD_URL):
        response = http.get(address)
        if allowed:
            assert response.status_code == 200, address
            if address == PERIOD_URL:
                assert "Stored synthetic reason" in response.content.decode()
            else:
                assert subject.last_name in response.content.decode()
        else:
            assert response.status_code == (
                302 if audience in {"anonymous", "deactivated"} else 403
            ), address
            assert subject.last_name not in response.content.decode()
            assert "Stored synthetic reason" not in response.content.decode()
    submitted = http.post(PERIOD_URL, PERIOD_INPUT)
    if allowed:
        assert submitted.status_code == 302 and submitted.url == UNUSED_URL
    else:
        assert submitted.status_code in (302, 403)
        assert not WorkspaceUse.objects.exists()
    assert AuditEvent.objects.filter(action="access_review.unused_period_changed").count() == (
        2 if allowed else 1
    )


@pytest.mark.django_db
def test_removing_the_role_after_the_form_opens_refuses_the_submission(reviewer):
    http = HttpClient()
    http.force_login(reviewer)
    opened = http.get(PERIOD_URL)
    assert opened.status_code == 200
    revision = opened.context["form"]["expected_revision"].value()
    reviewer.groups.remove(Group.objects.get(name="supervisor"))
    answer = http.post(PERIOD_URL, {**PERIOD_INPUT, "expected_revision": revision})
    assert answer.status_code == 403
    assert not AuditEvent.objects.filter(action="access_review.unused_period_changed").exists()


@pytest.mark.django_db
def test_opening_the_period_page_records_no_workspace_use(reviewer):
    # Adapted in round 1 (finding M4, resolved by documentation): the period's form, like the
    # Reviews tab's forms, records no use; the view's docstring now says only the two lists'
    # pages and exports are recorded. The form shows the period and its changes, which Changes
    # lists too.
    from audit_review import access_views

    http = HttpClient()
    http.force_login(reviewer)
    answer = http.get(PERIOD_URL)
    assert answer.status_code == 200 and "Save period" in answer.content.decode()
    uses = WorkspaceUse.objects.filter(account=reviewer, request_ref=answer["X-Request-ID"])
    assert not uses.exists()
    assert "the period's form is not" in access_views.__doc__


@pytest.mark.django_db
def test_invalid_unused_filters_still_record_the_period_that_was_shown(reviewer):
    from audit_review.access_report import set_period

    set_period(actor=reviewer, days=27, expected_revision=0, reason="Synthetic choice")
    http = HttpClient()
    http.force_login(reviewer)
    answer = http.get(UNUSED_URL, {"kind": "not-a-kind"})
    assert answer.status_code == 200
    assert "no sign-in for 27 days" in answer.content.decode()
    assert "Correct the highlighted filter" in answer.content.decode()
    use = WorkspaceUse.objects.get(request_ref=answer["X-Request-ID"])
    assert use.filters == {"invalid": True, "days": "27"} and use.rows == 0


@pytest.mark.django_db
def test_head_other_methods_and_redirected_exports_leave_no_list_use(reviewer):
    http = HttpClient()
    http.force_login(reviewer)
    for address in LIST_ADDRESSES:
        for method in ("head", "post", "put", "options"):
            assert getattr(http, method)(address).status_code == 405
    assert http.get(UNUSED_URL + "export/").status_code == 302
    assert http.get(ACCOUNTS_URL + "export/", {"status": "unrecognized"}).status_code == 302
    for method in ("head", "put", "options"):
        getattr(http, method)(PERIOD_URL)
    assert not WorkspaceUse.objects.exists()
    assert not AuditEvent.objects.filter(action="access_review.unused_period_changed").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("address", LIST_ADDRESSES)
def test_failed_use_write_withholds_the_report_and_file(address, reviewer, monkeypatch):
    from audit_review.access_report import set_period
    from core import access

    set_period(actor=reviewer, days=14, expected_revision=0, reason="Synthetic")
    http = HttpClient(raise_request_exception=False)
    http.force_login(reviewer)
    attempted = []

    def storage_failure(request, records):
        attempted.extend(records)
        raise RuntimeError("simulated history storage outage")

    monkeypatch.setattr(access, "write", storage_failure)
    response = http.get(address)
    assert response.status_code == 500 and len(attempted) == 1
    assert attempted[0].source == ("accounts" if address.startswith(ACCOUNTS_URL) else "unused")
    assert not WorkspaceUse.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("address", LIST_ADDRESSES)
def test_grants_added_after_a_report_snapshot_appear_on_the_next_request(
    address, make_user, reviewer, monkeypatch
):
    from audit_review import access_views
    from audit_review.access_report import set_period

    subject = make_user("snapshot-account", date_joined=timezone.now() - timedelta(days=40))
    set_period(actor=reviewer, days=8, expected_revision=0, reason="Synthetic")
    http = HttpClient()
    http.force_login(reviewer)
    permission = review_permission()
    original = access_views.reading_account

    def add_grant():
        try:
            User.objects.get(pk=subject.pk).user_permissions.add(permission.pk)
        finally:
            connections.close_all()

    def start_snapshot(request):
        reader = original(request)
        with ThreadPoolExecutor(max_workers=1) as writers:
            writers.submit(add_grant).result(timeout=10)
        return reader

    def row_has_grant(response):
        assert response.status_code == 200
        if address.endswith("/export/"):
            [row] = [r for r in read_csv(response) if r["Username"] == subject.username]
            return "Can review access and audit records" in row["Capabilities"]
        [row] = [r for r in response.context["page_obj"] if r.username == subject.username]
        return any(item.text == "Can review access and audit records" for item in row.capabilities)

    with monkeypatch.context() as scoped:
        scoped.setattr(access_views, "reading_account", start_snapshot)
        assert not row_has_grant(http.get(address))
    assert row_has_grant(http.get(address))
    assert WorkspaceUse.objects.filter(account=reviewer).count() == 2


@pytest.mark.django_db
def test_pagination_recounts_current_accounts_and_records_the_matching_total(reviewer):
    people = [User(username=f"list-{i:03}", email=f"list{i}@example.invalid") for i in range(53)]
    User.objects.bulk_create(people)
    http = HttpClient()
    http.force_login(reviewer)
    first = http.get(ACCOUNTS_URL)
    first_names = [row.username for row in first.context["page_obj"]]
    assert len(first_names) == 50
    User.objects.create(username="aaa-earlier", email="earlier@example.invalid")
    second = http.get(ACCOUNTS_URL, {"page": 2})
    second_names = [row.username for row in second.context["page_obj"]]
    assert second_names[0] == first_names[-1]
    assert list(WorkspaceUse.objects.order_by("id").values_list("rows", "filters")) == [
        (54, {"page": "1"}),
        (55, {"page": "2"}),
    ]
    exported = read_csv(http.get(ACCOUNTS_URL + "export/"))
    assert len(exported) == 55 and exported[-1]["Username"] == second_names[-1]


@pytest.mark.django_db
def test_full_csv_columns_and_text_quoting_on_both_exports(make_user, reviewer, clinic):
    from audit_review.access_report import set_period

    clinic.name = "+Éxample directory"
    clinic.save(update_fields=["name"])
    portal = make_user(
        "@sample", client=clinic, first_name="-Zoë", date_joined=timezone.now() - timedelta(days=40)
    )
    team = Team.objects.create(slug="synthetic", name="+Équipe")
    worker = make_user("team-member", date_joined=portal.date_joined)
    TeamMembership.objects.create(user=worker, team=team)
    http = HttpClient()
    http.force_login(reviewer)
    set_period(actor=reviewer, days=8, expected_revision=0, reason="Synthetic")
    account_columns = (
        "Person",
        "Username",
        "Kind",
        "Client organization",
        "Status",
        "Deactivated",
        "Admin console",
        "Roles",
        "Capabilities",
        "Teams",
        "Portal access",
        "Second factor",
        "Required by the rule",
        "Last sign-in",
        "Created",
    )
    unused_columns = (
        "Person",
        "Username",
        "Kind",
        "Client organization",
        "Roles",
        "Capabilities",
        "Last sign-in",
        "Created",
        "Days without a sign-in",
        "Unused after (days)",
    )
    for address, name, columns in [
        (ACCOUNTS_URL, "accounts", account_columns),
        (UNUSED_URL, "unused-accounts", unused_columns),
    ]:
        shown = http.get(address)
        response = http.get(shown.context["export_url"])
        rows = read_csv(response)
        assert tuple(rows[0]) == columns
        assert "no-store" in response["Cache-Control"]
        assert re.fullmatch(
            r'attachment; filename="access-review-' + name + r'-\d{8}-\d{6}\.csv"',
            response["Content-Disposition"],
        )
        assert [r["Username"].removeprefix("'") for r in rows] == [
            r.username for r in shown.context["page_obj"]
        ]
        [entry] = [r for r in rows if r["Username"] == "'@sample"]
        assert entry["Person"] == "'-Zoë" and entry["Client organization"] == "'+Éxample directory"
        if address == ACCOUNTS_URL:
            [entry] = [r for r in rows if r["Username"] == worker.username]
            assert entry["Teams"] == "'+Équipe"


@pytest.mark.django_db
def test_private_source_fields_stay_out_of_successful_reports_records_and_logs(
    reviewer, clinic, make_ticket, make_user, caplog
):
    from audit_review.access_report import set_period

    patient_marker = "Synthetic ticket detail 24B"
    make_ticket(clinic, subject=patient_marker)
    subject = make_user(
        "ordinary-subject",
        first_name="Listed subject",
        date_joined=timezone.now() - timedelta(days=33),
    )
    secret, recovery = "synthetic-authenticator-marker", "synthetic-recovery-marker"
    Authenticator.objects.create(user=subject, type="totp", data={"secret": secret})
    Authenticator.objects.create(user=subject, type="recovery_codes", data={"codes": [recovery]})
    set_period(actor=reviewer, days=9, expected_revision=0, reason="Synthetic review")
    http = HttpClient()
    http.force_login(reviewer)
    caplog.clear()
    captured = []
    for address in (*LIST_ADDRESSES, PERIOD_URL):
        response = http.get(address)
        assert response.status_code == 200 and "no-store" in response["Cache-Control"]
        body = response.content.decode("utf-8-sig")
        assert ("Listed subject" if address != PERIOD_URL else "Synthetic review") in body
        captured.append(body)
    captured.append(json.dumps(list(WorkspaceUse.objects.values("source", "filters", "rows"))))
    captured.append(
        json.dumps(
            list(
                AuditEvent.objects.filter(action="access_review.unused_period_changed").values(
                    "before", "after", "reason"
                )
            )
        )
    )
    assert any(r.name == "lab_ops.request" for r in caplog.records)
    captured.extend(record.getMessage() for record in caplog.records)
    for value in (patient_marker, secret, recovery, subject.email, subject.password):
        assert all(value not in part for part in captured)


@pytest.mark.django_db
def test_workspace_help_identifies_the_new_sources_listed_in_workspace_use(reviewer):
    http = HttpClient()
    http.force_login(reviewer)
    for address in (ACCOUNTS_URL, UNUSED_URL):
        assert http.get(address).status_code == 200
    answer = http.get(reverse("audit_review:uses"))
    for label in ("Access review: accounts", "Access review: unused accounts"):
        assert label in answer.content.decode()
    file = Path(__file__).resolve().parents[1] / "docs/pages/audit_review/workspace.json"
    help_text = json.loads(file.read_text())["sections"]
    description = next(
        item["description"]
        for section in help_text
        for item in section.get("items", [])
        if item["label"] == "Workspace use"
    )
    assert "access review" in description.lower() and "unused" in description.lower()


@pytest.mark.django_db(transaction=True)
def test_period_save_holds_reviewer_access_while_queued_for_the_setting(reviewer):
    """Both role and grant removals take this account's exclusive access gate."""
    from threading import Event

    from django.db import transaction

    from accounts.acting import ACCESS_GATE, INT4
    from audit_review.access_report import PERIOD_LOCK, set_period

    reached_period = Event()

    def observe(execute, sql, params, many, ctx):
        if "pg_advisory_xact_lock(" in sql and tuple(params or ()) == PERIOD_LOCK:
            reached_period.set()
        return execute(sql, params, many, ctx)

    def save():
        try:
            with connection.execute_wrapper(observe):
                return set_period(actor=reviewer, days=40, expected_revision=0, reason="Synthetic")
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as workers:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s,%s)", PERIOD_LOCK)
            pending = workers.submit(save)
            assert reached_period.wait(10)
            cursor.execute(
                "SELECT pg_try_advisory_xact_lock(%s,%s)", (ACCESS_GATE, reviewer.pk % INT4)
            )
            assert cursor.fetchone() == (False,)
        period, changed = pending.result(timeout=10)
    assert changed and (period.revision, period.days) == (1, 40)


@pytest.mark.django_db(transaction=True)
def test_open_period_form_reads_value_revision_and_history_in_the_same_snapshot(
    reviewer, monkeypatch
):
    from audit_review import review_views
    from audit_review.access_report import set_period

    http = HttpClient()
    http.force_login(reviewer)
    set_period(actor=reviewer, days=13, expected_revision=0, reason="First choice")
    original = review_views.reading_account

    def edit():
        try:
            set_period(actor=reviewer, days=31, expected_revision=1, reason="Second choice")
        finally:
            connections.close_all()

    def start_snapshot(request):
        actor = original(request)
        with ThreadPoolExecutor(max_workers=1) as writers:
            writers.submit(edit).result(timeout=10)
        return actor

    with monkeypatch.context() as scoped:
        scoped.setattr(review_views, "reading_account", start_snapshot)
        opened = http.get(PERIOD_URL)
    for response, (days, revision, event_days) in [
        (opened, (13, 1, [13])),
        (http.get(PERIOD_URL), (31, 2, [31, 13])),
    ]:
        assert response.status_code == 200
        assert response.context["current"].days == days
        assert response.context["form"]["expected_revision"].value() == revision
        assert [event.after["days"] for event in response.context["history"]] == event_days


def _signed_in(account):
    from django.test import Client as HttpClient

    http = HttpClient()
    http.force_login(account)
    return http


def _history_overlapping_posts(waiting_http, url, data, table, finish_other_save, at=None):
    """Allow an independent browser to finish after validation and before the first UPDATE (or
    before the first statement `at` names: adapted in round 1, see the group grant test)."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from django.db import connection, connections

    ready = Event()
    resume = Event()

    def hold_update(execute, sql, params, many, ctx):
        held = at(sql) if at is not None else sql.startswith(f'UPDATE "{table}" SET')
        if not ready.is_set() and held:
            ready.set()
            assert resume.wait(15), "The other browser did not complete its save"
        return execute(sql, params, many, ctx)

    def pending():
        try:
            with connection.execute_wrapper(hold_update):
                return waiting_http.post(url, data).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(pending)
        try:
            assert ready.wait(15), "The pending save did not reach the database update"
            finish_other_save()
        finally:
            resume.set()
        assert result.result(timeout=15) == 302


@pytest.mark.parametrize("evidence", ["change_history", "access_report"])
@pytest.mark.django_db(transaction=True)
def test_group_grant_is_attributed_only_to_the_browser_that_added_it(make_user, evidence):
    from django.contrib.auth.models import Group, Permission
    from django.urls import reverse

    from core.models import AuditEvent

    first = make_user("first_editor", is_staff=True, is_superuser=True)
    second = make_user("second_editor", is_staff=True, is_superuser=True)
    first_http, second_http = _signed_in(first), _signed_in(second)
    group = Group.objects.create(name="Study readers")
    permission = Permission.objects.get(
        content_type__app_label="accounts", codename="review_audit_records"
    )
    url = reverse("admin:auth_group_change", args=[group.pk])
    assert first_http.get(url).status_code == second_http.get(url).status_code == 200
    values = {"name": group.name, "permissions": [permission.pk]}

    def finish_first():
        assert first_http.post(url, values).status_code == 302
        assert group.permissions.filter(pk=permission.pk).exists()

    # Adapted in round 1: the role group page now holds the group's row (SELECT ... FOR NO KEY
    # UPDATE) before it reads the values it records, ahead of its UPDATE; the second save is held
    # just before that lock, so the first finishes in between, as the original test intended.
    _history_overlapping_posts(
        second_http,
        url,
        values,
        "auth_group",
        finish_first,
        at=lambda sql: 'FROM "auth_group"' in sql and "FOR NO KEY UPDATE" in sql,
    )
    if evidence == "access_report":
        from audit_review import access_report

        member = make_user("reader")
        member.groups.add(group)
        (grant,) = access_report.describe([member])[0].capabilities
        assert f"granted to the group by {first.username} ({first.username}) on " in grant.note
        return
    events = AuditEvent.objects.filter(action="group.updated", object_id=str(group.pk))
    credited = [
        event.actor.username
        for event in events
        if "accounts.review_audit_records"
        in set(event.after["permissions"]) - set(event.before["permissions"])
    ]
    assert credited == [first.username]


@pytest.mark.parametrize("evidence", ["change_history", "access_report"])
@pytest.mark.django_db(transaction=True)
def test_membership_attribution_follows_group_renamed_during_account_save(make_user, evidence):
    from django.contrib.auth.models import Group, Permission
    from django.urls import reverse

    from accounts.tests.test_confirm_identity import page_data
    from core.models import AuditEvent

    giver = make_user("membership_editor", is_staff=True, is_superuser=True)
    namer = make_user("group_editor", is_staff=True, is_superuser=True)
    giving, naming = _signed_in(giver), _signed_in(namer)
    member = make_user("study_member")
    permission = Permission.objects.get(
        content_type__app_label="accounts", codename="review_audit_records"
    )
    assert (
        naming.post(
            reverse("admin:auth_group_add"), {"name": "Study A", "permissions": [permission.pk]}
        ).status_code
        == 302
    )
    group = Group.objects.get(name="Study A")
    url = reverse("admin:accounts_user_change", args=[member.pk])
    form = giving.get(url).context["adminform"].form
    values = page_data(form, groups=[group.pk])

    def rename():
        renamed = naming.post(
            reverse("admin:auth_group_change", args=[group.pk]),
            {"name": "Study B", "permissions": [permission.pk]},
        )
        assert renamed.status_code == 302

    _history_overlapping_posts(giving, url, values, "accounts_user", rename)
    assert list(member.groups.values_list("pk", "name")) == [(group.pk, "Study B")]
    if evidence == "change_history":
        changed = AuditEvent.objects.get(action="account.updated", object_id=str(member.pk))
        renamed = AuditEvent.objects.get(action="group.updated", object_id=str(group.pk))
        assert changed.occurred_at > renamed.occurred_at
        assert changed.after["groups"] == [renamed.after["name"]]
        return
    from audit_review import access_report

    (grant,) = access_report.describe([member])[0].capabilities
    assert f"group given by {giver.username} ({giver.username}) on " in grant.note


@pytest.mark.django_db
def test_report_distinguishes_new_group_from_earlier_group_with_same_name(make_user):
    from django.contrib.auth.models import Group, Permission
    from django.urls import reverse

    from accounts.tests.test_confirm_identity import account_change
    from audit_review import access_report

    giver = make_user("original_giver", is_staff=True, is_superuser=True)
    http = _signed_in(giver)
    member = make_user("sample_member")
    perm = Permission.objects.get(
        content_type__app_label="accounts", codename="review_audit_records"
    )
    values = {"name": "Observers", "permissions": [perm.pk]}
    url = reverse("admin:auth_group_add")
    assert http.post(url, values).status_code == 302
    old = Group.objects.get(name=values["name"])
    old_id = old.pk
    assert account_change(http, member, groups=[old_id]).status_code == 302
    # The administrator's recorded membership gave the old group. Its name
    # becomes available again when that group is deleted outside the console.
    old.delete()
    assert http.post(url, values).status_code == 302
    replacement = Group.objects.get(name=values["name"])
    assert replacement.pk != old_id
    member.groups.add(replacement)  # a membership the change history never recorded
    (grant,) = access_report.describe([member])[0].capabilities
    assert "group given: not in the change history" in grant.note


@pytest.mark.django_db
def test_report_reads_setup_account_creation_and_later_role_addition():
    from datetime import timedelta

    from django.utils import timezone

    from accounts.models import User
    from accounts.setup import setup_lab
    from audit_review import access_report
    from core.models import AuditEvent
    from nonconformance.seed import happened_at

    with happened_at(timezone.now() - timedelta(days=3)):
        setup_lab(
            lab_name="Example Access Laboratory",
            username="bootstrap_editor",
            email="setup@example.invalid",
            password="Distinct synthetic password 17",
        )
    setup_lab(username="bootstrap_editor", roles=["technical"])
    editor = User.objects.get(username="bootstrap_editor")
    editor.first_name = "Renamed"
    editor.save(update_fields=["first_name"])
    events = AuditEvent.objects.filter(object_type="accounts.user", object_id=str(editor.pk))
    creation = events.get(action="setup.account_created")
    addition = events.get(action="setup.account_roles_added")
    row = access_report.describe([editor])[0]

    def label(event):
        when = timezone.localtime(event.occurred_at).strftime("%Y-%m-%d %H:%M")
        return f"Given by Renamed (bootstrap_editor) on {when}"

    assert [(item.text, item.note) for item in row.roles] == [
        ("Administrator", label(creation)),
        ("Technical staff", label(addition)),
    ]
    assert [(item.text, item.note) for item in row.console] == [
        ("Admin console", label(creation)),
        ("Superuser", label(creation)),
    ]


@pytest.mark.django_db
def test_added_account_role_regrant_and_reused_group_name_keep_distinct_givers(make_user):
    from django.contrib.auth.models import Group, Permission
    from django.urls import reverse

    from accounts.models import User
    from accounts.tests.test_admin import add_user
    from accounts.tests.test_confirm_identity import account_change
    from audit_review import access_report

    first = make_user("initial_editor", is_staff=True, is_superuser=True)
    next_editor = make_user("later_editor", is_staff=True, is_superuser=True)
    create, amend = _signed_in(first), _signed_in(next_editor)
    perm = Permission.objects.get(
        content_type__app_label="accounts", codename="review_audit_records"
    )
    group_url = reverse("admin:auth_group_add")
    assert (
        create.post(group_url, {"name": "Site desk", "permissions": [perm.pk]}).status_code == 302
    )
    initial_group = Group.objects.get(name="Site desk")
    role = Group.objects.get_or_create(name="technical")[0]
    assert add_user(create, groups=[role.pk, initial_group.pk], is_staff="on").status_code == 302
    added = User.objects.get(username="newuser")
    for new_name in ["Laboratory desk", "Review desk"]:
        assert (
            amend.post(
                reverse("admin:auth_group_change", args=[initial_group.pk]),
                {"name": new_name, "permissions": [perm.pk]},
            ).status_code
            == 302
        )
    assert amend.post(group_url, {"name": "Site desk", "permissions": [perm.pk]}).status_code == 302
    replacement = Group.objects.get(name="Site desk")
    assert account_change(amend, added, groups=[initial_group.pk]).status_code == 302
    assert (
        account_change(amend, added, groups=[initial_group.pk, replacement.pk, role.pk]).status_code
        == 302
    )
    row = access_report.describe([added])[0]
    assert "(later_editor)" in row.roles[0].note
    assert "(initial_editor)" in row.console[0].note
    assert len(row.capabilities) == 2
    by_group = {item.note.split(";")[0]: item.note for item in row.capabilities}
    for name, giver in [("Review desk", first.username), ("Site desk", next_editor.username)]:
        note = by_group[f"Through the group “{name}”"]
        assert f"granted to the group by {giver} ({giver}) on " in note
        assert f"group given by {giver} ({giver}) on " in note


def _listed_usernames(response):
    assert response.status_code == 200
    return [row.username for row in response.context["page_obj"]]


def _exported_usernames(response):
    return [row["Username"] for row in read_csv(response)]


@pytest.mark.django_db
def test_saved_person_sort_and_export_order_from_a_second_browser(client, reviewer, make_user):
    from django.test import Client

    # Equal displayed names deliberately have the opposite order in username and id.
    make_user("zulu_twin", "technical", first_name="Alice")
    make_user("able_twin", "technical", first_name="aLICE")
    make_user("bravo", "technical")
    make_user("first_account", "technical", last_name="Delta")
    ascending = ["able_twin", "zulu_twin", "bravo", "first_account"]
    url = reverse("access_review:accounts")
    client.force_login(reviewer)
    first = client.get(url, {"role": "technical", "sort": "-person"})
    assert _listed_usernames(first) == ascending[::-1]
    other = Client()
    other.force_login(reviewer)
    saved = other.get(url, {"role": "technical"})
    assert _listed_usernames(saved) == ascending[::-1]
    assert _exported_usernames(other.get(saved.context["export_url"])) == ascending[::-1]
    fallback = other.get(url, {"role": "technical", "sort": "outdated_choice"})
    assert _listed_usernames(fallback) == ascending
    assert _listed_usernames(client.get(url, {"role": "technical"})) == ascending[::-1]


@pytest.mark.django_db
def test_person_sort_matches_help_for_compound_first_names(client, reviewer, make_user):
    # Both first names start with Pat. As separate keys, Pat sorts before Pat Lee,
    # independently of surnames. The concatenated display names order the other way.
    make_user("short_first", "technical", first_name="Pat", last_name="Zane")
    make_user("compound_first", "technical", first_name="Pat Lee", last_name="Adams")
    client.force_login(reviewer)
    listing = client.get(reverse("access_review:accounts"), {"role": "technical", "sort": "person"})
    actual = _listed_usernames(listing)
    assert _exported_usernames(client.get(listing.context["export_url"])) == actual
    # Adapted in round 1 (finding L6, resolved in the help): Person sorts by the name shown, first
    # and last name as one name, then the username, so "Pat Lee Adams" comes before "Pat Zane".
    assert actual == ["compound_first", "short_first"]
    file = Path(__file__).resolve().parents[1] / "docs/pages/audit_review/access-accounts.json"
    assert "by the name shown (first name, then last name, as one name)" in file.read_text()


@pytest.mark.django_db
def test_report_intersects_filters_and_records_ids(client, reviewer, make_user, clinic, water):
    from allauth.mfa.models import Authenticator

    from audit_review.models import WorkspaceUse
    from teams.models import Team, TeamMembership

    focus = make_user("stored_portal", "technical", client=clinic, is_active=False)
    make_user("other_clinic", client=clinic)
    make_user("other_water", "technical", client=water)
    team = Team.objects.create(name="Synthetic team", slug="synthetic-team", is_active=False)
    TeamMembership.objects.create(team=team, user=focus, is_lead=True)
    perm = Permission.objects.get(
        content_type__app_label="accounts", codename="view_patient_information"
    )
    group = Group.objects.create(name="Review training")
    group.permissions.add(perm)
    focus.groups.add(group)
    focus.user_permissions.add(perm)
    Authenticator.objects.create(user=focus, type=Authenticator.Type.TOTP, data={})
    client.force_login(reviewer)
    filters = {
        "person": str(focus.pk),
        "kind": "portal",
        "status": "deactivated",
        "role": "technical",
        "capability": "view_patient_information",
        "team": str(team.pk),
        "organization": str(clinic.pk),
        "second_factor": "set_up",
    }
    url = reverse("access_review:accounts")
    result = client.get(url, filters)
    assert _listed_usernames(result) == [focus.username]
    row = result.context["page_obj"][0]
    assert len(row.capabilities) == 2
    assert [(item.text, item.note.lower()) for item in row.teams] == [
        ("Synthetic team", "lead, inactive team")
    ]
    assert WorkspaceUse.objects.get().filters == {**filters, "page": "1"}
    assert _exported_usernames(client.get(result.context["export_url"])) == [focus.username]
    for mismatch in (
        {"kind": "staff"},
        {"status": "active"},
        {"organization": str(water.pk)},
        {"capability": "review_audit_records"},
        {"second_factor": "required_missing"},
    ):
        assert _listed_usernames(client.get(url, {**filters, **mismatch})) == [], mismatch


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("person", "987654"),
        ("team", "987654"),
        ("organization", "987654"),
        ("kind", "retired"),
        ("status", "unknown"),
        ("role", "past_role"),
        ("capability", "retired_capability"),
        ("console", "unknown"),
        ("second_factor", "unknown"),
    ],
)
@pytest.mark.django_db
def test_report_invalid_filter_errors_and_no_export(client, reviewer, field_name, value):
    from audit_review.models import WorkspaceUse

    client.force_login(reviewer)
    query = {field_name: value}
    result = client.get(reverse("access_review:accounts"), query)
    assert _listed_usernames(result) == []
    assert field_name in result.context["filter_form"].errors
    assert not result.context["exportable"]
    assert client.get(reverse("access_review:accounts-export"), query).status_code == 302
    assert WorkspaceUse.objects.get().filters == {"invalid": True}


@pytest.mark.django_db
def test_report_requirement_matches_individual_accounts_for_each_rule(make_user, clinic):
    from allauth.mfa.models import Authenticator

    from audit_review import access_report
    from core import second_factor
    from core.models import SecondFactorRule

    direct = make_user("patient_direct", "supervisor")
    grouped = make_user("patient_group", "technical")
    admin = make_user("admin_role", "admin")
    console = make_user("console_only", is_staff=True, is_superuser=True)
    portal = make_user("client_account", client=clinic)
    inactive = make_user("inactive_staff", "technical", is_active=False, is_staff=True)
    people = [direct, grouped, admin, console, portal, inactive]
    perm = Permission.objects.get(
        content_type__app_label="accounts", codename="view_patient_information"
    )
    group = Group.objects.create(name="Synthetic privacy group")
    group.permissions.add(perm)
    grouped.groups.add(group)
    for person in (direct, portal, inactive):
        person.user_permissions.add(perm)
    Authenticator.objects.create(user=direct, type=Authenticator.Type.TOTP, data={})
    Authenticator.objects.create(user=grouped, type=Authenticator.Type.RECOVERY_CODES, data={})
    for chosen, expected in (
        ({}, set()),
        ({"staff": True}, {direct, grouped, admin, console}),
        ({"patient_information": True}, {direct, grouped}),
        ({"roles": ["technical"]}, {grouped, admin}),
        ({"admin_console": True}, {console}),
        ({"portal": True}, {portal}),
        (
            {"roles": ["technical"], "portal": True, "admin_console": True},
            {grouped, admin, console, portal},
        ),
    ):
        stored = SecondFactorRule(pk=1, **chosen)
        stored.save()
        assert {p for p in people if second_factor.requires(p)} == expected, chosen
        rows = access_report.describe(list(access_report.accounts({})))
        assert {r.account for r in rows if r.account in people and r.required} == expected, chosen
        missing = set(access_report.accounts({"second_factor": "required_missing"}))
        assert missing.intersection(people) == expected - {direct}, chosen


@pytest.mark.django_db
def test_report_uses_portal_grant_for_current_organization_only(make_user, clinic, water):
    from audit_review import access_report
    from clients.models import ClientSite, PortalAccess

    portal = make_user("portal_move", client=clinic)
    stale = PortalAccess.objects.create(user=portal, client=water, whole_organization=True)
    elsewhere = ClientSite.objects.create(client=water, name="Elsewhere", code="E")
    stale.sites.add(elsewhere)
    assert not stale.applies_to(portal)
    rows = access_report.describe(list(access_report.accounts({"person": portal})))
    assert len(rows) == 1
    assert rows[0].organization == clinic.name
    assert rows[0].portal == "Only the tickets it raised"


def _period_change(actor, days, revision, reason):
    from audit_review.access_report import set_period

    return set_period(actor=actor, days=days, expected_revision=revision, reason=reason)


@pytest.mark.django_db
def test_unused_empty_state_accounts_for_people_who_never_signed_in(client, reviewer, make_user):
    new = make_user("just_joined", date_joined=timezone.now() - timedelta(days=1))
    client.force_login(reviewer)
    _period_change(reviewer, 30, 0, "Monthly review")
    response = client.get(reverse("access_review:unused"))
    assert response.status_code == 200
    assert response.context["total"] == 0
    new.refresh_from_db()
    assert new.is_active and new.last_login is None
    assert "No unused accounts" in response.content.decode()
    assert "Every active account has signed in within the period." not in response.content.decode()


@pytest.mark.django_db
def test_unused_tab_retains_the_group_name_shown_in_its_export(client, reviewer, make_user):
    person = make_user("review_assistant", date_joined=timezone.now() - timedelta(days=35))
    group = Group.objects.create(name="Quality QA East")
    group.permissions.add(review_permission())
    person.groups.add(group)
    client.force_login(reviewer)
    _period_change(reviewer, 30, 0, "Compare tab and export")
    listing = client.get(reverse("access_review:unused"))
    assert listing.status_code == 200
    [row] = read_csv(client.get(reverse("access_review:unused-export")))
    assert row["Username"] == person.username
    assert f"through the group “{group.name}”" in row["Capabilities"].replace("Through", "through")
    table_body = listing.content.decode().split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert group.name in table_body


@pytest.mark.django_db
def test_unused_cutoff_and_csv_whole_days_across_clock_change(
    client, reviewer, make_user, clinic, monkeypatch, settings
):
    from accounts.models import User
    from audit_review import access_views
    from jobs.actor import system_account

    settings.TIME_ZONE = "America/New_York"
    now = datetime(2026, 3, 10, 4, 30, tzinfo=UTC)
    cutoff = now - timedelta(days=3)
    tick = timedelta(microseconds=1)
    for key, offset in [("before", -tick), ("at", timedelta()), ("after", tick)]:
        make_user(
            f"signed_{key}", date_joined=cutoff - timedelta(days=20), last_login=cutoff + offset
        )
        make_user(f"never_{key}", date_joined=cutoff + offset)
    make_user("departed", is_active=False, date_joined=cutoff - timedelta(days=20))
    make_user("portal_old", client=clinic, date_joined=cutoff - timedelta(days=4, hours=20))
    # Make both criteria true for the system account, so its exclusion tests the kind itself.
    jobs = system_account()
    User.objects.filter(pk=jobs.pk).update(is_active=True, date_joined=cutoff - timedelta(days=30))
    client.force_login(reviewer)
    _period_change(reviewer, 3, 0, "Verify local times")
    # Keep session time on the real clock. Freeze only the report's observation time.
    monkeypatch.setattr(
        access_views, "timezone", SimpleNamespace(now=lambda: now, localtime=timezone.localtime)
    )
    listing = client.get(reverse("access_review:unused"))
    assert listing.status_code == 200
    # Adapted in round 1 (the order is now longest unused first: by the last sign-in, or the
    # creation of an account that never signed in, then the account's id). signed_before and
    # never_before went unused at the same moment; signed_before was created first.
    assert [row.username for row in listing.context["page_obj"]] == [
        "portal_old",
        "signed_before",
        "never_before",
    ]
    assert "6 March 2026, 23:30" in listing.content.decode()
    rows = read_csv(client.get(reverse("access_review:unused-export")))
    assert [
        (row["Username"], row["Last sign-in"], row["Days without a sign-in"]) for row in rows
    ] == [
        ("portal_old", "Never", "7"),
        ("signed_before", "2026-03-06T23:29:59-05:00", "3"),
        ("never_before", "Never", "3"),
    ]
    assert {row["Unused after (days)"] for row in rows} == {"3"}


@pytest.mark.django_db
def test_unused_export_rechecks_the_period_after_a_page_opens(client, reviewer, make_user):
    from audit_review.models import Source, UseKind, WorkspaceUse

    make_user("over_a_week", date_joined=timezone.now() - timedelta(days=10))
    make_user("over_a_month", date_joined=timezone.now() - timedelta(days=40))
    _period_change(reviewer, 7, 0, "Weekly")
    client.force_login(reviewer)
    listing = client.get(reverse("access_review:unused"))
    assert [row.username for row in listing.context["page_obj"]] == ["over_a_month", "over_a_week"]
    link = listing.context["export_url"]
    _period_change(reviewer, 30, 1, "Monthly")
    assert [(r["Username"], r["Unused after (days)"]) for r in read_csv(client.get(link))] == [
        ("over_a_month", "30")
    ]
    _period_change(reviewer, None, 2, "Clear period")
    assert client.get(link).status_code == 302
    recorded = WorkspaceUse.objects.filter(kind=UseKind.EXPORTED, source=Source.UNUSED)
    assert list(recorded.values_list("filters", "rows")) == [({"days": "30"}, 1)]
    assert "No period is set" in client.get(reverse("access_review:unused")).content.decode()


@pytest.mark.django_db
def test_period_form_inclusive_bounds_noop_clear_and_history(client, reviewer):
    from audit_review.access_report import PERIOD_CHANGED, period

    client.force_login(reviewer)
    url = reverse("access_review:unused-period")

    def send(days, reason):
        opened = client.get(url)
        revision = opened.context["form"]["expected_revision"].value()
        return client.post(url, {"expected_revision": revision, "days": days, "reason": reason})

    for days, reason, field in [
        ("0", "Zero", "days"),
        ("367", "Too long", "days"),
        ("x", "Not a number", "days"),
        ("20", "  ", "reason"),
    ]:
        response = send(days, reason)
        assert response.status_code == 200 and field in response.context["form"].errors
    assert period().revision == 0 and not AuditEvent.objects.filter(action=PERIOD_CHANGED).exists()
    for days, reason in [("1", "Daily"), ("366", "Upper bound"), ("366", "Same"), ("", "Stop")]:
        assert send(days, reason).status_code == 302
    assert (period().days, period().revision) == (None, 3)
    history = client.get(url).context["history"]
    assert [(e.before["days"], e.after["days"], e.reason, e.actor_id) for e in history] == [
        (366, None, "Stop", reviewer.pk),
        (1, 366, "Upper bound", reviewer.pk),
        (None, 1, "Daily", reviewer.pk),
    ]


@pytest.mark.django_db(transaction=True)
def test_two_reviewers_saving_the_period_keep_one_change_and_redraw_the_stale_form(
    reviewer, make_user
):
    from audit_review.access_report import PERIOD_CHANGED, PERIOD_LOCK, STALE_PERIOD, period

    second = make_user("alternate_reviewer", "technical")
    second.user_permissions.add(review_permission())
    url = reverse("access_review:unused-period")
    browsers = {}
    submissions = {}
    for actor, days in [(reviewer, "20"), (second, "40")]:
        http = HttpClient()
        http.force_login(actor)
        form = http.get(url).context["form"]
        browsers[days] = http
        submissions[days] = {
            "days": days,
            "reason": f"Use {days} days",
            "expected_revision": form["expected_revision"].value(),
        }
    assert {data["expected_revision"] for data in submissions.values()} == {0}
    both_saving = Barrier(2)

    def at_period_lock(execute, sql, params, many, execution):
        if "pg_advisory_xact_lock(" in sql and tuple(params or ()) == PERIOD_LOCK:
            both_saving.wait(timeout=10)
        return execute(sql, params, many, execution)

    def send_concurrently(days):
        try:
            with connection.execute_wrapper(at_period_lock):
                return browsers[days].post(url, submissions[days])
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {days: pool.submit(send_concurrently, days) for days in submissions}
        answers = {days: future.result(timeout=30) for days, future in futures.items()}
    assert sorted(answer.status_code for answer in answers.values()) == [200, 302]
    winner, loser = sorted(answers, key=lambda days: -answers[days].status_code)
    assert (period().days, period().revision) == (int(winner), 1)
    event = AuditEvent.objects.get(action=PERIOD_CHANGED)
    assert (event.before, event.after, event.reason) == (
        {"days": None},
        {"days": int(winner)},
        f"Use {winner} days",
    )
    assert event.actor_id == (reviewer.pk if winner == "20" else second.pk)
    html = answers[loser].content.decode()
    assert STALE_PERIOD in html and f"Now: unused after {winner} days" in html
    assert f"Use {winner} days" in html and f"Use {loser} days" in html
    assert f'value="{loser}"' in html
    form = answers[loser].context["form"]
    assert form["expected_revision"].value() == "1"
    retry = browsers[loser].post(
        url, {**submissions[loser], "expected_revision": form["expected_revision"].value()}
    )
    assert retry.status_code == 302
    assert (period().days, period().revision) == (int(loser), 2)


def _access_demo_options(monkeypatch, settings):
    from io import StringIO

    monkeypatch.setenv("DEMO_PASSWORD", "A blue notebook in the synthetic lab")
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    return {"stdout": StringIO(), "stderr": StringIO(), "verbosity": 0}


@pytest.mark.django_db
def test_access_seed_completes_when_another_account_owns_the_example_email(monkeypatch, settings):
    from django.core.management import call_command

    from accounts.models import User

    held = User.objects.create_user(
        username="previous_example", email="former.technician@example.invalid"
    )
    call_command("seed_demo", **_access_demo_options(monkeypatch, settings))
    held.refresh_from_db()
    assert (held.username, held.email) == ("previous_example", "former.technician@example.invalid")
    assert User.objects.filter(username="demo_supervisor").exists()


@pytest.mark.django_db
def test_access_seed_omits_account_email_from_new_attribution_events(monkeypatch, settings):
    from django.core.management import call_command

    from accounts.models import User
    from core.models import AuditEvent

    call_command("seed_demo", **_access_demo_options(monkeypatch, settings))
    staff = User.objects.filter(client__isnull=True, email__endswith="@example.invalid")
    assert staff.exists()
    for user in staff:
        assert not AuditEvent.objects.filter(
            object_type="accounts.user",
            object_id=str(user.pk),
            action="account.created",
            after__email=user.email,
        ).exists(), user.username


@pytest.mark.django_db(transaction=True)
def test_access_demo_rerun_preserves_history_and_local_period_and_reset_restores_example(
    monkeypatch, settings
):
    from datetime import timedelta

    from django.core.management import call_command
    from django.utils import timezone

    from accounts.models import User
    from accounts.roles import ROLES
    from audit_review import access_report
    from audit_review.models import WorkspaceUse
    from core.models import AuditEvent

    options = _access_demo_options(monkeypatch, settings)
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    call_command("seed_demo", **options)

    def check_demo():
        example = User.objects.get(username="former_technician")
        assert not example.has_usable_password()
        assert example.is_active and example.client_id is None
        assert list(example.groups.values_list("name", flat=True)) == ["technical"]
        assert timezone.now() - example.last_login >= timedelta(days=120)
        assert (example.last_login - example.date_joined) == timedelta(days=80)
        assert list(access_report.unused_accounts(90, {}).values_list("pk", flat=True)) == [
            example.pk
        ]
        events = {}
        for name, role in [
            *(("demo_" + role, role) for role in ROLES),
            ("former_technician", "technical"),
        ]:
            user = User.objects.get(username=name)
            event = AuditEvent.objects.get(
                action="account.created", object_type="accounts.user", object_id=str(user.pk)
            )
            assert event.after["groups"] == [role]
            assert event.actor.username == "demo_admin"
            events[name] = event.pk, event.occurred_at
        return events

    assert (access_report.period().days, access_report.period().revision) == (90, 1)
    original_events = check_demo()
    reviewer = User.objects.get(username="demo_supervisor")
    access_report.set_period(actor=reviewer, days=143, expected_revision=1, reason="Demo policy")
    use = WorkspaceUse.objects.create(
        account=reviewer, kind="viewed", source="unused", rows=0, request_ref="example-visit"
    )
    call_command("seed_demo", **options)
    assert check_demo() == original_events
    assert (access_report.period().days, access_report.period().revision) == (143, 2)
    assert WorkspaceUse.objects.get(pk=use.pk).request_ref == "example-visit"
    call_command("reset_demo", interactive=False, **options)
    check_demo()
    assert (access_report.period().days, access_report.period().revision) == (90, 1)
    assert not WorkspaceUse.objects.filter(request_ref="example-visit").exists()


@pytest.mark.django_db
def test_access_seed_preserves_prior_username_without_retrospective_attribution(
    monkeypatch, settings
):
    from django.core.management import call_command

    from accounts.models import User
    from core.models import AuditEvent

    earlier = User.objects.create_user(
        username="former_technician", email="earlier@example.invalid", is_active=False
    )
    before = (earlier.email, earlier.password, earlier.date_joined, earlier.is_active)
    call_command("seed_demo", **_access_demo_options(monkeypatch, settings))
    earlier.refresh_from_db()
    assert (earlier.email, earlier.password, earlier.date_joined, earlier.is_active) == before
    assert not earlier.groups.exists()
    assert not AuditEvent.objects.filter(
        object_type="accounts.user", object_id=str(earlier.pk), action="account.created"
    ).exists()


@pytest.mark.parametrize("new_source", ["accounts", "unused"])
@pytest.mark.django_db(transaction=True)
def test_access_migration_keeps_existing_and_overlapping_writes_and_refuses_loss(new_source):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from django.db import DatabaseError, connection, connections, transaction
    from django.db.migrations.executor import MigrationExecutor

    before = ("audit_review", "0003_review_records")
    after = ("audit_review", "0004_access_report")

    def move(target):
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        return executor.loader.project_state([target]).apps

    def record(model, source):
        return model.objects.create(
            account_id=31, source=source, kind="viewed", rows=4, filters={"page": "1"}
        )

    def existing_rows():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, source, occurred_at, rows, filters, xmin::text, ctid::text "
                "FROM audit_review_workspaceuse ORDER BY id"
            )
            return cursor.fetchall()

    reached_ddl = Event()

    def upgrading():
        def starting_ddl(execute, sql, params, many, ctx):
            if sql.startswith('ALTER TABLE "audit_review_workspaceuse" DROP CONSTRAINT'):
                reached_ddl.set()
            return execute(sql, params, many, ctx)

        try:
            with connection.execute_wrapper(starting_ddl):
                move(after)
        finally:
            connections.close_all()

    old_model = move(before).get_model("audit_review", "WorkspaceUse")
    try:
        for source in ("changes", "access", "sign_ins", "people", "uses", "reviews"):
            record(old_model, source)
        with ThreadPoolExecutor(max_workers=1) as workers:
            with transaction.atomic():
                record(old_model, "reviews")
                original = existing_rows()
                upgrade = workers.submit(upgrading)
                assert reached_ddl.wait(15), "Upgrade did not reach the workspace-use constraint"
            upgrade.result(timeout=30)
        assert existing_rows() == original
        record(old_model, "changes")
        all_old = existing_rows()
        old_model = move(before).get_model("audit_review", "WorkspaceUse")
        assert old_model.objects.count() == 8 and existing_rows() == all_old
        new_apps = move(after)
        assert not new_apps.get_model("audit_review", "UnusedPeriod").objects.exists()
        record(new_apps.get_model("audit_review", "WorkspaceUse"), new_source)
        expected = existing_rows()
        with pytest.raises(DatabaseError, match="Access review uses are recorded"):
            move(before)
        assert existing_rows() == expected
        assert after in MigrationExecutor(connection).loader.applied_migrations
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes("audit_review"))


@pytest.mark.django_db
def test_no_email_claim_with_an_email_address_accepted_as_the_username(make_user, reviewer):
    from accounts.tests.test_admin import add_user
    from audit_review.access_report import set_period

    owner = make_user("synthetic-owner", is_staff=True, is_superuser=True)
    address = "synthetic.reader@example.invalid"
    created = add_user(_signed_in(owner), username=address, email=address)
    assert created.status_code == 302
    person = User.objects.get(username=address)
    User.objects.filter(pk=person.pk).update(
        first_name="Ellis", date_joined=timezone.now() - timedelta(days=60)
    )
    set_period(actor=reviewer, days=30, expected_revision=0, reason="Synthetic")
    http = _signed_in(reviewer)
    # Adapted in round 1 (finding L2, resolved in the help): a username is shown as it was chosen,
    # even where it is an address; the account's own email field never is.
    User.objects.filter(pk=person.pk).update(email="private.field@example.invalid")
    for route in LIST_ADDRESSES:
        response = http.get(route)
        assert response.status_code == 200
        assert "Ellis" in response.content.decode() and address in response.content.decode()
        assert "private.field@example.invalid" not in response.content.decode()
    file = Path(__file__).resolve().parents[1] / "docs/pages/audit_review/access-accounts.json"
    assert "A username is shown as it was chosen, even where it is an email address." in (
        file.read_text()
    )


@pytest.mark.django_db
def test_csv_deactivation_attribution_stays_text_even_when_the_actors_name_begins_with_a_sign(
    make_user, reviewer
):
    from accounts.tests.test_confirm_identity import account_change

    owner = make_user("seed-admin", first_name="+Alex", is_staff=True, is_superuser=True)
    subject = make_user("former-assistant", "technical")
    assert account_change(_signed_in(owner), subject, is_active="").status_code == 302
    url = reverse("access_review:accounts-export", query={"person": subject.pk})
    [row] = read_csv(_signed_in(reviewer).get(url))
    assert row["Deactivated"].startswith("'+Alex (seed-admin) on ")
    assert row["Status"] == "Deactivated"
