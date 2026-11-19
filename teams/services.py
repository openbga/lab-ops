"""Changes to teams and their members. Each locks the team, rechecks its revision where the form
carries one, and writes an attributed audit event in the same transaction. The admin role creates
and changes teams; admins and supervisors manage membership (docs/product.md "Roles and
capabilities")."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F
from django.utils.text import slugify

from accounts.roles import ROLES, has_role, require_role
from core.audit import record_event, snapshot

from .models import Team, TeamMembership

TEAM_FIELDS = ("name", "description", "is_active", "is_default")
MEMBERSHIP_ROLES = ("admin", "supervisor")


def can_manage_teams(user):
    return has_role(user, "admin")


def can_manage_members(user):
    return has_role(user, *MEMBERSHIP_ROLES)


def _unique_slug(name):
    base = slugify(name)[:50] or "team"
    slug, n = base, 2
    while Team.objects.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def _check_default(team):
    if team.is_default and not team.is_active:
        raise ValidationError({"is_active": "The default team must stay active."})


@transaction.atomic
def create_team(*, actor, name, description="", is_default=False):
    require_role(actor, "admin")
    team = Team(
        slug=_unique_slug(name),
        name=(name or "").strip(),
        description=(description or "").strip(),
        is_default=bool(is_default),
    )
    # "One default team" holds by moving the default here, in this transaction; the database
    # constraint still guards the result. The other rules are validated as usual.
    team.full_clean(exclude=["is_default"])
    if team.is_default:
        # The team that loses the default changes too: an open form for it is now stale.
        Team.objects.select_for_update().filter(is_default=True).update(
            is_default=False, revision=F("revision") + 1
        )
    team.save()
    record_event(actor=actor, action="team.created", obj=team, after=snapshot(team))
    return team


@transaction.atomic
def update_team(*, actor, team_id, expected_revision, reason, **data):
    require_role(actor, "admin")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required."})
    # No key changes here, so not FOR UPDATE: what holds a team only against a member being added
    # (key share: notices being sent, the breach job) does not hold up an edit of its details.
    team = Team.objects.select_for_update(no_key=True).get(pk=team_id)
    if team.revision != expected_revision:
        raise ValidationError("This team changed. Reload and review the current record.")
    before = snapshot(team)
    if team.is_default and data.get("is_default") is False:
        raise ValidationError(
            {"is_default": "Make another team the default instead; one team always is."}
        )
    for name in TEAM_FIELDS:
        if name in data:
            value = data[name]
            setattr(team, name, value.strip() if isinstance(value, str) else value)
    _check_default(team)
    team.full_clean(exclude=["is_default"])  # the default moves below, as in create_team
    if snapshot(team) == before:
        raise ValidationError("Change at least one detail before saving.")
    if team.is_default and not before["is_default"]:
        Team.objects.select_for_update().filter(is_default=True).exclude(pk=team.pk).update(
            is_default=False, revision=F("revision") + 1
        )
    team.revision += 1
    team.save()
    record_event(
        actor=actor,
        action="team.updated",
        obj=team,
        before=before,
        after=snapshot(team),
        reason=reason,
    )
    return team


def _staff_member(user):
    if user.client_id is not None or not user.is_active or not has_role(user, *ROLES):
        raise ValidationError({"user": "Choose an active staff account."})


@transaction.atomic
def add_member(*, actor, team, user, is_lead=False):
    if not can_manage_members(actor):
        raise PermissionDenied("Admins and supervisors manage team members.")
    team = Team.objects.select_for_update().get(pk=team.pk)
    _staff_member(user)
    if TeamMembership.objects.filter(team=team, user=user).exists():
        raise ValidationError({"user": "This person already belongs to the team."})
    membership = TeamMembership.objects.create(team=team, user=user, is_lead=bool(is_lead))
    record_event(
        actor=actor,
        action="team.member_added",
        obj=team,
        after={"user_id": user.pk, "is_lead": membership.is_lead},
    )
    return membership


@transaction.atomic
def remove_member(*, actor, membership, reason=""):
    """Remove a member. Their requests in this team that are not closed return to the team's
    queue in the same transaction; returns how many."""
    from desk.services import release_assignments

    if not can_manage_members(actor):
        raise PermissionDenied("Admins and supervisors manage team members.")
    membership = TeamMembership.objects.select_for_update().get(pk=membership.pk)
    team, user = membership.team, membership.user
    membership.delete()
    released = release_assignments(
        actor=actor, user=user, team=team, reason=f"Removed from the team {team.name}"
    )
    record_event(
        actor=actor,
        action="team.member_removed",
        obj=team,
        before={"user_id": user.pk, "is_lead": membership.is_lead},
        after={"requests_released": released},
        reason=(reason or "").strip(),
    )
    return released


@transaction.atomic
def set_lead(*, actor, membership, is_lead):
    if not can_manage_members(actor):
        raise PermissionDenied("Admins and supervisors manage team members.")
    membership = TeamMembership.objects.select_for_update().get(pk=membership.pk)
    if membership.is_lead == bool(is_lead):
        raise ValidationError("Nothing to change.")
    membership.is_lead = bool(is_lead)
    membership.save(update_fields=["is_lead"])
    record_event(
        actor=actor,
        action="team.lead_changed",
        obj=membership.team,
        after={"user_id": membership.user_id, "is_lead": membership.is_lead},
    )
    return membership
