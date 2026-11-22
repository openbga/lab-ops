"""Fifth confirmation of select values, dialog loads and recovery in Chromium."""

from urllib.parse import urlsplit

import pytest
import test_review_kept_typing_new_ticket as r
from helpers import field, pick
from playwright.sync_api import expect

pytestmark = pytest.mark.django_db(transaction=True)


def test_cancelled_log_ticket_preserves_the_copy_and_allows_the_operator_to_edit(page, demo):
    r.open_ticket(page, demo, "list")
    pick(field(page, "Category"), label="Pickup or missed pickup")
    expect(page.locator("#category-section")).to_contain_text("Ticket form")
    pick(field(page, "Channel"), label="Email")
    subject = field(page, "Subject")
    subject.fill(r.SUBJECT)
    field(page, "What the client asked").fill("Synthetic collection at reception.")
    posted = []
    page.on("request", lambda req: posted.append(req) if req.method == "POST" else None)
    page.evaluate("""window.addEventListener("htmx:beforeRequest", e => {
        if (e.detail.requestConfig.verb.toLowerCase() === "post") {
            window.cancelledSave = true; e.preventDefault();
        }
    }, {once:true})""")
    page.get_by_role("button", name="Log ticket").click()
    assert page.evaluate("window.cancelledSave") and not posted
    observed = page.evaluate("""() => {
        const dialog = document.querySelector("#vr-form-content");
        const form = dialog.querySelector("form");
        const copy = window.vrKeptTyping.collect({commit:true})[0]?.values;
        return [copy?.subject, copy?.channel, dialog.inert, form.elements.subject.disabled];
    }""")
    assert observed == [r.SUBJECT, "email", False, False]
    subject.fill("Corrected synthetic collection")


@pytest.mark.parametrize("second", ["GET", "POST"])
def test_overlapping_dialog_loads_suppress_only_until_the_last_get_finishes(page, demo, second):
    r.open_ticket(page, demo, "list")
    field(page, "Subject").fill(r.SUBJECT)
    pick(field(page, "Channel"), label="Email")
    responses = {}
    page.route(
        "**/requests/new/?round5=*",
        lambda route: responses.update({urlsplit(route.request.url).query.split("&")[0]: route}),
    )
    page.evaluate(
        """verb => {
        const form = document.querySelector("#vr-form-content form");
        window.copyPair = () => window.vrKeptTyping.collect().map(
            copy => [copy.values.subject, copy.values.channel]);
        window.roundFiveSend = (id, method) => {
            const source = document.createElement("button");
            document.body.append(source);
            window["roundFiveDone" + id] = htmx.ajax(method, form.action + "?round5=" + id,
                {source, target:"#vr-form-content"});
        };
        window.roundFiveSend(1, "GET");
        window.roundFiveSend(2, verb);
    }""",
        second,
    )
    expect(field(page, "Subject")).to_be_disabled()
    assert page.evaluate("window.copyPair()") == []
    assert set(responses) == {"round5=1", "round5=2"}
    responses["round5=1"].fulfill(status=204)
    page.evaluate("window.roundFiveDone1")
    during = page.evaluate("window.copyPair()")
    responses["round5=2"].fulfill(status=204)
    page.evaluate("window.roundFiveDone2")
    expect(field(page, "Subject")).to_be_enabled()
    pair = [[r.SUBJECT, "email"]]
    assert (during, page.evaluate("window.copyPair()")) == ([] if second == "GET" else pair, pair)


@pytest.mark.parametrize("detach", [False, True], ids=["attached", "detached"])
def test_failed_dialog_get_stops_suppressing_the_next_sent_save(page, demo, detach):
    r.open_ticket(page, demo, "list")
    field(page, "Subject").fill(r.SUBJECT)

    def fail_load(route):
        if detach:
            page.evaluate("window.roundFiveSource.remove()")
        route.abort()

    page.route("**/requests/new/?round5=old*", fail_load)
    with page.expect_request("**/requests/new/?round5=old*"):
        page.evaluate("""() => {
            const source = document.createElement("button");
            document.body.append(source);
            window.roundFiveSource = source;
            window.roundFiveError = new Promise(resolve => source.addEventListener(
                "htmx:sendError", () => resolve(true), {once:true}));
            htmx.ajax("GET", "/requests/new/?round5=old", {source, target:"#vr-form-content"})
                .catch(() => {});
        }""")
    assert page.evaluate("window.roundFiveError")
    page.route("**/requests/new/?round5=save", lambda route: route.fulfill(status=204))
    with page.expect_response("**/requests/new/?round5=save"):
        page.evaluate("""() => {
            const form = document.querySelector("#vr-form-content form");
            document.addEventListener("htmx:beforeSend", () => {
                window.roundFiveSubject = window.vrKeptTyping.collect()[0]?.values.subject ?? null;
            }, {once:true});
            htmx.ajax("POST", form.action + "?round5=save",
                {source:form, target:"#vr-form-content"});
        }""")
    assert page.evaluate("window.roundFiveSubject") == r.SUBJECT


def test_get_replacing_a_form_suppresses_its_associated_external_textbox(page, demo):
    r.open_ticket(page, demo, "list")
    field(page, "Subject").fill(r.SUBJECT)
    form = page.locator("#vr-form-content form").first
    form.evaluate("""form => {
        const input = form.elements.subject;
        form.id ||= "round-five-ticket";
        input.setAttribute("form", form.id);
        document.body.append(input);
        if (new FormData(form).get("subject") !== input.value) throw Error("Lost form association");
    }""")
    assert page.evaluate("window.vrKeptTyping.collect()[0].values.subject") == r.SUBJECT
    pending = []
    page.route("**/requests/new/?round5=leave*", lambda route: pending.append(route))
    with page.expect_request("**/requests/new/?round5=leave*"):
        form.evaluate("""form => {
            window.roundFiveLoad = htmx.ajax("GET", form.action + "?round5=leave",
            {source:form, target:"#vr-form-content"});}""")
    during = page.evaluate("window.vrKeptTyping.collect().map(copy => copy.values.subject)")
    pending[0].fulfill(status=204)
    page.evaluate("window.roundFiveLoad")
    assert during == [], "The associated textbox belongs to the form being replaced"


@pytest.mark.parametrize("disabled", [False, True], ids=["enabled-group", "disabled-group"])
def test_option_inside_a_group_wrapper_matches_what_chromium_submits(page, demo, disabled):
    r.open_ticket(page, demo, "list")
    field(page, "Subject").fill(r.SUBJECT)
    form = page.locator("#vr-form-content form").first
    expected = form.evaluate(
        """(form, disabled) => {
        const channel = form.elements.channel;
        const option = Array.from(channel.options).find(item => item.value === "email");
        const group = document.createElement("optgroup");
        group.label = "Contact methods";
        group.disabled = disabled;
        const container = document.createElement("div");
        group.append(container);
        channel.append(group);
        container.append(option);
        option.selected = true;
        if (channel.selectedOptions[0] !== option) throw Error("Option is not selected");
        return new FormData(form).get("channel") || "";
    }""",
        disabled,
    )
    idle = page.evaluate("window.vrKeptTyping.collect()[0].values.channel")
    page.route("**/requests/new/?round5=group", lambda route: route.fulfill(status=204))
    with page.expect_response("**/requests/new/?round5=group"):
        form.evaluate("""form => {
            document.addEventListener("htmx:beforeSend", () => {
                window.roundFiveChoice = window.vrKeptTyping.collect()[0].values.channel;
            }, {once:true});
            htmx.ajax("POST", form.action + "?round5=group",
                {source:form, target:"#vr-form-content"});
        }""")
    assert (idle, page.evaluate("window.roundFiveChoice")) == (expected, expected)


def test_remote_and_native_selects_keep_their_submitted_values_during_save(page, demo):
    r.open_ticket(page, demo, "list")
    pick(field(page, "Organization"), label="Example Family Clinic")
    pick(field(page, "Category"), label="Pickup or missed pickup")
    expect(page.locator("#category-section")).to_contain_text("Ticket form")
    pick(field(page, "Ticket form"), label="Pickup")
    field(page, "Courier or route").fill("Example North route")
    pick(field(page, "Channel"), label="Email")
    field(page, "Subject").fill(r.SUBJECT)
    form = page.locator("#vr-form-content form").first
    before = form.evaluate("""form => {
        window.roundFiveNames = ["category", "request_form", "channel", "client", "field-courier"];
        const data = new FormData(form);
        return window.roundFiveNames.map(name => data.get(name));
    }""")
    assert all(before) and before[2] == "email" and before[4] == "Example North route"
    page.route("**/requests/new/?round5=values", lambda route: route.fulfill(status=204))
    with page.expect_response("**/requests/new/?round5=values"):
        form.evaluate("""form => {
            document.addEventListener("htmx:beforeSend", () => {
                const copy = window.vrKeptTyping.collect({commit:true})[0];
                window.roundFiveBusy = window.roundFiveNames.map(name => copy.values[name]);
            }, {once:true});
            htmx.ajax("POST", form.action + "?round5=values",
                {source:form, target:"#vr-form-content"});
        }""")
    expect(field(page, "Subject")).to_be_enabled()
    assert page.evaluate("window.roundFiveBusy") == before


def test_correcting_an_unavailable_organization_saves_the_courier_answer_still_on_screen(
    page, demo
):
    from clients.models import Client
    from desk.models import ServiceRequest
    from request_forms.models import FieldValue, RequestTemplate

    available = Client.objects.create(name="Example Dispatch Office", account_number="R5DISPATCH")
    unavailable = Client.objects.create(name="Example Routing Office", account_number="R5ROUTING")
    pickup = RequestTemplate.objects.get(key="pickup")
    pickup.clients.set([available])
    RequestTemplate.objects.create(
        key="round5-alternative", name="Example general pickup", category=pickup.category
    )
    r.open_ticket(page, demo, "list")
    pick(field(page, "Category"), label="Pickup or missed pickup")
    expect(page.locator("#category-section")).to_contain_text("Ticket form")
    pick(field(page, "Ticket form"), value=str(pickup.pk))
    courier = field(page, "Courier or route")
    route = "Example van 5 at the east door."
    courier.fill(route)
    page.get_by_label("Some required details are not known yet").check()
    field(page, "Subject").fill(r.SUBJECT)
    field(page, "What the client asked").fill("Collect the sample cartons at reception.")
    pick(field(page, "Organization"), label=unavailable.name)
    Client.objects.filter(pk=unavailable.pk).update(is_active=False)
    with page.expect_response(
        lambda resp: urlsplit(resp.url).path == r.PAGE and resp.request.method == "POST"
    ) as refused:
        page.get_by_role("button", name="Log ticket").click()
    assert refused.value.status == 200
    expect(page.locator("#vr-form-content .vr-field-errors").first).to_be_visible()
    assert not ServiceRequest.objects.filter(subject=r.SUBJECT).exists()
    if not courier.count():
        expect(page.locator("#vr-form-content #id_request_form_error")).to_be_visible()
        expect(page.locator("#vr-form-content select[name=request_form]")).to_have_value("")
        return
    expect(courier).to_have_value(route)
    pick(field(page, "Organization"), label=available.name)
    expect(courier).to_have_value(route)
    selected = page.locator("#vr-form-content select[name=request_form]").input_value()
    with page.expect_response(
        lambda resp: urlsplit(resp.url).path == r.PAGE and resp.request.method == "POST"
    ):
        page.get_by_role("button", name="Log ticket").click()
    ticket = ServiceRequest.objects.get(subject=r.SUBJECT, client=available)
    page.wait_for_url(f"**/requests/{ticket.pk}/", wait_until="load")
    expect(page.locator("main")).to_contain_text(ticket.number)
    stored = list(
        FieldValue.objects.filter(filled_form__request=ticket, key="courier").values_list(
            "value", flat=True
        )
    )
    assert (selected, stored) == (str(pickup.pk), [route])
