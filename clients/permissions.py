"""Who works with client organizations. Views and services both check these."""

from accounts.roles import has_role, require_role

# Staff who create and change client organizations and their contacts. Every staff role reads them.
MANAGE_ROLES = ("account_manager", "client_services")


def can_manage_clients(user):
    return has_role(user, *MANAGE_ROLES)


def require_client_manager(user):
    require_role(user, *MANAGE_ROLES)
