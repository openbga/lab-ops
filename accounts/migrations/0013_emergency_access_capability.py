from django.db import migrations


def remove_capability(apps, schema_editor):
    """Reversed: the capability goes with its grants, to people and to groups, and their ends, so
    that applying the migration again gives it to nobody. (Django creates the permission after a
    forward migration and never removes one.)"""
    Permission = apps.get_model("auth", "Permission")
    Permission.objects.filter(
        content_type__app_label="accounts",
        content_type__model="user",
        codename="use_emergency_access",
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0012_deactivation"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        # The capability "Can use emergency access" (accounts.roles.CAPABILITIES, desk.emergency):
        # a permission Django creates after the migration, granted to nobody.
        migrations.AlterModelOptions(
            name="user",
            options={
                "permissions": [
                    ("view_patient_information", "Can view patient information"),
                    ("review_audit_records", "Can review access and audit records"),
                    ("use_emergency_access", "Can use emergency access"),
                ],
                "verbose_name": "user",
                "verbose_name_plural": "users",
            },
        ),
        migrations.RunPython(migrations.RunPython.noop, remove_capability),
    ]
