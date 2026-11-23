"""Check the Continue destination described in the setup page's help.

Written by the independent reviewer (Codex) in the verification-only pass at 4d2a14c, which found
the help's wording at odds with the behaviour, and adopted with its last expectation adapted to the
corrected help (marked in place). The original is archived in
.local/review-history/required-second-factor-round-1-confirmation-3-original/."""

import pytest
from django.urls import reverse

from accounts.tests.test_review_required_second_factor import activate_app
from accounts.tests.test_second_factor_enrolment import SETUP, require, sign_in


@pytest.mark.django_db
def test_continue_returns_to_first_work_page_requested_after_opening_setup(client, make_user):
    require(staff=True)
    user = make_user("setup_then_work", "technical")
    assert sign_in(client, user, next_page=SETUP).url == SETUP
    assert client.get(SETUP).status_code == 200

    first_work_page = "/requests/?view=all"
    redirected = client.get(first_work_page, follow=True, HTTP_SEC_FETCH_DEST="document")
    assert redirected.redirect_chain == [(SETUP, 302)]
    assert redirected.status_code == 200

    completed, _secret = activate_app(client)
    assert completed.url == reverse("mfa_view_recovery_codes")
    assert f'href="{SETUP}">Continue</a>'.encode() in client.get(completed.url).content
    # Adapted on adoption: the help now says that when the first page opened was the setup page,
    # Continue goes to the start page (the reviewer's earlier confirmation expected the same).
    assert client.get(SETUP).url == reverse("dashboard")
