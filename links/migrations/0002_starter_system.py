"""Setup creates one external system for the laboratory to rename (L1)."""

from django.db import migrations


def create_starter(apps, schema_editor):
    ExternalSystem = apps.get_model("links", "ExternalSystem")
    ExternalSystem.objects.get_or_create(
        key="lis", defaults={"name": "Laboratory information system", "kind": "lis"}
    )


class Migration(migrations.Migration):
    dependencies = [("links", "0001_initial")]

    operations = [migrations.RunPython(create_starter, migrations.RunPython.noop)]
