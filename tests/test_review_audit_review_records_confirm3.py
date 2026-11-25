"""Third confirmation of signed reviews, using the ordinary sign-in form and synthetic accounts."""

import sys
from contextlib import contextmanager

import pytest
from allauth.core import context
from django.contrib.auth import SESSION_KEY
from django.urls import reverse
from django.views.debug import get_exception_reporter_filter

from accounts.models import AccountSession, SignInEvent
from audit_review.tests.conftest import PASSWORD
from audit_review.tests.conftest import make_user as make_user
from audit_review.tests.conftest import reviewer as reviewer


@contextmanager
def _observe_report_masks(fields_by_function, password):
    """Keep function names and mask checks only, during an ordinary request."""
    observed = {}
    previous = sys.getprofile()

    def sample(frame, event, _arg):
        if event != "call":
            return
        key = (frame.f_globals.get("__name__"), frame.f_code.co_name)
        field = fields_by_function.get(key)
        if field is None:
            return
        request = context.request
        reporter = get_exception_reporter_filter(request)
        local_vars = dict(reporter.get_traceback_frame_variables(request, frame))
        value = local_vars[field]
        if isinstance(value, dict):
            value = value.get("password")
        observed[key] = observed.get(key, True) and value != password

    try:
        sys.setprofile(sample)
        yield observed
    finally:
        sys.setprofile(previous)


@pytest.mark.confirmation
@pytest.mark.parametrize("outcome", ["right", "wrong", "locked", "inactive", "request-limit"])
def test_sign_in_preserves_the_result_history_and_adapter_masks(
    client, make_user, settings, outcome
):
    settings.DEBUG = False
    user = make_user("adapter-reader", "technical", is_active=outcome != "inactive")
    url = reverse("account_login")
    failed = ("failed", "password", "")
    expected_history = {
        "right": [("signed_in", "password", "")],
        "wrong": [failed],
        "locked": [failed, ("locked_out", "password", "")],
        "inactive": [("refused", "", "deactivated")],
        "request-limit": [failed],
    }
    settings.ACCOUNT_RATE_LIMITS = {
        "login_failed": "1/m/key" if outcome == "locked" else "10/m/key",
        "login": "1/m/ip" if outcome == "request-limit" else "10/m/ip",
    }
    wrong = "wrong synthetic credential"
    if outcome in {"locked", "request-limit"}:
        first = client.post(url, {"login": user.username, "password": wrong})
        assert first.status_code == 200
    password = wrong if outcome == "wrong" else PASSWORD
    functions = {"authenticate", "pre_authenticate", "authentication_failed", "_record_attempt"}
    targets = {("accounts.adapter", name): "credentials" for name in functions}
    with _observe_report_masks(targets, password) as observed:
        response = client.post(url, {"login": user.username, "password": password})

    expected_methods = {
        "right": {"authenticate", "pre_authenticate"},
        "inactive": {"authenticate", "pre_authenticate"},
        "wrong": functions,
        "locked": {"authenticate", "pre_authenticate", "_record_attempt"},
        "request-limit": set(),
    }
    assert (
        response.status_code
        == {"right": 302, "wrong": 200, "locked": 200, "inactive": 302, "request-limit": 429}[
            outcome
        ]
    )
    if outcome == "inactive":
        assert response.url == reverse("account_inactive")
    if outcome == "right":
        assert client.get(reverse("dashboard")).wsgi_request.user.pk == user.pk
    else:
        assert SESSION_KEY not in client.session
    assert AccountSession.objects.filter(user=user).count() == int(outcome == "right")
    assert (
        list(
            SignInEvent.objects.filter(user=user)
            .order_by("pk")
            .values_list("kind", "factor", "ending")
        )
        == expected_history[outcome]
    )
    assert {method for _, method in observed} == expected_methods[outcome]
    assert all(observed.values()), observed
    reporter = get_exception_reporter_filter(response.wsgi_request)
    masked = (
        reporter.get_post_parameters(response.wsgi_request)["password"]
        == reporter.cleansed_substitute
    )
    assert masked


@pytest.mark.confirmation
@pytest.mark.xfail(
    strict=True,
    reason="Accepted limit (third confirmation, finding 1; predates): Django's authenticate() "
    "names only `credentials` as sensitive, and as the nearest marker its list applies to the "
    "authentication backend it calls, whose password argument stays readable to the report "
    "filter. This release sends no error report (docs/operations.md; accounts/adapter.py).",
)
def test_sign_in_masks_the_password_in_the_backend_called_by_the_adapter(
    client, make_user, settings
):
    """The adapter's documentation also covers the credential check it calls."""
    settings.DEBUG = False
    user = make_user("backend-reader", "technical")
    key = ("django.contrib.auth.backends", "authenticate")
    with _observe_report_masks({key: "password"}, PASSWORD) as observed:
        response = client.post(
            reverse("account_login"), {"login": user.username, "password": PASSWORD}
        )
    assert response.status_code == 302 and client.session[SESSION_KEY] == str(user.pk)
    assert key in observed
    assert observed[key], "The production filter retained the submitted password in the backend."


@pytest.mark.parametrize("unavailable", ["note-revoked", "interval-revoked", "note-missing"])
@pytest.mark.parametrize("visible_input", ["valid", "invalid"])
def test_unavailable_review_forms_do_not_redraw_or_write(
    client, reviewer, unavailable, visible_input
):
    """A revision that would need repair does not reopen a refused or missing form."""
    from audit_review import reviews
    from audit_review.tests.conftest import review_permission
    from audit_review.tests.test_reviews import sign
    from core.models import AuditEvent

    record = sign(reviewer)
    client.force_login(reviewer)
    if unavailable == "interval-revoked":
        url = reverse("audit_review:review-interval")
        data = {"interval_days": 7, "reason": "Weekly." if visible_input == "valid" else ""}
        action = reviews.INTERVAL_CHANGED
    else:
        key = record.pk + 1 if unavailable == "note-missing" else record.pk
        url = reverse("audit_review:review-note", args=[key])
        data = {"text": "Synthetic observation." if visible_input == "valid" else ""}
        action = reviews.NOTE_ADDED
    if unavailable.endswith("revoked"):
        assert client.get(url).status_code == 200
        reviewer.user_permissions.remove(review_permission())
    response = client.post(url, {**data, "expected_revision": "invalid"})
    assert response.status_code == (404 if unavailable == "note-missing" else 403)
    assert b'name="expected_revision"' not in response.content
    assert b"Check the highlighted" not in response.content
    assert b"This form is shown again" not in response.content
    assert not AuditEvent.objects.filter(action=action).exists()
    assert not record.notes.exists() and reviews.schedule().revision == 0
