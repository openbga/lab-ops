"""The acting account, held from the start of a change until it commits.

A change to a ticket or to what belongs to one (its entries, files, links and form values), a
nonconformance and an import of external identifiers decide who may act from the acting
account: active, a staff role or, for a client portal account, the sites it is granted, a
capability, a team membership. Reading the account again after the change's locks is not
enough on its own. PostgreSQL checks the rows a change names (its author, its creator, the
audit event's actor, the organization) at COMMIT, because Django declares foreign keys
DEFERRABLE INITIALLY DEFERRED, and that check can wait for another change. An account changed
while it waits would not be seen.

`hold_actor` therefore holds the rows those decisions rest on FOR KEY SHARE, before the
change's first other lock and until it commits:

- the account;
- for staff, its role memberships and their groups, its grants of a capability (its own and
  its roles') and its team memberships;
- for a client portal account, its portal access and the sites it grants.

Whatever takes access away waits for the change, and a change that starts after it reads the new
state and is refused:

- a save that deactivates the account or moves a portal account to another organization
  (accounts.models.User.save locks the row FOR UPDATE first);
- removing a role, a capability or a team membership (a deletion, and teams.services locks the
  membership FOR UPDATE first);
- changing portal access (clients.services locks the account FOR UPDATE first).

Additions wait for nothing: a role or a site granted meanwhile only widens what the reading
allows. Paths that bypass save() (queryset.update, raw SQL) are not covered.

FOR KEY SHARE rather than FOR SHARE: other updates of the account go on (a sign-in's time, an
assignment's FOR NO KEY UPDATE of its assignee), so two people assigning tickets to each other,
or one person's two changes at once, never wait for each other here.

Held first, the account comes before every ticket. Ending a person's work locks the account (a
deactivation) or the membership (a removal from a team) and then that person's tickets. The
person's own change holds the same rows and then locks a ticket. The two never wait for each
other in a cycle: whichever comes second waits for the person's rows.
"""

import functools

from django.db import connection, transaction

from .roles import CAPABILITIES


def _rows(sql, params):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return [row[0] for row in cursor.fetchall()]


def _locked_account(pk):
    """The account row read and held FOR KEY SHARE in one statement, or None."""
    from .models import User

    quote = connection.ops.quote_name
    sql = (
        f"SELECT * FROM {quote(User._meta.db_table)} "
        f"WHERE {quote(User._meta.pk.column)} = %s FOR KEY SHARE"
    )
    return next(iter(User.objects.raw(sql, [pk])), None)


def _hold_staff_rows(pk):
    from django.contrib.auth.models import Group, Permission
    from django.contrib.contenttypes.models import ContentType

    from jobs.locks import hold_group_names
    from teams.models import TeamMembership

    from .models import User

    quote = connection.ops.quote_name
    hold_group_names(pk, mode="key share")  # role memberships, then their groups
    # Grants of a capability: the account's own, then its roles'.
    granted = User.user_permissions.through._meta
    by_role = Group.permissions.through._meta
    in_groups = User.groups.through._meta
    capability = (
        f"SELECT p.{quote(Permission._meta.pk.column)} "
        f"FROM {quote(Permission._meta.db_table)} AS p "
        f"JOIN {quote(ContentType._meta.db_table)} AS ct "
        f"ON ct.{quote(ContentType._meta.pk.column)} "
        f"= p.{quote(Permission._meta.get_field('content_type').column)} "
        f"WHERE ct.{quote(ContentType._meta.get_field('app_label').column)} = 'accounts' "
        f"AND p.{quote(Permission._meta.get_field('codename').column)} = ANY(%s)"
    )
    codenames = list(CAPABILITIES)
    _rows(
        f"SELECT g.{quote(granted.pk.column)} FROM {quote(granted.db_table)} AS g "
        f"WHERE g.{quote(granted.get_field('user').column)} = %s "
        f"AND g.{quote(granted.get_field('permission').column)} IN ({capability}) "
        f"ORDER BY g.{quote(granted.pk.column)} FOR KEY SHARE OF g",
        [pk, codenames],
    )
    _rows(
        f"SELECT r.{quote(by_role.pk.column)} FROM {quote(by_role.db_table)} AS r "
        f"JOIN {quote(in_groups.db_table)} AS m "
        f"ON m.{quote(in_groups.get_field('group').column)} "
        f"= r.{quote(by_role.get_field('group').column)} "
        f"WHERE m.{quote(in_groups.get_field('user').column)} = %s "
        f"AND r.{quote(by_role.get_field('permission').column)} IN ({capability}) "
        f"ORDER BY r.{quote(by_role.pk.column)} FOR KEY SHARE OF r",
        [pk, codenames],
    )
    memberships = TeamMembership._meta
    _rows(
        f"SELECT {quote(memberships.pk.column)} FROM {quote(memberships.db_table)} "
        f"WHERE {quote(memberships.get_field('user').column)} = %s "
        f"ORDER BY {quote(memberships.pk.column)} FOR KEY SHARE",
        [pk],
    )


def _hold_portal_rows(pk):
    from clients.models import PortalAccess

    quote = connection.ops.quote_name
    access = PortalAccess._meta
    sites = PortalAccess.sites.through._meta
    _rows(
        f"SELECT {quote(access.pk.column)} FROM {quote(access.db_table)} "
        f"WHERE {quote(access.get_field('user').column)} = %s "
        f"ORDER BY {quote(access.pk.column)} FOR KEY SHARE",
        [pk],
    )
    _rows(
        f"SELECT s.{quote(sites.pk.column)} FROM {quote(sites.db_table)} AS s "
        f"JOIN {quote(access.db_table)} AS a "
        f"ON a.{quote(access.pk.column)} = s.{quote(sites.get_field('portalaccess').column)} "
        f"WHERE a.{quote(access.get_field('user').column)} = %s "
        f"ORDER BY s.{quote(sites.pk.column)} FOR KEY SHARE OF s",
        [pk],
    )


def hold_actor(actor):
    """Hold the acting account's rows until the transaction ends (see the module docstring) and
    return the account as read under its lock, or None for an account that does not exist.

    Call it before anything else the change locks. Calling it again in the same transaction
    holds nothing new and does not wait. It decides nothing: the change's own checks, made
    after its locks (desk.services.settle and the like), read the account as it is held."""
    if not connection.in_atomic_block:
        raise RuntimeError("The acting account is held for a transaction; open one first.")
    pk = getattr(actor, "pk", None)
    if pk is None:
        return None
    current = _locked_account(pk)
    if current is None:
        return None
    if current.client_id is None:
        _hold_staff_rows(pk)
    else:
        _hold_portal_rows(pk)
    return current


def acting_change(service):
    """For a service that takes the keyword argument `actor`: run it in one transaction
    (transaction.atomic) that holds the acting account (hold_actor) before anything else."""

    @functools.wraps(service)
    def wrapper(*args, **kwargs):
        with transaction.atomic():
            hold_actor(kwargs.get("actor"))
            return service(*args, **kwargs)

    wrapper.holds_actor = True
    return wrapper
