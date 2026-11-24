from django.conf import settings
from django.db import models
from django.db.models.functions import Now


class Source(models.TextChoices):
    """The lists of the audit review workspace (audit_review.sources)."""

    CHANGES = "changes", "Changes"
    ACCESS = "access", "Access records"
    SIGN_INS = "sign_ins", "Sign-ins"
    USES = "uses", "Workspace use"


class UseKind(models.TextChoices):
    VIEWED = "viewed", "Viewed a page"
    EXPORTED = "exported", "Exported"


class WorkspaceUse(models.Model):
    """One use of the audit review workspace: a page of one of its lists shown, or a list exported
    (audit_review.views). Who, when, which list, the filters as the form accepted them (ids, codes
    and dates, never text as typed), how many rows matched and the request ID.

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
