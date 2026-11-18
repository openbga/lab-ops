"""Staff pages for client organizations: the list, the record page, and create/edit forms for
organizations and their contacts. Every staff role reads; MANAGE_ROLES change (clients.services
checks again)."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.text import Truncator

from accounts.models import User
from accounts.roles import require_staff
from core.grids import active_filters
from core.lists import list_response, return_list_params, toolbar_context
from core.models import AuditEvent

from .forms import (
    ClientEditForm,
    ClientFilter,
    ClientForm,
    ContactEditForm,
    ContactForm,
    PortalAccessForm,
    SiteEditForm,
    SiteForm,
    TimelineFilter,
)
from .models import Client, ClientContact, ClientSite, PortalAccess
from .permissions import require_client_manager
from .services import (
    add_contact,
    create_client,
    create_site,
    set_portal_access,
    update_client,
    update_contact,
    update_site,
)
from .timeline import STATUS_LABELS, timeline_page

CLIENT_COLUMNS = {
    name: name
    for name in (
        "name",
        "account_number",
        "organization_type",
        "email",
        "phone",
        "city",
        "state",
        "is_active",
    )
}
INACTIVE_NOTICE = "This organization is inactive. Its records and history stay readable."
RECENT = 8


def _errors(form, exc):
    if hasattr(exc, "message_dict"):
        for name, errors in exc.message_dict.items():
            form.add_error(name if name in form.fields else None, errors)
    else:
        form.add_error(None, exc)


def _history(model_label, pk, limit=None):
    events = (
        AuditEvent.objects.filter(object_type=model_label, object_id=str(pk))
        .select_related("actor")
        .order_by("-occurred_at")
    )
    return events[:limit] if limit else events


def _refine(queryset, cleaned):
    if cleaned.get("contacts") == "none":
        queryset = queryset.exclude(contacts__is_active=True)
    return queryset


@login_required
def client_list(request):
    require_staff(request.user)
    return list_response(
        request,
        queryset=Client.objects.select_related("parent"),
        columns=CLIENT_COLUMNS,
        default_sort="name",
        form_class=ClientFilter,
        search_fields=("name", "account_number", "email", "phone", "city"),
        filter_fields={"state": "state", "organization_type": "organization_type"},
        template="clients/client_list.html",
        title="Client organizations",
        placeholder="Organization, account code, email, phone or city",
        inline=("active",),
        more=("organization_type", "state", "contacts"),
        refine=_refine,
    )


@login_required
def client_create(request):
    require_client_manager(request.user)
    form = ClientForm(request.POST or None)
    list_url = reverse("clients:client-list")
    if request.method == "POST" and form.is_valid():
        try:
            client = create_client(actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, f"{client.name} added.")
            return redirect("clients:client-detail", pk=client.pk)
    return render(
        request,
        "core/record_form.html",
        {
            "page_title": "New client organization",
            "submit_label": "Create organization",
            "form": form,
            "crumbs": [("Client organizations", list_url), ("New organization", None)],
            "cancel_url": list_url,
        },
    )


@login_required
def client_edit(request, pk):
    require_client_manager(request.user)
    client = get_object_or_404(Client, pk=pk)
    name = client.name  # a failed ModelForm validation copies the input onto the instance
    record_url = reverse("clients:client-detail", kwargs={"pk": client.pk})
    form = ClientEditForm(
        request.POST or None, instance=client, initial={"expected_revision": client.revision}
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_client(actor=request.user, client_id=client.pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Changes recorded with their reason.")
            return redirect(record_url)
    return render(
        request,
        "core/record_form.html",
        {
            "page_title": f"Edit {name}",
            "submit_label": "Save changes",
            "form": form,
            "object": client,
            "inactive_notice": INACTIVE_NOTICE,
            "crumbs": [
                ("Client organizations", reverse("clients:client-list")),
                (name, record_url),
                ("Edit", None),
            ],
            "cancel_url": record_url,
        },
    )


def _portal_accounts(client):
    """Portal accounts of the organization, each with `current_access`: its PortalAccess when
    granted for this organization, else None (not set, or left from another organization)."""
    accounts = list(
        User.objects.filter(client=client)
        .select_related("portal_access__contact")
        .prefetch_related("portal_access__sites")
        .order_by("-is_active", "email")
    )
    for account in accounts:
        access = getattr(account, "portal_access", None)
        account.current_access = access if access and access.applies_to(account) else None
    return accounts


def _toc(**counts):
    """On-this-page anchors of the organization page, in page order, with counts."""
    items = [("organization-heading", "Organization")]
    for anchor, label, count in (
        ("requests-heading", "Open tickets", counts["requests"]),
        ("sites-heading", "Sites", counts["sites"]),
        ("contacts-heading", "Contacts", counts["contacts"]),
        ("portal-heading", "Portal accounts", counts["portal"]),
    ):
        items.append((anchor, f"{label} ({count})"))
    items.append(("history-heading", "History"))
    return items


def staff_request_url(request_id):
    """URL of the staff ticket page."""
    return reverse("desk:request-detail", args=[request_id])


def new_request_url(client):
    """The staff intake form with the organization preselected."""
    return f"{reverse('desk:request-create')}?client={client.pk}"


def _requests_overview(user, client):
    """Open requests and counts by status for one organization, read through staff_requests so
    restricted categories stay hidden from staff who may not read them (C5)."""
    from desk.models import Status
    from desk.selectors import staff_requests

    requests = staff_requests(user).filter(client=client)
    counts = dict(requests.values("status").annotate(n=Count("pk")).values_list("status", "n"))
    open_statuses = (Status.OPEN, Status.WAITING_CLIENT, Status.WAITING_THIRD_PARTY)
    open_requests = list(
        requests.filter(status__in=open_statuses)
        .select_related("site", "team", "assignee", "category")
        .order_by("-updated_at", "-pk")[:RECENT]
    )
    for item in open_requests:
        item.staff_url = staff_request_url(item.pk)
    return {
        "status_counts": [(code, label, counts.get(code, 0)) for code, label in Status.choices],
        "open_count": sum(counts.get(code, 0) for code in open_statuses),
        "total_requests": sum(counts.values()),
        "open_requests": open_requests,
    }


@login_required
def client_detail(request, pk):
    require_staff(request.user)
    client = get_object_or_404(Client.objects.select_related("parent", "account_owner"), pk=pk)
    contacts = list(client.contacts.prefetch_related("sites"))
    portal_users = list(_portal_accounts(client))
    sites = list(client.sites.order_by("-is_active", "name", "pk"))
    overview = _requests_overview(request.user, client)
    return render(
        request,
        "clients/client_detail.html",
        {
            "page_title": client.name,
            "list_url": reverse(
                "clients:client-list", query=return_list_params(request, ClientFilter)
            ),
            "open_tickets_url": reverse(
                "desk:request-list", query={"view": "all", "status": "active", "client": client.pk}
            ),
            "client": client,
            "children": client.children.order_by("-is_active", "name"),
            "contacts": contacts,
            "sites": sites,
            "portal_users": portal_users,
            **overview,
            "new_request_url": new_request_url(client) if client.is_active else "",
            "can_invite": request.user.is_active and request.user.is_superuser,
            "inactive_notice": INACTIVE_NOTICE,
            "history": _history("clients.client", client.pk, RECENT),
            "toc": _toc(
                requests=overview["open_count"],
                sites=len(sites),
                contacts=len(contacts),
                portal=len(portal_users),
            ),
        },
    )


@login_required
def portal_access_edit(request, pk, user_pk):
    require_client_manager(request.user)
    client = get_object_or_404(Client, pk=pk)
    account = get_object_or_404(User, pk=user_pk, client=client)
    stored = PortalAccess.objects.filter(user=account).select_related("contact").first()
    # Access granted for another organization (the account was moved) shows as not set; saving
    # replaces it, with a reason because a record exists.
    access = stored if stored and stored.applies_to(account) else None
    granted = bool(access and (access.whole_organization or access.sites.exists()))
    initial = {
        "contact": access.contact_id if access else None,
        "scope": "sites"
        if access and not access.whole_organization and granted
        else "organization",
        "expected_revision": stored.revision if stored else 0,
    }
    form = PortalAccessForm(request.POST or None, account=account, access=stored, initial=initial)
    record_url = reverse("clients:client-detail", kwargs={"pk": client.pk})
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            set_portal_access(
                actor=request.user,
                user_id=account.pk,
                contact=data["contact"],
                whole_organization=data["whole_organization"],
                sites=data.get("sites") or (),
                expected_client_id=client.pk,  # the organization this form was decided for
                expected_revision=data.get("expected_revision"),
                reason=data.get("reason", ""),
            )
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Portal access saved.")
            return redirect(record_url)
    name = account.get_full_name() or account.get_username()
    return render(
        request,
        "clients/portal_access_form.html",
        {
            "page_title": f"Portal access of {name}",
            "client": client,
            "account": account,
            "access": access,
            "granted": granted,
            "form": form,
            "cancel_url": record_url,
            "history": _history("clients.portalaccess", stored.pk) if stored else [],
        },
    )


@login_required
def contact_edit(request, pk, contact_pk=None):
    require_client_manager(request.user)
    client = get_object_or_404(Client, pk=pk)
    contact = get_object_or_404(ClientContact, pk=contact_pk, client=client) if contact_pk else None
    if contact:
        form = ContactEditForm(
            request.POST or None,
            instance=contact,
            client=client,
            initial={"expected_revision": contact.revision},
        )
    else:
        form = ContactForm(request.POST or None, client=client)
    if request.method == "POST" and form.is_valid():
        try:
            if contact:
                update_contact(actor=request.user, contact_id=contact.pk, **form.cleaned_data)
            else:
                add_contact(actor=request.user, client_id=client.pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Contact saved.")
            return redirect("clients:client-detail", pk=client.pk)
    return render(
        request,
        "clients/contact_form.html",
        {
            "page_title": f"Edit {contact.name}" if contact else "Add contact",
            "client": client,
            "form": form,
            "contact": contact,
            "history": _history("clients.clientcontact", contact.pk) if contact else [],
        },
    )


@login_required
def site_edit(request, pk, site_pk=None):
    require_client_manager(request.user)
    client = get_object_or_404(Client, pk=pk)
    site = get_object_or_404(ClientSite, pk=site_pk, client=client) if site_pk else None
    record_url = reverse("clients:client-detail", kwargs={"pk": client.pk})
    if site:
        name = site.name
        form = SiteEditForm(
            request.POST or None, instance=site, initial={"expected_revision": site.revision}
        )
    else:
        form = SiteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            if site:
                update_site(actor=request.user, site_id=site.pk, **form.cleaned_data)
            else:
                create_site(actor=request.user, client_id=client.pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Site saved.")
            return redirect(record_url)
    return render(
        request,
        "clients/site_form.html",
        {
            "page_title": f"Edit {name}" if site else "Add site",
            "submit_label": "Save changes" if site else "Add site",
            "form": form,
            "object": site,
            "inactive_notice": "This site is inactive. Its tickets and history are kept.",
            "crumbs": [
                ("Client organizations", reverse("clients:client-list")),
                (client.name, record_url),
                (f"Edit {name}" if site else "Add site", None),
            ],
            "cancel_url": record_url,
            "history": _history("clients.clientsite", site.pk) if site else [],
        },
    )


ENTRY_HEADINGS = {
    "opened": "Ticket opened",
    "status": "Status changed",
    "reply": "Reply, visible to the client",
    "note": "Internal note",
    "contact": "Logged contact",
}


def _describe_entry(item):
    """Title, text and detail line of one request entry on the timeline (history_item)."""
    entry = item["entry"]
    request = entry.request
    title = ENTRY_HEADINGS[item["kind"]]
    if item["kind"] == "status":
        title += f": {STATUS_LABELS.get((entry.event_data or {}).get('new'), '')}"
    item["title"] = f"{title} · {request.number}"
    detail = [request.subject]
    if request.site_id:
        detail.append(request.site.name)
    if entry.kind == "contact":
        detail += [
            entry.get_contact_direction_display(),
            entry.get_contact_channel_display(),
            entry.get_contact_outcome_display(),
        ]
        if entry.contact_with:
            detail.append(entry.contact_with)
    if entry.withdrawn_at:
        detail.append("Withdrawn from client view")
    item["detail"] = " · ".join(part for part in detail if part)
    item["text"] = Truncator(entry.body).chars(280) if entry.body else ""
    item["request_url"] = staff_request_url(entry.request_id)


@login_required
def client_timeline(request, pk):
    require_staff(request.user)
    client = get_object_or_404(Client, pk=pk)
    form = TimelineFilter(request.GET, client=client)
    valid = form.is_valid()
    kind = form.cleaned_data["kind"] if valid else ""
    site = form.cleaned_data["site"] if valid else None
    page_obj = timeline_page(
        request.user,
        client,
        kinds=[kind] if kind else None,
        site=site,
        page=request.GET.get("page"),
    )
    if not valid:
        page_obj.object_list = []
    for item in page_obj.object_list:
        if "entry" in item:
            _describe_entry(item)
    chips = active_filters(request, form) if valid else []
    record_url = reverse("clients:client-detail", kwargs={"pk": client.pk})
    return render(
        request,
        "clients/client_timeline.html",
        {
            "page_title": f"Timeline of {client.name}",
            "client": client,
            "filter_form": form,
            "active_filters": chips,
            **toolbar_context(form, chips, inline=("kind", "site")),
            "page_obj": page_obj,
            "record_url": record_url,
        },
    )
