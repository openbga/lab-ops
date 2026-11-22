"""Second confirmation of New ticket's refusal and busy-state handling in Chromium."""

from urllib.parse import urlsplit

import pytest
import test_review_kept_typing_new_ticket as r
from helpers import field, pick, sign_in
from playwright.sync_api import expect

from accounts.models import KeptDraft

pytestmark = pytest.mark.django_db(transaction=True)


def test_a_receipt_for_another_tabs_earlier_copy_preserves_the_current_warning(page, demo):
    r.open_ticket(page, demo, "list")
    pick(field(page, "Category"), label="Pickup or missed pickup")
    expect(page.locator("#category-section")).to_contain_text("Ticket form")
    field(page, "Subject").fill(r.SUBJECT)
    field(page, "What the client asked").fill("Confirm this afternoon's transport.")

    first = page.context.new_page()
    first.goto(r.PAGE)
    field(first, "Subject").fill("Earlier morning collection")
    # Deliver the other tab's actual form without navigating it or acknowledging the second tab.
    r.expire_row()
    status = field(first, "Subject").evaluate("""async input => {
        const form = input.form;
        const response = await fetch(form.action, {
            method: "POST", body: new FormData(form),
            headers: {"HX-Request":"true", "X-VR-Form":"1"}
        });
        return response.status;
    }""")
    assert (
        status == 401 and KeptDraft.objects.get().values["subject"] == "Earlier morning collection"
    )
    questions = []

    def answer(dialog):
        questions.append(dialog.type)
        dialog.accept()

    page.on("dialog", answer)
    page.route("**/account/session/keep/", lambda route: route.abort())
    with page.expect_response(
        lambda resp: urlsplit(resp.url).path == r.PAGE and resp.status == 401
    ):
        page.get_by_role("button", name="Log ticket").click()
    assert r.wait_for_sign_in(page) == r.PAGE
    sign_in(page, r.STAFF, demo.password, visit=False)
    expect(field(page, "Subject")).to_have_value("Earlier morning collection")
    assert questions == ["beforeunload"], "This tab's different subject was never held"


def test_finishing_one_of_two_posts_keeps_the_busy_forms_subject_collectible(page, demo):
    r.open_ticket(page, demo, "list")
    field(page, "Subject").fill(r.SUBJECT)
    form = page.locator("#vr-form-content form").first
    # Independent htmx sources on the same form can have one request outstanding each.
    # Hold the HTTP responses and finish them in order without replacing the form.
    responses = {}
    page.route(
        "**/requests/new/?confirm=*",
        lambda route: responses.update({urlsplit(route.request.url).query: route}),
    )
    with page.expect_request("**/requests/new/?confirm=second"):
        form.evaluate("""form => {
            const button = document.createElement("button");
            form.id ||= "confirmation-ticket";
            button.setAttribute("form", form.id);
            document.body.append(button);
            for (const [source, id] of [[form, "first"], [button, "second"]]) {
                const address = form.action + "?confirm=" + id;
                htmx.ajax("POST", address, {source, target:"#vr-form-content"});
            }
        }""")
    subject = field(page, "Subject")
    expect(subject).to_be_disabled()
    collect = "() => window.vrKeptTyping.collect().map(copy => copy.values.subject)"
    assert page.evaluate(collect) == [r.SUBJECT]
    first = responses["confirm=first"]
    with page.expect_response("**/requests/new/?confirm=first"):
        first.fulfill(status=204)
    page.wait_for_function("""() => !document.querySelector('#vr-form-content form')
        .classList.contains('htmx-request')""")
    expect(page.locator("#vr-form-content")).to_have_attribute("aria-busy", "true")
    during = page.evaluate(collect)
    responses["confirm=second"].fulfill(status=204)
    expect(subject).to_be_enabled()
    assert subject.get_attribute("data-vr-busy-disabled") is None
    assert during == [r.SUBJECT], "The other form POST was still in flight"


def test_busy_collection_obeys_inherited_fieldset_disability(page, demo):
    r.open_ticket(page, demo, "list")
    field(page, "Subject").fill(r.SUBJECT)
    contact = field(page, "Reported by")
    contact.fill("Retired example contact")
    contact.evaluate("""el => {
        const fieldset = document.createElement("fieldset");
        el.before(fieldset);
        fieldset.append(el);
        fieldset.disabled = true;
    }""")
    form = page.locator("#vr-form-content form").first
    assert form.evaluate("form => !new FormData(form).has('reported_by')")
    page.route("**/requests/new/?confirm=fieldset", lambda route: route.fulfill(status=204))
    form.evaluate("""form => {
        document.addEventListener("htmx:beforeRequest", () => {
            window.confirmationHeld = window.vrKeptTyping.collect()[0].values;
        }, {once:true});
        const address = form.action + "?confirm=fieldset";
        htmx.ajax("POST", address, {source:form,target:"#vr-form-content"});
    }""")
    page.wait_for_function("window.confirmationHeld !== undefined")
    values = page.evaluate("window.confirmationHeld")
    assert values["subject"] == r.SUBJECT
    assert "reported_by" not in values
