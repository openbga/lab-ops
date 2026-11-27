"""Demonstration seed for client organizations, sites, contacts and portal accounts
(core.seeding). Synthetic data only: names use "Example", emails end in .example.invalid,
phone numbers are 555-01xx. Everything is written through the audited services.

The organizations cover the laboratory types of the build plan's seed: a clinic, a health
system with its hospital, a recovery center (toxicology), an employer program (workplace
toxicology), a water utility (environmental), a manufacturer (QC), a research institute
(biorepository) and an inactive former client. demo_client sees its whole organization;
demo_client_water is narrowed to one site, with a last day (PORTAL_LAST_DAYS).
"""

from datetime import timedelta

from accounts.acting import hold_actor
from accounts.grant_ends import later_today
from accounts.models import User

from .models import Client, ClientContact, ClientSite
from .services import (
    add_contact,
    create_client,
    create_site,
    hold_directory,
    set_portal_access,
    update_client,
)

# (organization fields, parent account code or None,
#  sites: ((code, name, kind, phone), ...),
#  contacts: ((name, role, email, phone, responsibilities, site codes), ...))
DEMO_CLIENTS = (
    (
        {
            "name": "Example Family Clinic",
            "account_number": "EXCLIN",
            "organization_type": "practice",
            "email": "frontdesk@clinic.example.invalid",
            "phone": "555-0100",
            "address_line1": "100 Example Street",
            "city": "Exampleville",
            "state": "NY",
            "postal_code": "00000",
            "notes": "Synthetic physician office for demonstration.",
        },
        None,
        (
            ("MAIN", "Main office", "office", "555-0102"),
            ("WEST", "Westside office", "office", "555-0103"),
        ),
        (
            (
                "Synthetic Office Manager",
                "Primary contact",
                "office@clinic.example.invalid",
                "",
                ["ordering", "results"],
                (),
            ),
            (
                "Synthetic Billing Clerk",
                "Billing",
                "billing@clinic.example.invalid",
                "555-0101",
                ["billing"],
                ("MAIN",),
            ),
        ),
    ),
    (
        {
            "name": "Example Health System",
            "account_number": "EXHS",
            "organization_type": "healthcare",
            "email": "outreach@health.example.invalid",
            "phone": "555-0130",
            "city": "Exampleville",
            "state": "NY",
            "notes": "Synthetic health system for demonstration; parent of Example General "
            "Hospital.",
        },
        None,
        (),
        (),
    ),
    (
        {
            "name": "Example General Hospital",
            "account_number": "EXGEN",
            "organization_type": "healthcare",
            "phone": "555-0131",
            "address_line1": "5 Example Plaza",
            "city": "Exampleville",
            "state": "NY",
            "postal_code": "00000",
            "notes": "Synthetic hospital outreach client.",
        },
        "EXHS",
        (
            ("ED", "Emergency department", "facility", "555-0132"),
            ("DRAW", "Outpatient draw station", "collection_site", "555-0133"),
        ),
        (
            (
                "Synthetic Lab Liaison",
                "Laboratory liaison",
                "liaison@hospital.example.invalid",
                "555-0134",
                ["results", "quality"],
                (),
            ),
            (
                "Synthetic Courier Coordinator",
                "Courier coordinator",
                "courier@hospital.example.invalid",
                "555-0135",
                ["logistics"],
                ("DRAW",),
            ),
        ),
    ),
    (
        {
            "name": "Example Recovery Center",
            "account_number": "EXREC",
            "organization_type": "healthcare",
            "phone": "555-0140",
            "city": "Sampletown",
            "state": "PA",
            "notes": "Synthetic treatment program using clinical toxicology.",
        },
        None,
        (("CLINIC", "Clinic", "office", "555-0141"),),
        (
            (
                "Synthetic Program Coordinator",
                "Program coordinator",
                "program@recovery.example.invalid",
                "555-0143",
                ["ordering", "results"],
                (),
            ),
        ),
    ),
    (
        {
            "name": "Example Freight Lines",
            "account_number": "EXFRT",
            "organization_type": "employer",
            "phone": "555-0150",
            "city": "Testburg",
            "state": "OH",
            "notes": "Synthetic employer with a workplace testing program.",
        },
        None,
        (("COLLECT", "Collection site", "collection_site", "555-0151"),),
        (
            (
                "Synthetic Employer Representative",
                "Designated employer representative",
                "der@freight.example.invalid",
                "555-0152",
                ["general"],
                (),
            ),
            (
                "Synthetic Program Administrator",
                "Program administrator",
                "admin@freight.example.invalid",
                "555-0153",
                ["billing"],
                (),
            ),
        ),
    ),
    (
        {
            "name": "Example County Water Authority",
            "account_number": "EXWATER",
            "organization_type": "utility",
            "email": "compliance@water.example.invalid",
            "phone": "555-0110",
            "address_line1": "1 Reservoir Road",
            "city": "Sampletown",
            "state": "PA",
            "postal_code": "00000",
            "notes": "Synthetic utility for demonstration.",
        },
        None,
        (
            ("NORTH", "North treatment plant", "plant", "555-0112"),
            ("SOUTH", "South treatment plant", "plant", "555-0113"),
            ("DIST", "Distribution sampling points", "sampling_location", ""),
        ),
        (
            (
                "Synthetic Compliance Lead",
                "Results and reports",
                "results@water.example.invalid",
                "555-0111",
                ["results", "technical"],
                (),
            ),
            (
                "Synthetic Field Sampler",
                "Field sampler",
                "sampler@water.example.invalid",
                "555-0114",
                ["logistics"],
                ("NORTH", "DIST"),
            ),
        ),
    ),
    (
        {
            "name": "Example Manufacturing Co.",
            "account_number": "EXMFG",
            "organization_type": "manufacturer",
            "phone": "555-0120",
            "city": "Testburg",
            "state": "OH",
            "notes": "Synthetic manufacturer for demonstration. No contact recorded yet.",
        },
        None,
        (("PLANT1", "Plant 1", "plant", "555-0121"),),
        (),
    ),
    (
        {
            "name": "Example Research Institute",
            "account_number": "EXRES",
            "organization_type": "research",
            "email": "studies@research.example.invalid",
            "phone": "555-0160",
            "city": "Exampleville",
            "state": "NY",
            "notes": "Synthetic research sponsor using the biorepository.",
        },
        None,
        (("STUDY", "Study coordination office", "office", "555-0161"),),
        (
            (
                "Synthetic Study Coordinator",
                "Study coordinator",
                "coordinator@research.example.invalid",
                "555-0162",
                ["ordering", "technical"],
                (),
            ),
        ),
    ),
    (
        {"name": "Example Former Client", "account_number": "EXOLD", "city": "Exampleville"},
        None,
        (),
        (),
    ),
)
INACTIVE = "EXOLD"
# Portal accounts: (username, last name, email, account code, linked contact email, site codes or
# None for the whole organization). Each is "Demo" and its client's name, so staff can tell from
# "Logged by" which one raised a request.
DEMO_PORTAL_ACCOUNTS = (
    (
        "demo_client",
        "Family Clinic",
        "client@clinic.example.invalid",
        "EXCLIN",
        "office@clinic.example.invalid",
        None,
    ),
    (
        "demo_client_water",
        "Water Authority",
        "client@water.example.invalid",
        "EXWATER",
        "sampler@water.example.invalid",
        ("NORTH",),
    ),
)


# Portal accounts whose access has a last day, so many days after the seed's day
# (core.seeding.SeedContext.day): the demonstration, reset each day, shows an end to come.
PORTAL_LAST_DAYS = {"demo_client_water": 90}


def seed_setup(ctx):
    """Nothing to configure."""


def portal_account_fields(username, last_name, email):
    """A demonstration portal account's name and address, as the seed creates it (and as
    seed_demo checks the published password against it before writing anything)."""
    # Each portal account is named after its client.
    return {"username": username, "email": email, "first_name": "Demo", "last_name": last_name}


def seed_records(ctx):
    actor = User.objects.get(username="demo_account_manager")
    ctx.created["clients"] = _seed_clients(actor)
    for username, last_name, email, account, contact_email, site_codes in DEMO_PORTAL_ACCOUNTS:
        client = Client.objects.get(account_number=account)
        fields = portal_account_fields(username, last_name, email)
        portal_user, new = User.objects.get_or_create(
            username=fields.pop("username"), defaults={**fields, "client": client}
        )
        if new:
            portal_user.set_password(ctx.password)
            portal_user.save()
        if not hasattr(portal_user, "portal_access"):
            days = PORTAL_LAST_DAYS.get(username)
            set_portal_access(
                actor=actor,
                user_id=portal_user.pk,
                expected_client_id=client.pk,
                contact=ClientContact.objects.get(client=client, email=contact_email),
                whole_organization=site_codes is None,
                sites=list(ClientSite.objects.filter(client=client, code__in=site_codes or ())),
                last_day=(ctx.day or later_today()) + timedelta(days=days) if days else None,
            )


def _seed_clients(actor):
    """Create the synthetic client organizations once, through the audited services. Returns
    those created in this run by account code (none when they already existed)."""
    created = {}
    # The actor, then the directory lock, before deciding: an import under way is waited for and
    # its organizations seen (clients.services.hold_directory).
    hold_actor(actor)
    hold_directory()
    if Client.objects.exists():
        return created
    for fields, parent_code, sites, contacts in DEMO_CLIENTS:
        parent = created[parent_code] if parent_code else None  # created above, in this run
        client = create_client(
            actor=actor, reason="Demonstration seed", parent=parent, account_owner=actor, **fields
        )
        created[fields["account_number"]] = client
        by_code = {}
        for code, name, kind, phone in sites:
            by_code[code] = create_site(
                actor=actor, client_id=client.pk, code=code, name=name, kind=kind, phone=phone
            )
        for name, role, email, phone, responsibilities, site_codes in contacts:
            add_contact(
                actor=actor,
                client_id=client.pk,
                name=name,
                role=role,
                email=email,
                phone=phone,
                responsibilities=responsibilities,
                sites=[by_code[code] for code in site_codes],
            )
        if fields["account_number"] == INACTIVE:
            update_client(
                actor=actor,
                client_id=client.pk,
                expected_revision=client.revision,
                reason="Demonstration seed: no longer a client",
                is_active=False,
            )
    return created
