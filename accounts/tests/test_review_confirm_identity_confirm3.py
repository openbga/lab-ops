"""Third confirmation checks. Every account, form value, and storage fault is synthetic.

Adopted from the review (round 1, third confirmation). Changed on adoption: failures of the server
or of the session's storage after a code was accepted, an admin save refused after its form was
checked, and a stored recovery-code proof beside an app added at the very same instant are
accepted limits, strict xfails (the accounts.confirmation module docstring)."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group
from django.contrib.sessions.backends.base import UpdateError
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.sessions.models import Session
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, OperationalError, connection
from django.http import HttpResponseServerError
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts import compromised, confirmation
from accounts.admin import AccountAdmin, RoleGroupAdmin
from accounts.models import AccountSession, CompromisedPassword
from core.form_dialog import FormDialogMiddleware
from core.models import AuditEvent

from .conftest import PASSWORD
from .test_review_confirm_identity import (
    factor_app,
    factor_browser,
    factor_proofs,
    review_form_case,
)

pytestmark = [pytest.mark.confirmation, pytest.mark.django_db(transaction=True)]

FAULT = pytest.mark.xfail(
    strict=True,
    reason="Accepted limit: a failure of the server or of the session's storage after a code was "
    "accepted can leave it used without the confirmation recorded (allauth's own pages too); the "
    "person confirms again.",
)


@pytest.fixture
def editor(make_user):
    return make_user("confirmation-three", "admin", is_superuser=True, is_staff=True)


def unused(recovery):
    recovery.instance.refresh_from_db()
    return recovery.get_unused_codes()


def methods(session):
    return [
        item["method"]
        for item in session.get(AUTHENTICATION_METHODS_SESSION_KEY, [])
        if item.get("reauthenticated")
    ]


@pytest.mark.parametrize("case", ["limits-full", "limits-dialog", "add-role"])
def test_response_session_write_preserves_proofs_and_later_messages(
    editor, settings, monkeypatch, case
):
    settings.MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"
    browser = factor_browser(editor)
    session = browser.session
    session["review_note"] = "existing preference"
    session.save()
    key = session.session_key
    factor_app(editor)
    recovery = RecoveryCodes.activate(editor)
    code = recovery.get_unused_codes()[0]
    url, headers, data = review_form_case(browser, case, None)
    saves = []
    persist = SessionStore.save

    def observe(store, *args, **kwargs):
        persist(store, *args, **kwargs)
        stored = SessionStore(session_key=key)
        saves.append((methods(stored), "_messages" in stored))

    monkeypatch.setattr(SessionStore, "save", observe)
    response = browser.post(
        url, {**data, "confirm_password": PASSWORD, "confirm_code": code}, **headers
    )
    dialog = case == "limits-dialog"
    assert response.status_code == (204 if dialog else 302)
    destination = response["HX-Redirect" if dialog else "Location"]
    assert saves and all(record == ["password", "mfa"] for record, _ in saves)
    assert saves[-1][1]  # The response saved the later success notice as well.
    saved = SessionStore(session_key=key)
    assert browser.session.session_key == key
    assert methods(saved) == ["password", "mfa"]
    assert saved["review_note"] == "existing preference"
    assert code not in unused(recovery)
    page = browser.get(destination)
    expected = "added successfully" if case == "add-role" else "Staff session limits saved."
    assert expected in page.content.decode()
    assert AuditEvent.objects.filter(actor=editor).count() == 1


@pytest.mark.parametrize(
    "case",
    [
        "limits-dialog",
        "add-role",
        pytest.param("own-recovery", marks=FAULT),
        pytest.param("own-password", marks=FAULT),
    ],
)
def test_proved_factors_survive_response_failure_after_the_view_returns(
    editor, monkeypatch, settings, case
):
    settings.DEBUG = False
    browser = factor_browser(editor)
    factor_app(editor)
    recovery = RecoveryCodes.activate(editor)
    code = recovery.get_unused_codes()[0]
    headers = {}
    if case.startswith("own-"):
        is_password = case == "own-password"
        url = reverse("account_reauthenticate" if is_password else "mfa_reauthenticate")
        data = {"password": PASSWORD} if is_password else {"code": code}
    else:
        url, headers, data = review_form_case(browser, case, None)
        data.update(confirm_password=PASSWORD, confirm_code=code)
    transport = FormDialogMiddleware.__call__
    completed = []

    def fail_response(middleware, request):
        response = transport(middleware, request)
        completed.append((response.status_code, methods(request.session)))
        return HttpResponseServerError("Synthetic response failure.")

    monkeypatch.setattr(FormDialogMiddleware, "__call__", fail_response)
    response = browser.post(url, data, **headers)
    expected = {"own-password": ["password"], "own-recovery": ["mfa"]}.get(
        case, ["password", "mfa"]
    )
    assert completed == [(204 if case == "limits-dialog" else 302, expected)]
    assert response.status_code == 500
    assert (code not in unused(recovery)) == ("mfa" in expected)
    assert AuditEvent.objects.filter(actor=editor).count() == int(not case.startswith("own-"))
    assert factor_proofs(browser) == expected


def test_admin_audit_error_rolls_back_early_session_save_and_code(editor, monkeypatch, settings):
    settings.DEBUG = False
    browser = factor_browser(editor)
    browser.raise_request_exception = False
    factor_app(editor)
    recovery = RecoveryCodes.activate(editor)
    code = recovery.get_unused_codes()[0]
    url, headers, data = review_form_case(browser, "add-role", None)
    fail = Mock(side_effect=IntegrityError("Synthetic audit write failure."))
    monkeypatch.setattr(AuditEvent.objects, "create", fail)
    response = browser.post(
        url, {**data, "confirm_password": PASSWORD, "confirm_code": code}, **headers
    )
    fail.assert_called_once()
    assert response.status_code == 500
    assert not Group.objects.filter(name=data["name"]).exists()
    assert not LogEntry.objects.filter(user=editor).exists()
    assert code in unused(recovery)
    assert factor_proofs(browser) == []


@pytest.mark.parametrize("case", ["limits-dialog", "add-role", "own-password"])
def test_interrupted_session_save_uses_djangos_interruption_response(
    editor, monkeypatch, settings, case
):
    """Exercise the database backend's missing-row result, without overlapping requests."""
    settings.DEBUG = False
    browser = factor_browser(editor)
    browser.raise_request_exception = False
    headers = {}
    if case == "own-password":
        url, data = reverse("account_reauthenticate"), {"password": PASSWORD}
    else:
        url, headers, data = review_form_case(browser, case, None)
        # An ordinary required business field is empty, so this submission only checks the form.
        data["name" if case == "add-role" else "reason"] = ""
        data["confirm_password"] = PASSWORD
    interrupted = Mock(side_effect=UpdateError("The session row no longer exists."))
    monkeypatch.setattr(SessionStore, "save", interrupted)
    response = browser.post(url, data, **headers)
    interrupted.assert_called_once()
    assert not AuditEvent.objects.filter(actor=editor).exists()
    assert response.status_code == 400


@pytest.mark.parametrize(
    "case",
    [pytest.param("limits-full", marks=FAULT), pytest.param("mark", marks=FAULT), "add-role"],
)
def test_recovery_use_matches_receipt_if_session_storage_cannot_update(
    editor, make_user, monkeypatch, settings, case
):
    """A different backend failure: the existing row survives a failed UPDATE."""
    settings.DEBUG = False
    browser = factor_browser(editor)
    browser.raise_request_exception = False
    subject = make_user("storage-subject")
    url, headers, data = review_form_case(browser, case, subject)
    data["name" if case == "add-role" else "reason"] = ""
    factor_app(editor)
    recovery = RecoveryCodes.activate(editor)
    code = recovery.get_unused_codes()[0]
    key = browser.session.session_key
    failed = []

    def storage_fault(execute, sql, params, many, context):
        if sql.startswith('UPDATE "django_session" SET'):
            failed.append(True)
            # The DB session backend translates a database update failure to UpdateError.
            raise OperationalError("Synthetic session-table write failure.")
        return execute(sql, params, many, context)

    with connection.execute_wrapper(storage_fault):
        response = browser.post(
            url, {**data, "confirm_password": PASSWORD, "confirm_code": code}, **headers
        )
    assert failed and response.status_code in (400, 500)
    assert Session.objects.filter(session_key=key).exists()
    assert not AuditEvent.objects.filter(actor=editor).exists()
    used = code not in unused(recovery)
    proved = "mfa" in methods(SessionStore(session_key=key))
    assert used == proved, {"code_used": used, "durable_proof": proved}


@pytest.mark.xfail(
    strict=True,
    reason="Accepted limit: a recovery-code proof stored in the session is compared with the "
    "newest app's time, so it counts for a replacement app added at the very same instant.",
)
@pytest.mark.parametrize("surface", ["combined", "own"])
def test_stored_recovery_proof_tracks_the_app_when_enrolment_times_match(make_user, surface):
    """Read the confirmation status of a draft-only account after restoring an enrollment date."""
    reader = make_user("enrolment-reader")
    _, original = factor_app(reader)
    recovery = RecoveryCodes.activate(reader)
    code = recovery.get_unused_codes()[0]
    browser = factor_browser(reader)
    if surface == "own":
        assert (
            browser.post(reverse("account_reauthenticate"), {"password": PASSWORD}).status_code
            == 302
        )
        assert browser.post(reverse("mfa_reauthenticate"), {"code": code}).status_code == 302
    else:
        from allauth.core.context import request_context
        from django import forms
        from django.test import RequestFactory

        class Draft(confirmation.ConfirmIdentity, forms.Form):
            pass

        request = RequestFactory().post("/", {"confirm_password": PASSWORD, "confirm_code": code})
        request.user, request.session = reader, browser.session
        with request_context(request):
            assert Draft(request.POST).is_valid()
        # Deliver the response-time write too, as on both reviewed revisions.
        request.session.save()
    request = SimpleNamespace(user=reader, session=browser.session)
    assert confirmation.current(request)
    original_id, original_time = original.pk, original.created_at
    original.delete()
    _, replacement = factor_app(reader)
    assert original_id != replacement.pk
    Authenticator.objects.filter(pk=replacement.pk).update(created_at=original_time)
    assert not confirmation.current(request)


@pytest.mark.parametrize("case", ["limits-dialog", "add-role"])
def test_durable_receipt_after_the_response_time_session_update_fails(
    editor, monkeypatch, settings, case
):
    settings.DEBUG = False
    browser = factor_browser(editor)
    browser.raise_request_exception = False
    factor_app(editor)
    recovery = RecoveryCodes.activate(editor)
    code = recovery.get_unused_codes()[0]
    url, headers, data = review_form_case(browser, case, None)
    process = SessionMiddleware.process_response
    fail = Mock(side_effect=UpdateError("Synthetic response-time storage failure."))

    def finish(middleware, request, response):
        with monkeypatch.context() as patch:
            patch.setattr(request.session, "save", fail)
            return process(middleware, request, response)

    monkeypatch.setattr(SessionMiddleware, "process_response", finish)
    response = browser.post(
        url, {**data, "confirm_password": PASSWORD, "confirm_code": code}, **headers
    )
    fail.assert_called_once()
    assert response.status_code == 400
    assert AuditEvent.objects.filter(actor=editor).count() == 1
    assert code not in unused(recovery)
    assert factor_proofs(browser) == ["password", "mfa"]


def test_failed_mark_history_write_rolls_back_the_service_after_account_reread(
    editor, make_user, monkeypatch, settings
):
    settings.DEBUG = False
    subject = make_user("mark-history-subject")
    factor_browser(subject)
    browser = factor_browser(editor)
    browser.raise_request_exception = False
    url, headers, data = review_form_case(browser, "mark", subject)
    factor_app(editor)
    recovery = RecoveryCodes.activate(editor)
    code = recovery.get_unused_codes()[0]
    failure = Mock(side_effect=IntegrityError("Synthetic admin history failure."))
    monkeypatch.setattr(AccountAdmin, "log_change", failure)
    response = browser.post(
        url, {**data, "confirm_password": PASSWORD, "confirm_code": code}, **headers
    )
    failure.assert_called_once()
    assert response.status_code == 500
    assert not CompromisedPassword.objects.filter(user=subject).exists()
    assert not AuditEvent.objects.filter(action=compromised.ACTION).exists()
    assert AccountSession.objects.get(user=subject).ended_at is None
    assert code not in unused(recovery)
    assert factor_proofs(browser) == ["password", "mfa"]


def test_factor_inventory_distinguishes_apps_from_recovery_and_unsupported_rows(make_user):
    reader = make_user("inventory-only")
    other = make_user("other-inventory")
    factor_app(other)
    recovery = RecoveryCodes.activate(reader)
    Authenticator.objects.create(user=reader, type=Authenticator.Type.WEBAUTHN, data={})
    assert confirmation.read(reader).asks() == (True, False)
    _, app = factor_app(reader)
    with CaptureQueriesContext(connection) as queries:
        state = confirmation.read(reader)
    assert len(queries) == 1
    assert state.asks() == (True, True) and state.authenticators == {app.pk, recovery.instance.pk}
    assert state.app_since == app.created_at.timestamp()
    assert state.apps == {app.pk}


@pytest.mark.parametrize(
    "late_failure",
    [
        pytest.param(False, id="normal-response"),
        pytest.param(True, id="failed-response", marks=FAULT),
    ],
)
def test_editor_retains_the_receipt_when_another_accounts_password_page_rotates_the_session(
    editor, make_user, settings, monkeypatch, late_failure
):
    settings.DEBUG = False
    subject = make_user("rotation-subject")
    factor_app(editor)
    recovery = RecoveryCodes.activate(editor)
    code = recovery.get_unused_codes()[0]
    browser = factor_browser(editor)
    cookie = settings.SESSION_COOKIE_NAME
    original = browser.cookies[cookie].value
    url, headers, data = review_form_case(browser, "set-password", subject)
    delivered = []
    transport = FormDialogMiddleware.__call__

    def finish(middleware, request):
        result = transport(middleware, request)
        delivered.append(result.status_code)
        return HttpResponseServerError("Synthetic response failure.") if late_failure else result

    with monkeypatch.context() as patch:
        patch.setattr(FormDialogMiddleware, "__call__", finish)
        response = browser.post(
            url, {**data, "confirm_password": PASSWORD, "confirm_code": code}, **headers
        )
    assert delivered == [302] and response.status_code == (500 if late_failure else 302)
    subject.refresh_from_db()
    assert subject.check_password(data["password1"])
    assert AuditEvent.objects.get(action="account.password_changed").actor_id == editor.pk
    assert code not in unused(recovery)
    assert not Session.objects.filter(session_key=original).exists()
    # Read through precisely the cookie the browser received, without creating a new test session.
    saved = SessionStore(session_key=browser.cookies[cookie].value)
    assert (saved.get("_auth_user_id") == str(editor.pk), methods(saved)) == (
        True,
        ["password", "mfa"],
    )


@pytest.mark.xfail(
    strict=True,
    reason="Accepted limit: an admin console save refused after its form was checked can keep the "
    "confirmation in the session while its transaction gives the code back.",
)
def test_admin_permission_exception_keeps_rolled_back_code_use_and_receipt_together(
    editor, monkeypatch
):
    browser = factor_browser(editor)
    factor_app(editor)
    recovery = RecoveryCodes.activate(editor)
    code = recovery.get_unused_codes()[0]
    url, headers, data = review_form_case(browser, "add-role", None)
    # Exercise an exception inside the admin's transaction after validation has finished.
    refused = Mock(side_effect=PermissionDenied("Synthetic save-hook refusal."))
    monkeypatch.setattr(RoleGroupAdmin, "save_model", refused)
    response = browser.post(
        url, {**data, "confirm_password": PASSWORD, "confirm_code": code}, **headers
    )
    refused.assert_called_once()
    assert response.status_code == 403
    assert not Group.objects.filter(name=data["name"]).exists()
    assert not AuditEvent.objects.filter(actor=editor).exists()
    used = code not in unused(recovery)
    proved = "mfa" in factor_proofs(browser)
    assert used == proved, {"code_used": used, "durable_proof": proved}
