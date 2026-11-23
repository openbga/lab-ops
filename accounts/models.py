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

    def set_unusable_password(self):
        # A password set before and not yet written is dropped with it: Django would otherwise tell
        # the password validators of it at the next save.
        super().set_unusable_password()
        self._password = None

    def save(self, *args, **kwargs):
        # The names to save, read once and passed on as a list (an iterable can be read only
        # once), also in Django 5.2's deprecated positional form (force_insert, force_update,
        # using, update_fields), which is passed on as it came so that Django still warns of it.
        if len(args) > 3:
            update_fields = None if args[3] is None else list(args[3])
            args = (*args[:3], update_fields, *args[4:])
        else:
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = list(kwargs["update_fields"])
            update_fields = kwargs.get("update_fields")
        # One transaction for the account and what its save signals change with it (releasing
        # a deactivated account's assignments, resetting portal access on a move), so they
        # succeed or fail together on every path, inside or outside a caller's transaction.
        with transaction.atomic():
            if self.pk is not None and _may_change_access(update_fields):
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
            # A password chosen (set_password, which Django's own hash upgrade at sign-in does not
            # count as one) lifts a mark of the account's password as compromised, with this save
            # (accounts.compromised). Read before the save: Django forgets it once saved, also
            # after a save that does not write the password (a password set, then a save of
            # other columns, then a save of the password lifts nothing: no path of the
            # application saves that way).
            chosen = new_password and self._password is not None and self.has_usable_password()
            # Before the account's row is updated: ending a session waits for a change of the
            # person's under way that holds it (accounts.sessions.hold_acting_session), and that
            # change may still take the account's row (an assignment to the person) after it.
            self._end_sessions(update_fields, new_password=new_password)
            if chosen:
                self._check_chosen_while_marked()
            super().save(*args, **kwargs)
            if chosen:
                CompromisedPassword.objects.filter(user_id=self.pk, lifted_at__isnull=True).update(
                    lifted_at=Now()
                )

    def _check_chosen_while_marked(self):
        """A password chosen for an account whose password is marked as compromised, by a request
        acting through a session of that same account, needs that session still open: a change
        started before the mark (its form checked against the old password) and saved after the
        mark ended the session would otherwise lift the mark for whoever held the session. Read
        after the locks a mark also takes (the account's session-ending lock for a password saved
        alone, the account's row for a whole save), so a mark either committed before this reading
        or waits for this save. A reset through an emailed link proves the account's address
        whatever session the browser has (accounts.sessions.RESETTING_VIEWS), and other paths (a
        superuser setting it, a command) act through no session of the account: neither is
        affected. Choose a new password holds its session already
        (accounts.compromised.choose_new_password)."""
        from .sessions import RESETTING_VIEWS, SessionEnded, _view, hold_acting_session

        if _view.get() in RESETTING_VIEWS:
            return
        if not CompromisedPassword.objects.filter(user_id=self.pk, lifted_at__isnull=True).exists():
            return

        if not hold_acting_session(self.pk):
            raise SessionEnded

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
            if not _may_change_access(update_fields):
                # A save of the password alone takes no access gate exclusively, so it locks the
                # sessions before the account's row (written below), the other way round from
                # an access change. Take, in the order the person's own endings take them (Where
                # you are signed in, which hold their acting session before the others): the
                # account's session-ending lock, so the two take turns; then its access gate
                # shared, so an access change under way finishes first and one that starts
                # afterwards waits. The person's own changes under way, which hold the gate
                # shared too, are not waited for here.
                from .acting import share_access
                from .session_list import lock_endings

                lock_endings(self.pk)
                share_access([self.pk])
            end_sessions(self, Ending.NEW_PASSWORD, keep=kept_on_password_change(self))

    class Meta(AbstractUser.Meta):
        permissions = list(CAPABILITIES.items())


class SignInFactor(models.TextChoices):
    """What a sign-in proved, or the step at which an attempt failed (accounts.signin_history).
    A failure at the second step names neither kind of code: the form takes either."""

    PASSWORD = "password", "Password"
    PASSWORD_AUTHENTICATOR = "password_totp", "Password and authenticator app"
    PASSWORD_RECOVERY_CODE = "password_recovery", "Password and recovery code"
    CODE = "code", "Emailed code"
    CODE_AUTHENTICATOR = "code_totp", "Emailed code and authenticator app"
    CODE_RECOVERY_CODE = "code_recovery", "Emailed code and recovery code"
    SECOND_FACTOR = "second_factor", "Authenticator or recovery code"


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
        # The account's password was marked as compromised (accounts.compromised): every session
        # open then, and a later sign-in that proved nothing but the password.
        MARKED_COMPROMISED = "password_compromised", "Password marked as compromised"

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
    # Whether the laboratory's rule required a second sign-in factor of the account when it
    # signed in (core.second_factor): while it has none, such a session reaches only the pages
    # that set one up (accounts.enrolment). Decided once at sign-in, so a rule saved later applies
    # from the account's next sign-in. The database's own default, like the two columns above.
    second_factor_required = models.BooleanField(default=False, db_default=False)
    # What the sign-in proved (accounts.signin_history.sign_in_factor), copied into its sign-in
    # history entry by the database (migration 0009). Blank where it is not known: a session from
    # before this was recorded, or one a release still running while this one migrates signed in.
    factor = models.CharField(
        max_length=20, choices=SignInFactor.choices, blank=True, db_default=""
    )

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


class SignInEvent(models.Model):
    """One entry of an account's sign-in history (accounts.signin_history). History: the database
    refuses to change or delete a row (migration 0009).

    A sign-in, a sign-in that started ended (refused), and a session's end are written by the
    database itself, in the statement that starts or ends the session row, so that no path that
    starts or ends a session, a release still running during a deployment included, leaves none.
    Failed attempts, refusals after too many failed attempts and refused sign-ins that start no
    session are written by accounts.signin_log, for the one account the attempt names. Never a
    password, a code or what was typed as the username or email address."""

    class Kind(models.TextChoices):
        SIGNED_IN = "signed_in", "Signed in"
        # The password was right but the sign-in was not let through: why is in `ending`.
        REFUSED = "refused", "Sign-in refused"
        FAILED = "failed", "Sign-in failed"
        LOCKED_OUT = "locked_out", "Too many failed attempts"
        # A signed-in page asked for the password or a code again, and it was not accepted.
        REAUTHENTICATION_FAILED = "reauth_failed", "Re-authentication failed"
        SIGNED_OUT = "signed_out", "Signed out"

    # Accounts are deactivated, never deleted, and this is their history.
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="sign_in_events")
    # When it happened, by the database's clock: a session's sign-in, or its end (for a session
    # that passed a limit, the moment the limit passed, which can be before the row is written).
    occurred_at = models.DateTimeField(db_default=Now())
    kind = models.CharField(max_length=20, choices=Kind.choices)
    # What a sign-in proved, or the step an attempt failed at. Blank for a session's end, and
    # where it is not known.
    factor = models.CharField(max_length=20, choices=SignInFactor.choices, blank=True)
    # Why a session ended, or why a sign-in was refused.
    ending = models.CharField(max_length=20, choices=AccountSession.Ending.choices, blank=True)
    # The session's id, for a sign-in, a refused sign-in that started ended and a session's end:
    # not a foreign key, since the session row is state and this is history.
    session = models.UUIDField(null=True, blank=True)
    # As the browser and the deployment reported them (accounts.sessions.reported_by): for a
    # session's end, those it reported when it signed in.
    user_agent = models.CharField(max_length=300, blank=True)
    address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        # Written only by the database and accounts.signin_history: no model permission to grant.
        default_permissions = ()
        indexes = [models.Index(fields=["user", "-occurred_at"], name="sign_in_event_recent")]

    def __str__(self):
        return f"{self.user} {self.kind} {self.occurred_at:%Y-%m-%d %H:%M}"


class CompromisedPassword(models.Model):
    """A superuser's mark of an account's password as compromised (accounts.compromised), kept
    after it is lifted. While it is open (lifted_at empty) the password alone no longer signs the
    account in, and a session that proved more than the password (a second factor or an emailed
    code) reaches only Choose a new password. Any password chosen for the account afterwards
    lifts it, in the same save (User.save): the person's own, one set through an emailed reset
    link, or one a superuser sets in the admin console. At most one is open per account."""

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="compromised_passwords")
    marked_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    marked_at = models.DateTimeField(db_default=Now())
    # Why, as the superuser gave it: recorded in the audit history too. Not shown to the person.
    reason = models.TextField()
    # The stored hash of the marked password, copied when it was marked, so that the same password
    # is refused as a new one (accounts.passwords.MarkedPasswordValidator). Never shown, logged,
    # audited or registered in the admin console.
    password = models.CharField(max_length=128)
    lifted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        # Changed only through accounts.compromised: no model permission to grant.
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(lifted_at__isnull=True),
                name="compromised_password_open_once",
            )
        ]

    def __str__(self):
        return f"{self.user} {self.marked_at:%Y-%m-%d %H:%M}"


class KeptDraft(models.Model):
    """What a person had typed in a form when their session ended because it passed a limit
    (accounts.kept): kept for that account in that browser only, offered once on the same form
    in a later session and removed 24 hours after the session ended. Not history and never
    audited: the person's own unsent text, which may hold anything they typed, so it is never
    logged, listed or shown to anyone else. Only the fields the page named when it was drawn
    are kept (no files, hidden values or patient-information fields)."""

    session = models.ForeignKey(AccountSession, on_delete=models.CASCADE, related_name="drafts")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="kept_drafts")
    # The form on its page, as the page named it ("ticket-entry-12"): no content of the record.
    form = models.CharField(max_length=80)
    # The SHA-256 of the random value in the keeping browser's own cookie: given back only there.
    browser = models.CharField(max_length=64)
    # Field name -> value (a string, or a list for a field with several values). Emptied when the
    # draft is taken back, so that nothing typed outlives its use.
    values = models.JSONField(null=True)
    # Field name -> the kind of control the value came from (accounts.kept.FAMILIES), as the page
    # was drawn: a value goes back only into a control of the same kind.
    controls = models.JSONField(default=dict)
    # Something typed was left out when the copy was kept (a field no longer on the form): the
    # page that gives it back says so.
    dropped = models.BooleanField(default=False)
    # The revision of the record the form was drawn with, for a form that posts one
    # (accounts.kept: kept_revision): a page giving the copy back posts it, so a save is checked
    # against the record as it stood when the person typed. None for other forms.
    revision = models.PositiveIntegerField(null=True, blank=True)
    kept_at = models.DateTimeField(db_default=Now())
    # When a page offered it back (the row then blocks a later copy for the same session and form).
    taken_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["session", "form"], name="kept_draft_once"),
            models.CheckConstraint(
                condition=models.Q(taken_at__isnull=True, values__isnull=False)
                | models.Q(taken_at__isnull=False, values__isnull=True),
                name="kept_draft_taken",
            ),
        ]
        indexes = [
            models.Index(
                fields=["user", "form"],
                condition=models.Q(taken_at__isnull=True),
                name="kept_draft_offered",
            )
        ]

    def __str__(self):
        return f"{self.user} {self.form}"


class KeptDraftCutoff(models.Model):
    """When the account last signed out with Sign out (accounts.kept.discard_all): no copy of what
    was typed in a session that ended at or before this moment is kept, so a page of an earlier
    session sending later finds the Sign out. One row per account, moved forward at each Sign
    out."""

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, primary_key=True, related_name="kept_draft_cutoff"
    )
    discarded_at = models.DateTimeField()

    def __str__(self):
        return f"{self.user} {self.discarded_at:%Y-%m-%d %H:%M}"
