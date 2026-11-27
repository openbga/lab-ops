"""Reads of request forms: which templates an account may use, which fields it may fill, and the
values of a request as staff and as a client may see them.

Visibility of a stored value is the stricter of its copy and the current field:

- a client sees a value only when the copy and the current field both say the client sees it,
  and only on a request `desk.portal_scope.portal_requests` gives the account;
- a value is patient information when the copy or the current field says so; staff without
  `view_patient_information` get a placeholder, never the value, and staff with it reveal it
  one value at a time (request_forms.request_views.reveal_value, audited).
"""

from dataclasses import dataclass, field

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import Exists, OuterRef, Q

from accounts.roles import ROLES, has_capability, has_role

from . import kinds
from .models import (
    CLIENT_SEES,
    NO_VALUE_KINDS,
    Audience,
    FieldKind,
    FieldValue,
    RequestTemplate,
    TemplateField,
)

PATIENT_CAPABILITY = "view_patient_information"
WIDE_KINDS = frozenset({FieldKind.LONG_TEXT, FieldKind.ITEMS, FieldKind.MULTIPLE_CHOICE})


def is_portal_account(user):
    return user.is_authenticated and user.client_id is not None


def may_see_patient_information(user):
    """The capability, on every request the account may read. Emergency access gives it on one
    request only: ask `sees_patient_information` about a request."""
    return (
        user.is_authenticated
        and user.client_id is None
        and has_role(user, *ROLES)
        and has_capability(user, PATIENT_CAPABILITY)
    )


def sees_patient_information(user, request_id):
    """Whether this staff account sees the patient information on request `request_id` (which the
    caller has decided it may read): the capability, or emergency access in force there."""
    from desk.emergency import sees_patient_information as sees

    return sees(user, request_id)


_USABLE = Q(
    is_active=True,
    category__is_active=True,
    category__parent__isnull=False,
    category__parent__is_active=True,
)


def portal_templates(user):
    """Templates this portal account may raise a request with, in the laboratory's order."""
    if not is_portal_account(user) or not user.is_active:
        return RequestTemplate.objects.none()
    offered = RequestTemplate.objects.filter(
        _USABLE, offered_in_portal=True, category__client_may_raise=True
    )
    # Offered to every organization (no organizations listed), or to this account's own.
    everyone = offered.filter(clients__isnull=True).values("pk")
    listed = offered.filter(clients=user.client_id).values("pk")
    return (
        RequestTemplate.objects.filter(Q(pk__in=everyone) | Q(pk__in=listed))
        .select_related("category")
        .order_by("position", "name", "pk")
    )


def portal_menu(user):
    """The portal home's service menu: the featured templates this account may use."""
    return portal_templates(user).filter(featured=True)


def staff_templates(category=None, client=None):
    """Active templates for staff intake, optionally only those filing under `category`, and
    only those offered to `client` (a Client or its id) when one is given. Each carries
    `offered_to_some`: offered to selected organizations only."""
    listed = RequestTemplate.clients.through.objects.filter(requesttemplate_id=OuterRef("pk"))
    templates = (
        RequestTemplate.objects.filter(_USABLE)
        .select_related("category")
        .annotate(offered_to_some=Exists(listed))
    )
    if category is not None:
        templates = templates.filter(category=category)
    if client is not None:
        client_id = getattr(client, "pk", client)
        templates = templates.filter(
            Q(offered_to_some=False) | Exists(listed.filter(client_id=client_id))
        )
    return templates.order_by("position", "name", "pk")


def _value_fields(template, *, headings=False, new_request=True):
    fields = template.fields.filter(is_active=True).select_related("catalog")
    if not headings:
        fields = fields.exclude(kind__in=NO_VALUE_KINDS)
    if new_request:
        # An item list whose catalog is retired takes no new orders (F4): it is left off new
        # requests; a correction still shows the lines a request already holds.
        fields = fields.exclude(kind=FieldKind.ITEMS, catalog__is_active=False)
    return fields


def fillable_fields(template, user, *, headings=False, new_request=True):
    """The active fields of `template` this account may fill, in order: client fields for a
    portal account; every field for staff, less patient-information fields for staff without
    the capability. Section headings (which take no value) only with `headings=True`. On a new
    request (`new_request`), an item list whose catalog is retired is left out."""
    fields = _value_fields(template, headings=headings, new_request=new_request)
    if is_portal_account(user):
        fields = fields.filter(audience=Audience.CLIENT)
    elif not may_see_patient_information(user):
        fields = fields.filter(holds_patient_information=False)
    return list(fields.order_by("position", "pk"))


def hidden_required(template, user):
    """Required fields of a new request that this staff account cannot fill because they hold
    patient information and it lacks the capability. Such staff log the request with "Some
    required details are not known yet" ticked; a colleague with the capability adds them."""
    if is_portal_account(user) or may_see_patient_information(user):
        return []
    return list(
        _value_fields(template)
        .filter(required=True, holds_patient_information=True)
        .order_by("position", "pk")
    )


def current_values(filled_form):
    """The current value row of each field, keyed by field id."""
    rows = filled_form.values.filter(superseded_at__isnull=True).select_related("field")
    return {row.field_id: row for row in rows}


MAX_KEPT_ROWS = 100  # item-list value rows a correction page may name (one per item list)


def kept_item_values(filled_form, rows=None):
    """The immutable item-list values shown on a correction page, scoped to this ticket's
    form. Without a page's row IDs, use the current item lists."""
    found = filled_form.values.filter(kind=FieldKind.ITEMS)
    if rows is None:
        found = found.filter(superseded_at__isnull=True)
    else:
        found = found.filter(pk__in=list(rows)[:MAX_KEPT_ROWS])
    return found.only("field_id", "kind", "value", "holds_patient_information")


@dataclass
class ValueRow:
    label: str
    lines: list = field(default_factory=list)
    hidden: bool = False  # patient information this person may not see
    reveal_id: int | None = None  # patient information this person may reveal (audited)
    staff_only: bool = False
    patient: bool = False
    corrected: bool = False
    retired: bool = False  # the field is no longer on the form
    wide: bool = False  # long text or a list: the whole row
    value_id: int | None = None  # the value row shown (a portal row; core.access records it)


@dataclass
class FormDetails:
    template_label: str
    rows: list
    missing: list = field(default_factory=list)  # labels of required fields with no value
    can_edit: bool = False


def _patient(row):
    return row.holds_patient_information or row.field.holds_patient_information


def _client_visible(row):
    return row.audience in CLIENT_SEES and row.field.audience in CLIENT_SEES


def staff_values(service_request, user, *, defn=None, current=None):
    """What staff see of a request's form, or None. The caller has already authorized the
    request; this checks again through desk.selectors.staff_requests. A page that captures
    values passes the reading it is drawn from: `defn` (its definition) and `current` (its
    current value rows, by field id), so what it says is missing describes the fields it shows."""
    from desk.selectors import staff_requests

    if is_portal_account(user) or not has_role(user, *ROLES):
        return None
    filled = _filled(service_request)
    if filled is None or not staff_requests(user).filter(pk=service_request.pk).exists():
        return None
    # The capability, or emergency access in force on this ticket (desk.emergency).
    sees_patient = sees_patient_information(user, service_request.pk)
    values = current if current is not None else current_values(filled)
    rows = []
    for row in sorted(values.values(), key=lambda row: (row.position, row.pk)):
        lines = kinds.display(row.kind, row.value)
        if not lines:
            continue
        patient = _patient(row)
        rows.append(
            ValueRow(
                # Patient information is never on the page: holders of the capability reveal it
                # (POST, audited, no-store), as person-level link keys are revealed.
                label=row.label,
                lines=[] if patient else lines,
                hidden=patient and not sees_patient,
                reveal_id=row.pk if patient and sees_patient else None,
                staff_only=not _client_visible(row),
                patient=patient,
                # Without the capability the row is the placeholder and nothing else.
                corrected=row.replaces_id is not None and not (patient and not sees_patient),
                retired=not row.field.is_active,
                wide=row.kind in WIDE_KINDS,
            )
        )
    # Required fields still empty, by label, those this person cannot see included (a patient-
    # information field reads as needed, never with a value). An item list whose catalog is
    # retired is no longer asked for.
    required = (
        [template_field for template_field in defn.value_fields() if template_field.required]
        if defn is not None
        else TemplateField.objects.filter(
            template_id=filled.template_id, is_active=True, required=True
        )
        .exclude(kind__in=NO_VALUE_KINDS)
        .exclude(kind=FieldKind.ITEMS, catalog__is_active=False)
        .order_by("position", "pk")
    )
    missing = [
        template_field.label
        for template_field in required
        if not kinds.display(
            template_field.kind, getattr(values.get(template_field.pk), "value", None)
        )
    ]
    return FormDetails(
        template_label=filled.template_name,
        rows=rows,
        missing=missing,
        # A closed request's form values are frozen, as its links are.
        can_edit=not _closed(service_request),
    )


def _closed(service_request):
    from desk.models import Status

    return service_request.status == Status.CLOSED


def client_values(service_request, user):
    """What a portal account sees of a request's form, or None: client-visible values only,
    and only on a request within desk.portal_scope.portal_requests."""
    from desk.portal_scope import portal_requests

    filled = _filled(service_request)
    if filled is None or not portal_requests(user).filter(pk=service_request.pk).exists():
        return None
    rows = []
    for row in sorted(current_values(filled).values(), key=lambda row: (row.position, row.pk)):
        if not _client_visible(row):
            continue
        lines = kinds.display(row.kind, row.value)
        if lines:
            rows.append(
                ValueRow(
                    label=row.label,
                    lines=lines,
                    wide=row.kind in WIDE_KINDS,
                    # A client sees its own patient information on its tickets; each such answer
                    # shown is an access record (request_forms.templatetags.request_form_details).
                    patient=_patient(row),
                    value_id=row.pk,
                )
            )
    return FormDetails(template_label=filled.template_client_label, rows=rows)


def _filled(service_request):
    from .models import FilledForm

    return (
        FilledForm.objects.filter(request_id=service_request.pk)
        .only("pk", "template_id", "template_name", "template_client_label")
        .first()
    )


def value_history(filled_form):
    """Every value row, current and superseded, oldest first (for the correction page)."""
    return FieldValue.objects.filter(filled_form=filled_form).order_by("entered_at", "pk")


def repeat_values(template, user, number, defn=None):
    """What "Request again" copies from this portal account's earlier request `number`, raised
    with the same form: ({field key: initial form value}, site or None), or None when the
    account cannot see such a request (repeat_from, without the request it was copied from)."""
    found = repeat_from(template, user, number, defn=defn)
    return None if found is None else found[:2]


def repeat_from(template, user, number, defn=None):
    """repeat_values, with the earlier request the values were copied from:
    ({field key: initial form value}, site or None, request), or None.

    Copied: the current values of client fields still on the form that the laboratory lets be
    copied, and only as the form still accepts them (an option or item no longer offered is
    dropped). Copying follows the display rule: only a value the client may see on the earlier
    request (its captured audience and the field's current one both let the client see it).
    Never copied: patient information (marked at capture or now), dates, times, confirmations,
    and fields marked as belonging to one order or sample. The site is copied while it is still
    in the account's scope; subject, description, reference and the urgent box are not.

    `defn`: the definition the page is drawn from (request_forms.definition.read), so what is
    copied follows the same fields and items as the form it fills.
    """
    from desk.portal_scope import portal_requests, portal_sites

    from . import definition
    from .models import NEVER_REPEATED

    earlier = (
        portal_requests(user)
        .filter(number=number, filled_form__template=template)
        .select_related("site")
        .first()
    )
    if earlier is None:
        return None
    defn = defn or definition.read(template)
    fields = {
        field.pk: field
        for field in defn.fillable(user)
        if field.copy_on_repeat
        and field.kind not in NEVER_REPEATED
        and not field.holds_patient_information
    }
    values = {}
    for row in current_values(earlier.filled_form).values():
        field = fields.get(row.field_id)
        if field is None or row.value is None or _patient(row) or not _client_visible(row):
            continue
        value = row.value
        items = None
        if field.kind == FieldKind.ITEMS:
            items = {item.pk: item for item in defn.orderable_items(field)}
            value = {
                entry["item"]: entry["quantity"]
                for entry in value
                if entry["item"] in items
                and (
                    not items[entry["item"]].max_quantity
                    or entry["quantity"] <= items[entry["item"]].max_quantity
                )
            }
        elif field.kind == FieldKind.MULTIPLE_CHOICE:
            value = [choice for choice in value if choice in field.choices]
        try:
            stored = kinds.normalize(field, value, items=items)
        except ValidationError:
            continue
        if stored is not None:
            values[field.key] = kinds.initial_for_form(field.kind, stored)
    site = earlier.site
    if site is not None and not portal_sites(user).filter(pk=site.pk).exists():
        site = None
    return values, site, earlier


def revealable_value(user, value_id):
    """The current patient-information value row `value_id` on a request this staff account may
    read, for an audited reveal; None when it is not one (another request, not patient
    information, superseded, neither the capability nor emergency access there). `revealable`
    also says what lets the account see it."""
    return revealable(user, value_id)[0]


def revealable(user, value_id):
    """(row, grant): `revealable_value`'s row and what lets this account see the patient
    information on its request (desk.emergency.patient_grant: the capability, or emergency access
    in force there); (None, None) when the row is not revealable to it."""
    from desk.emergency import patient_grant
    from desk.selectors import staff_requests

    row = (
        FieldValue.objects.filter(pk=value_id, superseded_at__isnull=True)
        .select_related("field", "filled_form__request")
        .first()
    )
    if row is None or not _patient(row):
        return None, None
    if not staff_requests(user).filter(pk=row.filled_form.request_id).exists():
        return None, None
    grant = patient_grant(user, row.filled_form.request_id)
    if grant is None:
        return None, None
    return row, grant


def kept_eligible_fields(template_id, user_id):
    """For keeping what was typed across a sign-out (accounts.kept): the form field names a new
    request form of this template would draw for this account as it stands now (definition.read,
    its fillable fields and an item list's orderable items, as the portal page draws them), less
    those that hold patient information, and for staff the box "Some required details are not
    known yet" when the form would draw it now (forms.ValuesMixin). Read with the definition's own
    share locks, which a change of the form takes first, so a change under way finishes before
    this reads. None when there is no such template or account."""
    from django.contrib.auth import get_user_model

    from . import definition
    from .forms import NOT_KNOWN, field_name

    template = RequestTemplate.objects.filter(pk=template_id).first()
    user = get_user_model().objects.filter(pk=user_id).first()
    if template is None or user is None:
        return None
    try:
        defn = definition.read(template)
    except ObjectDoesNotExist:
        return None  # removed while this waited for its lock
    names = []
    fillable = defn.fillable(user, new_request=True)
    if not is_portal_account(user) and (
        any(f.required for f in fillable) or defn.hidden_required(user)
    ):
        names.append(NOT_KNOWN)
    for template_field in fillable:
        if template_field.kind in NO_VALUE_KINDS or template_field.holds_patient_information:
            continue
        name = field_name(template_field.key)
        if template_field.kind == FieldKind.ITEMS:
            items = defn.orderable_items(template_field)
            names.extend(f"{name}_{item.pk}" for item in items)
        else:
            names.append(name)
    return names
