from django.db import migrations

# Form values are history: never deleted and never changed, except that a correction marks the
# value it replaces superseded, once. The filled form itself never changes. Whatever code path
# asks.
SQL = """
CREATE FUNCTION request_forms_value_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Request form values cannot be deleted';
  END IF;
  IF OLD.superseded_at IS NOT NULL
     OR NEW.superseded_at IS NULL
     OR (NEW.id, NEW.filled_form_id, NEW.field_id, NEW.key, NEW.label, NEW.kind, NEW.audience,
         NEW.holds_patient_information, NEW.position, NEW.value::text, NEW.entered_by_id,
         NEW.entered_at, NEW.replaces_id, NEW.reason, NEW.reason_code)
        IS DISTINCT FROM
        (OLD.id, OLD.filled_form_id, OLD.field_id, OLD.key, OLD.label, OLD.kind, OLD.audience,
         OLD.holds_patient_information, OLD.position, OLD.value::text, OLD.entered_by_id,
         OLD.entered_at, OLD.replaces_id, OLD.reason, OLD.reason_code)
  THEN
    RAISE EXCEPTION 'Request form values cannot be changed; a correction adds a value';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER request_forms_value_immutable BEFORE UPDATE OR DELETE
ON request_forms_fieldvalue FOR EACH ROW EXECUTE FUNCTION request_forms_value_guard();

CREATE FUNCTION request_forms_filled_guard() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'A request''s filled form cannot be changed or deleted';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER request_forms_filled_immutable BEFORE UPDATE OR DELETE
ON request_forms_filledform FOR EACH ROW EXECUTE FUNCTION request_forms_filled_guard();
"""
REVERSE = """
DROP TRIGGER request_forms_filled_immutable ON request_forms_filledform;
DROP FUNCTION request_forms_filled_guard();
DROP TRIGGER request_forms_value_immutable ON request_forms_fieldvalue;
DROP FUNCTION request_forms_value_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("request_forms", "0001_initial")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
