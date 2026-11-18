"""Standard request forms (F7): templates with their fields and the catalogs they order from, for
each laboratory-type preset of the request taxonomy (taxonomy.presets), keyed the same way.

Loading (`load_form_presets`) runs after the category presets: a template is created only when
its category key exists, and is offered in the portal only when that category is one clients may
raise. It is additive and idempotent, keyed by stable keys: templates by key, their fields by key
within the template, catalogs by key, catalog items by name within the catalog. Something already
there is left exactly as it is; a preset may add fields to a template another preset created.

A laboratory can rename, edit or deactivate the standard forms.
No preset field holding patient information is active: such fields ship inactive, for a
laboratory that decides it needs them to switch on (tests enforce this).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from django.db import connection, transaction
from django.db.models import F as Col

from core.audit import record_event, snapshot

from .models import Audience, Catalog, CatalogItem, FieldKind, RequestTemplate, TemplateField

K = FieldKind
CLIENT, SHOWN, STAFF = Audience.CLIENT, Audience.SHOWN, Audience.STAFF
_PRESET_LOCK = 0x4C43_5246  # "LCRF": one form preset load at a time
# Fields that name one order, sample, report or occasion: "Request again" leaves them blank
# (copy_on_repeat off). Dates, times, confirmations and patient fields are never copied anyway.
ONE_OCCASION_KEYS = frozenset(
    {
        "order-reference",
        "sample",
        "new-order",
        "report",
        "invoice",
        "amount",
        "affected",
        "example",
        "case-number",
        "form-number",
        "document-reference",
        "lot",
        "certificate",
        "shipment-reference",
        "request-reference",
    }
)


@dataclass(frozen=True)
class PresetField:
    key: str
    label: str
    kind: str
    required: bool = False
    help_text: str = ""
    choices: tuple[str, ...] = ()
    catalog: str = ""  # catalog key, for an item list
    audience: str = CLIENT
    patient: bool = False  # holds patient information: then always active=False in a preset
    active: bool = True
    whole_number: bool = True
    min_value: int | None = None
    max_value: int | None = None


@dataclass(frozen=True)
class PresetTemplate:
    key: str
    name: str
    category: str  # taxonomy category key
    fields: tuple[PresetField, ...] = ()
    client_label: str = ""
    description: str = ""
    instructions: str = ""
    icon: str = "file-earmark-text"
    featured: bool = False
    offered_in_portal: bool = True


@dataclass(frozen=True)
class FieldAddition:
    """Fields a laboratory-type preset adds to a template another preset defines."""

    template: str
    fields: tuple[PresetField, ...]


@dataclass(frozen=True)
class PresetItem:
    name: str
    unit: str = ""
    search_words: str = ""
    max_quantity: int | None = None


@dataclass(frozen=True)
class PresetCatalog:
    key: str
    name: str
    description: str = ""
    items: tuple[PresetItem, ...] = ()


@dataclass(frozen=True)
class FormPreset:
    key: str  # a taxonomy.presets key
    name: str
    templates: tuple[PresetTemplate, ...] = ()
    additions: tuple[FieldAddition, ...] = ()
    catalogs: tuple[PresetCatalog, ...] = ()


F = PresetField
T = PresetTemplate
I = PresetItem  # noqa: E741

# Fields several templates share, defined once.
REFERENCE = F(
    "order-reference",
    "Order, requisition or sample number",
    K.SHORT_TEXT,
    required=True,
    help_text="The number on your order, requisition, sample label or report.",
)
PATIENT_IDENTIFIER = F(
    "patient-identifier",
    "Patient identifier",
    K.SHORT_TEXT,
    help_text="The identifier the laboratory uses to find the patient's order. Not a name or date "
    "of birth.",
    patient=True,
    active=False,
)
DETAILS = F("details", "Anything else the laboratory should know", K.LONG_TEXT)
HANDLING = ("Room temperature", "Refrigerated", "Frozen", "Dry ice")

ALL = FormPreset(
    "all",
    "All laboratories",
    catalogs=(
        PresetCatalog(
            "supplies",
            "Supplies",
            "Collection and shipping supplies clients order. Add the laboratory's own items "
            "and item codes.",
            (
                I("Specimen transport bags", "pack of 100", "biohazard bag, zip bag"),
                I("Shipping boxes", "each", "mailer, box"),
                I("Insulated shipper with cold packs", "each", "cooler, styrofoam"),
                I("Requisition forms", "pad of 50", "order forms, test request forms"),
                I("Sample labels", "roll of 500", "barcode labels, stickers"),
            ),
        ),
    ),
    templates=(
        T(
            "pickup",
            "Pickup",
            "pickup",
            client_label="Request a pickup",
            description="A courier collects samples from your site.",
            icon="clock-history",
            featured=True,
            fields=(
                F(
                    "ready-at",
                    "Ready for pickup at",
                    K.DATETIME,
                    required=True,
                    help_text="When the samples will be packed and ready.",
                ),
                F(
                    "container-count",
                    "Number of samples, boxes or coolers",
                    K.NUMBER,
                    required=True,
                    min_value=1,
                    max_value=999,
                ),
                F("handling", "Storage while waiting", K.MULTIPLE_CHOICE, choices=HANDLING),
                F(
                    "site-contact",
                    "Person and phone at the pickup point",
                    K.SHORT_TEXT,
                    required=True,
                ),
                F(
                    "access-notes",
                    "Where to collect and how to get in",
                    K.LONG_TEXT,
                    help_text="Door, desk, lockbox or opening hours.",
                ),
                F("courier", "Courier or route", K.SHORT_TEXT, audience=STAFF),
                F("picked-up-at", "Picked up at", K.DATETIME, audience=SHOWN),
            ),
        ),
        T(
            "supply-order",
            "Supply order",
            "supply-order",
            client_label="Order supplies",
            description="Collection and shipping supplies delivered to your site.",
            icon="archive",
            featured=True,
            fields=(
                F("items", "Items", K.ITEMS, required=True, catalog="supplies"),
                F("needed-by", "Needed by", K.DATE),
                F("delivery-notes", "Delivery notes", K.LONG_TEXT),
                F("shipped-on", "Shipped on", K.DATE, audience=SHOWN),
                F("tracking", "Tracking number", K.SHORT_TEXT, audience=SHOWN),
            ),
        ),
        T(
            "result-status",
            "Result status",
            "result-status",
            client_label="Ask about a result",
            description="An order, sample or result you are waiting for.",
            icon="search",
            featured=True,
            fields=(
                F(
                    "problem",
                    "What is the problem?",
                    K.CHOICE,
                    required=True,
                    choices=(
                        "The order was not acknowledged",
                        "The sample was not received",
                        "The result is taking longer than expected",
                        "The result was released but we did not receive it",
                        "Something else",
                    ),
                ),
                REFERENCE,
                F("collected-on", "Collection date", K.DATE),
                F("tests", "Tests or analyses", K.SHORT_TEXT),
                DETAILS,
                F(
                    "checked-in-system",
                    "Checked in the laboratory system",
                    K.YES_NO,
                    audience=STAFF,
                ),
            ),
        ),
        T(
            "result-question",
            "Result or report question",
            "result-question",
            client_label="Question about a result or report",
            description="Explain a result, a flag or something that looks wrong.",
            icon="info-circle",
            fields=(
                F(
                    "question-kind",
                    "What kind of question?",
                    K.CHOICE,
                    required=True,
                    choices=(
                        "Please explain a result",
                        "A result may be wrong",
                        "The report did not arrive or cannot be opened",
                    ),
                ),
                REFERENCE,
                F("test", "Test or analyte", K.SHORT_TEXT, required=True),
                F("question", "Your question", K.LONG_TEXT, required=True),
                F(
                    "needs-consult",
                    "Needs a consult by a qualified person",
                    K.YES_NO,
                    audience=STAFF,
                ),
            ),
        ),
        T(
            "add-on",
            "Add-on or change to an order",
            "add-on-cancellation",
            client_label="Add a test or change an order",
            description="Add, cancel or change tests on a sample already sent.",
            icon="plus-lg",
            instructions="Sending this ticket does not approve the change. The laboratory "
            "checks the sample and its records and tells you what it decided.",
            fields=(
                F(
                    "change",
                    "What change?",
                    K.CHOICE,
                    required=True,
                    choices=("Add a test", "Cancel a test", "Change a method or scope"),
                ),
                REFERENCE,
                F(
                    "sample",
                    "Sample ID or collection date",
                    K.SHORT_TEXT,
                    required=True,
                    help_text="So the laboratory can find the sample.",
                ),
                F("tests", "Tests or methods", K.LONG_TEXT, required=True),
                F(
                    "new-order-placed",
                    "Have you placed a new order for it?",
                    K.YES_NO,
                    required=True,
                ),
                F("new-order", "New order number", K.SHORT_TEXT),
                F(
                    "requested-by",
                    "Requested by (name and role)",
                    K.SHORT_TEXT,
                    required=True,
                ),
                F("reason", "Reason", K.LONG_TEXT),
                F(
                    "sample-eligible",
                    "Sample eligible (volume, stability, holding time)",
                    K.CHOICE,
                    audience=STAFF,
                    choices=("Eligible", "Not eligible", "Not checked yet"),
                ),
            ),
        ),
        T(
            "sample-problem",
            "Sample problem or recollection",
            "sample-problem",
            client_label="Report a sample problem",
            description="A sample that is missing, damaged, mislabeled or needs recollection.",
            icon="exclamation-triangle",
            fields=(
                F(
                    "problem",
                    "What is wrong?",
                    K.MULTIPLE_CHOICE,
                    required=True,
                    choices=(
                        "Missing",
                        "Mislabeled",
                        "Damaged or leaking",
                        "Wrong container or preservative",
                        "Not enough sample",
                        "Temperature or holding time",
                    ),
                ),
                F("sample", "Sample ID or accession number", K.SHORT_TEXT, required=True),
                F("collected-at", "Collected at", K.DATETIME),
                F("replacement", "Will you send a replacement?", K.YES_NO),
                F("recollect-on", "Recollection date", K.DATE),
                DETAILS,
                F(
                    "decision",
                    "Laboratory decision",
                    K.CHOICE,
                    audience=SHOWN,
                    choices=(
                        "Tested as received",
                        "Tested with a comment",
                        "Rejected",
                        "Recollect",
                    ),
                ),
            ),
        ),
        T(
            "report-request",
            "Report copy, correction or reissue",
            "report-amendment",
            client_label="Report copy or correction",
            description="Resend a report, or ask for a correction or reissue.",
            icon="file-earmark-text",
            fields=(
                F(
                    "request-kind",
                    "What do you need?",
                    K.CHOICE,
                    required=True,
                    choices=("A copy or resend", "A correction", "An addendum", "A reissue"),
                ),
                F("report", "Report number and version", K.SHORT_TEXT, required=True),
                F(
                    "send-to",
                    "Where to send it",
                    K.SHORT_TEXT,
                    required=True,
                    help_text="A destination the laboratory has on file for your organization.",
                ),
                F("what-is-wrong", "What is wrong or missing", K.LONG_TEXT),
            ),
        ),
        T(
            "billing-question",
            "Billing question",
            "billing-question",
            client_label="Invoice or billing question",
            description="An invoice, statement, purchase order or charge.",
            icon="tag",
            fields=(
                F(
                    "question-kind",
                    "What is it about?",
                    K.CHOICE,
                    required=True,
                    choices=(
                        "An invoice",
                        "A statement",
                        "A charge we dispute",
                        "A purchase order",
                    ),
                ),
                F("invoice", "Invoice, statement or PO number", K.SHORT_TEXT, required=True),
                F("amount", "Amount in question", K.SHORT_TEXT),
                F("details", "Details", K.LONG_TEXT, required=True),
            ),
        ),
        T(
            "service-information",
            "Test or collection information",
            "service-information",
            client_label="Test or collection question",
            description="Which sample to collect, how to prepare it, turnaround or limits.",
            icon="journal-text",
            fields=(
                F("test", "Test, method or panel", K.SHORT_TEXT, required=True),
                F(
                    "topic",
                    "What do you need to know?",
                    K.CHOICE,
                    required=True,
                    choices=(
                        "Sample type or container",
                        "Preparation or collection",
                        "Turnaround",
                        "Limits or cut-offs",
                        "Something else",
                    ),
                ),
                F("question", "Your question", K.LONG_TEXT, required=True),
            ),
        ),
        T(
            "turnaround-delay",
            "Turnaround delay",
            "turnaround-delay",
            client_label="Results are late",
            description="Results you were promised by a date have not arrived.",
            icon="clock-history",
            fields=(
                REFERENCE,
                F("promised-for", "When were the results expected?", K.DATETIME),
                F("impact", "How does the delay affect you?", K.LONG_TEXT),
                F("revised-for", "Revised expected time", K.DATETIME, audience=SHOWN),
            ),
        ),
        T(
            "complaint",
            "Complaint",
            "complaint",
            client_label="Make a complaint",
            description="Tell the laboratory about a service problem.",
            icon="megaphone",
            fields=(
                F("what-happened", "What happened?", K.LONG_TEXT, required=True),
                F("happened-on", "When did it happen?", K.DATE),
                F("affected", "Affected order, sample or report", K.SHORT_TEXT),
                F(
                    "response",
                    "How would you like the laboratory to respond?",
                    K.CHOICE,
                    choices=("Call me", "Reply here", "No response needed"),
                ),
            ),
        ),
        T(
            "technical-support",
            "Technical support",
            "report-delivery",
            client_label="Technical support",
            description="Electronic orders, results in your system, label printers, the portal.",
            icon="tools",
            fields=(
                F(
                    "problem",
                    "What is not working?",
                    K.CHOICE,
                    required=True,
                    choices=(
                        "Electronic orders fail",
                        "Results missing in our system",
                        "Label printer",
                        "Portal sign-in",
                        "Something else",
                    ),
                ),
                F("system", "Your system or printer", K.SHORT_TEXT),
                F(
                    "affected-count",
                    "How many orders or results are affected?",
                    K.NUMBER,
                    min_value=0,
                ),
                F("since", "Since when?", K.DATE),
                F(
                    "example",
                    "An example order or message number",
                    K.SHORT_TEXT,
                    help_text="A number, not patient details.",
                ),
                F("error", "Error message", K.LONG_TEXT),
                F(
                    "interface-reference",
                    "Interface or message reference",
                    K.SHORT_TEXT,
                    audience=STAFF,
                ),
            ),
        ),
        T(
            "account-change",
            "Account or contact change",
            "account-change",
            client_label="Update our account or contacts",
            description="Add or change a site, contact, provider or report destination.",
            icon="people",
            fields=(
                F(
                    "change",
                    "What should change?",
                    K.CHOICE,
                    required=True,
                    choices=(
                        "Add or change a contact",
                        "Add or change a site",
                        "Add or change a provider",
                        "Change where reports go",
                        "Remove someone or something",
                    ),
                ),
                F(
                    "details",
                    "Details",
                    K.LONG_TEXT,
                    required=True,
                    help_text="Names, roles, phone numbers, email or fax.",
                ),
                F("effective-on", "From when?", K.DATE),
                F("authorized-by", "Authorized by (name and role)", K.SHORT_TEXT, required=True),
                F("verified", "Authority verified", K.YES_NO, audience=STAFF),
            ),
        ),
    ),
)

# Laboratory-type presets are in presets_types.py (data only).
from .presets_types import TYPE_PRESETS  # noqa: E402

FORM_PRESETS = {preset.key: preset for preset in (ALL, *TYPE_PRESETS)}
ALWAYS = "all"


# Loading -------------------------------------------------------------------------------------


def _audit_subject():
    """record_event needs an object; a preset load acts on the catalogue, not on one row."""
    return SimpleNamespace(
        _meta=SimpleNamespace(label_lower="request_forms.preset"), pk="catalogue"
    )


def _created(actor, obj, action, preset):
    from .services import template_state

    record_event(
        actor=actor,
        action=action,
        obj=obj,
        after=template_state(obj) if isinstance(obj, RequestTemplate) else snapshot(obj),
        reason=f"Loaded from the ticket form preset “{preset.name}”.",
    )


def _next(queryset):
    last = queryset.order_by("-position").values_list("position", flat=True).first()
    return (last or 0) + 10


def _load_catalog(actor, preset, item, counts):
    catalog = Catalog.objects.filter(key=item.key).first()
    if catalog is None:
        catalog = Catalog.objects.filter(name__iexact=item.name).first()
    if catalog is None:
        catalog = Catalog.objects.create(
            key=item.key, name=item.name, description=item.description, preset=preset.key
        )
        _created(actor, catalog, "catalog.created", preset)
        counts["catalogs"] += 1
    # Items join under the catalog's lock, as in the setup services (request_forms.definition).
    catalog = Catalog.objects.select_for_update().get(pk=catalog.pk)
    names = {name.casefold() for name in catalog.items.values_list("name", flat=True)}
    for entry in item.items:
        if entry.name.casefold() in names:
            continue
        row = CatalogItem.objects.create(
            catalog=catalog,
            name=entry.name,
            unit=entry.unit,
            search_words=entry.search_words,
            max_quantity=entry.max_quantity,
            position=_next(catalog.items.all()),
        )
        names.add(entry.name.casefold())
        _created(actor, row, "catalog_item.created", preset)
        counts["items"] += 1


def _load_fields(actor, preset, template, fields, counts):
    # Fields join under the template's lock, as in the setup services (request_forms.definition).
    RequestTemplate.objects.select_for_update().get(pk=template.pk)
    existing = set(template.fields.values_list("key", flat=True))
    added = False
    for spec in fields:
        if spec.key in existing:
            continue
        catalog = Catalog.objects.filter(key=spec.catalog).first() if spec.catalog else None
        if spec.catalog and catalog is None:  # the laboratory keeps it under another key
            counts["fields_skipped"] += 1
            continue
        field = TemplateField.objects.create(
            template=template,
            key=spec.key,
            label=spec.label,
            help_text=spec.help_text,
            kind=spec.kind,
            required=spec.required or spec.kind == FieldKind.CONFIRMATION,
            choices=list(spec.choices),
            whole_number=spec.whole_number,
            min_value=spec.min_value,
            max_value=spec.max_value,
            catalog=catalog,
            audience=spec.audience,
            holds_patient_information=spec.patient,
            copy_on_repeat=spec.key not in ONE_OCCASION_KEYS and spec.kind != FieldKind.HEADING,
            is_active=spec.active and not spec.patient,
            position=_next(template.fields.all()),
        )
        existing.add(spec.key)
        _created(actor, field, "request_form_field.created", preset)
        counts["fields"] += 1
        added = True
    if added:  # a form opened before is refused at submission (services._template_changed)
        RequestTemplate.objects.filter(pk=template.pk).update(revision=Col("revision") + 1)


def _load_template(actor, preset, spec, counts):
    from taxonomy.models import RequestCategory

    template = RequestTemplate.objects.filter(key=spec.key).first()
    if template is None:
        category = RequestCategory.objects.filter(key=spec.category, parent__isnull=False).first()
        if category is None or RequestTemplate.objects.filter(name__iexact=spec.name).exists():
            counts["skipped"] += 1
            return
        portal = spec.offered_in_portal and category.client_may_raise
        template = RequestTemplate.objects.create(
            key=spec.key,
            name=spec.name,
            client_label=spec.client_label,
            description=spec.description,
            instructions=spec.instructions,
            icon=spec.icon,
            category=category,
            offered_in_portal=portal,
            featured=portal and spec.featured,
            position=_next(RequestTemplate.objects.all()),
            preset=preset.key,
        )
        _created(actor, template, "request_form.created", preset)
        counts["templates"] += 1
    _load_fields(actor, preset, template, spec.fields, counts)


@transaction.atomic
def load_form_presets(*, actor, keys=(ALWAYS,)):
    """Load request form presets additively, "All laboratories" first. Returns counts of created
    catalogs, items, templates and fields, of templates skipped because their category is not
    set up, and of fields skipped because their catalog is missing. Existing rows are left exactly
    as they are."""
    from .services import require_setup

    require_setup(actor)
    unknown = set(keys) - set(FORM_PRESETS)
    if unknown:
        raise ValueError(f"Unknown ticket form preset: {', '.join(sorted(unknown))}.")
    ordered = [ALWAYS] + [key for key in FORM_PRESETS if key in keys and key != ALWAYS]
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_PRESET_LOCK])
    # The forms, then the catalogs, as every capture locks them (request_forms.definition): taken
    # up front, so adding a field or an item later never takes them in the other order.
    list(RequestTemplate.objects.select_for_update().order_by("pk").values_list("pk", flat=True))
    list(Catalog.objects.select_for_update().order_by("pk").values_list("pk", flat=True))
    counts = {
        "catalogs": 0,
        "items": 0,
        "templates": 0,
        "fields": 0,
        "skipped": 0,
        "fields_skipped": 0,
    }
    for key in ordered:
        preset = FORM_PRESETS[key]
        for catalog in preset.catalogs:
            _load_catalog(actor, preset, catalog, counts)
        for template in preset.templates:
            _load_template(actor, preset, template, counts)
        for addition in preset.additions:
            template = RequestTemplate.objects.filter(key=addition.template).first()
            if template is not None:
                _load_fields(actor, preset, template, addition.fields, counts)
    if any(value for name, value in counts.items() if "skipped" not in name):
        record_event(
            actor=actor,
            action="request_form_preset.loaded",
            obj=_audit_subject(),
            after={"presets": ordered, **counts},
        )
    return counts
