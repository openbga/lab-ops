"""Changes to client organizations and their contacts. Each change locks the record, rechecks its
revision, and records an attributed audit event in the same transaction."""

import json

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, connection, transaction
from django.forms.models import model_to_dict

from accounts.acting import hold_actor
from accounts.roles import ROLES
from core.audit import record_event, snapshot

from .models import Client, ClientContact, ClientSite, PortalAccess, Responsibility
from .permissions import require_client_manager

CLIENT_FIELDS = (
    "name",
    "organization_type",
    "parent",
    "account_owner",
    "account_number",
    "email",
    "phone",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
    "notes",
)
CONTACT_FIELDS = ("name", "role", "email", "phone", "responsibilities", "is_active")
SITE_FIELDS = (
    "code",
    "name",
    "kind",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
    "phone",
    "instructions",
    "is_active",
)


def _reason(reason):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required."})
    return reason


def _strip(obj, names):
    for name in names:
        setattr(obj, name, (getattr(obj, name) or "").strip())


OWNER_REFUSED = "Choose an active laboratory staff account as the account owner."


def hold_owners(owners):
    """Lock the accounts named as organizations' owners, in id order, until the transaction
    ends: each account FOR NO KEY UPDATE, then its roles (held FOR KEY SHARE, as an assignment
    holds its assignee's, desk.services._held_assignee). Returns {id: (account or None, roles)}
    as read under those locks. Nothing is decided here (_held_owner).

    A deactivation (accounts.models.User.save) locks the account FOR NO KEY UPDATE, and removing
    a role deletes a held membership: either committed before and the reading sees it, or it
    waits for this change. Taken after the directory lock and before any organization's row: a
    ticket created naming the owner as its assignee holds the account and then checks the
    organization at its COMMIT, and an import holds every owner its file names before reading
    the rows (dataimport.importing._hold_named_accounts), which lock organizations one by one.
    Taking them again in the same transaction does not wait."""
    from django.contrib.auth import get_user_model

    from accounts.roles import held_roles

    user_model = get_user_model()
    held = {}
    for pk in sorted({getattr(owner, "pk", owner) for owner in owners if owner is not None}):
        current = user_model.objects.select_for_update(no_key=True).filter(pk=pk).first()
        held[pk] = (current, held_roles(current, mode="key share") if current else set())
    return held


def _held_owner(owner):
    """`owner` as it stands now, held until the transaction ends (hold_owners), if it may own an
    organization's account: an active staff account with a staff role."""
    current, roles = hold_owners([owner])[owner.pk]
    if current is None or not (current.is_active and roles.intersection(ROLES)):
        raise ValidationError({"account_owner": OWNER_REFUSED})
    return current


# The advisory lock of the organization directory (PostgreSQL's two-key form; the first key is
# this lock's own, as accounts.acting.ACCESS_GATE is the access gates').
DIRECTORY_LOCK = (19554, 0)
CHANGED = "This client organization changed. Reload and review the current record."


def hold_directory():
    """Take the organization directory lock until the transaction ends, before the change locks
    any organization. Taken, after holding the actor, by whatever changes how an import finds or
    checks records, or may lock several organizations in an order of its own:

    - an import, which finds organizations by account code, checks new ones' names, finds sites
      by code and contacts by email, and locks organizations in the file's order
      (dataimport.importing._importing);
    - creating an organization;
    - changing an organization's name, account code or parent (its own row, then the new
      parent's chain upwards, _check_parent);
    - changing a site's code;
    - adding a contact, or changing a contact's email or name (a contact without an email is
      found by name);
    - the demonstration seed, after holding the accounts it acts as.

    One of them at a time: the second waits for the first to commit, then reads and locks what it
    needs, so neither ends in a deadlock and an import reads names, codes and emails as the change
    before it left them. Changing an organization's other details, taking it out of its group, a
    site's or a contact's other details and links do not take it. Taking it again in the same
    transaction does not wait."""
    if not connection.in_atomic_block:
        raise RuntimeError("The directory lock is held for a transaction; open one first.")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", list(DIRECTORY_LOCK))


def _held_manager(actor):
    """The acting account held until the transaction ends (accounts.acting.hold_actor), before
    anything else the change locks, and its role checked again on that reading: the change can
    wait for the directory lock, and a role taken away meanwhile then refuses it."""
    current = hold_actor(actor)
    if current is None:
        raise PermissionDenied("Your role does not permit this action.")
    require_client_manager(current)
    return current


def _save_unique(obj):
    """Save `obj`; a unique value taken by another change meanwhile (a write that did not wait
    for the directory lock) is refused with the constraint's own message, as the form's
    validation would refuse it, not as a database error, even if that change has let it go
    again by then."""
    try:
        with transaction.atomic():
            obj.save()
    except IntegrityError as error:
        name = getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)
        for constraint in type(obj)._meta.constraints:
            if name is not None and constraint.name == name:
                raise ValidationError(constraint.get_violation_error_message()) from error
        raise


def _check_parent(obj):
    """Refuse a parent that is the organization itself or one of its descendants. The chain is
    walked upwards under row locks, so two concurrent changes cannot close a loop between them;
    the caller holds the directory lock (hold_directory), so they wait for each other one way."""
    if obj.parent_id is None:
        return
    seen = set()
    node_id = obj.parent_id
    while node_id is not None:
        if obj.pk is not None and node_id == obj.pk:
            raise ValidationError(
                {"parent": "This parent would make the organization part of its own group."}
            )
        if node_id in seen:  # an existing loop: never extend it
            raise ValidationError({"parent": "The chosen parent's group is not consistent."})
        seen.add(node_id)
        node_id = (
            Client.objects.select_for_update()
            .filter(pk=node_id)
            .values_list("parent_id", flat=True)
            .first()
        )


def _normalise_responsibilities(contact):
    chosen = set(contact.responsibilities or ())
    order = Responsibility.values
    unknown = chosen.difference(order)
    if unknown:
        raise ValidationError({"responsibilities": "Choose responsibilities from the list."})
    contact.responsibilities = [code for code in order if code in chosen]


@transaction.atomic
def create_client(*, actor, reason="", **data):
    require_client_manager(actor)  # who asks, before anything is locked
    actor = _held_manager(actor)
    hold_directory()  # before the parent chain's locks and the account code's uniqueness check
    if data.get("account_owner") is not None:  # held before any organization's row
        data = {**data, "account_owner": _held_owner(data["account_owner"])}
    obj = Client(**{k: v for k, v in data.items() if k in CLIENT_FIELDS})
    _strip(obj, ("name", "account_number", "phone", "city", "postal_code"))
    obj.full_clean()
    _check_parent(obj)
    _save_unique(obj)
    record_event(
        actor=actor, action="client.created", obj=obj, after=snapshot(obj), reason=reason.strip()
    )
    return obj


DIRECTORY_FIELDS = ("parent_id", "name", "account_number")


def _directory_changes(found, data):
    """Whether saving `data` changes how the organization `found` (its parent, name and account
    code) is grouped, found or checked by an import: a new parent, or another name or account
    code as the save stores them (stripped, so a stored value with spaces counts too)."""
    parent = data.get("parent")
    if parent is not None and parent.pk != found["parent_id"]:
        return True
    return any(
        (data.get(name, found[name]) or "").strip() != (found[name] or "")
        for name in ("name", "account_number")
    )


@transaction.atomic
def update_client(*, actor, client_id, expected_revision, reason, **data):
    require_client_manager(actor)  # who asks, before anything is locked
    reason = _reason(reason)
    actor = _held_manager(actor)
    # Read without a lock to decide whether the directory lock and a new owner's lock are needed,
    # which come before the organization's lock. Every change made through these services
    # advances the revision, so when the revision below is the one the form was drawn from, this
    # reading was of that state.
    found = Client.objects.filter(pk=client_id).values(*DIRECTORY_FIELDS, "account_owner_id")
    found = found.first()
    directory = found is not None and _directory_changes(found, data)
    if directory:
        hold_directory()
    owner = data.get("account_owner")
    appointing = found is not None and owner is not None and owner.pk != found["account_owner_id"]
    if appointing:  # the new owner, held and checked as it stands now
        data = {**data, "account_owner": _held_owner(owner)}
    obj = Client.objects.select_for_update().get(pk=client_id)
    if obj.revision != expected_revision:
        raise ValidationError(CHANGED)
    if not directory and _directory_changes(
        {name: getattr(obj, name) for name in DIRECTORY_FIELDS}, data
    ):
        # Changed without a new revision (a write that bypassed these services) so that this
        # save would now need the directory lock: refused rather than saved without it. Other
        # such writes are not detected.
        raise ValidationError(CHANGED)
    before = snapshot(obj)
    for name in (*CLIENT_FIELDS, "is_active"):
        if name in data:
            setattr(obj, name, data[name])
    _strip(obj, ("name", "account_number", "phone", "city", "postal_code"))
    if obj.parent_id != before["parent"]:
        _check_parent(obj)
    obj.full_clean()
    if snapshot(obj) == before:
        raise ValidationError("Change at least one detail before saving.")
    if obj.account_owner_id not in (before["account_owner"], None) and not appointing:
        # The stored owner changed without a new revision (a write that bypassed these
        # services), so this save would set an owner it did not hold: refused as changed.
        raise ValidationError(CHANGED)
    obj.revision += 1
    _save_unique(obj)
    record_event(
        actor=actor,
        action="client.updated",
        obj=obj,
        before=before,
        after=snapshot(obj),
        reason=reason,
    )
    return obj


def _contact_state(contact, site_ids):
    """Audited shape of a contact: its fields, and its served sites as sorted ids."""
    state = json.loads(json.dumps(model_to_dict(contact, exclude=["sites"]), cls=DjangoJSONEncoder))
    state["sites"] = sorted(site_ids)
    return state


def _served_sites(contact, sites):
    """Ids of `sites` after checking each belongs to the contact's organization. Inactive sites
    the contact already serves may stay; new ones must be active. The sites are held FOR SHARE
    until the transaction ends and checked as read under that lock, so a site deactivated or
    moved while the change waited is seen, and one changed afterwards waits for it."""
    from jobs.locks import share_lock

    wanted = [site.pk for site in sites or ()]
    share_lock(ClientSite, wanted)
    sites = list(ClientSite.objects.filter(pk__in=wanted).order_by("pk"))
    if len(sites) != len(set(wanted)):
        raise ValidationError({"sites": "Choose sites of this contact's organization."})
    current = set(contact.sites.values_list("pk", flat=True)) if contact.pk else set()
    for site in sites:
        if site.client_id != contact.client_id:
            raise ValidationError({"sites": "Choose sites of this contact's organization."})
        if not site.is_active and site.pk not in current:
            raise ValidationError({"sites": f"{site.name} is inactive."})
    return {site.pk for site in sites}


@transaction.atomic
def add_contact(*, actor, client_id, sites=(), **data):
    require_client_manager(actor)  # who asks, before anything is locked
    actor = _held_manager(actor)
    hold_directory()  # an import finds contacts by email
    client = Client.objects.select_for_update().get(pk=client_id)
    contact = ClientContact(client=client, **{k: v for k, v in data.items() if k in CONTACT_FIELDS})
    _strip(contact, ("name", "role", "email", "phone"))
    _normalise_responsibilities(contact)
    contact.full_clean()
    site_ids = _served_sites(contact, sites)
    contact.save()
    contact.sites.set(site_ids)
    record_event(
        actor=actor,
        action="client_contact.added",
        obj=contact,
        after=_contact_state(contact, site_ids),
    )
    return contact


@transaction.atomic
def update_contact(*, actor, contact_id, expected_revision, reason, sites=None, **data):
    """Change a contact. `sites` None leaves the served sites as they are."""
    require_client_manager(actor)  # who asks, before anything is locked
    reason = _reason(reason)
    actor = _held_manager(actor)
    # A new email or name takes the directory lock first (an import finds contacts by email,
    # and one without an email by name); decided from a reading before the locks, as in
    # update_client.
    found = ClientContact.objects.filter(pk=contact_id).values("email", "name").first()
    directory = found is not None and _contact_identity_changes(found, data)
    if directory:
        hold_directory()
    contact = ClientContact.objects.select_for_update().get(pk=contact_id)
    if contact.revision != expected_revision:
        raise ValidationError("This contact changed. Reload and review the current record.")
    if not directory and _contact_identity_changes(
        {"email": contact.email, "name": contact.name}, data
    ):
        raise ValidationError("This contact changed. Reload and review the current record.")
    current = set(contact.sites.values_list("pk", flat=True))
    before = _contact_state(contact, current)
    for name in CONTACT_FIELDS:
        if name in data:
            setattr(contact, name, data[name])
    _strip(contact, ("name", "role", "email", "phone"))
    _normalise_responsibilities(contact)
    contact.full_clean()
    site_ids = current if sites is None else _served_sites(contact, sites)
    after = _contact_state(contact, site_ids)
    if after == before:
        raise ValidationError("Change at least one detail before saving.")
    contact.revision += 1
    contact.save()
    contact.sites.set(site_ids)
    after["revision"] = contact.revision
    record_event(
        actor=actor,
        action="client_contact.updated",
        obj=contact,
        before=before,
        after=after,
        reason=reason,
    )
    return contact


@transaction.atomic
def create_site(*, actor, client_id, **data):
    """Add a site to an organization. Its code, when given, is unique within the organization
    whatever its case (database constraint client_site_code)."""
    require_client_manager(actor)  # who asks, before anything is locked
    actor = _held_manager(actor)
    client = Client.objects.select_for_update().get(pk=client_id)
    site = ClientSite(client=client, **{k: v for k, v in data.items() if k in SITE_FIELDS})
    _strip(site, ("code", "name", "phone", "city", "postal_code"))
    site.full_clean()
    _save_unique(site)
    record_event(actor=actor, action="site.created", obj=site, after=snapshot(site))
    return site


def _contact_identity_changes(found, data):
    return any(_value_changes(found[name], data, name) for name in ("email", "name"))


def _value_changes(stored, data, name):
    """Whether saving `data` stores another value of `name` than `stored` (stripped, as saved)."""
    return (data.get(name, stored) or "").strip() != (stored or "")


@transaction.atomic
def update_site(*, actor, site_id, expected_revision, reason, **data):
    """Change or deactivate a site. Deactivating keeps its requests, contacts and history; it is
    only no longer offered for new requests and portal scopes."""
    require_client_manager(actor)  # who asks, before anything is locked
    reason = _reason(reason)
    actor = _held_manager(actor)
    # A new site code takes the directory lock first (an import finds sites by code); decided
    # from a reading before the locks, as in update_client.
    found = ClientSite.objects.filter(pk=site_id).values_list("code", flat=True).first()
    directory = found is not None and _value_changes(found, data, "code")
    if directory:
        hold_directory()
    site = ClientSite.objects.select_for_update().get(pk=site_id)
    if site.revision != expected_revision:
        raise ValidationError("This site changed. Reload and review the current record.")
    if not directory and _value_changes(site.code, data, "code"):
        raise ValidationError("This site changed. Reload and review the current record.")
    before = snapshot(site)
    for name in SITE_FIELDS:
        if name in data:
            setattr(site, name, data[name])
    _strip(site, ("code", "name", "phone", "city", "postal_code"))
    site.full_clean()
    if snapshot(site) == before:
        raise ValidationError("Change at least one detail before saving.")
    site.revision += 1
    _save_unique(site)
    record_event(
        actor=actor,
        action="site.updated",
        obj=site,
        before=before,
        after=snapshot(site),
        reason=reason,
    )
    return site


def portal_access_state(access, site_ids=None):
    """The audited shape of one account's portal access (ids and flags, no names)."""
    if access is None:
        return {"client": None, "contact": None, "whole_organization": False, "sites": []}
    if site_ids is None:
        site_ids = access.sites.values_list("pk", flat=True) if access.pk else ()
    return {
        "client": access.client_id,
        "contact": access.contact_id,
        "whole_organization": access.whole_organization,
        "sites": sorted(site_ids),
    }


@transaction.atomic
def set_portal_access(
    *,
    actor,
    user_id,
    contact=None,
    whole_organization=False,
    sites=(),
    expected_client_id,
    expected_revision=None,
    reason="",
):
    """Set the linked contact and scope of one client portal account.

    `expected_client_id` is the organization the caller decided for (the form's organization).
    After the account is locked, a different current organization refuses the change: an
    account moved meanwhile never receives a grant meant for its previous organization.

    The account must be a portal account (`client_id` set); the contact and every site must
    belong to the same organization. Scope is the whole organization, or one or more listed
    sites (new ones active); listed sites are cleared when the scope is the whole organization.
    Changing existing access needs a reason and the revision the form was opened at (a missing
    revision is refused like a stale one); setting it the first time does not. Audited as
    `portal_access.set` with ids and flags only.
    """
    from accounts.acting import close_access
    from accounts.models import User

    require_client_manager(actor)
    close_access([user_id])  # waits for the account's changes under way (accounts.acting)
    account = User.objects.select_for_update().get(pk=user_id)
    if account.client_id is None:
        raise ValidationError("Portal access is for client portal accounts only.")
    _check_organization(account, expected_client_id)
    if contact is not None and contact.client_id != account.client_id:
        raise ValidationError({"contact": "Choose a contact of this account's organization."})
    access = PortalAccess.objects.select_for_update().filter(user=account).first()
    before = portal_access_state(access)
    current = set(before["sites"])
    if access is None:
        if expected_revision not in (None, 0):
            raise ValidationError(
                "This account's portal access changed. Reload and review the current access."
            )
        access = PortalAccess(user=account)
        reason = (reason or "").strip()
    else:
        if expected_revision != access.revision:  # missing counts as stale: never overwrite
            raise ValidationError(
                "This account's portal access changed. Reload and review the current access."
            )
        reason = _reason(reason)
    site_ids = set()
    if not whole_organization:
        for site in sites or ():
            if site.client_id != account.client_id:
                raise ValidationError({"sites": "Choose sites of this account's organization."})
            if not site.is_active and site.pk not in current:
                raise ValidationError({"sites": f"{site.name} is inactive."})
            site_ids.add(site.pk)
        if not site_ids:
            raise ValidationError(
                {"sites": "Choose at least one site, or give access to the whole organization."}
            )
    access.client_id = account.client_id
    access.contact = contact
    access.whole_organization = bool(whole_organization)
    after = portal_access_state(access, site_ids)
    if access.pk is not None:
        if after == before:
            raise ValidationError("Change at least one detail before saving.")
        access.revision += 1
    access.save()
    access.sites.set(site_ids)
    record_event(
        actor=actor,
        action="portal_access.set",
        obj=access,
        before=before,
        after={**after, "user": account.pk},
        reason=reason,
    )
    return access


ORGANIZATION_CHANGED = "The account's organization changed; portal access was reset."
MOVED = "This account moved to another organization. Reload it and decide its access there."


def _check_organization(account, expected_client_id):
    """Refuse a portal-access write when the locked account is no longer in the organization
    the caller decided for."""
    if account.client_id != expected_client_id:
        raise ValidationError(MOVED)


@transaction.atomic
def reset_portal_access(
    *, actor, user_id, expected_client_id, before=None, reason=ORGANIZATION_CHANGED
):
    """Take away every grant of one account's portal access: no contact, no sites, not the whole
    organization, and the access now belongs to the account's current organization (none for an
    account that is no longer a client account). The account then sees only the requests it
    raised until staff set its access again. Audited as `portal_access.reset`.

    Called when an account's organization changes (the account administration, and
    clients.signals for any other save), with `expected_client_id` the organization it moved to.
    The database trigger of clients/0007 has usually emptied the access already, in the same
    update; `before` is then the state the signal captured before the update, so the event still
    records what was taken away. Returns the access, or None when there was none or nothing to
    reset. Not a staff action of its own, so it checks no role: the caller's own authorization
    covers the change of organization.
    """
    from accounts.acting import close_access
    from accounts.models import User

    close_access([user_id])  # waits for the account's changes under way (accounts.acting)
    account = User.objects.select_for_update().get(pk=user_id)
    _check_organization(account, expected_client_id)
    access = PortalAccess.objects.select_for_update().filter(user=account).first()
    if access is None:
        return None
    if before is None:
        before = portal_access_state(access)
    empty = {
        "client": account.client_id,
        "contact": None,
        "whole_organization": False,
        "sites": [],
    }
    if before == empty:
        return None
    if portal_access_state(access) != empty:  # not yet emptied by the trigger
        access.client_id = account.client_id
        access.contact = None
        access.whole_organization = False
        access.revision += 1
        access.save()
        access.sites.clear()
    record_event(
        actor=actor,
        action="portal_access.reset",
        obj=access,
        before=before,
        after={**empty, "user": account.pk},
        reason=reason,
    )
    return access
