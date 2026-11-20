from django.contrib.auth.models import AbstractUser
from django.db import models, transaction

from .roles import CAPABILITIES

# The columns whose change takes access away from the account's work under way: whether it
# is active, and for a client portal account the organization it belongs to.
ACCESS_FIELDS = frozenset({"is_active", "client", "client_id"})


def _may_change_access(update_fields):
    return update_fields is None or bool(ACCESS_FIELDS.intersection(update_fields))


class User(AbstractUser):
    email = models.EmailField(unique=True)
    # Set for a client portal account, which sees only this organization (clients.portal).
    client = models.ForeignKey(
        "clients.Client",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="users",
        verbose_name="client organization",
    )
    # Set only by `seed_demo --announce-accounts`, for accounts whose password it publishes.
    credentials_locked = models.BooleanField(
        default=False,
        help_text="Shared demonstration account: whoever signs in cannot change its password, "
        "email addresses or two-factor settings.",
    )

    def save(self, *args, **kwargs):
        # One transaction for the account and what its save signals change with it (releasing
        # a deactivated account's assignments, resetting portal access on a move), so they
        # succeed or fail together on every path, inside or outside a caller's transaction.
        with transaction.atomic():
            if self.pk is not None and _may_change_access(kwargs.get("update_fields")):
                # The access gate, before the save: a change the person has under way holds it
                # until that change commits (accounts.acting), and the save waits for it. Then
                # the row FOR NO KEY UPDATE, which an assignment to this account also takes
                # (desk.services._held_assignee), so the two wait for each other. Not FOR
                # UPDATE: the database checks the account named by another person's change (a
                # notice to a ticket's assignee) at that change's COMMIT with a key-share lock,
                # which FOR UPDATE would make wait while a deactivation waits for that ticket
                # (desk.signals).
                from .acting import close_access

                close_access([self.pk])
                list(
                    type(self)
                    .objects.select_for_update(no_key=True)
                    .filter(pk=self.pk)
                    .values_list("pk", flat=True)
                )
            super().save(*args, **kwargs)

    class Meta(AbstractUser.Meta):
        permissions = list(CAPABILITIES.items())
