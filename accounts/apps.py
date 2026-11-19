from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "accounts"

    def ready(self):
        from . import acting, signin_log  # noqa: F401

        acting.connect()  # removals that take access away take the access gate
