from django.db import migrations

# Request entries are history: never deleted, never changed, except that a reply may be withdrawn
# from client view once (the three withdrawal columns, set together). Whatever code path asks.
SQL = """
CREATE FUNCTION desk_entry_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Request entries cannot be deleted';
  END IF;
  IF OLD.withdrawn_at IS NOT NULL
     OR NEW.withdrawn_at IS NULL
     OR (NEW.id, NEW.request_id, NEW.kind, NEW.author_id, NEW.body, NEW.contact_direction,
         NEW.contact_channel, NEW.contact_with, NEW.contact_outcome, NEW.event_action,
         NEW.event_data, NEW.created_at)
        IS DISTINCT FROM
        (OLD.id, OLD.request_id, OLD.kind, OLD.author_id, OLD.body, OLD.contact_direction,
         OLD.contact_channel, OLD.contact_with, OLD.contact_outcome, OLD.event_action,
         OLD.event_data, OLD.created_at)
  THEN
    RAISE EXCEPTION 'Request entries cannot be changed; a reply can only be withdrawn once';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER desk_entry_immutable BEFORE UPDATE OR DELETE ON desk_requestentry
FOR EACH ROW EXECUTE FUNCTION desk_entry_guard();
"""
REVERSE = """
DROP TRIGGER desk_entry_immutable ON desk_requestentry;
DROP FUNCTION desk_entry_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("desk", "0001_initial")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
