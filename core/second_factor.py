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

from functools import reduce
from operator import or_

from allauth.mfa.models import Authenticator
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import BooleanField, Count, Exists, ExpressionWrapper, OuterRef, Q

from accounts.acting import hold_actor
from accounts.roles import OPERATIONAL_ROLES, ROLE_LABELS, ROLES, effective_roles, has_role

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


def _role_names(roles):
    """The role group names that give any of `roles` as effective_roles counts them: the roles
    themselves, and the admin role when an operational role is among them."""
    names = set(roles)
    if names & OPERATIONAL_ROLES:
        names.add("admin")
    return sorted(names)


def _has_capability():
    """Exists() of the patient-information grant to the account or to one of its groups."""
    grant = Permission.objects.filter(
        codename=PATIENT_CAPABILITY, content_type__app_label="accounts"
    )
    User = get_user_model()
    direct = User.user_permissions.through.objects.filter(
        user_id=OuterRef("pk"), permission__in=grant
    )
    through_group = User.groups.through.objects.filter(
        user_id=OuterRef("pk"), group__permissions__in=grant
    )
    return Exists(direct) | Exists(through_group)


def _parts(current):
    """{part: Q of the active accounts it covers} for each part the rule turns on."""
    User = get_user_model()
    staff = Q(client_id__isnull=True)
    chosen = values(current)
    parts = {}
    if chosen["staff"]:
        parts["staff"] = staff
    if chosen["roles"]:
        held = User.groups.through.objects.filter(
            user_id=OuterRef("pk"), group__name__in=_role_names(chosen["roles"])
        )
        parts["roles"] = staff & Exists(held)
    if chosen["patient_information"]:
        parts["patient_information"] = staff & _has_capability()
    if chosen["admin_console"]:
        parts["admin_console"] = Q(is_staff=True)
    if chosen["portal"]:
        parts["portal"] = Q(client_id__isnull=False)
    return parts


def covered(current):
    """Q of the active accounts the rule requires a second factor of, or None when it requires
    none."""
    parts = list(_parts(current).values())
    if not parts:
        return None
    return Q(is_active=True) & reduce(or_, parts)


def requires(user, current=None):
    """Whether the rule (as it stands, unless one is given) requires a second factor of this
    account, as the account stands in the database now: one statement."""
    current = current if current is not None else rule()
    condition = covered(current)
    if condition is None or user is None or user.pk is None:
        return False
    return get_user_model().objects.filter(condition, pk=user.pk).exists()


def reasons(user, current=None):
    """Which parts of the rule cover this account now, in page order, as the setup page names
    them ("the Supervisor role", "client portal accounts"): one statement. For the roles part, the
    chosen roles the account holds as effective_roles counts them."""
    current = current if current is not None else rule()
    parts = _parts(current)
    if not parts or user is None or user.pk is None:
        return []
    covers = {
        f"covers_{name}": ExpressionWrapper(condition, output_field=BooleanField())
        for name, condition in parts.items()
    }
    row = get_user_model().objects.filter(pk=user.pk, is_active=True).values(**covers).first()
    if row is None:
        return []
    found = []
    for name in FIELDS:
        if not row.get(f"covers_{name}"):
            continue
        if name == "roles":
            held = effective_roles(user)
            labels = [ROLE_LABELS[role] for role in values(current)["roles"] if role in held]
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
    """The authenticator types that are a second factor. Recovery codes come with an authenticator
    app and stand in for it when it is lost; on their own they are not counted."""
    return [kind for kind in Authenticator.Type.values if kind != Authenticator.Type.RECOVERY_CODES]


def has_second_factor(user):
    return Authenticator.objects.filter(user_id=user.pk, type__in=second_factor_types()).exists()


def counts(current=None):
    """(accounts the rule covers, of which without a second factor), in one statement over the
    active accounts. (0, 0) when the rule requires nothing."""
    current = current if current is not None else rule()
    condition = covered(current)
    if condition is None:
        return 0, 0
    enrolled = Exists(
        Authenticator.objects.filter(user=OuterRef("pk"), type__in=second_factor_types())
    )
    totals = (
        get_user_model()
        .objects.filter(condition)
        .annotate(enrolled=enrolled)
        .aggregate(covered=Count("pk"), missing=Count("pk", filter=Q(enrolled=False)))
    )
    return totals["covered"], totals["missing"]


def _clean(new):
    """The rule's parts from a form or a caller, checked: each flag a boolean, each role a known
    one. Returns them as values() would."""
    errors = {}
    cleaned = {}
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
    change saved in between refuses this one. Sessions already open keep going; the rule applies
    from each account's next sign-in.
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
    before = values(stored)
    if after == before:
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
