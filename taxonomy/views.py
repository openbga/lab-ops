"""Setup pages for the request taxonomy (T1) and presets (T2). Admin role only; the services
check again."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count, Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.ids import ascii_digits

from . import presets as catalogue
from .forms import CategoryEditForm, CategoryForm, GroupEditForm, GroupForm, PresetForm
from .models import RequestCategory
from .services import (
    create_category,
    load_presets,
    move_category,
    require_setup,
    update_category,
)

LIST_TITLE = "Ticket categories"


def _errors(form, exc):
    if hasattr(exc, "message_dict"):
        for name, errors in exc.message_dict.items():
            form.add_error(name if name in form.fields else None, errors)
    else:
        form.add_error(None, exc)


def _list_url():
    return reverse("taxonomy:category-list")


@login_required
def category_list(request):
    require_setup(request.user)
    show = request.GET.get("show", "active")
    children = RequestCategory.objects.select_related("default_team").order_by(
        "position", "name", "pk"
    )
    groups = RequestCategory.objects.filter(parent__isnull=True)
    if show == "active":
        children = children.filter(is_active=True)
        groups = groups.filter(is_active=True)
    groups = groups.annotate(
        inactive_children=Count("children", filter=Q(children__is_active=False))
    ).prefetch_related(Prefetch("children", queryset=children, to_attr="shown_children"))
    groups = list(groups.order_by("position", "name", "pk"))
    for group in groups:
        group.anchor, group.heading_id = f"group-{group.pk}", f"group-{group.pk}-heading"
    return render(
        request,
        "taxonomy/category_list.html",
        {
            "page_title": LIST_TITLE,
            "groups": groups,
            "show": show,
            "has_any": RequestCategory.objects.exists(),
        },
    )


def _form_page(request, form, *, title, submit_label, crumbs, cancel_url, obj=None):
    return render(
        request,
        "core/record_form.html",
        {
            "page_title": title,
            "submit_label": submit_label,
            "form": form,
            "object": obj,
            "inactive_notice": "This is inactive: it is not offered for new tickets. Existing "
            "tickets keep it.",
            "crumbs": crumbs,
            "cancel_url": cancel_url,
        },
    )


@login_required
def group_create(request):
    require_setup(request.user)
    form = GroupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            group = create_category(actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, f"Group {group.name} added.")
            return redirect(_list_url())
    return _form_page(
        request,
        form,
        title="New group",
        submit_label="Create group",
        crumbs=[(LIST_TITLE, _list_url()), ("New group", None)],
        cancel_url=_list_url(),
    )


@login_required
def category_create(request):
    require_setup(request.user)
    initial = {}
    if group_id := request.GET.get("group", ""):
        if ascii_digits(group_id):
            initial["parent"] = group_id
    form = CategoryForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            category = create_category(actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, f"Category {category.name} added.")
            return redirect(_list_url())
    return _form_page(
        request,
        form,
        title="New category",
        submit_label="Create category",
        crumbs=[(LIST_TITLE, _list_url()), ("New category", None)],
        cancel_url=_list_url(),
    )


@login_required
def category_edit(request, pk):
    require_setup(request.user)
    obj = get_object_or_404(RequestCategory, pk=pk)
    name, is_group = obj.name, obj.is_group
    form_class = GroupEditForm if is_group else CategoryEditForm
    form = form_class(
        request.POST or None, instance=obj, initial={"expected_revision": obj.revision}
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_category(actor=request.user, category_id=obj.pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Changes recorded with their reason.")
            # No #fragment: a dialog save to the same path plus a fragment would not reload.
            return redirect(_list_url())
    return _form_page(
        request,
        form,
        title=f"Edit {name}",
        submit_label="Save changes",
        crumbs=[(LIST_TITLE, _list_url()), (name, None), ("Edit", None)],
        cancel_url=_list_url(),
        obj=obj,
    )


@login_required
@require_POST
def category_move(request, pk, direction):
    require_setup(request.user)
    obj = get_object_or_404(RequestCategory, pk=pk)
    try:
        move_category(actor=request.user, category_id=obj.pk, direction=direction)
    except ValidationError as exc:
        messages.info(request, " ".join(exc.messages))
    return redirect(f"{_list_url()}#group-{obj.parent_id or obj.pk}")


@login_required
def preset_load(request):
    require_setup(request.user)
    form = PresetForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            counts = load_presets(actor=request.user, keys=form.cleaned_data["presets"])
        except ValidationError as exc:
            _errors(form, exc)
        else:
            if any(counts.values()):
                messages.success(
                    request,
                    f"Presets loaded: {counts['groups']} group{'s' * (counts['groups'] != 1)}, "
                    f"{counts['categories']} categor{'ies' if counts['categories'] != 1 else 'y'} "
                    f"and {counts['teams']} team{'s' * (counts['teams'] != 1)} added.",
                )
            else:
                messages.info(request, "Nothing new: everything in these presets is already here.")
            return redirect(_list_url())
    loaded_keys = set(RequestCategory.objects.values_list("key", flat=True))
    presets = [
        {
            "key": key,
            "name": preset.name,
            "description": preset.description,
            "count": len(preset.categories),
            "missing": sum(item.key not in loaded_keys for item in preset.categories),
        }
        for key, preset in catalogue.PRESETS.items()
    ]
    return render(
        request,
        "taxonomy/preset_form.html",
        {
            "page_title": "Load presets",
            "form": form,
            "presets": presets,
            "crumbs": [(LIST_TITLE, _list_url()), ("Load presets", None)],
            "cancel_url": _list_url(),
        },
    )
