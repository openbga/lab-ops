"""Third verification of the assignment lock order (candidate db1fc14). All records are synthetic.

Adopted: V3-01 (introduced by the second resolution, fixed). The verification's other cases
(V3-02 to V3-04) predate this change; they are the acceptance cases of the roadmap item "Review
follow-up: the acting account until commit, and the import's link order", kept outside the suite
until that item is built.

Each test states intended behaviour. The races run every party in its own PostgreSQL session (a
thread with its own connection) with a bounded lock_timeout; a party is paused at a named point,
or holds its transaction open, until the others wait for a lock (seen with pg_blocking_pids, and
where it matters with the statement that waits, from pg_stat_activity) or have finished. The file
uses no helper only the candidate has, so it runs on 791976b and on the base 9fbf6c0 too.
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, connection, connections, transaction
from django.http import Http404
from django.utils import timezone

from accounts.models import User
from clients.models import ClientSite, PortalAccess
from core.models import AuditEvent
from desk.models import ServiceRequest, Status
from links import services as link_services
from links.models import ExternalSystem, LinkAttachment

from .test_contract import new_request

pytestmark = pytest.mark.django_db(transaction=True)

LOCK_WAIT = "8s"  # long enough for the other sessions; a mistake fails, never hangs


@pytest.fixture(autouse=True)
def locmem_mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


# Sessions ---------------------------------------------------------------------------------------


def _outcome_of(exc):
    if isinstance(exc, OperationalError):
        return getattr(exc.__cause__, "sqlstate", None) or "database error"
    if isinstance(exc, ValidationError):
        return "refused"
    if isinstance(exc, PermissionDenied):
        return "denied"
    if isinstance(exc, (ServiceRequest.DoesNotExist, Http404)):
        return "not found"
    return repr(exc)


def _set_lock_timeout(value):
    with connection.cursor() as cursor:
        cursor.execute(f"SET lock_timeout = '{value}'")


def _blocked(pid):
    with connection.cursor() as cursor:
        cursor.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", [pid])
        return cursor.fetchone()[0]


def _blocked_by(pid, other):
    """Whether session `pid` waits for a lock that session `other` holds."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [other, pid])
        return cursor.fetchone()[0]


def _statement(pid):
    """The statement session `pid` is running (or waiting in)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_stat_clear_snapshot()")
        cursor.execute("SELECT query FROM pg_stat_activity WHERE pid = %s", [pid])
        row = cursor.fetchone()
    return row[0] if row else ""


class Session:
    """`work` in its own PostgreSQL session; `outcome`: "done" or what it raised."""

    def __init__(self, work, lock_timeout=LOCK_WAIT):
        self.work, self.lock_timeout = work, lock_timeout
        self.pid, self.outcome, self.error, self.result = None, None, None, None
        self.connected, self.ended = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            _set_lock_timeout(self.lock_timeout)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                self.pid = cursor.fetchone()[0]
            self.connected.set()
            self.result = self.work()
            self.outcome = "done"
        except Exception as exc:  # reported by the assertions
            self.error = exc
            self.outcome = _outcome_of(exc)
        finally:
            self.connected.set()
            self.ended.set()
            connections.close_all()

    def start(self):
        self.thread.start()
        return self

    def waiting_or_ended(self, seconds=8):
        """ "waiting" once the session waits for a lock, "ended" once it has finished."""
        self.connected.wait(timeout=seconds)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.ended.is_set():
                return "ended"
            if self.pid is not None and _blocked(self.pid):
                return "waiting"
            time.sleep(0.02)
        return "neither"

    def join(self, seconds=30):
        self.thread.join(timeout=seconds)
        return self.outcome


class Held:
    """A session that runs `work` in a transaction and keeps it open until `release()`."""

    def __init__(self, work):
        self.holding, self.released = threading.Event(), threading.Event()

        def run():
            with transaction.atomic():
                work()
                self.holding.set()
                self.released.wait(timeout=20)

        self.session = Session(run).start()
        assert self.holding.wait(timeout=10), self.session.error

    def release(self):
        self.released.set()
        return self.session.join()


def run_here(work):
    """`work` in this session, with the races' lock_timeout; its outcome."""
    _set_lock_timeout(LOCK_WAIT)
    try:
        work()
        return "done"
    except Exception as exc:  # reported by the assertions
        return _outcome_of(exc)
    finally:
        _set_lock_timeout("0")


def paused(module, name, during, *, before=True):
    """Patch `module`.`name` to run `during()` once, before or after its first call."""
    real = getattr(module, name)
    fired = []

    def wrapper(*args, **kwargs):
        if before and not fired:
            fired.append(True)
            during()
        result = real(*args, **kwargs)
        if not before and not fired:
            fired.append(True)
            during()
        return result

    return patch.object(module, name, wrapper)


def _race(hold, change, meanwhile):
    """`change` (its own session) waits for a lock `hold` takes in another session, kept open;
    `meanwhile()` commits; the lock is released. Returns (whether the change waited, the
    statement it waited in, its Session once it has ended)."""
    holder = Held(hold)
    session = Session(change).start()
    try:
        state = session.waiting_or_ended()
        statement = _statement(session.pid)
        meanwhile()
    finally:
        assert holder.release() == "done"
    session.join()
    return state, statement, session


def _fresh(ticket):
    return ServiceRequest.objects.get(pk=ticket.pk)


def _deactivate(user):
    def run():
        account = User.objects.get(pk=user.pk)
        account.is_active = False
        account.save()

    return run


def _deactivate_by(user, owner):
    """As the account administration deactivates an account (accounts.admin: changed_by set, then
    a full User.save)."""

    def run():
        account = User.objects.get(pk=user.pk)
        account.is_active = False
        account.changed_by = owner
        account.save()

    return run


def _remove_role(user, role):
    def run():
        User.objects.get(pk=user.pk).groups.remove(Group.objects.get(name=role))

    return run


def _demote(user):
    """The supervisor role taken away; technical staff remains."""

    def run():
        account = User.objects.get(pk=user.pk)
        account.groups.remove(Group.objects.get(name="supervisor"))
        account.groups.add(Group.objects.get_or_create(name="technical")[0])

    return run


def _created_events():
    return AuditEvent.objects.filter(action="request.created").count()


# People and records -----------------------------------------------------------------------------


@pytest.fixture
def lead(make_user):
    """A supervisor, member of no team."""
    return make_user("verify3_lead", "supervisor")


@pytest.fixture
def clerk(make_user):
    return make_user("verify3_clerk", "client_services")


@pytest.fixture
def colleague(make_user):
    return make_user("verify3_colleague", "client_services")


@pytest.fixture
def setup_admin(make_user):
    return make_user("verify3_admin", "admin")


@pytest.fixture
def lis(db):
    return ExternalSystem.objects.get_or_create(
        key="lis", defaults={"name": "Laboratory information system", "kind": "lis"}
    )[0]


@pytest.fixture
def restricted_ticket(agent, privacy_category):
    """A ticket in "Privacy request" (restricted, routed to Quality)."""
    return new_request(agent, privacy_category, client=None)


@pytest.fixture
def two_site_portal(clinic_portal, clinic):
    """The clinic's portal account, granted two of its sites (A and B)."""
    site_a = ClientSite.objects.create(client=clinic, code="SA", name="Site A")
    site_b = ClientSite.objects.create(client=clinic, code="SB", name="Site B")
    access = PortalAccess.objects.create(user=clinic_portal, client=clinic)
    access.sites.set([site_a, site_b])
    return SimpleNamespace(user=clinic_portal, access=access, a=site_a, b=site_b)


# V3-01. Who asks is answered before what is wrong with the ticket --------------------------------


def _link_attachment(actor, ticket, lis, identifier):
    attachment, _created = link_services.add_link(
        actor=actor,
        request_id=ticket.pk,
        system=lis,
        record_type="order",
        identifier=identifier,
    )
    return attachment


def _closing(ticket):
    """What a colleague does while holding the ticket's lock: close it."""

    def close():
        ServiceRequest.objects.select_for_update().get(pk=ticket.pk)
        now = timezone.now()
        ServiceRequest.objects.filter(pk=ticket.pk).update(
            status=Status.CLOSED, resolved_at=now, closed_at=now
        )

    return close


def _race_with_access_change(hold, change, access_change):
    """`change` (its own session) waits for a lock that `hold` takes in another session, kept
    open. `access_change` is then started in its own session: the change holds the account's rows
    (accounts.acting), so it waits behind the change. The lock is released and both are joined.
    Returns (whether the change waited, whether the access change waited, whether it waited behind
    the change, the change's Session, the access change's Session)."""
    holder = Held(hold)
    session = Session(change).start()
    try:
        state = session.waiting_or_ended()
        access = Session(access_change).start()
        access_state = access.waiting_or_ended()
        behind_change = access.pid is not None and _blocked_by(access.pid, session.pid)
    finally:
        assert holder.release() == "done"
    session.join()
    access.join()
    return state, access_state, behind_change, session, access


def _link_work(change, lead, ticket, attachment, lis):
    return {
        "remove": lambda: link_services.remove_link(
            actor=lead, attachment_id=attachment.pk, reason="Wrong record"
        ),
        "show": lambda: link_services.set_shown_to_client(
            actor=lead, attachment_id=attachment.pk, shown=True
        ),
        "add": lambda: link_services.add_link(
            actor=lead,
            request_id=ticket.pk,
            system=lis,
            record_type="order",
            identifier="ORD-TEST-000312",
        ),
    }[change]


@pytest.mark.parametrize("change", ["remove", "show", "add"])
def test_a_link_change_whose_actor_was_deactivated_while_it_waited_is_not_told_it_closed(
    change, lead, restricted_ticket, lis
):
    """A supervisor in no team removes, shows to the client, or adds an order link on a
    restricted ticket (Privacy request, Quality) while a colleague closes it, holding its lock.
    During the wait the supervisor is deactivated by a path that bypasses User.save (a queryset
    update), which the hold of accounts.acting does not make wait. Who asks is answered before
    what is wrong with the ticket (desk.services: "so who asks is answered before what is wrong
    with the choice"): the change reads the account again after its locks and is refused as an
    account that may no longer act (PermissionDenied), or as a ticket they cannot open
    (DoesNotExist). It does not say that the ticket is closed."""
    ticket = restricted_ticket
    attachment = _link_attachment(lead, ticket, lis, "ORD-TEST-000311")
    work = _link_work(change, lead, ticket, attachment, lis)

    def deactivate():
        User.objects.filter(pk=lead.pk).update(is_active=False)

    state, _statement_seen, session = _race(_closing(ticket), work, deactivate)
    assert state == "waiting", state  # for the ticket
    assert session.outcome in ("denied", "not found"), (session.outcome, repr(session.error))
    current = LinkAttachment.objects.get(pk=attachment.pk)
    assert (current.removed_at, current.shown_to_client) == (None, False)
    assert LinkAttachment.objects.filter(request=ticket).count() == 1


@pytest.mark.parametrize("change", ["remove", "show", "add"])
def test_a_demotion_waits_for_a_link_change_under_way_on_a_ticket_a_colleague_closes(
    change, lead, restricted_ticket, lis
):
    """The same change, but the supervisor is demoted to technical staff while it waits for the
    ticket. The change holds the account's roles until it commits (accounts.acting), so the
    demotion waits behind it, and the change is decided by an account that still may open the
    ticket: it is refused on the merits (the ticket is closed: a refusal, not a 404), and nothing
    is saved. The demotion commits after it."""
    ticket = restricted_ticket
    attachment = _link_attachment(lead, ticket, lis, "ORD-TEST-000311")
    work = _link_work(change, lead, ticket, attachment, lis)
    state, demotion_state, behind_change, session, demotion = _race_with_access_change(
        _closing(ticket), work, _demote(lead)
    )
    assert state == "waiting", state  # for the ticket
    assert (demotion_state, behind_change) == ("waiting", True)
    assert demotion.outcome == "done", repr(demotion.error)
    assert session.outcome == "refused", (session.outcome, repr(session.error))
    current = LinkAttachment.objects.get(pk=attachment.pk)
    assert (current.removed_at, current.shown_to_client) == (None, False)
    assert LinkAttachment.objects.filter(request=ticket).count() == 1
    roles = set(User.objects.get(pk=lead.pk).groups.values_list("name", flat=True))
    assert roles == {"technical"}


@pytest.mark.parametrize("change", ["remove", "show", "add"])
def test_a_link_change_whose_actor_was_demoted_before_it_began_is_not_told_it_closed(
    change, lead, restricted_ticket, lis
):
    """The supervisor was demoted to technical staff (the ticket is no longer one they may open)
    and the demotion committed before the change began, while the caller still holds the account
    as it was. Who asks is answered before what is wrong with the ticket: the change is refused as
    a ticket they cannot open (DoesNotExist, the pages' 404), even though a colleague closes the
    ticket meanwhile. It does not say that the ticket is closed."""
    ticket = restricted_ticket
    attachment = _link_attachment(lead, ticket, lis, "ORD-TEST-000311")
    work = _link_work(change, lead, ticket, attachment, lis)
    _demote(lead)()
    _state, _statement_seen, session = _race(_closing(ticket), work, lambda: None)
    assert session.outcome == "not found", (session.outcome, repr(session.error))
    current = LinkAttachment.objects.get(pk=attachment.pk)
    assert (current.removed_at, current.shown_to_client) == (None, False)
    assert LinkAttachment.objects.filter(request=ticket).count() == 1


# Added by the author next to V3-01: a form correction has the same order. -----------------------


def _closable_form_ticket(agent, privacy_category, quality):
    from request_forms import definition
    from request_forms.models import FieldKind, FieldValue, RequestTemplate
    from request_forms.services import create_request_with_form

    template = RequestTemplate.objects.create(
        key="correction-neighbour", name="Correction form", category=privacy_category
    )
    template.fields.create(key="note", label="Note", kind=FieldKind.SHORT_TEXT)
    ticket = create_request_with_form(
        actor=agent,
        template=template,
        expected_definition=definition.token(template),
        values={"note": "first"},
        first_entry_kind="note",
        channel="phone",
        team=quality,
    )
    row = FieldValue.objects.get(filled_form__request=ticket, superseded_at__isnull=True)

    def correction(actor):
        from request_forms.services import correct_values

        return lambda: correct_values(
            actor=actor,
            request=ticket,
            expected_definition=definition.token(template),
            values={"note": "second"},
            reason="Typo",
            expected={"note": row.pk},
        )

    return ticket, row, correction


def _unchanged_value(ticket, row):
    from request_forms.models import FieldValue

    current = FieldValue.objects.get(filled_form__request=ticket, superseded_at__isnull=True)
    return (current.pk, current.value) == (row.pk, row.value)


def test_a_form_correction_whose_actor_was_deactivated_while_it_waited_is_not_told_it_closed(
    lead, agent, privacy_category, quality
):
    """A supervisor in no team corrects a form value on a restricted ticket while a colleague
    closes it, holding its lock; during the wait the supervisor is deactivated by a path that
    bypasses User.save (a queryset update), which the hold of accounts.acting does not make wait.
    Who asks is answered before what is wrong with the ticket: the correction reads the account
    again after its locks and is refused as an account that may no longer act (PermissionDenied)
    or a ticket they cannot open (DoesNotExist), never told that the ticket is closed, and
    nothing is saved."""
    ticket, row, correction = _closable_form_ticket(agent, privacy_category, quality)

    def deactivate():
        User.objects.filter(pk=lead.pk).update(is_active=False)

    state, _statement_seen, session = _race(_closing(ticket), correction(lead), deactivate)
    assert state == "waiting", state  # for the ticket
    assert session.outcome in ("denied", "not found"), (session.outcome, repr(session.error))
    assert _unchanged_value(ticket, row)


def test_a_demotion_waits_for_a_form_correction_under_way_on_a_ticket_a_colleague_closes(
    lead, agent, privacy_category, quality
):
    """The supervisor is demoted to technical staff while the correction waits for the ticket.
    The correction holds the account's roles until it commits (accounts.acting), so the demotion
    waits behind it, and the correction is decided by an account that still may open the ticket:
    it is refused on the merits (the ticket is closed: a refusal, not a 404), and nothing is
    saved. The demotion commits after it."""
    ticket, row, correction = _closable_form_ticket(agent, privacy_category, quality)
    state, demotion_state, behind_change, session, demotion = _race_with_access_change(
        _closing(ticket), correction(lead), _demote(lead)
    )
    assert state == "waiting", state  # for the ticket
    assert (demotion_state, behind_change) == ("waiting", True)
    assert demotion.outcome == "done", repr(demotion.error)
    assert session.outcome == "refused", (session.outcome, repr(session.error))
    assert _unchanged_value(ticket, row)
    roles = set(User.objects.get(pk=lead.pk).groups.values_list("name", flat=True))
    assert roles == {"technical"}


def test_a_form_correction_whose_actor_was_demoted_before_it_began_is_not_told_it_closed(
    lead, agent, privacy_category, quality
):
    """The supervisor was demoted to technical staff and the demotion committed before the
    correction began, while the caller still holds the account as it was. Who asks is answered
    before what is wrong with the ticket: the correction is refused as a ticket they cannot open
    (DoesNotExist), never told that the ticket is closed, and nothing is saved."""
    ticket, row, correction = _closable_form_ticket(agent, privacy_category, quality)
    _demote(lead)()
    _state, _statement_seen, session = _race(_closing(ticket), correction(lead), lambda: None)
    assert session.outcome == "not found", (session.outcome, repr(session.error))
    assert _unchanged_value(ticket, row)
