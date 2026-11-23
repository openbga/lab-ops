import os
from datetime import date
from pathlib import Path

from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.acting import hold_actors, scope
from accounts.models import User
from accounts.passwords import (
    SINGLE_FACTOR_MINIMUM,
    check_published_password,
    laboratory_named,
)
from accounts.roles import ROLE_LABELS, ROLES
from clients.services import hold_directory
from core import demo, lab_profile
from core.announcements import save_announcement
from core.audit import record_event
from core.models import Announcement, AuditEvent, Lab, LabCertificate
from core.seeding import SeedContext, run_seed

DEMO_LAB_NAME = "Example Reference Laboratory"
# An invented emblem for the demonstration laboratory; not the product's mark.
DEMO_LAB_LOGO = Path(__file__).resolve().parents[2] / "demo_assets" / "example-lab-logo.png"
DEMO_LAB_IDENTITY = {
    "address_line1": "200 Example Way",
    "address_line2": "Building C (synthetic)",
    "city": "Exampleville",
    "state": "NY",
    "postal_code": "00000",
    "phone": "555-0142",
    "email": "clientservices@example.invalid",
    "website": "https://laboratory.example.invalid",
}
# kind: name or office, title or role, email, phone. Invented, like every seeded record.
DEMO_LAB_CONTACTS = {
    "security": (
        "Information Security Office",
        "Security official",
        "security@example.invalid",
        "555-0143",
    ),
    "privacy": ("Compliance Office", "Privacy official", "privacy@example.invalid", "555-0144"),
}
# (issuing authority, program or scope, certificate or laboratory ID, has validity dates)
DEMO_LAB_CERTIFICATES = (
    ("Example Federal Program", "Laboratory certificate", "EX-0000000000", True),
    ("Example Accreditation Body", "Testing laboratory", "EX-ACC-0000", False),
)
ACCOUNTS_ANNOUNCEMENT = "Demo accounts"
# Listed on the sign-in page by --announce-accounts. demo_admin manages announcements and
# accounts, so it is never listed and must not share the published password.
ANNOUNCED_ACCOUNTS = (
    (
        "demo_supervisor",
        "supervisor: every ticket, restricted ones too, complaints awaiting review, patient links",
    ),
    ("demo_client_services", "client services: the desk queues, calls, replies and assignments"),
    ("demo_account_manager", "account manager: organizations, sites, contacts, portal access"),
    ("demo_technical", "technical staff: technical questions; restricted tickets stay hidden"),
    ("demo_client", "client portal of a clinic, whole organization"),
    ("demo_client_water", "client portal of a water utility, one plant only"),
)

# Every account the seed's steps act as (see each app's seed).
SEED_ACTORS = ("demo_admin", *(username for username, _summary in ANNOUNCED_ACCOUNTS))


def staff_account_fields(role):
    """A demonstration staff account's name and address, as the seed creates it."""
    return {
        "username": f"demo_{role}",
        "email": f"{role.replace('_', '-')}@example.invalid",
        "first_name": "Demo",
        "last_name": ROLE_LABELS[role],
        "is_staff": role == "admin",
    }


def published_passwords():
    """(DEMO_PASSWORD, the password demo_admin gets), each setting read once, checked: the length
    of a password used alone, then the rule for every account the seed creates. Refused with a
    CommandError that never repeats a password.

    seed_demo checks them before its transaction, so that nothing a password check reads is held
    while it waits for the seed lock; reset_demo checks them once, holding that lock, before it
    erases, and gives them to the seed (a caller that wraps seed_demo in a transaction of its own
    holds whatever the check reads, as reset_demo does after taking the lock). Validators see each
    account as the seed will create it, before it exists: with its username, email address and
    names, and no id or organization yet. On a reset they run before the erase, so a validator
    that reads the database sees the demonstration being replaced; the release's validators read
    nothing there (accounts.passwords.laboratory_named with read_saved false)."""
    password = os.environ.get("DEMO_PASSWORD")
    # The length the password rule asks of a password used alone (accounts.passwords).
    if not password or len(password) < SINGLE_FACTOR_MINIMUM:
        raise CommandError(
            f"Set DEMO_PASSWORD to at least {SINGLE_FACTOR_MINIMUM} characters; it is never "
            "printed."
        )
    admin_setting = os.environ.get("DEMO_ADMIN_PASSWORD") or None
    if admin_setting is not None and len(admin_setting) < SINGLE_FACTOR_MINIMUM:
        raise CommandError(
            f"DEMO_ADMIN_PASSWORD needs at least {SINGLE_FACTOR_MINIMUM} characters, or leave "
            "it unset."
        )
    check_published_passwords(password, admin_setting)
    return password, admin_setting or password


def check_published_passwords(password, admin_password):
    """Before anything is locked or written: the passwords the seed publishes meet the password
    rule for every account it creates (accounts.passwords), each checked against the account as
    it will be created. A refusal names the setting and, unless the password contains its
    username, the account, never the password."""
    from clients.seed import DEMO_PORTAL_ACCOUNTS, portal_account_fields

    accounts = []
    for role in ROLES:
        if role == "admin" and admin_password:
            accounts.append((staff_account_fields(role), admin_password, "DEMO_ADMIN_PASSWORD"))
        else:
            accounts.append((staff_account_fields(role), password, "DEMO_PASSWORD"))
    for username, last_name, email, *_rest in DEMO_PORTAL_ACCOUNTS:
        fields = portal_account_fields(username, last_name, email)
        accounts.append((fields, password, "DEMO_PASSWORD"))
    # The seed's laboratory is the demonstration's (it refuses any other), and the check comes
    # before the seed may read a table: compare with its name rather than reading the saved one.
    with laboratory_named(DEMO_LAB_NAME, read_saved=False):
        for fields, chosen, setting in accounts:
            try:
                check_published_password(chosen, User(**fields), setting)
            except ValidationError as error:
                raise CommandError(" ".join(error.messages)) from None


def refuse_announcing_a_shared_admin_password(password, admin_password):
    """--announce-accounts publishes DEMO_PASSWORD, which must not open demo_admin: refused
    before anything is written when demo_admin is about to get it (reset_demo)."""
    if admin_password == password:
        raise CommandError(
            "Refusing to publish DEMO_PASSWORD: it also opens demo_admin, which manages "
            "announcements and accounts. Set DEMO_ADMIN_PASSWORD to another password and run this "
            "again. Nothing was changed."
        )


class Command(BaseCommand):
    # reset_demo gives the seed the passwords it checked (published_passwords), so that one
    # reading of the settings is checked and used. Only through Django's Python API (no
    # command-line flag): a caller passing it is taken to have checked them. A deployment that
    # replaces seed_demo with its own command keeps this option, or reset_demo cannot call it.
    stealth_options = ("checked_passwords",)
    help = "Create synthetic demo records once; never reset existing data or passwords."

    def add_arguments(self, parser):
        parser.add_argument(
            "--announce-accounts",
            action="store_true",
            help="For a public demonstration: also post a sign-in page announcement that lists "
            "the demo accounts and DEMO_PASSWORD, where anyone can read them, and stop those "
            "accounts changing their own credentials. demo_admin is left out and needs its own "
            "password: set DEMO_ADMIN_PASSWORD before the first seed. DEMO_RESET_NOTE, if set, "
            "ends the announcement (for example when a scheduled reset_demo runs).",
        )

    def handle(self, *args, **options):
        # The settings first, each read once, outside the transaction: whatever their check reads
        # is not held while the seed waits for its lock. reset_demo has checked them already.
        password, admin_password = options.get("checked_passwords") or published_passwords()
        with transaction.atomic():
            # Then one seed at a time; then the demonstration accounts it acts as, in their
            # gates' order (as an access change takes them), held for the whole seed
            # (accounts.acting.scope: its steps lock those that existed when it began nothing
            # again; accounts it creates are its own until it commits); then the organization
            # directory lock, before anything else it locks. No step of the seed then holds a
            # record an import needs while it waits for the import, and a seed that waited sees
            # the accounts the one before made.
            demo.hold_seed_lock()
            actors = hold_actors(User.objects.filter(username__in=SEED_ACTORS))
            with scope(actors):
                hold_directory()
                return self._seed(password, admin_password, *args, **options)

    def _seed(self, password, admin_password, *args, **options):
        # Only a seed that starts from an empty database (the test reset_demo applies) may seal
        # the laboratory it creates: the seal is what later lets reset_demo erase everything.
        # The lock keeps every other connection from writing until this seed commits, so the
        # database the seal is issued for is the one scanned plus what this seed writes.
        leftovers = []
        if not Lab.objects.exists():
            demo.lock_application_tables()
            leftovers = demo.nonempty_tables()
        # The laboratory row is locked before its name is checked and stays locked until this
        # seed commits: a rename committed before is seen here, and one started later waits.
        lab = Lab.objects.select_for_update().filter(pk=1).first()
        created = lab is None
        if created:
            lab = Lab.objects.create(pk=1, name=DEMO_LAB_NAME)
        if not created and lab.name != DEMO_LAB_NAME:
            raise CommandError("Refusing to seed a non-demo laboratory.")
        if leftovers:
            self.stderr.write(
                "This database was not empty before the seed ("
                + ", ".join(leftovers[:5])
                + (", ..." if len(leftovers) > 5 else "")
                + "), so the demonstration is seeded "
                "without its seal: reset_demo will refuse to erase it and --announce-accounts "
                "will refuse to publish DEMO_PASSWORD. For a public demonstration, start from an "
                "empty database."
            )
        for role in ROLES:
            group, _ = Group.objects.get_or_create(name=role)
            fields = staff_account_fields(role)
            user, new = User.objects.get_or_create(username=fields.pop("username"), defaults=fields)
            if new:
                user.set_password(admin_password if role == "admin" else password)
                user.save()
                user.groups.add(group)
        if created and not leftovers:
            # The seal that makes this database the demonstration's for reset_demo (core.demo).
            # Only for a laboratory this seed created in an empty database: a laboratory set up
            # with setup_lab, even under the same name, or a database that already held
            # accounts or records, never gets one.
            admin = User.objects.get(username="demo_admin")
            record_event(
                actor=admin,
                action=demo.SEAL_ACTION,
                obj=lab,
                after={"seal": demo.seal_for(lab, admin)},
                reason="Demonstration laboratory created by seed_demo",
            )
        self._seed_profile()
        run_seed(SeedContext(password=password, stdout=self.stdout))
        if options.get("announce_accounts"):
            posted = self._announce_accounts(password)
            self.stdout.write(
                "Demo accounts announced on the sign-in page."
                if posted
                else "Demo accounts announcement already present; left as it is."
            )
        self.stdout.write(
            self.style.SUCCESS("Synthetic demo ready; existing records/passwords retained.")
        )

    def _seed_profile(self):
        """Give the demonstration laboratory an invented identity, once.

        A profile that was ever changed, by an earlier seed or by an administrator, is left as
        it is.
        """
        admin = User.objects.get(username="demo_admin")
        # Every profile change locks the laboratory row first (core.lab_profile), so with the row
        # locked here a change committed before is seen by the check below, and one that starts
        # later waits for this seed's transaction.
        # The name is read again under the lock: setup_lab may have renamed the laboratory since
        # the seed checked it, and a laboratory that is no longer the demonstration's gets no
        # invented profile.
        lab = Lab.objects.select_for_update().filter(pk=1).first()
        if lab is None or lab.name != DEMO_LAB_NAME:
            return
        if AuditEvent.objects.filter(
            action__in=lab_profile.PROFILE_ACTIONS
        ).exists() or not lab_profile.can_manage(admin):
            return
        lab_profile.save_identity(actor=admin, values=DEMO_LAB_IDENTITY)
        lab_profile.set_logo(
            actor=admin,
            uploaded=SimpleUploadedFile(DEMO_LAB_LOGO.name, DEMO_LAB_LOGO.read_bytes()),
        )
        year = timezone.localdate().year
        for position, (authority, program, number, dated) in enumerate(DEMO_LAB_CERTIFICATES, 1):
            lab_profile.save_certificate(
                actor=admin,
                instance=LabCertificate(
                    authority=authority,
                    program=program,
                    number=number,
                    valid_from=date(year, 1, 1) if dated else None,
                    valid_until=date(year + 1, 12, 31) if dated else None,
                    position=position,
                ),
            )
        for kind, (name, title, email, phone) in DEMO_LAB_CONTACTS.items():
            lab_profile.save_contact(
                actor=admin,
                kind=kind,
                values={"name": name, "title": title, "email": email, "phone": phone},
                expected_revision=0,
                reason="Demonstration contact, invented for the synthetic laboratory.",
            )

    def _announce_accounts(self, password):
        """Lock the accounts DEMO_PASSWORD opens and post them on the sign-in page, once.

        Never demo_admin: it manages announcements and accounts. Only on the sealed demonstration
        laboratory, and only for accounts holding exactly the authority the seed gives them
        (seeded_authority); any difference refuses the whole announcement.
        """
        lab = Lab.objects.filter(pk=1).first()
        if lab is None or not demo.is_sealed(lab):
            raise CommandError(
                "Refusing to publish DEMO_PASSWORD: this laboratory was not created by the "
                "demonstration seed. Nothing was saved."
            )
        admin = User.objects.get(username="demo_admin")
        if admin.check_password(password):
            raise CommandError(
                "Refusing to publish DEMO_PASSWORD: it also opens demo_admin, which manages "
                "announcements and accounts. Give demo_admin its own password (set "
                "DEMO_ADMIN_PASSWORD before the first seed, or change its password) and run "
                "this again. Nothing was saved."
            )
        users = User.objects.in_bulk(
            [name for name, _ in ANNOUNCED_ACCOUNTS], field_name="username"
        )
        shared = [
            (name, work)
            for name, work in ANNOUNCED_ACCOUNTS
            if name in users and users[name].check_password(password)
        ]
        if not shared:
            raise CommandError(
                "No demo account uses DEMO_PASSWORD, so there is nothing to announce. "
                "Nothing was saved."
            )
        # Anyone can sign in to these accounts, so none of them may change its own credentials.
        User.objects.filter(username__in=[name for name, _ in shared]).update(
            credentials_locked=True
        )
        problems = [
            problem
            for name, _ in shared
            for problem in _authority_problems(User.objects.get(username=name))
        ]
        if problems:
            raise CommandError(
                "REFUSING TO PUBLISH DEMO_PASSWORD: an announced account holds other authority "
                "than the announcement states. " + " ".join(problems) + " Correct the accounts "
                "in the admin console (or reset the demonstration) and run this again. Nothing "
                "was saved."
            )
        if Announcement.objects.filter(title=ACCOUNTS_ANNOUNCEMENT).exists():
            return False  # posted before; an administrator may have changed or withdrawn it
        lines = [f"{name}: {work}" for name, work in shared]
        # A deployment with a scheduled reset_demo says when (render.demo.yaml sets this).
        reset_note = (
            os.environ.get("DEMO_RESET_NOTE", "").strip()
            or "The demonstration can be reset at any time."
        )
        body = "\n".join(
            [
                "Public demonstration with synthetic data. Sign in as:",
                "",
                *lines,
                "",
                f"Password for each: {password}",
                "",
                "Do not enter real data. Anyone can use these accounts. " + reset_note,
            ]
        )
        try:
            save_announcement(
                actor=admin,
                instance=Announcement(title=ACCOUNTS_ANNOUNCEMENT, body=body, show_on_sign_in=True),
            )
        except PermissionDenied as exc:
            raise CommandError(
                "demo_admin no longer holds the admin role, so it cannot post the announcement. "
                "Nothing was saved."
            ) from exc
        except ValidationError as exc:
            raise CommandError(
                f"The demo accounts announcement was refused: {' '.join(exc.messages)} "
                "Nothing was saved."
            ) from exc
        return True


def seeded_authority(username):
    """Everything that decides what `username` can read or do, as the seed defines it.

    Derived from the seed's own definitions, so the announcement can only describe what the seed
    built: the staff role in the account's name (this command), the capability grant
    (desk.story), team memberships (teams.seed) and the portal grant (clients.seed). A portal
    grant is (account code, linked contact's email, site codes or None for the whole
    organization).
    """
    from clients.seed import DEMO_PORTAL_ACCOUNTS
    from desk.story import CAPABILITY, CAPABILITY_HOLDER
    from teams.seed import DEMO_MEMBERS

    portal = {row[0]: (row[3], row[4], row[5]) for row in DEMO_PORTAL_ACCOUNTS}
    grant = portal.get(username)
    if grant is not None:
        grant = (grant[0], grant[1], None if grant[2] is None else frozenset(grant[2]))
    roles = {role for role in ROLES if username == f"demo_{role}"}
    return {
        "roles": roles,
        "capabilities": {CAPABILITY} if username == CAPABILITY_HOLDER else set(),
        "teams": {
            (slug, lead)
            for slug, _, members in DEMO_MEMBERS
            for name, lead in members
            if name == username
        },
        "portal": grant,
    }


def held_authority(user):
    """The same facts as seeded_authority, read from the account as it is now."""
    from accounts.roles import CAPABILITIES, has_capability
    from clients.models import PortalAccess

    access = PortalAccess.objects.filter(user=user).select_related("client", "contact").first()
    grant = None
    if access is not None:
        sites = frozenset(access.sites.values_list("code", flat=True))
        grant = (
            access.client.account_number,
            access.contact.email if access.contact_id else None,
            None if access.whole_organization else sites,
        )
        foreign_sites = access.sites.exclude(client_id=access.client_id).exists()
        if (access.whole_organization and sites) or foreign_sites:
            grant += ("sites outside the grant",)
        if access.client_id != user.client_id or (
            access.contact_id and access.contact.client_id != access.client_id
        ):
            grant += ("granted for another organization",)
    held = {code for code in CAPABILITIES if has_capability(user, code)}
    return {
        "roles": set(user.groups.values_list("name", flat=True)),
        "capabilities": held | set(user.user_permissions.values_list("codename", flat=True)),
        "teams": set(user.team_memberships.values_list("team__slug", "is_lead")),
        "portal": grant,
    }


SUPERVISING_ROLES = {"supervisor", "admin"}  # desk.permissions.is_supervisor: every ticket


def seeded_reach(username):
    """What the announcement says `username` can read, from the seed's definitions.

    Staff: patient information only for the capability holder; restricted tickets only for a
    supervising role (the story keeps restricted work in Quality, whose members supervise).
    Portal: its own organization, and the listed sites, or every active site of a
    whole-organization grant.
    """
    from clients.models import ClientSite

    authority = seeded_authority(username)
    patient = bool(authority["capabilities"])
    if authority["portal"] is None:
        return {
            "patient information": patient,
            "restricted tickets": bool(authority["roles"] & SUPERVISING_ROLES),
        }
    code, _, sites = authority["portal"]
    if sites is None:
        sites = frozenset(
            ClientSite.objects.filter(client__account_number=code, is_active=True).values_list(
                "code", flat=True
            )
        )
    return {
        "patient information": patient,
        "restricted tickets": False,
        "organizations": {code},
        "sites": sites,
    }


def held_reach(user):
    """What `user` can read now, evaluated with the selectors every page uses."""
    from clients.portal import current_access
    from desk.portal_scope import portal_requests, portal_sites, raised_by
    from desk.selectors import staff_requests
    from links.services import can_see_person_links
    from request_forms.selectors import may_see_patient_information
    from taxonomy.models import RequestCategory

    patient = may_see_patient_information(user) or can_see_person_links(user)
    if user.client_id is None:
        # Restricted tickets it reads now, or will as soon as one arrives in its team. None:
        # the account reads no tickets at all (it holds no staff role).
        try:
            reads_restricted = (
                staff_requests(user).filter(category__is_restricted=True).exists()
                or RequestCategory.objects.filter(
                    is_restricted=True, default_team__memberships__user=user
                ).exists()
            )
        except PermissionDenied:
            reads_restricted = None
        return {"patient information": patient, "restricted tickets": reads_restricted}
    others = portal_requests(user).exclude(raised_by(user))
    sites = frozenset(portal_sites(user).values_list("code", flat=True))
    access = current_access(user)
    whole = access is not None and access.whole_organization
    beyond = others.exclude(site__code__in=sites)
    if whole:
        beyond = beyond.exclude(site__isnull=True)  # the organization's own, siteless tickets
    return {
        "patient information": patient,
        "restricted tickets": others.filter(category__is_restricted=True).exists(),
        "organizations": set(others.values_list("client__account_number", flat=True).distinct())
        | {user.client.account_number},
        "sites": sites | ({"(tickets beyond these sites)"} if beyond.exists() else set()),
    }


def _authority_problems(user):
    """Why `user` may not be announced: any difference from seeded_authority, or not locked."""
    expected = seeded_authority(user.username)
    held = held_authority(user)
    problems = [
        f"{user.username}: {fact} are {_show(held[fact])}, not {_show(expected[fact])}."
        for fact in expected
        if held[fact] != expected[fact]
    ]
    expected_reach = seeded_reach(user.username)
    reach = held_reach(user)
    problems += [
        f"{user.username} can read {fact}: {_show(reach.get(fact))}, the announcement states "
        f"{_show(expected_reach[fact])}."
        for fact in expected_reach
        if reach.get(fact) != expected_reach[fact]
    ]
    if user.is_staff or user.is_superuser:
        problems.append(f"{user.username} is a staff or superuser account.")
    portal_account = expected["portal"] is not None
    client_code = user.client.account_number if user.client_id else None
    if (user.client_id is not None) != portal_account or (
        portal_account and client_code != expected["portal"][0]
    ):
        problems.append(f"{user.username} is not the kind of account announced.")
    if not user.is_active or not user.credentials_locked:
        problems.append(f"{user.username} is inactive or not credential-locked.")
    return problems


def _show(value):
    if isinstance(value, (set, frozenset)):
        return str(sorted(value))
    if isinstance(value, tuple):
        return "(" + ", ".join(_show(part) for part in value) + ")"
    return repr(value)
