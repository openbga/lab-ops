"""Regression tests for the cases next to the round 1 fixes of the assignment lock order (R03,
R04, R05). All records are synthetic.

Each test states the rule it holds. The races run every party in its own PostgreSQL session (a
thread with its own connection) with a bounded lock_timeout; the change under test waits for a
lock another session holds, and is released once that session commits.
"""

import threading
import time
from unittest.mock import patch

import pytest
from django import forms
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, connection, connections, transaction
from django.http import Http404
from django.test import Client
from django.urls import reverse

from accounts.models import User
from attachments.models import Attachment
from attachments.services import add_entry_with_files, withdraw_attachment
from attachments.tests.conftest import png_file
from clients.models import PortalAccess
from core.models import AuditEvent, LabModule
from desk import services
from desk.models import EntryKind, RequestEntry, ServiceRequest, Status
from links import services as link_services
from links.models import ExternalSystem, LinkAttachment
from nonconformance import services as nc_services
from nonconformance.models import TicketLink
from request_forms import definition
from request_forms.models import FieldKind, FieldValue, RequestTemplate
from request_forms.services import correct_values, create_request_with_form

from .test_contract import new_request, selection, set_status

pytestmark = pytest.mark.django_db(transaction=True)

LOCK_WAIT = "8s"  # a mistake fails after this, never hangs


@pytest.fixture(autouse=True)
def locmem_mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


# Sessions ---------------------------------------------------------------------------------------


def _set_lock_timeout(value):
    with connection.cursor() as cursor:
        cursor.execute(f"SET lock_timeout = '{value}'")


def _blocked(pid):
    with connection.cursor() as cursor:
        cursor.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", [pid])
        return cursor.fetchone()[0]


def _sqlstate(error):
    if isinstance(error, OperationalError):
        return getattr(error.__cause__, "sqlstate", None) or "database error"
    return None


class Session:
    """`work` in its own PostgreSQL session; `error`: what it raised."""

    def __init__(self, work, lock_timeout=LOCK_WAIT):
        self.work, self.lock_timeout = work, lock_timeout
        self.pid, self.error, self.result, self.done = None, None, None, False
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
            self.done = True
        except Exception as exc:  # reported by the assertions
            self.error = exc
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
        return self

    @property
    def outcome(self):
        if self.done:
            return "done"
        return _sqlstate(self.error) or repr(self.error)


def paused(name, during, *, before):
    """Patch desk.services.`name` to run `during()` once, before or after its first call."""
    real = getattr(services, name)
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

    return patch.object(services, name, wrapper)


def run_here(work):
    """`work` in this session with a bounded lock_timeout; the exception it raised, or None."""
    _set_lock_timeout(LOCK_WAIT)
    try:
        work()
        return None
    except Exception as exc:  # reported by the assertions
        return exc
    finally:
        _set_lock_timeout("0")


def _fresh(ticket):
    return ServiceRequest.objects.get(pk=ticket.pk)


# People and tickets -----------------------------------------------------------------------------


@pytest.fixture
def tech(make_user, join, accessioning):
    """A technician (no supervisor role) of Accessioning, not of Quality."""
    user = make_user("nb_technician", "technical")
    join(user, accessioning)
    return user


@pytest.fixture
def lead(make_user):
    return make_user("nb_lead", "supervisor")


@pytest.fixture
def ticket(agent, category, quality):
    """An ordinary ticket of Quality: a technician of Accessioning reads it while its category is
    unrestricted and no longer once it is recategorized into a restricted one."""
    return new_request(agent, category, team=quality, client=None)


@pytest.fixture
def portal(clinic_portal, clinic):
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    return clinic_portal


def _measure(ticket, **extra):
    """What a refused change must leave as it found it."""

    def measure():
        return {
            "revision": ServiceRequest.objects.get(pk=ticket.pk).revision,
            "audit": AuditEvent.objects.count(),
            "entries": RequestEntry.objects.count(),
            **{name: count() for name, count in extra.items()},
        }

    return measure


def race(ticket, recategorizer, target, change, measure):
    """`change` waits for the ticket's lock while `recategorizer` holds an open recategorization
    of the ticket into `target`; the recategorization then commits. Returns (the change's session,
    what `measure` read inside the recategorization, what it reads after both ended)."""
    holding, release = threading.Event(), threading.Event()
    baseline = {}

    def recategorize():
        with transaction.atomic():
            current = _fresh(ticket)
            services.update_properties(
                actor=recategorizer,
                request=current,
                expected_revision=current.revision,
                priority=current.priority,
                category=target,
                reason="Moved",
            )
            baseline.update(measure())
            holding.set()
            release.wait(timeout=20)

    holder = Session(recategorize).start()
    assert holding.wait(timeout=10), holder.error
    session = Session(change).start()
    try:
        state = session.waiting_or_ended()
    finally:
        release.set()
    holder.join()
    session.join()
    assert holder.done, holder.error
    assert state == "waiting", f"the change did not wait for the ticket's lock ({state})"
    return session, baseline, measure()


READABLE, RESTRICTED = "readable", "restricted"


@pytest.fixture
def target(request, sample_category, privacy_category):
    return {READABLE: sample_category, RESTRICTED: privacy_category}[request.param]


both = pytest.mark.parametrize("target", [READABLE, RESTRICTED], indirect=True)
only_readable = pytest.mark.parametrize("target", [READABLE], indirect=True)


def _assert_missing(session, baseline, after, kind=ServiceRequest.DoesNotExist):
    assert type(session.error) is kind, session.outcome
    assert after == baseline, "a refused change saved something"


# R05. A change that waited for a ticket being recategorized, at every site -------------------


@both
def test_a_staff_note_waiting_for_a_recategorization_is_missing_only_if_hidden(
    target, ticket, tech, lead
):
    """Adding a note (desk.services.add_entry, staff): when the ticket is recategorized while the
    note waits, a technician who still reads it is refused as changed (STALE), not told "No such
    ticket"; one who no longer reads it (restricted category of another team) is told the ticket
    is missing. Nothing is saved either way."""
    seen = _fresh(ticket)
    session, baseline, after = race(
        ticket,
        lead,
        target,
        lambda: services.add_entry(
            actor=tech,
            request=seen,
            kind=EntryKind.NOTE,
            body="Called the clinic back.",
            expected_revision=seen.revision,
        ),
        _measure(ticket),
    )
    if target.is_restricted:
        _assert_missing(session, baseline, after)
    else:
        assert isinstance(session.error, ValidationError), session.outcome
        assert session.error.messages == [services.STALE]
        assert after == baseline


@both
def test_a_status_change_waiting_for_a_recategorization_is_missing_only_if_hidden(
    target, ticket, tech, lead
):
    """Changing the status (desk.services.change_status): refused as changed (STALE) for a
    technician who still reads the recategorized ticket, missing for one who no longer does; the
    status and revision stay as the recategorization left them."""
    seen = _fresh(ticket)
    session, baseline, after = race(
        ticket,
        lead,
        target,
        lambda: services.change_status(
            actor=tech, request=seen, expected_revision=seen.revision, status=Status.WAITING_CLIENT
        ),
        _measure(ticket),
    )
    if target.is_restricted:
        _assert_missing(session, baseline, after)
    else:
        assert isinstance(session.error, ValidationError), session.outcome
        assert session.error.messages == [services.STALE]
        assert after == baseline
    assert _fresh(ticket).status == seen.status


@both
def test_a_portal_reply_waiting_for_a_recategorization_is_missing_only_if_hidden(
    target, agent, category, quality, clinic, portal, lead
):
    """A portal reply (desk.services.add_entry, portal): the portal scope joins the category too.
    A ticket recategorized into another client-readable category while the reply waited is
    refused as changed (STALE); one moved into a restricted category the account did not raise is
    missing for it. Nothing is saved either way."""
    ticket = new_request(agent, category, team=quality, client=clinic)
    seen = _fresh(ticket)
    session, baseline, after = race(
        ticket,
        lead,
        target,
        lambda: services.add_entry(
            actor=portal,
            request=seen,
            kind=EntryKind.REPLY,
            body="Thank you, we will wait.",
            expected_revision=seen.revision,
        ),
        _measure(ticket),
    )
    if target.is_restricted:
        _assert_missing(session, baseline, after)
    else:
        assert isinstance(session.error, ValidationError), session.outcome
        assert session.error.messages == [services.STALE]
        assert after == baseline


@pytest.fixture
def nc_module(db):
    LabModule.objects.update_or_create(
        key="nonconformance", defaults={"is_enabled": True, "revision": 1}
    )


@pytest.fixture
def owned_nc(nc_module, lead, tech):
    return nc_services.raise_nonconformance(
        actor=lead,
        title="Freezer excursion",
        description="The freezer log shows a night excursion.",
        source="equipment",
        severity="major",
        owner=tech,
    )


@both
def test_linking_a_ticket_waiting_for_a_recategorization_is_missing_only_if_hidden(
    target, ticket, tech, lead, owned_nc
):
    """link_ticket by the record's owner (a technician): the ticket's own revision is not part
    of the request, so a ticket recategorized while the link waited and still readable is linked
    (and its revision advances once); a ticket the owner can no longer read is Http404 and nothing
    is linked."""
    session, baseline, after = race(
        ticket,
        lead,
        target,
        lambda: nc_services.link_ticket(
            actor=tech, nc=owned_nc, expected_revision=owned_nc.revision, request=ticket
        ),
        _measure(ticket, links=TicketLink.objects.count),
    )
    if target.is_restricted:
        _assert_missing(session, baseline, after, kind=Http404)
        assert not TicketLink.objects.filter(request=ticket).exists()
    else:
        assert session.done, session.outcome
        assert TicketLink.objects.filter(request=ticket, nonconformance=owned_nc).count() == 1
        assert after["links"] == baseline["links"] + 1
        assert after["revision"] == baseline["revision"] + 1


@both
def test_unlinking_a_ticket_waiting_for_a_recategorization_is_missing_only_if_hidden(
    target, ticket, tech, lead, owned_nc
):
    """unlink_ticket by the record's owner: a still-readable recategorized ticket is unlinked
    (the link keeps who removed it); one the owner can no longer read is Http404 and the link
    stays."""
    link = nc_services.link_ticket(
        actor=lead, nc=owned_nc, expected_revision=owned_nc.revision, request=ticket
    )
    owned_nc.refresh_from_db()
    removed = lambda: TicketLink.objects.filter(removed_at__isnull=False).count()  # noqa: E731
    session, baseline, after = race(
        ticket,
        lead,
        target,
        lambda: nc_services.unlink_ticket(
            actor=tech, nc=owned_nc, expected_revision=owned_nc.revision, link=link, reason="Wrong"
        ),
        _measure(ticket, removed=removed),
    )
    if target.is_restricted:
        _assert_missing(session, baseline, after, kind=Http404)
    else:
        assert session.done, session.outcome
        assert after["removed"] == baseline["removed"] + 1
        assert TicketLink.objects.get(pk=link.pk).removed_by_id == tech.pk


@both
def test_a_withdrawal_waiting_for_a_recategorization_is_not_answered_as_missing(
    target, ticket, agent, lead
):
    """withdraw_attachment by a supervisor, who reads every ticket: a recategorization that
    commits while the withdrawal waits (into a readable or a restricted category) never makes it
    "No such ticket"; the file is withdrawn with its audit event."""
    seen = _fresh(ticket)
    add_entry_with_files(
        actor=agent,
        request=seen,
        kind=EntryKind.REPLY,
        body="Requisition attached.",
        expected_revision=seen.revision,
        files=[png_file()],
    )
    attachment = Attachment.objects.get()
    session, baseline, after = race(
        ticket,
        lead,
        target,
        lambda: withdraw_attachment(
            actor=lead, attachment_id=attachment.pk, reason="Wrong patient"
        ),
        _measure(ticket),
    )
    assert session.done, session.outcome
    attachment.refresh_from_db()
    assert attachment.withdrawn_at is not None and attachment.withdrawn_by_id == lead.pk
    assert after["audit"] == baseline["audit"] + 1
    assert AuditEvent.objects.filter(action="attachment.withdrawn").count() == 1


@pytest.fixture
def lis(db):
    return ExternalSystem.objects.get_or_create(
        key="lis", defaults={"name": "Laboratory information system", "kind": "lis"}
    )[0]


@both
def test_attaching_a_link_waiting_for_a_recategorization_is_missing_only_if_hidden(
    target, ticket, tech, lead, lis
):
    """add_link (links.services.locked_request): a still-readable recategorized ticket takes the
    link; one the technician can no longer read is "No such ticket" (DoesNotExist) and no link
    is stored."""
    session, baseline, after = race(
        ticket,
        lead,
        target,
        lambda: link_services.add_link(
            actor=tech,
            request_id=ticket.pk,
            system=lis,
            record_type="order",
            identifier="ORD-TEST-000042",
        ),
        _measure(ticket, links=LinkAttachment.objects.count),
    )
    if target.is_restricted:
        _assert_missing(session, baseline, after)
    else:
        assert session.done, session.outcome
        assert after["links"] == baseline["links"] + 1


@both
def test_removing_a_link_waiting_for_a_recategorization_is_missing_only_if_hidden(
    target, ticket, tech, lead, lis
):
    """remove_link (links.services._locked_attachment, through locked_request): a
    still-readable recategorized ticket lets the technician remove the link; otherwise the
    ticket is missing and the link stays."""
    attachment, _created = link_services.add_link(
        actor=lead,
        request_id=ticket.pk,
        system=lis,
        record_type="order",
        identifier="ORD-TEST-000043",
    )
    removed = lambda: LinkAttachment.objects.filter(removed_at__isnull=False).count()  # noqa: E731
    session, baseline, after = race(
        ticket,
        lead,
        target,
        lambda: link_services.remove_link(actor=tech, attachment_id=attachment.pk, reason="Wrong"),
        _measure(ticket, removed=removed),
    )
    if target.is_restricted:
        _assert_missing(session, baseline, after)
    else:
        assert session.done, session.outcome
        assert after["removed"] == baseline["removed"] + 1


@both
def test_a_form_correction_waiting_for_a_recategorization_is_missing_only_if_hidden(
    target, agent, category, quality, tech, lead
):
    """correct_values (request_forms.services): a ticket raised with a form and recategorized
    while a technician's correction waited is still corrected when the technician may read it
    (the correction has no revision check, and the ticket's revision advances once); otherwise it
    is "No such ticket" and the stored value stays."""
    template = RequestTemplate.objects.create(
        key="nb-form", name="Neighbour form", category=category, offered_in_portal=False
    )
    template.fields.create(key="note", label="Note", kind=FieldKind.SHORT_TEXT)
    raised = create_request_with_form(
        actor=agent,
        template=template,
        expected_definition=definition.token(template),
        values={"note": "first"},
        first_entry_kind=EntryKind.NOTE,
        channel="phone",
        team=quality,
    )
    row = FieldValue.objects.get(filled_form__request=raised, superseded_at__isnull=True)
    values = lambda: FieldValue.objects.filter(  # noqa: E731
        filled_form__request=raised, superseded_at__isnull=True
    ).count()
    session, baseline, after = race(
        raised,
        lead,
        target,
        lambda: correct_values(
            actor=tech,
            request=raised,
            expected_definition=definition.token(template),
            values={"note": "second"},
            reason="Typo",
            expected={"note": row.pk},
        ),
        _measure(raised, current=values),
    )
    if target.is_restricted:
        _assert_missing(session, baseline, after)
        assert FieldValue.objects.get(pk=row.pk).superseded_at is None
    else:
        assert session.done, session.outcome
        assert session.result == ["note"]
        assert FieldValue.objects.get(pk=row.pk).superseded_at is not None
        assert after["revision"] == baseline["revision"] + 1


# R04. A bulk action decides who asks after the tickets' locks -----------------------------------


def _bulk_calls(actor, tickets, accessioning, person, *, revision_offset=0):
    wanted = {pk: revision + revision_offset for pk, revision in selection(tickets).items()}
    return {
        "bulk_status": lambda: services.bulk_status(
            actor=actor, selected=wanted, status=Status.WAITING_CLIENT
        ),
        "bulk_close": lambda: services.bulk_close(actor=actor, selected=wanted),
        "bulk_assign": lambda: services.bulk_assign(
            actor=actor, selected=wanted, team=accessioning, assignee=person
        ),
    }


def _untouched(tickets):
    return {
        "tickets": [(r.revision, r.status, r.team_id, r.assignee_id) for r in map(_fresh, tickets)],
        "audit": AuditEvent.objects.count(),
        "entries": RequestEntry.objects.count(),
    }


BULK = ["bulk_status", "bulk_close", "bulk_assign"]


def _blocked_by_this_session(pid):
    """Whether session `pid` waits for a lock this session holds (pg_blocking_pids reads the lock
    manager live)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
        return cursor.fetchone()[0]


def _access_change_meanwhile(change, seen):
    """Start `change` in its own session once the service has begun and do not wait for it: it
    cannot commit while the service holds the account's rows (accounts.acting). What it was seen
    doing is put in `seen`."""
    session = Session(change).start()
    seen["session"] = session
    seen["state"] = session.waiting_or_ended()
    seen["behind_this_session"] = session.pid is not None and _blocked_by_this_session(session.pid)


def _remove_roles(actor):
    def run():
        User.objects.get(pk=actor.pk).groups.clear()

    return run


def _demote_to_technician(actor):
    def run():
        stored = User.objects.get(pk=actor.pk)
        stored.groups.set([Group.objects.get_or_create(name="technical")[0]])

    return run


@pytest.mark.parametrize("action", BULK)
def test_a_role_removal_waits_for_a_bulk_action_under_way(
    action, make_user, join, category, accessioning, tech
):
    """The acting account's last staff role is removed once a bulk action has begun. The action
    holds the account's rows until it commits (accounts.acting), so the removal waits behind
    this session instead of committing under it. The action is saved first, by an account that
    still had its role, and the role is gone afterwards."""
    actor = make_user("nb_bulk_actor", "technical")
    tickets = [
        new_request(actor, category, team=accessioning),
        new_request(actor, category, team=accessioning),
    ]
    if action == "bulk_close":  # a ticket is closed once resolved
        tickets = [set_status(r, Status.RESOLVED) for r in tickets]
    calls = _bulk_calls(actor, tickets, accessioning, tech)
    seen = {}
    with paused(
        "_bulk_selection",
        lambda: _access_change_meanwhile(_remove_roles(actor), seen),
        before=False,
    ):
        error = run_here(calls[action])
    seen["session"].join()
    assert (seen["state"], seen["behind_this_session"]) == ("waiting", True)
    assert error is None, repr(error)
    assert seen["session"].done, seen["session"].outcome
    after = [_fresh(r) for r in tickets]
    if action == "bulk_status":
        assert {r.status for r in after} == {Status.WAITING_CLIENT}
    elif action == "bulk_close":
        assert {r.status for r in after} == {Status.CLOSED}
    else:
        assert {(r.team_id, r.assignee_id) for r in after} == {(accessioning.pk, tech.pk)}
    assert all(r.revision > b.revision for r, b in zip(after, tickets, strict=True))
    assert not User.objects.get(pk=actor.pk).groups.exists()


@pytest.mark.parametrize("action", BULK)
def test_a_bulk_action_by_an_account_whose_last_role_was_removed_is_refused_first(
    action, make_user, join, category, accessioning, tech
):
    """The account's last staff role was removed (and committed) before the bulk action began,
    while the caller still holds the account as it was: the action is refused as asked by
    someone who may not (not as a changed revision, although the selection also carries an
    out-of-date one). Nothing is saved."""
    actor = make_user("nb_bulk_actor", "technical")
    tickets = [
        new_request(actor, category, team=accessioning),
        new_request(actor, category, team=accessioning),
    ]
    before = _untouched(tickets)
    calls = _bulk_calls(actor, tickets, accessioning, tech, revision_offset=7)
    _remove_roles(actor)()
    error = run_here(calls[action])
    assert type(error) in (PermissionDenied, ServiceRequest.DoesNotExist, Http404), repr(error)
    assert _untouched(tickets) == before


@pytest.mark.parametrize("action", BULK)
def test_a_demotion_waits_for_a_bulk_action_under_way_on_a_restricted_ticket(
    action, make_user, join, category, privacy_category, accessioning, quality, tech
):
    """A supervisor whose selection includes a restricted ticket of a team they are not in is
    demoted to technician once the bulk action has begun. The action holds the account's roles
    until it commits (accounts.acting), so the demotion waits behind this session. The action is
    saved first, for the tickets it selected, and the supervisor role is gone afterwards."""
    actor = make_user("nb_demoted", "supervisor")
    join(actor, accessioning)
    plain = new_request(actor, category, team=accessioning)
    hidden = new_request(actor, privacy_category, team=quality)
    tickets = [plain, hidden]
    if action == "bulk_close":  # a ticket is closed once resolved
        tickets = [set_status(r, Status.RESOLVED) for r in tickets]
    calls = _bulk_calls(actor, tickets, accessioning, tech)
    seen = {}
    with paused(
        "_bulk_selection",
        lambda: _access_change_meanwhile(_demote_to_technician(actor), seen),
        before=False,
    ):
        error = run_here(calls[action])
    seen["session"].join()
    assert (seen["state"], seen["behind_this_session"]) == ("waiting", True)
    assert error is None, repr(error)
    assert seen["session"].done, seen["session"].outcome
    after = [_fresh(r) for r in tickets]
    assert all(r.revision > b.revision for r, b in zip(after, tickets, strict=True))
    if action == "bulk_status":
        assert {r.status for r in after} == {Status.WAITING_CLIENT}
    elif action == "bulk_close":
        assert {r.status for r in after} == {Status.CLOSED}
    else:
        assert {(r.team_id, r.assignee_id) for r in after} == {(accessioning.pk, tech.pk)}
    roles = set(User.objects.get(pk=actor.pk).groups.values_list("name", flat=True))
    assert roles == {"technical"}


@pytest.mark.parametrize("action", BULK)
def test_a_supervisor_demoted_before_the_action_is_refused_a_restricted_ticket_of_another_team(
    action, make_user, join, category, privacy_category, accessioning, quality, tech
):
    """The supervisor was demoted to technician (and the demotion committed) before the bulk
    action began, while the caller still holds the account as it was. A selection that includes
    a restricted ticket of a team they are not in is refused as "not available" (not as a
    changed revision, although the selection also carries an out-of-date one). Nothing is
    saved."""
    actor = make_user("nb_demoted", "supervisor")
    join(actor, accessioning)
    plain = new_request(actor, category, team=accessioning)
    hidden = new_request(actor, privacy_category, team=quality)
    tickets = [plain, hidden]
    before = _untouched(tickets)
    calls = _bulk_calls(actor, tickets, accessioning, tech, revision_offset=7)
    _demote_to_technician(actor)()
    error = run_here(calls[action])
    assert type(error) in (PermissionDenied, ServiceRequest.DoesNotExist, Http404), repr(error)
    assert _untouched(tickets) == before


@pytest.mark.parametrize("action", BULK)
def test_a_portal_account_calling_a_bulk_action_is_denied(
    action, portal, agent, category, accessioning, tech, clinic
):
    """Bulk actions are staff work: a client portal account is denied before anything about the
    tickets (even its own ticket, even with an out-of-date revision). Nothing is saved."""
    mine = new_request(agent, category, team=accessioning, client=clinic)
    before = _untouched([mine])
    calls = _bulk_calls(portal, [mine], accessioning, tech, revision_offset=7)
    with pytest.raises(PermissionDenied):
        calls[action]()
    assert _untouched([mine]) == before


# R03. Owners stepping down in account administration --------------------------------------------


def _admin_change_data(http, user, **changes):
    """The account page's form as drawn for `user`, with `changes`: what the browser posts."""
    response = http.get(reverse("admin:accounts_user_change", args=[user.pk]))
    assert response.status_code == 200
    form = response.context["adminform"].form
    data = {}
    for name, field in form.fields.items():
        if field.disabled:
            continue
        value = form[name].value()
        if isinstance(field.widget, forms.MultiWidget):
            parts = field.widget.decompress(value)
            for index, part in enumerate(parts):
                data[f"{name}_{index}"] = "" if part is None else str(part)
        elif isinstance(value, (list, tuple)):
            data[name] = [str(item) for item in value]
        elif isinstance(value, bool):
            if value:
                data[name] = "on"
        else:
            data[name] = "" if value is None else value
    for name, value in changes.items():
        if value is False:
            data.pop(name, None)
        else:
            data[name] = value
    return data


def _owner(make_user, join, team, name):
    owner = make_user(name, "admin", is_staff=True, is_superuser=True)
    join(owner, team)
    return owner


def _deactivate(owner, account):
    """The admin form of `account`, deactivating it, as `owner` posts it; call it to post."""
    http = Client()
    http.force_login(owner)
    data = _admin_change_data(http, account, is_active=False)

    def post():
        return http.post(reverse("admin:accounts_user_change", args=[account.pk]), data)

    return post


def test_two_owners_stepping_down_at_once_take_turns_and_the_last_owner_stays(
    make_user, join, accessioning
):
    """Two active owners each deactivate their own account in account administration at the same
    time. The owner check takes every owner's row until the save commits, so the second waits
    for the first and then sees the first gone: it is refused ("Keep at least one active
    superuser") and its owner stays active. FOR NO KEY UPDATE is as exclusive as FOR UPDATE
    between two such checks."""
    first = _owner(make_user, join, accessioning, "nb_owner_a")
    second = _owner(make_user, join, accessioning, "nb_owner_b")
    step_down_first, step_down_second = _deactivate(first, first), _deactivate(second, second)
    holding, release = threading.Event(), threading.Event()
    from accounts.admin import AccessRules

    real_clean = AccessRules.clean
    holder = {}

    def clean(self):
        data = real_clean(self)
        if threading.current_thread() is holder["session"].thread:
            holding.set()  # the check has passed and its locks are held until the save commits
            release.wait(timeout=20)
        return data

    with patch.object(AccessRules, "clean", clean):
        holder["session"] = Session(step_down_first).start()
        assert holding.wait(timeout=10), holder["session"].error
        loser = Session(step_down_second).start()
        try:
            state = loser.waiting_or_ended()
        finally:
            release.set()
        holder["session"].join()
        loser.join()
    assert state == "waiting", "the second owner did not wait for the first"
    assert holder["session"].done and holder["session"].result.status_code == 302
    assert loser.done, loser.error
    response = loser.result
    assert response.status_code == 200, "the second step-down was not refused"
    assert "Keep at least one active superuser" in response.content.decode()
    assert not User.objects.get(pk=first.pk).is_active
    assert User.objects.get(pk=second.pk).is_active


@pytest.mark.parametrize("change", ["note", "status"])
def test_deactivating_an_owner_does_not_deadlock_with_another_owner_changing_their_ticket(
    change, make_user, join, agent, category, accessioning
):
    """An owner is deactivated in account administration (every owner row held, the account
    updated, its open tickets returned to the queue) while another owner adds a note to, or
    changes the status of, a ticket the first holds. The ticket's author and actor rows of the
    second owner are only key-shared by the deactivation (FOR NO KEY UPDATE), so the two do not
    wait for each other in a cycle: no deadlock (40P01), both complete and the ticket is back in
    the queue."""
    admin_owner = _owner(make_user, join, accessioning, "nb_owner_1")
    held_by = _owner(make_user, join, accessioning, "nb_owner_2")
    writer = _owner(make_user, join, accessioning, "nb_owner_3")
    ticket = new_request(agent, category, team=accessioning, assignee=held_by)
    deactivate = _deactivate(admin_owner, held_by)
    seen = _fresh(ticket)
    calls = {
        "note": lambda: services.add_entry(
            actor=writer,
            request=seen,
            kind=EntryKind.NOTE,
            body="Spoke to the clinic.",
            expected_revision=seen.revision,
        ),
        "status": lambda: services.change_status(
            actor=writer,
            request=seen,
            expected_revision=seen.revision,
            status=Status.WAITING_CLIENT,
        ),
    }
    patched = {"note": "_locked_request", "status": "_staff_request"}[change]
    started = {}

    def during():
        started["deactivation"] = Session(deactivate).start()
        started["state"] = started["deactivation"].waiting_or_ended()

    with paused(patched, during, before=False):
        error = run_here(calls[change])
    deactivation = started["deactivation"].join()
    outcomes = {"change": _sqlstate(error) or error, "deactivation": deactivation.outcome}
    assert "40P01" not in outcomes.values(), f"deadlock: {outcomes}"
    assert started["state"] == "waiting", started
    assert error is None, error
    assert deactivation.done and deactivation.result.status_code == 302, deactivation.outcome
    assert not User.objects.get(pk=held_by.pk).is_active
    after = _fresh(ticket)
    assert after.assignee_id is None
    if change == "note":
        assert RequestEntry.objects.filter(
            request=ticket, kind=EntryKind.NOTE, author=writer
        ).exists()
    else:
        assert after.status == Status.WAITING_CLIENT
