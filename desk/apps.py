from django.apps import AppConfig


class DeskConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "desk"
    verbose_name = "Tickets"

    def ready(self):
        from django.contrib.auth import get_user_model

        from . import (
            lookups,  # noqa: F401  (registers the desk's remote option lists)
            signals,
        )

        signals.connect(get_user_model())
