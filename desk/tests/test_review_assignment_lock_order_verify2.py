"""Second verification of the assignment lock order (candidate 791976b). All records are
synthetic.

Each test states intended behaviour. The races run every party in its own PostgreSQL session (a
thread with its own connection) with a bounded lock_timeout; a party is paused at a named point,
or holds its transaction open, until the others wait for a lock (seen with pg_blocking_pids) or
have finished. The file uses no helper only the candidate has, so it runs on 5cd2ef1 and on the
base 9fbf6c0 too.
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, connection, connections, transaction
from django.http import Http404
from django.test import Client
from django.urls import reverse

from accounts.models import User
from attachments.models import Attachment
from attachments.services import add_entry_with_files
from attachments.tests.conftest import png_file
from core.models import AuditEvent
from desk import notifications, services
from desk.models import EntryKind, RequestEntry, ServiceRequest
from jobs import outbox
from jobs.models import Notice
from links import services as link_services
from links.models import ExternalRecordLink, ExternalSystem, LinkAttachment
from request_forms import definition
from request_forms.models import FieldKind, FieldValue, RequestTemplate
from request_forms.services import correct_values, create_request_with_form
from teams import services as team_services
from teams.models import TeamMembership

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


def _held_while(hold, change, meanwhile):
    """`change` (run in its own session) waits for a lock that `hold` takes in another session,
    kept open; `meanwhile()` commits; the lock is released. Returns ("waiting" if the change
    waited, its Session once it has ended)."""
    holder = Held(hold)
    session = Session(change).start()
    try:
        state = session.waiting_or_ended()
        meanwhile()
    finally:
        assert holder.release() == "done"
    session.join()
    return state, session


def _hold_ticket(ticket):
    return lambda: ServiceRequest.objects.select_for_update().get(pk=ticket.pk)


def _fresh(ticket):
    return ServiceRequest.objects.get(pk=ticket.pk)


def _assign(actor, ticket, team, person=None):
    ticket = _fresh(ticket)
    return services.assign(
        actor=actor, request=ticket, expected_revision=ticket.revision, team=team, assignee=person
    )


def _deactivate(user):
    def run():
        account = User.objects.get(pk=user.pk)
        account.is_active = False
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


def _capability():
    return Permission.objects.get(
        codename="view_patient_information", content_type__app_label="accounts"
    )


def _revoke(user):
    def run():
        User.objects.get(pk=user.pk).user_permissions.remove(_capability())

    return run


# People and records -----------------------------------------------------------------------------


@pytest.fixture
def lead(make_user):
    """A supervisor, member of no team."""
    return make_user("verify2_lead", "supervisor")


@pytest.fixture
def clerk(make_user):
    return make_user("verify2_clerk", "client_services")


@pytest.fixture
def capable(make_user):
    """Client services with the patient information capability."""
    user = make_user("verify2_capable", "client_services")
    user.user_permissions.add(_capability())
    return user


@pytest.fixture
def technician(make_user, join, quality):
    """Technical staff, member of Quality (and so a reader of its restricted tickets)."""
    user = make_user("verify2_technician", "technical")
    join(user, quality)
    return user


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
def notice_in_category(agent, make_user, category, quality):
    """A ticket in `category` of Quality assigned to a technician, its staff notice waiting to be
    sent."""
    recipient = make_user("verify2_recipient", "technical")
    TeamMembership.objects.create(team=quality, user=recipient)
    ticket = new_request(agent, category, team=quality)
    with patch.object(outbox, "send_now", return_value={}):
        _assign(agent, ticket, quality, recipient)
    return Notice.objects.get(recipient=recipient, topic=notifications.TOPIC)


class Sending:
    """A staff notice being sent: the attempt's shared locks (desk.notifications, the ticket, the
    recipient, the ticket's category FOR SHARE, the roles, the team and the membership) held, as
    while the mail server answers, until `release()`."""

    def __init__(self, row):
        self.ready, self.released = threading.Event(), threading.Event()

        def send():
            with transaction.atomic():
                assert notifications.prepare_request_notice(row) is not None
                self.ready.set()
                self.released.wait(timeout=20)

        self.session = Session(send).start()
        assert self.ready.wait(timeout=10), self.session.error

    def release(self):
        self.released.set()
        return self.session.join()


def _signed_in(user):
    """A browser session of `user` that reports a server error as a 500, as a person sees it."""
    http = Client(raise_request_exception=False)
    http.force_login(user)
    return http


# V2-01. A ticket that leaves the actor's scope while a link or file change waits: 404 -----------


def _link_attachment(actor, ticket, lis, identifier):
    attachment, _created = link_services.add_link(
        actor=actor,
        request_id=ticket.pk,
        system=lis,
        record_type="order",
        identifier=identifier,
    )
    return attachment


def _file_on(actor, ticket):
    seen = _fresh(ticket)
    add_entry_with_files(
        actor=actor,
        request=seen,
        kind=EntryKind.NOTE,
        body="Requisition attached.",
        expected_revision=seen.revision,
        files=[png_file()],
    )
    return Attachment.objects.get(request=ticket)


@pytest.mark.parametrize(
    "page",
    ["link-add", "link-add-member-removed", "link-remove", "link-shown", "file-withdraw"],
)
def test_a_link_or_file_page_whose_ticket_left_the_actors_scope_while_it_waited_answers_404(
    page, lead, technician, restricted_ticket, lis, quality
):
    """A restricted ticket (Privacy request, Quality) is open to a supervisor, and to a member of
    Quality. While the page's change waits for the ticket's lock, the supervisor is demoted to
    technical staff, or the technician is removed from Quality: the ticket is no longer one they
    may open. links.services.locked_request ("DoesNotExist otherwise (the view's 404)"),
    desk.selectors.locked_in_scope ("ServiceRequest.DoesNotExist, which pages answer as 404") and
    the desk's own pages (desk/views.py "left the actor's scope meanwhile" -> 404) answer such a
    change with 404, nothing saved. Not a server error."""
    actor, meanwhile = lead, _demote(lead)
    ticket = restricted_ticket
    if page == "link-add-member-removed":
        membership = TeamMembership.objects.get(team=quality, user=technician)
        actor = technician

        def meanwhile():
            team_services.remove_member(actor=lead, membership=membership, reason="Moved")

    if page in ("link-add", "link-add-member-removed"):
        url = reverse("link_records:request-link-add", args=[ticket.pk])
        data = {
            "system": lis.pk,
            "record_type": "order",
            "identifier": "ORD-TEST-000051",
            "role": "related",
        }

        def unchanged():
            return not LinkAttachment.objects.filter(request=ticket).exists()

    elif page in ("link-remove", "link-shown"):
        attachment = _link_attachment(lead, ticket, lis, "ORD-TEST-000052")
        name = "attachment-remove" if page == "link-remove" else "attachment-shown"
        url = reverse(f"link_records:{name}", args=[attachment.pk])
        data = {"reason": "Wrong record"} if page == "link-remove" else {"shown": "1"}

        def unchanged():
            current = LinkAttachment.objects.get(pk=attachment.pk)
            return current.removed_at is None and not current.shown_to_client

    else:
        attachment = _file_on(lead, ticket)
        url = reverse("attachments:withdraw", args=[attachment.pk])
        data = {"reason": "Wrong file"}

        def unchanged():
            return Attachment.objects.get(pk=attachment.pk).withdrawn_at is None

    http = _signed_in(actor)
    entries = RequestEntry.objects.filter(request=ticket).count()
    state, session = _held_while(_hold_ticket(ticket), lambda: http.post(url, data), meanwhile)
    assert state == "waiting", state
    assert session.outcome == "done", repr(session.error)
    # (the page's answer, whether nothing of the change was saved)
    assert (session.result.status_code, unchanged()) == (404, True)
    assert RequestEntry.objects.filter(request=ticket).count() == entries


# V2-02. The account read again after the ticket's lock, then a later lock waited for -----------


@pytest.fixture
def form_ticket(agent, category, quality):
    """A ticket in `category` raised with a staff form whose "note" holds "first"."""
    template = RequestTemplate.objects.create(
        key="verify2-correction", name="Correction form", category=category, offered_in_portal=False
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
    return SimpleNamespace(ticket=raised, template=template, row=row)


@pytest.mark.parametrize("meanwhile", ["deactivated", "role-removed"])
def test_a_form_correction_waiting_for_its_forms_category_refuses_an_account_changed_meanwhile(
    meanwhile, clerk, form_ticket, notice_in_category
):
    """correct_values locks the ticket, reads the acting account again (locked_staff_request),
    then locks the form's definition, whose category it takes FOR NO KEY UPDATE. A staff notice
    about another ticket of the category, being sent, holds the category FOR SHARE until the mail
    server answers, so the correction waits there, after its reading of the account. The account
    is deactivated, or loses its only staff role, during that wait. The rule is the ticket
    services' one: the account is read again after the change's last lock (desk.services.settle,
    "After a staff change's last lock"; current_staff: "an account deactivated ... or left without
    a staff role while its request waited is refused"). The correction is refused; the stored
    value stays current and no value row, timeline entry or revision is written."""
    token = definition.token(form_ticket.template)
    before = (
        _fresh(form_ticket.ticket).revision,
        FieldValue.objects.count(),
        RequestEntry.objects.count(),
    )
    change = {
        "deactivated": _deactivate(clerk),
        "role-removed": _remove_role(clerk, "client_services"),
    }[meanwhile]
    sending = Sending(notice_in_category)
    session = Session(
        lambda: correct_values(
            actor=clerk,
            request=form_ticket.ticket,
            expected_definition=token,
            values={"note": "second"},
            reason="Typo",
            expected={"note": form_ticket.row.pk},
        )
    ).start()
    try:
        state = session.waiting_or_ended()
        change()
    finally:
        assert sending.release() == "done"
    session.join()
    assert state == "waiting", state  # for the category, held by the notice being sent
    assert session.outcome in ("denied", "not found"), (session.outcome, repr(session.error))
    assert FieldValue.objects.get(pk=form_ticket.row.pk).superseded_at is None
    assert before == (
        _fresh(form_ticket.ticket).revision,
        FieldValue.objects.count(),
        RequestEntry.objects.count(),
    )


@pytest.mark.parametrize(
    ("record_type", "meanwhile"),
    [("order", "deactivated"), ("order", "role-removed"), ("patient", "capability-revoked")],
)
def test_adding_a_link_that_waits_for_its_system_refuses_an_account_changed_meanwhile(
    record_type, meanwhile, capable, agent, category, quality, lis
):
    """links.services.add_link locks the ticket and reads the acting account again
    (locked_staff_request), then locks the external system and the link. Another link being added
    with the same system holds the system FOR UPDATE to its commit (add_link), and an
    administrator's edit of the system does too (update_system), so the addition waits there,
    after its reading of the account. The account is deactivated, loses its staff role, or (for
    a patient link) loses the patient information capability during that wait. As for the ticket
    services, the account is read again after the change's last lock (desk.services.settle) and
    a person-level link needs the capability as it stands ("Person-level links ... need the
    view_patient_information capability for every action"). Refused; nothing is attached and no
    timeline entry is written."""
    ticket = new_request(agent, category, team=quality)
    change = {
        "deactivated": _deactivate(capable),
        "role-removed": _remove_role(capable, "client_services"),
        "capability-revoked": _revoke(capable),
    }[meanwhile]
    entries = RequestEntry.objects.filter(request=ticket).count()
    state, session = _held_while(
        lambda: ExternalSystem.objects.select_for_update().get(pk=lis.pk),
        lambda: link_services.add_link(
            actor=capable,
            request_id=ticket.pk,
            system=lis,
            record_type=record_type,
            identifier="PAT-TEST-000061" if record_type == "patient" else "ORD-TEST-000061",
        ),
        change,
    )
    assert state == "waiting", state  # for the system
    assert session.outcome in ("denied", "not found"), (session.outcome, repr(session.error))
    assert not LinkAttachment.objects.filter(request=ticket).exists()
    assert RequestEntry.objects.filter(request=ticket).count() == entries


# V2-03. Ticket creation and the creating account ------------------------------------------------


@pytest.fixture
def assignee(make_user, join, accessioning):
    user = make_user("verify2_assignee", "technical")
    join(user, accessioning)
    return user


@pytest.mark.parametrize("meanwhile", ["deactivated", "role-removed"])
def test_a_ticket_whose_creator_lost_access_while_it_waited_for_the_assignee_is_not_saved(
    meanwhile, clerk, assignee, sample_category, accessioning
):
    """A clerk logs a ticket naming a technician. The technician's account row is held as an
    assignment to them holds it while it waits for a busy ticket (desk.services._hold_assignment;
    "an intake that waits for the person then holds nothing"), so the creation waits for the
    person. The clerk's account is deactivated, or loses its only staff role, during that wait.
    The creation was authorized from the reading taken before it waited (create_request:
    require_staff before any lock); the ticket services refuse an account changed while the
    change waited (current_staff, settle, docs/architecture.md "Every change reads the acting
    account again"; AGENTS.md 4 "Recheck concurrent changes"). Refused; no ticket, entry or audit
    event of a creation is written."""
    change = {
        "deactivated": _deactivate(clerk),
        "role-removed": _remove_role(clerk, "client_services"),
    }[meanwhile]
    created = AuditEvent.objects.filter(action="request.created").count()
    state, session = _held_while(
        lambda: User.objects.select_for_update(no_key=True).get(pk=assignee.pk),
        lambda: new_request(
            clerk,
            sample_category,
            subject="Recollection needed",
            team=accessioning,
            assignee=assignee,
        ),
        change,
    )
    assert state == "waiting", state  # for the person
    assert session.outcome in ("denied", "refused"), (session.outcome, repr(session.error))
    assert not ServiceRequest.objects.filter(subject="Recollection needed").exists()
    assert AuditEvent.objects.filter(action="request.created").count() == created


def test_a_portal_ticket_whose_account_was_deactivated_while_it_waited_is_not_saved(
    clinic_portal, category, notice_in_category
):
    """A client portal account raises a ticket in a category while a staff notice about another
    ticket of that category is being sent (it holds the category FOR SHARE until the mail server
    answers), so the creation waits for the category's lock. The laboratory deactivates the portal
    account during that wait. As a portal reply is refused when the account was deactivated while
    it waited (settle_portal; review C-01), the creation is refused and nothing is saved."""
    created = AuditEvent.objects.filter(action="request.created").count()
    sending = Sending(notice_in_category)
    session = Session(
        lambda: services.create_request(
            actor=clinic_portal,
            category=category,
            subject="Portal question",
            first_entry_kind=EntryKind.REPLY,
            first_entry_body="Where is the report for order ORD-TEST-000081?",
            channel="portal",
        )
    ).start()
    try:
        state = session.waiting_or_ended()
        _deactivate(clinic_portal)()
    finally:
        assert sending.release() == "done"
    session.join()
    assert state == "waiting", state  # for the category
    assert session.outcome in ("denied", "not found", "refused"), (
        session.outcome,
        repr(session.error),
    )
    assert not ServiceRequest.objects.filter(subject="Portal question").exists()
    assert AuditEvent.objects.filter(action="request.created").count() == created


# V2-04. Adding a link and changing the same link at the same moment -----------------------------


@pytest.mark.parametrize("change", ["remove", "show", "edit"])
def test_adding_a_link_and_changing_the_same_link_never_deadlock(
    change, clerk, make_user, agent, category, quality, clinic, lis
):
    """A ticket carries an order link. One clerk adds the same order to the same ticket in another
    role while a colleague removes the existing link from it, shows it to the client, or corrects
    the link's label. links.services.add_link locks the ticket, the system, then the link;
    removing or showing locks the link attachment with its link, then the ticket
    (_locked_attachment); a label correction locks the link with its system in one statement
    (update_link). Each change is paused once it holds its first locks, until the other waits. No
    change ends with a deadlock (40P01): each either goes through or is refused, as the other
    paths that lock people, teams, categories and tickets (docs/architecture.md "Neither fails
    with a deadlock")."""
    colleague = make_user("verify2_colleague", "client_services")
    ticket = new_request(agent, category, team=quality, client=clinic)
    existing = _link_attachment(colleague, ticket, lis, "ORD-TEST-000071")
    link = ExternalRecordLink.objects.get(pk=existing.link_id)
    sessions, seen = {}, {}

    def add():
        link_services.add_link(
            actor=clerk,
            request_id=ticket.pk,
            system=lis,
            record_type="order",
            identifier="ORD-TEST-000071",
            role="primary",
        )

    other = {
        "remove": lambda: link_services.remove_link(
            actor=colleague, attachment_id=existing.pk, reason="Wrong role"
        ),
        "show": lambda: link_services.set_shown_to_client(
            actor=colleague, attachment_id=existing.pk, shown=True
        ),
        "edit": lambda: link_services.update_link(
            actor=colleague,
            link_id=link.pk,
            expected_revision=link.revision,
            display_label="Order 71",
            reason="Clearer label",
        ),
    }[change]

    if change == "edit":
        # The addition holds the ticket and the system when the correction starts.
        def during():
            sessions["edit"] = Session(other).start()
            seen["edit"] = sessions["edit"].waiting_or_ended()

        with paused(link_services, "_find_or_create_link", during):
            first = ("add", run_here(add))
    else:
        # The removal or show holds the link attachment and its link when the addition starts.
        def during():
            sessions["add"] = Session(add).start()
            seen["add"] = sessions["add"].waiting_or_ended()

        with paused(link_services, "locked_request", during):
            first = (change, run_here(other))
    outcome = {name: session.join() for name, session in sessions.items()}
    outcome[first[0]] = first[1]
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}, during the pause {seen}"
    assert set(outcome.values()) <= {"done", "refused"}, outcome
