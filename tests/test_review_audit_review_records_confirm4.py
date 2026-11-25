"""Check the release note's claim about passwords entered again in a review workflow."""

import pytest
from django.views.debug import get_exception_reporter_filter

from audit_review.tests.conftest import PASSWORD, review_permission
from audit_review.tests.conftest import make_user as make_user
from tests.test_review_audit_review_records_confirm3 import (
    BackendPasswordReadable,
    _observe_report_masks,
)
from tests.test_review_audit_review_records_verify import password_request


@pytest.mark.confirmation
@pytest.mark.parametrize("flow", ["signing", "confirmation", "reauthentication"])
@pytest.mark.xfail(
    strict=True,
    # Adapted (fourth confirmation, finding 1): the release note now names this limit; only the
    # limit's own assertion is expected to fail, every other one must pass.
    raises=BackendPasswordReadable,
    reason="Accepted limit (third confirmation, finding 1; predates): Django's authentication "
    "backend receives the re-entered password as an argument its report filter does not hide. "
    "The release note and docs/operations.md say so; this release sends no error report.",
)
def test_reentered_password_is_hidden_from_the_report_as_described(
    client, make_user, settings, flow
):
    settings.DEBUG = False
    user = make_user("wording-reviewer", "admin")
    user.user_permissions.add(review_permission())
    client.force_login(user)
    path, data = password_request(flow, PASSWORD)
    backend = ("django.contrib.auth.backends", "authenticate")
    with _observe_report_masks({backend: "password"}, PASSWORD) as observed:
        response = client.post(path, data)

    assert response.status_code == 302
    request = response.wsgi_request
    reporter = get_exception_reporter_filter(request)
    field = "password" if flow == "reauthentication" else "confirm_password"
    post_was_masked = reporter.get_post_parameters(request)[field] == reporter.cleansed_substitute
    assert post_was_masked
    assert backend in observed
    from pathlib import Path

    from django.conf import settings as django_settings

    note = Path(django_settings.BASE_DIR, "changelog.d/2026-11-25-audit-review-records.md")
    assert "authentication backend still receives the password" in note.read_text()
    if not observed[backend]:
        raise BackendPasswordReadable(
            "The report filter retained the re-entered password in the backend."
        )
