from allauth.account.adapter import DefaultAccountAdapter
from django.core.exceptions import ValidationError
from django.views.decorators.debug import sensitive_variables


class ClosedSignupAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return False

    # These hold the credentials of a sign-in (the password among them): every variable here, and in
    # what they call, is left out of a traceback's report, except where the code called names its
    # own sensitive variables: Django's authenticate() names only `credentials`, so the password
    # argument of the authentication backend it calls stays in that frame (an accepted limit of
    # Django's report filter; this release sends no report, docs/operations.md).
    @sensitive_variables()
    def pre_authenticate(self, request, **credentials):
        try:
            super().pre_authenticate(request, **credentials)
        except ValidationError:
            # Throttling stops before Django authenticates or emits user_login_failed.
            from .signin_log import log_sign_in_failure

            log_sign_in_failure(sender=type(self), credentials=credentials, request=request)
            _record_attempt(request, credentials, locked_out=True, account=self._reauthenticating)
            raise

    # The account whose password reauthenticate is checking (allauth's re-authentication page),
    # while it checks it: what the checks it calls decide is about that account, whatever the
    # credentials name and whoever else is signed in.
    _reauthenticating = None
    # The account authenticate() accepted while reauthenticate checked: its stored hash is the
    # password that was checked (after any upgrade the check made), for accounts.confirmation.
    _verified = None

    @sensitive_variables()
    def authenticate(self, request, **credentials):
        user = super().authenticate(request, **credentials)
        if self._reauthenticating is not None:
            self._verified = user
        return user

    @sensitive_variables()
    def authentication_failed(self, request, **credentials):
        # A password not accepted (allauth calls this when no account accepted it, so not for a
        # deactivated account's right password, which pre_login refuses): at sign-in, recorded
        # for the account the attempt names; on re-authentication, by reauthenticate.
        if self._reauthenticating is None:
            _record_attempt(request, credentials)
        super().authentication_failed(request, **credentials)

    # Every variable here and in what it calls (allauth, the authentication backends, which hold
    # the password in their credentials) is left out of a traceback's report.
    @sensitive_variables()
    def reauthenticate(self, user, password):
        # The password asked for again of the signed-in account (allauth's re-authentication
        # page): not accepted when no account accepted it, or when another account did.
        self._reauthenticating = user
        self._verified = None
        try:
            accepted = super().reauthenticate(user, password)
        finally:
            self._reauthenticating = None
        if accepted:
            # Which password it was, kept for the request until the re-authentication is recorded
            # (accounts.confirmation: it counts only while the account keeps that password).
            from allauth.core import context

            from .confirmation import password_checked

            verified = self._verified
            if verified is not None and verified.pk == user.pk:
                password_checked(context.request, verified.password)
        if not accepted:
            from allauth.core import context

            from .models import SignInEvent, SignInFactor
            from .signin_history import record

            record(
                user,
                SignInEvent.Kind.REAUTHENTICATION_FAILED,
                context.request,
                factor=SignInFactor.PASSWORD,
            )
        return accepted

    def login(self, request, user):
        # What this sign-in proved, read from allauth's steps before Django signs the browser in
        # (which empties its session when another account was signed in there), for the session
        # row and the sign-in history (accounts.sessions.start_session).
        from .signin_history import sign_in_factor

        request.vr_sign_in_factor = sign_in_factor(request, user)
        try:
            super().login(request, user)
        finally:
            # Taken by the sign-in's session start; gone too when the sign-in failed before it.
            request.__dict__.pop("vr_sign_in_factor", None)

    def pre_login(self, request, user, **kwargs):
        # The scheduled jobs' account (jobs.actor) never signs in, even if someone activates it
        # and gives it a password in the admin console: a person's actions would otherwise be
        # recorded as the scheduled jobs'.
        from jobs.actor import is_system_account

        if not user.is_active:
            # The right password (or an emailed code) for a deactivated account, the scheduled
            # jobs' account included: refused, and recorded in the account's sign-in history (for
            # the account it was accepted for, whatever else the name typed matches).
            from .models import AccountSession, SignInEvent
            from .signin_history import record

            record(
                user,
                SignInEvent.Kind.REFUSED,
                request,
                ending=AccountSession.Ending.DEACTIVATED,
            )
        if is_system_account(user):
            from allauth.account.internal.stagekit import clear_login

            clear_login(request)
            return self.respond_user_inactive(request, user)
        return super().pre_login(request, user, **kwargs)

    def add_message(self, request, level, message_template=None, *args, **kwargs):
        # A sign-in whose session started ended (accounts.compromised: the password alone, while
        # it is marked as compromised) is not announced as signed in: the next page says why it
        # was signed out instead.
        if message_template == "account/messages/logged_in.txt" and getattr(
            request, "vr_sign_in_refused", False
        ):
            return
        super().add_message(request, level, message_template, *args, **kwargs)

    def send_password_reset_mail(self, user, email, context):
        # A shared demonstration account keeps its published password (accounts.middleware).
        # The page answers as for any address, so it does not reveal which accounts are shared.
        if user.credentials_locked:
            return
        super().send_password_reset_mail(user, email, context)

    def set_password(self, user, password):
        # The password only: `user` was read when the request began, and a full save would put
        # back what changed since, such as a deactivation committed while the password was being
        # checked and hashed.
        user.set_password(password)
        user.save(update_fields=["password"])


def _record_attempt(request, credentials, *, locked_out=False, account=None):
    """A password attempt that failed, or was refused for too many failed attempts before the
    password was checked, in the sign-in history of the account it concerns: `account` when a
    re-authentication checks that account's password, otherwise the one account the attempt's
    username or email address names (none is recorded when it names none or several). A failed
    re-authentication is recorded by ClosedSignupAdapter.reauthenticate."""
    from .models import SignInEvent, SignInFactor
    from .signin_history import account_named, record

    if account is None:
        account = account_named(credentials)
    kind = SignInEvent.Kind.LOCKED_OUT if locked_out else SignInEvent.Kind.FAILED
    if account is not None:
        record(account, kind, request, factor=SignInFactor.PASSWORD)
