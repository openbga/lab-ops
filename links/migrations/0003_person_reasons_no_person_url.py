"""Person-level links get coded removal reasons with an optional private note, and lose
any web address (it can hold the key). Existing addresses on person links are cleared here; the
constraint follows in 0004."""

from django.db import migrations, models


def clear_person_addresses(apps, schema_editor):
    ExternalRecordLink = apps.get_model("links", "ExternalRecordLink")
    ExternalRecordLink.objects.filter(is_person=True).exclude(url="").update(url="")


class Migration(migrations.Migration):
    dependencies = [("links", "0002_starter_system")]

    operations = [
        migrations.AddField(
            model_name="linkattachment",
            name="removal_note",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="linkattachment",
            name="removal_reason_code",
            field=models.CharField(
                blank=True,
                choices=[
                    ("wrong_person", "Wrong person"),
                    ("entered_in_error", "Entered in error"),
                    ("no_longer_needed", "No longer needed"),
                    ("other", "Other"),
                ],
                max_length=30,
            ),
        ),
        migrations.RunPython(clear_person_addresses, migrations.RunPython.noop),
    ]
