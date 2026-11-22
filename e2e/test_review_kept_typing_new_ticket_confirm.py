"""Narrow confirmation journeys for the kept-typing fixes, against a synthetic laboratory."""

from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import test_review_kept_typing_new_ticket as r
from django.urls import reverse
from helpers import field, sign_in
from playwright.sync_api import expect

from accounts.models import KeptDraft
from desk.models import ServiceRequest

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("edit", [False, True], ids=["discard", "confirm_discard"])
def test_expired_discard_reload_does_not_keep_the_discarded_form(page, demo, edit):
    r.open_ticket(page, demo, "direct")
    field(page, "Subject").fill(r.SUBJECT)
    r.expire_row()
    page.clock.fast_forward(30 * 60 * 1000)
    assert r.wait_for_sign_in(page) == r.PAGE
    sign_in(page, r.STAFF, demo.password, visit=False)
    expect(field(page, "Subject")).to_have_value(r.SUBJECT)
    assert KeptDraft.objects.get().taken_at is not None
    if edit:
        field(page, "Subject").fill("Superseded synthetic ticket")
        page.get_by_role("link", name="Discard", exact=True).click()
        button = page.locator("#vr-form-discard").get_by_role("button", name="Discard changes")
    else:
        button = page.get_by_role("link", name="Discard", exact=True)
    r.expire_row()
    with page.expect_response(
        lambda resp: urlsplit(resp.url).path == r.PAGE and resp.status == 401
    ):
        button.click()
    assert r.wait_for_sign_in(page) == r.PAGE
    pending = list(
        KeptDraft.objects.filter(taken_at__isnull=True).values_list("values__subject", flat=True)
    )
    sign_in(page, r.STAFF, demo.password, visit=False)
    subject = field(page, "Subject")
    expect(subject).to_be_enabled()
    assert (pending, subject.input_value()) == ([], ""), (
        "Discard's GET must return to an empty ticket after sign-in"
    )


def test_busy_collection_excludes_a_control_already_disabled_before_the_request(page, demo):
    r.open_ticket(page, demo, "list")
    field(page, "Subject").fill(r.SUBJECT)
    field(page, "Reported by").fill("Retired contact label")
    # Model a conditional control disabled by the form itself. Remaining controls are
    # disabled by the real form dialog when it posts. Native submission omits the former.
    field(page, "Reported by").evaluate("el => { el.disabled = true; }")
    field(page, "What the client asked").fill("Confirm carton pickup.")
    form = page.locator("#vr-form-content form").first
    before = form.evaluate("""form => {
        window.confirmationBusyValues = null;
        document.addEventListener("htmx:beforeRequest", () => {
            if (document.querySelector("#vr-form-content").getAttribute("aria-busy") === "true") {
                const copy = window.vrKeptTyping.collect({commit:true})[0];
                window.confirmationBusyValues = copy?.values;
            }
        });
        return window.vrKeptTyping.collect()[0].values;
    }""")
    assert "reported_by" not in before
    # Send a real dialog request with the same source/target. Its temporary disabling
    # happens before the collection above. A missing category in this fixture is intentional.
    form.evaluate("""form => { htmx.ajax("POST", form.action, {
        source:form, target:"#vr-form-content",
        values: {csrfmiddlewaretoken: form.elements.csrfmiddlewaretoken.value}}); }""")
    page.wait_for_function("window.confirmationBusyValues !== null")
    during = page.evaluate("window.confirmationBusyValues")
    assert during["subject"] == r.SUBJECT
    assert "reported_by" not in during


def test_legacy_notification_without_form_identity_preserves_an_unsent_composer(page, demo):
    ticket = ServiceRequest.objects.filter(
        status="open", category__is_restricted=False, team__memberships__user__username=r.STAFF
    ).first()
    sign_in(page, r.STAFF, demo.password)
    page.goto(reverse("desk:request-detail", args=[ticket.pk]))
    field(page.locator("form.vr-composer"), "Message").fill(r.SUBJECT)
    states = page.evaluate("""() => {
        const warned = () => !window.dispatchEvent(new Event("beforeunload", {cancelable:true}));
        const before = warned();
        document.dispatchEvent(new CustomEvent("vr:typing-kept", {detail:{kept:1}}));
        return [before, warned()];
    }""")
    assert states == [True, True]


def test_local_signin_return_retains_the_path_and_query_once(page, demo):
    from test_review_kept_typing_new_ticket_verify import issue_partial, listed

    listed(page, demo)
    r.expire_row()
    dest = "/requests/?q=carton%20labels&view=all"
    address = "/accounts/login/?" + urlencode({"next": dest, "extra": "unused"})
    issue_partial(page, {"signed_in": False, "sign_in": address}, address)
    assert r.wait_for_sign_in(page) == dest
    assert parse_qs(urlsplit(page.url).query) == {"next": [dest]}


def test_a_held_dialog_post_can_leave_if_the_second_acknowledgment_is_unavailable(page, demo):
    from helpers import pick

    r.open_ticket(page, demo, "list")
    pick(field(page, "Category"), label="Pickup or missed pickup")
    expect(page.locator("#category-section")).to_contain_text("Ticket form")
    field(page, "Subject").fill(r.SUBJECT)
    field(page, "What the client asked").fill("Check the transport route.")
    questions = []

    def accept(question):
        questions.append(question.type)
        question.accept()

    page.on("dialog", accept)
    page.route("**/account/session/keep/", lambda route: route.abort())
    r.expire_row()
    page.get_by_role("button", name="Log ticket").click()
    assert r.wait_for_sign_in(page) == r.PAGE
    assert KeptDraft.objects.get().values["subject"] == r.SUBJECT
    assert not ServiceRequest.objects.filter(subject=r.SUBJECT).exists()
    assert questions == [], "The refused POST had already kept the dialog's copy"


def test_detached_request_ignores_unrelated_answers_and_sends_once(page, demo):
    r.open_ticket(page, demo, "list")
    field(page, "Subject").fill(r.SUBJECT)
    page.evaluate("""() => {
        const source = document.createElement("button");
        document.body.append(source);
        window.confirmationSource = source;
        const detail = {elt:source, target:document.body, requestConfig:{verb:"get"}};
        for (let i = 0; i < 2; i++) {
            source.dispatchEvent(new CustomEvent("htmx:beforeRequest", {bubbles:true, detail}));
        }
        source.remove();
    }""")
    r.expire_row()
    sent = []
    page.on("request", lambda req: sent.append(req) if "/session/keep/" in req.url else None)
    results = page.evaluate("""() => {
        function accepted(status, body, redirect) {
            const xhr = {status, responseText:body,
                getResponseHeader: name => name === "HX-Redirect" ? redirect : null};
            const event = new CustomEvent("htmx:beforeOnLoad", {
                cancelable:true, bubbles:true, detail:{xhr, target:document.body}
            });
            return window.confirmationSource.dispatchEvent(event);
        }
        const login = "/accounts/login/?next=%2Frequests%2F";
        const refused = JSON.stringify({signed_in:false, sign_in:login});
        return [accepted(403, refused, login), accepted(401, "Not JSON", login),
                accepted(401, refused, null), accepted(401, "{}", login),
                accepted(401, refused, login), accepted(401, refused, login)];
    }""")
    assert results == [True, True, True, True, False, False]
    assert r.wait_for_sign_in(page) == r.PAGE
    assert len(sent) == 1 and KeptDraft.objects.get().values["subject"] == r.SUBJECT
