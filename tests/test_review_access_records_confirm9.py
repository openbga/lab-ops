"""View-entry attribution, including sign-out without a second sign-in during the request.

Adopted from the review's ninth confirmation (the reviewer's original is kept outside the
repository)."""

# Pytest resolves the request-form fixtures by name.
# ruff: noqa: F811
from urllib.parse import urlsplit

import pytest
from django.contrib.auth import logout
from django.contrib.auth.models import AnonymousUser
from django.urls import reverse

from core import access
from core.models import AccessRecord
from desk import portal_views
from request_forms.tests.conftest import (  # noqa: F401
    PATIENT_SENTINEL,
    capable,
    catalog,
    clinic,
    group,
    logistics,
    make_user,
    pickup,
    pickup_category,
    portal_user,
)
from request_forms.tests.test_pages import raised


def test_note_keeps_the_bound_reader_when_given_the_signed_out_user(rf, django_user_model):
    # No database write. The only identified account is the one present at view entry.
    reader = django_user_model(pk=9)
    request = rf.get("/")
    request.user = reader
    access.AccessBindMiddleware(lambda request: None).process_view(request, None, (), {})
    request.user = AnonymousUser()
    access.note(request, "ticket", ticket_id=7, account=request.user)
    assert [(row.account_id, row.capacity) for row in access.noted(request)] == [
        (reader.pk, "laboratory")
    ]


def test_note_accepts_an_explicit_reader_without_middleware(rf, django_user_model):
    request = rf.get("/")
    reader = django_user_model(pk=9)
    access.note(request, "ticket", ticket_id=7, account=reader)
    assert [(row.account_id, row.capacity) for row in access.noted(request)] == [
        (reader.pk, "laboratory")
    ]


@pytest.mark.django_db
def test_person_help_and_rows_identify_the_reader_after_portal_signout(
    client, portal_user, capable, pickup, monkeypatch
):
    ticket = raised(portal_user, pickup, {"count": 2, "patient": PATIENT_SENTINEL})
    client.force_login(portal_user)
    render = portal_views.render

    def draw_and_sign_out(request, *args, **kwargs):
        page = render(request, *args, **kwargs)
        assert request.user.pk == portal_user.pk and PATIENT_SENTINEL.encode() in page.content
        logout(request)
        return page

    monkeypatch.setattr(portal_views, "render", draw_and_sign_out)
    url = reverse("portal:request-detail", args=[ticket.number])
    read = client.get(url)
    assert read.status_code == 200
    refused = client.get(url)
    assert refused.status_code == 302
    assert not AccessRecord.objects.filter(request_ref=refused["X-Request-ID"]).exists()

    client.force_login(capable)
    page = client.get(reverse("desk:request-access", args=[ticket.pk]))
    assert page.status_code == 200
    rows = [row for row in page.context["page_obj"] if row.request_ref == read["X-Request-ID"]]
    assert len(rows) == 2
    assert {(row.username, row.capacity) for row in rows} == {
        (portal_user.username, "Client portal")
    }
    columns = next(
        s for s in page.context["page_documentation"]["sections"] if s["title"] == "Columns"
    )
    person = next(item for item in columns["items"] if item["label"] == "Person")
    assert "account signed in when the page was asked for" in person["description"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("method", "route", "arg"),
    [
        ("get", "desk:request-detail", 1),
        ("post", "desk:request-entry", 1),
        ("get", "desk:request-history", 1),
        ("get", "desk:request-access", 1),
        ("get", "desk:complaint-decision", 1),
        ("get", "desk:complaint-record", 1),
        ("get", "desk:complaint-review", 1),
        ("get", "request_details:edit", "DEMO-1"),
        ("post", "request_details:reveal", 1),
        ("post", "link_records:attachment-reveal", 1),
        ("get", "attachments:download", 1),
        ("get", "clients:client-timeline", 1),
        ("get", "portal:request-detail", "DEMO-1"),
        ("post", "portal:request-reply", "DEMO-1"),
        ("get", "portal_forms:raise", "pickup"),
    ],
)
def test_anonymous_recording_routes_require_signin(client, method, route, arg):
    query = {"again": "DEMO-1"} if route == "portal_forms:raise" else None
    response = getattr(client, method)(reverse(route, args=[arg], query=query))
    assert response.status_code == 302 and urlsplit(response.url).path == reverse("account_login")
    assert not AccessRecord.objects.filter(request_ref=response["X-Request-ID"]).exists()
