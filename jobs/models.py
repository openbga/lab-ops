"""Scheduled jobs and the notification outbox (docs/product.md "Notifications").

- `Notice`: one email notice to one account, recorded in the transaction of the change that raised
  it and kept from then to its delivery. It holds ids, codes and the record's number only; what
  it says is decided again when it is sent (jobs.outbox).
- `JobState`: the last run of each scheduled job, so administrators can see whether the
  scheduler runs at all. One row per job, overwritten by each run; it is not history.
- `JobSettings`: the laboratory's settings for the jobs, and the account the jobs act as.
"""

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

# How many days a resolved ticket may stay resolved before the scheduled job closes it.
CLOSE_AFTER_DAYS_MAX = 365


class NoticeStatus(models.TextChoices):
    WAITING = "waiting", "Waiting to send"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    NOT_SENT = "not_sent", "Not sent"


class NotSentReason(models.TextChoices):
    NO_LONGER_APPLIES = "no_longer_applies", "No longer applies"
    MAIL_OFF = "mail_off", "Email is off on this deployment"


class Notice(models.Model):
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    # Which handler in jobs.registry.NOTICE_TOPICS checks and words this notice.
    topic = models.CharField(max_length=40)
    # The record the notice is about, for the handler to read again at sending.
    object_id = models.PositiveBigIntegerField()
    # The record's number (REQ-000123): the only record data a notice carries.
    reference = models.CharField(max_length=20)
    # Event codes the change raised for this recipient; the handler picks the one it sends.
    events = models.JSONField(default=list)
    # Ids and codes only (the replies an event is about, a clock and its due time), never text.
    data = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=10, choices=NoticeStatus.choices, default=NoticeStatus.WAITING
    )
    not_sent_reason = models.CharField(max_length=20, choices=NotSentReason.choices, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    # The class of the last delivery error only: a mail server's message can echo addresses.
    last_error = models.CharField(max_length=80, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        verbose_name = "email notice"
        indexes = [
            models.Index(
                fields=["next_attempt_at"],
                condition=Q(status="waiting"),
                name="notice_waiting_due",
            ),
            models.Index(fields=["status", "-created_at"], name="notice_status_created"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(status="sent", sent_at__isnull=False)
                | (~Q(status="sent") & Q(sent_at__isnull=True)),
                name="notice_sent_at",
            ),
            models.CheckConstraint(
                condition=Q(status="not_sent") & ~Q(not_sent_reason="")
                | ~Q(status="not_sent") & Q(not_sent_reason=""),
                name="notice_not_sent_reason",
            ),
        ]

    def __str__(self):
        return f"Notice {self.pk} about {self.reference}"


class JobOutcome(models.TextChoices):
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"


class JobState(models.Model):
    """The last run of one scheduled job (jobs.runner)."""

    name = models.CharField(max_length=60, unique=True)
    last_started_at = models.DateTimeField(null=True, blank=True)
    last_finished_at = models.DateTimeField(null=True, blank=True)
    last_succeeded_at = models.DateTimeField(null=True, blank=True)
    last_outcome = models.CharField(max_length=10, choices=JobOutcome.choices, blank=True)
    # The class of the last error only; the log has the frames.
    last_error = models.CharField(max_length=80, blank=True)
    # What the last run did, as counts (closed: 3, sent: 12).
    last_counts = models.JSONField(default=dict, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["name"]
        verbose_name = "scheduled job state"

    def __str__(self):
        return self.name


class JobSettings(models.Model):
    """The one row of settings for the scheduled jobs. Created when first saved or needed; a
    database without it has the defaults (nothing closes automatically)."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    close_resolved_after_days = models.PositiveSmallIntegerField(
        "close resolved tickets after (days)",
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(CLOSE_AFTER_DAYS_MAX)],
        help_text="Empty: resolved tickets stay resolved until someone closes them.",
    )
    # The account the scheduled jobs act as in timelines and audit events (jobs.actor).
    system_account = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        verbose_name = "scheduled job settings"
        verbose_name_plural = "scheduled job settings"
        constraints = [
            models.CheckConstraint(condition=Q(id=1), name="job_settings_single_row"),
            models.CheckConstraint(
                condition=Q(close_resolved_after_days__isnull=True)
                | Q(close_resolved_after_days__gte=1, close_resolved_after_days__lte=365),
                name="job_settings_close_after_range",
            ),
        ]

    def __str__(self):
        return "Scheduled job settings"
