"""The definition a form page is drawn from and a write is checked against (reviews 5 and 6).

Every page that captures values (staff intake, the portal form and "Request again", the staff
correction) reads the definition once, as one consistent snapshot (`read`), and derives both its
controls and its token from that same object. Every write reads it once more, locked, and
compares, validates and stores from that one object. The write is refused when the token differs,
whatever changed. The definition is:

- the form: its revision (any change of its own settings moves it on), its names and
  instructions, whether it is active and offered in the portal, its category, and the
  organizations it is offered to;
- every field of the form, active or not: its revision and every setting that affects what an
  answer means or how it is protected (key, label, help text, kind, audience, required, the
  patient-information flag, options, number limits, catalog, copy on repeat, active);
- every catalog an item-list field uses, and every item of those catalogs;
- on a correction, the items it keeps from a catalog the form no longer uses (`retained`: those
  on the value rows the page drew, which never change, so a colleague's later correction of the
  values is not a change of the form), with their catalogs: the page shows them, so they are
  signed too (reviews 7 and 8);
- the category: its revision and its protection (restricted, complaint, clients may raise,
  active, its group active).

Order is left out (a reordered form asks the same). The token is an opaque keyed digest of that
state: it says nothing about the definition (field keys, which hold patient information), and
cannot be made without the deployment's secret key.

Locks, one order everywhere: the form, then its catalogs, then its category, then the category's
group. Setup changes take the form's lock to change a field, the catalog's to change an item, the
category's (or group's) to change it; preset loading takes forms before catalogs; staff intake
takes the form's locks before the desk creates the request. A drawing holds share locks while it
reads, so no setup change commits in the middle of it; a write holds them (the category as the
desk's own creation does) until it commits.

Everything a capture page says about the form (its instructions, its fields, the items it offers
or keeps) comes from the one Definition it signs, and a refused page is drawn from a new reading
with nothing that was typed. A view, form or template tag that queries the form again after the
reading would show one state and sign another (reviews 6 and 7).
"""

import hashlib
import json
from dataclasses import dataclass, field

from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.db.models import Q
from django.utils.crypto import constant_time_compare, salted_hmac

from taxonomy.models import RequestCategory

from .models import (
    NO_VALUE_KINDS,
    Audience,
    Catalog,
    CatalogItem,
    FieldKind,
    RequestTemplate,
    TemplateField,
)

SALT = "request_forms.definition"
TEMPLATE_SETTINGS = (
    "pk",
    "revision",
    "key",
    "name",
    "client_label",
    "instructions",
    "category_id",
    "offered_in_portal",
    "is_active",
)
FIELD_SETTINGS = (
    "pk",
    "revision",
    "key",
    "label",
    "help_text",
    "kind",
    "audience",
    "required",
    "holds_patient_information",
    "choices",
    "whole_number",
    "min_value",
    "max_value",
    "catalog_id",
    "copy_on_repeat",
    "is_active",
)
# Columns excluded from the definition include menu and setup presentation,
# preset bookkeeping, staff-only descriptions and search words.
LEFT_OUT = {
    "RequestTemplate": {"id", "description", "icon", "featured", "position", "preset"},
    "TemplateField": {"id", "template_id", "position"},
    "Catalog": {"id", "key", "description", "preset"},
    "CatalogItem": {"id", "search_words", "position"},
    "RequestCategory": {"id", "position", "preset"},
}
CATALOG_SETTINGS = ("pk", "revision", "name", "is_active")
ITEM_SETTINGS = (
    "pk",
    "catalog_id",
    "revision",
    "code",
    "name",
    "unit",
    "max_quantity",
    "is_active",
)
CATEGORY_SETTINGS = (
    "pk",
    "revision",
    "key",
    "name",
    "client_label",
    "guidance",
    "parent_id",
    "default_team_id",
    "default_priority",
    "client_may_raise",
    "is_complaint",
    "is_restricted",
    "is_active",
)


def _row(obj, names):
    return [getattr(obj, name) for name in names]


@dataclass(frozen=True)
class Definition:
    """One consistent read of a form's definition. Everything a capture page draws, validates
    or stores comes from it."""

    template: RequestTemplate
    category: RequestCategory
    fields: tuple  # every field, in the form's order, each with its catalog attached
    catalogs: dict  # id -> Catalog (the fields' catalogs, and the catalogs of retained items)
    items: tuple  # every item of the fields' catalogs and the retained items, each with its catalog
    client_ids: tuple
    digest: str = field(repr=False)
    retained: frozenset = frozenset()  # the item ids a correction keeps, read and signed
    for_write: bool = False  # read, locked, for a write in the caller's transaction

    @property
    def token(self):
        """The opaque token a page drawn from this definition carries."""
        return salted_hmac(SALT, self.digest, algorithm="sha256").hexdigest()

    def matches(self, raw):
        """Whether a posted token names exactly this definition."""
        return isinstance(raw, str) and constant_time_compare(raw, self.token)

    # What the definition offers a person ------------------------------------------------------

    def value_fields(self, *, headings=False, new_request=True):
        out = []
        for template_field in self.fields:
            if not template_field.is_active:
                continue
            if template_field.kind in NO_VALUE_KINDS and not headings:
                continue
            if (
                new_request
                and template_field.kind == FieldKind.ITEMS
                and not self._catalog_active(template_field)
            ):
                continue  # a retired catalog takes no new orders (F4)
            out.append(template_field)
        return out

    def fillable(self, user, *, headings=False, new_request=True):
        """As request_forms.selectors.fillable_fields, from this definition."""
        from .selectors import is_portal_account, may_see_patient_information

        fields = self.value_fields(headings=headings, new_request=new_request)
        if is_portal_account(user):
            return [f for f in fields if f.audience == Audience.CLIENT]
        if not may_see_patient_information(user):
            return [f for f in fields if not f.holds_patient_information]
        return fields

    def hidden_required(self, user):
        """As request_forms.selectors.hidden_required, from this definition."""
        from .selectors import is_portal_account, may_see_patient_information

        if is_portal_account(user) or may_see_patient_information(user):
            return []
        return [f for f in self.value_fields() if f.required and f.holds_patient_information]

    def retired_catalog_keys(self):
        return {
            f.key
            for f in self.fields
            if f.is_active and f.kind == FieldKind.ITEMS and not self._catalog_active(f)
        }

    def orderable_items(self, template_field, retained=()):
        """As kinds.orderable_items, from this definition: the active items of the field's catalog
        while it is active, plus `retained` (item ids already on the value being corrected, kept
        even from another catalog). Items come only from this reading, never from a query of
        their own, so a page signs every item it shows: a kept item the reading did
        not include (`read(..., retained=...)`) is not offered."""
        retained = set(retained or ())
        active = self._catalog_active(template_field)
        out = [
            item
            for item in self.items
            if (item.catalog_id == template_field.catalog_id and item.is_active and active)
            or item.pk in retained
        ]
        return sorted(out, key=lambda item: (item.position, item.name, item.pk))

    def _catalog_active(self, template_field):
        catalog = self.catalogs.get(template_field.catalog_id)
        return catalog is not None and catalog.is_active


def _lock(model, ids, mode):
    """Lock rows by id, in id order: `share` (FOR SHARE) or `key` (FOR NO KEY UPDATE)."""
    ids = sorted(set(ids))
    if not ids:
        return
    clause = {"share": "FOR SHARE", "key": "FOR NO KEY UPDATE"}[mode]
    table = connection.ops.quote_name(model._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT id FROM {table} WHERE id = ANY(%s) ORDER BY id {clause}",  # noqa: S608
            [ids],
        )


def read(template, *, for_write=False, category=None, retained=()):
    """The definition of `template` (a RequestTemplate or its id), read once, consistently.

    The form, its catalogs and its category are locked in that order while it is read (shared;
    a write, `for_write`, keeps the locks to its commit and takes the category as the desk's
    creation does). `category`: the category object the page shows (the desk's intake shows its
    guidance and protection from it), used when it is the form's own, so the token describes
    what the page shows; a write always reads it itself. `retained`: on a correction, the ids of
    the items the page keeps (read_for_correction); those outside the
    fields' catalogs are read and locked with their catalogs, and signed with the rest."""
    template_id = getattr(template, "pk", template)
    retained = frozenset(retained or ())
    with transaction.atomic():
        _lock(RequestTemplate, [template_id], "share")
        own = RequestTemplate.objects.get(pk=template_id)
        fields = list(
            TemplateField.objects.filter(template_id=template_id).order_by("position", "pk")
        )
        field_catalog_ids = {f.catalog_id for f in fields if f.catalog_id}
        catalog_ids = set(field_catalog_ids)
        if retained:
            # Read before the lock: the services never move an item to another catalog.
            catalog_ids |= set(
                CatalogItem.objects.filter(pk__in=retained).values_list("catalog_id", flat=True)
            )
        _lock(Catalog, catalog_ids, "share")
        catalogs = {c.pk: c for c in Catalog.objects.filter(pk__in=catalog_ids)}
        items = list(
            CatalogItem.objects.filter(
                Q(catalog_id__in=field_catalog_ids) | Q(pk__in=retained)
            ).order_by("catalog_id", "position", "name", "pk")
        )
        moved = {item.catalog_id for item in items} - catalogs.keys()
        if moved:  # an item moved to another catalog in between (only outside the services)
            _lock(Catalog, moved, "share")
            catalogs |= {c.pk: c for c in Catalog.objects.filter(pk__in=moved)}
        if for_write or category is None or category.pk != own.category_id:
            _lock(RequestCategory, [own.category_id], "key" if for_write else "share")
            parent_id = RequestCategory.objects.values_list("parent_id", flat=True).get(
                pk=own.category_id
            )
            # Its group's active flag is signed too: locked (the category's lock keeps it the
            # category's group), then read with it, so the group is not retired between a
            # write's comparison and its commit.
            _lock(RequestCategory, [parent_id] if parent_id else [], "share")
            category = RequestCategory.objects.select_related("parent").get(pk=own.category_id)
        client_ids = tuple(sorted(own.clients.values_list("pk", flat=True)))
    for template_field in fields:
        template_field.template = own
        if template_field.catalog_id:
            template_field.catalog = catalogs[template_field.catalog_id]
    for item in items:
        item.catalog = catalogs[item.catalog_id]
    own.category = category
    state = {
        "template": _row(own, TEMPLATE_SETTINGS),
        "clients": client_ids,
        "fields": sorted(_row(f, FIELD_SETTINGS) for f in fields),
        "catalogs": sorted(_row(c, CATALOG_SETTINGS) for c in catalogs.values()),
        "items": sorted(_row(i, ITEM_SETTINGS) for i in items),
        "category": [
            *_row(category, CATEGORY_SETTINGS),
            category.parent.is_active if category.parent_id else None,
        ],
    }
    text = json.dumps(state, cls=DjangoJSONEncoder, sort_keys=True, separators=(",", ":"))
    return Definition(
        template=own,
        category=category,
        fields=tuple(fields),
        catalogs=catalogs,
        items=tuple(items),
        client_ids=client_ids,
        digest=hashlib.sha256(text.encode()).hexdigest(),
        retained=retained,
        for_write=for_write,
    )


def read_for_correction(filled_form, *, rows=None, for_write=False):
    """The definition a correction of `filled_form` (a request's FilledForm) is drawn from and
    written against: its form's, with the items on the item-list value rows the page drew
    (`rows`, the ids it carries; None: the current rows), so the page signs the items it keeps
    from a catalog the form no longer uses."""
    from .kinds import held_item_ids
    from .selectors import kept_item_values

    return read(
        filled_form.template_id,
        for_write=for_write,
        retained=held_item_ids(kept_item_values(filled_form, rows)),
    )


def token(template):
    """The token a page drawn now carries (a convenience for callers outside a page)."""
    return read(template).token


def unchanged(template, raw):
    """Whether the posted token `raw` names the template's definition as it is now."""
    return read(template).matches(raw)


def lock(template):
    """Lock the form's definition for the caller's transaction (as a write does); the template."""
    return read(template, for_write=True).template
