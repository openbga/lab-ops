from django.db import migrations

# Attachments are history: never deleted, never changed, except that a supervisor may withdraw one
# once (the three withdrawal columns, set together). Their bytes are never changed or deleted.
# reset_demo empties these tables with TRUNCATE, which row triggers do not see.
SQL = """
CREATE FUNCTION attachments_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Attachments cannot be deleted';
  END IF;
  IF OLD.withdrawn_at IS NOT NULL
     OR NEW.withdrawn_at IS NULL
     OR (NEW.id, NEW.request_id, NEW.entry_id, NEW.original_name, NEW.extension,
         NEW.content_type, NEW.size, NEW.sha256, NEW.scan_status, NEW.uploaded_by_id,
         NEW.uploaded_at)
        IS DISTINCT FROM
        (OLD.id, OLD.request_id, OLD.entry_id, OLD.original_name, OLD.extension,
         OLD.content_type, OLD.size, OLD.sha256, OLD.scan_status, OLD.uploaded_by_id,
         OLD.uploaded_at)
  THEN
    RAISE EXCEPTION 'Attachments cannot be changed; a file can only be withdrawn once';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER attachments_immutable BEFORE UPDATE OR DELETE ON attachments_attachment
FOR EACH ROW EXECUTE FUNCTION attachments_guard();

CREATE FUNCTION attachments_blob_guard() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'Attachment files cannot be changed or deleted';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER attachments_blob_immutable BEFORE UPDATE OR DELETE ON attachments_attachmentblob
FOR EACH ROW EXECUTE FUNCTION attachments_blob_guard();
"""
REVERSE = """
DROP TRIGGER attachments_blob_immutable ON attachments_attachmentblob;
DROP FUNCTION attachments_blob_guard();
DROP TRIGGER attachments_immutable ON attachments_attachment;
DROP FUNCTION attachments_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("attachments", "0001_initial")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
