"""Round-one regressions for staff New ticket, written by the independent reviewer
(.local/review-history/kept-typing-new-ticket-round-1.md) and adopted; cases changed on adoption
say so ("Adopted:"). All tickets and accounts are synthetic."""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from queue import Queue
from threading import Event
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.models.functions import Now
from django.test import Client as Browser
from django.urls import reverse

from accounts import kept
from accounts.models import AccountSession, KeptDraft
from accounts.roles import ROLES
from attachments.models import AttachmentBlob
from clients.models import ClientContact
from core.models import AuditEvent
from desk import views
from desk.models import ServiceRequest
from desk.tests import test_kept_typing as pages
from jobs.models import Notice
from request_forms.models import FieldKind, RequestTemplate
from request_forms.tests import conftest as forms_fixtures
from tests.test_review_kept_typing_verify import missed
from tests.test_review_session_list_verify import in_worker

pytestmark = pytest.mark.django_db
agent = forms_fixtures.agent
capable = forms_fixtures.capable
make_user = forms_fixtures.make_user
clinic = forms_fixtures.clinic
clinic_site = forms_fixtures.clinic_site
other_org = forms_fixtures.other_org
group = forms_fixtures.group
pickup_category = forms_fixtures.pickup_category
logistics = forms_fixtures.logistics
staff_category = forms_fixtures.staff_category
catalog = forms_fixtures.catalog
pickup = forms_fixtures.pickup

PAGE = reverse("desk:request-create")
SECTION = reverse("desk:category-guidance")
SUBJECT = "Collect the demonstration cartons"
ROUTE = "Use the demonstration loading door"


def section(browser, pickup, **choices):
    return browser.get(
        SECTION,
        {"category": pickup.category_id, "request_form": pickup.pk, **choices},
        headers={"HX-Request": "true"},
    )


def draft_values(pickup, **changes):
    return {
        "category": str(pickup.category_id),
        "subject": SUBJECT,
        "body": "The carton is ready.",
        "channel": "phone",
        "first_entry_kind": "note",
        "request_form": str(pickup.pk),
        "field-count": "3",
        "field-courier": ROUTE,
        **changes,
    }


def drawn_marker(response):
    assert response.status_code == 200
    marker = pages.marker(response)
    assert marker is not None, "The rendered New ticket form has no keep marker"
    return marker


def pending(user, pickup, **changes):
    browser = pages.browser_for(user)
    token = drawn_marker(section(browser, pickup))[0]
    pages.end_open(user)
    assert pages.send(browser, token, draft_values(pickup, **changes)) == 1
    browser.force_login(user)
    return browser


def selected(response, name):
    """The value the browser would submit for this single select, from the delivered HTML."""
    select = re.search(rf'<select\b[^>]*name="{name}"[^>]*>(.*?)</select>', response.text, re.S)
    if not select:
        return None
    options = re.findall(r'<option\b[^>]*value="([^"]*)"([^>]*)>', select[1])
    return next((value for value, attrs in options if " selected" in attrs), options[0][0])


def hidden(response, name):
    match = re.search(rf'<input\b[^>]*name="{name}"[^>]*value="([^"]+)"', response.text)
    assert match is not None, f"Missing hidden field {name}"
    return match[1]


@contextmanager
def retire_after_read(monkeypatch, target, method, record):
    """Commit an ordinary setup edit after the chosen read. If the reader locks the record,
    let the reader finish first. Both serial orders are valid for the rendered page."""
    original = getattr(target, method)
    workers = []
    with ThreadPoolExecutor(max_workers=1) as pool, monkeypatch.context() as patch:

        def read_then_retire(*args, **kwargs):
            result = original(*args, **kwargs)
            if workers:
                return result
            pids = Queue()
            worker = pool.submit(
                in_worker,
                lambda: type(record).objects.filter(pk=record.pk).update(is_active=False),
                pids,
            )
            workers.append(worker)
            pid = pids.get(timeout=5)
            deadline = time.monotonic() + 5
            with connection.cursor() as cursor:
                while not worker.done():
                    cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
                    if cursor.fetchone()[0]:
                        break
                    assert time.monotonic() < deadline, "Setup edit neither committed nor waited"
                    time.sleep(0.01)
            return result

        patch.setattr(target, method, read_then_retire)
        yield
    assert len(workers) == 1 and workers[0].result() == 1


@pytest.mark.parametrize("role", [*ROLES, None, "portal"])
def test_direct_addresses_require_the_staff_role_and_the_typists_account(
    agent, pickup, clinic, make_user, role
):
    browser = pending(agent, pickup)
    if role == "portal":
        other = make_user("review_portal", client=clinic)
    else:
        other = make_user("review_staff", *([role] if role else []))
    browser.force_login(other)  # same keep cookie, different signed-in person
    for path in (PAGE, SECTION):
        response = browser.get(path, {"category": pickup.category_id})
        assert response.status_code == (200 if role in ROLES else 403)
        assert SUBJECT not in response.text and ROUTE not in response.text
        assert KeptDraft.objects.get().taken_at is None
    browser.force_login(agent)
    assert SUBJECT in browser.get(PAGE).text


def test_second_browser_partial_head_prefetch_refusal_and_other_form_leave_the_copy(
    agent, pickup, clinic
):
    owner = pending(agent, pickup)
    second = pages.browser_for(agent)
    responses = [
        second.get(PAGE),
        owner.get(SECTION, {"category": pickup.category_id}),
        section(owner, pickup),
        owner.head(PAGE),
        owner.get(PAGE, headers={"X-VR-Form": "1"}),
        owner.get(PAGE, headers={"Sec-Purpose": "prefetch"}),
        owner.post(PAGE, {"body": "Correct a missing subject"}),
    ]
    ticket = pages.ticket.__wrapped__(agent, pickup.category, clinic)
    responses.append(owner.get(pages.ticket_page(ticket)))
    assert "Correct a missing subject" in responses[-2].text
    for response in responses:
        assert response.status_code == 200 and SUBJECT not in response.text
        assert "no-store" in response["Cache-Control"]
    assert KeptDraft.objects.get().taken_at is None
    assert SUBJECT in owner.get(PAGE).text
    assert SUBJECT not in owner.get(PAGE).text


@pytest.mark.parametrize("change", ["deactivate", "delete"])
@pytest.mark.parametrize("field", ["client", "site", "contact"])
def test_changed_remote_record_is_left_out_with_a_notice(agent, pickup, clinic, field, change):
    record = clinic
    if field == "site":
        record = clinic.sites.create(name="Depot")
    elif field == "contact":
        record = ClientContact.objects.create(
            client=clinic, name="Avery Example", email="avery@example.invalid"
        )
    browser = pending(agent, pickup, **{field: str(record.pk)})
    if change == "deactivate":
        type(record).objects.filter(pk=record.pk).update(is_active=False)
    else:
        record.delete()
    page = browser.get(PAGE)
    assert selected(page, field) == "" and missed(page) and SUBJECT in page.text


@pytest.mark.parametrize("field", ["site", "contact"])
def test_a_record_of_another_organization_still_requires_correction_on_submit(
    agent, pickup, clinic, other_org, field
):
    record = (
        other_org.sites.create(name="Other depot")
        if field == "site"
        else ClientContact.objects.create(
            client=other_org, name="Other caller", email="other@example.invalid"
        )
    )
    values = {"client": str(clinic.pk), field: str(record.pk)}
    browser = pending(agent, pickup, **values)
    page = browser.get(PAGE)
    # Both are active in the field's staff directory. Saving must check the pair.
    assert selected(page, field) == str(record.pk)
    response = browser.post(
        PAGE,
        draft_values(
            pickup,
            **values,
            category_terms=hidden(page, "category_terms"),
            form_definition=hidden(page, "form_definition"),
        ),
    )
    assert "Choose one of the organization" in str(response.context["form"].errors[field])
    assert not ServiceRequest.objects.exists()


def test_children_of_an_inactive_organization_follow_each_fields_own_records(
    agent, pickup, clinic, clinic_site
):
    contact = ClientContact.objects.create(
        client=clinic, name="Avery Example", email="avery@example.invalid"
    )
    browser = pending(
        agent, pickup, client=str(clinic.pk), site=str(clinic_site.pk), contact=str(contact.pk)
    )
    type(clinic).objects.filter(pk=clinic.pk).update(is_active=False)
    page = browser.get(PAGE)
    assert selected(page, "client") == "" and missed(page)
    for name, record, lookup in (
        ("site", clinic_site, "desk-sites"),
        ("contact", contact, "desk-contacts"),
    ):
        assert browser.get(reverse("lookup", args=[lookup])).json()["results"] == []
        still_in_field = page.context["form"].fields[name].queryset.filter(pk=record.pk).exists()
        assert selected(page, name) == (str(record.pk) if still_in_field else "")


@pytest.mark.parametrize("change", ["retire", "group", "move_form", "withdraw_offer", "restrict"])
def test_category_and_form_changes_are_reflected_in_the_restored_section(
    agent, pickup, clinic, other_org, staff_category, change
):
    browser = pending(agent, pickup, client=str(clinic.pk))
    cat = pickup.category
    if change == "retire":
        type(cat).objects.filter(pk=cat.pk).update(is_active=False)
    elif change == "group":
        type(cat).objects.filter(pk=cat.pk).update(parent=None)
    elif change == "move_form":
        RequestTemplate.objects.filter(pk=pickup.pk).update(category=staff_category)
    elif change == "withdraw_offer":
        pickup.clients.set([other_org])
    else:
        type(cat).objects.filter(pk=cat.pk).update(is_restricted=True)
    page = browser.get(PAGE)
    assert SUBJECT in page.text
    if change == "restrict":
        assert "Restricted: only its team" in page.text
        assert selected(page, "category") == str(cat.pk)
        assert ROUTE in page.text and not missed(page)
    else:
        assert selected(page, "category") == ("" if change in ("retire", "group") else str(cat.pk))
        assert ROUTE not in page.text and "Number of coolers" not in page.text
        assert selected(page, "request_form") in (None, "") and missed(page)


def test_switched_picker_starts_the_other_forms_required_details_box_clear(agent, pickup):
    other = RequestTemplate.objects.create(
        key="dispatch", name="Dispatch", category=pickup.category
    )
    other.fields.create(
        key="destination", label="Destination", kind=FieldKind.SHORT_TEXT, required=True
    )
    browser = pending(agent, pickup, request_form=str(other.pk), details_not_known="on")
    page = browser.get(PAGE)
    assert selected(page, "request_form") == str(other.pk)
    assert "Destination" in page.text and "Number of coolers" not in page.text
    checkbox = re.search(r'<input\b[^>]*name="details_not_known"[^>]*>', page.text)
    assert checkbox and " checked" not in checkbox[0], "The previous form's checkbox carried over"


def test_unchanged_form_restores_items_and_unknown_details_and_omits_retired_quantity(
    agent, pickup, catalog
):
    kept_item, retired = catalog.items.filter(is_active=True).order_by("pk")
    first, second = f"field-items_{kept_item.pk}", f"field-items_{retired.pk}"
    browser = pending(agent, pickup, details_not_known="on", **{first: "5", second: "3"})
    catalog.items.filter(pk=retired.pk).update(is_active=False)
    page = browser.get(PAGE)
    assert re.search(rf'<input\b(?=[^>]*name="{first}")(?=[^>]*value="5")[^>]*>', page.text)
    assert second not in page.text and missed(page)
    assert re.search(r'<input\b(?=[^>]*name="details_not_known")(?=[^>]*checked)[^>]*>', page.text)


@pytest.mark.parametrize("user_fixture", ["agent", "capable"])
@pytest.mark.parametrize("when", ["before_keep", "before_return"])
def test_reclassified_answer_is_omitted_for_either_capability(request, pickup, user_fixture, when):
    user = request.getfixturevalue(user_fixture)
    browser = pages.browser_for(user)
    token, _, _ = drawn_marker(section(browser, pickup))
    field = pickup.fields.filter(key="courier")
    pages.end_open(user)
    if when == "before_keep":
        field.update(holds_patient_information=True)
    assert pages.send(browser, token, draft_values(pickup)) == 1
    if when == "before_keep":
        assert "field-courier" not in KeptDraft.objects.get().values
    else:
        field.update(holds_patient_information=True)
    browser.force_login(user)
    page = browser.get(PAGE)
    assert ROUTE not in page.text and SUBJECT in page.text and missed(page)
    assert ('name="field-courier"' in page.text) == (user_fixture == "capable")


def test_removing_a_ticket_form_before_keep_preserves_the_core_ticket_fields(agent, pickup):
    browser = pages.browser_for(agent)
    token = drawn_marker(section(browser, pickup))[0]
    values = draft_values(pickup)
    pickup.fields.all().delete()
    pickup.delete()
    pages.end_open(agent)
    assert pages.send(browser, token, values) == 1, "The ticket subject and body were also lost"
    browser.force_login(agent)
    page = browser.get(PAGE)
    assert SUBJECT in page.text and ROUTE not in page.text and missed(page)


def test_total_size_limit_counts_ticket_picker_and_custom_fields_together(agent, pickup):
    browser = pages.browser_for(agent)
    token = drawn_marker(section(browser, pickup))[0]
    pages.end_open(agent)
    values = {
        "body": "B" * kept.MAX_VALUE,
        "field-courier": "C" * kept.MAX_VALUE,
        "request_form": str(pickup.pk),
    }
    assert pages.send(browser, token, values) == 0
    assert not KeptDraft.objects.exists()
    values["body"] = values["body"][len(str(pickup.pk)) :]
    assert pages.send(browser, token, values) == 1


def test_form_growing_past_the_marker_limit_still_returns_the_already_held_text(
    agent, pickup, catalog
):
    browser = pending(agent, pickup)
    # Add real, individually named quantity inputs. The marker covers all three parts together.
    catalog.items.bulk_create(
        [
            catalog.items.model(catalog=catalog, name=f"Example item {n}")
            for n in range(kept.MAX_FIELDS)
        ]
    )
    page = browser.get(PAGE)
    assert pages.marker(page) is None
    if KeptDraft.objects.get().taken_at is None:
        catalog.items.filter(name__startswith="Example item ").update(is_active=False)
        page = browser.get(PAGE)
    assert SUBJECT in page.text
    assert ROUTE in page.text and "data-vr-kept-notice" in page.text, (
        "The old copy was consumed without returning its custom answers or showing its notice"
    )


@pytest.mark.parametrize("change", ["none", "category", "form"])
def test_restored_page_carries_current_terms_and_definition_on_each_submit(agent, pickup, change):
    browser = pending(agent, pickup)
    page = browser.get(PAGE)
    values = draft_values(
        pickup,
        category_terms=hidden(page, "category_terms"),
        form_definition=hidden(page, "form_definition"),
    )
    if change == "category":
        type(pickup.category).objects.filter(pk=pickup.category_id).update(is_complaint=True)
    if change == "form":
        pickup.fields.filter(key="courier").update(label="Delivery door")
    response = browser.post(PAGE, values)
    if change != "none":
        assert response.status_code == 200 and not ServiceRequest.objects.exists()
        assert SUBJECT in response.text
        values.update(
            category_terms=hidden(response, "category_terms"),
            form_definition=hidden(response, "form_definition"),
        )
        response = browser.post(PAGE, values)
    assert response.status_code == 302 and ServiceRequest.objects.get().subject == SUBJECT


def test_failed_page_draw_allows_a_later_get_to_recover_the_complete_copy(
    agent, pickup, monkeypatch
):
    browser = pending(agent, pickup)
    with monkeypatch.context() as patch:

        def fail(*args, **kwargs):
            assert KeptDraft.objects.get().taken_at is not None
            raise RuntimeError("Synthetic render failure")

        patch.setattr(views, "render", fail)
        with pytest.raises(RuntimeError, match="Synthetic render failure"):
            browser.get(PAGE)
    assert KeptDraft.objects.get().taken_at is None
    assert ROUTE in browser.get(PAGE).text


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("second", ["page", "section"])
def test_simultaneous_reads_deliver_the_copy_once(agent, pickup, monkeypatch, second):
    browser = pending(agent, pickup)
    tab = Browser()
    tab.cookies = deepcopy(browser.cookies)  # two tabs in one signed-in browser
    claimed, resume = Event(), Event()
    take = kept.take

    def hold_claim(*args, **kwargs):
        copy = take(*args, **kwargs)
        if copy is not None:
            claimed.set()
            assert resume.wait(10)
        return copy

    monkeypatch.setattr(kept, "take", hold_claim)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(in_worker, lambda: browser.get(PAGE))
        try:
            assert claimed.wait(10)
            response = tab.get(PAGE) if second == "page" else section(tab, pickup)
            assert response.status_code == 200 and SUBJECT not in response.text
        finally:
            resume.set()
        assert SUBJECT in first.result(timeout=10).text
    assert KeptDraft.objects.get().values is None


@pytest.mark.django_db(transaction=True)
def test_organization_and_picker_come_from_the_same_reading(
    agent, pickup, clinic, other_org, monkeypatch
):
    # With Clinic selected, the unrelated form is absent. With no organization selected,
    # the normal category section lists both. Either complete reading is consistent.
    other = RequestTemplate.objects.create(
        name="Other organization form", key="other-form", category=pickup.category
    )
    other.clients.add(other_org)
    browser = pending(agent, pickup, client=str(clinic.pk))
    with retire_after_read(monkeypatch, kept, "draw", clinic):
        page = browser.get(PAGE)
    organization = selected(page, "client")
    current_section = section(browser, pickup, client=organization)
    assert (other.name in page.text) == (other.name in current_section.text), (
        f"Picker differs from a fresh section for visible organization {organization!r}"
    )


@pytest.mark.django_db(transaction=True)
def test_retiring_category_while_reopening_does_not_split_the_selector_and_guidance(
    agent, pickup, monkeypatch
):
    cat = pickup.category
    type(cat).objects.filter(pk=cat.pk).update(guidance="Count the demonstration cartons.")
    browser = pending(agent, pickup)
    with retire_after_read(monkeypatch, kept, "draw", cat):
        page = browser.get(PAGE)
    assert (selected(page, "category") == str(cat.pk)) == (
        "Count the demonstration cartons." in page.text
    ), "The selector is empty but the retired category's filing guidance is shown"


@pytest.mark.parametrize("dialog", [False, True])
def test_expired_multipart_returns_to_form_and_keeps_text_out_of_side_effects(
    agent, pickup, dialog, caplog
):
    browser = pages.browser_for(agent)
    token = drawn_marker(section(browser, pickup))[0]
    before = (AuditEvent.objects.count(), Notice.objects.count(), len(mail.outbox))
    headers = {"Referer": f"http://testserver{PAGE}"}
    if dialog:
        headers.update(
            {
                "X-VR-Form": "1",
                "HX-Request": "true",
                "HX-Current-URL": "http://testserver/requests/?view=all",
            }
        )
    AccountSession.objects.filter(user=agent, ended_at__isnull=True).update(
        last_active_at=Now() - timedelta(minutes=31)
    )
    with caplog.at_level(logging.INFO):
        response = browser.post(
            PAGE,
            draft_values(
                pickup,
                **{kept.FIELD: token},
                files=SimpleUploadedFile("example-file.txt", b"sample-file-bytes"),
            ),
            headers=headers,
        )
        assert response.status_code == (401 if dialog else 302)
        target = urlsplit(response["HX-Redirect" if dialog else "Location"])
        assert not target.netloc and parse_qs(target.query)["next"] == [PAGE]
        row = KeptDraft.objects.get()
        assert row.values["field-courier"] == ROUTE and "files" not in row.values
        assert "sample-file-bytes" not in str(row.values) and "example-file.txt" not in str(
            row.values
        )
        assert not ServiceRequest.objects.exists() and not AttachmentBlob.objects.exists()
        browser.force_login(agent)
        page = browser.get(PAGE)
        assert ROUTE in page.text and "no-store" in page["Cache-Control"]
        assert {"lab_ops.request", "lab_ops.signin"} <= {r.name for r in caplog.records}
        for sample in (ROUTE, SUBJECT, "example-file.txt", "sample-file-bytes"):
            assert sample not in caplog.text + browser.cookies.output()
            assert sample not in response.cookies.output() + str(response.headers)
        assert before == (AuditEvent.objects.count(), Notice.objects.count(), len(mail.outbox))


@pytest.mark.django_db(transaction=True)
def test_section_picker_and_custom_fields_agree_when_the_form_is_retired(
    agent, pickup, monkeypatch
):
    from request_forms.forms import ValuesForm

    RequestTemplate.objects.create(key="spare", name="Another form", category=pickup.category)
    browser = pages.browser_for(agent)
    with retire_after_read(monkeypatch, ValuesForm, "__init__", pickup):
        page = section(browser, pickup)
    chosen = selected(page, "request_form") == str(pickup.pk)
    assert ('name="field-count"' in page.text, 'name="form_definition"' in page.text) == (
        chosen,
        chosen,
    ), "The picker and the signed set of custom fields describe different forms"


@pytest.mark.parametrize("change", ["category", "template"])
@pytest.mark.django_db(transaction=True)
def test_edit_committed_between_take_and_draw_gives_a_coherent_page(
    agent, pickup, monkeypatch, change
):
    browser = pending(agent, pickup)
    record = pickup.category if change == "category" else pickup
    with retire_after_read(monkeypatch, kept, "take", record):
        page = browser.get(PAGE)
    chosen = selected(page, "request_form") == str(pickup.pk)
    assert SUBJECT in page.text
    assert (ROUTE in page.text) == chosen
    assert ('name="form_definition"' in page.text) == chosen
    assert missed(page) != chosen


@pytest.mark.parametrize("surface", ["staff", "portal"])
def test_held_standalone_post_without_referrer_returns_to_its_form(
    agent, pickup, clinic, portal_user, surface
):
    if surface == "staff":
        user, path = agent, PAGE
        browser = pages.browser_for(user)
        token = drawn_marker(section(browser, pickup))[0]
        data = draft_values(pickup)
    else:
        user, path = portal_user(clinic), reverse("portal:request-create")
        browser = pages.browser_for(user)
        token = pages.marker(browser.get(path))[0]
        data = {"subject": SUBJECT, "description": "The carton is ready."}
    AccountSession.objects.filter(user=user, ended_at__isnull=True).update(
        last_active_at=Now() - timedelta(minutes=31)
    )
    response = browser.post(path, {**data, kept.FIELD: token})
    assert response.status_code == 302 and KeptDraft.objects.get().values["subject"] == SUBJECT
    assert parse_qs(urlsplit(response["Location"]).query).get("next") == [path]


def test_page_help_claim_about_changed_fields_matches_a_renamed_question(agent, pickup):
    # Adopted: the help was corrected instead of the behaviour. An answer goes back to the same
    # field while it fits it (the shared rule for every kept form, docs/architecture.md); the help
    # now says that a field whose label changed keeps its answer, to be checked.
    import json
    from pathlib import Path

    browser = pending(agent, pickup)
    pickup.fields.filter(key="count").update(label="Number of return labels")
    page = browser.get(PAGE)
    assert "Number of return labels" in page.text
    assert page.context["intake_drawn"][1]["field-count"].value() not in ("", None)
    help_page = Path(__file__).resolve().parents[1] / "docs/pages/desk/request-create.json"
    text = json.dumps(json.loads(help_page.read_text()))
    assert "A field whose label changed keeps its answer" in text
    assert "a form field changed or removed since" not in text


def test_page_help_claim_about_chosen_values_includes_a_choice_only_ticket(agent, pickup):
    # Adopted: the help was corrected instead of the behaviour. A copy needs text in a field one
    # types in (the shared rule, docs/architecture.md); the help now says so.
    import json
    from pathlib import Path

    browser = pages.browser_for(agent)
    token = drawn_marker(section(browser, pickup))[0]
    pages.end_open(agent)
    assert (
        pages.send(
            browser,
            token,
            {
                "category": str(pickup.category_id),
                "request_form": str(pickup.pk),
                "priority": "high",
            },
        )
        == 0
    )
    help_page = Path(__file__).resolve().parents[1] / "docs/pages/desk/request-create.json"
    text = json.dumps(json.loads(help_page.read_text()))
    assert "while a text box here holds some text" in text


def test_combined_field_count_includes_the_ticket_and_picker_in_the_cutoff(agent, pickup, catalog):
    browser = pages.browser_for(agent)
    _, names, _ = drawn_marker(section(browser, pickup))
    remaining = kept.MAX_FIELDS - len(names)
    catalog.items.bulk_create(
        [catalog.items.model(catalog=catalog, name=f"Boundary item {n}") for n in range(remaining)]
    )
    assert len(drawn_marker(section(browser, pickup))[1]) == kept.MAX_FIELDS
    catalog.items.create(name="One more boundary item")
    assert pages.marker(section(browser, pickup)) is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first", ["keep", "signout"])
def test_signout_and_ticket_keep_discard_the_copy_in_both_commit_orders(agent, pickup, first):
    from django.db import transaction

    from tests.test_review_session_list import on_connection, wait_for_block

    expired = pages.browser_for(agent)
    token = drawn_marker(section(expired, pickup))[0]
    pages.end_open(agent)
    live = pages.browser_for(agent)

    def deliver():
        return pages.send(expired, token, draft_values(pickup))

    def logout():
        return live.post(reverse("account_logout")).status_code

    leading, following = (deliver, logout) if first == "keep" else (logout, deliver)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            assert leading() == (1 if first == "keep" else 302)
            other = pool.submit(on_connection, following, pids)
            wait_for_block(other, pids)
        assert other.result(timeout=10) == (302 if first == "keep" else 0)
    assert not KeptDraft.objects.filter(user=agent).exists()


def test_all_four_previous_forms_leave_the_staff_ticket_for_its_own_page(
    pickup, clinic, portal_user, agent
):
    from clients.models import PortalAccess

    browser = pending(agent, pickup)
    ticket = pages.ticket.__wrapped__(agent, pickup.category, clinic)
    staff_page = browser.get(pages.ticket_page(ticket))
    marker = drawn_marker(staff_page)
    assert kept._read(marker[0])["k"] != "staff-ticket" and SUBJECT not in staff_page.text

    portal = portal_user(clinic)
    PortalAccess.objects.create(user=portal, client=clinic, whole_organization=True)
    browser.force_login(portal)
    for path in [
        reverse("portal:request-detail", args=[ticket.number]),
        reverse("portal:request-create"),
        reverse("portal_forms:raise", args=[pickup.key]),
    ]:
        page = browser.get(path)
        marker = drawn_marker(page)
        assert kept._read(marker[0])["k"] != "staff-ticket" and SUBJECT not in page.text
    assert KeptDraft.objects.get().taken_at is None
    browser.force_login(agent)
    assert SUBJECT in browser.get(PAGE).text


def test_planned_safeguards_column_agrees_with_the_shipped_new_ticket_page(agent):
    from pathlib import Path

    shipped = pages.marker(pages.browser_for(agent).get(PAGE)) is not None
    document = (Path(__file__).resolve().parents[1] / "docs/safeguards.md").read_text()
    row = next(line for line in document.splitlines() if line.startswith("| Automatic logoff ("))
    planned = row.strip("|").split("|")[-1]
    assert not (shipped and "staff member's new ticket" in planned), (
        "The safeguards table still lists the shipped staff ticket work under Planned"
    )


def test_shared_help_includes_the_logged_contact_that_the_security_settings_fact_lists():
    import json
    from pathlib import Path

    directory = Path(__file__).resolve().parents[1] / "docs/pages/core"
    shared = json.loads((directory / "shared.json").read_text())
    description = next(
        item["description"]
        for section in shared["sections"]
        for item in section.get("items", [])
        if item["label"] == "Automatic sign-out"
    )
    assert "logged contact" in (directory / "security-settings.json").read_text()
    assert "logged contact" in description, (
        "Shared help lists the forms that keep typing and says the other forms lose it"
    )
