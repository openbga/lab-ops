"""Verification of kept typing at b984f45, using synthetic forms and ordinary page requests."""

import re
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from queue import Queue
from threading import current_thread

import pytest
from django.db import connection, transaction
from django.db.models.functions import Now
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts import kept
from accounts import models as account_models
from accounts.models import AccountSession, KeptDraft
from accounts.roles import ROLES
from desk.tests import test_kept_typing as pages
from request_forms.models import Audience, Catalog, FieldKind
from request_forms.services import update_field
from tests import test_review_kept_typing as adopted
from tests.test_review_session_list_verify import in_worker, wait_for_lock

pytestmark = pytest.mark.django_db
case = adopted.review_form_cases


def raise_page(case):
    return reverse("portal_forms:raise", args=[case.template.key])


def add_field(case, key, kind, **kwargs):
    return case.template.fields.create(
        key=key, label=key.title(), audience=Audience.CLIENT, kind=kind, **kwargs
    )


def pending(account, page, values):
    browser, token = pages.typed_then_signed_out(account, page)
    assert pages.send(browser, token, values) == 1
    browser.force_login(account)
    return browser


def missed(page):
    found = re.search(r"<p\b[^>]*\bdata-vr-kept-missed\b[^>]*>", page.text)
    return bool(found and "hidden" not in found.group())


def test_all_removed_answers_still_explain_what_could_not_be_returned(case):
    url = raise_page(case)
    browser = pending(case.portal, url, {"field-route": "West dock"})
    case.template.fields.filter(key="route").update(is_active=False)
    page = browser.get(url)
    assert page.status_code == 200 and "West dock" not in page.text
    assert KeptDraft.objects.get().taken_at is not None
    assert missed(page), "The only populated answer disappeared, without a notice"


def test_an_independent_public_field_can_share_the_beginning_of_a_patient_fields_code(case):
    field = add_field(case, "reference_note", FieldKind.SHORT_TEXT)
    field.full_clean()
    url = raise_page(case)
    browser, token = pages.typed_then_signed_out(case.portal, url)
    assert "field-reference_note" in kept._read(token)["f"]
    fields = {"description": "Morning collection", "field-reference_note": "Box at reception"}
    assert pages.send(browser, token, fields) == 1
    assert KeptDraft.objects.get().values.get("field-reference_note") == "Box at reception"


@pytest.mark.django_db(transaction=True)
def test_patient_item_quantities_are_filtered_after_the_definition_writer_commits(case, staff):
    actor = staff("verify_form_editor", "admin")
    catalog = Catalog.objects.create(key="boxes", name="Boxes")
    item = catalog.items.create(name="Return boxes", unit="box")
    field = add_field(case, "boxes", FieldKind.ITEMS, catalog=catalog)
    url = raise_page(case)
    browser, token = pages.typed_then_signed_out(case.portal, url)
    part = f"field-boxes_{item.pk}"
    assert part in kept._read(token)["f"]
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            update_field(
                actor=actor,
                field_id=field.pk,
                expected_revision=field.revision,
                reason="Change the example field classification.",
                holds_patient_information=True,
            )

            def keep():
                return pages.send(browser, token, {"description": "Return run", part: "2"})

            worker = pool.submit(in_worker, keep, pids)
            wait_for_lock(worker, pids)
        assert worker.result(timeout=10) == 1
    saved = KeptDraft.objects.get().values
    assert saved["description"] == "Return run" and part not in saved


def test_removed_template_refuses_the_original_form(case):
    url = raise_page(case)
    browser, token = pages.typed_then_signed_out(case.portal, url)
    case.template.fields.all().delete()
    case.template.delete()
    assert pages.send(browser, token, {"description": "Next pickup"}) == 0
    assert not KeptDraft.objects.exists()


def test_stable_request_form_draws_each_kind_of_answer_in_its_native_control(case):
    values = {
        "short": (FieldKind.SHORT_TEXT, "Dock C"),
        "long": (FieldKind.LONG_TEXT, "Phone on arrival."),
        "count": (FieldKind.NUMBER, "12.5000"),
        "day": (FieldKind.DATE, "2026-12-02"),
        "scheduled": (FieldKind.DATETIME, "2026-12-02T13:45"),
        "hour": (FieldKind.TIME, "13:45"),
        "dock": (FieldKind.CHOICE, "West"),
        "route_code": (FieldKind.CHOICE, "R6"),
        "supplies": (FieldKind.MULTIPLE_CHOICE, ["Labels", "Boxes"]),
        "call": (FieldKind.YES_NO, "no"),
        "confirm": (FieldKind.CONFIRMATION, "on"),
    }
    options = {
        "dock": ["West", "East"],
        "route_code": ["R1", "R2", "R3", "R4", "R5", "R6"],
        "supplies": ["Labels", "Bags", "Boxes"],
    }
    fields = {}
    for key, (kind, answer) in values.items():
        add_field(case, key, kind, choices=options.get(key, []))
        fields[f"field-{key}"] = answer
    catalog = Catalog.objects.create(key="cartons", name="Cartons")
    item = catalog.items.create(name="Small carton", unit="each")
    add_field(case, "cartons", FieldKind.ITEMS, catalog=catalog)
    fields[f"field-cartons_{item.pk}"] = "3"
    url = raise_page(case)
    browser = pending(case.portal, url, fields)
    page = browser.get(url)
    assert pages.marker(page)[2] and not missed(page)
    form = page.context["form"]
    for key, (kind, answer) in values.items():
        name = f"field-{key}"
        widget = form[name].as_widget()
        assert f'name="{name}"' in page.text
        if key == "route_code":
            assert re.search(r'<option value="R6" selected\s*>', widget)
        elif key in ("dock", "supplies", "call"):
            selected = re.findall(
                r'<input\b(?=[^>]*\bchecked\b)[^>]* value="([^"]+)"[^>]*>', widget
            )
            assert selected == (answer if isinstance(answer, list) else [answer])
        elif kind == FieldKind.CONFIRMATION:
            assert " checked" in widget
        elif kind == FieldKind.LONG_TEXT:
            assert f"{answer}</textarea>" in widget
        else:
            assert f'value="{answer}"' in widget
    assert re.search(
        rf'<input\b(?=[^>]*name="field-cartons_{item.pk}")(?=[^>]*value="3")[^>]*>', page.text
    )


@pytest.mark.parametrize(
    "before,after",
    [
        (FieldKind.CHOICE, FieldKind.MULTIPLE_CHOICE),
        (FieldKind.MULTIPLE_CHOICE, FieldKind.CHOICE),
    ],
)
def test_still_offered_single_answer_survives_switching_choice_presentation(
    case, staff, before, after
):
    options = ["AB", "A", "B"]
    field = add_field(case, "dock", before, choices=options)
    url = raise_page(case)
    original = ["AB"] if before == FieldKind.MULTIPLE_CHOICE else "AB"
    browser = pending(case.portal, url, {"field-dock": original, "description": "New collection"})
    update_field(
        actor=staff("choice_editor", "admin"),
        field_id=field.pk,
        expected_revision=field.revision,
        kind=after,
        reason="Update the control.",
    )
    page = browser.get(url)
    rendered = page.context["form"]["field-dock"].as_widget()
    chosen = re.findall(r'<input\b(?=[^>]*\bchecked\b)[^>]* value="([^"]+)"[^>]*>', rendered)
    assert chosen == ["AB"], rendered
    assert not missed(page)


def test_a_no_answer_does_not_check_a_new_confirmation(case, staff):
    field = add_field(case, "confirm", FieldKind.YES_NO)
    url = raise_page(case)
    browser = pending(case.portal, url, {"field-confirm": "no", "description": "Friday pickup"})
    update_field(
        actor=staff("confirmation_editor", "admin"),
        field_id=field.pk,
        expected_revision=field.revision,
        kind=FieldKind.CONFIRMATION,
        reason="Require a new explicit confirmation.",
    )
    page = browser.get(url)
    assert 'name="field-confirm"' in page.text
    assert page.context["form"]["field-confirm"].field.required
    assert not re.search(
        r'<input\b(?=[^>]*name="field-confirm")(?=[^>]*\bchecked\b)[^>]*>', page.text
    )
    assert missed(page)


@pytest.mark.parametrize("surface", ["staff ticket", "portal reply", "portal new", "request form"])
@pytest.mark.django_db(transaction=True)
def test_a_render_failure_after_the_marked_form_leaves_the_copy_for_the_next_page(
    case, surface, monkeypatch
):
    from core.templatetags.ui import ComponentNode

    choices = {
        "staff ticket": (case.agent, pages.ticket_page(case.ticket), "body"),
        "portal reply": (
            case.portal,
            reverse("portal:request-detail", args=[case.ticket.number]),
            "body",
        ),
        "portal new": (case.portal, reverse("portal:request-create"), "description"),
        "request form": (case.portal, raise_page(case), "description"),
    }
    account, url, name = choices[surface]
    values = {name: "Load at gate seven."}
    browser = pending(account, url, values)
    original = ComponentNode.render

    def stop_after_form(node, context):
        contents = original(node, context)
        if node.name == "form_page" and "data-vr-kept-notice" in contents:
            raise RuntimeError("Synthetic failure after drawing the form")
        return contents

    with monkeypatch.context() as patch:
        patch.setattr(ComponentNode, "render", stop_after_form)
        with pytest.raises(RuntimeError, match="Synthetic failure"):
            browser.get(url)
    page = browser.get(url)
    assert page.status_code == 200
    assert "Load at gate seven." in page.text, "The failed page consumed the copy"


@pytest.mark.django_db(transaction=True)
def test_a_stale_page_reading_finishes_even_if_another_page_already_claimed_the_copy(
    case, monkeypatch
):
    url = reverse("portal:request-detail", args=[case.ticket.number])
    browser = pending(case.portal, url, {"body": "Late afternoon"})
    other_tab = Client()
    other_tab.cookies = deepcopy(browser.cookies)
    caller = current_thread()
    original = kept.offer
    outcomes = []

    with ThreadPoolExecutor(max_workers=1) as pool:

        def claim(request, key, names):
            if current_thread() is not caller:
                return original(request, key, names)
            # The outer view has done its activity write and opened its real page snapshot.
            assert connection.in_atomic_block and KeptDraft.objects.get().taken_at is None
            other = pool.submit(in_worker, lambda: other_tab.get(url)).result(timeout=10)
            assert "Late afternoon" in other.text
            result = original(request, key, names)
            outcomes.append(result)
            assert KeptDraft.objects.get().taken_at is None  # snapshot still usable
            return result

        monkeypatch.setattr(kept, "offer", claim)
        page = browser.get(url)
    assert outcomes == [None]
    assert page.status_code == 200 and "Late afternoon" not in page.text
    assert case.ticket.subject in page.text and KeptDraft.objects.get().taken_at is not None


@pytest.mark.parametrize(
    "route,method,dialog,discards",
    [
        ("admin:logout", "post", False, True),
        ("account_logout", "post", False, True),
        ("account_logout", "get", False, False),
        ("portal:request-create", "post", True, False),
        ("portal:request-create", "post", False, False),
    ],
)
def test_only_a_signout_post_advances_the_cutoff(case, route, method, dialog, discards):
    if route == "admin:logout":
        account = account_models.User.objects.create_superuser("console_operator")
        # A console owner still needs an operational role to draw the staff composer.
        account.groups.set(case.agent.groups.all())
        browser = pending(account, pages.ticket_page(case.ticket), {"body": "Collection"})
    else:
        account = case.portal
        browser = pending(account, reverse("portal:request-create"), {"subject": "Collection"})
    assert not account_models.KeptDraftCutoff.objects.filter(user=account).exists()
    AccountSession.objects.filter(user=account, ended_at__isnull=True).update(
        last_active_at=Now() - timedelta(minutes=31)
    )
    headers = {"HTTP_X_VR_FORM": "1"} if dialog else {}
    response = getattr(browser, method)(reverse(route), **headers)
    assert response.status_code in (200, 302, 401)
    assert account_models.KeptDraftCutoff.objects.filter(user=account).exists() is discards
    assert KeptDraft.objects.filter(user=account).exists() is not discards


def test_an_open_session_keep_refusal_leaves_the_existing_cookie_alone(case):
    url = reverse("portal:request-create")
    browser = pending(case.portal, url, {"subject": "Previous note"})
    assert kept.BROWSER_COOKIE in browser.cookies
    page = browser.get(url)
    token = pages.marker(page)[0]
    assert "Previous note" in page.text
    result = browser.post(
        reverse("account-session-keep"),
        {"forms": [{"token": token, "values": {"subject": "Still signed in"}}]},
        content_type="application/json",
    )
    assert result.json() == {"kept": 0} and not KeptDraft.objects.filter(taken_at=None).exists()
    assert kept.BROWSER_COOKIE not in result.cookies


def test_no_draft_offer_from_csrf_refusal_of_a_live_form(case):
    url = reverse("portal:request-create")
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(case.portal)
    token = pages.marker(browser.get(url))[0]
    previous = pages.open_row(case.portal).last_active_at
    before = type(case.ticket).objects.count()
    reply = browser.post(url, {kept.FIELD: token, "subject": "Unsubmitted example"})
    assert reply.status_code == 403 and "no-store" in reply["Cache-Control"]
    assert (
        "Unsubmitted example" not in reply.text
        and "What you had typed in it is kept" not in reply.text
    )
    assert not KeptDraft.objects.exists() and type(case.ticket).objects.count() == before
    assert pages.open_row(case.portal).last_active_at == previous


@pytest.mark.parametrize(
    "audience", [*ROLES, "no role", "owner", "client", "other client", "visitor"]
)
def test_entry_get_returns_only_the_result_the_ticket_page_allows(
    case, staff, portal_user, audience
):
    from accounts.models import User
    from clients.models import Client as Organization

    browser = Client()
    if audience in ROLES:
        browser.force_login(staff("second", audience))
    elif audience == "owner":
        browser.force_login(User.objects.create_superuser("verify_owner"))
    elif audience == "no role":
        browser.force_login(User.objects.create_user("verify_unassigned"))
    elif audience == "client":
        browser.force_login(case.portal)
    elif audience == "other client":
        other = Organization.objects.create(name="Other clinic", account_number="OTHVERIFY")
        browser.force_login(portal_user(other, "verify_other"))

    for target in ("ordinary", "missing", "restricted"):
        if target == "restricted":
            type(case.category).objects.filter(pk=case.category.pk).update(is_restricted=True)
        pk = case.ticket.pk + 100 if target == "missing" else case.ticket.pk
        number = "REQ-999999" if target == "missing" else case.ticket.number
        pairs = [
            (reverse("desk:request-entry", args=[pk]), reverse("desk:request-detail", args=[pk])),
            (
                reverse("portal:request-reply", args=[number]),
                reverse("portal:request-detail", args=[number]),
            ),
        ]
        for action, detail in pairs:
            expected = browser.get(detail, follow=True)
            answer = browser.get(action, follow=True)
            assert answer.status_code == expected.status_code
            assert (case.ticket.subject in answer.text) == (case.ticket.subject in expected.text)
            if case.ticket.subject in answer.text:
                assert answer.wsgi_request.path == detail


def test_return_from_reply_action_keeps_the_ticket_breadcrumb_filters(case):
    browser = pages.browser_for(case.portal)
    ticket_url = reverse("portal:request-detail", args=[case.ticket.number])
    first = browser.get(ticket_url, {"list": "status=active&page=2"})
    original_list = first.context["list_url"]
    assert "status=active" in original_list and "page=2" in original_list
    action = first.context["reply_url"]
    refused = browser.post(action, {"body": "", "expected_revision": case.ticket.revision})
    assert refused.context["list_url"] == original_list
    reopened = browser.get(action, follow=True)
    assert reopened.status_code == 200 and reopened.wsgi_request.path == ticket_url
    assert reopened.context["list_url"] == original_list


def test_explicit_signout_checks_the_form_before_discarding(case, monkeypatch):
    """Observe validation order with a valid form, replacing the destructive callback with a spy."""
    from django.middleware.csrf import CsrfViewMiddleware

    url = reverse("account_logout")
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(case.portal)
    screen = browser.get(reverse("portal:request-create"))
    csrf = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', screen.text).group(1)
    events = []
    original = CsrfViewMiddleware._accept

    def accepted(check, request):
        if request.path == url:
            events.append("form accepted")
        return original(check, request)

    monkeypatch.setattr(CsrfViewMiddleware, "_accept", accepted)
    monkeypatch.setattr(kept, "discard_all", lambda user_id: events.append("discard"))
    browser.post(url, {"csrfmiddlewaretoken": csrf})
    assert events.index("form accepted") < events.index("discard")
    events.clear()
    browser.force_login(case.portal)
    screen = browser.get(reverse("portal:request-create"))
    csrf = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', screen.text).group(1)
    AccountSession.objects.filter(user=case.portal, ended_at__isnull=True).update(
        last_active_at=Now() - timedelta(minutes=31)
    )
    browser.post(url, {"csrfmiddlewaretoken": csrf})
    assert "discard" in events
    assert "form accepted" in events and events.index("form accepted") < events.index("discard"), (
        events
    )


def test_a_delayed_first_page_response_does_not_strand_an_acknowledged_draft(case):
    url = reverse("portal:request-create")
    browser = pages.browser_for(case.portal)
    other_tab = Client()
    other_tab.cookies = deepcopy(browser.cookies)
    first = browser.get(url)
    delayed = other_tab.get(raise_page(case))
    token = pages.marker(first)[0]
    pages.end_open(case.portal)
    assert pages.send(browser, token, {"subject": "Boxes already kept"}) == 1
    # The second page was requested before the browser had its first cookie.
    # Its HTTP response is delivered after the first tab's successful keep response.
    browser.cookies.update(delayed.cookies)
    browser.force_login(case.portal)
    assert "Boxes already kept" in browser.get(url).text


@pytest.mark.django_db(transaction=True)
def test_busy_newest_copy_does_not_restore_superseded_wording(case):
    url = reverse("portal:request-create")
    browser, first = pages.typed_then_signed_out(case.portal, url)
    assert pages.send(browser, first, {"description": "Earlier loading time"}) == 1
    browser.force_login(case.portal)
    next_page = browser.get(url, headers={"Sec-Purpose": "prefetch"})
    assert pages.given_back(next_page) is None
    second = pages.marker(next_page)[0]
    pages.end_open(case.portal)
    assert pages.send(browser, second, {"description": "Later loading time"}) == 1
    browser.force_login(case.portal)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            KeptDraft.objects.select_for_update().get(values__description="Later loading time")
            response = pool.submit(in_worker, lambda: browser.get(url)).result(timeout=10)
            assert response.status_code == 200
            assert "Earlier loading time" not in response.text
    assert "Later loading time" in browser.get(url).text


@pytest.mark.django_db(transaction=True)
def test_taken_newer_copy_excludes_an_older_pending_copy_at_give_back(case):
    url = reverse("portal:request-create")
    browser, first = pages.typed_then_signed_out(case.portal, url)
    assert pages.send(browser, first, {"description": "Former wording"}) == 1
    browser.force_login(case.portal)
    second = pages.marker(browser.get(url, headers={"Sec-Purpose": "prefetch"}))[0]
    pages.end_open(case.portal)
    assert pages.send(browser, second, {"description": "Updated wording"}) == 1
    browser.force_login(case.portal)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            KeptDraft.objects.select_for_update().get(values__description="Former wording")
            newest = pool.submit(in_worker, lambda: browser.get(url)).result(timeout=10)
            assert "Updated wording" in newest.text
    assert KeptDraft.objects.get(values__description="Former wording").taken_at is None
    assert "Former wording" not in browser.get(url).text


def test_receipt_does_not_announce_a_copy_hidden_by_a_previous_workers_signout(case):
    url = raise_page(case)
    browser, token = pages.typed_then_signed_out(case.portal, url)
    draft = {"description": "Check the box count"}
    assert pages.send(browser, token, draft) == 1
    # The mixed-release test exercises the real previous worker. This represents its ledger row.
    AccountSession.objects.create(
        user=case.portal, ending=AccountSession.Ending.SIGNED_OUT, ended_at=timezone.now()
    )
    receipt = pages.send(browser, token, draft)
    browser.force_login(case.portal)
    assert not pages.given_back(browser.get(url))
    assert receipt == 0, "The repeat keep reported a copy the account cannot recover"


def test_equal_end_times_are_not_ordered_as_older(case):
    url = raise_page(case)
    browser, earlier = pages.typed_then_signed_out(case.portal, url)
    ended = AccountSession.objects.get(user=case.portal).ended_at
    browser.force_login(case.portal)
    other = pages.marker(browser.get(url))[0]
    pages.end_open(case.portal)
    AccountSession.objects.filter(user=case.portal).update(
        started_at=ended - timedelta(hours=1),
        last_active_at=ended - timedelta(minutes=30),
        ended_at=ended,
    )
    assert pages.send(browser, other, {"description": "North route"}) == 1
    browser.force_login(case.portal)
    assert "North route" in browser.get(url).text
    assert pages.send(browser, earlier, {"description": "South route"}) == 1
    assert "South route" in browser.get(url).text


def test_unrelated_read_error_is_distinguished_from_a_claim_in_another_tab(case, monkeypatch):
    from django.db import OperationalError
    from django.db.models.query import QuerySet

    url = reverse("portal:request-create")
    browser = pending(case.portal, url, {"description": "The collection note"})
    original = QuerySet.update

    def update(queryset, **kwargs):
        if queryset.model is KeptDraft:
            raise OperationalError("Synthetic storage failure")
        return original(queryset, **kwargs)

    browser.raise_request_exception = False
    with monkeypatch.context() as patch:
        patch.setattr(QuerySet, "update", update)
        response = browser.get(url)
    assert "The collection note" in browser.get(url).text
    assert response.status_code >= 500, "A storage failure returned a successful blank form"


@pytest.mark.parametrize("kind", ["marked", "prefetch", "dialog", "unmarked", "visitor"])
def test_cookie_on_pages_tracks_presence_of_a_signed_form(case, settings, kind):
    settings.SESSION_COOKIE_SECURE = True
    browser = Client() if kind == "visitor" else pages.browser_for(case.portal)
    address = reverse("account-sessions") if kind == "unmarked" else raise_page(case)
    headers = {"Purpose": "prefetch"} if kind == "prefetch" else {}
    if kind == "dialog":
        headers = {"X-VR-Form": "1"}
    page = browser.get(address, headers=headers)
    marker = pages.marker(page)
    assert bool(marker) == (kept.BROWSER_COOKIE in page.cookies)
    if kind in ("unmarked", "visitor"):
        assert marker is None
    if marker:
        cookie = page.cookies[kept.BROWSER_COOKIE]
        assert cookie["secure"] and cookie["httponly"] and cookie["samesite"] == "Lax"
        assert cookie["path"] == "/" and int(cookie["max-age"]) == 86400


@pytest.mark.parametrize("header", ["X-Purpose", "X-Moz"])
def test_other_prefetch_headers_leave_the_copy_for_navigation(case, header):
    url = reverse("portal:request-create")
    browser = pending(case.portal, url, {"subject": "Load on Wednesday"})
    assert not pages.given_back(browser.get(url, headers={header: "prefetch"}))
    assert "Load on Wednesday" in browser.get(url).text
