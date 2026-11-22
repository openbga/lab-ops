"""Verify Discard and ordinary dialog recovery using a synthetic investigation.

Changed on adoption: each case waits for the page's network to be idle before the session is
ended, so the page's own status request (sent after a dialog load, and reused by a check while on
its way) is answered first; on a slow runner it otherwise re-armed a full deadline.
"""

import re
from urllib.parse import urlsplit

import pytest
import test_review_kept_typing_nonconformance as prior
from django.urls import reverse
from helpers import field, sign_in
from playwright.sync_api import expect
from test_review_kept_typing_new_ticket import expire_row, wait_for_sign_in

from accounts.models import KeptDraft, User
from core import modules
from core.models import LabModule
from nonconformance import services
from nonconformance.models import ActionStatus, Status

pytestmark = pytest.mark.django_db(transaction=True)
changed_investigation = prior.changed_investigation
EDIT = "A fresh note typed after the connection failed."


def discard(page):
    return page.locator("#vr-form-content").get_by_role("link", name="Discard", exact=True)


def abort_reload(page, address):
    page.route("**" + address, lambda route: route.abort("failed"), times=1)
    discard(page).click()
    expect(page.locator("#vr-form-error")).to_contain_text("check the connection")
    expect(field(page, "Similar cases")).to_have_value(prior.DRAFT)
    return page.get_by_role("button", name="Try again")


def reassign(record):
    supervisor = User.objects.get(username="demo_supervisor")
    services.set_owner(
        actor=supervisor,
        nc=record,
        expected_revision=record.revision,
        owner=supervisor,
        reason="The supervisor is following up with the clinic.",
    )


def open_on_surface(page, address, detail, surface):
    if surface == "dedicated":
        return
    # A partial GET never takes a copy. Replay the actual restored fragment once so the
    # same Discard can be exercised inside the dialog opened by a link on the record.
    fragment = page.locator("#vr-form-content [data-vr-form-page]").evaluate("el => el.outerHTML")
    page.goto(detail)
    page.route(
        "**" + address,
        lambda route: route.fulfill(content_type="text/html", body=fragment),
        times=1,
    )
    page.get_by_role("link", name="Record investigation").first.click()
    expect(field(page, "Similar cases")).to_have_value(prior.DRAFT)


def switch_off():
    switch = LabModule.objects.get(key="nonconformance")
    modules.set_enabled(
        actor=User.objects.get(username="demo_admin"),
        key=switch.key,
        enabled=False,
        expected_revision=switch.revision,
        reason="Scheduled review.",
    )


def close_investigation(record):
    supervisor = User.objects.get(username="demo_supervisor")
    for action in record.actions.filter(status=ActionStatus.OPEN):
        services.finish_action(
            actor=supervisor,
            nc=record,
            expected_revision=record.revision,
            action=action,
            done=True,
            note="Courier confirmed.",
        )
        record.refresh_from_db()
    record = services.change_status(
        actor=supervisor, nc=record, expected_revision=record.revision, status=Status.VERIFICATION
    )
    record = services.record_effectiveness(
        actor=supervisor,
        nc=record,
        expected_revision=record.revision,
        result="effective",
        note="The collection arrived on time.",
    )
    services.close(
        actor=supervisor, nc=record, expected_revision=record.revision, note="Completed."
    )


@pytest.mark.parametrize("surface", ["dedicated", "record"])
@pytest.mark.parametrize("answer", ["form", "forbidden", "missing", "redirect"])
def test_discard_opens_the_current_answer_on_each_surface(
    page, changed_investigation, surface, answer
):
    address, record = changed_investigation
    detail = reverse("nonconformance:detail", args=[record.pk])
    open_on_surface(page, address, detail, surface)
    if answer == "forbidden":
        reassign(record)
    elif answer == "missing":
        switch_off()
    elif answer == "redirect":
        close_investigation(record)

    if answer in {"forbidden", "missing"}:
        status = 403 if answer == "forbidden" else 404
        with page.expect_response(
            lambda r: (
                r.request.is_navigation_request()
                and urlsplit(r.url).path == address
                and r.status == status
            )
        ):
            discard(page).click()
        expect(field(page, "Similar cases")).to_have_count(0)
    elif answer == "redirect":
        discard(page).click()
        expect(page).to_have_url(re.compile(re.escape(detail) + "$"))
        expect(page.get_by_text(services.CLOSED, exact=True).first).to_be_visible()
    else:
        discard(page).click()
        expect(field(page, "Similar cases")).to_have_value(prior.SAVED)
        expect(field(page, "Containment")).to_be_focused()
        assert urlsplit(page.url).path == (address if surface == "dedicated" else detail)
    expect(discard(page)).to_have_count(0)


@pytest.mark.parametrize("surface", ["dedicated", "record"])
@pytest.mark.parametrize("answer", ["form", "forbidden"])
def test_try_again_finishes_the_failed_discard(page, changed_investigation, surface, answer):
    address, record = changed_investigation
    detail = reverse("nonconformance:detail", args=[record.pk])
    open_on_surface(page, address, detail, surface)
    retry = abort_reload(page, address)
    if answer == "forbidden":
        reassign(record)
    with page.expect_response(
        lambda r: (
            urlsplit(r.url).path == address and r.status == (403 if answer == "forbidden" else 200)
        )
    ):
        retry.click()
    expect(discard(page)).to_have_count(0)
    if answer == "form":
        expect(field(page, "Similar cases")).to_have_value(prior.SAVED)
    else:
        expect(field(page, "Similar cases")).to_have_count(0)


@pytest.mark.parametrize("status", [503, 404], ids=["unavailable-response", "module-switched-off"])
def test_unsuccessful_save_after_failed_discard_preserves_new_typing(
    page, changed_investigation, status
):
    address, record = changed_investigation
    abort_reload(page, address)
    field(page, "Similar cases").fill(EDIT)
    if status == 503:
        page.route(
            "**" + address,
            lambda route: route.fulfill(status=503, body="Service unavailable"),
            times=1,
        )
    else:
        switch_off()
    with page.expect_response(lambda r: r.request.method == "POST" and r.status == status):
        page.get_by_role("button", name="Save investigation").click()
    expect(page.locator("#vr-form-error")).to_contain_text("Your input was kept")
    expect(field(page, "Similar cases")).to_have_value(EDIT)
    record.refresh_from_db()
    assert record.similar_cases == prior.SAVED


def test_retry_asks_before_replacing_text_edited_since_the_failure(page, changed_investigation):
    address, _ = changed_investigation
    retry = abort_reload(page, address)
    field(page, "Similar cases").fill(EDIT)
    retry.click()
    question = page.locator("#vr-form-discard")
    expect(question).to_be_visible()
    question.get_by_role("button", name="Keep editing").click()
    expect(field(page, "Similar cases")).to_have_value(EDIT)


@pytest.mark.parametrize("surface", ["dedicated", "record"])
def test_edited_discard_can_be_declined_then_confirmed_after_expiry(
    page, demo, changed_investigation, surface
):
    address, record = changed_investigation
    detail = reverse("nonconformance:detail", args=[record.pk])
    open_on_surface(page, address, detail, surface)
    field(page, "Similar cases").fill(EDIT)
    discard(page).click()
    question = page.locator("#vr-form-discard")
    question.get_by_role("button", name="Keep editing").click()
    expect(field(page, "Similar cases")).to_have_value(EDIT)
    discard(page).click()
    page.wait_for_load_state("networkidle")  # the page's own status request answered first
    expire_row()
    with page.expect_response(lambda r: urlsplit(r.url).path == address and r.status == 401):
        question.get_by_role("button", name="Discard changes").click()
    next_page = wait_for_sign_in(page)
    assert next_page == (address if surface == "dedicated" else detail)
    assert not KeptDraft.objects.filter(taken_at__isnull=True).exists()
    sign_in(page, prior.OWNER, demo.password, visit=False)
    if next_page == detail:
        page.get_by_role("link", name="Record investigation").first.click()
    expect(field(page, "Similar cases")).to_have_value(prior.SAVED)


def test_cancel_after_network_error_returns_to_record(page, changed_investigation):
    address, record = changed_investigation
    abort_reload(page, address)
    field(page, "Similar cases").fill(EDIT)
    page.locator("#vr-form-content").get_by_role("link", name="Cancel").click()
    page.locator("#vr-form-discard").get_by_role("button", name="Discard changes").click()
    expect(page.get_by_role("link", name="Record investigation").first).to_be_visible()
    assert urlsplit(page.url).path == reverse("nonconformance:detail", args=[record.pk])
    page.get_by_role("link", name="Record investigation").first.click()
    expect(field(page, "Similar cases")).to_have_value(prior.SAVED)


def test_ordinary_form_link_failure_can_retry_then_cancel_in_place(page, demo):
    record = prior.Nonconformance.objects.get(title=prior.RECORD)
    detail = reverse("nonconformance:detail", args=[record.pk])
    address = reverse("nonconformance:investigation", args=[record.pk])
    prior.signed_in_at(page, demo, prior.OWNER, detail)
    page.route(
        "**" + address, lambda route: route.fulfill(status=403, body="Permission denied"), times=1
    )
    page.get_by_role("link", name="Record investigation").first.click()
    expect(page.locator("#vr-form-error")).to_contain_text("You do not have permission")
    assert urlsplit(page.url).path == detail
    page.get_by_role("button", name="Try again").click()
    expect(field(page, "Similar cases")).to_have_value(record.similar_cases)
    page.locator("#vr-form-content").get_by_role("link", name="Cancel").click()
    expect(page.locator("#vr-form-dialog")).to_be_hidden()
    assert urlsplit(page.url).path == detail


@pytest.mark.parametrize("drawable", [True, False], ids=["restored-typing", "nothing-restored"])
def test_second_timeout_without_further_typing_on_a_legacy_copy(page, demo, drawable):
    record = prior.Nonconformance.objects.get(title=prior.RECORD)
    address = reverse("nonconformance:investigation", args=[record.pk])
    prior.signed_in_at(page, demo, prior.OWNER, address)
    field(page, "Similar cases").fill(prior.DRAFT)
    page.wait_for_load_state("networkidle")  # the page's own status request answered first
    prior.expire(page, prior.OWNER)
    draft = KeptDraft.objects.get(taken_at__isnull=True)
    draft.revision = None
    if not drawable:
        draft.values = {"retired_dispatch_note": prior.DRAFT}
        draft.controls = {"retired_dispatch_note": "text"}
    draft.save(update_fields=["revision", "values", "controls"])
    sign_in(page, prior.OWNER, demo.password, visit=False)
    notice = page.locator("[data-vr-kept-notice]")
    expect(notice).to_contain_text("kept without the version")
    expect(discard(page)).to_be_visible()
    expect(page.locator('input[name="expected_revision"]')).to_have_value("0")
    if drawable:
        expect(field(page, "Similar cases")).to_have_value(prior.DRAFT)
    else:
        expect(notice).to_contain_text("none of it fits")
        expect(field(page, "Similar cases")).to_have_value(record.similar_cases)

    page.wait_for_load_state("networkidle")  # the page's own status request answered first
    prior.expire(page, prior.OWNER)
    assert wait_for_sign_in(page) == address
    pending = KeptDraft.objects.filter(taken_at__isnull=True)
    if drawable:
        again = pending.get()
        assert again.revision == 0 and again.values["similar_cases"] == prior.DRAFT
    else:
        assert not pending.exists()
    sign_in(page, prior.OWNER, demo.password, visit=False)
    expect(field(page, "Similar cases")).to_have_value(
        prior.DRAFT if drawable else record.similar_cases
    )
    expect(page.locator('input[name="expected_revision"]')).to_have_value(
        "0" if drawable else str(record.revision)
    )
