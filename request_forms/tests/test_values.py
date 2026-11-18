"""Values on requests (F2, F4, F6): capture at the service boundary, who may fill what, what each
audience sees, history kept through template changes and corrections, and patient information
kept out of everything but the request page."""

import datetime as dt
import json
import logging

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, transaction

from core.models import AuditEvent
from desk.models import EntryKind, RequestEntry, ServiceRequest
from request_forms.models import Audience, FieldValue, FilledForm
from request_forms.selectors import client_values, portal_menu, staff_values
from request_forms.services import (
    CORRECTION_AUDIT_REASON,
    correct_values,
    create_request_with_form,
)

from .conftest import PATIENT_SENTINEL, definition_of

pytestmark = pytest.mark.django_db


def portal_raise(user, template, values, **extra):
    return create_request_with_form(
        actor=user,
        template=template,
        expected_definition=definition_of(template),
        values=values,
        first_entry_kind=EntryKind.REPLY,
        channel="portal",
        **extra,
    )


def staff_raise(user, template, values, **extra):
    extra.setdefault("first_entry_kind", EntryKind.NOTE)
    extra.setdefault("channel", "phone")
    return create_request_with_form(
        actor=user,
        template=template,
        expected_definition=definition_of(template),
        values=values,
        **extra,
    )


def current(request):
    return {
        row.key: row
        for row in FieldValue.objects.filter(
            filled_form__request=request, superseded_at__isnull=True
        )
    }


# Capture --------------------------------------------------------------------------------------


def test_portal_raise_stores_values_with_copies_and_defaults(portal_user, pickup, catalog):
    bags = catalog.items.get(code="TEST-BAG")
    request = portal_raise(
        portal_user,
        pickup,
        {"count": 3, "items": {bags.pk: 2}, "handling": "Frozen", "patient": PATIENT_SENTINEL},
    )
    assert request.subject == "Request a pickup"
    assert request.category == pickup.category
    assert request.entries.get(kind=EntryKind.REPLY).body == (
        "Details are in the form “Request a pickup”."
    )
    filled = FilledForm.objects.get(request=request)
    assert (filled.template_key, filled.template_client_label) == ("pickup", "Request a pickup")
    rows = current(request)
    assert rows["count"].value == "3"
    assert rows["count"].label == "Number of coolers"
    assert rows["items"].value == [
        {
            "item": bags.pk,
            "code": "TEST-BAG",
            "name": "Transport bags",
            "unit": "pack",
            "quantity": 2,
        }
    ]
    assert rows["patient"].holds_patient_information is True
    assert set(rows) == {"count", "items", "handling", "patient"}  # empty fields store nothing


def test_portal_must_fill_required_client_fields(portal_user, pickup):
    with pytest.raises(ValidationError) as caught:
        portal_raise(portal_user, pickup, {"handling": "Frozen"})
    assert "count" in caught.value.message_dict
    assert not ServiceRequest.objects.exists()


def test_portal_cannot_submit_a_staff_field(portal_user, pickup):
    for key in ("courier", "tracking"):
        with pytest.raises(PermissionDenied):
            portal_raise(portal_user, pickup, {"count": 1, key: "anything"})
    assert not FieldValue.objects.exists()


def test_no_one_submits_another_templates_field(portal_user, agent, pickup, staff_template):
    with pytest.raises(ValidationError):
        portal_raise(portal_user, pickup, {"count": 1, "note": "from another form"})
    with pytest.raises(ValidationError):
        staff_raise(agent, staff_template, {"count": 1})


def test_portal_cannot_skip_required_details(portal_user, pickup):
    with pytest.raises(PermissionDenied):
        portal_raise(portal_user, pickup, {}, details_not_known=True)


def test_portal_cannot_use_a_staff_form(portal_user, staff_template):
    with pytest.raises(PermissionDenied):
        portal_raise(portal_user, staff_template, {})


def test_inactive_form_takes_no_requests(agent, pickup):
    pickup.is_active = False
    pickup.save()
    with pytest.raises(ValidationError):
        staff_raise(agent, pickup, {"count": 1})


def test_staff_logging_a_call_may_leave_required_details_for_later(agent, pickup):
    with pytest.raises(ValidationError):
        staff_raise(agent, pickup, {"courier": "Route 4"})
    request = staff_raise(agent, pickup, {"courier": "Route 4"}, details_not_known=True)
    event = AuditEvent.objects.get(action="request_form.filled")
    assert event.after["missing"] == ["count"]
    details = staff_values(request, agent)
    assert details.missing == ["Number of coolers"]


def test_staff_without_the_capability_cannot_fill_patient_fields(agent, capable, pickup):
    with pytest.raises(PermissionDenied):
        staff_raise(agent, pickup, {"count": 1, "patient": PATIENT_SENTINEL})
    request = staff_raise(capable, pickup, {"count": 1, "patient": PATIENT_SENTINEL})
    assert current(request)["patient"].value == PATIENT_SENTINEL


@pytest.mark.parametrize(
    ("values", "key"),
    [
        ({"count": 0}, "count"),  # below the lowest value
        ({"count": "2.5"}, "count"),  # whole numbers only
        ({"count": 1, "handling": "Boiling"}, "handling"),  # not an option
        ({"count": 1, "items": {"999999": 1}}, "items"),  # not in the catalog
        ({"count": 1, "ready-at": "next week"}, "ready-at"),
    ],
)
def test_values_are_checked_at_the_service(portal_user, pickup, values, key):
    with pytest.raises(ValidationError) as caught:
        portal_raise(portal_user, pickup, values)
    assert key in caught.value.message_dict
    assert "Boiling" not in str(caught.value)  # a message never repeats the value


def test_item_limits_and_retired_items(portal_user, pickup, catalog):
    box = catalog.items.get(name="Shipping box")
    retired = catalog.items.get(name="Retired item")
    with pytest.raises(ValidationError):
        portal_raise(portal_user, pickup, {"count": 1, "items": {box.pk: 6}})
    with pytest.raises(ValidationError):
        portal_raise(portal_user, pickup, {"count": 1, "items": {retired.pk: 1}})
    request = portal_raise(portal_user, pickup, {"count": 1, "items": {box.pk: 5}})
    assert current(request)["items"].value[0]["quantity"] == 5


def test_date_and_time_are_kept_as_laboratory_wall_clock(portal_user, pickup, settings):
    from django.utils import timezone

    moment = timezone.make_aware(dt.datetime(2026, 11, 20, 14, 30))
    request = portal_raise(portal_user, pickup, {"count": 1, "ready-at": moment})
    assert current(request)["ready-at"].value == "2026-11-20 14:30"


# What each audience sees -------------------------------------------------------------------


def test_client_sees_client_and_shown_values_only(portal_user, colleague, agent, pickup):
    request = portal_raise(portal_user, pickup, {"count": 2, "patient": PATIENT_SENTINEL})
    correct_values(
        actor=agent,
        request=request,
        expected_definition=definition_of(request),
        values={"courier": "Route 4", "tracking": "TRK-TEST-1"},
        reason="",
        expected={"courier": None, "tracking": None},
    )
    labels = {row.label: row.lines for row in client_values(request, portal_user).rows}
    assert labels == {
        "Number of coolers": ["2"],
        "Tracking": ["TRK-TEST-1"],
        "Patient identifier": [PATIENT_SENTINEL],  # the client who may see the request
    }
    assert {row.label for row in client_values(request, colleague).rows} == set(labels)


def test_other_organization_and_staff_scope(portal_user, outsider, make_user, pickup):
    request = portal_raise(portal_user, pickup, {"count": 2})
    assert client_values(request, outsider) is None
    assert staff_values(request, outsider) is None


def test_restricted_request_hidden_from_non_member(agent, capable, pickup, pickup_category):
    pickup_category.is_restricted = True
    pickup_category.save()
    request = staff_raise(capable, pickup, {"count": 1})
    assert staff_values(request, agent) is None
    assert staff_values(request, capable) is not None


def test_patient_value_placeholder_for_staff_without_capability(
    portal_user, agent, capable, pickup
):
    request = portal_raise(portal_user, pickup, {"count": 1, "patient": PATIENT_SENTINEL})
    hidden = {row.label: row for row in staff_values(request, agent).rows}["Patient identifier"]
    assert hidden.hidden and hidden.lines == []
    shown = {row.label: row for row in staff_values(request, capable).rows}["Patient identifier"]
    assert shown.lines == [] and shown.reveal_id == current(request)["patient"].pk


def test_old_request_keeps_its_labels_when_the_form_changes(portal_user, agent, pickup):
    request = portal_raise(portal_user, pickup, {"count": 2, "handling": "Frozen"})
    count = pickup.fields.get(key="count")
    count.label = "Coolers ready"
    count.position = 999
    count.save()
    handling = pickup.fields.get(key="handling")
    handling.is_active = False
    handling.save()
    pickup.name, pickup.client_label = "Courier visit", "Book a courier"
    pickup.save()
    details = staff_values(request, agent)
    assert details.template_label == "Pickup"
    rows = {row.label: row for row in details.rows}
    assert rows["Number of coolers"].lines == ["2"]
    assert rows["Handling"].retired and rows["Handling"].lines == ["Frozen"]
    assert client_values(request, portal_user).template_label == "Request a pickup"


def test_visibility_is_the_stricter_of_capture_and_now(portal_user, agent, pickup):
    request = portal_raise(portal_user, pickup, {"count": 2})
    count = pickup.fields.get(key="count")
    count.audience = Audience.STAFF
    count.holds_patient_information = True
    count.save()
    assert client_values(request, portal_user).rows == []
    row = staff_values(request, agent).rows[0]
    assert row.hidden and row.staff_only

    staff_only = staff_raise(
        agent, pickup, {"courier": "Route 9"}, details_not_known=True, client=portal_user.client
    )
    courier = pickup.fields.get(key="courier")
    courier.audience = Audience.SHOWN  # a later change never exposes what was staff-only
    courier.save()
    assert client_values(staff_only, portal_user).rows == []


def test_menu_lists_featured_forms_the_portal_may_use(portal_user, agent, pickup, staff_template):
    assert list(portal_menu(portal_user)) == [pickup]
    pickup.category.client_may_raise = False
    pickup.category.save()
    assert list(portal_menu(portal_user)) == []
    assert list(portal_menu(agent)) == []


# Corrections ---------------------------------------------------------------------------------


def test_correction_adds_a_value_and_keeps_the_old_one(portal_user, agent, pickup):
    request = portal_raise(portal_user, pickup, {"count": 2})
    old = current(request)["count"]
    with pytest.raises(ValidationError) as caught:
        correct_values(
            actor=agent,
            request=request,
            expected_definition=definition_of(request),
            values={"count": 3},
            reason="",
            expected={"count": old.pk},
        )
    assert "reason" in caught.value.message_dict
    changed = correct_values(
        actor=agent,
        request=request,
        expected_definition=definition_of(request),
        values={"count": 3},
        reason="Client called back",
        expected={"count": old.pk},
    )
    assert changed == ["count"]
    new = current(request)["count"]
    assert (new.value, new.replaces_id, new.reason) == ("3", old.pk, "Client called back")
    old.refresh_from_db()
    assert old.superseded_at is not None and old.value == "2"
    event = RequestEntry.objects.get(request=request, event_action="form_values_changed")
    assert event.event_data == {"fields": ["count"]}
    audit = AuditEvent.objects.get(action="request_form.values_changed")
    assert audit.before == {"value_ids": [old.pk]}
    assert audit.after["value_ids"] == [new.pk]


def test_filling_an_empty_field_needs_no_reason(portal_user, agent, pickup):
    request = portal_raise(portal_user, pickup, {"count": 2})
    correct_values(
        actor=agent,
        request=request,
        expected_definition=definition_of(request),
        values={"courier": "Route 4"},
        reason="",
        expected={"courier": None},
    )
    assert current(request)["courier"].value == "Route 4"


def test_stale_correction_is_refused(portal_user, agent, pickup):
    request = portal_raise(portal_user, pickup, {"count": 2})
    seen = current(request)["count"].pk
    correct_values(
        actor=agent,
        request=request,
        expected_definition=definition_of(request),
        values={"count": 4},
        reason="First",
        expected={"count": seen},
    )
    with pytest.raises(ValidationError):
        correct_values(
            actor=agent,
            request=request,
            expected_definition=definition_of(request),
            values={"count": 5},
            reason="Second",
            expected={"count": seen},
        )
    assert current(request)["count"].value == "4"


def test_correction_of_patient_value_needs_the_capability(portal_user, agent, capable, pickup):
    request = portal_raise(portal_user, pickup, {"count": 1, "patient": PATIENT_SENTINEL})
    seen = current(request)["patient"].pk
    with pytest.raises(PermissionDenied):
        correct_values(
            actor=agent,
            request=request,
            expected_definition=definition_of(request),
            values={"patient": "PT-OTHER"},
            reason="Typo",
            expected={"patient": seen},
        )
    with pytest.raises(ValidationError) as caught:  # free text alone is not enough here
        correct_values(
            actor=capable,
            request=request,
            expected_definition=definition_of(request),
            values={"patient": "PT-TEST-2"},
            reason="Typo",
            expected={"patient": seen},
        )
    assert "reason_code" in caught.value.message_dict
    correct_values(
        actor=capable,
        request=request,
        expected_definition=definition_of(request),
        values={"patient": "PT-TEST-2"},
        reason="Typo",
        expected={"patient": seen},
        reason_code="typing",
    )


def test_clients_cannot_correct(portal_user, pickup):
    request = portal_raise(portal_user, pickup, {"count": 1})
    with pytest.raises(PermissionDenied):
        correct_values(
            actor=portal_user,
            request=request,
            expected_definition=definition_of(request),
            values={"count": 2},
            reason="x",
            expected={"count": current(request)["count"].pk},
        )


def test_values_are_history_in_the_database(portal_user, pickup):
    request = portal_raise(portal_user, pickup, {"count": 1})
    row = current(request)["count"]
    with pytest.raises(DatabaseError), transaction.atomic():
        FieldValue.objects.filter(pk=row.pk).update(value="9")
    with pytest.raises(DatabaseError), transaction.atomic():
        FieldValue.objects.filter(pk=row.pk).delete()
    with pytest.raises(DatabaseError), transaction.atomic():
        FilledForm.objects.filter(request=request).update(template_name="Changed")


# Patient information stays on the request page --------------------------------------------------


def test_patient_value_reaches_no_audit_entry_subject_mail_or_log(
    portal_user, capable, pickup, caplog, django_capture_on_commit_callbacks
):
    caplog.set_level(logging.DEBUG)
    with django_capture_on_commit_callbacks(execute=True):
        request = portal_raise(portal_user, pickup, {"count": 1, "patient": PATIENT_SENTINEL})
        correct_values(
            actor=capable,
            request=request,
            expected_definition=definition_of(request),
            values={"patient": PATIENT_SENTINEL + "-2"},
            reason=f"It was {PATIENT_SENTINEL}, now corrected",
            reason_code="source_record",
            expected={"patient": current(request)["patient"].pk},
        )
    for event in AuditEvent.objects.all():
        payload = json.dumps([event.before, event.after, event.reason])
        assert PATIENT_SENTINEL not in payload, event.action
    for entry in RequestEntry.objects.all():
        assert PATIENT_SENTINEL not in entry.body
        assert PATIENT_SENTINEL not in json.dumps(entry.event_data)
    assert PATIENT_SENTINEL not in ServiceRequest.objects.get().subject
    for message in mail.outbox:
        assert PATIENT_SENTINEL not in message.subject + message.body
    assert PATIENT_SENTINEL not in caplog.text


def test_staff_only_patient_value_never_reaches_a_client(portal_user, capable, pickup):
    pickup.fields.create(
        key="staff-patient",
        label="Staff patient note",
        kind="short_text",
        audience=Audience.STAFF,
        holds_patient_information=True,
        position=999,
    )
    request = staff_raise(
        capable,
        pickup,
        {"count": 1, "staff-patient": PATIENT_SENTINEL},
        client=portal_user.client,
    )
    rows = client_values(request, portal_user).rows
    assert [row.label for row in rows] == ["Number of coolers"]
    assert all(PATIENT_SENTINEL not in line for row in rows for line in row.lines)


# Visibility and access to recorded values


def test_patient_correction_keeps_free_text_out_of_the_history(portal_user, capable, pickup):
    request = portal_raise(portal_user, pickup, {"count": 1, "patient": "PT-TEST-1"})
    rows = current(request)
    correct_values(
        actor=capable,
        request=request,
        expected_definition=definition_of(request),
        values={"patient": "PT-TEST-2", "count": 2},
        reason="Recounted at pickup",
        reason_code="other_person",
        patient_note=f"Old one was {PATIENT_SENTINEL}",
        expected={"patient": rows["patient"].pk, "count": rows["count"].pk},
    )
    event = AuditEvent.objects.get(action="request_form.values_changed")
    assert event.reason == CORRECTION_AUDIT_REASON  # no typed text in the audit history
    assert event.after["reason_code"] == "other_person"
    assert PATIENT_SENTINEL not in json.dumps([event.before, event.after, event.reason])
    assert "Recounted" not in event.reason
    entry = RequestEntry.objects.get(event_action="form_values_changed")
    assert PATIENT_SENTINEL not in json.dumps(entry.event_data)
    now = current(request)
    assert now["patient"].reason == f"Old one was {PATIENT_SENTINEL}"  # behind the capability
    assert now["patient"].reason_code == "other_person"
    assert now["count"].reason == "Recounted at pickup"  # the ordinary reason, never the note
    assert now["count"].reason_code == ""


def test_hidden_row_is_only_a_placeholder(portal_user, agent, capable, pickup):
    request = portal_raise(portal_user, pickup, {"count": 1, "patient": "PT-TEST-1"})
    correct_values(
        actor=capable,
        request=request,
        expected_definition=definition_of(request),
        values={"patient": "PT-TEST-2"},
        reason="",
        reason_code="typing",
        expected={"patient": current(request)["patient"].pk},
    )
    row = {r.label: r for r in staff_values(request, agent).rows}["Patient identifier"]
    assert row.hidden and not row.corrected and row.lines == [] and row.reveal_id is None


def test_superseded_value_is_not_revealable(portal_user, capable, pickup):
    from request_forms.selectors import revealable_value

    request = portal_raise(portal_user, pickup, {"count": 1, "patient": "PT-TEST-1"})
    old = current(request)["patient"]
    correct_values(
        actor=capable,
        request=request,
        expected_definition=definition_of(request),
        values={"patient": "PT-TEST-2"},
        reason="",
        reason_code="typing",
        expected={"patient": old.pk},
    )
    assert revealable_value(capable, old.pk) is None
    assert revealable_value(capable, current(request)["patient"].pk) is not None


def test_first_fill_is_checked_after_the_lock(portal_user, agent, pickup):
    request = portal_raise(portal_user, pickup, {"count": 1})
    correct_values(
        actor=agent,
        request=request,
        expected_definition=definition_of(request),
        values={"courier": "Route 1"},
        reason="",
        expected={"courier": None},
    )
    with pytest.raises(ValidationError):  # someone filled it after this person saw it empty
        correct_values(
            actor=agent,
            request=request,
            expected_definition=definition_of(request),
            values={"courier": "Route 2"},
            reason="",
            expected={"courier": None},
        )


def test_capability_alone_reads_nothing_after_losing_the_request(
    portal_user, capable, pickup, pickup_category, logistics
):
    from django.contrib.auth.models import Group

    from request_forms.selectors import revealable_value
    from teams.models import TeamMembership

    capable.groups.set([Group.objects.get_or_create(name="technical")[0]])
    TeamMembership.objects.create(team=logistics, user=capable)
    pickup_category.is_restricted = True
    pickup_category.save()
    request = portal_raise(portal_user, pickup, {"count": 1, "patient": "PT-TEST-1"})
    value = current(request)["patient"]
    assert revealable_value(capable, value.pk) is not None
    TeamMembership.objects.filter(user=capable).delete()  # left the team
    assert revealable_value(capable, value.pk) is None
    assert staff_values(request, capable) is None
