"""Checks adjacent to the second confirmation fixes, using synthetic data.

Adopted from the review (round 1, second confirmation). Changed on adoption: a form refused before
its final reading while its account's methods change is an accepted limit, a strict xfail (the
accounts.confirmation module docstring)."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from allauth.core.context import request_context
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from django import forms
from django.contrib.admin.models import LogEntry
from django.contrib.messages import get_messages
from django.db import IntegrityError, connections
from django.test import RequestFactory

from accounts import compromised, confirmation
from accounts.models import AccountSession, CompromisedPassword, User
from core.models import AuditEvent, SecondFactorRule, SessionLimit

from .conftest import PASSWORD
from .test_review_confirm_identity import (
    factor_app,
    factor_browser,
    factor_proofs,
    review_form_case,
)

pytestmark = [pytest.mark.confirmation, pytest.mark.django_db]

REDRAWN = pytest.mark.xfail(
    strict=True,
    reason="Accepted limit: a confirmation refused before the final reading (a field left empty, "
    "a wrong password or code) while the account gains or loses a method is drawn again with the "
    "fields it asked for; the save is refused, and the next form asks for what the account has.",
)


class Draft(confirmation.ConfirmIdentity, forms.Form):
    """A form with no model, service, or save method."""

    note = forms.CharField()


def draft_request(reader, **values):
    request = RequestFactory().post("/", {"note": "synthetic draft", **values})
    request.user, request.session = reader, {}
    return request


@REDRAWN
@pytest.mark.parametrize("method", ["password", "app"])
@pytest.mark.parametrize("filled", [False, True], ids=["empty", "entered"])
def test_loss_of_last_method_explains_recovery_even_on_early_return(make_user, method, filled):
    reader = make_user("draft-reader")
    if method == "app":
        reader.set_unusable_password()
        reader.save(update_fields=["password"])
        _, app = factor_app(reader)
        field, value = "confirm_code", "sample entry"
    else:
        field, value = "confirm_password", PASSWORD
    request = draft_request(reader, **{field: value if filled else ""})
    with request_context(request):
        form = Draft(request.POST)
        assert [item.name for item in form.confirm_fields()] == [field]
        if method == "app":
            app.delete()
        else:
            reader.set_unusable_password()
            reader.save(update_fields=["password"])
        assert not form.is_valid()
        assert form["note"].value() == "synthetic draft"
        assert not form.confirmed_now
        assert (list(form.non_field_errors()), [f.name for f in form.confirm_fields()]) == (
            [confirmation.NO_METHOD],
            [],
        )


@REDRAWN
def test_method_added_to_an_empty_inventory_updates_the_retry_form(make_user):
    reader = make_user("draft-enrolling")
    reader.set_unusable_password()
    reader.save(update_fields=["password"])
    request = draft_request(reader)
    with request_context(request):
        form = Draft(request.POST)
        assert form.confirm_fields() == []
        factor_app(reader)
        assert not form.is_valid()
        assert [field.name for field in form.confirm_fields()] == ["confirm_code"]
        assert confirmation.NO_METHOD not in form.non_field_errors()


def test_correct_password_checked_after_rotation_is_accepted(make_user):
    reader = make_user("draft-rotating")
    updated = "new synthetic draft password"
    request = draft_request(reader, confirm_password=updated)
    with request_context(request):
        form = Draft(request.POST)
        reader.set_password(updated)
        reader.save(update_fields=["password"])
        assert form.is_valid()
        assert confirmation.current(request)


@pytest.mark.parametrize(
    ("change", "should_accept"),
    [
        ("replace-app-same-time", False),
        ("replace-app-later", False),
        ("replace-recovery-set", True),
        ("unchanged", True),
    ],
)
def test_recovery_and_app_proofs_follow_enrolment_identity(
    make_user, monkeypatch, change, should_accept
):
    """Use database inventories and a dummy code check on a draft with no save operation."""
    reader = make_user("inventory-reader")
    reader.set_unusable_password()
    reader.save(update_fields=["password"])
    _, app = factor_app(reader)
    recovery = RecoveryCodes.activate(reader)
    verified = app if change == "replace-recovery-set" else recovery.instance
    verifier = Mock(authenticator=verified)
    verifier.is_valid.return_value = True
    monkeypatch.setattr("accounts.forms.MFAReauthenticateForm", lambda **kwargs: verifier)
    request = draft_request(reader, confirm_code="fixture")
    with request_context(request):
        form = Draft(request.POST)
        if change.startswith("replace-app"):
            original_id, original_time = app.pk, app.created_at
            app.delete()
            _, replacement = factor_app(reader)
            assert replacement.pk != original_id
            if change == "replace-app-same-time":
                Authenticator.objects.filter(pk=replacement.pk).update(created_at=original_time)
            else:
                assert replacement.created_at > original_time
        elif change == "replace-recovery-set":
            recovery.instance.delete()
            RecoveryCodes.activate(reader)
        assert form.is_valid() is should_accept
        assert verifier.save.call_count == int(should_accept)


@pytest.mark.parametrize("session", [None, "absent", {}, {"_confirmation_password": "previous"}])
def test_password_signal_without_a_checked_password_handles_each_session(session):
    request = SimpleNamespace()
    if session != "absent":
        request.session = session
    confirmation.remember_password(None, request=request, method="password", reauthenticated=True)
    assert getattr(request, "session", None) in (None, {})


def settings_and_mark_records(subject):
    return (
        list(SessionLimit.objects.order_by("pk").values()),
        list(SecondFactorRule.objects.order_by("pk").values()),
        CompromisedPassword.objects.filter(user=subject).count(),
        list(AccountSession.objects.filter(user=subject).values_list("id", "ending")),
        AuditEvent.objects.count(),
        LogEntry.objects.count(),
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("case", ["limits-full", "rule-dialog", "mark"])
def test_audit_failure_keeps_recovery_inventory_and_browser_receipt_consistent(
    make_user, monkeypatch, settings, case
):
    settings.DEBUG = False
    editor = make_user("audit-editor", "admin", is_staff=True, is_superuser=True)
    subject = make_user("audit-subject")
    factor_browser(subject)
    factor_app(editor)
    recovery = RecoveryCodes.activate(editor)
    code = recovery.get_unused_codes()[0]
    browser = factor_browser(editor)
    browser.raise_request_exception = False
    url, headers, data = review_form_case(browser, case, subject)
    before = settings_and_mark_records(subject)
    with monkeypatch.context() as patch:
        write = Mock(side_effect=IntegrityError("synthetic audit storage failure"))
        patch.setattr(AuditEvent.objects, "create", write)
        response = browser.post(
            url, {**data, "confirm_password": PASSWORD, "confirm_code": code}, **headers
        )
    write.assert_called_once()
    assert response.status_code == 500
    assert settings_and_mark_records(subject) == before
    recovery.instance.refresh_from_db()
    used = code not in recovery.get_unused_codes()
    proved = "mfa" in factor_proofs(browser)
    assert used == proved, {"code_used": used, "browser_proof": proved}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("outcome", ["stale", "saved"])
def test_mark_history_and_operator_message_identify_current_account(
    make_user, monkeypatch, outcome
):
    editor = make_user("mark-editor", is_staff=True, is_superuser=True)
    subject = make_user("former-account-name")
    browser = factor_browser(editor)
    url, headers, data = review_form_case(browser, "mark", subject)
    if outcome == "stale":
        data["drawn"] = "superseded"
    current_name = "current-account-name"
    mark = compromised.mark_compromised

    def rename():
        try:
            account = User.objects.get(pk=subject.pk)
            account.username = current_name
            account.save(update_fields=["username"])
        finally:
            connections.close_all()

    def save(**kwargs):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(rename).result(timeout=10)
        return mark(**kwargs)

    monkeypatch.setattr(compromised, "mark_compromised", save)
    response = browser.post(url, {**data, "confirm_password": PASSWORD}, **headers)
    if outcome == "stale":
        assert response.status_code == 200
        assert compromised.CHANGED_SINCE_DRAWN in response.context["form"].non_field_errors()
        assert response.context["form"]["reason"].value() == data["reason"]
        assert (
            current_name in response.context["title"] and current_name in response.content.decode()
        )
        assert not (
            LogEntry.objects.exists() or CompromisedPassword.objects.filter(user=subject).exists()
        )
    else:
        assert response.status_code == 302
        assert CompromisedPassword.objects.get(user=subject).marked_by_id == editor.pk
        assert AuditEvent.objects.get(action=compromised.ACTION).object_id == str(subject.pk)
        entry = LogEntry.objects.get(object_id=str(subject.pk))
        notices = [str(msg) for msg in get_messages(response.wsgi_request)]
        (mark_notice,) = [text for text in notices if text.startswith("Marked the password of ")]
        assert (entry.object_repr, mark_notice.partition(" as compromised.")[0]) == (
            current_name,
            f"Marked the password of {current_name}",
        )
