"""Pages for external systems (L1, admin) and record links (L2 to L5, staff).

Every read goes through `desk.selectors.staff_requests` for requests (another reader's restricted
request answers 404) and through the capability for person-level links. A person-level key never
enters a URL, a page title or a list cell: links are addressed by attachment id or link UUID, the
reveal and the person-key search are POST, and their responses are not cached.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from accounts.roles import require_staff
from clients.models import Client
from core.concurrency import protect_form
from core.ids import whole_number
from core.lists import list_response
from core.models import AuditEvent
from desk.selectors import staff_requests

from .forms import (
    LinkEditForm,
    LinkForm,
    PersonSearchForm,
    RemoveLinkForm,
    SystemEditForm,
    SystemFilter,
    SystemForm,
)
from .identifiers import url_on_system_host
from .models import ExternalRecordLink, ExternalSystem, LinkAttachment
from .services import (
    CLOSED_MESSAGE,
    add_link,
    can_see_person_links,
    create_system,
    person_link_readable,
    remove_link,
    require_setup,
    reveal_person_key,
    search_person_key,
    set_shown_to_client,
    update_link,
    update_system,
)

SYSTEMS_TITLE = "External systems"
SYSTEM_COLUMNS = {"name": "name", "kind": "kind", "base_url": "base_url", "is_active": "is_active"}
NO_STORE = "no-store, max-age=0"


def _errors(form, exc):
    if hasattr(exc, "message_dict"):
        for name, errors in exc.message_dict.items():
            form.add_error(name if name in form.fields else None, errors)
    else:
        form.add_error(None, exc)


def request_url(service_request):
    """URL of the staff ticket page."""
    return reverse("desk:request-detail", args=[service_request.pk])


def _readable_request(user, pk):
    try:
        return staff_requests(user).select_related("client").get(pk=pk)
    except ObjectDoesNotExist as error:
        raise Http404 from error


def _no_store(response):
    response["Cache-Control"] = NO_STORE
    response["Pragma"] = "no-cache"
    return response


# External systems (L1) ---------------------------------------------------------------------


@login_required
def system_list(request):
    require_setup(request.user)
    return list_response(
        request,
        queryset=ExternalSystem.objects.all(),
        columns=SYSTEM_COLUMNS,
        default_sort="name",
        form_class=SystemFilter,
        search_fields=("name", "key", "base_url"),
        filter_fields={},
        template="links/system_list.html",
        title=SYSTEMS_TITLE,
        placeholder="System name, key or address",
        inline=("active",),
    )


def _system_form(request, form, *, title, submit_label, crumbs, cancel_url, obj=None):
    return render(
        request,
        "core/record_form.html",
        {
            "page_title": title,
            "submit_label": submit_label,
            "form": form,
            "object": obj,
            "inactive_notice": "This system is inactive and takes no new links. Its existing "
            "links stay.",
            "crumbs": crumbs,
            "cancel_url": cancel_url,
        },
    )


@login_required
def system_create(request):
    require_setup(request.user)
    list_url = reverse("links:system-list")
    form = SystemForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            system = create_system(actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, f"{system.name} added.")
            return redirect(list_url)
    return _system_form(
        request,
        form,
        title="New external system",
        submit_label="Create system",
        crumbs=[(SYSTEMS_TITLE, list_url), ("New external system", None)],
        cancel_url=list_url,
    )


@login_required
def system_edit(request, pk):
    require_setup(request.user)
    system = get_object_or_404(ExternalSystem, pk=pk)
    name = system.name
    list_url = reverse("links:system-list")
    form = SystemEditForm(
        request.POST or None, instance=system, initial={"expected_revision": system.revision}
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_system(actor=request.user, system_id=system.pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Changes recorded with their reason.")
            return redirect(list_url)
    return _system_form(
        request,
        form,
        title=f"Edit {name}",
        submit_label="Save changes",
        crumbs=[(SYSTEMS_TITLE, list_url), (name, None), ("Edit", None)],
        cancel_url=list_url,
        obj=system,
    )


# Adding and removing links (L2, L3, L4) ----------------------------------------------------


def _add_page(request, form, *, title, crumbs, cancel_url):
    return render(
        request,
        "links/link_form.html",
        {
            "page_title": title,
            "form": form,
            "crumbs": crumbs,
            "cancel_url": cancel_url,
        },
    )


@login_required
def request_link_add(request, pk):
    require_staff(request.user)
    service_request = _readable_request(request.user, pk)
    back = request_url(service_request)
    if service_request.status == "closed":
        messages.info(request, CLOSED_MESSAGE)
        return redirect(back)
    allow_person = can_see_person_links(request.user)
    initial = {}
    if request.GET.get("from") == "reference" and service_request.client_reference:
        initial = {"identifier": service_request.client_reference}
    form = LinkForm(request.POST or None, initial=initial, allow_person=allow_person)
    if request.method == "POST" and form.is_valid():
        try:
            attachment, created = add_link(
                actor=request.user, request_id=service_request.pk, **form.cleaned_data
            )
        except ObjectDoesNotExist as error:  # left the actor's scope while the change waited
            raise Http404 from error
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(
                request,
                "Link added."
                if created
                else "Link added. The same record was already linked "
                "elsewhere, so the existing link was reused.",
            )
            return redirect(back)
    response = _add_page(
        request,
        form,
        title="Add link",
        crumbs=[("Tickets", None), (service_request.number, back), ("Add link", None)],
        cancel_url=back,
    )
    return _no_store(response) if allow_person else response


@login_required
def client_link_add(request, pk):
    require_staff(request.user)
    client = get_object_or_404(Client, pk=pk)
    back = reverse("clients:client-detail", args=[client.pk])
    form = LinkForm(request.POST or None, allow_person=False, for_request=False)
    if request.method == "POST" and form.is_valid():
        try:
            _attachment, created = add_link(
                actor=request.user, client_id=client.pk, **form.cleaned_data
            )
        except ObjectDoesNotExist as error:
            raise Http404 from error
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Link added." if created else "Link added (reused).")
            return redirect(back)
    return _add_page(
        request,
        form,
        title="Add link",
        crumbs=[
            ("Client organizations", reverse("clients:client-list")),
            (client.name, back),
            ("Add link", None),
        ],
        cancel_url=back,
    )


def _attachment_for(user, pk):
    """An attachment the user may act on: its request readable, a person link only with the
    capability. 404 otherwise, so a hidden record is indistinguishable from a missing one."""
    attachment = get_object_or_404(
        LinkAttachment.objects.select_related("link", "link__system", "request", "client"), pk=pk
    )
    if attachment.request_id is not None:
        _readable_request(user, attachment.request_id)
    if attachment.is_person and not can_see_person_links(user):
        raise Http404
    return attachment


def _target_url(attachment):
    if attachment.request_id is not None:
        return request_url(attachment.request)
    return reverse("clients:client-detail", args=[attachment.client_id])


@login_required
def attachment_remove(request, pk):
    require_staff(request.user)
    attachment = _attachment_for(request.user, pk)
    back = _target_url(attachment)
    if attachment.request_id is not None and attachment.request.status == "closed":
        messages.info(request, CLOSED_MESSAGE)
        return redirect(back)
    form = RemoveLinkForm(request.POST or None, person=attachment.is_person)
    # The link as this form shows it: shown to the client or not, current or removed. A removal
    # sent after someone else changed it is refused (links.services.remove_link).
    protect_form(form, attachment)
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        expected_state = data.pop("original_state")
        try:
            remove_link(
                actor=request.user,
                attachment_id=attachment.pk,
                expected_state=expected_state,
                **data,
            )
        except ObjectDoesNotExist as error:  # left the actor's scope while the change waited
            raise Http404 from error
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Link removed. Its history stays.")
            return redirect(back)
    target = attachment.request.number if attachment.request_id else attachment.client.name
    response = render(
        request,
        "links/link_remove.html",
        {
            "page_title": "Remove link",
            "form": form,
            "attachment": attachment,
            "what": (
                f"{attachment.link.get_record_type_display()} link"
                if attachment.is_person
                else attachment.link.display_label
            ),
            "crumbs": [(target, back), ("Remove link", None)],
            "cancel_url": back,
            "removed": attachment.removed_at is not None,
        },
    )
    return _no_store(response)


@login_required
@require_POST
def attachment_shown(request, pk):
    require_staff(request.user)
    attachment = _attachment_for(request.user, pk)
    choice = request.POST.get("shown")
    if choice not in ("0", "1"):  # only the form's two actions: anything else changes nothing
        messages.error(request, "Choose Show to client or Hide from client.")
        return redirect(f"{_target_url(attachment)}#links")
    shown = choice == "1"
    # The ticket's revision on the page the form was on; a form without one (drawn before it
    # carried one) is refused as stale, as a page drawn before a change is.
    expected = whole_number(request.POST.get("expected_revision", "")) or 0
    try:
        set_shown_to_client(
            actor=request.user, attachment_id=attachment.pk, shown=shown, expected_revision=expected
        )
    except ObjectDoesNotExist as error:  # left the actor's scope while the change waited
        raise Http404 from error
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "Shown to the client." if shown else "Hidden from the client.")
    return redirect(f"{_target_url(attachment)}#links")


@never_cache
@login_required
@require_POST
def attachment_reveal(request, pk):
    """The full key of a person-level link: an HTMX fragment, or a small page without JS."""
    require_staff(request.user)
    attachment = _attachment_for(request.user, pk)
    if not attachment.is_person:
        raise Http404
    try:
        link = reveal_person_key(actor=request.user, attachment_id=attachment.pk)
    except ObjectDoesNotExist as error:  # removed, or no longer readable
        raise Http404 from error
    context = {"link": link, "attachment": attachment, "back": _target_url(attachment)}
    template = (
        "links/_revealed_key.html" if request.headers.get("HX-Request") else "links/revealed.html"
    )
    if template == "links/revealed.html":
        context |= {
            "page_title": f"{link.get_record_type_display()} key",
            "crumbs": [(attachment.request.number, context["back"]), ("Key", None)],
        }
    return _no_store(render(request, template, context))


# A link's own page and its corrections -----------------------------------------------------


def _link_for(user, pk):
    """A link the user may open. A person-level link needs the capability and a carrier request
    the user can read now; otherwise it is indistinguishable from a
    missing one."""
    link = get_object_or_404(ExternalRecordLink.objects.select_related("system"), pk=pk)
    if link.is_person and not person_link_readable(user, link):
        raise Http404
    return link


@login_required
def link_detail(request, pk):
    require_staff(request.user)
    link = _link_for(request.user, pk)
    readable = staff_requests(request.user).values("pk")
    attachments = list(
        link.attachments.filter(Q(request__isnull=True) | Q(request__in=readable))
        .select_related("request", "client", "added_by", "removed_by")
        .order_by("added_at", "pk")
    )
    for attachment in attachments:
        attachment.target_url = _target_url(attachment)
    # History follows the carriers the reader may read: events of the
    # attachments listed above, and events of the link itself unless they name a request the
    # reader cannot read (a reveal on a restricted request).
    link_events = list(
        AuditEvent.objects.filter(
            object_type="links.externalrecordlink", object_id=str(link.pk)
        ).select_related("actor")
    )
    named = {(event.after or {}).get("request") for event in link_events} - {None}
    readable_ids = set(
        staff_requests(request.user).filter(pk__in=named).values_list("pk", flat=True)
    )
    link_events = [
        event
        for event in link_events
        if (event.after or {}).get("request") in (None, *readable_ids)
    ]
    attachment_events = AuditEvent.objects.filter(
        object_type="links.linkattachment",
        object_id__in=[str(attachment.pk) for attachment in attachments],
    ).select_related("actor")
    events = sorted([*link_events, *attachment_events], key=lambda event: event.occurred_at)
    response = render(
        request,
        "links/link_detail.html",
        {
            # Never the label or key of a person-level link in the title.
            "page_title": (
                f"{link.get_record_type_display()} link" if link.is_person else link.display_label
            ),
            "link": link,
            "attachments": attachments,
            "history": events,
            "url_on_host": url_on_system_host(link.url, link.system.base_url),
        },
    )
    return _no_store(response) if link.is_person else response


@login_required
def link_edit(request, pk):
    require_staff(request.user)
    link = _link_for(request.user, pk)
    record_url = reverse("link_records:link-detail", args=[link.pk])
    title = f"{link.get_record_type_display()} link" if link.is_person else link.display_label
    form = LinkEditForm(
        request.POST or None, instance=link, initial={"expected_revision": link.revision}
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_link(actor=request.user, link_id=link.pk, **form.cleaned_data)
        except ObjectDoesNotExist as error:  # no longer readable while the change waited
            raise Http404 from error
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Changes recorded with their reason.")
            return redirect(record_url)
    response = render(
        request,
        "core/record_form.html",
        {
            "page_title": f"Edit {title}",
            "submit_label": "Save changes",
            "form": form,
            "crumbs": [(title, record_url), ("Edit", None)],
            "cancel_url": record_url,
        },
    )
    return _no_store(response) if link.is_person else response


# Person-key search (L3) ------------------------------------------------------------------


@never_cache
@login_required
def person_search(request):
    """POST-only search by a person-level key. GET shows the empty form; the key is never put
    back into the page, so it stays out of the HTML, history and caches."""
    require_staff(request.user)
    allowed = can_see_person_links(request.user)
    if not allowed and request.method == "POST":
        raise PermissionDenied("This needs an explicit permission: Can view patient information.")
    results = None
    form = PersonSearchForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            ids = search_person_key(actor=request.user, key=form.cleaned_data["key"])
        except ValidationError as exc:
            _errors(form, exc)
        else:
            results = list(
                staff_requests(request.user)
                .filter(pk__in=ids)
                .select_related("client", "category")
                .order_by("-created_at")
            )
            for found in results:
                found.url = request_url(found)
            form = PersonSearchForm()
    return _no_store(
        render(
            request,
            "links/person_search.html",
            {
                "page_title": "Find by patient key",
                "allowed": allowed,
                "form": form if allowed else None,
                "results": results,
            },
        )
    )
