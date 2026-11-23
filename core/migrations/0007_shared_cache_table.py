"""The database table of the shared cache (CACHES in config/settings.py), where the sign-in
limits count failed attempts for every server process. The same table Django's createcachetable
makes, created here so that `migrate` alone prepares a deployment. It holds no model: nothing
reads it but the cache, and reset_demo leaves it alone (its counts expire within minutes)."""

from django.db import migrations

TABLE = "lab_ops_cache"


class Migration(migrations.Migration):
    dependencies = [("core", "0006_second_factor_rule")]

    operations = [
        migrations.RunSQL(
            sql=[
                f'CREATE TABLE IF NOT EXISTS "{TABLE}" ('
                '"cache_key" varchar(255) NOT NULL PRIMARY KEY, '
                '"value" text NOT NULL, '
                '"expires" timestamp with time zone NOT NULL)',
                f'CREATE INDEX IF NOT EXISTS "{TABLE}_expires" ON "{TABLE}" ("expires")',
            ],
            reverse_sql=[f'DROP TABLE IF EXISTS "{TABLE}"'],
        )
    ]
