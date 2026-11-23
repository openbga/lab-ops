"""Operational sign-in events on the console, independent of database resets.

Only stored usernames are logged, never submitted identifiers, passwords or codes.
The console filter attaches the application and Render request IDs.

The wrong codes received here are also written to the account's sign-in history
(accounts.signin_history); the history's password failures, refusals after too many failed
attempts and refused sign-ins of a deactivated account are written by accounts.adapter, which
can tell them apart.
"""

import logging
from urllib.parse import quote

from allauth.account.signals import login_code_rejected
from allauth.mfa.signals import authentication_failed
from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_out, user_login_failed
from django.db.models import Q
from django.dispatch import receiver

logger = logging.getLogger("lab_ops.signin")


def _log(event, user, request):
    username = quote(user.get_username(), safe="@.+-_") if user is not None else "<unknown>"
    logger.info("%s user=%s", event, username, extra={"request": request})


def log_sign_in(request, user):
    """A completed sign-in, logged by accounts.sessions.start_session (Django's user_logged_in,
    sent only after all required authentication stages have completed) once the sign-in's session
    has started: one whose session starts ended (accounts.compromised: the password alone,
    while it is marked as compromised) did not complete, and is logged as
    signout.password_compromised instead. Logged there rather than by a receiver of its own, so
    the decision is made before, whatever order the receivers run in."""
    _log("signin.success", user, request)


def accounts_named(credentials):
    """The accounts (at most two) whose username or email address, in any letter case, is the
    username or email address an attempt gave."""
    matches = Q()
    for field in ("username", "email"):
        identifier = credentials.get(field)
        if isinstance(identifier, str) and identifier:
            matches |= (
                Q(username__iexact=identifier)
                | Q(email__iexact=identifier)
                | Q(emailaddress__email__iexact=identifier)
            )
    return list(get_user_model().objects.filter(matches).distinct()[:2]) if matches else []


@receiver(user_login_failed, dispatch_uid="lab_ops_signin_failure")
def log_sign_in_failure(sender, credentials, request=None, **kwargs):
    users = accounts_named(credentials)
    _log("signin.failed", users[0] if len(users) == 1 else None, request)


@receiver(login_code_rejected, dispatch_uid="lab_ops_signin_code_failure")
def log_login_code_failure(sender, request, user, **kwargs):
    from .models import SignInEvent, SignInFactor
    from .signin_history import record

    _log("signin.failed", user, request)
    if user is not None and user.pk is not None:
        record(user, SignInEvent.Kind.FAILED, request, factor=SignInFactor.CODE)


@receiver(authentication_failed, dispatch_uid="lab_ops_signin_mfa_failure")
def log_mfa_failure(sender, request, user, reauthentication=False, **kwargs):
    from .models import SignInEvent, SignInFactor
    from .signin_history import record

    if not reauthentication:
        _log("signin.failed", user, request)
    if user is not None and user.pk is not None:
        kind = (
            SignInEvent.Kind.REAUTHENTICATION_FAILED
            if reauthentication
            else SignInEvent.Kind.FAILED
        )
        record(user, kind, request, factor=SignInFactor.SECOND_FACTOR)


@receiver(user_logged_out, dispatch_uid="lab_ops_signout")
def log_sign_out(sender, request, user, **kwargs):
    if user is not None:
        _log("signout", user, request)
