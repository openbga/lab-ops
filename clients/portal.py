"""The client portal: the pages a client portal account sees.

Every query starts from the signed-in account's own client organization (`user.client_id`); a
record of another organization is indistinguishable from a missing one (404). Staff accounts are
refused (403). Feature apps add portal pages with `client_required` and the same scoping.
"""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import render

from core.lab_profile import logo_facts
from core.models import Lab

from .models import ClientContact, PortalAccess


def client_required(view):
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.user.client_id is None or not request.user.is_active:
            raise PermissionDenied("The client portal is for client accounts.")
        return view(request, *args, **kwargs)

    return wrapper


def client_scope(queryset, user, field="client_id"):
    """`queryset` narrowed to the account's own organization; never broadened."""
    return queryset.filter(**{field: user.client_id})


def current_access(user):
    """The account's effective portal grant, or None.

    None for an account that is not an active portal account, has no access record, holds
    access granted for another organization (it was moved: PortalAccess.applies_to), or holds an
    access that grants nothing (neither the whole organization nor any site, as after a reset).
    Every portal read that depends on scope (desk.portal_scope, portal_contacts) uses this, so
    "no grant" means the same everywhere: the account's own portal requests, no site choice, and
    only the contacts who serve the whole organization.
    """
    if not user.is_authenticated or user.client_id is None or not user.is_active:
        return None
    access = PortalAccess.objects.filter(user=user, client_id=user.client_id).first()
    if access is None or not access.applies_to(user):
        return None
    if not access.whole_organization and not access.sites.exists():
        return None
    return access


def portal_contacts(user):
    """Active contacts of the account's organization that it may see (requirement C4, decision of
    2026-11-17): all of them with a whole-organization grant; otherwise the contacts who serve the
    whole organization (no sites listed) and, with listed sites, those who serve one of them."""
    contacts = client_scope(ClientContact.objects.filter(is_active=True), user)
    access = current_access(user)
    if access is not None and access.whole_organization:
        return contacts
    serves = Q(sites__isnull=True)
    if access is not None:
        serves |= Q(sites__in=access.sites.values("pk"))
    return contacts.filter(serves).distinct()


@client_required
def home(request):
    from desk.portal_views import portal_queue

    client = request.user.client
    return render(
        request,
        "clients/portal/home.html",
        {
            **portal_queue(request.user),
            "page_title": client.name,
            "client": client,
            "contacts": portal_contacts(request.user).order_by("name", "pk"),
            "logo": logo_facts(Lab.objects.filter(pk=1).first()),
        },
    )
