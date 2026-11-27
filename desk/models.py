"""Requests and their conversations: the records the client-services desk works.

A request (ServiceRequest) belongs to one team, optionally to a person on it, and optionally to a
client organization, one of its sites and one of its contacts. Its entries (RequestEntry) are the
conversation and the operational timeline. Which entries a client may see is decided in one
place, desk.visibility. Changes go through desk.services; reads through desk.selectors (staff)
and desk.portal_scope (client portal). docs/product.md "Requests: life cycle and rules".
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from taxonomy.choices import Priority

NUMBER_PREFIX = "REQ-"


class Status(models.TextChoices):
    OPEN = "open", "Open"
    WAITING_CLIENT = "waiting_client", "Waiting on client"
    WAITING_THIRD_PARTY = "waiting_third_party", "Waiting on third party"
    RESOLVED = "resolved", "Resolved"
    CLOSED = "closed", "Closed"


# How the portal names each status.
CLIENT_STATUS_LABELS = {
    Status.OPEN: "With the laboratory",
    Status.WAITING_CLIENT: "Waiting for you",
    Status.WAITING_THIRD_PARTY: "With a third party",
    Status.RESOLVED: "Resolved",
    Status.CLOSED: "Closed",
}


class Channel(models.TextChoices):
    PHONE = "phone", "Phone"
    EMAIL = "email", "Email"
    FAX = "fax", "Fax"
    PORTAL = "portal", "Client portal"
    IN_PERSON = "in_person", "In person"
    INTERNAL = "internal", "Internal"
    OTHER = "other", "Other"


# Channels where the person answered the client while logging the request: the first-response
# clock is met at creation (docs/product.md "Service clocks").
ANSWERED_AT_INTAKE = frozenset({Channel.PHONE, Channel.IN_PERSON})


class ServiceRequest(models.Model):
    number = models.CharField(
        max_length=20,
        blank=True,
        editable=False,
        help_text="REQ- and six digits from the record's sequence; means nothing else.",
    )
    client = models.ForeignKey(
        "clients.Client",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="requests",
        verbose_name="client organization",
    )
    site = models.ForeignKey(
        "clients.ClientSite",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="requests",
    )
    contact = models.ForeignKey(
        "clients.ClientContact",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="requests",
    )
    reported_by = models.CharField(
        max_length=160, blank=True, help_text="An unknown caller's name and role."
    )
    channel = models.CharField(max_length=20, choices=Channel.choices)
    category = models.ForeignKey(
        "taxonomy.RequestCategory", on_delete=models.PROTECT, related_name="requests"
    )
    subject = models.CharField(max_length=120)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    team = models.ForeignKey("teams.Team", on_delete=models.PROTECT, related_name="requests")
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="assigned_requests",
    )
    is_complaint = models.BooleanField("complaint", default=False)
    client_reference = models.CharField(
        max_length=80,
        blank=True,
        help_text="An order, sample, report or invoice number the client gave.",
    )
    follow_up_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="follow_ups"
    )
    resolution_summary = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_requests"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-created_at", "-pk"]
        verbose_name = "ticket"
        constraints = [
            models.UniqueConstraint(
                fields=["number"], condition=~Q(number=""), name="request_number_unique"
            ),
            models.CheckConstraint(
                condition=Q(site__isnull=True) | Q(client__isnull=False),
                name="request_site_needs_client",
                violation_error_message="A site belongs to a client organization; choose it.",
            ),
            models.CheckConstraint(
                condition=Q(contact__isnull=True) | Q(client__isnull=False),
                name="request_contact_needs_client",
                violation_error_message="A contact belongs to a client organization; choose it.",
            ),
            models.CheckConstraint(
                condition=Q(status=Status.CLOSED, closed_at__isnull=False)
                | (~Q(status=Status.CLOSED) & Q(closed_at__isnull=True)),
                name="request_closed_at",
            ),
            models.CheckConstraint(
                condition=~Q(status__in=(Status.RESOLVED, Status.CLOSED))
                | Q(resolved_at__isnull=False),
                name="request_resolved_at",
            ),
        ]
        indexes = [
            models.Index(fields=["team", "status"], name="request_team_status"),
            models.Index(fields=["assignee", "status"], name="request_assignee_status"),
            models.Index(fields=["client", "-created_at"], name="request_client_created"),
            models.Index(fields=["status", "updated_at"], name="request_status_updated"),
        ]

    def __str__(self):
        return self.number or f"Ticket {self.pk}"

    @property
    def client_status_label(self):
        return CLIENT_STATUS_LABELS[self.status]


class EntryKind(models.TextChoices):
    REPLY = "reply", "Reply"  # visible to the client
    NOTE = "note", "Internal note"
    CONTACT = "contact", "Logged contact"  # internal: a call or message outside the application
    EVENT = "event", "Event"  # written by the services: what changed


class ContactDirection(models.TextChoices):
    TO_CLIENT = "to_client", "To the client"
    FROM_CLIENT = "from_client", "From the client"


class ContactChannel(models.TextChoices):
    PHONE = "phone", "Phone"
    EMAIL = "email", "Email"
    FAX = "fax", "Fax"
    IN_PERSON = "in_person", "In person"
    OTHER = "other", "Other"


class ContactOutcome(models.TextChoices):
    SPOKE_WITH = "spoke_with", "Spoke with"
    MESSAGE_LEFT = "message_left", "Message left"
    NO_ANSWER = "no_answer", "No answer"
    MESSAGE_DELIVERED = "message_delivered", "Message delivered"


# Logged contacts that reached the client: they count as a first response.
REACHED_OUTCOMES = frozenset({ContactOutcome.SPOKE_WITH, ContactOutcome.MESSAGE_DELIVERED})


class AuthorCapacity(models.TextChoices):
    """In which capacity an entry's author wrote it, recorded when it is written and never
    derived again from the author's account: the portal signs laboratory entries with the team
    and the laboratory, and client entries with the client's name."""

    LABORATORY = "laboratory", "The laboratory"
    CLIENT = "client", "The client"


def capacity_of(user):
    """The capacity of an account writing now: a client portal account writes as the client."""
    return AuthorCapacity.CLIENT if user.client_id is not None else AuthorCapacity.LABORATORY


class RequestEntry(models.Model):
    """One entry in a request's timeline. Never edited or deleted (a database trigger refuses
    it); a reply may be withdrawn from client view once, with a reason."""

    request = models.ForeignKey(ServiceRequest, on_delete=models.PROTECT, related_name="entries")
    kind = models.CharField(max_length=10, choices=EntryKind.choices)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="request_entries"
    )
    # Set when the entry is written (desk.services; a database trigger fills it from the author's
    # account at insert time for any other writer) and immutable afterwards (desk_entry_guard).
    author_capacity = models.CharField(
        max_length=12, choices=AuthorCapacity.choices, blank=True, default=""
    )
    body = models.TextField(blank=True)
    contact_direction = models.CharField(
        max_length=20, choices=ContactDirection.choices, blank=True
    )
    contact_channel = models.CharField(max_length=20, choices=ContactChannel.choices, blank=True)
    contact_with = models.CharField(max_length=160, blank=True)
    contact_outcome = models.CharField(max_length=20, choices=ContactOutcome.choices, blank=True)
    # An event's code (created, status_changed, assigned ...) and its data: ids, codes and field
    # names only, never entry text, subjects or person-level keys.
    event_action = models.CharField(max_length=60, blank=True)
    event_data = models.JSONField(default=dict, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawn_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="withdrawn_request_entries",
    )
    withdrawal_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        verbose_name = "ticket entry"
        verbose_name_plural = "ticket entries"
        constraints = [
            models.CheckConstraint(
                condition=Q(kind=EntryKind.CONTACT)
                & ~Q(contact_direction="")
                & ~Q(contact_channel="")
                & ~Q(contact_outcome="")
                | ~Q(kind=EntryKind.CONTACT)
                & Q(contact_direction="", contact_channel="", contact_with="", contact_outcome=""),
                name="request_entry_contact_fields",
            ),
            models.CheckConstraint(
                condition=Q(kind=EntryKind.EVENT) & ~Q(event_action="")
                | ~Q(kind=EntryKind.EVENT) & Q(event_action=""),
                name="request_entry_event_action",
            ),
            models.CheckConstraint(
                condition=Q(kind=EntryKind.EVENT) | ~Q(body=""),
                name="request_entry_body",
            ),
            models.CheckConstraint(
                condition=Q(author_capacity__in=AuthorCapacity.values),
                name="request_entry_author_capacity",
            ),
            models.CheckConstraint(
                condition=Q(withdrawn_at__isnull=True, withdrawn_by__isnull=True)
                | Q(kind=EntryKind.REPLY, withdrawn_at__isnull=False, withdrawn_by__isnull=False),
                name="request_entry_withdrawal",
            ),
        ]
        indexes = [models.Index(fields=["request", "created_at"], name="request_entry_timeline")]

    def __str__(self):
        return f"{self.get_kind_display()} on {self.request}"

    def save(self, *args, **kwargs):
        # A new entry saved without a capacity is written in its author's capacity at this moment
        # (the same rule as the desk_entry_capacity insert trigger, which still covers
        # bulk_create and raw SQL), so the object in memory holds the stored value too.
        if self._state.adding and not self.author_capacity and self.author_id is not None:
            self.author_capacity = capacity_of(self.author)
        super().save(*args, **kwargs)

    def recorded_capacity(self):
        """The capacity stored with the entry. Model saves fill it; a bulk_create or raw insert
        that left it blank had it filled by the database (desk_entry_capacity): read that rather
        than the author's account."""
        if not self.author_capacity and self.pk:
            self.author_capacity = (
                type(self)
                .objects.filter(pk=self.pk)
                .values_list("author_capacity", flat=True)
                .first()
                or ""
            )
        return self.author_capacity


class ComplaintDecision(models.TextChoices):
    INVESTIGATE = "investigate", "Investigate"
    NO_INVESTIGATION = "no_investigation", "No investigation needed"


class ComplaintReview(models.Model):
    """The complaint record of a request that is a complaint: the investigation decision and its
    rationale, findings, the corrective action reference and the independent review. A complaint
    closes only once reviewed (docs/product.md "Complaints and feedback")."""

    request = models.OneToOneField(
        ServiceRequest, on_delete=models.PROTECT, related_name="complaint_review"
    )
    decision = models.CharField(max_length=20, choices=ComplaintDecision.choices, blank=True)
    rationale = models.TextField(blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="complaint_decisions",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    findings = models.TextField("findings and actions", blank=True)
    corrective_action_reference = models.CharField(
        max_length=120,
        blank=True,
        help_text="The number of the corrective action in the laboratory's quality system.",
    )
    outcome_communicated_on = models.DateField(null=True, blank=True)
    outcome_communicated_how = models.CharField(max_length=120, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="complaint_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        verbose_name = "complaint review"
        constraints = [
            models.CheckConstraint(
                condition=Q(decision="", decided_by__isnull=True, decided_at__isnull=True)
                | (~Q(decision="") & Q(decided_by__isnull=False, decided_at__isnull=False)),
                name="complaint_decision_complete",
            ),
            models.CheckConstraint(
                condition=Q(reviewed_by__isnull=True, reviewed_at__isnull=True)
                | Q(
                    reviewed_by__isnull=False,
                    reviewed_at__isnull=False,
                    decided_by__isnull=False,
                )
                & ~Q(reviewed_by=models.F("decided_by")),
                name="complaint_review_independent",
                violation_error_message="The reviewer must not be the person who decided.",
            ),
        ]

    def __str__(self):
        return f"Complaint record of {self.request}"

    @property
    def is_reviewed(self):
        return self.reviewed_at is not None


class ViewSharing(models.TextChoices):
    PRIVATE = "private", "Only me"
    TEAM = "team", "A team"
    EVERYONE = "everyone", "Everyone"


class SavedView(models.Model):
    """A named set of request-list filters and a sort. Its filters are the list's own filters,
    validated on save and on use, so a view never shows its reader more than the list would."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="saved_views"
    )
    name = models.CharField(max_length=80)
    shared_with = models.CharField(
        max_length=10, choices=ViewSharing.choices, default=ViewSharing.PRIVATE
    )
    team = models.ForeignKey(
        "teams.Team", null=True, blank=True, on_delete=models.PROTECT, related_name="saved_views"
    )
    filters = models.JSONField(default=dict, blank=True)
    sort = models.CharField(max_length=40, blank=True)
    position = models.PositiveSmallIntegerField(default=0)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["position", "name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "name"],
                name="saved_view_name_unique",
                violation_error_message="You already have a view with this name.",
            ),
            models.CheckConstraint(
                condition=Q(shared_with=ViewSharing.TEAM, team__isnull=False)
                | (~Q(shared_with=ViewSharing.TEAM) & Q(team__isnull=True)),
                name="saved_view_team",
                violation_error_message="Choose the team a team view is shared with.",
            ),
        ]

    def __str__(self):
        return self.name


# Emergency access (desk.emergency) -----------------------------------------------------------

EMERGENCY_PREFIX = "EA-"


class EmergencyKind(models.TextChoices):
    """What an emergency access use gives on its ticket."""

    PATIENT_INFORMATION = "patient_information", "Patient information on the ticket"


class EmergencyReason(models.TextChoices):
    """Why a person used emergency access: a code, so the change history never holds free text
    about a patient. The person explains in their own words as well (`explanation`)."""

    CRITICAL_RESULT = "critical_result", "Reporting a critical or urgent result"
    TREATING_CLINICIAN = "treating_clinician", "An urgent request from someone treating the patient"
    SAMPLE_IDENTITY = "sample_identity", "An urgent problem identifying a sample or a patient"
    OTHER = "other", "Another emergency"


class EmergencyOutcome(models.TextChoices):
    APPROPRIATE = "appropriate", "Appropriate"
    NOT_APPROPRIATE = "not_appropriate", "Not appropriate: follow-up needed"


class EmergencyAccess(models.Model):
    """One use of emergency access (desk.emergency): a person without the usual grant gave a reason
    and, from `started_at` until `ends_at` (by the database's clock), may reveal the patient
    information on one ticket. Who it was, by name, username and roles, is kept as it stood when
    they started. Written once: the database refuses to change or delete a row (migration 0008),
    and a review is a row of its own (EmergencyReview). reset_demo empties the table with TRUNCATE,
    which row triggers do not see."""

    id = models.BigAutoField(primary_key=True)
    # By id, as for the access records: a record outlives what it names.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="+",
    )
    user_name = models.TextField()
    user_username = models.CharField(max_length=150)
    user_roles = models.CharField(max_length=200)
    ticket = models.ForeignKey(
        ServiceRequest, on_delete=models.PROTECT, related_name="emergency_uses"
    )
    kind = models.CharField(max_length=24, choices=EmergencyKind.choices)
    reason = models.CharField(max_length=24, choices=EmergencyReason.choices)
    explanation = models.TextField(max_length=1000)
    # The database's clock at the start of the transaction that recorded it, and the end it gave.
    started_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    request_ref = models.CharField("request ID", max_length=32, blank=True)

    class Meta:
        # Written only by desk.emergency: no model permission to grant, and no admin.
        default_permissions = ()
        verbose_name = "emergency access"
        verbose_name_plural = "emergency access uses"
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["user", "ticket", "-ends_at"], name="emergency_in_force"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=EmergencyKind.values), name="emergency_kind"
            ),
            models.CheckConstraint(
                condition=Q(reason__in=EmergencyReason.values), name="emergency_reason"
            ),
            models.CheckConstraint(
                condition=Q(ends_at__gt=models.F("started_at")), name="emergency_ends_later"
            ),
            models.CheckConstraint(condition=~Q(explanation=""), name="emergency_explained"),
        ]

    def __str__(self):
        return self.number

    @property
    def number(self):
        return f"{EMERGENCY_PREFIX}{self.pk:06d}" if self.pk else ""


class EmergencyReview(models.Model):
    """The review of one emergency access use, recorded once it has ended by someone other than the
    person who used it (audit_review.emergency): the outcome, a note, and who reviewed, by name,
    username and roles as they stood, and when (the database's clock). The database refuses a
    review by the person who used it or of a use still in force (migration 0008), and to change or
    delete a row."""

    use = models.OneToOneField(
        EmergencyAccess, on_delete=models.PROTECT, primary_key=True, related_name="review"
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="+",
    )
    reviewer_name = models.TextField()
    reviewer_username = models.CharField(max_length=150)
    reviewer_roles = models.CharField(max_length=200)
    outcome = models.CharField(max_length=20, choices=EmergencyOutcome.choices)
    note = models.TextField(max_length=2000)
    reviewed_at = models.DateTimeField()
    request_ref = models.CharField("request ID", max_length=32, blank=True)

    class Meta:
        default_permissions = ()
        verbose_name = "emergency access review"
        constraints = [
            models.CheckConstraint(
                condition=Q(outcome__in=EmergencyOutcome.values), name="emergency_review_outcome"
            ),
            models.CheckConstraint(condition=~Q(note=""), name="emergency_review_noted"),
        ]

    def __str__(self):
        return f"Review of {self.use_id}"
