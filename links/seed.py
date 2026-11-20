"""Demonstration seed for external systems and links (core.seeding).

Systems: the starter "Laboratory information system" renamed "Example LIS", an OpenBGA LIMS and a
billing system. Organization links are seeded here; request links come
with the requests' story (`seed_request_link`), all with synthetic TEST identifiers.
"""

from accounts.acting import hold_actor
from accounts.models import User
from clients.models import Client
from clients.services import hold_directory

from .models import ExternalSystem
from .services import add_link, create_system, update_system

SYSTEMS = (
    ("example-lims", "Example Environmental LIMS", "openbga", "https://lims.example.invalid/"),
    ("example-billing", "Example Billing", "billing", "https://billing.example.invalid/"),
)
# (organization account code, system key, record type, identifier, label)
CLIENT_LINKS = (
    ("EXWATER", "example-lims", "project", "PROJ-TEST-0001", "Distribution monitoring project"),
    ("EXCLIN", "example-billing", "other", "ACCT-TEST-1001", "Billing account ACCT-TEST-1001"),
)


def seed_setup(ctx):
    admin = User.objects.get(username="demo_admin")
    starter, _ = ExternalSystem.objects.get_or_create(
        key="lis", defaults={"name": "Laboratory information system", "kind": "lis"}
    )
    if starter.name != "Example LIS":
        update_system(
            actor=admin,
            system_id=starter.pk,
            expected_revision=starter.revision,
            reason="Demonstration seed: the laboratory's own name",
            name="Example LIS",
            base_url="https://lis.example.invalid/",
        )
    for key, name, kind, base_url in SYSTEMS:
        if not ExternalSystem.objects.filter(key=key).exists():
            create_system(
                actor=admin,
                key=key,
                name=name,
                kind=kind,
                base_url=base_url,
                reason="Demonstration seed",
            )


def seed_records(ctx):
    actor = User.objects.get(username="demo_account_manager")
    # The actor, then the directory lock, before the first organization's lock: the links lock
    # their organizations in this list's order, which an import may take the other way round.
    hold_actor(actor)
    hold_directory()
    for account, system_key, record_type, identifier, label in CLIENT_LINKS:
        client = Client.objects.filter(account_number=account).first()
        if client is None or client.link_attachments.exists():
            continue
        add_link(
            actor=actor,
            client_id=client.pk,
            system=ExternalSystem.objects.get(key=system_key),
            record_type=record_type,
            identifier=identifier,
            display_label=label,
        )


def seed_request_link(request, *, system_key, record_type, identifier, actor=None, **extra):
    """Attach a synthetic link to a seeded request (for the desk's demonstration story).
    Person-level links need an actor holding the capability: the demonstration story
    grants "Can view patient information" to demo_supervisor before calling this."""
    if actor is None:
        username = (
            "demo_supervisor"
            if record_type in ("patient", "participant", "donor")
            else "demo_client_services"
        )
        actor = User.objects.get(username=username)
    return add_link(
        actor=actor,
        request_id=request.pk,
        system=ExternalSystem.objects.get(key=system_key),
        record_type=record_type,
        identifier=identifier,
        **extra,
    )
