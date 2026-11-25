"""Second confirmation of signed reviews, using ordinary requests and synthetic accounts."""

import re
import sys
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
from threading import Event
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from allauth.core import context
from django.db import connections
from django.test import Client
from django.urls import reverse
from django.views.debug import ExceptionReporter, get_exception_reporter_filter

from accounts import confirmation
from accounts.models import SignInEvent
from audit_review.tests.conftest import PASSWORD, review_permission
from audit_review.tests.conftest import make_user as make_user
from audit_review.tests.conftest import reviewer as reviewer
from core.models import AuditEvent

# The released base has the account forms, before signed reviews were added.
if find_spec("audit_review.reviews") is not None:
    from audit_review import review_views, reviews
    from audit_review.tests.test_reviews import sign


def _assert_request_data_is_filtered(response, submitted):
    """Render locally with no exception or traceback. Keep only the names of failing surfaces."""
    request = response.wsgi_request
    reporter = ExceptionReporter(request, None, None, None)
    sections = {
        "POST section": dict(reporter.get_traceback_data()["filtered_POST_items"]),
        "request data in frame variables": reporter.filter.cleanse_special_types(
            request, request.POST
        ),
    }
    rendered = {"HTML": reporter.get_traceback_html(), "text": reporter.get_traceback_text()}
    missing_masks = []
    for field, marker in submitted.items():
        was_submitted = request.POST.get(field) == marker
        assert was_submitted, field
        for label, data in sections.items():
            if data.get(field) == marker:
                missing_masks.append((field, label))
        for label, content in rendered.items():
            if marker in content:
                missing_masks.append((field, label))
    assert not missing_masks, missing_masks


@pytest.mark.confirmation
@pytest.mark.parametrize("endpoint", ["account_login", "account_reauthenticate"])
@pytest.mark.parametrize("result", ["accepted", "wrong", "csrf", "body-limit"])
def test_request_reports_hide_the_password_on_both_account_pages(
    make_user, settings, endpoint, result
):
    settings.DEBUG = False
    if result == "body-limit":
        settings.MAX_REQUEST_BODY_BYTES = 10
    session = Client(enforce_csrf_checks=result == "csrf")
    user = make_user("synthetic-operator", "technical")
    login = endpoint == "account_login"
    if not login:
        session.force_login(user)
    url = reverse(endpoint)
    initial = session.get(url)
    assert initial.status_code == 200 and "password" in initial.context["form"].fields
    marker = "missing synthetic credential" if result == "wrong" else PASSWORD
    data = {"password": marker}
    if login:
        data["login"] = user.username
    response = session.post(url, data)
    assert (
        response.status_code
        == {"accepted": 302, "wrong": 200, "csrf": 403, "body-limit": 413}[result]
    )
    _assert_request_data_is_filtered(response, {"password": marker})
    if login:
        assert (session.session.get("_auth_user_id") == str(user.pk)) == (result == "accepted")
    elif result in ("accepted", "wrong"):
        assert confirmation.current(session.get(reverse("dashboard")).wsgi_request) == (
            result == "accepted"
        )
    failure = SignInEvent.Kind.FAILED if login else SignInEvent.Kind.REAUTHENTICATION_FAILED
    assert SignInEvent.objects.filter(user=user, kind=failure).count() == int(result == "wrong")


@pytest.mark.confirmation
@pytest.mark.parametrize("endpoint", ["account_login", "account_reauthenticate"])
def test_production_filter_protects_passwords_during_account_authentication(
    client, make_user, settings, endpoint
):
    """Record whether a synthetic password survives filtering in the application adapter."""
    settings.DEBUG = False
    user = make_user("authentication-reader", "technical")
    data = {"password": PASSWORD}
    if endpoint == "account_login":
        data["login"] = user.username
    else:
        client.force_login(user)
    visible = []

    def inspect_adapter(frame, event, _arg):
        if (
            event != "call"
            or frame.f_code.co_name != "authenticate"
            or frame.f_globals.get("__name__") != "accounts.adapter"
        ):
            return
        request = context.request
        filtered = dict(
            get_exception_reporter_filter(request).get_traceback_frame_variables(request, frame)
        )
        credentials = filtered.get("credentials")
        visible.append(isinstance(credentials, dict) and credentials.get("password") == PASSWORD)

    previous = sys.getprofile()
    try:
        sys.setprofile(inspect_adapter)
        response = client.post(reverse(endpoint), data)
    finally:
        sys.setprofile(previous)
    assert response.status_code == 302
    assert visible and not any(visible), (
        "The submitted password survived filtering in the account adapter."
    )


@pytest.mark.parametrize(
    "endpoint", ["account_change_password", "account_set_password", "account_signup"]
)
def test_views_with_explicit_field_lists_keep_masking_their_submitted_passwords(
    client, make_user, settings, endpoint
):
    settings.DEBUG = False
    user = make_user("field-list-reader", "technical")
    chosen = "paprika willow maple lantern 701"
    data = {"password1": chosen, "password2": chosen}
    if endpoint == "account_signup":
        data.update(username="unregistered-reader", email="unregistered@example.invalid")
    else:
        if endpoint == "account_set_password":
            user.set_unusable_password()
            user.save(update_fields=["password"])
        else:
            data["oldpassword"] = PASSWORD
        client.force_login(user)
    response = client.post(reverse(endpoint), data)
    if endpoint == "account_signup":
        assert (
            response.status_code == 200
            and not type(user).objects.filter(username=data["username"]).exists()
        )
        assert any("signup_closed" in (template.name or "") for template in response.templates)
    else:
        assert response.status_code == 302
        user.refresh_from_db()
        assert user.check_password(chosen)
    _assert_request_data_is_filtered(
        response, {name: value for name, value in data.items() if "password" in name}
    )


@pytest.mark.parametrize("existing", [("additional",), "__ALL__"], ids=["named", "all"])
def test_mark_sensitive_preserves_existing_names_and_multivalued_request_data(
    rf, settings, existing
):
    settings.DEBUG = False
    request = rf.post(
        "/",
        {
            "password": ["synthetic-one", "synthetic-two"],
            "confirm_password": "synthetic-confirmation",
            "confirm_code": "synthetic-code",
            "additional": "synthetic-additional",
            "public": "visible-as-usual",
        },
    )
    request.sensitive_post_parameters = existing
    confirmation.mark_sensitive(request)
    confirmation.mark_sensitive(request)
    reporter = get_exception_reporter_filter(request)
    for filtered in (
        reporter.get_post_parameters(request),
        reporter.cleanse_special_types(request, request.POST),
    ):
        for key in ("password", "confirm_password", "confirm_code", "additional"):
            assert filtered.getlist(key) == [reporter.cleansed_substitute], key
        assert filtered.get("public") == (
            reporter.cleansed_substitute if existing == "__ALL__" else "visible-as-usual"
        )
    assert request.POST.getlist("password") == ["synthetic-one", "synthetic-two"]


def test_admin_sign_in_still_routes_to_the_application_password_form(client, db):
    response = client.get(reverse("admin:login"))
    assert response.status_code == 302 and urlsplit(response.url).path == reverse("account_login")
    form = client.get(response.url).context["form"]
    assert {"login", "password"} <= set(form.fields)


def _revision_from_html(response):
    assert response.status_code == 200
    (number,) = re.findall(rb'name="expected_revision" value="([0-9]+)"', response.content)
    return int(number)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("surface", ["note", "interval"])
@pytest.mark.parametrize("revision", [None, "not-a-number", "-1"])
def test_visible_and_revision_errors_repair_only_the_snapshot_shown(
    reviewer, make_user, client, surface, revision
):
    record = sign(reviewer)
    colleague = make_user("repair-colleague", "technical")
    colleague.user_permissions.add(review_permission())
    other = Client()
    other.force_login(colleague)
    client.force_login(reviewer)
    if surface == "note":
        url = reverse("audit_review:review-note", args=[record.pk])
        action = reviews.NOTE_ADDED
        bad = {"text": "R" * 4001}
        good = {"text": "Corrected observation."}
        first = {"text": "First comparison."}
        later = {"text": "Later unseen change."}
        invalid_field = "text"
    else:
        url = reverse("audit_review:review-interval")
        action = reviews.INTERVAL_CHANGED
        bad = {"reason": "Replace the range error.", "interval_days": 367}
        good = {**bad, "interval_days": 21}
        first = {"reason": "First comparison.", "interval_days": 7}
        later = {"reason": "Later unseen change.", "interval_days": 14}
        invalid_field = "interval_days"

    start = _revision_from_html(client.get(url))
    assert other.post(url, {**first, "expected_revision": start}).status_code == 302
    if revision is not None:
        bad["expected_revision"] = revision
    initial_read_finished, allow_response = Event(), Event()
    render = review_views._render

    def hold_render(*args, **kwargs):
        # The invalid form has its repair and its rows. A second connection commits another
        # legitimate save before the first request turns those rows and controls into HTML.
        initial_read_finished.set()
        assert allow_response.wait(15)
        return render(*args, **kwargs)

    def send_invalid():
        try:
            return client.post(url, bad)
        finally:
            connections.close_all()

    with patch.object(review_views, "_render", hold_render), ThreadPoolExecutor(1) as pool:
        response = pool.submit(send_invalid)
        try:
            assert initial_read_finished.wait(10)
            assert other.post(url, {**later, "expected_revision": start + 1}).status_code == 302
        finally:
            allow_response.set()
        refused = response.result(timeout=10)

    seen = _revision_from_html(refused)
    assert seen == start + 1
    assert (
        b"First comparison." in refused.content and b"Later unseen change." not in refused.content
    )
    form = refused.context["form"]
    # Adapted (second confirmation, finding 2): with a visible field refused, the repaired revision
    # is not said as well; that field's error alone guides, and the summary counts only it.
    assert set(form.errors) == {invalid_field}
    assert form.non_field_errors() == []
    assert b"This form is shown again as" not in refused.content
    assert b"Expected revision:" not in refused.content
    assert f'id="id_{invalid_field}_error"'.encode() in refused.content
    for key, value in bad.items():
        if key != "expected_revision":
            assert str(form[key].value()) == str(value)
    assert AuditEvent.objects.filter(action=action).count() == 2

    still_stale = client.post(url, {**good, "expected_revision": seen})
    now_seen = _revision_from_html(still_stale)
    assert now_seen == start + 2 and b"Later unseen change." in still_stale.content
    assert set(still_stale.context["form"].errors) == {"__all__"}
    assert b"This form is shown again as" not in still_stale.content
    assert AuditEvent.objects.filter(action=action).count() == 2
    assert client.post(url, {**good, "expected_revision": now_seen}).status_code == 302
    assert AuditEvent.objects.filter(action=action).count() == 3


@pytest.mark.parametrize(
    ("surface", "input_data", "invalid_count"),
    [
        pytest.param("note", {"text": "Retained note."}, 0, id="note-hidden-only"),
        pytest.param(
            "interval", {"reason": "Weekly.", "interval_days": 7}, 0, id="interval-hidden-only"
        ),
        pytest.param("note", {"text": ""}, 1, id="note-one-visible"),
        pytest.param("interval", {"interval_days": 21, "reason": ""}, 1, id="interval-one-visible"),
        pytest.param(
            "interval", {"interval_days": 367, "reason": ""}, 2, id="interval-two-visible"
        ),
    ],
)
def test_error_summary_refers_to_the_number_of_highlighted_controls(
    client, reviewer, surface, input_data, invalid_count
):
    record = sign(reviewer)
    client.force_login(reviewer)
    if surface == "note":
        url = reverse("audit_review:review-note", args=[record.pk])
    else:
        url = reverse("audit_review:review-interval")
    response = client.post(url, input_data)
    assert response.status_code == 200
    html = response.content.decode()
    invalid_controls = re.findall(
        r'<(?:textarea|select|input)\b(?=[^>]*aria-invalid="true")(?![^>]*type="hidden")[^>]*>',
        html,
    )
    assert len(invalid_controls) == invalid_count
    instruction = re.search(r"Check the highlighted (fields?) below", html)
    if invalid_count:
        assert instruction is not None
        expected = "field" if invalid_count == 1 else "fields"
        assert instruction[1] == expected, (
            f"Prompt used {instruction[1]!r}. Visible controls highlighted: {invalid_count}."
        )
    else:
        assert instruction is None


@pytest.mark.parametrize("surface", ["note", "interval"])
def test_revoked_review_access_refuses_a_form_even_if_its_revision_needs_repair(
    client, reviewer, surface
):
    record = sign(reviewer)
    client.force_login(reviewer)
    if surface == "note":
        url = reverse("audit_review:review-note", args=[record.pk])
        data = {"text": "Synthetic follow-up."}
        action = reviews.NOTE_ADDED
    else:
        url = reverse("audit_review:review-interval")
        data = {"interval_days": 14, "reason": "Fortnightly."}
        action = reviews.INTERVAL_CHANGED
    assert client.get(url).status_code == 200
    reviewer.user_permissions.remove(review_permission())
    refused = client.post(url, data)
    assert refused.status_code == 403
    assert b'name="expected_revision"' not in refused.content
    assert not AuditEvent.objects.filter(action=action).exists()
