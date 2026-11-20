"""What makes a database the public demonstration's, for reset_demo's whole-database erase.

Two states qualify, and nothing else:

- **empty**: no business record at all. Every application table is empty except the schema's own
  descriptions (content types, permissions) and the rows this release's migrations create (the
  default team, the starter external system), each exactly as the migration created it: a starter
  row that was renamed, re-addressed or otherwise edited is the laboratory's own configuration;
- **sealed**: the laboratory was created by `seed_demo` starting from an empty database, and the
  seed recorded a `demo.seeded` audit event carrying a seal: an HMAC of the laboratory's id and
  name and the seeding account, keyed with the deployment's SECRET_KEY (or one of
  SECRET_KEY_FALLBACKS, so rotating the key keeps a demo resettable). `setup_lab` never writes
  it, audit events cannot be changed afterwards (database trigger), and a laboratory named like
  the seed's is not enough: forging the seal needs the key.
"""

from django.conf import settings
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.utils.crypto import constant_time_compare, salted_hmac

SEAL_ACTION = "demo.seeded"
_SALT = "openbga.lab_ops.demonstration_seal"
SCHEMA_TABLES = {ContentType._meta.db_table, Permission._meta.db_table}


def migration_starters():
    """The rows this release's migrations create in an empty database, as unsaved instances
    (teams/0002_default_team, links/0002_starter_system). Each field not named takes its
    model default, as it did when the migration ran."""
    from links.models import ExternalSystem, SystemKind
    from teams.models import DEFAULT_TEAM_NAME, DEFAULT_TEAM_SLUG, Team

    return (
        Team(slug=DEFAULT_TEAM_SLUG, name=DEFAULT_TEAM_NAME, is_default=True, is_active=True),
        ExternalSystem(key="lis", name="Laboratory information system", kind=SystemKind.LIS),
    )


def _content(instance):
    return {
        field.attname: getattr(instance, field.attname)
        for field in instance._meta.concrete_fields
        if not field.primary_key
    }


def _seal(lab, actor_id, secret):
    value = f"{lab.pk}|{lab.name}|{actor_id}"
    return salted_hmac(_SALT, value, secret=secret, algorithm="sha256").hexdigest()


def seal_for(lab, actor):
    """The seal seed_demo records when it creates the demonstration laboratory."""
    return _seal(lab, actor.pk, settings.SECRET_KEY)


def is_sealed(lab):
    """Whether `lab` carries a valid seal recorded by seed_demo."""
    from core.models import AuditEvent

    secrets = [settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", [])]
    events = AuditEvent.objects.filter(
        action=SEAL_ACTION, object_type="core.lab", object_id=str(lab.pk)
    )
    for event in events:
        recorded = (event.after or {}).get("seal", "")
        if not isinstance(recorded, str):
            continue
        for secret in secrets:
            if constant_time_compare(recorded, _seal(lab, event.actor_id, secret)):
                return True
    return False


def business_tables():
    return [
        table
        for table in connection.introspection.django_table_names(only_existing=True)
        if table not in SCHEMA_TABLES
    ]


def lock_application_tables():
    """Keep every other connection from writing any application table until this transaction
    ends, waiting for writers already under way. Taken before a scan whose answer must still hold
    when the transaction commits: SHARE ROW EXCLUSIVE conflicts with every row write (and with
    itself, so two seeds take turns) but not with reads, and never with this transaction's own
    writes. In READ COMMITTED each later statement sees everything committed before the lock.

    The organization directory lock comes first (clients.services.hold_directory): an import
    takes it before it writes, and the seed creates organizations, which takes it too, so the
    seed waits for an import under way before it holds the tables the import writes to."""
    if not connection.in_atomic_block:
        raise RuntimeError("lock_application_tables() needs a transaction.")
    from clients.services import hold_directory

    hold_directory()
    quote = connection.ops.quote_name
    tables = connection.introspection.django_table_names(only_existing=True)
    with connection.cursor() as cursor:
        cursor.execute(
            f"LOCK TABLE {', '.join(quote(t) for t in tables)} IN SHARE ROW EXCLUSIVE MODE"
        )


def nonempty_tables():
    """Application tables holding anything beyond the migrations' own rows, unchanged."""
    starters = {type(row)._meta.db_table: row for row in migration_starters()}
    quote = connection.ops.quote_name
    found = []
    with connection.cursor() as cursor:
        for table in business_tables():
            if table in starters:
                starter = starters[table]
                rows = [_content(row) for row in type(starter).objects.all()[:2]]
                if rows not in ([], [_content(starter)]):
                    found.append(table)
                continue
            cursor.execute(f"SELECT EXISTS (SELECT 1 FROM {quote(table)})")
            if cursor.fetchone()[0]:
                found.append(table)
    return found
