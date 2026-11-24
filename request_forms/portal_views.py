"""The client portal's request forms (F5): every form the laboratory offers, and raising a request
with one. Mounted at /portal/forms/. Portal accounts only (clients.portal.client_required refuses
staff with 403); a form not offered to this account answers 404. desk.services.create_request
checks the organization, site and category; request_forms.services the values."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.text import slugify
from django.views.decorators.cache import never_cache

from attachments.services import attach_to_new_request
from clients.portal import client_required
from core import access
from core.concurrency import one_reading
from core.models import AccessKind
from desk.models import EntryKind
from taxonomy.choices import Priority

from . import definition
from .forms import PortalRaiseForm
from .selectors import portal_templates, repeat_from
from .services import FORM_CHANGED, create_request_with_form, is_form_changed, offered_in_portal

# The request's own fields, kept when a changed form is drawn again (they mean the same whatever
# the form's definition); the form's answers are not.
KEPT_ON_CHANGE = ("subject", "description", "client_reference", "site", "urgent")

# More forms than this: the chooser groups them under their category groups' client labels.
GROUP_ABOVE = 7

NO_LONGER_OFFERED = "That form is no longer offered, so nothing was sent. Choose a form again."


def _groups(templates):
    """[{heading, forms, heading_id}] by category group: groups in the taxonomy's order, forms in
    the laboratory's order within each."""
    groups = {}
    for template in templates:
        group = template.category.parent
        groups.setdefault(group.pk, (group, []))[1].append(template)
    ordered = sorted(groups.values(), key=lambda pair: (pair[0].position, pair[0].name))
    return [
        {"heading": group.portal_label, "forms": forms, "heading_id": f"forms-group-{group.pk}"}
        for group, forms in ordered
    ]


@client_required
def choose(request):
    templates = list(portal_templates(request.user).select_related("category__parent"))
    return render(
        request,
        "request_forms/portal/choose.html",
        {
            "page_title": "New ticket",
            "templates": templates,
            "groups": _groups(templates) if len(templates) > GROUP_ABOVE else None,
        },
    )


def _common_errors(form, exc):
    """Errors of desk.services.create_request on the common fields; values errors by key."""
    if not hasattr(exc, "message_dict"):
        form.add_error(None, exc)
        return
    common = {"site", "subject", "client_reference", "priority", "first_entry_body", "category"}
    renamed = {"first_entry_body": "description", "priority": "urgent", "category": None}
    for name, errors in exc.message_dict.items():
        if name in common:
            target = renamed.get(name, name)
            form.add_error(target if target in form.fields else None, errors)
        else:
            form.add_value_errors(ValidationError({name: errors}))


def _not_offered(request):
    """A form this account is not offered: a page asked for answers 404, as for any other key; a
    submission (opened while offered, sent after it was retired or withdrawn) is told so and
    nothing is sent or shown, never under another form (reviews 3 and 7)."""
    if request.method == "POST":
        messages.warning(request, NO_LONGER_OFFERED)
        return redirect("portal_forms:choose")
    raise Http404("No such form.")


@never_cache  # a failed submission re-renders typed values, patient information included
@client_required
def raise_request(request, key):
    template = portal_templates(request.user).filter(key=key).first()
    if template is None:
        return _not_offered(request)
    # One read of the definition: the controls, the instructions, the token and "Request again"
    # all come from it, and it is checked for this account first: the list above was an earlier
    # read.
    defn = definition.read(template)
    if not offered_in_portal(defn, request.user):
        return _not_offered(request)
    again = (request.GET.get("again") or "").strip()
    copied, initial, repeated = None, {}, False
    if again and request.method != "POST":
        repeat = repeat_from(template, request.user, again, defn=defn)
        if repeat is None:
            raise Http404("No such ticket.")
        copied, site, earlier = repeat
        repeated = bool(copied) or site is not None
        if repeated:
            # What it copied is shown in the new form: the earlier ticket is read (core.access).
            access.note(request, AccessKind.REPEAT, ticket_id=earlier.pk)
        if site is not None:
            initial["site"] = site.pk
    form = PortalRaiseForm(
        template,
        request.POST or None,
        request.FILES or None,
        user=request.user,
        initial=initial,
        initial_values=copied,
        definition=defn,
    )
    changed = request.method == "POST" and form.definition_changed
    if request.method == "POST" and not changed and form.is_valid():
        data = form.cleaned_data
        try:
            # The request, its form values and its files commit together (attachments).
            with transaction.atomic():
                created = create_request_with_form(
                    actor=request.user,
                    template=template,
                    expected_definition=form.posted_definition,
                    values=form.values(),
                    subject=data["subject"],
                    first_entry_body=data["description"],
                    first_entry_kind=EntryKind.REPLY,
                    channel="portal",
                    site=data.get("site"),
                    client_reference=data["client_reference"],
                    priority=Priority.HIGH if data["urgent"] else None,
                )
                attach_to_new_request(request.user, created, data.get("files") or [])
        except ValidationError as exc:
            if is_form_changed(exc):
                changed = True
            else:
                _common_errors(form, exc)
        else:
            messages.success(
                request, f"Ticket {created.number} sent. It is now with the laboratory."
            )
            return redirect("portal:request-detail", number=created.number)
    if changed:
        # Changed or retired since it was drawn, whichever layer noticed: a fresh form from a new
        # reading, with none of the answers sent, once that reading shows the form is still
        # offered to this account; otherwise nothing of it (reviews 5, 6 and 7).
        fresh = definition.read(template)
        if not offered_in_portal(fresh, request.user):
            return _not_offered(request)
        messages.error(request, FORM_CHANGED)
        kept = {name: request.POST.getlist(name) for name in KEPT_ON_CHANGE if name in request.POST}
        initial = {name: value[0] if len(value) == 1 else value for name, value in kept.items()}
        # The refreshed definition has new questions, but the common ticket file picker still
        # needs to tell the user which uploads to reselect.
        initial["files"] = form["files"].value()
        form = PortalRaiseForm(
            template,
            user=request.user,
            initial=initial,
            definition=fresh,
        )
    patient_fields = [f for f in form.template_fields if f.holds_patient_information]
    # Drawn in one reading, so that typing given back into the form (accounts.kept) is taken only
    # if the page draws: a page that fails leaves it to the next one.
    with one_reading():
        return render(
            request,
            "request_forms/portal/raise.html",
            {
                "page_title": form.template.portal_label,
                "template": form.template,  # as the form's definition read it
                "form": form,
                "asks_patient_information": bool(patient_fields),
                # The notice names the earlier ticket only when something was copied from it (a
                # site deactivated while the page is drawn is still counted: docs/architecture.md).
                "again": again if repeated else "",
                # What was typed is kept for this form, and apart for it copying an earlier
                # ticket ("Request again", also when drawn again after a refused save).
                "keep_id": f"{form.template.pk}-again-{slugify(again)[:30]}"
                if again
                else form.template.pk,
            },
        )
