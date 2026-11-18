"""Breach notices of the scheduled job (service_levels.jobs).

Adds when each clock's breach was noticed. Clocks already running past their due time when this
migration runs are marked noticed now, so the first run of the job does not send a notice for
every breach that happened before it was installed. Reversing drops the columns.
"""

from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone

STOPPED = ("resolved", "closed")


def mark_existing_breaches(apps, schema_editor):
    RequestClock = apps.get_model("service_levels", "RequestClock")
    now = timezone.now()
    running = RequestClock.objects.exclude(request__status__in=STOPPED)
    running.filter(first_response_met_at__isnull=True, first_response_due_at__lt=now).update(
        first_response_breach_noticed_at=now
    )
    running.filter(
        Q(resolution_met_at__isnull=True)
        & Q(paused_since__isnull=True)
        & Q(resolution_due_at__lt=now)
    ).update(resolution_breach_noticed_at=now)


class Migration(migrations.Migration):
    dependencies = [
        ("service_levels", "0002_ticket_terminology"),
    ]

    operations = [
        migrations.AddField(
            model_name="requestclock",
            name="first_response_breach_noticed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="requestclock",
            name="resolution_breach_noticed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(mark_existing_breaches, migrations.RunPython.noop),
    ]
