from django.db import migrations

# Django creates missing default permissions after migrate, but keeps the names of existing ones.
MODEL_LABELS = (
    ("desk", "servicerequest", "request", "ticket"),
    ("desk", "requestentry", "request entry", "ticket entry"),
    ("request_forms", "requesttemplate", "request form", "ticket form"),
    ("service_levels", "requestclock", "request clock", "ticket clock"),
    ("taxonomy", "requestcategory", "request category", "ticket category"),
)


def relabel(apps, schema_editor, *, reverse):
    permissions = apps.get_model("auth", "Permission").objects.using(schema_editor.connection.alias)
    for app, model, old_label, new_label in MODEL_LABELS:
        if reverse:
            old_label, new_label = new_label, old_label
        for action in ("add", "change", "delete", "view"):
            permissions.filter(
                content_type__app_label=app,
                content_type__model=model,
                codename=f"{action}_{model}",
                name=f"Can {action} {old_label}",
            ).update(name=f"Can {action} {new_label}")


def forward(apps, schema_editor):
    relabel(apps, schema_editor, reverse=False)


def backward(apps, schema_editor):
    relabel(apps, schema_editor, reverse=True)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("desk", "0007_ticket_terminology"),
        ("request_forms", "0003_ticket_terminology"),
        ("service_levels", "0002_ticket_terminology"),
    ]
    operations = [migrations.RunPython(forward, backward)]
