"""Spreadsheet import of client organizations, sites and contacts: one CSV file per record type,
in a single transaction.

Every row goes through the clients services (create_client, update_client, create_site,
update_site, add_contact, update_contact), so validation, role checks, revision checks and the
per-record audit events are those of manual entry. This module reads the file, finds the record
each row names by its natural key, converts cells, and translates the services' errors onto the
row. It never writes a model itself, never creates sign-in accounts or portal access, and never
sends anything.

Preview runs the same service calls as the import inside a transaction that is always rolled
back, so it reports what the import would do and stores nothing (sequence numbers drawn by the
rolled-back inserts are not reused). Import parses the file again, refuses it if the file or the
records it touches changed since the preview (state token), writes every row in one transaction
and records one summary event for the file. Any row error writes nothing. A file whose rows are
all unchanged (the same file imported again) writes nothing at all, not even the summary.

Keys never cross organizations: a site or contact is always looked up inside the organization its
row names, and the sites a contact serves inside that same organization, so no row can move a
record from one organization to another.
"""

import base64
import binascii
import csv
import hashlib
import io
import json
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import DatabaseError, transaction

from accounts.acting import hold_actor
from accounts.roles import ROLES, has_role
from clients import services
from clients.models import Client, ClientContact, ClientSite, OrganizationType, Responsibility
from clients.permissions import MANAGE_ROLES, require_client_manager
from core.models import AuditEvent
from core.vocabulary import US_STATES

MAX_BYTES = 1024 * 1024
MAX_ROWS = 2000
SUMMARY_ACTION = "dataimport.file_imported"
PREVIEW_REASON = "Preview"  # stands in for the reason during the dry run; never stored
LIST_SEPARATOR = ";"
# OWASP CSV injection: a cell starting with one of these is a formula to spreadsheet programs
# (ASCII and full-width forms). Imported unchanged, but flagged in the preview.
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r", "＝", "＋", "－", "＠")
# Headers that would carry patient data. The files hold organizations, sites and contacts only.
PATIENT_HEADERS = {
    "patient",
    "patient_name",
    "patient_id",
    "mrn",
    "dob",
    "date_of_birth",
    "birth_date",
    "ssn",
    "social_security_number",
}
_BOOLEAN = {"yes": True, "no": False, "true": True, "false": False}
_STATES = {code for code, _ in US_STATES}
logger = logging.getLogger(__name__)


class RowError(Exception):
    def __init__(self, *messages):
        super().__init__(*messages)
        self.messages = list(messages)


@dataclass(frozen=True)
class Column:
    """One CSV column. A key column identifies the record; a data column names a value."""

    name: str
    help: str
    attr: str = ""  # the service's argument; "" for a key column
    kind: str = "text"  # text | choice | boolean | state | user | code | list
    key: bool = False
    optional: bool = False  # a key column that may be blank (contacts: email)
    required_on_create: bool = False
    max_length: int = 0
    choices: tuple = ()  # (value, label) for choice and list columns
    blank_ok: bool = True  # a data column that may be blank (clears the value)


@dataclass(frozen=True)
class RecordType:
    slug: str
    title: str  # plural, as on the index: "Organizations"
    noun: str
    columns: tuple
    needs: str  # what must exist first, in words
    example: dict  # the template's synthetic row
    find: Callable  # (keys, row_data, context) -> (existing or None, new_fields, label)
    before: Callable  # (existing, column) -> display text of the stored value
    write: Callable  # (actor, existing, new_fields, data, reason) -> None
    roles: tuple = MANAGE_ROLES
    version: Callable = lambda row: row.existing.revision if row.existing else ""
    # The row heading before the lookup: its keys as written (external identifiers: without a
    # person-level identifier, which is never shown).
    early_label: Callable = lambda keys: " · ".join(value for value in keys.values() if value)
    # (rows) -> None: before any row is written, lock the records the rows share in the order
    # their service locks them (external identifiers: hold_external_ids).
    hold: Callable = lambda rows: None

    @property
    def key_columns(self):
        return [c for c in self.columns if c.key]

    @property
    def data_columns(self):
        return [c for c in self.columns if not c.key]

    def column_for(self, name):
        """The CSV column a service error key refers to."""
        for column in self.columns:
            if name and name in (column.attr, column.name):
                return column.name
        return {
            "account_number": "account_code",
            "code": "site_code",
            "client": "account_code",
        }.get(name, name)

    def describe(self, column):
        """(requirement, format) for the column reference on the upload page."""
        if column.key:
            requirement = "Key, optional" if column.optional else "Key, required"
        elif column.required_on_create:
            requirement = "Required for a new record; cannot be blank"
        elif not column.blank_ok:
            requirement = "Optional; cannot be blank"
        else:
            requirement = "Optional; blank clears it"
        if column.kind == "choice":
            return requirement, "One of: " + ", ".join(value for value, _ in column.choices)
        if column.kind == "list":
            values = ", ".join(value for value, _ in column.choices)
            return requirement, f"Any of {values}, separated by '{LIST_SEPARATOR}'"
        if column.kind == "boolean":
            return requirement, "yes or no"
        if column.kind == "state":
            return requirement, "Two-letter US state or territory code, such as NY"
        if column.kind == "user":
            return requirement, "Username of an active laboratory staff account"
        if column.kind == "code":
            return requirement, f"Codes separated by '{LIST_SEPARATOR}'"
        if column.max_length:
            return requirement, f"Text, up to {column.max_length} characters"
        return requirement, "Text"


@dataclass
class Row:
    line: int
    values: dict  # column name -> cell text, stripped of surrounding whitespace
    label: str = ""
    action: str = ""  # create | update | unchanged; blank when the row could not be read
    existing: object = None
    new_fields: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)  # service argument -> converted value
    changes: list = field(default_factory=list)  # (column, before, after) display text
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    notes: list = field(default_factory=list)  # information, not a problem
    not_applied: list = field(default_factory=list)  # values the import leaves as recorded
    bound: dict = field(default_factory=dict)  # resolved ids the preview showed (state token)

    @property
    def ok(self):
        return not self.errors


@dataclass
class Preview:
    record_type: RecordType
    filename: str
    file_hash: str = ""
    file_bytes: int = 0
    rows: list = field(default_factory=list)
    file_errors: list = field(default_factory=list)
    state_token: str = ""

    @property
    def ok(self):
        return not self.file_errors and bool(self.rows) and all(r.ok for r in self.rows)

    @property
    def error_rows(self):
        return [r for r in self.rows if r.errors]

    @property
    def error_count(self):
        return len(self.file_errors) + sum(len(r.errors) for r in self.rows)

    @property
    def warning_count(self):
        return sum(len(r.warnings) for r in self.rows)

    @property
    def not_applied_count(self):
        return sum(1 for r in self.rows if r.not_applied)

    @property
    def counts(self):
        counted = Counter(r.action for r in self.rows if r.ok)
        return {
            "create": counted["create"],
            "update": counted["update"],
            "unchanged": counted["unchanged"],
            "error": len(self.error_rows),
        }

    @property
    def needs_reason(self):
        return any(r.action == "update" for r in self.rows)

    @property
    def ordered_rows(self):
        """Rows with errors first, then rows with warnings, then file order."""
        flagged = [r for r in self.rows if not r.errors and (r.warnings or r.not_applied)]
        return (
            self.error_rows
            + flagged
            + [r for r in self.rows if not r.errors and not r.warnings and not r.not_applied]
        )


# ------------------------------------------------------------------------------ file transport


def encode_payload(data):
    """Uploaded bytes as ASCII for the hidden field that carries them from preview to import."""
    return base64.b64encode(data).decode("ascii")


def decode_payload(value):
    """The bytes back from encode_payload. Raises ValidationError."""
    try:
        data = base64.b64decode(value, validate=True)
    except binascii.Error, ValueError:
        raise ValidationError("The file could not be read back. Upload it again.") from None
    if len(data) > MAX_BYTES:
        raise ValidationError(f"The file is larger than {MAX_BYTES // 1024} KiB.")
    return data


def file_digest(data):
    """SHA-256 of the uploaded bytes exactly as received."""
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------------- cells


def formula_warning(column_name, raw):
    """A preview warning for a cell a spreadsheet program would read as a formula, or None."""
    if raw.startswith(FORMULA_STARTS) or raw.strip().startswith(FORMULA_STARTS):
        first = raw.strip()[:1] or repr(raw[:1])
        return (
            f"{column_name}: starts with '{first}', which spreadsheet programs treat as a "
            "formula. It is imported as text, unchanged; check it is what you meant."
        )
    return None


def _choice(column, text):
    by_value = {value: value for value, _ in column.choices}
    by_label = {label.casefold(): value for value, label in column.choices}
    found = by_value.get(text) or by_value.get(text.casefold()) or by_label.get(text.casefold())
    if found is None:
        allowed = ", ".join(value for value, _ in column.choices)
        raise RowError(f"{column.name}: '{text}' is not allowed. Use one of: {allowed}.")
    return found


def _cell(column, text):
    """Converted value of a nonblank data cell. Raises RowError naming the column."""
    name = column.name
    if column.max_length and len(text) > column.max_length:
        raise RowError(f"{name}: longer than {column.max_length} characters.")
    if column.kind == "choice":
        return _choice(column, text)
    if column.kind == "list":
        parts = [part.strip() for part in text.split(LIST_SEPARATOR) if part.strip()]
        values = []
        for part in parts:
            value = _choice(column, part)
            if value not in values:
                values.append(value)
        order = [value for value, _ in column.choices]
        return sorted(values, key=order.index)
    if column.kind == "code":
        return sorted({part.strip() for part in text.split(LIST_SEPARATOR) if part.strip()})
    if column.kind == "boolean":
        if text.casefold() not in _BOOLEAN:
            raise RowError(f"{name}: '{text}' is not yes or no.")
        return _BOOLEAN[text.casefold()]
    if column.kind == "state":
        code = text.upper()
        if code not in _STATES:
            raise RowError(f"{name}: '{text}' is not a two-letter US state or territory code.")
        return code
    if column.kind == "user":
        user = get_user_model().objects.filter(username=text).first()
        if user is None:
            raise RowError(f"{name}: no account with username '{text}'.")
        if not (user.is_active and has_role(user, *ROLES)):
            raise RowError(f"{name}: '{text}' is not an active laboratory staff account.")
        return user
    return text


def _blank(column):
    if column.kind in ("list", "code"):
        return []
    if column.kind == "user":
        return None
    return ""


def show(value):
    """Display text for a value in the preview."""
    if value is None or value == "" or value == []:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return f"{LIST_SEPARATOR} ".join(str(item) for item in value)
    if hasattr(value, "get_username"):
        return value.get_username()
    return str(value)


# ------------------------------------------------------------------------- organizations


class ParentCode(str):
    """A parent_account_code cell: the code as written (shown and compared in the preview) and
    the organization it resolved to when the file was read, locked for the import. The write uses
    this result without looking the code up again."""

    resolved = None


def _organization(code, lock=False):
    organizations = Client.objects.filter(account_number__iexact=code)
    if lock:
        organizations = organizations.select_for_update()
    found = list(organizations[:2])
    if len(found) > 1:
        raise RowError(
            f"account_code: '{code}' matches more than one organization (codes differing only "
            "in letter case); fix them in the application first."
        )
    return found[0] if found else None


def _known_organization(code):
    organization = _organization(code)
    if organization is None:
        raise RowError(
            f"account_code: no organization with account code '{code}'. Import the "
            "organizations first, or give the organization this account code."
        )
    return organization


def find_organization(keys, data, context):
    code = keys["account_code"]
    existing = _organization(code)
    parent = data.get("parent")
    if parent:
        known_parent = _organization(parent, lock=True)  # held until the import commits
        context["bound"][code] = {"parent": known_parent.pk if known_parent else None}
        data["parent"] = ParentCode(parent)
        data["parent"].resolved = known_parent
        if parent.casefold() == code.casefold():
            raise RowError("parent_account_code: an organization cannot be its own parent.")
        if known_parent is None and parent.casefold() not in context["new_codes"]:
            raise RowError(
                f"parent_account_code: no organization with account code '{parent}', in the "
                "application or in an earlier row of this file. Put parents first."
            )
    if existing is None:
        context["new_codes"].add(code.casefold())
        name = data.get("name", "")
        if name.casefold() in context["new_names"]:
            raise RowError(
                f"name: an earlier row of this file also creates an organization named '{name}'."
            )
        context["new_names"].add(name.casefold())
        twins = list(
            Client.objects.filter(name__iexact=name).values_list("account_number", flat=True)
        )
        if name and twins:
            codes = ", ".join(c or "no account code" for c in twins)
            raise RowError(
                f"name: an organization named '{name}' already exists ({codes}). Use its account "
                "code in this row, or give it this account code in the application first."
            )
    return existing, {"account_number": code}, code


def organization_before(existing, column):
    if column.attr == "parent":
        return existing.parent.account_number if existing.parent_id else ""
    if column.attr == "account_owner":
        return existing.account_owner.get_username() if existing.account_owner_id else ""
    return show(getattr(existing, column.attr))


def _resolve_parent(data):
    if "parent" not in data:
        return data
    code = data["parent"]
    if not code:
        return {**data, "parent": None}
    if getattr(code, "resolved", None) is not None:
        return {**data, "parent": code.resolved}  # resolved and locked when the file was read
    # Not in the database when the file was read: created by an earlier row of this file, in
    # this same transaction.
    return {**data, "parent": _organization(code)}


def write_organization(actor, existing, new_fields, data, reason):
    data = _resolve_parent(data)
    if existing is None:
        active = data.pop("is_active", True)
        created = services.create_client(actor=actor, reason=reason, **new_fields, **data)
        if active is False:
            services.update_client(
                actor=actor,
                client_id=created.pk,
                expected_revision=created.revision,
                reason=reason,
                is_active=False,
            )
    else:
        services.update_client(
            actor=actor,
            client_id=existing.pk,
            expected_revision=existing.revision,
            reason=reason,
            **data,
        )


ACCOUNT_CODE = Column(
    "account_code",
    "The laboratory's account code of the organization (letter case ignored).",
    key=True,
)
ACTIVE = Column(
    "active",
    "yes or no. Leave the column out to keep the current setting.",
    "is_active",
    kind="boolean",
    blank_ok=False,
)
ADDRESS = (
    Column("address_line1", "Street address.", "address_line1", max_length=160),
    Column("address_line2", "Suite, building or PO box.", "address_line2", max_length=160),
    Column("city", "City.", "city", max_length=80),
    Column("state", "Two-letter state code.", "state", kind="state"),
    Column("postal_code", "ZIP code.", "postal_code", max_length=12),
)

ORGANIZATIONS = RecordType(
    slug="organizations",
    title="Organizations",
    noun="organization",
    needs="Nothing. A parent organization must exist already or come in an earlier row.",
    columns=(
        ACCOUNT_CODE,
        Column(
            "name",
            "Organization name.",
            "name",
            required_on_create=True,
            max_length=160,
            blank_ok=False,
        ),
        Column(
            "organization_type",
            "Organization type.",
            "organization_type",
            kind="choice",
            choices=tuple(OrganizationType.choices),
        ),
        Column(
            "parent_account_code",
            "Account code of the parent organization (a health system, utility or sponsor).",
            "parent",
        ),
        Column(
            "account_owner",
            "Username of the staff member who owns the account.",
            "account_owner",
            kind="user",
        ),
        Column("email", "General email address.", "email", max_length=254),
        Column("phone", "Main phone number.", "phone", max_length=40),
        *ADDRESS,
        Column("notes", "Internal notes; never shown to the client.", "notes"),
        ACTIVE,
    ),
    example={
        "account_code": "EXAMPLE01",
        "name": "Example Template Clinic",
        "organization_type": "practice",
        "parent_account_code": "",
        "account_owner": "",
        "email": "frontdesk@template.example.invalid",
        "phone": "555-0199",
        "address_line1": "1 Example Street",
        "address_line2": "",
        "city": "Exampleville",
        "state": "NY",
        "postal_code": "00000",
        "notes": "Synthetic example row: replace it with your own.",
        "active": "yes",
    },
    find=find_organization,
    before=organization_before,
    write=write_organization,
)


# ------------------------------------------------------------------------------- sites


def find_site(keys, data, context):
    code, site_code = keys["account_code"], keys["site_code"]
    organization = _known_organization(code)
    found = list(ClientSite.objects.filter(client=organization, code__iexact=site_code)[:2])
    existing = found[0] if found else None
    label = f"{organization.account_number} · {site_code}"
    return existing, {"client_id": organization.pk, "code": site_code}, label


def site_before(existing, column):
    return show(getattr(existing, column.attr))


def write_site(actor, existing, new_fields, data, reason):
    if existing is None:
        services.create_site(actor=actor, **new_fields, **data)
    else:
        services.update_site(
            actor=actor,
            site_id=existing.pk,
            expected_revision=existing.revision,
            reason=reason,
            **data,
        )


SITES = RecordType(
    slug="sites",
    title="Sites",
    noun="site",
    needs="Organizations: every account_code must exist already.",
    columns=(
        ACCOUNT_CODE,
        Column(
            "site_code",
            "The site's code, unique within its organization (letter case ignored).",
            key=True,
        ),
        Column(
            "name", "Site name.", "name", required_on_create=True, max_length=160, blank_ok=False
        ),
        Column(
            "kind",
            "What sort of place it is.",
            "kind",
            kind="choice",
            choices=tuple(ClientSite.Kind.choices),
            blank_ok=False,
        ),
        *ADDRESS,
        Column("phone", "Phone number.", "phone", max_length=40),
        Column("instructions", "Internal instructions; never shown to the client.", "instructions"),
        ACTIVE,
    ),
    example={
        "account_code": "EXAMPLE01",
        "site_code": "MAIN",
        "name": "Main office",
        "kind": "office",
        "address_line1": "1 Example Street",
        "address_line2": "",
        "city": "Exampleville",
        "state": "NY",
        "postal_code": "00000",
        "phone": "555-0198",
        "instructions": "Synthetic example row: replace it with your own.",
        "active": "yes",
    },
    find=find_site,
    before=site_before,
    write=write_site,
)


# ----------------------------------------------------------------------------- contacts


def find_contact(keys, data, context):
    code, email = keys["account_code"], keys["email"]
    organization = _known_organization(code)
    contacts = ClientContact.objects.filter(client=organization)
    name = data.get("name", "")
    if email:
        found = list(contacts.filter(email__iexact=email)[:2])
        if not found and name:  # the same person recorded without an email: this adds it
            found = list(contacts.filter(email="", name__iexact=name)[:2])
        label = f"{organization.account_number} · {email}"
    else:
        if not name:
            raise RowError("name: required to find a contact that has no email address.")
        found = list(contacts.filter(email="", name__iexact=name)[:2])
        label = f"{organization.account_number} · {name}"
    if len(found) > 1:
        raise RowError(
            f"This row matches more than one contact of {organization.account_number}; give "
            "them distinct email addresses in the application first."
        )
    if "sites" in data:
        codes = data["sites"]
        sites = list(ClientSite.objects.filter(client=organization, code__in=codes))
        known = {site.code for site in sites}
        missing = [c for c in codes if c not in known]
        if missing:
            raise RowError(
                f"site_codes: no site {', '.join(missing)} at {organization.account_number}. "
                "Sites belong to the row's organization; import them first."
            )
        data["sites"] = sorted(sites, key=lambda site: site.code)
    existing = found[0] if found else None
    if email and (existing is None or existing.email.casefold() != email.casefold()):
        data["email"] = email  # a new contact's address, or the address a name match gains
    return existing, {"client_id": organization.pk}, label


def contact_before(existing, column):
    if column.attr == "sites":
        return show(sorted(existing.sites.values_list("code", flat=True)))
    return show(getattr(existing, column.attr))


def write_contact(actor, existing, new_fields, data, reason):
    if existing is None:
        services.add_contact(actor=actor, **new_fields, **data)
    else:
        services.update_contact(
            actor=actor,
            contact_id=existing.pk,
            expected_revision=existing.revision,
            reason=reason,
            **data,
        )


CONTACTS = RecordType(
    slug="contacts",
    title="Contacts",
    noun="contact",
    needs="Organizations, and the sites named in site_codes.",
    columns=(
        ACCOUNT_CODE,
        Column(
            "email",
            "Email address; with account_code it identifies the contact. Leave it "
            "blank for a contact without email: the name identifies them then.",
            key=True,
            optional=True,
        ),
        Column(
            "name",
            "The person's name.",
            "name",
            required_on_create=True,
            max_length=160,
            blank_ok=False,
        ),
        Column("role", "What the laboratory contacts this person about.", "role", max_length=80),
        Column("phone", "Phone number.", "phone", max_length=40),
        Column(
            "responsibilities",
            "What the person deals with. Routes work; not a record of "
            "authority to receive results.",
            "responsibilities",
            kind="list",
            choices=tuple(Responsibility.choices),
        ),
        Column(
            "site_codes",
            "Codes of the sites of this organization the contact serves; blank "
            "for the whole organization.",
            "sites",
            kind="code",
        ),
        ACTIVE,
    ),
    example={
        "account_code": "EXAMPLE01",
        "email": "manager@template.example.invalid",
        "name": "Example Office Manager",
        "role": "Office manager",
        "phone": "555-0197",
        "responsibilities": "ordering; results",
        "site_codes": "MAIN",
        "active": "yes",
    },
    find=find_contact,
    before=contact_before,
    write=write_contact,
)


# ------------------------------------------------------------------- external identifiers


def find_external_id(keys, data, context):
    """The organization's current attachment of the link with this natural key (system,
    record type, scheme, identifier) in this role, or None for a new attachment. Person-level
    record types are never imported: they belong on a request, behind the patient-information
    permission (links.services)."""
    from links.identifiers import clean_identifier
    from links.masking import is_person_type
    from links.models import (
        ExternalRecordLink,
        ExternalSystem,
        LinkAttachment,
        LinkRole,
        RecordType,
    )

    organization = _known_organization(keys["account_code"])
    record_type = keys["record_type"].strip().lower()
    if is_person_type(record_type):
        raise RowError(
            f"record_type: '{keys['record_type']}' is a person-level record (patient, research "
            "participant, donor). Those are never imported and never belong on an organization."
        )
    allowed = [value for value in RecordType.values if not is_person_type(value)]
    if record_type not in allowed:
        raise RowError(
            f"record_type: '{keys['record_type']}' is not allowed. Use one of: "
            f"{', '.join(allowed)}."
        )
    system = ExternalSystem.objects.filter(key__iexact=keys["system"]).first()
    if system is None:
        raise RowError(
            f"system: no external system with key '{keys['system']}'. An administrator adds "
            "systems under External systems."
        )
    if not system.is_active:
        raise RowError(f"system: {system.name} is inactive and takes no new links.")
    try:
        identifier = clean_identifier(keys["identifier"])
    except ValidationError as exc:
        raise RowError(*(f"identifier: {message}" for message in exc.messages)) from None
    role = (keys["role"] or "related").strip().lower()
    if role not in LinkRole.values:
        raise RowError(
            f"role: '{keys['role']}' is not allowed. Use one of: {', '.join(LinkRole.values)}."
        )
    scheme = keys["identifier_scheme"].strip()
    link = ExternalRecordLink.objects.filter(
        system=system, record_type=record_type, identifier_scheme=scheme, identifier=identifier
    ).first()
    existing = None
    if link is not None:
        existing = LinkAttachment.objects.filter(
            link=link, client=organization, role=role, removed_at__isnull=True
        ).first()
    label = " · ".join(
        part
        for part in (organization.account_number, system.key, record_type, scheme, identifier, role)
        if part
    )
    new_fields = {
        "client_id": organization.pk,
        "system": system,
        "record_type": record_type,
        "identifier_scheme": scheme,
        "identifier": identifier,
        "role": role,
        "link": link,
    }
    if link is not None:
        # An existing external record is shared: the import attaches it and never changes it.
        differences = [
            f"{name}: the link keeps '{show(getattr(link, attr))}', not this row's "
            f"'{show(data[attr])}'"
            for name, attr in (("display_label", "display_label"), ("url", "url"))
            if attr in data and show(data[attr]) != show(getattr(link, attr))
        ]
        data.pop("display_label", None)
        data.pop("url", None)
        if differences:
            context["not_applied"][label] = [
                "This identifier is already recorded and is shared; the import does not change "
                "it. Change it on the link's page if needed. " + "; ".join(differences) + "."
            ]
        if existing is None:
            context["notes"][label] = [
                "Attaches the link already recorded for this identifier "
                f"(label '{link.display_label}')."
            ]
    return existing, new_fields, label


def external_id_before(existing, column):
    return show(getattr(existing.link, column.attr))


def write_external_id(actor, existing, new_fields, data, reason):
    from links import services as link_services

    fields = dict(new_fields)
    fields.pop("link")
    if existing is None:
        link_services.add_link(
            actor=actor,
            system=fields["system"],
            record_type=fields["record_type"],
            identifier=fields["identifier"],
            identifier_scheme=fields["identifier_scheme"],
            client_id=fields["client_id"],
            role=fields["role"],
            display_label=data.get("display_label", ""),
            url=data.get("url", ""),
        )
    # An attachment that exists already changes nothing: rows never edit a shared link
    # (find_external_id drops their label and address and reports any difference).


def hold_external_ids(rows):
    """Lock the organizations, then the external systems, of the links the file adds, before
    any row is written. links.services.add_link locks a row's organization, then its system,
    then the link. Written row by row, the file would lock its second organization after its
    first row's system, while a link added on that organization's page at the same moment locks
    the organization and then waits for the system: a cycle, which the database ends by failing
    one of them. Taken all at first, in add_link's order, the file and a link change only ever
    wait for each other one way.

    The organizations are locked in the order the file first names them, as the rows would lock
    them. A change of an organization's group locks the organization and then walks up to its
    new parent (clients.services._check_parent), the other way round from a file that names the
    group first; the import holds the organization directory lock from its start
    (_hold_importer) and that change takes it before its first lock, so the two take their
    turns."""
    from links.models import ExternalSystem

    adding = [
        row.new_fields
        for row in rows
        if not row.errors and row.action == "create" and row.existing is None
    ]
    for organization_id in dict.fromkeys(fields["client_id"] for fields in adding):
        list(Client.objects.select_for_update().filter(pk=organization_id).values_list("pk"))
    systems = {fields["system"].pk for fields in adding}
    list(
        ExternalSystem.objects.select_for_update()
        .filter(pk__in=systems)
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def external_id_early_label(keys):
    from links.masking import is_person_type

    if is_person_type(keys.get("record_type", "").strip().lower()):
        shown = [keys["account_code"], keys["system"], keys["record_type"]]
        return " · ".join(part for part in shown if part) + " · identifier not shown"
    return " · ".join(value for value in keys.values() if value)


def external_id_version(row):
    link = row.new_fields.get("link") if row.new_fields else None
    attachment = row.existing.pk if row.existing else ""
    return f"{attachment}:{link.pk if link else ''}:{link.revision if link else ''}"


EXTERNAL_IDS = RecordType(
    slug="external-ids",
    title="External identifiers",
    noun="external identifier",
    needs="Organizations, and the external systems (set up by an administrator).",
    columns=(
        ACCOUNT_CODE,
        Column(
            "system",
            "Key of the external system, as on External systems (for example lis).",
            key=True,
        ),
        Column(
            "record_type",
            "What the identifier names in that system. Person-level types "
            "(patient, participant, donor) are refused.",
            key=True,
        ),
        Column(
            "identifier_scheme",
            "Optional, when the system has more than one kind of number.",
            key=True,
            optional=True,
        ),
        Column("identifier", "As the source system writes it; never parsed.", key=True),
        Column(
            "role",
            "primary, related (the default), original or replacement.",
            key=True,
            optional=True,
        ),
        Column(
            "display_label",
            "Short label; blank uses the identifier. The label belongs to "
            "the link, which other records may share.",
            "display_label",
            max_length=120,
        ),
        Column(
            "url",
            "Optional https address on the system's host; allowed only when the system "
            "has a base address.",
            "url",
            max_length=500,
        ),
    ),
    example={
        "account_code": "EXAMPLE01",
        "system": "lis",
        "record_type": "other",
        "identifier_scheme": "client_code",
        "identifier": "TEST-CLIENT-0001",
        "role": "primary",
        "display_label": "LIS client TEST-CLIENT-0001",
        "url": "",
    },
    find=find_external_id,
    before=external_id_before,
    write=write_external_id,
    version=external_id_version,
    early_label=external_id_early_label,
    hold=hold_external_ids,
)


TYPES = (ORGANIZATIONS, SITES, CONTACTS, EXTERNAL_IDS)
BY_SLUG = {record_type.slug: record_type for record_type in TYPES}


def allowed_types(user):
    return [record_type for record_type in TYPES if has_role(user, *record_type.roles)]


def template_bytes(record_type):
    """Header and one synthetic example row, UTF-8 with a byte-order mark for spreadsheets."""
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(c.name for c in record_type.columns)
    writer.writerow(record_type.example.get(c.name, "") for c in record_type.columns)
    return ("﻿" + out.getvalue()).encode("utf-8")


# ------------------------------------------------------------------------------- parsing


def _read_header(record_type, header, preview):
    names = [h.strip().lower() for h in header]
    allowed = [c.name for c in record_type.columns]
    blank = [str(i) for i, name in enumerate(names, start=1) if not name]
    patient = [name for name in names if name in PATIENT_HEADERS]
    unknown = [name for name in names if name and name not in allowed and name not in patient]
    duplicate = sorted({name for name in names if name and names.count(name) > 1})
    missing = [c.name for c in record_type.key_columns if c.name not in names]
    if patient:
        preview.file_errors.append(
            f"Line 1: column(s) {', '.join(patient)} would hold patient data. This import takes "
            "organizations, sites and contacts only; remove those columns."
        )
    if blank:
        preview.file_errors.append(f"Line 1: column {', '.join(blank)} has no name.")
    if unknown:
        preview.file_errors.append(
            f"Line 1: unknown column(s) {', '.join(unknown)}. Allowed: {', '.join(allowed)}."
        )
    if duplicate:
        preview.file_errors.append(f"Line 1: duplicate column(s) {', '.join(duplicate)}.")
    if missing:
        preview.file_errors.append(f"Line 1: missing key column(s) {', '.join(missing)}.")
    return names


def _identity(row):
    if row.existing is not None:
        return ("existing", row.existing.pk)
    return ("new", row.label.casefold())


def _read_row(record_type, row, present, seen, context):
    keys = {}
    for column in record_type.key_columns:
        keys[column.name] = row.values.get(column.name, "")
        if not keys[column.name] and not column.optional:
            row.errors.append(f"{column.name}: required; it identifies the record.")
    # Named by its keys as written, until the lookup names the record it found.
    row.label = record_type.early_label(keys) or row.values.get("name", "")
    data = {}  # only columns present in the file: an absent column is left alone
    for column in record_type.data_columns:
        if column.name not in present:
            continue
        text = row.values[column.name]
        if text:
            try:
                data[column.attr] = _cell(column, text)
            except RowError as exc:
                row.errors += exc.messages
        elif not column.blank_ok:
            row.errors.append(f"{column.name}: cannot be blank.")
        else:
            data[column.attr] = _blank(column)
    if row.errors:
        return
    try:
        row.existing, row.new_fields, row.label = record_type.find(keys, data, context)
    except RowError as exc:
        row.errors += exc.messages
        return
    row.notes += context["notes"].pop(row.label, [])
    row.not_applied += context["not_applied"].pop(row.label, [])
    row.bound = _bound(row.new_fields, data, context["bound"].pop(row.label, {}))
    identity = _identity(row)
    if identity in seen:
        row.errors.append(f"Duplicate of line {seen[identity]}: one row per record.")
        return
    seen[identity] = row.line
    row.data = data
    if row.existing is None:
        row.action = "create"
        for column in record_type.data_columns:
            if column.required_on_create and column.attr not in data:
                row.errors.append(
                    f"{column.name}: required for a new {record_type.noun}; the column is "
                    "missing from the file."
                )
            elif column.attr in data and show(data[column.attr]) != "":
                row.changes.append((column.name, "", _display(column, data[column.attr])))
        return
    for column in record_type.data_columns:
        if column.attr not in data:
            continue
        before = record_type.before(row.existing, column)
        after = _display(column, data[column.attr])
        if before.casefold() == after.casefold() and column.attr == "parent":
            del data[column.attr]  # account codes compare without letter case
        elif before != after:
            row.changes.append((column.name, before, after))
        else:
            del data[column.attr]  # unchanged values are not sent to the service
    if "email" in data and not any(c.attr == "email" for c in record_type.data_columns):
        row.changes.append(("email", row.existing.email, data["email"]))  # a contact gains one
    row.action = "update" if row.changes else "unchanged"
    if row.action == "unchanged":
        row.data = {}


def _display(column, value):
    if column.attr == "sites":
        return show(sorted(site.code for site in value))
    return show(value)


def parse(record_type, data, filename):
    """Read and resolve the uploaded bytes. Reads the database; writes nothing."""
    preview = Preview(
        record_type, filename=filename, file_hash=file_digest(data), file_bytes=len(data)
    )
    if len(data) > MAX_BYTES:
        preview.file_errors.append(f"The file is larger than {MAX_BYTES // 1024} KiB.")
        return preview
    try:
        text = data.decode("utf-8-sig")  # a leading byte-order mark (spreadsheet export) is fine
    except UnicodeDecodeError:
        preview.file_errors.append(
            "The file is not UTF-8 text. Save it from your spreadsheet as CSV UTF-8."
        )
        return preview
    if "\x00" in text:
        preview.file_errors.append("The file contains NUL characters; it is not a text CSV.")
        return preview
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader, None)
    except csv.Error as exc:
        preview.file_errors.append(f"Line 1: the header could not be read ({exc}).")
        return preview
    if header is None:
        preview.file_errors.append("The file is empty.")
        return preview
    names = _read_header(record_type, header, preview)
    if preview.file_errors:
        return preview
    present = set(names)
    seen = {}
    context = {"new_codes": set(), "new_names": set(), "notes": {}, "not_applied": {}, "bound": {}}
    line = reader.line_num + 1
    try:
        for raw in reader:
            start, line = line, reader.line_num + 1
            if not any(cell.strip() for cell in raw):
                continue
            if len(preview.rows) >= MAX_ROWS:
                preview.file_errors.append(f"More than {MAX_ROWS} rows; split the file.")
                break
            row = Row(start, {})
            if len(raw) != len(names):
                row.errors.append(f"Expected {len(names)} cells, found {len(raw)}.")
            else:
                row.values = {name: cell.strip() for name, cell in zip(names, raw, strict=True)}
                for name, cell in zip(names, raw, strict=True):
                    if warning := formula_warning(name, cell):
                        row.warnings.append(warning)
                _read_row(record_type, row, present, seen, context)
            preview.rows.append(row)
    except csv.Error as exc:
        preview.file_errors.append(f"Line {reader.line_num}: malformed CSV ({exc}).")
    if not preview.rows and not preview.file_errors:
        preview.file_errors.append("The file has a header but no data rows.")
    preview.state_token = _state_token(preview)
    return preview


def _ids(value):
    if hasattr(value, "pk"):
        return value.pk
    if isinstance(value, (list, tuple)) and value:
        ids = [_ids(item) for item in value]
        return ids if all(item is not None for item in ids) else None
    return None


def _bound(new_fields, data, extra):
    """The database ids a row's codes resolved to, for the state token."""
    bound = {name: _ids(value) for name, value in new_fields.items() if _ids(value) is not None}
    bound.update({k: v for k, v in new_fields.items() if k.endswith("_id")})
    bound.update({name: _ids(value) for name, value in data.items() if _ids(value) is not None})
    bound.update(extra)
    return bound


def _state_token(preview):
    """Fingerprint of what the preview showed: the file (bytes and name) and, for every row, the
    record it touches (id and revision) and the records its codes resolved to (organization,
    parent, sites, system, link). The import is refused when any of them differs."""
    parts = [preview.record_type.slug, preview.file_hash, preview.filename]
    for row in preview.rows:
        existing = row.existing
        parts.append(
            f"{row.line}:{row.action}:{existing.pk if existing else ''}:"
            f"{preview.record_type.version(row) if row.action else ''}:"
            f"{json.dumps(row.bound, sort_keys=True, default=str)}"
        )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


# ------------------------------------------------------------------------- preview / import


def _service_errors(record_type, exc):
    if hasattr(exc, "error_dict"):
        return [
            message if name == "__all__" else f"{record_type.column_for(name)}: {message}"
            for name, messages in exc.message_dict.items()
            for message in messages
        ]
    return list(exc.messages)


LOCKS_REFUSED = (
    "Another change to the organizations or systems in this file was being saved at the same "
    "moment. Nothing was imported; upload the file again."
)


def _apply(record_type, actor, preview, reason):
    """Run every changing row through its service inside the caller's transaction, after the
    records the rows share are locked in their service's order (RecordType.hold)."""
    source = f"{preview.filename}, SHA-256 {preview.file_hash}"
    try:
        with transaction.atomic():
            record_type.hold(preview.rows)
    except DatabaseError:  # the database ended a deadlock with another change: refused, not a 500
        # (no change of the application locks these records in another order; kept for safety)
        preview.file_errors.append(LOCKS_REFUSED)
        return
    for row in preview.rows:
        if row.errors or row.action not in ("create", "update"):
            continue
        if row.action == "create":
            row_reason = f"Created by import of {source}"
        else:
            row_reason = f"{reason or PREVIEW_REASON} (import of {source})"
        try:
            with transaction.atomic():
                record_type.write(actor, row.existing, row.new_fields, dict(row.data), row_reason)
        except ValidationError as exc:
            row.errors += _service_errors(record_type, exc)
        except ObjectDoesNotExist:
            row.errors.append("This record no longer exists. Upload the file again.")
        except DatabaseError:  # the row's savepoint is rolled back; the file is then refused
            row.errors.append("The database refused this row. Check its values and try again.")
        except PermissionDenied:
            raise
        except Exception:  # never a server error for one row: the file is refused
            logger.exception("Import %s line %s failed unexpectedly", record_type.slug, row.line)
            row.errors.append(
                f"Line {row.line} could not be saved because of an unexpected error. Nothing was "
                "imported; check the row's values or report the problem."
            )


def _hold_importer(actor):
    """The importing account held until the file's transaction ends, before anything else it
    locks (accounts.acting), and its role checked again on that reading. Then the organization
    directory lock (clients.services.hold_directory), before the file is read: a file names
    organizations by account code and in any order, so it and a change of an organization's
    code or group, a creation or another import take their turns, and the file is read after
    the one before it has committed."""
    current = hold_actor(actor)
    if current is None:
        raise PermissionDenied("Your account no longer has a staff role.")
    require_client_manager(current)
    services.hold_directory()


def preview_file(record_type, *, actor, data, filename):
    """What importing the file would do, row by row. Always rolled back: stores nothing."""
    require_client_manager(actor)
    with transaction.atomic():
        _hold_importer(actor)
        preview = parse(record_type, data, filename)
        if not preview.file_errors:
            _apply(record_type, actor, preview, "")
        transaction.set_rollback(True)
    return preview


def commit_file(record_type, *, actor, data, filename, reason, state_token):
    """Check the file again and write every row, or nothing. Returns the preview it wrote.

    Raises ValidationError: {"reason": ...} when the file changes existing records and no reason
    was given; otherwise a message (stale preview, file or row errors). Nothing is written then.
    """
    require_client_manager(actor)
    reason = (reason or "").strip()
    with transaction.atomic():
        _hold_importer(actor)
        preview = parse(record_type, data, filename)
        if not preview.ok:
            raise ValidationError(
                f"The file has {preview.error_count} error(s). Nothing was imported."
            )
        if preview.state_token != state_token:
            raise ValidationError(
                "The file or the records it changes are different from the preview. Check the "
                "new preview and import again."
            )
        if preview.needs_reason and not reason:
            raise ValidationError({"reason": "Give the reason for changing existing records."})
        _apply(record_type, actor, preview, reason)
        if not preview.ok:
            transaction.set_rollback(True)
            if preview.file_errors:  # refused as a whole (LOCKS_REFUSED): say why
                raise ValidationError(preview.file_errors)
            raise ValidationError(
                f"The file has {preview.error_count} error(s). Nothing was imported."
            )
        counts = preview.counts
        if not counts["create"] and not counts["update"]:
            return preview  # the same file again: nothing changes, so nothing is recorded
        # No model owns an uploaded file: the summary event is keyed by the file's SHA-256.
        AuditEvent.objects.create(
            actor=actor,
            action=SUMMARY_ACTION,
            object_type="dataimport.file",
            object_id=preview.file_hash,
            after={
                "record_type": record_type.slug,
                "filename": preview.filename,
                "file_sha256": preview.file_hash,
                "file_bytes": preview.file_bytes,
                "rows": len(preview.rows),
                "created": counts["create"],
                "updated": counts["update"],
                "unchanged": counts["unchanged"],
                "formula_warnings": preview.warning_count,
            },
            reason=reason if preview.needs_reason else "",
        )
    return preview
