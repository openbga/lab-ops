"""The Emergency access tab of Access review: each use of emergency access (desk.emergency), its
reveals and its review, for holders of "Can review access and audit records"
(audit_review.permissions).

A use is In force until its end has passed by the database's clock, then Waiting for review until
someone records a review (desk.models.EmergencyReview). `record_review` holds the acting account,
locks the use's row so two reviews of one use take turns, and decides from the account and the use
as they stand: an active staff account with a laboratory role and the review capability, not the
person who used the access, a use that has ended and has no review yet. It records the review and
a change event (`emergency_access.reviewed`, with the outcome, never the note) in one transaction.
The database refuses a review by the person who used it or of a use still in force (desk migration
0008).

The reveals a use gave are read from the change history: the events of revealing a patient-marked
answer or a person-level key that name the use (`emergency` in `after`), described by the field's
label or the link's record type as they stand now, never by a value.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import BooleanField, Case, Exists, OuterRef, Value, When

from accounts.acting import acting_change
from accounts.grant_ends import TransactionTime
from core.models import AuditEvent
from desk.models import EmergencyAccess, EmergencyOutcome, EmergencyReview

from .permissions import may_review

NOTE_LIMIT = 2000
REVEAL_ACTIONS = ("request_form.value_revealed", "external_link.person_revealed")


class Status:
    IN_FORCE = "in_force"
    WAITING = "waiting"
    REVIEWED = "reviewed"


STATUS_LABELS = {
    Status.IN_FORCE: "In force",
    Status.WAITING: "Waiting for review",
    Status.REVIEWED: "Reviewed",
}
STATUS_CHOICES = [("", "Any status"), *STATUS_LABELS.items()]

OWN_USE = "Someone other than the person who used emergency access reviews it."
IN_FORCE = "This use is still in force. Review it once it has ended."
ALREADY_REVIEWED = (
    "Someone recorded a review of this use after this page was opened. Their review is shown below."
)
NOTE_REQUIRED = "Say what you checked and found, without patient information."
OUTCOME_REQUIRED = "Choose the outcome."


def uses(status=""):
    """Every use, newest first, with `in_force` and `reviewed` worked out in the same statement
    (the end against the database's clock at the start of the transaction), narrowed to
    `status`."""
    reviewed = Exists(EmergencyReview.objects.filter(use=OuterRef("pk")))
    rows = EmergencyAccess.objects.annotate(
        in_force=Case(
            When(ends_at__gt=TransactionTime(), then=Value(True)),
            default=Value(False),
            output_field=BooleanField(),
        ),
        reviewed=reviewed,
    ).select_related("ticket", "review")
    if status == Status.IN_FORCE:
        rows = rows.filter(ends_at__gt=TransactionTime())
    elif status == Status.WAITING:
        rows = rows.filter(ends_at__lte=TransactionTime()).exclude(reviewed)
    elif status == Status.REVIEWED:
        rows = rows.filter(reviewed)
    return rows.order_by("-pk")


def status_of(use):
    """The status of a use read through `uses`."""
    if use.reviewed:
        return Status.REVIEWED
    return Status.IN_FORCE if use.in_force else Status.WAITING


def waiting_count():
    return uses(Status.WAITING).count()


def reveals(use):
    """[(time, description)] of what was revealed under `use`, oldest first."""
    from links.models import LinkAttachment
    from request_forms.models import FieldValue

    events = list(
        AuditEvent.objects.filter(
            actor_id=use.user_id, action__in=REVEAL_ACTIONS, after__emergency=use.pk
        ).order_by("occurred_at", "pk")
    )
    value_ids = {e.after.get("value_id") for e in events if e.action == REVEAL_ACTIONS[0]}
    attachment_ids = {e.after.get("attachment") for e in events if e.action == REVEAL_ACTIONS[1]}
    values = {
        row.pk: row.field.label
        for row in FieldValue.objects.filter(
            pk__in=[i for i in value_ids if i is not None]
        ).select_related("field")
    }
    links = {
        row.pk: row.link.get_record_type_display()
        for row in LinkAttachment.objects.filter(
            pk__in=[i for i in attachment_ids if i is not None]
        ).select_related("link")
    }
    rows = []
    for event in events:
        if event.action == REVEAL_ACTIONS[0]:
            label = values.get(event.after.get("value_id"))
            what = f"Patient-marked answer: {label}" if label else "A patient-marked answer"
        else:
            label = links.get(event.after.get("attachment"))
            what = f"Person-level key: {label}" if label else "A person-level key"
        rows.append((event.occurred_at, what))
    return rows


# Recording a review ------------------------------------------------------------------------


def clean_outcome(outcome):
    if outcome not in EmergencyOutcome.values:
        raise ValidationError({"outcome": OUTCOME_REQUIRED})
    return outcome


def clean_note(note):
    note = (note or "").strip()
    if not note:
        raise ValidationError({"note": NOTE_REQUIRED})
    if len(note) > NOTE_LIMIT:
        raise ValidationError({"note": f"Keep it to {NOTE_LIMIT} characters or fewer."})
    return note


@acting_change
def record_review(*, actor, use_id, outcome, note):
    """Record the review of use `use_id` by `actor`. Raises EmergencyAccess.DoesNotExist,
    PermissionDenied when the account may not review, and ValidationError when the use is the
    account's own, still in force or reviewed already, or the outcome or note is refused."""
    from core.audit import record_event
    from core.request_logging import current_request_id
    from desk.services import current_staff

    from .reviews import roles_label

    outcome = clean_outcome(outcome)
    note = clean_note(note)
    # After the hold (acting_change): two reviews of one use take turns here.
    use = EmergencyAccess.objects.select_for_update(of=("self",)).filter(pk=use_id).first()
    if use is None:
        raise EmergencyAccess.DoesNotExist("No such use of emergency access.")
    actor = current_staff(actor)  # the account as held, not the request's copy
    if not may_review(actor):
        raise PermissionDenied(
            "This needs an explicit permission: Can review access and audit records."
        )
    if use.user_id == actor.pk:
        raise ValidationError(OWN_USE)
    if EmergencyReview.objects.filter(use=use).exists():
        raise ValidationError(ALREADY_REVIEWED)
    if EmergencyAccess.objects.filter(pk=use.pk, ends_at__gt=TransactionTime()).exists():
        raise ValidationError(IN_FORCE)
    review = EmergencyReview.objects.create(
        use=use,
        reviewed_by=actor,
        reviewer_name=actor.get_full_name() or actor.get_username(),
        reviewer_username=actor.get_username(),
        reviewer_roles=roles_label(actor),
        outcome=outcome,
        note=note,
        reviewed_at=TransactionTime(),
        request_ref=current_request_id(),
    )
    review.refresh_from_db(fields=["reviewed_at"])
    record_event(
        actor=actor,
        action="emergency_access.reviewed",
        obj=use,
        after={"outcome": outcome, "reviewed_at": review.reviewed_at.isoformat()},
    )
    return review
