from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("links", "0003_person_reasons_no_person_url")]

    operations = [
        migrations.AddConstraint(
            model_name="externalrecordlink",
            constraint=models.CheckConstraint(
                condition=models.Q(("is_person", False), ("url", ""), _connector="OR"),
                name="external_link_person_has_no_url",
                violation_error_message="Person-level links have no web address.",
            ),
        ),
    ]
