from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "accounts"

    def ready(self):
        from . import acting, confirmation, sessions, signin_log  # noqa: F401

        confirmation.connect()  # which password a password re-authentication was

        acting.connect()  # removals that take access away take the access gate
        from . import grant_ends

        grant_ends.connect()  # an end set takes the gate; an end goes with its grant
        # The sign-in's time is saved by accounts.sessions.start_session, after the account's
        # session-ending lock, not by Django's own receiver before it (with a transaction around
        # the request, that receiver's hold on the account's row would come first).
        from django.contrib.auth.models import update_last_login
        from django.contrib.auth.signals import user_logged_in

        user_logged_in.disconnect(update_last_login, dispatch_uid="update_last_login")

        # manage.py changepassword saves the password alone (accounts.password_command).
        from . import password_command

        password_command.install()
