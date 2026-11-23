from allauth.account.adapter import DefaultAccountAdapter
from django.core.exceptions import ValidationError


class ClosedSignupAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return False

    def pre_authenticate(self, request, **credentials):
        try:
            super().pre_authenticate(request, **credentials)
        except ValidationError:
            # Throttling stops before Django authenticates or emits user_login_failed.
            from .signin_log import log_sign_in_failure

            log_sign_in_failure(sender=type(self), credentials=credentials, request=request)
            _record_attempt(request, credentials, locked_out=True)
            raise

    def authentication_failed(self, request, **credentials):
        # A password not accepted, at sign-in or when a signed-in page asks for it again (allauth
        # calls this for both, and not for a deactivated account's right password, which
        # pre_login refuses).
        _record_attempt(request, credentials)
        super().authentication_failed(request, **credentials)

    def pre_login(self, request, user, **kwargs):
        # The scheduled jobs' account (jobs.actor) never signs in, even if someone activates it
        # and gives it a password in the admin console: a person's actions would otherwise be
        # recorded as the scheduled jobs'.
        from jobs.actor import is_system_account

        if is_system_account(user):
            from allauth.account.internal.stagekit import clear_login

            clear_login(request)
            return self.respond_user_inactive(request, user)
        if not user.is_active:
            # The right password (or an emailed code) for a deactivated account: refused by
            # allauth below, and recorded in the account's sign-in history.
            from .models import AccountSession, SignInEvent
            from .signin_history import record

            record(
                user,
                SignInEvent.Kind.REFUSED,
                request,
                ending=AccountSession.Ending.DEACTIVATED,
            )
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


def _record_attempt(request, credentials, *, locked_out=False):
    """A password attempt that failed, or was refused for too many failed attempts before the
    password was checked, in the sign-in history of the account it concerns: the signed-in
    account when a page asked for its password again, otherwise the one account the attempt's
    username or email address names (none is recorded when it names none or several)."""
    from .models import SignInEvent, SignInFactor
    from .signin_history import account_named, record, signed_in_account

    account = signed_in_account(request)
    if account is not None:
        kind = SignInEvent.Kind.REAUTHENTICATION_FAILED
    else:
        account = account_named(credentials)
        kind = SignInEvent.Kind.FAILED
    if locked_out:
        kind = SignInEvent.Kind.LOCKED_OUT
    if account is not None:
        record(account, kind, request, factor=SignInFactor.PASSWORD)
