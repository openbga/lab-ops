"""Independent regression checks for the laboratory's required enrolment workflow.

Written by the round-1 independent reviewer (Codex) at 6215b89 and adopted unchanged, except that
the two-tab Continue case is a strict xfail: Continue's page is kept in the browser's session,
which Django writes as a whole, so two tabs choosing Continue at the same moment can both go there
(documented in docs/architecture.md, "Required second factor"). The original is archived in
.local/review-history/required-second-factor-round-1-original/."""

import json
import logging
from importlib.util import find_spec
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import (
    SECRET_SESSION_KEY,
    TOTP,
    format_hotp_value,
    generate_totp_secret,
    hotp_value,
    yield_hotp_counters_from_time,
)
from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.test import Client as Browser
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import AccountSession
from clients.models import Client as Organization
from core.models import AuditEvent

from .conftest import PASSWORD
from .test_login import mailed_code, request_code

pytestmark = pytest.mark.django_db

SETUP = "/account/second-factor/"
SECURITY = "/lab/security/"
EDIT = "/lab/security/second-factor/"


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def policy():
    return pytest.importorskip("core.second_factor")


def password_sign_in(browser, user, destination=None):
    url = reverse("account_login")
    response = browser.post(
        url,
        {"login": user.username, "password": PASSWORD, "next": destination or "/"},
    )
    assert response.status_code == 302
    return response


def activate_app(browser):
    opened = browser.get(reverse("mfa_activate_totp"))
    if opened.status_code == 302:
        assert urlsplit(opened.url).path == reverse("account_reauthenticate")
        assert browser.post(opened.url, {"password": PASSWORD}).status_code == 302
        opened = browser.get(reverse("mfa_activate_totp"))
    assert opened.status_code == 200
    secret = browser.session[SECRET_SESSION_KEY]
    code = format_hotp_value(hotp_value(secret, next(yield_hotp_counters_from_time())))
    submitted = browser.post(reverse("mfa_activate_totp"), {"code": code})
    assert submitted.status_code == 302
    return submitted, secret


def help_text(file):
    return (Path(settings.BASE_DIR) / "docs" / "pages" / file).read_text()


@pytest.mark.parametrize(
    ("audience", "status"),
    [
        ("anonymous", 302),
        ("admin", 200),
        ("supervisor", 403),
        ("client_services", 403),
        ("account_manager", 403),
        ("technical", 403),
        ("no_role", 403),
        ("superuser", 403),
        ("portal", 403),
    ],
)
def test_direct_settings_and_setup_pages(client, make_user, audience, status):
    if audience != "anonymous":
        if audience == "portal":
            user = make_user("review_portal", client=Organization.objects.create(name="Review"))
        elif audience == "superuser":
            user = make_user("review_owner", is_superuser=True, is_staff=True)
        else:
            roles = () if audience == "no_role" else (audience,)
            user = make_user("review_staff", *roles)
        client.force_login(user)
    settings_page = client.get(SECURITY)
    assert settings_page.status_code == status
    if audience == "anonymous":
        assert urlsplit(settings_page.url).path == reverse("account_login")
    if find_spec("core.second_factor") is None:
        return  # The settings page exists on the previous release. The two new pages do not.
    form = client.get(EDIT)
    assert form.status_code == status
    submitted = client.post(EDIT, {"expected_revision": 0, "portal": "on", "reason": "Review."})
    assert submitted.status_code == (302 if audience == "admin" else status)
    events = AuditEvent.objects.filter(action="lab.second_factor_rule_changed")
    assert events.count() == int(audience == "admin")
    if audience == "anonymous":
        for response in (form, submitted, client.get(SETUP)):
            assert urlsplit(response.url).path == reverse("account_login")
    else:
        assert client.get(SETUP).url == reverse("dashboard")
        from core.models import SecondFactorRule

        SecondFactorRule.objects.update_or_create(pk=1, defaults={"staff": True, "portal": True})
        browser = Browser()
        browser.force_login(user)
        setup_page = browser.get(SETUP)
        assert setup_page.status_code == 200
        assert setup_page.context["reasons"] == [
            "client portal accounts" if audience == "portal" else "every staff account"
        ]


def test_signin_uses_the_rule_in_force_when_the_last_factor_completes(client, make_user, policy):
    admin = make_user("review_signin_admin", "admin")
    user = make_user("review_signin", "technical")
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    staged = password_sign_in(client, user)
    assert staged.url == reverse("mfa_authenticate")
    assert not AccountSession.objects.filter(user=user).exists()

    other = Browser()
    other.force_login(admin)
    assert (
        other.post(EDIT, {"staff": "on", "expected_revision": 0, "reason": "Review."}).url
        == SECURITY
    )
    counter = next(yield_hotp_counters_from_time())
    assert (
        client.post(
            staged.url, {"code": format_hotp_value(hotp_value(secret, counter))}
        ).status_code
        == 302
    )
    assert AccountSession.objects.get(user=user).second_factor_required


def test_current_rule_and_account_decision_use_one_statement(make_user, policy):
    user = make_user("review_snapshot", "technical")
    policy.SecondFactorRule.objects.create(pk=1, roles=["technical"])
    with CaptureQueriesContext(connection) as queries:
        assert policy.requires(user)
    assert len(queries) == 1, (
        "requires(user) promises one statement for the current account and rule"
    )


def test_a_head_request_leaves_the_first_browser_page_for_continue(client, make_user, policy):
    policy.SecondFactorRule.objects.create(pk=1, staff=True)
    user = make_user("review_head", "technical")
    password_sign_in(client, user)
    assert client.head("/whats-new/").url == SETUP
    assert client.get("/requests/").url == SETUP
    activate_app(client)
    assert client.get(SETUP).url == "/requests/"


def test_a_head_request_leaves_continue_for_the_next_get(client, make_user, policy):
    policy.SecondFactorRule.objects.create(pk=1, staff=True)
    password_sign_in(client, make_user("review_head_continue", "technical"))
    assert client.get("/requests/").url == SETUP
    activate_app(client)
    assert client.head(SETUP).status_code == 302
    assert client.get(SETUP).url == "/requests/"


@pytest.mark.parametrize("has_codes", [False, True], ids=["recovery-page", "overview"])
def test_continue_offers_the_start_page_after_direct_setup(client, make_user, policy, has_codes):
    policy.SecondFactorRule.objects.create(pk=1, staff=True)
    user = make_user("review_direct", "technical")
    if has_codes:
        RecoveryCodes.activate(user)
    assert password_sign_in(client, user, SETUP).url == SETUP
    assert client.get(SETUP).status_code == 200
    destination, _ = activate_app(client)
    expected = "mfa_index" if has_codes else "mfa_view_recovery_codes"
    assert destination.url == reverse(expected)
    page = client.get(destination.url)
    assert page.status_code == 200
    offers_continue = f'href="{SETUP}">Continue</a>'.encode() in page.content
    assert offers_continue
    assert client.get(SETUP).url == reverse("dashboard")


def test_a_person_with_existing_codes_is_told_where_activation_takes_them(
    client, make_user, policy
):
    policy.SecondFactorRule.objects.create(pk=1, staff=True)
    user = make_user("review_existing_codes", "technical")
    RecoveryCodes.activate(user)
    password_sign_in(client, user)
    assert client.get("/requests/").url == SETUP
    page = client.get(SETUP)
    assert page.status_code == 200
    destination, _ = activate_app(client)
    assert destination.url == reverse("mfa_index")
    assert b"Save the recovery codes shown next" not in page.content


def test_an_emailed_code_user_can_use_a_remaining_recovery_code_to_confirm_setup(
    client, make_user, settings
):
    """The same account can replace its lost app on both sides of the upgrade."""
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    user = make_user("review_email_only", "technical")
    user.set_unusable_password()
    user.save(update_fields=["password"])
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    stored_codes = RecoveryCodes.activate(user)
    if find_spec("core.second_factor"):
        from core.second_factor import SecondFactorRule

        SecondFactorRule.objects.create(pk=1, staff=True)
    request_code(client, user.email)
    client.post(reverse("account_confirm_login_code"), {"code": mailed_code()})
    assert client.session["_auth_user_id"] == str(user.pk)
    # Follow the links for replacing this account's app, using its available confirmation method.
    prompt = client.get(reverse("mfa_activate_totp"), follow=True)
    assert prompt.status_code == 200
    assert prompt.resolver_match.view_name == "mfa_reauthenticate"
    response = client.post(
        prompt.wsgi_request.get_full_path(), {"code": stored_codes.get_unused_codes()[0]}
    )
    assert response.status_code == 302
    setup_form = client.get(response.url)
    assert setup_form.status_code == 200
    assert setup_form.resolver_match.view_name == "mfa_activate_totp"
    secret = client.session[SECRET_SESSION_KEY]
    counter = next(yield_hotp_counters_from_time())
    enrolled = client.post(response.url, {"code": format_hotp_value(hotp_value(secret, counter))})
    assert enrolled.status_code == 302 and enrolled.url == reverse("mfa_index")
    assert Authenticator.objects.filter(user=user, type="totp").exists()


@pytest.mark.parametrize("area", ["activation", "additional-address"])
def test_email_help_matches_what_an_enrolled_owner_can_do(client, make_user, area):
    """The two releases ship different allauth settings and their own operator help."""
    from allauth.mfa.internal.flows.add import validate_can_add_authenticator
    from django.core.exceptions import ValidationError

    user = make_user("review_email_help", "technical")
    help_doc = help_text("accounts/email.json")
    if area == "activation":
        EmailAddress.objects.create(user=user, email=user.email, verified=False, primary=True)
        try:
            validate_can_add_authenticator(user)
        except ValidationError:
            blocked = True
        else:
            blocked = False
        assert (
            "MFA enrollment is separately blocked if any associated email record is unverified"
            in help_doc
        ) == blocked
    else:
        EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
        secret = generate_totp_secret()
        TOTP.activate(user, secret)
        challenge = password_sign_in(client, user)
        counter = next(yield_hotp_counters_from_time())
        client.post(challenge.url, {"code": format_hotp_value(hotp_value(secret, counter))})
        assert client.session["_auth_user_id"] == str(user.pk)
        alias = "review-alias@example.invalid"
        client.post(reverse("account_email"), {"action_add": "1", "email": alias})
        blocked = not EmailAddress.objects.filter(user=user, email=alias).exists()
        assert ("MFA currently blocks adding email addresses" in help_doc) == blocked


def test_account_without_any_email_row_can_enrol(client, make_user):
    user = make_user("review_no_email", "technical")
    user.email = ""
    user.save(update_fields=["email"])
    password_sign_in(client, user)
    assert not EmailAddress.objects.filter(user=user).exists()
    result, _ = activate_app(client)
    assert result.url == reverse("mfa_view_recovery_codes")
    assert Authenticator.objects.filter(user=user, type="totp").exists()


def test_settings_help_explains_the_displayed_second_factor_source(client, make_user):
    from core.security_settings import in_force

    client.force_login(make_user("review_help_source", "admin"))
    assert client.get(SECURITY).status_code == 200
    label = next(f.source_label for g in in_force() for f in g.facts if f.key == "second-factor")
    sections = json.loads(help_text("core/security-settings.json"))["sections"]
    description = next(
        item["description"]
        for section in sections
        for item in section.get("items", [])
        if item["label"] == "Set by"
    )
    assert label in description


def test_required_enrolment_log_matches_the_sign_in_help(client, make_user, caplog, policy):
    policy.SecondFactorRule.objects.create(pk=1, staff=True)
    user = make_user("review_first_signin", "technical")
    caplog.set_level(logging.INFO, logger="lab_ops.signin")
    password_sign_in(client, user)
    assert client.get(SETUP).status_code == 200
    assert not Authenticator.objects.filter(user=user).exists()
    recorded = any(
        record.getMessage() == f"signin.success user={user.username}" for record in caplog.records
    )
    assert not (
        recorded
        and "A completed sign-in is recorded only after any required second factor succeeds"
        in help_text("accounts/login.json")
    )


@pytest.mark.django_db(transaction=True)
def test_enrolment_logs_audit_messages_and_addresses_keep_private_codes_out(
    client, make_user, caplog, settings
):
    from django.contrib.messages import get_messages

    settings.ACCOUNT_EMAIL_NOTIFICATIONS = True
    caplog.set_level(logging.INFO, logger="lab_ops.signin")
    caplog.set_level(logging.INFO, logger="lab_ops.request")
    user = make_user("review_private", "technical")
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    if find_spec("core.second_factor"):
        from core import second_factor

        admin = make_user("review_private_admin", "admin")
        second_factor.save_rule(
            actor=admin, new={"staff": True}, expected_revision=0, reason="Synthetic policy."
        )
    password_sign_in(client, user)
    initial_page = "/requests/?view=all"
    client.get(initial_page)
    activated, secret = activate_app(client)
    page = client.get(activated.url)
    assert page.status_code == 200 and "no-store" in page["Cache-Control"]
    codes = RecoveryCodes(
        Authenticator.objects.get(user=user, type="recovery_codes")
    ).get_unused_codes()
    assert len(codes) == 10
    lines = [
        r.getMessage() for r in caplog.records if r.name in {"lab_ops.request", "lab_ops.signin"}
    ]
    assert any("method=" in line for line in lines)
    assert any("signin.success" in line for line in lines)
    events = list(AuditEvent.objects.values("action", "before", "after", "reason"))
    assert mail.outbox
    outputs = [
        *lines,
        json.dumps(events),
        *(m.body for m in mail.outbox),
        *(m.subject for m in mail.outbox),
        *(str(m) for m in get_messages(page.wsgi_request)),
        activated.url,
    ]
    if find_spec("core.second_factor"):
        from accounts.enrolment import NEXT_KEY

        assert client.session[NEXT_KEY] == initial_page
        outputs.append(client.get(SETUP).url)
    assert all(secret not in item and all(code not in item for code in codes) for item in outputs)


def test_panel_counts_match_session_flags_for_overlapping_groups(policy, make_user, client):
    from django.contrib.auth.models import Group, Permission

    from jobs.actor import system_account

    admin = make_user("review_count_admin", "admin")
    client.force_login(admin)
    supervisor = make_user("review_count_sup", "supervisor")
    tech = make_user("review_count_tech", "technical", is_staff=True)
    custom = make_user("review_count_custom")
    unassigned = make_user("review_count_unassigned")
    portal = make_user("review_count_portal", client=Organization.objects.create(name="Test lab"))
    inactive = make_user("review_count_inactive", "admin", is_active=False, is_staff=True)
    jobs = system_account()
    grant = Permission.objects.get(
        codename="view_patient_information", content_type__app_label="accounts"
    )
    group = Group.objects.create(name="Review permission group")
    group.permissions.add(grant)
    custom.groups.add(group)
    tech.user_permissions.add(grant)
    TOTP.activate(custom, generate_totp_secret())
    RecoveryCodes.activate(supervisor)

    active = [admin, supervisor, tech, custom, unassigned, portal]
    cases = [
        ({"staff": True}, {admin, supervisor, tech, custom, unassigned}),
        ({"roles": ["supervisor"]}, {admin, supervisor}),
        ({"roles": ["technical"]}, {admin, tech}),
        ({"roles": ["admin"]}, {admin}),
        ({"patient_information": True}, {custom, tech}),
        ({"admin_console": True}, {tech}),
        ({"portal": True}, {portal}),
        (
            {"roles": ["supervisor"], "patient_information": True, "admin_console": True},
            {admin, supervisor, tech, custom},
        ),
    ]
    for revision, (parts, covered) in enumerate(cases):
        current, changed = policy.save_rule(
            actor=admin, new=parts, expected_revision=revision, reason="Check the account counts."
        )
        assert changed
        for account in active:
            browser = Browser()
            browser.force_login(account)
            session = AccountSession.objects.get(pk=browser.session["_account_session"])
            assert session.second_factor_required is (account in covered), (parts, account.username)
        assert not policy.requires(inactive, current)
        assert not policy.requires(jobs, current)
        page = client.get("/lab/security/")
        assert page.status_code == 200
        expected = (
            f"Active accounts required to have one: {len(covered)}; "
            f"without one yet: {len(covered - {custom})}."
        )
        assert expected in page.content.decode(), parts


@pytest.mark.django_db(transaction=True)
def test_setup_reason_labels_describe_one_saved_membership(policy, make_user):
    from concurrent.futures import ThreadPoolExecutor

    from django.contrib.auth.models import Group, Permission
    from django.db import OperationalError, connection, transaction

    account = make_user("review_reason_label", "supervisor")
    supervisor = Group.objects.get(name="supervisor")
    technical = Group.objects.get_or_create(name="technical")[0]
    grant = Permission.objects.get(
        codename="view_patient_information", content_type__app_label="accounts"
    )
    supervisor.permissions.add(grant)
    rule = policy.rule()
    rule.roles = ["supervisor", "technical"]
    rule.patient_information = True

    def save_new_membership():
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '1s'")
                account.groups.set([technical])
        except OperationalError as exc:
            if getattr(exc.__cause__, "sqlstate", None) != "55P03":
                raise
        finally:
            connection.close()

    changed = False

    def edit_after_account_read(execute, sql, params, many, context):
        nonlocal changed
        result = execute(sql, params, many, context)
        if not changed and sql.lstrip().upper().startswith("SELECT") and '"accounts_user"' in sql:
            changed = True
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(save_new_membership).result(timeout=5)
        return result

    with connection.execute_wrapper(edit_after_account_read):
        labels = policy.reasons(account, current=rule)
    assert changed, "The test did not observe an account read."
    assert labels in (
        ["the Supervisor role", "staff who can view patient information"],
        ["the Technical staff role"],
    )


def test_stale_rule_form_keeps_all_choices_and_only_audits_first_admin(policy, make_user):

    first = make_user("review_rule_first", "admin")
    second = make_user("review_rule_second", "admin")
    first_browser = Browser()
    second_browser = Browser()
    first_browser.force_login(first)
    second_browser.force_login(second)
    path = "/lab/security/second-factor/"
    first_revision = first_browser.get(path).context["form"]["expected_revision"].value()
    second_revision = second_browser.get(path).context["form"]["expected_revision"].value()
    assert first_revision == second_revision == 0
    saved = first_browser.post(
        path, {"expected_revision": first_revision, "portal": "on", "reason": "Portal rollout."}
    )
    assert saved.status_code == 302
    stale = second_browser.post(
        path,
        {
            "expected_revision": second_revision,
            "staff": "on",
            "admin_console": "on",
            "patient_information": "on",
            "roles": ["technical", "supervisor"],
            "reason": "Staff rollout.",
        },
    )
    assert stale.status_code == 200
    assert "changed after you opened this form" in stale.content.decode()
    form = stale.context["form"]
    assert form["reason"].value() == "Staff rollout."
    assert set(form["roles"].value()) == {"technical", "supervisor"}
    assert all(form[field].value() for field in ("admin_console", "patient_information", "staff"))
    assert not form["portal"].value()
    assert str(form["expected_revision"].value()) == "0"
    assert policy.rule().revision == 1
    event = AuditEvent.objects.get(action=policy.ACTION)
    assert event.actor == first
    assert event.reason == "Portal rollout."
    assert event.after == {
        "roles": [],
        "admin_console": False,
        "staff": False,
        "patient_information": False,
        "portal": True,
    }

    second.groups.remove(*second.groups.filter(name="admin"))
    denied = second_browser.post(
        path, {"expected_revision": 1, "staff": "on", "reason": "Staff rollout."}
    )
    assert denied.status_code == 403
    assert policy.rule().revision == 1
    assert AuditEvent.objects.filter(action=policy.ACTION).count() == 1


@pytest.mark.django_db(transaction=True)
def test_upgrade_preserves_sessions_and_accepts_a_waiting_previous_release_insert(make_user):
    """Apply both migrations with a live old-format row and an overlapping old-format writer."""
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue
    from time import monotonic, sleep

    from django.db import connection, connections, transaction
    from django.db.migrations.executor import MigrationExecutor

    factor = pytest.importorskip("core.second_factor")
    person = make_user("review-migration")
    previous = [("accounts", "0006_kept_draft_revision"), ("core", "0005_sessionlimit")]
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    old_sessions = executor.loader.project_state(previous).apps.get_model(
        "accounts", "AccountSession"
    )
    writer_pid = Queue()

    def sign_in_using_old_columns():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '15s'")
                cursor.execute("SELECT pg_backend_pid()")
                writer_pid.put(cursor.fetchone()[0])
            return old_sessions.objects.create(user_id=person.pk).pk
        finally:
            connections.close_all()

    try:
        executor.migrate(previous)
        before = old_sessions.objects.create(user_id=person.pk, user_agent="Example Browser")
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                MigrationExecutor(connection).migrate(latest)
                writer = pool.submit(sign_in_using_old_columns)
                pid = writer_pid.get(timeout=8)
                deadline = monotonic() + 8
                with connection.cursor() as cursor:
                    while monotonic() < deadline:
                        cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
                        if cursor.fetchone()[0]:
                            break
                        sleep(0.02)
                    else:
                        pytest.fail("The previous-release insert never waited for the migration.")
            after = writer.result(timeout=8)
    finally:
        MigrationExecutor(connection).migrate(latest)

    rows = AccountSession.objects.filter(user=person).in_bulk()
    assert set(rows) == {before.pk, after}
    assert rows[before.pk].user_agent == "Example Browser"
    assert rows[before.pk].started_at == before.started_at
    assert all(not row.second_factor_required and row.ended_at is None for row in rows.values())
    assert not factor.SecondFactorRule.objects.exists()
    assert factor.counts() == (0, 0)


def test_demo_reseed_keeps_the_rule_and_reset_restores_unrestricted_shared_signin(
    client, monkeypatch
):
    """A public demo retains its policy on reseed and returns to the seed policy on reset."""
    from django.core.management import call_command
    from django.urls import reverse

    from accounts.models import User

    factor = pytest.importorskip("core.second_factor")
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-review-shared")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "synthetic-review-owner")
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    call_command("seed_demo", announce_accounts=True, verbosity=0)
    assert not factor.SecondFactorRule.objects.exists()
    admin = User.objects.get(username="demo_admin")
    shared = User.objects.get(username="demo_technical")
    assert shared.credentials_locked
    factor.save_rule(
        actor=admin, new={"roles": ["technical"]}, expected_revision=0, reason="Demo review."
    )

    call_command("seed_demo", announce_accounts=True, verbosity=0)
    assert factor.rule().roles == ["technical"] and factor.rule().revision == 1
    assert AuditEvent.objects.filter(action=factor.ACTION).count() == 1
    client.force_login(shared)
    setup = client.get(reverse("account-second-factor"))
    assert setup.status_code == 200
    assert b"This shared demonstration account cannot add a second factor" in setup.content
    assert reverse("account_logout").encode() in setup.content
    assert reverse("mfa_activate_totp").encode() not in setup.content

    call_command("reset_demo", interactive=False, announce_accounts=True, verbosity=0)
    assert not factor.SecondFactorRule.objects.exists()
    assert not AuditEvent.objects.filter(action=factor.ACTION).exists()
    assert not AccountSession.objects.exists()
    restored = User.objects.get(username="demo_technical")
    assert restored.credentials_locked
    login = client.post(
        reverse("account_login"),
        {"login": restored.username, "password": "synthetic-review-shared"},
    )
    assert login.status_code == 302
    assert not AccountSession.objects.get(user=restored).second_factor_required
    assert client.get(reverse("dashboard")).status_code == 200


def test_standard_password_post_to_login_keeps_the_open_identity(client, make_user, policy):
    current = make_user("review_open_account", "technical")
    other = make_user("review_other_account", "technical")
    policy.SecondFactorRule.objects.create(pk=1, staff=True)
    password_sign_in(client, current)
    first_id = client.session["_account_session"]
    assert password_sign_in(client, other).status_code == 302
    assert client.session["_auth_user_id"] == str(current.pk)
    assert client.session["_account_session"] == first_id
    assert not AccountSession.objects.filter(user=other).exists()
    # Signal-driven re-login of the same identity creates a new session row.
    client.force_login(current)
    assert client.session["_account_session"] != first_id
    assert AccountSession.objects.get(pk=first_id).ending == AccountSession.Ending.SIGNED_IN_AGAIN
    assert AccountSession.objects.get(pk=client.session["_account_session"]).second_factor_required


def test_an_expired_waiting_session_returns_to_sign_in(client, make_user, policy):
    from datetime import timedelta

    from django.utils import timezone

    user = make_user("review_expired", "technical")
    policy.SecondFactorRule.objects.create(pk=1, staff=True)
    password_sign_in(client, user)
    past = timezone.now() - timedelta(days=5)
    AccountSession.objects.filter(user=user).update(started_at=past, last_active_at=past)
    answer = client.get("/requests/")
    assert urlsplit(answer.url).path == reverse("account_login")
    assert "no-store" in answer["Cache-Control"]
    assert "_auth_user_id" not in client.session
    assert AccountSession.objects.get(user=user).ended_at is not None


def test_the_waiting_shell_stays_without_work_navigation_on_a_missing_page(
    client, make_user, policy
):
    policy.SecondFactorRule.objects.create(pk=1, staff=True)
    password_sign_in(client, make_user("review_missing_page", "technical"))
    response = client.get("/review-missing-bookmark/", follow=True)
    assert response.status_code in (200, 404) and "no-store" in response["Cache-Control"]
    shows_work_navigation = b'aria-label="Primary"' in response.content
    assert not shows_work_navigation


def test_waiting_file_and_background_requests_are_refused_without_remembering_a_file(
    client, make_user, policy
):
    from accounts.enrolment import NEXT_KEY

    policy.SecondFactorRule.objects.create(pk=1, staff=True)
    password_sign_in(client, make_user("review_resources", "technical"))
    for route, headers, status in (
        (reverse("attachments:download", args=[1]), {"Sec-Fetch-Dest": "empty"}, 403),
        ("/requests/", {"HX-Request": "true"}, 204),
        (reverse("desk:request-create"), {"X-VR-Form": "1"}, 204),
    ):
        response = client.get(route, headers=headers)
        assert response.status_code == status and "no-store" in response["Cache-Control"]
        destination = response.json()["location"] if status == 403 else response["HX-Redirect"]
        assert destination == SETUP
    assert NEXT_KEY not in client.session
    response = client.get(reverse("attachments:download", args=[1]))
    assert response.status_code == 302 and response.url == SETUP


@pytest.mark.django_db(transaction=True)
def test_both_enrolled_browsers_observe_the_owners_changes_at_the_next_request(
    make_user, policy, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    from django.db import connections

    from accounts import session_views

    def during_status_request(action):
        # The second browser is asking for its own session times. Complete an ordinary
        # account action in the first browser before the status view finishes drawing.
        original = session_views._answer

        def run_action():
            try:
                action()
            finally:
                connections.close_all()

        def answer(request):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(run_action).result(timeout=10)
            return original(request)

        with monkeypatch.context() as patch:
            patch.setattr(session_views, "_answer", answer)
            status = second.get(reverse("account-session"))
        assert status.status_code == 200 and status.json()["signed_in"]
        assert "no-store" in status["Cache-Control"]

    user = make_user("review_two_enrolled", "technical")
    policy.SecondFactorRule.objects.create(pk=1, staff=True)
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    recovery = RecoveryCodes.activate(user).get_unused_codes()[0]
    counter = next(yield_hotp_counters_from_time())
    first, second = Browser(), Browser()
    for browser, code in (
        (first, format_hotp_value(hotp_value(secret, counter))),
        (second, recovery),
    ):
        challenge = password_sign_in(browser, user)
        assert challenge.url == reverse("mfa_authenticate")
        assert browser.post(challenge.url, {"code": code}).status_code == 302
        assert browser.get(SETUP).url == reverse("dashboard")
    assert first.session["_account_session"] != second.session["_account_session"]
    assert first.post(reverse("account_reauthenticate"), {"password": PASSWORD}).status_code == 302

    def remove_app():
        assert first.post(reverse("mfa_deactivate_totp")).status_code == 302

    during_status_request(remove_app)
    assert not Authenticator.objects.filter(user=user, type="totp").exists()
    for browser in (first, second):
        assert browser.get("/requests/").url == SETUP
    during_status_request(lambda: activate_app(first))
    assert Authenticator.objects.filter(user=user, type="totp").exists()
    for browser in (first, second):
        assert browser.get(SETUP).url == "/requests/"


@pytest.mark.django_db(transaction=True)
@pytest.mark.xfail(
    strict=True,
    reason="Django writes the browser's session as a whole: two tabs choosing Continue at the same "
    "moment can both go to the remembered page (docs/architecture.md, Required second factor).",
)
def test_two_tabs_consume_one_completed_enrolments_continue_only_once(
    client, make_user, policy, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.contrib.sessions.backends.db import SessionStore
    from django.db import connections

    policy.SecondFactorRule.objects.create(pk=1, staff=True)
    password_sign_in(client, make_user("review_continue_tabs", "technical"))
    destination = "/requests/?view=all"
    assert client.get(destination).url == SETUP
    activate_app(client)
    # Two tabs of this one fully enrolled browser have the same session cookie.
    session_id = client.cookies[settings.SESSION_COOKIE_NAME].value
    both_open = Barrier(2, timeout=8)
    original_load = SessionStore.load

    def load_together(store):
        state = original_load(store)
        if store.session_key == session_id:
            both_open.wait()
        return state

    def open_continue():
        try:
            tab = Browser()
            tab.cookies[settings.SESSION_COOKIE_NAME] = session_id
            response = tab.get(SETUP)
            assert response.status_code == 302
            return response.url
        finally:
            connections.close_all()

    monkeypatch.setattr(SessionStore, "load", load_together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tabs = [pool.submit(open_continue) for _ in range(2)]
        locations = [tab.result(timeout=15) for tab in tabs]
    assert sorted(locations) == sorted([destination, reverse("dashboard")])


def test_architecture_distinguishes_an_absent_address_from_an_unverified_row(make_user, settings):
    from allauth.mfa.internal.flows.add import validate_can_add_authenticator

    account = make_user("review_absent_address")
    account.email = ""
    account.save(update_fields=["email"])
    assert not EmailAddress.objects.filter(user=account).exists()
    settings.MFA_ALLOW_UNVERIFIED_EMAIL = False  # Previous-release behavior.
    validate_can_add_authenticator(account)
    architecture = (Path(settings.BASE_DIR) / "docs/architecture.md").read_text()
    assert "would leave a required account without email unable to enrol" not in architecture


def test_patient_grant_help_includes_custom_permission_groups(policy, make_user):
    from django.contrib.auth.models import Group, Permission

    from core.lab_profile_forms import SecondFactorRuleForm

    person = make_user("review_custom_group_help")
    group = Group.objects.create(name="Review specimen records")
    group.permissions.add(
        Permission.objects.get(
            codename="view_patient_information", content_type__app_label="accounts"
        )
    )
    person.groups.add(group)
    rule = policy.rule()
    rule.patient_information = True
    assert policy.requires(person, rule)

    drawer = json.loads(help_text("core/second-factor-form.json"))
    field = SecondFactorRuleForm().fields["patient_information"]
    description = next(
        item["description"]
        for section in drawer["sections"]
        for item in section.get("items", [])
        if item["label"] == field.label
    )
    narrow = [
        text
        for text in (field.help_text, description)
        if "through a role group" in text and "custom group" not in text
    ]
    assert not narrow, narrow


@pytest.mark.parametrize("grant_source", ["direct", "group"])
def test_removing_and_restoring_a_grant_updates_a_waiting_setup_page(
    client, make_user, policy, grant_source
):
    from django.contrib.auth.models import Group, Permission

    user = make_user("review_changing_grant", "technical")
    grant = Permission.objects.get(
        codename=policy.PATIENT_CAPABILITY, content_type__app_label="accounts"
    )
    if grant_source == "group":
        group = Group.objects.create(name="Review grant group")
        user.groups.add(group)
        grants = group.permissions
    else:
        grants = user.user_permissions
    grants.add(grant)
    policy.SecondFactorRule.objects.create(pk=1, patient_information=True)
    password_sign_in(client, user)
    assert client.get(SETUP).context["reasons"] == ["staff who can view patient information"]
    grants.remove(grant)
    assert client.get(SETUP).url == reverse("dashboard")
    grants.add(grant)
    assert client.get(SETUP).context["reasons"] == ["staff who can view patient information"]


def test_a_replacement_signin_has_its_own_continue_destination(client, make_user, settings, policy):
    # allauth normally redirects an already signed-in user. With its sign-in form enabled
    # for signed-in users as well, two real password submissions exercise start_session.
    settings.ACCOUNT_AUTHENTICATED_LOGIN_REDIRECTS = False
    user = make_user("review_signed_in_twice", "technical")
    policy.SecondFactorRule.objects.create(pk=1, staff=True)
    password_sign_in(client, user, "/whats-new/")
    assert client.get("/whats-new/").url == SETUP
    old_row = client.session["_account_session"]
    assert password_sign_in(client, user, "/requests/").url == "/requests/"
    assert client.session["_account_session"] != old_row
    assert AccountSession.objects.get(pk=old_row).ending == AccountSession.Ending.SIGNED_IN_AGAIN
    assert client.get("/requests/").url == SETUP
    activate_app(client)
    assert client.get(SETUP).url == "/requests/"


def test_reinstating_the_rule_matches_the_open_session_help(client, make_user, policy):
    user = make_user("review_reinstated", "technical")
    current = policy.SecondFactorRule.objects.create(pk=1, roles=["technical"])
    password_sign_in(client, user)
    assert client.get("/requests/").url == SETUP
    current.roles = []
    current.save()
    assert client.get("/requests/").status_code == 200
    current.roles = ["technical"]
    current.save()
    interrupted = client.get("/requests/").get("Location") == SETUP
    claims_no_interruption = "Sessions already open are not interrupted" in help_text(
        "core/second-factor-form.json"
    )
    assert not (interrupted and claims_no_interruption)


def test_a_rule_write_rolls_back_when_its_attributed_event_cannot_be_recorded(
    make_user, policy, monkeypatch
):
    admin = make_user("review_atomic_save", "admin")
    first, _ = policy.save_rule(
        actor=admin, new={"portal": True}, expected_revision=0, reason="Initial rule."
    )

    def unavailable(**kwargs):
        raise RuntimeError("Synthetic event store failure")

    monkeypatch.setattr(policy, "record_event", unavailable)
    with pytest.raises(RuntimeError, match="Synthetic event store failure"):
        policy.save_rule(
            actor=admin,
            new={"staff": True},
            expected_revision=first.revision,
            reason="Revised rule.",
        )
    current = policy.rule()
    assert current.revision == first.revision and current.portal and not current.staff
    assert AuditEvent.objects.filter(action=policy.ACTION).count() == 1


def test_the_activated_jobs_identity_is_counted_but_remains_ineligible_for_signin(
    client, make_user, policy
):
    """The admin forbids this state, but a legacy or external database update can leave it."""
    from jobs.actor import system_account

    admin = make_user("review_jobs_owner", "admin")
    account = system_account()
    client.force_login(admin)
    current, _ = policy.save_rule(
        actor=admin, new={"staff": True}, expected_revision=0, reason="Review."
    )
    assert policy.counts(current) == (1, 1)
    account.is_active = True
    account.set_password(PASSWORD)
    account.save(update_fields=["is_active", "password"])
    assert password_sign_in(Browser(), account).url == reverse("account_inactive")
    assert not AccountSession.objects.filter(user=account).exists()
    assert policy.counts(current) == (2, 2)


def test_the_app_inventory_distinguishes_a_saved_security_key_from_an_authenticator_app(make_user):
    from core.security_settings import in_force

    user = make_user("review_key_inventory", "technical")
    # Counting reads only the stored type. Model an installation which kept its credential
    # row after returning to the release configuration. No credential is used to sign in.
    Authenticator.objects.create(user=user, type=Authenticator.Type.WEBAUTHN, data={})
    fact = next(item for group in in_force() for item in group.facts if item.key == "second-factor")
    assert "authenticator app" in fact.note
    assert "Active staff accounts with one: 0 of 1." in fact.note


def test_rule_validation_rejects_an_unknown_part_name(policy):
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        policy._clean({"staff_accounts": True})


@pytest.mark.django_db(transaction=True)
def test_a_setup_page_has_reasons_from_the_rule_that_sent_the_person_there(
    make_user, policy, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    from django.db import connections

    from accounts import enrolment

    admin = make_user("review_reason_editor", "admin")
    person = make_user("review_setup_display", "technical")
    editor, reader = Browser(), Browser()
    editor.force_login(admin)
    policy.save_rule(
        actor=admin, new={"roles": ["technical"]}, expected_revision=0, reason="Rollout."
    )
    password_sign_in(reader, person)

    def finish_rule_edit():
        try:
            response = editor.post(EDIT, {"expected_revision": 1, "reason": "End rollout."})
            assert response.url == SECURITY
        finally:
            connections.close_all()

    original = enrolment.pending
    edited = False

    def apply_edit_while_drawing(request):
        nonlocal edited
        needs_setup = original(request)
        if request.path == SETUP and needs_setup and not edited:
            edited = True
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(finish_rule_edit).result(timeout=10)
        return needs_setup

    monkeypatch.setattr(enrolment, "pending", apply_edit_while_drawing)
    page = reader.get(SETUP)
    assert edited
    if page.status_code == 302:
        assert page.url == reverse("dashboard")
    else:
        assert page.status_code == 200
        assert page.context["reasons"] == ["the Technical staff role"]
