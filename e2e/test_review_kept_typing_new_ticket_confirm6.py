"""Verification of htmx request timing and New ticket recovery with synthetic Chromium forms."""

from threading import Event
from urllib.parse import urlsplit

import pytest
import test_review_kept_typing_new_ticket as r
from helpers import field, pick, sign_in
from playwright.sync_api import expect

from accounts import sessions
from accounts.models import KeptDraft

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("verb", ["GET", "POST"])
def test_collection_before_sending_during_sending_and_after_completion(page, demo, verb):
    r.open_ticket(page, demo, "list")
    subject = field(page, "Subject")
    subject.fill(r.SUBJECT)
    form = page.locator("#vr-form-content form").first
    page.route("**/requests/new/?six=phases*", lambda route: route.fulfill(status=204))
    form.evaluate(
        """async (form, verb) => {
        const state = () => [form.elements.subject.disabled,
            window.vrKeptTyping.collect()[0]?.values.subject ?? null];
        const samples = [];
        for (const phase of ["htmx:beforeRequest", "htmx:beforeSend"]) {
            document.addEventListener(phase, () => samples.push(state()), {once:true});
        }
        await htmx.ajax(verb, form.action + "?six=phases", {source:form,target:"#vr-form-content"});
        samples.push(state());
        window.sixPhases = samples;
    }""",
        verb,
    )
    expect(subject).to_be_enabled()
    assert page.evaluate("window.sixPhases") == [
        [False, r.SUBJECT],
        [True, None if verb == "GET" else r.SUBJECT],
        [False, r.SUBJECT],
    ]


def test_sent_save_preserves_a_control_temporarily_disabled_by_htmx(page, demo):
    r.open_ticket(page, demo, "list")
    subject = field(page, "Subject")
    subject.fill("RoundSixPickup")
    field(page, "What the client asked").fill("Use the reception desk.")
    form = page.locator("#vr-form-content form").first
    page.route("**/requests/new/?six=disable", lambda route: route.fulfill(status=204))
    with page.expect_request("**/requests/new/?six=disable") as sent:
        during = form.evaluate("""async form => {
            form.setAttribute("hx-disabled-elt", "find [name=subject]");
            let kept;
            document.addEventListener("htmx:beforeSend", () => {
                kept = window.vrKeptTyping.collect({commit:true})[0]?.values.subject ?? null;
            }, {once:true});
            await htmx.ajax("POST", form.action + "?six=disable",
                {source:form,target:"#vr-form-content"});
            return kept;
        }""")
    assert b"RoundSixPickup" in sent.value.post_data_buffer
    assert (during, subject.is_enabled()) == ("RoundSixPickup", True)


def test_session_keep_winning_during_an_htmx_disabled_field_save_restores_its_subject(
    page, demo, monkeypatch
):
    r.open_ticket(page, demo, "list")
    pick(field(page, "Category"), label="Pickup or missed pickup")
    expect(page.locator("#category-section")).to_contain_text("Ticket form")
    subject = field(page, "Subject")
    subject.fill(r.SUBJECT)
    body = "Use the collection desk at the east entrance."
    field(page, "What the client asked").fill(body)
    form = page.locator("#vr-form-content form").first
    form.evaluate('form => form.setAttribute("hx-disabled-elt", "find [name=subject]")')
    entered, release = Event(), Event()
    full = []
    refuse = sessions._refusal

    def hold_submission(request):
        if request.method == "POST" and request.path == r.PAGE:
            full.append(request.POST.get("subject"))
            entered.set()
            release.wait(timeout=12)
        return refuse(request)

    monkeypatch.setattr(sessions, "_refusal", hold_submission)
    r.expire_row()
    try:
        page.get_by_role("button", name="Log ticket").click()
        assert entered.wait(5) and full == [r.SUBJECT]
        with page.expect_response(lambda resp: urlsplit(resp.url).path == "/account/session/keep/"):
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        saved = KeptDraft.objects.get().values
    finally:
        release.set()
    assert r.wait_for_sign_in(page) == r.PAGE
    sign_in(page, r.STAFF, demo.password, visit=False)
    expect(field(page, "What the client asked")).to_have_value(body)
    assert (saved.get("subject"), field(page, "Subject").input_value()) == (r.SUBJECT, r.SUBJECT)


@pytest.mark.parametrize("detach", [False, True], ids=["attached", "detached"])
def test_finished_replacement_load_returns_the_live_form_to_the_operator(page, demo, detach):
    r.open_ticket(page, demo, "list")
    subject = field(page, "Subject")
    subject.fill(r.SUBJECT)

    def fail_load(route):
        if detach:
            page.evaluate("window.sixSource.remove()")
        route.abort()

    page.route("**/requests/new/?six=finish*", fail_load)
    result = page.evaluate("""async () => {
        const dialog = document.querySelector("#vr-form-content");
        const form = dialog.querySelector("form");
        const source = window.sixSource = document.createElement("button");
        document.body.append(source);
        const failed = new Promise(resolve => source.addEventListener(
            "htmx:sendError", () => resolve(), {once:true}));
        const request = htmx.ajax("GET", form.action + "?six=finish",
            {source, target:"#vr-form-content"}).catch(() => {});
        await failed;
        await request;
        return [window.vrKeptTyping.collect()[0]?.values.subject ?? null,
            dialog.inert, form.elements.subject.disabled];
    }""")
    assert result == [r.SUBJECT, False, False]


def test_retransmission_with_an_unanswered_optional_radio_leaves_without_warning(page, demo):
    from accounts.models import AccountSession
    from request_forms.models import FieldKind, RequestTemplate
    from request_forms.tests.test_kept_typing_new_ticket import end_open

    route = "Round six, loading bay three."

    def fill_ticket(tab):
        pick(field(tab, "Category"), label="Pickup or missed pickup")
        expect(tab.locator("#category-section")).to_contain_text("Ticket form")
        pick(field(tab, "Ticket form"), label="Pickup")
        field(tab, "Courier or route").fill(route)
        tab.get_by_label("Some required details are not known yet").check()
        field(tab, "Subject").fill(r.SUBJECT)
        field(tab, "What the client asked").fill("Collect the cartons at the marked door.")
        return tab.evaluate("window.vrKeptTyping.collect({commit:true})")

    r.open_ticket(page, demo, "list")
    original = fill_ticket(page)
    template = RequestTemplate.objects.get(key="pickup")
    template.fields.create(
        key="dock-six",
        label="Optional receiving dock",
        kind=FieldKind.CHOICE,
        choices=["North dock", "South dock"],
    )
    later = page.context.new_page()
    later.goto(page.url)
    later.get_by_role("link", name="New ticket", exact=True).first.click()
    current = fill_ticket(later)
    key = "field-dock-six"
    assert current[0]["values"] == {**original[0]["values"], key: ""}
    assert key not in original[0]["values"]
    end_open(AccountSession.objects.get(user__username=r.STAFF, ended_at__isnull=True).user)
    first = page.request.post("/account/session/keep/", data={"forms": original})
    assert first.status == 200 and first.json()["held"] == [True]
    questions = []

    def accept(question):
        questions.append(question.type)
        question.accept()

    later.on("dialog", accept)
    with later.expect_response(
        lambda reply: urlsplit(reply.url).path == r.PAGE and reply.status == 401
    ):
        later.get_by_role("button", name="Log ticket").click()
    assert r.wait_for_sign_in(later) == r.PAGE
    sign_in(later, r.STAFF, demo.password, visit=False)
    expect(field(later, "Subject")).to_have_value(r.SUBJECT)
    expect(field(later, "Courier or route")).to_have_value(route)
    radios = later.locator(f'#vr-form-content input[name="{key}"]')
    assert radios.count() >= 2 and radios.evaluate_all("items => items.every(i => !i.checked)")
    assert questions == [], "The held form returns every entered answer"


def test_sent_post_omits_a_textbox_disabled_before_the_operator_clicked_save(page, demo):
    r.open_ticket(page, demo, "list")
    field(page, "Subject").fill(r.SUBJECT)
    field(page, "Reported by").fill("Former contact")
    field(page, "Reported by").evaluate("el => { el.disabled = true; }")
    form = page.locator("#vr-form-content form").first
    page.route("**/requests/new/?six=disabled-before", lambda route: route.fulfill(status=204))
    with page.expect_request("**/requests/new/?six=disabled-before") as sent:
        captured = form.evaluate("""async form => {
            const sample = () => {
                const values = window.vrKeptTyping.collect({commit:true})[0].values;
                return [values.subject, Object.hasOwn(values, "reported_by")];
            };
            const values = [sample()];
            document.addEventListener("htmx:beforeSend", () => values.push(sample()), {once:true});
            await htmx.ajax("POST", form.action + "?six=disabled-before",
                {source:form,target:"#vr-form-content"});
            values.push(sample());
            return values;
        }""")
    assert b"Former contact" not in sent.value.post_data_buffer
    assert captured == [[r.SUBJECT, False]] * 3
    expect(field(page, "Subject")).to_be_enabled()
    expect(field(page, "Reported by")).to_be_disabled()
