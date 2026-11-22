"""{% kept_typing form key record %}: a form's part in keeping what was typed (accounts.kept).

Rendered by includes/form_page.html inside a form drawn with keep=... ({% form_page %}): the
hidden token naming the fields that may be kept and, on a page opened with GET, what this person
had typed in this form when an earlier session of theirs ended by a limit, for
static/js/kept-typing.js to put back. Nothing for a visitor or a form with nothing to keep."""

import json

from django import forms, template
from django.utils.text import slugify

from .. import kept

register = template.Library()


@register.inclusion_tag("includes/kept_typing.html", takes_context=True)
def kept_typing(context, form, key, record=""):
    request = context.get("request")
    if request is None or not form or not key:
        return {"token": ""}
    key = f"{key}-{record}" if record not in ("", None) else str(key)
    names, typed = kept.keepable(form)
    value = kept.token(request, key, names, typed)
    if not value:
        return {"token": ""}
    offered = None if form.is_bound else kept.offer(request, key, names)
    return {
        "token": value,
        "field": kept.FIELD,
        "names": json.dumps(names),
        "values": offered[0] if offered else None,
        "ended_at": offered[1] if offered else None,
        "script_id": f"vr-kept-{slugify(key)}",
        "has_files": any(
            isinstance(bound.field.widget, forms.FileInput) for bound in form.visible_fields()
        ),
    }
