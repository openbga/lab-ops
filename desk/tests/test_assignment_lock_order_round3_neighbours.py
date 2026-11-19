"""Neighbours of the third round of assignment lock order fixes (V2-01 to V2-04). All records are
synthetic.

Each test states a rule. The races run every party in its own PostgreSQL session (a thread with
its own connection) with a bounded lock_timeout; a party is held at a named lock until the others
wait for it (seen with pg_blocking_pids) or have finished. The helpers are copied from
test_review_assignment_lock_order_verify2.
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
from clients.models import Client as ClientOrg
from clients.models import ClientSite, PortalAccess
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
from teams.models import Team, TeamMembership

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


def _deactivate_bypassing_save(user):
    """A deactivation by a path that bypasses User.save (a queryset update). It takes a FOR NO KEY
    UPDATE of the account row, which the hold of accounts.acting (FOR KEY SHARE) does not make
    wait."""

    def run():
        User.objects.filter(pk=user.pk).update(is_active=False)

    return run


def _blocked_by(pid, other):
    """Whether session `pid` waits for a lock that session `other` holds."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [other, pid])
        return cursor.fetchone()[0]


def _held_with_access_change(hold, change, access_change):
    """`change` waits for a lock that `hold` takes (another session, kept open). Then
    `access_change` is started in its own session. The change holds the account's rows
    (accounts.acting), so the access change waits behind it. The lock is released and both are
    joined. Returns (whether the change waited, whether the access change waited, whether it
    waited behind the change, the change's Session, the access change's Session)."""
    holder = Held(hold)
    return _release_after_access_change(holder.release, change, access_change)


def _release_after_access_change(release, change, access_change):
    session = Session(change).start()
    try:
        state = session.waiting_or_ended()
        access = Session(access_change).start()
        access_state = access.waiting_or_ended()
        behind_change = access.pid is not None and _blocked_by(access.pid, session.pid)
    finally:
        assert release() == "done"
    session.join()
    access.join()
    return state, access_state, behind_change, session, access


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


# Records ----------------------------------------------------------------------------------------


def _link_attachment(actor, ticket, lis, identifier, record_type="order"):
    attachment, _created = link_services.add_link(
        actor=actor,
        request_id=ticket.pk,
        system=lis,
        record_type=record_type,
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


def _hold_system(system):
    return lambda: ExternalSystem.objects.select_for_update().get(pk=system.pk)


@pytest.fixture
def capable_lead(make_user):
    """A supervisor with the patient information capability."""
    user = make_user("r3_capable_lead", "supervisor")
    user.user_permissions.add(_capability())
    return user


# V2-01. The service's DoesNotExist or PermissionDenied is a 404 or 403 page, never a 500 -------


def _person_link_edit(capable_lead, restricted_ticket, lis):
    """A patient link on a restricted ticket, the supervisor's signed-in browser, the edit page's
    address and what it posts."""
    attachment = _link_attachment(
        capable_lead, restricted_ticket, lis, "PAT-TEST-000081", "patient"
    )
    link = ExternalRecordLink.objects.get(pk=attachment.link_id)
    http = _signed_in(capable_lead)
    url = reverse("link_records:link-edit", args=[link.pk])
    data = {
        "display_label": "Alias for the patient",
        "reason_code": "other",
        "expected_revision": link.revision,
    }
    return link, http, url, data


def test_a_demotion_waits_for_a_person_link_edit_under_way_on_a_restricted_ticket(
    capable_lead, restricted_ticket, lis
):
    """A person-level link is editable by a reader of one of its carrier tickets with the
    capability. The edit waits for the system's lock; meanwhile the supervisor is demoted and
    would no longer read the restricted carrier ticket. The edit holds the account's roles until
    it commits (accounts.acting), so the demotion waits behind it. The edit is saved first (the
    label changes, an event is written), by an account that still had its role, and the
    supervisor role is gone afterwards."""
    link, http, url, data = _person_link_edit(capable_lead, restricted_ticket, lis)
    state, demotion_state, behind_edit, session, demotion = _held_with_access_change(
        _hold_system(lis), lambda: http.post(url, data), _demote(capable_lead)
    )
    assert state == "waiting", state  # for the system
    assert (demotion_state, behind_edit) == ("waiting", True)
    assert (session.outcome, demotion.outcome) == ("done", "done"), repr(session.error)
    assert session.result.status_code in (200, 302), session.result.status_code
    after = ExternalRecordLink.objects.get(pk=link.pk)
    assert (after.display_label, after.revision) == ("Alias for the patient", link.revision + 1)
    assert AuditEvent.objects.filter(action="external_link.updated").exists()
    roles = set(User.objects.get(pk=capable_lead.pk).groups.values_list("name", flat=True))
    assert roles == {"technical"}


def test_a_person_link_edit_whose_carrier_ticket_left_the_editors_reach_before_it_began_is_404(
    capable_lead, restricted_ticket, lis
):
    """The supervisor was demoted (and the demotion committed) before the edit began, so they no
    longer read the restricted carrier ticket. The edit is answered as 404 (links/views.py), not
    500. Nothing is saved: the label and revision stay."""
    link, http, url, data = _person_link_edit(capable_lead, restricted_ticket, lis)
    _demote(capable_lead)()
    response = http.post(url, data)
    assert response.status_code == 404, response.status_code
    after = ExternalRecordLink.objects.get(pk=link.pk)
    assert (after.display_label, after.revision) == (link.display_label, link.revision)
    assert not AuditEvent.objects.filter(action="external_link.updated").exists()


def test_a_client_link_page_whose_account_was_deactivated_while_it_waited_bypassing_save_is_403(
    clerk, clinic, lis
):
    """Adding a link to a client organization locks the organization, the system, the link, then
    reads the account again (current_staff). The account is deactivated while the addition waits
    for the organization's lock, by a path that bypasses User.save (a queryset update), which the
    hold of accounts.acting does not make wait. The reading after the locks refuses it: the
    service raises PermissionDenied, which the page answers as 403 (not 500). Nothing is attached
    to the organization."""
    http = _signed_in(clerk)
    url = reverse("link_records:client-link-add", args=[clinic.pk])
    data = {
        "system": lis.pk,
        "record_type": "order",
        "identifier": "ORD-TEST-000082",
        "role": "related",
    }
    state, session = _held_while(
        lambda: ClientOrg.objects.select_for_update().get(pk=clinic.pk),
        lambda: http.post(url, data),
        _deactivate_bypassing_save(clerk),
    )
    assert state == "waiting", state  # for the organization
    assert session.outcome == "done", repr(session.error)
    assert session.result.status_code == 403
    assert not LinkAttachment.objects.filter(client=clinic).exists()
    assert not ExternalRecordLink.objects.filter(identifier="ORD-TEST-000082").exists()


def _withdrawal(lead, agent, category, quality):
    """An attachment of an unrestricted ticket, the supervisor's signed-in browser, the withdraw
    page's address and the ticket's timeline entries so far."""
    ticket = new_request(agent, category, team=quality)
    attachment = _file_on(lead, ticket)
    http = _signed_in(lead)
    url = reverse("attachments:withdraw", args=[attachment.pk])
    return ticket, attachment, http, url, RequestEntry.objects.filter(request=ticket).count()


def test_a_demotion_waits_for_a_file_withdrawal_by_a_supervisor_under_way(
    lead, agent, category, quality
):
    """Withdrawing a file locks the attachment, then the ticket (here an unrestricted one, still
    readable by technical staff), then rereads the account. The supervisor is demoted while the
    withdrawal waits for the ticket. The withdrawal holds the account's roles until it commits
    (accounts.acting), so the demotion waits behind it. The file is withdrawn first, by an
    account that still was a supervisor, with a timeline entry; the role is gone afterwards."""
    ticket, attachment, http, url, entries = _withdrawal(lead, agent, category, quality)
    state, demotion_state, behind_withdrawal, session, demotion = _held_with_access_change(
        _hold_ticket(ticket),
        lambda: http.post(url, {"reason": "Wrong file"}),
        _demote(lead),
    )
    assert state == "waiting", state  # for the ticket
    assert (demotion_state, behind_withdrawal) == ("waiting", True)
    assert (session.outcome, demotion.outcome) == ("done", "done"), repr(session.error)
    assert session.result.status_code in (200, 302), session.result.status_code
    assert Attachment.objects.get(pk=attachment.pk).withdrawn_at is not None
    assert RequestEntry.objects.filter(request=ticket).count() == entries + 1
    roles = set(User.objects.get(pk=lead.pk).groups.values_list("name", flat=True))
    assert roles == {"technical"}


def test_a_file_withdrawal_by_a_supervisor_demoted_before_it_began_is_refused_not_a_500(
    lead, agent, category, quality
):
    """The supervisor was demoted (and the demotion committed) before the withdrawal began: it
    is refused (PermissionDenied: 403, or a 404). Never a 500, and the file stays unwithdrawn
    with no timeline entry."""
    ticket, attachment, http, url, entries = _withdrawal(lead, agent, category, quality)
    _demote(lead)()
    response = http.post(url, {"reason": "Wrong file"})
    assert response.status_code in (403, 404), response.status_code
    assert Attachment.objects.get(pk=attachment.pk).withdrawn_at is None
    assert RequestEntry.objects.filter(request=ticket).count() == entries


# V2-02. The account is read again after the change's last lock ----------------------------------


@pytest.mark.parametrize("change", ["remove", "show"])
def test_a_link_change_waiting_for_the_attachments_lock_refuses_a_bypassing_deactivation(
    change, clerk, agent, category, quality, lis
):
    """remove_link and set_shown_to_client lock the ticket, then the attachment with its link,
    and read the account again after those (links.services._locked_attachment). Another session
    holds the attachment while the ticket is free, so the change waits for the attachment, after
    the ticket's lock; the account is deactivated meanwhile by a path that bypasses User.save (a
    queryset update), which the hold of accounts.acting does not make wait. The reading taken
    after the last lock refuses it (PermissionDenied); the attachment is unchanged and no entry
    is written."""
    ticket = new_request(agent, category, team=quality)
    attachment = _link_attachment(agent, ticket, lis, "ORD-TEST-000091")
    entries = RequestEntry.objects.filter(request=ticket).count()
    if change == "remove":

        def work():
            link_services.remove_link(actor=clerk, attachment_id=attachment.pk, reason="Wrong")
    else:

        def work():
            link_services.set_shown_to_client(actor=clerk, attachment_id=attachment.pk, shown=True)

    state, session = _held_while(
        lambda: LinkAttachment.objects.select_for_update().get(pk=attachment.pk),
        work,
        _deactivate_bypassing_save(clerk),
    )
    assert state == "waiting", state  # for the attachment
    assert session.outcome == "denied", (session.outcome, repr(session.error))
    current = LinkAttachment.objects.get(pk=attachment.pk)
    assert (current.removed_at, current.shown_to_client) == (None, False)
    assert RequestEntry.objects.filter(request=ticket).count() == entries


def test_a_link_correction_waiting_for_its_system_refuses_an_account_deactivated_bypassing_save(
    clerk, agent, category, quality, lis
):
    """update_link locks the system, then the link, then reads the account again. It waits for
    the system's lock while the account is deactivated by a path that bypasses User.save (a
    queryset update), which the hold of accounts.acting does not make wait: refused
    (PermissionDenied); the label and revision stay and no event is written."""
    ticket = new_request(agent, category, team=quality)
    attachment = _link_attachment(agent, ticket, lis, "ORD-TEST-000092")
    link = ExternalRecordLink.objects.get(pk=attachment.link_id)
    state, session = _held_while(
        _hold_system(lis),
        lambda: link_services.update_link(
            actor=clerk,
            link_id=link.pk,
            expected_revision=link.revision,
            display_label="Order 92",
            reason="Clearer label",
        ),
        _deactivate_bypassing_save(clerk),
    )
    assert state == "waiting", state  # for the system
    assert session.outcome == "denied", (session.outcome, repr(session.error))
    after = ExternalRecordLink.objects.get(pk=link.pk)
    assert (after.display_label, after.revision) == (link.display_label, link.revision)
    assert not AuditEvent.objects.filter(action="external_link.updated").exists()


def test_adding_a_link_to_an_organization_waiting_for_the_system_refuses_a_bypassing_deactivation(
    clerk, clinic, lis
):
    """add_link to an organization locks the organization, the system, the link, then reads the
    account again. It waits for the system while the account is deactivated by a path that
    bypasses User.save (a queryset update), which the hold of accounts.acting does not make
    wait: refused (PermissionDenied); nothing is attached and no link row is created."""
    state, session = _held_while(
        _hold_system(lis),
        lambda: link_services.add_link(
            actor=clerk,
            client_id=clinic.pk,
            system=lis,
            record_type="order",
            identifier="ORD-TEST-000093",
        ),
        _deactivate_bypassing_save(clerk),
    )
    assert state == "waiting", state  # for the system
    assert session.outcome == "denied", (session.outcome, repr(session.error))
    assert not LinkAttachment.objects.filter(client=clinic).exists()
    assert not ExternalRecordLink.objects.filter(identifier="ORD-TEST-000093").exists()


def _patient_correction(capable, category, quality):
    """A ticket with a patient-information value, and the correction of it: (ticket, the value's
    row, what it changes)."""
    template = RequestTemplate.objects.create(
        key="r3-patient", name="Patient form", category=category, offered_in_portal=False
    )
    template.fields.create(
        key="patient_ref",
        label="Patient",
        kind=FieldKind.SHORT_TEXT,
        holds_patient_information=True,
    )
    ticket = create_request_with_form(
        actor=capable,
        template=template,
        expected_definition=definition.token(template),
        values={"patient_ref": "PAT-TEST-000094"},
        first_entry_kind=EntryKind.NOTE,
        channel="phone",
        team=quality,
    )
    row = FieldValue.objects.get(filled_form__request=ticket, superseded_at__isnull=True)
    token = definition.token(template)

    def correct():
        return correct_values(
            actor=capable,
            request=ticket,
            expected_definition=token,
            values={"patient_ref": "PAT-TEST-000095"},
            reason="",
            reason_code="typing",
            expected={"patient_ref": row.pk},
        )

    return ticket, row, correct


def test_a_capability_revocation_waits_for_a_patient_value_correction_under_way(
    capable, category, quality, notice_in_category
):
    """correct_values reads the account again after the form definition's locks (its category
    among them). A staff notice being sent holds the category FOR SHARE, so the correction waits
    there; the patient information capability is revoked meanwhile. The correction holds the
    account's grants until it commits (accounts.acting), so the revocation waits behind it. The
    correction is saved first, by an account that still had the capability (the new patient value
    is current, the old one superseded), and the capability is gone afterwards."""
    ticket, row, correct = _patient_correction(capable, category, quality)
    revision = _fresh(ticket).revision
    sending = Sending(notice_in_category)
    state, revoke_state, behind_correction, session, revoke = _release_after_access_change(
        sending.release, correct, _revoke(capable)
    )
    assert state == "waiting", (state, session.outcome, repr(session.error))  # the category
    assert (revoke_state, behind_correction) == ("waiting", True)
    assert (session.outcome, revoke.outcome) == ("done", "done"), repr(session.error)
    assert FieldValue.objects.get(pk=row.pk).superseded_at is not None
    current = FieldValue.objects.get(filled_form__request=ticket, superseded_at__isnull=True)
    assert current.value == "PAT-TEST-000095"
    assert _fresh(ticket).revision > revision
    assert not User.objects.get(pk=capable.pk).user_permissions.filter(pk=_capability().pk).exists()


def test_a_patient_value_correction_is_not_saved_by_an_account_that_lost_the_capability(
    capable, category, quality
):
    """The patient information capability was revoked (and the revocation committed) before the
    correction began, while the caller still holds the account as it was. The correction reads
    the account again and is refused (nothing changed, or PermissionDenied): the stored patient
    value stays current, with no new value row, timeline entry or revision."""
    ticket, row, correct = _patient_correction(capable, category, quality)
    before = (_fresh(ticket).revision, FieldValue.objects.count(), RequestEntry.objects.count())
    _revoke(capable)()
    outcome = run_here(correct)
    assert outcome in ("refused", "denied"), outcome
    assert FieldValue.objects.get(pk=row.pk).superseded_at is None
    assert before == (
        _fresh(ticket).revision,
        FieldValue.objects.count(),
        RequestEntry.objects.count(),
    )


# V2-03. Ticket creation reads the creating account again after its locks ------------------------


@pytest.fixture
def two_site_portal(clinic_portal, clinic):
    """The clinic's portal account, granted two sites (A and B)."""
    site_a = ClientSite.objects.create(client=clinic, code="SA", name="Site A")
    site_b = ClientSite.objects.create(client=clinic, code="SB", name="Site B")
    access = PortalAccess.objects.create(user=clinic_portal, client=clinic)
    access.sites.set([site_a, site_b])
    return SimpleNamespace(user=clinic_portal, access=access, a=site_a, b=site_b)


def _portal_raise(user, category, site):
    return lambda: services.create_request(
        actor=user,
        category=category,
        subject="Portal question",
        first_entry_kind=EntryKind.REPLY,
        first_entry_body="Where is the report for order ORD-TEST-000096?",
        channel="portal",
        site=site,
    )


def _raise_while_category_is_busy_with_access_change(notice, user, category, site, access_change):
    sending = Sending(notice)
    return _release_after_access_change(
        sending.release, _portal_raise(user, category, site), access_change
    )


def test_a_site_grant_drop_waits_for_a_portal_ticket_under_way_for_that_site(
    two_site_portal, category, notice_in_category
):
    """A portal account raises a ticket for site A while a notice being sent holds the category.
    During the wait its grant is narrowed to site B. The creation holds the account's access and
    site grants until it commits (accounts.acting), so the change of grant waits behind it. The
    ticket is saved first, for site A, by an account that still had that grant, with its creation
    event; the grant is gone afterwards."""
    created = AuditEvent.objects.filter(action="request.created").count()
    portal = two_site_portal
    state, drop_state, behind_creation, session, drop = (
        _raise_while_category_is_busy_with_access_change(
            notice_in_category,
            portal.user,
            category,
            portal.a,
            lambda: portal.access.sites.remove(portal.a),
        )
    )
    assert state == "waiting", state  # for the category
    assert (drop_state, behind_creation) == ("waiting", True)
    assert (session.outcome, drop.outcome) == ("done", "done"), repr(session.error)
    assert ServiceRequest.objects.get(subject="Portal question").site_id == portal.a.pk
    assert AuditEvent.objects.filter(action="request.created").count() == created + 1
    assert not portal.access.sites.filter(pk=portal.a.pk).exists()


def test_a_portal_ticket_for_a_site_the_grant_dropped_before_it_began_is_refused_on_the_site(
    two_site_portal, category
):
    """The portal account's grant of site A was dropped (and the change committed) before the
    creation began, while the caller still holds the account as it was. create_request reads the
    account again (_creator_after_locks, check_portal_site): ValidationError on `site`; no ticket
    and no creation event."""
    created = AuditEvent.objects.filter(action="request.created").count()
    tickets = ServiceRequest.objects.count()
    portal = two_site_portal
    portal.access.sites.remove(portal.a)
    session = Session(_portal_raise(portal.user, category, portal.a))
    session.start()
    session.join()
    assert session.outcome == "refused", (session.outcome, repr(session.error))
    assert "site" in session.error.message_dict
    assert ServiceRequest.objects.count() == tickets
    assert AuditEvent.objects.filter(action="request.created").count() == created


def _move_to_another_organization(account_pk, water, agent):
    def move():
        account = User.objects.get(pk=account_pk)
        account.client = water
        account.changed_by = agent
        account.save()

    return move


def test_a_move_to_another_organization_waits_for_a_portal_ticket_under_way(
    clinic, clinic_portal, water, agent, category, notice_in_category
):
    """A portal account raises a ticket while a notice being sent holds the category; meanwhile
    it is moved to another organization (changed_by set, as clients.signals asks). The creation
    holds the account until it commits (accounts.acting) and the move is a save, which locks the
    account FOR UPDATE first, so the move waits behind it. The ticket is saved first, for the
    organization the account belonged to when it began, and the account is moved afterwards."""
    created = AuditEvent.objects.filter(action="request.created").count()
    state, move_state, behind_creation, session, move = (
        _raise_while_category_is_busy_with_access_change(
            notice_in_category,
            clinic_portal,
            category,
            None,
            _move_to_another_organization(clinic_portal.pk, water, agent),
        )
    )
    assert state == "waiting", state  # for the category
    assert (move_state, behind_creation) == ("waiting", True)
    assert (session.outcome, move.outcome) == ("done", "done"), repr(session.error)
    assert ServiceRequest.objects.get(subject="Portal question").client_id == clinic.pk
    assert AuditEvent.objects.filter(action="request.created").count() == created + 1
    assert User.objects.get(pk=clinic_portal.pk).client_id == water.pk


def test_a_portal_ticket_by_an_account_moved_to_another_organization_before_it_began_is_denied(
    clinic_portal, water, agent, category
):
    """The portal account was moved to another organization (and the move committed) before the
    creation began, while the caller still holds the account as it was. The ticket would be
    recorded for the old organization: the reading has another client, so the creation is
    refused (PermissionDenied). Nothing is saved."""
    _move_to_another_organization(clinic_portal.pk, water, agent)()
    created = AuditEvent.objects.filter(action="request.created").count()
    tickets = ServiceRequest.objects.count()
    session = Session(_portal_raise(clinic_portal, category, None)).start()
    session.join()
    assert session.outcome == "denied", (session.outcome, repr(session.error))
    assert ServiceRequest.objects.count() == tickets
    assert AuditEvent.objects.filter(action="request.created").count() == created


def test_a_staff_ticket_whose_creator_was_deactivated_bypassing_save_while_it_waited_is_denied(
    clerk, category, quality
):
    """A clerk logs a ticket for the Quality team while another session holds the team's row. The
    clerk's account is deactivated during that wait by a path that bypasses User.save (a queryset
    update), which the hold of accounts.acting does not make wait. create_request reads the
    creator again after its last lock (current_staff): PermissionDenied; no ticket, entry or
    creation event."""
    created = AuditEvent.objects.filter(action="request.created").count()
    tickets = ServiceRequest.objects.count()
    state, session = _held_while(
        lambda: Team.objects.select_for_update().get(pk=quality.pk),
        lambda: new_request(clerk, category, subject="Team wait", team=quality),
        _deactivate_bypassing_save(clerk),
    )
    assert state == "waiting", state  # for the team
    assert session.outcome == "denied", (session.outcome, repr(session.error))
    assert ServiceRequest.objects.count() == tickets
    assert AuditEvent.objects.filter(action="request.created").count() == created


# V2-04. Link changes lock the ticket, then the system, then the link ----------------------------


def test_the_same_record_added_to_two_tickets_at_once_serializes_without_a_deadlock(
    clerk, agent, category, quality, lis
):
    """Two clerks add the same (new) record to two different tickets at once. Each locks its own
    ticket, then they meet at the system's lock and the link. The first, paused once it holds the
    system, makes the second wait for it; both then finish, none with a deadlock (40P01). One link
    row exists, attached to both tickets."""
    first_ticket = new_request(agent, category, team=quality, subject="First")
    second_ticket = new_request(agent, category, team=quality, subject="Second")
    seen, sessions = {}, {}

    def add(ticket):
        return lambda: link_services.add_link(
            actor=clerk,
            request_id=ticket.pk,
            system=lis,
            record_type="order",
            identifier="ORD-TEST-000097",
        )

    def during():
        sessions["second"] = Session(add(second_ticket)).start()
        seen["second"] = sessions["second"].waiting_or_ended()

    with paused(link_services, "_find_or_create_link", during):
        first = run_here(add(first_ticket))
    outcome = {"first": first, "second": sessions["second"].join()}
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}"
    assert outcome == {"first": "done", "second": "done"}, outcome
    assert seen["second"] == "waiting", seen  # for the system
    assert ExternalRecordLink.objects.filter(identifier="ORD-TEST-000097").count() == 1
    assert LinkAttachment.objects.filter(link__identifier="ORD-TEST-000097").count() == 2


@pytest.mark.parametrize("first", ["update", "remove"])
def test_correcting_a_link_and_removing_its_attachment_at_once_never_deadlock(
    first, clerk, make_user, agent, category, quality, lis
):
    """One clerk corrects a link's label (update_link: the system, then the link) while a
    colleague removes the link's attachment (remove_link: the ticket, then the attachment with its
    link). Whichever starts first is paused once it holds all its locks, until the other waits;
    then it goes on. Neither ends with a deadlock (40P01)."""
    colleague = make_user("r3_colleague", "client_services")
    ticket = new_request(agent, category, team=quality)
    attachment = _link_attachment(colleague, ticket, lis, "ORD-TEST-000098")
    link = ExternalRecordLink.objects.get(pk=attachment.link_id)

    def update():
        link_services.update_link(
            actor=clerk,
            link_id=link.pk,
            expected_revision=link.revision,
            display_label="Order 98",
            reason="Clearer label",
        )

    def remove():
        link_services.remove_link(actor=colleague, attachment_id=attachment.pk, reason="Wrong")

    work = {"update": update, "remove": remove}
    other_name = "remove" if first == "update" else "update"
    sessions, seen = {}, {}

    def during():
        sessions[other_name] = Session(work[other_name]).start()
        seen[other_name] = sessions[other_name].waiting_or_ended()

    # record_event is the first thing either does once all its locks are held.
    with paused(link_services, "record_event", during):
        outcome = {first: run_here(work[first])}
    outcome[other_name] = sessions[other_name].join()
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}, during the pause {seen}"
    assert seen[other_name] == "waiting", seen
    assert set(outcome.values()) <= {"done", "refused"}, outcome


@pytest.mark.parametrize("first", ["add", "remove"])
def test_adding_and_removing_the_same_record_on_a_busy_ticket_never_deadlock(
    first, clerk, make_user, notice_in_category, lis
):
    """A ticket carries an order link and is busy: a staff notice being sent holds it FOR SHARE.
    One clerk adds the same order again while a colleague removes it, started in either order;
    both wait for the ticket (add_link, and _locked_attachment, which reads the attachment's
    ticket without a lock) and neither holds the link meanwhile. When the ticket is free they go
    on one after the other: no deadlock (40P01), each either done or refused."""
    colleague = make_user("r3_colleague", "client_services")
    ticket = ServiceRequest.objects.get()
    existing = _link_attachment(colleague, ticket, lis, "ORD-TEST-000099")
    work = {
        "add": lambda: link_services.add_link(
            actor=clerk,
            request_id=ticket.pk,
            system=lis,
            record_type="order",
            identifier="ORD-TEST-000099",
            role="primary",
        ),
        "remove": lambda: link_services.remove_link(
            actor=colleague, attachment_id=existing.pk, reason="Wrong role"
        ),
    }
    order = [first, "remove" if first == "add" else "add"]
    sending = Sending(notice_in_category)
    sessions, seen = {}, {}
    try:
        for name in order:
            sessions[name] = Session(work[name]).start()
            seen[name] = sessions[name].waiting_or_ended()
    finally:
        assert sending.release() == "done"
    outcome = {name: session.join() for name, session in sessions.items()}
    assert seen == {"add": "waiting", "remove": "waiting"}, seen  # for the ticket
    assert "40P01" not in outcome.values(), f"deadlock: {outcome}"
    assert set(outcome.values()) <= {"done", "refused"}, outcome
