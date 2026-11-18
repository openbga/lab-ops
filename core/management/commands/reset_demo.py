"""Erase a demonstration database and seed it again, as one transaction.

For a public demonstration whose visitors share accounts: a scheduled job runs this so the
synthetic records, accounts and announcements return to the seed's state. It is never part of a
normal deployment. Three things must all hold before anything is erased: the deployment opts in
with ALLOW_DEMO_RESET=true, the database is empty of business records or was provably created by
the demonstration seed (core.demo: a sealed demonstration laboratory), and the database schema is
exactly this release's.
"""

import os

from django.apps import apps
from django.conf import settings
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor

from accounts.models import User
from core import demo
from core.models import Lab

from .seed_demo import DEMO_LAB_NAME

# This project's sequences that no table column owns (a feature's own numbering), so TRUNCATE
# does not restart them. Only names starting with one of this project's app labels: anything else
# in the schema (another tool's sequence) is not the demonstration's to restart.
UNOWNED_SEQUENCES = """
SELECT c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'S'
  AND n.nspname = current_schema()
  AND c.relname LIKE ANY(%s)
  AND NOT EXISTS (
    SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype IN ('a', 'i')
  )
ORDER BY c.relname
"""


def project_sequence_patterns():
    """LIKE patterns for sequences named after this project's own apps."""
    base = str(settings.BASE_DIR)
    labels = [app.label for app in apps.get_app_configs() if str(app.path).startswith(base)]
    return [label.replace("_", "\\_") + "\\_%" for label in sorted(labels)]


class Command(BaseCommand):
    help = (
        "Demonstration deployments only: erase every record and run seed_demo again, in one "
        "transaction. Requires ALLOW_DEMO_RESET=true and the demonstration laboratory."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_false",
            dest="interactive",
            help="Do not ask for confirmation (for a scheduled job).",
        )
        parser.add_argument(
            "--announce-accounts",
            action="store_true",
            help="Post the demo accounts on the sign-in page (seed_demo --announce-accounts).",
        )

    def handle(self, *args, **options):
        if os.environ.get("ALLOW_DEMO_RESET", "").lower() != "true":
            raise CommandError(
                "Refusing to reset: set ALLOW_DEMO_RESET=true on a demonstration deployment. "
                "Never set it where a laboratory keeps its records."
            )
        self._require_this_release()
        with transaction.atomic():
            self._require_demo_laboratory()
            if options["interactive"]:
                name = connection.settings_dict["NAME"]
                answer = input(
                    f"This erases every record in database {name!r}, including accounts and audit "
                    "history, then seeds the demonstration again.\n"
                    "Type 'reset' to continue: "
                )
                if answer != "reset":
                    raise CommandError("Reset cancelled. Nothing was changed.")
            # Decide again with every other writer shut out (not while a person reads the
            # prompt), so nothing written after the decision is erased with it.
            demo.lock_application_tables()
            self._require_demo_laboratory()
            tables = self._erase()
            call_command(
                "seed_demo",
                announce_accounts=options["announce_accounts"],
                stdout=self.stdout,
                verbosity=options["verbosity"],
            )
        self.stdout.write(
            self.style.SUCCESS(f"Demonstration reset: {tables} tables erased and seeded again.")
        )

    def _require_this_release(self):
        """A reset from another release than the web service's would seed the wrong schema."""
        executor = MigrationExecutor(connection)
        if executor.migration_plan(executor.loader.graph.leaf_nodes()):
            raise CommandError(
                "Refusing to reset: this release has migrations the database lacks. Deploy the "
                "web service, which migrates, then run the reset from the same release."
            )
        if executor.loader.applied_migrations.keys() - executor.loader.disk_migrations.keys():
            raise CommandError(
                "Refusing to reset: the database has migrations this release does not know. "
                "Deploy this job from the same release as the web service."
            )

    def _require_demo_laboratory(self):
        """Erase only a database that is empty of business records or was provably created by
        the demonstration seed. A laboratory merely named like the seed's does not qualify."""
        names = list(Lab.objects.values_list("name", flat=True))
        if names and names != [DEMO_LAB_NAME]:
            raise CommandError(
                f"Refusing to reset: this database belongs to {names[0]!r}, not the "
                "demonstration laboratory. Nothing was changed."
            )
        if not names and User.objects.exists():
            raise CommandError(
                "Refusing to reset: the database has accounts but no demonstration laboratory. "
                "Nothing was changed."
            )
        if names:
            if not demo.is_sealed(Lab.objects.get(name=DEMO_LAB_NAME)):
                raise CommandError(
                    "Refusing to reset: this laboratory was not created by the demonstration "
                    "seed (it carries no valid demonstration seal), whatever its name. Nothing "
                    "was changed."
                )
            return
        tables = demo.nonempty_tables()
        if tables:
            raise CommandError(
                "Refusing to reset: the database has no demonstration laboratory but is not "
                f"empty ({', '.join(tables[:5])}{', ...' if len(tables) > 5 else ''}). Nothing "
                "was changed."
            )

    def _erase(self):
        """Empty every application table. Content types and permissions describe the schema,
        not the laboratory, and running web processes cache their IDs, so they stay."""
        keep = {ContentType._meta.db_table, Permission._meta.db_table}
        tables = [
            table
            for table in connection.introspection.django_table_names(only_existing=True)
            if table not in keep
        ]
        quote = connection.ops.quote_name
        connection.check_constraints()  # TRUNCATE refuses while deferred checks are pending
        with connection.cursor() as cursor:
            # Give up rather than queue behind a long request; the next run tries again.
            cursor.execute("SET LOCAL lock_timeout = '60s'")
            # The history tables' triggers guard rows against UPDATE and DELETE; TRUNCATE of the
            # whole demonstration is the one deliberate exception and fires none of them.
            cursor.execute(f"TRUNCATE {', '.join(quote(t) for t in tables)} RESTART IDENTITY")
            cursor.execute(UNOWNED_SEQUENCES, [project_sequence_patterns()])
            for (sequence,) in cursor.fetchall():
                cursor.execute(f"ALTER SEQUENCE {quote(sequence)} RESTART")
        return len(tables)
