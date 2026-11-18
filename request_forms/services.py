"""Changes to request forms.

Setup (admin role): templates, their fields, catalogs and their items. Each change locks the row,
rechecks its revision, requires a reason on an edit, and records an attributed audit event in the
same transaction (the worked example is clients.services).

Requests: `create_request_with_form` raises a request with a template's values in one
transaction; `correct_values` lets staff fill or correct them later. Values are checked here,
at the boundary, against the template's current fields and the actor: a portal account may
give only client fields, staff without `view_patient_information` no patient-information
field, and nobody a field of another template. Audit payloads and timeline events carry
template and field keys and value row ids, never values.
"""

import json

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.db.models import F, Max
from django.forms.models import model_to_dict
from django.utils import timezone

from accounts.roles import require_role, require_staff
from core.audit import record_event, snapshot

from . import definition, kinds
from .models import (
    CHOICE_KINDS,
    NO_VALUE_KINDS,
    Catalog,
    CatalogItem,
    FieldKind,
    FieldValue,
    FilledForm,
    PatientReason,
    RequestTemplate,
    TemplateField,
)
from .selectors import (
    is_portal_account,
    may_see_patient_information,
)

SETUP_ROLE = "admin"
DETAILS_IN_FORM = "Details are in the form “{label}”."
STALE = "This changed after you opened it. Reload and review the current record."
TEMPLATE_FIELDS = (
    "name",
    "client_label",
    "description",
    "instructions",
    "icon",
    "category",
    "offered_in_portal",
    "featured",
    "is_active",
)
FIELD_FIELDS = (
    "label",
    "help_text",
    "kind",
    "required",
    "choices",
    "whole_number",
    "min_value",
    "max_value",
    "catalog",
    "audience",
    "holds_patient_information",
    "copy_on_repeat",
    "is_active",
)
CATALOG_FIELDS = ("name", "description", "is_active")
ITEM_FIELDS = ("code", "name", "unit", "search_words", "max_quantity", "is_active")


def require_setup(user):
    require_role(user, SETUP_ROLE)


def _reason(reason):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required."})
    return reason


def _strip(obj, names):
    for name in names:
        value = getattr(obj, name)
        if isinstance(value, str):
            setattr(obj, name, value.strip())


def _save(obj, *, unique_message):
    try:
        with transaction.atomic():
            obj.full_clean(exclude=["revision", "position"], validate_constraints=True)
            obj.save()
    except IntegrityError:
        raise ValidationError(unique_message) from None


def _locked(model, pk, expected_revision):
    obj = model.objects.select_for_update().get(pk=pk)
    if expected_revision is not None and obj.revision != expected_revision:
        raise ValidationError(STALE)
    return obj


def _template_changed(template_id):
    """A field was added or changed: the template's revision moves on, so a request form opened
    before (portal or staff intake) is refused at submission rather than read under the new
    fields. Called with the template locked."""
    RequestTemplate.objects.filter(pk=template_id).update(revision=F("revision") + 1)


def _next_position(queryset):
    return (queryset.aggregate(top=Max("position"))["top"] or 0) + 10


def _move(actor, model, obj, siblings, direction, action):
    if direction not in ("up", "down"):
        raise ValidationError("Move it up or down.")
    # Among the rows of the same state: a list that hides inactive rows moves visibly.
    siblings = list(
        siblings.filter(is_active=obj.is_active).select_for_update().order_by("position", "pk")
    )
    index = next(i for i, sibling in enumerate(siblings) if sibling.pk == obj.pk)
    other = index - 1 if direction == "up" else index + 1
    if not 0 <= other < len(siblings):
        raise ValidationError(f"This is already the {'first' if direction == 'up' else 'last'}.")
    siblings[index], siblings[other] = siblings[other], siblings[index]
    for position, sibling in enumerate(siblings, start=1):
        if sibling.position != position * 10:
            model.objects.filter(pk=sibling.pk).update(position=position * 10)
    record_event(
        actor=actor,
        action=action,
        obj=obj,
        after={"direction": direction, "order": [sibling.pk for sibling in siblings]},
    )


# Templates -----------------------------------------------------------------------------------


def _check_template(obj):
    category = obj.category
    if category.parent_id is None:
        raise ValidationError({"category": "Choose a category, not a group."})
    if obj.is_active and not category.is_active:
        raise ValidationError({"category": "This category is inactive; choose an active one."})
    if obj.featured and not obj.offered_in_portal:
        raise ValidationError(
            {"featured": "Only a form offered in the portal can be on the portal menu."}
        )
    if obj.offered_in_portal and not category.client_may_raise:
        raise ValidationError(
            {
                "offered_in_portal": "Clients may not raise tickets in this category. Allow it "
                "in the category's setup, or keep this form for staff."
            }
        )


def template_state(obj, client_ids=None):
    """A template's audit snapshot: its own fields and the ids of the organizations it is
    offered to (model_to_dict cannot serialize the many-to-many)."""
    data = json.loads(json.dumps(model_to_dict(obj, exclude=["clients"]), cls=DjangoJSONEncoder))
    if client_ids is None:
        client_ids = obj.clients.values_list("pk", flat=True) if obj.pk else []
    data["clients"] = sorted(client_ids)
    return data


def _client_ids(clients):
    return sorted({getattr(client, "pk", client) for client in clients or ()})


@transaction.atomic
def create_template(*, actor, key, reason="", clients=(), **data):
    require_setup(actor)
    obj = RequestTemplate(key=key, **{k: v for k, v in data.items() if k in TEMPLATE_FIELDS})
    _strip(obj, ("name", "client_label", "description", "instructions"))
    _check_template(obj)
    obj.position = _next_position(RequestTemplate.objects.all())
    _save(obj, unique_message={"key": "A form with this key or name exists."})
    obj.clients.set(_client_ids(clients))
    record_event(
        actor=actor,
        action="request_form.created",
        obj=obj,
        after=template_state(obj),
        reason=(reason or "").strip(),
    )
    return obj


@transaction.atomic
def update_template(*, actor, template_id, expected_revision, reason, clients=None, **data):
    """`clients` None leaves the organizations as they are; a list (empty: every
    organization) replaces them."""
    require_setup(actor)
    reason = _reason(reason)
    obj = _locked(RequestTemplate, template_id, expected_revision)
    before = template_state(obj)
    for name in TEMPLATE_FIELDS:
        if name in data:
            setattr(obj, name, data[name])
    _strip(obj, ("name", "client_label", "description", "instructions"))
    _check_template(obj)
    client_ids = before["clients"] if clients is None else _client_ids(clients)
    if template_state(obj, client_ids) == before:
        raise ValidationError("Nothing changed.")
    obj.revision += 1
    _save(obj, unique_message={"name": "A form with this name exists."})
    obj.clients.set(client_ids)
    record_event(
        actor=actor,
        action="request_form.updated",
        obj=obj,
        before=before,
        after=template_state(obj),
        reason=reason,
    )
    return obj


@transaction.atomic
def clone_template(*, actor, template_id, key, name, reason=""):
    """Copy a form with all its fields (inactive ones too, keys kept) under a new key and name.
    The copy is active and offered like the original, but not on the service menu; its requests
    and history are its own."""
    require_setup(actor)
    source = RequestTemplate.objects.select_for_update().get(pk=template_id)
    copy = RequestTemplate(
        key=key,
        name=(name or "").strip(),
        client_label="",
        description=source.description,
        instructions=source.instructions,
        icon=source.icon,
        category=source.category,
        offered_in_portal=source.offered_in_portal and source.category.client_may_raise,
        featured=False,
        position=_next_position(RequestTemplate.objects.all()),
    )
    _save(copy, unique_message={"key": "A form with this key or name exists."})
    copy.clients.set(source.clients.values_list("pk", flat=True))
    for field in source.fields.order_by("position", "pk"):
        field.pk = None
        field.template = copy
        field.revision = 1
        field.save(force_insert=True)
        record_event(
            actor=actor,
            action="request_form_field.created",
            obj=field,
            after=snapshot(field),
            reason=f"Copied with the form “{source.name}”.",
        )
    record_event(
        actor=actor,
        action="request_form.created",
        obj=copy,
        after={**template_state(copy), "copied_from": source.key},
        reason=(reason or "").strip() or f"Copied from “{source.name}”.",
    )
    return copy


@transaction.atomic
def move_template(*, actor, template_id, direction):
    require_setup(actor)
    obj = RequestTemplate.objects.select_for_update().get(pk=template_id)
    _move(
        actor,
        RequestTemplate,
        obj,
        RequestTemplate.objects.all(),
        direction,
        "request_form.reordered",
    )
    return obj


# Fields --------------------------------------------------------------------------------------


def _clean_choices(choices):
    out, seen = [], set()
    for choice in choices or []:
        choice = (choice or "").strip()
        if choice and choice.casefold() not in seen:
            seen.add(choice.casefold())
            out.append(choice)
    return out


def _check_field(obj):
    kind = obj.kind
    if kind in NO_VALUE_KINDS:  # a heading asks nothing
        obj.required = obj.holds_patient_information = obj.copy_on_repeat = False
    obj.choices = _clean_choices(obj.choices) if kind in CHOICE_KINDS else []
    if kind == FieldKind.CHOICE and len(obj.choices) < 2:
        raise ValidationError({"choices": "Give at least two options, one per line."})
    if kind == FieldKind.MULTIPLE_CHOICE and not obj.choices:
        raise ValidationError({"choices": "Give the options, one per line."})
    if any(len(choice) > 120 for choice in obj.choices):
        raise ValidationError({"choices": "Keep each option to 120 characters."})
    if kind == FieldKind.ITEMS:
        if obj.catalog is None:
            raise ValidationError({"catalog": "Choose the catalog clients pick items from."})
        if obj.is_active and not obj.catalog.is_active:
            raise ValidationError({"catalog": "This catalog is inactive."})
    else:
        obj.catalog = None
    if kind != FieldKind.NUMBER:
        obj.min_value = obj.max_value = None
        obj.whole_number = True
    if kind == FieldKind.CONFIRMATION:
        obj.required = True  # a confirmation exists to be ticked
    if obj.min_value is not None and obj.max_value is not None and obj.min_value > obj.max_value:
        raise ValidationError({"max_value": "The highest value must not be below the lowest."})


@transaction.atomic
def create_field(*, actor, template_id, key, reason="", **data):
    require_setup(actor)
    template = RequestTemplate.objects.select_for_update().get(pk=template_id)
    obj = TemplateField(
        template=template, key=key, **{k: v for k, v in data.items() if k in FIELD_FIELDS}
    )
    _strip(obj, ("label", "help_text"))
    _check_field(obj)
    obj.position = _next_position(template.fields.all())
    _save(obj, unique_message={"key": "This form already has a field with this key."})
    _template_changed(template.pk)
    record_event(
        actor=actor,
        action="request_form_field.created",
        obj=obj,
        after=snapshot(obj),
        reason=(reason or "").strip(),
    )
    return obj


@transaction.atomic
def update_field(*, actor, field_id, expected_revision, reason, **data):
    require_setup(actor)
    reason = _reason(reason)
    template_id = TemplateField.objects.values_list("template_id", flat=True).get(pk=field_id)
    RequestTemplate.objects.select_for_update().get(pk=template_id)  # template, then field
    obj = _locked(TemplateField, field_id, expected_revision)
    before = snapshot(obj)
    old_kind = obj.kind
    for name in FIELD_FIELDS:
        if name in data:
            setattr(obj, name, data[name])
    _strip(obj, ("label", "help_text"))
    if obj.kind != old_kind and obj.values.exists():
        raise ValidationError(
            {
                "kind": "Tickets already hold values of this field, so its type stays. Add a "
                "new field and make this one inactive."
            }
        )
    if (
        before["holds_patient_information"]
        and not obj.holds_patient_information
        and obj.values.exists()
    ):
        raise ValidationError(
            {
                "holds_patient_information": "Tickets already hold values of this field as "
                "patient information, so the mark stays. Add a new field if the laboratory "
                "decided it no longer needs patient information."
            }
        )
    _check_field(obj)
    after = snapshot(obj)
    if after == before:
        raise ValidationError("Nothing changed.")
    obj.revision += 1
    _save(obj, unique_message="This form already has a field with this key.")
    _template_changed(template_id)
    record_event(
        actor=actor,
        action="request_form_field.updated",
        obj=obj,
        before=before,
        after=snapshot(obj),
        reason=reason,
    )
    return obj


@transaction.atomic
def move_field(*, actor, field_id, direction):
    require_setup(actor)
    obj = TemplateField.objects.select_for_update().get(pk=field_id)
    _move(
        actor,
        TemplateField,
        obj,
        TemplateField.objects.filter(template_id=obj.template_id),
        direction,
        "request_form_field.reordered",
    )
    return obj


# Catalogs ------------------------------------------------------------------------------------


@transaction.atomic
def create_catalog(*, actor, key, reason="", **data):
    require_setup(actor)
    obj = Catalog(key=key, **{k: v for k, v in data.items() if k in CATALOG_FIELDS})
    _strip(obj, ("name", "description"))
    _save(obj, unique_message={"key": "A catalog with this key or name exists."})
    record_event(
        actor=actor,
        action="catalog.created",
        obj=obj,
        after=snapshot(obj),
        reason=(reason or "").strip(),
    )
    return obj


@transaction.atomic
def update_catalog(*, actor, catalog_id, expected_revision, reason, **data):
    require_setup(actor)
    reason = _reason(reason)
    obj = _locked(Catalog, catalog_id, expected_revision)
    before = snapshot(obj)
    for name in CATALOG_FIELDS:
        if name in data:
            setattr(obj, name, data[name])
    _strip(obj, ("name", "description"))
    if snapshot(obj) == before:
        raise ValidationError("Nothing changed.")
    obj.revision += 1
    _save(obj, unique_message={"name": "A catalog with this name exists."})
    record_event(
        actor=actor,
        action="catalog.updated",
        obj=obj,
        before=before,
        after=snapshot(obj),
        reason=reason,
    )
    return obj


@transaction.atomic
def create_item(*, actor, catalog_id, reason="", **data):
    require_setup(actor)
    catalog = Catalog.objects.select_for_update().get(pk=catalog_id)
    obj = CatalogItem(catalog=catalog, **{k: v for k, v in data.items() if k in ITEM_FIELDS})
    _strip(obj, ("code", "name", "unit", "search_words"))
    obj.position = _next_position(catalog.items.all())
    _save(obj, unique_message={"code": "This catalog already has an item with this code."})
    record_event(
        actor=actor,
        action="catalog_item.created",
        obj=obj,
        after=snapshot(obj),
        reason=(reason or "").strip(),
    )
    return obj


@transaction.atomic
def update_item(*, actor, item_id, expected_revision, reason, **data):
    require_setup(actor)
    reason = _reason(reason)
    catalog_id = CatalogItem.objects.values_list("catalog_id", flat=True).get(pk=item_id)
    Catalog.objects.select_for_update().get(pk=catalog_id)  # catalog, then item (definition.lock)
    obj = _locked(CatalogItem, item_id, expected_revision)
    before = snapshot(obj)
    for name in ITEM_FIELDS:
        if name in data:
            setattr(obj, name, data[name])
    _strip(obj, ("code", "name", "unit", "search_words"))
    if snapshot(obj) == before:
        raise ValidationError("Nothing changed.")
    obj.revision += 1
    _save(obj, unique_message={"code": "This catalog already has an item with this code."})
    record_event(
        actor=actor,
        action="catalog_item.updated",
        obj=obj,
        before=before,
        after=snapshot(obj),
        reason=reason,
    )
    return obj


@transaction.atomic
def move_item(*, actor, item_id, direction):
    require_setup(actor)
    obj = CatalogItem.objects.select_for_update().get(pk=item_id)
    _move(
        actor,
        CatalogItem,
        obj,
        CatalogItem.objects.filter(catalog_id=obj.catalog_id),
        direction,
        "catalog_item.reordered",
    )
    return obj


# Values on requests --------------------------------------------------------------------------


def _items_for(defn, fields, retained=None):
    """The catalog items each item-list field takes, by field id then item id, from the
    definition: active items of an active catalog, plus, on a correction, the items the request
    already holds (`retained`, by field id)."""
    retained = retained or {}
    return {
        template_field.pk: {
            item.pk: item
            for item in defn.orderable_items(template_field, retained.get(template_field.pk))
        }
        for template_field in fields
        if template_field.kind == FieldKind.ITEMS
    }


def _check_keys(values, allowed, defn):
    """Refuse a key the actor may not fill on this form. Says which key, never its value."""
    unknown = sorted(set(values) - set(allowed))
    if not unknown:
        return
    if set(unknown) <= {template_field.key for template_field in defn.fields}:
        raise PermissionDenied("A field of this form is not yours to fill.")
    raise ValidationError(f"This form has no field {unknown[0]!r}.")


RETIRED_CATALOG = "This list takes no new orders: its catalog is retired."


def _refuse_retired_catalogs(defn, values):
    """An item list whose catalog is retired is not on new requests: an order through it is
    refused (an empty one is ignored), whoever sends it."""
    for key in defn.retired_catalog_keys() & set(values):
        if values.pop(key):
            raise ValidationError({key: RETIRED_CATALOG})


HIDDEN_REQUIRED = (
    "This form has a required field for patient information, which only staff with “Can view "
    "patient information” fill. Tick “Some required details are not known yet”: the ticket "
    "page then shows it as still needed."
)


def validate_values(defn, actor, values, *, enforce_required=True, new_request=True, retained=None):
    """Check `values` (field key -> cleaned value) against the fields of the definition `defn`
    (request_forms.definition.read) this actor may fill. Returns [(field, JSON value or None)]
    in field order, and the keys of required fields left empty. Raises ValidationError keyed by
    field key. The fields returned are the definition's own objects, so what is stored is what
    was compared.

    On a new request, a required patient-information field that this staff account cannot fill
    counts as left empty: refused unless required details may stay unknown. `retained` (a
    correction) maps item-list field ids to the item ids the request already holds."""
    fields = defn.fillable(actor, new_request=new_request)
    by_key = {template_field.key: template_field for template_field in fields}
    values = dict(values or {})
    if new_request:
        _refuse_retired_catalogs(defn, values)
    _check_keys(values, by_key, defn)
    items = _items_for(defn, fields, retained)
    errors, out, missing = {}, [], []
    for template_field in fields:
        try:
            stored = kinds.normalize(
                template_field,
                values.get(template_field.key),
                items=items.get(template_field.pk),
            )
        except ValidationError as exc:
            errors[template_field.key] = exc.messages
            continue
        if stored is None and template_field.required:
            if enforce_required:
                errors[template_field.key] = (
                    ["Tick the box to confirm."]
                    if template_field.kind == FieldKind.CONFIRMATION
                    else ["This field is required."]
                )
                continue
            missing.append(template_field.key)
        out.append((template_field, stored))
    if new_request:
        hidden = [template_field.key for template_field in defn.hidden_required(actor)]
        if hidden and enforce_required:
            errors["details_not_known"] = [HIDDEN_REQUIRED]
        missing.extend(hidden)
    if errors:
        raise ValidationError(errors)
    return out, missing


def _copy(template_field):
    return {
        "field": template_field,
        "key": template_field.key,
        "label": template_field.label,
        "kind": template_field.kind,
        "audience": template_field.audience,
        "holds_patient_information": template_field.holds_patient_information,
        "position": template_field.position,
    }


FORM_RETIRED = "This form is no longer used, so nothing was saved. Choose a form again."
FORM_CHANGED = (
    "This form changed after the page was opened, so nothing was saved. Check the form as it is "
    "now and send it again."
)


NOT_OFFERED = (
    "This form is offered only to selected organizations, and the ticket's organization is not "
    "one of them, so nothing was saved. Choose another form, or the organization the form is for."
)


def offered_to(template, client):
    """Whether `template` may be used for a request of `client` (a Client, an id or None): a form
    offered to every organization (none listed) is; a form for selected organizations only for
    one of them, never for a request with no organization."""
    listed = template.clients.values_list("pk", flat=True)
    if not listed.exists():
        return True
    client_id = getattr(client, "pk", client)
    return client_id is not None and listed.filter(pk=client_id).exists()


def offered_in(defn, client):
    """offered_to, from a definition: `client` a Client, an id or None (a request with no
    organization takes only a form offered to every organization)."""
    client_id = getattr(client, "pk", client)
    return not defn.client_ids or (client_id is not None and client_id in defn.client_ids)


def offered_in_portal(defn, user):
    """selectors.portal_templates for one form, from a definition: a portal page checks it on
    the reading it draws, so it never shows a form the account is no longer offered."""
    template, category = defn.template, defn.category
    return bool(
        user.is_active
        and template.is_active
        and template.offered_in_portal
        and category.is_active
        and category.client_may_raise
        and category.parent_id
        and category.parent.is_active
        and offered_in(defn, user.client_id)
    )


def _correction_definition(filled, expected_definition, kept_rows):
    """The definition of a correction of `filled`, read once and locked for the caller's
    transaction (definition.read_for_correction, with the item rows the page drew), if it is the
    one the page was drawn from (`expected_definition`); FORM_CHANGED otherwise (reviews 5 to
    8). The comparison, the validation and the values written all use this one object."""
    defn = definition.read_for_correction(filled, rows=kept_rows, for_write=True)
    if not defn.matches(expected_definition):
        raise ValidationError(FORM_CHANGED, code="form_changed")
    return defn


def is_form_changed(exc):
    """Whether a ValidationError is the refusal of a form changed or retired since the page was
    drawn. The page then draws a fresh form from a new reading, with nothing that was typed,
    after checking the form is still offered to the account (reviews 6 and 7)."""
    return any(
        getattr(error, "code", None) in {"form_changed", "form_retired"}
        for error in getattr(exc, "error_list", None) or [exc]
    )


def _usable_definition(actor, template, details_not_known, expected_definition, locked=None):
    """The definition, locked, if this actor may raise a request with the form and it is still
    the one the page was drawn from: a retired or changed form is refused, never read under
    other fields, catalogs or category settings (reviews 3, 5 and 6). A portal account may use
    only the forms offered to it; staff, only for a request of an organization the form is
    offered to (`_check_offer`, once the request has its organization). `locked`: the reading
    staff intake took for writing before the request was created (lock_for_intake), used as it
    is: the locks it holds keep it current to the commit, so the form is read once."""
    portal = is_portal_account(actor)
    if portal and details_not_known:
        raise PermissionDenied("Clients fill every required field.")
    if not portal:
        require_staff(actor)
    if locked is None:
        defn = definition.read(template, for_write=True)
    elif locked.for_write and locked.template.pk == getattr(template, "pk", template):
        defn = locked
    else:
        raise RuntimeError("Values are stored from the form's definition read for writing.")
    if not defn.template.is_active:
        raise ValidationError(FORM_RETIRED, code="form_retired")
    if not defn.matches(expected_definition):
        raise ValidationError(FORM_CHANGED, code="form_changed")
    if portal and not offered_in_portal(defn, actor):
        raise PermissionDenied("This form is not offered to your organization.")
    return defn


def _check_offer(defn, request):
    """A form offered to selected organizations is used only for a request of one of them, as
    created (its organization may come from its site or contact)."""
    if not offered_in(defn, request.client_id):
        raise ValidationError(NOT_OFFERED, code="not_offered")


def _store(actor, request, defn, rows, missing, details_not_known):
    template = defn.template
    if request.category_id != template.category_id:
        raise ValidationError({"category": "This form belongs to another category."})
    filled = FilledForm.objects.create(
        request=request,
        template=template,
        template_key=template.key,
        template_name=template.name,
        template_client_label=template.portal_label,
        created_by=actor,
    )
    stored = [
        FieldValue(filled_form=filled, value=value, entered_by=actor, **_copy(template_field))
        for template_field, value in rows
        if value is not None
    ]
    FieldValue.objects.bulk_create(stored)
    record_event(
        actor=actor,
        action="request_form.filled",
        obj=filled,
        after={
            "request_id": request.pk,
            "template": template.key,
            "fields": [row.key for row in stored],
            "missing": missing,
            "details_not_known": bool(details_not_known),
        },
    )
    return filled


def lock_for_intake(template):
    """Read the form's definition for writing, taking its locks (form, catalogs, category) before
    the desk creates the request, so staff intake locks in the same order as every other capture
    (request_forms.definition); the reading, which attach_values then uses as it is."""
    return definition.read(template, for_write=True)


def attach_values(
    *,
    actor,
    request,
    template,
    expected_definition,
    values,
    details_not_known=False,
    locked=None,
):
    """Store a form's values on a request being created, inside its transaction: the
    `on_created` of desk.services.create_request for staff intake. Checks the form (still
    active, its definition still `expected_definition`, the one the page was drawn from, offered
    to the request's organization) and the values for the actor again, from one locked read of
    the definition, and that the form files under the request's category; raises
    ValidationError keyed by field key, which undoes the creation. Call `lock_for_intake` before
    the creation, in the same transaction, and pass its reading as `locked` (read once)."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Form values are stored with the ticket, in its transaction.")
    defn = _usable_definition(actor, template, details_not_known, expected_definition, locked)
    _check_offer(defn, request)
    rows, missing = validate_values(defn, actor, values, enforce_required=not details_not_known)
    return _store(actor, request, defn, rows, missing, details_not_known)


@transaction.atomic
def create_request_with_form(
    *,
    actor,
    template,
    expected_definition,
    values,
    details_not_known=False,
    subject="",
    first_entry_body="",
    **create_request_kwargs,
):
    """Raise a request with `template` and its values in one transaction; return the request.

    `values` maps field keys to cleaned values (ValuesForm.values()); `expected_definition` is the
    token of the definition the page was drawn from (ValuesForm.posted_definition): a form
    retired or changed since (its fields, catalogs, items and category included) is refused and
    nothing is saved, as is a form not offered to the request's organization. The definition is
    read once, locked, and used for the comparison, the validation and the values stored. A
    portal account must fill every required client field and may not set `details_not_known`.
    Staff may tick `details_not_known` to log a request (a phone call, say) while required
    details are still unknown; the request page then lists what is missing. A blank subject
    becomes the form's client label; a blank first entry a fixed sentence. Everything else is
    passed to desk.services.create_request, which checks it; the values are stored through its
    `on_created`, so a refusal there undoes the whole creation.
    """
    from desk.services import create_request

    defn = _usable_definition(actor, template, details_not_known, expected_definition)
    rows, missing = validate_values(defn, actor, values, enforce_required=not details_not_known)

    def store(request):
        _check_offer(defn, request)
        return _store(actor, request, defn, rows, missing, details_not_known)

    label = defn.template.portal_label
    return create_request(
        actor=actor,
        category=defn.category,
        subject=(subject or "").strip() or label[:120],
        first_entry_body=(first_entry_body or "").strip() or DETAILS_IN_FORM.format(label=label),
        on_created=store,
        **create_request_kwargs,
    )


CLOSED_MESSAGE = "This ticket is closed, so its form details can no longer change."
CORRECTION_AUDIT_REASON = "Form details corrected; each value records its own reason."


def _same(kind, old, new):
    """Whether a stored value and a normalized one say the same. An item list compares items and
    quantities, not the names copied at capture (an item renamed since is not a change)."""
    if kind == FieldKind.ITEMS:

        def lines(value):
            return sorted((row["item"], row["quantity"]) for row in value or [])

        return lines(old) == lines(new)
    return old == new


@transaction.atomic
def correct_values(
    *,
    actor,
    request,
    expected_definition,
    values,
    reason,
    expected,
    reason_code="",
    patient_note="",
    kept_rows=None,
):
    """Fill or correct values of a request's form (staff). Returns the keys changed.

    `values` maps field keys to cleaned values; a key left out is not touched, an empty value
    clears the field. `expected` maps each key in `values` to the id of the value row the
    person saw, or None: a different current row means someone else changed it (stale), checked
    after the request is locked. `expected_definition` is the token of the form's definition the
    page was drawn from (request_forms.definition): any change since (a field's patient flag,
    audience, kind or choices, a catalog or item) refuses the whole correction, so a value is
    never stored under a definition other than the one the person saw. A closed
    request's values are frozen.

    Filling an empty field needs no reason. Replacing or clearing a stored value (a recorded
    "No" included) needs one: `reason`, free text, for ordinary fields; for patient information
    a code from PatientReason (`reason_code`). `patient_note`, optional, is kept on the
    patient-information rows that change, read behind the capability. The code and the note
    apply only to patient-information values that actually change; when none does they are
    discarded, never stored or audited. The audit event and the timeline entry carry keys, row
    ids and the code, never typed text (each value row keeps its own reason). The request's
    revision moves on, so a dialog or bulk selection opened before is refused.

    `kept_rows`: the ids of the item-list value rows the page drew its kept items from
    (CorrectionForm.kept_rows); None, for a caller without a page, the current ones. The
    definition compared is read with the items on them (definition.read_for_correction).
    """
    from desk.models import EntryKind, RequestEntry, ServiceRequest, Status
    from desk.selectors import staff_requests

    require_staff(actor)
    try:
        locked = staff_requests(actor).select_for_update(of=("self",)).get(pk=request.pk)
    except ServiceRequest.DoesNotExist:
        raise ServiceRequest.DoesNotExist("No such ticket.") from None
    if locked.status == Status.CLOSED:
        raise ValidationError(CLOSED_MESSAGE, code="closed")
    filled = FilledForm.objects.filter(request=locked).select_related("template").first()
    if filled is None:
        raise ValidationError("This ticket was not raised with a form.")
    # The items on the value rows the page drew are part of the definition it signed (reviews 7
    # and 8); only a holder of the ticket's lock writes values, so they are read first.
    current = {
        row.field_id: row
        for row in FieldValue.objects.select_for_update().filter(
            filled_form=filled, superseded_at__isnull=True
        )
    }
    defn = _correction_definition(filled, expected_definition, kept_rows)
    for template_field in defn.fillable(actor, new_request=False):
        if template_field.key in values:
            old = current.get(template_field.pk)
            if (expected or {}).get(template_field.key) != (old.pk if old else None):
                raise ValidationError(STALE)
    retained = {
        row.field_id: kinds.item_ids(row.value)
        for row in current.values()
        if row.kind == FieldKind.ITEMS
    }
    rows, _missing = validate_values(
        defn,
        actor,
        values,
        enforce_required=False,
        new_request=False,
        retained=retained,
    )
    changes = []
    for template_field, value in rows:
        if template_field.key not in values:
            continue
        old = current.get(template_field.pk)
        patient = template_field.holds_patient_information or bool(
            old and old.holds_patient_information
        )
        if patient and not may_see_patient_information(actor):
            raise PermissionDenied("This value holds patient information.")
        if not _same(template_field.kind, old.value if old else None, value):
            changes.append((template_field, old, value, patient))
    if not changes:
        raise ValidationError("Nothing changed.")

    def replaces(old):
        return old is not None and old.value is not None  # False is a value

    reason = (reason or "").strip()
    ordinary = [change for change in changes if not change[3]]
    patient_changes = [change for change in changes if change[3]]
    if any(replaces(old) for _field, old, *_rest in ordinary) and not reason:
        raise ValidationError({"reason": "A reason is required to change or clear a value."})
    if patient_changes:
        if reason_code and reason_code not in PatientReason.values:
            raise ValidationError({"reason_code": "Choose a reason from the list."})
        if any(replaces(old) for _field, old, *_rest in patient_changes) and not reason_code:
            raise ValidationError({"reason_code": "Choose why the patient information changed."})
        patient_note = (patient_note or "").strip()
    else:
        reason_code, patient_note = "", ""  # nothing of patient information changed
    now = timezone.now()
    changed, before_ids, after_ids = [], [], []
    for template_field, old, value, patient in changes:
        if old is not None:
            FieldValue.objects.filter(pk=old.pk).update(superseded_at=now)
            before_ids.append(old.pk)
        new = FieldValue.objects.create(
            filled_form=filled,
            value=value,
            entered_by=actor,
            replaces=old,
            # The note only on patient-information rows, read behind the capability.
            reason=patient_note if patient else reason,
            reason_code=reason_code if patient else "",
            **_copy(template_field),
        )
        after_ids.append(new.pk)
        changed.append(template_field.key)
    locked.revision += 1
    locked.save(update_fields=["revision", "updated_at"])
    RequestEntry.objects.create(
        request=locked,
        kind=EntryKind.EVENT,
        author=actor,
        event_action="form_values_changed",
        event_data={"fields": changed},
    )
    record_event(
        actor=actor,
        action="request_form.values_changed",
        obj=filled,
        before={"value_ids": before_ids},
        after={
            "request_id": locked.pk,
            "fields": changed,
            "value_ids": after_ids,
            "reason_code": reason_code,
        },
        reason=CORRECTION_AUDIT_REASON,
    )
    return changed


@transaction.atomic
def reveal_value(*, actor, value_id):
    """A patient-information value for a staff account holding the capability, on a request it
    may read. Audited; the event names the request, the field and the value row, never the
    value. Raises FieldValue.DoesNotExist for anything else."""
    from .selectors import revealable_value

    require_staff(actor)
    row = revealable_value(actor, value_id)
    if row is None:
        raise FieldValue.DoesNotExist("No such value.")
    record_event(
        actor=actor,
        action="request_form.value_revealed",
        obj=row.filled_form,
        after={"request_id": row.filled_form.request_id, "field": row.key, "value_id": row.pk},
    )
    return row
