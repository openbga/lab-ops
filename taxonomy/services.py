"""Changes to the request taxonomy (T1) and preset loading (T2).

Admin only (docs/product.md "Roles and capabilities"). Each change locks what it changes,
rechecks the revision, and records an attributed audit event in the same transaction. A
category's key never changes. Changing the complaint or restricted flag is recorded as
`category.flags_changed`, with the reason every edit needs.
"""

from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import Max
from django.db.models.functions import Upper

from accounts.roles import require_role
from core.audit import record_event, snapshot
from teams.models import Team

from . import presets as catalogue
from .models import RequestCategory

SETUP_ROLE = "admin"
FIELDS = (
    "name",
    "client_label",
    "guidance",
    "parent",
    "default_team",
    "default_priority",
    "client_may_raise",
    "is_complaint",
    "is_restricted",
    "is_active",
)
FLAGS = ("is_complaint", "is_restricted")
STALE = "This category changed after you opened it. Reload and review the current record."
# Serializes preset loading: two admins loading at once must not race on the same keys.
_PRESET_LOCK = 0x4C43_5450  # "LCTP"


def require_setup(user):
    require_role(user, SETUP_ROLE)


def _reason(reason):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required."})
    return reason


def _clean_text(obj):
    for name in ("name", "client_label", "guidance"):
        setattr(obj, name, (getattr(obj, name) or "").strip())


def _check_structure(obj):
    """Two levels: a group has no parent; a category's parent is a group."""
    parent = obj.parent
    if parent is None:
        return
    if obj.pk and parent.pk == obj.pk:
        raise ValidationError({"parent": "A category cannot be its own group."})
    if parent.parent_id is not None:
        raise ValidationError({"parent": "Choose a group. Categories have two levels only."})
    if obj.pk and RequestCategory.objects.filter(parent_id=obj.pk).exists():
        raise ValidationError(
            {"parent": "This group holds categories, so it cannot move inside another group."}
        )


def _check_team(obj, before_team_id=None):
    team = obj.default_team
    if team is not None and not team.is_active and team.pk != before_team_id:
        raise ValidationError({"default_team": "Choose an active team."})


def _save(obj):
    try:
        with transaction.atomic():
            obj.save()
    except IntegrityError as error:
        raise ValidationError(
            {"name": "This group already has a category with this name."}
        ) from error


def _next_position(parent_id):
    last = RequestCategory.objects.filter(parent_id=parent_id).aggregate(m=Max("position"))["m"]
    return 10 if last is None else last + 10


@transaction.atomic
def create_category(*, actor, key, reason="", **data):
    require_setup(actor)
    obj = RequestCategory(key=(key or "").strip(), **{k: v for k, v in data.items() if k in FIELDS})
    _clean_text(obj)
    if obj.parent is not None:
        RequestCategory.objects.select_for_update().get(pk=obj.parent_id)
    _check_structure(obj)
    _check_team(obj)
    if not obj.position:
        obj.position = _next_position(obj.parent_id)
    if RequestCategory.objects.filter(key=obj.key).exists():
        raise ValidationError({"key": "Another category or group already uses this key."})
    obj.full_clean(validate_constraints=False)
    _save(obj)
    record_event(
        actor=actor,
        action="category.created",
        obj=obj,
        after=snapshot(obj),
        reason=(reason or "").strip(),
    )
    return obj


@transaction.atomic
def update_category(*, actor, category_id, expected_revision, reason, **data):
    require_setup(actor)
    reason = _reason(reason)
    obj = RequestCategory.objects.select_for_update().get(pk=category_id)
    if obj.revision != expected_revision:
        raise ValidationError(STALE)
    if "key" in data and (data["key"] or "").strip() != obj.key:
        raise ValidationError({"key": "A category's key never changes."})
    before = snapshot(obj)
    before_team_id = obj.default_team_id
    for name in FIELDS:
        if name in data:
            setattr(obj, name, data[name])
    _clean_text(obj)
    _check_structure(obj)
    _check_team(obj, before_team_id)
    obj.full_clean(validate_constraints=False)
    after = snapshot(obj)
    if after == before:
        raise ValidationError("Change at least one detail before saving.")
    flags_changed = any(before[name] != after[name] for name in FLAGS)
    obj.revision += 1
    _save(obj)
    record_event(
        actor=actor,
        action="category.flags_changed" if flags_changed else "category.updated",
        obj=obj,
        before=before,
        after=snapshot(obj),
        reason=reason,
    )
    return obj


@transaction.atomic
def move_category(*, actor, category_id, direction):
    """Swap a group or category with its neighbour above ('up') or below ('down')."""
    require_setup(actor)
    if direction not in ("up", "down"):
        raise ValidationError("Move a category up or down.")
    obj = RequestCategory.objects.select_for_update().get(pk=category_id)
    siblings = list(
        RequestCategory.objects.select_for_update()
        .filter(parent_id=obj.parent_id)
        .order_by("position", "name", "pk")
    )
    index = next(i for i, sibling in enumerate(siblings) if sibling.pk == obj.pk)
    other_index = index - 1 if direction == "up" else index + 1
    if not 0 <= other_index < len(siblings):
        raise ValidationError(f"This is already the {'first' if direction == 'up' else 'last'}.")
    # Renumber the whole sibling list so equal positions cannot stall a move.
    siblings[index], siblings[other_index] = siblings[other_index], siblings[index]
    for position, sibling in enumerate(siblings, start=1):
        if sibling.position != position * 10:
            sibling.position = position * 10
            sibling.save(update_fields=["position"])
    record_event(
        actor=actor,
        action="category.reordered",
        obj=obj,
        after={"direction": direction, "order": [sibling.pk for sibling in siblings]},
    )
    return obj


def offered_categories(*, portal=False):
    """Categories a new request may take: active, in an active group; for the portal, only
    those clients may raise. Ordered by group, then position."""
    queryset = RequestCategory.objects.filter(
        is_active=True, parent__isnull=False, parent__is_active=True
    )
    if portal:
        queryset = queryset.filter(client_may_raise=True)
    return queryset.select_related("parent").order_by(
        "parent__position", "parent__name", "position", "name"
    )


# Presets -----------------------------------------------------------------------------------


def _audit_subject():
    """record_event needs an object; a preset load is an action on the catalogue, not a row.
    The presets loaded are in the event's `after`."""
    return SimpleNamespace(_meta=SimpleNamespace(label_lower="taxonomy.preset"), pk="catalogue")


def _team(slug, counts, actor):
    team = Team.objects.filter(slug=slug).first()
    if team is None:
        name = catalogue.TEAMS[slug]
        # A team the laboratory already keeps under that name serves, whatever its slug.
        team = Team.objects.filter(name__iexact=name).first()
    if team is None:
        team = Team.objects.create(slug=slug, name=catalogue.TEAMS[slug])
        record_event(
            actor=actor,
            action="team.created",
            obj=team,
            after=snapshot(team),
            reason="Created by a ticket category preset.",
        )
        counts["teams"] += 1
    return team


def _existing(key, name, parent_id):
    return (
        RequestCategory.objects.filter(key=key).first()
        or RequestCategory.objects.filter(parent_id=parent_id)
        .annotate(upper=Upper("name"))
        .filter(upper=name.upper())
        .first()
    )


def _create_from_preset(actor, preset_key, counts, **fields):
    obj = RequestCategory(preset=preset_key, **fields)
    obj.position = _next_position(obj.parent_id)
    obj.full_clean(validate_constraints=False)
    obj.save()
    record_event(
        actor=actor,
        action="category.created",
        obj=obj,
        after=snapshot(obj),
        reason=f"Loaded from the preset “{catalogue.PRESETS[preset_key].name}”.",
    )
    counts["groups" if obj.parent_id is None else "categories"] += 1
    return obj


@transaction.atomic
def load_presets(*, actor, keys):
    """Load presets additively. "All laboratories" always comes first. Returns the counts of
    created groups, categories and teams; existing rows (matched by key, or by name within the
    same group) are left exactly as they are. No service targets are created."""
    require_setup(actor)
    unknown = set(keys) - set(catalogue.PRESETS)
    if unknown:
        raise ValidationError(f"Unknown preset: {', '.join(sorted(unknown))}.")
    ordered = [catalogue.ALWAYS] + [k for k in catalogue.PRESETS if k in keys and k != "all"]
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_PRESET_LOCK])
    counts = {"groups": 0, "categories": 0, "teams": 0}
    groups = {}
    for preset_key in ordered:
        for item in catalogue.PRESETS[preset_key].categories:
            if item.group not in groups:
                groups[item.group] = _existing(
                    item.group, catalogue.GROUPS[item.group], None
                ) or _create_from_preset(
                    actor,
                    preset_key,
                    counts,
                    key=item.group,
                    name=catalogue.GROUPS[item.group],
                )
            group = groups[item.group]
            if _existing(item.key, item.name, group.pk):
                continue
            _create_from_preset(
                actor,
                preset_key,
                counts,
                key=item.key,
                name=item.name,
                parent=group,
                default_team=_team(item.team, counts, actor),
                default_priority=item.priority,
                client_may_raise=item.client_may_raise,
                is_complaint=item.is_complaint,
                is_restricted=item.is_restricted,
            )
    if any(counts.values()):
        record_event(
            actor=actor,
            action="preset.loaded",
            obj=_audit_subject(),
            after={"presets": ordered, **counts},
        )
    # The standard request forms of the same presets follow their categories. They
    # load additively too and record their own audit events; the counts above stay categories'.
    from request_forms.presets import load_form_presets

    load_form_presets(actor=actor, keys=ordered)
    return counts
