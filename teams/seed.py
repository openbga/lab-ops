"""Demonstration seed for teams (core.seeding): the default team in setup, the demo accounts'
memberships in records (after every app's setup, so the presets' teams exist)."""

from accounts.models import User

from .models import Team, TeamMembership, ensure_default_team
from .services import create_team

# (team slug, team name, ((username, lead), ...)); a team the presets did not create is created.
DEMO_MEMBERS = (
    (
        "client-services",
        "Client services",
        (
            ("demo_client_services", False),
            ("demo_account_manager", False),
            ("demo_supervisor", True),
        ),
    ),
    ("accessioning", "Accessioning", (("demo_client_services", False),)),
    ("technical", "Technical", (("demo_technical", True),)),
    ("quality", "Quality", (("demo_supervisor", True),)),
    ("billing", "Billing", (("demo_admin", False),)),
    ("logistics", "Logistics", (("demo_admin", True),)),
    ("interfaces-it", "Interfaces and IT", (("demo_admin", True),)),
)


def seed_setup(ctx):
    ensure_default_team()  # a reset database has lost the row the data migration made


def seed_records(ctx):
    if TeamMembership.objects.exists():
        return
    admin = User.objects.get(username="demo_admin")
    users = User.objects.in_bulk(
        [n for _, _, m in DEMO_MEMBERS for n, _ in m], field_name="username"
    )
    for slug, name, members in DEMO_MEMBERS:
        team = Team.objects.filter(slug=slug).first() or create_team(actor=admin, name=name)
        if team.slug != slug:
            Team.objects.filter(pk=team.pk).update(slug=slug)
        for username, lead in members:
            if username in users:
                TeamMembership.objects.get_or_create(
                    team=team, user=users[username], defaults={"is_lead": lead}
                )
