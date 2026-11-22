"""Second confirmation of the dialog's Discard focus and failed-load retry."""

from urllib.parse import urlsplit

import pytest
import test_review_kept_typing_nonconformance as first_round
from django.urls import reverse
from helpers import field, sign_in
from playwright.sync_api import expect
from test_review_kept_typing_new_ticket import expire_row, wait_for_sign_in
from test_review_kept_typing_nonconformance_confirm import returned_status as returned_status
from test_review_kept_typing_nonconformance_verify import discard

from accounts.models import KeptDraft
from nonconformance.models import Nonconformance

pytestmark = pytest.mark.django_db(transaction=True)


def interrupt_discard(page, address):
    page.route("**" + address, lambda route: route.abort("failed"), times=1)
    discard(page).click()
    expect(page.locator("#vr-form-error")).to_contain_text("check the connection")
    return page.get_by_role("button", name="Try again")


@pytest.mark.parametrize("interaction", ["read-reason", "edit-reason", "maximize"])
def test_discard_retry_still_focuses_reason_after_using_the_open_dialog(
    page, returned_status, interaction
):
    again = interrupt_discard(page, returned_status)
    reason = field(page, "Reason (optional)")
    if interaction == "maximize":
        page.get_by_role("button", name="Maximize").click()
    else:
        reason.click()
    if interaction == "edit-reason":
        original = reason.input_value()
        reason.press("End")
        reason.press_sequentially(" Checked.")
        expect(reason).to_have_value(original + " Checked.")
        again.click()
        again = page.get_by_role("button", name="Discard changes")

    with page.expect_response(
        lambda response: (
            response.request.method == "GET"
            and urlsplit(response.url).path == returned_status
            and response.status == 200
        )
    ):
        again.click()
    expect(discard(page)).to_have_count(0)
    expect(reason).to_be_enabled()
    expect(reason).to_have_value("")
    focused = page.evaluate("() => [document.activeElement.name, document.activeElement.type]")
    expect(reason, f"Focus after the Discard GET: {focused}").to_be_focused()


@pytest.mark.parametrize("phase", ["capture", "bubble"])
def test_visible_try_again_reloads_after_a_save_cancelled_before_send(page, returned_status, phase):
    again = interrupt_discard(page, returned_status)
    reason = field(page, "Reason (optional)")
    original = reason.input_value()
    requests = []

    def track(request):
        if urlsplit(request.url).path == returned_status:
            requests.append(request.method)

    page.on("request", track)
    # Model the two supported veto points: a capture guard like the ticket composer,
    # and a late page check like the existing New ticket cancellation regression.
    page.evaluate(
        """phase => {
        function veto(event) {
            if (event.detail.requestConfig.verb.toLowerCase() !== "post") return;
            event.preventDefault();
            event.stopPropagation();
            window.removeEventListener("htmx:beforeRequest", veto, phase === "capture");
            window.confirmationVetoedSave = true;
        }
        window.addEventListener("htmx:beforeRequest", veto, phase === "capture");
    }""",
        phase,
    )
    page.get_by_role("button", name="Change status", exact=True).click()
    assert page.evaluate("window.confirmationVetoedSave") and requests == []
    expect(reason).to_have_value(original)
    expect(reason).to_be_enabled()
    expect(again).to_be_visible()
    again.click()
    expect(discard(page), "Try again should finish the interrupted Discard GET.").to_have_count(0)
    assert requests == ["GET"]
    expect(reason).to_have_value("")


@pytest.mark.parametrize("leave_edited", [False, True], ids=["reverted", "changed"])
def test_signout_compares_the_dialog_with_its_original_text_after_a_failed_save(
    page, demo, leave_edited
):
    record = Nonconformance.objects.get(title=first_round.RECORD)
    detail = reverse("nonconformance:detail", args=[record.pk])
    address = reverse("nonconformance:investigation", args=[record.pk])
    sign_in(page, first_round.OWNER, demo.password)
    page.goto(detail)
    page.get_by_role("link", name="Record investigation").first.click()
    note = field(page.locator("#vr-form-content"), "Similar cases")
    expect(note).to_be_enabled()
    original = note.input_value()
    assert original != first_round.DRAFT
    note.fill(first_round.DRAFT)
    page.route("**" + address, lambda route: route.fulfill(status=503, body="Unavailable"), times=1)
    with page.expect_response(lambda r: r.status == 503 and r.request.method == "POST"):
        page.get_by_role("button", name="Save investigation", exact=True).click()
    expect(page.locator("#vr-form-error")).to_contain_text("Your input was kept")
    expect(note).to_be_enabled()
    if not leave_edited:
        note.fill(original)
    expire_row()
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    destination = wait_for_sign_in(page)
    saved = list(
        KeptDraft.objects.filter(taken_at__isnull=True).values_list(
            "values__similar_cases", flat=True
        )
    )
    if leave_edited:
        assert (destination, saved) == (address, [first_round.DRAFT])
    else:
        assert (destination, saved) == (detail, [])
