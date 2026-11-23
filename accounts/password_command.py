"""`manage.py changepassword`, saving the password alone.

Django's command reads the account, waits while the operator types the new password twice, and
then saves the whole account as it was read: an edit of the account committed meanwhile (a name
corrected, the account deactivated, a role's staff status changed) would be put back. This one
checks the password against the rule (accounts.passwords) and saves only the password, as the
account pages and the admin console's password page do (accounts.adapter, accounts.admin).

django.contrib.auth, listed before accounts in INSTALLED_APPS, provides the command, so
AccountsConfig.ready() puts this class in its place.
"""

import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.management.commands import changepassword
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import CommandError

ATTEMPTS = 3


class Command(changepassword.Command):
    help = "Change an account's password, saving the password alone."

    def handle(self, *args, **options):
        User = get_user_model()
        username = options["username"] or getpass.getuser()
        try:
            account = User._default_manager.using(options["database"]).get(
                **{User.USERNAME_FIELD: username}
            )
        except User.DoesNotExist:
            raise CommandError(f"user '{username}' does not exist") from None
        self.stdout.write(f"Changing password for user '{account}'")
        for _attempt in range(ATTEMPTS):
            first = self._get_pass()
            second = self._get_pass("Password (again): ")
            if first != second:
                self.stdout.write("Passwords do not match. Please try again.")
                continue
            try:
                validate_password(second, account)
            except ValidationError as error:
                self.stderr.write("\n".join(error.messages))
                continue
            account.set_password(second)
            account.save(update_fields=["password"])
            return f"Password changed successfully for user '{account}'"
        raise CommandError(
            f"Aborting password change for user '{account}' after {ATTEMPTS} attempts"
        )


def install():
    """Put Command in the place of Django's, where `manage.py changepassword` loads it."""
    changepassword.Command = Command
