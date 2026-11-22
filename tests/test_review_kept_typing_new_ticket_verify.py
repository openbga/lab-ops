"""Independent follow-up cases for the New ticket review, using synthetic records."""

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest
from django.db import OperationalError, connection, transaction
from django.urls import reverse

from accounts import kept
from accounts.models import KeptDraft
from request_forms import intake
from request_forms.models import FieldKind, RequestTemplate
from tests import test_review_kept_typing as previous
from tests import test_review_kept_typing_new_ticket as r

pytestmark = pytest.mark.django_db
agent = r.agent
make_user = r.make_user
group = r.group
logistics = r.logistics
pickup_category = r.pickup_category
staff_category = r.staff_category
catalog = r.catalog
pickup = r.pickup
clinic = r.clinic
other_org = r.other_org
review_form_cases = previous.review_form_cases


def test_exact_answer_survives_a_question_rename(agent, pickup):
    browser = r.pending(agent, pickup)
    pickup.fields.filter(key="count").update(label="Number of return labels")
    page = browser.get(r.PAGE)
    assert str(page.context["intake_drawn"][1]["field-count"].value()) == "3"
    assert not r.missed(page)


def test_checkbox_absent_at_keep_does_not_reappear_on_a_new_requirement(agent, pickup):
    browser = r.pages.browser_for(agent)
    token = r.drawn_marker(r.section(browser, pickup))[0]
    pickup.fields.update(required=False)
    assert 'name="details_not_known"' not in r.section(browser, pickup).text
    r.pages.end_open(agent)
    assert r.pages.send(browser, token, r.draft_values(pickup, details_not_known="on")) == 1
    pickup.fields.create(
        key="boxes", label="Packing boxes", kind=FieldKind.SHORT_TEXT, required=True
    )
    browser.force_login(agent)
    page = browser.get(r.PAGE)
    assert r.SUBJECT in page.text and "Packing boxes" in page.text
    box = re.search(r'<input\b[^>]*name="details_not_known"[^>]*>', page.text)
    assert box and " checked" not in box[0]


def spare_form(pickup):
    return RequestTemplate.objects.create(
        key="spare", name="Alternate form", category=pickup.category
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["retired", "moved", "withdrawn"])
@pytest.mark.parametrize("surface", ["section", "refused_post"])
def test_picker_and_definition_agree_across_setup_edit(
    agent, pickup, clinic, other_org, staff_category, monkeypatch, change, surface
):
    spare_form(pickup)
    browser = r.pages.browser_for(agent)
    initial = r.section(browser, pickup, client=clinic.pk)
    data = r.draft_values(
        pickup,
        subject="",
        client=str(clinic.pk),
        category_terms=r.hidden(initial, "category_terms"),
        form_definition=r.hidden(initial, "form_definition"),
    )

    def edit():
        if change == "withdrawn":
            pickup.clients.set([other_org])
        else:
            changes = {"is_active": False} if change == "retired" else {"category": staff_category}
            RequestTemplate.objects.filter(pk=pickup.pk).update(**changes)

    # Both paths have now read and accepted the old definition. Commit the setup edit
    # before the slot constructs its final choices or the refused page draws the bound picker.
    calls = []
    target, name = (
        (intake.Intake, "is_valid") if surface == "refused_post" else (intake, "RequestFormPicker")
    )
    original = getattr(target, name)

    def with_edit(*args, **kwargs):
        if not calls:
            calls.append(True)
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(r.in_worker, edit).result(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, with_edit)
    page = (
        browser.post(r.PAGE, data)
        if surface == "refused_post"
        else r.section(browser, pickup, client=clinic.pk)
    )
    assert calls and page.status_code == 200
    if surface == "refused_post":
        assert page.context["form"].has_error("subject") and not r.ServiceRequest.objects.exists()
    chosen = r.selected(page, "request_form") == str(pickup.pk)
    offered_before_edit = (
        chosen and 'name="field-count"' in page.text and 'name="form_definition"' in page.text
    )
    omitted_after_edit = (
        not chosen
        and 'name="field-count"' not in page.text
        and 'name="form_definition"' not in page.text
    )
    if change == "withdrawn" and offered_before_edit:
        option = re.search(
            rf'<option[^>]+value="{pickup.pk}"[^>]*>([^<]+)</option>',
            page.text[page.text.index('name="request_form"') :],
        )
        assert option and "selected organizations only" not in option[1], (
            "The picker selected a newly restricted form for an excluded organization"
        )
    assert offered_before_edit or omitted_after_edit, "The picker and the signed fields disagree"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("direction", ["one_to_two", "two_to_one"])
def test_automatic_selection_uses_the_offers_actually_shown(agent, pickup, monkeypatch, direction):
    spare = spare_form(pickup)
    if direction == "one_to_two":
        RequestTemplate.objects.filter(pk=spare.pk).update(is_active=False)
    original = intake.chosen_template

    def choose(*args, **kwargs):
        chosen = original(*args, **kwargs)
        RequestTemplate.objects.filter(pk=spare.pk).update(is_active=direction == "one_to_two")
        return chosen

    monkeypatch.setattr(intake, "chosen_template", choose)
    browser = r.pages.browser_for(agent)
    page = browser.get(r.SECTION, {"category": pickup.category_id}, headers={"HX-Request": "true"})
    spare_offered = "Alternate form" in page.text
    expected = "" if spare_offered else str(pickup.pk)
    assert r.selected(page, "request_form") == expected
    assert ('name="form_definition"' in page.text) == bool(expected)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("remove_role", [False, True])
def test_real_serialization_retry_rereads_account_and_rolls_back_take(
    agent, pickup, monkeypatch, remove_role
):
    browser = r.pending(agent, pickup)
    original = kept.take
    takes = []

    def edit():
        with transaction.atomic():
            RequestTemplate.objects.filter(pk=pickup.pk).update(name="Updated pickup form")
            if remove_role:
                agent.groups.clear()

    def take(*args, **kwargs):
        copy = original(*args, **kwargs)
        takes.append(copy is not None)
        if len(takes) == 1:
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(r.in_worker, edit).result(timeout=10)
        return copy

    monkeypatch.setattr(kept, "take", take)
    page = browser.get(r.PAGE)
    if remove_role:
        assert page.status_code == 403 and takes == [True]
        assert KeptDraft.objects.get().taken_at is None and r.SUBJECT not in page.text
    else:
        assert page.status_code == 200 and takes == [True, True]
        assert r.ROUTE in page.text and "Updated pickup form" in page.text
        assert KeptDraft.objects.get().taken_at is not None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(("state", "attempts"), [("40001", 2), ("55P03", 1)])
def test_draw_retries_once_only_for_serialization(agent, pickup, monkeypatch, state, attempts):
    browser = r.pending(agent, pickup)
    original = kept.take
    takes = []

    def refuse(*args, **kwargs):
        takes.append(original(*args, **kwargs) is not None)
        with connection.cursor() as cursor:
            cursor.execute(
                "DO $$ BEGIN RAISE EXCEPTION 'Synthetic draw refusal' "
                f"USING ERRCODE = '{state}'; END $$"
            )

    with monkeypatch.context() as patch:
        patch.setattr(kept, "take", refuse)
        with pytest.raises(OperationalError) as exc:
            browser.get(r.PAGE)
    assert exc.value.__cause__.sqlstate == state and takes == [True] * attempts
    assert KeptDraft.objects.get().taken_at is None and r.ROUTE in browser.get(r.PAGE).text


@pytest.mark.parametrize("surface", ["staff_entry", "portal_reply", "portal_form"])
def test_referrerless_held_post_returns_to_usable_form(review_form_cases, surface):
    case = review_form_cases
    if surface == "staff_entry":
        user, key = case.agent, case.ticket.pk
        action = reverse("desk:request-entry", args=[key])
        page = reverse("desk:request-detail", args=[key])
        data = {"body": r.ROUTE, "kind": "note"}
    elif surface == "portal_reply":
        user, key = case.portal, case.ticket.number
        action = reverse("portal:request-reply", args=[key])
        page = reverse("portal:request-detail", args=[key])
        data = {"body": r.ROUTE}
    else:
        user = case.portal
        action = page = reverse("portal_forms:raise", args=[case.template.key])
        data = {"description": r.ROUTE}
    browser = r.pages.browser_for(user)
    token = r.drawn_marker(browser.get(page))[0]
    previous._review_expire(user)
    refusal = browser.post(action, {**data, kept.FIELD: token})
    assert parse_qs(urlsplit(refusal.url).query)["next"] == [action]
    browser.force_login(user)
    restored = browser.get(action, follow=True)
    assert restored.status_code == 200 and r.ROUTE in restored.text
    assert KeptDraft.objects.get().taken_at is not None


def test_capped_receipt_preserves_failed_slots(agent, pickup):
    browser = r.pages.browser_for(agent)
    token = r.drawn_marker(r.section(browser, pickup))[0]
    valid = {"token": token, "values": r.draft_values(pickup)}
    batch = [None] * (kept.MAX_FORMS - 1) + [valid, valid]
    r.pages.end_open(agent)
    receipt = browser.post(reverse("account-session-keep"), {"forms": batch}, "application/json")
    assert receipt.json() == {"kept": 1, "held": [False] * (kept.MAX_FORMS - 1) + [True]}
    assert KeptDraft.objects.get().values["subject"] == r.SUBJECT


def test_new_ticket_reads_assignment_options_once_without_a_retry(agent, monkeypatch):
    from desk import forms

    browser = r.pages.browser_for(agent)
    reads = []
    original = forms.assign_choices

    def assigned(*args, **kwargs):
        choices = original(*args, **kwargs)
        reads.append(choices)
        return choices

    monkeypatch.setattr(forms, "assign_choices", assigned)
    assert browser.get(r.PAGE).status_code == 200
    assert len(reads) == 1, "Only one assignment list is used on the rendered page"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("per_request", [False, True])
def test_setup_can_finish_while_section_is_being_drawn(agent, pickup, monkeypatch, per_request):
    from queue import Queue
    from time import monotonic, sleep

    browser = r.pages.browser_for(agent)
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", per_request)
    original = intake.ValuesForm
    blocked = []
    pids = Queue()
    changes = []

    def updated():
        return RequestTemplate.objects.filter(pk=pickup.pk).update(name="New pickup label")

    with ThreadPoolExecutor(max_workers=1) as executor:

        def draw(*args, **kwargs):
            form = original(*args, **kwargs)
            future = executor.submit(r.in_worker, updated, pids)
            changes.append(future)
            pid = pids.get(timeout=5)
            deadline = monotonic() + 5
            with connection.cursor() as cursor:
                while not future.done():
                    cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
                    if cursor.fetchone()[0]:
                        blocked.append(True)
                        break
                    assert monotonic() < deadline
                    sleep(0.01)
            return form

        monkeypatch.setattr(intake, "ValuesForm", draw)
        assert r.section(browser, pickup).status_code == 200
    assert len(changes) == 1 and changes[0].result() == 1
    assert not blocked, "The section held the template lock while its fields were drawn"


@pytest.mark.django_db(transaction=True)
def test_selected_label_and_instructions_describe_one_form_revision(agent, pickup, monkeypatch):
    before = {"name": "Carton booking", "instructions": "Enter the carton count."}
    after = {"name": "Route booking", "instructions": "Enter the route identifier."}
    target = RequestTemplate.objects.filter(pk=pickup.pk)
    target.update(**before)
    original = intake.ValuesForm

    def draw(*args, **kwargs):
        form = original(*args, **kwargs)
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert (
                executor.submit(r.in_worker, lambda: target.update(**after)).result(timeout=10) == 1
            )
        return form

    monkeypatch.setattr(intake, "ValuesForm", draw)
    page = r.section(r.pages.browser_for(agent), pickup)
    assert r.selected(page, "request_form") == str(pickup.pk)
    assert any(
        state["name"] in page.text and state["instructions"] in page.text
        for state in (before, after)
    )


def test_completed_login_uses_a_local_destination_for_an_inert_nonlocal_next(client, agent):
    path = reverse("account_login") + "?next=about%3Ablank"
    response = client.post(
        path,
        {"login": agent.username, "password": r.forms_fixtures.PASSWORD, "next": "about:blank"},
    )
    assert response.status_code == 302
    location = urlsplit(response.url)
    assert not location.scheme and not location.netloc and location.path.startswith("/")
