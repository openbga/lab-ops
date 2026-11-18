"""Record the author's capacity (laboratory or client) on existing request entries, then keep it
for every entry written from now on and never let it change.

Backfill, from what the entry itself shows before anything about the author's account, which
may have changed kind on a release that allowed it:

1. Entries only staff could have written are the laboratory's, whatever the account is today:
   internal notes, logged contacts, and every event except a request's creation and a status
   change back to open (assignment, team and property changes, complaint marking, decision,
   update and review, reply withdrawal, status changes to waiting, resolved or closed, link
   events, form corrections).
2. A request's "created" event and its first reply: the request's channel says who raised it
   (the portal: the client; any other channel: the laboratory).
3. Any other entry (a later reply, a status change back to open, which a client's reply also
   makes): the author's kind at the time of the entry. The account administration recorded each
   change of an account's client organization (`account.updated`, client before and after); a
   change between laboratory and client account after the entry means the entry was written in
   the kind the account had before it. Without such a record, the author's account today.

Cannot be reconstructed: a later reply or a reopening by an account whose kind was changed
outside the account administration (a shell, a script or SQL), which left no record. Such an
entry follows the account as it is at the upgrade. Kinds have been permanent since clients/0006,
so no entry written after it is affected.

The entry-immutability trigger (desk/0002) refuses every update, so it is disabled for the
backfill inside this migration's transaction only. Afterwards:

- desk_entry_guard also keeps author_capacity unchanged;
- desk_entry_capacity fills a blank capacity at insert from the author's account at that moment,
  so a writer that does not set it (a timeline event of another app, a seed) still records the
  capacity in which the entry was written. desk.services sets it explicitly.
"""

from django.db import connection as default_connection
from django.db import migrations

GUARD = """
CREATE OR REPLACE FUNCTION desk_entry_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Request entries cannot be deleted';
  END IF;
  IF OLD.withdrawn_at IS NOT NULL
     OR NEW.withdrawn_at IS NULL
     OR (NEW.id, NEW.request_id, NEW.kind, NEW.author_id, NEW.author_capacity, NEW.body,
         NEW.contact_direction, NEW.contact_channel, NEW.contact_with, NEW.contact_outcome,
         NEW.event_action, NEW.event_data, NEW.created_at)
        IS DISTINCT FROM
        (OLD.id, OLD.request_id, OLD.kind, OLD.author_id, OLD.author_capacity, OLD.body,
         OLD.contact_direction, OLD.contact_channel, OLD.contact_with, OLD.contact_outcome,
         OLD.event_action, OLD.event_data, OLD.created_at)
  THEN
    RAISE EXCEPTION 'Request entries cannot be changed; a reply can only be withdrawn once';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION desk_entry_capacity() RETURNS trigger AS $$
BEGIN
  IF NEW.author_capacity = '' THEN
    SELECT CASE WHEN client_id IS NULL THEN 'laboratory' ELSE 'client' END
      INTO NEW.author_capacity FROM accounts_user WHERE id = NEW.author_id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER desk_entry_capacity BEFORE INSERT ON desk_requestentry
FOR EACH ROW EXECUTE FUNCTION desk_entry_capacity();
"""

PREVIOUS_GUARD = """
DROP TRIGGER IF EXISTS desk_entry_capacity ON desk_requestentry;
DROP FUNCTION IF EXISTS desk_entry_capacity();
CREATE OR REPLACE FUNCTION desk_entry_guard() RETURNS trigger AS $$
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
"""

LABORATORY, CLIENT = "laboratory", "client"


# Events a client portal account could cause: raising a request in the portal, and the
# reopening its reply makes. Every other event is a staff action.
CLIENT_POSSIBLE_EVENTS = {"created", "status_changed"}


def _staff_only(entry):
    if entry.kind in ("note", "contact"):
        return True
    if entry.kind != "event":
        return False
    if entry.event_action not in CLIENT_POSSIBLE_EVENTS:
        return True
    if entry.event_action == "status_changed":
        return (getattr(entry, "event_data", None) or {}).get("new") != "open"
    return False


def capacity(entry, first_reply_ids, kind_changes=None):
    """The capacity of one legacy entry. `kind_changes` maps an author's id to the recorded
    changes of their account's kind, [(when, was_client_account)], in time order."""
    if _staff_only(entry):
        return LABORATORY
    if (entry.kind == "event" and entry.event_action == "created") or entry.pk in first_reply_ids:
        return CLIENT if entry.request.channel == "portal" else LABORATORY
    history = kind_changes.get(getattr(entry, "author_id", None), ()) if kind_changes else ()
    for when, was_client in history:
        if when > entry.created_at:  # the first recorded change after the entry
            return CLIENT if was_client else LABORATORY
    return CLIENT if entry.author.client_id is not None else LABORATORY


def recorded_kind_changes(apps, author_ids):
    """Changes between laboratory and client account recorded by the account administration."""
    AuditEvent = apps.get_model("core", "AuditEvent")
    changes = {}
    events = AuditEvent.objects.filter(
        action="account.updated",
        object_type="accounts.user",
        object_id__in=[str(pk) for pk in author_ids],
    ).order_by("occurred_at", "id")
    for event in events:
        if "client" not in (event.before or {}) or "client" not in (event.after or {}):
            continue
        was_client = event.before["client"] is not None
        if was_client != (event.after["client"] is not None):
            changes.setdefault(int(event.object_id), []).append((event.occurred_at, was_client))
    return changes


def backfill(apps, schema_editor):
    RequestEntry = apps.get_model("desk", "RequestEntry")
    connection = schema_editor.connection if schema_editor else default_connection
    blank = RequestEntry.objects.filter(author_capacity="")
    if not blank.exists():
        return
    first_reply_ids = set()
    for request_id in blank.values_list("request_id", flat=True).distinct():
        first = (
            RequestEntry.objects.filter(request_id=request_id)
            .exclude(kind="event")
            .order_by("created_at", "pk")
            .first()
        )
        if first is not None and first.kind == "reply":
            first_reply_ids.add(first.pk)
    kind_changes = recorded_kind_changes(
        apps, set(blank.values_list("author_id", flat=True).distinct())
    )
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE desk_requestentry DISABLE TRIGGER desk_entry_immutable")
        try:
            for entry in blank.select_related("request", "author").iterator():
                RequestEntry.objects.filter(pk=entry.pk).update(
                    author_capacity=capacity(entry, first_reply_ids, kind_changes)
                )
        finally:
            cursor.execute("ALTER TABLE desk_requestentry ENABLE TRIGGER desk_entry_immutable")


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("core", "0001_initial"),
        ("desk", "0004_entry_author_capacity"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
        migrations.RunSQL(GUARD, PREVIOUS_GUARD),
    ]
