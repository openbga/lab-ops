"""Fourth confirmation using a synthetic New ticket, Chromium and real receipt endpoints."""

from urllib.parse import urlsplit

import pytest
import test_review_kept_typing_new_ticket as r
from helpers import field, pick
from playwright.sync_api import expect

from accounts.models import AccountSession, KeptDraft
from request_forms.tests.test_kept_typing_new_ticket import end_open

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("first", ["json", "form"])
@pytest.mark.parametrize("multiline", [False, True], ids=["single_line", "multiline"])
def test_identical_browser_typing_has_one_receipt_across_json_and_native_post(
    page, demo, first, multiline
):
    r.open_ticket(page, demo, "list")
    pick(field(page, "Category"), label="Pickup or missed pickup")
    expect(page.locator("#category-section")).to_contain_text("Ticket form")
    field(page, "Subject").fill(r.SUBJECT)
    body = "Book the pickup." + ("\nUse dispatch." if multiline else "")
    field(page, "What the client asked").fill(body)
    form = page.locator("#vr-form-content form").first
    end_open(AccountSession.objects.get(user__username=r.STAFF, ended_at__isnull=True).user)

    def send(kind):
        if kind == "json":
            copies = page.evaluate("window.vrKeptTyping.collect({commit:true})")
            assert copies[0]["values"]["body"] == body
            response = page.request.post("/account/session/keep/", data={"forms": copies})
            assert response.status == 200 and response.json()["kept"] == 1
            return response.json()["held"][0]
        result = form.evaluate("""async form => {
            const response = await fetch(form.action, {method:"POST", body:new FormData(form),
                headers:{"HX-Request":"true", "X-VR-Form":"1", "HX-Current-URL":location.href}});
            return [response.status, (await response.json()).kept];
        }""")
        assert result[0] == 401
        return result[1]

    assert send(first)
    assert KeptDraft.objects.get().values["body"].replace("\r\n", "\n") == body
    assert send("form" if first == "json" else "json"), "Identical visible text was already held"


@pytest.mark.parametrize("disabled_by", ["enabled", "option", "optgroup", "fieldset"])
def test_sent_dialog_values_respect_native_disability_and_the_first_legend(page, demo, disabled_by):
    r.open_ticket(page, demo, "list")
    field(page, "Subject").fill(r.SUBJECT)
    form = page.locator("#vr-form-content form").first
    native = form.evaluate(
        """(form, kind) => {
        const channel = form.elements.channel;
        channel.value = "phone";
        const option = channel.selectedOptions[0];
        if (kind === "option") option.disabled = true;
        if (kind === "optgroup") {
            const group = document.createElement("optgroup");
            group.disabled = true;
            channel.append(group);
            group.append(option);
            option.selected = true;
        }
        if (kind === "fieldset") {
            const fieldset = document.createElement("fieldset");
            const legend = document.createElement("legend");
            form.append(fieldset);
            fieldset.append(legend, channel);
            legend.append(form.elements.subject);
            fieldset.disabled = true;
        }
        const data = new FormData(form);
        return [data.get("subject"), data.get("channel") || ""];
    }""",
        disabled_by,
    )
    expected = [r.SUBJECT, "phone" if disabled_by == "enabled" else ""]
    assert native == expected
    page.route("**/requests/new/?round=four", lambda route: route.fulfill(status=204))
    with page.expect_response("**/requests/new/?round=four"):
        form.evaluate("""form => {
            document.addEventListener("htmx:beforeSend", () => {
                const copy = window.vrKeptTyping.collect({commit:true})[0];
                window.sentValues = [copy?.values.subject, copy?.values.channel || ""];
            }, {once:true});
            const url = form.action + "?round=four";
            htmx.ajax("POST", url, {source:form, target:"#vr-form-content"});
        }""")
    expect(field(page, "Subject")).to_be_enabled()
    assert page.evaluate("window.sentValues") == expected
    assert form.locator("[data-vr-busy-disabled]").count() == 0


def test_cancelling_a_dialog_targeted_post_leaves_typing_collectible_and_editable(page, demo):
    r.open_ticket(page, demo, "list")
    subject = field(page, "Subject")
    subject.fill(r.SUBJECT)
    attempts = []
    page.on(
        "request",
        lambda req: attempts.append(req.url) if urlsplit(req.url).query == "cancel=1" else None,
    )
    form = page.locator("#vr-form-content form").first
    state = form.evaluate("""form => {
        window.addEventListener("htmx:beforeRequest", event => event.preventDefault(), {once:true});
        htmx.ajax("POST", form.action + "?cancel=1", {source:form,target:"#vr-form-content"});
        return {busy:document.querySelector("#vr-form-content").getAttribute("aria-busy"),
            disabled:form.elements.subject.matches(":disabled"),
            copies:window.vrKeptTyping.collect().map(copy => copy.values.subject)};
    }""")
    assert not attempts
    assert state == {"busy": "false", "disabled": False, "copies": [r.SUBJECT]}


def test_first_copy_from_a_pending_save_restores_its_selected_values_and_typed_answer(
    page, demo, monkeypatch
):
    from threading import Event

    from helpers import sign_in

    from accounts import sessions

    r.open_ticket(page, demo, "list")
    pick(field(page, "Category"), label="Pickup or missed pickup")
    expect(page.locator("#category-section")).to_contain_text("Ticket form")
    pick(field(page, "Ticket form"), label="Pickup")
    route_text = "Example route 4, side entrance."
    field(page, "Courier or route").fill(route_text)
    page.get_by_label("Some required details are not known yet").check()
    pick(field(page, "Channel"), label="Email")
    field(page, "Subject").fill(r.SUBJECT)
    field(page, "What the client asked").fill("Collect the cartons by email arrangement.")
    category = page.locator("#vr-form-content [name=category]").input_value()
    entered, release = Event(), Event()
    refusal = sessions._refusal

    def delay_post(request):
        if request.method == "POST" and request.path == r.PAGE:
            entered.set()
            release.wait(timeout=12)
        return refusal(request)

    monkeypatch.setattr(sessions, "_refusal", delay_post)
    r.expire_row()
    try:
        page.get_by_role("button", name="Log ticket").click()
        assert entered.wait(5)
        with page.expect_response(
            lambda reply: urlsplit(reply.url).path == "/account/session/keep/"
        ):
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        saved = KeptDraft.objects.get().values
    finally:
        release.set()
    assert r.wait_for_sign_in(page) == r.PAGE
    sign_in(page, r.STAFF, demo.password, visit=False)
    expect(field(page, "Subject")).to_have_value(r.SUBJECT)
    form = page.locator("#vr-form-content form").first
    restored = form.evaluate("""form => [
        form.elements.category.value, form.elements.channel.value,
        form.elements.namedItem("field-courier")?.value ?? null
    ]""")
    expected = [category, "email", route_text]
    assert ([saved["category"], saved["channel"], saved.get("field-courier")], restored) == (
        expected,
        expected,
    )


@pytest.mark.parametrize(
    "body", ["Dispatch today.", "Dispatch today.\nUse bay two."], ids=["single_line", "multiline"]
)
def test_refused_retransmission_of_a_held_dialog_leaves_without_a_question(page, demo, body):
    from helpers import sign_in

    r.open_ticket(page, demo, "list")
    pick(field(page, "Category"), label="Pickup or missed pickup")
    expect(page.locator("#category-section")).to_contain_text("Ticket form")
    field(page, "Subject").fill(r.SUBJECT)
    field(page, "What the client asked").fill(body)
    copies = page.evaluate("window.vrKeptTyping.collect({commit:true})")
    assert copies[0]["values"]["body"] == body
    end_open(AccountSession.objects.get(user__username=r.STAFF, ended_at__isnull=True).user)
    first = page.request.post("/account/session/keep/", data={"forms": copies})
    assert first.status == 200 and first.json()["held"] == [True]
    questions = []

    def accept(dialog):
        questions.append(dialog.type)
        dialog.accept()

    page.on("dialog", accept)
    page.route("**/account/session/keep/", lambda route: route.abort())
    with page.expect_response(
        lambda reply: urlsplit(reply.url).path == r.PAGE and reply.status == 401
    ):
        page.get_by_role("button", name="Log ticket").click()
    assert r.wait_for_sign_in(page) == r.PAGE
    sign_in(page, r.STAFF, demo.password, visit=False)
    expect(field(page, "Subject")).to_have_value(r.SUBJECT)
    expect(field(page, "What the client asked")).to_have_value(body)
    assert questions == [], "The exact displayed text returned after sign-in"
