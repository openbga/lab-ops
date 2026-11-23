"""Independent regression checks for the password-mark workflow, using synthetic accounts."""

import logging
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from queue import Queue
from threading import Event
from time import monotonic, sleep
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import pytest
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.contrib.admin.models import LogEntry
from django.contrib.auth.hashers import PBKDF2PasswordHasher, identify_hasher
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.test import Client
from django.urls import reverse

from accounts import sessions
from accounts.models import AccountSession, User
from accounts.roles import ROLES
from clients.models import Client as Organization
from core.models import AuditEvent, SecondFactorRule
from core.security_settings import CHANGED, in_force

from .conftest import PASSWORD
from .test_login import mailed_code, request_code


def with_mark(person, data):
    """The new-password form's data with the mark the page is drawn for (the account's latest),
    as a browser posts it (adopted: the form carries its mark since round 1)."""
    from accounts.models import CompromisedPassword

    latest = CompromisedPassword.objects.filter(user_id=person.pk).order_by("-pk").first()
    return {**data, "mark": str(latest.pk) if latest else ""}


pytestmark = pytest.mark.django_db
REASON = "Synthetic independent review reference"
REPLACEMENT = "seven winds beyond the lighthouse"


@pytest.fixture(autouse=True)
def fresh_sign_in_limits(db):
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def feature():
    return pytest.importorskip("accounts.compromised")


@pytest.fixture
def review_owner(make_user):
    return make_user("review_owner", "admin", is_superuser=True, is_staff=True)


@pytest.fixture
def review_person(make_user):
    return make_user("review_person", "technical")


def mark_page(user):
    return reverse("admin:accounts_user_compromised", args=[user.pk])


def submit_review_mark(owner, target):
    browser = Client()
    browser.force_login(owner)
    url = mark_page(target)
    page = browser.get(url)
    assert page.status_code == 200
    drawn = page.context["form"].initial["drawn"]
    result = browser.post(url, {"drawn": drawn, "reason": REASON})
    assert result.status_code == 302
    return browser, result


def enter_with_email(browser, user):
    assert request_code(browser, user.email).url == reverse("account_confirm_login_code")
    answer = browser.post(reverse("account_confirm_login_code"), {"code": mailed_code()})
    assert answer.status_code == 302
    return AccountSession.objects.get(pk=browser.session[sessions.SESSION_KEY])


@pytest.mark.parametrize(
    "audience", ["anonymous", "portal", *ROLES, "staff", "nonstaff-root", "inactive-root"]
)
def test_mark_page_and_submit_require_active_staff_superuser(
    feature, review_person, make_user, audience
):
    browser = Client()
    if audience != "anonymous":
        roles = [audience] if audience in ROLES else []
        organization = (
            Organization.objects.create(name="Synthetic clinic") if audience == "portal" else None
        )
        actor = make_user(
            "review_actor",
            *roles,
            is_staff=audience in (*ROLES, "staff", "inactive-root"),
            is_superuser=audience in ("nonstaff-root", "inactive-root"),
            is_active=audience != "inactive-root",
            client=organization,
        )
        browser.force_login(actor)
    url = mark_page(review_person)
    data = {"drawn": feature.drawn_password(review_person), "reason": REASON}
    for result in (browser.get(url), browser.post(url, data)):
        assert result.status_code in (302, 403)
        if result.status_code == 302:
            assert result.url.startswith(reverse("admin:login"))
    assert not feature.is_marked(review_person.pk)
    assert not AuditEvent.objects.filter(action=feature.ACTION).exists()


def test_job_account_has_no_mark_post(feature, review_owner):
    from jobs.actor import system_account

    target = system_account()
    browser = Client()
    browser.force_login(review_owner)
    data = {"drawn": feature.drawn_password(target), "reason": REASON}
    assert browser.post(mark_page(target), data).status_code == 403
    assert not feature.is_marked(target.pk)


@pytest.mark.parametrize("audience", ["staff", "portal", "superuser"])
def test_mark_ends_a_session_opened_after_the_admin_drew_the_page(
    feature, review_owner, make_user, audience
):
    org = Organization.objects.create(name="Review clinic") if audience == "portal" else None
    target = make_user(
        "signed_in_target",
        client=org,
        is_staff=audience == "superuser",
        is_superuser=audience == "superuser",
    )
    admin, browser = Client(), Client()
    admin.force_login(review_owner)
    form = admin.get(mark_page(target)).context["form"]
    browser.force_login(target)
    row_pk = browser.session[sessions.SESSION_KEY]
    result = admin.post(mark_page(target), {"drawn": form.initial["drawn"], "reason": REASON})
    assert result.status_code == 302
    row = AccountSession.objects.get(pk=row_pk)
    assert row.ending == AccountSession.Ending.PASSWORD_COMPROMISED and row.ended_at is not None
    assert AuditEvent.objects.get(action=feature.ACTION).after["sessions"] == [row_pk]
    assert browser.get(reverse("account_reauthenticate")).url.startswith(reverse("account_login"))


@pytest.mark.parametrize("entry", ["username", "email", "admin"])
def test_marked_password_signin_ends_the_new_row(feature, review_owner, make_user, entry):
    target = make_user("review_console", "admin", is_staff=True, is_superuser=True)
    submit_review_mark(review_owner, target)
    browser = Client()
    url = reverse("account_login")
    if entry == "admin":
        admin_login = browser.get(reverse("admin:index"))
        url = browser.get(admin_login.url).url
        assert urlsplit(url).path == reverse("account_login") and "next=" in url
    result = browser.post(
        url, {"login": target.email if entry == "email" else target.username, "password": PASSWORD}
    )
    assert result.status_code == 302
    row = AccountSession.objects.get(pk=browser.session[sessions.SESSION_KEY])
    assert row.ending == AccountSession.Ending.PASSWORD_COMPROMISED
    assert row.ended_at is not None
    assert browser.get(reverse("account-session")).json()["signed_in"] is False
    assert "_auth_user_id" not in browser.session


def test_a_signed_out_browsers_prior_email_proof_is_not_a_new_proof(
    feature, client, review_owner, review_person, settings
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    first = enter_with_email(client, review_person)
    assert first.ended_at is None
    client.post(reverse("account_logout"))
    submit_review_mark(review_owner, review_person)
    client.post(reverse("account_login"), {"login": review_person.username, "password": PASSWORD})
    next_row = AccountSession.objects.get(pk=client.session[sessions.SESSION_KEY])
    assert next_row.pk != first.pk
    assert next_row.ending == AccountSession.Ending.PASSWORD_COMPROMISED


def test_recovery_code_and_password_reach_only_the_replacement_workflow(
    feature, review_owner, review_person, client
):
    TOTP.activate(review_person, generate_totp_secret())
    recovery = RecoveryCodes.activate(review_person).get_unused_codes()[0]
    submit_review_mark(review_owner, review_person)
    challenge = client.post(
        reverse("account_login"), {"login": review_person.email, "password": PASSWORD}
    )
    assert challenge.url == reverse("mfa_authenticate")
    assert client.post(challenge.url, {"code": recovery}).status_code == 302
    page = reverse("account-new-password")
    assert client.get(page).context["password_change_pending"]
    for url in (
        reverse("attachments:download", args=[1]),
        reverse("desk:request-find"),
        reverse("admin:index"),
        reverse("whats-new"),
        reverse("account-sessions"),
        reverse("account_reauthenticate"),
        reverse("account_email"),
        reverse("mfa_reauthenticate"),
    ):
        result = client.get(url)
        assert result.status_code == 302 and result.url == page, url
        assert not result.content
        assert "no-store" in result["Cache-Control"]
    assert client.post(reverse("account-session-continue")).json()["signed_in"]
    assert client.get(reverse("account_logout")).status_code == 200
    assert client.post(reverse("account_logout")).status_code == 302
    assert feature.is_marked(review_person.pk)


def test_normal_mark_and_replacement_keep_credentials_out_of_records_and_notices(
    feature,
    review_owner,
    review_person,
    client,
    settings,
    caplog,
    django_capture_on_commit_callbacks,
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    caplog.set_level(logging.INFO)
    old_hash = review_person.password
    with django_capture_on_commit_callbacks(execute=True):
        administrator, result = submit_review_mark(review_owner, review_person)
    assert not mail.outbox
    admin_page = administrator.get(result.url)
    enter_with_email(client, review_person)
    page = reverse("account-new-password")
    rendered = client.get(page)
    assert REASON not in rendered.content.decode()
    # The requested sign-in code is the only email in this journey.
    mail_count = len(mail.outbox)
    assert mail_count == 1
    with django_capture_on_commit_callbacks(execute=True):
        saved = client.post(
            page,
            with_mark(review_person, {"new_password1": REPLACEMENT, "new_password2": REPLACEMENT}),
        )
    assert saved.status_code == 302
    review_person.refresh_from_db()
    assert review_person.check_password(REPLACEMENT)
    assert len(mail.outbox) == mail_count
    events = list(
        AuditEvent.objects.filter(action__in=(feature.ACTION, feature.CHANGED_ACTION)).values(
            "action", "before", "after", "reason"
        )
    )
    assert len(events) == 2
    messages = [str(m) for m in client.get(saved.url).context["messages"]]
    logs = [
        r.getMessage() for r in caplog.records if r.name in ("lab_ops.request", "lab_ops.signin")
    ]
    assert any("method=POST" in line for line in logs)
    assert any("signin.success" in line for line in logs)
    history = list(
        LogEntry.objects.filter(object_id=str(review_person.pk)).values_list(
            "change_message", flat=True
        )
    )
    assert history
    surfaces = [
        str(events),
        *messages,
        *logs,
        *history,
        rendered.content.decode(),
        admin_page.content.decode(),
        mail.outbox[0].body,
    ]
    for value in (PASSWORD, old_hash, REPLACEMENT, review_person.password):
        assert all(value not in surface for surface in surfaces)
    assert all(REASON not in surface for surface in (*logs, *messages, *history))


def test_an_ended_at_creation_session_is_not_logged_as_a_completed_signin(
    feature, review_owner, review_person, client, caplog, django_capture_on_commit_callbacks
):
    submit_review_mark(review_owner, review_person)
    caplog.clear()
    caplog.set_level(logging.INFO, logger="lab_ops.signin")
    with django_capture_on_commit_callbacks(execute=True):
        client.post(
            reverse("account_login"), {"login": review_person.username, "password": PASSWORD}
        )
    row = AccountSession.objects.get(user=review_person)
    assert row.ended_at is not None
    lines = [r.getMessage() for r in caplog.records if r.name == "lab_ops.signin"]
    assert f"signout.password_compromised user={review_person.username} sessions=1" in lines
    assert f"signin.success user={review_person.username}" not in lines


def test_admin_confirmation_offers_a_recovery_method_the_deployment_can_use(
    feature, review_owner, review_person, settings
):
    settings.EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = False
    admin, result = submit_review_mark(review_owner, review_person)
    notices = [str(message) for message in admin.get(result.url).context["messages"]]
    assert any("account's password page" in text for text in notices)
    assert not any("use Forgot your password?" in text for text in notices)


def test_admin_help_accounts_for_email_code_recovery_without_an_authenticator(
    feature, review_owner, review_person, client, settings
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    admin, _ = submit_review_mark(review_owner, review_person)
    page_help = admin.get(mark_page(review_person)).context["page_documentation"]
    enter_with_email(client, review_person)
    assert client.get(reverse("account-new-password")).context["form"] is not None
    notes = " ".join(page_help["good_to_know"])
    assert "needs a reset or a password you set" not in notes


@pytest.mark.parametrize(
    "setting,keys",
    [
        ("MIDDLEWARE", ("compromised-password",)),
        ("AUTH_PASSWORD_VALIDATORS", ("passwords", "compromised-password")),
    ],
)
def test_security_facts_report_the_specific_configuration_change(feature, settings, setting, keys):
    if setting == "MIDDLEWARE":
        settings.MIDDLEWARE = [
            row
            for row in settings.MIDDLEWARE
            if row != "accounts.compromised.PasswordChangeMiddleware"
        ]
    else:
        settings.AUTH_PASSWORD_VALIDATORS = [
            row
            for row in settings.AUTH_PASSWORD_VALIDATORS
            if row["NAME"] != "accounts.passwords.MarkedPasswordValidator"
        ]
    facts = {fact.key: fact for group in in_force() for fact in group.facts}
    for key in keys:
        assert facts[key].value == CHANGED
        assert setting in facts[key].note


def test_security_settings_explain_enrolment_before_password_replacement(
    feature, review_owner, review_person, client, settings
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    submit_review_mark(review_owner, review_person)
    SecondFactorRule.objects.update_or_create(pk=1, defaults={"roles": ["technical"]})
    enter_with_email(client, review_person)
    landing = client.get(reverse("account-new-password"), follow=True)
    assert landing.resolver_match.view_name == "account-second-factor"
    fact = next(
        fact for group in in_force() for fact in group.facts if fact.key == "compromised-password"
    )
    assert re.search(r"(set up|enrol|add).{0,80}(authenticator|factor)", fact.note, re.I), fact.note


def test_unmarked_normal_login_still_logs_success_and_leaves_the_session_open(
    review_person, client, caplog
):
    caplog.set_level(logging.INFO, logger="lab_ops.signin")
    response = client.post(
        reverse("account_login"), {"login": review_person.email, "password": PASSWORD}
    )
    assert response.status_code == 302
    assert AccountSession.objects.get(pk=client.session[sessions.SESSION_KEY]).ended_at is None
    assert any(
        r.getMessage() == f"signin.success user={review_person.username}"
        for r in caplog.records
        if r.name == "lab_ops.signin"
    )


RECOVERY_PASSPHRASE = "Gentle acorns on the hickory trail"
LATER_PASSPHRASE = "Prisms light the empty pottery studio"


def mark_in_service(marker, person):
    from accounts.compromised import drawn_password, mark_compromised

    return mark_compromised(
        actor=marker, account_id=person.pk, reason="Synthetic review", drawn=drawn_password(person)
    ).mark


@pytest.fixture
def recovery_accounts(feature):
    marker = User.objects.create_superuser("paths-marker", "paths-marker@example.invalid", PASSWORD)
    person = User.objects.create_superuser(
        "paths-birch-glen", "paths-birch-glen@example.invalid", PASSWORD
    )
    return marker, person


@pytest.mark.parametrize("kind", ["allauth-change", "allauth-set", "admin-own"])
def test_other_password_forms_lift_an_open_mark_at_the_save_hook(recovery_accounts, kind):
    # These regular, signed-in pages redirect a held browser to Choose a new password.
    # Exercise the forms' normal validation and save hooks independently of that routing.
    from accounts.admin import OwnPasswordForm
    from accounts.forms import ChangePasswordForm, SetPasswordForm

    marker, person = recovery_accounts
    mark = mark_in_service(marker, person)
    both = {"password1": RECOVERY_PASSPHRASE, "password2": RECOVERY_PASSPHRASE}
    if kind == "allauth-set":
        person.set_unusable_password()
        person.save(update_fields=["password"])
        mark.refresh_from_db()
        assert mark.lifted_at is None
        form = SetPasswordForm(person, data=both)
    elif kind == "allauth-change":
        form = ChangePasswordForm(person, data={**both, "oldpassword": PASSWORD})
    else:
        form = OwnPasswordForm(
            person,
            data={
                "old_password": PASSWORD,
                "new_password1": RECOVERY_PASSPHRASE,
                "new_password2": RECOVERY_PASSPHRASE,
            },
        )
    assert form.is_valid(), form.errors
    form.save()
    mark.refresh_from_db()
    person.refresh_from_db()
    assert person.check_password(RECOVERY_PASSPHRASE)
    assert mark.lifted_at is not None


@pytest.mark.parametrize(
    "kind", ["allauth-change", "allauth-set", "admin-own", "admin-target", "reset"]
)
def test_regular_password_pages_refuse_a_lifted_marks_password(client, recovery_accounts, kind):
    from accounts.tests.test_review_password_rules import reset_form

    marker, person = recovery_accounts
    mark = mark_in_service(marker, person)
    person.set_password(RECOVERY_PASSPHRASE)
    person.save(update_fields=["password"])
    mark.refresh_from_db()
    assert mark.lifted_at is not None
    if kind == "allauth-set":
        person.set_unusable_password()
        person.save(update_fields=["password"])
    original = person.password
    if kind == "reset":
        url = reset_form(client, person)
    else:
        client.force_login(marker if kind == "admin-target" else person)
        route = {
            "allauth-change": "account_change_password",
            "allauth-set": "account_set_password",
            "admin-own": "admin:password_change",
            "admin-target": "admin:auth_user_password_change",
        }[kind]
        url = reverse(route, args=[person.pk] if kind == "admin-target" else [])
    data = {"password1": PASSWORD, "password2": PASSWORD}
    if kind == "admin-own":
        data = {
            "old_password": RECOVERY_PASSPHRASE,
            "new_password1": PASSWORD,
            "new_password2": PASSWORD,
        }
    elif kind == "allauth-change":
        data["oldpassword"] = RECOVERY_PASSPHRASE
    elif kind == "admin-target":
        data["usable_password"] = "true"
    page = client.post(url, data)
    assert page.status_code == 200
    assert "This password was marked as compromised" in str(page.context["form"].errors)
    person.refresh_from_db()
    assert person.password == original


def test_changepassword_lifts_the_mark_and_refuses_the_old_value_on_the_next_run(
    recovery_accounts, monkeypatch
):
    from accounts.password_command import Command

    marker, person = recovery_accounts
    mark = mark_in_service(marker, person)
    answers = iter([RECOVERY_PASSPHRASE] * 2 + [PASSWORD] * 2 + [LATER_PASSPHRASE] * 2)
    monkeypatch.setattr(Command, "_get_pass", lambda *args: next(answers))
    errors = StringIO()
    call_command("changepassword", person.username, stdout=StringIO(), stderr=errors)
    mark.refresh_from_db()
    assert mark.lifted_at is not None
    person.refresh_from_db()
    assert person.check_password(RECOVERY_PASSPHRASE) and not errors.getvalue()
    call_command("changepassword", person.username, stdout=StringIO(), stderr=errors)
    assert "This password was marked as compromised" in errors.getvalue()
    assert PASSWORD not in errors.getvalue()
    person.refresh_from_db()
    assert person.check_password(LATER_PASSPHRASE)


def test_setup_for_an_existing_marked_account_keeps_its_mark(recovery_accounts, monkeypatch):
    marker, person = recovery_accounts
    mark = mark_in_service(marker, person)
    original = person.password
    monkeypatch.setenv("SETUP_ADMIN_PASSWORD", RECOVERY_PASSPHRASE)
    call_command(
        "setup_lab",
        lab_name="Synthetic Test Lab",
        admin_username=person.username,
        stdout=StringIO(),
    )
    mark.refresh_from_db()
    person.refresh_from_db()
    assert person.password == original and mark.lifted_at is None


def test_real_hash_upgrade_keeps_mark_and_invalidates_earlier_admin_preview(
    recovery_accounts, client
):
    from accounts.compromised import CHANGED_SINCE_DRAWN

    marker, person = recovery_accounts
    old = PBKDF2PasswordHasher().encode(PASSWORD, "synthetic-salt", iterations=10)
    User.objects.filter(pk=person.pk).update(password=old)
    person.refresh_from_db()
    admin_browser = Client()
    admin_browser.force_login(marker)
    url = reverse("admin:accounts_user_compromised", args=[person.pk])
    preview = admin_browser.get(url)
    drawn = preview.context["form"].initial["drawn"]
    client.post(reverse("account_login"), {"login": person.username, "password": PASSWORD})
    person.refresh_from_db()
    assert person.password != old and not identify_hasher(person.password).must_update(
        person.password
    )
    stale = admin_browser.post(url, {"drawn": drawn, "reason": "Synthetic review"})
    assert CHANGED_SINCE_DRAWN in stale.context["form"].non_field_errors()

    # Start a second, independent upgrade with an open mark on the synthetic legacy hash.
    User.objects.filter(pk=person.pk).update(password=old)
    person.refresh_from_db()
    mark = mark_in_service(marker, person)
    Client().post(reverse("account_login"), {"login": person.username, "password": PASSWORD})
    person.refresh_from_db()
    mark.refresh_from_db()
    assert person.password != old and not identify_hasher(person.password).must_update(
        person.password
    )
    assert mark.password == old and mark.lifted_at is None


def test_profile_edits_and_deactivation_leave_the_mark_open(recovery_accounts):
    marker, person = recovery_accounts
    mark = mark_in_service(marker, person)
    person.first_name = "Corrected"
    person.save(update_fields=["first_name"])
    mark.refresh_from_db()
    assert mark.lifted_at is None
    person.is_active = False
    person.save(update_fields=["is_active"])
    mark.refresh_from_db()
    assert mark.lifted_at is None


def test_chosen_password_saved_after_an_unrelated_field_lifts_the_mark(recovery_accounts):
    marker, person = recovery_accounts
    mark = mark_in_service(marker, person)
    person.set_password(RECOVERY_PASSPHRASE)
    person.first_name = "Corrected"
    person.save(update_fields=["first_name"])
    mark.refresh_from_db()
    assert mark.lifted_at is None
    assert User.objects.get(pk=person.pk).check_password(PASSWORD)
    person.save(update_fields=["password"])
    assert User.objects.get(pk=person.pk).check_password(RECOVERY_PASSPHRASE)
    mark.refresh_from_db()
    assert mark.lifted_at is not None, "The new password persisted, but its mark remains open"


def verified_recovery_browser(person):
    from accounts.tests.test_login import mailed_code, request_code

    browser = Client()
    request_code(browser, person.email)
    assert (
        browser.post(reverse("account_confirm_login_code"), {"code": mailed_code()}).status_code
        == 302
    )
    assert browser.get(reverse("account-new-password")).status_code == 200
    return browser


def test_only_sessions_open_at_marking_are_described_as_ended(recovery_accounts, settings):
    from accounts.models import AccountSession
    from accounts.sessions import SESSION_KEY

    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    marker, person = recovery_accounts
    mark_in_service(marker, person)
    one = verified_recovery_browser(person)
    two = verified_recovery_browser(person)
    assert one.session[SESSION_KEY] != two.session[SESSION_KEY]
    assert AccountSession.objects.filter(user=person, ended_at__isnull=True).count() == 2
    page = one.get(reverse("account-new-password"))
    assert "Your other sessions have ended." not in page.content.decode()


def test_replacing_password_in_one_held_browser_ends_the_other(recovery_accounts, settings):
    from accounts.models import AccountSession
    from accounts.sessions import SESSION_KEY
    from core.models import AuditEvent

    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    marker, person = recovery_accounts
    mark = mark_in_service(marker, person)
    one = verified_recovery_browser(person)
    two = verified_recovery_browser(person)
    ids = [client.session[SESSION_KEY] for client in (one, two)]
    assert ids[0] != ids[1]
    assert AccountSession.objects.filter(pk__in=ids, ended_at__isnull=True).count() == 2
    saved = one.post(
        reverse("account-new-password"),
        with_mark(
            person,
            {
                "new_password1": RECOVERY_PASSPHRASE,
                "new_password2": RECOVERY_PASSPHRASE,
            },
        ),
    )
    assert saved.status_code == 302
    assert one.get(reverse("admin:index")).status_code == 200
    assert two.get(reverse("account-new-password")).url.startswith(reverse("account_login"))
    assert AccountSession.objects.get(pk=ids[0]).ended_at is None
    assert AccountSession.objects.get(pk=ids[1]).ending == AccountSession.Ending.NEW_PASSWORD
    assert AuditEvent.objects.get(action="account.password_changed").after == {
        "compromised_mark": mark.pk
    }


def test_forced_change_checks_the_rule_again_when_the_form_is_submitted(
    recovery_accounts, settings
):
    from core.models import SecondFactorRule

    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    marker, person = recovery_accounts
    mark = mark_in_service(marker, person)
    browser = verified_recovery_browser(person)
    url = reverse("account-new-password")
    SecondFactorRule.objects.update_or_create(pk=1, defaults={"admin_console": True})
    page = browser.get(url)
    assert "at least 8 characters" in str(page.context["form"]["new_password1"].help_text)
    SecondFactorRule.objects.update_or_create(pk=1, defaults={"admin_console": False})
    for password, code in [
        ("jade fern", "password_too_short"),
        ("1q2w3e4r5t6y7u8i", "password_too_common"),
        ("Open BGA Lab Ops 4827", "password_context_name"),
        ("paths-birch-glenhill", "password_too_similar"),
        (PASSWORD, "password_marked_compromised"),
    ]:
        page = browser.post(
            url, with_mark(person, {"new_password1": password, "new_password2": password})
        )
        assert page.status_code == 200 and page.context["form"].has_error("new_password1", code)
        assert "at least 15 characters" in str(page.context["form"]["new_password1"].help_text)
    page = browser.post(
        url,
        with_mark(
            person, {"new_password1": RECOVERY_PASSPHRASE, "new_password2": LATER_PASSPHRASE}
        ),
    )
    assert page.context["form"].has_error("new_password2", "password_mismatch")
    mark.refresh_from_db()
    person.refresh_from_db()
    assert mark.lifted_at is None and person.check_password(PASSWORD)


def _overlap_credential_operations(first, second):
    """Run both operations on separate connections and verify that the second really waited."""
    written = Event()
    release = Event()
    backend_ids = Queue()

    def run(operation, is_first):
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '12s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    backend_ids.put(cursor.fetchone()[0])
                result = operation()
                if is_first:
                    written.set()
                    assert release.wait(10), "coordinator did not release the first transaction"
                return result
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as workers:
        leader = workers.submit(run, first, True)
        try:
            first_pid = backend_ids.get(timeout=3)
            if not written.wait(5):
                leader.result(timeout=0)
                pytest.fail("first operation did not finish its write")
            follower = workers.submit(run, second, False)
            second_pid = backend_ids.get(timeout=3)
            deadline = monotonic() + 5
            with connection.cursor() as cursor:
                while monotonic() < deadline:
                    cursor.execute("SELECT pg_blocking_pids(%s)", [second_pid])
                    if first_pid in cursor.fetchone()[0]:
                        break
                    if follower.done():
                        follower.result()
                        pytest.fail("second operation completed before the first committed")
                    sleep(0.02)
                else:
                    pytest.fail("PostgreSQL did not report the expected wait")
        finally:
            release.set()
        return leader.result(timeout=8), follower.result(timeout=8)


def _draw_mark_form(owner, account):
    browser = Client()
    browser.force_login(owner)
    url = reverse("admin:accounts_user_compromised", args=[account.pk])
    page = browser.get(url)
    assert page.status_code == 200
    return browser, url, page.context["form"]["drawn"].value()


@pytest.mark.django_db(transaction=True)
def test_two_administrators_marking_together_record_one_mark_and_one_ending():
    compromised = pytest.importorskip("accounts.compromised")
    from accounts.tests.test_compromised_password import row_of, signed_in

    alice = User.objects.create_superuser("review-alice", "alice@example.invalid", "seed")
    bob = User.objects.create_superuser("review-bob", "bob@example.invalid", "seed")
    person = User.objects.create_user("review-target", "target@example.invalid", "seed")
    row = row_of(signed_in(person))
    one, url, first_drawn = _draw_mark_form(alice, person)
    two, _, second_drawn = _draw_mark_form(bob, person)
    accepted, refused = _overlap_credential_operations(
        lambda: one.post(url, {"drawn": first_drawn, "reason": "First review"}),
        lambda: two.post(url, {"drawn": second_drawn, "reason": "Second review"}),
    )
    assert accepted.status_code == 302
    assert refused.status_code == 200
    assert compromised.ALREADY_MARKED in refused.context["form"].non_field_errors()
    mark = compromised.CompromisedPassword.objects.get(user=person)
    assert mark.marked_by == alice and mark.reason == "First review" and mark.lifted_at is None
    audit = AuditEvent.objects.get(action=compromised.ACTION, object_id=person.pk)
    assert audit.actor == alice and audit.after["sessions"] == [str(row.pk)]
    row.refresh_from_db()
    assert row.ending == AccountSession.Ending.PASSWORD_COMPROMISED


@pytest.mark.django_db(transaction=True)
def test_mark_waits_for_people_saving_their_own_new_password(settings):
    compromised = pytest.importorskip("accounts.compromised")
    from accounts.tests.test_compromised_password import (
        NEW_PAGE,
        NEW_PASSWORD,
        PASSWORD,
        mark,
        row_of,
    )

    first = User.objects.create_superuser("review-first", "first@example.invalid", "seed")
    other = User.objects.create_superuser("review-next", "next@example.invalid", "seed")
    person = User.objects.create_user("review-writer", "writer@example.invalid", PASSWORD)
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    marker, url, drawn = _draw_mark_form(other, person)
    assert mark(first, person)[1].status_code == 302
    writer = Client()
    enter_with_email(writer, person)
    assert writer.get(NEW_PAGE).status_code == 200
    row = row_of(writer)
    saved, refused = _overlap_credential_operations(
        lambda: writer.post(
            NEW_PAGE,
            with_mark(person, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}),
        ),
        lambda: marker.post(url, {"reason": "Independent review", "drawn": drawn}),
    )
    assert saved.status_code == 302 and refused.status_code == 200
    assert compromised.CHANGED_SINCE_DRAWN in refused.context["form"].non_field_errors()
    assert compromised.CompromisedPassword.objects.get(user=person).lifted_at is not None
    assert AuditEvent.objects.filter(action=compromised.ACTION, object_id=person.pk).count() == 1
    assert (
        AuditEvent.objects.filter(action=compromised.CHANGED_ACTION, object_id=person.pk).count()
        == 1
    )
    person.refresh_from_db()
    row.refresh_from_db()
    assert person.check_password(NEW_PASSWORD) and row.ended_at is None


@pytest.mark.django_db
@pytest.mark.parametrize("change", ["self", "admin", "reset-link", "sign-in-upgrade"])
def test_mark_form_detects_the_stored_password_changing_on_each_path(change):
    compromised = pytest.importorskip("accounts.compromised")
    from accounts.tests.test_compromised_password import NEW_PASSWORD, PASSWORD

    owner = User.objects.create_superuser("review-owner", "owner@example.invalid", "seed")
    person = User.objects.create_user("review-fingerprint", "fingerprint@example.invalid", PASSWORD)
    if change == "sign-in-upgrade":
        hasher = PBKDF2PasswordHasher()
        person.password = hasher.encode(PASSWORD, hasher.salt(), iterations=hasher.iterations // 2)
        person.save(update_fields=["password"])
    before = person.password
    marker, url, drawn = _draw_mark_form(owner, person)
    browser = Client()
    values = {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD}
    if change == "self":
        browser.force_login(person)
        result = browser.post(
            reverse("account_change_password"), {**values, "oldpassword": PASSWORD}
        )
    elif change == "admin":
        other = User.objects.create_superuser("review-editor", "editor@example.invalid", "seed")
        browser.force_login(other)
        result = browser.post(
            reverse("admin:auth_user_password_change", args=[person.pk]),
            {**values, "usable_password": "true"},
        )
    elif change == "reset-link":
        assert (
            browser.post(reverse("account_reset_password"), {"email": person.email}).status_code
            == 302
        )
        link = re.search(r"https?://[^\s<>]+/password/reset/key/[^\s<>]+/", mail.outbox[-1].body)
        assert link
        page = browser.get(urlsplit(link[0]).path, follow=True)
        assert page.status_code == 200
        result = browser.post(page.redirect_chain[-1][0], values)
    else:
        result = browser.post(
            reverse("account_login"), {"login": person.username, "password": PASSWORD}
        )
    assert result.status_code == 302
    person.refresh_from_db()
    assert person.password != before
    assert person.check_password(PASSWORD if change == "sign-in-upgrade" else NEW_PASSWORD)
    response = marker.post(url, {"drawn": drawn, "reason": "Review of earlier password"})
    assert response.status_code == 200
    assert compromised.CHANGED_SINCE_DRAWN in response.context["form"].non_field_errors()
    assert not compromised.CompromisedPassword.objects.filter(user=person).exists()
    assert not AuditEvent.objects.filter(action=compromised.ACTION, object_id=person.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_password_step_before_the_mark_commits_and_session_start_afterwards():
    compromised = pytest.importorskip("accounts.compromised")
    from allauth.account.signals import authentication_step_completed

    from accounts.tests.test_compromised_password import PASSWORD, row_of

    owner = User.objects.create_superuser("review-overlap-owner", "owner@example.invalid", "seed")
    person = User.objects.create_user("review-overlap-person", "person@example.invalid", PASSWORD)
    marker, url, drawn = _draw_mark_form(owner, person)
    signer = Client()
    password_steps = []

    def observe_step(sender, request, user, method, **kwargs):
        if user == person and method == "password":
            password_steps.append(compromised.is_marked(person.pk))

    authentication_step_completed.connect(observe_step)
    try:
        marked, signed = _overlap_credential_operations(
            lambda: marker.post(url, {"drawn": drawn, "reason": "Independent review"}),
            lambda: signer.post(
                reverse("account_login"), {"login": person.username, "password": PASSWORD}
            ),
        )
    finally:
        authentication_step_completed.disconnect(observe_step)
    assert marked.status_code == signed.status_code == 302
    assert password_steps == [False]  # The mark had not committed when authentication succeeded.
    row = row_of(signer)
    assert row.ended_at is not None and row.ending == AccountSession.Ending.PASSWORD_COMPROMISED
    assert signer.get(reverse("dashboard")).url.startswith(reverse("account_login"))
    person.refresh_from_db()
    assert person.last_login is None


@pytest.mark.xfail(
    strict=True,
    reason="Accepted and documented (docs/operations.md, Marking a password as compromised during "
    "an upgrade): a process of an earlier release knows nothing of marks, so a password set there "
    "does not lift one. Runs only beside the review's previous-release checkout.",
)
@pytest.mark.django_db(transaction=True)
def test_replacement_by_previous_release_lifts_the_mark_on_an_upgraded_database(
    feature, review_owner, review_person
):
    """A previous-release administrator legitimately resets a password during rolling deployment."""
    checkout = Path("/tmp/labops-cpw-base")
    if not (checkout / ".venv/bin/python").exists():
        pytest.skip("requires the previous release checkout specified in the review brief")
    name = connection.settings_dict["NAME"]
    if not name.startswith("test_labops_cpw_review"):
        pytest.skip("mixed-release check uses the review's disposable PostgreSQL database")
    url = urlsplit(os.environ["DATABASE_URL"])
    env = dict(
        os.environ,
        DATABASE_URL=urlunsplit(url._replace(path="/" + name)),
        PYTHONDONTWRITEBYTECODE="1",
        DJANGO_SETTINGS_MODULE="config.settings",
    )
    env.pop("TEST_DATABASE_NAME", None)
    mark = mark_in_service(review_owner, review_person)
    code = """
import sys
import django
django.setup()
from accounts.models import User
from django.test import Client
from django.urls import reverse
owner, target, replacement = sys.argv[1:]
browser = Client()
browser.force_login(User.objects.get(pk=owner))
page = reverse("admin:auth_user_password_change", args=[target])
assert browser.get(page).status_code == 200
result = browser.post(page, {"usable_password": "true", "password1": replacement,
                             "password2": replacement})
assert result.status_code == 302
"""
    subprocess.run(
        [
            str(checkout / ".venv/bin/python"),
            "-c",
            code,
            str(review_owner.pk),
            str(review_person.pk),
            REPLACEMENT,
        ],
        cwd=checkout,
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
    )
    review_person.refresh_from_db()
    mark.refresh_from_db()
    assert review_person.check_password(REPLACEMENT)
    assert mark.lifted_at is not None, "Previous-release password replacement left the mark open"


@pytest.mark.parametrize("change", ["deactivated", "shared", "signed-out-elsewhere"])
def test_account_changes_after_the_new_password_page_opens_prevent_its_save(
    feature, review_owner, review_person, client, settings, change
):
    from accounts.session_list import end_account_sessions

    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    mark = mark_in_service(review_owner, review_person)
    session = enter_with_email(client, review_person)
    url = reverse("account-new-password")
    assert client.get(url).context["form"] is not None
    if change == "shared":
        review_person.credentials_locked = True
        review_person.save(update_fields=["credentials_locked"])
    elif change == "deactivated":
        review_person.is_active = False
        review_person.save(update_fields=["is_active"])
    else:
        end_account_sessions(
            actor=review_owner, account_id=review_person.pk, reason="Synthetic session cleanup"
        )
    result = client.post(
        url, with_mark(review_person, {"new_password1": REPLACEMENT, "new_password2": REPLACEMENT})
    )
    if change == "shared":
        assert result.status_code == 200 and result.context["form"] is None
    else:
        assert result.status_code == 302 and result.url.startswith(reverse("account_login"))
        session.refresh_from_db()
        assert session.ended_at is not None
    mark.refresh_from_db()
    assert mark.lifted_at is None
    assert User.objects.get(pk=review_person.pk).check_password(PASSWORD)
    assert not AuditEvent.objects.filter(action=feature.CHANGED_ACTION).exists()


def test_the_marked_password_page_describes_its_own_account_without_record_content(
    feature, review_owner, client, make_user, settings
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    org = Organization.objects.create(name="Synthetic preview clinic")
    person = make_user("portal_review", client=org)
    # A real organization record, including a field the account shell does not use.
    org.notes = "Synthetic private directory detail"
    org.save(update_fields=["notes"])
    mark_in_service(review_owner, person)
    enter_with_email(client, person)
    page = client.get(reverse("account-new-password"))
    assert page.status_code == 200 and page.context["password_change_pending"]
    assert org.notes not in page.content.decode()
    assert "no-store" in page["Cache-Control"]
    assert 'class="vr-portal-nav"' not in page.content.decode()


def test_an_admins_own_account_explains_the_self_mark_refusal_before_offering_the_action(
    feature, review_owner, client
):
    client.force_login(review_owner)
    page = client.get(reverse("admin:accounts_user_change", args=[review_owner.pk]))
    assert page.status_code == 200
    assert client.get(mark_page(review_owner)).context["refused"] == feature.REFUSALS["own"]
    offered = f'href="{mark_page(review_owner)}"' in page.content.decode()
    assert not offered, "Change user offers the action that its destination refuses"


def test_facts_flag_a_configuration_that_cannot_read_a_retained_password_hash(
    feature, review_owner, review_person, settings
):
    from django.contrib.auth.hashers import PBKDF2SHA1PasswordHasher

    # An older, supported reader when marked. The current account uses the release's
    # preferred algorithm after it recovers. Only the retained history needs the old reader.
    old_hasher = PBKDF2SHA1PasswordHasher()
    review_person.password = old_hasher.encode(PASSWORD, old_hasher.salt())
    review_person.save(update_fields=["password"])
    mark = mark_in_service(review_owner, review_person)
    review_person.set_password(REPLACEMENT)
    review_person.save(update_fields=["password"])
    mark.refresh_from_db()
    assert (
        mark.lifted_at is not None
        and identify_hasher(mark.password).algorithm == old_hasher.algorithm
    )
    settings.PASSWORD_HASHERS = [
        hasher
        for hasher in settings.PASSWORD_HASHERS
        if not hasher.endswith(".PBKDF2SHA1PasswordHasher")
    ]
    with pytest.raises(ValueError):
        identify_hasher(mark.password)
    assert identify_hasher(review_person.password)
    facts = {f.key: f for group in in_force() for f in group.facts}
    affected = [facts[key] for key in ("compromised-password", "passwords")]
    unreported = [
        fact.key
        for fact in affected
        if fact.value != CHANGED or "PASSWORD_HASHERS" not in fact.note
    ]
    assert not unreported, unreported


def test_enrolment_then_replacement_then_continue_returns_to_the_original_page(
    feature, review_owner, review_person, client, settings
):
    from allauth.mfa.totp.internal import auth as totp

    from .test_second_factor_enrolment import code_for, reauthenticate

    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    mark_in_service(review_owner, review_person)
    SecondFactorRule.objects.update_or_create(pk=1, defaults={"roles": ["technical"]})
    enter_with_email(client, review_person)
    original = reverse("desk:request-find") + "?q=synthetic"
    enrol = reverse("account-second-factor")
    choose = reverse("account-new-password")
    assert client.get(original).url == enrol
    assert client.get(enrol).status_code == 200
    reauthenticate(client)
    add_app = reverse("mfa_activate_totp")
    assert client.get(add_app).status_code == 200
    # Issue and check the authenticator code at the same instant, even on a period boundary.
    instant = totp.time.time()
    with patch.object(totp, "time", wraps=totp.time) as clock:
        clock.time.return_value = instant
        added = client.post(add_app, {"code": code_for(client.session[totp.SECRET_SESSION_KEY])})
    codes = reverse("mfa_view_recovery_codes")
    assert added.url == codes
    assert client.get(codes).url == choose
    assert client.get(choose).context["password_change_pending"]
    saved = client.post(
        choose,
        with_mark(review_person, {"new_password1": REPLACEMENT, "new_password2": REPLACEMENT}),
    )
    next_page = client.get(saved.url, follow=True)
    if next_page.resolver_match.view_name == "mfa_view_recovery_codes":
        assert f'href="{enrol}"' in next_page.content.decode()
        assert client.get(enrol).url == original
    else:
        assert saved.url == original
    assert client.get(original).status_code == 200
    assert client.get(choose).url == reverse("dashboard")


def test_drawn_form_has_distinct_state_for_successive_marks(feature, recovery_accounts, settings):
    """Compare two authorized GETs, without submitting either form under a different mark."""
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    owner, person = recovery_accounts

    def draw():
        mark = mark_in_service(owner, person)
        browser = Client()
        enter_with_email(browser, person)
        before = dict(browser.session)
        page = browser.get(reverse("account-new-password"))
        assert page.status_code == 200 and page.context["marked_at"] == mark.marked_at
        # Django's CSRF token belongs to a sign-in. The inputs of the password form and
        # any session state newly recorded by drawing it can carry a mark-specific reading.
        form = page.context["form"]
        fields = [(f.html_name, f.value()) for f in form.hidden_fields()]
        saved = {k: v for k, v in browser.session.items() if before.get(k) != v}
        action = re.search(
            r'<form\b[^>]*\baction="([^"]*)"[^>]*>(?:(?!</form>).)*?'
            r'name="new_password1"',
            page.content.decode(),
            re.S,
        )
        return mark.pk, (fields, saved, action.group(1) if action else page.request["PATH_INFO"])

    first, first_state = draw()
    person.set_password(REPLACEMENT)
    person.save(update_fields=["password"])
    second, second_state = draw()
    assert first != second
    assert first_state != second_state, "Both forms carry the same state for different marks"


def test_profile_only_save_defers_a_staged_password_until_the_password_write(review_person):
    review_person.set_password(REPLACEMENT)
    review_person.first_name = "Synthetic update"
    review_person.save(update_fields=["first_name"])
    assert User.objects.get(pk=review_person.pk).check_password(PASSWORD)
    review_person.save(update_fields=["password"])
    assert User.objects.get(pk=review_person.pk).check_password(REPLACEMENT)


def test_held_responses_exclude_a_real_ticket_and_its_file(
    feature, review_owner, review_person, client, settings
):
    from attachments.inspection import inspect_bytes
    from attachments.models import Attachment
    from attachments.services import create_request_with_files
    from taxonomy.models import RequestCategory
    from teams.models import Team

    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    org = Organization.objects.create(name="Review-only laboratory client")
    team, _ = Team.objects.get_or_create(
        slug="client-services", defaults={"name": "Client services", "is_default": True}
    )
    group = RequestCategory.objects.create(key="review-group", name="Review group")
    category = RequestCategory.objects.create(
        key="review-case", name="Review case", parent=group, default_team=team
    )
    content = b"Synthetic attachment text for the review"
    record = create_request_with_files(
        actor=review_owner,
        client=org,
        category=category,
        subject="Synthetic review subject",
        first_entry_kind="note",
        first_entry_body="Synthetic first entry",
        channel="email",
        files=[inspect_bytes("synthetic-review.txt", content)],
    )
    file_url = reverse("attachments:download", args=[Attachment.objects.get(request=record).pk])
    client.force_login(review_person)
    assert client.get(file_url).content == content
    mark_in_service(review_owner, review_person)
    client = Client()
    enter_with_email(client, review_person)
    page = reverse("account-new-password")
    cases = [
        (file_url, {}, 302),
        (file_url, {"Sec-Fetch-Dest": "empty"}, 403),
        (reverse("desk:request-create"), {"X-VR-Form": "1"}, 204),
        (reverse("desk:request-find"), {"HX-Request": "true"}, 204),
    ]
    for url, headers, status in cases:
        result = client.get(url, headers=headers)
        assert result.status_code == status
        if status == 403:
            assert result.json() == {"password_change_required": True, "location": page}
        else:
            assert result["Location" if status == 302 else "HX-Redirect"] == page
            assert not result.content
        assert "Content-Disposition" not in result
    form = client.get(page).content
    for private_value in (
        record.number.encode(),
        record.subject.encode(),
        org.name.encode(),
        content,
    ):
        assert private_value not in form


@pytest.mark.parametrize("permission", ["is_staff", "is_active", "is_superuser"])
def test_losing_admin_authority_after_drawing_the_mark_form_prevents_the_write(
    feature, review_owner, review_person, permission
):
    admin, url, drawn = _draw_mark_form(review_owner, review_person)
    setattr(review_owner, permission, False)
    review_owner.save(update_fields=[permission])
    result = admin.post(url, {"drawn": drawn, "reason": REASON})
    assert result.status_code in (302, 403)
    assert not feature.is_marked(review_person.pk)
    assert not AuditEvent.objects.filter(action=feature.ACTION).exists()


def test_wrong_password_for_marked_account_creates_no_session(
    feature, review_owner, review_person, client
):
    mark_in_service(review_owner, review_person)
    assert not AccountSession.objects.filter(user=review_person).exists()
    response = client.post(
        reverse("account_login"),
        {"login": review_person.username, "password": "unrelated synthetic value"},
    )
    assert response.status_code == 200 and response.context["form"].errors
    assert not AccountSession.objects.filter(user=review_person).exists()
    assert "_auth_user_id" not in client.session


def test_drawn_fingerprint_depends_on_both_the_account_and_the_deployment_key(
    feature, review_owner, review_person, settings
):
    # Equal stored encodings isolate what the account id contributes.
    review_owner.password = review_person.password
    first = feature.drawn_password(review_person)
    assert first != feature.drawn_password(review_owner)
    settings.SECRET_KEY = "independent-synthetic-key-for-this-test"
    assert feature.drawn_password(review_person) != first
