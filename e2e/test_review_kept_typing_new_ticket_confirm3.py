"""Chromium checks of New ticket receipts, the busy request count and native disabled controls."""

from urllib.parse import urlsplit

import pytest
import test_review_kept_typing_new_ticket as r
from helpers import field, pick, sign_in
from playwright.sync_api import expect

from accounts.models import KeptDraft

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("same_delivery", "followup_available"),
    [(False, True), (True, False), (True, True)],
    ids=["different_with_followup", "same_without_followup", "same_with_followup"],
)
def test_unload_guard_matches_what_actually_returns_to_this_form(
    page, demo, same_delivery, followup_available
):
    r.open_ticket(page, demo, "list")
    pick(field(page, "Category"), label="Pickup or missed pickup")
    expect(page.locator("#category-section")).to_contain_text("Ticket form")
    field(page, "Subject").fill(r.SUBJECT)
    field(page, "What the client asked").fill("Use the synthetic collection route.")
    original_subject = r.SUBJECT if same_delivery else "Earlier collection from another tab"
    first = page
    if not same_delivery:
        first = page.context.new_page()
        first.goto(r.PAGE)
        field(first, "Subject").fill(original_subject)
    r.expire_row()
    # The first HTTP delivery stores its form. Its receipt never reaches the active dialog's JS.
    status = field(first, "Subject").evaluate("""async input => {
        const response = await fetch(input.form.action, {
            method:"POST", body:new FormData(input.form),
            headers: {"HX-Request":"true", "X-VR-Form":"1"}
        });
        return response.status;
    }""")
    stored = KeptDraft.objects.get().values
    assert status == 401 and stored["subject"] == original_subject
    questions, followups = [], []

    def accept(dialog):
        questions.append(dialog.type)
        dialog.accept()

    def capture(request):
        if urlsplit(request.url).path == "/account/session/keep/":
            followups.append(request.post_data_json["forms"][0]["values"])

    page.on("dialog", accept)
    page.on("request", capture)
    if not followup_available:
        page.route("**/account/session/keep/", lambda route: route.abort())
    with page.expect_response(
        lambda reply: urlsplit(reply.url).path == r.PAGE and reply.status == 401
    ):
        page.get_by_role("button", name="Log ticket").click()
    assert r.wait_for_sign_in(page) == r.PAGE
    sign_in(page, r.STAFF, demo.password, visit=False)
    expect(field(page, "Subject")).to_have_value(original_subject)
    assert [copy["subject"] for copy in followups] == [r.SUBJECT]
    if same_delivery:
        assert {k: v for k, v in followups[0].items() if v} == {
            k: v for k, v in stored.items() if v
        }
    assert questions == ([] if same_delivery else ["beforeunload"])


def test_cancelled_post_does_not_count_against_a_later_dialog_load(page, demo):
    r.open_ticket(page, demo, "list")
    subject = field(page, "Subject")
    subject.fill(r.SUBJECT)
    form = page.locator("#vr-form-content form").first
    # A beforeRequest listener may cancel htmx before it sends an XHR. Use a non-dialog
    # target for that request so it does not independently change the dialog's busy state.
    form.evaluate("""form => {
        window.addEventListener("htmx:beforeRequest", event => { event.preventDefault(); },
            {once:true});
        htmx.ajax("POST", form.action + "?phase=cancel", {source:form, target:form});
    }""")
    expect(subject).to_be_enabled()
    page.route("**/requests/new/?phase=sent", lambda route: route.fulfill(status=204))
    with page.expect_response("**/requests/new/?phase=sent"):
        form.evaluate("""form => {
            const url = form.action + "?phase=sent";
            htmx.ajax("POST", url, {source:form, target:"#vr-form-content"});
        }""")
    expect(subject).to_be_enabled()
    # The form has finished its one actual POST. A subsequent load over it is a GET.
    page.route("**/requests/new/?phase=load*", lambda route: route.fulfill(status=204))
    # Adopted: collected once the load is sent (htmx:beforeSend). The dialog now takes a request
    # as under way when it is sent (static/js/form-dialog.js), so a request cancelled before never
    # freezes it; at beforeRequest the form is still the live form, and collecting it is right.
    form.evaluate("""form => {
        document.addEventListener("htmx:beforeSend", () => {
            window.loadCopies = window.vrKeptTyping.collect().map(item => item.values.subject);
        }, {once:true});
        htmx.ajax("GET", form.action + "?phase=load", {source:form, target:"#vr-form-content"});
    }""")
    page.wait_for_function("Array.isArray(window.loadCopies)")
    assert page.evaluate("window.loadCopies") == []


def test_fieldset_first_legend_matches_native_submission_before_during_and_after_save(page, demo):
    r.open_ticket(page, demo, "list")
    subject, contact = field(page, "Subject"), field(page, "Reported by")
    subject.fill(r.SUBJECT)
    contact.fill("Example obsolete name")
    form = page.locator("#vr-form-content form").first
    form.evaluate("""form => {
        const fieldset = document.createElement("fieldset");
        const first = document.createElement("legend"), second = document.createElement("legend");
        form.append(fieldset);
        fieldset.append(first, second);
        first.append(form.elements.subject);
        second.append(form.elements.reported_by);
        fieldset.disabled = true;
        window.legendValues = () => {
            const copy = window.vrKeptTyping.collect()[0];
            return [copy?.values.subject, copy?.values.reported_by ?? null];
        };
    }""")
    expected = [r.SUBJECT, None]
    assert (
        form.evaluate("""form => {
        const data = new FormData(form);
        return [data.get("subject"), data.get("reported_by")];
    }""")
        == expected
    )
    assert page.evaluate("window.legendValues()") == expected
    page.route("**/requests/new/?phase=legend", lambda route: route.fulfill(status=204))
    with page.expect_response("**/requests/new/?phase=legend"):
        form.evaluate("""form => {
            document.addEventListener("htmx:beforeRequest", () => {
                window.legendDuring = window.legendValues();
            }, {once:true});
            const target = "#vr-form-content";
            htmx.ajax("POST", form.action + "?phase=legend", {source:form,target});
        }""")
    expect(subject).to_be_enabled()
    expect(contact).to_be_disabled()
    assert page.evaluate("window.legendDuring") == expected
    assert page.evaluate("window.legendValues()") == expected
    assert form.locator("[data-vr-busy-disabled]").count() == 0


@pytest.mark.parametrize("disabled_by", ["option", "optgroup"])
def test_busy_collection_omits_a_channel_that_native_submission_omits(page, demo, disabled_by):
    r.open_ticket(page, demo, "list")
    field(page, "Subject").fill(r.SUBJECT)
    form = page.locator("#vr-form-content form").first
    omitted = form.evaluate(
        """(form, disabledBy) => {
        const select = form.elements.channel;
        select.value = "phone";
        const option = select.selectedOptions[0];
        if (disabledBy === "option") {
            option.disabled = true;
        } else {
            const group = document.createElement("optgroup");
            group.disabled = true;
            select.append(group);
            group.append(option);
            option.selected = true;
        }
        return select.value === "phone" && !new FormData(form).has("channel");
    }""",
        disabled_by,
    )
    assert omitted
    page.route("**/requests/new/?phase=channel", lambda route: route.fulfill(status=204))
    form.evaluate("""form => {
        document.addEventListener("htmx:beforeRequest", () => {
            window.channelCopy = window.vrKeptTyping.collect()[0].values;
        }, {once:true});
        const url = form.action + "?phase=channel";
        htmx.ajax("POST", url, {source:form, target:"#vr-form-content"});
    }""")
    page.wait_for_function("window.channelCopy !== undefined")
    values = page.evaluate("window.channelCopy")
    assert values["subject"] == r.SUBJECT
    assert not values.get("channel")
