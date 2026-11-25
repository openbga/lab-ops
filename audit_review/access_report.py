"""The Access review section (docs/product.md "Access review"): the access report, which lists
every account with what it holds, and the list of active accounts unused for a period the
laboratory sets. Read-only, apart from the period. For holders of "Can review access and audit
records" with a laboratory role (audit_review.permissions), as the audit review workspace is.

The report. For each account: its kind (staff, client portal, or the scheduled jobs' own) and
whether it is active, its admin console access (staff status, superuser), its roles (the role
groups of accounts.roles.ROLES) and capabilities (accounts.roles.CAPABILITIES, granted to the
account or through one of its groups), its teams, its portal organization and what of it the
account may see (clients.models.PortalAccess), whether it has a second factor and whether the
laboratory's rule requires one (core.second_factor), and its last sign-in. Each is read as it stands
when the page is drawn, in one reading (core.concurrency.one_reading).

Who gave each role, capability and admin console access, and when, is read from the change
history (core.models.AuditEvent): the account administration, setup_lab and the demonstration seed
record an account's roles, capability grants and flags before and after each change they make, and
the role group's page records the capabilities granted to the group. For each one held now, the
latest recorded change that gave it is shown; none is shown ("Not in the change history") when no
recorded change gave it, or a later recorded change took it away (it was given again by a path that
records nothing, such as the database directly). The history names roles by name: a group renamed
on its own page is followed through its recorded renames. A deactivated account shows the recorded
change that deactivated it. Names of the people who made a change are read as they stand now.

Unused accounts. An active account is unused when it has not signed in for the laboratory's period
(UnusedPeriod) by the application's clock: its last sign-in (User.last_login, written at each
accepted sign-in) is older than that many days, or it never signed in and was created before then.
No period ships: until a reviewer sets one, nothing is listed. The period is changed with a reason,
under its own lock, against the revision the form showed, with an audit event (set_period).

Nothing here shows a password, a second factor's secret or codes, an email address, or anything of
a ticket.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from allauth.mfa.models import Authenticator
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import CharField, Exists, F, OuterRef, Q, Value
from django.db.models.functions import Coalesce, Concat, Lower, NullIf, Trim
from django.utils import timezone

from accounts.roles import CAPABILITIES, ROLE_LABELS, ROLES
from clients.models import PortalAccess
from core import second_factor
from core.audit import record_event
from core.models import AuditEvent
from teams.models import TeamMembership

from .models import UnusedPeriod
from .reviews import _reviewer as reviewer
from .reviews import _text as text_value

ACCOUNT_OBJECT = "accounts.user"
GROUP_OBJECT = "auth.group"
# The recorded changes that name an account's roles, capability grants and flags before and after
# (accounts.admin, accounts.setup), and the demonstration seed's capability grant (desk.story).
ACCOUNT_CHANGES = (
    "account.created",
    "account.updated",
    "setup.account_created",
    "setup.account_roles_added",
)
SEED_CAPABILITY = "account.capability_granted"
# The recorded changes that name a group's name and capability grants before and after.
GROUP_CHANGES = ("group.created", "group.updated", "setup.group_created")
CREATED = ("account.created", "setup.account_created", "group.created", "setup.group_created")
# Admin console access, by the flag that gives it, as the account page names it.
CONSOLE = (("is_staff", "Admin console"), ("is_superuser", "Superuser"))

PERIOD_CHANGED = "access_review.unused_period_changed"
MOST_DAYS = 366
TEXT_LIMIT = 4000
# The advisory lock (PostgreSQL's two-key form) a save of the period takes. First keys 19553 to
# 19564 name other locks (accounts.acting, audit_review.reviews and their neighbours).
PERIOD_LOCK = (19565, 0)
STALE_PERIOD = (
    "The unused account period was changed after you opened this form. Your input has been "
    "kept: compare it with the period as it now stands, then save again."
)
# Accounts described per batch of queries (an export describes many).
BATCH = 500

KIND_STAFF = "staff"
KIND_PORTAL = "portal"
KIND_LABELS = {KIND_STAFF: "Staff", KIND_PORTAL: "Client portal"}
SYSTEM_KIND = "Scheduled jobs"
NOT_RECORDED = "Not in the change history"

SORTS = {
    "person": "Person, A to Z",
    "-person": "Person, Z to A",
    "last_sign_in": "Last sign-in, oldest first",
    "-last_sign_in": "Last sign-in, newest first",
}
DEFAULT_SORT = "person"


# ------------------------------------------------------------------------------ what a row holds


@dataclass(frozen=True)
class Given:
    """A recorded change that gave something: who made it and when."""

    by: str
    username: str
    at: object

    def plain(self):
        when = timezone.localtime(self.at).strftime("%Y-%m-%d %H:%M")
        return f"{self.by} ({self.username}) on {when}"

    def text(self):
        return f"by {self.plain()}"


@dataclass
class Item:
    """One thing an account holds (a role, a capability, a team...), and a line under it."""

    text: str
    note: str = ""
    # Nothing was recorded for the note: shown muted.
    missing: bool = False


@dataclass
class AccountRow:
    account: object
    name: str
    username: str
    kind: str
    active: bool
    deactivated: Given | None = None
    organization: str = ""
    console: list = field(default_factory=list)
    roles: list = field(default_factory=list)
    capabilities: list = field(default_factory=list)
    teams: list = field(default_factory=list)
    portal: str = ""
    second_factor: bool = False
    required: bool = False
    last_sign_in: object = None
    joined: object = None

    @property
    def status(self):
        if self.kind == SYSTEM_KIND:
            return "Nobody signs in"
        return "Active" if self.active else "Deactivated"


def system_account_id():
    from jobs.actor import system_account_id as stored

    return stored()


def kind_of(account, system_id):
    if system_id is not None and account.pk == system_id:
        return SYSTEM_KIND
    return KIND_LABELS[KIND_PORTAL if account.client_id is not None else KIND_STAFF]


def person_name(account):
    return account.get_full_name() or account.username


# ------------------------------------------------------------------------------ the lists


def _capability_permissions(codenames):
    return Permission.objects.filter(content_type__app_label="accounts", codename__in=codenames)


def holds_role(role):
    return Exists(
        get_user_model().groups.through.objects.filter(user_id=OuterRef("pk"), group__name=role)
    )


def holds_capability(codename):
    """Granted to the account or to one of its groups, as accounts.roles.has_capability counts it
    (whatever the account's kind or status: the report shows what is stored)."""
    User = get_user_model()
    permissions = _capability_permissions([codename])
    direct = User.user_permissions.through.objects.filter(
        user_id=OuterRef("pk"), permission__in=permissions
    )
    grouped = User.groups.through.objects.filter(
        user_id=OuterRef("pk"), group__permissions__in=permissions
    )
    return Q(Exists(direct)) | Q(Exists(grouped))


def has_second_factor():
    return Exists(
        Authenticator.objects.filter(
            user_id=OuterRef("pk"), type__in=second_factor.second_factor_types()
        )
    )


def _kind(queryset, kind, system_id):
    if kind == KIND_STAFF:
        queryset = queryset.filter(client__isnull=True)
        return queryset.exclude(pk=system_id) if system_id is not None else queryset
    if kind == KIND_PORTAL:
        return queryset.filter(client__isnull=False)
    return queryset


def shown_name():
    """The name a row shows (person_name): the full name, or the username when it has none."""
    full = Trim(Concat("first_name", Value(" "), "last_name", output_field=CharField()))
    return Coalesce(NullIf(full, Value("")), "username", output_field=CharField())


def ordered(queryset, sort):
    """`sort` order: by the name each row shows, or by the last sign-in, never signed in first
    when oldest first; the account's id breaks ties."""
    if sort in ("person", "-person"):
        keys = (Lower(shown_name()), Lower("username"), F("pk"))
        return queryset.order_by(*(key.desc() if sort == "-person" else key.asc() for key in keys))
    if sort == "last_sign_in":
        return queryset.order_by(F("last_login").asc(nulls_first=True), "pk")
    if sort == "-last_sign_in":
        return queryset.order_by(F("last_login").desc(nulls_last=True), "-pk")
    return ordered(queryset, DEFAULT_SORT)


def accounts(filters, sort=DEFAULT_SORT):
    """Every account the filters (AccountFilter's cleaned data) keep, in `sort` order."""
    system_id = system_account_id()
    queryset = _kind(
        get_user_model().objects.select_related("client"), filters.get("kind"), system_id
    )
    if person := filters.get("person"):
        queryset = queryset.filter(pk=person.pk)
    status = filters.get("status")
    if status == "active":
        queryset = queryset.filter(is_active=True)
    elif status == "deactivated":
        queryset = queryset.filter(is_active=False)
        if system_id is not None:
            queryset = queryset.exclude(pk=system_id)
    if role := filters.get("role"):
        queryset = queryset.filter(holds_role(role))
    if capability := filters.get("capability"):
        queryset = queryset.filter(holds_capability(capability))
    if team := filters.get("team"):
        queryset = queryset.filter(
            Exists(TeamMembership.objects.filter(user_id=OuterRef("pk"), team=team))
        )
    if organization := filters.get("organization"):
        queryset = queryset.filter(client=organization)
    console = filters.get("console")
    if console == "console":
        queryset = queryset.filter(is_staff=True)
    elif console == "superuser":
        queryset = queryset.filter(is_superuser=True)
    factor = filters.get("second_factor")
    if factor == "set_up":
        queryset = queryset.filter(has_second_factor())
    elif factor == "none":
        queryset = queryset.filter(~has_second_factor())
    elif factor == "required_missing":
        queryset = queryset.filter(pk__in=second_factor.covered_among(None)).filter(
            ~has_second_factor()
        )
    return ordered(queryset, sort)


def cutoff(days, now=None):
    """The moment before which an account's last sign-in makes it unused: `days` before now."""
    return (now or timezone.now()) - timedelta(days=days)


def unused_accounts(days, filters, now=None):
    """Active accounts with no sign-in in the last `days` days: a last sign-in before then, or none
    and created before then. Longest unused first."""
    before = cutoff(days, now)
    system_id = system_account_id()
    queryset = _kind(
        get_user_model().objects.select_related("client").filter(is_active=True),
        filters.get("kind"),
        system_id,
    )
    if system_id is not None:
        queryset = queryset.exclude(pk=system_id)
    queryset = queryset.filter(
        Q(last_login__lt=before) | Q(last_login__isnull=True, date_joined__lt=before)
    )
    if role := filters.get("role"):
        queryset = queryset.filter(holds_role(role))
    if capability := filters.get("capability"):
        queryset = queryset.filter(holds_capability(capability))
    return queryset.order_by(F("last_login").asc(nulls_first=True), F("date_joined").asc(), "pk")


# ------------------------------------------------------------------------------ who gave what


def _names(value):
    return {str(item) for item in value} if isinstance(value, list) else None


def _set_changes(event, key):
    """(given, taken) names of `key` ("groups" or "permissions") that a recorded change made, or
    None when it records no list of them before and after."""
    before, after = event.before or {}, event.after or {}
    if event.action == SEED_CAPABILITY:
        capability = after.get("capability")
        if key == "permissions" and isinstance(capability, str) and capability:
            return {f"accounts.{capability}"}, set()
        return None
    new = _names(after.get(key))
    if new is None:
        return None
    if event.action in CREATED and not before:
        old = set()
    else:
        old = _names(before.get(key))
        if old is None:
            return None
    return new - old, old - new


def _flag_change(event, flag):
    """True when a recorded change turned `flag` on, False when it turned it off, else None."""
    before, after = event.before or {}, event.after or {}
    if event.action == SEED_CAPABILITY or not isinstance(after.get(flag), bool):
        return None
    if event.action in CREATED and not before:
        return True if after[flag] else None
    if not isinstance(before.get(flag), bool) or before[flag] == after[flag]:
        return None
    return after[flag]


def _given(event):
    actor = event.actor
    return Given(person_name(actor), actor.username, event.occurred_at)


@dataclass
class _Tracked:
    """What the recorded changes of one account (or group) gave and did not take away since."""

    groups: dict = field(default_factory=dict)
    permissions: dict = field(default_factory=dict)
    flags: dict = field(default_factory=dict)
    deactivated: Given | None = None


def _apply(tracked, event):
    for key in ("groups", "permissions"):
        changes = _set_changes(event, key)
        if changes is None:
            continue
        given, taken = changes
        held = getattr(tracked, key)
        for name in taken:
            held.pop(name, None)
        for name in given:
            held[name] = _given(event)
    for flag in ("is_active", "is_staff", "is_superuser"):
        turned = _flag_change(event, flag)
        if turned is None:
            continue
        if flag == "is_active":
            tracked.deactivated = None if turned else _given(event)
        elif turned:
            tracked.flags[flag] = _given(event)
        else:
            tracked.flags.pop(flag, None)


def _renames(event):
    """(old name, new name) when a recorded change of a group renamed it, else None."""
    if event.action != "group.updated":
        return None
    old, new = (event.before or {}).get("name"), (event.after or {}).get("name")
    if isinstance(old, str) and isinstance(new, str) and old != new:
        return old, new
    return None


def grant_history(account_ids, group_ids):
    """({account id: _Tracked}, {group id: _Tracked}) from the change history, read in order."""
    accounts_tracked = {pk: _Tracked() for pk in account_ids}
    groups_tracked = {pk: _Tracked() for pk in group_ids}
    by_account = {str(pk): pk for pk in account_ids}
    events = (
        AuditEvent.objects.filter(
            Q(
                object_type=ACCOUNT_OBJECT,
                object_id__in=list(by_account),
                action__in=(*ACCOUNT_CHANGES, SEED_CAPABILITY),
            )
            | Q(object_type=GROUP_OBJECT, action__in=GROUP_CHANGES)
        )
        .select_related("actor")
        .order_by("occurred_at", "id")
    )
    for event in events:
        if event.object_type == GROUP_OBJECT:
            renamed = _renames(event)
            if renamed is not None:
                old, new = renamed
                for tracked in accounts_tracked.values():
                    if old in tracked.groups:
                        tracked.groups[new] = tracked.groups.pop(old)
            group_id = int(event.object_id) if event.object_id.isdigit() else None
            if group_id in groups_tracked:
                _apply(groups_tracked[group_id], event)
            continue
        _apply(accounts_tracked[by_account[event.object_id]], event)
    return accounts_tracked, groups_tracked


# ------------------------------------------------------------------------------ describing rows


def _given_item(text, given, how=""):
    if given is None:
        return Item(text, f"{how}; {NOT_RECORDED.lower()}" if how else NOT_RECORDED, missing=True)
    return Item(text, f"{how}, given {given.text()}" if how else f"Given {given.text()}")


def _role_label(name):
    return ROLE_LABELS.get(name, name)


def _group_order(pair):
    """Role groups in accounts.roles.ROLES order, then other groups by name."""
    name = pair[1]
    return (0, ROLES.index(name), "") if name in ROLES else (1, 0, name.lower())


def _group_words(name):
    return f"the {ROLE_LABELS[name]} role" if name in ROLES else f"the group “{name}”"


def _portal_text(account, access, sites):
    if account.client_id is None:
        return ""
    if access is None or not access.applies_to(account):
        return "Only the tickets it raised"
    if access.whole_organization:
        return "Its organization's tickets"
    if sites:
        return "Tickets of " + ", ".join(sites) + ", and those it raised"
    return "Only the tickets it raised"


def describe(rows, *, history=True):
    """AccountRow for each account of `rows` (a list), with what it holds read in a few queries.
    `history`: also who gave each role, capability and admin console access, and when."""
    described = []
    for start in range(0, len(rows), BATCH):
        described.extend(_describe(rows[start : start + BATCH], history))
    return described


def _describe(accounts_list, history):
    User = get_user_model()
    ids = [account.pk for account in accounts_list]
    system_id = system_account_id()
    memberships = {}
    group_ids = set()
    for user_id, group_id, name in User.groups.through.objects.filter(user_id__in=ids).values_list(
        "user_id", "group_id", "group__name"
    ):
        memberships.setdefault(user_id, []).append((group_id, name))
        group_ids.add(group_id)
    capabilities = {}
    for user_id, codename in User.user_permissions.through.objects.filter(
        user_id__in=ids, permission__in=_capability_permissions(list(CAPABILITIES))
    ).values_list("user_id", "permission__codename"):
        capabilities.setdefault(user_id, set()).add(codename)
    group_grants = {}
    for group_id, codename in Group.permissions.through.objects.filter(
        group_id__in=group_ids, permission__in=_capability_permissions(list(CAPABILITIES))
    ).values_list("group_id", "permission__codename"):
        group_grants.setdefault(group_id, set()).add(codename)
    teams = {}
    for membership in (
        TeamMembership.objects.filter(user_id__in=ids)
        .select_related("team")
        .order_by(Lower("team__name"), "team_id")
    ):
        notes = [
            word
            for word, on in (
                ("lead", membership.is_lead),
                ("inactive team", not membership.team.is_active),
            )
            if on
        ]
        teams.setdefault(membership.user_id, []).append(
            Item(membership.team.name, ", ".join(notes).capitalize())
        )
    portal = {
        access.user_id: access
        for access in PortalAccess.objects.filter(user_id__in=ids).prefetch_related("sites")
    }
    with_factor = set(
        Authenticator.objects.filter(
            user_id__in=ids, type__in=second_factor.second_factor_types()
        ).values_list("user_id", flat=True)
    )
    required = set(second_factor.covered_among(ids))
    if history:
        tracked, tracked_groups = grant_history(ids, group_ids)
    else:
        tracked, tracked_groups = {}, {}
    rows = []
    for account in accounts_list:
        mine = tracked.get(account.pk, _Tracked())
        held = sorted(memberships.get(account.pk, ()), key=_group_order)
        role_items = [
            _given_item(_role_label(name), mine.groups.get(name))
            if history
            else Item(_role_label(name))
            for _group_id, name in held
            if name in ROLES
        ]
        capability_items = []
        for codename, label in CAPABILITIES.items():
            if codename in capabilities.get(account.pk, ()):
                given = mine.permissions.get(f"accounts.{codename}")
                capability_items.append(
                    _given_item(label, given, "Directly") if history else Item(label, "Directly")
                )
            for group_id, name in held:
                if codename not in group_grants.get(group_id, ()):
                    continue
                how = f"Through {_group_words(name)}"
                if not history:
                    capability_items.append(Item(label, how))
                    continue
                to_group = tracked_groups.get(group_id, _Tracked()).permissions.get(
                    f"accounts.{codename}"
                )
                member = mine.groups.get(name)
                whose = "role" if name in ROLES else "group"
                parts = [
                    how,
                    f"granted to the {whose} {to_group.text()}"
                    if to_group
                    else f"granted to the {whose}: {NOT_RECORDED.lower()}",
                    f"{whose} given {member.text()}"
                    if member
                    else f"{whose} given: {NOT_RECORDED.lower()}",
                ]
                capability_items.append(
                    Item(label, "; ".join(parts), missing=to_group is None or member is None)
                )
        console_items = []
        for flag, label in CONSOLE:
            if getattr(account, flag):
                given = mine.flags.get(flag)
                console_items.append(_given_item(label, given) if history else Item(label))
        access = portal.get(account.pk)
        sites = (
            sorted(site.name for site in access.sites.all())
            if access is not None and account.client_id is not None
            else []
        )
        rows.append(
            AccountRow(
                account=account,
                name=person_name(account),
                username=account.username,
                kind=kind_of(account, system_id),
                active=account.is_active,
                deactivated=mine.deactivated if not account.is_active else None,
                organization=account.client.name if account.client_id is not None else "",
                console=console_items,
                roles=role_items,
                capabilities=capability_items,
                teams=teams.get(account.pk, []),
                portal=_portal_text(account, access, sites),
                second_factor=account.pk in with_factor,
                required=account.pk in required,
                last_sign_in=account.last_login,
                joined=account.date_joined,
            )
        )
    return rows


# ------------------------------------------------------------------------------ the period


def period():
    """The period as it stands: the saved row, or an unsaved one with none set (revision 0)."""
    return UnusedPeriod.objects.filter(pk=1).first() or UnusedPeriod(pk=1)


@transaction.atomic
def set_period(*, actor, days, expected_revision, reason):
    """Set after how many days without a sign-in an active account is unused, or None for no
    period, with a reason. Returns (period, changed). `expected_revision` is the revision the form
    showed (0 for a period never saved): a change saved in between refuses this one."""
    current = reviewer(actor)
    reason = text_value(reason, "reason", "A reason", TEXT_LIMIT, required=True)
    if days is not None and (
        not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= MOST_DAYS
    ):
        raise ValidationError({"days": f"Choose from 1 to {MOST_DAYS} days."})
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", list(PERIOD_LOCK))
    stored = UnusedPeriod.objects.select_for_update().filter(pk=1).first()
    if stored is None:
        stored = UnusedPeriod(pk=1)
    if expected_revision is None or stored.revision != expected_revision:
        raise ValidationError(STALE_PERIOD)
    if stored.days == days:
        return stored, False
    before = {"days": stored.days}
    stored.days = days
    stored.revision += 1
    stored.save()
    record_event(
        actor=current,
        action=PERIOD_CHANGED,
        obj=stored,
        before=before,
        after={"days": days},
        reason=reason,
    )
    return stored, True


def period_history(limit=10):
    """The newest changes to the period, newest first."""
    return (
        AuditEvent.objects.filter(action=PERIOD_CHANGED)
        .select_related("actor")
        .order_by("-occurred_at", "-id")[:limit]
    )
