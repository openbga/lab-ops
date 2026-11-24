"""Who may use the audit review workspace: an active staff account with a laboratory role that
holds the explicit capability "Can review access and audit records" (accounts.roles), directly or
through a group. No role implies it, the admin role and superuser status included, and a client
portal account never holds it. A grant to an account without a role opens nothing, as for the
patient-information capability."""

from accounts.roles import (
    REVIEW_CAPABILITY,
    ROLES,
    has_capability,
    has_role,
    require_capability,
    require_staff,
)


def may_review(user):
    return has_role(user, *ROLES) and has_capability(user, REVIEW_CAPABILITY)


def require_reviewer(user):
    require_staff(user)
    require_capability(user, REVIEW_CAPABILITY)
