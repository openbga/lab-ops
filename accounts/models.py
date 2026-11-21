import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models, transaction
from django.db.models.functions import Now

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
        update_fields = kwargs.get("update_fields")
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
                # which FOR UPDATE would make wait while a deactivation waits for that ticket.
                # A full save's update then locks the row as FOR UPDATE does (it names the
                # unique columns and the table has an update trigger), so a deactivation locks
                # the person's tickets before it (desk.signals).
                from .acting import close_access

                close_access([self.pk])
                list(
                    type(self)
                    .objects.select_for_update(no_key=True)
                    .filter(pk=self.pk)
                    .values_list("pk", flat=True)
                )
            # The stored password, when this save writes the column: whatever changes the
            # stored value (a new password, an unusable one, a hash upgraded at sign-in) changes
            # what Django's sessions are checked against, so the sessions end with it.
            new_password = False
            if not self._state.adding and (update_fields is None or "password" in update_fields):
                stored = (
                    type(self).objects.filter(pk=self.pk).values_list("password", flat=True).first()
                )
                new_password = stored is not None and stored != self.password
            # Before the account's row is updated: ending a session waits for a change of the
            # person's under way that holds it (accounts.sessions.hold_acting_session), and that
            # change may still take the account's row (an assignment to the person) after it.
            self._end_sessions(update_fields, new_password=new_password)
            super().save(*args, **kwargs)

    def _end_sessions(self, update_fields, *, new_password):
        """A deactivation ends every session of the account at once, and a new password every
        session but the one that set it, when the account set its own (accounts.sessions). Their
        browsers are signed out at their next request, and an ended session never comes back,
        even if the account is made active again."""
        if self._state.adding:
            return
        from .sessions import Ending, end_sessions, kept_on_password_change

        if not self.is_active and (update_fields is None or "is_active" in update_fields):
            end_sessions(self, Ending.DEACTIVATED)
        elif new_password:
            end_sessions(self, Ending.NEW_PASSWORD, keep=kept_on_password_change(self))

    class Meta(AbstractUser.Meta):
        permissions = list(CAPABILITIES.items())


class AccountSession(models.Model):
    """One sign-in of an account, from the moment it signs in until its session ends
    (accounts.sessions). The browser's session carries this row's id; a session whose row is
    missing or ended is signed out at its next request. The row is kept after it ends, with when
    and why. Its browser and address are shown only to the account it belongs to (Where you are
    signed in, accounts.session_list); the account administration shows how many are open."""

    class Ending(models.TextChoices):
        SIGNED_OUT = "signed_out", "Signed out"
        INACTIVITY = "inactivity", "No activity"
        MAXIMUM = "maximum", "Longest session reached"
        DEACTIVATED = "deactivated", "Account deactivated"
        NEW_PASSWORD = "new_password", "New password"
        SIGNED_IN_AGAIN = "signed_in_again", "Signed in again in the same browser"
        CHANGE_REFUSED = "change_refused", "A change found it past a limit"
        SIGNED_OUT_ELSEWHERE = "signed_out_elsewhere", "Signed out from another session"
        ADMINISTRATOR = "administrator", "Ended by an administrator"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Not PROTECT: this is the state of a sign-in, not history. Accounts are deactivated, never
    # deleted (accounts.admin), and their change history protects them.
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="account_sessions")
    started_at = models.DateTimeField(db_default=Now())
    last_active_at = models.DateTimeField(db_default=Now())
    ended_at = models.DateTimeField(null=True, blank=True)
    ending = models.CharField(max_length=20, choices=Ending.choices, blank=True)
    # As the browser and the deployment reported them at sign-in (blank for sessions from before
    # they were recorded, or when the request carried none): the User-Agent header, cut to its
    # first characters, and the client address by the deployment's trusted proxy settings. The
    # database's own default, so that a release still running while this one migrates (whose
    # sign-in names neither column) goes on signing people in.
    user_agent = models.CharField(max_length=300, blank=True, db_default="")
    address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["user"],
                condition=models.Q(ended_at__isnull=True),
                name="account_session_open",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ended_at__isnull=True, ending="")
                | models.Q(ended_at__isnull=False) & ~models.Q(ending=""),
                name="account_session_ending",
            )
        ]

    def __str__(self):
        return f"{self.user} {self.started_at:%Y-%m-%d %H:%M}"
