"""The laboratory's rule on who must have a second sign-in factor (an authenticator app, with its
recovery codes), and whether an account has one.

The rule names groups of accounts: every staff account, staff with chosen roles, staff who hold
the patient-information capability, accounts that can open the admin console, and client portal
accounts. Nothing is required until an administrator saves it. accounts.enrolment applies it at
sign-in: a session of an account the rule covers reaches only the pages that set up a second
factor while the account has none. A rule saved later applies from each account's next sign-in.

The groups follow the application's own access checks (accounts.roles): a role counts as
effective_roles counts it, so the admin role, which does the work of every other role, is covered
whenever any role is chosen; the capability counts when granted to the account or one of its
groups, with or without a role; the admin console is open to an active account marked staff
status. Only active accounts are counted; inactive ones cannot sign in.

The admin role changes the rule on the Security settings page, with a reason. A save holds the
acting account (accounts.acting), takes the rule's lock, checks the revision the form showed and
the role as the account is held, and records one audit event with the rule before and after.

Basis: 45 CFR §164.312(d), person or entity authentication, which names no method. NIST SP
800-63B-4 asks for two factors at AAL2 (a recommendation). Requiring multi-factor authentication
is part of the proposed changes to the HIPAA Security Rule (90 FR 898, proposed §164.312(f)), not
in force. Which accounts must have a second factor is the laboratory's decision.
"""

from allauth.mfa import app_settings as mfa_settings
from allauth.mfa.models import Authenticator
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction

from accounts.acting import hold_actor
from accounts.roles import OPERATIONAL_ROLES, ROLE_LABELS, ROLES, expand_roles, has_role

from .audit import record_event
from .models import AuditEvent, SecondFactorRule

MANAGER_ROLES = ("admin",)
ACTION = "lab.second_factor_rule_changed"
PATIENT_CAPABILITY = "view_patient_information"
FLAGS = ("staff", "patient_information", "admin_console", "portal")
FIELDS = ("staff", "roles", "patient_information", "admin_console", "portal")
# The advisory lock (PostgreSQL's two-key form; see core.session_limits for the neighbouring keys)
# that one save of the rule holds at a time, its first save included.
RULE_LOCK = (19559, 0)
# How the rule's parts are named on the pages, in this order.
PART_LABELS = {
    "staff": "Every staff account",
    "roles": "Staff with these roles",
    "patient_information": "Staff who can view patient information",
    "admin_console": "Accounts that can open the admin console",
    "portal": "Client portal accounts",
}


def can_manage(user):
    return has_role(user, *MANAGER_ROLES)


def rule():
    """The rule as it stands: the saved row, or an unsaved one requiring nothing (revision 0)."""
    return SecondFactorRule.objects.filter(pk=1).first() or SecondFactorRule(pk=1)


def values(current):
    """The rule's parts as plain values, the roles in ROLES order."""
    return {
        "staff": current.staff,
        "roles": [role for role in ROLES if role in set(current.roles or ())],
        "patient_information": current.patient_information,
        "admin_console": current.admin_console,
        "portal": current.portal,
    }


def requires_any(current):
    return any(values(current)[name] for name in FIELDS)


def _tables():
    from django.contrib.auth.models import Group
    from django.contrib.contenttypes.models import ContentType

    User = get_user_model()
    quote = connection.ops.quote_name
    return {
        name: quote(model._meta.db_table)
        for name, model in (
            ("user", User),
            ("user_groups", User.groups.through),
            ("user_permissions", User.user_permissions.through),
            ("group", Group),
            ("group_permissions", Group.permissions.through),
            ("permission", Permission),
            ("content_type", ContentType),
            ("authenticator", Authenticator),
            ("rule", SecondFactorRule),
        )
    }


def _rule_source(current):
    """(SQL of a one-row relation `r` holding the rule's parts, its parameters): the saved row as
    the statement reads it, or `current`'s values when given (a rule read earlier in the same
    snapshot, or one not saved). No saved row: no row, so nothing is covered."""
    tables = _tables()
    if current is None:
        return (
            "(SELECT staff, roles, patient_information, admin_console, portal "
            f"FROM {tables['rule']} WHERE id = 1)",
            [],
        )
    chosen = values(current)
    return (
        "(SELECT %s::boolean AS staff, %s::varchar[] AS roles, "
        "%s::boolean AS patient_information, %s::boolean AS admin_console, %s::boolean AS portal)",
        [
            chosen["staff"],
            chosen["roles"],
            chosen["patient_information"],
            chosen["admin_console"],
            chosen["portal"],
        ],
    )


def _parts_sql():
    """[(part, SQL condition on account `u` and rule `r`, parameters)] in FIELDS order. Roles as
    effective_roles counts them (the admin role does the work of the operational ones), and only
    the role names of accounts.roles.ROLES, whatever else a stored row holds, as values() reads it;
    the
    patient-information grant on the account or any of its groups, as has_capability counts it;
    staff status as the admin console checks it."""
    t = _tables()
    held = (
        f"SELECT 1 FROM {t['user_groups']} ug JOIN {t['group']} g ON g.id = ug.group_id "
        "WHERE ug.user_id = u.id AND g.name = ANY(%s::varchar[]) AND (g.name = ANY(r.roles) "
        "OR (g.name = 'admin' AND r.roles && %s::varchar[]))"
    )
    grant = (
        f"SELECT 1 FROM {t['permission']} p JOIN {t['content_type']} ct "
        "ON ct.id = p.content_type_id WHERE p.codename = %s AND ct.app_label = 'accounts' AND ("
        f"p.id IN (SELECT up.permission_id FROM {t['user_permissions']} up "
        "WHERE up.user_id = u.id) "
        f"OR p.id IN (SELECT gp.permission_id FROM {t['group_permissions']} gp "
        f"JOIN {t['user_groups']} ug2 ON ug2.group_id = gp.group_id WHERE ug2.user_id = u.id))"
    )
    return [
        ("staff", "(r.staff AND u.client_id IS NULL)", []),
        (
            "roles",
            f"(u.client_id IS NULL AND EXISTS ({held}))",
            [list(ROLES), sorted(OPERATIONAL_ROLES)],
        ),
        (
            "patient_information",
            f"(r.patient_information AND u.client_id IS NULL AND EXISTS ({grant}))",
            [PATIENT_CAPABILITY],
        ),
        ("admin_console", "(r.admin_console AND u.is_staff)", []),
        ("portal", "(r.portal AND u.client_id IS NOT NULL)", []),
    ]


def _covered_sql():
    """(SQL: the rule `r` covers the account `u`, its parameters)."""
    parts = _parts_sql()
    return (
        "(" + " OR ".join(sql for _name, sql, _params in parts) + ")",
        [param for _name, _sql, params in parts for param in params],
    )


def requires(user, current=None):
    """Whether the rule requires a second factor of this active account: the rule as it stands
    (or `current`'s values) and the account as it stands, read in one statement."""
    if user is None or user.pk is None:
        return False
    t = _tables()
    source, source_params = _rule_source(current)
    condition, params = _covered_sql()
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT EXISTS (SELECT 1 FROM {t['user']} u, {source} r "
            f"WHERE u.id = %s AND u.is_active AND {condition})",
            [*source_params, user.pk, *params],
        )
        return bool(cursor.fetchone()[0])


def reasons(user, current=None):
    """Which parts of the rule cover this active account now, in page order, as the setup page
    names them ("the Supervisor role", "client portal accounts"): the rule, the account, its roles
    and grants read in one statement. For the roles part, the chosen roles the account holds as
    effective_roles counts them."""
    if user is None or user.pk is None:
        return []
    t = _tables()
    parts = _parts_sql()
    source, source_params = _rule_source(current)
    columns = ", ".join(sql for _name, sql, _params in parts)
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {columns}, r.roles, ARRAY(SELECT g.name FROM {t['user_groups']} ug "
            f"JOIN {t['group']} g ON g.id = ug.group_id WHERE ug.user_id = u.id) "
            f"FROM {t['user']} u, {source} r WHERE u.id = %s AND u.is_active",
            [
                *[param for _name, _sql, params in parts for param in params],
                *source_params,
                user.pk,
            ],
        )
        row = cursor.fetchone()
    if row is None:
        return []
    covers = dict(zip([name for name, _sql, _params in parts], row[: len(parts)], strict=True))
    chosen, group_names = row[len(parts)] or [], row[len(parts) + 1] or []
    held = expand_roles(group_names)
    found = []
    for name in FIELDS:
        if not covers.get(name):
            continue
        if name == "roles":
            labels = [ROLE_LABELS[role] for role in ROLES if role in chosen and role in held]
            found.append(f"the {_either(labels)} role" if labels else "staff with chosen roles")
        else:
            found.append(REASON_LABELS[name])
    return found


REASON_LABELS = {
    "staff": "every staff account",
    "patient_information": "staff who can view patient information",
    "admin_console": "accounts that can open the admin console",
    "portal": "client portal accounts",
}


def _either(names):
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def second_factor_types():
    """The authenticator types that are a second factor: those this deployment offers
    (MFA_SUPPORTED_TYPES), less recovery codes, which come with an authenticator app and stand in
    for it when it is lost; on their own they are not counted. A stored credential of a type the
    deployment no longer offers is not counted either."""
    return [
        kind for kind in mfa_settings.SUPPORTED_TYPES if kind != Authenticator.Type.RECOVERY_CODES
    ]


def has_second_factor(user):
    return Authenticator.objects.filter(user_id=user.pk, type__in=second_factor_types()).exists()


def counts(current=None):
    """(active accounts the rule covers, of which without a second factor), in one statement: the
    rule as it stands (or `current`'s values) and the accounts. (0, 0) when it covers none."""
    t = _tables()
    source, source_params = _rule_source(current)
    condition, params = _covered_sql()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*), count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM "
            f"{t['authenticator']} a WHERE a.user_id = u.id AND a.type = ANY(%s))) "
            f"FROM {t['user']} u, {source} r WHERE u.is_active AND {condition}",
            [second_factor_types(), *source_params, *params],
        )
        covered, missing = cursor.fetchone()
    return covered, missing


def _clean(new):
    """The rule's parts from a form or a caller, checked: each flag a boolean, each role a known
    one. Returns them as values() would."""
    errors = {}
    cleaned = {}
    unknown_parts = sorted(set(new) - set(FIELDS))
    if unknown_parts:
        errors["__all__"] = "Unknown parts of the rule: " + ", ".join(unknown_parts) + "."
    for name in FLAGS:
        value = new.get(name, False)
        if not isinstance(value, bool):
            errors[name] = "Choose yes or no."
        cleaned[name] = value
    roles = new.get("roles", [])
    if not isinstance(roles, list | tuple | set) or not all(isinstance(r, str) for r in roles):
        errors["roles"] = "Choose from the listed roles."
        roles = []
    unknown = sorted(set(roles) - set(ROLES))
    if unknown:
        errors["roles"] = "Choose from the listed roles."
    if errors:
        raise ValidationError(errors)
    cleaned["roles"] = [role for role in ROLES if role in set(roles)]
    return cleaned


@transaction.atomic
def save_rule(*, actor, new, expected_revision, reason):
    """Set the rule, with a reason. Returns (rule, changed).

    `expected_revision` is the rule's revision the form showed (0 for a rule never saved): a
    change saved in between refuses this one. Whether a session is held to the rule is decided at
    its sign-in (accounts.enrolment): a session the rule did not cover then is not held.
    """
    current = hold_actor(actor)  # before any other lock (accounts.acting)
    if current is None or not can_manage(current):
        raise PermissionDenied("Your role does not permit this action.")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required."})
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", list(RULE_LOCK))
    stored = SecondFactorRule.objects.select_for_update().filter(pk=1).first()
    if stored is None:
        stored = SecondFactorRule(pk=1)
    if expected_revision is None or stored.revision != expected_revision:
        raise ValidationError(
            "The second sign-in factor rule was changed after you opened this form. Your input "
            "has been kept; reload and compare the current rule before saving again."
        )
    after = _clean(new)
    # The row as stored (its role names as they are, for the audit event): a stored name outside
    # ROLES makes it differ, so a save clears it; order and repeats of known names do not.
    before = {**values(stored), "roles": list(stored.roles or [])}
    strays = set(before["roles"]) - set(ROLES)
    if after == values(stored) and not strays:
        return stored, False
    for name, value in after.items():
        setattr(stored, name, value)
    stored.revision += 1
    stored.save()
    record_event(
        actor=current, action=ACTION, obj=stored, before=before, after=after, reason=reason
    )
    return stored, True


def summary(chosen):
    """What a rule's values require, in words: "Every staff account; client portal accounts", or
    "Nobody" when none is chosen."""
    parts = []
    for name in FIELDS:
        value = chosen.get(name)
        if name == "roles":
            if value:
                labels = [ROLE_LABELS.get(role, role) for role in value]
                parts.append("Staff with the " + _either(labels) + " role")
        elif value is True:
            parts.append(PART_LABELS[name])
    if not parts:
        return "Nobody"
    return "; ".join([parts[0], *[part[0].lower() + part[1:] for part in parts[1:]]])


def history(limit=20):
    """The newest changes to the rule, newest first."""
    return (
        AuditEvent.objects.filter(action=ACTION)
        .select_related("actor")
        .order_by("-occurred_at")[:limit]
    )
