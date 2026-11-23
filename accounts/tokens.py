"""The password reset links' tokens (ACCOUNT_PASSWORD_RESET_TOKEN_GENERATOR in
config/settings.py), loaded by allauth from that setting only.

allauth's token already stops working once the account's password, last sign-in or email
addresses change. Here it also does not work while the account is a shared demonstration account
(credentials_locked): whoever signs in to one cannot change its password (accounts.middleware),
and a link mailed before the account was shared must not do it either. Should the account stop
being shared, a link mailed before still works until it expires, as for any account. Links of
other accounts are unchanged by this.
"""

from allauth.account.forms import EmailAwarePasswordResetTokenGenerator


class ResetTokenGenerator(EmailAwarePasswordResetTokenGenerator):
    def _make_hash_value(self, user, timestamp):
        value = super()._make_hash_value(user, timestamp)
        return value + "|credentials-locked" if user.credentials_locked else value
