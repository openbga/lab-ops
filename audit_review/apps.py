from django.apps import AppConfig


class AuditReviewConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "audit_review"
    verbose_name = "Audit review"

    def ready(self):
        from . import lookups  # noqa: F401  (registers the Person filter's option list)
