"""Confirmation journeys for Discard retries and the record-page sign-in destination."""

from urllib.parse import urlsplit

import pytest
import test_review_kept_typing_nonconformance as prior
from django.urls import reverse
from helpers import field, sign_in
from playwright.sync_api import expect
from test_review_kept_typing_new_ticket import expire_row, wait_for_sign_in
from test_review_kept_typing_nonconformance_verify import discard

from accounts.models import KeptDraft
from nonconformance.models import Nonconformance

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def returned_status(page, demo):
    record = Nonconformance.objects.get(title=prior.RECORD)
    address = reverse("nonconformance:status", args=[record.pk])
    sign_in(page, prior.OWNER, demo.password)
    page.goto(address)
    page.locator("#vr-form-content").get_by_role("radio").first.check()
    field(page, "Reason (optional)").fill(prior.DRAFT)
    expire_row()
    with page.expect_response(lambda response: response.status == 401):
        page.get_by_role("button", name="Change status").click()
    assert wait_for_sign_in(page) == address
    sign_in(page, prior.OWNER, demo.password, visit=False)
    expect(field(page, "Reason (optional)")).to_have_value(prior.DRAFT)
    return address


@pytest.mark.parametrize("failures", [0, 1, 2], ids=["direct", "retry", "edited-retry"])
def test_discard_reaches_the_first_text_field_after_retry(page, returned_status, failures):
    address = returned_status
    trigger = discard(page)
    reason = field(page, "Reason (optional)")
    for _ in range(failures):
        page.route("**" + address, lambda route: route.abort("failed"), times=1)
        trigger.click()
        expect(page.locator("#vr-form-error")).to_contain_text("check the connection")
        expect(reason).to_have_value(prior.DRAFT)
        trigger = page.get_by_role("button", name="Try again")
    if failures == 2:
        reason.fill("Checked after reconnecting.")
        trigger.click()
        page.get_by_role("button", name="Keep editing").click()
        expect(reason).to_have_value("Checked after reconnecting.")
        expect(trigger).to_be_focused()
        trigger.click()
        trigger = page.get_by_role("button", name="Discard changes")
    trigger.click()
    expect(discard(page)).to_have_count(0)
    expect(reason).to_have_value("")
    expect(reason).to_be_enabled()
    focused = page.evaluate("() => [document.activeElement.name, document.activeElement.type]")
    expect(reason, f"Focus after loading the current form: {focused}").to_be_focused()


def test_timed_out_discard_retries_through_session_expiry(page, returned_status):
    address = returned_status
    requests = []
    page.locator("#vr-form-dialog").evaluate(
        """el => {
          el.setAttribute("hx-request", '{"timeout":350}');
          document.addEventListener("htmx:timeout", () => { window.sawDialogTimeout = true; });
        }"""
    )
    page.route("**" + address, lambda route: requests.append(route), times=1)
    discard(page).click()
    expect(page.locator("#vr-form-error")).to_contain_text("check the connection")
    assert requests and page.evaluate("window.sawDialogTimeout")
    # The retry uses the live application's ordinary request timeout and real 401.
    page.locator("#vr-form-dialog").evaluate("el => el.removeAttribute('hx-request')")
    reason = field(page, "Reason (optional)")
    reason.fill("A revision considered after the timeout.")
    page.get_by_role("button", name="Try again").click()
    expire_row()
    with page.expect_response(
        lambda response: response.status == 401 and urlsplit(response.url).path == address
    ):
        page.get_by_role("button", name="Discard changes").click()
    assert wait_for_sign_in(page) == address
    assert not KeptDraft.objects.filter(taken_at__isnull=True).exists()


@pytest.mark.parametrize(
    "interrupted_discard", [False, True], ids=["save-only", "discard-then-save"]
)
def test_recovery_from_a_failed_save_never_replays_an_earlier_discard(
    page, returned_status, interrupted_discard
):
    address = returned_status
    if interrupted_discard:
        page.route("**" + address, lambda route: route.abort("failed"), times=1)
        discard(page).click()
        expect(page.locator("#vr-form-error")).to_contain_text("check the connection")
    # The original status choice and reason are valid. Save explicitly asks to keep them.
    page.route("**" + address, lambda route: route.fulfill(status=503, body="Unavailable"), times=1)
    save = page.get_by_role("button", name="Change status", exact=True)
    with page.expect_response(
        lambda response: response.status == 503 and response.request.method == "POST"
    ):
        save.click()
    reason = field(page, "Reason (optional)")
    expect(reason).to_have_value(prior.DRAFT)
    expect(page.locator("#vr-form-error")).to_contain_text("Your input was kept")
    expect(save).to_be_enabled()
    again = page.get_by_role("button", name="Try again")
    action = again if again.is_visible() else save
    with page.expect_request(lambda request: urlsplit(request.url).path == address) as attempt:
        action.click()
    if attempt.value.method == "GET":
        expect(discard(page)).to_have_count(0)
        expect(reason).to_have_value("")
    assert attempt.value.method == "POST", (
        "Try again reloaded the current form and erased the reason after Save failed."
    )
    expect(reason).to_be_hidden()
    expect(page.get_by_text("Status changed.", exact=True).first).to_be_visible()


# Observe when the kept-typing script first records the form's initial values, relative to a
# trusted input event. The controlled case postpones only that callback until after the edit.
# The ordinary case observes the callback at its original delay under the fixture's clock.
OBSERVE = """([hold, draft]) => {
    const waiting = [];
    const order = [];
    // Persist only event order and flags across the sign-in navigation.
    function recordOrder() {
        sessionStorage.setItem('confirmation-event-order', JSON.stringify(order));
    }
    window.addEventListener('pagehide', recordOrder);
    window.addEventListener('beforeunload', recordOrder);
    document.addEventListener('htmx:beforeSend', event => {
        if (event.detail.target.id === 'vr-form-content') order.length = 0;
    });
    const inspect = (event, trusted) => {
        const field = document.querySelector('#vr-form-content textarea[name="similar_cases"]');
        if (!field) return;
        order.push({
            event, trusted, edited: field.value === draft,
            disabled: field.disabled, busy: document.querySelector('#vr-form-content').inert,
        });
    };
    const schedule = window.setTimeout.bind(window);
    window.setTimeout = function(callback, delay, ...args) {
        if (typeof callback !== 'function' || callback.name !== 'baseline')
            return schedule(callback, delay, ...args);
        const run = () => {
            inspect('snapshot');
            callback(...args);
        };
        return schedule(() => hold ? waiting.push(run) : run(), delay);
    };
    window.releaseSnapshots = () => {
        hold = false;
        waiting.splice(0).forEach(run => run());
    };
    document.addEventListener('vr:form-ready', () => inspect('ready'));
    document.addEventListener('input', event => {
        if (event.target.matches('textarea[name="similar_cases"]')) {
            inspect('input', event.isTrusted);
        }
    });
}"""


@pytest.mark.parametrize("schedule", ["fixture", "wall-clock", "edit-first", "snapshot-first"])
def test_first_edit_in_an_enabled_dialog_survives_signout(page, demo, schedule):
    record = Nonconformance.objects.get(title=prior.RECORD)
    detail = reverse("nonconformance:detail", args=[record.pk])
    address = reverse("nonconformance:investigation", args=[record.pk])
    if schedule == "fixture":
        prior.signed_in_at(page, demo, prior.OWNER, detail)
    else:
        # The controlled order uses the browser's real clock, including throughout sign-out.
        sign_in(page, prior.OWNER, demo.password)
        page.goto(detail)
    keep_posts = []

    def on_request(request):
        if request.method == "POST" and urlsplit(request.url).path == reverse(
            "account-session-keep"
        ):
            keep_posts.append(request.post_data_json["forms"][0]["values"].get("similar_cases"))

    page.on("request", on_request)
    page.evaluate(OBSERVE, [schedule == "edit-first", prior.DRAFT])
    dialog = page.locator("#vr-form-dialog")
    note = field(dialog, "Similar cases")
    next_paint = (
        "() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))"
    )
    # Sample fresh openings for the intermittent case, with ordinary browser timing. Once a
    # single trusted edit goes unrecognized, let the session actually expire on that opening.
    openings = 15 if schedule == "wall-clock" else 1
    for attempt in range(1, openings + 1):
        page.get_by_role("link", name="Record investigation").first.click()
        if schedule == "snapshot-first":
            expect(note).to_be_enabled()
            page.evaluate(next_paint)
        note.fill(prior.DRAFT)
        if schedule == "edit-first":
            page.evaluate("window.releaseSnapshots()")
        if attempt == openings:
            break
        page.evaluate(next_paint)
        pending = page.evaluate("window.vrKeptTyping.collect().length")
        if not pending:
            break
        dialog.get_by_role("link", name="Cancel").click()
        page.get_by_role("button", name="Discard changes").click()
        expect(dialog).to_be_hidden()
    if schedule == "fixture":
        prior.expire(page, prior.OWNER)
    else:
        expire_row()
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    destination = wait_for_sign_in(page)
    draft = list(
        KeptDraft.objects.filter(taken_at__isnull=True).values_list(
            "values__similar_cases", flat=True
        )
    )
    events = page.evaluate("""() => {
        const order = sessionStorage.getItem('confirmation-event-order');
        sessionStorage.removeItem('confirmation-event-order');
        return JSON.parse(order || '[]');
    }""")
    assert any(
        e["event"] == "input" and e["trusted"] and not e["disabled"] and not e["busy"]
        for e in events
    ), events
    order = " / ".join(f"{e['event']}({'edited' if e['edited'] else 'fresh'})" for e in events)
    assert (destination, keep_posts, draft) == (address, [prior.DRAFT], [prior.DRAFT]), (
        f"Opening {attempt} ({schedule}): {order}"
    )


def test_queued_keystroke_during_first_focus_is_kept_with_normal_browser_timers(page, demo):
    """Queue a real keystroke during a slow focus handler. All app timers run normally."""
    record = Nonconformance.objects.get(title=prior.RECORD)
    detail = reverse("nonconformance:detail", args=[record.pk])
    address = reverse("nonconformance:investigation", args=[record.pk])
    sign_in(page, prior.OWNER, demo.password)
    page.goto(detail)
    page.evaluate("""() => {
        function firstFocus(event) {
            if (!event.target.matches('#vr-form-content textarea[name="containment"]')) return;
            document.removeEventListener('focusin', firstFocus);
            console.info('confirmation-first-focus');
            // Let the native input queue while the form has focus and its setup task finishes.
            const until = performance.now() + 300;
            while (performance.now() < until) {}
        }
        document.addEventListener('focusin', firstFocus);
    }""")
    with page.expect_console_message(lambda message: message.text == "confirmation-first-focus"):
        page.get_by_role("link", name="Record investigation").first.click()
    page.keyboard.insert_text("Dispatch checked. ")
    note = field(page, "Containment")
    expect(note).to_be_focused()
    edited = note.input_value()
    assert "Dispatch checked." in edited and edited != record.containment
    expire_row()
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    destination = wait_for_sign_in(page)
    saved = list(
        KeptDraft.objects.filter(taken_at__isnull=True).values_list(
            "values__containment", flat=True
        )
    )
    assert (destination, saved) == (address, [edited])
