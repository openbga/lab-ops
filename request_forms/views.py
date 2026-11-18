"""Setup pages for request forms (F1, F7) and catalogs (F3), mounted at /setup/request-forms/.
Admin role only; the services check again."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.models import AuditEvent

from .forms import (
    CatalogEditForm,
    CatalogForm,
    CopyForm,
    FieldEditForm,
    FieldForm,
    ItemEditForm,
    ItemForm,
    TemplateEditForm,
    TemplateForm,
    ValuesForm,
)
from .models import Catalog, CatalogItem, RequestTemplate, TemplateField
from .services import (
    clone_template,
    create_catalog,
    create_field,
    create_item,
    create_template,
    move_field,
    move_item,
    move_template,
    require_setup,
    update_catalog,
    update_field,
    update_item,
    update_template,
)

LIST_TITLE = "Ticket forms"
CATALOGS_TITLE = "Catalogs"
HISTORY_LIMIT = 20


def _errors(form, exc):
    if hasattr(exc, "message_dict"):
        for name, errors in exc.message_dict.items():
            form.add_error(name if name in form.fields else None, errors)
    else:
        form.add_error(None, exc)


def _list_url():
    return reverse("request_forms:template-list")


def _catalogs_url():
    return reverse("request_forms:catalog-list")


def _history(obj, limit=HISTORY_LIMIT):
    return (
        AuditEvent.objects.filter(object_type=obj._meta.label_lower, object_id=str(obj.pk))
        .select_related("actor")
        .order_by("-occurred_at")[:limit]
    )


def _form_page(request, form, *, title, submit_label, crumbs, cancel_url, obj=None, notice=""):
    return render(
        request,
        "core/record_form.html",
        {
            "page_title": title,
            "submit_label": submit_label,
            "form": form,
            "object": obj,
            "inactive_notice": notice,
            "crumbs": crumbs,
            "cancel_url": cancel_url,
        },
    )


def _show(request):
    return "all" if request.GET.get("show") == "all" else "active"


# Templates -----------------------------------------------------------------------------------


@login_required
def template_list(request):
    require_setup(request.user)
    show = _show(request)
    templates = RequestTemplate.objects.select_related("category", "category__parent").annotate(
        field_count=Count("fields", filter=Q(fields__is_active=True))
    )
    inactive = templates.filter(is_active=False).count()
    if show == "active":
        templates = templates.filter(is_active=True)
    return render(
        request,
        "request_forms/template_list.html",
        {
            "page_title": LIST_TITLE,
            "templates": list(templates.order_by("position", "name", "pk")),
            "show": show,
            "inactive_count": inactive,
            "has_any": RequestTemplate.objects.exists(),
        },
    )


@login_required
def template_create(request):
    require_setup(request.user)
    form = TemplateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            template = create_template(actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, f"Ticket form {template.name} added. Now add its fields.")
            return redirect("request_forms:template-detail", pk=template.pk)
    return _form_page(
        request,
        form,
        title="New ticket form",
        submit_label="Create ticket form",
        crumbs=[(LIST_TITLE, _list_url()), ("New ticket form", None)],
        cancel_url=_list_url(),
    )


@login_required
def template_detail(request, pk):
    require_setup(request.user)
    template = get_object_or_404(
        RequestTemplate.objects.select_related("category", "category__parent"), pk=pk
    )
    show = _show(request)
    fields = template.fields.select_related("catalog").annotate(value_count=Count("values"))
    inactive = fields.filter(is_active=False).count()
    if show == "active":
        fields = fields.filter(is_active=True)
    category = template.category
    return render(
        request,
        "request_forms/template_detail.html",
        {
            "page_title": template.name,
            "template": template,
            "fields": list(fields.order_by("position", "pk")),
            "show": show,
            "inactive_count": inactive,
            "category_usable": category.is_active
            and category.parent is not None
            and category.parent.is_active,
            "in_portal": template.offered_in_portal and category.client_may_raise,
            "request_count": template.filled_forms.count(),
            "history": _history(template),
        },
    )


@login_required
def template_edit(request, pk):
    require_setup(request.user)
    template = get_object_or_404(RequestTemplate, pk=pk)
    name = template.name
    detail_url = reverse("request_forms:template-detail", args=[template.pk])
    form = TemplateEditForm(
        request.POST or None, instance=template, initial={"expected_revision": template.revision}
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_template(actor=request.user, template_id=template.pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Changes recorded with their reason.")
            return redirect(detail_url)
    return _form_page(
        request,
        form,
        title=f"Edit {name}",
        submit_label="Save changes",
        crumbs=[(LIST_TITLE, _list_url()), (name, detail_url), ("Edit", None)],
        cancel_url=detail_url,
        obj=template,
        notice="This form is inactive: it is not offered for new tickets. Tickets raised "
        "with it keep their values.",
    )


@login_required
@require_POST
def template_move(request, pk, direction):
    require_setup(request.user)
    template = get_object_or_404(RequestTemplate, pk=pk)
    try:
        move_template(actor=request.user, template_id=template.pk, direction=direction)
    except ValidationError as exc:
        messages.info(request, " ".join(exc.messages))
    query = {"show": "all"} if request.POST.get("show") == "all" else None
    return redirect(reverse("request_forms:template-list", query=query, fragment=f"move-form-{pk}"))


@login_required
def template_copy(request, pk):
    require_setup(request.user)
    template = get_object_or_404(RequestTemplate, pk=pk)
    detail_url = reverse("request_forms:template-detail", args=[template.pk])
    form = CopyForm(request.POST or None, initial={"name": f"{template.name} (copy)"})
    if request.method == "POST" and form.is_valid():
        try:
            copy = clone_template(actor=request.user, template_id=template.pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, f"{copy.name} created from {template.name}.")
            return redirect("request_forms:template-detail", pk=copy.pk)
    return _form_page(
        request,
        form,
        title=f"Copy {template.name}",
        submit_label="Copy form",
        crumbs=[(LIST_TITLE, _list_url()), (template.name, detail_url), ("Copy", None)],
        cancel_url=detail_url,
    )


@login_required
def template_preview(request, pk):
    """The form as a client sees it in the portal, filled with nothing and sent nowhere."""
    require_setup(request.user)
    template = get_object_or_404(RequestTemplate.objects.select_related("category"), pk=pk)
    detail_url = reverse("request_forms:template-detail", args=[template.pk])
    preview = _PreviewUser()
    return render(
        request,
        "request_forms/template_preview.html",
        {
            "page_title": f"Preview {template.name}",
            "template": template,
            "values_form": ValuesForm(template, user=preview),
            "crumbs": [(LIST_TITLE, _list_url()), (template.name, detail_url), ("Preview", None)],
            "detail_url": detail_url,
        },
    )


class _PreviewUser:
    """Stands in for a portal account so the preview shows exactly the client's fields. It is
    never saved and authorizes nothing."""

    is_authenticated = True
    is_active = True
    client_id = -1


# Fields --------------------------------------------------------------------------------------


@login_required
def field_create(request, pk):
    require_setup(request.user)
    template = get_object_or_404(RequestTemplate, pk=pk)
    detail_url = reverse("request_forms:template-detail", args=[template.pk])
    form = FieldForm(request.POST or None, initial={"audience": "client"})
    if request.method == "POST" and form.is_valid():
        try:
            field = create_field(actor=request.user, template_id=template.pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, f"Field {field.label} added.")
            return redirect(detail_url)
    return _form_page(
        request,
        form,
        title="Add field",
        submit_label="Add field",
        crumbs=[(LIST_TITLE, _list_url()), (template.name, detail_url), ("Add field", None)],
        cancel_url=detail_url,
    )


@login_required
def field_edit(request, pk):
    require_setup(request.user)
    field = get_object_or_404(TemplateField.objects.select_related("template"), pk=pk)
    template, label = field.template, field.label
    detail_url = reverse("request_forms:template-detail", args=[template.pk])
    form = FieldEditForm(
        request.POST or None, instance=field, initial={"expected_revision": field.revision}
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_field(actor=request.user, field_id=field.pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Changes recorded with their reason.")
            return redirect(detail_url)
    return _form_page(
        request,
        form,
        title=f"Edit {label}",
        submit_label="Save changes",
        crumbs=[(LIST_TITLE, _list_url()), (template.name, detail_url), (label, None)],
        cancel_url=detail_url,
        obj=field,
        notice="This field is inactive: it is not on the form. Tickets that hold a value keep it.",
    )


@login_required
@require_POST
def field_move(request, pk, direction):
    require_setup(request.user)
    field = get_object_or_404(TemplateField, pk=pk)
    try:
        move_field(actor=request.user, field_id=field.pk, direction=direction)
    except ValidationError as exc:
        messages.info(request, " ".join(exc.messages))
    return redirect("request_forms:template-detail", pk=field.template_id)


# Catalogs ------------------------------------------------------------------------------------


@login_required
def catalog_list(request):
    require_setup(request.user)
    catalogs = Catalog.objects.annotate(
        item_count=Count("items", filter=Q(items__is_active=True), distinct=True),
        field_count=Count("fields", filter=Q(fields__is_active=True), distinct=True),
    ).order_by("name", "pk")
    return render(
        request,
        "request_forms/catalog_list.html",
        {"page_title": CATALOGS_TITLE, "catalogs": list(catalogs)},
    )


@login_required
def catalog_create(request):
    require_setup(request.user)
    form = CatalogForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            catalog = create_catalog(actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, f"Catalog {catalog.name} added. Now add its items.")
            return redirect("request_forms:catalog-detail", pk=catalog.pk)
    return _form_page(
        request,
        form,
        title="New catalog",
        submit_label="Create catalog",
        crumbs=[(CATALOGS_TITLE, _catalogs_url()), ("New catalog", None)],
        cancel_url=_catalogs_url(),
    )


@login_required
def catalog_detail(request, pk):
    require_setup(request.user)
    catalog = get_object_or_404(Catalog, pk=pk)
    show = _show(request)
    items = catalog.items.all()
    inactive = items.filter(is_active=False).count()
    if show == "active":
        items = items.filter(is_active=True)
    return render(
        request,
        "request_forms/catalog_detail.html",
        {
            "page_title": catalog.name,
            "catalog": catalog,
            "items": list(items.order_by("position", "name", "pk")),
            "show": show,
            "inactive_count": inactive,
            "used_by": list(
                catalog.fields.filter(is_active=True)
                .select_related("template")
                .order_by("template__position", "template__name")
            ),
            "history": _history(catalog),
        },
    )


@login_required
def catalog_edit(request, pk):
    require_setup(request.user)
    catalog = get_object_or_404(Catalog, pk=pk)
    name = catalog.name
    detail_url = reverse("request_forms:catalog-detail", args=[catalog.pk])
    form = CatalogEditForm(
        request.POST or None, instance=catalog, initial={"expected_revision": catalog.revision}
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_catalog(actor=request.user, catalog_id=catalog.pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Changes recorded with their reason.")
            return redirect(detail_url)
    return _form_page(
        request,
        form,
        title=f"Edit {name}",
        submit_label="Save changes",
        crumbs=[(CATALOGS_TITLE, _catalogs_url()), (name, detail_url), ("Edit", None)],
        cancel_url=detail_url,
        obj=catalog,
        notice="This catalog is inactive: new item-list fields cannot use it.",
    )


@login_required
def item_create(request, pk):
    require_setup(request.user)
    catalog = get_object_or_404(Catalog, pk=pk)
    detail_url = reverse("request_forms:catalog-detail", args=[catalog.pk])
    form = ItemForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            item = create_item(actor=request.user, catalog_id=catalog.pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, f"Item {item.name} added.")
            return redirect(detail_url)
    return _form_page(
        request,
        form,
        title="Add item",
        submit_label="Add item",
        crumbs=[(CATALOGS_TITLE, _catalogs_url()), (catalog.name, detail_url), ("Add item", None)],
        cancel_url=detail_url,
    )


@login_required
def item_edit(request, pk):
    require_setup(request.user)
    item = get_object_or_404(CatalogItem.objects.select_related("catalog"), pk=pk)
    catalog, name = item.catalog, item.name
    detail_url = reverse("request_forms:catalog-detail", args=[catalog.pk])
    form = ItemEditForm(
        request.POST or None, instance=item, initial={"expected_revision": item.revision}
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_item(actor=request.user, item_id=item.pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Changes recorded with their reason.")
            return redirect(detail_url)
    return _form_page(
        request,
        form,
        title=f"Edit {name}",
        submit_label="Save changes",
        crumbs=[(CATALOGS_TITLE, _catalogs_url()), (catalog.name, detail_url), (name, None)],
        cancel_url=detail_url,
        obj=item,
        notice="This item is inactive. It stays on existing tickets. New orders cannot include it.",
    )


@login_required
@require_POST
def item_move(request, pk, direction):
    require_setup(request.user)
    item = get_object_or_404(CatalogItem, pk=pk)
    try:
        move_item(actor=request.user, item_id=item.pk, direction=direction)
    except ValidationError as exc:
        messages.info(request, " ".join(exc.messages))
    return redirect("request_forms:catalog-detail", pk=item.catalog_id)


# Presets -------------------------------------------------------------------------------------


@login_required
def preset_load(request):
    from .forms import FormPresetForm
    from .presets import FORM_PRESETS, load_form_presets

    require_setup(request.user)
    form = FormPresetForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        counts = load_form_presets(actor=request.user, keys=form.cleaned_data["presets"])
        added = counts["templates"] + counts["fields"] + counts["catalogs"] + counts["items"]
        if added:
            messages.success(
                request,
                f"Standard forms loaded: {counts['templates']} form"
                f"{'s' * (counts['templates'] != 1)}, {counts['fields']} field"
                f"{'s' * (counts['fields'] != 1)}, {counts['catalogs']} catalog"
                f"{'s' * (counts['catalogs'] != 1)} and {counts['items']} catalog item"
                f"{'s' * (counts['items'] != 1)} added.",
            )
        else:
            messages.info(request, "Nothing new: everything in these presets is already here.")
        if counts["skipped"]:
            messages.info(
                request,
                f"{counts['skipped']} form{'s were' if counts['skipped'] != 1 else ' was'} not "
                "loaded because its ticket category is not set up. Load the category presets "
                "first.",
            )
        return redirect(_list_url())
    from taxonomy.models import RequestCategory

    category_keys = set(
        RequestCategory.objects.filter(parent__isnull=False).values_list("key", flat=True)
    )
    loaded = set(RequestTemplate.objects.values_list("key", flat=True))
    presets = [
        {
            "key": key,
            "name": preset.name,
            "count": len(preset.templates),
            "missing": sum(t.key not in loaded for t in preset.templates),
            "no_category": sum(
                t.key not in loaded and t.category not in category_keys for t in preset.templates
            ),
            "templates": [t.client_label or t.name for t in preset.templates],
        }
        for key, preset in FORM_PRESETS.items()
    ]
    return render(
        request,
        "request_forms/preset_form.html",
        {
            "page_title": "Load standard forms",
            "form": form,
            "presets": presets,
            "crumbs": [(LIST_TITLE, _list_url()), ("Load standard forms", None)],
            "cancel_url": _list_url(),
        },
    )
