"""Access-record attribution at the boundary between deciding a read and drawing its response.

Adopted from the review's eighth confirmation (the reviewer's original is kept outside the
repository). Cases that sign another account in during a request are accepted limits, strict
xfails (IN_REQUEST_SWITCH)."""

# The request-form fixtures are collected by name.
# ruff: noqa: F811
import pytest
from allauth.core import context
from django.apps import apps
from django.contrib.auth import login, logout
from django.urls import reverse

from clients import views as client_views
from core.models import AuditEvent
from desk import portal_views as portal_tickets
from desk import services
from desk import views as desk_views
from links import views as link_views
from links.models import ExternalSystem
from links.services import add_link
from request_forms import portal_views as portal_forms
from request_forms import request_views
from request_forms.models import FieldValue
from request_forms.tests.conftest import (  # noqa: F401
    PATIENT_SENTINEL,
    admin,
    capable,
    catalog,
    clinic,
    colleague,
    group,
    logistics,
    make_user,
    pickup,
    pickup_category,
    portal_user,
)
from request_forms.tests.test_pages import raised

pytestmark = pytest.mark.django_db

# Accepted limit (core/access.py ACCOUNT, docs/architecture.md "Access records"): a record names the
# account that made the request, as signed in when its view was called; these cases sign another
# account in during the request, which nothing in this release does.
IN_REQUEST_SWITCH = {
    "test_record_names_the_account_used_for_the_resource_read[timeline]",
    "test_record_names_the_account_used_for_the_resource_read[form-details]",
    "test_record_names_the_account_used_for_the_resource_read[answer]",
    "test_record_names_the_account_used_for_the_resource_read[person-key]",
    "test_complaint_record_names_the_reader_from_the_access_check[refused-redraw-decision]",
    "test_complaint_record_names_the_reader_from_the_access_check[refused-redraw-record]",
    "test_complaint_record_names_the_reader_from_the_access_check[refused-redraw-review]",
    "test_history_record_after_the_ticket_lookup_names_the_supervisor_who_opened_history",
    "test_portal_answer_names_the_reader_used_by_the_template",
    "test_note_default_and_explicit_reader",
    "test_staff_ticket_reads_use_the_account_reread_in_the_snapshot[ticket]",
    "test_staff_ticket_reads_use_the_account_reread_in_the_snapshot[redraw]",
    "test_staff_ticket_reads_use_the_account_reread_in_the_snapshot[access]",
}


@pytest.fixture(autouse=True)
def _accepted_attribution_limit(request):
    if request.node.name in IN_REQUEST_SWITCH:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason="Accepted limit: the record names the account that made the request; an "
                "account signed in during the request is not followed (core/access.py).",
            )
        )


@pytest.fixture
def ticket(portal_user, pickup):
    return raised(portal_user, pickup, {"count": 38, "patient": PATIENT_SENTINEL})


@pytest.fixture
def second_staff(make_user, capable):
    other = make_user("second-review-reader", "supervisor")
    other.user_permissions.set(capable.user_permissions.all())
    return other


def sign_in(account):
    login(context.request, account, backend="django.contrib.auth.backends.ModelBackend")


def after_once(monkeypatch, module, name, action):
    """Keep the real operation and apply the transition immediately after its first call."""
    original = getattr(module, name)
    calls = []

    def wrapped(*args, **kwargs):
        result = original(*args, **kwargs)
        if not calls:
            calls.append(context.request.user.pk)
            action()
        return result

    monkeypatch.setattr(module, name, wrapped)
    return calls


def check_rows(response, ticket, reader, kinds):
    # The pre-feature base can confirm who actually received the page or reveal.
    if "accessrecord" in apps.get_app_config("core").models:
        rows = apps.get_model("core", "AccessRecord").objects.filter(
            request_ref=response["X-Request-ID"]
        )
        capacity = "client" if reader.client_id else "laboratory"
        assert sorted(rows.values_list("kind", "account_id", "capacity", "ticket_id")) == sorted(
            (kind, reader.pk, capacity, ticket.pk) for kind in kinds
        )


def test_repeat_record_uses_the_reader_who_supplied_the_copied_answers(
    client, ticket, pickup, portal_user, colleague, monkeypatch
):
    client.force_login(portal_user)
    selector = "repeat_from" if hasattr(portal_forms, "repeat_from") else "repeat_values"
    calls = after_once(monkeypatch, portal_forms, selector, lambda: sign_in(colleague))
    url = reverse("portal_forms:raise", args=[pickup.key], query={"again": ticket.number})
    response = client.get(url)
    assert calls == [portal_user.pk] and response.status_code == 200
    assert response.context["form"]["field-count"].value() in (38, "38")
    check_rows(response, ticket, portal_user, ["repeat"])


@pytest.fixture(params=["timeline", "form-details", "answer", "person-key"])
def staff_read(request, clinic, ticket, capable):
    surface = request.param
    if surface == "timeline":
        module, name, pk, kind = client_views, "clients:client-timeline", clinic.pk, "timeline"
    elif surface == "form-details":
        module, name, pk, kind = (
            request_views,
            "request_details:edit",
            ticket.number,
            "form_details",
        )
    elif surface == "answer":
        row = FieldValue.objects.get(filled_form__request=ticket, key="patient")
        module, name, pk, kind = request_views, "request_details:reveal", row.pk, "patient_answer"
    else:
        system = ExternalSystem.objects.create(key="review-source", name="Synthetic", kind="lis")
        attachment, _ = add_link(
            actor=capable,
            request_id=ticket.pk,
            system=system,
            record_type="patient",
            identifier="synthetic-review-key",
        )
        module, name, pk, kind = (
            link_views,
            "link_records:attachment-reveal",
            attachment.pk,
            "person_key",
        )
    return surface, module, reverse(name, args=[pk]), kind


def test_record_names_the_account_used_for_the_resource_read(
    client, capable, second_staff, ticket, monkeypatch, staff_read
):
    surface, module, url, kind = staff_read
    client.force_login(capable)
    # Both supervisors hold the same grant. Sign in the second after the generic staff check.
    calls = after_once(monkeypatch, module, "require_staff", lambda: sign_in(second_staff))
    if surface in ("answer", "person-key"):
        response = client.post(url)
        action = (
            "request_form.value_revealed"
            if surface == "answer"
            else "external_link.person_revealed"
        )
        event = AuditEvent.objects.filter(action=action).get()
        assert event.actor_id == second_staff.pk
        if hasattr(event, "request_ref"):
            assert event.request_ref == response["X-Request-ID"]
    else:
        selector = "timeline_page" if surface == "timeline" else "staff_values"
        select = getattr(module, selector)
        readers = []

        def observe(*args, **kwargs):
            readers.append(args[0 if surface == "timeline" else 1].pk)
            return select(*args, **kwargs)

        monkeypatch.setattr(module, selector, observe)
        response = client.get(url)
        assert readers == [second_staff.pk]
    assert calls == [capable.pk] and response.status_code == 200
    check_rows(response, ticket, second_staff, [kind])


@pytest.fixture
def complaint(ticket, admin):
    marked = services.set_complaint(
        actor=admin,
        request=ticket,
        expected_revision=ticket.revision,
        is_complaint=True,
        reason="Synthetic review",
    )
    services.decide_complaint(
        actor=admin,
        request=marked,
        expected_revision=marked.revision,
        decision="investigate",
        rationale="Synthetic decision",
    )
    marked.refresh_from_db()
    services.update_complaint(
        actor=admin, request=marked, expected_revision=marked.revision, findings="Synthetic finding"
    )
    return marked


@pytest.mark.parametrize("route", ["decision", "record", "review"])
@pytest.mark.parametrize("redraw", [False, True], ids=["open", "refused-redraw"])
def test_complaint_record_names_the_reader_from_the_access_check(
    client, capable, second_staff, complaint, monkeypatch, route, redraw
):
    client.force_login(capable)
    checks = []
    if redraw:
        # The refused POST makes a second scoped read to draw the form.
        original = desk_views._complaint_or_404

        def recheck(request, pk):
            if checks:
                sign_in(second_staff)
            checks.append(request.user.pk)
            return original(request, pk)

        monkeypatch.setattr(desk_views, "_complaint_or_404", recheck)
    else:
        # The caller has already scoped the complaint and checked the reviewer's role.
        original = desk_views._action_form

        def enter_form(request, *args, **kwargs):
            checks.append(request.user.pk)
            sign_in(second_staff)
            return original(request, *args, **kwargs)

        monkeypatch.setattr(desk_views, "_action_form", enter_form)
    url = reverse(f"desk:complaint-{route}", args=[complaint.pk])
    response = client.post(url, {"expected_revision": "invalid"}) if redraw else client.get(url)
    assert response.status_code == 200 and complaint.number.encode() in response.content
    assert checks == ([capable.pk, second_staff.pk] if redraw else [capable.pk])
    check_rows(response, complaint, second_staff if redraw else capable, ["complaint"])


@pytest.mark.parametrize("transition", ["sign-in", "sign-out"])
@pytest.mark.parametrize(
    "surface", ["ticket", "redraw", "history", "access", "portal", "portal-redraw"]
)
def test_ticket_pages_keep_the_reader_after_drawing(
    client, ticket, capable, portal_user, transition, surface, monkeypatch
):
    portal = surface in ("portal", "portal-redraw")
    reader = portal_user if portal else capable
    client.force_login(reader)
    route = {
        "ticket": "desk:request-detail",
        "redraw": "desk:request-entry",
        "history": "desk:request-history",
        "access": "desk:request-access",
        "portal": "portal:request-detail",
        "portal-redraw": "portal:request-reply",
    }[surface]
    other = capable if portal else portal_user
    switch = (
        (lambda: sign_in(other)) if transition == "sign-in" else (lambda: logout(context.request))
    )
    calls = after_once(monkeypatch, portal_tickets if portal else desk_views, "render", switch)
    url = reverse(route, args=[ticket.number if portal else ticket.pk])
    if surface in ("redraw", "portal-redraw"):
        response = client.post(url, {"expected_revision": "invalid"})
    else:
        response = client.get(url)
    assert calls == [reader.pk] and response.status_code == (400 if surface == "redraw" else 200)
    kinds = (
        ["ticket", "patient_answer"]
        if portal
        else [surface if surface in ("history", "access") else "ticket"]
    )
    check_rows(response, ticket, reader, kinds)


def test_history_record_after_the_ticket_lookup_names_the_supervisor_who_opened_history(
    client, capable, second_staff, ticket, monkeypatch
):
    client.force_login(capable)
    calls = after_once(monkeypatch, desk_views, "_request_or_404", lambda: sign_in(second_staff))
    authorized = []
    supervisor = desk_views.is_supervisor

    def check(user):
        allowed = supervisor(user)
        if allowed:
            authorized.append(user.pk)
        return allowed

    monkeypatch.setattr(desk_views, "is_supervisor", check)
    response = client.get(reverse("desk:request-history", args=[ticket.pk]))
    assert calls == [capable.pk] and response.status_code == 200
    assert authorized and authorized[0] == second_staff.pk
    check_rows(response, ticket, second_staff, ["history"])


@pytest.mark.parametrize("transition", ["sign-in", "sign-out"])
def test_download_keeps_the_file_reader_after_reading_the_bytes(
    client, capable, portal_user, ticket, transition, monkeypatch
):
    from attachments import views
    from attachments.inspection import inspect_bytes
    from attachments.services import add_entry_with_files
    from desk.models import EntryKind

    data = b"Synthetic review file."
    file = add_entry_with_files(
        actor=capable,
        request=ticket,
        kind=EntryKind.REPLY,
        expected_revision=ticket.revision,
        body="Synthetic reply",
        files=[inspect_bytes("review.txt", data)],
    ).attachments.get()
    client.force_login(portal_user)
    switch = (
        (lambda: sign_in(capable)) if transition == "sign-in" else (lambda: logout(context.request))
    )
    calls = after_once(monkeypatch, views, "file_bytes", switch)
    response = client.get(reverse("attachments:download", args=[file.pk]))
    assert calls == [portal_user.pk] and response.status_code == 200 and response.content == data
    check_rows(response, ticket, portal_user, ["file"])


def test_portal_answer_names_the_reader_used_by_the_template(
    client, portal_user, colleague, ticket, monkeypatch
):
    from request_forms.templatetags import request_forms as tags

    # Both people can read the ordinary ticket and its marked fixture answer.
    client.force_login(portal_user)
    render = portal_tickets.render

    def draw(request, *args, **kwargs):
        sign_in(colleague)
        return render(request, *args, **kwargs)

    monkeypatch.setattr(portal_tickets, "render", draw)
    calls = after_once(monkeypatch, tags, "client_values", lambda: sign_in(portal_user))
    response = client.get(reverse("portal:request-detail", args=[ticket.number]))
    assert response.status_code == 200 and PATIENT_SENTINEL.encode() in response.content
    assert calls == [colleague.pk]
    records = apps.get_model("core", "AccessRecord").objects.filter(
        request_ref=response["X-Request-ID"], ticket_id=ticket.pk
    )
    assert sorted(records.values_list("kind", "account_id", "capacity")) == [
        ("patient_answer", colleague.pk, "client"),
        ("ticket", portal_user.pk, "client"),
    ]


def test_note_default_and_explicit_reader(rf, capable, portal_user):
    from django.contrib.auth.models import AnonymousUser

    from core import access

    request = rf.get("/")
    request.user = capable
    binder = access.AccessBindMiddleware(lambda request: None)
    binder.process_view(request, None, (), {})
    request.user = portal_user
    access.note(request, "ticket", ticket_id=1)
    access.note(request, "history", ticket_id=1, account=capable)
    request.user = AnonymousUser()
    access.note(request, "access", ticket_id=1)
    assert [(r.kind, r.account_id, r.capacity) for r in access.noted(request)] == [
        ("ticket", portal_user.pk, "client"),
        ("history", capable.pk, "laboratory"),
        ("access", capable.pk, "laboratory"),
    ]


@pytest.mark.parametrize("transition", ["sign-in", "sign-out"])
def test_staff_read_keeps_its_reader_after_render(
    client, capable, portal_user, ticket, monkeypatch, staff_read, transition
):
    surface, module, url, kind = staff_read
    client.force_login(capable)
    switch = (
        (lambda: sign_in(portal_user))
        if transition == "sign-in"
        else (lambda: logout(context.request))
    )
    calls = after_once(monkeypatch, module, "render", switch)
    response = client.post(url) if surface in ("answer", "person-key") else client.get(url)
    assert calls == [capable.pk] and response.status_code == 200
    check_rows(response, ticket, capable, [kind])


@pytest.mark.parametrize("transition", ["sign-in", "sign-out"])
@pytest.mark.parametrize(
    "route", ["complaint-decision", "complaint-record", "complaint-review", "repeat"]
)
def test_complaint_or_repeat_keeps_the_reader_after_render(
    client, capable, portal_user, pickup, complaint, route, transition, monkeypatch
):
    if route == "repeat":
        reader, other, module, kind = portal_user, capable, portal_forms, "repeat"
        url = reverse("portal_forms:raise", args=[pickup.key], query={"again": complaint.number})
    else:
        reader, other, module, kind = capable, portal_user, desk_views, "complaint"
        url = reverse(f"desk:{route}", args=[complaint.pk])
    client.force_login(reader)
    switch = (
        (lambda: sign_in(other)) if transition == "sign-in" else (lambda: logout(context.request))
    )
    calls = after_once(monkeypatch, module, "render", switch)
    response = client.get(url)
    assert calls == [reader.pk] and response.status_code == 200
    check_rows(response, complaint, reader, [kind])


@pytest.mark.parametrize("surface", ["ticket", "redraw", "access"])
def test_staff_ticket_reads_use_the_account_reread_in_the_snapshot(
    client, capable, second_staff, ticket, surface, monkeypatch
):
    client.force_login(capable)
    original = desk_views.reading_account
    readers = []

    def reread(request):
        sign_in(second_staff)
        reader = original(request)
        readers.append(reader.pk)
        return reader

    monkeypatch.setattr(desk_views, "reading_account", reread)
    name = {"ticket": "request-detail", "redraw": "request-entry", "access": "request-access"}[
        surface
    ]
    url = reverse(f"desk:{name}", args=[ticket.pk])
    response = (
        client.post(url, {"expected_revision": "invalid"})
        if surface == "redraw"
        else client.get(url)
    )
    assert readers == [second_staff.pk] and response.status_code == (
        400 if surface == "redraw" else 200
    )
    check_rows(response, ticket, second_staff, ["access" if surface == "access" else "ticket"])
