"""A deactivated account stays nobody's assignee, whatever saves it.

Every save that turns an active account inactive, or saves an inactive account that still holds
open work (a retry of a deactivation whose release did not complete), returns its requests that
are not closed to their teams' queues (release_assignments) in the same transaction as the
account change: accounts.models.User.save wraps the save and its signals in one transaction, so
both succeed or fail together. The release is attributed to `user.changed_by`
when the caller sets it, as the account administration does. A save without it (maintenance
code, an integration) is not refused: refusing would leave the person active, which is worse
than an unattributed release. Its timeline and audit events then name the deactivated account
itself as the actor, with a reason that says no actor was recorded.

The account's row is locked before its requests, the order an assignment takes them in
(desk.services._hold_assignment): an assignment to the account either comes first and its
request is released here, or waits and is refused. User.save locks the row FOR NO KEY UPDATE
after the account's access gate, and the requests are locked here, before the row is updated.
Another person's change to one of those requests that names the account without locking it, such
as a client's reply recording a notice to the assignee, is checked against the account only when
it commits, with a key-share lock. Neither the no-key lock nor an update of columns other than
the username and email blocks that check, and the requests are held before the update could
change either of those, so the reply commits and the deactivation, waiting for its request, then
releases it. A reply that comes second waits for the request, and is refused as changed if it
was drawn before the release (the release advances the request's revision); sent again, it finds
the request unassigned and records no notice to the deactivated account.

Paths that bypass save() (queryset.update, raw SQL) are not seen here; an account they
deactivate still cannot sign in or be chosen as an assignee.
"""

from django.db import transaction
from django.db.models.signals import post_save, pre_save

DEACTIVATING = "_desk_deactivating"
RELEASED = "released_assignments"  # how many requests the save returned to their queues


def remember_deactivation(sender, instance, raw=False, update_fields=None, **kwargs):
    if raw or instance.pk is None or instance.is_active:
        return
    if update_fields is not None and "is_active" not in update_fields:
        return
    from .services import open_assignments

    # The person's open requests, locked before the row is updated (see above), in the order
    # release_assignments locks them.
    held = list(
        open_assignments(instance)
        .select_for_update(of=("self",))
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    # Deactivating now, or saving an inactive account that still holds work (a deactivation
    # whose release did not complete, made by a path that bypassed save()): release either way.
    if held or sender.objects.filter(pk=instance.pk, is_active=True).exists():
        setattr(instance, DEACTIVATING, True)


def release_on_deactivation(sender, instance, created=False, raw=False, **kwargs):
    if raw or created or not getattr(instance, DEACTIVATING, False):
        return
    delattr(instance, DEACTIVATING)
    from .services import release_assignments

    actor = getattr(instance, "changed_by", None)
    reason = "Account deactivated"
    if actor is None:
        actor, reason = instance, "Account deactivated; no actor was recorded on the save"
    with transaction.atomic():
        setattr(
            instance,
            RELEASED,
            release_assignments(actor=actor, user=instance, reason=reason),
        )


def connect(user_model):
    pre_save.connect(remember_deactivation, sender=user_model, dispatch_uid="desk-deactivation")
    post_save.connect(release_on_deactivation, sender=user_model, dispatch_uid="desk-release")
