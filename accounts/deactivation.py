"""What an account held when it was deactivated, and the admin console's Deactivate page.

Recording (record_held and record, from accounts.models.User.save): a save that deactivates an
account (its stored row active, the save writing is_active false) reads what the account holds,
after the save has taken the account's access gate and locked its row and before it writes the row,
ends the sessions or returns the tickets; once the row is written, in the same transaction, it
stores that reading (accounts.models.Deactivation) with who saved (the save's changed_by, as the
desk's release reads it), the reason given on the Deactivate page, and how many open tickets the
save returned to their team queues (desk.signals). The Deactivate page hands the save the reading
it compared with the page under the same locks, so its record is the list the page showed (a name
changed meanwhile, which takes no gate, does not slip in). The account page's Active box, the
Deactivate page and any code that deactivates an account through save() each leave one. Paths that
bypass save() (queryset.update, raw SQL) leave none, nor did deactivations before this release.
The database refuses to change or delete a record (migration 0012).

What it held (holdings): its kind, organization, admin console access, roles, capabilities (given
directly, with the last day or as ended, or through a role or group), teams (lead, inactive team)
and portal access, worded as the access report words them (audit_review.access_report.describe,
without who gave each), with names as they stood then. Never a ticket's content, an email address or
patient information. A group that is neither a role nor gives a capability gives nothing and is
not listed, as in the access report. Grants that the account page removes in the same save as it
deactivates the account are in the record: the account's row is written before its roles and
capabilities. Known limit: the reading sees what had committed when it was read. A change that
takes the account's access gate (its page in the admin console, setting portal access, a removal
from a team, a role or a capability) waits for the deactivating save or is read by it; an addition
that takes no gate of the account (a member added to a team or made its lead on the team page, a
capability added to a group on the role group page, code, the database directly) and commits after
the reading is not in the record, and does not make the Deactivate page's save refused. It gives
the deactivated account nothing.

A deactivated account keeps its roles, capabilities, team memberships and portal access, which give
nothing until it is made active again (the account page's Active box): the record says what it held
when it was deactivated, whatever was changed since.

Deactivate (deactivate, from the account's page in the admin console): an owner of the account
administration (an active superuser with staff status) is shown what the account holds now and
what deactivating does, gives a reason and confirms it is them. The save takes the access gates of
both accounts, in id order, so that the owner's own access cannot change under it and two owners
deactivating each other take turns; reads the owner again (one who is no longer an owner is
refused); refuses the owner's own account, an account already deactivated and the scheduled jobs'
account; locks the account's row; and refuses the save when what the account holds, as the page
showed it (a fingerprint of holdings), changed after the page was opened. It then saves the account
inactive, which records what it held, and records `account.updated` with the account before and
after and the reason, all in one transaction. The last owner of the account administration is
never deactivated here: the owner saving stays one, and their access cannot change until the save
commits.
"""

import hashlib
import json

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .models import Deactivation, User

REASON_LENGTH = 1000
# Set on the account by the Deactivate page before its save: the reason the record keeps, and the
# reading of what it holds that the page's fingerprint was compared with, which the record keeps.
REASON = "_deactivation_reason"
HELD = "_deactivation_held"

REFUSALS = {
    "own": "This is your own account. Another superuser can deactivate it.",
    "inactive": "This account is already deactivated.",
    "system": "Nobody signs in as the scheduled jobs' account: it stays deactivated.",
}
CHANGED_SINCE_DRAWN = (
    "What this account holds changed after you opened this page, so it was not deactivated. The "
    "page now shows what it holds. Your reason was kept: check the list, then deactivate it again."
)
NOT_ALLOWED = "Only an active superuser with staff status can deactivate an account."


def holdings(user_id):
    """What the account holds now, in words (see the module docstring), read in a few
    statements, or None when there is no such account."""
    from audit_review.access_report import describe

    account = User.objects.select_related("client").filter(pk=user_id).first()
    if account is None:
        return None
    row = describe([account], history=False)[0]
    return {
        "kind": row.kind,
        "organization": row.organization,
        "console": [item.text for item in row.console],
        "roles": [item.text for item in row.roles],
        "capabilities": [{"text": item.text, "note": item.note} for item in row.capabilities],
        "teams": [{"text": item.text, "note": item.note} for item in row.teams],
        "portal": row.portal,
    }


def fingerprint(held):
    """A fingerprint of a holdings reading, for the page that shows it: the save is refused when
    what the account holds then differs from what the page showed."""
    return hashlib.sha256(json.dumps(held, sort_keys=True).encode()).hexdigest()


def record_held(user):
    """For accounts.models.User.save, once it holds the account's access gate and row and has
    found that it deactivates the account: what the account holds, read before the save writes
    anything; or, from the Deactivate page, the reading its fingerprint was compared with under
    the same locks, so that the record is the list the page showed."""
    held = user.__dict__.pop(HELD, None)
    return held if held is not None else holdings(user.pk)


def record(user, held):
    """For accounts.models.User.save, once the account's row is written inactive: store what it
    held (record_held's reading) with who saved, the reason and the tickets returned."""
    from desk.signals import RELEASED

    actor = getattr(user, "changed_by", None)
    return Deactivation.objects.create(
        user_id=user.pk,
        deactivated_by_id=getattr(actor, "pk", None),
        reason=getattr(user, REASON, "") or "",
        held=held or {},
        tickets_returned=getattr(user, RELEASED, 0) or 0,
    )


def latest(user_id):
    """The account's latest deactivation record, or None."""
    return Deactivation.objects.filter(user_id=user_id).order_by("-deactivated_at", "-id").first()


def owns_administration(user):
    """An owner of the account administration: an active superuser with staff status."""
    return bool(user and user.is_active and user.is_superuser and user.is_staff)


def refusal(account, actor):
    """Why `actor` cannot deactivate `account` as they stand, in REFUSALS' words, or None."""
    from jobs.actor import is_system_account

    if is_system_account(account):
        return REFUSALS["system"]
    if actor is not None and account.pk == actor.pk:
        return REFUSALS["own"]
    if not account.is_active:
        return REFUSALS["inactive"]
    return None


def open_tickets(user):
    """How many open tickets are assigned to the account now (the save returns them to their
    team queues): counted for the page, not part of what it holds."""
    from desk.services import open_assignments

    return open_assignments(user).count()


def deactivate(*, actor, account_id, reason, drawn):
    """Deactivate the account (see the module docstring). Returns its Deactivation record. Raises
    PermissionDenied when `actor` may not deactivate accounts, User.DoesNotExist when the account
    does not exist, and ValidationError with the reason when this account cannot be deactivated
    now or changed after the page was drawn."""
    from core.audit import record_event

    from .acting import close_access
    from .admin import ENDS, _end_labels, account_reading
    from .setup import seed_snapshot

    actor_id = getattr(actor, "pk", None)
    if actor_id is None or not owns_administration(User.objects.filter(pk=actor_id).first()):
        # Before anything else, from the acting account as stored now (not the copy given);
        # decided again below, once the owner's access gate is held.
        raise PermissionDenied(NOT_ALLOWED)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required."})
    if len(reason) > REASON_LENGTH:
        raise ValidationError({"reason": f"Keep the reason to {REASON_LENGTH:,} characters."})
    if actor_id == account_id:
        # Decided before any lock: the owner's own gate is taken below for their access.
        raise ValidationError(REFUSALS["own"])
    with transaction.atomic():
        # Both gates, in id order, before any row: the owner's access and the account's cannot
        # change until this commits, and two owners deactivating each other take turns (the
        # second then finds it is no longer an owner).
        close_access([pk for pk in (actor_id, account_id) if pk is not None])
        held_actor = User.objects.filter(pk=actor_id).first() if actor_id is not None else None
        if not owns_administration(held_actor):
            raise PermissionDenied(NOT_ALLOWED)
        account = User.objects.filter(pk=account_id).first()
        if account is None:
            raise User.DoesNotExist
        # The account's row, as its save will lock it, then the account as it stands.
        current = User.objects.select_for_update(no_key=True).filter(pk=account_id).first()
        if current is None:
            raise User.DoesNotExist
        refused = refusal(current, held_actor)
        if refused:
            raise ValidationError(refused)
        compared = holdings(current.pk)
        if str(drawn or "") != fingerprint(compared):
            raise ValidationError(CHANGED_SINCE_DRAWN)
        # The account change event's shape (accounts.admin), less the email address, which the
        # change does not touch: the ends of capabilities given directly only when it has one.
        before = seed_snapshot(current)
        ends = account_reading(current)[ENDS]
        if ends:
            before[ENDS] = _end_labels(ends)
        current.is_active = False
        current.changed_by = held_actor
        setattr(current, REASON, reason)
        setattr(current, HELD, compared)
        current.save(update_fields=["is_active"])
        record_event(
            actor=held_actor,
            action="account.updated",
            obj=current,
            before=before,
            after={**before, "is_active": False},
            reason=reason,
        )
        return latest(current.pk)
