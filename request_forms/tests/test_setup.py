"""Setup of request forms (F1, F3) and the standard forms (F7): admin only, audited with reasons,
stale edits refused, and the rules that protect requests already raised."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from core.models import AuditEvent
from desk.models import EntryKind
from request_forms import presets
from request_forms.models import Catalog, FieldKind, RequestTemplate, TemplateField
from request_forms.services import (
    create_field,
    create_request_with_form,
    create_template,
    move_field,
    update_field,
    update_template,
)
from taxonomy.models import RequestCategory

from .conftest import definition_of

pytestmark = pytest.mark.django_db


def test_only_admins_set_up_forms(agent, capable, pickup_category):
    for user in (agent, capable):
        with pytest.raises(PermissionDenied):
            create_template(actor=user, key="x", name="X", category=pickup_category)


def test_template_rules(admin, group, pickup_category, staff_category):
    with pytest.raises(ValidationError):  # a group is not a category
        create_template(actor=admin, key="g", name="G", category=group)
    with pytest.raises(ValidationError) as caught:
        create_template(
            actor=admin, key="s", name="S", category=staff_category, offered_in_portal=True
        )
    assert "offered_in_portal" in caught.value.message_dict
    with pytest.raises(ValidationError):
        create_template(
            actor=admin,
            key="f",
            name="F",
            category=pickup_category,
            offered_in_portal=False,
            featured=True,
        )
    template = create_template(actor=admin, key="p", name="P", category=pickup_category)
    event = AuditEvent.objects.get(action="request_form.created")
    assert event.object_id == str(template.pk)


def test_edit_needs_reason_refuses_stale_and_no_change(admin, pickup):
    with pytest.raises(ValidationError):
        update_template(
            actor=admin,
            template_id=pickup.pk,
            expected_revision=pickup.revision,
            reason="",
            name="Courier",
        )
    with pytest.raises(ValidationError):
        update_template(
            actor=admin,
            template_id=pickup.pk,
            expected_revision=pickup.revision + 5,
            reason="Rename",
            name="Courier",
        )
    with pytest.raises(ValidationError):
        update_template(
            actor=admin,
            template_id=pickup.pk,
            expected_revision=pickup.revision,
            reason="Nothing",
            name=pickup.name,
        )
    updated = update_template(
        actor=admin,
        template_id=pickup.pk,
        expected_revision=pickup.revision,
        reason="Clients call it a courier",
        name="Courier",
    )
    assert updated.revision == pickup.revision + 1
    event = AuditEvent.objects.get(action="request_form.updated")
    assert (event.before["name"], event.after["name"], event.reason) == (
        "Pickup",
        "Courier",
        "Clients call it a courier",
    )


def test_field_rules(admin, pickup, catalog):
    with pytest.raises(ValidationError):  # one choice needs two options
        create_field(
            actor=admin,
            template_id=pickup.pk,
            key="c",
            label="C",
            kind=FieldKind.CHOICE,
            choices=["Only"],
        )
    with pytest.raises(ValidationError):  # an item list needs a catalog
        create_field(actor=admin, template_id=pickup.pk, key="i", label="I", kind=FieldKind.ITEMS)
    with pytest.raises(ValidationError):  # keys are unique within the form
        create_field(
            actor=admin,
            template_id=pickup.pk,
            key="count",
            label="Again",
            kind=FieldKind.SHORT_TEXT,
        )
    confirmation = create_field(
        actor=admin,
        template_id=pickup.pk,
        key="labels",
        label="Labeled",
        kind=FieldKind.CONFIRMATION,
        required=False,
        choices=["stray"],
    )
    assert confirmation.required is True and confirmation.choices == []


def test_type_and_patient_mark_stay_once_requests_hold_values(admin, portal_user, pickup):
    create_request_with_form(
        actor=portal_user,
        template=pickup,
        expected_definition=definition_of(pickup),
        values={"count": 1, "patient": "PT-TEST-1"},
        first_entry_kind=EntryKind.REPLY,
        channel="portal",
    )
    count = pickup.fields.get(key="count")
    with pytest.raises(ValidationError) as caught:
        update_field(
            actor=admin,
            field_id=count.pk,
            expected_revision=count.revision,
            reason="Make it text",
            kind=FieldKind.SHORT_TEXT,
        )
    assert "kind" in caught.value.message_dict
    patient = pickup.fields.get(key="patient")
    with pytest.raises(ValidationError) as caught:
        update_field(
            actor=admin,
            field_id=patient.pk,
            expected_revision=patient.revision,
            reason="Not needed",
            holds_patient_information=False,
        )
    assert "holds_patient_information" in caught.value.message_dict


def test_reorder_fields(admin, pickup):
    keys = list(pickup.fields.order_by("position").values_list("key", flat=True))
    move_field(actor=admin, field_id=pickup.fields.get(key=keys[1]).pk, direction="up")
    assert list(pickup.fields.order_by("position").values_list("key", flat=True))[:2] == [
        keys[1],
        keys[0],
    ]
    with pytest.raises(ValidationError):
        move_field(actor=admin, field_id=pickup.fields.get(key=keys[1]).pk, direction="up")


# Presets -------------------------------------------------------------------------------------


def test_no_preset_ships_an_active_patient_field():
    for preset in presets.FORM_PRESETS.values():
        fields = [f for t in preset.templates for f in t.fields]
        fields += [f for a in preset.additions for f in a.fields]
        for field in fields:
            assert not (field.patient and field.active), (preset.key, field.key)


def test_preset_icons_and_shapes_are_valid():
    from request_forms.models import ICONS

    icons = {name for name, _ in ICONS}
    keys = set()
    for preset in presets.FORM_PRESETS.values():
        catalogs = {c.key for p in presets.FORM_PRESETS.values() for c in p.catalogs}
        for template in preset.templates:
            assert template.icon in icons, template.key
            assert template.key not in keys or template in _shared(), template.key
            keys.add(template.key)
            for field in template.fields:
                if field.kind == FieldKind.ITEMS:
                    assert field.catalog in catalogs
                if field.kind == FieldKind.CHOICE:
                    assert len(field.choices) >= 2


def _shared():
    templates = [t for p in presets.FORM_PRESETS.values() for t in p.templates]
    return [t for t in templates if sum(t is other for other in templates) > 1]


def _categories(*keys, raisable=True):
    group = RequestCategory.objects.create(key="preset-group", name="Preset group")
    for key in keys:
        RequestCategory.objects.create(
            key=key,
            name=key.replace("-", " ").capitalize(),
            parent=group,
            client_may_raise=raisable,
        )


def test_presets_load_additively_and_idempotently(admin):
    all_keys = {t.category for t in presets.ALL.templates}
    _categories(*sorted(all_keys - {"complaint"}))
    first = presets.load_form_presets(actor=admin, keys=["all", "clinical"])
    expected = {t.key for t in presets.ALL.templates} - {"complaint"}
    assert expected <= set(RequestTemplate.objects.values_list("key", flat=True))
    assert first["skipped"] >= 1  # complaint, and clinical forms without their categories
    assert not RequestTemplate.objects.filter(key="complaint").exists()
    patient_fields = TemplateField.objects.filter(holds_patient_information=True)
    assert patient_fields.exists() and not patient_fields.filter(is_active=True).exists()
    assert Catalog.objects.filter(key="supplies").exists()
    counts = AuditEvent.objects.count()
    second = presets.load_form_presets(actor=admin, keys=["all", "clinical"])
    assert {k: v for k, v in second.items() if "skipped" not in k} == dict.fromkeys(
        ("catalogs", "items", "templates", "fields"), 0
    )
    assert AuditEvent.objects.count() == counts


def test_preset_keeps_lab_changes_and_respects_client_rules(admin):
    _categories("pickup", "supply-order", raisable=False)
    presets.load_form_presets(actor=admin, keys=["all"])
    pickup = RequestTemplate.objects.get(key="pickup")
    assert (pickup.offered_in_portal, pickup.featured) == (False, False)
    pickup.name = "Courier"
    pickup.save()
    field = pickup.fields.first()
    field.is_active = False
    field.save()
    presets.load_form_presets(actor=admin, keys=["all"])
    pickup.refresh_from_db()
    field.refresh_from_db()
    assert pickup.name == "Courier" and field.is_active is False


def test_presets_need_admin(agent):
    with pytest.raises(PermissionDenied):
        presets.load_form_presets(actor=agent, keys=["all"])


def test_category_presets_bring_their_standard_forms(admin):
    from taxonomy.services import load_presets

    load_presets(actor=admin, keys=["environmental"])
    keys = set(RequestTemplate.objects.values_list("key", flat=True))
    assert {"pickup", "supply-order", "bottle-kit-order", "coc-correction"} <= keys
    assert "urgent-pickup" not in keys  # clinical forms only with the clinical preset
