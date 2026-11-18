"""What each field kind means: its Django form field, the stored value, and how it reads.

Three functions per kind, kept together so a kind is defined once:

- `form_field(field, *, enforce_required)`: the Django form field for the ValuesForm;
- `normalize(field, value, *, items)`: the service boundary. Takes a cleaned Python value (or the
  raw string a caller passes), checks it against the field's current definition and returns the
  JSON value to store, or None when empty. Raises ValidationError with a message that never
  repeats the value (a patient-information value must not reach a log through an error);
- `display(kind, value)`: the stored JSON as text lines for people.
"""

import datetime as dt
from decimal import Decimal, InvalidOperation

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from .models import CatalogItem, FieldKind

TEXT_LIMITS = {FieldKind.SHORT_TEXT: 200, FieldKind.LONG_TEXT: 4000}
TIME_FORMATS = ("%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p")
YES_NO = (("yes", "Yes"), ("no", "No"))
RADIO_LIMIT = 5  # one choice from at most this many options renders as radio buttons
MAX_ITEM_QUANTITY = 9999


def _decimal_text(value):
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


# Form fields ---------------------------------------------------------------------------------


class ItemsWidget(forms.MultiWidget):
    """One quantity box per catalog item, rendered as a list (request_forms/widgets/items.html).
    Each box is the shared number control; the submitted value is {item id: typed text}."""

    template_name = "request_forms/widgets/items.html"

    def __init__(self, items, attrs=None):
        self.items = list(items)
        widgets = {
            str(item.pk): forms.NumberInput(attrs={"min": 0, "inputmode": "numeric"})
            for item in self.items
        }
        super().__init__(widgets, attrs)

    def decompress(self, value):
        value = value or {}
        return [value.get(item.pk, value.get(str(item.pk), "")) or "" for item in self.items]

    def value_from_datadict(self, data, files, name):
        return {item.pk: (data.get(f"{name}_{item.pk}") or "").strip() for item in self.items}

    def value_omitted_from_data(self, data, files, name):
        return all(f"{name}_{item.pk}" not in data for item in self.items)

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        subwidgets = context["widget"]["subwidgets"]
        context["widget"]["rows"] = [
            (item, sub, not (item.is_active and item.catalog.is_active))
            for item, sub in zip(self.items, subwidgets, strict=True)
        ]
        return context


class ItemsField(forms.Field):
    """Quantities by catalog item. Cleans to {item id: positive int}; empty is {}."""

    def __init__(self, items, **kwargs):
        self.items = {item.pk: item for item in items}
        kwargs.setdefault("widget", ItemsWidget(self.items.values()))
        super().__init__(**kwargs)

    def to_python(self, value):
        quantities = {}
        for pk, text in (value or {}).items():
            if text in ("", None):
                continue
            try:
                quantity = int(str(text).strip())
            except ValueError:
                raise ValidationError("Quantities are whole numbers.") from None
            if quantity < 0:
                raise ValidationError("Quantities cannot be negative.")
            if quantity:
                quantities[int(pk)] = quantity
        return quantities

    def validate(self, value):
        if self.required and not value:
            raise ValidationError("Choose at least one item.", code="required")

    def has_changed(self, initial, data):
        try:
            return self.to_python(initial) != self.to_python(data)
        except ValidationError:
            return True


def orderable_items(field, retained=()):
    """The catalog items an item-list field takes: the active items of its catalog while the
    catalog is active, plus `retained`, the ids of items already on the value being corrected
    (kept through a correction even after they or their catalog were retired)."""
    usable = Q(catalog_id=field.catalog_id, is_active=True, catalog__is_active=True)
    if retained:
        usable |= Q(pk__in=list(retained))
    return (
        CatalogItem.objects.filter(usable)
        .select_related("catalog")
        .order_by("position", "name", "pk")
    )


def item_ids(value):
    """The item ids on a stored item-list value."""
    return {row["item"] for row in value or []}


def held_item_ids(rows):
    """The item ids on the item-list values among `rows` (value rows of a request's form)."""
    return frozenset(
        item for row in rows if row.kind == FieldKind.ITEMS for item in item_ids(row.value)
    )


def form_field(field, *, enforce_required=True, items=None):
    """The Django form field for one TemplateField. `items`: for an item list, the items to
    offer (default: orderable_items(field))."""
    required = field.required and enforce_required
    common = {"label": field.label, "help_text": field.help_text, "required": required}
    kind = field.kind
    if kind == FieldKind.SHORT_TEXT:
        return forms.CharField(max_length=TEXT_LIMITS[kind], **common)
    if kind == FieldKind.LONG_TEXT:
        return forms.CharField(
            max_length=TEXT_LIMITS[kind], widget=forms.Textarea(attrs={"rows": 3}), **common
        )
    if kind == FieldKind.NUMBER:
        attrs = {}
        if field.min_value is not None and field.min_value >= 0:
            attrs["min"] = 0
        if not field.whole_number:
            attrs["step"] = "any"
        return forms.DecimalField(
            max_digits=14,
            decimal_places=0 if field.whole_number else 4,
            min_value=field.min_value,
            max_value=field.max_value,
            widget=forms.NumberInput(attrs=attrs),
            **common,
        )
    if kind == FieldKind.DATE:
        return forms.DateField(
            widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"), **common
        )
    if kind == FieldKind.DATETIME:
        return forms.DateTimeField(
            widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            **common,
        )
    if kind == FieldKind.TIME:
        common["help_text"] = " ".join(
            filter(None, [field.help_text, "Laboratory time, for example 14:30 or 2:30 pm."])
        )
        return forms.TimeField(
            input_formats=TIME_FORMATS,
            widget=forms.TimeInput(
                format="%H:%M", attrs={"autocomplete": "off", "placeholder": "HH:MM"}
            ),
            **common,
        )
    if kind == FieldKind.CHOICE:
        options = [(choice, choice) for choice in field.choices]
        if len(options) <= RADIO_LIMIT:
            return forms.ChoiceField(choices=options, widget=forms.RadioSelect, **common)
        return forms.ChoiceField(choices=[("", "Choose one"), *options], **common)
    if kind == FieldKind.MULTIPLE_CHOICE:
        return forms.MultipleChoiceField(
            choices=[(choice, choice) for choice in field.choices],
            widget=forms.CheckboxSelectMultiple,
            **common,
        )
    if kind == FieldKind.YES_NO:
        return forms.ChoiceField(choices=YES_NO, widget=forms.RadioSelect, **common)
    if kind == FieldKind.CONFIRMATION:
        return forms.BooleanField(**common)
    if kind == FieldKind.HEADING:
        return None
    if kind == FieldKind.ITEMS:
        return ItemsField(orderable_items(field) if items is None else items, **common)
    raise ValueError(f"Unknown field kind {kind!r}")


def initial_for_form(kind, value):
    """A stored JSON value as the ValuesForm's initial value."""
    if value is None:
        return None
    if kind == FieldKind.YES_NO:
        return "yes" if value else "no"
    if kind == FieldKind.ITEMS:
        return {row["item"]: str(row["quantity"]) for row in value}
    if kind == FieldKind.DATETIME:
        return value.replace(" ", "T")
    return value


# The service boundary ------------------------------------------------------------------------


def _empty(value):
    return value is None or value == "" or value == [] or value == {} or value is False


def normalize(field, value, *, items=None):
    """The JSON value to store for `field`, or None when the value is empty. `items` maps active
    catalog item ids to items (for an item list)."""
    kind = field.kind
    if kind in (FieldKind.SHORT_TEXT, FieldKind.LONG_TEXT):
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValidationError("Enter text.")
        value = value.strip()
        if len(value) > TEXT_LIMITS[kind]:
            raise ValidationError(f"Use at most {TEXT_LIMITS[kind]} characters.")
        return value or None
    if kind == FieldKind.NUMBER:
        if value in (None, ""):
            return None
        try:
            number = Decimal(str(value).strip())
        except InvalidOperation:
            raise ValidationError("Enter a number.") from None
        if not number.is_finite():
            raise ValidationError("Enter a number.")
        if field.whole_number and number != number.to_integral_value():
            raise ValidationError("Enter a whole number.")
        if field.min_value is not None and number < field.min_value:
            raise ValidationError(f"Enter {_decimal_text(field.min_value)} or more.")
        if field.max_value is not None and number > field.max_value:
            raise ValidationError(f"Enter {_decimal_text(field.max_value)} or less.")
        return _decimal_text(number)
    if kind == FieldKind.DATE:
        if value in (None, ""):
            return None
        if isinstance(value, dt.datetime) or not isinstance(value, (dt.date, str)):
            raise ValidationError("Enter a date.")
        if isinstance(value, str):
            try:
                value = dt.date.fromisoformat(value.strip())
            except ValueError:
                raise ValidationError("Enter a date as YYYY-MM-DD.") from None
        return value.isoformat()
    if kind == FieldKind.DATETIME:
        if value in (None, ""):
            return None
        if isinstance(value, str):
            try:
                value = dt.datetime.fromisoformat(value.strip())
            except ValueError:
                raise ValidationError("Enter a date and time as YYYY-MM-DD HH:MM.") from None
        if not isinstance(value, dt.datetime):
            raise ValidationError("Enter a date and time.")
        if timezone.is_aware(value):
            value = timezone.localtime(value)  # the laboratory's wall clock
        return value.strftime("%Y-%m-%d %H:%M")
    if kind == FieldKind.TIME:
        if value in (None, ""):
            return None
        if isinstance(value, str):
            try:
                value = dt.time.fromisoformat(value.strip())
            except ValueError:
                raise ValidationError("Enter a time as HH:MM.") from None
        if not isinstance(value, dt.time):
            raise ValidationError("Enter a time.")
        return value.strftime("%H:%M")
    if kind == FieldKind.CHOICE:
        if value in (None, ""):
            return None
        if value not in field.choices:
            raise ValidationError("Choose one of the options.")
        return value
    if kind == FieldKind.MULTIPLE_CHOICE:
        if not value:
            return None
        if isinstance(value, str) or not all(choice in field.choices for choice in value):
            raise ValidationError("Choose from the options.")
        chosen = set(value)
        return [choice for choice in field.choices if choice in chosen]
    if kind == FieldKind.YES_NO:
        if value in (None, ""):
            return None
        if value in (True, "yes"):
            return True
        if value in (False, "no"):
            return False
        raise ValidationError("Answer yes or no.")
    if kind == FieldKind.CONFIRMATION:
        if value in (None, "", False):
            return None
        if value is not True:
            raise ValidationError("Tick the box to confirm.")
        return True
    if kind == FieldKind.ITEMS:
        if not value:
            return None
        if not isinstance(value, dict):
            raise ValidationError("Give a quantity for each item.")
        items = items or {}
        rows = []
        for pk, quantity in value.items():
            try:
                pk = int(pk)
            except TypeError, ValueError:
                raise ValidationError("Choose items from the list.") from None
            item = items.get(pk)
            if item is None:
                raise ValidationError("One of the items is no longer offered.")
            if isinstance(quantity, bool) or not isinstance(quantity, int):
                raise ValidationError("Quantities are whole numbers.")
            if quantity == 0:
                continue
            if quantity < 0 or quantity > MAX_ITEM_QUANTITY:
                raise ValidationError("Quantities run from 1 to 9999.")
            if item.max_quantity and quantity > item.max_quantity:
                raise ValidationError(f"{item.name}: at most {item.max_quantity} at once.")
            rows.append(
                {
                    "item": item.pk,
                    "code": item.code,
                    "name": item.name,
                    "unit": item.unit,
                    "quantity": quantity,
                    "_order": (item.position, item.name, item.pk),
                }
            )
        rows.sort(key=lambda row: row.pop("_order"))
        return rows or None
    raise ValidationError("This field cannot take a value.")


def is_empty(value):
    return _empty(value)


# Display -------------------------------------------------------------------------------------


def display(kind, value):
    """Text lines for a stored value. An empty or cleared value is no lines."""
    if value is None:
        return []
    if kind == FieldKind.DATE:
        return [value]
    if kind == FieldKind.MULTIPLE_CHOICE:
        return [", ".join(value)]
    if kind == FieldKind.YES_NO:
        return ["Yes" if value else "No"]
    if kind == FieldKind.CONFIRMATION:
        return ["Confirmed"] if value else []
    if kind == FieldKind.ITEMS:
        lines = []
        for row in value:
            name = f"{row['name']} ({row['code']})" if row.get("code") else row["name"]
            unit = f", {row['unit']}" if row.get("unit") else ""
            lines.append(f"{row['quantity']} × {name}{unit}")
        return lines
    return [str(value)]
