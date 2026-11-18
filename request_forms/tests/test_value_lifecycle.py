"""Form values during intake, correction, retirement and repeat requests."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from core.models import AuditEvent
from desk import services as desk
from desk.models import ServiceRequest, Status
from desk.tests.test_ticket_actions import change
from request_forms import selectors, services
from request_forms.models import Audience, FieldKind, FilledForm, RequestTemplate
from taxonomy.terms import drawn

from .conftest import PASSWORD, definition_of, revision_of
from .test_staff_integration import intake
from .test_values import current, portal_raise, staff_raise

pytestmark = pytest.mark.django_db

MARK = "SYNTHETIC-VALUE-421"


# A stale intake never rebinds answers to another form


def test_retired_staff_template_does_not_rebind_answers(client, capable, admin, pickup, clinic):
    replacement = RequestTemplate.objects.create(
        key="other-pickup", name="Other pickup", category=pickup.category
    )
    replacement.fields.create(
        key="patient", label="Public reference", kind=FieldKind.SHORT_TEXT, audience=Audience.CLIENT
    )
    client.force_login(capable)
    page = client.get(
        reverse("desk:category-guidance"),
        {"category": pickup.category_id, "request_form": pickup.pk},
    )
    assert b"Patient identifier" in page.content
    services.update_template(
        actor=admin,
        template_id=pickup.pk,
        expected_revision=pickup.revision,
        reason="Retire old intake",
        is_active=False,
    )
    response = client.post(
        reverse("desk:request-create"),
        {
            "category": pickup.category_id,
            "category_terms": drawn([pickup.category]),
            "client": clinic.pk,
            "channel": "phone",
            "subject": "Pickup",
            "body": "Synthetic call",
            "first_entry_kind": "note",
            "request_form": pickup.pk,
            "field-count": "1",
            "field-patient": MARK,
        },
    )
    assert not FilledForm.objects.filter(
        template=replacement, values__value=MARK, values__holds_patient_information=False
    ).exists()
    # Refused with a message, nothing saved, and the typed value is not cached.
    assert response.status_code == 200 and not ServiceRequest.objects.exists()
    assert "no longer used for this category" in response.content.decode()


def test_staff_intake_refuses_a_form_changed_since_it_was_drawn(client, agent, admin, pickup):
    login(client, agent)
    drawn = definition_of(pickup)
    services.create_field(
        actor=admin, template_id=pickup.pk, key="seal", label="Seal number", kind="short_text"
    )
    assert definition_of(pickup) != drawn
    response = intake(
        client,
        pickup.category,
        request_form=pickup.pk,
        form_definition=drawn,
        **{"field-count": "1"},
    )
    page = response.content.decode()
    assert response.status_code == 200 and not ServiceRequest.objects.exists()
    assert "This form changed after the page was opened" in page
    # Shown again with the current definition: sending it again now works.
    assert f'name="form_definition" value="{definition_of(pickup)}"' in page
    again = intake(client, pickup.category, request_form=pickup.pk, **{"field-count": "1"})
    assert again.status_code == 302 and FilledForm.objects.get().template == pickup


def test_portal_submission_of_a_changed_form_is_refused(client, portal_user, admin, pickup):
    login(client, portal_user)
    url = reverse("portal_forms:raise", args=["pickup"])
    drawn = definition_of(pickup)
    field = pickup.fields.get(key="count")
    services.update_field(
        actor=admin,
        field_id=field.pk,
        expected_revision=field.revision,
        reason="Clearer label",
        label="Number of coolers or boxes",
    )
    response = client.post(url, {"field-count": "2", "form_definition": drawn})
    assert response.status_code == 200 and not ServiceRequest.objects.exists()
    assert "This form changed after the page was opened" in response.content.decode()
    # The service refuses a stale revision too, whatever the page did.
    with pytest.raises(ValidationError, match="changed after the page was opened"):
        services.create_request_with_form(
            actor=portal_user,
            template=pickup,
            expected_definition=drawn,
            values={"count": 2},
            first_entry_kind="reply",
            channel="portal",
        )
    assert not ServiceRequest.objects.exists()


def test_portal_submission_of_a_retired_form_is_refused_with_a_message(
    client, portal_user, admin, pickup
):
    login(client, portal_user)
    drawn = revision_of(pickup)
    services.update_template(
        actor=admin,
        template_id=pickup.pk,
        expected_revision=drawn,
        reason="Retired",
        is_active=False,
    )
    url = reverse("portal_forms:raise", args=["pickup"])
    response = client.post(url, {"field-count": "2", "form_definition": drawn})
    assert response.status_code == 302 and response.url == reverse("portal_forms:choose")
    assert not ServiceRequest.objects.exists()
    assert client.get(url).status_code == 404


def test_refused_form_leaves_no_file_and_a_bad_file_leaves_no_request(
    client, portal_user, admin, pickup, monkeypatch
):
    """With attachments: the request, its form values and its files commit together."""
    from attachments import services as attachments
    from attachments.models import Attachment
    from attachments.tests.conftest import SAMPLES, upload
    from request_forms import portal_views
    from request_forms.models import FieldValue

    login(client, portal_user)
    url = reverse("portal_forms:raise", args=["pickup"])
    drawn = definition_of(pickup)
    field = pickup.fields.get(key="count")
    services.update_field(
        actor=admin,
        field_id=field.pk,
        expected_revision=field.revision,
        reason="Clearer label",
        label="Number of coolers or boxes",
    )
    stale = client.post(
        url,
        {
            "field-count": "2",
            "form_definition": drawn,
            "files": [upload("cooler.png", SAMPLES["photo.png"])],
        },
    )
    assert (
        stale.status_code == 200 and "changed after the page was opened" in stale.content.decode()
    )
    assert not ServiceRequest.objects.exists() and not Attachment.objects.exists()

    def refuse(actor, request, files):
        attachments.attach_to_new_request(actor, request, files)  # stored, then refused
        raise ValidationError("Synthetic refusal of a file after storing it")

    monkeypatch.setattr(portal_views, "attach_to_new_request", refuse)
    refused = client.post(
        url,
        {
            "field-count": "2",
            "form_definition": definition_of(pickup),
            "files": [upload("cooler.png", SAMPLES["photo.png"])],
        },
    )
    assert refused.status_code == 200 and "Synthetic refusal" in refused.content.decode()
    assert not ServiceRequest.objects.exists() and not Attachment.objects.exists()
    assert not FieldValue.objects.exists() and not FilledForm.objects.exists()


# Request again follows the display rule


def test_repeat_respects_captured_audience(pickup, agent, admin, clinic, portal_user):
    record = staff_raise(agent, pickup, {"count": 1, "courier": MARK}, client=clinic)
    field = pickup.fields.get(key="courier")
    services.update_field(
        actor=admin,
        field_id=field.pk,
        expected_revision=field.revision,
        reason="Clients enter this on future requests",
        audience=Audience.CLIENT,
    )
    details = selectors.client_values(record, portal_user)
    assert all(MARK not in row.lines for row in details.rows)
    initial, _ = selectors.repeat_values(pickup, portal_user, record.number)
    assert "courier" not in initial, "Repeat prefilled an answer hidden on the source request"


# The patient reason and note stay with changed patient values


def test_private_correction_note_stays_private_if_reentered_value_is_unchanged(
    pickup, capable, portal_user
):
    record = portal_raise(portal_user, pickup, {"count": 1, "patient": MARK})
    rows = current(record)
    services.correct_values(
        actor=capable,
        request=record,
        expected_definition=definition_of(record),
        values={"count": 2, "patient": MARK},
        expected={key: rows[key].pk for key in ("count", "patient")},
        reason=f"Checked source reference {MARK}",
        reason_code="source_record",
    )
    audit = AuditEvent.objects.get(action="request_form.values_changed")
    assert MARK not in audit.reason, (
        "Free text submitted with the patient reason code reached audit"
    )


def test_patient_code_and_note_are_dropped_when_no_patient_value_changes(
    pickup, capable, portal_user
):
    record = portal_raise(portal_user, pickup, {"count": 1, "patient": MARK})
    rows = current(record)
    services.correct_values(
        actor=capable,
        request=record,
        expected_definition=definition_of(record),
        values={"count": 2, "patient": MARK},
        expected={key: rows[key].pk for key in ("count", "patient")},
        reason="Recount",
        reason_code="source_record",
        patient_note=f"Checked {MARK}",
    )
    audit = AuditEvent.objects.get(action="request_form.values_changed")
    assert audit.after["reason_code"] == "" and audit.after["fields"] == ["count"]
    now = current(record)
    assert now["patient"].pk == rows["patient"].pk  # untouched
    assert now["count"].reason == "Recount" and now["count"].reason_code == ""
    assert not any(MARK in (row.reason or "") for row in now.values())


# Scope is checked again before the correction page is drawn


def test_correction_render_honours_negative_scope_check(client, monkeypatch, agent, pickup):
    from request_forms import request_views

    record = staff_raise(agent, pickup, {}, details_not_known=True)
    client.force_login(agent)
    monkeypatch.setattr(request_views, "staff_values", lambda *args, **kwargs: None)
    result = client.post(
        reverse("request_details:edit", args=[record.number]), {"field-count": "missing"}
    )
    assert result.status_code == 404


# A closed request's form values are frozen


def test_closed_request_keeps_its_client_form_values(pickup, agent, portal_user):
    record = portal_raise(portal_user, pickup, {"count": 2})
    change(record, desk.change_status, agent, status=Status.RESOLVED, summary="Completed")
    change(record, desk.change_status, agent, status=Status.CLOSED)
    with pytest.raises((PermissionDenied, ValidationError)):
        services.correct_values(
            actor=agent,
            request=record,
            expected_definition=definition_of(record),
            expected={"count": current(record)["count"].pk},
            values={"count": 3},
            reason="Recount",
        )


def test_closed_request_offers_no_correction_page(client, pickup, agent, portal_user):
    record = portal_raise(portal_user, pickup, {"count": 2})
    change(record, desk.change_status, agent, status=Status.RESOLVED, summary="Completed")
    change(record, desk.change_status, agent, status=Status.CLOSED)
    login(client, agent)
    edit = reverse("request_details:edit", args=[record.number])
    page = client.get(reverse("desk:request-detail", args=[record.pk])).content.decode()
    assert "Number of coolers" in page and edit not in page
    response = client.post(edit, {"field-count": "3", "reason": "Recount"})
    assert response.status_code == 302
    assert current(record)["count"].value == "2"


# A correction moves the request's revision on


def test_correction_invalidates_old_bulk_selection(pickup, agent, portal_user):
    record = portal_raise(portal_user, pickup, {"count": 2})
    initial_revision = record.revision
    services.correct_values(
        actor=agent,
        request=record,
        expected_definition=definition_of(record),
        expected={"count": current(record)["count"].pk},
        values={"count": 3},
        reason="Recount",
    )
    with pytest.raises(ValidationError):
        desk.bulk_status(
            actor=agent, selected={record.pk: initial_revision}, status=Status.WAITING_CLIENT
        )


# A retired item stays on the order through unrelated corrections


def test_unrelated_correction_preserves_retired_order_line(
    client, portal_user, agent, admin, pickup, catalog
):
    item = catalog.items.get(code="TEST-BAG")
    record = portal_raise(portal_user, pickup, {"count": 1, "items": {item.pk: 2}})
    original = current(record)
    services.update_item(
        actor=admin,
        item_id=item.pk,
        expected_revision=item.revision,
        reason="No longer available",
        is_active=False,
    )
    client.force_login(agent)
    page = client.post(
        reverse("request_details:edit", args=[record.number]),
        {
            "field-count": "2",
            "seen_count": str(original["count"].pk),
            "seen_items": str(original["items"].pk),
            "form_definition": definition_of(record),
            "reason": "Correct the cooler count",
        },
    )
    assert page.status_code == 302
    assert current(record)["count"].value == "2"
    assert current(record)["items"].pk == original["items"].pk, "Unedited order line was superseded"


def test_correction_page_shows_and_keeps_a_retired_line(
    client, portal_user, agent, admin, pickup, catalog
):
    bag = catalog.items.get(code="TEST-BAG")
    record = portal_raise(portal_user, pickup, {"count": 1, "items": {bag.pk: 2}})
    original = current(record)
    services.update_item(
        actor=admin,
        item_id=bag.pk,
        expected_revision=bag.revision,
        reason="No longer available",
        is_active=False,
    )
    login(client, agent)
    url = reverse("request_details:edit", args=[record.number])
    page = client.get(url).content.decode()
    assert "No longer offered" in page and f'name="field-items_{bag.pk}"' in page
    for invalid_rows in ("", "invalid"):
        refused = client.post(
            url,
            {
                "kept_rows": invalid_rows,
                "seen_items": str(original["items"].pk),
                "seen_count": str(original["count"].pk),
                f"field-items_{bag.pk}": "0",
                "field-count": "3",
                "form_definition": definition_of(record),
                "reason": "Recount",
            },
        )
        assert refused.status_code == 200
        assert refused.context["form"].is_bound is False  # redraw after contradictory row IDs
        assert current(record)["count"].pk == original["count"].pk
        assert current(record)["items"].pk == original["items"].pk
    # As the browser sends it back: the retired line with its quantity, the count changed.
    response = client.post(
        url,
        {
            "field-count": "3",
            f"field-items_{bag.pk}": "2",
            "seen_count": str(original["count"].pk),
            "seen_items": str(original["items"].pk),
            "form_definition": definition_of(record),
            "reason": "Recount",
        },
    )
    assert response.status_code == 302
    assert current(record)["items"].pk == original["items"].pk
    # Changing the field itself may remove the retired line, with a reason.
    response = client.post(
        url,
        {
            f"field-items_{bag.pk}": "0",
            "seen_count": str(current(record)["count"].pk),
            "seen_items": str(original["items"].pk),
            "form_definition": definition_of(record),
            "reason": "Not sent",
        },
    )
    assert response.status_code == 302 and current(record).get("items").value is None
    # A retired item cannot be added to an order that did not hold it.
    other = portal_raise(portal_user, pickup, {"count": 1})
    with pytest.raises(ValidationError):
        services.correct_values(
            actor=agent,
            request=other,
            expected_definition=definition_of(other),
            values={"items": {bag.pk: 1}},
            reason="",
            expected={"items": None},
        )


# A recorded "No" is a value: replacing it needs a reason


@pytest.mark.parametrize("patient", [False, True])
def test_no_answer_needs_correction_reason(pickup, capable, portal_user, patient):
    field = pickup.fields.create(
        key="yes-or-no",
        label="Confirmed detail?",
        kind=FieldKind.YES_NO,
        holds_patient_information=patient,
    )
    record = portal_raise(portal_user, pickup, {"count": 1, field.key: "no"})
    original = current(record)[field.key]
    assert original.value is False
    with pytest.raises(ValidationError):
        services.correct_values(
            actor=capable,
            request=record,
            expected_definition=definition_of(record),
            values={field.key: "yes"},
            expected={field.key: original.pk},
            reason="",
        )


# A refused staff intake with patient information is not cached


def test_staff_invalid_intake_protects_patient_response(client, capable, pickup):
    client.force_login(capable)
    response = intake(client, pickup.category, request_form=pickup.pk, **{"field-patient": MARK})
    assert response.status_code == 200
    assert MARK.encode() not in response.content or "no-store" in response.get("Cache-Control", "")
    assert "no-store" in response["Cache-Control"]


def test_staff_intake_is_never_cached_whatever_the_form(client, agent, pickup):
    # Whether a field holds patient information may change
    # between drawing and sending, so the intake page is never cached, with or without one.
    login(client, agent)  # no capability: the bound form has no patient field
    response = intake(client, pickup.category, request_form=pickup.pk)
    assert response.status_code == 200 and "no-store" in response.get("Cache-Control", "")
    assert "no-store" in client.get(reverse("desk:request-create")).get("Cache-Control", "")


# A retired catalog takes no new orders


def test_retired_catalog_does_not_take_new_orders(pickup, catalog, admin, portal_user):
    item = catalog.items.get(code="TEST-BAG")
    try:
        services.update_catalog(
            actor=admin,
            catalog_id=catalog.pk,
            expected_revision=catalog.revision,
            reason="Catalog retired",
            is_active=False,
        )
    except ValidationError:
        return  # refusing retirement while a live form uses it is also safe
    with pytest.raises(ValidationError):
        portal_raise(portal_user, pickup, {"count": 1, "items": {item.pk: 1}})


def test_retired_catalog_leaves_its_field_off_new_requests(
    client, pickup, catalog, admin, portal_user, agent
):
    services.update_catalog(
        actor=admin,
        catalog_id=catalog.pk,
        expected_revision=catalog.revision,
        reason="Catalog retired",
        is_active=False,
    )
    login(client, portal_user)
    page = client.get(reverse("portal_forms:raise", args=["pickup"])).content.decode()
    assert "Number of coolers" in page and "field-items_" not in page
    assert portal_raise(portal_user, pickup, {"count": 1}).number  # the rest still works
    client.logout()
    login(client, admin)
    detail = client.get(reverse("request_forms:template-detail", args=[pickup.pk]))
    assert "catalog retired, left off new tickets" in detail.content.decode()


# A required patient field staff cannot see needs the explicit deferral


def test_required_patient_field_needs_explicit_staff_deferral(pickup, admin, agent):
    field = pickup.fields.get(key="patient")
    services.update_field(
        actor=admin,
        field_id=field.pk,
        expected_revision=field.revision,
        reason="Required for this form",
        required=True,
    )
    with pytest.raises(ValidationError):
        staff_raise(agent, pickup, {"count": 1}, details_not_known=False)


def test_deferred_patient_field_is_listed_as_needed_without_a_value(client, pickup, admin, agent):
    field = pickup.fields.get(key="patient")
    services.update_field(
        actor=admin,
        field_id=field.pk,
        expected_revision=field.revision,
        reason="Required for this form",
        required=True,
    )
    login(client, agent)
    refused = intake(client, pickup.category, request_form=pickup.pk, **{"field-count": "1"})
    assert refused.status_code == 200 and not ServiceRequest.objects.exists()
    assert "only staff with “Can view patient information” fill" in refused.content.decode()
    record = staff_raise(agent, pickup, {"count": 1}, details_not_known=True)
    audit = AuditEvent.objects.get(action="request_form.filled")
    assert "patient" in audit.after["missing"]
    details = selectors.staff_values(record, agent)
    assert "Patient identifier" in details.missing
    assert all(row.label != "Patient identifier" for row in details.rows)


def login(client, user):
    assert client.login(username=user.username, password=PASSWORD)
