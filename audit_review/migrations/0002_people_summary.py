# The People tab (audit_review.people): its pages and exports are recorded as workspace uses of a
# fifth source. Only the allowed values change; no row is rewritten. Reversing it fails once a use
# of the People tab is recorded, since the history cannot be changed or deleted.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("audit_review", "0001_initial"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="workspaceuse",
            name="workspace_use_source",
        ),
        migrations.AlterField(
            model_name="workspaceuse",
            name="source",
            field=models.CharField(
                choices=[
                    ("changes", "Changes"),
                    ("access", "Access records"),
                    ("sign_ins", "Sign-ins"),
                    ("people", "People"),
                    ("uses", "Workspace use"),
                ],
                max_length=12,
            ),
        ),
        migrations.AddConstraint(
            model_name="workspaceuse",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("source__in", ["changes", "access", "sign_ins", "people", "uses"])
                ),
                name="workspace_use_source",
            ),
        ),
    ]
