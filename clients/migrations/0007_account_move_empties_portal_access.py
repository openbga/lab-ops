"""Moving a portal account to another organization empties its portal access in the database,
whatever path makes the move (a save, queryset.update, raw SQL): no linked contact, no listed
sites, not the whole organization, and the access now belongs to the new organization. A later
move back therefore revives nothing; staff decide the access again. The audited reset is still
written by clients.signals on a save, with the state captured before the update.

Extends the trigger function of clients/0006, which keeps refusing a change of account kind."""

from django.db import migrations

FUNCTION = """
CREATE OR REPLACE FUNCTION clients_account_kind_is_permanent() RETURNS trigger AS $$
BEGIN
    IF (OLD.client_id IS NULL) <> (NEW.client_id IS NULL) THEN
        RAISE EXCEPTION 'An account stays a laboratory account or a client portal account for life.'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.client_id IS DISTINCT FROM NEW.client_id THEN
        DELETE FROM clients_portalaccess_sites
            WHERE portalaccess_id IN (SELECT id FROM clients_portalaccess WHERE user_id = NEW.id);
        UPDATE clients_portalaccess
            SET contact_id = NULL,
                whole_organization = FALSE,
                client_id = NEW.client_id,
                revision = revision + 1
            WHERE user_id = NEW.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

PREVIOUS = """
CREATE OR REPLACE FUNCTION clients_account_kind_is_permanent() RETURNS trigger AS $$
BEGIN
    IF (OLD.client_id IS NULL) <> (NEW.client_id IS NULL) THEN
        RAISE EXCEPTION 'An account stays a laboratory account or a client portal account for life.'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


class Migration(migrations.Migration):
    dependencies = [("clients", "0006_account_kind_is_permanent")]

    operations = [migrations.RunSQL(FUNCTION, PREVIOUS)]
