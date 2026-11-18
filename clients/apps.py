from django.apps import AppConfig


class ClientsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "clients"
    verbose_name = "Client organizations"

    def ready(self):
        from django.contrib.auth import get_user_model

        from . import signals

        signals.connect(get_user_model())
