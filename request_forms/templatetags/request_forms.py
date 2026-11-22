"""Template tags other apps' pages use to show request forms (the integration contract,
.local scope request-forms-contract.md):

  {% load request_forms %}
  {% portal_service_menu %}                    portal home: the service menu
  {% portal_new_request_url as url %}          portal home: the chooser, or the general form
  {% request_form_details service_request %}   staff and portal request pages: the form details
  {% request_form_fields values_form %}        a ValuesForm inside another page's form
  {% request_form_intake %}                    desk intake slot: form picker and the form's fields

Each reads the signed-in account from the context's request and checks scope again through
request_forms.selectors (staff_requests or portal_requests) before showing anything.
"""

from urllib.parse import quote

from django import template
from django.urls import reverse

from ..forms import NOT_KNOWN
from ..selectors import (
    client_values,
    is_portal_account,
    portal_menu,
    portal_templates,
    staff_values,
)

register = template.Library()


def _user(context):
    request = context.get("request")
    return getattr(request, "user", None)


@register.simple_tag(takes_context=True)
def portal_new_request_url(context):
    """Where "New request" goes: the forms chooser, or the general form when this account is
    offered no request forms (nothing to choose)."""
    user = _user(context)
    if user is not None and portal_templates(user).exists():
        return reverse("portal_forms:choose")
    return reverse("portal:request-create")


@register.inclusion_tag("request_forms/_portal_menu.html", takes_context=True)
def portal_service_menu(context):
    user = _user(context)
    templates = list(portal_menu(user)) if user is not None else []
    offered = portal_templates(user).count() if user is not None else 0
    return {
        "templates": templates,
        "has_more": offered > len(templates),
        "choose_url": reverse("portal_forms:choose"),
        "other_url": reverse("portal:request-create"),
    }


@register.inclusion_tag("request_forms/_details.html", takes_context=True)
def request_form_details(context, service_request, embedded=False):
    user = _user(context)
    if user is None or not user.is_authenticated or service_request is None:
        return {"details": None}
    staff = not is_portal_account(user)
    details = (staff_values if staff else client_values)(service_request, user)
    again_url = ""
    if details is not None and not staff:
        key = (
            portal_templates(user)
            .filter(filled_forms__request=service_request)
            .values_list("key", flat=True)
            .first()
        )
        if key:
            again_url = (
                f"{reverse('portal_forms:raise', args=[key])}?again={quote(service_request.number)}"
            )
    return {
        "details": details,
        "staff": staff,
        "again_url": again_url,
        "embedded": embedded,
        "edit_url": reverse("request_details:edit", args=[service_request.number]) if staff else "",
    }


@register.inclusion_tag("request_forms/_fields.html")
def request_form_fields(values_form):
    not_known = values_form[NOT_KNOWN] if NOT_KNOWN in values_form.fields else None
    return {"form": values_form, "template": values_form.template, "not_known": not_known}


@register.inclusion_tag("request_forms/_intake.html", takes_context=True)
def request_form_intake(context):
    """The desk's new-request slot: the form picker for the chosen category and the chosen
    form's fields (request_forms.intake.slot). The page's view may have drawn them already
    (`intake_drawn`, (picker, values_form)), so that what it keeps across a sign-out names exactly
    what is drawn; otherwise after a refused submit the view's Intake (`intake_values`) is shown as
    posted, with its errors, else the form is unbound, its choice read from the picker's own
    request, and only the forms offered to the organization it names.

    Everything the slot says about the chosen form (its instructions, fields and token) comes
    from the one reading its ValuesForm signs, never from the picker's list, which was queried
    before it; a form that reading shows retired, moved or withdrawn is not drawn."""
    from ..intake import slot

    drawn = context.get("intake_drawn")
    if drawn is not None:
        picker, values_form = drawn
    else:
        request = context.get("request")
        query = request.GET if request is not None and request.method == "GET" else {}
        picker, values_form = slot(
            context.get("category"),
            _user(context),
            intake=context.get("intake_values"),
            query=query,
        )
    if picker is None:
        return {"picker": None}
    template = values_form.template if values_form is not None else None
    not_known = None
    if values_form is not None and NOT_KNOWN in values_form.fields:
        not_known = values_form[NOT_KNOWN]
    return {
        "picker": picker,
        "template": template,
        "form": values_form,
        "not_known": not_known,
    }
