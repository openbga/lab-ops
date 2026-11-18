"""The teams requests are routed to, and the staff who belong to them.

Every request belongs to one team (desk.ServiceRequest.team). One active team is the default:
it receives requests whose category names no team. Teams are deactivated, never deleted.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Upper

DEFAULT_TEAM_SLUG = "client-services"
DEFAULT_TEAM_NAME = "Client services"


class Team(models.Model):
    # A stable key: presets and seeds find a team by it, whatever the laboratory renames it to.
    slug = models.SlugField(max_length=60, unique=True)
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True)
    is_default = models.BooleanField(
        "default team",
        default=False,
        help_text="Receives tickets whose category names no team. Exactly one team is the default.",
    )
    is_active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["name", "pk"]
        constraints = [
            models.UniqueConstraint(
                Upper("name"),
                name="team_name_unique",
                violation_error_message="Another team already has this name.",
            ),
            models.UniqueConstraint(
                fields=["is_default"],
                condition=Q(is_default=True),
                name="team_one_default",
                violation_error_message="Another team is already the default.",
            ),
            models.CheckConstraint(
                condition=Q(is_active=True) | Q(is_default=False),
                name="team_default_active",
                violation_error_message="The default team must be active.",
            ),
        ]

    def __str__(self):
        return self.name


class TeamMembership(models.Model):
    """A staff account's membership of a team. Removing a member deletes the row (audited)."""

    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="team_memberships"
    )
    is_lead = models.BooleanField("team lead", default=False)

    class Meta:
        ordering = ["team", "user__username"]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "user"],
                name="team_membership_unique",
                violation_error_message="This person already belongs to the team.",
            )
        ]

    def __str__(self):
        return f"{self.user} in {self.team}"


def ensure_default_team():
    """The default team, created when no team is the default (a fresh or reset database)."""
    team = Team.objects.filter(is_default=True).first()
    if team is None:
        team, _ = Team.objects.get_or_create(
            slug=DEFAULT_TEAM_SLUG, defaults={"name": DEFAULT_TEAM_NAME}
        )
        if not team.is_default or not team.is_active:
            Team.objects.filter(pk=team.pk).update(is_default=True, is_active=True)
            team.refresh_from_db()
    return team
