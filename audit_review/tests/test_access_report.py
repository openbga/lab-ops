"""The Access review section (audit_review.access_report): who may open it, what the Accounts tab
says each account holds and who gave it, the Unused accounts tab and its period, and the record of
each use. All data is synthetic."""

import csv
import io
from datetime import timedelta

import pytest
from allauth.mfa.models import Authenticator
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.db import connection
from django.test import Client as HttpClient
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from audit_review import access_report
from audit_review.models import Source, UnusedPeriod, UseKind, WorkspaceUse
from clients.models import ClientSite, PortalAccess
from core.audit import record_event
from core.models import AuditEvent, SecondFactorRule
from teams.models import Team, TeamMembership

from .conftest import PASSWORD, review_permission

ROUTES = (
    "access_review:accounts",
    "access_review:accounts-export",
    "access_review:unused",
    "access_review:unused-export",
    "access_review:unused-period",
)


@pytest.fixture(autouse=True)
def fresh_limits():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def owner(make_user):
    """The deployment owner: opens the account administration, holds no laboratory role."""
    return make_user("owner", is_staff=True, is_superuser=True, first_name="Olu", last_name="Owner")


def http_for(user):
    http = HttpClient()
    http.force_login(user)
    return http


def page_data(form, **changes):
    """The values a Django form was drawn with, as the browser posts them, changed."""
    data = {}
    for name, field in form.fields.items():
        if field.disabled or name.startswith("confirm_"):
            continue
        value = form[name].value()
        if value is None or value is False:
            continue
        if isinstance(value, (list, tuple)):
            data[name] = [getattr(v, "pk", v) for v in value]
        elif value is True:
            data[name] = "on"
        else:
            data[name] = getattr(value, "pk", value)
    data.update(changes)
    return data


def admin_save(http, url, **changes):
    form = http.get(url).context["adminform"].form
    response = http.post(url, page_data(form, confirm_password=PASSWORD, **changes))
    assert response.status_code == 302, response.content.decode()[:3000]
    return response


def account_change(http, account, **changes):
    return admin_save(http, reverse("admin:accounts_user_change", args=[account.pk]), **changes)


def group_change(http, group, **changes):
    return admin_save(http, reverse("admin:auth_group_change", args=[group.pk]), **changes)


def capability(codename):
    return Permission.objects.get(content_type__app_label="accounts", codename=codename)


def rows_for(**filters):
    return {
        row.username: row for row in access_report.describe(list(access_report.accounts(filters)))
    }


def texts(items):
    return [item.text for item in items]


# ------------------------------------------------------------------------------ who may open it


@pytest.mark.django_db
def test_only_reviewers_with_a_role_open_the_access_review(make_user, reviewer, supervisor, clinic):
    anonymous = HttpClient()
    no_role = make_user("loner")
    no_role.user_permissions.add(review_permission())
    portal = make_user("portal", client=clinic)
    portal.user_permissions.add(review_permission())
    for name in ROUTES:
        url = reverse(name)
        assert anonymous.get(url).status_code == 302, name
        for account in (supervisor, no_role, portal):
            assert http_for(account).get(url).status_code == 403, (name, account.username)
            assert http_for(account).post(url).status_code in (403, 405), (name, account.username)
    reviewer_http = http_for(reviewer)
    for name in ("access_review:accounts", "access_review:unused", "access_review:unused-period"):
        assert reviewer_http.get(reverse(name)).status_code == 200, name


@pytest.mark.django_db
def test_the_capability_through_a_group_opens_it(make_user):
    reviewers = Group.objects.create(name="Reviewers")
    reviewers.permissions.add(review_permission())
    person = make_user("grouped", "technical")
    person.groups.add(reviewers)
    assert http_for(person).get(reverse("access_review:accounts")).status_code == 200


@pytest.mark.django_db
def test_the_sidebar_offers_it_only_to_reviewers(reviewer, supervisor):
    url = reverse("access_review:accounts")
    page = http_for(reviewer).get(reverse("audit_review:changes")).content.decode()
    assert f'href="{url}"' in page
    page = http_for(supervisor).get("/").content.decode()
    assert f'href="{url}"' not in page


# ------------------------------------------------------------------------------ what each holds


@pytest.mark.django_db
def test_each_account_shows_its_kind_teams_portal_access_and_second_factor(
    make_user, reviewer, agent, clinic
):
    team = Team.objects.create(slug="intake", name="Intake")
    TeamMembership.objects.create(team=team, user=agent, is_lead=True)
    north = ClientSite.objects.create(client=clinic, name="North site", code="N")
    portal = make_user("portal", client=clinic, first_name="Pia", last_name="Portal")
    access = PortalAccess.objects.create(user=portal, client=clinic)
    access.sites.add(north)
    whole = make_user("whole", client=clinic)
    PortalAccess.objects.create(user=whole, client=clinic, whole_organization=True)
    make_user("alone", client=clinic)
    Authenticator.objects.create(user=agent, type=Authenticator.Type.TOTP, data={})
    SecondFactorRule.objects.create(pk=1, staff=True)
    rows = rows_for()
    assert rows["agent"].kind == "Staff" and rows["agent"].status == "Active"
    assert texts(rows["agent"].roles) == ["Client services"]
    assert [(item.text, item.note) for item in rows["agent"].teams] == [("Intake", "Lead")]
    assert rows["agent"].second_factor and rows["agent"].required
    assert not rows["reviewer"].second_factor and rows["reviewer"].required
    assert rows["portal"].kind == "Client portal"
    assert rows["portal"].organization == clinic.name
    assert rows["portal"].portal == "Tickets of North site, and those it raised"
    assert rows["whole"].portal == "Its organization's tickets"
    assert rows["alone"].portal == "Only the tickets it raised"
    assert rows["agent"].portal == "" and not rows["portal"].required
    assert texts(rows["reviewer"].capabilities) == ["Can review access and audit records"]
    assert rows["reviewer"].capabilities[0].note.startswith("Directly")


@pytest.mark.django_db
def test_who_gave_a_role_is_the_latest_recorded_change_that_gave_it(owner, make_user):
    person = make_user("person")
    technical = Group.objects.get_or_create(name="technical")[0]
    http = http_for(owner)
    account_change(http, person, groups=[technical.pk])
    event = AuditEvent.objects.get(action="account.updated", object_id=str(person.pk))
    role = rows_for(person=person)["person"].roles[0]
    assert role.text == "Technical staff" and not role.missing
    assert (
        role.note == f"Given {access_report.Given('Olu Owner', 'owner', event.occurred_at).text()}"
    )
    # Taken away on the page, then put back by a path that records nothing.
    account_change(http, person, groups=[])
    person.groups.add(technical)
    role = rows_for(person=person)["person"].roles[0]
    assert role.missing and role.note == access_report.NOT_RECORDED


@pytest.mark.django_db
def test_a_capability_through_a_group_names_both_recorded_changes(owner, make_user):
    person = make_user("person", "technical")
    http = http_for(owner)
    response = http.post(
        reverse("admin:auth_group_add"),
        {
            "name": "Privacy officers",
            "permissions": [capability("view_patient_information").pk],
            "confirm_password": PASSWORD,
        },
    )
    assert response.status_code == 302, response.content.decode()[:3000]
    officers = Group.objects.get(name="Privacy officers")
    account_change(http, person, groups=[Group.objects.get(name="technical").pk, officers.pk])
    item = rows_for(person=person)["person"].capabilities[0]
    assert item.text == "Can view patient information" and not item.missing
    assert item.note.startswith("Through the group “Privacy officers”; granted to the group by Olu")
    assert "; group given by Olu Owner (owner) on " in item.note
    # Renamed on the group's page: the account's recorded membership follows the new name.
    group_change(http, officers, name="Privacy team")
    item = rows_for(person=person)["person"].capabilities[0]
    assert item.note.startswith("Through the group “Privacy team”; granted to the group by Olu")
    assert not item.missing


@pytest.mark.django_db
def test_a_direct_grant_admin_console_access_and_a_deactivation_are_attributed(owner, make_user):
    person = make_user("person", "supervisor")
    http = http_for(owner)
    patient = capability("view_patient_information")
    account_change(http, person, user_permissions=[patient.pk], is_staff="on")
    row = rows_for(person=person)["person"]
    assert row.capabilities[0].note.startswith("Directly, given by Olu Owner (owner) on ")
    assert [(item.text, item.missing) for item in row.console] == [("Admin console", False)]
    account_change(http, person, is_active="")
    row = rows_for(person=person)["person"]
    assert row.status == "Deactivated" and row.deactivated.username == "owner"
    # Made active again: no longer shown as deactivated.
    account_change(http, person, is_active="on")
    assert rows_for(person=person)["person"].deactivated is None


@pytest.mark.django_db
def test_the_demonstration_seeds_capability_event_is_read(owner, make_user):
    person = make_user("person", "supervisor")
    person.user_permissions.add(review_permission())
    from django.db import transaction

    with transaction.atomic():
        record_event(
            actor=owner,
            action="account.capability_granted",
            obj=person,
            after={"capability": "review_audit_records"},
            reason="Synthetic",
        )
    item = rows_for(person=person)["person"].capabilities[0]
    assert item.note.startswith("Directly, given by Olu Owner (owner) on ")


@pytest.mark.django_db
def test_an_unrecorded_grant_says_so(make_user):
    person = make_user("person", "supervisor")
    person.user_permissions.add(capability("view_patient_information"))
    row = rows_for(person=person)["person"]
    assert row.roles[0].missing and row.roles[0].note == access_report.NOT_RECORDED
    assert row.capabilities[0].missing


@pytest.mark.django_db
def test_the_scheduled_jobs_account_is_listed_as_its_own_kind(make_user):
    from jobs.actor import system_account

    jobs = system_account()
    rows = rows_for()
    assert rows[jobs.username].kind == access_report.SYSTEM_KIND
    assert rows[jobs.username].status == "Nobody signs in"
    assert jobs.username not in rows_for(kind="staff")
    assert jobs.username not in rows_for(status="deactivated")


@pytest.mark.django_db
def test_the_filters(make_user, reviewer, agent, supervisor, clinic):
    team = Team.objects.create(slug="intake", name="Intake")
    TeamMembership.objects.create(team=team, user=agent)
    portal = make_user("portal", client=clinic)
    gone = make_user("gone", "technical", is_active=False)
    make_user("console", is_staff=True)
    Authenticator.objects.create(user=supervisor, type=Authenticator.Type.TOTP, data={})
    officers = Group.objects.create(name="Officers")
    officers.permissions.add(capability("view_patient_information"))
    agent.groups.add(officers)
    SecondFactorRule.objects.create(pk=1, roles=["client_services", "supervisor"])

    def names(**filters):
        return set(rows_for(**filters))

    assert {"portal"} == names(kind="portal")
    assert "portal" not in names(kind="staff")
    assert names(status="deactivated") == {"gone"}
    assert "gone" not in names(status="active")
    assert names(role="supervisor") == {"reviewer", "supervisor"}
    assert names(capability="review_audit_records") == {"reviewer"}
    assert names(capability="view_patient_information") == {"agent"}
    assert names(team=team) == {"agent"}
    assert names(organization=clinic) == {"portal"}
    assert names(console="console") == {"console"}
    assert names(second_factor="set_up") == {"supervisor"}
    assert "supervisor" not in names(second_factor="none")
    assert names(second_factor="required_missing") == {"reviewer", "agent"}
    assert names(person=portal) == {"portal"}
    assert gone.username in names()


@pytest.mark.django_db
def test_the_sorts(make_user):
    now = timezone.now()
    make_user("b_old", first_name="Bea", last_login=now - timedelta(days=9))
    make_user("a_new", first_name="Al", last_login=now - timedelta(days=1))
    make_user("c_never", first_name="Cy")
    # No name: shown, and sorted, by its username.
    make_user("bob", last_login=now - timedelta(days=5))

    def order(sort):
        return [
            account.username
            for account in access_report.accounts({}, sort)
            if account.username in {"a_new", "b_old", "c_never", "bob"}
        ]

    assert order("person") == ["a_new", "b_old", "bob", "c_never"]
    assert order("-person") == ["c_never", "bob", "b_old", "a_new"]
    assert order("last_sign_in") == ["c_never", "b_old", "bob", "a_new"]
    assert order("-last_sign_in") == ["a_new", "bob", "b_old", "c_never"]


@pytest.mark.django_db
def test_covered_among_agrees_with_requires_for_each_account(make_user, reviewer, agent, clinic):
    from core import second_factor

    portal = make_user("portal", client=clinic)
    gone = make_user("gone", "supervisor", is_active=False)
    console = make_user("console", is_staff=True)
    accounts = [reviewer, agent, portal, gone, console]
    ids = [account.pk for account in accounts]
    for rule in (
        {"staff": True},
        {"roles": ["supervisor"]},
        {"portal": True},
        {"admin_console": True},
        {"patient_information": True},
    ):
        current = SecondFactorRule(pk=1, **rule)
        covered = set(second_factor.covered_among(ids, current))
        assert covered == {a.pk for a in accounts if second_factor.requires(a, current)}, rule
        assert covered <= set(second_factor.covered_among(None, current))


@pytest.mark.django_db
def test_a_page_reads_a_bounded_number_of_times(make_user, reviewer, clinic):
    team = Team.objects.create(slug="intake", name="Intake")
    for number in range(30):
        account = make_user(f"person{number}", "client_services")
        TeamMembership.objects.create(team=team, user=account)
        portal = make_user(f"portal{number}", client=clinic)
        PortalAccess.objects.create(user=portal, client=clinic, whole_organization=True)
    http = http_for(reviewer)
    http.get(reverse("access_review:accounts"))  # the saved preferences' first reading
    with CaptureQueriesContext(connection) as queries:
        assert http.get(reverse("access_review:accounts")).status_code == 200
    assert len(queries) < 45, len(queries)


# ------------------------------------------------------------------------------ the pages


@pytest.mark.django_db
def test_the_accounts_page_and_its_record(reviewer, agent):
    http = http_for(reviewer)
    page = http.get(reverse("access_review:accounts"), {"role": "client_services"})
    text = page.content.decode()
    assert "Ada Agent" in text and "Client services" in text
    assert "Not in the change history" in text
    use = WorkspaceUse.objects.get()
    assert (use.kind, use.source, use.rows) == (UseKind.VIEWED, Source.ACCOUNTS, 1)
    assert use.filters == {"role": "client_services", "page": "1"}
    assert use.account_id == reviewer.pk


@pytest.mark.django_db
def test_an_invalid_filter_lists_nothing_and_records_so(reviewer):
    http = http_for(reviewer)
    page = http.get(reverse("access_review:accounts"), {"role": "nobody"})
    assert page.status_code == 200
    assert "Correct the highlighted filter" in page.content.decode()
    assert WorkspaceUse.objects.get().filters == {"invalid": True}
    response = http.get(reverse("access_review:accounts-export"), {"role": "nobody"})
    assert response.status_code == 302
    assert WorkspaceUse.objects.count() == 1


@pytest.mark.django_db
def test_the_export(make_user, reviewer, agent):
    make_user("formula", "technical", first_name="=SUM(A1)", last_name="Example")
    http = http_for(reviewer)
    response = http.get(reverse("access_review:accounts-export"), {"sort": "-person"})
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"].startswith(
        'attachment; filename="access-review-accounts-'
    )
    content = response.content.decode()
    assert content.startswith("﻿")
    rows = list(csv.reader(io.StringIO(content.lstrip("﻿"))))
    assert rows[0][:3] == ["Person", "Username", "Kind"]
    by_name = {row[1]: row for row in rows[1:]}
    assert by_name["formula"][0] == "'=SUM(A1) Example"
    assert by_name["agent"][7] == "Client services (Not in the change history)"
    assert [row[1] for row in rows[1:]] == [
        account.username for account in access_report.accounts({}, "-person")
    ]
    use = WorkspaceUse.objects.get()
    assert (use.kind, use.source) == (UseKind.EXPORTED, Source.ACCOUNTS)
    assert use.filters == {"sort": "-person"}
    assert use.rows == len(rows) - 1


@pytest.mark.django_db
def test_an_export_over_the_limit_is_refused(reviewer, agent, monkeypatch):
    from audit_review import access_views

    monkeypatch.setattr(access_views, "EXPORT_LIMIT", 1)
    response = http_for(reviewer).get(reverse("access_review:accounts-export"))
    assert response.status_code == 302
    assert not WorkspaceUse.objects.exists()


# ------------------------------------------------------------------------------ unused accounts


def set_days(reviewer, days):
    access_report.set_period(
        actor=reviewer, days=days, expected_revision=access_report.period().revision, reason="Test"
    )


@pytest.mark.django_db
def test_nothing_is_unused_until_a_period_is_set(reviewer, make_user):
    make_user("idle", "technical", last_login=timezone.now() - timedelta(days=400))
    http = http_for(reviewer)
    page = http.get(reverse("access_review:unused")).content.decode()
    assert "No period is set" in page and "idle" not in page
    assert http.get(reverse("access_review:unused-export")).status_code == 302
    use = WorkspaceUse.objects.get()
    assert (use.source, use.rows, use.filters) == (Source.UNUSED, 0, {"days": "none"})


@pytest.mark.django_db
def test_unused_accounts(reviewer, make_user, clinic):
    from jobs.actor import system_account

    system_account()
    now = timezone.now()
    old, recent = now - timedelta(days=40), now - timedelta(days=10)
    make_user("stale", "technical", last_login=old)
    make_user("fresh", "technical", last_login=recent)
    make_user("never_old", date_joined=old)
    make_user("never_new", date_joined=recent)
    make_user("gone", "technical", last_login=old, is_active=False)
    make_user("portal_stale", client=clinic, last_login=old - timedelta(days=1))
    set_days(reviewer, 30)
    names = [
        account.username for account in access_report.unused_accounts(30, {}) if account.username
    ]
    assert names == ["never_old", "portal_stale", "stale"]
    assert [a.username for a in access_report.unused_accounts(30, {"kind": "staff"})] == [
        "never_old",
        "stale",
    ]
    assert [a.username for a in access_report.unused_accounts(30, {"role": "technical"})] == [
        "stale"
    ]
    http = http_for(reviewer)
    page = http.get(reverse("access_review:unused"))
    assert "no sign-in for 30 days" in page.content.decode()
    assert [row.username for row in page.context["page_obj"]] == [
        "never_old",
        "portal_stale",
        "stale",
    ]
    response = http.get(reverse("access_review:unused-export"))
    rows = list(csv.reader(io.StringIO(response.content.decode().lstrip("﻿"))))
    assert [row[1] for row in rows[1:]] == ["never_old", "portal_stale", "stale"]
    assert rows[3][-2:] == ["40", "30"]
    assert rows[1][6] == "Never"
    uses = list(
        WorkspaceUse.objects.order_by("id").values_list("kind", "source", "rows", "filters")
    )
    assert uses == [
        (UseKind.VIEWED, Source.UNUSED, 3, {"page": "1", "days": "30"}),
        (UseKind.EXPORTED, Source.UNUSED, 3, {"days": "30"}),
    ]


# ------------------------------------------------------------------------------ the period


@pytest.mark.django_db
def test_setting_the_period(reviewer):
    http = http_for(reviewer)
    url = reverse("access_review:unused-period")
    page = http.get(url)
    assert page.context["form"].initial == {"days": None, "expected_revision": 0}
    response = http.post(url, {"days": "60", "reason": "Quarterly", "expected_revision": "0"})
    assert response.status_code == 302 and response["Location"] == reverse("access_review:unused")
    stored = UnusedPeriod.objects.get()
    assert (stored.days, stored.revision) == (60, 1)
    event = AuditEvent.objects.get(action=access_report.PERIOD_CHANGED)
    assert (event.before, event.after, event.reason) == ({"days": None}, {"days": 60}, "Quarterly")
    assert event.actor == reviewer
    # The same value again records nothing.
    http.post(url, {"days": "60", "reason": "Again", "expected_revision": "1"})
    assert AuditEvent.objects.filter(action=access_report.PERIOD_CHANGED).count() == 1
    # Cleared: no period.
    http.post(url, {"days": "", "reason": "Stop", "expected_revision": "1"})
    assert UnusedPeriod.objects.get().days is None
    history = http.get(url).context["history"]
    assert [event.after for event in history] == [{"days": None}, {"days": 60}]


@pytest.mark.django_db
def test_a_period_changed_since_the_form_opened_is_refused_with_the_input_kept(reviewer):
    http = http_for(reviewer)
    url = reverse("access_review:unused-period")
    http.post(url, {"days": "60", "reason": "First", "expected_revision": "0"})
    response = http.post(url, {"days": "90", "reason": "Mine", "expected_revision": "0"})
    assert response.status_code == 200
    assert access_report.STALE_PERIOD in response.content.decode()
    form = response.context["form"]
    assert form.data["days"] == "90" and form.data["reason"] == "Mine"
    assert form.data["expected_revision"] == "1"
    assert response.context["current"].days == 60
    assert UnusedPeriod.objects.get().days == 60


@pytest.mark.parametrize(
    ("days", "reason", "field"),
    [("0", "x", "days"), ("367", "x", "days"), ("30", "", "reason"), ("abc", "x", "days")],
)
@pytest.mark.django_db
def test_a_period_out_of_bounds_or_without_a_reason_is_refused(reviewer, days, reason, field):
    response = http_for(reviewer).post(
        reverse("access_review:unused-period"),
        {"days": days, "reason": reason, "expected_revision": "0"},
    )
    assert response.status_code == 200
    assert field in response.context["form"].errors
    assert not UnusedPeriod.objects.exists()


@pytest.mark.django_db
def test_a_reviewer_who_lost_the_capability_cannot_save_the_period(reviewer):
    http = http_for(reviewer)
    url = reverse("access_review:unused-period")
    assert http.get(url).status_code == 200
    reviewer.user_permissions.remove(review_permission())
    response = http.post(url, {"days": "30", "reason": "x", "expected_revision": "0"})
    assert response.status_code == 403
    assert not UnusedPeriod.objects.exists()


@pytest.mark.django_db
def test_the_service_refuses_an_account_that_may_not_review(supervisor):
    from django.core.exceptions import PermissionDenied

    with pytest.raises(PermissionDenied):
        access_report.set_period(actor=supervisor, days=30, expected_revision=0, reason="x")
    assert not UnusedPeriod.objects.exists()


# ------------------------------------------------------------------------------ workspace use


@pytest.mark.django_db
def test_workspace_use_names_the_access_review_filters(reviewer, agent):
    team = Team.objects.create(slug="intake", name="Intake")
    http = http_for(reviewer)
    http.get(
        reverse("access_review:accounts"),
        {
            "kind": "staff",
            "status": "active",
            "role": "supervisor",
            "capability": "view_patient_information",
            "team": team.pk,
            "console": "superuser",
            "second_factor": "required_missing",
            "sort": "-last_sign_in",
        },
    )
    page = http.get(reverse("audit_review:uses")).content.decode()
    for words in (
        "Access review: accounts",
        "Kind: Staff",
        "Status: Active",
        "Role: Supervisor",
        "Capability: Can view patient information",
        "Team: Intake",
        "Admin console: Superuser",
        "Second factor: Required by the rule and not set up",
        "Sorted by Last sign-in, newest first",
    ):
        assert words in page, words


@pytest.mark.django_db
def test_the_unused_period_bounds_hold_in_the_database(reviewer):
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        UnusedPeriod.objects.create(pk=1, days=0)
    with pytest.raises(IntegrityError), transaction.atomic():
        UnusedPeriod.objects.create(pk=2, days=10)


@pytest.mark.django_db
def test_user_password_never_reaches_the_report(owner, make_user):
    person = make_user("person")
    http = http_for(owner)
    account_change(http, person, groups=[Group.objects.get_or_create(name="supervisor")[0].pk])
    person.user_permissions.add(review_permission())
    http = http_for(person)
    text = http.get(reverse("access_review:accounts")).content.decode()
    export = http.get(reverse("access_review:accounts-export")).content.decode()
    for value in (PASSWORD, person.password, owner.password, person.email):
        assert value not in text and value not in export
    assert User.objects.filter(pk=person.pk).exists()


def _migrate(target):
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate(target or executor.loader.graph.leaf_nodes("audit_review"))


@pytest.mark.parametrize("recorded", [False, True])
@pytest.mark.django_db(transaction=True)
def test_reversing_the_migration_is_refused_once_an_access_review_use_is_recorded(recorded):
    from django.db import DatabaseError
    from django.db.migrations.recorder import MigrationRecorder

    if recorded:
        WorkspaceUse.objects.create(kind="viewed", source=Source.UNUSED, account_id=7, rows=0)
    try:
        if recorded:
            with pytest.raises(DatabaseError, match="Access review uses are recorded"):
                _migrate([("audit_review", "0003_review_records")])
            assert WorkspaceUse.objects.get().source == Source.UNUSED
        else:
            _migrate([("audit_review", "0003_review_records")])
        applied = MigrationRecorder.Migration.objects.filter(
            app="audit_review", name="0004_access_report"
        ).exists()
        assert applied == recorded
    finally:
        _migrate(None)
    assert UnusedPeriod.objects.count() == 0
