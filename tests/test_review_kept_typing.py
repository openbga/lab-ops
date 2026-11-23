"""Independent regressions for recovering a person's own unsent work after a session limit.

Adopted from review round 1 (.local/review-history/kept-typing-round-1.md; the original file is
kept beside it). Cases changed while adopting are marked "Adopted:" with the reason: the fixes
changed a contract the case asserted (the browser's cookie set when a page draws a kept form,
function signatures, a large form left unmarked, an old release's Sign out leaving a copy that is
no longer given back, a late older copy refused).
"""

import hashlib
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from importlib.util import find_spec
from io import StringIO
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.functions import Now
from django.http import HttpResponseRedirect
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from accounts import sessions
from accounts.models import AccountSession, User
from accounts.roles import ROLES
from attachments.models import Attachment, AttachmentBlob
from clients.models import PortalAccess
from core.models import AuditEvent
from desk.models import EntryKind, RequestEntry, ServiceRequest, Status
from desk.services import create_request
from desk.tests import conftest as desk_fixtures
from jobs.models import Notice
from request_forms.models import Audience, FieldKind, FilledForm, RequestTemplate
from taxonomy.models import RequestCategory

if find_spec("accounts.kept") is not None:
    from accounts import kept
    from accounts.models import KeptDraft
    from desk.tests import test_kept_typing as review_pages
    from desk.tests.test_kept_typing import ticket as ticket
    from desk.tests.test_kept_typing import whole as whole


# Reuse the application's desk-role, category and organization fixtures.
agent = desk_fixtures.agent
category = desk_fixtures.category
clinic = desk_fixtures.clinic
group = desk_fixtures.group
make_user = desk_fixtures.make_user
water = desk_fixtures.water

pytestmark = pytest.mark.django_db


def _composer(case):
    browser = review_pages.browser_for(case.agent)
    page = review_pages.ticket_page(case.ticket)
    token, _, _ = review_pages.marker(browser.get(page))
    return browser, page, token


def test_each_form_acknowledged_in_one_keep_can_be_reopened_in_that_browser(review_form_cases):
    case = review_form_cases
    browser = review_pages.browser_for(case.portal)
    pages = [
        reverse("portal:request-detail", args=[case.ticket.number]),
        reverse("portal_forms:raise", args=[case.template.key]),
    ]
    values = [{"body": "Wednesday morning"}, {"description": "Thursday afternoon"}]
    batch = [
        {"token": review_pages.marker(browser.get(page))[0], "values": fields}
        for page, fields in zip(pages, values, strict=True)
    ]
    review_pages.end_open(case.portal)
    # Adopted: the pages now set the browser's cookie when drawn; take it away, so that one keep
    # request for two forms from a browser without one is what is tested.
    del browser.cookies[kept.BROWSER_COOKIE]
    response = browser.post(reverse("account-session-keep"), {"forms": batch}, "application/json")
    # The keep answer also says, for each form sent, whether a copy is held (new-ticket batch).
    assert response.json() == {"kept": 2, "held": [True, True]}
    browser.force_login(case.portal)
    restored = [review_pages.given_back(browser.get(page)) or {} for page in pages]
    assert all(
        fields.items() <= copy.items() for fields, copy in zip(values, restored, strict=True)
    )


@pytest.mark.parametrize("secure", [True, False])
def test_keep_cookie_is_renewed_and_normal_round_trip_leaves_no_content_in_side_effects(
    review_form_cases, settings, caplog, secure
):

    settings.SESSION_COOKIE_SECURE = secure
    case = review_form_cases
    browser, page, token = _composer(case)
    phrase = "Return the blue transport boxes by Friday"
    browser.cookies[kept.BROWSER_COOKIE] = "invalid"
    before = (AuditEvent.objects.count(), Notice.objects.count(), len(mail.outbox))
    with caplog.at_level(logging.INFO):
        _review_expire(case.agent)
        keep = browser.post(
            reverse("account-session-keep"),
            {"forms": [{"token": token, "values": {"body": phrase}}]},
            content_type="application/json",
        )
        # The keep answer also says, for each form sent, whether a copy is held (new-ticket batch).
        assert keep.status_code == 200 and keep.json() == {"kept": 1, "held": [True]}
        cookie = keep.cookies[kept.BROWSER_COOKIE]
        assert cookie.value != "invalid" and cookie["path"] == "/"
        assert (
            bool(cookie["secure"]) == secure and cookie["httponly"] and cookie["samesite"] == "Lax"
        )
        assert int(cookie["max-age"]) == 48 * 60 * 60  # Adopted: as long as a page's token
        assert KeptDraft.objects.get().browser == hashlib.sha256(cookie.value.encode()).hexdigest()
        assert not browser.get(reverse("account-session")).json()["signed_in"]
        browser.force_login(case.agent)
        offered = browser.get(page)
        assert review_pages.given_back(offered)["body"] == phrase
        assert "no-store" in keep["Cache-Control"] and "no-store" in offered["Cache-Control"]
        assert phrase not in keep.text
        for result in [keep, offered]:
            assert (
                phrase
                not in str(result.headers) + result.cookies.output() + result.request["PATH_INFO"]
            )
        assert before == (AuditEvent.objects.count(), Notice.objects.count(), len(mail.outbox))
        assert {"lab_ops.request", "lab_ops.signin"} <= {record.name for record in caplog.records}
        assert phrase not in caplog.text

    next_token = review_pages.marker(offered)[0]
    _review_expire(case.agent)
    second = browser.post(
        reverse("account-session-keep"),
        {"forms": [{"token": next_token, "values": {"body": "Another visit"}}]},
        content_type="application/json",
    )
    renewed = second.cookies[kept.BROWSER_COOKIE]
    assert renewed.value == cookie.value and int(renewed["max-age"]) == 172_800  # Adopted


@pytest.mark.parametrize("has_referer", [True, False], ids=["with-referrer", "without-referrer"])
def test_submit_from_open_tab_after_status_has_ended_session(review_form_cases, has_referer):
    case = review_form_cases
    browser = Client(enforce_csrf_checks=True)
    page = reverse("portal:request-create")
    browser.force_login(case.portal)
    token = review_pages.marker(browser.get(page))[0]
    csrf = browser.cookies["csrftoken"].value
    _review_expire(case.portal)
    assert browser.get(reverse("account-session")).json()["signed_in"] is False
    fields = {
        "description": "Two boxes on the loading dock",
        kept.FIELD: token,
        "csrfmiddlewaretoken": csrf,
    }
    headers = {"HTTP_REFERER": f"http://testserver{page}"} if has_referer else {}
    answer = browser.post(page, fields, **headers)
    assert answer.status_code == 302
    assert fields["description"] not in answer.url
    assert KeptDraft.objects.filter(
        user=case.portal, values__description=fields["description"]
    ).exists()
    messages = [str(m) for m in browser.get(answer.url).context["messages"]]
    assert kept.KEPT_MESSAGE in messages


def test_refusal_keeps_cookies_from_the_replaced_response(review_form_cases):
    case = review_form_cases
    _, page, token = _composer(case)
    review_pages.end_open(case.agent)
    action = reverse("desk:request-entry", args=[case.ticket.pk])
    request = RequestFactory().post(
        action,
        {kept.FIELD: token, "body": "Morning collection"},
        HTTP_REFERER="http://testserver" + page,
    )
    request.user = AnonymousUser()

    def sign_in_required(request):
        response = HttpResponseRedirect(sessions.sign_in_url(request.get_full_path()))
        response.set_cookie("review_preference", "phone", httponly=True, samesite="Strict")
        return response

    response = sessions.SessionLimitMiddleware(sign_in_required)(request)
    original = response.cookies["review_preference"]
    assert original.value == "phone" and original["httponly"] and original["samesite"] == "Strict"
    assert response.cookies[kept.BROWSER_COOKIE]["httponly"]
    assert parse_qs(urlsplit(response.url).query)["next"] == [page]


def test_unreadable_form_still_returns_the_limit_notice_without_keeping_text(review_form_cases):
    case = review_form_cases
    browser, page, _ = _composer(case)
    _review_expire(case.agent)
    response = browser.generic(
        "POST",
        page,
        b"unfinished form",
        content_type="multipart/form-data",
        HTTP_REFERER="http://testserver" + page,
    )
    assert response.status_code == 302 and "no-store" in response["Cache-Control"]
    notices = [str(m) for m in browser.get(response.url).context["messages"]]
    assert any("without activity" in n for n in notices) and kept.KEPT_MESSAGE not in notices
    assert not KeptDraft.objects.exists()


def test_the_retention_deadline_is_checked_at_the_keep_decision(review_form_cases):
    """Place the deadline between the transaction start and the insert, without timing a sleep."""

    case = review_form_cases
    _, _, token = _composer(case)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT transaction_timestamp(), clock_timestamp()")
        start, now = cursor.fetchone()
        assert now > start
        deadline = start + (now - start) / 2
        ended = deadline - kept.KEPT_FOR
        AccountSession.objects.filter(user=case.agent).update(
            started_at=ended - timedelta(hours=1),
            last_active_at=ended - timedelta(minutes=30),
            ended_at=ended,
            ending=AccountSession.Ending.INACTIVITY,
        )
        held = kept.keep(token, {"body": "End of the previous shift"}, "c" * 64)
        assert not held and not KeptDraft.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first", ["keep", "signout"])
def test_signout_and_keep_from_two_sessions_leave_no_account_copy(review_form_cases, first):
    from queue import Queue

    from tests.test_review_session_list import on_connection, wait_for_block

    case = review_form_cases
    expired, _, token = _composer(case)
    review_pages.end_open(case.agent)
    live = review_pages.browser_for(case.agent)

    def submit():
        return review_pages.send(expired, token, {"body": "Monday route"})

    def sign_out():
        return live.post(reverse("account_logout")).status_code

    leading, following = (submit, sign_out) if first == "keep" else (sign_out, submit)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as workers:
        with transaction.atomic():
            assert leading() == (1 if first == "keep" else 302)
            pending = workers.submit(on_connection, following, pids)
            wait_for_block(pending, pids)
        assert pending.result(timeout=12) == (302 if first == "keep" else 0)
    assert AccountSession.objects.filter(
        user=case.agent, ending=AccountSession.Ending.SIGNED_OUT
    ).exists()
    assert not KeptDraft.objects.filter(user=case.agent).exists()


def test_clean_enforces_character_limits_on_the_whole_copy():
    names = ["a", "b", "c"]
    full = {"a": "a" * kept.MAX_VALUE, "b": "b" * kept.MAX_VALUE, "c": ""}
    assert sum(map(len, full.values())) == kept.MAX_TOTAL
    # Adopted: _clean also takes the names that hold several values.
    assert kept._clean(full, names, names, []) == full
    assert kept._clean({**full, "c": "c"}, names, names, []) is None
    assert kept._clean({"a": "north\0dock"}, names, names, []) == {
        "a": "northdock",
        "b": "",
        "c": "",
    }


def test_old_form_token_does_not_outlive_its_documented_age(review_form_cases, monkeypatch):
    from django.core import signing

    case = review_form_cases
    _, _, token = _composer(case)
    review_pages.end_open(case.agent)
    assert kept._read(token) is not None
    later = signing.time.time() + kept.TOKEN_AGE.total_seconds() + 5
    with monkeypatch.context() as patch:
        patch.setattr(signing, "time", SimpleNamespace(time=lambda: later))
        assert kept._read(token) is None


@pytest.fixture
def review_form_cases(staff, client_org, portal_user):
    agent = staff("form_review_agent", "client_services")
    portal = portal_user(client_org, "form_review_client")
    PortalAccess.objects.create(user=portal, client=client_org, whole_organization=True)
    group = RequestCategory.objects.create(key="shipping", name="Shipping")
    category = RequestCategory.objects.create(
        key="courier", name="Courier", parent=group, client_may_raise=True
    )
    ticket = create_request(
        actor=agent,
        client=client_org,
        category=category,
        subject="Courier time",
        first_entry_kind=EntryKind.NOTE,
        first_entry_body="Confirm the pickup time.",
        channel="phone",
    )
    template = RequestTemplate.objects.create(
        key="route",
        name="Route details",
        client_label="Route details",
        category=category,
        offered_in_portal=True,
    )
    for key, label, protected in (
        ("route", "Courier route", False),
        ("reference", "Private example field", True),
    ):
        template.fields.create(
            key=key,
            label=label,
            kind=FieldKind.SHORT_TEXT,
            audience=Audience.CLIENT,
            holds_patient_information=protected,
        )
    return SimpleNamespace(
        agent=agent, portal=portal, category=category, ticket=ticket, template=template
    )


def _review_expire(account):
    changed = AccountSession.objects.filter(user=account, ended_at__isnull=True).update(
        last_active_at=Now() - timedelta(minutes=31)
    )
    assert changed == 1


@pytest.mark.parametrize("surface", ["composer", "reply", "new", "request form"])
def test_expired_multipart_post_recovers_the_text_and_commits_no_ticket_or_file(
    client,
    review_form_cases,
    surface,
):

    case = review_form_cases
    account = case.agent if surface == "composer" else case.portal
    note = "Collect the empty transport boxes after three."
    if surface == "composer":
        page = reverse("desk:request-detail", args=[case.ticket.pk])
        action = reverse("desk:request-entry", args=[case.ticket.pk])
        data = {"kind": "note", "body": note, "expected_revision": case.ticket.revision}
    elif surface == "reply":
        page = reverse("portal:request-detail", args=[case.ticket.number])
        action = reverse("portal:request-reply", args=[case.ticket.number])
        data = {"body": note, "expected_revision": case.ticket.revision}
    elif surface == "new":
        action = page = reverse("portal:request-create")
        data = {"subject": "Packing boxes", "category": case.category.pk, "description": note}
    else:
        action = page = reverse("portal_forms:raise", args=[case.template.key])
        data = {"description": note, "field-route": "South", "field-reference": "Example only"}

    client.force_login(account)
    drawn = client.get(page)
    token, names, _ = review_pages.marker(drawn)
    if surface == "request form":
        data["form_definition"] = drawn.context["form"]["form_definition"].value()
    data[kept.FIELD] = token
    data["files"] = SimpleUploadedFile("empty-boxes.txt", b"Synthetic packing list.", "text/plain")
    models = (ServiceRequest, RequestEntry, Attachment, AttachmentBlob, FilledForm)
    before = [model.objects.count() for model in models]
    _review_expire(account)
    result = client.post(action, data, HTTP_REFERER=f"http://testserver{page}")
    assert result.status_code == 302
    assert [model.objects.count() for model in models] == before
    revision = ServiceRequest.objects.values_list("revision", flat=True).get(pk=case.ticket.pk)
    assert revision == case.ticket.revision
    row = KeptDraft.objects.get()
    assert row.values == {name: str(data.get(name, "")) for name in names}
    assert all(
        key not in row.values
        for key in ("files", "expected_revision", "form_definition", "field-reference")
    )
    notices = [str(message) for message in client.get(result.url).context["messages"]]
    assert kept.KEPT_MESSAGE in notices and any("without activity" in msg for msg in notices)
    client.force_login(account)
    assert review_pages.given_back(client.get(page)) == row.values


def test_large_request_form_does_not_report_a_partial_copy_as_kept(client, review_form_cases):
    case = review_form_cases
    for index in range(kept.MAX_FIELDS):
        case.template.fields.create(
            key=f"stop-{index:02d}",
            label=f"Stop {index}",
            kind=FieldKind.SHORT_TEXT,
            audience=Audience.CLIENT,
            position=index + 1,
        )
    page = reverse("portal_forms:raise", args=[case.template.key])
    client.force_login(case.portal)
    # Adopted: a form with more fields than MAX_FIELDS is not marked at all, so nothing of it can
    # be kept in part; a token naming too many fields is refused as a whole.
    assert review_pages.marker(client.get(page)) is None
    names = [f"field-stop-{index:02d}" for index in range(kept.MAX_FIELDS)] + ["description"]
    session = AccountSession.objects.get(user=case.portal, ended_at__isnull=True)
    oversized = kept.signing.dumps(
        {"u": case.portal.pk, "s": str(session.pk), "k": "x", "f": names, "t": names},
        salt=kept.SALT,
        compress=True,
    )
    assert kept._read(oversized) is None
    _review_expire(case.portal)
    response = client.post(
        page,
        {
            kept.FIELD: oversized,
            "description": "Stop at both buildings",
            names[-2]: "Last building",
        },
    )
    assert response.status_code == 302
    assert not KeptDraft.objects.exists(), "The oversized copy must be refused as a whole"


def test_changed_request_form_returns_only_still_eligible_answers(client, review_form_cases):
    case = review_form_cases
    page = reverse("portal_forms:raise", args=[case.template.key])
    client.force_login(case.portal)
    token, names, _ = review_pages.marker(client.get(page))
    assert "field-route" in names and "field-reference" not in names
    _review_expire(case.portal)
    assert (
        client.post(
            page, {kept.FIELD: token, "field-route": "North", "description": "Morning run"}
        ).status_code
        == 302
    )
    case.template.fields.filter(key="route").update(holds_patient_information=True)
    case.template.fields.filter(key="reference").update(holds_patient_information=False)
    client.force_login(case.portal)
    restored = review_pages.given_back(client.get(page))
    assert restored["description"] == "Morning run"
    assert "field-route" not in restored and "field-reference" not in restored


@pytest.mark.parametrize("account_kind", ["staff", "client"])
def test_patient_marked_fields_do_not_join_the_keep_list_when_capability_changes(
    review_form_cases,
    account_kind,
):
    from django.contrib.auth.models import Permission

    from request_forms.forms import ValuesForm

    case = review_form_cases
    account = case.agent if account_kind == "staff" else case.portal
    capability = Permission.objects.get(
        content_type__app_label="accounts", codename="view_patient_information"
    )
    for grant in (True, False):
        if grant:
            account.user_permissions.add(capability)
        else:
            account.user_permissions.remove(capability)
        form = ValuesForm(case.template, user=User.objects.get(pk=account.pk))
        assert ("field-reference" in form.fields) == (account_kind == "client" or grant)
        names, typed, _several = kept.keepable(form)  # Adopted: keepable gives three lists
        assert "field-route" in typed and "field-reference" not in names


def test_reclassified_field_is_excluded_from_the_persisted_field_set(client, review_form_cases):
    case = review_form_cases
    page = reverse("portal_forms:raise", args=[case.template.key])
    client.force_login(case.portal)
    token, names, _ = review_pages.marker(client.get(page))
    assert "field-route" in names
    case.template.fields.filter(key="route").update(holds_patient_information=True)
    _review_expire(case.portal)
    # Only type in the ordinary description. Inspect field membership, not protected content.
    client.post(page, {kept.FIELD: token, "description": "Collect empty boxes"})
    saved = KeptDraft.objects.get().values
    assert saved["description"] == "Collect empty boxes"
    assert "field-route" not in saved


@pytest.mark.parametrize("kind", [FieldKind.ITEMS, FieldKind.MULTIPLE_CHOICE])
def test_request_form_preserves_the_items_and_selections_described_as_kept(
    client,
    review_form_cases,
    kind,
):
    import json

    from request_forms.models import Catalog

    case = review_form_cases
    options = ["East entrance", "West entrance"]
    if kind == FieldKind.ITEMS:
        catalog = Catalog.objects.create(key="box-types", name="Box types")
        box = catalog.items.create(name="Empty boxes", unit="box")
        field_options = {"catalog": catalog}
        submitted = {f"field-options_{box.pk}": "37"}
        answers = ["37"]
    else:
        field_options = {"choices": options}
        submitted = {"field-options": options}
        answers = options
    case.template.fields.create(
        key="options",
        label="Pickup options",
        audience=Audience.CLIENT,
        kind=kind,
        **field_options,
    )
    page = reverse("portal_forms:raise", args=[case.template.key])
    client.force_login(case.portal)
    draw = client.get(page)
    assert "field-options" in draw.context["form"].fields
    token, _, _ = review_pages.marker(draw)
    _review_expire(case.portal)
    client.post(page, {kept.FIELD: token, "description": "Packing instructions", **submitted})
    client.force_login(case.portal)
    restored = review_pages.given_back(client.get(page))
    assert restored["description"] == "Packing instructions"
    assert all(answer in json.dumps(restored) for answer in answers), restored


def test_request_again_preserves_the_chosen_earlier_tickets_answers(client, review_form_cases):
    import re

    from request_forms import definition
    from request_forms.services import create_request_with_form

    case = review_form_cases
    earlier = create_request_with_form(
        actor=case.portal,
        template=case.template,
        expected_definition=definition.token(case.template),
        values={"route": "North"},
        first_entry_kind=EntryKind.REPLY,
        channel="portal",
    )
    page = reverse("portal_forms:raise", args=[case.template.key])
    client.force_login(case.portal)
    drawn = client.get(page)
    token = re.search(rb'name="_vr_kept" value="([^"]+)"', drawn.content)
    post = {"field-route": "South", "description": "Afternoon run"}
    if token:
        post["_vr_kept"] = token.group(1).decode()
    _review_expire(case.portal)
    assert client.post(page, post).status_code == 302
    client.force_login(case.portal)
    again = client.get(page, {"again": earlier.number})
    assert f"Answers copied from {earlier.number}" in again.content.decode()
    assert again.context["form"]["field-route"].value() == "North"
    assert b"Afternoon run" not in again.content, "A draft overlays the selected ticket"
    if token:
        assert b"Afternoon run" in client.get(page).content


@pytest.fixture
def review_portal_reply(ticket, whole):
    url = reverse("portal:request-detail", args=[ticket.number])
    browser, token = review_pages.typed_then_signed_out(whole, url)
    assert review_pages.send(browser, token, {"body": "Review reply from my browser."}) == 1
    return browser, url


@pytest.mark.parametrize("role", [*ROLES, "no_role", "superuser"])
def test_other_staff_on_same_browser_open_their_own_form(role, make_user, agent, ticket):
    page = review_pages.ticket_page(ticket)
    browser, token = review_pages.typed_then_signed_out(agent, page)
    assert review_pages.send(browser, token, {"body": "My own composer text."}) == 1
    if role == "superuser":
        colleague = make_user("deployment_owner", is_staff=True, is_superuser=True)
    elif role == "no_role":
        colleague = make_user("no_role", is_staff=True)
    else:
        colleague = make_user("second_staff", role)
    browser.force_login(colleague)
    result = browser.get(page)
    assert result.status_code == (200 if role in ROLES else 403)
    assert b"My own composer text." not in result.content
    assert KeptDraft.objects.get().taken_at is None
    browser.force_login(agent)
    assert review_pages.given_back(browser.get(page))["body"] == "My own composer text."


@pytest.mark.parametrize("same_organization", [True, False])
def test_other_portal_accounts_on_same_browser_get_their_own_page(
    same_organization, review_portal_reply, make_user, whole, clinic, water
):
    browser, page = review_portal_reply
    organization = clinic if same_organization else water
    colleague = make_user("review_portal_colleague", client=organization)
    PortalAccess.objects.create(user=colleague, client=organization, whole_organization=True)
    browser.force_login(colleague)
    result = browser.get(page)
    assert result.status_code == (200 if same_organization else 404)
    assert b"Review reply from my browser." not in result.content
    assert KeptDraft.objects.get().taken_at is None
    browser.force_login(whole)
    assert review_pages.given_back(browser.get(page)) == {"body": "Review reply from my browser."}


def test_shared_demo_visitors_keep_their_own_drafts(make_user, ticket):
    user = make_user("review_demo", "technical", credentials_locked=True)
    page = review_pages.ticket_page(ticket)
    browsers = []
    for index in range(2):
        browser, token = review_pages.typed_then_signed_out(user, page)
        assert review_pages.send(browser, token, {"body": f"Visitor {index} text"}) == 1
        browsers.append(browser)
    for index, browser in enumerate(browsers):
        browser.force_login(user)
        assert review_pages.given_back(browser.get(page))["body"] == f"Visitor {index} text"


@pytest.mark.parametrize("new_state", ["closed", "restricted", "grant_removed", "inactive"])
def test_portal_reply_requires_a_ticket_the_owner_can_still_reply_to(
    new_state, review_portal_reply, whole, ticket
):
    browser, page = review_portal_reply
    browser.force_login(whole)
    if new_state == "closed":
        ServiceRequest.objects.filter(pk=ticket.pk).update(
            status=Status.CLOSED, resolved_at=Now(), closed_at=Now()
        )
    elif new_state == "restricted":
        type(ticket.category).objects.filter(pk=ticket.category_id).update(is_restricted=True)
    elif new_state == "grant_removed":
        PortalAccess.objects.filter(user=whole).delete()
    else:
        whole.is_active = False
        whole.save(update_fields=["is_active"])
    result = browser.get(page)
    expected = {"closed": 200, "inactive": 302, "restricted": 404, "grant_removed": 404}
    assert result.status_code == expected[new_state]
    assert b"Review reply from my browser." not in result.content
    assert KeptDraft.objects.get().taken_at is None


@pytest.mark.parametrize(
    "headers",
    [
        {"X-VR-Form": "1"},
        {"Sec-Purpose": "prefetch"},
        {"Purpose": "prefetch"},
        {"Sec-Fetch-Dest": "empty"},
    ],
    ids=["form-dialog", "sec-purpose-prefetch", "purpose-prefetch", "script"],
)
def test_background_draws_leave_the_copy_for_the_next_navigation(
    headers, review_portal_reply, whole
):
    browser, page = review_portal_reply
    browser.force_login(whole)
    background = browser.get(page, headers=headers)
    assert background.status_code == 200
    assert review_pages.given_back(background) is None
    assert KeptDraft.objects.get().taken_at is None
    assert review_pages.given_back(browser.get(page)) == {"body": "Review reply from my browser."}


@pytest.mark.django_db(transaction=True)
def test_two_tabs_opening_one_reply_display_the_draft_once(review_portal_reply, whole, monkeypatch):
    browser, page = review_portal_reply
    browser.force_login(whole)
    tabs = [Client(), Client()]
    for tab in tabs:
        tab.cookies = deepcopy(browser.cookies)
    # Each tab has opened the ticket in its own production page reading. Both finish drawing it.
    both_reading = Barrier(2, timeout=10)
    original_offer = kept.offer

    def offer_while_both_are_reading(*args, **kwargs):
        both_reading.wait()
        return original_offer(*args, **kwargs)

    monkeypatch.setattr(kept, "offer", offer_while_both_are_reading)

    def open_page(tab):
        try:
            response = tab.get(page)
            assert response.status_code == 200
            return review_pages.given_back(response)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        restored = list(executor.map(open_page, tabs))
    assert KeptDraft.objects.get().taken_at is not None
    assert restored.count({"body": "Review reply from my browser."}) == 1
    assert restored.count(None) == 1


def test_delayed_first_delivery_returns_the_newest_copy_still_unclaimed(whole, ticket):
    page = reverse("portal:request-detail", args=[ticket.number])
    browser, old = review_pages.typed_then_signed_out(whole, page)
    browser.force_login(whole)
    new, _, _ = review_pages.marker(browser.get(page))
    review_pages.end_open(whole)
    assert review_pages.send(browser, new, {"body": "Newer wording"}) == 1
    browser.force_login(whole)
    assert review_pages.given_back(browser.get(page)) == {"body": "Newer wording"}
    # The older tab's copy reaches the service for the first time after the new one was read.
    # Adopted: a copy older than one already given back for the form in this browser is refused,
    # and never given back.
    assert review_pages.send(browser, old, {"body": "Earlier wording"}) == 0
    assert review_pages.given_back(browser.get(page)) is None


def test_first_cookie_responses_for_two_tabs_keep_both_of_their_forms(whole, ticket):
    """Both tabs send before either keep response has set the browser's first draft cookie."""
    reply_page = reverse("portal:request-detail", args=[ticket.number])
    new_page = reverse("portal:request-create")
    browser = review_pages.browser_for(whole)
    reply_token = review_pages.marker(browser.get(reply_page))[0]
    new_token = review_pages.marker(browser.get(new_page))[0]
    review_pages.end_open(whole)
    # Snapshot both requests' cookies before processing either response, as one browser does.
    posted = [Client(), Client()]
    for tab in posted:
        tab.cookies = deepcopy(browser.cookies)
    for tab, token, fields in (
        (posted[0], reply_token, {"body": "Reply being typed"}),
        (posted[1], new_token, {"subject": "New ticket being typed"}),
    ):
        assert review_pages.send(tab, token, fields) == 1
        browser.cookies.update(tab.cookies)
    browser.force_login(whole)
    new_draft = review_pages.given_back(browser.get(new_page))
    assert new_draft and new_draft["subject"] == "New ticket being typed"
    assert review_pages.given_back(browser.get(reply_page)) == {"body": "Reply being typed"}


def test_send_from_old_tab_after_signing_back_in_on_another_tab(review_form_cases):
    """The old form carries the tokens originally drawn, including the browser's CSRF token."""
    import re

    from desk.tests.conftest import PASSWORD

    def form_csrf(response):
        return (
            re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', response.content)
            .group(1)
            .decode()
        )

    whole, ticket = review_form_cases.portal, review_form_cases.ticket
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(whole)
    page = reverse("portal:request-detail", args=[ticket.number])
    drawn = browser.get(page)
    old_csrf = form_csrf(drawn)
    old_marker = re.search(rb'name="_vr_kept" value="([^"]+)"', drawn.content)
    _review_expire(whole)
    assert not browser.get(reverse("account-session")).json()["signed_in"]

    # The original tab stays open with its unsent text. Log in normally on another tab.
    login = reverse("account_login")
    signed_in = browser.post(
        login,
        {
            "login": whole.username,
            "password": PASSWORD,
            "csrfmiddlewaretoken": form_csrf(browser.get(login)),
        },
    )
    assert signed_in.status_code == 302
    assert browser.get(reverse("account-session")).json()["signed_in"]
    body = "Text entered on the original reply page."
    response = browser.post(
        reverse("portal:request-reply", args=[ticket.number]),
        {
            "body": body,
            "expected_revision": ticket.revision,
            **({"_vr_kept": old_marker.group(1).decode()} if old_marker else {}),
            "csrfmiddlewaretoken": old_csrf,
        },
        HTTP_REFERER="http://testserver" + page,
    )
    assert not ticket.entries.filter(body=body).exists()
    if old_marker:
        assert review_pages.given_back(browser.get(page)) == {"body": body}, response.status_code
    else:
        assert response.status_code == 403


@pytest.mark.django_db(transaction=True)
def test_migration_preserves_existing_open_and_ended_sessions():
    # Adapted when accounts 0007 added AccountSession.second_factor_required: the rows of the
    # earlier schema are written through its historical model, and the browser's session is the
    # one a sign-in of that release left (force_login would write a row of today's model).
    from importlib import import_module

    from django.conf import settings
    from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY
    from django.contrib.auth import SESSION_KEY as AUTH_SESSION_KEY

    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    previous = [("accounts", "0004_session_browser_address")]
    executor.migrate(previous)
    try:
        then = executor.loader.project_state(previous).apps.get_model("accounts", "AccountSession")
        account = User.objects.create_user("migration-review")
        opened = then.objects.create(user_id=account.pk)
        store = import_module(settings.SESSION_ENGINE).SessionStore()
        store[AUTH_SESSION_KEY] = str(account.pk)
        store[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
        store[HASH_SESSION_KEY] = account.get_session_auth_hash()
        store[sessions.SESSION_KEY] = str(opened.pk)
        store.save()
        browser = Client()
        browser.cookies[settings.SESSION_COOKIE_NAME] = store.session_key
        ended = then.objects.create(
            user_id=account.pk, ended_at=timezone.now(), ending=AccountSession.Ending.INACTIVITY
        )
        before = list(then.objects.filter(user_id=account.pk).order_by("id").values())
        assert len(before) == 2 and sum(row["ended_at"] is None for row in before) == 1
    finally:
        MigrationExecutor(connection).migrate(latest)

    after = list(AccountSession.objects.filter(user=account).order_by("id").values())
    assert [row.pop("second_factor_required") for row in after] == [False, False]
    assert after == before
    assert browser.get(reverse("account-session")).json()["signed_in"] is True
    assert AccountSession.objects.get(pk=ended.pk).ending == AccountSession.Ending.INACTIVITY
    assert not KeptDraft.objects.exists()


@pytest.mark.django_db
def test_demo_seed_preserves_copies_and_reset_removes_copies_and_markers(monkeypatch):
    from core import demo

    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-review-demo-password")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "synthetic-review-owner-password")
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    call_command("seed_demo", announce_accounts=True, stdout=StringIO())
    account = User.objects.get(username="demo_technical")
    assert account.credentials_locked
    ended = AccountSession.objects.create(
        user=account, ended_at=timezone.now(), ending=AccountSession.Ending.INACTIVITY
    )
    draft_data = {"user": account, "session": ended, "browser": "a" * 64}
    draft = KeptDraft.objects.create(
        **draft_data, form="ticket-entry-1", values={"body": "Synthetic demo note"}
    )
    marker = KeptDraft.objects.create(
        **draft_data, form="ticket-entry-2", values=None, taken_at=timezone.now()
    )
    assert KeptDraft._meta.db_table in demo.nonempty_tables()
    call_command("seed_demo", announce_accounts=True, stdout=StringIO())
    draft.refresh_from_db()
    marker.refresh_from_db()
    assert draft.values == {"body": "Synthetic demo note"} and marker.taken_at is not None

    call_command("reset_demo", interactive=False, announce_accounts=True, stdout=StringIO())
    assert not KeptDraft.objects.exists() and not AccountSession.objects.exists()
    assert User.objects.get(username="demo_technical").credentials_locked


@pytest.mark.django_db(transaction=True)
def test_old_worker_signout_removes_account_copies_on_upgraded_schema():
    """Exercise an actual previous-release worker during a rolling deployment."""

    previous = os.environ.get("KEPT_REVIEW_BASE")
    if not previous:
        pytest.skip("Set KEPT_REVIEW_BASE to the previous checkout with its .venv")
    name = connection.settings_dict["NAME"]
    assert name.startswith("test_") and name == os.environ["TEST_DATABASE_NAME"]
    env = os.environ.copy()
    env.pop("TEST_DATABASE_NAME")
    env["DATABASE_URL"] = urlsplit(env["DATABASE_URL"])._replace(path="/" + name).geturl()
    env["DJANGO_SETTINGS_MODULE"] = "config.settings"
    # Use the migrated model so the previous checkout can run the same check.
    schema = MigrationExecutor(connection).loader.project_state([("accounts", "0005_kept_draft")])
    draft_model = schema.apps.get_model("accounts", "KeptDraft")
    account = User.objects.create_user("rolling-review")
    ended = AccountSession.objects.create(
        user=account, ended_at=timezone.now(), ending=AccountSession.Ending.INACTIVITY
    )
    draft_model.objects.create(
        user_id=account.pk,
        session_id=ended.pk,
        browser="b" * 64,
        form="ticket-entry-1",
        values={"body": "Synthetic rolling release note"},
    )
    script = """
import sys
import django
django.setup()
from django.db import connection
from django.test import Client
from django.urls import reverse
from accounts.models import User
assert connection.settings_dict['NAME'] == sys.argv[1]
browser = Client()
browser.force_login(User.objects.get(pk=sys.argv[2]))
assert browser.get(reverse('account-session')).json()['signed_in'] is True
assert browser.post(reverse('account_logout')).status_code == 302
assert browser.get(reverse('account-session')).json()['signed_in'] is False
"""
    subprocess.run(
        [str(Path(previous) / ".venv/bin/python"), "-c", script, name, str(account.pk)],
        cwd=previous,
        env=env,
        check=True,
        timeout=30,
    )
    assert AccountSession.objects.filter(
        user=account, ending=AccountSession.Ending.SIGNED_OUT
    ).exists()
    # Adopted: the previous release cannot remove the copy; it is never given back after a Sign
    # out recorded by any release, and the scheduled job removes it 24 hours after its session.
    request = RequestFactory().get("/")
    request.user = account
    request.account_session = SimpleNamespace(id=AccountSession.objects.create(user=account).pk)
    request.COOKIES[kept.BROWSER_COOKIE] = "c" * 43
    draft_model.objects.filter(user_id=account.pk).update(
        browser=hashlib.sha256(("c" * 43).encode()).hexdigest()
    )
    assert kept.offer(request, "ticket-entry-1", ["body"]) is None
    assert draft_model.objects.get(user_id=account.pk).taken_at is None


@pytest.mark.django_db(transaction=True)
def test_cleanup_skips_held_draft_and_removes_available_draft():
    now = timezone.now()
    account = User.objects.create_user("cleanup-review")
    ended = AccountSession.objects.create(
        user=account,
        ending=AccountSession.Ending.INACTIVITY,
        ended_at=now - timedelta(hours=25),
        started_at=now - timedelta(hours=26),
        last_active_at=now - timedelta(hours=26),
    )
    draft_fields = {
        "user": account,
        "session": ended,
        "browser": "c" * 64,
        "values": {"body": "Synthetic cleanup note"},
    }
    held = KeptDraft.objects.create(form="ticket-entry-1", **draft_fields)
    available = KeptDraft.objects.create(form="ticket-entry-2", **draft_fields)

    def cleanup_on_another_connection():
        try:
            with transaction.atomic():
                # Bound the test if a cleanup waits for the holder instead of skipping its row.
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '1s'")
                return kept.remove_expired(now)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            KeptDraft.objects.select_for_update().get(pk=held.pk)
            # Adopted: the job also counts what it left for the next run.
            assert pool.submit(cleanup_on_another_connection).result(timeout=10) == {
                "kept_removed": 1,
                "busy": 1,
            }
            assert not KeptDraft.objects.filter(pk=available.pk).exists()
            assert KeptDraft.objects.filter(pk=held.pk).exists()

    assert kept.remove_expired(now) == {"kept_removed": 1, "busy": 0}
    assert not KeptDraft.objects.filter(user=account).exists()


@pytest.mark.parametrize("dialog", [False, True], ids=["window", "dialog"])
def test_unmarked_submit_preserves_the_session_refusal_contract(review_form_cases, dialog):
    case = review_form_cases
    browser = Client()
    browser.force_login(case.agent)
    page = reverse("desk:request-detail", args=[case.ticket.pk])
    action = reverse("desk:request-entry", args=[case.ticket.pk])
    assert browser.get(page).status_code == 200
    _review_expire(case.agent)
    text = "Check the blue boxes at ten"
    headers = {"HTTP_REFERER": "http://testserver" + page}
    if dialog:
        headers["HTTP_X_VR_FORM"] = "1"
    response = browser.post(action, {"kind": "note", "body": text}, **headers)
    address = sessions.sign_in_url(page)
    if dialog:
        assert response.status_code == 401 and response["HX-Redirect"] == address
        # The refusal also says whether what this form posted is the copy kept (new-ticket batch).
        assert response.json() == {"signed_in": False, "sign_in": address, "kept": False}
    else:
        assert response.status_code == 302 and response.url == address
    assert not case.ticket.entries.filter(body=text).exists()
    assert "no-store" in response["Cache-Control"]
    assert browser.get(reverse("account-session")).json()["signed_in"] is False
    assert browser.post(reverse("account-session-continue")).json()["signed_in"] is False
    assert not AccountSession.objects.filter(user=case.agent, ended_at__isnull=True).exists()


def test_pressing_sign_out_as_the_session_expires_discards_existing_copies(review_form_cases):
    """The account presses Sign out with an unclaimed copy from an earlier session."""
    case = review_form_cases
    page = reverse("portal:request-create")
    browser = Client()
    browser.force_login(case.portal)
    drawn = browser.get(page)
    if hasattr(sessions, "KEEP_ROUTE"):
        token = review_pages.marker(drawn)[0]
        review_pages.end_open(case.portal)
        assert review_pages.send(browser, token, {"description": "Return the crates"}) == 1
    browser.force_login(case.portal)
    _review_expire(case.portal)
    response = browser.post(reverse("account_logout"), HTTP_REFERER="http://testserver" + page)
    assert response.status_code == 302
    assert browser.get(reverse("account-session")).json()["signed_in"] is False
    if hasattr(sessions, "KEEP_ROUTE"):
        assert not KeptDraft.objects.filter(user=case.portal).exists()


def test_keep_processes_at_most_the_advertised_number_of_forms(review_form_cases):
    case = review_form_cases
    browser = review_pages.browser_for(case.portal)
    batch = []
    for index in range(kept.MAX_FORMS + 1):
        template = RequestTemplate.objects.create(
            key=f"route-{index}",
            name=f"Route {index}",
            category=case.category,
            offered_in_portal=True,
        )
        page = browser.get(reverse("portal_forms:raise", args=[template.key]))
        batch.append({"token": review_pages.marker(page)[0], "values": {"description": str(index)}})
    review_pages.end_open(case.portal)
    answer = browser.post(reverse("account-session-keep"), {"forms": batch}, "application/json")
    # The keep answer also says, for each form sent, whether a copy is held (new-ticket batch).
    assert answer.json() == {"kept": kept.MAX_FORMS, "held": [True] * kept.MAX_FORMS}
    assert set(KeptDraft.objects.values_list("values__description", flat=True)) == {
        str(index) for index in range(kept.MAX_FORMS)
    }


def test_logged_contact_restores_direction_channel_recipient_and_outcome(review_form_cases):
    case = review_form_cases
    browser, page, token = _composer(case)
    action = reverse("desk:request-entry", args=[case.ticket.pk])
    fields = {
        "kind": "contact",
        "body": "Dispatcher will confirm the collection.",
        "contact_direction": "to_client",
        "contact_channel": "phone",
        "contact_with": "Example dispatch desk",
        "contact_outcome": "message_left",
        "new_status": "waiting_client",
    }
    before = case.ticket.entries.count()
    _review_expire(case.agent)
    result = browser.post(
        action, {**fields, kept.FIELD: token}, HTTP_REFERER="http://testserver" + page
    )
    assert result.status_code == 302 and case.ticket.entries.count() == before
    browser.force_login(case.agent)
    assert review_pages.given_back(browser.get(page)) == fields
