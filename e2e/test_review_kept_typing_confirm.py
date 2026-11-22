"""Date editor recovery through the normal session poll and sign-in page."""

import re
from datetime import timedelta

import pytest
from django.db.models.functions import Now
from django.urls import reverse
from helpers import field, sign_in
from playwright.sync_api import expect
from test_session_limits import INACTIVITY, PORTAL, open_session, signed_in_at

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("typed", ["2026-12-03 09:", "2026-12-03 09:45"])
def test_unfinished_and_complete_date_edits_come_back_as_shown(page, demo, typed):
    from accounts.models import KeptDraft

    signed_in_at(page, demo, PORTAL, reverse("portal_forms:raise", args=["pickup"]))
    editor = field(page, "Ready for pickup at")
    editor.fill(typed)
    expect(editor).to_be_focused()
    assert (
        open_session(PORTAL).update(last_active_at=Now() - INACTIVITY - timedelta(seconds=1)) == 1
    )
    page.clock.fast_forward(65_000)
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next="), timeout=15_000)
    saved = KeptDraft.objects.get().values["field-ready-at"]
    assert saved.replace("T", " ") == typed
    sign_in(page, PORTAL, demo.password, visit=False)
    expect(page.locator("[data-vr-kept-notice]")).to_be_visible()
    expect(editor).to_have_value(typed)
