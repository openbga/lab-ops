"""Verify where a retried Discard puts focus when the operator stays in the status form.

Adopted from the independent review's third confirmation. Three cases are strict xfails for a
documented limitation that predates this batch (docs/architecture.md, Sessions): after a Discard
load failed, a Close or a save that is then abandoned drops the Discard focus rule, so Try again's
form takes the dialog's own focus. The typing, the request and the form drawn are as intended."""

from urllib.parse import urlsplit

import pytest
from helpers import field
from playwright.sync_api import expect
from test_review_kept_typing_nonconformance_confirm import returned_status as returned_status
from test_review_kept_typing_nonconformance_confirm2 import interrupt_discard
from test_review_kept_typing_nonconformance_verify import discard

pytestmark = pytest.mark.django_db(transaction=True)


LIMITATION = pytest.mark.xfail(
    strict=True,
    reason="Documented limitation (predates): an abandoned Close or save after a failed Discard "
    "drops the Discard focus rule; Try again's form takes the dialog's own focus.",
)


@pytest.mark.parametrize(
    "action",
    [
        "escape",
        pytest.param("close-button", marks=LIMITATION),
        pytest.param("save-capture", marks=LIMITATION),
        pytest.param("save-bubble", marks=LIMITATION),
    ],
)
def test_discard_retry_focus_after_an_abandoned_close_or_save(page, returned_status, action):
    again = interrupt_discard(page, returned_status)
    reason = field(page, "Reason (optional)")
    reason.fill("The clinic confirmed the collection time.")
    requests = []

    def record_request(request):
        if urlsplit(request.url).path == returned_status:
            requests.append(request.method)

    page.on("request", record_request)
    if action.startswith("save-"):
        # Exercise the same two cancellation points as the adopted Try again regression.
        page.evaluate(
            """capture => {
            function cancel(event) {
                if (event.detail.requestConfig.verb.toLowerCase() !== "post") return;
                event.preventDefault();
                event.stopPropagation();
                window.removeEventListener("htmx:beforeRequest", cancel, capture);
                window.confirm3Cancelled = true;
            }
            window.addEventListener("htmx:beforeRequest", cancel, capture);
        }""",
            action == "save-capture",
        )
        page.get_by_role("button", name="Change status", exact=True).click()
        assert page.evaluate("window.confirm3Cancelled")
    else:
        if action == "escape":
            reason.press("Escape")
        else:
            page.get_by_role("button", name="Close form", exact=True).click()
        page.get_by_role("button", name="Keep editing", exact=True).click()

    expect(reason).to_have_value("The clinic confirmed the collection time.")
    expect(again).to_be_visible()
    assert requests == []
    again.click()
    with page.expect_response(
        lambda r: r.request.method == "GET" and urlsplit(r.url).path == returned_status and r.ok
    ):
        page.get_by_role("button", name="Discard changes", exact=True).click()
    expect(discard(page)).to_have_count(0)
    expect(reason).to_have_value("")
    expect(reason).to_be_enabled()
    assert requests == ["GET"]
    focused = page.evaluate("() => [document.activeElement.name, document.activeElement.type]")
    expect(reason, f"Focus after the confirmed Discard GET: {focused}").to_be_focused()
