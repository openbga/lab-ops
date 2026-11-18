"""Who works requests. Views and services both check these (docs/product.md "Roles and
capabilities")."""

from accounts.roles import has_role

# Every staff role reads and works non-restricted requests. Supervisors (and admin, which implies
# it) also read restricted ones, unmark complaints, withdraw replies and review complaints.
SUPERVISOR_ROLES = ("supervisor",)


def is_supervisor(user):
    return has_role(user, *SUPERVISOR_ROLES)
