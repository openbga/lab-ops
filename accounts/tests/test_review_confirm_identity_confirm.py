"""Regression checks for confirmation after the second set of fixes. All accounts are synthetic.

Adopted from the review (round 1 confirmation). Changed on adoption: three accepted limits are
strict xfails, with the reasons in the accounts.confirmation module docstring."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from allauth.account.internal.flows.login import record_authentication
from allauth.core.context import request_context
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from django import forms
from django.contrib.auth.hashers import PBKDF2PasswordHasher, make_password
from django.core.exceptions import PermissionDenied
from django.db import connection, connections
from django.test import RequestFactory
from django.urls import reverse
from django.views.debug import SafeExceptionReporterFilter

from accounts import compromised, confirmation
from accounts.models import AccountSession, SignInEvent, User
from accounts.session_list import SESSION_ENDINGS
from core.models import AuditEvent

from .conftest import PASSWORD
from .test_login import mailed_code, request_code
from .test_review_confirm_identity import (
    factor_app,
    factor_browser,
    factor_code,
    factor_prompt,
    factor_proofs,
    review_form_case,
)

pytestmark = [pytest.mark.confirmation, pytest.mark.django_db]
RECEIPT = "_confirmation_password"


@pytest.fixture
def owner(make_user):
    return make_user("confirmation-owner", "admin", is_staff=True, is_superuser=True)


@pytest.mark.parametrize("route", ["password", "email", "admin"])
@pytest.mark.parametrize("with_app", [False, True])
def test_sign_in_records_its_factors_and_still_asks_for_confirmation(
    owner, client, settings, route, with_app
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    if with_app:
        secret, _ = factor_app(owner)
    if route == "email":
        assert request_code(client, owner.email).url == reverse("account_confirm_login_code")
        response = client.post(reverse("account_confirm_login_code"), {"code": mailed_code()})
    else:
        if route == "admin":
            response = client.post(
                reverse("admin:login"), {"username": owner.username, "password": PASSWORD}
            )
            assert response.status_code == 302 and response.url.split("?")[0] == reverse(
                "account_login"
            )
            assert "_auth_user_id" not in client.session
        response = client.post(
            reverse("account_login"), {"login": owner.username, "password": PASSWORD}
        )
    if with_app:
        assert response.url == reverse("mfa_authenticate")
        assert RECEIPT not in client.session and "_auth_user_id" not in client.session
        response = client.post(response.url, {"code": factor_code(secret)})
    assert response.status_code == 302 and client.session["_auth_user_id"] == str(owner.pk)
    method = "code" if route == "email" else "password"
    expected = method + ("_totp" if with_app else "")
    assert SignInEvent.objects.get(user=owner, kind="signed_in").factor == expected
    assert AccountSession.objects.get(user=owner).factor == expected
    assert RECEIPT not in client.session and not factor_proofs(client)
    fields = {"confirm_password", "confirm_code"} if with_app else {"confirm_password"}
    assert factor_prompt(client) == fields


def test_other_allauth_steps_leave_existing_password_receipt_alone(owner):
    request = SimpleNamespace(session={RECEIPT: "existing receipt"})
    for method, extra in (("password", {}), ("code", {}), ("mfa", {"reauthenticated": True})):
        record_authentication(request, owner, method, **extra)
        assert request.session[RECEIPT] == "existing receipt"
    # The installed signal clears obsolete provenance for a new password proof with no check.
    record_authentication(request, owner, "password", reauthenticated=True)
    assert RECEIPT not in request.session


class Draft(confirmation.ConfirmIdentity, forms.Form):
    """A data-only form with no save or service attached."""

    note = forms.CharField()


def test_challenged_form_checks_the_password_version_at_its_final_read(make_user, monkeypatch):
    reader = make_user("draft-reader")
    initial = confirmation.read(reader)
    revised = replace(initial, password_hash=make_password("different synthetic password"))
    inventories = iter((initial, revised))
    monkeypatch.setattr(confirmation, "read", lambda user: next(inventories, revised))
    request = RequestFactory().post("/", {"note": "keep this", "confirm_password": PASSWORD})
    request.user, request.session = reader, {}
    with request_context(request):
        draft = Draft(request.POST)
        assert not draft.is_valid()
        assert draft["note"].value() == "keep this" and draft.confirm_fields()
        assert RECEIPT not in request.session


@pytest.mark.parametrize("change", ["replace-app", "remove-last-app"])
def test_challenged_form_rechecks_recovery_proof_against_enrolment(make_user, monkeypatch, change):
    """Use actual inventories and a data-only form with a completed dummy recovery check."""
    reader = make_user("draft-recovery")
    reader.set_unusable_password()
    reader.save(update_fields=["password"])
    _, app = factor_app(reader)
    recovery = RecoveryCodes.activate(reader)

    class CheckedRecovery:
        authenticator = recovery.instance

        def is_valid(self):
            # Deliver the inventory change after this test verifier has selected the retained
            # recovery row. There is no saveable object or privileged user in this fixture.
            app.delete()
            if change == "replace-app":
                factor_app(reader)
            return True

        def save(self):
            pass

    monkeypatch.setattr("accounts.forms.MFAReauthenticateForm", lambda **kwargs: CheckedRecovery())
    request = RequestFactory().post("/", {"note": "keep this", "confirm_code": "fixture"})
    request.user, request.session = reader, {}
    with request_context(request):
        draft = Draft(request.POST)
        assert not draft.is_valid()
        expected = confirmation.NO_METHOD if change == "remove-last-app" else confirmation.AGAIN
        assert expected in draft.non_field_errors()
        assert draft["note"].value() == "keep this"


@pytest.mark.xfail(
    strict=True,
    reason="Accepted limit: Django's filter of a traceback's variables does not read the "
    "everything marker its admin add and password views set; a request refused there is a 403, "
    "for which no error report is made, and this deployment sends none (no mail handler).",
)
@pytest.mark.parametrize("case", ["account-add", "account-password"])
def test_early_admin_refusal_preserves_all_post_field_redaction(make_user, settings, case):
    settings.DEBUG = False
    staff = make_user("review-staff", "technical", is_staff=True)
    browser = factor_browser(staff)
    url = (
        reverse("admin:accounts_user_add")
        if case == "account-add"
        else reverse("admin:auth_user_password_change", args=[staff.pk])
    )
    # A harmless placeholder is sufficient to check the all-fields marker on these views.
    response = browser.post(url, {"confirm_code": "placeholder"})
    assert response.status_code == 403
    request = response.wsgi_request
    reporter = SafeExceptionReporterFilter()
    assert reporter.get_post_parameters(request)["confirm_code"] == reporter.cleansed_substitute
    masked = reporter.cleanse_special_types(request, request.POST)
    assert masked["confirm_code"] == reporter.cleansed_substitute


@pytest.mark.django_db(transaction=True)
def test_refused_mark_redraw_names_the_account_as_reread(owner, make_user, monkeypatch):
    target = make_user("mark-old-name")
    browser = factor_browser(owner)
    url, headers, data = review_form_case(browser, "mark", target)
    original_service = compromised.mark_compromised

    def revise_target():
        try:
            User.objects.filter(pk=target.pk).update(
                username="mark-current-name",
                password=make_password("replacement synthetic password"),
            )
        finally:
            connections.close_all()

    def save(**kwargs):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(revise_target).result(timeout=10)
        return original_service(**kwargs)

    monkeypatch.setattr(compromised, "mark_compromised", save)
    response = browser.post(url, {**data, "confirm_password": PASSWORD}, **headers)
    assert response.status_code == 200
    assert compromised.CHANGED_SINCE_DRAWN in response.context["form"].non_field_errors()
    assert not AuditEvent.objects.filter(action=compromised.ACTION).exists()
    assert "mark-current-name" in response.context["title"]
    assert response.context["original"].username == "mark-current-name"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("case", ["limits-full", "rule-dialog", "mark"])
def test_hash_upgrade_and_session_endings_survive_a_stale_form(make_user, case):
    target = make_user("old-hash-subject")
    editor = make_user("old-hash-owner", "admin", is_staff=True, is_superuser=True)
    hasher = PBKDF2PasswordHasher()
    old = hasher.encode(PASSWORD, hasher.salt(), iterations=10)
    User.objects.filter(pk=editor.pk).update(password=old)
    editor.refresh_from_db()
    factor_browser(editor)
    browser = factor_browser(editor)
    assert AccountSession.objects.filter(user=editor, ended_at__isnull=True).count() == 2
    url, headers, data = review_form_case(browser, case, target)
    if case == "mark":
        data["drawn"] = "obsolete"
    else:
        data["expected_revision"] += 1
    before = AuditEvent.objects.count()
    response = browser.post(url, {**data, "confirm_password": PASSWORD}, **headers)
    assert response.status_code == 200 and response.context["form"].non_field_errors()
    assert AuditEvent.objects.count() == before
    editor.refresh_from_db()
    assert editor.password != old and not hasher.must_update(editor.password)
    assert list(AccountSession.objects.filter(user=editor).values_list("ending", flat=True)) == [
        AccountSession.Ending.NEW_PASSWORD,
        AccountSession.Ending.NEW_PASSWORD,
    ]


@pytest.mark.django_db(transaction=True)
def test_mark_acquires_session_ending_locks_in_key_order_during_hash_maintenance(make_user):
    target = make_user("earlier-subject")
    editor = make_user("later-editor", is_staff=True, is_superuser=True)
    hasher = PBKDF2PasswordHasher()
    User.objects.filter(pk=editor.pk).update(password=hasher.encode(PASSWORD, hasher.salt(), 10))
    editor.refresh_from_db()
    browser = factor_browser(editor)
    url, headers, data = review_form_case(browser, "mark", target)
    data["drawn"] = "outdated"
    locks = defaultdict(list)

    def observe(execute, sql, params, many, context):
        if "pg_advisory_xact_lock(" in sql and params and params[0] == SESSION_ENDINGS:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_current_xact_id()::text")
                tx = cursor.fetchone()[0]
            if params[1] not in locks[tx]:
                locks[tx].append(params[1])
        return execute(sql, params, many, context)

    with connection.execute_wrapper(observe):
        response = browser.post(url, {**data, "confirm_password": PASSWORD}, **headers)
    assert response.status_code == 200 and response.context["form"].non_field_errors()
    assert any(target.pk in keys and editor.pk in keys for keys in locks.values())
    assert all(keys == sorted(keys) for keys in locks.values()), list(locks.values())


@pytest.mark.parametrize("case", ["limits-full", "rule-full", "mark"])
@pytest.mark.parametrize("refusal", ["stale", "permission"])
def test_service_refusal_keeps_recovery_usage_consistent_with_session(
    owner, make_user, monkeypatch, case, refusal
):
    factor_app(owner)
    recovery = RecoveryCodes.activate(owner)
    code = recovery.get_unused_codes()[0]
    browser = factor_browser(owner)
    target = make_user("refusal-subject")
    url, headers, data = review_form_case(browser, case, target)
    if refusal == "stale":
        if case == "mark":
            data["drawn"] = "obsolete"
        else:
            data["expected_revision"] += 1
    else:

        def refuse(**kwargs):
            raise PermissionDenied

        service = {
            "limits-full": "core.session_limits.save_limits",
            "rule-full": "core.second_factor.save_rule",
            "mark": "accounts.compromised.mark_compromised",
        }[case]
        monkeypatch.setattr(service, refuse)
    before = AuditEvent.objects.count()
    response = browser.post(
        url, {**data, "confirm_password": PASSWORD, "confirm_code": code}, **headers
    )
    assert response.status_code == (200 if refusal == "stale" else 403)
    assert AuditEvent.objects.count() == before
    recovery.instance.refresh_from_db()
    consumed = code not in recovery.get_unused_codes()
    recorded = "mfa" in factor_proofs(browser)
    if refusal == "stale":
        assert recorded
    assert consumed == recorded


@pytest.mark.xfail(
    strict=True,
    reason="Accepted limit: the password proof is one digest per session beside allauth's records; "
    "only a server clock set back between two re-authentications with different passwords lets "
    "the older record's time stand for the newer password.",
)
def test_password_record_uses_the_time_of_the_version_it_proved(make_user, monkeypatch):
    """A clock correction between two readings of private, in-memory allauth history."""
    from allauth.account.adapter import get_adapter
    from allauth.account.internal.flows.login import get_authentication_records
    from allauth.account.internal.flows.reauthentication import reauthenticate_by_password

    reader = make_user("receipt-clock")
    request = RequestFactory().post("/")
    request.user, request.session = reader, {}
    with request_context(request):
        for password in (PASSWORD, "replacement synthetic phrase"):
            if password != PASSWORD:
                reader.set_password(password)
                reader.save(update_fields=["password"])
            assert get_adapter(request).reauthenticate(reader, password)
            reauthenticate_by_password(request)
        first, last = get_authentication_records(request, reader)
        assert first["at"] < last["at"]
        now = (first["at"] + last["at"]) / 2
        monkeypatch.setattr(confirmation, "time", SimpleNamespace(time=lambda: now))
        assert not confirmation.current(request)


@pytest.mark.xfail(
    strict=True,
    reason="Accepted limit: the confirmation is read when the save is checked and not held "
    "through the save; a factor changed by another request between that check and the commit "
    "does not stop the save (accounts.confirmation module docstring).",
)
@pytest.mark.django_db(transaction=True)
def test_limits_rechecks_or_holds_the_confirmed_app_at_the_service_decision(make_user, monkeypatch):
    """Observe an unchanged limits submission. The second connection only reserves a row."""
    from allauth.mfa.models import Authenticator
    from django.db import OperationalError, transaction

    from core.session_limits import DEFAULTS, LIMITS_LOCK

    editor = make_user("limits-editor", "admin")
    secret, app = factor_app(editor)
    browser = factor_browser(editor)
    for route, fields in (
        ("account_reauthenticate", {"password": PASSWORD}),
        ("mfa_reauthenticate", {"code": factor_code(secret)}),
    ):
        assert browser.post(reverse(route), fields).status_code == 302
    url, headers, data = review_form_case(browser, "limits-full", None)
    data.update(DEFAULTS)
    order = []
    read = confirmation.read

    def read_inventory(user):
        state = read(user)
        order.append("inventory")
        return state

    def app_is_held():
        try:
            with transaction.atomic():
                Authenticator.objects.select_for_update(nowait=True).only("pk").get(pk=app.pk)
            return False
        except OperationalError as exc:
            if exc.__cause__.sqlstate != "55P03":
                raise
            return True
        finally:
            connections.close_all()

    def observe(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if "pg_advisory_xact_lock(" in sql and tuple(params or ()) == LIMITS_LOCK:
            with ThreadPoolExecutor(max_workers=1) as pool:
                held = pool.submit(app_is_held).result(timeout=10)
            order.append("held" if held else "available")
        return result

    monkeypatch.setattr(confirmation, "read", read_inventory)
    before = AuditEvent.objects.count()
    with connection.execute_wrapper(observe):
        response = browser.post(url, data, **headers)
    assert response.status_code == 302 and AuditEvent.objects.count() == before
    assert "inventory" in order and ("held" in order or "available" in order)
    assert "held" in order or order[-1] == "inventory", order
