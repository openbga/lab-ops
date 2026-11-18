"""Nonconformance and corrective action: an optional module (core.modules key "nonconformance").

A nonconformance (`NC-000123`) records a problem in the laboratory's work: where it came from,
what was done to contain it, whether results already reported were affected and whether the
client was told, its root cause, the actions taken with a person and a due date, and a check
that they worked. Someone other than its owner closes it. Changes go through
nonconformance.services, each audited with the values before and after; reads through
nonconformance.selectors. docs/product.md "Nonconformance and corrective action".
"""

from django.conf import settings
from django.db import models
from django.db.models import F, Q

NUMBER_PREFIX = "NC-"


class Status(models.TextChoices):
    OPEN = "open", "Open"
    INVESTIGATING = "investigating", "Investigating"
    CORRECTIVE_ACTION = "corrective_action", "Corrective action"
    VERIFICATION = "verification", "Verification"
    CLOSED = "closed", "Closed"


ACTIVE_STATUSES = (
    Status.OPEN,
    Status.INVESTIGATING,
    Status.CORRECTIVE_ACTION,
    Status.VERIFICATION,
)


class Source(models.TextChoices):
    COMPLAINT = "complaint", "Complaint"
    INTERNAL = "internal", "Found internally"
    AUDIT = "audit", "Audit finding"
    PROFICIENCY = "proficiency", "Proficiency testing"
    EQUIPMENT = "equipment", "Equipment"
    EXTERNAL_ASSESSMENT = "external_assessment", "External assessment"
    IMPROVEMENT = "improvement", "Preventive action or improvement"
    OTHER = "other", "Other"


class Severity(models.TextChoices):
    """The laboratory's own grading; its procedures say what each level means."""

    MINOR = "minor", "Minor"
    MAJOR = "major", "Major"
    CRITICAL = "critical", "Critical"


class Impact(models.TextChoices):
    """The decision on results the laboratory has already reported."""

    NONE = "none", "No reported results affected"
    AFFECTED = "affected", "Reported results affected"
    NOT_APPLICABLE = "not_applicable", "Does not concern results"


class ClientNotice(models.TextChoices):
    TOLD = "told", "Client told"
    NOT_NEEDED = "not_needed", "No need to tell a client"


class Effectiveness(models.TextChoices):
    EFFECTIVE = "effective", "Effective"
    NOT_EFFECTIVE = "not_effective", "Not effective"
    NO_ACTION_NEEDED = "no_action_needed", "No corrective action needed"


# Effectiveness results that let a supervisor close the record.
CLOSABLE_RESULTS = frozenset({Effectiveness.EFFECTIVE, Effectiveness.NO_ACTION_NEEDED})


class Nonconformance(models.Model):
    number = models.CharField(
        max_length=20,
        blank=True,
        editable=False,
        help_text="NC- and six digits from the record's sequence; means nothing else.",
    )
    title = models.CharField(max_length=160)
    description = models.TextField("what happened", max_length=4000)
    source = models.CharField(max_length=30, choices=Source.choices)
    severity = models.CharField(max_length=20, choices=Severity.choices)
    area = models.CharField(
        "area or section",
        max_length=80,
        blank=True,
        help_text="Where in the laboratory it happened, in the laboratory's own terms.",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="owned_nonconformances",
    )
    # The complaint ticket it was opened from, if any: at most one record per complaint.
    source_request = models.ForeignKey(
        "desk.ServiceRequest",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="nonconformances_opened",
    )
    raised_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="raised_nonconformances"
    )
    raised_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    containment = models.TextField(blank=True, max_length=4000)
    root_cause = models.TextField(blank=True, max_length=4000)
    similar_cases = models.TextField(
        "similar cases",
        blank=True,
        max_length=4000,
        help_text="Whether similar nonconformities exist or could occur, and what was checked.",
    )

    impact = models.CharField(max_length=20, choices=Impact.choices, blank=True)
    impact_assessment = models.TextField("orders and reports reviewed", blank=True, max_length=4000)
    impact_recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    impact_recorded_at = models.DateTimeField(null=True, blank=True)
    client_notice = models.CharField(max_length=20, choices=ClientNotice.choices, blank=True)
    client_told_on = models.DateField(null=True, blank=True)
    client_told_how = models.CharField(max_length=160, blank=True)
    client_told_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    effectiveness = models.CharField(max_length=20, choices=Effectiveness.choices, blank=True)
    effectiveness_note = models.TextField(blank=True, max_length=4000)
    effectiveness_checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    effectiveness_checked_at = models.DateTimeField(null=True, blank=True)

    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    closure_note = models.TextField(blank=True, max_length=4000)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-raised_at", "-pk"]
        verbose_name = "nonconformance"
        constraints = [
            models.UniqueConstraint(
                fields=["number"], condition=~Q(number=""), name="nonconformance_number_unique"
            ),
            models.UniqueConstraint(
                fields=["source_request"],
                condition=Q(source_request__isnull=False),
                name="nonconformance_one_per_complaint",
                violation_error_message="This complaint already has a nonconformance.",
            ),
            models.CheckConstraint(
                condition=Q(status=Status.CLOSED, closed_at__isnull=False, closed_by__isnull=False)
                | (~Q(status=Status.CLOSED) & Q(closed_at__isnull=True, closed_by__isnull=True)),
                name="nonconformance_closed",
            ),
            models.CheckConstraint(
                condition=Q(closed_by__isnull=True)
                | Q(owner__isnull=False) & ~Q(closed_by=F("owner")),
                name="nonconformance_closed_by_other",
                violation_error_message="Someone other than the owner closes a nonconformance.",
            ),
            models.CheckConstraint(
                condition=Q(effectiveness="", effectiveness_checked_by__isnull=True)
                | (~Q(effectiveness="") & Q(effectiveness_checked_by__isnull=False)),
                name="nonconformance_effectiveness_complete",
            ),
            models.CheckConstraint(
                condition=Q(impact="", impact_recorded_by__isnull=True)
                | (~Q(impact="") & Q(impact_recorded_by__isnull=False)),
                name="nonconformance_impact_complete",
            ),
            models.CheckConstraint(
                condition=~Q(client_notice=ClientNotice.TOLD)
                | Q(client_told_on__isnull=False, client_told_by__isnull=False),
                name="nonconformance_client_told_complete",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "-raised_at"], name="nonconformance_status"),
            models.Index(fields=["owner", "status"], name="nonconformance_owner"),
        ]

    def __str__(self):
        return self.number or f"Nonconformance {self.pk}"

    @property
    def is_closed(self):
        return self.status == Status.CLOSED


class ActionStatus(models.TextChoices):
    OPEN = "open", "Open"
    DONE = "done", "Done"
    CANCELLED = "cancelled", "Cancelled"


class CorrectiveAction(models.Model):
    """One action taken on a nonconformance, with the person who does it and its due date."""

    nonconformance = models.ForeignKey(
        Nonconformance, on_delete=models.PROTECT, related_name="actions"
    )
    description = models.TextField(max_length=2000)
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="corrective_actions"
    )
    due_on = models.DateField()
    status = models.CharField(
        max_length=20, choices=ActionStatus.choices, default=ActionStatus.OPEN
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    finished_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    finished_at = models.DateTimeField(null=True, blank=True)
    finish_note = models.TextField(blank=True, max_length=2000)

    class Meta:
        ordering = ["due_on", "pk"]
        verbose_name = "corrective action"
        constraints = [
            models.CheckConstraint(
                condition=Q(status=ActionStatus.OPEN, finished_at__isnull=True)
                | (~Q(status=ActionStatus.OPEN) & Q(finished_at__isnull=False)),
                name="corrective_action_finished",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "due_on"], name="corrective_action_due"),
            models.Index(fields=["assignee", "status"], name="corrective_action_assignee"),
        ]

    def __str__(self):
        return f"Action {self.pk} on {self.nonconformance}"


class TicketLink(models.Model):
    """A ticket a nonconformance concerns. Removing one keeps the row with who removed it and
    why."""

    nonconformance = models.ForeignKey(
        Nonconformance, on_delete=models.PROTECT, related_name="ticket_links"
    )
    request = models.ForeignKey(
        "desk.ServiceRequest", on_delete=models.PROTECT, related_name="nonconformance_links"
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    added_at = models.DateTimeField(auto_now_add=True)
    removed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    removed_at = models.DateTimeField(null=True, blank=True)
    removal_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["added_at", "pk"]
        verbose_name = "linked ticket"
        constraints = [
            models.UniqueConstraint(
                fields=["nonconformance", "request"],
                condition=Q(removed_at__isnull=True),
                name="nonconformance_ticket_link_unique",
                violation_error_message="This ticket is already linked.",
            ),
            models.CheckConstraint(
                condition=Q(removed_at__isnull=True, removed_by__isnull=True)
                | Q(removed_at__isnull=False, removed_by__isnull=False),
                name="nonconformance_ticket_link_removed",
            ),
        ]
