"""Team pages: the list, a team with its members, and the create and edit forms. Every staff role
reads them; the admin role creates and changes teams; admins and supervisors manage members."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.roles import require_role, require_staff
from core.models import AuditEvent
from desk.selectors import staff_requests

from . import services
from .forms import MemberForm, TeamEditForm, TeamForm
from .models import Team, TeamMembership

ACTIVE_STATUSES = ("open", "waiting_client", "waiting_third_party")


def _errors(form, exc):
    if hasattr(exc, "message_dict"):
        for name, errors in exc.message_dict.items():
            form.add_error(name if name in form.fields else None, errors)
    else:
        form.add_error(None, exc)


@login_required
def team_list(request):
    require_staff(request.user)
    teams = list(
        Team.objects.annotate(member_count=Count("memberships", distinct=True)).order_by(
            "-is_active", "name"
        )
    )
    # Open or waiting requests per team, among those this user may read (restricted ones out).
    readable = (
        staff_requests(request.user)
        .filter(status__in=ACTIVE_STATUSES)
        .values("team")
        .annotate(n=Count("pk"))
    )
    counts = {row["team"]: row["n"] for row in readable}
    for team in teams:
        team.open_count = counts.get(team.pk, 0)
    return render(
        request,
        "teams/team_list.html",
        {
            "page_title": "Teams",
            "teams": teams,
            "can_manage_teams": services.can_manage_teams(request.user),
        },
    )


def _members_with_open_work(team):
    """The team's members, each with `open_requests`: their requests in this team that are not
    closed, which return to the team's queue if they are removed (the confirmation says so)."""
    from desk.models import ServiceRequest

    members = list(
        team.memberships.select_related("user").order_by(
            "-is_lead", "user__first_name", "user__last_name", "user__username"
        )
    )
    counts = dict(
        ServiceRequest.objects.filter(team=team, assignee__in=[m.user_id for m in members])
        .exclude(status="closed")
        .values("assignee")
        .annotate(n=Count("pk"))
        .values_list("assignee", "n")
    )
    for membership in members:
        membership.open_requests = counts.get(membership.user_id, 0)
    return members


@login_required
def team_detail(request, pk):
    require_staff(request.user)
    team = get_object_or_404(Team, pk=pk)
    can_members = services.can_manage_members(request.user)
    form = MemberForm(request.POST or None) if can_members else None
    if request.method == "POST":
        if not can_members:
            raise PermissionDenied("Admins and supervisors manage team members.")
        if form.is_valid():
            try:
                services.add_member(
                    actor=request.user,
                    team=team,
                    user=form.cleaned_data["user"],
                    is_lead=form.cleaned_data["is_lead"],
                )
            except ValidationError as exc:
                _errors(form, exc)
            else:
                messages.success(request, "Member added.")
                return redirect("teams:team-detail", pk=team.pk)
    return render(
        request,
        "teams/team_detail.html",
        {
            "page_title": team.name,
            "team": team,
            "members": _members_with_open_work(team),
            "member_form": form,
            "can_manage_members": can_members,
            "can_manage_teams": services.can_manage_teams(request.user),
            "history": AuditEvent.objects.filter(object_type="teams.team", object_id=str(team.pk))
            .select_related("actor")
            .order_by("occurred_at"),
        },
    )


@login_required
def team_create(request):
    require_role(request.user, "admin")
    form = TeamForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            team = services.create_team(actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Team created.")
            return redirect("teams:team-detail", pk=team.pk)
    return render(
        request,
        "core/record_form.html",
        {
            "page_title": "New team",
            "submit_label": "Create team",
            "form": form,
            "crumbs": [("Teams", reverse("teams:team-list")), ("New team", None)],
            "cancel_url": reverse("teams:team-list"),
        },
    )


@login_required
def team_edit(request, pk):
    require_role(request.user, "admin")
    team = get_object_or_404(Team, pk=pk)
    name = team.name
    record_url = reverse("teams:team-detail", args=[team.pk])
    form = TeamEditForm(
        request.POST or None, instance=team, initial={"expected_revision": team.revision}
    )
    if request.method == "POST" and form.is_valid():
        try:
            services.update_team(actor=request.user, team_id=team.pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Changes recorded with their reason.")
            return redirect(record_url)
    return render(
        request,
        "core/record_form.html",
        {
            "page_title": f"Edit {name}",
            "submit_label": "Save changes",
            "form": form,
            "object": team,
            "inactive_notice": "This team is inactive: it takes no new tickets.",
            "crumbs": [("Teams", reverse("teams:team-list")), (name, record_url), ("Edit", None)],
            "cancel_url": record_url,
        },
    )


@login_required
@require_POST
def member_remove(request, pk, membership_pk):
    # The role first: whether the row exists is nobody else's business.
    if not services.can_manage_members(request.user):
        raise PermissionDenied("Admins and supervisors manage team members.")
    membership = get_object_or_404(TeamMembership, pk=membership_pk, team_id=pk)
    try:
        released = services.remove_member(actor=request.user, membership=membership)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(
            request,
            "Member removed."
            + (
                f" {released} ticket{'s' if released != 1 else ''} returned to the team's queue."
                if released
                else ""
            ),
        )
    return redirect("teams:team-detail", pk=pk)


@login_required
@require_POST
def member_lead(request, pk, membership_pk):
    if not services.can_manage_members(request.user):
        raise PermissionDenied("Admins and supervisors manage team members.")
    membership = get_object_or_404(TeamMembership, pk=membership_pk, team_id=pk)
    try:
        services.set_lead(
            actor=request.user, membership=membership, is_lead=request.POST.get("is_lead") == "1"
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect("teams:team-detail", pk=pk)
