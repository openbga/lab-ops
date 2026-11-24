"""Second confirmation of access records on the portal, with synthetic accounts and tickets.

Adopted from the review's second confirmation (the reviewer's original is kept outside the
repository). Accepted limits are strict xfails naming the document that states them. No browser
import: CI's unit job installs no Playwright."""

# The imported fixtures are resolved by pytest by name.
# ruff: noqa: F811
import pytest
from django.apps import apps
from django.db import connection
from django.template.response import TemplateResponse
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from clients.models import PortalAccess
from desk.models import ServiceRequest
from desk.tests.test_kept_typing import end_open, given_back, marker, send
from request_forms.models import Audience, FieldKind, FieldValue
from request_forms.tests.conftest import (  # noqa: F401
    PATIENT_SENTINEL,
    catalog,
    clinic,
    clinic_site,
    group,
    logistics,
    make_user,
    pickup,
    pickup_category,
    portal_user,
)
from request_forms.tests.test_capture_consistency import Inputs, other_connection
from request_forms.tests.test_pages import raised

pytestmark = pytest.mark.django_db


def repeat_url(template, source):
    return reverse("portal_forms:raise", args=[template.key], query={"again": source.number})


def rows(response):
    record = apps.get_model("core", "AccessRecord")
    return list(
        record.objects.filter(request_ref=response["X-Request-ID"])
        .order_by("kind", "id")
        .values_list("kind", "ticket_id", "account_id", "capacity", "object_id")
    )


def has_records():
    """The pre-feature base supports the same form and draft flow without the access table."""
    return "accessrecord" in apps.get_app_config("core").models


@pytest.mark.xfail(
    strict=True,
    reason="Accepted limit: typing kept across a sign-out given back over Request again's copied "
    "answers (docs/architecture.md, Access records).",
)
def test_restored_repeat_draft_only_reports_source_answers_the_user_sees(
    client, portal_user, pickup
):
    source = raised(portal_user, pickup, {"count": 730812})
    assert source.site_id is None
    url = repeat_url(pickup, source)
    client.force_login(portal_user)
    first = client.get(url)
    source_notice = f"Answers copied from {source.number}".encode()
    assert first.status_code == 200 and Inputs(first).values["field-count"] == "730812"
    assert source_notice in first.content
    expected = [("repeat", source.pk, portal_user.pk, "client", "")]
    if has_records():
        assert rows(first) == expected
    end_open(portal_user)
    assert send(client, marker(first)[0], {"field-count": "859120"}) == 1

    client.force_login(portal_user)
    restored = client.get(url)
    assert restored.status_code == 200 and given_back(restored)["field-count"] == "859120"
    assert Inputs(restored).values["field-count"] == "859120"
    assert b'value="730812"' not in restored.content
    next_visit = client.get(url)
    assert given_back(next_visit) is None and Inputs(next_visit).values["field-count"] == "730812"
    assert source_notice in next_visit.content
    if has_records():
        assert rows(next_visit) == expected
    observed = (source_notice in restored.content, rows(restored) if has_records() else [])
    assert observed == (False, []), "The only source answer was replaced with the user's own draft"


def test_options_does_not_open_a_repeat_form_or_claim_a_copy(portal_user, pickup):
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(portal_user)
    source = raised(portal_user, pickup, {"count": 730812})
    url = repeat_url(pickup, source)
    response = browser.options(url)
    # OPTIONS asks about the resource's methods. The confirmation brief puts copying on GET.
    observed = (
        Inputs(response).values.get("field-count"),
        f"Answers copied from {source.number}".encode() in response.content,
        rows(response) if has_records() else [],
    )
    assert observed == (None, False, [])


def test_automatically_selected_site_is_not_a_copy(client, portal_user, pickup, clinic_site):
    pickup.fields.filter(key="count").update(copy_on_repeat=False)
    source = raised(portal_user, pickup, {"count": 7})
    assert source.site_id is None
    grant = PortalAccess.objects.get(user=portal_user)
    grant.sites.add(clinic_site)
    grant.whole_organization = False
    grant.save(update_fields=["whole_organization"])
    client.force_login(portal_user)
    response = client.get(repeat_url(pickup, source))
    assert response.status_code == 200
    assert f'value="{clinic_site.pk}" selected' in str(response.context["form"]["site"])
    assert Inputs(response).values["field-count"] == ""
    assert b"Answers copied from" not in response.content
    assert rows(response) == []


def test_zero_is_a_copied_answer_even_without_a_site(client, portal_user, pickup):
    pickup.fields.filter(key="count").update(min_value=0)
    source = raised(portal_user, pickup, {"count": 0, "patient": PATIENT_SENTINEL})
    client.force_login(portal_user)
    response = client.get(repeat_url(pickup, source))
    assert response.status_code == 200 and Inputs(response).values["field-count"] == "0"
    assert f"Answers copied from {source.number}".encode() in response.content
    assert PATIENT_SENTINEL.encode() not in response.content
    assert rows(response) == [("repeat", source.pk, portal_user.pk, "client", "")]


@pytest.mark.django_db(transaction=True)
def test_repeat_redraw_at_save_keeps_the_submitted_site_without_claiming_a_copy(
    client, portal_user, pickup, clinic_site, monkeypatch
):
    from request_forms import portal_views
    from request_forms.services import FORM_CHANGED

    source = raised(portal_user, pickup, {"count": 71})
    create = portal_views.create_request_with_form

    def change_definition_then_create(**kwargs):
        assert (
            other_connection(
                lambda: pickup.fields.filter(key="count").update(label="Coolers in revised form")
            )
            == 1
        )
        return create(**kwargs)

    monkeypatch.setattr(portal_views, "create_request_with_form", change_definition_then_create)
    client.force_login(portal_user)
    url = repeat_url(pickup, source)
    first = client.get(url)
    assert Inputs(first).values["field-count"] == "71"
    posted = {
        "form_definition": Inputs(first).values["form_definition"],
        "field-count": "86",
        "site": str(clinic_site.pk),
    }
    refused = client.post(url, posted)
    assert refused.status_code == 200 and FORM_CHANGED in refused.content.decode()
    assert b"Coolers in revised form" in refused.content
    assert Inputs(refused).values["field-count"] == ""
    assert f'value="{clinic_site.pk}" selected' in str(refused.context["form"]["site"])
    assert ServiceRequest.objects.count() == 1
    assert b"Answers copied from" not in refused.content
    if has_records():
        assert rows(refused) == []


def test_an_invalid_repeat_post_and_its_successful_retry_leave_only_the_get_read(
    client, portal_user, pickup, monkeypatch
):
    from request_forms import portal_views

    source = raised(portal_user, pickup, {"count": 73})
    client.force_login(portal_user)
    url = repeat_url(pickup, source)
    first = client.get(url)
    token = Inputs(first).values["form_definition"]
    assert rows(first) == [("repeat", source.pk, portal_user.pk, "client", "")]

    def unexpected_lookup(*args, **kwargs):
        pytest.fail("A submission reopened its source ticket")

    monkeypatch.setattr(portal_views, "repeat_from", unexpected_lookup)
    refused = client.post(url, {"form_definition": token, "field-count": "0"})
    assert refused.status_code == 200 and refused.context["form"].errors.get("field-count")
    assert Inputs(refused).values["field-count"] == "0"
    assert b"Answers copied from" not in refused.content and rows(refused) == []
    saved = client.post(url, {"form_definition": token, "field-count": "85"})
    assert saved.status_code == 302 and rows(saved) == []
    created = ServiceRequest.objects.exclude(pk=source.pk).get()
    assert saved.url == reverse("portal:request-detail", args=[created.number])
    assert FieldValue.objects.get(filled_form__request=created, key="count").value == "85"


def test_deferred_portal_page_writes_each_target_once_in_one_batch(
    client, portal_user, pickup, monkeypatch
):
    from desk import portal_views

    pickup.fields.create(
        key="second-patient-field",
        label="Second synthetic field",
        kind=FieldKind.SHORT_TEXT,
        audience=Audience.CLIENT,
        holds_patient_information=True,
        position=180,
    )
    ticket = raised(
        portal_user,
        pickup,
        {"count": 3, "patient": PATIENT_SENTINEL, "second-patient-field": "Synthetic second value"},
    )
    targets = set(
        FieldValue.objects.filter(
            filled_form__request=ticket, key__in=["patient", "second-patient-field"]
        ).values_list("pk", flat=True)
    )
    assert len(targets) == 2
    monkeypatch.setattr(portal_views, "render", TemplateResponse)
    client.force_login(portal_user)
    url = reverse("portal:request-detail", args=[ticket.number])
    head = client.head(url)
    assert head.status_code == 200 and head.content == b"" and rows(head) == []
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url)
    assert response.is_rendered and response.status_code == 200
    assert (
        PATIENT_SENTINEL.encode() in response.content
        and b"Synthetic second value" in response.content
    )
    inserts = [q for q in queries if 'INSERT INTO "core_accessrecord"' in q["sql"]]
    assert len(inserts) == 1
    expected = [("ticket", ticket.pk, portal_user.pk, "client", "")]
    expected += [("patient_answer", ticket.pk, portal_user.pk, "client", str(pk)) for pk in targets]
    assert sorted(rows(response)) == sorted(expected)
