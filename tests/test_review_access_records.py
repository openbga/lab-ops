"""Independent read-accounting regressions using synthetic records and local PostgreSQL.

Adopted from the round-1 independent review of access records (the reviewer's original is kept
outside the repository). Adaptations are marked "Adapted": the write hook now receives the
records the answer carries, the pager's `upto`, and two findings resolved the other way (the
complaint forms are recorded; the help now says Assign and Status show only the number). No
browser import: CI's unit job installs no Playwright."""

# The imported pytest fixtures are resolved by parameter name.
# ruff: noqa: F811
import io
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.apps import apps
from django.db import connection, connections
from django.test import Client
from django.urls import reverse

from request_forms.models import FieldValue
from request_forms.tests.conftest import (  # noqa: F401
    PASSWORD,
    PATIENT_SENTINEL,
    admin,
    agent,
    capable,
    catalog,
    clinic,
    clinic_site,
    colleague,
    group,
    logistics,
    make_user,
    other_org,
    outsider,
    pickup,
    pickup_category,
    portal_user,
)
from request_forms.tests.test_pages import raised

pytestmark = pytest.mark.django_db


@pytest.fixture
def review_ticket(portal_user, pickup):
    return raised(portal_user, pickup, {"count": 2, "patient": PATIENT_SENTINEL})


@pytest.fixture
def review_patient(review_ticket):
    return FieldValue.objects.get(filled_form__request=review_ticket, key="patient")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("atomic_requests", [False, True])
def test_reveal_response_log_change_event_and_access_record_agree(
    client, capable, review_ticket, review_patient, atomic_requests, monkeypatch
):
    from core import access
    from core.models import AccessRecord, AuditEvent
    from core.request_logging import RequestIdFilter, current_request_id, logger

    client.force_login(capable)
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", atomic_requests)
    outer_write = access.write
    write_state = []

    def check_transaction(request, *records):
        write_state.append(connection.in_atomic_block)
        return outer_write(request, *records)

    monkeypatch.setattr(access, "write", check_transaction)
    log = io.StringIO()
    handler = logging.StreamHandler(log)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(logging.Formatter("%(request_id)s %(message)s"))
    logger.addHandler(handler)
    try:
        response = client.post(
            reverse("request_details:reveal", args=[review_patient.pk]),
            HTTP_X_REQUEST_ID="client-supplied-id",
        )
    finally:
        logger.removeHandler(handler)
    assert response.status_code == 200 and PATIENT_SENTINEL.encode() in response.content
    ref = response["X-Request-ID"]
    assert len(ref) == 32 and int(ref, 16) and ref != "client-supplied-id"
    assert write_state == [False] and current_request_id() == ""
    record = AccessRecord.objects.get()
    assert (record.account_id, record.capacity, record.kind, record.ticket_id) == (
        capable.pk,
        "laboratory",
        "patient_answer",
        review_ticket.pk,
    )
    assert (record.object_type, record.object_id, record.request_ref) == (
        "request_forms.fieldvalue",
        str(review_patient.pk),
        ref,
    )
    event = AuditEvent.objects.get(action="request_form.value_revealed")
    assert event.request_ref == ref
    (line,) = log.getvalue().splitlines()
    assert line.startswith(f"{ref} method=POST ") and "status=200" in line
    assert "client-supplied-id" not in line
    assert not any(
        PATIENT_SENTINEL in str(item)
        for item in (record.__dict__, line, event.before, event.after, event.reason)
    )


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_readers_get_separate_correlated_batches(
    portal_user, agent, review_ticket, review_patient, monkeypatch
):
    from core import access
    from core.models import AccessRecord

    portal, desk = Client(), Client()
    portal.force_login(portal_user)
    desk.force_login(agent)
    gate = Barrier(2, timeout=12)
    write = access.write

    def write_together(request, *records):
        gate.wait()
        return write(request, *records)

    def visit(browser, url):
        try:
            response = browser.get(url)
            assert response.status_code == 200
            return response["X-Request-ID"]
        finally:
            connections.close_all()

    monkeypatch.setattr(access, "write", write_together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        staff_read = pool.submit(
            visit, desk, reverse("desk:request-detail", args=[review_ticket.pk])
        )
        portal_read = pool.submit(
            visit, portal, reverse("portal:request-detail", args=[review_ticket.number])
        )
        staff_ref, portal_ref = staff_read.result(timeout=20), portal_read.result(timeout=20)
    assert staff_ref != portal_ref
    assert sorted(AccessRecord.objects.values_list("request_ref", "kind", "account_id")) == sorted(
        [
            (staff_ref, "ticket", agent.pk),
            (portal_ref, "ticket", portal_user.pk),
            (portal_ref, "patient_answer", portal_user.pk),
        ]
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.confirmation
def test_discarded_response_after_session_ends_is_not_a_shown_answer(
    client, capable, review_patient, monkeypatch
):
    """The two tabs share a sign-in. Django discards the reveal on saving its session."""
    from django.contrib.sessions.middleware import SessionMiddleware

    from core.models import RevealConfirmationRule

    RevealConfirmationRule.objects.create(pk=1, first_reveal=True, revision=1)
    client.force_login(capable)
    reveal_url = reverse("request_details:reveal", args=[review_patient.pk])
    confirm_url = reverse("account-confirm-reveal")
    for headers, status, location in [
        ({}, 302, "Location"),
        ({"HTTP_HX_REQUEST": "true"}, 204, "HX-Redirect"),
    ]:
        refusal = client.post(reveal_url, **headers)
        assert refusal.status_code == status and refusal[location].startswith(confirm_url)
        if "accessrecord" in apps.get_app_config("core").models:
            assert review_pages_records(refusal) == []
    confirmed = client.post(reverse("account_reauthenticate"), {"password": PASSWORD})
    assert confirmed.status_code == 302
    other_tab = Client()
    other_tab.cookies.update(client.cookies)
    original = SessionMiddleware.process_response

    def sign_out():
        try:
            return other_tab.post(reverse("account_logout")).status_code
        finally:
            connections.close_all()

    def finish_request(middleware, request, response):
        if request.path != reveal_url:
            return original(middleware, request, response)
        assert request.session.modified and PATIENT_SENTINEL.encode() in response.content
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(sign_out).result(timeout=10) == 302
        return original(middleware, request, response)

    monkeypatch.setattr(SessionMiddleware, "process_response", finish_request)
    response = client.post(reveal_url)
    assert response.status_code == 400 and PATIENT_SENTINEL.encode() not in response.content
    if "accessrecord" in apps.get_app_config("core").models:
        assert review_pages_records(response) == []


@pytest.mark.parametrize(
    ("failure", "status"), [("server", 500), ("not_found", 404), ("permission", 403)]
)
def test_replaced_template_records_only_the_content_delivered(
    client, portal_user, review_ticket, monkeypatch, failure, status
):
    """The inclusion tag has noted an answer when drawing the remainder of the page fails."""
    from django.core.exceptions import PermissionDenied
    from django.http import Http404

    from core import access
    from core.models import AccessRecord
    from desk import portal_views

    original = portal_views.render
    drawn = []

    def interrupted(*args, **kwargs):
        page = original(*args, **kwargs)
        drawn.append(PATIENT_SENTINEL.encode() in page.content)
        assert [row.kind for row in access.noted(args[0])] == ["patient_answer"]
        exceptions = {"server": RuntimeError, "not_found": Http404, "permission": PermissionDenied}
        raise exceptions[failure]("Synthetic view interruption")

    client.force_login(portal_user)
    monkeypatch.setattr(portal_views, "render", interrupted)
    client.raise_request_exception = False
    response = client.get(reverse("portal:request-detail", args=[review_ticket.number]))
    assert drawn == [True] and response.status_code == status
    assert PATIENT_SENTINEL.encode() not in response.content
    assert not AccessRecord.objects.filter(request_ref=response["X-Request-ID"]).exists()


def test_exception_report_omits_ticket_reply_body(
    client, portal_user, review_ticket, settings, monkeypatch
):
    """Inspect the standard production reporter after an interrupted ticket redraw."""
    from django.views.debug import ExceptionReporter

    from desk import portal_views

    settings.DEBUG = False
    client.force_login(portal_user)
    client.raise_request_exception = False
    marker = "synthetic-unsent-reply-4081"
    render = portal_views.render

    def interrupted(*args, **kwargs):
        page = render(*args, **kwargs)
        assert marker.encode() in page.content
        raise RuntimeError("Synthetic failure after drawing the reply")

    monkeypatch.setattr(portal_views, "render", interrupted)
    response = client.post(
        reverse("portal:request-reply", args=[review_ticket.number]),
        {"body": marker, "expected_revision": 0},
        secure=True,
    )
    assert response.status_code == 500 and marker.encode() not in response.content
    assert isinstance(response.exc_info[1], RuntimeError)
    reporter = ExceptionReporter(response.wsgi_request, *response.exc_info)
    data = dict(reporter.get_traceback_data()["filtered_POST_items"])
    assert data.get("body") != marker, "The production error reporter retains the submitted body"


def test_opened_next_page_continues_with_older_records(client, capable, review_ticket):
    """Following Next in a quiet system must not show the same read on consecutive pages."""
    from html.parser import HTMLParser
    from urllib.parse import urljoin

    from django.urls import reverse

    from core.models import AccessRecord
    from request_forms.tests.test_pages import login

    class NextPage(HTMLParser):
        href = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "a" and attrs.get("rel") == "next":
                self.href = attrs["href"]

    refs = [f"{i:032x}" for i in range(1, 31)]
    AccessRecord.objects.bulk_create(
        [
            AccessRecord(
                account=capable,
                capacity="laboratory",
                kind="ticket",
                ticket_id=review_ticket.pk,
                request_ref=ref,
            )
            for ref in refs
        ]
    )
    login(client, capable)
    url = reverse("desk:request-access", args=[review_ticket.pk])
    first = client.get(url)
    assert first.status_code == 200
    assert [row.request_ref for row in first.context["page_obj"]] == refs[::-1][:25]
    pager = NextPage()
    pager.feed(first.content.decode())
    assert pager.href
    second = client.get(urljoin(url, pager.href))
    assert second.status_code == 200
    assert [row.request_ref for row in second.context["page_obj"]] == refs[::-1][25:]


def test_opened_missing_person_and_ticket_filter(
    client, capable, make_user, review_ticket, pickup, portal_user
):
    from django.urls import reverse

    from core.models import AccessRecord
    from request_forms.tests.test_pages import login, raised

    reader = make_user("review_no_longer_here", "technical")
    pk = reader.pk
    other_ticket = raised(portal_user, pickup, {"count": 1})
    for ticket, ref in [(review_ticket, "a" * 32), (other_ticket, "b" * 32)]:
        AccessRecord.objects.create(
            account=reader,
            capacity="laboratory",
            kind="ticket",
            ticket_id=ticket.pk,
            request_ref=ref,
        )
    reader.delete()
    login(client, capable)
    response = client.get(reverse("desk:request-access", args=[review_ticket.pk]))
    assert response.status_code == 200
    [row] = response.context["page_obj"]
    assert (row.person, row.username, row.request_ref) == (
        f"Account {pk}",
        "no longer exists",
        "a" * 32,
    )
    assert "b" * 32 not in response.content.decode()


def test_opened_full_mixed_page_uses_bounded_queries(
    agent, review_ticket, django_assert_num_queries
):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from attachments.models import Attachment
    from attachments.services import add_entry_with_files
    from core.models import AccessRecord
    from desk import opened
    from desk.models import EntryKind, ServiceRequest
    from request_forms.models import FieldValue
    from request_forms.tests.test_access_records import png

    value = FieldValue.objects.get(filled_form__request=review_ticket, key="patient")
    entry = add_entry_with_files(
        actor=agent,
        request=review_ticket,
        kind=EntryKind.REPLY,
        body="Synthetic reply.",
        expected_revision=ServiceRequest.objects.get(pk=review_ticket.pk).revision,
        files=[png()],
    )
    attachment = Attachment.objects.get(entry=entry)
    pair = [("patient_answer", value), ("file", attachment)]

    def make_rows(n):
        for i in range(n):
            kind, target = pair[i % len(pair)]
            AccessRecord.objects.create(
                account=agent,
                kind=kind,
                capacity="laboratory",
                ticket_id=review_ticket.pk,
                object_type=target._meta.label_lower,
                object_id=str(target.pk),
            )

    make_rows(2)
    with CaptureQueriesContext(connection) as small:
        sample = opened.page(review_ticket, 1)
    assert {row.detail for row in sample} == {value.label, attachment.download_name}
    make_rows(23)
    with django_assert_num_queries(len(small)):
        full = opened.page(review_ticket, 1)
        assert len(full) == 25
        assert {row.detail for row in full} == {value.label, attachment.download_name}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("atomic_requests", [False, True])
def test_opened_actor_and_list_share_snapshot(
    client, capable, agent, review_ticket, monkeypatch, atomic_requests
):
    """A concurrent rename and read appear together on the subsequent visit."""
    from concurrent.futures import ThreadPoolExecutor

    from django.db import connection, transaction
    from django.urls import reverse

    from accounts.models import User
    from core.models import AccessRecord
    from desk import opened
    from request_forms.tests.test_pages import login

    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", atomic_requests)

    def record(ref):
        AccessRecord.objects.create(
            account_id=agent.pk,
            capacity="laboratory",
            kind="ticket",
            ticket_id=review_ticket.pk,
            request_ref=ref,
        )

    def commit_rename_and_read():
        try:
            with transaction.atomic():
                User.objects.filter(pk=agent.pk).update(first_name="Changed")
                record("b" * 32)
        finally:
            connection.close()

    record("a" * 32)
    login(client, capable)
    original = opened.page

    def page_after_commit(ticket, number, *upto):  # adapted: the pager's `upto` (round-1 fix)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(commit_rename_and_read).result(timeout=10)
        return original(ticket, number, *upto)

    url = reverse("desk:request-access", args=[review_ticket.pk])
    with monkeypatch.context() as patch:
        patch.setattr(opened, "page", page_after_commit)
        first = client.get(url)
    assert first.status_code == 200
    assert [(row.person, row.request_ref) for row in first.context["page_obj"]] == [
        (agent.username, "a" * 32)
    ]
    second = client.get(url)
    assert second.status_code == 200
    readers = {row.request_ref: row.person for row in second.context["page_obj"]}
    assert readers["a" * 32] == readers["b" * 32] == "Changed"


def test_opened_help_distinguishes_reveal_event_from_ticket_history(client, capable, review_ticket):
    """Check the help for the Form details panel against the actual ticket history it names."""
    import json
    from pathlib import Path

    from django.conf import settings
    from django.urls import reverse

    from core.models import AuditEvent
    from request_forms.models import FieldValue
    from request_forms.tests.test_pages import login

    login(client, capable)
    value = FieldValue.objects.get(filled_form__request=review_ticket, key="patient")
    assert client.post(reverse("request_details:reveal", args=[value.pk])).status_code == 200
    event = AuditEvent.objects.get(
        action="request_form.value_revealed", object_id=str(value.filled_form_id)
    )
    assert event.after["value_id"] == value.pk
    history = client.get(reverse("desk:request-history", args=[review_ticket.pk]))
    assert history.status_code == 200
    assert event.action not in [row.action for row in history.context["events"]]
    help_path = Path(settings.BASE_DIR) / "docs/pages/desk/request-detail.json"
    fields = [
        i
        for section in json.loads(help_path.read_text())["sections"]
        for i in section.get("items", [])
    ]
    field = next(i for i in fields if i["label"] == "Form details")
    assert "recorded in the audit history" not in field["description"]


def review_pages_records(response):
    from core.models import AccessRecord

    return list(
        AccessRecord.objects.filter(request_ref=response["X-Request-ID"])
        .order_by("kind", "id")
        .values_list("kind", "account_id", "capacity", "ticket_id", "object_type", "object_id")
    )


@pytest.mark.parametrize(
    ("role", "supervisor", "reads_tickets", "flags"),
    [
        ("client_services", False, True, {}),
        ("account_manager", False, True, {}),
        ("technical", False, True, {}),
        ("supervisor", True, True, {}),
        ("admin", True, True, {}),
        ("client_services", False, True, {"credentials_locked": True}),
        (None, False, False, {}),
        (None, False, False, {"is_superuser": True, "is_staff": True}),
    ],
    ids=[
        "client-services",
        "account-manager",
        "technical",
        "supervisor",
        "admin",
        "shared",
        "no-role",
        "superuser-only",
    ],
)
def test_pages_staff_roles_and_supervisor_reads(
    client, make_user, review_ticket, role, supervisor, reads_tickets, flags
):
    roles = [role] if role else []
    account = make_user("review_staff", *roles, **flags)
    client.force_login(account)
    for route, kind, permitted in [
        ("desk:request-detail", "ticket", reads_tickets),
        ("desk:request-history", "history", supervisor),
        ("desk:request-access", "access", supervisor),
        ("request_details:edit", "form_details", reads_tickets),
    ]:
        key = review_ticket.number if route == "request_details:edit" else review_ticket.pk
        response = client.get(reverse(route, args=[key]))
        assert response.status_code == (200 if permitted else 403), route
        assert review_pages_records(response) == (
            [(kind, account.pk, "laboratory", review_ticket.pk, "", "")] if permitted else []
        ), route


def test_pages_anonymous_and_inactive_read_no_ticket(client, agent, review_ticket):
    from accounts.models import User

    url = reverse("desk:request-detail", args=[review_ticket.pk])
    responses = [client.get(url)]
    client.force_login(agent)
    User.objects.filter(pk=agent.pk).update(is_active=False)
    responses.append(client.get(url))
    for response in responses:
        assert response.status_code == 302
        assert review_pages_records(response) == []


def test_pages_portal_scope_and_multiple_marked_values(
    client, portal_user, colleague, outsider, make_user, clinic, clinic_site, pickup
):
    from clients.models import PortalAccess
    from request_forms.models import FieldKind, FieldValue
    from request_forms.tests.test_pages import raised

    pickup.fields.create(
        key="second-marker",
        label="Other identifier",
        kind=FieldKind.SHORT_TEXT,
        holds_patient_information=True,
    )
    ticket = raised(
        portal_user,
        pickup,
        {"count": 2, "patient": "synthetic-id-a", "second-marker": "synthetic-id-b"},
        site=clinic_site,
    )
    fields = list(
        FieldValue.objects.filter(
            filled_form__request=ticket, holds_patient_information=True
        ).values_list("pk", flat=True)
    )
    assert len(fields) == 2
    site_reader = make_user("review_site", client=clinic)
    grant = PortalAccess.objects.create(user=site_reader, client=clinic, whole_organization=False)
    grant.sites.add(clinic_site)
    url = reverse("portal:request-detail", args=[ticket.number])
    for account in (colleague, site_reader, outsider):
        client.force_login(account)
        response = client.get(url, HTTP_HX_REQUEST="true")
        if account == outsider:
            assert response.status_code == 404
            assert review_pages_records(response) == []
        else:
            assert response.status_code == 200
            assert all(
                value in response.content for value in (b"synthetic-id-a", b"synthetic-id-b")
            )
            expected = [("ticket", account.pk, "client", ticket.pk, "", "")]
            expected.extend(
                (
                    "patient_answer",
                    account.pk,
                    "client",
                    ticket.pk,
                    "request_forms.fieldvalue",
                    str(pk),
                )
                for pk in fields
            )
            assert sorted(review_pages_records(response)) == sorted(expected)
        client.logout()


def test_pages_portal_head_never_shows_a_patient_answer(client, portal_user, review_ticket):
    from request_forms.models import FieldValue

    patient = FieldValue.objects.get(filled_form__request=review_ticket, key="patient")
    client.force_login(portal_user)
    url = reverse("portal:request-detail", args=[review_ticket.number])
    get = client.get(url)
    assert get.status_code == 200
    assert str(patient.value).encode() in get.content
    if "accessrecord" in apps.get_app_config("core").models:
        assert any(record[0] == "patient_answer" for record in review_pages_records(get))
    head = client.head(url)
    assert head.status_code == 200 and head.content == b""
    if "accessrecord" in apps.get_app_config("core").models:
        assert not any(record[0] == "patient_answer" for record in review_pages_records(head))


def test_pages_methods_redirects_and_lists(client, agent, capable, review_ticket):
    from request_forms.models import FieldValue

    pk = review_ticket.pk
    patient = FieldValue.objects.get(filled_form__request=review_ticket, key="patient")
    client.force_login(agent)
    detail = reverse("desk:request-detail", args=[pk])
    for url, params in [
        (reverse("desk:request-list"), {}),
        (reverse("desk:request-find"), {"q": review_ticket.subject}),
        (reverse("clients:client-timeline", args=[review_ticket.client_id]), {"kind": "unknown"}),
    ]:
        page = client.get(url, params)
        assert page.status_code == 200 and not review_pages_records(page)
    for url, params in [
        (reverse("desk:request-find"), {"q": review_ticket.number}),
        (reverse("desk:request-entry", args=[pk]), {}),
        (detail.rstrip("/"), {}),
    ]:
        page = client.get(url, params)
        assert page.status_code in (301, 302) and not review_pages_records(page)
        assert page["Location"].split("?")[0] == detail
    # A padded numeric address and a posted direct request both draw the ordinary staff page.
    for page in [client.get(detail.replace(f"/{pk}/", f"/00{pk}/")), client.post(detail)]:
        assert page.status_code == 200
        assert review_pages_records(page) == [("ticket", agent.pk, "laboratory", pk, "", "")]
    client.logout()
    client.force_login(capable)
    access_url = reverse("desk:request-access", args=[pk])
    reveal_url = reverse("request_details:reveal", args=[patient.pk])
    for page in [
        client.head(access_url),
        client.post(access_url),
        client.get(reveal_url),
        client.head(reveal_url),
    ]:
        assert page.status_code == 405 and not review_pages_records(page)
    for hx in ({}, {"HTTP_HX_REQUEST": "true"}):
        shown = client.post(reveal_url, **hx)
        assert shown.status_code == 200 and str(patient.value).encode() in shown.content
        assert review_pages_records(shown) == [
            (
                "patient_answer",
                capable.pk,
                "laboratory",
                pk,
                "request_forms.fieldvalue",
                str(patient.pk),
            )
        ]


def test_pages_request_again_accounts_for_the_source_ticket(
    client,
    portal_user,
    pickup,
):
    from request_forms.models import FieldKind
    from request_forms.tests.test_pages import raised

    pickup.fields.create(key="courier-hint", label="Pickup note", kind=FieldKind.SHORT_TEXT)
    earlier = raised(
        portal_user,
        pickup,
        {
            "count": 1,
            "courier-hint": "Copyable synthetic note",
            "patient": "Synthetic patient marker",
        },
    )
    client.force_login(portal_user)
    response = client.get(
        reverse("portal_forms:raise", args=[pickup.key]), {"again": earlier.number}
    )
    assert response.status_code == 200 and b"Copyable synthetic note" in response.content
    assert b"Synthetic patient marker" not in response.content
    assert f"Answers copied from {earlier.number}".encode() in response.content
    if "accessrecord" in apps.get_app_config("core").models:
        # The brief asks which single-ticket reads expose saved answers outside lists/searches.
        rows = review_pages_records(response)
        assert any(row[3] == earlier.pk for row in rows), "No record names the source ticket"


def test_pages_complaint_actions_show_previously_stored_narrative(
    client, capable, admin, review_ticket
):
    from desk import services

    ticket = services.set_complaint(
        actor=admin,
        request=review_ticket,
        expected_revision=review_ticket.revision,
        is_complaint=True,
        reason="Synthetic review",
    )
    services.decide_complaint(
        actor=admin,
        request=ticket,
        expected_revision=ticket.revision,
        decision="investigate",
        rationale="Synthetic stored rationale",
    )
    ticket.refresh_from_db()
    services.update_complaint(
        actor=admin,
        request=ticket,
        expected_revision=ticket.revision,
        findings="Synthetic stored finding",
    )
    client.force_login(capable)
    for route, text in [
        ("desk:complaint-decision", b"Synthetic stored rationale"),
        ("desk:complaint-record", b"Synthetic stored finding"),
        ("desk:complaint-review", b"Synthetic stored finding"),
    ]:
        response = client.get(reverse(route, args=[ticket.pk]))
        assert response.status_code == 200 and text in response.content
        # Adapted (round-1 finding 8, resolved by recording these forms): one record each.
        if "accessrecord" in apps.get_app_config("core").models:
            assert review_pages_records(response) == [
                ("complaint", capable.pk, "laboratory", ticket.pk, "", "")
            ]


@pytest.mark.parametrize("route", ["desk:request-assign", "desk:request-status"])
def test_pages_action_forms_identify_the_ticket_as_help_describes(
    client, agent, portal_user, pickup, route
):
    from request_forms.tests.test_pages import raised

    ticket = raised(portal_user, pickup, {"count": 1}, subject="Synthetic subject for action help")
    client.force_login(agent)
    response = client.get(reverse(route, args=[ticket.pk]))
    assert response.status_code == 200 and ticket.number.encode() in response.content
    # Adapted (round-1 finding 8, resolved by correcting the help): these forms show the ticket's
    # number only, as the help now says, and are not recorded.
    assert ticket.subject.encode() not in response.content
    assert review_pages_records(response) == []
    import json
    from pathlib import Path

    help_text = json.dumps(json.loads(Path("docs/pages/desk/request-access.json").read_text()))
    assert "action pages that show only the ticket's number, such as Assign, Status" in help_text


def test_pages_file_methods_and_rendered_bytes(client, agent, portal_user, review_ticket):
    from attachments.inspection import inspect_bytes
    from attachments.services import add_entry_with_files
    from desk.models import EntryKind

    data = b"Ordinary synthetic file for response verification.\n"
    file = add_entry_with_files(
        actor=agent,
        request=review_ticket,
        kind=EntryKind.REPLY,
        body="Synthetic reply",
        expected_revision=review_ticket.revision,
        files=[inspect_bytes("synthetic-public.txt", data)],
    ).attachments.get()
    url = reverse("attachments:download", args=[file.pk])
    for account, capacity in [(portal_user, "client"), (agent, "laboratory")]:
        client.force_login(account)
        for response in [client.head(url), client.post(url)]:
            assert response.status_code == 405 and review_pages_records(response) == []
        response = client.get(url, HTTP_HX_REQUEST="true")
        assert response.status_code == 200 and response.content == data
        assert review_pages_records(response) == [
            ("file", account.pk, capacity, review_ticket.pk, "attachments.attachment", str(file.pk))
        ]
        client.logout()


@pytest.mark.django_db(transaction=True)
def test_storage_0009_upgrade_overlapping_old_writer_and_reverse():
    """A real pre-0009 ORM insert waits for the DDL and keeps its original payload."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue

    from django.db import connection, connections, transaction
    from django.db.migrations.executor import MigrationExecutor

    from accounts.models import User
    from core.models import AccessRecord, AuditEvent

    previous = [("core", "0008_reveal_confirmation_rule")]
    migration = [("core", "0009_access_records")]
    executor = MigrationExecutor(connection)
    original = executor.loader.graph.leaf_nodes()
    actor = User.objects.create_user("migration-reader", "migration@example.invalid")
    old_event = executor.loader.project_state(previous).apps.get_model("core", "AuditEvent")

    def old_insert(action):
        return old_event.objects.create(
            actor_id=actor.pk,
            action=action,
            object_type="core.lab",
            object_id="1",
            after={"count": 3},
        ).pk

    def overlapping_insert(pids):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '12s'")
                cursor.execute("SELECT pg_backend_pid()")
                pids.put(cursor.fetchone()[0])
            return old_insert("review.upgrading")
        finally:
            connections.close_all()

    try:
        executor.migrate(previous)
        before = old_insert("review.before")
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                MigrationExecutor(connection).migrate(migration)
                pids = Queue()
                pending = pool.submit(overlapping_insert, pids)
                pid = pids.get(timeout=10)
                deadline = time.monotonic() + 8
                with connection.cursor() as cursor:
                    while True:
                        cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
                        if cursor.fetchone()[0]:
                            break
                        assert not pending.done(), "Old writer did not wait for the uncommitted DDL"
                        assert time.monotonic() < deadline, "No lock wait observed for old writer"
                        time.sleep(0.01)
            overlap = pending.result(timeout=10)

        assert sorted(
            AuditEvent.objects.filter(pk__in=[before, overlap]).values_list(
                "action", "request_ref", "after"
            )
        ) == [("review.before", "", {"count": 3}), ("review.upgrading", "", {"count": 3})]
        current = AuditEvent.objects.create(
            actor=actor,
            action="review.after",
            object_type="core.lab",
            object_id="1",
            request_ref="1" * 32,
        )
        AccessRecord.objects.create(
            account=actor, capacity="laboratory", kind="ticket", ticket_id=1
        )

        MigrationExecutor(connection).migrate(previous)
        assert AccessRecord._meta.db_table not in connection.introspection.table_names()
        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(
                cursor, AuditEvent._meta.db_table
            )
        assert "request_ref" not in {column.name for column in columns}
        assert old_event.objects.filter(pk__in=[before, overlap, current.pk]).count() == 3
    finally:
        MigrationExecutor(connection).migrate(original)
    assert not AccessRecord.objects.exists()
    assert set(
        AuditEvent.objects.filter(pk__in=[before, overlap, current.pk]).values_list(
            "request_ref", flat=True
        )
    ) == {""}


@pytest.mark.django_db
def test_storage_clock_constraints_permissions_and_admin():
    from django.contrib import admin as django_admin
    from django.contrib.auth.models import Permission
    from django.db import IntegrityError, connection, transaction

    from core.models import AccessRecord

    attrs = {"capacity": "laboratory", "kind": "ticket", "account_id": 88, "ticket_id": 10}

    def database_time():
        with connection.cursor() as cursor:
            cursor.execute("SELECT clock_timestamp()")
            return cursor.fetchone()[0]

    before = database_time()
    row = AccessRecord.objects.create(**attrs)
    assert before <= row.occurred_at <= database_time()
    assert not django_admin.site.is_registered(AccessRecord)
    assert not Permission.objects.filter(
        content_type__app_label="core", content_type__model="accessrecord"
    ).exists()

    for updates, constraint in [
        ({"kind": "other"}, "access_record_kind"),
        ({"capacity": "other"}, "access_record_capacity"),
        ({"object_type": "request_forms.fieldvalue"}, "access_record_object"),
        ({"object_id": "1"}, "access_record_object"),
    ]:
        with pytest.raises(IntegrityError) as rejected, transaction.atomic():
            AccessRecord.objects.create(**(attrs | updates))
        assert rejected.value.__cause__.diag.constraint_name == constraint


@pytest.mark.django_db
def test_storage_shared_demo_reads_survive_reseed_and_reset_clears_them(client, monkeypatch):
    from io import StringIO

    from django.core.management import call_command
    from django.urls import reverse

    from accounts.models import User
    from core.models import AccessRecord
    from desk.models import ServiceRequest

    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-shared-demo-password")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "separate-synthetic-admin-secret")
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    options = {
        "announce_accounts": True,
        "verbosity": 0,
        "stdout": StringIO(),
        "stderr": StringIO(),
    }
    call_command("seed_demo", **options)
    shared = User.objects.get(username="demo_technical", credentials_locked=True)
    ticket = ServiceRequest.objects.filter(category__is_restricted=False).first()
    assert ticket is not None
    client.force_login(shared)
    response = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    assert response.status_code == 200
    record = AccessRecord.objects.get(
        account=shared, kind="ticket", ticket_id=ticket.pk, request_ref=response["X-Request-ID"]
    )
    assert record.capacity == "laboratory"
    call_command("seed_demo", **options)
    assert AccessRecord.objects.filter(pk=record.pk, request_ref=record.request_ref).exists()
    call_command("reset_demo", interactive=False, **options)
    assert not AccessRecord.objects.exists()
    assert User.objects.filter(username=shared.username, credentials_locked=True).exists()
    assert ServiceRequest.objects.exists()


@pytest.mark.django_db
def test_storage_deleted_account_keeps_its_number_when_the_pending_record_is_written():
    from django.test import RequestFactory

    from accounts.models import User
    from core import access
    from core.models import AccessRecord
    from desk import opened
    from desk.models import ServiceRequest

    account = User.objects.create_user("retired-reader", "retired@example.invalid")
    ticket = ServiceRequest(pk=71)
    account_id = account.pk
    request = RequestFactory().get("/")
    request.user = account
    access.note(request, "ticket", ticket_id=ticket.pk)
    account.delete()
    access.write(request)

    assert AccessRecord.objects.get().account_id == account_id
    [row] = opened.page(ticket, 1).object_list
    assert (row.person, row.username, row.capacity) == (
        f"Account {account_id}",
        "no longer exists",
        "Laboratory staff",
    )


@pytest.fixture
def review_storage_after_read(monkeypatch):
    """Commit a real change after the response is drawn and immediately before its insert."""
    from django.db import connection

    from core import access
    from core.models import AccessRecord
    from request_forms.tests.test_definition_consistency import other_connection

    write = access.write

    def arrange(change):
        # Adapted: the records reach the write with the answer they belong to (round-1 fix), no
        # longer from the request's notes.
        def write_after_change(request, *records):
            assert records and records[0]
            assert not connection.in_atomic_block
            assert not AccessRecord.objects.filter(request_ref=request.request_id).exists()
            other_connection(change)
            return write(request, *records)

        monkeypatch.setattr(access, "write", write_after_change)

    return arrange


def review_storage_assert_reads(response, account, capacity, ticket, expected):
    from core.models import AccessRecord

    assert response.status_code == 200
    request_id = response["X-Request-ID"]
    assert len(request_id) == 32
    rows = list(
        AccessRecord.objects.values_list(
            "account_id", "capacity", "ticket_id", "request_ref", "kind", "object_type", "object_id"
        )
    )
    assert sorted(rows) == sorted(
        (account.pk, capacity, ticket.pk, request_id, kind, target, pk)
        for kind, target, pk in expected
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["correct-answer", "move-account", "deactivate-account"])
def test_storage_portal_read_keeps_original_account_and_answer_after_commit(
    change, review_storage_after_read, client, portal_user, capable, other_org, review_ticket
):
    from django.urls import reverse

    from accounts.models import User
    from request_forms.models import FieldValue
    from request_forms.services import correct_values
    from request_forms.tests.conftest import definition_of

    answer = FieldValue.objects.get(filled_form__request=review_ticket, key="patient")
    updated = "UPDATED-SYNTHETIC-PATIENT-ANSWER"
    assert answer.value != updated

    def commit_change():
        if change == "correct-answer":
            correct_values(
                actor=capable,
                request=review_ticket,
                expected={"patient": answer.pk},
                expected_definition=definition_of(review_ticket),
                values={"patient": updated},
                reason="",
                reason_code="typing",
            )
        else:
            reader = User.objects.get(pk=portal_user.pk)
            reader.changed_by = capable
            if change == "move-account":
                reader.client = other_org
                reader.save(update_fields=["client"])
            else:
                reader.is_active = False
                reader.save(update_fields=["is_active"])

    client.force_login(portal_user)
    review_storage_after_read(commit_change)
    response = client.get(reverse("portal:request-detail", args=[review_ticket.number]))
    review_storage_assert_reads(
        response,
        portal_user,
        "client",
        review_ticket,
        [
            ("ticket", "", ""),
            ("patient_answer", "request_forms.fieldvalue", str(answer.pk)),
        ],
    )
    assert (
        str(answer.value) in response.content.decode() and updated not in response.content.decode()
    )
    if change == "correct-answer":
        assert FieldValue.objects.get(replaces=answer).value == updated
    elif change == "move-account":
        assert User.objects.get(pk=portal_user.pk).client_id == other_org.pk
    else:
        assert not User.objects.get(pk=portal_user.pk).is_active


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["remove-role", "restricted-category"])
def test_storage_staff_ticket_open_precedes_role_or_category_change(
    change, review_storage_after_read, client, agent, capable, group, review_ticket
):
    from django.urls import reverse

    from desk.services import update_properties
    from taxonomy.models import RequestCategory

    restricted = RequestCategory.objects.create(
        parent=group,
        key="private-review",
        name="Private review",
        is_restricted=True,
        default_team_id=review_ticket.team_id,
    )

    def commit_change():
        if change == "remove-role":
            agent.groups.remove(agent.groups.get(name="client_services"))
        else:
            review_ticket.refresh_from_db()
            update_properties(
                actor=capable,
                request=review_ticket,
                expected_revision=review_ticket.revision,
                priority=review_ticket.priority,
                category=restricted,
                reason="Synthetic reassignment",
            )

    client.force_login(agent)
    review_storage_after_read(commit_change)
    response = client.get(reverse("desk:request-detail", args=[review_ticket.pk]))
    review_storage_assert_reads(response, agent, "laboratory", review_ticket, [("ticket", "", "")])
    assert review_ticket.number in response.content.decode()
    if change == "remove-role":
        assert not agent.groups.filter(name="client_services").exists()
    else:
        review_ticket.refresh_from_db()
        assert review_ticket.category == restricted


@pytest.mark.django_db(transaction=True)
def test_storage_staff_reveal_is_attributed_after_its_grant_is_removed(
    review_storage_after_read, client, capable, review_ticket
):
    from django.urls import reverse

    from core.models import AuditEvent
    from request_forms.models import FieldValue

    answer = FieldValue.objects.get(filled_form__request=review_ticket, key="patient")
    grant = capable.user_permissions.get(codename="view_patient_information")
    client.force_login(capable)
    review_storage_after_read(lambda: capable.user_permissions.remove(grant))
    response = client.post(
        reverse("request_details:reveal", args=[answer.pk]), HTTP_HX_REQUEST="true"
    )
    review_storage_assert_reads(
        response,
        capable,
        "laboratory",
        review_ticket,
        [("patient_answer", "request_forms.fieldvalue", str(answer.pk))],
    )
    assert str(answer.value) in response.content.decode()
    assert not capable.user_permissions.filter(pk=grant.pk).exists()
    assert (
        AuditEvent.objects.get(action="request_form.value_revealed").request_ref
        == response["X-Request-ID"]
    )


@pytest.mark.django_db(transaction=True)
def test_storage_completed_download_keeps_its_file_after_withdrawal(
    review_storage_after_read, client, agent, capable, portal_user, review_ticket
):
    from django.urls import reverse

    from attachments.inspection import inspect_bytes
    from attachments.services import add_entry_with_files, withdraw_attachment

    data = b"Synthetic contents of the published reply attachment.\n"
    review_ticket.refresh_from_db()
    entry = add_entry_with_files(
        actor=agent,
        request=review_ticket,
        kind="reply",
        body="Synthetic file for this ticket.",
        files=[inspect_bytes("review-sample.txt", data)],
        expected_revision=review_ticket.revision,
    )
    attachment = entry.attachments.get()
    client.force_login(portal_user)
    review_storage_after_read(
        lambda: withdraw_attachment(
            actor=capable,
            attachment_id=attachment.pk,
            reason="Synthetic withdrawal after download",
        )
    )
    response = client.get(reverse("attachments:download", args=[attachment.pk]))
    review_storage_assert_reads(
        response,
        portal_user,
        "client",
        review_ticket,
        [("file", "attachments.attachment", str(attachment.pk))],
    )
    assert response.content == data
    attachment.refresh_from_db()
    assert attachment.is_withdrawn


@pytest.mark.parametrize(
    ("kind", "object_type", "object_id"),
    [
        ("file", "", ""),
        ("patient_answer", "attachments.attachment", "8"),
        ("ticket", "request_forms.fieldvalue", "8"),
    ],
    ids=["file-without-target", "answer-with-file-target", "ticket-with-answer-target"],
)
def test_stored_object_matches_the_kind_of_read(kind, object_type, object_id, agent, review_ticket):
    """The persisted object identifies a file or answer for that kind, and is empty for a page."""
    from django.db import IntegrityError, transaction

    from core.models import AccessRecord

    with pytest.raises(IntegrityError), transaction.atomic():
        AccessRecord.objects.create(
            account=agent,
            capacity="laboratory",
            kind=kind,
            ticket_id=review_ticket.pk,
            object_type=object_type,
            object_id=object_id,
            request_ref="a" * 32,
        )


@pytest.mark.django_db(transaction=True)
def test_database_insert_failure_replaces_redraw_and_omits_content_from_log(
    client, portal_user, review_ticket, caplog
):
    """Raise a real database error during the single insert for a two-record portal response."""
    from django.db import DatabaseError, transaction

    from core.models import AccessRecord

    client.force_login(portal_user)
    client.raise_request_exception = False
    attempts = []

    def refuse_insert(execute, sql, params, many, context):
        if sql.startswith('INSERT INTO "core_accessrecord"'):
            attempts.append(sql)
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1 / 0")
            pytest.fail("PostgreSQL should have refused division by zero")
        return execute(sql, params, many, context)

    with connection.execute_wrapper(refuse_insert):
        response = client.get(reverse("portal:request-detail", args=[review_ticket.number]))
    assert len(attempts) == 1
    assert response.status_code == 500 and isinstance(response.exc_info[1], DatabaseError)
    assert PATIENT_SENTINEL.encode() not in response.content
    assert not AccessRecord.objects.exists()
    # Inspect emitted log records with the configured filter, as the console sees them.
    messages = []
    for record in caplog.records:
        if record.name == "lab_ops.request" and record.levelno == logging.ERROR:
            messages.append((record.request_id, record.getMessage()))
    assert len(messages) == 1
    ref, message = messages[0]
    assert ref == response["X-Request-ID"] and "status=500" in message
    assert PATIENT_SENTINEL not in message and review_ticket.subject not in message


@pytest.mark.django_db(transaction=True)
def test_one_batch_per_sensitive_page_and_exact_change_event(
    client, agent, capable, portal_user, review_ticket, review_patient, record_property
):
    """Count full requests and access INSERTs on the same fixture, excluding test setup queries."""
    from django.test.utils import CaptureQueriesContext

    from attachments.inspection import inspect_bytes
    from attachments.services import add_entry_with_files
    from core.models import AuditEvent
    from desk.models import EntryKind, RequestEntry

    attachment = add_entry_with_files(
        actor=agent,
        request=review_ticket,
        kind=EntryKind.REPLY,
        body="Synthetic visible entry",
        files=[inspect_bytes("synthetic-note.txt", b"Synthetic attachment text\n")],
        expected_revision=review_ticket.revision,
    ).attachments.get()
    urls = [
        (capable, "get", "desk:request-detail", review_ticket.pk),
        (capable, "get", "desk:request-history", review_ticket.pk),
        (capable, "get", "desk:request-access", review_ticket.pk),
        (capable, "get", "request_details:edit", review_ticket.number),
        (capable, "post", "request_details:reveal", review_patient.pk),
        (capable, "get", "clients:client-timeline", review_ticket.client_id),
        (portal_user, "get", "portal:request-detail", review_ticket.number),
        (portal_user, "get", "attachments:download", attachment.pk),
    ]
    for reader, method, name, pk in urls:
        client.force_login(reader)
        with CaptureQueriesContext(connection) as queries:
            response = getattr(client, method)(reverse(name, args=[pk]))
        assert response.status_code == 200
        record_property(name, len(queries))
        inserts = [q for q in queries if q["sql"].startswith('INSERT INTO "core_accessrecord"')]
        assert len(inserts) == 1, name

    review_ticket.refresh_from_db()
    client.force_login(agent)
    body = "Synthetic new note entered for the correlation check."
    response = client.post(
        reverse("desk:request-entry", args=[review_ticket.pk]),
        {"expected_revision": review_ticket.revision, "kind": "note", "body": body},
    )
    assert response.status_code == 302
    note = RequestEntry.objects.get(request=review_ticket, body=body)
    event = AuditEvent.objects.get(
        action="entry.added", object_type=note._meta.label_lower, object_id=str(note.pk)
    )
    assert event.request_ref == response["X-Request-ID"]
    assert review_pages_records(response) == []
    assert all(body not in str(obj) for obj in (event.reason, event.before, event.after))


def test_opened_nonstandard_page_values_select_the_documented_page(capable, review_ticket):
    from core.models import AccessRecord
    from desk.opened import page

    AccessRecord.objects.bulk_create(
        [
            AccessRecord(
                account=capable, capacity="laboratory", kind="ticket", ticket_id=review_ticket.pk
            )
            for _ in range(28)
        ]
    )
    for raw in [None, "", "not a number", "9" * 5000]:
        first = page(review_ticket, raw)
        assert (first.number, len(first)) == (1, 25)
    for raw in ["0", "-1", "3", "999999999999"]:
        last = page(review_ticket, raw)
        assert (last.number, len(last)) == (2, 3)


def test_timeline_growth_is_per_distinct_ticket_on_this_page(
    client, agent, clinic, pickup_category
):
    from desk.models import EntryKind
    from desk.services import create_request

    tickets = [
        create_request(
            actor=agent,
            client=clinic,
            category=pickup_category,
            channel="phone",
            first_entry_kind=EntryKind.NOTE,
            subject=f"Synthetic visit {index}",
            first_entry_body=f"Synthetic timeline note {index}.",
        )
        for index in range(26)
    ]
    client.force_login(agent)
    url = reverse("clients:client-timeline", args=[clinic.pk])
    for page, expected in [(1, tickets[1:]), (2, tickets[:1])]:
        response = client.get(url, {"kind": "note", "page": page})
        assert response.status_code == 200
        ids = {item["entry"].request_id for item in response.context["page_obj"]}
        assert ids == {ticket.pk for ticket in expected}
        assert sorted(review_pages_records(response)) == sorted(
            ("timeline", agent.pk, "laboratory", pk, "", "") for pk in ids
        )


@pytest.mark.parametrize("hx", [False, True], ids=["full-page", "htmx"])
def test_methods_on_the_ticket_read_and_redraw_routes(
    hx, capable, portal_user, review_ticket, review_patient
):
    """Compare the final response and its accounting on both ordinary and enhanced requests."""
    staff_browser, portal_browser = Client(), Client()
    staff_browser.force_login(capable)
    portal_browser.force_login(portal_user)
    headers = {"HTTP_HX_REQUEST": "true"} if hx else {}
    pk, number = review_ticket.pk, review_ticket.number
    for name, key, browser, kinds, statuses in [
        ("desk:request-detail", pk, staff_browser, ["ticket"], (200, 200, 200)),
        ("desk:request-history", pk, staff_browser, ["history"], (200, 200, 200)),
        ("desk:request-access", pk, staff_browser, ["access"], (200, 405, 405)),
        ("request_details:edit", number, staff_browser, ["form_details"], (200, 200, 200)),
        (
            "clients:client-timeline",
            review_ticket.client_id,
            staff_browser,
            ["timeline"],
            (200, 200, 200),
        ),
        (
            "portal:request-detail",
            number,
            portal_browser,
            ["patient_answer", "ticket"],
            (200, 200, 200),
        ),
        ("desk:request-entry", pk, staff_browser, ["ticket"], (302, 302, 400)),
        (
            "portal:request-reply",
            number,
            portal_browser,
            ["patient_answer", "ticket"],
            (302, 302, 200),
        ),
        (
            "request_details:reveal",
            review_patient.pk,
            staff_browser,
            ["patient_answer"],
            (405, 405, 200),
        ),
    ]:
        for method, status in zip(["get", "head", "post"], statuses, strict=True):
            response = getattr(browser, method)(reverse(name, args=[key]), **headers)
            case = (name, method, hx)
            assert response.status_code == status, case
            records = review_pages_records(response)
            if status in (302, 405):
                assert records == [], case
            elif method == "head":
                assert response.content == b"", case
                # The portal HEAD test checks the "shown" label on a bodyless response.
            else:
                assert [row[0] for row in records] == kinds, case
                assert {row[3] for row in records} == {pk}, case
                assert (PATIENT_SENTINEL.encode() in response.content) == (
                    "patient_answer" in kinds
                ), case
