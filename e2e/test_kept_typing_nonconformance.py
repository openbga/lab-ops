"""What a staff member was typing in a nonconformance form in the form dialog, kept across a
sign-out by a session limit and given back on that form's own page at the next sign-in, with the
revision it was typed against (accounts/kept.py, nonconformance.views, static/js/session.js).

As in e2e/test_kept_typing.py, time is moved instead of waited for: the session's row is moved
back on the server and the page's clock forward by as much.
"""

import re
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from django.db.models.functions import Now
from django.utils import timezone
from helpers import field, native_select, pick, sign_in
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

INACTIVITY = timedelta(minutes=30)
STAFF = "demo_client_services"  # owns the seeded record below, open in Corrective action
RECORD = "Results reached one clinic late three times in a month"
SIMILAR = "Checked the other three courier routes this quarter: no change of pickup time."
KEPT = (
    "What you were typing is kept for 24 hours. Sign in again and open the same page to get it "
    "back."
)
PUT_BACK = re.compile(
    r"What you were typing here when you were signed out at .* is back in the form"
)
CHANGED = "This record changed after you typed it, so saving this form is refused."
STALE = "This nonconformance changed after you opened it. Reload it and review the latest details."
# The refused post's answer is a 401 by design (accounts.sessions._refusal); the browser logs every
# such answer.
REFUSED = "Failed to load resource: the server responded with a status of 401 (Unauthorized)"


def open_session(username):
    from accounts.models import AccountSession

    return AccountSession.objects.filter(user__username=username, ended_at__isnull=True)


def record():
    from nonconformance.models import Nonconformance

    return Nonconformance.objects.get(title=RECORD)


def signed_out_by_inactivity(page: Page) -> None:
    """Move the session past its limit and the page's clock with it, then wait for sign in."""
    assert open_session(STAFF).update(last_active_at=Now() - (INACTIVITY - timedelta(seconds=3)))
    page.clock.fast_forward(int((INACTIVITY - timedelta(seconds=3)).total_seconds() * 1000))
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next="), timeout=15_000)
    expect(page.get_by_text(KEPT)).to_be_visible()


def open_in_dialog(page: Page, demo, link: str, title: str):
    page.clock.install()
    sign_in(page, STAFF, demo.password)
    page.goto(f"/quality/nonconformances/{record().pk}/")
    page.get_by_role("link", name=link).first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#vr-form-title")).to_have_text(title)
    return dialog


def test_an_investigation_typed_in_the_dialog_comes_back_on_its_page(page: Page, demo, view):
    from accounts.models import KeptDraft

    nc = record()
    dialog = open_in_dialog(page, demo, "Record investigation", "Record investigation")
    field(dialog, "Similar cases").fill(SIMILAR)
    signed_out_by_inactivity(page)
    address = f"/quality/nonconformances/{nc.pk}/investigation/"
    assert parse_qs(urlsplit(page.url).query)["next"] == [address]
    copy = KeptDraft.objects.get()
    assert copy.values["similar_cases"] == SIMILAR and copy.revision == nc.revision

    sign_in(page, STAFF, demo.password, visit=False)
    expect(page).to_have_url(re.compile(re.escape(address) + "$"))
    notice = page.locator("[data-vr-kept-notice]")
    expect(notice).to_contain_text(PUT_BACK)
    expect(notice).not_to_contain_text(CHANGED)
    expect(field(page, "Similar cases")).to_have_value(SIMILAR)
    expect(field(page, "Root cause")).to_have_value(re.compile("courier pickup"))
    assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
    discard = notice.get_by_role("link", name="Discard")
    box = discard.bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= page.viewport_size["width"]
    if view.phone:
        assert box["height"] >= 44, box

    page.get_by_role("button", name="Save investigation").click()
    expect(page.get_by_text("Investigation saved.")).to_be_visible()
    assert record().similar_cases == SIMILAR
    assert not page.console_errors, page.console_errors


def test_a_record_changed_meanwhile_gives_the_typing_back_and_refuses_the_save(page: Page, demo):
    from accounts.models import User
    from nonconformance import services
    from nonconformance.models import Status

    nc = record()
    dialog = open_in_dialog(page, demo, "Record investigation", "Record investigation")
    field(dialog, "Similar cases").fill(SIMILAR)
    signed_out_by_inactivity(page)
    # A supervisor moves the record on while the person is signed out.
    services.change_status(
        actor=User.objects.get(username="demo_supervisor"),
        nc=nc,
        expected_revision=nc.revision,
        status=Status.INVESTIGATING,
    )

    sign_in(page, STAFF, demo.password, visit=False)
    notice = page.locator("[data-vr-kept-notice]")
    expect(notice).to_contain_text(CHANGED)
    expect(field(page, "Similar cases")).to_have_value(SIMILAR)
    page.get_by_role("button", name="Save investigation").click()
    expect(page.get_by_text(STALE).first).to_be_visible()
    expect(field(page, "Similar cases")).to_have_value(SIMILAR)  # still there to copy
    assert record().similar_cases != SIMILAR

    # The refused form carries no notice; opening the form again shows the record as it stands.
    page.goto(f"/quality/nonconformances/{nc.pk}/investigation/")
    expect(page.locator("[data-vr-kept-notice]")).to_have_count(0)
    expect(field(page, "Similar cases")).not_to_have_value(SIMILAR)
    assert not page.console_errors, page.console_errors


def test_discard_after_the_form_left_the_record_opens_the_record(page: Page, demo):
    """The action was finished while the person was signed out: Edit action is no longer offered.
    The typing is given back with the earlier revision; Discard then opens the address as a page,
    which sends the person to the record with the reason, and the typing leaves the screen."""
    from accounts.models import User
    from nonconformance import services
    from nonconformance.models import ActionStatus, CorrectiveAction

    nc = record()
    action = CorrectiveAction.objects.get(nonconformance=nc, status=ActionStatus.OPEN)
    page.clock.install()
    sign_in(page, STAFF, demo.password)
    address = f"/quality/nonconformances/{nc.pk}/actions/{action.pk}/edit/"
    page.goto(address)
    field(page, "Action").fill("Agree the pickup time in writing and by phone.")
    field(page, "Reason").fill("Phone confirmation added.")
    signed_out_by_inactivity(page)
    services.finish_action(
        actor=User.objects.get(username="demo_supervisor"),
        nc=nc,
        expected_revision=nc.revision,
        action=action,
        done=True,
        note="Agreed with the courier.",
    )

    sign_in(page, STAFF, demo.password, visit=False)
    notice = page.locator("[data-vr-kept-notice]")
    expect(notice).to_contain_text(CHANGED)
    expect(field(page, "Reason")).to_have_value("Phone confirmation added.")
    notice.get_by_role("link", name="Discard").click()
    expect(page).to_have_url(re.compile(rf"/quality/nonconformances/{nc.pk}/$"))
    expect(page.get_by_text("This action is already finished.")).to_be_visible()
    expect(page.get_by_text("Phone confirmation added.")).to_have_count(0)
    assert not page.console_errors, page.console_errors


def test_an_action_added_after_the_limit_passed_comes_back_with_its_person(page: Page, demo):
    from accounts.models import KeptDraft, User
    from nonconformance.models import CorrectiveAction

    nc = record()
    dialog = open_in_dialog(page, demo, "Add action", "Add action")
    field(dialog, "Action").fill("Confirm the courier pickup time each month.")
    pick(field(dialog, "Person"), label="Demo Supervisor (demo_supervisor)")
    chosen = str(User.objects.get(username="demo_supervisor").pk)
    expect(native_select(field(dialog, "Person"))).to_have_value(chosen)
    due = (timezone.localdate() + timedelta(days=14)).strftime("%Y-%m-%d")
    field(dialog, "Due date").fill(due)
    actions_before = CorrectiveAction.objects.filter(nonconformance=nc).count()
    # The server's limit passes while the page still believes it has time (a laptop asleep).
    assert open_session(STAFF).update(last_active_at=Now() - INACTIVITY - timedelta(seconds=5))
    dialog.get_by_role("button", name="Add action").click()
    expect(page).to_have_url(re.compile(r"/accounts/login/\?next="))
    expect(page.get_by_text(KEPT)).to_be_visible()
    assert CorrectiveAction.objects.filter(nonconformance=nc).count() == actions_before
    assert KeptDraft.objects.get().values["assignee"] == chosen

    sign_in(page, STAFF, demo.password, visit=False)
    expect(page).to_have_url(re.compile(rf"/quality/nonconformances/{nc.pk}/actions/new/$"))
    expect(field(page, "Action")).to_have_value("Confirm the courier pickup time each month.")
    expect(native_select(field(page, "Person"))).to_have_value(chosen)
    page.get_by_role("button", name="Add action").click()
    expect(page.get_by_text("Action added.")).to_be_visible()
    assert CorrectiveAction.objects.filter(nonconformance=nc).count() == actions_before + 1
    assert [error for error in page.console_errors if error != REFUSED] == []
