"""Independent verification of access-record fixes. All accounts and ticket data are synthetic.

Adopted from the review's verification round (the reviewer's original is kept outside the
repository). Adaptations are marked "Adapted"; accepted limits are strict xfails naming the
document that states them. No browser import: CI's unit job installs no Playwright."""

# The imported fixtures are collected by pytest and resolved by argument name.
# ruff: noqa: F811
import io
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from threading import Event
from urllib.parse import urljoin
from uuid import uuid4

import pytest
from django.apps import apps
from django.db import connection, connections, transaction
from django.http import FileResponse, StreamingHttpResponse
from django.template import engines
from django.template.response import TemplateResponse
from django.urls import reverse
from django.views.debug import ExceptionReporter, get_exception_reporter_filter

from request_forms.tests.conftest import (  # noqa: F401
    PATIENT_SENTINEL,
    admin,
    agent,
    capable,
    catalog,
    clinic,
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
def ticket(portal_user, pickup):
    return raised(portal_user, pickup, {"count": 3, "patient": PATIENT_SENTINEL})


def access_model():
    return apps.get_model("core", "AccessRecord")


def recorded(response):
    return list(
        access_model()
        .objects.filter(request_ref=response["X-Request-ID"])
        .order_by("id")
        .values_list("kind", "ticket_id", "account_id", "capacity", "object_type", "object_id")
    )


class PagerLink(HTMLParser):
    def __init__(self, relation):
        super().__init__()
        self.relation = relation
        self.href = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("rel") == self.relation:
            self.href = attrs["href"]


def linked_page(client, page, url, relation="next"):
    parser = PagerLink(relation)
    parser.feed(page.content.decode())
    assert parser.href
    response = client.get(urljoin(url, parser.href))
    assert response.status_code == 200
    return response


def history_rows(ticket, actor, count):
    model = access_model()
    rows = [
        model(
            account_id=actor.pk,
            ticket_id=ticket.pk,
            kind="ticket",
            capacity="laboratory",
            request_ref=uuid4().hex,
        )
        for _ in range(count)
    ]
    return model.objects.bulk_create(rows)


@pytest.mark.xfail(
    strict=True,
    reason="Accepted limit: a record being written when the first page was drawn can take a place "
    "in the later pages (docs/architecture.md, Access records).",
)
@pytest.mark.django_db(transaction=True)
def test_pager_keeps_the_first_committed_set_when_writers_finish_out_of_order(
    client, capable, ticket
):
    old = history_rows(ticket, capable, 30)
    inserted, allow_commit = Event(), Event()

    def finish_later():
        try:
            with transaction.atomic():
                (row,) = history_rows(ticket, capable, 1)
                inserted.set()
                assert allow_commit.wait(15)
            return row
        finally:
            connections.close_all()

    client.force_login(capable)
    url = reverse("desk:request-access", args=[ticket.pk])
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(finish_later)
        try:
            assert inserted.wait(10)
            (newest_visible,) = history_rows(ticket, capable, 1)
            first = client.get(url)
            assert first.status_code == 200
            as_first_drawn = [newest_visible.request_ref] + [r.request_ref for r in reversed(old)]
            assert [r.request_ref for r in first.context["page_obj"]] == as_first_drawn[:25]
            assert first.context["page_obj"].paginator.count == len(as_first_drawn)
        finally:
            allow_commit.set()
        late = pending.result(timeout=10)
    assert late.pk < newest_visible.pk
    second = linked_page(client, first, url)
    older = [r.request_ref for r in second.context["page_obj"]]
    previous = linked_page(client, second, url, relation="prev")
    now_on_first = [r.request_ref for r in previous.context["page_obj"]]
    observed = (
        older == as_first_drawn[25:],
        late.request_ref in now_on_first,
        second.context["page_obj"].paginator.count,
    )
    assert observed == (True, False, 31), (
        "Expected the original second page, the late commit absent on Previous, and 31 rows"
    )


def test_next_link_freezes_the_page_even_with_a_future_numeric_cutoff(client, capable, ticket):
    rows = history_rows(ticket, capable, 30)
    client.force_login(capable)
    url = reverse("desk:request-access", args=[ticket.pk])
    first = client.get(url, {"upto": rows[-1].pk + 100})
    assert first.status_code == 200 and first.context["page_obj"].paginator.count == 30
    history_rows(ticket, capable, 1)
    second = linked_page(client, first, url)
    assert [row.request_ref for row in second.context["page_obj"]] == [
        row.request_ref for row in reversed(rows[:-25])
    ]


def test_pager_respects_an_explicit_empty_boundary(client, capable, ticket):
    from desk.opened import newest

    empty_cutoff = newest(ticket)
    assert empty_cutoff == 0
    history_rows(ticket, capable, 1)
    client.force_login(capable)
    page = client.get(reverse("desk:request-access", args=[ticket.pk]), {"upto": empty_cutoff})
    assert page.status_code == 200
    assert page.context["page_obj"].paginator.count == 0


def test_pager_never_collects_rows_of_another_ticket(client, capable, ticket, pickup, portal_user):
    other = raised(portal_user, pickup, {"count": 1})
    own = history_rows(ticket, capable, 30)
    foreign = history_rows(other, capable, 1)
    client.force_login(capable)
    url = reverse("desk:request-access", args=[ticket.pk])
    first = client.get(url, {"upto": foreign[0].pk})
    second = linked_page(client, first, url)
    visible = {row.request_ref for p in (first, second) for row in p.context["page_obj"]}
    assert visible == {row.request_ref for row in own}
    assert foreign[0].request_ref not in visible


@pytest.mark.xfail(
    strict=True,
    reason="Accepted limit: Django's own error report holds request values; this release never "
    "produces it (config/logging.py replaces Django's handlers; docs/operations.md).",
)
def test_report_omits_validated_reply_text_from_the_actual_view_frame(
    client, agent, ticket, settings, monkeypatch
):
    from desk import views

    def interrupted(**kwargs):
        raise RuntimeError("Synthetic service interruption")

    settings.DEBUG = False
    client.force_login(agent)
    client.raise_request_exception = False
    monkeypatch.setattr(views, "add_entry_with_files", interrupted)
    marker = "synthetic-note-" + uuid4().hex
    response = client.post(
        reverse("desk:request-entry", args=[ticket.pk]),
        {"kind": "note", "body": marker, "expected_revision": ticket.revision},
        secure=True,
    )
    assert response.status_code == 500 and marker.encode() not in response.content
    report = ExceptionReporter(response.wsgi_request, *response.exc_info)
    (frame,) = [
        f
        for f in report.get_traceback_data()["frames"]
        if f["function"] == "request_entry" and f["filename"].endswith("desk/views.py")
    ]
    variables = dict(frame["vars"])
    visible_in = (marker in variables["data"], marker in report.get_traceback_html())
    assert visible_in == (False, False), "Validated note visible in frame data or HTML report"


@pytest.mark.xfail(
    strict=True,
    reason="Accepted limit: Django's own error report holds request values; this release never "
    "produces it (config/logging.py replaces Django's handlers; docs/operations.md).",
)
def test_report_omits_the_name_of_a_submitted_attachment(rf, settings):
    from django.core.files.uploadedfile import SimpleUploadedFile

    settings.DEBUG = False
    name = f"synthetic-scan-{uuid4().hex}.txt"
    request = rf.post("/", {"files": SimpleUploadedFile(name, b"Synthetic file")})
    problem = RuntimeError("Synthetic error")
    reporter = ExceptionReporter(request, type(problem), problem, None)
    visible_in = (name in reporter.get_traceback_text(), name in reporter.get_traceback_html())
    assert visible_in == (False, False), "Original filename visible in text or HTML report"


@pytest.mark.parametrize(
    "debug",
    [
        pytest.param(
            False,
            id="production",
            marks=pytest.mark.xfail(
                strict=True,
                reason="Accepted limit: Django's report filter hides only marked fields; the "
                "partial filter was withdrawn (docs/operations.md).",
            ),
        ),
        pytest.param(True, id="debug"),
    ],
)
def test_report_filter_respects_debug_and_preserves_confirmation_markers(rf, settings, debug):
    from accounts import confirmation

    settings.DEBUG = debug
    request = rf.post(
        "/",
        {"topic": ["first example", "second example"], confirmation.PASSWORD_FIELD: "example"},
    )
    confirmation.mark_sensitive(request)
    marked = list(request.sensitive_post_parameters)
    reporter = get_exception_reporter_filter(request)
    frame_data = reporter.cleanse_special_types(request, request.POST)
    displayed = reporter.get_post_parameters(request)
    if debug:
        assert frame_data.getlist("topic") == ["first example", "second example"]
        assert displayed.get("topic") == "second example"
    else:
        assert frame_data.getlist("topic") == [reporter.cleansed_substitute]
        assert displayed.get("topic") == reporter.cleansed_substitute
        assert displayed.get(confirmation.PASSWORD_FIELD) == reporter.cleansed_substitute
    assert request.POST.getlist("topic") == ["first example", "second example"]
    assert request.sensitive_post_parameters == marked
    assert confirmation.PASSWORD_FIELD in marked and confirmation.CODE_FIELD in marked


class SubstituteTemplate:
    """A deployment template-response hook which returns a different, harmless document."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_template_response(self, request, response):
        return TemplateResponse(request, engines["django"].from_string("Replacement document"))


@pytest.mark.parametrize(
    "transport",
    [
        "template",
        "file",
        "stream",
        pytest.param(
            "substituted-template",
            marks=pytest.mark.xfail(
                strict=True,
                reason="Accepted limit: a deployment's own template-response hook replacing a "
                "deferred page (docs/architecture.md, Access records).",
            ),
        ),
    ],
)
def test_receipt_follows_the_document_actually_returned(
    client, capable, ticket, settings, monkeypatch, transport
):
    from desk import views

    client.force_login(capable)
    if transport == "substituted-template":
        middleware = list(settings.MIDDLEWARE)
        middleware.insert(
            middleware.index("core.access.AccessBindMiddleware"), f"{__name__}.SubstituteTemplate"
        )
        settings.MIDDLEWARE = middleware

    def render_response(request, template, context):
        if transport == "file":
            return FileResponse(io.BytesIO(ticket.number.encode()))
        if transport == "stream":
            return StreamingHttpResponse([ticket.number.encode()])
        return TemplateResponse(
            request, engines["django"].from_string("{{ req.number }}"), {"req": ticket}
        )

    monkeypatch.setattr(views, "render", render_response)
    response = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    assert response.status_code == 200
    rows = recorded(response)
    body = b"".join(response.streaming_content) if response.streaming else response.content
    if transport == "substituted-template":
        assert body == b"Replacement document"
        assert rows == [], "A replacement template retained the receipt for the original document"
    else:
        assert body == ticket.number.encode()
        assert rows == [("ticket", ticket.pk, capable.pk, "laboratory", "", "")]


def test_request_again_does_not_claim_answers_copied_when_none_qualify(
    client, capable, portal_user, pickup
):
    from request_forms.forms import field_name
    from request_forms.selectors import repeat_values

    pickup.fields.filter(key="count").update(copy_on_repeat=False)
    source = raised(portal_user, pickup, {"count": 271, "patient": PATIENT_SENTINEL})
    assert repeat_values(pickup, portal_user, source.number) == ({}, None)
    client.force_login(portal_user)
    response = client.get(
        reverse("portal_forms:raise", args=[pickup.key]), {"again": source.number}
    )
    assert response.status_code == 200 and PATIENT_SENTINEL.encode() not in response.content
    assert response.context["form"][field_name("count")].value() in (None, "")
    claims = []
    if b"Answers copied from" in response.content:
        claims.append("portal notice")
    if "accessrecord" in apps.get_app_config("core").models:
        # Adapted (verification finding, resolved by recording nothing): nothing of the earlier
        # ticket is shown when nothing qualifies, so nothing is recorded or claimed.
        assert recorded(response) == []
        client.force_login(capable)
        history = client.get(reverse("desk:request-access", args=[source.pk]))
        if any(row.what.startswith("Answers copied") for row in history.context["page_obj"]):
            claims.append("Who opened it row")
    assert not claims, f"{', '.join(claims)} says answers were copied with zero eligible answers"


def test_request_again_scope_and_delivery(client, pickup, ticket, capable, portal_user, outsider):
    url = reverse("portal_forms:raise", args=[pickup.key])
    query = {"again": ticket.number}
    assert client.get(url, query).status_code == 302
    for reader, status in [(capable, 403), (outsider, 404), (portal_user, 200)]:
        client.force_login(reader)
        page = client.get(url, query, HTTP_HX_REQUEST="true")
        assert page.status_code == status
        assert recorded(page) == (
            [("repeat", ticket.pk, reader.pk, "client", "", "")] if status == 200 else []
        )
    head = client.head(url, query)
    assert head.status_code == 200 and head.content == b"" and recorded(head) == []
    missing = client.get(url, {"again": "synthetic-missing-number"})
    assert missing.status_code == 404 and recorded(missing) == []
    # A refused submission draws the caller's posted form; it doesn't copy the source again.
    posted_url = reverse("portal_forms:raise", args=[pickup.key], query=query)
    refused = client.post(posted_url, {"subject": "Synthetic unsent ticket"})
    assert refused.status_code == 200 and recorded(refused) == []
    pickup.offered_in_portal = False
    pickup.save(update_fields=["offered_in_portal"])
    redirected = client.post(posted_url)
    assert redirected.status_code == 302 and recorded(redirected) == []


def test_complaint_dialog_reads_redraws_and_redirects(client, capable, agent, ticket):
    from desk import services

    url = reverse("desk:complaint-decision", args=[ticket.pk])
    headers = {"HTTP_HX_REQUEST": "true", "HTTP_X_VR_FORM": "1"}
    client.force_login(capable)
    absent = client.get(url, **headers)
    assert absent.status_code == 404 and recorded(absent) == []
    ticket = services.set_complaint(
        request=ticket,
        actor=capable,
        expected_revision=ticket.revision,
        is_complaint=True,
        reason="Synthetic review",
    )
    client.force_login(agent)
    refused = client.get(url, **headers)
    assert refused.status_code == 403 and recorded(refused) == []
    client.force_login(capable)
    assert recorded(client.head(url, **headers)) == []
    expected = [("complaint", ticket.pk, capable.pk, "laboratory", "", "")]
    for response in (client.get(url, **headers), client.post(url, {"decision": ""}, **headers)):
        assert response.status_code == 200 and b"data-vr-form-page" in response.content
        assert recorded(response) == expected
    response = client.post(
        url,
        {
            "expected_revision": ticket.revision,
            "decision": "investigate",
            "rationale": "Synthetic investigation",
        },
        **headers,
    )
    assert response.status_code == 204 and response.content == b""
    assert response["HX-Redirect"].endswith(f"/requests/{ticket.pk}/")
    assert recorded(response) == []


@pytest.mark.confirmation
def test_person_key_records_the_chosen_carrier_only_after_confirmation(
    client, capable, ticket, pickup, portal_user
):
    from accounts import confirmation
    from core.models import AuditEvent, RevealConfirmationRule
    from links.models import ExternalSystem
    from links.services import add_link
    from request_forms.tests.conftest import PASSWORD

    other = raised(portal_user, pickup, {"count": 1})
    system = ExternalSystem.objects.create(key="verify-lis", name="Synthetic LIS", kind="lis")
    key = "synthetic-key-" + uuid4().hex
    attachments = [
        add_link(
            actor=capable, request_id=t.pk, system=system, record_type="patient", identifier=key
        )[0]
        for t in (ticket, other)
    ]
    assert attachments[0].link_id == attachments[1].link_id
    RevealConfirmationRule.objects.create(pk=1, first_reveal=True, revision=1)
    client.force_login(capable)
    url = reverse("link_records:attachment-reveal", args=[attachments[0].pk])
    for headers, status in [({}, 302), ({"HTTP_HX_REQUEST": "true"}, 204)]:
        asked = client.post(url, **headers)
        assert asked.status_code == status and recorded(asked) == []
    done = client.post(reverse("account-confirm-reveal"), {confirmation.PASSWORD_FIELD: PASSWORD})
    assert done.status_code == 302
    for attachment, headers in zip(attachments, [{}, {"HTTP_HX_REQUEST": "true"}], strict=True):
        url = reverse("link_records:attachment-reveal", args=[attachment.pk])
        delivered = client.post(url, **headers)
        assert delivered.status_code == 200 and key.encode() in delivered.content
        assert recorded(delivered) == [
            (
                "person_key",
                attachment.request_id,
                capable.pk,
                "laboratory",
                "links.linkattachment",
                str(attachment.pk),
            )
        ]
        event = AuditEvent.objects.get(
            action="external_link.person_revealed", request_ref=delivered["X-Request-ID"]
        )
        assert event.after == {"request": attachment.request_id, "attachment": attachment.pk}
        assert key not in str(event.after)


def test_database_shape_for_every_shipped_read_kind(agent, ticket):
    from django.db import IntegrityError

    from core.models import AccessKind

    kinds = dict.fromkeys(
        ("ticket", "history", "access", "form_details", "timeline", "repeat", "complaint"), ""
    )
    kinds.update(
        file="attachments.attachment",
        patient_answer="request_forms.fieldvalue",
        person_key="links.linkattachment",
    )
    assert set(kinds) == set(AccessKind.values)
    for kind, expected_type in kinds.items():
        for target in set(kinds.values()):
            attrs = dict(
                kind=kind,
                object_type=target,
                object_id="1" if target else "",
                ticket_id=ticket.pk,
                account=agent,
                capacity="laboratory",
            )
            if target == expected_type:
                access_model().objects.create(**attrs)
            else:
                with pytest.raises(IntegrityError) as bad, transaction.atomic():
                    access_model().objects.create(**attrs)
                assert bad.value.__cause__.diag.constraint_name == "access_record_object"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("atomic_requests", [False, True])
def test_new_workflows_write_after_the_view_transaction(
    client, capable, portal_user, pickup, ticket, monkeypatch, atomic_requests
):
    from core import access
    from desk import services

    ticket = services.set_complaint(
        actor=capable,
        request=ticket,
        expected_revision=ticket.revision,
        is_complaint=True,
        reason="Synthetic complaint",
    )
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", atomic_requests)
    write = access.write
    writes = []

    def observe(request, records):
        writes.append((request.request_id, [r.kind for r in records], connection.in_atomic_block))
        return write(request, records)

    monkeypatch.setattr(access, "write", observe)
    for user, kind, url in [
        (capable, "complaint", reverse("desk:complaint-record", args=[ticket.pk])),
        (
            portal_user,
            "repeat",
            reverse("portal_forms:raise", args=[pickup.key], query={"again": ticket.number}),
        ),
    ]:
        client.force_login(user)
        response = client.get(url)
        assert response.status_code == 200
        assert writes == [(response["X-Request-ID"], [kind], False)]
        writes.clear()
        assert recorded(response) == [
            (kind, ticket.pk, user.pk, "client" if user.client_id else "laboratory", "", "")
        ]
