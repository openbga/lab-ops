from django.db import migrations

# Audit events and uploaded logos are history: the database refuses to change or delete them,
# whatever code path asks. Feature apps attach their own history tables to the same function.
SQL = """
CREATE FUNCTION prevent_record_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'Historical records cannot be updated or deleted';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE ON core_auditevent
FOR EACH ROW EXECUTE FUNCTION prevent_record_mutation();
CREATE TRIGGER lab_logo_immutable BEFORE UPDATE OR DELETE ON core_lablogo
FOR EACH ROW EXECUTE FUNCTION prevent_record_mutation();
"""
REVERSE = """
DROP TRIGGER lab_logo_immutable ON core_lablogo;
DROP TRIGGER audit_immutable ON core_auditevent;
DROP FUNCTION prevent_record_mutation();
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
