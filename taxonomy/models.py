"""The laboratory's ticket taxonomy: groups and the categories inside them.

Two levels. A group has no parent; a category belongs to a group. A ticket takes a category,
never a group. The key never changes after creation, so presets and reports keep finding a
category the laboratory has renamed. docs/product.md "Ticket taxonomy and presets".
"""

from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Upper

from .choices import Priority


class RequestCategory(models.Model):
    key = models.SlugField(
        max_length=60, unique=True, help_text="Stable identifier; never changes after creation."
    )
    name = models.CharField(max_length=120, help_text="What staff see.")
    client_label = models.CharField(
        max_length=120,
        blank=True,
        help_text="What client portal users see. Empty: the staff name.",
    )
    guidance = models.TextField(
        "intake guidance", blank=True, help_text="Shown to staff when they log a ticket."
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
        verbose_name="group",
    )
    position = models.PositiveSmallIntegerField(default=0)
    default_team = models.ForeignKey(
        "teams.Team",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="default_for_categories",
    )
    default_priority = models.CharField(
        max_length=10, choices=Priority.choices, default=Priority.NORMAL
    )
    client_may_raise = models.BooleanField(
        "clients may raise it", default=False, help_text="Offered in the client portal."
    )
    is_complaint = models.BooleanField(
        "complaint", default=False, help_text="Tickets in it follow the complaint rules."
    )
    is_restricted = models.BooleanField(
        "restricted",
        default=False,
        help_text="Tickets in it are visible only to their team's members and to supervisors.",
    )
    preset = models.CharField(
        max_length=40, blank=True, help_text="The preset that created it, if any."
    )
    is_active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["position", "name", "pk"]
        verbose_name = "ticket category"
        verbose_name_plural = "ticket categories"
        constraints = [
            models.UniqueConstraint(
                Upper("name"),
                F("parent"),
                nulls_distinct=False,
                name="request_category_name_unique",
                violation_error_message="This group already has a category with this name.",
            ),
            models.CheckConstraint(
                condition=~Q(parent=F("pk")),
                name="request_category_not_own_parent",
                violation_error_message="A category cannot be its own group.",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def is_group(self):
        return self.parent_id is None

    @property
    def portal_label(self):
        return self.client_label or self.name
