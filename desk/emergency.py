"""Emergency access: patient information on one ticket without the usual grant (docs/product.md
"Emergency access").

Who. Staff the laboratory names as eligible hold the capability "Can use emergency access"
(accounts.roles.EMERGENCY_CAPABILITY), given to a person or a role group in the admin console like
the others; nobody holds it until the laboratory names someone, so the feature is off until then.
A client portal account holds no capability, so it never has emergency access. Staff who can
view patient information (view_patient_information) need none and cannot start one.

What. A use (desk.models.EmergencyAccess) names one ticket the person may read and gives, from
when it starts until MINUTES later by the database's clock, what view_patient_information gives on
that ticket only: its patient-marked answers and its person-level keys, each revealed one at a
time with its own record (request_forms.services.reveal_value, links.services.reveal_person_key;
their change events carry the use's id as `emergency`). It gives nothing else: no other ticket, no
search by a patient key, no link page, no change to patient fields or person-level links.

Every check (`patient_grant`) reads the use in the statement that compares its end with the
database's clock at the start of the transaction (accounts.grant_ends.TransactionTime), and the
capability as the account holds it then: a use gives something only while the account may use
emergency access, so removing the capability, the account's staff role or the account itself
stops it at once (given back before the use's end, the use gives again until its end), and a
reveal under way holds them (accounts.acting.hold_actor) until it commits, as for every grant. A
page drawn while a use was in force offers Reveal; a reveal sent after the end is refused as one
by anyone without the grant.

Starting. `start` holds the acting account, takes the account's own lock (START_LOCK) so two
starts of one person take turns, and decides from the account and the ticket as they stand then:
an active staff account with a laboratory role and the capability, without view_patient_information,
on a ticket it may read (desk.selectors.staff_requests) that holds patient information hidden
from it (`has_hidden_patient_information`), with no use of its own in force on it. It records the
use with the reason (a code) and the person's explanation, a change event
(`emergency_access.started`, with the ticket, the kind, the reason code, the start and the end,
never the explanation), and a notice to each supervisor and each holder of "Can review access
and audit records", the person excepted (TOPIC), in one transaction.

Told. The notice is worded from desk.notifications' allow-list (the ticket number, a fixed phrase,
the laboratory's name and a sign-in link): to a reviewer, the use's page under Access review; to a
supervisor who is not one, the ticket's Who opened it page. Checked again at sending
(`prepare_emergency_notice`): the recipient still an active staff account with an address and
still a supervisor or a reviewer. Sent whatever the person chose for their own email notices, and
only where the deployment sends email (jobs.outbox): it is how the laboratory learns at once that
the usual grant was set aside.

Reviewed. Each use waits on Access review's Emergency access tab until someone other than the
person records a review, once it has ended (audit_review.emergency; the database refuses any
other review, desk migration 0008).
"""

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.db.models import DateTimeField, ExpressionWrapper, Q, Value

from accounts.acting import acting_change
from accounts.grant_ends import TransactionTime
from accounts.roles import EMERGENCY_CAPABILITY, REVIEW_CAPABILITY, ROLES, has_capability, has_role

from .models import EmergencyAccess, EmergencyKind, EmergencyReason, ServiceRequest

MINUTES = 60
DURATION = timedelta(minutes=MINUTES)
EXPLANATION_LIMIT = 1000
# Advisory lock of one person's starts (PostgreSQL's two-key form): the second key is the account.
START_LOCK = 19570
INT4 = 2**31 - 1
TOPIC = "desk.emergency"  # jobs.registry.NOTICE_TOPICS
EVENT = "emergency_access"  # desk.notifications.PHRASES
SUPERVISOR_GROUPS = ("supervisor", "admin")  # desk.permissions: admin includes the supervisor role

# The grant a check found: the capability itself, rather than a use.
BY_CAPABILITY = "capability"

NOT_ELIGIBLE = "Your account cannot use emergency access."
ALREADY_HOLDS = (
    "You can see patient information without emergency access, so none was started. Open the "
    "ticket and reveal it there."
)
EXPLANATION_REQUIRED = "Say why you need it, without patient information."
NOTHING_HIDDEN = (
    "This ticket holds no patient information hidden from you, so emergency access was not started."
)
REASON_REQUIRED = "Choose why you need it."


def is_eligible(user):
    """An active staff account with a laboratory role that holds the capability, as it stands in
    this transaction."""
    return (
        user.is_authenticated
        and user.is_active
        and user.client_id is None
        and has_role(user, *ROLES)
        and has_capability(user, EMERGENCY_CAPABILITY)
    )


def in_force(user, ticket_id, kind=EmergencyKind.PATIENT_INFORMATION):
    """The person's use of `kind` on the ticket whose end has not passed by the database's clock at
    the start of this transaction (the latest, should there be two), or None. Says nothing about
    whether the account may still use it (`patient_grant`)."""
    if not user.is_authenticated or ticket_id is None:
        return None
    return (
        EmergencyAccess.objects.filter(
            user_id=user.pk, ticket_id=ticket_id, kind=kind, ends_at__gt=TransactionTime()
        )
        .order_by("-ends_at", "-pk")
        .first()
    )


def patient_grant(user, ticket_id):
    """What lets this account see the patient information on ticket `ticket_id`: BY_CAPABILITY
    (view_patient_information, which opens every ticket the account may read), the use in force
    on the ticket while the account may use emergency access, or None. The caller has decided that
    the account may read the ticket."""
    from request_forms.selectors import may_see_patient_information

    if may_see_patient_information(user):
        return BY_CAPABILITY
    if not is_eligible(user):
        return None
    return in_force(user, ticket_id)


def sees_patient_information(user, ticket_id):
    return patient_grant(user, ticket_id) is not None


def emergency_marker(grant):
    """What a reveal's change event adds for `grant`: the use's id, when a use gave it."""
    return {"emergency": grant.pk} if isinstance(grant, EmergencyAccess) else {}


def has_hidden_patient_information(ticket):
    """Whether the ticket holds patient information a person without the grant sees only as a
    placeholder: a current person-level link, or a current patient-marked answer with a value
    (a cleared one shows nothing)."""
    from links.models import LinkAttachment
    from request_forms import kinds
    from request_forms.models import FieldValue

    links = LinkAttachment.objects.filter(request=ticket, is_person=True, removed_at__isnull=True)
    if links.exists():
        return True
    answers = (
        FieldValue.objects.filter(filled_form__request=ticket, superseded_at__isnull=True)
        .filter(Q(holds_patient_information=True) | Q(field__holds_patient_information=True))
        .only("kind", "value")
    )
    # Only an answer the ticket shows (request_forms.selectors.staff_values): a cleared one is none.
    return any(kinds.display(row.kind, row.value) for row in answers)


def ticket_state(user, ticket):
    """What the ticket's page says about emergency access to this reader: `use`, the reader's use
    in force on it (only while it gives something), and `offer`, whether to offer to start one
    (the reader may use emergency access, lacks the usual grant, has none in force here, and the
    ticket holds patient information hidden from them)."""
    grant = patient_grant(user, ticket.pk)
    if isinstance(grant, EmergencyAccess):
        return {"use": grant, "offer": False}
    offer = grant is None and is_eligible(user) and has_hidden_patient_information(ticket)
    return {"use": None, "offer": offer}


# Starting ----------------------------------------------------------------------------------


def _lock_starts(user_id):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [START_LOCK, user_id % INT4])


def clean_reason(reason):
    if reason not in EmergencyReason.values:
        raise ValidationError({"reason": REASON_REQUIRED})
    return reason


def clean_explanation(explanation):
    explanation = (explanation or "").strip()
    if not explanation:
        raise ValidationError({"explanation": EXPLANATION_REQUIRED})
    if len(explanation) > EXPLANATION_LIMIT:
        raise ValidationError(
            {"explanation": f"Keep it to {EXPLANATION_LIMIT} characters or fewer."}
        )
    return explanation


class AlreadyInForce(Exception):
    """The person already has a use in force on the ticket: `use`."""

    def __init__(self, use):
        super().__init__("Emergency access is already in force on this ticket.")
        self.use = use


@acting_change
def start(*, actor, ticket_id, reason, explanation):
    """Start a use of emergency access to the patient information on ticket `ticket_id` for
    `actor`, for MINUTES from now by the database's clock. Raises PermissionDenied when the account
    may not use emergency access, ValidationError when it needs none or the reason or explanation
    is refused, AlreadyInForce, and ServiceRequest.DoesNotExist when the ticket is not one it may
    read. Returns the use."""
    from audit_review.reviews import roles_label
    from core.audit import record_event
    from core.request_logging import current_request_id
    from request_forms.selectors import may_see_patient_information

    from .selectors import staff_requests
    from .services import current_staff

    if getattr(actor, "pk", None) is None or not actor.is_authenticated:
        raise PermissionDenied(NOT_ELIGIBLE)
    reason = clean_reason(reason)
    explanation = clean_explanation(explanation)
    _lock_starts(actor.pk)  # after the hold (acting_change), so two starts of one person take turns
    try:
        actor = current_staff(actor)  # the account as held, not the request's copy
    except PermissionDenied:
        raise PermissionDenied(NOT_ELIGIBLE) from None
    if not has_capability(actor, EMERGENCY_CAPABILITY):
        raise PermissionDenied(NOT_ELIGIBLE)
    ticket = staff_requests(actor).filter(pk=ticket_id).first()
    if ticket is None:
        raise ServiceRequest.DoesNotExist("No such ticket.")
    if may_see_patient_information(actor):
        raise ValidationError(ALREADY_HOLDS)
    existing = in_force(actor, ticket.pk)
    if existing is not None:
        raise AlreadyInForce(existing)
    if not has_hidden_patient_information(ticket):
        raise ValidationError(NOTHING_HIDDEN)
    now = TransactionTime()
    use = EmergencyAccess.objects.create(
        user=actor,
        user_name=actor.get_full_name() or actor.get_username(),
        user_username=actor.get_username(),
        user_roles=roles_label(actor),
        ticket=ticket,
        kind=EmergencyKind.PATIENT_INFORMATION,
        reason=reason,
        explanation=explanation,
        started_at=now,
        ends_at=ExpressionWrapper(now + Value(DURATION), output_field=DateTimeField()),
        request_ref=current_request_id(),
    )
    use.refresh_from_db(fields=["started_at", "ends_at"])
    record_event(
        actor=actor,
        action="emergency_access.started",
        obj=use,
        after={
            "ticket": ticket.pk,
            "kind": use.kind,
            "reason": use.reason,
            "started_at": use.started_at.isoformat(),
            "ends_at": use.ends_at.isoformat(),
        },
    )
    _record_notices(use)
    return use


# Notices -----------------------------------------------------------------------------------


def _holders(codename):
    """Ids of the active staff accounts with a laboratory role that hold `codename`, directly
    (while the grant has not ended) or through a group."""
    from django.contrib.auth import get_user_model

    from accounts.roles import direct_grants

    grant = {"codename": codename, "content_type__app_label": "accounts"}
    direct = direct_grants(**{f"permission__{key}": value for key, value in grant.items()})
    return set(
        get_user_model()
        .objects.filter(is_active=True, client_id__isnull=True, groups__name__in=ROLES)
        .filter(
            Q(pk__in=direct.values("user_id"))
            | Q(**{f"groups__permissions__{key}": value for key, value in grant.items()})
        )
        .values_list("pk", flat=True)
    )


def told(use):
    """The accounts told of `use` when it starts: every active supervisor (the supervisor or admin
    role) and every reviewer (a laboratory role and "Can review access and audit records") with an
    email address, except the person who used it and the scheduled jobs' account."""
    from django.contrib.auth import get_user_model

    from jobs.actor import system_account_id

    supervisors = set(
        get_user_model()
        .objects.filter(is_active=True, client_id__isnull=True, groups__name__in=SUPERVISOR_GROUPS)
        .values_list("pk", flat=True)
    )
    ids = (supervisors | _holders(REVIEW_CAPABILITY)) - {use.user_id, system_account_id()}
    return list(
        get_user_model().objects.filter(pk__in=ids, is_active=True).exclude(email="").order_by("pk")
    )


def _record_notices(use):
    from jobs import outbox

    notices = [
        outbox.record(
            recipient=account,
            topic=TOPIC,
            object_id=use.pk,
            reference=use.ticket.number,
            events=[EVENT],
            data={"use": use.pk},
        )
        for account in told(use)
    ]
    outbox.send_after_commit([notice.pk for notice in notices])


def _review_path(use_id):
    from django.urls import reverse

    return reverse("access_review:emergency-use", args=[use_id])


def _hold_recipient(recipient_id):
    """Hold, until the attempt's transaction ends (jobs.outbox), the rows the decision about a
    notice's recipient rests on, and read the account after them: its account row (an address
    change, a deactivation), its roles, read and held in one statement (accounts.roles.held_roles),
    and its grants of "Can review access and audit records", its own and its roles'. Taken without
    waiting, so a row someone is changing leaves the notice for a later attempt. Returns (account,
    roles), or (None, set())."""
    from django.contrib.auth.models import Group

    from accounts.models import User
    from accounts.roles import held_roles
    from jobs.locks import share_lock

    share_lock(User, [recipient_id], nowait=True)
    account = (
        User.objects.filter(pk=recipient_id, is_active=True, client_id__isnull=True)
        .exclude(email="")
        .first()
    )
    if account is None:
        return None, set()
    roles = held_roles(account, nowait=True, mode="key share")
    grant = {
        "permission__codename": REVIEW_CAPABILITY,
        "permission__content_type__app_label": "accounts",
    }
    direct = User.user_permissions.through.objects.filter(user_id=account.pk, **grant)
    share_lock(User.user_permissions.through, direct.values_list("pk", flat=True), nowait=True)
    through_groups = Group.permissions.through.objects.filter(group__user=account, **grant)
    share_lock(Group.permissions.through, through_groups.values_list("pk", flat=True), nowait=True)
    return account, roles


def prepare_emergency_notice(notice):
    """At each sending attempt (jobs.outbox): None when the notice no longer applies (the use is
    gone, or the recipient is no longer an active staff account with an address that is a
    supervisor or a reviewer); otherwise the notice, worded from desk.notifications' allow-list,
    with a link to the use's review page for a reviewer and to the ticket's Who opened it page for
    a supervisor who is not one. The rows the decision rests on are held until the notice is sent
    (`_hold_recipient`); one someone is changing leaves it for a later attempt."""
    from django.urls import reverse

    from accounts.roles import ROLES
    from jobs.outbox import Prepared

    from . import notifications
    from .permissions import SUPERVISOR_ROLES

    use = EmergencyAccess.objects.select_related("ticket").filter(pk=notice.object_id).first()
    if use is None or notice.recipient_id == use.user_id:
        return None
    recipient, roles = _hold_recipient(notice.recipient_id)
    if recipient is None or not roles.intersection(ROLES):
        return None
    if has_capability(recipient, REVIEW_CAPABILITY):
        path = _review_path(use.pk)
    elif roles.intersection(SUPERVISOR_ROLES):
        path = reverse("desk:request-access", args=[use.ticket_id])
    else:
        return None
    context = notifications.notice_context(
        number=use.ticket.number,
        event=EVENT,
        url=notifications.sign_in_url(path),
        laboratory=notifications.lab_name(),
    )
    subject, body = notifications.render_notice(context)
    return Prepared(to=recipient.email, subject=subject, body=body, event=EVENT)
