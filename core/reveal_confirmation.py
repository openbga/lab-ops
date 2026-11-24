"""Confirm it is you before the first reveal of patient information in a session, where the
laboratory turns it on (docs/safeguards.md).

Staff who hold "Can view patient information" reveal a patient-information value on a ticket's
form, or a person-level link's key, one at a time, and each reveal is recorded
(request_forms.request_views.reveal_value, links.views.attachment_reveal). With the rule on, the
first reveal in a browser's session asks the person to confirm it is them: the password, and the
second factor where the account has one, as accounts.confirmation asks before a change to access
and security settings. Once confirmed, the session reveals without asking again until it ends
(Sign out, a session limit, a new password set for the account or a deactivation). A confirmation
given in this session within the last ACCOUNT_REAUTHENTICATION_TIMEOUT seconds (5 minutes in this
release), for any change that asks for one, counts too. The sign-in itself does not.

Off until an administrator turns it on. The admin role changes it on the Security settings page,
with a reason, and confirms it is them to save it. A save holds the acting account
(accounts.acting), takes the rule's lock, checks the revision the form showed and the role as the
account is held, and records one audit event with the rule before and after.

Each reveal decides in its own transaction, after it holds the acting account and before it reads
anything it reveals: it takes the rule's lock shared, so a save of the rule waits for the reveals
under way, and a reveal that starts while a save waits reads the rule as saved. Without a
confirmation, nothing is read, revealed or recorded: the person is sent to the confirmation page
(core.reveal_confirmation_views), which returns them to the page they were on.

Not asked before a search by a person-level key (the searcher typed the key) or for what a client
portal account sees of its own tickets, which is shown without a reveal.

Basis: none requires it. 45 CFR §164.312(d) (person or entity authentication) names no method and
no moment; NIST SP 800-63B-4 sets no requirement to authenticate again before a sensitive read.
Asking for it is the laboratory's choice, offered as a setting.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction

from accounts.acting import hold_actor
from accounts.roles import has_role

from .audit import record_event
from .models import AuditEvent, RevealConfirmationRule

MANAGER_ROLES = ("admin",)
ACTION = "lab.reveal_confirmation_changed"
# The advisory lock (PostgreSQL's two-key form) that a save of the rule takes, and every reveal
# takes shared. First keys 19553 to 19561 name other locks (accounts.acting, accounts.kept,
# accounts.signin_history, core.second_factor and their neighbours).
RULE_LOCK = (19562, 0)
# The browser's session confirmed it is them for reveals: the account's id.
SESSION_KEY = "_reveal_confirmed"
LABEL = "Confirm it is you before the first reveal of patient information in a session"


def can_manage(user):
    return has_role(user, *MANAGER_ROLES)


def rule():
    """The rule as it stands: the saved row, or an unsaved one asking nothing (revision 0)."""
    return RevealConfirmationRule.objects.filter(pk=1).first() or RevealConfirmationRule(pk=1)


def summary(on):
    return "Asked once in each session" if on else "Not asked"


@transaction.atomic
def save_rule(*, actor, first_reveal, expected_revision, reason):
    """Turn the rule on or off, with a reason. Returns (rule, changed).

    `expected_revision` is the rule's revision the form showed (0 for a rule never saved): a
    change saved in between refuses this one. It applies from each session's next reveal; a
    session that confirmed already is not asked again."""
    current = hold_actor(actor)  # before any other lock (accounts.acting)
    if current is None or not can_manage(current):
        raise PermissionDenied("Your role does not permit this action.")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required."})
    if not isinstance(first_reveal, bool):
        raise ValidationError({"first_reveal": "Choose yes or no."})
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", list(RULE_LOCK))
    stored = RevealConfirmationRule.objects.select_for_update().filter(pk=1).first()
    if stored is None:
        stored = RevealConfirmationRule(pk=1)
    if expected_revision is None or stored.revision != expected_revision:
        raise ValidationError(
            "The confirmation before revealing patient information was changed after you opened "
            "this form. Your input has been kept; reload and compare the current setting before "
            "saving again."
        )
    if stored.first_reveal == first_reveal:
        return stored, False
    before = {"first_reveal": stored.first_reveal}
    stored.first_reveal = first_reveal
    stored.revision += 1
    stored.save()
    record_event(
        actor=current,
        action=ACTION,
        obj=stored,
        before=before,
        after={"first_reveal": first_reveal},
        reason=reason,
    )
    return stored, True


def history(limit=20):
    """The newest changes to the rule, newest first."""
    return (
        AuditEvent.objects.filter(action=ACTION)
        .select_related("actor")
        .order_by("-occurred_at")[:limit]
    )


def confirmed(request):
    """Whether this browser's session confirmed it is them for reveals (remember())."""
    user = getattr(request, "user", None)
    session = getattr(request, "session", None)
    if session is None or getattr(user, "pk", None) is None:
        return False
    value = session.get(SESSION_KEY)
    return isinstance(value, int) and not isinstance(value, bool) and value == user.pk


def remember(request):
    """Record in the browser's session that its account confirmed it is them for reveals. Django
    empties the session at every sign-in of another account and at every sign-out, so it lasts
    for this sign-in."""
    request.session[SESSION_KEY] = request.user.pk


def may_reveal(request):
    """Whether the signed-in staff account of `request` may reveal now without being asked: the
    rule is off, or this session confirmed for reveals, or it confirmed within the window for any
    change that asks (which is then remembered for reveals). Call it inside the reveal's
    transaction, after the acting account is held and before anything revealed is read: it takes
    the rule's lock shared until the transaction ends."""
    from accounts import confirmation

    if not connection.in_atomic_block:
        raise RuntimeError("Decide a reveal in its transaction; open one first.")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock_shared(%s, %s)", list(RULE_LOCK))
    if not rule().first_reveal:
        return True
    if confirmed(request):
        return True
    if confirmation.current(request):
        remember(request)
        return True
    return False


def asks(request):
    """Whether the rule is on and this session has not confirmed for reveals (for the
    confirmation page, which reads without deciding a reveal)."""
    return rule().first_reveal and not confirmed(request)
