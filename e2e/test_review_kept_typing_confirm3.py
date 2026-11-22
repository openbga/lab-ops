"""Check that the recovery notice agrees with the actual native date control."""

import re
from datetime import timedelta

import pytest
from django.db.models.functions import Now
from django.urls import reverse
from helpers import field, sign_in
from playwright.sync_api import expect
from test_session_limits import INACTIVITY, PORTAL, open_session, signed_in_at

from accounts.models import KeptDraft

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "typed",
    ["2028-02-29T09:45:12.125", "2028-02-29T09:45", "2028-02-29 09:"],
    ids=["fractional-seconds", "minutes", "unfinished"],
)
def test_native_recovery_warns_only_if_the_browser_cannot_display_the_edit(
    page, browser, browser_context_args, demo, typed
):
    label = "Ready for pickup at"
    signed_in_at(page, demo, PORTAL, reverse("portal_forms:raise", args=["pickup"]))
    field(page, label).fill(typed)
    assert open_session(PORTAL).update(last_active_at=Now() - INACTIVITY - timedelta(seconds=1))
    page.clock.fast_forward(65_000)
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next="), timeout=15_000)
    assert KeptDraft.objects.get().values["field-ready-at"].replace("T", " ") == typed.replace(
        "T", " "
    )
    with browser.new_context(
        **{
            **browser_context_args,
            "storage_state": page.context.storage_state(),
            "java_script_enabled": False,
        }
    ) as resumed:
        returned = resumed.new_page()
        returned.goto(page.url)
        sign_in(returned, PORTAL, demo.password, visit=False)
        notice = returned.locator("[data-vr-kept-notice]")
        expect(notice).to_be_visible()
        missed = notice.locator("[data-vr-kept-missed]")
        control = field(returned, label)
        if typed.endswith(":"):
            expect(control).to_have_value("")
            listed = missed.locator("noscript span")
            expect(listed).to_be_visible()
            assert listed.inner_text() == f"{label}: {typed}"
        else:
            expect(control).to_have_value(typed)
            assert not missed.is_visible(), (control.input_value(), missed.inner_text())
