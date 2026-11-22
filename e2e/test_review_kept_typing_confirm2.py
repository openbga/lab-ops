"""Check the value a client can actually read after a date draft has been taken."""

import re
from datetime import timedelta

import pytest
from django.db.models.functions import Now
from django.urls import reverse
from helpers import field, sign_in
from playwright.sync_api import expect
from test_session_limits import INACTIVITY, PORTAL, open_session, signed_in_at

from accounts.models import KeptDraft, User
from request_forms.models import Audience, FieldKind, RequestTemplate
from request_forms.services import create_field, update_field

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "kind,text,scripts,target",
    [
        (FieldKind.DATE, "2028-02-29", False, "same"),
        (FieldKind.DATETIME, "2028-02-29 09:45", False, "same"),
        (FieldKind.DATETIME, "2028-02-29 09:", False, "same"),
        (FieldKind.DATE, "2026-02-30", True, "same"),
        (FieldKind.DATETIME, "2026-02-30 09:45", True, "same"),
        (FieldKind.DATETIME, "2028-02-29 09:", True, "text"),
        (FieldKind.DATE, "2028-02-29", False, "text"),
    ],
    ids=[
        "native-date",
        "native-datetime",
        "native-partial",
        "impossible-date",
        "impossible-datetime",
        "partial-to-text",
        "date-to-native-text",
    ],
)
def test_reopened_date_answer_is_available_in_a_control_or_the_recovery_notice(
    page, browser, browser_context_args, demo, kind, text, scripts, target
):
    template = RequestTemplate.objects.get(key="pickup")
    actor = User.objects.get(username="demo_admin")
    entry = create_field(
        actor=actor,
        template_id=template.pk,
        key="candidate-date",
        label="Collection checkpoint",
        kind=kind,
        audience=Audience.CLIENT,
    )
    url = reverse("portal_forms:raise", args=[template.key])
    signed_in_at(page, demo, PORTAL, url)
    editor = field(page, "Collection checkpoint")
    editor.fill(text)
    expect(editor).to_be_focused()
    assert (
        open_session(PORTAL).update(last_active_at=Now() - INACTIVITY - timedelta(seconds=1)) == 1
    )
    page.clock.fast_forward(65_000)
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next="), timeout=15_000)
    assert KeptDraft.objects.get().values["field-candidate-date"].replace("T", " ") == text
    if target == "text":
        update_field(
            actor=actor,
            field_id=entry.pk,
            expected_revision=entry.revision,
            kind=FieldKind.SHORT_TEXT,
            reason="Accept a written collection time.",
        )
    # Reopen with the original browser's cookies. Only script availability changes.
    with browser.new_context(
        **{
            **browser_context_args,
            "storage_state": page.context.storage_state(),
            "java_script_enabled": scripts,
        }
    ) as resumed:
        reopened = resumed.new_page()
        reopened.goto(page.url)
        sign_in(reopened, PORTAL, demo.password, visit=False)
        notice = reopened.locator("[data-vr-kept-notice]")
        expect(notice).to_be_visible()
        value = field(reopened, "Collection checkpoint").input_value()
        assert value.replace("T", " ") == text or text in notice.inner_text(), (
            f"The reopened control shows {value!r}. The notice says {notice.inner_text()!r}."
        )
        assert KeptDraft.objects.get().taken_at is not None
