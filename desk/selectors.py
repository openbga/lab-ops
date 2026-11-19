"""Reads of requests for staff. Every staff list, page, search, count, dashboard queue, timeline
and partial starts from staff_requests(user); a request outside it answers 404."""

from django.core.exceptions import PermissionDenied
from django.db.models import Exists, OuterRef, Q

from accounts.roles import ROLES, require_staff
from teams.models import TeamMembership

from .models import ServiceRequest
from .permissions import SUPERVISOR_ROLES, is_supervisor


def staff_requests(user, roles=None):
    """Requests this staff account may read. Raises PermissionDenied for anyone else.

    Every staff role reads every request except those in a restricted category, which need
    membership of the request's team or the supervisor role (admin implies it).

    `roles`, when given, are the account's roles as the caller read and holds them
    (accounts.roles.held_roles), and the decision takes them from that reading; otherwise they
    are read now.
    """
    if roles is None:
        require_staff(user)
        supervisor = is_supervisor(user)
    else:
        if not roles.intersection(ROLES):
            raise PermissionDenied("Your role does not permit this action.")
        supervisor = bool(roles.intersection(SUPERVISOR_ROLES))
    requests = ServiceRequest.objects.all()
    if supervisor:
        return requests
    member = TeamMembership.objects.filter(team=OuterRef("team"), user=user)
    return requests.filter(Q(category__is_restricted=False) | Exists(member))


def portal_audience(request):
    """The active portal accounts that can read `request` (desk.portal_scope decides). Empty when
    a reply would reach nobody."""
    from accounts.models import User

    from .portal_scope import portal_requests

    if request.client_id is None:
        return []
    accounts = User.objects.filter(client_id=request.client_id, is_active=True)
    return [
        account for account in accounts if portal_requests(account).filter(pk=request.pk).exists()
    ]
