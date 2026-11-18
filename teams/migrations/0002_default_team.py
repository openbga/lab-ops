from django.db import migrations

# A fresh deployment needs one team to route requests to. The laboratory renames it freely;
# teams.models.ensure_default_team recreates it after a demonstration reset.


def create_default_team(apps, schema_editor):
    Team = apps.get_model("teams", "Team")
    if not Team.objects.filter(is_default=True).exists():
        Team.objects.update_or_create(
            slug="client-services",
            defaults={"name": "Client services", "is_default": True, "is_active": True},
        )


class Migration(migrations.Migration):
    dependencies = [("teams", "0001_initial")]
    operations = [migrations.RunPython(create_default_team, migrations.RunPython.noop)]
