"""Choice lists shared by the request apps (desk, service_levels) without an import cycle."""

from django.db import models


class Priority(models.TextChoices):
    LOW = "low", "Low"
    NORMAL = "normal", "Normal"
    HIGH = "high", "High"
    URGENT = "urgent", "Urgent"
