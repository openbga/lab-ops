"""Server-rendered list pages: one filtered, sorted, paginated grid per call (core.grids)."""

from django.db.models import Q
from django.http import HttpResponseBadRequest, QueryDict
from django.shortcuts import render

from core.grids import GridRequest, active_filters


def return_list_params(request, form_class):
    """Only the list's filters, sorting, and pagination may travel back from a record."""
    params = QueryDict(request.GET.get("list", ""))
    allowed = {*form_class.base_fields, "page", "per_page", "sort"}
    return {key: value for key, value in params.items() if key in allowed}


def toolbar_context(form, chips, *, inline=(), more=(), placeholder=""):
    """Context for includes/grid_toolbar.html: fields by role, whether More filters is open."""
    data = form.data if form.is_bound else {}
    return {
        "inline": [form[name] for name in inline],
        "more": [form[name] for name in more],
        "more_open": bool(form.errors) or any(data.get(name) for name in more),
        "active": bool(chips) or bool(form.errors),
        "placeholder": placeholder,
    }


def list_response(
    request,
    *,
    queryset,
    columns,
    default_sort,
    form_class,
    search_fields,
    filter_fields,
    date_fields=(),
    template,
    title,
    inline=(),
    more=(),
    placeholder="",
    extra=None,
    refine=None,
):
    """Render `template` with one page of `queryset`, filtered by `form_class` from the query
    string. `filter_fields` maps a filter field to the ORM path it must equal; `date_fields` holds
    (prefix, path) pairs for `<prefix>_from` / `<prefix>_to`; `refine(queryset, cleaned_data)`
    applies any filter that is not a plain match. An invalid filter shows no rows, never all."""
    try:
        grid = GridRequest.parse(request.GET, columns, default_sort)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    data = request.GET.copy()
    data["q"] = grid.query
    form = form_class(data)
    if form.is_valid():
        cleaned = form.cleaned_data
        if cleaned["q"]:
            condition = Q()
            for field in search_fields:
                condition |= Q(**{f"{field}__icontains": cleaned["q"]})
            queryset = queryset.filter(condition)
        for param, field in filter_fields.items():
            if cleaned.get(param):
                queryset = queryset.filter(**{field: cleaned[param]})
        if cleaned.get("active") in {"yes", "no"}:
            queryset = queryset.filter(is_active=cleaned["active"] == "yes")
        for prefix, field in date_fields:
            for suffix, lookup in (("from", "gte"), ("to", "lte")):
                if value := cleaned.get(f"{prefix}_{suffix}"):
                    queryset = queryset.filter(**{f"{field}__date__{lookup}": value})
        if refine:
            queryset = refine(queryset, cleaned)
    else:
        queryset = queryset.none()
    chips = active_filters(request, form)
    return render(
        request,
        template,
        {
            "page_title": title,
            "page_obj": grid.page(queryset, columns),
            "filter_form": form,
            "q": grid.query,
            "sort": grid.sort,
            "per_page": grid.per_page,
            "active_filters": chips,
            **toolbar_context(form, chips, inline=inline, more=more, placeholder=placeholder),
            **(extra or {}),
        },
    )
