"""Saved views of the request list: a name, the list's own filters and a sort, kept private or
shared with a team or everyone (sharing needs the supervisor role). A view's filters are checked
against the list's filter form when saved and again when used, so a view never widens what its
reader may see: the list always starts from staff_requests(reader)."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Q

from accounts.roles import require_staff
from core.audit import record_event, snapshot

from .lists import COLUMNS, SAVED_FILTERS, RequestFilter
from .models import SavedView, ViewSharing
from .permissions import is_supervisor


def clean_filters(params):
    """The storable filters among `params` (a QueryDict or dict): valid ones, as strings."""
    data = {name: params.get(name, "") for name in SAVED_FILTERS if params.get(name)}
    form = RequestFilter(data)
    if not form.is_valid():
        raise ValidationError("Correct the list's filters before saving them as a view.")
    return {name: value for name, value in data.items() if form.cleaned_data.get(name)}


def clean_sort(sort):
    sort = (sort or "").strip()
    return sort if sort.removeprefix("-") in COLUMNS else ""


SEARCH_NOT_RECORDED = "(search text not recorded)"


def _audited(view):
    """The view's audit snapshot without the text of its search, which can name a caller or a
    contact: history records that the view keeps a search, not what it is."""
    data = snapshot(view)
    if (data.get("filters") or {}).get("q"):
        data["filters"] = {**data["filters"], "q": SEARCH_NOT_RECORDED}
    return data


def visible_views(user):
    """Views this staff account may use: its own, those shared with everyone, and those shared
    with a team it belongs to."""
    require_staff(user)
    return SavedView.objects.filter(
        Q(owner=user)
        | Q(shared_with=ViewSharing.EVERYONE)
        | Q(shared_with=ViewSharing.TEAM, team__memberships__user=user)
    ).distinct()


def _sharing(actor, shared_with, team):
    if shared_with not in ViewSharing.values:
        raise ValidationError({"shared_with": "Choose who sees the view."})
    if shared_with != ViewSharing.PRIVATE and not is_supervisor(actor):
        raise PermissionDenied("Only a supervisor shares a view with a team or everyone.")
    if shared_with == ViewSharing.TEAM and team is None:
        raise ValidationError({"team": "Choose the team."})
    return team if shared_with == ViewSharing.TEAM else None


@transaction.atomic
def create_view(*, actor, name, shared_with, team=None, params, sort=""):
    require_staff(actor)
    view = SavedView(
        owner=actor,
        name=(name or "").strip(),
        shared_with=shared_with,
        team=_sharing(actor, shared_with, team),
        filters=clean_filters(params),
        sort=clean_sort(sort),
        position=(SavedView.objects.filter(owner=actor).aggregate(top=Max("position"))["top"] or 0)
        + 1,
    )
    view.full_clean()
    view.save()
    record_event(actor=actor, action="saved_view.created", obj=view, after=_audited(view))
    return view


def _own(actor, view):
    require_staff(actor)
    view = SavedView.objects.select_for_update().get(pk=view.pk)
    if view.owner_id != actor.pk:
        raise PermissionDenied("Only the view's owner changes it.")
    return view


@transaction.atomic
def update_view(*, actor, view, expected_revision, name, shared_with, team=None):
    view = _own(actor, view)
    if view.revision != expected_revision:
        raise ValidationError("This view changed. Reload it.")
    before = _audited(view)
    view.name = (name or "").strip()
    view.shared_with = shared_with
    view.team = _sharing(actor, shared_with, team)
    view.full_clean()
    if _audited(view) == before:
        raise ValidationError("Change the name or who sees it before saving.")
    view.revision += 1
    view.save()
    record_event(
        actor=actor, action="saved_view.updated", obj=view, before=before, after=_audited(view)
    )
    return view


@transaction.atomic
def delete_view(*, actor, view, expected_revision=None):
    view = _own(actor, view)
    if expected_revision is not None and str(view.revision) != str(expected_revision):
        raise ValidationError("This view changed. Review it before deleting.")
    record_event(actor=actor, action="saved_view.deleted", obj=view, before=_audited(view))
    view.delete()


@transaction.atomic
def move_view(*, actor, view, direction):
    """Swap the view with its neighbour among the owner's views ("up" or "down")."""
    view = _own(actor, view)
    views = list(SavedView.objects.select_for_update().filter(owner=actor))
    index = next(i for i, v in enumerate(views) if v.pk == view.pk)
    other = index - 1 if direction == "up" else index + 1
    if not 0 <= other < len(views):
        return view
    for position, item in enumerate(views, 1):
        item.position = position
    views[index].position, views[other].position = views[other].position, views[index].position
    SavedView.objects.bulk_update(views, ["position"])
    record_event(actor=actor, action="saved_view.moved", obj=view, after={"direction": direction})
    return view
