"""Pages of request forms: who may open each URL directly, the portal raise flow, the staff
correction page, and the template tags other pages include."""

import pytest
from django.template import Context, Template
from django.test import RequestFactory
from django.urls import reverse

from desk.models import EntryKind, ServiceRequest
from request_forms.models import FieldValue
from request_forms.services import create_request_with_form

from .conftest import PASSWORD, PATIENT_SENTINEL, definition_of

pytestmark = pytest.mark.django_db


def login(client, user):
    assert client.login(username=user.username, password=PASSWORD)
    return client


def raised(user, template, values, **extra):
    extra.setdefault("first_entry_kind", EntryKind.REPLY if user.client_id else EntryKind.NOTE)
    extra.setdefault("channel", "portal" if user.client_id else "phone")
    return create_request_with_form(
        actor=user,
        template=template,
        expected_definition=definition_of(template),
        values=values,
        **extra,
    )


def setup_urls(template, catalog):
    field = template.fields.first()
    item = catalog.items.first()
    return [
        reverse("request_forms:template-list"),
        reverse("request_forms:template-create"),
        reverse("request_forms:preset-load"),
        reverse("request_forms:template-detail", args=[template.pk]),
        reverse("request_forms:template-edit", args=[template.pk]),
        reverse("request_forms:template-preview", args=[template.pk]),
        reverse("request_forms:field-create", args=[template.pk]),
        reverse("request_forms:field-edit", args=[field.pk]),
        reverse("request_forms:catalog-list"),
        reverse("request_forms:catalog-create"),
        reverse("request_forms:catalog-detail", args=[catalog.pk]),
        reverse("request_forms:catalog-edit", args=[catalog.pk]),
        reverse("request_forms:item-create", args=[catalog.pk]),
        reverse("request_forms:item-edit", args=[item.pk]),
    ]


def test_setup_pages_admin_only(client, admin, agent, portal_user, pickup, catalog):
    urls = setup_urls(pickup, catalog)
    login(client, admin)
    for url in urls:
        assert client.get(url).status_code == 200, url
    for user in (agent, portal_user):
        client.logout()
        login(client, user)
        for url in urls:
            assert client.get(url).status_code == 403, (user, url)
    client.logout()
    assert client.get(urls[0]).status_code == 302


def test_setup_moves_are_post_only_and_admin_only(client, admin, agent, pickup):
    field = pickup.fields.first()
    url = reverse("request_forms:field-move", args=[field.pk, "down"])
    login(client, agent)
    assert client.post(url).status_code == 403
    client.logout()
    login(client, admin)
    assert client.get(url).status_code == 405
    assert client.post(url).status_code == 302


def test_admin_creates_a_form_and_a_field(client, admin, pickup_category):
    login(client, admin)
    response = client.post(
        reverse("request_forms:template-create"),
        {
            "name": "Courier visit",
            "category": pickup_category.pk,
            "offered_in_portal": "on",
            "featured": "on",
            "icon": "clock-history",
        },
    )
    assert response.status_code == 302
    template = pickup_category.request_templates.get(name="Courier visit")
    assert template.key == "courier-visit"
    response = client.post(
        reverse("request_forms:field-create", args=[template.pk]),
        {
            "label": "Cooler count",
            "kind": "number",
            "audience": "client",
            "required": "on",
            "whole_number": "on",
            "min_value": "1",
        },
    )
    assert response.status_code == 302, response.content.decode()[:2000]
    assert template.fields.get().key == "cooler-count"


def test_field_form_explains_patient_information(client, admin, pickup):
    login(client, admin)
    page = client.get(reverse("request_forms:field-create", args=[pickup.pk])).content.decode()
    assert "This field holds patient information" in page
    assert "never appear in email, lists, search, logs" in page


def test_changing_field_type_validates_only_the_settings_on_screen(client, admin, pickup):
    request_field = pickup.fields.get(key="items")
    login(client, admin)
    response = client.post(
        reverse("request_forms:field-edit", args=[request_field.pk]),
        {
            "label": "Courier instructions",
            "kind": "short_text",
            "audience": "client",
            # A native form still posts hidden controls after the operator changes Type.
            "catalog": request_field.catalog_id,
            "min_value": "unfinished",
            "choices": "Morning\nAfternoon",
            "expected_revision": request_field.revision,
            "is_active": "on",
            "reason": "This field now records instructions.",
        },
    )
    assert response.status_code == 302
    request_field.refresh_from_db()
    assert request_field.kind == "short_text"
    assert request_field.catalog_id is None and request_field.min_value is None
    assert request_field.choices == []


# Portal --------------------------------------------------------------------------------------


def test_portal_pages_refuse_staff_and_hide_unoffered_forms(
    client, agent, portal_user, pickup, staff_template
):
    login(client, agent)
    assert client.get(reverse("portal_forms:choose")).status_code == 403
    assert client.get(reverse("portal_forms:raise", args=["pickup"])).status_code == 403
    client.logout()
    login(client, portal_user)
    assert client.get(reverse("portal_forms:raise", args=[staff_template.key])).status_code == 404
    assert client.get(reverse("portal_forms:raise", args=["no-such-form"])).status_code == 404
    page = client.get(reverse("portal_forms:choose")).content.decode()
    assert "Request a pickup" in page and "Something else" in page
    assert "Internal check form" not in page


def test_portal_form_shows_only_client_fields(client, portal_user, pickup):
    login(client, portal_user)
    page = client.get(reverse("portal_forms:raise", args=["pickup"])).content.decode()
    assert "Number of coolers" in page and "Transport bags" in page
    assert "Courier" not in page and "Tracking" not in page
    assert "Retired item" not in page
    assert "Enter patient details only in the fields that ask for them" in page


def test_portal_raise_ignores_injected_staff_fields(client, portal_user, pickup, catalog):
    login(client, portal_user)
    bags = catalog.items.get(code="TEST-BAG")
    response = client.post(
        reverse("portal_forms:raise", args=["pickup"]),
        {
            "field-count": "2",
            f"field-items_{bags.pk}": "3",
            "field-courier": "injected",
            "field-tracking": "injected",
            "subject": "",
            "description": "",
            "client_reference": "ORD-TEST-1",
            "form_definition": definition_of(pickup),
        },
    )
    request = ServiceRequest.objects.get()
    assert response.status_code == 302
    assert response.url == reverse("portal:request-detail", args=[request.number])
    assert request.subject == "Request a pickup" and request.client_reference == "ORD-TEST-1"
    assert set(FieldValue.objects.values_list("key", flat=True)) == {"count", "items"}


def test_portal_raise_keeps_input_on_error(client, portal_user, pickup):
    login(client, portal_user)
    response = client.post(
        reverse("portal_forms:raise", args=["pickup"]),
        {"field-count": "", "field-handling": "Frozen", "subject": "Kept subject"},
    )
    page = response.content.decode()
    assert response.status_code == 200
    assert "This field is required." in page and "Kept subject" in page
    assert not ServiceRequest.objects.exists()


# Staff corrections -------------------------------------------------------------------------------


def test_correction_page_scope(client, agent, capable, portal_user, outsider, pickup):
    request = raised(portal_user, pickup, {"count": 1, "patient": PATIENT_SENTINEL})
    url = reverse("request_details:edit", args=[request.number])
    login(client, agent)
    page = client.get(url).content.decode()
    assert "Number of coolers" in page
    assert PATIENT_SENTINEL not in page
    assert "Only staff who can view patient information see, fill or correct them" in page
    client.logout()
    login(client, capable)
    page = client.get(url).content.decode()
    assert PATIENT_SENTINEL not in page  # revealed one by one on the request, never pre-filled
    assert "A value is recorded and not shown here" in page
    client.logout()
    login(client, outsider)
    assert client.get(url).status_code == 403
    client.logout()
    login(client, agent)
    plain = ServiceRequest.objects.create(
        number="REQ-999999",
        channel="phone",
        category=pickup.category,
        subject="No form",
        team=request.team,
        created_by=agent,
    )
    assert client.get(reverse("request_details:edit", args=[plain.number])).status_code == 404


def test_correction_page_hides_restricted_requests(client, agent, capable, pickup):
    pickup.category.is_restricted = True
    pickup.category.save()
    request = raised(capable, pickup, {"count": 1})
    login(client, agent)
    assert client.get(reverse("request_details:edit", args=[request.number])).status_code == 404


def test_correction_page_records_a_change(client, agent, portal_user, pickup):
    request = raised(portal_user, pickup, {"count": 1})
    url = reverse("request_details:edit", args=[request.number])
    old = FieldValue.objects.get(key="count")
    login(client, agent)
    data = {
        "field-count": "4",
        "seen_count": str(old.pk),
        "reason": "",
        "form_definition": definition_of(request),
    }
    for key in ("ready-at", "courier", "tracking", "items", "handling"):
        data[f"seen_{key}"] = ""
    response = client.post(url, data)
    assert response.status_code == 200
    assert "A reason is required to change or clear a value." in response.content.decode()
    data["reason"] = "Client called back"
    response = client.post(url, data)
    assert response.status_code == 302
    assert FieldValue.objects.get(key="count", superseded_at__isnull=True).value == "4"
    page = client.get(url).content.decode()
    assert "Number of coolers: 1" in page and "Client called back" in page


# Template tags -----------------------------------------------------------------------------------


def render(user, source, **context):
    http = RequestFactory().get("/")
    http.user = user
    return Template("{% load request_forms %}" + source).render(
        Context({"request": http, **context})
    )


def test_details_tag_by_audience(agent, capable, portal_user, outsider, pickup):
    request = raised(portal_user, pickup, {"count": 2, "patient": PATIENT_SENTINEL})
    from request_forms.services import correct_values

    correct_values(
        actor=agent,
        request=request,
        expected_definition=definition_of(request),
        values={"courier": "Route 4", "tracking": "TRK-TEST"},
        reason="",
        expected={"courier": None, "tracking": None},
    )
    tag = "{% request_form_details service_request %}"
    client_html = render(portal_user, tag, service_request=request)
    assert "TRK-TEST" in client_html and PATIENT_SENTINEL in client_html
    assert "Route 4" not in client_html and "Edit form details" not in client_html
    staff_html = render(agent, tag, service_request=request)
    assert "Route 4" in staff_html and "Not shown to the client" in staff_html
    assert PATIENT_SENTINEL not in staff_html and "You do not have permission" in staff_html
    capable_html = render(capable, tag, service_request=request)
    assert PATIENT_SENTINEL not in capable_html and "Reveal" in capable_html
    assert render(outsider, tag, service_request=request).strip() == ""


def test_service_menu_tag(portal_user, pickup):
    html = render(portal_user, "{% portal_service_menu %}")
    assert reverse("portal_forms:raise", args=["pickup"]) in html
    assert "Request a pickup" in html and reverse("portal:request-create") in html


def test_chooser_groups_many_forms_by_category_group(client, portal_user, pickup, group):
    from request_forms.models import RequestTemplate
    from taxonomy.models import RequestCategory

    other_group = RequestCategory.objects.create(
        key="status-results", name="Status and results", client_label="Results", position=99
    )
    results = RequestCategory.objects.create(
        key="result-status", name="Result status", parent=other_group, client_may_raise=True
    )
    for number in range(7):
        RequestTemplate.objects.create(
            key=f"result-{number}", name=f"Result form {number}", category=results
        )
    login(client, portal_user)
    page = client.get(reverse("portal_forms:choose")).content.decode()
    assert page.index("Supplies and logistics") < page.index("Request a pickup")
    assert page.index("Request a pickup") < page.index("Results") < page.index("Result form 0")
    assert page.index("Anything else") < page.index("Something else")


# Reveal ----------------------------------------------------------------------------------------


def test_reveal_patient_value(client, agent, capable, portal_user, pickup, pickup_category):
    from core.models import AuditEvent

    request = raised(portal_user, pickup, {"count": 1, "patient": PATIENT_SENTINEL})
    row = FieldValue.objects.get(key="patient")
    url = reverse("request_details:reveal", args=[row.pk])
    login(client, agent)
    assert client.post(url).status_code == 404  # no capability: nothing to reveal
    client.logout()
    login(client, capable)
    assert client.get(url).status_code == 405
    response = client.post(url, HTTP_HX_REQUEST="true")
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    assert PATIENT_SENTINEL in response.content.decode()
    event = AuditEvent.objects.get(action="request_form.value_revealed")
    assert event.after == {"request_id": request.pk, "field": "patient", "value_id": row.pk}
    assert PATIENT_SENTINEL not in str(event.after) + event.reason
    page = client.post(url)  # without JavaScript: a page titled by the label, not the value
    assert "<title>Patient identifier" in page.content.decode()
    plain = FieldValue.objects.get(key="count")
    assert client.post(reverse("request_details:reveal", args=[plain.pk])).status_code == 404
    pickup_category.is_restricted = True
    pickup_category.save()
    capable.groups.clear()
    from django.contrib.auth.models import Group

    capable.groups.add(Group.objects.get_or_create(name="client_services")[0])
    assert client.post(url).status_code == 404  # restricted request outside its scope


def test_capable_staff_replace_a_hidden_patient_value(client, capable, portal_user, pickup):
    request = raised(portal_user, pickup, {"count": 1, "patient": PATIENT_SENTINEL})
    url = reverse("request_details:edit", args=[request.number])
    seen = {row.key: str(row.pk) for row in FieldValue.objects.all()}
    data = {
        f"seen_{key}": seen.get(key, "")
        for key in ("count", "ready-at", "courier", "tracking", "patient", "items", "handling")
    }
    data |= {"field-count": "1", "reason": "", "form_definition": definition_of(request)}
    login(client, capable)
    response = client.post(url, data)  # patient left empty: kept, nothing changed
    assert "Nothing changed." in response.content.decode()
    data |= {"field-patient": "PT-TEST-REPLACED", "patient_note": "Typo in the identifier"}
    response = client.post(url, data)
    assert "Choose why the patient information changed" in response.content.decode()
    assert "no-store" in response["Cache-Control"]  # the typed value is in this response
    data["reason_code"] = "typing"
    assert client.post(url, data).status_code == 302
    assert FieldValue.objects.get(key="patient", superseded_at__isnull=True).value == (
        "PT-TEST-REPLACED"
    )


def test_earlier_patient_values_are_bare_placeholders_without_the_capability(
    client, agent, capable, portal_user, pickup
):
    from request_forms.services import correct_values

    request = raised(portal_user, pickup, {"count": 1, "patient": "PT-TEST-1"})
    correct_values(
        actor=capable,
        request=request,
        expected_definition=definition_of(request),
        values={"patient": "PT-TEST-2"},
        reason="",
        reason_code="typing",
        patient_note=f"Note {PATIENT_SENTINEL}",
        expected={"patient": FieldValue.objects.get(key="patient").pk},
    )
    url = reverse("request_details:edit", args=[request.number])
    login(client, agent)
    response = client.get(url)
    page = response.content.decode()
    assert "no-store" in response["Cache-Control"]
    assert "Patient identifier: patient information (not shown)" in page
    import re

    history = " ".join(re.findall(r'<li class="vr-history-item">.*?</li>', page, re.S))
    assert "patient information (not shown)" in history
    assert PATIENT_SENTINEL not in page and "PT-TEST" not in page
    assert "Typing error" not in history and "Changed by" not in history
    client.logout()
    login(client, capable)
    page = client.get(url).content.decode()
    history = " ".join(re.findall(r'<li class="vr-history-item">.*?</li>', page, re.S))
    assert "Typing error" in history and PATIENT_SENTINEL in history  # code and note
    assert "PT-TEST" not in page  # never the value


def test_portal_pages_with_values_are_not_cached(client, portal_user, pickup):
    request = raised(portal_user, pickup, {"count": 1, "patient": PATIENT_SENTINEL})
    login(client, portal_user)
    detail = client.get(reverse("portal:request-detail", args=[request.number]))
    assert PATIENT_SENTINEL in detail.content.decode()
    assert "no-store" in detail["Cache-Control"]
    failed = client.post(
        reverse("portal_forms:raise", args=["pickup"]),
        {"field-count": "", "field-patient": PATIENT_SENTINEL},
    )
    assert failed.status_code == 200 and "no-store" in failed["Cache-Control"]
