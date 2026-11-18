from django.contrib.auth.models import AbstractUser
from django.db import models, transaction

from .roles import CAPABILITIES


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
            super().save(*args, **kwargs)

    class Meta(AbstractUser.Meta):
        permissions = list(CAPABILITIES.items())
