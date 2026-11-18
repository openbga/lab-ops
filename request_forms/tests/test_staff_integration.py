"""Request forms inside the desk's staff pages: the form picker and fields on the new-request
page (the slot desk/_intake_fields.html), saving the values in the creation's transaction, and the
form details on the staff request page (desk/_request_details.html)."""

import pytest
from django.urls import reverse

from desk.models import EntryKind, ServiceRequest
from request_forms.models import FieldValue, FilledForm, RequestTemplate
from request_forms.services import create_request_with_form
from taxonomy.terms import drawn

from .conftest import PASSWORD, PATIENT_SENTINEL, definition_of

pytestmark = pytest.mark.django_db


def login(client, user):
    assert client.login(username=user.username, password=PASSWORD)


def intake(client, category, **extra):
    data = {
        "category": category.pk,
        "category_terms": drawn([category]),
        "channel": "phone",
        "subject": "Pickup for the clinic",
        "first_entry_kind": "note",
        "body": "Caller asked for a pickup.",
    }
    data.update(extra)
    chosen = extra.get("request_form")
    if chosen and "form_definition" not in extra:  # as the page drew it
        data["form_definition"] = definition_of(RequestTemplate.objects.get(pk=chosen))
    return client.post(reverse("desk:request-create"), data)


def test_category_with_one_form_offers_it_with_its_fields(client, agent, pickup):
    login(client, agent)
    partial = client.get(reverse("desk:category-guidance"), {"category": pickup.category.pk})
    page = partial.content.decode()
    assert "Ticket form" in page and "Number of coolers" in page and "Courier" in page
    assert "Patient identifier" not in page  # no capability: not offered
    assert "Some required details are not known yet" in page
    none = client.get(
        reverse("desk:category-guidance"), {"category": pickup.category.pk, "request_form": ""}
    ).content.decode()
    assert "Ticket form" in none and "Number of coolers" not in none


def test_staff_log_a_request_with_its_form(client, agent, pickup):
    login(client, agent)
    response = intake(
        client,
        pickup.category,
        request_form=pickup.pk,
        **{"field-count": "2", "field-courier": "Route 4"},
    )
    request = ServiceRequest.objects.get()
    assert response.status_code == 302
    assert response["Location"] == reverse("desk:request-detail", args=[request.pk])
    assert request.channel == "phone" and request.entries.get(kind=EntryKind.NOTE)
    values = dict(FieldValue.objects.values_list("key", "value"))
    assert values == {"count": "2", "courier": "Route 4"}


def test_missing_required_detail_keeps_input_and_creates_nothing(client, agent, pickup):
    login(client, agent)
    response = intake(client, pickup.category, request_form=pickup.pk, **{"field-courier": "R9"})
    page = response.content.decode()
    assert response.status_code == 200 and not ServiceRequest.objects.exists()
    assert "This field is required." in page and "R9" in page
    assert "Check the highlighted form details below" in page
    response = intake(
        client,
        pickup.category,
        request_form=pickup.pk,
        details_not_known="on",
        **{"field-courier": "R9"},
    )
    assert response.status_code == 302
    assert FilledForm.objects.count() == 1


def test_no_form_logs_a_plain_request(client, agent, pickup):
    login(client, agent)
    assert intake(client, pickup.category, request_form="").status_code == 302
    assert ServiceRequest.objects.count() == 1 and not FilledForm.objects.exists()


def test_crafted_patient_field_is_refused(client, agent, pickup):
    login(client, agent)
    response = intake(
        client,
        pickup.category,
        request_form=pickup.pk,
        **{"field-count": "1", "field-patient": PATIENT_SENTINEL},
    )
    # The field is not on this person's form: the posted value is ignored, never stored.
    assert response.status_code == 302
    assert not FieldValue.objects.filter(key="patient").exists()


def test_a_form_of_another_category_is_refused(client, agent, pickup, staff_template):
    login(client, agent)
    response = intake(
        client, pickup.category, request_form=staff_template.pk, **{"field-count": "1"}
    )
    # Never read under the category's only form: refused, input kept, nothing created.
    assert response.status_code == 200 and not ServiceRequest.objects.exists()
    assert "no longer used for this category" in response.content.decode()


def test_intake_without_the_picker_is_refused_when_the_category_has_forms(client, agent, pickup):
    login(client, agent)
    response = intake(client, pickup.category, **{"field-count": "1"})
    assert response.status_code == 200 and not ServiceRequest.objects.exists()
    assert "Choose a ticket form, or No form." in response.content.decode()


def test_staff_request_page_shows_the_details_inside_its_panel(
    client, agent, capable, portal_user, pickup
):
    request = create_request_with_form(
        actor=portal_user,
        template=pickup,
        expected_definition=definition_of(pickup),
        values={"count": 2, "patient": PATIENT_SENTINEL},
        first_entry_kind=EntryKind.REPLY,
        channel="portal",
    )
    url = reverse("desk:request-detail", args=[request.pk])
    login(client, agent)
    page = client.get(url).content.decode()
    assert 'id="form-details-heading"' in page and "Number of coolers" in page
    assert reverse("request_details:edit", args=[request.number]) in page
    assert PATIENT_SENTINEL not in page and "You do not have permission" in page
    client.logout()
    login(client, capable)
    page = client.get(url).content.decode()
    row = FieldValue.objects.get(key="patient")
    assert reverse("request_details:reveal", args=[row.pk]) in page
    assert PATIENT_SENTINEL not in page
    edit = client.get(reverse("request_details:edit", args=[request.number]))
    assert f'href="{url}#form-details-heading"' in edit.content.decode()
