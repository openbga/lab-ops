from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.db.models import Count, Min
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import NoReverseMatch, reverse

from accounts.roles import effective_roles, has_role, require_staff
from clients.models import Client
from clients.permissions import MANAGE_ROLES as CLIENT_ROLES

from . import changelog, preferences
from .models import AuditEvent, Lab
from .templatetags.navigation import ITEMS, ROUTE_ITEMS, visible_staff_items

# Each area counts its own records. A ticket can appear in several desk queues.
QUEUE_AREAS = (("desk", "Service desk"), ("clients", "Clients"))
QUEUE_STATUS = {
    "hold": "Needs a decision",
    "info": "Waiting",
    "progress": "In review",
    "warning": "Needs attention",
    "danger": "Overdue",
}
RECENT_CLIENTS = 8


def _queue(label, rows, url, params, *, area, icon, oldest=None, tone="info", note="", status=""):
    """One dashboard queue. `rows` is the same queryset the linked list builds for `params`, so
    the count here is the count there. A single aggregate query gives count and oldest item."""
    totals = {"count": Count("pk", distinct=True)}
    if oldest:
        totals["oldest"] = Min(oldest)
    totals = rows.aggregate(**totals)
    return {
        "label": label,
        "count": totals["count"],
        "url": f"{reverse(url)}?{urlencode(params)}" if params else reverse(url),
        "oldest_at": totals.get("oldest"),
        "tone": tone,
        "icon": icon,
        "note": note,
        "area": area,
        "status": status or QUEUE_STATUS[tone],
    }


def _queue_groups(queues):
    groups = []
    for family, name in QUEUE_AREAS:
        rows = [queue for queue in queues if queue["area"] == family]
        if rows:
            # Stable order within each group, with empty queues after those carrying work.
            rows.sort(key=lambda queue: not queue["count"])
            groups.append({"family": family, "name": name, "queues": rows})
    return groups


# Priority order within the service desk. The list itself enforces the operator's scope.
DESK_QUEUES = (
    (
        "Overdue tickets",
        {"view": "overdue"},
        "danger",
        "exclamation-circle-fill",
        "A service clock is past due.",
        None,
    ),
    (
        "Unassigned in my teams",
        {"view": "unassigned"},
        "warning",
        "inbox",
        "Take one or assign it.",
        None,
    ),
    (
        "Complaints awaiting my review",
        {"view": "complaints-review", "complaint": "review"},
        "progress",
        "list-check",
        "Decided complaints you may review independently.",
        ("supervisor",),
    ),
    ("My open tickets", {"view": "my-open"}, "info", "person-circle", "Assigned to you.", None),
)


def _desk_queues(user, roles):
    from desk.lists import view_queryset

    queues = []
    for label, params, tone, icon, note, needs in DESK_QUEUES:
        if needs and not roles.intersection(needs):
            continue
        queues.append(
            _queue(
                label,
                view_queryset(user, params),
                "desk:request-list",
                params,
                area="desk",
                icon=icon,
                oldest="created_at",
                tone=tone,
                note=note,
            )
        )
    return queues


def _work_queues(user):
    """Work waiting for this user. Each queue is shown to the roles that do that work (and may
    open its list); the lists themselves still authorize every request. Feature apps add their
    queues here, in navigation order."""
    roles = effective_roles(user)
    queues = _desk_queues(user, roles)
    if roles & set(CLIENT_ROLES):
        queues.append(
            _queue(
                "Active organizations without an active contact",
                Client.objects.filter(is_active=True).exclude(contacts__is_active=True),
                "clients:client-list",
                {"active": "yes", "contacts": "none"},
                area="clients",
                icon="building",
                tone="warning",
                status="Incomplete",
                note="Add the people the laboratory deals with there.",
            )
        )
    return queues


@login_required
def dashboard(request):
    if request.user.client_id:
        return redirect("portal:home")
    require_staff(request.user)
    queues = _work_queues(request.user)
    return render(
        request,
        "core/dashboard.html",
        {
            "page_title": "Dashboard",
            "queues": queues,
            "queue_groups": _queue_groups(queues),
            "queues_clear": not any(queue["count"] for queue in queues),
            "recent_clients": Client.objects.order_by("-pk")[:RECENT_CLIENTS],
        },
    )


def health(request):
    return JsonResponse({"status": "ok"})


def ready(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ready"})


@login_required
def record_retention(request):
    require_staff(request.user)
    lab = Lab.objects.first()
    return render(
        request,
        "core/record_retention.html",
        {
            "page_title": "Record retention",
            "lab": lab,
            "statement": lab.record_retention_statement if lab else "",
            "can_manage_policy": request.user.is_staff
            and request.user.is_superuser
            and has_role(request.user, "admin"),
            "policy_events": AuditEvent.objects.filter(
                object_type="core.lab", object_id="1", action="lab.record_retention_changed"
            )
            .select_related("actor")
            .order_by("-occurred_at")[:20],
        },
    )


def _entry_link(entry, visible):
    """The page an entry points to, when it is a staff menu destination this user has."""
    key = ROUTE_ITEMS.get(entry.link)
    if not entry.link or key not in visible:
        return None
    try:
        return {"url": reverse(entry.link), "label": ITEMS[key].label}
    except NoReverseMatch:
        return None


def _whats_new_month(group, visible, new_ids):
    sections = [
        {
            "label": label,
            "entries": [
                {"entry": entry, "new": entry.id in new_ids, "link": _entry_link(entry, visible)}
                for entry in entries
            ],
        }
        for _, label, entries in group.sections
    ]
    rows = [row for section in sections for row in section["entries"]]
    # Ties retain the full list's impact and date order.
    rows.sort(key=lambda row: (row["new"], row["entry"].highlight), reverse=True)
    return {
        "key": group.start.strftime("%Y-%m"),
        "label": group.label,
        "sections": sections,
        "count": group.count,
        "new_count": sum(row["new"] for row in rows),
        "highlights": rows[:3],
    }


@login_required
def whats_new(request):
    """Every change in changelog.d/, grouped like CHANGELOG.md (core.changelog), for any staff
    account, with or without a lab role. Opening it marks every entry as seen for this user (the
    whats_new.seen preference), which clears the top bar's indicator. Entries that were unread
    when the user arrived are marked New on this visit; an account without a seen mark has
    nothing new (the top bar records one on its first page)."""
    if request.user.client_id is not None:
        raise PermissionDenied("What's new is part of the staff workspace.")
    entries = changelog.entries()
    marker = preferences.for_request(request).get(preferences.WHATS_NEW_KEY)
    new_ids = {entry.id for entry in changelog.unread(entries, marker)} if marker else set()
    seen = changelog.seen_marker(entries)
    if seen is not None and seen != marker:
        try:
            preferences.save(request, preferences.WHATS_NEW_KEY, seen)
        except ValueError:
            pass  # too many saved preferences: the indicator stays, the list still shows
    visible = visible_staff_items(request.user, request=request)
    return render(
        request,
        "core/whats_new.html",
        {
            "page_title": "What's new",
            "groups": [
                _whats_new_month(group, visible, new_ids)
                for group in changelog.group_entries(entries)
            ],
            "new_count": len(new_ids),
        },
    )


def csrf_failure(request, reason=""):
    """Use the normal error shell without echoing CSRF diagnostics or request values."""
    return render(request, "403.html", {"csrf_failure": True}, status=403)
