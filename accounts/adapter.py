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
            raise

    def pre_login(self, request, user, **kwargs):
        # The scheduled jobs' account (jobs.actor) never signs in, even if someone activates it
        # and gives it a password in the admin console: a person's actions would otherwise be
        # recorded as the scheduled jobs'.
        from jobs.actor import is_system_account

        if is_system_account(user):
            from allauth.account.internal.stagekit import clear_login

            clear_login(request)
            return self.respond_user_inactive(request, user)
        return super().pre_login(request, user, **kwargs)

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
