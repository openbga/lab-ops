"""Forms of request forms: the values form clients and staff fill, the staff correction form, and
the setup forms for templates, fields, catalogs and items."""

from django import forms
from django.utils.text import slugify

from core.forms import REASON_HELP, GroupedForm, StyledForm
from core.ids import whole_number
from core.lookups import RemoteSelectMultiple
from desk.portal_forms import RaiseRequestForm as PortalRaiseBase
from taxonomy.models import RequestCategory

from . import definition, kinds
from .models import (
    CHOICE_KINDS,
    NO_VALUE_KINDS,
    Audience,
    Catalog,
    CatalogItem,
    FieldKind,
    PatientReason,
    RequestTemplate,
    TemplateField,
)
from .selectors import current_values, is_portal_account, kept_item_values
from .services import FORM_CHANGED, HIDDEN_REQUIRED

NOT_KNOWN = "details_not_known"
DEFINITION = "form_definition"
PATIENT_HELP = (
    "Tick only if the laboratory decided this field needs patient information. Its values are "
    "then revealed one at a time, with an audit entry, to staff who hold “Can view patient "
    "information”, and shown to the client who raised or may see the ticket. They never "
    "appear in email, lists, search, logs or the audit history, and staff without "
    "that permission cannot fill, reveal or correct them. "
    "Prefer an order, accession or sample number: the product's stance is minimal patient data."
)


FIELD_PREFIX = "field-"


def field_name(key):
    """The form field name of a template field key: prefixed, so it never meets a host form's own
    fields (subject, site ...)."""
    return f"{FIELD_PREFIX}{key}"


class ValuesMixin:
    """Adds one Django field per template field this account may fill (request_forms.selectors.
    fillable_fields), named `field-<key>`, to a host form. `values()` gives the cleaned values
    by key for request_forms.services. Staff get the box "Some required details are not known
    yet": ticked, required fields may stay empty; it is needed when a required field holds
    patient information this person cannot fill.

    The form is drawn from one read of the definition (request_forms.definition.read, kept as
    `definition`): its controls and the hidden `form_definition` token come from that same
    object, so a page never shows one definition and signs another. A submission is
    refused when its token is not the current definition's (`definition_changed`); the view then
    draws a fresh form from the current definition with no submitted values, as it does when
    the service refuses the write for the same reason (services.is_form_changed)."""

    new_request = True

    def add_value_fields(self, template, user, initial_values=None, defn=None):
        defn = defn or definition.read(template)
        self.definition = defn
        self.template = defn.template
        self.staff = not is_portal_account(user)
        self.template_fields = defn.fillable(user, new_request=self.new_request)
        self.layout_fields = defn.fillable(user, headings=True, new_request=self.new_request)
        self.hidden_required = defn.hidden_required(user) if self.new_request else []
        relaxed = self.staff and self.is_bound and self._not_known_ticked()
        initial_values = initial_values or {}
        for template_field in self.template_fields:
            field = kinds.form_field(
                template_field,
                enforce_required=not relaxed,
                items=self.items_for(template_field),
            )
            field.initial = initial_values.get(template_field.key)
            _style(field)
            self.fields[field_name(template_field.key)] = field
        if self.staff and (any(f.required for f in self.template_fields) or self.hidden_required):
            field = forms.BooleanField(
                required=False,
                label="Some required details are not known yet",
                help_text="You can open the ticket now and fill in the missing details later.",
            )
            _style(field)
            self.fields[NOT_KNOWN] = field
        self.fields[DEFINITION] = forms.CharField(
            required=False, widget=forms.HiddenInput, initial=defn.token
        )
        if self.is_bound:
            raw = self.data.get(self.add_prefix(DEFINITION))
            self.posted_definition = raw if isinstance(raw, str) else ""
            self.definition_changed = not defn.matches(self.posted_definition)
        else:
            self.posted_definition = defn.token
            self.definition_changed = False

    def items_for(self, template_field):
        """The catalog items an item-list field offers, from the definition (None for other
        kinds)."""
        if template_field.kind != FieldKind.ITEMS:
            return None
        return self.definition.orderable_items(template_field)

    def clean(self):
        cleaned = super().clean()
        if self.definition_changed:
            self.add_error(None, FORM_CHANGED)
            return cleaned
        if self.hidden_required and not self._not_known_ticked():
            self.add_error(NOT_KNOWN if NOT_KNOWN in self.fields else None, HIDDEN_REQUIRED)
        return cleaned

    def _not_known_ticked(self):
        return self.data.get(self.add_prefix(NOT_KNOWN)) in ("on", "true", "1", "True")

    @property
    def details_not_known(self):
        return bool(self.staff and self.cleaned_data.get(NOT_KNOWN))

    def values(self):
        return {
            template_field.key: self.cleaned_data.get(field_name(template_field.key))
            for template_field in self.template_fields
        }

    def value_fields(self):
        return [self[field_name(template_field.key)] for template_field in self.template_fields]

    def layout(self):
        """The form in order: ("heading", TemplateField) and ("field", BoundField) pairs. A
        heading with no field of this person's after it is left out."""
        out = []
        for template_field in self.layout_fields:
            if template_field.kind in NO_VALUE_KINDS:
                out.append(("heading", template_field))
            elif field_name(template_field.key) in self.fields:
                out.append(("field", self[field_name(template_field.key)]))
        return [
            item
            for index, item in enumerate(out)
            if item[0] == "field" or (index + 1 < len(out) and out[index + 1][0] == "field")
        ]

    def add_value_errors(self, exc):
        """Place a service ValidationError on the fields it names (by key), the rest on top."""
        if not hasattr(exc, "message_dict"):
            self.add_error(None, exc)
            return
        for key, errors in exc.message_dict.items():
            name = field_name(key) if field_name(key) in self.fields else key
            self.add_error(name if name in self.fields else None, errors)


def _style(field):
    widget = field.widget
    if isinstance(widget, forms.CheckboxInput):
        widget.attrs.setdefault("class", "form-check-input")
    elif isinstance(widget, (forms.RadioSelect, forms.CheckboxSelectMultiple, kinds.ItemsWidget)):
        return
    elif isinstance(widget, forms.Select):
        widget.attrs.setdefault("class", "form-select")
    else:
        widget.attrs.setdefault("class", "form-control")


class ValuesForm(ValuesMixin, StyledForm, forms.Form):
    """The values of one template on their own, for a host page that keeps its own form apart
    (staff intake): ValuesForm(template, data, user=...). Its fields are named `field-<key>`
    plus `details_not_known` for staff, so it needs no form prefix. `definition`: a definition
    already read for this page (request_forms.definition.read); read here otherwise."""

    def __init__(
        self, template, *args, user, initial_values=None, prefix=None, definition=None, **kwargs
    ):
        super().__init__(*args, prefix=prefix, **kwargs)
        self.add_value_fields(template, user, initial_values, defn=definition)


KEPT_ROWS = "kept_rows"


def _row_ids(raw):
    """The value-row ids a correction page carried (KEPT_ROWS): whole numbers, at most
    selectors.MAX_KEPT_ROWS of them; anything else is ignored (the token then refuses a page
    whose kept items differ)."""
    from .selectors import MAX_KEPT_ROWS

    parts = str(raw or "").split()[:MAX_KEPT_ROWS]
    return sorted({number for number in map(whole_number, parts) if number is not None})


class CorrectionForm(ValuesForm):
    """Staff fill or correct the values of a request's form. Each field starts from its current
    value, except patient information, which starts empty (typed: a replacement; empty: kept);
    a hidden token per field records which value row the person saw. An item list also offers
    the items the request already holds, retired ones included, so they stay until changed.
    Reasons: `reason` for ordinary values (each value row keeps it); for patient information a
    code and an optional note kept with the patient-information value only."""

    new_request = False

    def __init__(self, filled_form, *args, user, **kwargs):
        self.current = current_values(filled_form)
        data = args[0] if args else kwargs.get("data")
        item_rows = [row for row in self.current.values() if row.kind == FieldKind.ITEMS]
        seen_items = set()
        if data is None:
            item_values = item_rows
        else:
            # Older correction pages identify the values they showed with per-field IDs only.
            seen_items = {
                pk for row in item_rows if (pk := whole_number(data.get(f"seen_{row.key}")))
            }
            row_ids = _row_ids(data[KEPT_ROWS]) if KEPT_ROWS in data else seen_items
            item_values = kept_item_values(filled_form, row_ids)
        # A colleague may have removed a kept item since the page opened. Compare its
        # submitted quantity to the one the staff member saw, using those same controls.
        self.shown_items = {row.field_id: row for row in item_values}
        self.kept_rows = sorted(row.pk for row in self.shown_items.values())
        defn = definition.read(
            filled_form.template_id, retained=kinds.held_item_ids(self.shown_items.values())
        )
        initial_values = {}
        hidden = set()
        for template_field in defn.fillable(user, new_request=False):
            rows = self.shown_items if template_field.kind == FieldKind.ITEMS else self.current
            row = rows.get(template_field.pk)
            if row is None:
                continue
            if template_field.holds_patient_information or row.holds_patient_information:
                hidden.add(template_field.key)  # never on the page: revealed one by one
                continue
            initial_values[template_field.key] = kinds.initial_for_form(row.kind, row.value)
        super().__init__(
            defn.template,
            *args,
            user=user,
            initial_values=initial_values,
            definition=defn,
            **kwargs,
        )
        if not seen_items.issubset(self.kept_rows):
            self.definition_changed = True
        self.fields.pop(NOT_KNOWN, None)
        self.fields[KEPT_ROWS] = forms.CharField(
            required=False,
            widget=forms.HiddenInput,
            initial=" ".join(str(pk) for pk in self.kept_rows),
        )
        for template_field in self.template_fields:
            self.fields[field_name(template_field.key)].required = False
            if template_field.key in hidden:
                field = self.fields[field_name(template_field.key)]
                note = (
                    "A value is recorded and not shown here. Type a new value only to replace "
                    "it; leave it empty to keep it."
                )
                field.help_text = f"{field.help_text} {note}".strip()
            row = self.current.get(template_field.pk)
            name = f"seen_{template_field.key}"
            self.fields[name] = forms.CharField(
                required=False, widget=forms.HiddenInput, initial=str(row.pk) if row else ""
            )
        self.fields["reason"] = forms.CharField(
            label="Reason for change",
            required=False,
            help_text="Required when you change or clear a value. Kept with each value it "
            "changed, for staff. Do not write patient information here.",
            widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
        )
        if any(f.holds_patient_information for f in self.template_fields) or hidden:
            self.fields["reason_code"] = forms.ChoiceField(
                label="Reason for a patient-information change",
                required=False,
                choices=[("", "Choose a reason"), *PatientReason.choices],
                help_text="Required when you replace or clear patient information. The history "
                "records this choice.",
                widget=forms.Select(attrs={"class": "form-select"}),
            )
            self.fields["patient_note"] = forms.CharField(
                label="Note on the patient-information change",
                required=False,
                max_length=1000,
                help_text="Optional. Kept with the patient-information value it changed, shown "
                "only to staff who hold “Can view patient information”; never in the audit "
                "history. Dropped when no patient-information value changes.",
                widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
            )

    def items_for(self, template_field):
        if template_field.kind != FieldKind.ITEMS:
            return None
        row = self.shown_items.get(template_field.pk)
        return self.definition.orderable_items(
            template_field, kinds.item_ids(row.value if row else None)
        )

    def expected(self):
        out = {}
        for template_field in self.template_fields:
            seen = self.cleaned_data.get(f"seen_{template_field.key}") or ""
            out[template_field.key] = whole_number(seen)
        return out

    def changed_values(self):
        """Only the fields whose input differs from the value shown, so an untouched field is
        never rewritten; a field whose input was not sent at all is untouched too."""
        values = self.values()
        out = {}
        for key, value in values.items():
            name = field_name(key)
            widget = self.fields[name].widget
            if name not in self.changed_data:
                continue
            if widget.value_omitted_from_data(self.data, self.files, self.add_prefix(name)):
                continue
            out[key] = value
        return out


# Setup ---------------------------------------------------------------------------------------

KEY_HELP = (
    "A stable code that never changes after creation, used by presets. Leave empty to make one "
    "from the name."
)


class _KeyMixin:
    key_source = "name"

    def clean_key(self):
        key = (self.cleaned_data.get("key") or "").strip()
        if not key:
            key = slugify(self.data.get(self.add_prefix(self.key_source), ""))[:60]
        if not key:
            raise forms.ValidationError("Give a key, or a name it can be made from.")
        return key


class _EditMixin(forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(
        label="Reason for change", help_text=REASON_HELP, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("key", None)  # the key never changes


class CategoryField(forms.ModelChoiceField):
    def label_from_instance(self, category):
        return f"{category.parent.name}: {category.name}"


class TemplateForm(_KeyMixin, GroupedForm, StyledForm, forms.ModelForm):
    key = forms.SlugField(max_length=60, required=False, help_text=KEY_HELP)
    category = CategoryField(queryset=None, label="Category")
    groups = (
        (
            "Form",
            (
                ("name", "col-md-6"),
                ("key", "col-md-6"),
                ("category", "col-12"),
                ("instructions", "col-12"),
            ),
        ),
        (
            "Client portal",
            (
                ("offered_in_portal", "col-12"),
                ("featured", "col-12"),
                ("client_label", "col-md-6"),
                ("icon", "col-md-6"),
                ("description", "col-12"),
                ("clients", "col-12"),
            ),
        ),
        ("Status", (("is_active", "col-12"),)),
        ("Change record", (("reason", "col-12"),)),
    )

    class Meta:
        model = RequestTemplate
        fields = [
            "name",
            "category",
            "instructions",
            "offered_in_portal",
            "featured",
            "client_label",
            "icon",
            "description",
            "clients",
        ]
        labels = {
            "clients": "Only for these organizations",
            "name": "Form name",
            "offered_in_portal": "Clients may raise tickets with it in the portal",
            "featured": "Show it on the portal home's service menu",
            "client_label": "Client label",
            "description": "Menu description",
        }
        help_texts = {
            "category": "Tickets raised with this form are filed under this category, which "
            "routes them to its team and proposes its priority.",
            "instructions": "Shown at the top of the form, to clients and staff.",
            "client_label": "The name on the portal menu and the client's ticket. Empty: the "
            "form name.",
            "description": "One short line under the label on the portal menu.",
            "offered_in_portal": "Only when its category is one clients may raise.",
            "featured": "Needs it to be offered in the portal. The menu follows the order of "
            "the ticket forms list.",
            "clients": "Leave empty to offer it to every client organization. List organizations "
            "for a form only some use, such as a supply order with their own items.",
        }
        widgets = {
            "instructions": forms.Textarea(attrs={"rows": 3}),
            "clients": RemoteSelectMultiple("active-clients"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        categories = RequestCategory.objects.filter(parent__isnull=False)
        current = self.instance.category_id if self.instance.pk else None
        active = categories.filter(is_active=True, parent__is_active=True)
        if current:
            active = active | categories.filter(pk=current)
        self.fields["category"].queryset = active.select_related("parent").order_by(
            "parent__position", "parent__name", "position", "name"
        )
        self.fields["category"].empty_label = "Choose a category"


class TemplateEditForm(_EditMixin, TemplateForm):
    is_active = forms.BooleanField(
        label="Active",
        required=False,
        help_text="Clear to stop offering it. Tickets raised with it keep their values.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["is_active"].initial = self.instance.is_active


class ChoicesField(forms.CharField):
    """Options typed one per line, cleaned to a list."""

    widget = forms.Textarea(attrs={"rows": 4})

    def prepare_value(self, value):
        if isinstance(value, (list, tuple)):
            return "\n".join(value)
        return value

    def to_python(self, value):
        value = super().to_python(value)
        if isinstance(value, (list, tuple)):
            return list(value)
        return [line.strip() for line in (value or "").splitlines() if line.strip()]


class FieldForm(_KeyMixin, GroupedForm, StyledForm, forms.ModelForm):
    key_source = "label"
    key = forms.SlugField(max_length=60, required=False, help_text=KEY_HELP)
    choices = ChoicesField(
        required=False,
        label="Options",
        help_text="One per line, in the order shown. For one choice or several choices.",
    )
    groups = (
        (
            "Field",
            (
                ("label", "col-md-8"),
                ("key", "col-md-4"),
                ("kind", "col-md-6"),
                ("audience", "col-md-6"),
                ("help_text", "col-12"),
                ("required", "col-12 vr-setting-required"),
                ("copy_on_repeat", "col-12 vr-setting-repeat"),
            ),
        ),
        (
            "Type settings",
            (
                ("choices", "col-12 vr-setting-choices"),
                ("catalog", "col-md-6 vr-setting-catalog"),
                ("whole_number", "col-12 vr-setting-number"),
                ("min_value", "col-md-6 vr-setting-number"),
                ("max_value", "col-md-6 vr-setting-number"),
            ),
        ),
        ("Patient information", (("holds_patient_information", "col-12 vr-setting-patient"),)),
        ("Status", (("is_active", "col-12"),)),
        ("Change record", (("reason", "col-12"),)),
    )

    class Meta:
        model = TemplateField
        fields = [
            "label",
            "kind",
            "audience",
            "help_text",
            "required",
            "choices",
            "catalog",
            "whole_number",
            "min_value",
            "max_value",
            "copy_on_repeat",
            "holds_patient_information",
        ]
        labels = {
            "kind": "Type",
            "audience": "Who fills it",
            "help_text": "Help text",
            "required": "Required",
            "whole_number": "Whole numbers only",
            "min_value": "Lowest value",
            "max_value": "Highest value",
            "holds_patient_information": "This field holds patient information",
            "copy_on_repeat": "Copy the answer when a client chooses Request again",
        }
        help_texts = {
            "audience": "Client fills it: asked in the portal and shown on the client's ticket. "
            "Staff fill it, the client sees it: for answers such as a tracking number. Staff "
            "only: never shown to clients.",
            "help_text": "Shown under the field.",
            "required": "The client must fill it to send the ticket. Staff must too, unless they "
            "tick “Some required details are not known yet” while logging a call; the ticket "
            "then shows it as missing. A confirmation is always required.",
            "catalog": "For an item list: the catalog clients pick from.",
            "min_value": "For a number. Empty: no limit.",
            "max_value": "For a number. Empty: no limit.",
            "holds_patient_information": PATIENT_HELP,
            "copy_on_repeat": "Clear it for a number that belongs to one order or sample. Dates, "
            "times, confirmations and patient information are never copied.",
            "kind": "Choose the type to see its settings. A confirmation is always required. "
            "A section heading groups fields under its label and help text.",
        }
        widgets = {
            "min_value": forms.NumberInput(attrs={"inputmode": "decimal"}),
            "max_value": forms.NumberInput(attrs={"inputmode": "decimal"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        catalogs = Catalog.objects.filter(is_active=True)
        if self.instance.pk and self.instance.catalog_id:
            catalogs = catalogs | Catalog.objects.filter(pk=self.instance.catalog_id)
        self.fields["catalog"].queryset = catalogs.order_by("name")
        self.fields["catalog"].empty_label = "No catalog"
        kind_field = self.fields["kind"]
        kind_field.choices = FieldKind.choices
        self.fields["audience"].choices = Audience.choices
        kind_field.widget.attrs["data-vr-field-kind"] = ""
        if self.instance.pk and self.instance.values.exists():
            kind_field.disabled = True
            kind_field.help_text = (
                "Tickets already hold values of this field, so its type stays. Add a new field "
                "and make this one inactive to change it."
            )
            if self.instance.holds_patient_information:
                self.fields["holds_patient_information"].disabled = True
                self.fields["holds_patient_information"].help_text = (
                    PATIENT_HELP + " Tickets already hold values of this field, so the mark stays."
                )
        elif self.instance.pk:
            kind_field.help_text += (
                " Saving a different type removes settings that belong only to the previous type."
            )
        if self.is_bound:
            # CSS follows the native select, including without JavaScript. A hidden input may
            # still post a previous type's value; it must not invalidate the selected type.
            kind = self["kind"].value()
            self.data = self.data.copy()
            for name, applicable in (
                ("choices", kind in CHOICE_KINDS),
                ("catalog", kind == FieldKind.ITEMS),
                ("min_value", kind == FieldKind.NUMBER),
                ("max_value", kind == FieldKind.NUMBER),
            ):
                if not applicable:
                    self.data.pop(self.add_prefix(name), None)


class FieldEditForm(_EditMixin, FieldForm):
    is_active = forms.BooleanField(
        label="Active",
        required=False,
        help_text="Clear to take it off the form. Tickets that hold a value keep it.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["is_active"].initial = self.instance.is_active


class CatalogForm(_KeyMixin, GroupedForm, StyledForm, forms.ModelForm):
    key = forms.SlugField(max_length=60, required=False, help_text=KEY_HELP)
    groups = (
        ("Catalog", (("name", "col-md-8"), ("key", "col-md-4"), ("description", "col-12"))),
        ("Status", (("is_active", "col-12"),)),
        ("Change record", (("reason", "col-12"),)),
    )

    class Meta:
        model = Catalog
        fields = ["name", "description"]
        labels = {"name": "Catalog name"}
        help_texts = {"description": "For staff: what the catalog holds and who keeps it."}


class CatalogEditForm(_EditMixin, CatalogForm):
    is_active = forms.BooleanField(
        label="Active",
        required=False,
        help_text="Clear to retire this catalog. Item-list fields that use it are "
        "left off new tickets until they use an active catalog. Existing tickets "
        "keep the items already ordered.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["is_active"].initial = self.instance.is_active


class ItemForm(GroupedForm, StyledForm, forms.ModelForm):
    groups = (
        (
            "Item",
            (
                ("name", "col-md-8"),
                ("code", "col-md-4"),
                ("unit", "col-md-6"),
                ("max_quantity", "col-md-6"),
                ("search_words", "col-12"),
            ),
        ),
        ("Status", (("is_active", "col-12"),)),
        ("Change record", (("reason", "col-12"),)),
    )

    class Meta:
        model = CatalogItem
        fields = ["name", "code", "unit", "max_quantity", "search_words"]
        labels = {
            "name": "Item name",
            "code": "Item code",
            "max_quantity": "Most per ticket",
            "search_words": "Other names",
        }
        widgets = {"max_quantity": forms.NumberInput(attrs={"min": 1, "inputmode": "numeric"})}


class ItemEditForm(_EditMixin, ItemForm):
    is_active = forms.BooleanField(
        label="Active",
        required=False,
        help_text="Clear to stop offering this item. It stays on tickets that already include it.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["is_active"].initial = self.instance.is_active


class FormPresetForm(StyledForm, forms.Form):
    presets = forms.MultipleChoiceField(
        label="Laboratory types",
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="“All laboratories” is always loaded. Forms whose ticket category is not set "
        "up are skipped; load the category presets first.",
    )

    def __init__(self, *args, **kwargs):
        from .presets import ALWAYS, FORM_PRESETS

        super().__init__(*args, **kwargs)
        self.fields["presets"].choices = [
            (key, preset.name) for key, preset in FORM_PRESETS.items() if key != ALWAYS
        ]

    def clean_presets(self):
        from .presets import ALWAYS

        return [ALWAYS, *self.cleaned_data["presets"]]


class PortalRaiseForm(ValuesMixin, PortalRaiseBase):
    """Raise a request with one template in the portal: the template's client fields, then the
    common fields of the generic raise form (desk.portal_forms.RaiseRequestForm) less the
    category, which the template sets, and the follow-up. A subject and a description are
    optional here: the template's label and a fixed sentence stand in."""

    def __init__(self, template, *args, user, initial_values=None, definition=None, **kwargs):
        super().__init__(*args, user=user, **kwargs)
        for name in ("category", "follow_up"):
            self.fields.pop(name, None)
        self.add_value_fields(template, user, initial_values, defn=definition)
        # From the reading the page signs (self.template), never the caller's earlier read: the
        # label is also the subject an empty one becomes.
        self.fields["subject"].required = False
        self.fields[
            "subject"
        ].help_text = f"A few words you will recognize. Empty: “{self.template.portal_label}”."
        self.fields["description"].required = False
        self.fields["description"].label = "Anything else the laboratory should know"
        self.fields["description"].help_text = ""

    def site_fields(self):
        return [self["site"]] if "site" in self.fields else []

    def common_fields(self):
        """The generic raise form's own visible fields, in its order, after the form's fields:
        reference, subject, description, urgent, and any field that form gains later (an
        upload, say), so it appears here without a change to this form."""
        own = {field_name(f.key) for f in self.template_fields} | {"site", NOT_KNOWN}
        return [field for field in self.visible_fields() if field.name not in own]


class CopyForm(_KeyMixin, StyledForm, forms.Form):
    name = forms.CharField(
        label="Name of the copy",
        max_length=120,
        help_text="The copy starts with every field of the original and is not on the service "
        "menu. Its client label starts empty.",
    )
    key = forms.SlugField(max_length=60, required=False, help_text=KEY_HELP)
