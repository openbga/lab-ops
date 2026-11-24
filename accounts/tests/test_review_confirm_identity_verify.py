"""Independent verification of the first-round confirmation fixes, using synthetic accounts.

Adopted from the review (round 1 verification). Changed on adoption: the receipt test uses the
password_checked / remember_password interface the resolution introduced; the query count of a
successful password confirmation is 2 since the digest is no longer read again; three accepted
limits are strict xfails (accounts.confirmation module docstring)."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from html import unescape
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.core.context import request_context
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from django import forms
from django.contrib.admin.models import LogEntry
from django.contrib.auth.hashers import PBKDF2PasswordHasher, identify_hasher
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection, connections
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.views.debug import SafeExceptionReporterFilter

from accounts import compromised, confirmation
from accounts.models import AccountSession, CompromisedPassword, User
from core.models import AuditEvent

from .conftest import PASSWORD
from .test_review_confirm_identity import (
    factor_app,
    factor_browser,
    factor_check,
    factor_prompt,
    factor_proofs,
    review_form_case,
    review_snapshot,
)

pytestmark = [pytest.mark.django_db, pytest.mark.confirmation]

PASSWORD_RECEIPT = "_confirmation_password"
NEW_PASSWORD = "synthetic amber orchard violet 529"


@pytest.fixture
def owner(make_user):
    return make_user("verification-owner", "admin", is_staff=True, is_superuser=True)


def password_page(browser, password=PASSWORD):
    response = browser.post(reverse("account_reauthenticate"), {"password": password})
    assert response.status_code == 302
    return response


@pytest.mark.parametrize("surface", ["own-page", "combined-form"])
def test_hash_maintenance_uses_the_upgraded_digest(owner, surface):
    hasher = PBKDF2PasswordHasher()
    old = hasher.encode(PASSWORD, hasher.salt(), iterations=10)
    User.objects.filter(pk=owner.pk).update(password=old)
    owner.refresh_from_db()
    browser = factor_browser(owner)
    if surface == "own-page":
        password_page(browser)
    else:
        factor_check(browser, confirm_password=PASSWORD)
    owner.refresh_from_db()
    assert owner.password != old and not identify_hasher(owner.password).must_update(owner.password)
    assert browser.session[PASSWORD_RECEIPT] == confirmation._password_proof(owner.password)
    assert factor_proofs(browser) == ["password"]
    # This release ends sessions whose Django authentication hash changed during maintenance.
    assert AccountSession.objects.get(user=owner).ending == AccountSession.Ending.NEW_PASSWORD
    assert browser.get(reverse("admin:auth_group_add")).status_code == 302


@pytest.mark.parametrize("first", ["own-page", "combined-form"])
def test_legacy_session_acquires_proof_only_after_proving_password_again(owner, first):
    browser = factor_browser(owner)
    password_page(browser)
    legacy = browser.session
    del legacy[PASSWORD_RECEIPT]
    legacy.save()
    assert factor_prompt(browser) == {"confirm_password"}
    if first == "own-page":
        password_page(browser)
        factor_check(browser)  # accepts the proof on the other surface
    else:
        factor_check(browser, confirm_password=PASSWORD)
        password_page(browser)
    assert factor_prompt(browser) == set()
    assert factor_proofs(browser).count("password") >= 2


def test_disabling_and_reenabling_password_ends_prior_browser(owner, make_user):
    manager = make_user("verification-manager", is_staff=True, is_superuser=True)
    manager_browser = factor_browser(manager)
    browser = factor_browser(owner)
    password_page(browser)
    password_page(manager_browser)
    url = reverse("admin:auth_user_password_change", args=[owner.pk])
    for data in [
        {"usable_password": "false", "unset-password": "1"},
        {
            "usable_password": "true",
            "set-password": "1",
            "password1": NEW_PASSWORD,
            "password2": NEW_PASSWORD,
        },
    ]:
        response = manager_browser.post(url, data)
        assert response.status_code == 302 and response.url == reverse(
            "admin:accounts_user_change", args=[owner.pk]
        )
    assert AuditEvent.objects.filter(action="account.password_changed", actor=manager).count() == 2
    owner.refresh_from_db()
    assert owner.check_password(NEW_PASSWORD)
    assert browser.get(reverse("admin:auth_group_add")).status_code == 302
    assert factor_proofs(browser) == []
    assert factor_prompt(factor_browser(owner)) == {"confirm_password"}


def test_failed_combined_confirmation_adds_no_successful_password_proof(owner):
    factor_app(owner)
    browser = factor_browser(owner)
    factor_check(browser, confirm_password=PASSWORD, confirm_code="invalid synthetic code")
    assert factor_proofs(browser) == []
    assert PASSWORD_RECEIPT not in browser.session


@pytest.mark.django_db(transaction=True)
def test_password_receipt_identifies_the_verified_password_version(make_user):
    """Check provenance in memory for an ordinary account, outside a browser session."""
    user = make_user("verification-reader")
    checked = User.objects.get(pk=user.pk)
    assert checked.check_password(PASSWORD)
    original = SimpleNamespace(session={})
    confirmation.password_checked(original, checked.password)
    confirmation.remember_password(None, request=original, method="password", reauthenticated=True)

    def reset_password():
        try:
            writer = User.objects.get(pk=user.pk)
            writer.set_password(NEW_PASSWORD)
            writer.save(update_fields=["password"])
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(reset_password).result(timeout=10)
    receipt = SimpleNamespace(session={})
    confirmation.password_checked(receipt, checked.password)
    confirmation.remember_password(None, request=receipt, method="password", reauthenticated=True)
    # Refusing the obsolete receipt or retaining the verified version both preserve provenance.
    assert receipt.session.get(PASSWORD_RECEIPT) in (None, original.session[PASSWORD_RECEIPT])


@pytest.mark.parametrize("used", ["app", "recovery"])
def test_regenerating_recovery_codes_invalidates_only_the_replaced_method(owner, used):
    from .test_review_confirm_identity import factor_code

    secret, app = factor_app(owner)
    previous = RecoveryCodes.activate(owner)
    code = previous.get_unused_codes()[0] if used == "recovery" else factor_code(secret)
    browser = factor_browser(owner)
    factor_check(browser, confirm_password=PASSWORD, confirm_code=code)
    assert factor_prompt(browser) == set()
    response = browser.post(reverse("mfa_generate_recovery_codes"))
    assert response.status_code == 302
    assert Authenticator.objects.filter(pk=app.pk).exists()
    replacement = Authenticator.objects.get(user=owner, type="recovery_codes")
    assert replacement.pk != previous.instance.pk
    assert factor_prompt(browser) == (
        {"confirm_password", "confirm_code"} if used == "recovery" else set()
    )


def test_current_app_is_reflected_after_a_recovery_code_confirmation(owner, make_user):
    _, first_app = factor_app(owner)
    recovery = RecoveryCodes.activate(owner)
    browser = factor_browser(owner)
    factor_check(browser, confirm_password=PASSWORD, confirm_code=recovery.get_unused_codes()[0])
    assert factor_prompt(browser) == set()
    manager = make_user("verification-recovery-owner", is_staff=True, is_superuser=True)
    admin_browser = factor_browser(manager)
    url = reverse("admin:mfa_authenticator_delete", args=[first_app.pk])
    assert admin_browser.post(url, {"post": "yes", "confirm_password": PASSWORD}).status_code == 302
    assert Authenticator.objects.filter(pk=recovery.instance.pk).exists()
    assert factor_prompt(browser) == set()  # password is the only required method now
    _, second_app = factor_app(owner)
    assert first_app.pk != second_app.pk
    assert factor_prompt(browser) == {"confirm_password", "confirm_code"}


@pytest.mark.xfail(
    strict=True,
    reason="Accepted limit: Django writes a session whole, so two tabs re-authenticating at the "
    "same moment can each write over the other; one factor is asked for again.",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_allauth_pages_keep_both_successful_factor_records(make_user, monkeypatch):
    """Two tabs complete the two legitimate allauth pages from the same session snapshot."""
    actor = make_user("concurrent-confirmation")
    factor_app(actor)
    recovery = RecoveryCodes.activate(actor).get_unused_codes()[0]
    first = factor_browser(actor)
    second = Client()
    second.cookies.update(first.cookies)
    key = first.session.session_key
    rendezvous = Barrier(2, timeout=12)
    save = SessionStore.save

    def save_session(session, must_create=False):
        if session.session_key == key and not must_create:
            rendezvous.wait()
        return save(session, must_create=must_create)

    def submit(browser, route, data):
        try:
            return browser.post(reverse(route), data).status_code
        finally:
            connections.close_all()

    with monkeypatch.context() as local, ThreadPoolExecutor(max_workers=2) as pool:
        local.setattr(SessionStore, "save", save_session)
        password = pool.submit(submit, first, "account_reauthenticate", {"password": PASSWORD})
        code = pool.submit(submit, second, "mfa_reauthenticate", {"code": recovery})
        assert password.result(timeout=18) == code.result(timeout=18) == 302
    assert set(factor_proofs(first)) == {"mfa", "password"}


@pytest.mark.parametrize(
    "case",
    [
        "edit-account-save",
        "add-account-popup",
        "set-password",
        "unset-password",
        "mark",
        "remove",
        "add-role",
        "edit-role",
        "limits-full",
        "limits-dialog",
        "rule-full",
        "rule-dialog",
    ],
)
def test_expiry_at_validation_is_explained_on_each_form(owner, make_user, case, monkeypatch):
    browser = factor_browser(owner)
    password_page(browser)
    user = make_user("verification-target", "technical")
    url, headers, data = review_form_case(browser, case, user)
    before = review_snapshot()
    stamp = browser.session[AUTHENTICATION_METHODS_SESSION_KEY][-1]["at"]
    readings = iter((stamp + 1, stamp + confirmation.window()))
    clock = SimpleNamespace(time=lambda: next(readings, stamp + confirmation.window()))
    monkeypatch.setattr(confirmation, "time", clock)
    page = browser.post(url, data, **headers)
    assert page.status_code == 200
    html = unescape(page.content.decode())
    assert confirmation.AGAIN in html and 'name="confirm_password"' in html
    assert review_snapshot() == before
    assert factor_proofs(browser) == ["password"]


def test_form_rebuilds_fields_if_an_app_was_added_since_construction(owner):
    class ReviewForm(confirmation.ConfirmIdentity, forms.Form):
        note = forms.CharField()

    request = RequestFactory().post("/", {"note": "Keep this", "confirm_password": PASSWORD})
    request.user = owner
    request.session = {}
    with request_context(request):
        form = ReviewForm(request.POST)
        assert set(form.fields) == {"note", "confirm_password"}
        factor_app(owner)
        assert not form.is_valid()
        assert confirmation.AGAIN in form.non_field_errors()
        assert {field.name for field in form.confirm_fields()} == {
            "confirm_password",
            "confirm_code",
        }
        assert form["note"].value() == "Keep this"
        assert AUTHENTICATION_METHODS_SESSION_KEY not in request.session


def test_recheck_explains_how_to_recover_when_the_last_method_is_gone(owner):
    class ReviewForm(confirmation.ConfirmIdentity, forms.Form):
        pass

    browser = factor_browser(owner)
    password_page(browser)
    request = RequestFactory().post("/")
    request.user = owner
    request.session = browser.session
    with request_context(request):
        form = ReviewForm(request.POST)
        assert not form.confirming
        owner.set_unusable_password()
        owner.save(update_fields=["password"])
        assert not form.is_valid()
        assert form.confirm_fields() == []
        assert confirmation.NO_METHOD in form.non_field_errors()


@pytest.mark.parametrize(
    "endpoint", ["account-add", "account-password", "role-add", "early", "csrf"]
)
def test_production_reporter_redacts_post_and_traceback_request_data(owner, endpoint, settings):
    settings.DEBUG = False
    browser = Client(enforce_csrf_checks=endpoint == "csrf")
    browser.force_login(owner)
    urls = {
        "account-add": reverse("admin:accounts_user_add"),
        "account-password": reverse("admin:auth_user_password_change", args=[owner.pk]),
        "role-add": reverse("admin:auth_group_add"),
        "early": reverse("admin:auth_group_add"),
        "csrf": reverse("admin:auth_group_add"),
    }
    if endpoint == "early":
        settings.MAX_REQUEST_BODY_BYTES = 10
    data = {"confirm_password": "sample-password-field", "confirm_code": "sample-code-field"}
    response = browser.post(urls[endpoint], data)
    assert response.status_code == {"early": 413, "csrf": 403}.get(endpoint, 200)
    reporter = SafeExceptionReporterFilter()
    request = response.wsgi_request
    # Both are real Django reporter paths. The latter is used for a QueryDict in a traceback.
    for filtered in (
        reporter.get_post_parameters(request),
        reporter.cleanse_special_types(request, request.POST),
    ):
        assert all(filtered.get(name) == reporter.cleansed_substitute for name in data)


@pytest.mark.xfail(
    strict=True,
    reason="Accepted limit: a view that names its own sensitive fields (allauth's sign-in pages, "
    "which post no confirmation) replaces the list; a form that asks marks the fields again.",
)
def test_explicitly_named_view_decorators_keep_global_sensitive_markers(client):
    response = client.get(reverse("account_login"))
    assert response.status_code == 200
    marked = response.wsgi_request.sensitive_post_parameters
    assert marked == "__ALL__" or {"confirm_password", "confirm_code", "password"} <= set(marked)


@pytest.mark.django_db(transaction=True)
def test_withdrawn_mark_is_rendered_from_the_service_reading(make_user, monkeypatch):
    editor = make_user("verification-mark-owner", is_staff=True, is_superuser=True)
    subject = make_user("verification-mark-subject")
    browser = factor_browser(editor)
    url = reverse("admin:accounts_user_compromised", args=[subject.pk])
    form = browser.get(url).context["form"]
    mark = compromised.mark_compromised

    def disable_target_password():
        try:
            stored = User.objects.get(pk=subject.pk)
            stored.set_unusable_password()
            stored.save(update_fields=["password"])
        finally:
            connections.close_all()

    def service(**kwargs):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(disable_target_password).result(timeout=10)
        return mark(**kwargs)

    monkeypatch.setattr(compromised, "mark_compromised", service)
    response = browser.post(
        url,
        {
            "reason": "Synthetic withdrawal",
            "drawn": form["drawn"].value(),
            "confirm_password": PASSWORD,
        },
    )
    assert response.status_code == 200
    assert compromised.REFUSALS["unusable"] in response.content.decode()
    assert not CompromisedPassword.objects.filter(user=subject).exists()
    assert not AuditEvent.objects.filter(action=compromised.ACTION).exists()
    assert not LogEntry.objects.exists()
    assert response.context["refused"] == compromised.REFUSALS["unusable"]
    assert b'id="user_form"' not in response.content


@pytest.mark.parametrize("target", ["unknown", "read-only", "unsupported-audience"])
def test_unavailable_objects_do_not_reach_either_verifier(owner, target, monkeypatch):
    from jobs.actor import system_account

    verifier = Mock(side_effect=AssertionError("Unavailable object checked a credential"))
    monkeypatch.setattr("accounts.adapter.ClosedSignupAdapter.reauthenticate", verifier)
    monkeypatch.setattr("accounts.forms.MFAReauthenticateForm.is_valid", verifier)
    browser = factor_browser(owner)
    if target == "unknown":
        url = reverse("admin:mfa_authenticator_delete", args=[987654321])
        expected = 302
    elif target == "read-only":
        url = reverse("admin:auth_user_password_change", args=[system_account().pk])
        expected = 403
    else:
        url = reverse("session-limits-edit", args=["unrecognised"])
        expected = 404
    response = browser.post(url, {"confirm_password": PASSWORD})
    assert response.status_code == expected
    verifier.assert_not_called()


def test_authenticator_popup_returns_the_deleted_object_to_the_opener(owner, make_user):
    subject = make_user("verify-popup")
    _, app = factor_app(subject)
    browser = factor_browser(owner)
    url = reverse("admin:mfa_authenticator_delete", args=[app.pk])
    refused = browser.post(url, {"_popup": "1", "post": "yes"})
    assert refused.status_code == 200 and b'name="_popup"' in refused.content
    saved = browser.post(url, {"_popup": "1", "post": "yes", "confirm_password": PASSWORD})
    assert saved.status_code == 200
    assert json.loads(saved.context["popup_response_data"]) == {
        "action": "delete",
        "value": str(app.pk),
    }
    assert not Authenticator.objects.filter(pk=app.pk).exists()


def test_identity_query_count_and_locking_on_an_ordinary_settings_form(owner):
    browser = factor_browser(owner)
    url = reverse("session-limits-edit", args=["staff"])

    def observe(operation):
        with CaptureQueriesContext(connection) as captured:
            response = operation()
        assert response.status_code == 200
        reads = [
            query["sql"]
            for query in captured
            if "ARRAY_AGG" in query["sql"] and "mfa_authenticator" in query["sql"]
        ]
        assert all("FOR UPDATE" not in sql and "FOR SHARE" not in sql for sql in reads)
        return len(reads)

    assert observe(lambda: browser.get(url)) == 1
    # Business fields are deliberately empty, so only confirmation succeeds.
    assert observe(lambda: browser.post(url, {"confirm_password": PASSWORD})) == 2
    assert observe(lambda: browser.post(url, {})) == 2


HELD = pytest.mark.xfail(
    strict=True,
    reason="Accepted limit: the confirmation is read when the save is checked and not held "
    "through the save; a factor changed by another request between that check and the commit "
    "does not stop the save (accounts.confirmation module docstring).",
)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "case",
    [
        pytest.param("limits-full", marks=HELD),
        pytest.param("rule-full", marks=HELD),
        pytest.param("mark", marks=HELD),
        "add-role",  # Django's admin runs a change form in one transaction
    ],
)
def test_final_identity_read_and_audit_share_the_writes_transaction(make_user, case):
    """Observe PostgreSQL transactions on an ordinary, authorized save."""
    editor = make_user("transaction-reader", "admin", is_staff=True, is_superuser=True)
    subject = make_user("transaction-target", "technical")
    browser = factor_browser(editor)
    password_page(browser)
    url, headers, data = review_form_case(browser, case, subject)
    identity_reads, audits = [], []

    # Attribute statements to the transaction they actually run in. In autocommit an
    # individual SELECT has its own transaction and cannot share the later audit INSERT.
    def observe(execute, sql, params, many, context):
        if '"mfa_authenticator"' in sql and sql.startswith("SELECT"):
            # The predecessor used an EXISTS query in factors(). Exclude MFA reads by
            # unrelated middleware so the observation applies to both implementations.
            frame = sys._getframe()
            while frame and frame.f_globals.get("__name__") != confirmation.__name__:
                frame = frame.f_back
            if frame is None:
                return execute(sql, params, many, context)
            samples = identity_reads
        elif sql.startswith(f'INSERT INTO "{AuditEvent._meta.db_table}"'):
            samples = audits
        else:
            return execute(sql, params, many, context)
        if connection.in_atomic_block or not connection.get_autocommit():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_current_xact_id()::text")
                samples.append(cursor.fetchone()[0])
        else:
            samples.append(None)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(observe):
        result = browser.post(url, data, **headers)
    assert result.status_code == 302
    assert identity_reads and len(audits) == 1 and audits[0] is not None
    assert identity_reads[-1] == audits[0], (identity_reads, audits)


def test_combined_form_compares_the_identity_of_the_verified_app(make_user, monkeypatch):
    """An isolated, unsavable form checks the verifier's result against its final inventory."""
    user = make_user("verification-method-reader")
    user.set_unusable_password()
    user.save(update_fields=["password"])
    _, app = factor_app(user)
    state = confirmation.read(user)
    # Two valid inventories have the same field names but different enrollment IDs.
    next_state = replace(state, authenticators=frozenset({app.pk + 1}))
    inventories = iter((state, next_state))
    monkeypatch.setattr(confirmation, "read", lambda _: next(inventories, next_state))
    stub = Mock()
    stub.authenticator = app
    stub.is_valid.return_value = True
    monkeypatch.setattr("accounts.forms.MFAReauthenticateForm", lambda **kwargs: stub)

    class ReviewForm(confirmation.ConfirmIdentity, forms.Form):
        pass

    request = RequestFactory().post("/", {"confirm_code": "synthetic-verifier-result"})
    request.user, request.session = user, {}
    with request_context(request):
        form = ReviewForm(request.POST)
        assert not form.is_valid()
        assert confirmation.AGAIN in form.non_field_errors()
        stub.save.assert_not_called()


def test_delete_collector_refusal_precedes_the_verifier(owner, make_user, monkeypatch):
    """Exercise Django's collector contract with a synthetic missing related permission."""
    from accounts.admin import AuthenticatorAdmin

    subject = make_user("collector-subject")
    _, app = factor_app(subject)
    browser = factor_browser(owner)
    url = reverse("admin:mfa_authenticator_delete", args=[app.pk])
    check = Mock(return_value=False)
    monkeypatch.setattr("accounts.adapter.ClosedSignupAdapter.reauthenticate", check)
    monkeypatch.setattr(
        AuthenticatorAdmin, "get_deleted_objects", lambda *args: ([], {}, {"related item"}, [])
    )
    response = browser.post(url, {"post": "yes", "confirm_password": "synthetic field"})
    assert response.status_code in {200, 403}
    assert Authenticator.objects.filter(pk=app.pk).exists()
    assert (
        not LogEntry.objects.exists()
        and not AuditEvent.objects.filter(action="account.authenticator_removed").exists()
    )
    check.assert_not_called()


def test_security_fact_summary_allows_for_an_app_only_account(owner):
    from core.security_settings import in_force

    owner.set_unusable_password()
    owner.save(update_fields=["password"])
    factor_app(owner)
    recovery = RecoveryCodes.activate(owner)
    browser = factor_browser(owner)
    assert factor_prompt(browser) == {"confirm_code"}
    factor_check(browser, confirm_code=recovery.get_unused_codes()[0])
    assert factor_proofs(browser) == ["mfa"]
    fact = next(
        fact for group in in_force() for fact in group.facts if fact.key == "confirm-before-changes"
    )
    assert fact.source == "release"
    assert fact.value != "Needs the password, and the second factor, again"


def test_limits_help_accounts_for_proofs_given_at_different_times(owner, settings):
    from pathlib import Path

    # Check the actual remaining window against the unqualified claim in the operator help.
    # The page's section about confirming describes both these forms and the allauth pages.
    factor_app(owner)
    browser = factor_browser(owner)
    password_page(browser)
    code = RecoveryCodes.activate(owner).get_unused_codes()[0]
    assert browser.post(reverse("mfa_reauthenticate"), {"code": code}).status_code == 302
    session = browser.session
    records = session[AUTHENTICATION_METHODS_SESSION_KEY]
    now = records[-1]["at"]
    records[0]["at"] = now - confirmation.window()
    session[AUTHENTICATION_METHODS_SESSION_KEY] = records
    session.save()
    assert factor_prompt(browser) == {"confirm_password", "confirm_code"}
    help_path = Path(settings.BASE_DIR) / "docs/pages/core/session-limits-form.json"
    sections = json.loads(help_path.read_text())["sections"]
    section = next(section for section in sections if section["title"] == "Confirm it is you")
    claim = (
        "Once you have confirmed, neither these forms nor the other pages that ask "
        "(the second sign-in factor rule, and account administration for a superuser) "
        "ask again in this browser for 5 minutes."
    )
    explanation = " ".join(section["paragraphs"])
    assert claim not in explanation or any(
        phrase in explanation.lower()
        for phrase in ("from when", "from the time", "earliest", "earlier proof", "older proof")
    )
