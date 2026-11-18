"""Words for a request's timeline events (RequestEntry of kind event), from their codes and ids.
Event data holds ids and codes only; names are looked up here, for the page that shows them."""

from accounts.models import User
from taxonomy.choices import Priority
from teams.models import Team

from .models import Channel, ComplaintDecision, EntryKind, Status

STATUS = dict(Status.choices)
PRIORITY = dict(Priority.choices)
CHANNEL = dict(Channel.choices)
DECISION = dict(ComplaintDecision.choices)


def _name(user):
    return (user.get_full_name() or user.get_username()) if user else "nobody"


def describe(entries):
    """Attach `.phrase` to every event entry of `entries` (a list) and return the list."""
    events = [e for e in entries if e.kind == EntryKind.EVENT]
    team_ids, user_ids = set(), set()
    for event in events:
        data = event.event_data
        team_ids.update(v for k, v in data.items() if k.endswith("team_id") and v)
        user_ids.update(v for k, v in data.items() if k.endswith("assignee_id") and v)
    teams = Team.objects.in_bulk(team_ids)
    users = User.objects.in_bulk(user_ids)
    for event in events:
        event.phrase = _phrase(event.event_action, event.event_data, teams, users)
    return entries


def _phrase(action, data, teams, users):
    if action == "created":
        return f"Logged ({CHANNEL.get(data.get('channel'), 'other')})"
    if action == "assigned":
        team = teams.get(data.get("team_id"))
        person = users.get(data.get("assignee_id"))
        if data.get("team_id") != data.get("old_team_id"):
            moved = f"Moved to {team}" if team else "Moved to another team"
            return f"{moved}, assigned to {_name(person)}" if person else moved
        return f"Assigned to {_name(person)}" if person else "Assignee cleared"
    if action == "status_changed":
        return f"Status: {STATUS.get(data.get('old'))} → {STATUS.get(data.get('new'))}"
    if action == "updated":
        parts = []
        if "priority" in data.get("fields", ()):
            parts.append(f"priority {PRIORITY.get(data.get('priority'), '').lower()}")
        if "category_id" in data.get("fields", ()):
            parts.append("category changed")
        return "Changed " + " and ".join(parts) if parts else "Details changed"
    if action == "complaint_marked":
        return "Marked as a complaint" + (" (by its category)" if data.get("by_category") else "")
    if action == "complaint_unmarked":
        return "No longer a complaint"
    if action == "complaint_decided":
        return f"Complaint decision: {DECISION.get(data.get('decision'), '').lower()}"
    if action == "complaint_updated":
        return "Complaint record updated"
    if action == "complaint_reviewed":
        return "Complaint reviewed"
    if action == "reply_withdrawn":
        return "A reply was withdrawn from client view"
    if action == "attachment_withdrawn":  # never the file's name
        return "A file was withdrawn"
    if action in ("link.attached", "link.removed"):
        verb = "added" if action == "link.attached" else "removed"
        kind = str(data.get("record_type") or "record").replace("_", " ")
        if data.get("is_person"):  # never a label or key: the word for the kind only
            return f"{kind.capitalize()} link {verb}"
        return f"Link {verb}: {kind}"
    return action.replace("_", " ").replace(".", " ").capitalize()
