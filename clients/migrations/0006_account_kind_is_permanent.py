"""An account stays a laboratory account or a client portal account for life: the database
refuses an update of accounts_user that sets or clears client_id. Moving a portal account to
another organization (one non-null value to another) is allowed; clients.signals resets its
portal access. This covers paths that bypass Model.save() (queryset.update, raw SQL)."""

from django.db import migrations

CREATE = """
CREATE FUNCTION clients_account_kind_is_permanent() RETURNS trigger AS $$
BEGIN
    IF (OLD.client_id IS NULL) <> (NEW.client_id IS NULL) THEN
        RAISE EXCEPTION 'An account stays a laboratory account or a client portal account for life.'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER clients_account_kind_is_permanent
    BEFORE UPDATE OF client_id ON accounts_user
    FOR EACH ROW EXECUTE FUNCTION clients_account_kind_is_permanent();
"""

DROP = """
DROP TRIGGER IF EXISTS clients_account_kind_is_permanent ON accounts_user;
DROP FUNCTION IF EXISTS clients_account_kind_is_permanent();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("clients", "0005_backfill_portal_access_client"),
    ]

    operations = [migrations.RunSQL(CREATE, DROP)]
