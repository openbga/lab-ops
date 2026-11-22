"""Chromium follow-up verification using the synthetic laboratory and local receipt fixtures."""

import json
import re
from threading import Event
from urllib.parse import urlsplit

import pytest
import test_kept_typing_new_ticket as ticket
import test_review_kept_typing_new_ticket as r
from django.urls import reverse
from helpers import field, native_select, pick, sign_in
from playwright.sync_api import expect

from accounts import kept
from accounts.models import KeptDraft
from desk.models import ServiceRequest
from request_forms.models import FieldKind, TemplateField

pytestmark = pytest.mark.django_db(transaction=True)
LIST = "/requests/?view=all"


def listed(page, demo):
    page.clock.install()
    sign_in(page, r.STAFF, demo.password)
    page.goto(LIST)
    expect(page.get_by_role("link", name="New ticket", exact=True).first).to_be_visible()


def test_dialog_post_restores_fields_for_either_spelling_of_local_next(page, demo):
    r.open_ticket(page, demo, "list")
    pick(field(page, "Category"), label="Pickup or missed pickup")
    expect(page.locator("#category-section")).to_contain_text("Ticket form")
    field(page, "Subject").fill(r.SUBJECT)
    field(page, "What the client asked").fill(ticket.ASKED)
    r.expire_row()
    page.get_by_role("button", name="Log ticket").click()
    assert r.wait_for_sign_in(page) == r.PAGE
    assert KeptDraft.objects.get().values["subject"] == r.SUBJECT
    assert not ServiceRequest.objects.filter(subject=r.SUBJECT).exists()
    sign_in(page, r.STAFF, demo.password, visit=False)
    expect(field(page, "Subject")).to_have_value(r.SUBJECT)
    expect(field(page, "What the client asked")).to_have_value(ticket.ASKED)


def test_confirming_discard_after_edit_focuses_the_first_text_box(page, demo):
    r.open_ticket(page, demo, "direct")
    field(page, "Subject").fill(r.SUBJECT)
    r.expire_row()
    page.clock.fast_forward(30 * 60 * 1000)
    assert r.wait_for_sign_in(page) == r.PAGE
    sign_in(page, r.STAFF, demo.password, visit=False)
    expect(field(page, "Subject")).to_have_value(r.SUBJECT)
    field(page, "Subject").fill("Revised synthetic subject")
    page.get_by_role("link", name="Discard", exact=True).click()
    confirmation = page.locator("#vr-form-discard")
    expect(confirmation).to_be_visible()
    confirmation.get_by_role("button", name="Discard changes", exact=True).click()
    expect(page.locator("[data-vr-kept-notice]")).to_have_count(0)
    expect(field(page, "Subject")).to_have_value("")
    expect(field(page, "Reported by")).to_be_focused()


def test_oversized_restore_keeps_subject_again_when_a_new_marker_is_available(page, demo):
    ticket.typed_new_ticket(page, demo)
    key = int(native_select(field(page, "Ticket form")).input_value())
    ticket.signed_out_by_inactivity(page)
    TemplateField.objects.bulk_create(
        [
            TemplateField(
                template_id=key, key=f"carton-{i}", label=f"Carton {i}", kind=FieldKind.SHORT_TEXT
            )
            for i in range(kept.MAX_FIELDS)
        ]
    )
    sign_in(page, r.STAFF, demo.password, visit=False)
    expect(field(page, "Subject")).to_have_value(ticket.SUBJECT)
    expect(page.locator("[data-vr-kept-notice]")).to_be_visible()
    assert not page.locator("[data-vr-kept]").input_value()
    pick(field(page, "Ticket form"), label="No form")
    expect(page.locator("#category-section").get_by_text("Courier or route")).to_have_count(0)
    expect(page.locator("[data-vr-kept]")).to_have_value(re.compile(".+"))
    # Adopted: the second session ends as the first one did (ticket.signed_out_by_inactivity).
    # r.expire_row recorded its end a minute in the past, before the first session's recorded end,
    # a timeline that cannot happen (a later session ends after an earlier one), and keeping
    # refuses a copy from a session that ended before one already given back in this browser.
    ticket.signed_out_by_inactivity(page)
    assert r.wait_for_sign_in(page) == r.PAGE
    assert list(
        KeptDraft.objects.filter(taken_at__isnull=True).values_list("values__subject", flat=True)
    ) == [ticket.SUBJECT]


def test_previous_release_partial_receipt_does_not_choose_an_unheld_dialog(page, demo):
    # An earlier release of the same endpoint returned only the count. Preserve the real
    # server's status, cookies and decisions, removing only the new response property.
    def old_receipt(route):
        response = route.fetch()
        answer = response.json()
        assert answer["kept"] == 1
        route.fulfill(response=response, json={"kept": answer["kept"]})

    page.route("**/account/session/keep/", old_receipt)
    r.test_partial_keep_returns_to_the_surface_whose_copy_was_held(page, demo)


def test_composer_still_warns_when_only_the_dialog_copy_was_held(page, demo):
    example = ServiceRequest.objects.filter(
        status="open", category__is_restricted=False, team__memberships__user__username=r.STAFF
    ).first()
    listed(page, demo)
    page.goto(reverse("desk:request-detail", args=[example.pk]))
    composer = page.locator("form.vr-composer")
    field(composer, "Message").click()
    field(composer, "Message").fill("C" * (kept.MAX_VALUE + 1))
    composer.evaluate("""form => {
        const opener = document.createElement("a");
        opener.href = "/requests/new/";
        opener.textContent = "Open example ticket form";
        form.before(opener);
    }""")
    page.get_by_role("link", name="Open example ticket form").click()
    field(page.locator("#vr-form-dialog"), "Subject").fill(r.SUBJECT)
    questions = []

    def answer(dialog):
        questions.append(dialog.type)
        dialog.accept()

    page.on("dialog", answer)
    r.expire_row()
    page.clock.fast_forward(30 * 60 * 1000)
    assert r.wait_for_sign_in(page) == r.PAGE
    assert KeptDraft.objects.get().values["subject"] == r.SUBJECT
    assert questions == ["beforeunload"], "The unheld composer still has unsent text"


def test_dialog_guard_releases_only_its_own_form(page, demo):
    r.open_ticket(page, demo, "list")
    field(page, "Subject").fill(r.SUBJECT)
    states = page.locator("#vr-form-content form").first.evaluate("""form => {
        function warned() {
            return !window.dispatchEvent(new Event("beforeunload", {cancelable: true}));
        }
        function receipt(forms) {
            document.dispatchEvent(new CustomEvent("vr:typing-kept", {detail: {kept: 1, forms}}));
        }
        const before = warned();
        receipt([document.createElement("form")]);
        const other = warned();
        receipt([form]);
        return [before, other, warned()];
    }""")
    assert states == [True, True, False]


@pytest.mark.parametrize("surface", ["dialog_load", "list_partial"])
def test_real_htmx_refusal_without_kept_forms_returns_to_the_list(page, demo, surface):
    listed(page, demo)
    assert page.locator("[data-vr-kept]").count() == 0
    sends = []
    page.on(
        "request",
        lambda request: sends.append(request.url) if "/session/keep/" in request.url else None,
    )
    r.expire_row()
    if surface == "dialog_load":
        page.get_by_role("link", name="New ticket", exact=True).first.click()
    else:
        page.evaluate("""() => { htmx.ajax('GET', '/requests/?view=all', {
            source: document.querySelector('main'), target: 'main'
        }); }""")
    assert r.wait_for_sign_in(page) == LIST
    assert sends == [] and not KeptDraft.objects.exists()


def issue_partial(page, body, address=None, again=None):
    # Use the shipped htmx listener and a local, controlled partial endpoint on the current page.
    # Only the response contract under test is substituted. No foreign destination is contacted.
    def answer(route):
        headers = {"Content-Type": "application/json"}
        if address is not None:
            headers["HX-Redirect"] = address
        route.fulfill(status=401, headers=headers, body=json.dumps(body))

    page.route("**/verification-partial/", answer)
    if again:
        page.evaluate(
            """second => document.addEventListener("htmx:beforeOnLoad", event => {
            if (event.detail.xhr.status !== 401) return;
            document.dispatchEvent(new CustomEvent("htmx:beforeOnLoad", {
                cancelable: true, detail: {xhr: {status: 401, responseText: JSON.stringify(second),
                getResponseHeader: () => second.sign_in}, target: document.querySelector("main")}
            }));
        }, {once: true})""",
            again,
        )
    page.evaluate("""() => { htmx.ajax('GET', '/verification-partial/', {
        source: document.querySelector('main'), target: 'main'
    }); }""")


@pytest.mark.parametrize(
    "kind",
    [
        "non_session",
        "status_only",
        "wrong_path",
        "wrong_origin",
        "unparseable",
        "nonlocal_next",
        "twice",
    ],
)
def test_partial_refusal_uses_its_own_local_return(page, demo, kind):
    listed(page, demo)
    if kind in ("non_session", "status_only"):
        result = page.evaluate(
            """kind => {
            const body = kind === "non_session" ? {message: "Unavailable"} : {signed_in: false};
            const xhr = {status: 401, getResponseHeader: () => null,
              responseText: JSON.stringify(body)};
            const event = new CustomEvent("htmx:beforeOnLoad", {cancelable: true,
              detail: {xhr, target: document.querySelector("main")}});
            return document.dispatchEvent(event);
        }""",
            kind,
        )
        assert result, (
            "A non-session partial lacking the application's sign-in refusal was intercepted"
        )
        return
    addresses = {
        "wrong_path": "/requests/new/",
        "wrong_origin": "https://example.invalid/accounts/login/",
        "unparseable": "https://",
        "nonlocal_next": "/accounts/login/?next=about%3Ablank",
        "twice": "/accounts/login/?next=/requests/new/",
    }
    r.expire_row()
    page.route("https://example.invalid/**", lambda route: route.abort())
    address = addresses[kind]
    second = (
        {"signed_in": False, "sign_in": "/accounts/login/?next=/requests/"}
        if kind == "twice"
        else None
    )
    issue_partial(page, {"signed_in": False, "sign_in": address}, address, second)
    landing = r.wait_for_sign_in(page)
    expect(page).to_have_url(re.compile(r"/accounts/login/"))
    assert landing == (r.PAGE if kind == "twice" else LIST)
    assert urlsplit(landing).scheme == ""


def test_refusal_from_picker_replaced_while_loading_still_keeps_the_ticket(page, demo):
    """The old dialog's section answer arrives after a new ticket has been started."""
    r.open_ticket(page, demo, "list")
    field(page, "Subject").fill(r.SUBJECT)
    category = field(page, "Category")
    pick(category, label="Pickup or missed pickup")
    expect(field(page, "Ticket form")).to_be_enabled()
    pending = []

    def delay_picker(route):
        if not pending and "request_form=" in route.request.url:
            pending.append(route)
        else:
            route.continue_()

    page.route("**/requests/new/guidance/**", delay_picker)
    pick(field(page, "Ticket form"), label="Pickup")
    assert len(pending) == 1
    old_picker = native_select(field(page, "Ticket form")).element_handle()
    dialog = page.locator("#vr-form-dialog")
    dialog.locator("[data-vr-form-close]").click()
    page.locator("#vr-form-discard").get_by_role(
        "button", name="Discard changes", exact=True
    ).click()
    expect(dialog).to_be_hidden()
    page.wait_for_function("picker => !picker.isConnected", arg=old_picker)
    page.get_by_role("link", name="New ticket", exact=True).first.click()
    field(page, "Subject").click()
    field(page, "Subject").fill("Second ticket for the sample shipment")
    page.on("dialog", lambda question: question.accept())
    r.expire_row()
    with page.expect_response(lambda resp: resp.url == pending[0].request.url) as refusal:
        pending[0].continue_()
    assert refusal.value.status == 401
    destination = r.wait_for_sign_in(page)
    assert list(KeptDraft.objects.values_list("values__subject", flat=True)) == [
        "Second ticket for the sample shipment"
    ]
    assert destination == r.PAGE


def test_refused_dialog_post_without_a_held_copy_still_warns(page, demo):
    r.open_ticket(page, demo, "list")
    pick(field(page, "Category"), label="Pickup or missed pickup")
    expect(page.locator("#category-section")).to_contain_text("Ticket form")
    field(page, "Subject").fill(r.SUBJECT)
    field(page, "What the client asked").fill("R" * (kept.MAX_VALUE + 1))
    questions = []

    def proceed(dialog):
        questions.append(dialog.type)
        dialog.accept()

    page.on("dialog", proceed)
    r.expire_row()
    page.get_by_role("button", name="Log ticket").click()
    r.wait_for_sign_in(page)
    assert not KeptDraft.objects.exists()
    assert questions == ["beforeunload"], "The failed post and keep left unsaved text in the dialog"


def test_status_answer_while_an_expired_dialog_post_is_pending_returns_to_held_form(
    page, demo, monkeypatch
):
    from accounts import sessions

    r.open_ticket(page, demo, "list")
    pick(field(page, "Category"), label="Pickup or missed pickup")
    expect(page.locator("#category-section")).to_contain_text("Ticket form")
    field(page, "Subject").fill(r.SUBJECT)
    field(page, "What the client asked").fill(ticket.ASKED)
    receipt_ready, release = Event(), Event()
    original = sessions._refusal

    def slower_post(request):
        response = original(request)
        if request.method == "POST" and request.path == r.PAGE:
            receipt_ready.set()
            release.wait(timeout=8)
        return response

    monkeypatch.setattr(sessions, "_refusal", slower_post)
    r.expire_row()
    try:
        page.get_by_role("button", name="Log ticket").click()
        assert receipt_ready.wait(4)
        assert KeptDraft.objects.get().values["subject"] == r.SUBJECT
        with page.expect_response(
            lambda resp: urlsplit(resp.url).path == "/account/session/"
        ) as status:
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        assert status.value.status == 200
        # The status reply gets one turn to run before the POST receipt arrives.
        page.wait_for_timeout(100)
    finally:
        release.set()
    assert r.wait_for_sign_in(page) == r.PAGE


def test_link_panel_refusal_keeps_the_ticket_composer(page, demo):
    from test_links_journey import PATIENT_KEY, demo_request, grant_patient_capability

    from accounts.models import User
    from links.models import ExternalSystem
    from links.services import add_link

    actor = User.objects.get(username=r.STAFF)
    grant_patient_capability(actor)
    subject_ticket = demo_request()
    add_link(
        actor=actor,
        request_id=subject_ticket.pk,
        system=ExternalSystem.objects.get(name="Example LIS"),
        record_type="patient",
        identifier=PATIENT_KEY,
    )
    address = reverse("desk:request-detail", args=[subject_ticket.pk])
    page.clock.install()
    sign_in(page, r.STAFF, demo.password)
    page.goto(address)
    field(page.locator("form.vr-composer"), "Message").fill(r.SUBJECT)
    reveal = page.locator("#links").get_by_role("button", name="Reveal key")
    expect(reveal).to_be_visible()
    r.expire_row()
    with page.expect_response(lambda response: "/reveal/" in response.url) as response:
        reveal.click()
    assert response.value.status == 401 and r.wait_for_sign_in(page) == address
    assert KeptDraft.objects.get().values["body"] == r.SUBJECT


def test_short_receipt_maps_only_to_the_forms_it_acknowledges(page, demo):
    r.open_ticket(page, demo, "list")
    field(page, "Subject").fill(r.SUBJECT)
    # The server caps a batch. Add earlier forms to the document so that the real dialog
    # form is just beyond that cap, then check both consumers against the same receipt.
    result = page.evaluate(
        """limit => {
        const api = window.vrKeptTyping;
        const marker = document.querySelector("#vr-form-content input[data-vr-kept]");
        const earlier = [];
        for (let i = 0; i < limit; i++) {
            const form = document.createElement("form");
            const subject = document.createElement("input");
            subject.name = "subject";
            subject.value = "Example parcel " + i;
            const copy = marker.cloneNode();
            copy.setAttribute("data-vr-kept-bound", "");
            form.append(subject, copy);
            document.body.insertBefore(form, document.body.firstChild);
            earlier.unshift(form);
        }
        const batch = api.collect({commit: true});
        const held = earlier.map((_, index) => index === 0);
        const clipped = api.heldForms(held);
        const clippedPath = api.returnTo(held);
        const extended = held.concat(true);
        return [batch.length, clipped.length, clipped[0] === earlier[0], clippedPath,
            api.heldForms(extended).at(-1) === marker.form, api.returnTo(extended)];
    }""",
        kept.MAX_FORMS,
    )
    assert result == [kept.MAX_FORMS + 1, 1, True, None, True, r.PAGE]


def test_legacy_single_form_receipt_reopens_the_held_dialog(page, demo):
    r.open_ticket(page, demo, "list")
    field(page, "Subject").fill(r.SUBJECT)

    def count_only(route):
        response = route.fetch()
        assert response.json()["kept"] == 1
        route.fulfill(response=response, json={"kept": 1})

    page.route("**/account/session/keep/", count_only)
    r.expire_row()
    page.clock.fast_forward(30 * 60 * 1000)
    assert r.wait_for_sign_in(page) == r.PAGE
    sign_in(page, r.STAFF, demo.password, visit=False)
    expect(field(page, "Subject")).to_have_value(r.SUBJECT)
