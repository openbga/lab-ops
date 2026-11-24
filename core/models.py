import uuid

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.core.validators import URLValidator
from django.db import models
from django.db.models.functions import Now

from .request_logging import current_request_id
from .vocabulary import US_STATES

MAX_LOGO_BYTES = 512 * 1024


class LabLogoManager(models.Manager):
    """Default queries never load the image bytes; use `with_image()` to serve or render one."""

    def get_queryset(self):
        return super().get_queryset().defer("image")

    def with_image(self):
        return super().get_queryset()


class LabLogo(models.Model):
    """An image the laboratory uploaded as its logo, kept once per distinct file.

    Rows are never changed or deleted (database trigger), so the profile history can name each
    logo by content hash and still show it. core.lab_profile validates and stores uploads.
    """

    sha256 = models.CharField(max_length=64, unique=True)
    image = models.BinaryField()
    content_type = models.CharField(max_length=20)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    size = models.PositiveIntegerField()
    filename = models.CharField(max_length=255, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    objects = LabLogoManager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(size__gt=0, size__lte=MAX_LOGO_BYTES), name="lab_logo_size"
            )
        ]


class Lab(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    name = models.CharField(max_length=160)
    timezone = models.CharField(max_length=64, default="America/New_York")
    record_retention_statement = models.TextField(
        blank=True,
        max_length=8000,
        help_text="Laboratory-entered electronic record-retention policy; no automatic deletion.",
    )
    record_retention_revision = models.PositiveIntegerField(default=1)
    # The laboratory's identity as staff and clients see it (core.lab_profile).
    address_line1 = models.CharField("Street address", max_length=160, blank=True)
    address_line2 = models.CharField("Suite, building or PO box", max_length=160, blank=True)
    city = models.CharField(max_length=80, blank=True)
    state = models.CharField(max_length=2, blank=True, choices=US_STATES)
    postal_code = models.CharField("ZIP code", max_length=12, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    website = models.CharField(
        max_length=200,
        blank=True,
        validators=[URLValidator(schemes=["http", "https"])],
    )
    logo = models.ForeignKey(
        LabLogo, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="single_lab")]

    @property
    def address_lines(self):
        """Address as display lines; empty when no part is recorded."""
        locality = " ".join(
            part
            for part in (
                f"{self.city}," if self.city and self.state else self.city,
                self.state,
                self.postal_code,
            )
            if part
        )
        return [line for line in (self.address_line1, self.address_line2, locality) if line]


class LabCertificate(models.Model):
    """A certification or accreditation the laboratory holds, as the laboratory records it.

    Staff quote these identifiers to clients. The application checks nothing against a
    certificate and makes no certification or accreditation claim. core.lab_profile holds who
    may change one.
    """

    authority = models.CharField("Issuing authority", max_length=160)
    program = models.CharField("Program or scope", max_length=160, blank=True)
    number = models.CharField("Certificate or laboratory ID", max_length=80)
    valid_from = models.DateField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)
    position = models.PositiveSmallIntegerField("List position", default=0)

    class Meta:
        ordering = ["position", "authority", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(valid_from__isnull=True)
                | models.Q(valid_until__isnull=True)
                | models.Q(valid_until__gte=models.F("valid_from")),
                name="lab_certificate_valid_range",
                violation_error_message="Valid until cannot be earlier than Valid from.",
            )
        ]

    def __str__(self):
        return f"{self.authority} {self.number}"


class LabContact(models.Model):
    """A person or office the laboratory names for one responsibility, as the laboratory records
    it: its security contact and its privacy contact (core.lab_profile).

    The application checks nothing against a contact and designates no one. A row is written the
    first time an administrator saves that contact, never by a migration, so an empty database
    stays empty (core.demo). `revision` is the stale-edit check of the contact's form.
    """

    class Kind(models.TextChoices):
        SECURITY = "security", "Security contact"
        PRIVACY = "privacy", "Privacy contact"

    kind = models.CharField(max_length=20, choices=Kind.choices, unique=True)
    name = models.CharField("Name or office", max_length=160, blank=True)
    title = models.CharField("Title or role", max_length=160, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    revision = models.PositiveIntegerField(default=0)
    changed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(name__gt="") | models.Q(title="", email="", phone=""),
                name="lab_contact_named",
                violation_error_message="Give the name of the person or office.",
            )
        ]

    def __str__(self):
        return self.get_kind_display()

    @property
    def is_recorded(self):
        return bool(self.name)


class LabModule(models.Model):
    """Whether one optional module is in use (core.modules). A module without a row is off: rows
    are written the first time an administrator switches it, never by a migration, so an empty
    database stays empty (core.demo)."""

    key = models.CharField(max_length=40, unique=True)
    is_enabled = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=0)
    changed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.key


class SessionLimit(models.Model):
    """How long a session of one kind of account may last, as the laboratory sets it within fixed
    bounds (core.session_limits; accounts.sessions applies it). An audience without a row has the
    starting values: rows are written the first time an administrator saves that audience's
    limits, never by a migration, so an empty database stays empty (core.demo). `revision` is the
    stale-edit check of the form."""

    class Audience(models.TextChoices):
        STAFF = "staff", "Staff"
        PORTAL = "portal", "Client portal"

    audience = models.CharField(max_length=20, choices=Audience.choices, unique=True)
    inactivity_minutes = models.PositiveSmallIntegerField("Sign out after no activity for")
    maximum_hours = models.PositiveSmallIntegerField("Longest session")
    revision = models.PositiveIntegerField(default=0)
    changed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(inactivity_minutes__gte=5, inactivity_minutes__lte=60),
                name="session_limit_inactivity_bounds",
                violation_error_message="Choose from 5 to 60 minutes.",
            ),
            models.CheckConstraint(
                condition=models.Q(maximum_hours__gte=1, maximum_hours__lte=24),
                name="session_limit_maximum_bounds",
                violation_error_message="Choose from 1 to 24 hours.",
            ),
        ]

    def __str__(self):
        return self.get_audience_display()


class SecondFactorRule(models.Model):
    """Which accounts the laboratory requires to have a second sign-in factor (core.second_factor;
    accounts.enrolment applies it at sign-in). One row, written the first time an administrator
    saves the rule, never by a migration, so an empty database stays empty (core.demo); until then
    nothing is required. `revision` is the stale-edit check of the form."""

    # Every account that is not a client portal account.
    staff = models.BooleanField("Every staff account", default=False)
    # Staff accounts holding any of these roles (accounts.roles.ROLES), as the application counts
    # them: the admin role does the work of the others, so it is covered by any of them.
    roles = ArrayField(
        models.CharField(max_length=40),
        verbose_name="Staff with these roles",
        default=list,
        blank=True,
    )
    patient_information = models.BooleanField(
        "Staff who can view patient information", default=False
    )
    admin_console = models.BooleanField("Accounts that can open the admin console", default=False)
    portal = models.BooleanField("Client portal accounts", default=False)
    revision = models.PositiveIntegerField(default=0)
    changed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="second_factor_rule_single"),
        ]

    def __str__(self):
        return "Second sign-in factor"


class RevealConfirmationRule(models.Model):
    """Whether staff confirm it is them before the first reveal of patient information in a
    session (core.reveal_confirmation applies it to every reveal). One row, written the first time
    an administrator saves the rule, never by a migration, so an empty database stays empty
    (core.demo); until then nothing is asked. `revision` is the stale-edit check of the form."""

    first_reveal = models.BooleanField(
        "Confirm it is you before the first reveal in a session", default=False
    )
    revision = models.PositiveIntegerField(default=0)
    changed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(id=1), name="reveal_confirmation_rule_single"
            ),
        ]

    def __str__(self):
        return "Confirmation before revealing patient information"


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurred_at = models.DateTimeField(auto_now_add=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=80)
    object_type = models.CharField(max_length=100)
    object_id = models.CharField(max_length=64)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    reason = models.TextField(blank=True)
    # The server-generated ID of the request the change was made in (core.request_logging), as its
    # access-log line and X-Request-ID header carry it; blank outside a request (scheduled jobs,
    # commands) and for events recorded before this column existed. The database default lets a
    # release still running while the migration applies go on inserting events.
    request_ref = models.CharField(
        "request ID", max_length=32, blank=True, default=current_request_id, db_default=""
    )

    class Meta:
        ordering = ["occurred_at", "id"]
        indexes = [models.Index(fields=["object_type", "object_id", "occurred_at"])]


class AccessKind(models.TextChoices):
    """What an access record says was read (AccessRecord.kind)."""

    TICKET = "ticket", "Opened the ticket"
    HISTORY = "history", "Opened the audit history"
    ACCESS = "access", "Opened who opened it"
    FORM_DETAILS = "form_details", "Opened the form details"
    FILE = "file", "Downloaded a file"
    PATIENT_ANSWER = "patient_answer", "Shown a patient-marked answer"
    TIMELINE = "timeline", "Shown on the organization's timeline"
    PERSON_KEY = "person_key", "Shown a person-level key"


class AccessCapacity(models.TextChoices):
    """In which capacity an account read (AccessRecord.capacity), as desk.models.AuthorCapacity:
    decided from the account as it was read for the page."""

    LABORATORY = "laboratory", "Laboratory staff"
    CLIENT = "client", "Client portal"


class AccessRecord(models.Model):
    """One sensitive read (core.access): a staff or portal account opened a ticket, its audit
    history, who opened it or its form details, downloaded a file, was shown a patient-marked
    answer or a person-level key, or was shown the ticket's entries on its client organization's
    timeline. Who, when,
    in which capacity, which record and the request ID; never what was shown.

    History, kept apart from the change history (AuditEvent) so that a record's changes stay
    readable: the database refuses to change or delete a row (migration 0009); reset_demo empties
    the table with TRUNCATE, which row triggers do not see."""

    Kind = AccessKind
    Capacity = AccessCapacity

    id = models.BigAutoField(primary_key=True)
    # By the database's clock, when the record is written, just after the page was drawn.
    occurred_at = models.DateTimeField(db_default=Now())
    # The account, by id. Not a constraint in the database, as for the sign-in history: a record
    # outlives its account, and nothing about writing one waits for the account's row. Read
    # records by account_id; a join leaves out those of an account deleted by other means.
    account = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        db_index=False,  # access_record_account leads with it
        related_name="+",
    )
    capacity = models.CharField(max_length=12, choices=Capacity.choices)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    # The ticket read, by id (core depends on no other application). Every kind is about a ticket.
    ticket_id = models.BigIntegerField()
    # What on the ticket, for a file (attachments.attachment), an answer
    # (request_forms.fieldvalue) or a person-level link (links.linkattachment); blank otherwise.
    object_type = models.CharField(max_length=100, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    request_ref = models.CharField("request ID", max_length=32, blank=True)

    class Meta:
        # Written only by core.access: no model permission to grant, and no admin.
        default_permissions = ()
        indexes = [
            models.Index(fields=["ticket_id", "-occurred_at"], name="access_record_ticket"),
            models.Index(fields=["account", "-occurred_at"], name="access_record_account"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(kind__in=AccessKind.values), name="access_record_kind"
            ),
            models.CheckConstraint(
                condition=models.Q(capacity__in=AccessCapacity.values),
                name="access_record_capacity",
            ),
            models.CheckConstraint(
                condition=models.Q(object_type="", object_id="")
                | (~models.Q(object_type="") & ~models.Q(object_id="")),
                name="access_record_object",
            ),
        ]

    def __str__(self):
        return f"account {self.account_id} {self.kind} ticket {self.ticket_id}"


class Announcement(models.Model):
    """A notice the laboratory posts on the sign-in page, for its staff or for client users.

    core.announcements holds who may post one, who sees it and when it counts as showing.
    """

    class Tone(models.TextChoices):
        INFO = "info", "Notice"
        WARNING = "warning", "Important"

    title = models.CharField(max_length=120)
    body = models.TextField(max_length=2000)
    tone = models.CharField(max_length=10, choices=Tone.choices, default=Tone.INFO)
    show_on_sign_in = models.BooleanField(default=False)
    show_to_staff = models.BooleanField(default=False)
    show_to_clients = models.BooleanField(default=False)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-starts_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(show_on_sign_in=True)
                | models.Q(show_to_staff=True)
                | models.Q(show_to_clients=True),
                name="announcement_has_audience",
                violation_error_message="Choose at least one place to show this announcement.",
            ),
            models.CheckConstraint(
                condition=models.Q(ends_at__isnull=True)
                | models.Q(ends_at__gt=models.F("starts_at")),
                name="announcement_ends_after_start",
                violation_error_message="Show until must be later than Show from.",
            ),
        ]

    def __str__(self):
        return self.title


class UserPreference(models.Model):
    """One presentation setting of one user (core.preferences): a grid's columns, its page size
    and sort, the sidebar's state. Kept on the server, because lab workstations are shared."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="preferences"
    )
    key = models.CharField(max_length=120)
    value = models.JSONField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "key"], name="unique_user_preference")
        ]

    def __str__(self):
        return f"{self.user} {self.key}"
