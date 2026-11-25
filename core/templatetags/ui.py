"""Shared page components (docs/ui-standards.md section 8): every one is a tag of this library.

  {% load ui %}
  {% status label="Active" tone="success" plain=True %}
  {% icon name="plus-lg" %}
  {% panel title="Projects" count=projects|length table=True %}
    {% fill actions %}<a class="btn btn-sm btn-outline-secondary" href="...">Add</a>{% endfill %}
    ...body...
  {% endpanel %}
  {% fact label="Phone" value=client.phone %}             one dt/dd row; without value= a block

COMPONENTS below is the one declaration: each tag's template under templates/includes/ (whose
header comment documents it), its keyword parameters, the page variables it is documented to read,
and whether it wraps a body. Every tag renders its template in a context of its own, built from:
  - its parameters: keyword arguments only; an unknown one is a template syntax error, and one the
    caller leaves out is empty ("", as a missing variable renders), never a page variable of the
    same name;
  - its documented reads (`reads`): page variables taken from the caller's context, each only when
    the caller does not pass it as a parameter (the list views put filter_form, q, sort ... in it);
  - request, user and csrf_token.
A block tag's body renders first, in the page's context. {% fill name %} ... {% endfill %},
written directly inside the tag (not inside an {% if %}), renders into the named part
(slots.<name>) instead of the body. The body and each part are stripped of surrounding
whitespace, so an empty body is falsy.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from django import template
from django.http import QueryDict
from django.template.base import token_kwargs
from django.utils.html import strip_tags
from django.utils.safestring import mark_safe
from django.utils.text import slugify

from core.grids import PageSizeForm, chosen_query

register = template.Library()

# The one mapping from a status tone (includes/status.html) to a Bootstrap alert variant and the
# label a notice opens with when the caller gives none.
NOTICE_TONES = {
    "neutral": ("secondary", "Note"),
    "info": ("info", "Note"),
    "progress": ("progress", "In progress"),
    "success": ("success", "Done"),
    "warning": ("warning", "Warning"),
    "danger": ("danger", "Error"),
    "hold": ("hold", "On hold"),
}

# Page variables every component may read.
PASS_THROUGH = ("request", "user", "csrf_token")


class FillNode(template.Node):
    def __init__(self, name, nodelist):
        self.name, self.nodelist = name, nodelist

    def render(self, context):
        # A component collects its direct {% fill %} children; one reached here sits inside
        # another tag ({% if %}, {% for %}) and would vanish silently.
        raise template.TemplateSyntaxError(
            f"{{% fill {self.name} %}} must sit directly inside its component tag; "
            "put conditions inside the fill"
        )


@register.tag
def fill(parser, token):
    bits = token.split_contents()
    if len(bits) != 2:
        raise template.TemplateSyntaxError(
            "{% fill %} takes one part name, e.g. {% fill actions %}"
        )
    nodelist = parser.parse(("endfill",))
    parser.delete_first_token()
    return FillNode(bits[1], nodelist)


@dataclass(frozen=True)
class Component:
    template: str
    params: tuple = ()
    reads: tuple = ()  # documented page variables, taken when not passed as a parameter
    body: str = ""  # "" no body; "block" always a block; "unless:<param>" a block unless given
    derive: Callable | None = None  # extra template values from (values, body, slots)
    # extra template values from (values, context), worked out before the body is drawn
    before: Callable | None = None
    caller_templates: tuple = ()  # parameters naming a template rendered in the page's context


class ComponentNode(template.Node):
    def __init__(self, name, spec, kwargs, nodelist):
        self.name, self.spec, self.kwargs, self.nodelist = name, spec, kwargs, nodelist

    def render(self, context):
        spec = self.spec
        values = dict.fromkeys(spec.params, "")
        for name in spec.reads:
            if name not in self.kwargs:
                values[name] = context.get(name, "")
        values.update({name: value.resolve(context) for name, value in self.kwargs.items()})
        engine = context.template.engine
        for name in spec.caller_templates:
            if values[name]:
                values[name] = mark_safe(engine.get_template(values[name]).render(context))
        if spec.before:
            values.update(spec.before(values, context))
        body, slots = "", {}
        if self.nodelist is not None:
            parts = []
            for node in self.nodelist:
                if isinstance(node, FillNode):
                    slots[node.name] = mark_safe(node.nodelist.render(context).strip())
                else:
                    parts.append(node.render_annotated(context))
            body = mark_safe("".join(parts).strip())
        if spec.body:  # a single component may have its own "body" parameter (empty_state)
            values["body"], values["slots"] = body, slots
        if spec.derive:
            values.update(spec.derive(values, body, slots, self.nodelist is not None))
        for name in PASS_THROUGH:
            values.setdefault(name, context.get(name, ""))
        return engine.get_template(spec.template).render(context.new(values))


def compile_component(name, spec):
    def compile_tag(parser, token):
        bits = token.split_contents()
        kwargs = token_kwargs(bits[1:], parser, support_legacy=False)
        if len(kwargs) != len(bits) - 1:
            raise template.TemplateSyntaxError(
                f"{{% {name} %}} takes keyword arguments only (name=value)"
            )
        unknown = set(kwargs) - set(spec.params)
        if unknown:
            raise template.TemplateSyntaxError(
                f"{{% {name} %}} has no parameter {', '.join(sorted(unknown))}"
            )
        nodelist = None
        unless = spec.body.removeprefix("unless:")
        if spec.body == "block" or (spec.body.startswith("unless:") and unless not in kwargs):
            nodelist = parser.parse((f"end{name}",))
            parser.delete_first_token()
        return ComponentNode(name, spec, kwargs, nodelist)

    return compile_tag


def form_page_kept(values, context):
    """keep=...: the form's part in keeping what was typed (accounts.kept.prepare), worked out
    before the fields are drawn, so that what is given back is drawn in them."""
    from accounts import kept

    if values["kept"]:
        return {"kept": values["kept"]}  # worked out by the view (a page that took its copy first)
    if not values["keep"] or values["method"] == "get" or values["local"]:
        return {"kept": None}
    key = (
        values["keep"]
        if values["keep_id"] in ("", None)
        else f"{values['keep']}-{values['keep_id']}"
    )
    return {"kept": kept.prepare(context.get("request"), values["form"], key)}


def panel_values(values, body, slots, has_body):
    title = values["title"] or strip_tags(slots.get("title", "")) or "panel"
    return {"heading_id": values["heading_id"] or f"{slugify(title)}-heading"}


def list_results_values(values, body, slots, has_body):
    items = values["items"]
    paginator = getattr(items, "paginator", None)
    has_rows = bool(paginator.count) if paginator is not None else bool(items)
    return {
        "has_rows": has_rows,
        "paginated": paginator is not None and values["paginate"] is not False,
    }


def notice_values(values, body, slots, has_body):
    tone = values["tone"] or "info"
    if tone not in NOTICE_TONES:
        raise template.TemplateSyntaxError(
            f"{{% notice %}} tone must be one of {', '.join(NOTICE_TONES)}"
        )
    variant, label = NOTICE_TONES[tone]
    return {"tone": tone, "variant": variant, "label": values["label"] or label}


def history_panel_values(values, body, slots, has_body):
    title = values["title"] or "History"
    events = values["events"] or []
    if values["newest_first"]:
        events = list(reversed(list(events)))
    return {
        "title": title,
        "events": events,
        "heading_id": values["heading_id"] or f"{slugify(title)}-heading",
    }


def due_pill_values(values, body, slots, has_body):
    from core.due import TONES, due_state

    soon = values["soon_days"]
    kind, label = due_state(
        values["due"] or None,
        today=values["today"] or None,
        soon_days=30 if soon == "" else int(soon),
    )
    return {"kind": kind, "label": label, "tone": TONES[kind]}


def form_actions_values(values, body, slots, has_body):
    return {"extra": body}


def sheet_values(values, body, slots, has_body):
    below = values["below"]
    if below not in ("", "sm"):
        raise template.TemplateSyntaxError('{% sheet %} below must be "sm" or empty')
    return {"below": below}


def grid_toolbar_values(values, body, slots, has_body):
    """The phone summary of the active filters, and their count (the badge on the button)."""
    chips = values["chips"] or values["active_filters"] or []
    parts = [f"{chip['label']}: {chip['value']}" for chip in chips]
    search = str(values["q"] or "").strip()
    errors = values["invalid"]
    if not hasattr(errors, "keys"):  # not the form's errors: read them from the form
        errors = getattr(values["filter_form"], "errors", None) or {}
    refused = "q" in errors  # a search the form refused filters nothing
    if search and not refused and not any(search == str(chip["value"]) for chip in chips):
        parts.append(f"Search: {search}")
    return {"filter_count": len(parts), "filter_summary": " · ".join(parts) or "No filters"}


def fact_values(values, body, slots, has_body):
    value = body if has_body else values["value"]
    blank = value is None or (isinstance(value, str) and not value.strip())
    return {"value": None if blank else value}


I = "includes/"  # noqa: E741
COMPONENTS = {
    # Wrapping components (block tags)
    "panel": Component(
        I + "panel.html",
        ("title", "heading_id", "count", "table", "flush", "hidden_title", "collapsible",
         "open", "class", "id", "body_class"),
        body="block",
        derive=panel_values,
    ),
    "panel_section": Component(I + "panel_section.html", ("rule", "class"), body="block"),
    "list_results": Component(
        I + "list_results.html",
        ("items", "noun", "filtered", "invalid", "clear_url", "paginate", "post_name",
         "page_parameter", "empty_title", "empty_body", "action_url", "action_label",
         "no_results_title", "no_results_body", "error_body"),
        body="block",
        derive=list_results_values,
    ),
    "notice": Component(
        I + "notice.html",
        ("tone", "label", "heading", "blocking", "dismissible", "focus", "hidden", "id",
         "class", "hook"),
        body="block",
        derive=notice_values,
    ),
    "form_page": Component(
        I + "form_page.html",
        ("form", "formset", "formset_label", "action", "multipart", "note", "times",
         "no_required_note", "cancel_url", "submit_label", "destructive", "secondary",
         "no_submit", "local", "title", "method", "id", "class", "keep", "keep_id", "kept"),
        body="block",
        before=form_page_kept,
    ),
    "form_actions": Component(
        I + "form_actions.html",
        ("cancel_url", "submit_label", "destructive", "secondary", "no_submit"),
        body="block",
        derive=form_actions_values,
    ),
    "history_panel": Component(
        I + "history_panel.html",
        ("events", "newest_first", "title", "heading_id", "count", "class", "empty_title",
         "empty_body"),
        body="block",
        reads=("form_dialog",),
        derive=history_panel_values,
    ),
    "page_meta": Component(I + "page_meta.html", body="block"),
    "more_actions": Component(I + "more_actions.html", ("label", "small", "id"), body="block"),
    "record_layout": Component(
        I + "record_layout.html", ("aside_label", "stacked_lead"), body="block"
    ),
    "sheet": Component(
        I + "sheet.html", ("id", "title", "below", "class"), body="block", derive=sheet_values
    ),
    "fact": Component(
        I + "fact.html", ("label", "value", "empty", "class", "wide"), body="unless:value",
        derive=fact_values,
    ),
    # Single components
    "icon": Component(I + "icon.html", ("name", "class")),
    "due_pill": Component(
        I + "due_pill.html", ("due", "soon_days", "today", "plain"), derive=due_pill_values
    ),
    "status": Component(I + "status.html", ("label", "tone", "plain", "title")),
    "brand": Component(I + "brand.html", ("href", "name", "variant")),
    "breadcrumbs": Component(
        I + "breadcrumbs.html",
        ("crumbs", "section", "section_url", "tab", "tab_url", "record", "record_url", "child",
         "child_url", "current"),
        reads=("page_title",),
    ),
    "confirm_dialog": Component(
        I + "confirm_dialog.html",
        ("id", "label", "object", "title", "body", "action", "hidden_name", "hidden_value",
         "confirm_hook", "cancel_hook", "cancel_label"),
    ),
    "empty_state": Component(
        I + "empty_state.html",
        ("kind", "title", "body", "action_url", "action_label", "clear_url", "invalid"),
        reads=("filter_form",),
    ),
    "filter_chips": Component(I + "filter_chips.html", ("chips",)),
    "form_errors": Component(I + "form_errors.html", ("form", "formset", "formset_label")),
    "form_sections": Component(I + "form_sections.html", ("form",)),
    "required_note": Component(I + "required_note.html", ("note", "times")),
    "field": Component(I + "field.html", ("field", "prefix", "suffix", "no_help", "label")),
    "grid_search": Component(
        I + "grid_search.html", ("value", "placeholder", "label", "name", "id", "errors")
    ),
    "grid_toolbar": Component(
        I + "grid_toolbar.html",
        ("label", "q", "placeholder", "search_id", "no_search", "inline", "more", "more_open",
         "active", "clear_url", "grid", "hidden", "invalid", "chips", "sort"),
        reads=("q", "placeholder", "inline", "more", "more_open", "active", "sort",
               "filter_form", "active_filters"),
        derive=grid_toolbar_values,
    ),
    "grid_columns_button": Component(I + "grid_columns_button.html", ("grid",)),
    "sort_header": Component(
        I + "sort_header.html",
        ("label", "asc", "desc", "first", "sort", "class", "col", "locked", "hidden", "priority"),
        reads=("sort",),
    ),
    "pagination": Component(
        I + "pagination.html",
        ("page_obj", "noun", "filtered", "post_name", "page_parameter", "fixed_size"),
        reads=("page_obj",),
    ),
    "select_all": Component(I + "select_all.html", ("name", "label", "ready_label")),
    "row_select": Component(
        I + "row_select.html",
        ("name", "value", "label", "form_id", "ready", "checked", "selected_values",
         "disabled", "id", "describedby"),
    ),
    "post_button": Component(
        I + "post_button.html",
        ("action", "label", "variant", "small", "fields", "name", "value", "icon", "id"),
    ),
    "history_item": Component(
        I + "history_item.html",
        ("title", "at", "actor", "by", "reason", "changes", "detail", "extra"),
        caller_templates=("extra",),
    ),
    "page_toc": Component(I + "page_toc.html", ("items", "label")),
    "record_foot": Component(I + "record_foot.html", ("identifier",)),
    "source_footer": Component(I + "source_footer.html", ("css",)),
}  # fmt: skip

for _name, _spec in COMPONENTS.items():
    register.tag(_name, compile_component(_name, _spec))


@register.simple_tag
def page_size_field(per_page, noun):
    """The rows-per-page select of includes/pagination.html, as a bound form field.

    {% page_size_field paginator.per_page noun as field %}{{ field }}  (id "per-page-<noun>")
    """
    form = PageSizeForm(initial={"per_page": per_page})
    widget = form.fields["per_page"].widget
    widget.attrs.update(
        {
            "id": f"per-page-{slugify(noun or 'records')}",
            "class": "form-select form-select-sm w-auto",
        }
    )
    return form["per_page"]


def _chosen_query(context):
    """The request's query parameters without those added from the person's saved settings
    (request.vr_saved_params, core.preferences): a link or form of a list re-sends only what the
    person chose on this page, so it never saves again a value another tab has changed since."""
    request = context.get("request")
    if not hasattr(request, "GET"):  # a component drawn without a request
        return QueryDict(mutable=True)
    return chosen_query(request)


@register.simple_tag(takes_context=True)
def page_size_params(context, page_parameter="page"):
    """The query parameters the rows-per-page form of includes/pagination.html carries: all the
    person chose but the page and the page size."""
    query = _chosen_query(context)
    left_out = {page_parameter or "page", "per_page"}
    return [
        (key, value) for key, values in query.lists() if key not in left_out for value in values
    ]


@register.simple_tag(takes_context=True)
def chosen_param(context, name):
    """One query parameter as the person chose it on this page ("" when it came from their saved
    settings or is absent): the hidden sort and page size of a list's filter form."""
    return _chosen_query(context).get(name, "")


@register.simple_tag(takes_context=True)
def grid_query(context, **changes):
    """A list link's query string: what the person chose on this page, with `changes` applied
    (None removes a parameter). For sort headings: what Django's querystring tag does, applied to
    what the person chose, without the values added from saved settings (the steps are repeated
    here so that a parameter named `query_dict` can be changed too; only `context`, which every
    context tag takes, Django's included, cannot)."""
    chosen = _chosen_query(context)
    query = chosen.copy()
    for name, value in changes.items():
        if value is None:
            query.pop(name, None)
        elif isinstance(value, Iterable) and not isinstance(value, str):
            query.setlist(name, value)  # QueryDict turns bytes into text, as for Django's tag
        else:
            query[name] = value
    if not query and not chosen:
        return ""
    return "?" + query.urlencode()


@register.simple_tag(takes_context=True)
def chosen_query_string(context):
    """The list's query string as the person chose it ("" when none): a record link's `list`
    parameter, which its breadcrumb sends back to the list."""
    return _chosen_query(context).urlencode()


@register.simple_tag(takes_context=True)
def saved_param_names(context):
    """The query parameters the list middleware added from the person's saved settings, which a
    list's forms leave out (bound hidden fields of the filter toolbar)."""
    request = context.get("request")
    return sorted(getattr(request, "vr_saved_params", ()))


@register.simple_tag(takes_context=True)
def pagination_query(context, page_number, page_parameter="page"):
    """Change one table's GET page while preserving the other table and filter parameters the
    person chose (not those added from saved settings)."""
    query = _chosen_query(context)
    query[page_parameter or "page"] = page_number
    return "?" + query.urlencode()


@register.filter
def password_fields(form):
    """For the error summary: "all" when every visible field of `form` is a password field drawn
    again empty after a refused save (a PasswordInput, or one derived from it, that does not
    render its value), "some" when some are, "" when none is, so the summary does not say those
    were kept. Password fields in a formset's rows are not looked at: no form has them."""
    from django.forms import PasswordInput

    fields = list(getattr(form, "visible_fields", lambda: [])())
    passwords = [
        field
        for field in fields
        if isinstance(field.field.widget, PasswordInput)
        and not getattr(field.field.widget, "render_value", False)
    ]
    if not passwords:
        return ""
    return "all" if len(passwords) == len(fields) else "some"


@register.filter
def action_label(code):
    """Readable history title for an audit action code: "client.updated" -> "Client updated"."""
    words = str(code or "").replace(".", " ").replace("_", " ").split()
    if words and words[0] == "request":
        words[0] = "ticket"
    return " ".join(words).capitalize()


@register.filter
def role_label(name):
    """Display name of a role group: "client_services" -> "Client services"."""
    from accounts.roles import ROLE_LABELS

    return ROLE_LABELS.get(name, str(name).replace("_", " ").capitalize())
