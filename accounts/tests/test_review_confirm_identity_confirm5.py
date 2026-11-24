"""Confirmation and refusal order on the Mark as compromised admin page."""

from unittest.mock import patch

import pytest
from django.contrib.admin.models import LogEntry
from django.core.cache import cache
from django.test import Client
from django.utils.html import escape

from accounts import compromised
from accounts.admin import MarkCompromisedForm
from accounts.models import CompromisedPassword
from core.models import AuditEvent
from jobs.actor import system_account

from .conftest import PASSWORD
from .test_compromised_password import NEW_PAGE, NEW_PASSWORD
from .test_confirm_identity import sign_in_failures
from .test_review_compromised_password import (
    _draw_mark_form,
    _overlap_credential_operations,
    enter_with_email,
    mark_in_service,
    mark_page,
    with_mark,
)
from .test_review_confirm_identity import factor_browser, factor_check, factor_proofs

pytestmark = [pytest.mark.confirmation, pytest.mark.django_db]


@pytest.fixture
def editor(make_user):
    cache.clear()
    yield make_user("confirm5-editor", is_staff=True, is_superuser=True)
    cache.clear()


def assert_refusal_page(response, message):
    assert response.status_code == 200
    assert escape(message).encode() in response.content
    for field in ("reason", "confirm_password", "confirm_code"):
        assert f'name="{field}"'.encode() not in response.content


@pytest.mark.parametrize("case", ["own", "shared", "system", "unusable"])
def test_account_refusals_precede_form_validation(editor, make_user, case):
    if case == "own":
        person = editor
    elif case == "system":
        person = system_account()
    else:
        person = make_user("confirm5-person", credentials_locked=(case == "shared"))
        if case == "unusable":
            person.set_unusable_password()
            person.save(update_fields=["password"])

    browser = factor_browser(editor)
    url = mark_page(person)
    data = {"drawn": compromised.drawn_password(person), "reason": "Synthetic review"}
    assert compromised.refusal(person, editor) == compromised.REFUSALS[case]
    with patch.object(MarkCompromisedForm, "full_clean") as clean:
        for response in (
            browser.get(url),
            browser.post(url, {**data, "confirm_password": PASSWORD}),
        ):
            if case == "system":
                assert response.status_code == 403
            else:
                assert_refusal_page(response, compromised.REFUSALS[case])
        clean.assert_not_called()
    assert factor_proofs(browser) == []
    assert not CompromisedPassword.objects.exists()
    assert not AuditEvent.objects.filter(action=compromised.ACTION).exists()
    assert not LogEntry.objects.exists()


@pytest.mark.parametrize("confirmation", ["missing", "incorrect", "typed", "recent"])
def test_an_open_mark_on_a_drawn_page_and_a_submitted_form(editor, make_user, confirmation):
    person = make_user("confirm5-person")
    other = make_user("confirm5-other-editor", is_staff=True, is_superuser=True)
    browser, url, drawn = _draw_mark_form(editor, person)
    original = mark_in_service(other, person)
    assert_refusal_page(browser.get(url), compromised.ALREADY_MARKED)
    assert factor_proofs(browser) == []

    data = {"reason": "Second review", "drawn": drawn}
    if confirmation == "recent":
        factor_check(browser, confirm_password=PASSWORD)
        assert factor_proofs(browser) == ["password"]
    elif confirmation == "typed":
        data["confirm_password"] = PASSWORD
    elif confirmation == "incorrect":
        data["confirm_password"] = "incorrect synthetic password"
    with patch.object(compromised, "mark_compromised", wraps=compromised.mark_compromised) as save:
        response = browser.post(url, data)
    assert_refusal_page(response, compromised.ALREADY_MARKED)
    if confirmation in ("typed", "recent"):
        save.assert_called_once_with(
            actor=editor, account_id=person.pk, reason=data["reason"], drawn=drawn
        )
        assert compromised.ALREADY_MARKED in response.context["form"].non_field_errors()
        assert factor_proofs(browser) == ["password"]
    else:
        save.assert_not_called()
        assert factor_proofs(browser) == []
    failures = [("reauth_failed", "password")] if confirmation == "incorrect" else []
    assert sign_in_failures(editor) == failures
    mark = CompromisedPassword.objects.get(user=person)
    assert mark.pk == original.pk and mark.lifted_at is None
    assert AuditEvent.objects.get(action=compromised.ACTION, object_id=person.pk).actor == other
    assert not LogEntry.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_confirmation_on_submit_waits_for_the_account_holders_password_change(
    editor, make_user, settings
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    person = make_user("confirm5-person")
    other = make_user("confirm5-other-editor", is_staff=True, is_superuser=True)
    browser, url, drawn = _draw_mark_form(editor, person)
    mark = mark_in_service(other, person)
    writer = Client()
    session = enter_with_email(writer, person)
    replacement = with_mark(person, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
    submission = {"reason": "Second review", "drawn": drawn, "confirm_password": PASSWORD}

    saved, refused = _overlap_credential_operations(
        lambda: writer.post(NEW_PAGE, replacement), lambda: browser.post(url, submission)
    )
    assert saved.status_code == 302 and refused.status_code == 200
    assert escape(compromised.CHANGED_SINCE_DRAWN).encode() in refused.content
    assert escape(compromised.ALREADY_MARKED).encode() not in refused.content
    assert factor_proofs(browser) == ["password"]
    person.refresh_from_db()
    session.refresh_from_db()
    assert person.check_password(NEW_PASSWORD) and session.ended_at is None
    retained = CompromisedPassword.objects.get(user=person)
    assert retained.pk == mark.pk and retained.lifted_at is not None
    assert AuditEvent.objects.get(action=compromised.ACTION, object_id=person.pk).actor == other
    assert not LogEntry.objects.exists()
