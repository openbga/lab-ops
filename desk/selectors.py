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


def locked_in_scope(scope, request_id):
    """Request `request_id`, locked FOR UPDATE until the transaction ends, if `scope` (a reader's
    staff_requests or portal_requests) holds it both before and after the lock; otherwise
    ServiceRequest.DoesNotExist, which pages answer as 404.

    The row is locked by its id alone and the scope is read again afterwards, in a new statement
    that sees what committed while the lock was awaited. Locking through the scope query would
    not do: a scope joined to other tables (the category, for staff without the supervisor role
    and for portal accounts) is rechecked by PostgreSQL, for a row it waited for, against the
    joined rows it read before waiting, so a ticket a colleague recategorized meanwhile would
    drop out as if it had left the reader's scope. The first reading answers a reader who may not
    open the ticket before any lock is taken.

    `scope` was built from the reader's account before the wait (for a supervisor it filters
    nothing), so the caller reads the account again right after this lock (desk.services.settle,
    settle_portal), before deciding anything about the ticket."""
    if not scope.filter(pk=request_id).exists():
        raise ServiceRequest.DoesNotExist("No such ticket.")
    locked = ServiceRequest.objects.select_for_update(of=("self",)).filter(pk=request_id).first()
    if locked is None or not scope.filter(pk=request_id).exists():
        raise ServiceRequest.DoesNotExist("No such ticket.")
    return locked


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
