"""Access reviews: the laboratory's signed record that a reviewer went through who holds what and
decided, for each grant, to keep it or remove it (docs/product.md "Access review"; 45 CFR
§164.308(a)(3)(ii)(B) and §164.308(a)(4)(ii)(C), both addressable).

Who. Holders of "Can review access and audit records" with a laboratory role (audit_review.
permissions) record and sign an access review, and read every one. Nobody changes or removes a
signed access review or its decisions: the database refuses it (migration 0005).

What is reviewed. A review covers one scope (AccessScope): the active staff accounts, the scheduled
jobs' own aside, or the active client portal accounts, of every organization or of one; at most
MOST_ACCOUNTS accounts. For each account, every grant it holds as it stands (Grant, read_grants):
admin console access and superuser status, each group it is a member of (a role, or another group,
with the capabilities the group gives), each capability given to it directly, and for a client
portal account the portal account itself, with what of its organization it may see. An account that
holds none of these (a staff account with no group, capability or admin console access) is not
listed: it opens nothing but its own account pages.

Decisions. The reviewer decides Keep or Remove for every grant listed; there is no default. The
review records the decision; it removes nothing. A grant is removed where it is given: roles,
capabilities and admin console access on the account's page in the admin console, by a superuser; a
portal account by deactivating it there. A review's page shows, for each grant decided for removal,
whether the account still holds it (still_held), read as it stands when the page is drawn.

Signing. As for review records (audit_review.reviews): the reviewer enters their password again at
each signing, which the form checks before anything is locked. The service then reads the accounts
the scope covers and takes their access gates shared, with the signer's own, in key order (accounts.
acting.share_access: the order close_access takes them in, so the two never wait for each other in a
cycle), before anything else it locks; then it holds the acting account (hold_actor) and decides
from the account as held that it still may review and still has the password that was checked. From
then until the review commits, no change in this application takes a reviewed account's grants away
or widens them (each takes the account's gate exclusively), and the grants read are those the review
records. The moment of signing is taken, then the accounts and their grants are read again, as they
stand, and compared with what the form showed (fingerprint): any difference, an account added to the
scope or leaving it included, refuses the signing, and the form is drawn again as the grants stand,
keeping the decisions on grants that did not change. Only what the reading shows is written: the
review, a decision for each grant (AccessReviewItem), the sign-off (the signer's name, username and
roles as they stand, the meaning Reviewed, the moment, and a SHA-256 checksum of exactly what was
signed, signed_content) and its audit event, in one transaction. Known limit: an account created,
or made active, after the reading and committed before the review is not in it; it was not active
when the grants were read.

Nothing here shows a password, a second factor's secret or codes, an account's email address, or
anything of a ticket.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from accounts.acting import share_access
from accounts.roles import CAPABILITIES, ROLE_LABELS, ROLES
from clients.models import PortalAccess
from core.audit import record_event

from . import access_report
from .models import (
    ACCESS_NUMBER_PREFIX,
    AccessReview,
    AccessReviewItem,
    AccessScope,
    Decision,
    GrantKind,
    Meaning,
)
from .reviews import PASSWORD_CHANGED, roles_label
from .reviews import _reviewer as reviewer
from .reviews import _text as text_value

SIGNED = "access_review.signed"
TEXT_LIMIT = 4000
# The most accounts one review covers: their access gates are held until it commits (each takes a
# slot of PostgreSQL's shared lock table, accounts.acting.close_access).
MOST_ACCOUNTS = 500
# The most grants one review lists: each decision is a field of the form, and a request carries at
# most 1,000 fields (Django's DATA_UPLOAD_MAX_NUMBER_FIELDS).
MOST_GRANTS = 900
# The advisory lock every signing takes, until it commits, before it is given its number (as
# audit_review.reviews.SIGNING_LOCK): numbers then follow the order signings commit in. First keys
# 19553 to 19565 name other locks (accounts.acting, audit_review.reviews, access_report).
SIGNING_LOCK = (19566, 0)
STALE = (
    "The accounts or what they hold changed after you opened this review. The list below now shows "
    "them as they stand: your decisions are kept for the grants that did not change, and the "
    "grants marked “New or changed” need one. Check them, then sign again."
)
TOO_MANY = (
    f"A review covers at most {MOST_ACCOUNTS} accounts and {MOST_GRANTS} grants, and this choice "
    "has more. Review the client portal accounts of one organization at a time."
)
NOTHING = "No active account in this choice holds anything to review."
UNDECIDED = "Choose Keep or Remove."

PORTAL_LABEL = "Portal account"


# ------------------------------------------------------------------------------ what is reviewed


@dataclass(frozen=True)
class Grant:
    """One grant an account holds, as the form shows it and a decision records it."""

    account_id: int
    account_name: str
    account_username: str
    organization_name: str
    kind: str
    code: str
    label: str
    detail: str

    @property
    def key(self):
        """The grant's identity: the account, the kind and the code."""
        return f"{self.account_id}:{self.kind}:{self.code}"

    def shown(self):
        """What the form shows of the grant: its identity and every word about it."""
        return [
            self.account_id,
            self.account_name,
            self.account_username,
            self.organization_name,
            self.kind,
            self.code,
            self.label,
            self.detail,
        ]

    @property
    def field_name(self):
        """The name of the grant's decision on the form: a grant whose words change gets another
        one, so a decision made on the old words is never carried over to the new."""
        digest = hashlib.sha256(_json(self.shown()).encode()).hexdigest()
        return f"d-{digest[:24]}"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(grants):
    """A digest of exactly what a form showed: every grant in order, with its words."""
    return hashlib.sha256(_json([grant.shown() for grant in grants]).encode()).hexdigest()


def scope_accounts(scope, organization=None):
    """The active accounts `scope` covers, in the order the form lists them (the name shown, then
    the username, then the id: access_report.ordered)."""
    queryset = get_user_model().objects.select_related("client").filter(is_active=True)
    if scope == AccessScope.STAFF:
        if organization is not None:
            raise ValueError("Only client portal accounts are reviewed by organization.")
        queryset = queryset.filter(client__isnull=True)
        system_id = access_report.system_account_id()
        if system_id is not None:
            queryset = queryset.exclude(pk=system_id)
    elif scope == AccessScope.PORTAL:
        queryset = queryset.filter(client__isnull=False)
        if organization is not None:
            queryset = queryset.filter(client_id=organization.pk)
    else:
        raise ValueError(f"Unknown scope {scope!r}.")
    return access_report.ordered(queryset, access_report.DEFAULT_SORT)


def _group_words(name):
    return f"{ROLE_LABELS[name]} role" if name in ROLES else f"Group “{name}”"


def _portal(account, access):
    """(code, words) of a portal account's grant: its organization, and the whole organization,
    the sites granted (by id and name) or only the tickets it raised."""
    client = str(account.client_id)
    if access is None or not access.applies_to(account):
        return f"{client}:none", "Only the tickets it raised"
    if access.whole_organization:
        return f"{client}:all", "Its organization's tickets"
    sites = sorted(access.sites.all(), key=lambda site: (site.name.lower(), site.pk))
    if not sites:
        return f"{client}:none", "Only the tickets it raised"
    ids = ",".join(str(site.pk) for site in sorted(sites, key=lambda site: site.pk))
    names = ", ".join(site.name for site in sites)
    return f"{client}:{ids}", f"Tickets of {names}, and those it raised"


def read_grants(accounts_list):
    """Every grant of each account of `accounts_list` (in that order), as it stands: admin console
    access, superuser status, groups (roles first, in accounts.roles.ROLES order, then other groups
    by name), capabilities given directly, and a client portal account's portal account."""
    User = get_user_model()
    ids = [account.pk for account in accounts_list]
    memberships = {}
    group_ids = set()
    for user_id, group_id, name in User.groups.through.objects.filter(user_id__in=ids).values_list(
        "user_id", "group_id", "group__name"
    ):
        memberships.setdefault(user_id, []).append((group_id, name))
        group_ids.add(group_id)
    permissions = access_report._capability_permissions(list(CAPABILITIES))
    direct = {}
    for user_id, codename in User.user_permissions.through.objects.filter(
        user_id__in=ids, permission__in=permissions
    ).values_list("user_id", "permission__codename"):
        direct.setdefault(user_id, set()).add(codename)
    gives = {}
    for group_id, codename in Group.permissions.through.objects.filter(
        group_id__in=group_ids, permission__in=permissions
    ).values_list("group_id", "permission__codename"):
        gives.setdefault(group_id, set()).add(codename)
    portal = {
        access.user_id: access
        for access in PortalAccess.objects.filter(
            user_id__in=[account.pk for account in accounts_list if account.client_id is not None]
        ).prefetch_related("sites")
    }
    grants = []
    for account in accounts_list:
        name = access_report.person_name(account)
        organization = account.client.name if account.client_id is not None else ""

        def grant(kind, code, label, detail="", account=account, name=name, org=organization):
            return Grant(account.pk, name, account.username, org, kind, code, label, detail)

        for flag, label in access_report.CONSOLE:
            if getattr(account, flag):
                grants.append(grant(GrantKind.CONSOLE, flag, label))
        for group_id, group_name in sorted(
            memberships.get(account.pk, ()), key=access_report._group_order
        ):
            given = [
                label for code, label in CAPABILITIES.items() if code in gives.get(group_id, ())
            ]
            detail = f"Gives {' and '.join(given)}" if given else ""
            grants.append(grant(GrantKind.GROUP, str(group_id), _group_words(group_name), detail))
        for codename, label in CAPABILITIES.items():
            if codename in direct.get(account.pk, ()):
                grants.append(grant(GrantKind.CAPABILITY, codename, label, "Given directly"))
        if account.client_id is not None:
            code, detail = _portal(account, portal.get(account.pk))
            grants.append(grant(GrantKind.PORTAL, code, PORTAL_LABEL, detail))
    return grants


@dataclass
class AccountGrants:
    """One account's grants, as the form and a review's page group them."""

    account_id: int
    name: str
    username: str
    organization: str
    grants: list


def by_account(grants):
    """`grants` (in order) grouped by account, in the same order."""
    groups = []
    for grant in grants:
        if not groups or groups[-1].account_id != grant.account_id:
            groups.append(
                AccountGrants(
                    grant.account_id,
                    grant.account_name,
                    grant.account_username,
                    grant.organization_name,
                    [],
                )
            )
        groups[-1].grants.append(grant)
    return groups


@dataclass
class Reading:
    """The accounts a scope covers and their grants, read together."""

    accounts: int
    grants: list
    too_many: bool = False

    @property
    def fingerprint(self):
        return fingerprint(self.grants)


def reading(scope, organization=None):
    """The scope's accounts and their grants, as they stand, in the caller's reading; none and
    too_many beyond MOST_ACCOUNTS accounts or MOST_GRANTS grants."""
    accounts_list = list(scope_accounts(scope, organization)[: MOST_ACCOUNTS + 1])
    if len(accounts_list) > MOST_ACCOUNTS:
        return Reading(accounts=len(accounts_list), grants=[], too_many=True)
    grants = read_grants(accounts_list)
    accounts = len({grant.account_id for grant in grants})
    if len(grants) > MOST_GRANTS:
        return Reading(accounts=accounts, grants=[], too_many=True)
    return Reading(accounts=accounts, grants=grants)


# ------------------------------------------------------------------------------ what is signed


def signed_content(review, items):
    """Exactly what a sign-off signs, as the bytes its checksum is taken of: the review's number,
    its scope, every grant reviewed with its decision (in order), the counts, the findings and
    follow-up, and the sign-off itself (who by id, name, username and roles, the meaning and the
    moment), as JSON with sorted keys and no spaces."""
    content = {
        "record": "access review",
        "number": review.number,
        "scope": review.scope,
        "organization": (
            {"id": review.organization_id, "name": review.organization_name}
            if review.organization_id is not None
            else None
        ),
        "items": [
            [
                item.position,
                item.account_id,
                item.account_name,
                item.account_username,
                item.organization_name,
                item.kind,
                item.code,
                item.label,
                item.detail,
                item.decision,
            ]
            for item in items
        ],
        "accounts": review.accounts,
        "grants": review.grants,
        "removals": review.removals,
        "findings": review.findings,
        "follow_up": review.follow_up,
        "signer": {
            "id": review.signed_by_id,
            "name": review.signer_name,
            "username": review.signer_username,
            "roles": review.signer_roles,
        },
        "meaning": review.meaning,
        "signed_at": review.signed_at.astimezone(UTC).isoformat(timespec="microseconds"),
    }
    return _json(content).encode()


def checksum(review, items):
    return hashlib.sha256(signed_content(review, items)).hexdigest()


def intact(review, items=None):
    """Whether the review and its decisions as stored still match the checksum taken when it was
    signed."""
    items = list(review.items.order_by("position")) if items is None else items
    return checksum(review, items) == review.checksum


# ------------------------------------------------------------------------------ signing


def _next_id():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT nextval(pg_get_serial_sequence(%s, 'id'))", [AccessReview._meta.db_table]
        )
        return cursor.fetchone()[0]


def _decisions(grants, decisions):
    """Each grant's decision from `decisions` ({grant key: "keep" or "remove"}): every grant needs
    one, and none may name a grant not listed."""
    decisions = dict(decisions or {})
    keys = {grant.key for grant in grants}
    if set(decisions) - keys:
        raise ValidationError(STALE)
    chosen = []
    for grant in grants:
        decision = decisions.get(grant.key)
        if decision not in Decision.values:
            raise ValidationError(f"{UNDECIDED} Every grant listed needs a decision.")
        chosen.append(decision)
    return chosen


@transaction.atomic
def sign_access_review(
    *,
    actor,
    scope,
    organization=None,
    decisions,
    expected,
    findings="",
    follow_up="",
    request_ref="",
    password_hash=None,
):
    """Write an access review signed by `actor` as reviewed (see the module docstring).
    `decisions` maps each grant's key (Grant.key) to "keep" or "remove"; `expected` is the
    fingerprint of the grants the form showed. The caller checked the password entered at signing;
    `password_hash` is the stored hash that check accepted, and the account as held must still have
    it. Returns the review."""
    if scope not in AccessScope.values:
        raise ValidationError({"scope": "Choose which accounts to review."})
    if organization is not None and scope != AccessScope.PORTAL:
        raise ValidationError(
            {"organization": "Only client portal accounts are reviewed by organization."}
        )
    # The accounts the scope covers, before any lock; their gates, with the signer's, in key order.
    ids = list(
        scope_accounts(scope, organization).values_list("pk", flat=True)[: MOST_ACCOUNTS + 1]
    )
    if len(ids) > MOST_ACCOUNTS:
        raise ValidationError(TOO_MANY)
    share_access([*ids, getattr(actor, "pk", None)])
    signer = reviewer(actor)
    if password_hash is not None and signer.password != password_hash:
        raise ValidationError({"confirm_password": PASSWORD_CHANGED})
    findings = text_value(findings, "findings", "Findings", TEXT_LIMIT)
    follow_up = text_value(follow_up, "follow_up", "Follow-up", TEXT_LIMIT)
    signed_at = timezone.now()
    current = reading(scope, organization)
    if current.too_many or current.fingerprint != expected:
        raise ValidationError(STALE)
    if not current.grants:
        raise ValidationError(NOTHING)
    chosen = _decisions(current.grants, decisions)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", list(SIGNING_LOCK))
    pk = _next_id()
    review = AccessReview(
        id=pk,
        number=f"{ACCESS_NUMBER_PREFIX}{pk:06d}",
        scope=scope,
        organization_id=organization.pk if organization is not None else None,
        organization_name=organization.name if organization is not None else "",
        accounts=current.accounts,
        grants=len(current.grants),
        removals=chosen.count(Decision.REMOVE),
        findings=findings,
        follow_up=follow_up,
        signed_by_id=signer.pk,
        signer_name=access_report.person_name(signer),
        signer_username=signer.username,
        signer_roles=roles_label(signer),
        meaning=Meaning.REVIEWED,
        signed_at=signed_at,
        request_ref=(request_ref or "")[:32],
    )
    items = [
        AccessReviewItem(
            review=review,
            position=position,
            account_id=grant.account_id,
            account_name=grant.account_name,
            account_username=grant.account_username,
            organization_name=grant.organization_name,
            kind=grant.kind,
            code=grant.code,
            label=grant.label,
            detail=grant.detail,
            decision=decision,
        )
        for position, (grant, decision) in enumerate(zip(current.grants, chosen, strict=True), 1)
    ]
    review.checksum = checksum(review, items)
    review.save(force_insert=True)
    AccessReviewItem.objects.bulk_create(items)
    record_event(actor=signer, action=SIGNED, obj=review, after=_snapshot(review, items))
    return review


def _snapshot(review, items):
    """What an access review's audit event holds: its number, scope, counts, the grants decided for
    removal (account username and the grant's words), the findings, follow-up and sign-off, and
    its checksum; nothing of the request."""
    return {
        "number": review.number,
        "scope": review.scope,
        "organization": review.organization_name,
        "accounts": review.accounts,
        "grants": review.grants,
        "removals": review.removals,
        "removed": [
            {"account": item.account_username, "grant": item.label, "detail": item.detail}
            for item in items
            if item.decision == Decision.REMOVE
        ],
        "findings": review.findings,
        "follow_up": review.follow_up,
        "signer_name": review.signer_name,
        "signer_username": review.signer_username,
        "signer_roles": review.signer_roles,
        "meaning": review.meaning,
        "signed_at": review.signed_at.isoformat(),
        "checksum": review.checksum,
    }


# ------------------------------------------------------------------------------ after signing


def still_held(items):
    """{item id: True when the account still holds the grant now} for `items` (decisions to
    remove), read as the accounts stand in the caller's reading. A deactivated or missing account
    holds nothing; a portal account is held while it is active in the organization it was reviewed
    in."""
    User = get_user_model()
    items = list(items)
    accounts = {
        account.pk: account
        for account in User.objects.filter(pk__in={item.account_id for item in items})
    }
    group_items = [item for item in items if item.kind == GrantKind.GROUP and item.code.isdigit()]
    members = set(
        User.groups.through.objects.filter(
            user_id__in={item.account_id for item in group_items},
            group_id__in={int(item.code) for item in group_items},
        ).values_list("user_id", "group_id")
    )
    capability_items = [item for item in items if item.kind == GrantKind.CAPABILITY]
    direct = set(
        User.user_permissions.through.objects.filter(
            user_id__in={item.account_id for item in capability_items},
            permission__in=access_report._capability_permissions(
                [item.code for item in capability_items]
            ),
        ).values_list("user_id", "permission__codename")
    )
    held = {}
    for item in items:
        account = accounts.get(item.account_id)
        if account is None or not account.is_active:
            held[item.pk] = False
        elif item.kind == GrantKind.CONSOLE:
            held[item.pk] = item.code in ("is_staff", "is_superuser") and bool(
                getattr(account, item.code)
            )
        elif item.kind == GrantKind.GROUP:
            held[item.pk] = item.code.isdigit() and (item.account_id, int(item.code)) in members
        elif item.kind == GrantKind.CAPABILITY:
            held[item.pk] = (item.account_id, item.code) in direct
        else:
            held[item.pk] = str(account.client_id) == item.code.split(":", 1)[0]
    return held


def latest():
    """The access review signed most recently (numbers follow the order of signing), or None."""
    return AccessReview.objects.order_by("-id").first()
