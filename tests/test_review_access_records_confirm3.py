"""Confirm the portal form's method boundary and the help shown with a restored draft.

Adopted from the review's third confirmation (the reviewer's original is kept outside the
repository). No browser import at module level: CI's unit job installs no Playwright."""

# The imported fixtures are resolved by pytest by name.
# ruff: noqa: F811
from urllib.parse import parse_qs, urlsplit

import pytest
from django.apps import apps
from django.test import Client
from django.urls import reverse

from accounts.models import KeptDraft
from desk.models import ServiceRequest
from desk.tests.test_kept_typing import end_open, given_back, marker
from request_forms.tests.conftest import (  # noqa: F401
    agent,
    catalog,
    clinic,
    group,
    logistics,
    make_user,
    other_org,
    outsider,
    pickup,
    pickup_category,
    portal_user,
)
from request_forms.tests.test_capture_consistency import Inputs
from request_forms.tests.test_pages import raised

pytestmark = pytest.mark.django_db
TRANSPORTS = [
    pytest.param({}, id="page"),
    pytest.param({"HX-Request": "true"}, id="htmx"),
    pytest.param({"HX-Request": "true", "X-VR-Form": "1"}, id="dialog"),
]


@pytest.fixture
def source(portal_user, pickup):
    return raised(portal_user, pickup, {"count": 730812})


def repeat_url(template, source):
    return reverse("portal_forms:raise", args=[template.key], query={"again": source.number})


def reads(response):
    # The pre-feature base has the same portal and draft workflow, without access records.
    if "accessrecord" not in apps.get_app_config("core").models:
        return []
    return list(
        apps.get_model("core", "AccessRecord")
        .objects.filter(request_ref=response["X-Request-ID"])
        .values_list("kind", "ticket_id", "account_id")
    )


@pytest.mark.parametrize("headers", TRANSPORTS)
def test_unsupported_methods_stop_before_form_and_source_lookup(
    portal_user, pickup, source, headers, monkeypatch
):
    from request_forms import portal_views

    client = Client(enforce_csrf_checks=True)
    client.force_login(portal_user)
    url = repeat_url(pickup, source)
    token = Inputs(client.get(url)).values["csrfmiddlewaretoken"]
    lookups = []
    lookup = portal_views.portal_templates

    def observe(user):
        lookups.append(user.pk)
        return lookup(user)

    monkeypatch.setattr(portal_views, "portal_templates", observe)
    missing = reverse(
        "portal_forms:raise", args=["synthetic-missing"], query={"again": source.number}
    )
    for target in (url, missing):
        for method in ("OPTIONS", "PUT", "PATCH", "DELETE", "TRACE"):
            response = client.generic(method, target, headers={**headers, "X-CSRFToken": token})
            assert response.status_code == 405, (method, target)
            assert set(response["Allow"].split(", ")) == {"GET", "HEAD", "POST"}
            assert not response.content and "no-store" in response["Cache-Control"]
            assert "X-VR-Form" in response["Vary"]
            assert "HX-Location" not in response and "HX-Redirect" not in response
            assert reads(response) == []
    assert not lookups and ServiceRequest.objects.count() == 1


@pytest.mark.parametrize("headers", TRANSPORTS)
def test_audience_precedes_options_and_accepted_get_keeps_ticket_scope(
    agent, outsider, pickup, source, headers
):
    client = Client(enforce_csrf_checks=True)
    url = repeat_url(pickup, source)
    for method in (client.get, client.options):
        anonymous = method(url, headers=headers)
        location = "HX-Redirect" if "X-VR-Form" in headers else "Location"
        assert anonymous.status_code == (204 if "X-VR-Form" in headers else 302)
        redirect = urlsplit(anonymous[location])
        assert redirect.path == reverse("account_login") and parse_qs(redirect.query)["next"] == [
            url
        ]
        assert reads(anonymous) == []
    client.force_login(agent)
    for method in (client.get, client.options):
        refused = method(url, headers=headers)
        assert refused.status_code == 403 and reads(refused) == []
        assert b"730812" not in refused.content and "Allow" not in refused
    client.force_login(outsider)
    options = client.options(url, headers=headers)
    assert options.status_code == 405 and reads(options) == []
    foreign = client.get(url, headers=headers)
    assert foreign.status_code == 404 and b"730812" not in foreign.content and reads(foreign) == []


@pytest.mark.parametrize("headers", TRANSPORTS)
def test_allowed_methods_keep_their_form_help_validation_and_redirect(
    portal_user, pickup, source, headers
):
    client = Client(enforce_csrf_checks=True)
    client.force_login(portal_user)
    url = repeat_url(pickup, source)
    first = client.get(url, headers=headers)
    assert first.status_code == 200 and Inputs(first).values["field-count"] == "730812"
    assert reads(first) == [("repeat", source.pk, portal_user.pk)]
    assert first.context["page_documentation"]["id"] == "portal-forms-raise"
    assert b'data-vr-documentation-id="portal-forms-raise"' in first.content
    head = client.head(url, headers=headers)
    assert head.status_code == 200 and not head.content and reads(head) == []
    token = Inputs(first).values
    data = {"form_definition": token["form_definition"], "field-count": "0"}
    no_csrf = client.post(url, data, headers=headers)
    assert no_csrf.status_code == 403 and reads(no_csrf) == []
    data["csrfmiddlewaretoken"] = token["csrfmiddlewaretoken"]
    invalid = client.post(url, data, headers=headers)
    assert invalid.status_code == 200 and invalid.context["form"].errors.get("field-count")
    assert invalid.context["page_documentation"]["id"] == "portal-forms-raise"
    assert Inputs(invalid).values["field-count"] == "0" and reads(invalid) == []
    saved = client.post(url, {**data, "field-count": "3"}, headers=headers)
    created = ServiceRequest.objects.exclude(pk=source.pk).get()
    location = "HX-Redirect" if "X-VR-Form" in headers else "Location"
    assert saved.status_code == (204 if "X-VR-Form" in headers else 302)
    assert saved[location] == reverse("portal:request-detail", args=[created.number])
    assert reads(saved) == []


@pytest.fixture
def pending_draft(portal_user, pickup, source):
    client = Client(enforce_csrf_checks=True)
    client.force_login(portal_user)
    url = repeat_url(pickup, source)
    original = client.get(url)
    assert Inputs(original).values["field-count"] == "730812"
    token = marker(original)[0]
    end_open(portal_user)
    keep_url = reverse("account-session-keep")
    refused = client.options(keep_url)
    assert refused.status_code == 405 and refused["Allow"] == "POST"
    assert not KeptDraft.objects.exists() and reads(refused) == []
    kept = client.post(
        keep_url,
        {"forms": [{"token": token, "values": {"field-count": "859120"}}]},
        content_type="application/json",
    )
    assert kept.status_code == 200 and kept.json() == {"kept": 1, "held": [True]}
    assert reads(kept) == []
    client.force_login(portal_user)
    return client, url


def test_only_full_navigation_restores_the_repeat_draft(pending_draft, pickup, source, portal_user):
    client, url = pending_draft
    draft = KeptDraft.objects.get()
    options = client.options(url)
    assert options.status_code == 405 and reads(options) == []
    head = client.head(url)
    assert head.status_code == 200 and reads(head) == []
    for headers in ({"HX-Request": "true"}, {"X-VR-Form": "1"}, {"Sec-Purpose": "prefetch"}):
        partial = client.get(url, headers=headers)
        assert partial.status_code == 200 and Inputs(partial).values["field-count"] == "730812"
        assert given_back(partial) is None
    plain = client.get(reverse("portal_forms:raise", args=[pickup.key]))
    assert given_back(plain) is None and reads(plain) == []
    draft.refresh_from_db()
    assert draft.taken_at is None and draft.values["field-count"] == "859120"
    restored = client.get(url)
    assert given_back(restored)["field-count"] == "859120"
    assert Inputs(restored).values["field-count"] == "859120"
    assert f"Answers copied from {source.number}".encode() in restored.content
    assert reads(restored) == [("repeat", source.pk, portal_user.pk)]
    draft.refresh_from_db()
    assert draft.taken_at is not None and draft.values is None
    following = client.get(url)
    assert given_back(following) is None and Inputs(following).values["field-count"] == "730812"


def test_restored_form_help_does_not_say_the_record_proves_the_source_answers_were_opened(
    pending_draft,
):
    client, url = pending_draft
    restored = client.get(url)
    assert given_back(restored)["field-count"] == Inputs(restored).values["field-count"] == "859120"
    assert b'value="730812"' not in restored.content
    help_page = restored.context["page_documentation"]
    assert help_page["id"] == "portal-forms-raise"
    # The behavior is accepted. Check the explanation of the record supplied with this very page.
    for section in help_page["sections"]:
        paragraphs = " ".join(section.get("paragraphs", [])).lower()
        if "keeps a record that you opened them" in paragraphs:
            assert any(
                word in paragraphs for word in ("restor", "put back", "kept typing", "draft")
            ), (
                "The access-record explanation says the source answers were opened, "
                "even when the form displays only the user's restored typing"
            )
