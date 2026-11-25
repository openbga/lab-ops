from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Now
from django.utils import timezone


class Source(models.TextChoices):
    """What a workspace use is about: the tabs of the audit review workspace, four lists
    (audit_review.sources), the summary by person (audit_review.people) and the review records,
    and the two tabs of Access review (audit_review.access_report)."""

    CHANGES = "changes", "Changes"
    ACCESS = "access", "Access records"
    SIGN_INS = "sign_ins", "Sign-ins"
    # Counts by person over a period (audit_review.people), not a list of records.
    PEOPLE = "people", "People"
    USES = "uses", "Workspace use"
    # The review records (Review): their list, their pages and their export.
    REVIEWS = "reviews", "Reviews"
    # The Access review section (audit_review.access_report): every account and what it holds,
    # and the accounts unused for the laboratory's period. Their uses are recorded here too.
    ACCOUNTS = "accounts", "Access review: accounts"
    UNUSED = "unused", "Access review: unused accounts"


class UseKind(models.TextChoices):
    VIEWED = "viewed", "Viewed a page"
    EXPORTED = "exported", "Exported"


class WorkspaceUse(models.Model):
    """One use of the audit review workspace or of Access review: a page of one of its lists shown,
    or a list exported (audit_review.views, audit_review.access_views). Who, when, which list, the
    filters as the form accepted them (ids, codes and dates, never text as typed), how many rows
    matched and the request ID.

    History: the database refuses to change or delete a row (migration 0001); reset_demo empties
    the table with TRUNCATE, which row triggers do not see. Written with the answer it is about
    (core.access.keep), so a page or an export that is not sent leaves none, and one whose record
    cannot be written is not sent."""

    Source = Source
    Kind = UseKind

    id = models.BigAutoField(primary_key=True)
    # By the database's clock, when the record is written, just after the page or file was made.
    occurred_at = models.DateTimeField(db_default=Now())
    # The account, by id, as for access records (core.models.AccessRecord): a record outlives its
    # account, and writing one waits for nothing on the account's row.
    account = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        db_index=False,  # workspace_use_account leads with it
        related_name="+",
    )
    kind = models.CharField(max_length=12, choices=UseKind.choices)
    source = models.CharField(max_length=12, choices=Source.choices)
    # The filters the form accepted, by name: ids, codes, dates and the page number, as strings.
    filters = models.JSONField(default=dict)
    # How many rows the filters matched (an export writes them all).
    rows = models.PositiveIntegerField()
    request_ref = models.CharField("request ID", max_length=32, blank=True)

    class Meta:
        # Written only by audit_review.views: no model permission to grant, and no admin.
        default_permissions = ()
        indexes = [
            models.Index(fields=["-occurred_at"], name="workspace_use_recent"),
            models.Index(fields=["account", "-occurred_at"], name="workspace_use_account"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(kind__in=UseKind.values), name="workspace_use_kind"
            ),
            models.CheckConstraint(
                condition=models.Q(source__in=Source.values), name="workspace_use_source"
            ),
        ]

    def __str__(self):
        return f"account {self.account_id} {self.kind} {self.source}"


# ------------------------------------------------------------------------------ Review records

NUMBER_PREFIX = "AR-"
# What a review may say it examined: the workspace's lists (Source), the record list aside.
EXAMINED = (Source.CHANGES, Source.ACCESS, Source.SIGN_INS, Source.PEOPLE, Source.USES)


class Outcome(models.TextChoices):
    NOTHING = "nothing", "Nothing that needs follow-up"
    FOLLOW_UP = "follow_up", "Findings that need follow-up"


class Meaning(models.TextChoices):
    """What a sign-off means. A review is signed as reviewed by the person who carried it out."""

    REVIEWED = "reviewed", "Reviewed"


class Review(models.Model):
    """A review of the activity records, signed by the person who carried it out
    (audit_review.reviews): the period it covers, the lists and other records examined, the
    outcome, the findings and the follow-up, and the sign-off: who signed, by name, username and
    roles as they stood when they signed, what the signature means, when, and a SHA-256 checksum of
    exactly what was signed (reviews.signed_content). Written once, signed: the database refuses to
    change or delete a row (migration 0003). What comes later is added as a note (ReviewNote).
    reset_demo empties the table with TRUNCATE, which row triggers do not see."""

    Outcome = Outcome
    Meaning = Meaning

    id = models.BigAutoField(primary_key=True)
    number = models.CharField(max_length=20, unique=True)
    # The days covered, in the deployment's time zone, both included.
    period_from = models.DateField("from")
    period_to = models.DateField("to")
    # The workspace's lists examined (EXAMINED), in that order.
    examined = ArrayField(models.CharField(max_length=12), default=list, blank=True)
    examined_other = models.TextField("other records examined", blank=True, max_length=2000)
    outcome = models.CharField(max_length=12, choices=Outcome.choices)
    findings = models.TextField(blank=True, max_length=4000)
    follow_up = models.TextField("follow-up", blank=True, max_length=4000)
    # The signer, by id as for access records: a record outlives its account. Its name, username
    # and roles are kept as they stood when it signed: they are part of what was signed.
    signed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="+",
    )
    signer_name = models.TextField()
    signer_username = models.CharField(max_length=150)
    signer_roles = models.CharField(max_length=200)
    meaning = models.CharField(max_length=12, choices=Meaning.choices, default=Meaning.REVIEWED)
    # By the application's clock, as change events are timed.
    signed_at = models.DateTimeField()
    checksum = models.CharField("SHA-256 checksum", max_length=64)
    request_ref = models.CharField("request ID", max_length=32, blank=True)

    class Meta:
        # Written only by audit_review.reviews: no model permission to grant, and no admin.
        default_permissions = ()
        verbose_name = "audit review"
        ordering = ["-id"]
        indexes = [models.Index(fields=["-period_to"], name="review_period_to")]
        constraints = [
            models.CheckConstraint(
                condition=Q(period_from__lte=F("period_to")), name="review_period_order"
            ),
            models.CheckConstraint(condition=Q(outcome__in=Outcome.values), name="review_outcome"),
            models.CheckConstraint(condition=Q(meaning__in=Meaning.values), name="review_meaning"),
            models.CheckConstraint(
                condition=~Q(outcome=Outcome.FOLLOW_UP) | ~Q(findings=""),
                name="review_findings_with_follow_up",
            ),
            models.CheckConstraint(
                condition=~Q(examined=[]) | ~Q(examined_other=""), name="review_examined"
            ),
            models.CheckConstraint(
                condition=Q(examined__contained_by=[source.value for source in EXAMINED]),
                name="review_examined_lists",
            ),
            models.CheckConstraint(
                condition=Q(checksum__regex=r"^[0-9a-f]{64}$"), name="review_checksum"
            ),
        ]

    def __str__(self):
        return self.number

    def examined_labels(self):
        labels = dict(Source.choices)
        return [labels.get(code, code) for code in self.examined]


class ReviewNote(models.Model):
    """A note added to a signed review: a correction, or what the follow-up came to. Not part of
    what was signed. Who added it (by name as it stood then) and when. The database refuses to
    change or delete a row (migration 0003)."""

    id = models.BigAutoField(primary_key=True)
    review = models.ForeignKey(Review, on_delete=models.PROTECT, related_name="notes")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="+",
    )
    author_name = models.TextField()
    text = models.TextField(max_length=4000)
    # By the application's clock, as change events and sign-offs are timed.
    added_at = models.DateTimeField(default=timezone.now)
    request_ref = models.CharField("request ID", max_length=32, blank=True)

    class Meta:
        default_permissions = ()
        verbose_name = "audit review note"
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(condition=~Q(text=""), name="review_note_text"),
        ]

    def __str__(self):
        return f"Note {self.pk} on {self.review_id}"


class ReviewSchedule(models.Model):
    """How often the laboratory reviews its activity records, in days (audit_review.reviews): the
    next review is due that many days after the last one was signed. One row, written the first
    time a reviewer saves the interval, never by a migration, so an empty database stays empty
    (core.demo); until then no interval is set. `revision` is the stale-edit check of the form."""

    interval_days = models.PositiveSmallIntegerField("review every", null=True, blank=True)
    revision = models.PositiveIntegerField(default=0)
    changed_at = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()
        verbose_name = "audit review interval"
        constraints = [
            models.CheckConstraint(condition=Q(id=1), name="review_schedule_single"),
            models.CheckConstraint(
                condition=Q(interval_days__isnull=True)
                | Q(interval_days__gte=1, interval_days__lte=366),
                name="review_schedule_interval_bounds",
                violation_error_message="Choose from 1 to 366 days.",
            ),
        ]

    def __str__(self):
        return "Review interval"


class UnusedPeriod(models.Model):
    """After how many days without a sign-in an active account is listed as unused
    (audit_review.access_report). One row, written the first time a reviewer saves the period,
    never by a migration, so an empty database stays empty (core.demo); until then none is set and
    nothing is listed as unused. `revision` is the stale-edit check of the form."""

    days = models.PositiveSmallIntegerField("unused after", null=True, blank=True)
    revision = models.PositiveIntegerField(default=0)
    changed_at = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()
        verbose_name = "unused account period"
        constraints = [
            models.CheckConstraint(condition=Q(id=1), name="unused_period_single"),
            models.CheckConstraint(
                condition=Q(days__isnull=True) | Q(days__gte=1, days__lte=366),
                name="unused_period_bounds",
                violation_error_message="Choose from 1 to 366 days.",
            ),
        ]

    def __str__(self):
        return "Unused account period"
