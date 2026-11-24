"""Checks of interrupted confirmation writes using synthetic accounts and Django sessions.

Adopted from the review (round 1, fourth confirmation). Changed on adoption: a second write of an
interrupted session by the response (session-backed notices, SESSION_SAVE_EVERY_REQUEST) is an
accepted limit, a strict xfail: it is interrupted the same way and the answer stays 400."""

from hashlib import sha256
from unittest.mock import Mock

import pytest
from allauth.core.context import request_context
from django import forms
from django.contrib import messages
from django.contrib.auth.models import Group
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.base import UpdateError
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.exceptions import SessionInterrupted
from django.contrib.sessions.models import Session
from django.http import HttpResponse
from django.test import RequestFactory

from accounts.confirmation import ConfirmIdentity
from core.models import AuditEvent

from .conftest import PASSWORD
from .test_review_confirm_identity import factor_browser, review_form_case

pytestmark = [pytest.mark.confirmation, pytest.mark.django_db(transaction=True)]

AGAIN = pytest.mark.xfail(
    strict=True,
    reason="Accepted limit: the response can write an interrupted session again (session-backed "
    "notices shown on the error page, SESSION_SAVE_EVERY_REQUEST); that write is interrupted the "
    "same way and the answer stays 400.",
)


class Draft(ConfirmIdentity, forms.Form):
    """This draft only validates. It has no service or save method."""


def test_missing_database_session_interrupts_validation_without_recreating_the_row(make_user):
    reader = make_user("interrupted-reader")
    request = RequestFactory().post("/", {"confirm_password": PASSWORD})
    request.user = reader
    request.session = SessionStore()
    request.session["draft"] = "synthetic note"
    request.session.save()
    key = request.session.session_key
    with request_context(request):
        form = Draft(request.POST)
        # The backend has loaded the session. Removing the fixture row exercises its actual
        # missing-row UPDATE result rather than a mocked exception.
        Session.objects.get(session_key=key).delete()
        with pytest.raises(SessionInterrupted):
            form.is_valid()
    assert request.session.modified is False
    assert not form.confirmed_now
    assert not Session.objects.filter(session_key=key).exists()


def queue_notices(browser, *, overflow):
    """Prepare pending messages with the release's ordinary cookie-to-session fallback."""
    request = RequestFactory().get("/")
    request.session = browser.session
    storage = FallbackStorage(request)
    for number in range(80 if overflow else 1):
        record_id = sha256(str(number).encode()).hexdigest()
        storage.add(messages.INFO, f"Synthetic notice for record {record_id}.")
    response = HttpResponse()
    storage.update(response)
    request.session.save()
    browser.cookies.update(response.cookies)
    assert ("_messages" in browser.session) is overflow


@pytest.mark.parametrize(
    ("case", "notices", "save_every_request"),
    [
        pytest.param("limits-dialog", None, False, id="dialog-release-defaults"),
        pytest.param("add-role", "cookie", False, id="admin-cookie-notice"),
        pytest.param(
            "limits-dialog", "overflow", False, id="dialog-session-backed-notices", marks=AGAIN
        ),
        pytest.param("add-role", "overflow", False, id="admin-session-backed-notices", marks=AGAIN),
        pytest.param("limits-dialog", None, True, id="dialog-save-every-request", marks=AGAIN),
    ],
)
def test_interrupted_confirmation_is_not_written_again_during_the_response(
    make_user, monkeypatch, settings, case, notices, save_every_request
):
    settings.DEBUG = False
    settings.SESSION_SAVE_EVERY_REQUEST = save_every_request
    editor = make_user("interruption-editor", "admin", is_staff=True, is_superuser=True)
    browser = factor_browser(editor)
    url, headers, data = review_form_case(browser, case, None)
    if notices:
        queue_notices(browser, overflow=notices == "overflow")
    browser.raise_request_exception = False
    refused = Mock(side_effect=UpdateError("Synthetic interrupted session update."))
    monkeypatch.setattr(SessionStore, "save", refused)
    response = browser.post(url, {**data, "confirm_password": PASSWORD}, **headers)
    assert response.status_code >= 400
    assert not AuditEvent.objects.filter(actor=editor).exists()
    if case == "add-role":
        assert not Group.objects.filter(name=data["name"]).exists()
    assert refused.call_count == 1, (response.status_code, refused.call_count)
