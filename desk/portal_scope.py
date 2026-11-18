"""Reads of requests for client portal accounts (docs/architecture.md "Authorization").

Every portal list, page, count, reply and notification recipient check goes through
`portal_requests`. It starts from the account's own organization (clients.portal.client_scope)
and narrows by its portal access (clients.models.PortalAccess):

- whole organization: every request of the organization;
- listed sites: the requests for those sites;
- always: the requests the account raised itself through the portal (raised_by);
- never: a request in a restricted category that the account did not raise.

An account without an effective grant (clients.portal.current_access: no access record, access
granted for another organization, or access that lists nothing), an inactive account and a staff
account see only what they raised in the portal (staff raise none there). A request outside the
result answers 404.
"""

from django.core.exceptions import ValidationError
from django.db.models import Q

from clients.models import ClientSite
from clients.portal import client_scope, current_access

from .models import Channel, ServiceRequest


def _access(user):
    """The account's effective grant (clients.portal.current_access), or None."""
    return current_access(user)


def raised_by(user):
    """Requests the account raised itself through the portal. Only these count as "its own":
    a request it logged in another capacity (staff intake by phone) never does, even if the
    account could change kind (it cannot: clients.signals and the account trigger)."""
    return Q(created_by=user, channel=Channel.PORTAL)


def portal_requests(user):
    """Requests this portal account may read. Out-of-scope requests answer 404."""
    if not user.is_authenticated or user.client_id is None or not user.is_active:
        return ServiceRequest.objects.none()
    requests = client_scope(ServiceRequest.objects.all(), user)
    raised = raised_by(user)
    access = _access(user)
    if access is None:
        return requests.filter(raised)
    restricted = Q(category__is_restricted=True) & ~raised
    if not access.whole_organization:  # (an empty Q() would vanish from an OR: filter apart)
        requests = requests.filter(Q(site__in=access.sites.values("pk")) | raised)
    return requests.exclude(restricted)


def portal_sites(user):
    """Active sites this account may raise a request for: the organization's with a
    whole-organization grant, its listed sites otherwise, and none without a grant (it raises
    for its organization, with no site)."""
    sites = client_scope(ClientSite.objects.filter(is_active=True), user)
    access = _access(user)
    if access is None:
        return sites.none()
    if not access.whole_organization:
        sites = sites.filter(pk__in=access.sites.values("pk"))
    return sites.order_by("name", "pk")


def site_required(user):
    """Whether a raise form must ask for a site: the account is narrowed to sites, or it sees
    the whole organization and that has more than one active site. Never without a grant."""
    access = _access(user)
    if access is None:
        return False
    if not access.whole_organization:
        return True
    organization_sites = client_scope(ClientSite.objects.filter(is_active=True), user)
    return organization_sites.count() > 1


def check_portal_site(user, site):
    """Refuse a site the portal account may not raise a request for, and a missing site where
    one is required."""
    if site is None:
        if site_required(user):
            raise ValidationError({"site": "Choose the site this ticket is about."})
        return
    if not portal_sites(user).filter(pk=site.pk).exists():
        if _access(user) is None:
            raise ValidationError(
                {"site": "Your account is not linked to any site yet; raise it without a site."}
            )
        raise ValidationError({"site": "Choose one of your sites."})
