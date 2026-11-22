"""Confirmation of the kept-typing fixes using synthetic portal and staff records.

Adopted from the confirmation round (.local/review-history/kept-typing-round-1-confirmation.md;
the original file is kept beside it). Cases changed while adopting are marked "Adopted:" with the
reason.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from itertools import permutations
from urllib.parse import parse_qs, urlsplit

import pytest
from django import forms
from django.db import transaction
from django.db.models.functions import Now
from django.test import Client
from django.urls import reverse

from accounts import kept
from accounts.models import AccountSession, KeptDraft, KeptDraftCutoff, User
from clients.models import ClientSite
from desk.tests import test_kept_typing as pages
from request_forms.models import Catalog, FieldKind
from request_forms.services import update_field
from tests import test_review_kept_typing_verify as previous
from tests.test_review_session_list_verify import in_worker

pytestmark = pytest.mark.django_db
case = previous.case
K = FieldKind


def notice_text(page):
    """Only the live notice, excluding similar wording in the page help."""
    return page.text.split("data-vr-kept-notice", 1)[1].split("</p>", 1)[0]


def definition(kind, catalog):
    if kind == K.ITEMS:
        return {"catalog": catalog}
    if kind in (K.CHOICE, K.MULTIPLE_CHOICE):
        return {"choices": ["West", "East", "no"]}
    return {}


@pytest.mark.parametrize("before", list(K), ids=lambda kind: kind.value)
def test_authorized_field_kind_switches_round_trip_through_the_page(case, staff, before):
    """One field for each destination kind. The separate description makes every draft eligible."""
    catalog = Catalog.objects.create(key="sizes", name="Sizes")
    item = catalog.items.create(name="Small box", unit="each")
    actor = staff("confirm_editor", "admin")
    source = {
        K.SHORT_TEXT: "West",
        K.LONG_TEXT: "East",
        K.NUMBER: "3",
        K.DATE: "2026-12-03",
        K.DATETIME: "2026-12-03T13:45",
        K.TIME: "13:45",
        K.CHOICE: "West",
        K.MULTIPLE_CHOICE: ["East"],
        K.YES_NO: "no",
        K.CONFIRMATION: "on",
        K.ITEMS: "2",
        K.HEADING: "",
    }[before]
    fields = {}
    payload = {"description": "Dock instructions"}
    for after in K:
        if after == before:
            continue
        field = previous.add_field(
            case, f"was-{after.value}", before, **definition(before, catalog)
        )
        name = f"field-{field.key}"
        fields[after] = (field, name)
        if before != K.HEADING:
            payload[f"{name}_{item.pk}" if before == K.ITEMS else name] = source
    url = previous.raise_page(case)
    browser = previous.pending(case.portal, url, payload)
    for after, (field, _) in fields.items():
        update_field(
            actor=actor,
            field_id=field.pk,
            expected_revision=field.revision,
            kind=after,
            reason="Change the unfilled definition.",
            **definition(after, catalog),
        )
    page = browser.get(url)
    assert page.status_code == 200
    form = page.context["form"]
    assert form["description"].value() == payload["description"]
    scalar = source[0] if isinstance(source, list) else source
    for after, (_, name) in fields.items():
        if after == K.HEADING:
            assert name not in form.fields
            continue
        if before in (K.HEADING, K.ITEMS) or after == K.ITEMS:
            assert not form[name].value()
        elif after == K.CONFIRMATION:
            assert bool(form[name].value()) == (scalar == "on")
        elif after in (K.CHOICE, K.MULTIPLE_CHOICE, K.YES_NO):
            choices = dict(form.fields[name].choices)
            expected = scalar if scalar in choices else ""
            if after == K.MULTIPLE_CHOICE:
                assert (form[name].value() or []) == ([expected] if expected else [])
            else:
                assert (form[name].value() or "") == expected
        else:
            assert form[name].value() == scalar


@pytest.mark.parametrize(
    "attrs,saved,checked,missed",
    [
        ({}, "on", True, False),
        ({}, "", False, False),
        ({}, "no", False, True),
        ({"value": "agreed"}, "agreed", True, False),
        ({"value": "agreed"}, "on", False, True),
        ({"value": "agreed"}, "", False, False),
        ({"value": ""}, "", False, False),
    ],
)
def test_checkbox_matches_its_rendered_value_and_leaves_empty_answers_clear(
    attrs, saved, checked, missed
):
    form = forms.Form()
    form.fields["tick"] = forms.BooleanField(
        required=False, widget=forms.CheckboxInput(attrs=attrs)
    )
    # Adopted: _put_back takes the kinds of control the copy came from and gives (drawn, missed).
    assert kept._put_back(form, {"tick": saved}, {"tick": "box"})[1] is missed
    assert (" checked" in form["tick"].as_widget()) is checked


def test_a_switch_setting_does_not_answer_a_new_required_confirmation(case, staff):
    field = previous.add_field(case, "switch", K.CHOICE, choices=["on", "off"])
    url = previous.raise_page(case)
    browser = previous.pending(case.portal, url, {"field-switch": "on", "description": "Courier"})
    update_field(
        actor=staff("setting_editor", "admin"),
        field_id=field.pk,
        expected_revision=field.revision,
        kind=K.CONFIRMATION,
        label="I confirm the collection details",
        reason="Require acknowledgement.",
    )
    page = browser.get(url)
    box = page.context["form"]["field-switch"]
    assert box.field.required
    assert " checked" not in box.as_widget(), (
        "The client chose a switch setting, never this confirmation"
    )
    assert previous.missed(page)


@pytest.mark.parametrize("later", ["other catalog", "other kind"])
def test_quantity_names_of_a_newly_patient_marked_field_follow_its_identity(case, staff, later):
    catalog = Catalog.objects.create(key="boxes", name="Boxes")
    box = catalog.items.create(name="Empty box", unit="each")
    field = previous.add_field(case, "supplies", K.ITEMS, catalog=catalog)
    url = previous.raise_page(case)
    browser, token = pages.typed_then_signed_out(case.portal, url)
    old_quantity = f"field-supplies_{box.pk}"
    assert old_quantity in kept._read(token)["f"]
    changes = (
        {"kind": K.SHORT_TEXT}
        if later == "other kind"
        else {"catalog": Catalog.objects.create(key="labels", name="Labels")}
    )
    update_field(
        actor=staff("classification_editor", "admin"),
        field_id=field.pk,
        expected_revision=field.revision,
        holds_patient_information=True,
        reason="Update the catalog field.",
        **changes,
    )
    # No protected content is supplied. Only an unrelated description is typed.
    assert pages.send(browser, token, {"description": "Return the boxes"}) == 1
    assert old_quantity not in KeptDraft.objects.get().values


def test_only_populated_answer_no_longer_offered_has_a_truthful_notice(case, staff):
    field = previous.add_field(case, "dispatch", K.SHORT_TEXT)
    url = previous.raise_page(case)
    browser = previous.pending(case.portal, url, {"field-dispatch": "Old dock"})
    update_field(
        actor=staff("options_editor", "admin"),
        field_id=field.pk,
        expected_revision=field.revision,
        kind=K.CHOICE,
        choices=["East", "West"],
        reason="Offer the current docks.",
    )
    page = browser.get(url)
    assert not page.context["form"]["field-dispatch"].value()
    assert previous.missed(page)
    notice = notice_text(page)
    assert "Not put back" in notice and "is back in the form" not in notice


@pytest.mark.parametrize(
    "size,held", [(2, (0,)), (2, (1,)), (3, (2,)), (3, (1,)), (3, (0, 1)), (3, (0, 2))]
)
@pytest.mark.django_db(transaction=True)
def test_locked_pending_copies_return_the_newest_or_leave_all_for_later(case, size, held):
    url = reverse("portal:request-create")
    browser = pages.browser_for(case.portal)
    ids = []
    for index in range(size):
        token = pages.marker(browser.get(url, headers={"Purpose": "prefetch"}))[0]
        pages.end_open(case.portal)
        title = f"Pickup window {index}"
        assert pages.send(browser, token, {"subject": title}) == 1
        ids.append(KeptDraft.objects.get(values__subject=title).pk)
        browser.force_login(case.portal)
    with ThreadPoolExecutor(max_workers=1) as pool, transaction.atomic():
        list(KeptDraft.objects.select_for_update().filter(pk__in=[ids[i] for i in held]))
        response = pool.submit(in_worker, lambda: browser.get(url)).result(timeout=10)
        assert response.status_code == 200
        given = pages.given_back(response)
        if size - 1 in held:
            assert given is None and not KeptDraft.objects.exclude(taken_at=None).exists()
        else:
            assert given["subject"] == f"Pickup window {size - 1}"
    next_copy = pages.given_back(browser.get(url))
    if size - 1 in held:
        assert next_copy["subject"] == f"Pickup window {size - 1}"
    else:
        assert next_copy is None


def copy_tab(browser):
    tab = Client()
    tab.cookies = deepcopy(browser.cookies)
    return tab


def test_cold_answers_of_one_session_agree_in_every_delivery_order(case):
    browser = pages.browser_for(case.portal)
    urls = [
        reverse("portal:request-create"),
        previous.raise_page(case),
        reverse("portal:request-detail", args=[case.ticket.number]),
    ]
    first = [copy_tab(browser).get(url) for url in urls]
    for answer in first:
        assert pages.marker(answer)
    values = [answer.cookies[kept.BROWSER_COOKIE].value for answer in first]
    assert len(set(values)) == 1
    for order in permutations(first):
        tab = copy_tab(browser)
        for answer in order:
            tab.cookies.update(answer.cookies)
        assert tab.get(urls[0]).cookies[kept.BROWSER_COOKIE].value == values[0]
    browser.cookies.update(first[0].cookies)
    pages.end_open(case.portal)
    browser.force_login(case.portal)
    assert browser.get(urls[0]).cookies[kept.BROWSER_COOKIE].value == values[0]


@pytest.mark.parametrize("source", ["keep endpoint", "expired form POST"])
def test_request_without_a_session_uses_a_fresh_cookie_independent_of_the_form_token(case, source):
    url = reverse("portal:request-create")
    owner = pages.browser_for(case.portal)
    page = owner.get(url)
    token = pages.marker(page)[0]
    owner_value = page.cookies[kept.BROWSER_COOKIE].value
    pages.end_open(case.portal)
    anonymous = Client()
    payload = {"subject": "Tuesday collection"}
    if source == "keep endpoint":
        assert pages.send(anonymous, token, payload) == 1
    else:
        assert anonymous.post(url, {kept.FIELD: token, **payload}).status_code == 302
        assert KeptDraft.objects.count() == 1
    assert anonymous.cookies[kept.BROWSER_COOKIE].value != owner_value
    owner.force_login(case.portal)
    assert not pages.given_back(owner.get(url))
    anonymous.force_login(case.portal)
    assert pages.given_back(anonymous.get(url))["subject"] == payload["subject"]


@pytest.mark.xfail(
    strict=True,
    reason="Adopted: accepted limitation (docs/architecture.md): a browser without the keep cookie "
    "that receives a page of an earlier sign-in after a later sign-in's page ends with the earlier "
    "value, and a copy kept under the later one is not given back. The value is derived per "
    "session so that one session's answers agree; nothing on the server can tell which answer the "
    "browser took last, and the cookie stays HttpOnly.",
)
def test_late_page_from_an_earlier_signin_does_not_strand_a_second_signins_draft(case):
    url = reverse("portal:request-create")
    browser = pages.browser_for(case.portal)
    # This first response was still in flight when its session ended.
    late = copy_tab(browser).get(url)
    pages.end_open(case.portal)
    browser.force_login(case.portal)
    second = browser.get(url)
    token = pages.marker(second)[0]
    pages.end_open(case.portal)
    assert pages.send(browser, token, {"subject": "Second sign-in wording"}) == 1
    browser.cookies[kept.BROWSER_COOKIE] = late.cookies[kept.BROWSER_COOKIE].value
    browser.force_login(case.portal)
    assert "Second sign-in wording" in browser.get(url).text


@pytest.mark.parametrize(
    "change,held",
    [
        ("unchanged", True),
        ("inactive", False),
        ("expired", False),
        ("cutoff", False),
        ("other ending", False),
        ("taken", False),
    ],
)
def test_duplicate_receipt_uses_current_eligibility(case, change, held):
    url = reverse("portal:request-create")
    browser, token = pages.typed_then_signed_out(case.portal, url)
    values = {"subject": "Return the boxes"}
    assert pages.send(browser, token, values) == 1
    if change == "inactive":
        User.objects.filter(pk=case.portal.pk).update(is_active=False)
    elif change == "expired":
        AccountSession.objects.filter(user=case.portal).update(ended_at=Now() - timedelta(days=1))
    elif change == "cutoff":
        KeptDraftCutoff.objects.create(user=case.portal, discarded_at=Now())
    elif change == "other ending":
        AccountSession.objects.filter(user=case.portal).update(
            ending=AccountSession.Ending.NEW_PASSWORD
        )
    elif change == "taken":
        browser.force_login(case.portal)
        assert pages.given_back(browser.get(url))["subject"] == values["subject"]
    assert pages.send(browser, token, values) == int(held)


def test_repeat_delivery_reports_the_still_available_first_copy_when_the_form_was_cleared(case):
    url = reverse("portal:request-create")
    browser, token = pages.typed_then_signed_out(case.portal, url)
    assert pages.send(browser, token, {"subject": "Keep the original wording"}) == 1
    receipt = pages.send(browser, token, {"subject": ""})
    browser.force_login(case.portal)
    assert pages.given_back(browser.get(url))["subject"] == "Keep the original wording"
    assert receipt == 1


@pytest.mark.parametrize("route", ["account_logout", "admin:logout"])
@pytest.mark.parametrize("accepted", [True, False])
def test_expired_explicit_logout_honors_the_result_of_the_form_check(
    case, route, accepted, monkeypatch
):
    from django.http import HttpResponseForbidden
    from django.middleware.csrf import CsrfViewMiddleware

    account = case.portal if route == "account_logout" else User.objects.create_superuser("console")
    page = reverse("portal:request-create") if route == "account_logout" else reverse("admin:index")
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(account)
    screen = browser.get(page)
    assert screen.status_code == 200
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', screen.text).group(1)
    AccountSession.objects.filter(user=account, ended_at=None).update(
        last_active_at=Now() - timedelta(minutes=31)
    )
    original = CsrfViewMiddleware.process_view
    events = []

    def check(middleware, request, view, args, kwargs):
        result = original(middleware, request, view, args, kwargs)
        if request.path == reverse(route):
            assert result is None  # the real, valid form passed
            events.append("checked")
            return result if accepted else HttpResponseForbidden()
        return result

    monkeypatch.setattr(CsrfViewMiddleware, "process_view", check)
    monkeypatch.setattr(kept, "discard_all", lambda account_id: events.append("discard"))
    browser.post(reverse(route), {"csrfmiddlewaretoken": token})
    assert events == (["checked", "discard"] if accepted else ["checked"])


@pytest.mark.parametrize("which", ["general", "request form"])
@pytest.mark.django_db(transaction=True)
def test_portal_site_queryset_and_restored_choice_share_the_render_snapshot(
    case, which, monkeypatch
):
    site = ClientSite.objects.create(client_id=case.portal.client_id, name="East office", code="E")
    url = reverse("portal:request-create") if which == "general" else previous.raise_page(case)
    browser = previous.pending(case.portal, url, {"subject": "New boxes", "site": str(site.pk)})
    original = kept.prepare
    with ThreadPoolExecutor(max_workers=1) as pool:

        def prepare(request, form, key):
            contents = original(request, form, key)
            assert form["site"].value() == str(site.pk)
            changes = pool.submit(
                in_worker, lambda: ClientSite.objects.filter(pk=site.pk).update(is_active=False)
            ).result(timeout=10)
            assert changes == 1
            return contents

        with monkeypatch.context() as patch:
            patch.setattr(kept, "prepare", prepare)
            response = browser.get(url)
    assert response.status_code == 200
    assert re.search(rf'<option value="{site.pk}" selected\s*>East office', response.text)
    assert "East office" not in browser.get(url).text


@pytest.mark.parametrize("surface", ["desk", "portal"])
def test_reply_redirect_carries_only_recognized_list_parameters(case, surface):
    if surface == "desk":
        browser = pages.browser_for(case.agent)
        action = reverse("desk:request-entry", args=[case.ticket.pk])
    else:
        browser = pages.browser_for(case.portal)
        action = reverse("portal:request-reply", args=[case.ticket.number])
    response = browser.get(action, {"list": "q=courier&page=2&display_hint=compact"})
    assert response.status_code == 302
    nested = parse_qs(urlsplit(response.url).query)["list"][0]
    # Adopted: the staff ticket page carries the queue's address as the person chose it
    # (desk.views._queue_address); the portal keeps the list's own parameters (core.lists).
    expected = {"q": ["courier"], "page": ["2"]}
    if surface == "desk":
        expected["display_hint"] = ["compact"]
    assert parse_qs(nested) == expected


def test_signin_return_from_the_staff_composers_actual_post_keeps_the_queue(case):
    browser = pages.browser_for(case.agent)
    detail = pages.ticket_page(case.ticket)
    queue = "q=Courier&page=2"
    opened = browser.get(detail, {"list": queue})
    composer = re.search(r"<form\b(?=[^>]*vr-composer)[^>]*>", opened.text).group()
    action = re.search(r'action="([^"]+)"', composer).group(1)
    refused = browser.post(
        action,
        {"list": queue, "kind": "note", "body": "", "expected_revision": case.ticket.revision},
    )
    assert refused.status_code == 400 and refused.context["list_query"] == queue
    # After signing in the browser opens the address on screen with GET.
    reopened = browser.get(refused.wsgi_request.get_full_path(), follow=True)
    assert reopened.status_code == 200 and reopened.wsgi_request.path == detail
    assert reopened.context["list_query"] == queue


@pytest.mark.parametrize("target", [K.CHOICE, K.SHORT_TEXT, K.CONFIRMATION])
def test_multiple_answers_are_not_collapsed_to_a_single_value(case, staff, target):
    field = previous.add_field(case, "mode", K.MULTIPLE_CHOICE, choices=["West", "East"])
    url = previous.raise_page(case)
    browser = previous.pending(
        case.portal, url, {"description": "New boxes", "field-mode": ["West", "East"]}
    )
    options = {"choices": ["West", "East"]} if target == K.CHOICE else {}
    update_field(
        actor=staff("mode_editor", "admin"),
        field_id=field.pk,
        kind=target,
        expected_revision=field.revision,
        reason="Ask for one answer.",
        **options,
    )
    response = browser.get(url)
    assert not response.context["form"]["field-mode"].value()
    assert previous.missed(response)


@pytest.mark.parametrize("which", ["general", "request form"])
@pytest.mark.django_db(transaction=True)
def test_site_choices_and_requirement_describe_the_same_organization(case, which, monkeypatch):
    from desk.portal_forms import RaiseRequestForm
    from request_forms.forms import PortalRaiseForm

    first = ClientSite.objects.create(
        client_id=case.portal.client_id, name="First office", code="O1"
    )
    url = reverse("portal:request-create") if which == "general" else previous.raise_page(case)
    form_class = RaiseRequestForm if which == "general" else PortalRaiseForm
    original = form_class.__init__
    browser = pages.browser_for(case.portal)
    with ThreadPoolExecutor(max_workers=1) as pool:

        def after_construction(form, *args, **kwargs):
            original(form, *args, **kwargs)
            assert not form.fields["site"].required
            pool.submit(
                in_worker,
                lambda: ClientSite.objects.create(
                    client_id=first.client_id, name="Second office", code="O2"
                ),
            ).result(timeout=10)

        with monkeypatch.context() as patch:
            patch.setattr(form_class, "__init__", after_construction)
            page = browser.get(url)
    assert page.status_code == 200 and "First office" in page.text
    form = page.context["form"]
    payload = {hidden.html_name: hidden.value() or "" for hidden in form.hidden_fields()}
    payload.update(subject="Pickup", description="Collect boxes", category=case.category.pk)
    if which == "general":
        payload["category_terms"] = page.context["category_terms"]
    response = browser.post(url, payload)
    assert response.status_code == 200 and "site" in response.context["form"].errors
    shows_two = "Second office" in page.text
    assert form.fields["site"].required == shows_two, "Two active sites were offered as optional"


@pytest.mark.parametrize("source", ["keep endpoint", "expired form POST"])
def test_two_cookie_less_receivers_of_the_same_form_get_independent_random_values(case, source):
    url = reverse("portal:request-create")
    owner, token = pages.typed_then_signed_out(case.portal, url)
    issued = [owner.cookies[kept.BROWSER_COOKIE].value]
    # Roll each trial back so both receivers can exercise the first accepted delivery.
    for _ in range(2):
        with transaction.atomic():
            receiver = Client()
            if source == "keep endpoint":
                assert pages.send(receiver, token, {"subject": "New boxes"}) == 1
            else:
                response = receiver.post(url, {kept.FIELD: token, "subject": "New boxes"})
                assert response.status_code == 302 and KeptDraft.objects.count() == 1
            issued.append(receiver.cookies[kept.BROWSER_COOKIE].value)
            transaction.set_rollback(True)
    assert len(set(issued)) == 3


@pytest.mark.parametrize("status", ["resolved", "closed"])
def test_refused_composer_names_only_fields_the_ticket_still_offers(case, status):
    changes = {"status": status, "resolved_at": Now()}
    if status == "closed":
        changes["closed_at"] = Now()
    type(case.ticket).objects.filter(pk=case.ticket.pk).update(**changes)
    browser = pages.browser_for(case.agent)
    opened = browser.get(pages.ticket_page(case.ticket))
    assert "new_status" not in pages.marker(opened)[1]
    response = browser.post(
        reverse("desk:request-entry", args=[case.ticket.pk]),
        {"kind": "note", "body": "", "expected_revision": case.ticket.revision},
    )
    assert response.status_code == 400
    assert "body" in response.context["composer"].errors
    assert 'name="new_status"' not in response.text
    assert "new_status" not in pages.marker(response)[1]


@pytest.mark.django_db(transaction=True)
def test_receipt_for_an_older_pending_row_after_a_newer_one_was_given_back(case):
    url = reverse("portal:request-create")
    browser, early = pages.typed_then_signed_out(case.portal, url)
    assert pages.send(browser, early, {"subject": "Earlier wording"}) == 1
    browser.force_login(case.portal)
    late = pages.marker(browser.get(url, headers={"Purpose": "prefetch"}))[0]
    pages.end_open(case.portal)
    assert pages.send(browser, late, {"subject": "Later wording"}) == 1
    browser.force_login(case.portal)
    with ThreadPoolExecutor(max_workers=1) as pool, transaction.atomic():
        KeptDraft.objects.select_for_update().get(values__subject="Earlier wording")
        response = pool.submit(in_worker, lambda: browser.get(url)).result(timeout=10)
        assert pages.given_back(response)["subject"] == "Later wording"
    assert KeptDraft.objects.get(values__subject="Earlier wording").taken_at is None
    assert pages.send(browser, early, {"subject": "Earlier wording"}) == 0
    assert not pages.given_back(browser.get(url))


def test_all_retired_notice_names_the_outcome_once_without_rearming_the_form(case):
    url = previous.raise_page(case)
    browser = previous.pending(case.portal, url, {"field-route": "West"})
    case.template.fields.filter(key="route").update(is_active=False)
    page = browser.get(url)
    notice = notice_text(page)
    assert previous.missed(page) and "Not put back" in notice
    assert "is back in the form" not in notice
    marker = re.search(r'<input[^>]* name="_vr_kept"[^>]*>', page.text).group()
    assert "data-vr-kept-bound" not in marker
    assert "data-vr-kept-notice" not in browser.get(url).text
