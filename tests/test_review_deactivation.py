"""Independent checks of the deactivation workflow using synthetic accounts and tickets."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from html import escape, unescape
from html.parser import HTMLParser
from io import StringIO

import pytest
from django.contrib.auth.models import AnonymousUser, Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client as HttpClient
from django.urls import reverse

from accounts import deactivation, grant_ends
from accounts.acting import ACCESS_GATE, INT4
from accounts.models import AccountSession, CapabilityEnd, Deactivation, User
from accounts.roles import ROLES, has_capability
from accounts.tests.test_confirm_identity import page_data
from accounts.tests.test_deactivation import (
    PASSWORD,
    browser_for,
    capability,
    change_url,
    make_owner,
    page_url,
)
from accounts.tests.test_deactivation import assigned as assigned
from accounts.tests.test_deactivation import owner as owner
from accounts.tests.test_deactivation import person as person
from accounts.tests.test_deactivation import team as team
from clients.models import ClientSite, PortalAccess
from clients.portal import current_access
from core.models import AuditEvent
from desk import services
from desk.models import Status
from desk.tests.test_portal import raise_staff, set_status
from jobs.actor import system_account
from teams.models import Team, TeamMembership

pytestmark = pytest.mark.django_db

REASON = "Synthetic rotation completed"
DIRECT = "view_patient_information"


class DrawnInput(HTMLParser):
    def handle_starttag(self, tag, attrs):
        field = dict(attrs)
        if tag == "input" and field.get("name") == "drawn":
            self.tokens.append(field["value"])

    def __init__(self, response):
        super().__init__()
        self.tokens = []
        self.feed(response.content.decode())


def token(response):
    assert response.status_code == 200
    (value,) = DrawnInput(response).tokens
    return value


def submit(http, target, preview, reason=REASON, **fields):
    return http.post(page_url(target), {"drawn": token(preview), "reason": reason, **fields})


def account_data(http, target, **changes):
    form = http.get(change_url(target)).context["adminform"].form
    return page_data(form, **changes)


def account_events(target):
    return AuditEvent.objects.filter(
        object_type="accounts.user", object_id=str(target.pk), action="account.updated"
    )


def label_payload(response):
    """Only the account's displayed holdings, excluding field help and admin navigation."""
    html = response.content.decode()
    return unescape(html.split('<ul class="vr-held">', 1)[1].split("</ul>", 1)[0])


@pytest.mark.parametrize(
    "audience",
    [*ROLES, "anonymous", "staff only", "no role", "superuser only", "inactive owner", "portal"],
)
def test_only_an_owner_can_read_or_submit_either_account_page(owner, person, client_org, audience):
    if audience == "anonymous":
        actor, http = AnonymousUser(), HttpClient()
    else:
        actor = User.objects.create_user(
            "reader",
            "reader@example.invalid",
            PASSWORD,
            is_staff=audience in (*ROLES, "staff only", "inactive owner"),
            is_superuser=audience in ("superuser only", "inactive owner"),
            is_active=audience != "inactive owner",
            client=client_org if audience == "portal" else None,
        )
        if audience in ROLES:
            actor.groups.add(Group.objects.get_or_create(name=audience)[0])
        http = browser_for(actor)
    admin = browser_for(owner)
    drawn = token(admin.get(page_url(person)))
    for url, data in (
        (page_url(person), {"drawn": drawn, "reason": REASON}),
        (change_url(person), account_data(admin, person, is_active="")),
    ):
        for response in (http.get(url), http.post(url, data)):
            if response.status_code == 302:
                assert response.url.split("?")[0] == reverse("admin:login")
            else:
                assert response.status_code == 403
            assert "Bench chemistry" not in response.content.decode()
    with pytest.raises(PermissionDenied):
        deactivation.deactivate(actor=actor, account_id=person.pk, drawn=drawn, reason=REASON)
    person.refresh_from_db()
    assert person.is_active and not Deactivation.objects.filter(user=person).exists()
    assert not account_events(person).exists()


def test_scheduled_jobs_is_read_only_and_own_account_has_no_action(owner):
    jobs = system_account()
    http = browser_for(owner)
    for target in (owner, jobs):
        change = http.get(change_url(target))
        assert change.status_code == 200
        assert f'href="{page_url(target)}"' not in change.content.decode()
    assert "This is your own account" in http.get(change_url(owner)).content.decode()
    jobs_form = http.get(change_url(jobs))
    line = jobs_form.content.decode().split('field-deactivation"', 1)[1].split("</div>", 2)
    assert "—" in "".join(line)
    assert http.post(change_url(jobs), {"is_active": "on"}).status_code == 403
    for response in (http.get(page_url(jobs)), http.post(page_url(jobs), {"reason": REASON})):
        assert response.status_code == 403
    with pytest.raises(ValidationError, match="scheduled jobs"):
        deactivation.deactivate(actor=owner, account_id=jobs.pk, reason=REASON, drawn="")
    jobs.refresh_from_db()
    assert not jobs.is_active and not Deactivation.objects.filter(user=jobs).exists()


@pytest.mark.parametrize("scope", ["whole", "chosen sites", "no grant", "ended"])
def test_portal_scope_matches_preview_history_and_reactivation(
    owner, client_org, portal_user, scope
):
    target = portal_user(client_org)
    site = ClientSite.objects.create(client=client_org, name="South bench", code="SOUTH")
    last_day = grant_ends.today() + timedelta(days=-2 if scope == "ended" else 3)
    if scope != "no grant":
        access = PortalAccess.objects.create(
            user=target,
            client=client_org,
            whole_organization=scope != "chosen sites",
            ends_at=grant_ends.end_of(last_day),
        )
        if scope == "chosen sites":
            access.sites.add(site)
    expected = {
        "whole": f"Its organization's tickets, last day {last_day}",
        "chosen sites": f"Tickets of South bench, and those it raised, last day {last_day}",
        "no grant": "Only the tickets it raised",
        "ended": f"Only the tickets it raised (access ended after {last_day})",
    }[scope]
    expected_held = {
        "kind": "Client portal",
        "organization": client_org.name,
        "console": [],
        "roles": [],
        "capabilities": [],
        "teams": [],
        "portal": expected,
    }
    http = browser_for(owner)
    preview = http.get(page_url(target))
    assert expected in label_payload(preview)
    saved = submit(http, target, preview)
    assert saved.status_code == 302 and saved.url == change_url(target)
    row = Deactivation.objects.get(user=target)
    assert row.held == expected_held
    assert expected in label_payload(http.get(saved.url))
    target.refresh_from_db()
    assert not target.is_active and current_access(target) is None
    response = http.post(change_url(target), account_data(http, target, is_active="on"))
    assert response.status_code == 302
    target.refresh_from_db()
    assert target.is_active
    assert (current_access(target) is not None) == (scope in ("whole", "chosen sites"))
    assert Deactivation.objects.filter(user=target).count() == 1


def test_same_save_captures_flags_role_capability_and_end_before_their_removal(owner, person):
    person.is_staff = True
    person.save(update_fields=["is_staff"])
    last_day = grant_ends.today() - timedelta(days=2)
    grant = CapabilityEnd.objects.get(user=person)
    grant.ends_at = grant_ends.end_of(last_day)
    grant.save()
    http = browser_for(owner)
    words = label_payload(http.get(page_url(person)))
    assert "Admin console: Admin console" in words
    assert f"Directly, ended after {last_day}, gives nothing" in words
    changes = account_data(
        http,
        person,
        is_active="",
        is_staff="",
        groups=[],
        user_permissions=[],
        last_day_view_patient_information="",
    )
    result = http.post(change_url(person), changes)
    assert result.status_code == 302
    person.refresh_from_db()
    assert not (person.is_active or person.is_staff or person.groups.exists())
    assert not (
        person.user_permissions.exists() or CapabilityEnd.objects.filter(user=person).exists()
    )
    row = Deactivation.objects.get(user=person)
    assert (row.reason, row.deactivated_by_id) == ("", owner.pk)
    assert row.held["console"] == ["Admin console"] and row.held["roles"] == ["Supervisor"]
    assert {
        "text": "Can view patient information (ended)",
        "note": f"Directly, ended after {last_day}, gives nothing",
    } in row.held["capabilities"]
    assert label_payload(http.get(change_url(person))) == words


def test_two_deactivations_keep_separate_readings_and_show_the_current_saver_name(
    owner, person, team
):
    http = browser_for(owner)
    other = make_owner("second-review-owner")
    first = submit(http, person, http.get(page_url(person)), reason="First retirement")
    assert first.status_code == 302
    stale = User.objects.get(pk=person.pk)
    assert not stale.is_active and not has_capability(stale, DIRECT)
    again = http.post(change_url(person), account_data(http, person, is_active="on"))
    assert again.status_code == 302
    active = User.objects.get(pk=person.pk)
    assert active.is_active and has_capability(active, DIRECT)
    made_active = http.get(change_url(person)).content.decode()
    assert "Made active again since" in made_active and "First retirement" in made_active
    team.name = "Second bench"
    team.save(update_fields=["name"])
    stale.changed_by = other
    stale.save()
    stale.save(update_fields=["is_active"])
    rows = list(Deactivation.objects.filter(user=person).order_by("pk"))
    assert [(r.reason, r.held["teams"][0]["text"]) for r in rows] == [
        ("First retirement", "Bench chemistry"),
        ("", "Second bench"),
    ]
    other.username = "renamed-second-owner"
    other.save(update_fields=["username"])
    history = http.get(change_url(person)).content.decode()
    assert "by renamed-second-owner." in history and "First retirement" not in history
    assert "Second bench" in label_payload(http.get(change_url(person)))


@pytest.mark.parametrize("bad_reason", ["", "   ", "x" * 1001], ids=["empty", "spaces", "too-long"])
def test_invalid_reason_redraws_the_current_list_and_preserves_it_on_next_post(
    owner, person, bad_reason
):
    http = browser_for(owner)
    preview = http.get(page_url(person))
    tech = Group.objects.get_or_create(name="technical")[0]
    person.groups.add(tech)
    rejected = submit(http, person, preview, reason=bad_reason)
    assert rejected.status_code == 200 and "reason" in rejected.context["form"].errors
    assert "Technical staff" in label_payload(rejected)
    assert token(rejected) != token(preview)
    person.groups.remove(tech)
    stale = submit(http, person, rejected)
    assert "changed after you opened" in stale.content.decode()
    assert "Technical staff" not in label_payload(stale)
    assert token(stale) == token(preview)
    assert not Deactivation.objects.filter(user=person).exists()
    saved = submit(http, person, stale)
    assert saved.status_code == 302 and Deactivation.objects.get(user=person).reason == REASON


@pytest.mark.confirmation
def test_confirmation_refusal_and_accepted_confirmation_with_a_bad_reason(owner, person):
    http = browser_for(owner)
    original = http.get(page_url(person))
    person.groups.add(Group.objects.get_or_create(name="technical")[0])
    rejected = submit(http, person, original)
    assert "confirm_password" in rejected.context["form"].errors
    assert rejected.context["form"]["reason"].value() == REASON
    assert "Technical staff" in label_payload(rejected)
    assert token(rejected) != token(original)
    bad_reason = submit(http, person, rejected, reason=" ", confirm_password=PASSWORD)
    assert "reason" in bad_reason.context["form"].errors
    assert 'name="confirm_password"' not in bad_reason.content.decode()
    assert User.objects.get(pk=person.pk).is_active
    assert not Deactivation.objects.filter(user=person).exists()
    result = submit(http, person, bad_reason)
    assert result.status_code == 302 and Deactivation.objects.get(user=person).reason == REASON


def test_two_admin_sessions_when_target_is_deactivated_then_reactivated(owner, person):
    first = browser_for(owner)
    second = browser_for(make_owner("review-other"))
    opened = first.get(page_url(person))
    assert (
        submit(second, person, second.get(page_url(person)), reason="Second owner").status_code
        == 302
    )
    inactive = submit(first, person, opened)
    assert "already deactivated" in inactive.content.decode()
    assert not DrawnInput(inactive).tokens
    assert Deactivation.objects.filter(user=person).count() == 1
    restored = second.post(change_url(person), account_data(second, person, is_active="on"))
    assert restored.status_code == 302
    # Only the displayed holdings are fingerprinted. All of them are back as drawn.
    assert submit(first, person, opened, reason="First owner").status_code == 302
    assert list(
        Deactivation.objects.filter(user=person).order_by("pk").values_list("reason", flat=True)
    ) == ["Second owner", "First owner"]


def test_only_open_assignments_at_save_are_counted_and_live_sessions_end(
    owner, person, team, assigned
):
    http = browser_for(owner)
    preview = http.get(page_url(person))
    assert "1 open ticket now" in preview.content.decode()
    session = browser_for(person).session["_account_session"]
    resolved = raise_staff(person, assigned.category, assigned.client, team=team, assignee=person)
    set_status(resolved, Status.RESOLVED)
    closed = raise_staff(person, assigned.category, assigned.client, team=team, assignee=person)
    set_status(closed, Status.CLOSED)
    saved = submit(http, person, preview)
    assert saved.status_code == 302
    assert AccountSession.objects.get(pk=session).ended_at
    assert Deactivation.objects.get(user=person).tickets_returned == 2
    for ticket, assignee_id in [(resolved, None), (assigned, None), (closed, person.pk)]:
        ticket.refresh_from_db()
        assert ticket.assignee_id == assignee_id and ticket.team_id == team.pk


@pytest.mark.django_db(transaction=True)
def test_failed_release_rolls_back_session_ticket_record_and_event(
    owner, person, assigned, monkeypatch
):
    target_session = browser_for(person).session["_account_session"]
    http = browser_for(owner)
    preview = http.get(page_url(person))
    release = services.release_assignments

    def interrupt(**kwargs):
        assert release(**kwargs) == 1
        raise RuntimeError("synthetic release failure")

    with monkeypatch.context() as patch:
        patch.setattr(services, "release_assignments", interrupt)
        with pytest.raises(RuntimeError, match="synthetic release failure"):
            submit(http, person, preview)
    person.refresh_from_db()
    assigned.refresh_from_db()
    assert person.is_active and assigned.assignee_id == person.pk
    assert AccountSession.objects.get(pk=target_session).ended_at is None
    assert not (
        Deactivation.objects.filter(user=person).exists() or account_events(person).exists()
    )
    assert submit(http, person, preview).status_code == 302
    assert Deactivation.objects.get(user=person).tickets_returned == 1


def test_thousand_character_reason_is_stored_verbatim_and_escaped_on_the_account(owner, person):
    markup = "<em>rotation</em>"
    reason = markup + "x" * (1000 - len(markup))
    http = browser_for(owner)
    saved = submit(http, person, http.get(page_url(person)), reason=reason)
    assert saved.status_code == 302
    assert (
        Deactivation.objects.get(user=person).reason
        == account_events(person).get().reason
        == reason
    )
    html = http.get(saved.url).content.decode()
    assert escape(reason) in html and markup not in html


def test_deactivate_event_omits_the_account_email(owner, person):
    http = browser_for(owner)
    saved = submit(http, person, http.get(page_url(person)))
    assert saved.status_code == 302
    event = account_events(person).get()
    assert all(person.email not in json.dumps(value) for value in (event.before, event.after))


def test_event_keeps_the_end_in_both_account_snapshots(owner, person):
    http = browser_for(owner)
    end = CapabilityEnd.objects.get(user=person)
    assert submit(http, person, http.get(page_url(person))).status_code == 302
    event = account_events(person).get()
    expected = {f"accounts.{DIRECT}": grant_ends.iso(end.ends_at)}
    assert (event.before.get("capability_ends"), event.after.get("capability_ends")) == (
        expected,
        expected,
    )


@pytest.mark.django_db(transaction=True)
def test_saved_time_is_the_database_clock_when_the_record_is_written(person):
    # Round 1 found the model comment promised the transaction's start; the comment now says the
    # database's clock when the record is written, inside the deactivating transaction.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT transaction_timestamp()")
        (started,) = cursor.fetchone()
        person.is_active = False
        person.save(update_fields=["is_active"])
        cursor.execute("SELECT clock_timestamp()")
        (after,) = cursor.fetchone()
        assert started <= Deactivation.objects.get(user=person).deactivated_at <= after


@pytest.mark.django_db(transaction=True)
def test_migration_preserves_old_accounts_and_reverses_with_a_record():
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    old = [("accounts", "0011_grant_ends")]
    new = [("accounts", "0012_deactivation")]
    try:
        executor.migrate(old)
        OldUser = executor.loader.project_state(old).apps.get_model("accounts", "User")
        accounts = [
            OldUser.objects.create(
                username=f"legacy-{active}",
                email=f"legacy-{active}@example.invalid",
                is_active=active,
            ).pk
            for active in (True, False)
        ]
        MigrationExecutor(connection).migrate(new)
        assert not Deactivation.objects.exists()
        target = User.objects.get(pk=accounts[0], is_active=True)
        assert User.objects.filter(pk=accounts[1], is_active=False).exists()
        target.is_active = False
        target.save(update_fields=["is_active"])
        assert Deactivation.objects.get(user=target).held["kind"] == "Staff"
        MigrationExecutor(connection).migrate(old)
        assert "accounts_deactivation" not in connection.introspection.table_names()
        assert OldUser.objects.filter(pk__in=accounts, is_active=False).count() == 2
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regprocedure('accounts_deactivation_guard()')")
            assert cursor.fetchone() == (None,)
    finally:
        MigrationExecutor(connection).migrate(leaves)


@pytest.mark.django_db(transaction=True)
def test_demo_reset_reseeds_with_deactivation_history_present(monkeypatch, settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-review-demo-password")
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    output = StringIO()
    call_command("seed_demo", stdout=output)
    user = User.objects.get(username="demo_technical")
    user.is_active = False
    user.save(update_fields=["is_active"])
    assert Deactivation.objects.filter(user=user).exists()
    call_command("reset_demo", interactive=False, stdout=output)
    assert User.objects.get(username="demo_technical").is_active
    assert not Deactivation.objects.filter(user__username="demo_technical").exists()


def other_connection(operation, *args):
    """Run one operation on the worker thread's connection and always release it."""
    try:
        return operation(*args)
    finally:
        connections.close_all()


def backend_pid():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def holds_access_gate(pid, target_id):
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s
                 AND locktype = 'advisory' AND classid = %s AND objid = %s
                 AND objsubid = 2 AND mode = 'ExclusiveLock' AND granted)""",
            [pid, ACCESS_GATE, target_id % INT4],
        )
        return cursor.fetchone()[0]


def edit_to_post(kind, editor, person, team, portal):
    if kind == "portal":
        url = reverse("clients:portal-access-edit", args=[portal.client_id, portal.pk])
        form = editor.get(url).context["form"]
        return portal, url, page_data(form, scope="organization")
    if kind == "team add":
        extra = Team.objects.create(name="Night shift", slug="night-shift")
        return person, reverse("teams:team-detail", args=[extra.pk]), {"user": person.pk}
    member = TeamMembership.objects.get(team=team, user=person)
    if kind == "team lead":
        return person, reverse("teams:member-lead", args=[team.pk, member.pk]), {"is_lead": "0"}
    if kind == "team remove":
        return person, reverse("teams:member-remove", args=[team.pk, member.pk]), {}
    if kind == "group capability":
        role = person.groups.get()
        url = reverse("admin:auth_group_change", args=[role.pk])
        form = editor.get(url).context["adminform"].form
        permissions = [*role.permissions.values_list("pk", flat=True), capability(DIRECT).pk]
        return person, url, page_data(form, permissions=permissions)
    if kind == "role":
        technical = Group.objects.get_or_create(name="technical")[0]
        changes = {"groups": [person.groups.get().pk, technical.pk]}
    elif kind == "end":
        changes = {"last_day_view_patient_information": grant_ends.today() + timedelta(days=14)}
    else:
        changes = {"user_permissions": [], "last_day_view_patient_information": ""}
    return person, change_url(person), account_data(editor, person, **changes)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "kind",
    [
        "role",
        "direct capability",
        "end",
        "team remove",
        "portal",
        *(
            pytest.param(
                kind,
                marks=pytest.mark.xfail(
                    strict=True,
                    reason="Documented limit (accounts/deactivation.py, docs/architecture.md): an "
                    "addition on the team page or the role group page takes no gate of the member "
                    "(accounts/acting.py), so one committed after the deactivating save's reading "
                    "is not in the record; it gives the deactivated account nothing.",
                ),
            )
            for kind in ("team add", "team lead", "group capability")
        ),
    ],
)
def test_access_edit_page_holds_the_target_gate_through_commit(
    owner, person, team, client_org, portal_user, kind
):
    owner.groups.add(Group.objects.get_or_create(name="admin")[0])
    editor, reviewer = browser_for(owner), browser_for(make_owner("lock-observer"))
    portal = portal_user(client_org)
    target, url, data = edit_to_post(kind, editor, person, team, portal)
    original = reviewer.get(page_url(target))
    with ThreadPoolExecutor(max_workers=1) as pool, transaction.atomic():
        response = editor.post(url, data)
        assert response.status_code == 302, response.content.decode()[-1500:]
        held = pool.submit(other_connection, holds_access_gate, backend_pid(), target.pk).result(10)
    # Prove the edit changed an item on this particular account's displayed list.
    refreshed = reviewer.get(page_url(target))
    assert label_payload(original) != label_payload(refreshed)
    assert token(original) != token(refreshed)
    assert "changed after you opened" in submit(reviewer, target, original).content.decode()
    assert User.objects.get(pk=target.pk).is_active
    assert held, f"The {kind} edit did not hold the displayed account's access gate"


def waiting_post(pool, client, url, data):
    """Start a second request and wait until PostgreSQL sees the caller blocking it."""
    from queue import Queue
    from time import monotonic, sleep

    holder = backend_pid()
    pids = Queue()

    def post():
        pids.put(backend_pid())
        return client.post(url, data)

    future = pool.submit(other_connection, post)
    waiter = pids.get(timeout=10)
    limit = monotonic() + 8
    with connection.cursor() as cursor:
        while monotonic() < limit:
            cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [holder, waiter])
            if cursor.fetchone()[0]:
                return future
            assert not future.done(), "Second request completed before the first committed"
            sleep(0.02)
    pytest.fail("PostgreSQL did not observe the second request waiting for the first")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["role", "direct capability", "end", "team remove", "portal"])
def test_deactivate_waits_for_a_pending_edit_and_redraws_the_committed_change(
    owner, person, team, client_org, portal_user, kind
):
    owner.groups.add(Group.objects.get_or_create(name="admin")[0])
    editor, reviewer = browser_for(owner), browser_for(make_owner("review-second"))
    target, url, data = edit_to_post(kind, editor, person, team, portal_user(client_org))
    preview = reviewer.get(page_url(target))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            assert editor.post(url, data).status_code == 302
            future = waiting_post(
                pool, reviewer, page_url(target), {"drawn": token(preview), "reason": REASON}
            )
        refusal = future.result(timeout=10)
    assert refusal.status_code == 200 and "changed after you opened" in refusal.content.decode()
    assert label_payload(refusal) != label_payload(preview)
    fresh = reviewer.get(page_url(target))
    assert label_payload(refusal) == label_payload(fresh) and token(refusal) == token(fresh)
    assert (
        User.objects.get(pk=target.pk).is_active
        and not Deactivation.objects.filter(user=target).exists()
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first", ["Deactivate", "Active box"])
def test_both_admin_forms_saving_the_same_account_commit_one_record(owner, person, first):
    deactivate_http = browser_for(owner)
    active_owner = make_owner("checkbox-owner")
    active_http = browser_for(active_owner)
    posts = {
        "Deactivate": (
            deactivate_http,
            page_url(person),
            {"reason": REASON, "drawn": token(deactivate_http.get(page_url(person)))},
        ),
        "Active box": (
            active_http,
            change_url(person),
            account_data(active_http, person, is_active=""),
        ),
    }
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            client, url, data = posts.pop(first)
            assert client.post(url, data).status_code == 302
            (other_post,) = posts.values()
            future = waiting_post(pool, *other_post)
        refused = future.result(timeout=10)
    assert refused.status_code == 200
    if first == "Deactivate":
        assert "Someone else saved this account" in str(
            refused.context["adminform"].form.non_field_errors()
        )
    else:
        assert "already deactivated" in refused.content.decode()
    row = Deactivation.objects.get(user=person)
    assert (row.deactivated_by_id, row.reason) == (
        (owner.pk, REASON) if first == "Deactivate" else (active_owner.pk, "")
    )
    assert account_events(person).get().actor_id == row.deactivated_by_id


@pytest.mark.django_db(transaction=True)
@pytest.mark.confirmation
@pytest.mark.parametrize("refusal", ["reason", "confirmation", "changed holdings"])
def test_redraw_reads_the_list_after_the_refusal(owner, person, monkeypatch, refusal):
    from accounts.admin import DeactivateForm

    http = browser_for(owner)
    editor = browser_for(make_owner("redraw-editor"))
    preview = http.get(page_url(person))
    technical = Group.objects.get_or_create(name="technical")[0]
    role_ids = [person.groups.get().pk, technical.pk]
    if refusal == "changed holdings":
        # Give the service an actual changed list to refuse on its locked reading.
        assert (
            editor.post(
                change_url(person),
                account_data(editor, person, groups=role_ids, confirm_password=PASSWORD),
            ).status_code
            == 302
        )
    manager = Group.objects.get_or_create(name="account_manager")[0]
    edit = account_data(editor, person, groups=[*role_ids, manager.pk], confirm_password=PASSWORD)
    committed = []

    def finish_other_edit():
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(other_connection, editor.post, change_url(person), edit).result(10)
        assert result.status_code == 302
        committed.append(True)

    if refusal == "changed holdings":
        service = deactivation.deactivate

        def save(**kwargs):
            try:
                return service(**kwargs)
            except ValidationError:
                finish_other_edit()
                raise

        monkeypatch.setattr(deactivation, "deactivate", save)
    else:
        validate = DeactivateForm.is_valid

        def validate_form(form):
            valid = validate(form)
            if not valid:
                finish_other_edit()
            return valid

        monkeypatch.setattr(DeactivateForm, "is_valid", validate_form)
    bad = submit(
        http,
        person,
        preview,
        reason="" if refusal == "reason" else REASON,
        confirm_password="" if refusal == "confirmation" else PASSWORD,
    )
    assert committed == [True] and bad.status_code == 200
    assert not Deactivation.objects.filter(user=person).exists()
    refreshed = http.get(page_url(person))
    assert "Account manager" in label_payload(refreshed)
    retry = submit(http, person, bad, confirm_password=PASSWORD)
    assert (
        "Account manager" in label_payload(bad),
        token(bad) == token(refreshed),
        retry.status_code,
    ) == (True, True, 302)


@pytest.mark.django_db(transaction=True)
@pytest.mark.confirmation
def test_deactivation_artifacts_omit_credentials_and_ticket_content(
    owner, person, assigned, caplog
):
    from accounts.tests.test_confirm_identity import with_authenticator
    from links.models import ExternalSystem
    from links.services import add_link

    secret = with_authenticator(person)
    person_reference = "SYNTHETIC-PERSON-57053"
    system = ExternalSystem.objects.create(key="review-source", name="Review source")
    add_link(
        actor=person,
        system=system,
        record_type="patient",
        identifier=person_reference,
        request_id=assigned.pk,
        expected_revision=assigned.revision,
    )
    browser_for(person)
    http = browser_for(owner)
    caplog.clear()
    caplog.set_level(logging.INFO, logger="lab_ops")
    preview = http.get(page_url(person))
    result = submit(http, person, preview, confirm_password=PASSWORD)
    assert result.status_code == 302
    history = http.get(result.url)
    event = account_events(person).get()
    row = Deactivation.objects.get(user=person)
    assert row.tickets_returned == 1 and row.reason == event.reason == REASON
    assert any("method=POST" in rec.getMessage() for rec in caplog.records)
    for material in (
        preview.content.decode(),
        history.content.decode(),
        json.dumps(row.held),
        json.dumps([event.before, event.after, event.reason]),
        caplog.text,
    ):
        for marker in (secret, person_reference, person.password, PASSWORD, assigned.subject):
            assert marker not in material
    for material in (
        preview.content.decode(),
        label_payload(history),
        json.dumps(row.held),
        caplog.text,
    ):
        assert person.email not in material
    assert REASON not in caplog.text


def test_grantless_group_is_absent_but_a_named_group_grant_and_inactive_team_are_kept(
    owner, person, team
):
    empty = Group.objects.create(name="Unused review group")
    grant = Group.objects.create(name="Night review")
    grant.permissions.add(capability("review_audit_records"))
    person.groups.add(empty, grant)
    team.is_active = False
    team.save(update_fields=["is_active"])
    http = browser_for(owner)
    preview = http.get(page_url(person))
    assert empty.name not in label_payload(preview)
    assert grant.name in label_payload(preview)
    assert "Bench chemistry (lead, inactive team)" in label_payload(preview)
    assert submit(http, person, preview).status_code == 302
    record = Deactivation.objects.get(user=person)
    assert empty.name not in json.dumps(record.held)
    assert record.held["roles"] == ["Supervisor"]
    assert {
        "text": "Can review access and audit records",
        "note": "Through the group “Night review”",
    } in record.held["capabilities"]


def test_an_owner_gets_not_found_for_a_missing_account(owner):
    http = browser_for(owner)
    missing = User.objects.order_by("-pk").first().pk + 1
    url = reverse("admin:accounts_user_deactivate", args=[missing])
    for answer in (http.get(url), http.post(url, {"drawn": "", "reason": REASON})):
        assert answer.status_code == 404


@pytest.mark.parametrize("case", ["own account", "missing reason"])
def test_direct_service_reports_permission_denied_for_a_nonowner(person, owner, case):
    with pytest.raises(PermissionDenied):
        deactivation.deactivate(
            actor=person,
            account_id=person.pk if case == "own account" else owner.pk,
            drawn="",
            reason=REASON if case == "own account" else "",
        )


def test_reason_is_private_from_the_person_after_an_owner_account_is_restored(owner):
    target = make_owner("restored-owner")
    http = browser_for(owner)
    assert submit(http, target, http.get(page_url(target))).status_code == 302
    active = http.post(change_url(target), account_data(http, target, is_active="on"))
    assert active.status_code == 302
    target.refresh_from_db()
    assert target.is_active
    subject = browser_for(target)
    own_history = subject.get(reverse("account-sign-ins"))
    assert own_history.status_code == 200 and REASON not in own_history.content.decode()
    url = reverse("admin:accounts_user_history", args=[target.pk])
    admin_history = subject.get(url)
    assert admin_history.status_code == 200 and REASON not in admin_history.content.decode()
    own_form = subject.get(change_url(target))
    assert own_form.status_code == 200
    assert REASON not in own_form.content.decode()


def test_reactivation_advice_for_an_already_ended_grant(owner, person):
    end = CapabilityEnd.objects.get(user=person)
    end.ends_at = grant_ends.end_of(grant_ends.today() - timedelta(days=2))
    end.save()
    http = browser_for(owner)
    preview = http.get(page_url(person))
    assert "ended after" in label_payload(preview)
    assert submit(http, person, preview).status_code == 302
    assert (
        http.post(change_url(person), account_data(http, person, is_active="on")).status_code == 302
    )
    person.refresh_from_db()
    assert person.is_active and not has_capability(person, DIRECT)
    assert person.user_permissions.filter(pk=end.permission_id).exists()
    # The displayed advice must not promise restoration of access that has already ended.
    assert "which gives all of it back" not in preview.content.decode()


@pytest.mark.xfail(
    strict=True,
    reason="By design: the admin console shows a username as it was chosen, even an address, on "
    "every account page (its title, breadcrumbs, Mark as compromised, End sessions); the record "
    "holds no username or email address, and the event has no email field (it names the account "
    "by its username, as every account change event does).",
)
def test_deactivate_uses_a_safe_account_label_when_the_username_is_an_address(owner):
    address = "review-account@example.invalid"
    person = User.objects.create_user(address, address, PASSWORD)
    http = browser_for(owner)
    preview = http.get(page_url(person))
    assert preview.status_code == 200
    assert address not in preview.content.decode()


def test_active_box_uses_the_account_edit_event_and_admin_history_wording(owner, person):
    from django.contrib.admin.models import LogEntry

    http = browser_for(owner)
    response = http.post(change_url(person), account_data(http, person, is_active=""))
    assert response.status_code == 302 and Deactivation.objects.get(user=person).reason == ""
    assert account_events(person).get().reason == "Django admin"
    message = LogEntry.objects.get(object_id=str(person.pk)).get_change_message()
    assert "Active" in message and "Deactivated the account." not in message


def test_plain_model_save_keeps_a_record_without_an_admin_history_entry(person):
    from django.contrib.admin.models import LogEntry

    person.is_active = False
    person.save()
    assert Deactivation.objects.get(user=person).reason == ""
    assert not account_events(person).exists()
    assert not LogEntry.objects.filter(object_id=str(person.pk)).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.confirmation
def test_two_owners_submitting_their_confirmed_forms_leave_one_active():
    from threading import Barrier

    owners = [make_owner("east-owner"), make_owner("west-owner")]
    requests = []
    for actor, target in zip(owners, reversed(owners), strict=True):
        http = browser_for(actor)
        preview = http.get(page_url(target))
        requests.append((http, target, token(preview)))
    ready = Barrier(2)

    def post(http, target, drawn):
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout TO '10s'")
        ready.wait(timeout=10)
        return http.post(
            page_url(target), {"reason": REASON, "drawn": drawn, "confirm_password": PASSWORD}
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(other_connection, post, *args) for args in requests]
        responses = [future.result(timeout=20) for future in futures]
    record = Deactivation.objects.get(user__in=owners)
    assert account_events(record.user).get().actor_id == record.deactivated_by_id
    for (_, target, _), response in zip(requests, responses, strict=True):
        target.refresh_from_db()
        if record.user_id == target.pk:
            assert (
                not target.is_active
                and response.status_code == 302
                and response.url == change_url(target)
            )
        else:
            # A request refused by the service, or signed out earlier by session middleware.
            assert target.is_active
            if response.status_code == 302:
                assert response.url.split("?")[0] in (
                    reverse("admin:login"),
                    reverse("account_login"),
                )
            else:
                assert response.status_code == 403


@pytest.mark.django_db(transaction=True)
def test_audit_write_failure_rolls_back_the_already_inserted_record(
    owner, person, assigned, monkeypatch
):
    from core import audit

    http = browser_for(owner)
    opened = http.get(page_url(person))
    session_id = browser_for(person).session["_account_session"]
    record_event = audit.record_event

    def interrupt(**event):
        if event["action"] == "account.updated":
            assert Deactivation.objects.get(user=person).tickets_returned == 1
            raise RuntimeError("synthetic audit write failure")
        return record_event(**event)

    monkeypatch.setattr(audit, "record_event", interrupt)
    with pytest.raises(RuntimeError, match="synthetic audit write failure"):
        submit(http, person, opened)
    person.refresh_from_db()
    assigned.refresh_from_db()
    assert person.is_active and assigned.assignee_id == person.pk
    assert AccountSession.objects.get(pk=session_id).ended_at is None
    assert not Deactivation.objects.filter(user=person).exists()
    assert not account_events(person).exists()
