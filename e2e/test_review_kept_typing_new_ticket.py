"""Independent New ticket journeys with real Chromium controls and a synthetic laboratory."""

import re
from datetime import timedelta
from threading import Event
from urllib.parse import parse_qs, urlsplit

import pytest
from django.db.models.functions import Now
from helpers import field, pick, sign_in
from playwright.sync_api import expect

from accounts import session_views
from accounts.models import AccountSession, KeptDraft

pytestmark = pytest.mark.django_db(transaction=True)
PAGE = "/requests/new/"
STAFF = "demo_client_services"
SUBJECT = "Collect the example cartons after lunch"


def open_ticket(page, demo, surface):
    page.clock.install()
    sign_in(page, STAFF, demo.password)
    if surface == "list":
        page.goto("/requests/?view=all")
        page.get_by_role("link", name="New ticket", exact=True).first.click()
    else:
        page.goto(PAGE)
    expect(field(page, "Subject")).to_be_enabled()


def expire_row():
    changed = AccountSession.objects.filter(user__username=STAFF, ended_at__isnull=True).update(
        last_active_at=Now() - timedelta(minutes=31)
    )
    assert changed == 1


def wait_for_sign_in(page):
    expect(page).to_have_url(re.compile(r"/accounts/login/"), timeout=15_000)
    return parse_qs(urlsplit(page.url).query).get("next", [None])[0]


@pytest.mark.parametrize("surface", ["list", "direct"])
def test_pristine_and_reverted_fields_send_nothing_but_typing_survives_a_section_swap(
    page, demo, surface
):
    open_ticket(page, demo, surface)
    collect = "() => window.vrKeptTyping.collect({commit: true})"
    assert page.evaluate(collect) == []
    subject = field(page, "Subject")
    subject.fill(SUBJECT)
    copies = page.evaluate(collect)
    assert [copy["values"]["subject"] for copy in copies] == [SUBJECT]
    subject.fill("")
    assert page.evaluate(collect) == []
    subject.fill(SUBJECT)
    pick(field(page, "Category"), label="Pickup or missed pickup")
    expect(page.locator("#category-section")).to_contain_text("Ticket form")
    assert [copy["values"]["subject"] for copy in page.evaluate(collect)] == [SUBJECT]
    assert not page.console_errors


@pytest.mark.parametrize("surface", ["list", "direct"])
def test_clock_expiry_with_a_held_copy_goes_to_sign_in_without_an_unload_question(
    page, demo, surface
):
    open_ticket(page, demo, surface)
    questions = []

    def answer(dialog):
        questions.append(dialog.type)
        dialog.accept()

    page.on("dialog", answer)
    field(page, "Subject").click()  # activate native navigation protection, as a person would
    field(page, "Subject").fill(SUBJECT)
    expire_row()
    page.clock.fast_forward(30 * 60 * 1000)
    assert wait_for_sign_in(page) == PAGE
    assert KeptDraft.objects.get().values["subject"] == SUBJECT
    assert questions == [], f"After holding the copy Chromium still asked {questions}"


@pytest.mark.parametrize("surface", ["list", "direct"])
def test_expired_category_refresh_preserves_text_entered_before_the_refresh(
    page, demo, surface, monkeypatch
):
    open_ticket(page, demo, surface)
    field(page, "Subject").fill(SUBJECT)
    field(page, "Category").click()
    option = page.get_by_role("option", name="Pickup or missed pickup", exact=True)
    expect(option).to_be_visible()
    # Both responses are real. Let the section's 401 arrive first, as on a connection
    # where the subsequent status answer is slower. Also allow a client to await that answer.
    navigation = Event()
    answer = session_views._answer

    def slower_status(request):
        response = answer(request)
        if request.method == "GET" and not request.user.is_authenticated:
            navigation.wait(timeout=1)
        return response

    monkeypatch.setattr(session_views, "_answer", slower_status)
    page.on(
        "framenavigated",
        lambda frame: navigation.set() if "/accounts/login/" in frame.url else None,
    )
    expire_row()
    with page.expect_response(lambda response: "/new/guidance/" in response.url) as refresh:
        option.click()
    assert refresh.value.status == 401
    landing = wait_for_sign_in(page)
    sign_in(page, STAFF, demo.password, visit=False)
    if urlsplit(page.url).path != PAGE:
        page.goto(PAGE)
    expect(field(page, "Subject")).to_have_value(SUBJECT)
    assert landing == PAGE


def test_expired_clean_dialog_returns_to_the_list_without_a_keep_post(page, demo):
    open_ticket(page, demo, "list")
    sends = []
    page.on("request", lambda req: sends.append(req.url) if "/session/keep/" in req.url else None)
    expire_row()
    page.clock.fast_forward(30 * 60 * 1000)
    assert wait_for_sign_in(page) == "/requests/?view=all"
    assert sends == [] and not KeptDraft.objects.exists()


def test_partial_keep_returns_to_the_surface_whose_copy_was_held(page, demo):
    """Compose two supported forms in one document to exercise the batch receipt.
    The stock ticket detail has a composer. This fixture adds only a link to the registered
    New ticket dialog. Both forms, their keep markers and the server's receipt are real.
    """
    from django.urls import reverse

    from accounts import kept
    from desk.models import ServiceRequest

    ticket = ServiceRequest.objects.filter(
        status="open", category__is_restricted=False, team__memberships__user__username=STAFF
    ).first()
    assert ticket is not None
    address = reverse("desk:request-detail", args=[ticket.pk])
    page.clock.install()
    sign_in(page, STAFF, demo.password)
    page.goto(address)
    composer = page.locator("form.vr-composer")
    field(composer, "Message").fill("Collect the example boxes.")
    composer.evaluate(
        """form => {
            const opener = document.createElement("a");
            opener.href = "/requests/new/";
            opener.textContent = "Open example ticket form";
            form.before(opener);
        }"""
    )
    page.get_by_role("link", name="Open example ticket form").click()
    field(page.locator("#vr-form-dialog"), "What the client asked").fill("X" * (kept.MAX_VALUE + 1))
    expire_row()
    page.clock.fast_forward(30 * 60 * 1000)
    destination = wait_for_sign_in(page)
    held = KeptDraft.objects.get()
    assert (
        held.form == f"ticket-entry-{ticket.pk}"
        and held.values["body"] == "Collect the example boxes."
    )
    assert destination == address, "Only the background ticket had a copy held"


def test_discard_of_a_restored_ticket_focuses_its_first_visible_text_box(page, demo):
    open_ticket(page, demo, "direct")
    field(page, "Subject").fill(SUBJECT)
    expire_row()
    page.clock.fast_forward(30 * 60 * 1000)
    assert wait_for_sign_in(page) == PAGE
    sign_in(page, STAFF, demo.password, visit=False)
    expect(field(page, "Subject")).to_have_value(SUBJECT)
    page.locator("[data-vr-kept-discard]").click()
    expect(page.locator("[data-vr-kept-notice]")).to_have_count(0)
    expect(field(page, "Subject")).to_have_value("")
    assert page.evaluate("window.vrKeptTyping.collect()") == []
    expect(field(page, "Reported by")).to_be_focused()
