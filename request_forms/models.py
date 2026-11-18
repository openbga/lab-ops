"""Ticket forms: the laboratory's own templates, their fields, and the values a ticket
was raised with (docs/product.md "Ticket forms", docs/architecture.md "Ticket forms").

A template (RequestTemplate) is a form for one kind of routine ticket. It files the ticket
under a category of the taxonomy; several templates may share one category (a routine and an
urgent pickup both report as "Pickup"). Its fields (TemplateField) say what the client or staff
fill. Item-list fields choose from a catalog the laboratory keeps (Catalog, CatalogItem).

A ticket raised with a template has one FilledForm and its FieldValue rows. Each value row
copies the field's key, label, kind, audience and patient-information flag at capture, so a
later rename, reorder or retirement of the field never changes what an old ticket shows.
Value rows are history: a correction adds a row and marks the previous one superseded, and a
database trigger refuses any other update and every delete (migration 0002).

Visibility is the stricter of the copy and the current field (request_forms.selectors): a value
reaches a client only when both say the client sees it, and is patient information when either
says so. Patient-information values never enter entries, subjects, email, lists, search, logs or
audit payloads.
"""

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Upper

# Icons a template may show on the portal menu: a curated subset of static/img/icons.svg
# (tests check that each exists there).
ICONS = (
    ("file-earmark-text", "Document"),
    ("inbox", "Tray"),
    ("archive", "Box"),
    ("calendar3", "Calendar"),
    ("clock-history", "Clock"),
    ("search", "Magnifier"),
    ("list-check", "Checklist"),
    ("journal-text", "Notebook"),
    ("tools", "Tools"),
    ("envelope", "Envelope"),
    ("tag", "Label"),
    ("exclamation-triangle", "Warning"),
    ("info-circle", "Information"),
    ("megaphone", "Megaphone"),
    ("file-earmark-arrow-up", "Document upload"),
    ("building", "Building"),
    ("people", "People"),
    ("plus-lg", "Plus"),
    ("arrow-counterclockwise", "Repeat"),
    ("check2", "Tick"),
    ("lock", "Lock"),
    ("key", "Key"),
    ("stars", "Stars"),
)
DEFAULT_ICON = "file-earmark-text"


class FieldKind(models.TextChoices):
    SHORT_TEXT = "short_text", "Short text"
    LONG_TEXT = "long_text", "Long text"
    NUMBER = "number", "Number"
    DATE = "date", "Date"
    DATETIME = "datetime", "Date and time"
    TIME = "time", "Time of day"
    CHOICE = "choice", "One choice from a list"
    MULTIPLE_CHOICE = "multiple_choice", "Several choices from a list"
    YES_NO = "yes_no", "Yes or no"
    CONFIRMATION = "confirmation", "Confirmation to tick"
    ITEMS = "items", "Items with quantities from a catalog"
    HEADING = "heading", "Section heading (asks nothing)"


CHOICE_KINDS = frozenset({FieldKind.CHOICE, FieldKind.MULTIPLE_CHOICE})
# A heading groups the fields after it on the form; it takes no value.
NO_VALUE_KINDS = frozenset({FieldKind.HEADING})
# "Request again" never copies these: a date or time belongs to one occasion, and a confirmation
# must be ticked again each time.
NEVER_REPEATED = frozenset(
    {FieldKind.DATE, FieldKind.DATETIME, FieldKind.TIME, FieldKind.CONFIRMATION, FieldKind.HEADING}
)


class Audience(models.TextChoices):
    CLIENT = "client", "Client fills it; staff see it"
    SHOWN = "shown", "Staff fill it; the client sees it"
    STAFF = "staff", "Staff only"


# Who sees a value: the client sees client and shown fields; only clients fill client fields.
CLIENT_SEES = frozenset({Audience.CLIENT, Audience.SHOWN})


class PatientReason(models.TextChoices):
    """Why a patient-information value was changed: a fixed list, so the audit history never
    holds free text that could repeat the value. A typed note stays on the value row."""

    TYPING = "typing", "Typing error"
    SOURCE = "source_record", "Corrected from the source record"
    CLIENT = "client_correction", "The client gave a correction"
    OTHER_PERSON = "other_person", "Belongs to another person"
    NOT_NEEDED = "not_needed", "Not needed on this ticket"


class Catalog(models.Model):
    """A list of things clients may order with an item-list field: supplies, bottle kits."""

    key = models.SlugField(max_length=60, unique=True, help_text="Stable code; never changes.")
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=300, blank=True)
    preset = models.CharField(max_length=40, blank=True)
    is_active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["name", "pk"]
        constraints = [
            models.UniqueConstraint(
                Upper("name"),
                name="catalog_name_unique",
                violation_error_message="A catalog with this name exists.",
            )
        ]

    def __str__(self):
        return self.name


class CatalogItem(models.Model):
    catalog = models.ForeignKey(Catalog, on_delete=models.PROTECT, related_name="items")
    code = models.CharField(
        max_length=40, blank=True, help_text="The laboratory's item number, if it has one."
    )
    name = models.CharField(max_length=160)
    unit = models.CharField(
        max_length=60, blank=True, help_text="What one counts as: each, box of 50, case of 12."
    )
    search_words = models.CharField(
        max_length=300,
        blank=True,
        help_text="Other words clients use for it (a cap color, a common name), shown under its "
        "name in the form.",
    )
    max_quantity = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text="The most a client may order at once. Empty: no limit.",
    )
    position = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["position", "name", "pk"]
        constraints = [
            models.UniqueConstraint(
                F("catalog"),
                Upper("code"),
                condition=~Q(code=""),
                name="catalog_item_code_unique",
                violation_error_message="This catalog already has an item with this code.",
            ),
            models.CheckConstraint(
                condition=Q(max_quantity__isnull=True) | Q(max_quantity__gte=1),
                name="catalog_item_max_quantity",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})" if self.code else self.name


class RequestTemplate(models.Model):
    key = models.SlugField(
        max_length=60, unique=True, help_text="Stable code; never changes after creation."
    )
    name = models.CharField(max_length=120, help_text="What staff see.")
    client_label = models.CharField(
        max_length=120, blank=True, help_text="What clients see. Empty: the staff name."
    )
    description = models.CharField(
        max_length=160,
        blank=True,
        help_text="One short line under the label on the portal menu.",
    )
    instructions = models.TextField(
        blank=True, help_text="Shown at the top of the form, to clients and staff."
    )
    icon = models.CharField(max_length=40, choices=ICONS, default=DEFAULT_ICON)
    category = models.ForeignKey(
        "taxonomy.RequestCategory",
        on_delete=models.PROTECT,
        related_name="request_templates",
        help_text="Tickets raised with this form are filed under this category.",
    )
    offered_in_portal = models.BooleanField(
        default=True, help_text="Clients may raise tickets with it in the portal."
    )
    clients = models.ManyToManyField(
        "clients.Client",
        blank=True,
        related_name="request_templates",
        verbose_name="client organizations",
        help_text="Offered only to these organizations. Empty: every client organization.",
    )
    featured = models.BooleanField(
        default=False, help_text="Shown on the portal home's service menu."
    )
    position = models.PositiveIntegerField(default=0)
    preset = models.CharField(max_length=40, blank=True)
    is_active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["position", "name", "pk"]
        verbose_name = "ticket form"
        constraints = [
            models.UniqueConstraint(
                Upper("name"),
                name="request_template_name_unique",
                violation_error_message="A ticket form with this name exists.",
            )
        ]

    def __str__(self):
        return self.name

    @property
    def portal_label(self):
        return self.client_label or self.name


class TemplateField(models.Model):
    template = models.ForeignKey(RequestTemplate, on_delete=models.PROTECT, related_name="fields")
    key = models.SlugField(max_length=60, help_text="Stable code within the form.")
    label = models.CharField(max_length=160)
    help_text = models.CharField(max_length=300, blank=True)
    kind = models.CharField(max_length=20, choices=FieldKind.choices)
    required = models.BooleanField(default=False)
    choices = ArrayField(models.CharField(max_length=120), default=list, blank=True, size=40)
    whole_number = models.BooleanField(default=True)
    min_value = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    max_value = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    catalog = models.ForeignKey(
        Catalog, null=True, blank=True, on_delete=models.PROTECT, related_name="fields"
    )
    audience = models.CharField(max_length=10, choices=Audience.choices, default=Audience.CLIENT)
    holds_patient_information = models.BooleanField(default=False)
    copy_on_repeat = models.BooleanField(
        default=True,
        help_text="Copied into a new ticket when a client chooses Request again. Clear it for "
        "a number that belongs to one order or sample.",
    )
    position = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["position", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["template", "key"], name="template_field_key_unique"),
            models.CheckConstraint(
                condition=Q(kind=FieldKind.ITEMS, catalog__isnull=False)
                | (~Q(kind=FieldKind.ITEMS) & Q(catalog__isnull=True)),
                name="template_field_catalog",
                violation_error_message="An item list takes a catalog; other fields take none.",
            ),
            models.CheckConstraint(
                condition=Q(min_value__isnull=True)
                | Q(max_value__isnull=True)
                | Q(min_value__lte=F("max_value")),
                name="template_field_bounds",
                violation_error_message="The lowest value must not exceed the highest.",
            ),
        ]

    def __str__(self):
        return self.label


class FilledForm(models.Model):
    """The form a request was raised with. Copies the template's names at that time."""

    request = models.OneToOneField(
        "desk.ServiceRequest", on_delete=models.PROTECT, related_name="filled_form"
    )
    template = models.ForeignKey(
        RequestTemplate, on_delete=models.PROTECT, related_name="filled_forms"
    )
    template_key = models.CharField(max_length=60)
    template_name = models.CharField(max_length=120)
    template_client_label = models.CharField(max_length=120)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.template_name} on {self.request}"


class FieldValue(models.Model):
    """One value of a filled form, with copies of its field's definition at capture.

    `value` is JSON: text and numbers as strings, dates `YYYY-MM-DD`, times `HH:MM`, choices as
    the chosen text (a list for several), yes or no and confirmations as booleans, items as a
    list of {item, code, name, unit, quantity}. A null value records that a correction cleared
    the field. The current value of a field is its row with no `superseded_at`."""

    filled_form = models.ForeignKey(FilledForm, on_delete=models.PROTECT, related_name="values")
    field = models.ForeignKey(TemplateField, on_delete=models.PROTECT, related_name="values")
    key = models.CharField(max_length=60)
    label = models.CharField(max_length=160)
    kind = models.CharField(max_length=20, choices=FieldKind.choices)
    audience = models.CharField(max_length=10, choices=Audience.choices)
    holds_patient_information = models.BooleanField()
    position = models.PositiveIntegerField()
    value = models.JSONField(null=True)
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    entered_at = models.DateTimeField(auto_now_add=True)
    replaces = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="replaced_by"
    )
    reason = models.TextField(
        blank=True,
        help_text="Why a correction changed the value. For patient information: a note seen "
        "only by staff who can view patient information.",
    )
    reason_code = models.CharField(
        max_length=30,
        choices=PatientReason.choices,
        blank=True,
        help_text="For patient information: why it changed, from a fixed list.",
    )
    superseded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["position", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["filled_form", "field"],
                condition=Q(superseded_at__isnull=True),
                name="field_value_one_current",
            ),
        ]

    def __str__(self):
        return f"{self.label} on {self.filled_form.request}"
