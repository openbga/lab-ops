"""Keep an account's kind fixed and portal access from outliving a change of organization.

An account is a laboratory (staff) account or a client portal account for life (decision of
2026-11-17): a save that turns one into the other is refused here, in the account
administration's form, and by the database trigger of clients/0006 for paths that bypass save().
A portal account may move to another organization; then:

When an account's organization changes (`User.client`), its portal access was granted for the
old organization: its contact and sites belong there and a whole-organization grant must not
carry over. Every save of such a change resets the access (clients.services.reset_portal_access)
in the same transaction, attributed to the person who made it. The caller names that person by
setting `user.changed_by` before saving; the account administration does. A save without it,
for an account that has portal access, is refused, so no code path leaves the access in place
unattributed. Paths that bypass save() (queryset.update, raw SQL) cannot be refused here; for
them the access still grants nothing, because PortalAccess.applies_to compares the organization
it was granted for with the account's current one (desk.portal_scope).
"""

from django.core.exceptions import ValidationError
from django.db.models.signals import post_save, pre_save

from .models import PortalAccess

PREVIOUS = "_previous_client_id"


def remember_organization_change(sender, instance, raw=False, update_fields=None, **kwargs):
    # What an earlier save of this instance remembered and could not use, because its update
    # failed (a duplicate email) and its post_save never ran: never carried into this save.
    instance.__dict__.pop(PREVIOUS, None)
    if raw or instance.pk is None:
        return
    if update_fields is not None and not {"client", "client_id"}.intersection(update_fields):
        # The save writes other columns only (a sign-in's time, a notice preference): the
        # organization stays as stored, whatever this copy of the account, read earlier, says.
        # Comparing it would refuse the save when someone moved the account meanwhile.
        return
    previous = sender.objects.filter(pk=instance.pk).values_list("client_id", flat=True).first()
    if previous == instance.client_id:
        return
    if (previous is None) != (instance.client_id is None):
        raise ValidationError(
            "An account stays a laboratory account or a client portal account for life. "
            "Create a new account instead."
        )
    access = PortalAccess.objects.filter(user_id=instance.pk).first()
    # The state before the update: the database trigger (clients/0007) empties the access in the
    # update itself, and the audit event must still say what the move took away.
    from .services import portal_access_state

    setattr(instance, PREVIOUS, portal_access_state(access) if access else None)
    if getattr(instance, "changed_by", None) is None and access is not None:
        raise ValidationError(
            "This account has portal access. Change its organization in the account "
            "administration (or set changed_by) so the access is reset and the change recorded."
        )


def reset_after_organization_change(sender, instance, created=False, raw=False, **kwargs):
    if raw or created or not hasattr(instance, PREVIOUS):
        return
    before = getattr(instance, PREVIOUS)
    delattr(instance, PREVIOUS)
    actor = getattr(instance, "changed_by", None)
    if actor is None or before is None:
        return  # no portal access (checked before saving): nothing to reset
    from .services import reset_portal_access

    reset_portal_access(
        actor=actor, user_id=instance.pk, expected_client_id=instance.client_id, before=before
    )


def connect(user_model):
    pre_save.connect(
        remember_organization_change, sender=user_model, dispatch_uid="clients-account-move"
    )
    post_save.connect(
        reset_after_organization_change, sender=user_model, dispatch_uid="clients-account-reset"
    )
