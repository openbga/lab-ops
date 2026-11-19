"""Who may do what: staff roles (the role groups) and explicit capabilities.

Roles are Django groups with fixed names, created by `setup_lab`. A client portal account
(`user.client_id` set) holds no role and no capability. Superuser status alone grants no role and
no capability: it opens the admin console, which is deployment-owner work.

Capabilities are explicit permissions on accounts.User, granted to a person or to a group in the
admin console. No role implies one, the admin role included, and no group receives one at setup.
"""

from django.core.exceptions import PermissionDenied

ROLES = ("admin", "supervisor", "client_services", "account_manager", "technical")
# The admin role also does the work of these roles (it manages the laboratory's setup too).
OPERATIONAL_ROLES = frozenset({"supervisor", "client_services", "account_manager", "technical"})
ROLE_LABELS = {
    "admin": "Administrator",
    "supervisor": "Supervisor",
    "client_services": "Client services",
    "account_manager": "Account manager",
    "technical": "Technical staff",
}
# codename: label. Each is a permission declared in accounts.User.Meta.permissions.
CAPABILITIES = {
    "view_patient_information": "Can view patient information",
}


def effective_roles(user):
    """Internal work roles, including admin's operational access; no membership changes.

    Superuser status alone grants no application role.
    """
    if not user.is_authenticated or not user.is_active or user.client_id is not None:
        return set()
    return expand_roles(user.groups.values_list("name", flat=True))


def expand_roles(names):
    """The work roles that these role group names give: admin also does the operational roles'
    work."""
    roles = set(names)
    if "admin" in roles:
        roles.update(OPERATIONAL_ROLES)
    return roles


def held_roles(user, *, nowait=False, mode="share"):
    """effective_roles of `user` (an instance the caller read as it stands now), read and held in
    one statement until the transaction ends (jobs.locks.hold_group_names): removing one of these
    roles waits for the transaction, and a role granted after the reading does not count. A
    decision that must hold the roles it rests on decides from this reading alone. `mode` is how
    the memberships are held (jobs.locks.share_lock)."""
    from jobs.locks import hold_group_names

    if not user.is_authenticated or not user.is_active or user.client_id is not None:
        return set()
    return expand_roles(hold_group_names(user.pk, nowait=nowait, mode=mode))


def has_role(user, *roles):
    return bool(effective_roles(user).intersection(roles))


def require_role(user, *roles):
    if not has_role(user, *roles):
        raise PermissionDenied("Your role does not permit this action.")


def require_staff(user):
    require_role(user, *ROLES)


def has_capability(user, codename):
    """An explicit grant, to the account or one of its groups. Superuser status is not enough."""
    if codename not in CAPABILITIES:
        raise ValueError(f"Unknown capability {codename!r}.")
    if not user.is_authenticated or not user.is_active or user.client_id is not None:
        return False
    permission = {"codename": codename, "content_type__app_label": "accounts"}
    return (
        user.user_permissions.filter(**permission).exists()
        or user.groups.filter(
            permissions__codename=codename, permissions__content_type__app_label="accounts"
        ).exists()
    )


def require_capability(user, codename):
    if not has_capability(user, codename):
        raise PermissionDenied(f"This needs an explicit permission: {CAPABILITIES[codename]}.")
