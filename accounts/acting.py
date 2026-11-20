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
  (accounts.models.User.save takes the gate below, then the row FOR NO KEY UPDATE);
- removing a role, a capability or a team membership (a deletion, and teams.services locks the
  membership FOR UPDATE first);
- changing portal access (clients.services locks the account FOR UPDATE first).

Additions made on their own wait for nothing: a capability added to a role, a role added to an
account in code, or a member added to a team only widens what the reading allows. The account
page and the portal access form take the gate on every save, widening included, so any save
there waits for the person's change under way. Paths that bypass save() (queryset.update, raw
SQL) are not covered.

FOR KEY SHARE rather than FOR SHARE: other updates of the account go on (a sign-in's time, the
email notice preference, an assignment's FOR NO KEY UPDATE of its assignee), so two people
assigning tickets to each other, or one person's two changes at once, never wait for each other
here. A full save of the account (its page in the administration, a password change) takes the
gate and waits for the person's change under way.

Row locks alone would let a change started while an access change waits overtake it: PostgreSQL
grants a share lock on a row at once when the row carries only share locks, whoever waits for a
stronger one. So each account also has an access gate, a transaction-level advisory lock taken
before anything else on either side. A change holds its account's gate shared (`hold_actor`); an
access change takes the gate of every account it affects exclusively (`close_access`), in id
order, before any row it locks. PostgreSQL queues advisory locks in order, so a change that
starts while an access change waits queues behind it, reads the account once it has committed,
and is refused. The account administration, the team page, the portal access form, the role
group page and every removal of a role, a capability, a team membership or a portal site
(the signals below) take the gate.

Held first, the account comes before every ticket. Ending a person's work locks the account (a
deactivation) or the membership (a removal from a team) and then that person's tickets. The
person's own change holds the same rows and then locks a ticket. The two never wait for each
other in a cycle: whichever comes second waits for the person's rows.
"""

import functools

from django.db import connection, transaction

from .roles import CAPABILITIES

# The advisory lock namespace of the access gates (PostgreSQL's two-key form, whose keys never
# overlap the one-key locks used elsewhere): the first key names the gates, the second the account.
ACCESS_GATE = 19553


INT4 = 2**31 - 1


def _gate(user_ids, *, shared):
    """The gates of these accounts, in key order. The second key is the account id, folded into
    PostgreSQL's integer range: an id beyond it shares a gate with a smaller one, which only makes
    the two wait for each other."""
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    keys = sorted({pk % INT4 for pk in user_ids if pk is not None})
    with connection.cursor() as cursor:
        for key in keys:
            cursor.execute(f"SELECT {function}(%s, %s)", [ACCESS_GATE, key])


def close_access(user_ids):
    """Before a change that may take access away from these accounts (deactivation, a role, a
    capability, a team membership, a portal site or portal access): take their access gates
    exclusively, in id order, until the transaction ends. It waits for every change of theirs
    under way (hold_actor), and a change of theirs that starts meanwhile waits for it. With several
    accounts the gates are taken one at a time: an account not reached yet can still start a
    change, which is then saved before this one. Each gate takes a slot of PostgreSQL's shared
    lock table until the transaction ends (sized from max_locks_per_transaction and the number of
    connections); a role of a few hundred members fits the defaults. Call it before any other
    lock the access change takes on those accounts' rows."""
    if not connection.in_atomic_block:
        raise RuntimeError("An access gate is held for a transaction; open one first.")
    _gate(user_ids, shared=False)


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


# On the thread's own connection: account pk -> (PostgreSQL transaction id, the savepoints open
# when it was held, the reading), for the holds still in force.
_HELD = "_acting_held"


def _transaction_id():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_current_xact_id()::text")
        return cursor.fetchone()[0]


def _still_held(pk, xid):
    """The reading of an earlier hold of `pk` in this transaction, or None: the same top-level
    transaction (its id is never reused), and every savepoint open at that hold still open, since
    rolling one back releases the locks taken inside it."""
    conn = transaction.get_connection()
    found = getattr(conn, _HELD, {}).get(pk)
    if found is None or found[0] != xid:
        return None
    savepoints = tuple(conn.savepoint_ids[: len(found[1])])
    return found[2] if savepoints == found[1] else None


def hold_actor(actor):
    """Hold the acting account's rows until the transaction ends (see the module docstring) and
    return the account as read under its lock, or None for an account that does not exist.

    Call it before anything else the change locks. Called again in the same transaction while
    the first hold is in force (an organization change inside an import), it returns the first
    reading and locks nothing: a membership added since is not held, and nothing waits for it.
    It decides nothing: the change's own checks, made after its locks (desk.services.settle and
    the like), read the account as it is held."""
    if not connection.in_atomic_block:
        raise RuntimeError("The acting account is held for a transaction; open one first.")
    pk = getattr(actor, "pk", None)
    if pk is None:
        return None
    xid = _transaction_id()
    earlier = _still_held(pk, xid)
    if earlier is not None:
        return earlier
    _gate([pk], shared=True)  # behind an access change already waiting, if any
    current = _locked_account(pk)
    if current is None:
        return None
    if current.client_id is None:
        _hold_staff_rows(pk)
    else:
        _hold_portal_rows(pk)
    conn = transaction.get_connection()
    held = {key: value for key, value in getattr(conn, _HELD, {}).items() if value[0] == xid}
    held[pk] = (xid, tuple(conn.savepoint_ids), current)
    setattr(conn, _HELD, held)
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


# Removals that take access away, whatever saves them ------------------------------------------


def role_members(group_ids):
    from .models import User

    return User.groups.through.objects.filter(group_id__in=group_ids).values_list(
        "user_id", flat=True
    )


def _on_roles_or_grants(sender, instance, action, reverse, model, pk_set, **kwargs):
    """A role (group membership) or a capability grant removed from accounts."""
    from django.contrib.auth.models import Group

    if action not in ("pre_remove", "pre_clear"):
        return
    if not reverse:  # account.groups / account.user_permissions
        close_access([instance.pk])
    elif isinstance(instance, Group):  # group.user_set
        users = pk_set if action == "pre_remove" else role_members([instance.pk])
        close_access(users)
    else:  # permission.user_set
        from .models import User

        users = (
            pk_set
            if action == "pre_remove"
            else User.user_permissions.through.objects.filter(
                permission_id=instance.pk
            ).values_list("user_id", flat=True)
        )
        close_access(users)


def _on_group_permissions(sender, instance, action, reverse, model, pk_set, **kwargs):
    """A capability taken from a role group (or a group from a capability): every member."""
    from django.contrib.auth.models import Group

    if action not in ("pre_remove", "pre_clear"):
        return
    if not reverse:  # group.permissions
        close_access(role_members([instance.pk]))
    else:  # permission.group_set
        groups = (
            pk_set
            if action == "pre_remove"
            else Group.permissions.through.objects.filter(permission_id=instance.pk).values_list(
                "group_id", flat=True
            )
        )
        close_access(role_members(list(groups)))


def _on_portal_sites(sender, instance, action, reverse, model, pk_set, **kwargs):
    """A site taken out of portal access."""
    from clients.models import PortalAccess

    if action not in ("pre_remove", "pre_clear"):
        return
    if not reverse:  # access.sites
        close_access([instance.user_id])
    else:  # site.portal_accesses
        accesses = PortalAccess.objects.all()
        accesses = (
            accesses.filter(pk__in=pk_set)
            if action == "pre_remove"
            else accesses.filter(sites=instance)
        )
        close_access(accesses.values_list("user_id", flat=True))


def _on_deleted_membership(sender, instance, **kwargs):
    """A team membership, a portal access or a role group deleted."""
    from django.contrib.auth.models import Group

    if isinstance(instance, Group):
        close_access(role_members([instance.pk]))
    else:
        close_access([instance.user_id])


def connect():
    from django.contrib.auth.models import Group
    from django.db.models.signals import m2m_changed, pre_delete

    from clients.models import PortalAccess
    from teams.models import TeamMembership

    from .models import User

    for through, receiver, uid in (
        (User.groups.through, _on_roles_or_grants, "acting-roles"),
        (User.user_permissions.through, _on_roles_or_grants, "acting-grants"),
        (Group.permissions.through, _on_group_permissions, "acting-group-grants"),
        (PortalAccess.sites.through, _on_portal_sites, "acting-portal-sites"),
    ):
        m2m_changed.connect(receiver, sender=through, dispatch_uid=uid)
    for model, uid in (
        (TeamMembership, "acting-team-membership"),
        (PortalAccess, "acting-portal-access"),
        (Group, "acting-role-group"),
    ):
        pre_delete.connect(_on_deleted_membership, sender=model, dispatch_uid=uid)
