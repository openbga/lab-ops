"""Record, for existing portal access, the organization it was granted for, and keep the grant
only where that is evidently the account's current organization.

Before 0004 an account could be moved to another organization with its access untouched, so the
account's current organization is not evidence of where a grant was made. A grant is kept, for
the account's current organization, only when all of these hold:

- its latest `portal_access.set` audit event names that organization (the services have recorded
  the account's organization in every such event since portal access existed);
- its linked contact, if any, belongs to that organization;
- each of its listed sites belongs to that organization.

Every other grant is reset: no contact, no sites, not the whole organization, belonging to the
account's current organization (none for an account that is no longer a client account). The
account then sees only the requests it raised in the portal until staff set its access again.
Failing closed costs a staff decision; failing open would expose another organization's requests.

"""

from django.db import migrations


def _granted_for(AuditEvent, access):
    event = (
        AuditEvent.objects.filter(
            action="portal_access.set",
            object_type="clients.portalaccess",
            object_id=str(access.pk),
        )
        .order_by("-occurred_at", "-id")
        .first()
    )
    return (event.after or {}).get("client") if event else None


def backfill(apps, schema_editor):
    PortalAccess = apps.get_model("clients", "PortalAccess")
    AuditEvent = apps.get_model("core", "AuditEvent")
    for access in PortalAccess.objects.filter(client__isnull=True).select_related(
        "user", "contact"
    ):
        organization = access.user.client_id
        site_clients = set(access.sites.values_list("client_id", flat=True))
        evident = (
            organization is not None
            and _granted_for(AuditEvent, access) == organization
            and (access.contact is None or access.contact.client_id == organization)
            and site_clients <= {organization}
        )
        access.client_id = organization
        if not evident:
            access.contact = None
            access.whole_organization = False
            access.revision += 1
            access.sites.clear()
        access.save()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("core", "0001_initial"),
        ("clients", "0004_portal_access_client"),
    ]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
