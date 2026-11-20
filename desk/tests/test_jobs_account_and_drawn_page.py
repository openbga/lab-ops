"""The scheduled jobs' account on its first use, and ticket pages drawn while a change commits.

The review follow-up of "decisions read before a wait": its D5, D6 and D11 tests (adopted
unchanged from the review, .local/review-history) and the cases next to them. All records are
synthetic."""

import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.test import Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from clients.models import Client, PortalAccess
from core import changelog, concurrency, preferences
from core.models import AuditEvent, UserPreference
from desk import portal_views, views
from desk import services as portal_services
from desk.models import EntryKind, RequestEntry, ServiceRequest, Status
from jobs import actor as job_actor
from jobs.models import JobSettings
from links.models import ExternalSystem
from links.services import add_link, remove_link, set_shown_to_client, update_link
from links.templatetags import record_links
from service_levels import jobs as clock_jobs
from service_levels.models import RequestClock, ServiceTarget

from .test_contract import new_request, set_status
from .test_review_acting_account_until_commit import Gate, gated
from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by

pytestmark = pytest.mark.django_db(transaction=True)


def _while_change_is_open(holder, work):
    second = Session(work).start()
    try:
        state = second.waiting_or_ended(seconds=5)
        assert state in {"ended", "waiting"}, second.error
        if state == "waiting":
            assert _blocked_by(second.pid, holder.session.pid)
    finally:
        assert holder.release() == "done", holder.session.error
        second.join()
    return state, second


# ------------------------------------------------------------------- the review's tests (D5, D6)


def test_first_breach_job_with_a_concurrent_name_collision_can_defer_without_waiting(
    agent, category, accessioning, settings
):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    ServiceTarget.objects.create(name="Due this morning", resolution_minutes=20)
    ticket = new_request(agent, category, team=accessioning)
    RequestClock.objects.filter(request=ticket).update(
        resolution_due_at=timezone.now() - timedelta(minutes=3)
    )
    JobSettings.objects.create()
    person = Held(
        lambda: User.objects.create_user(job_actor.USERNAME, email="office-rep@example.invalid")
    )
    state, run = _while_change_is_open(person, clock_jobs.notice_breaches)
    later = clock_jobs.notice_breaches()
    assert RequestClock.objects.get(request=ticket).resolution_breach_noticed_at
    assert AuditEvent.objects.filter(action="jobs.system_account_created").count() == 1
    if run.outcome == "done":
        assert run.result["breaches"] + later["breaches"] == 1
    assert (state, run.outcome) == ("ended", "done"), (state, run.error)


@pytest.mark.parametrize("field", ["username", "email"])
def test_first_job_account_keeps_a_case_distinct_name_and_address_during_registration(field):
    JobSettings.objects.create()
    # Both spellings are handled case-insensitively when the existing person is already
    # committed (jobs/tests/test_actor.py). Starting their ordinary account at the same time
    # should not change how the two accounts are named.
    username = job_actor.USERNAME.title() if field == "username" else "confirm_coworker"
    base_email = f"{job_actor.USERNAME}@{job_actor.EMAIL_DOMAIN}"
    email = base_email.upper() if field == "email" else "confirm-coworker@example.invalid"
    person = Held(lambda: User.objects.create_user(username, email=email))
    _state, creation = _while_change_is_open(person, job_actor.system_account)
    assert creation.outcome == "done", creation.error
    human = User.objects.get(username=username)
    system = creation.result
    assert system.pk == JobSettings.objects.get().system_account_id != human.pk
    assert human.is_active and not system.is_active
    created = AuditEvent.objects.get(action="jobs.system_account_created")
    assert created.actor_id == system.pk
    assert getattr(system, field).casefold() != getattr(human, field).casefold(), (
        getattr(system, field),
        getattr(human, field),
    )


# ------------------------------------------------------------- the jobs' account: next to D5, D6

BASE_EMAIL = f"{job_actor.USERNAME}@{job_actor.EMAIL_DOMAIN}"
BEING_CREATED = {
    "name": (job_actor.USERNAME, "neighbour-rep@example.invalid"),
    "name in capitals": (job_actor.USERNAME.upper(), "neighbour-rep@example.invalid"),
    "address": ("neighbour_rep", BASE_EMAIL),
    "address in capitals": ("neighbour_rep", BASE_EMAIL.upper()),
    "both, mixed case": (job_actor.USERNAME.title(), BASE_EMAIL.title()),
}


@pytest.mark.parametrize("taken", list(BEING_CREATED), ids=list(BEING_CREATED))
@pytest.mark.parametrize("nowait", [True, False], ids=["breach job's form", "waiting form"])
def test_a_person_being_created_with_the_preferred_identity_never_delays_the_jobs_account(
    taken, nowait
):
    """Either form of the lookup (the breach job asks without waiting, the automatic close
    waits for the settings row) ends at once, with a name and address of its own."""
    JobSettings.objects.create()
    username, email = BEING_CREATED[taken]
    person = Held(lambda: User.objects.create_user(username, email=email))
    state, creation = _while_change_is_open(person, lambda: job_actor.system_account(nowait=nowait))
    assert (state, creation.outcome) == ("ended", "done"), (state, creation.error)
    human, system = User.objects.get(username=username), creation.result
    assert system.pk == JobSettings.objects.get().system_account_id != human.pk
    assert human.is_active and human.email == User.objects.normalize_email(email)
    assert not system.is_active
    for field in ("username", "email"):
        assert getattr(system, field).casefold() != getattr(human, field).casefold()
    assert AuditEvent.objects.filter(action="jobs.system_account_created").count() == 1


def test_the_jobs_account_name_and_address_carry_a_random_suffix():
    system = job_actor.system_account()
    assert re.fullmatch(rf"{job_actor.USERNAME}-[0-9a-f]{{8}}", system.username), system.username
    assert system.email == f"{system.username}@{job_actor.EMAIL_DOMAIN}"
    drawn = {job_actor._identity() for _ in range(5)}
    assert len(drawn) == 5  # chosen at random each time, not from what is saved


@pytest.mark.parametrize("field", ["username", "email"])
def test_a_saved_account_holding_the_drawn_identity_in_another_case_is_passed_over(
    field, monkeypatch, make_user
):
    suffixes = iter(["0a1b2c3d", "4e5f6a7b"])
    monkeypatch.setattr(job_actor, "secrets", SimpleNamespace(token_hex=lambda n: next(suffixes)))
    person = make_user("neighbour_holder", "client_services")
    first = f"{job_actor.USERNAME}-0a1b2c3d"
    value = first.upper() if field == "username" else f"{first}@{job_actor.EMAIL_DOMAIN}".upper()
    User.objects.filter(pk=person.pk).update(**{field: value})
    system = job_actor.system_account()
    assert system.username == f"{job_actor.USERNAME}-4e5f6a7b"
    assert system.email == f"{job_actor.USERNAME}-4e5f6a7b@{job_actor.EMAIL_DOMAIN}"
    person.refresh_from_db()
    assert getattr(person, field) == value and person.is_active


def test_an_account_saved_with_the_name_between_the_check_and_the_insert_makes_it_choose_again(
    monkeypatch,
):
    real = job_actor._identity
    calls = []

    def identity_then_taken():
        username, email = real()
        if not calls:
            # Saved after the check, before the insert (here in the same transaction, which
            # the check could not have seen).
            User.objects.create_user(username, email="neighbour-late@example.invalid")
        calls.append(username)
        return username, email

    monkeypatch.setattr(job_actor, "_identity", identity_then_taken)
    system = job_actor.system_account()
    assert len(calls) == 2 and system.username == calls[1] != calls[0]
    assert User.objects.get(username=calls[0]).email == "neighbour-late@example.invalid"
    assert JobSettings.objects.get().system_account_id == system.pk
    assert AuditEvent.objects.filter(action="jobs.system_account_created").count() == 1


def test_an_existing_jobs_account_keeps_its_name(make_user):
    """Deployments whose jobs' account was created before keep it: it is never renamed."""
    old = User.objects.create_user(job_actor.USERNAME, email=BASE_EMAIL, is_active=False)
    JobSettings.objects.create(system_account=old)
    assert job_actor.system_account().pk == old.pk
    old.refresh_from_db()
    assert (old.username, old.email) == (job_actor.USERNAME, BASE_EMAIL)
    assert not AuditEvent.objects.filter(action="jobs.system_account_created").exists()


# ------------------------------------------------------------------ the review's test (D11)


@pytest.fixture
def confirm_two_portal(client, agent, category, clinic, clinic_portal):
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    ticket = new_request(agent, category, client=clinic)
    client.force_login(clinic_portal)
    page = client.get(reverse("portal:request-detail", args=[ticket.number]))
    return (
        ticket,
        page.context["reply_url"],
        page.context["reply_form"]["expected_revision"].value(),
    )


def _confirm_two_list_ticket(client, response, ticket):
    """Follow the Tickets breadcrumb, then the link displayed for this ticket."""
    listing = response.context["list_url"]
    assert f'href="{listing}"'.encode() in response.content
    result = client.get(listing)
    assert result.status_code == 200
    detail = reverse("portal:request-detail", args=[ticket.number])
    assert f'href="{detail}">{ticket.number}</a>'.encode() in result.content
    return detail


def _confirm_two_staff_update(agent, ticket, text):
    ticket.refresh_from_db()
    return portal_services.add_entry(
        actor=agent,
        request=ticket,
        kind=EntryKind.REPLY,
        body=text,
        expected_revision=ticket.revision,
    )


def _confirm_two_fresh_send(client, page, text, files=None):
    fields = {
        "body": text,
        "expected_revision": page.context["reply_form"]["expected_revision"].value(),
    }
    if files is not None:
        fields["files"] = files
    return client.post(page.context["reply_url"], fields)


def test_revision_on_reopened_ticket_matches_the_conversation_shown_for_recovery(
    client, agent, confirm_two_portal
):
    ticket, url, initial = confirm_two_portal
    _confirm_two_staff_update(agent, ticket, "We have received the package.")
    ticket.refresh_from_db()
    earlier = ticket.revision
    draft = "The courier has ORD-TEST-000707."
    refused = client.post(url, {"body": draft, "expected_revision": initial})
    assert refused.status_code == 200 and refused.context["reply_form"].non_field_errors()
    detail = _confirm_two_list_ticket(client, refused, ticket)
    late_text = "The courier has checked the label."
    gate, gates = Gate(), {}
    with gated(portal_views, "_conversation", gates):
        read = Session(lambda: client.get(detail))
        gates[read.thread] = gate
        read.start()
        change = None
        try:
            assert gate.arrived.wait(timeout=5), read.error
            change = Session(lambda: _confirm_two_staff_update(agent, ticket, late_text)).start()
            state = change.waiting_or_ended(seconds=5)
            if state == "waiting":
                assert _blocked_by(change.pid, read.pid)
            else:
                assert state == "ended", change.error
        finally:
            gate.go.set()
            read.join()
            if change is not None:
                change.join()
    assert read.outcome == change.outcome == "done", (read.error, change.error)
    fresh = read.result
    assert b"We have received the package." in fresh.content
    ticket.refresh_from_db()
    saw_change = late_text.encode() in fresh.content
    hidden = int(fresh.context["reply_form"]["expected_revision"].value())
    sent = _confirm_two_fresh_send(client, fresh, draft)
    # An update after a consistent reading should refuse; an update that was already shown
    # should be the revision that page posts.
    expected = (ticket.revision, 302) if saw_change else (earlier, 200)
    assert (hidden, sent.status_code) == expected, {"latest_reply_visible": saw_change}


# ---------------------------------------------------------------- ticket pages: next to D11


def _drawn_while(module, name, page, change):
    """GET `page` in its own session, stopping at module.`name`; `change` commits meanwhile in
    another session. Both must finish without waiting for each other."""
    gate, gates = Gate(), {}
    with gated(module, name, gates):
        read = Session(page)
        gates[read.thread] = gate
        read.start()
        writer = None
        try:
            assert gate.arrived.wait(timeout=5), read.error
            writer = Session(change).start()
            assert writer.waiting_or_ended(seconds=5) == "ended", writer.error
        finally:
            gate.go.set()
            read.join()
            if writer is not None:
                writer.join()
    assert writer.outcome == "done", writer.error
    assert read.outcome == "done", read.error
    return read.result


def test_portal_page_drawn_after_a_staff_reply_shows_it_and_takes_a_reply(
    client, agent, confirm_two_portal
):
    ticket, _url, initial = confirm_two_portal
    _confirm_two_staff_update(agent, ticket, "The package arrived this morning.")
    page = client.get(reverse("portal:request-detail", args=[ticket.number]))
    ticket.refresh_from_db()
    assert b"The package arrived this morning." in page.content
    assert int(page.context["reply_form"]["expected_revision"].value()) == ticket.revision
    assert ticket.revision == int(initial) + 1
    sent = _confirm_two_fresh_send(client, page, "Thank you, ORD-TEST-000708 is the order.")
    assert sent.status_code == 302
    assert RequestEntry.objects.filter(
        request=ticket, body="Thank you, ORD-TEST-000708 is the order."
    ).exists()


@pytest.mark.parametrize("change", ["deactivated", "moved"])
def test_portal_page_reads_the_account_in_its_own_reading(
    client, confirm_two_portal, clinic_portal, change, make_user
):
    """An account deactivated, or moved to another organization, after the request was signed
    in but before the page's reading is refused: the page is never drawn for it."""
    ticket, _url, _initial = confirm_two_portal
    other = Client.objects.create(name="Example Hospital", account_number="EXHOSP")
    owner = make_user("neighbour_owner", "admin")

    def end_access():
        account = User.objects.get(pk=clinic_portal.pk)
        account.changed_by = owner  # as the account administration saves it
        if change == "deactivated":
            account.is_active = False
            account.save(update_fields=["is_active"])
        else:
            account.client = other
            account.save(update_fields=["client"])

    page = _drawn_while(
        portal_views,
        "reading_account",
        lambda: client.get(reverse("portal:request-detail", args=[ticket.number])),
        end_access,
    )
    assert page.status_code == (403 if change == "deactivated" else 404)
    assert b"ORD-TEST" not in page.content and ticket.subject.encode() not in page.content


@pytest.fixture
def staff_ticket(client, agent, category, clinic, clinic_portal):
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    ticket = new_request(agent, category, client=clinic)
    client.force_login(agent)
    return ticket


def _client_reply(clinic_portal, ticket, text):
    ticket.refresh_from_db()
    return portal_services.add_entry(
        actor=clinic_portal,
        request=ticket,
        kind=EntryKind.REPLY,
        body=text,
        expected_revision=ticket.revision,
    )


def _staff_note(client, ticket, page, text):
    return client.post(
        reverse("desk:request-entry", args=[ticket.pk]),
        {
            "kind": EntryKind.NOTE,
            "body": text,
            "expected_revision": page.context["composer"]["expected_revision"].value(),
        },
    )


def test_staff_page_drawn_while_a_client_reply_commits_carries_the_revision_it_shows(
    client, staff_ticket, clinic_portal
):
    ticket = staff_ticket
    ticket.refresh_from_db()
    earlier = ticket.revision
    late = "We sent the requisition with ORD-TEST-000709."
    page = _drawn_while(
        views,
        "with_files",
        lambda: client.get(reverse("desk:request-detail", args=[ticket.pk])),
        lambda: _client_reply(clinic_portal, ticket, late),
    )
    assert page.status_code == 200
    ticket.refresh_from_db()
    assert ticket.revision == earlier + 1
    saw_change = late.encode() in page.content
    hidden = int(page.context["composer"]["expected_revision"].value())
    sent = _staff_note(client, ticket, page, "Checked the requisition.")
    expected = (ticket.revision, 302) if saw_change else (earlier, 400)
    assert (hidden, sent.status_code) == expected, {"latest_reply_visible": saw_change}


def test_staff_page_links_and_timeline_come_from_one_reading(client, staff_ticket, agent):
    """Parts drawn by template tags (the links panel) read in the same reading as the
    timeline: a link added while the page is drawn appears in both or in neither."""
    ticket = staff_ticket
    lims = ExternalSystem.objects.create(key="lims", name="Example LIMS", kind="lims")

    def attach():
        return add_link(
            actor=User.objects.get(pk=agent.pk),
            system=lims,
            record_type="order",
            identifier="ORD-TEST-000910",
            request_id=ticket.pk,
        )

    page = _drawn_while(
        record_links,
        "request_link_rows",
        lambda: client.get(reverse("desk:request-detail", args=[ticket.pk])),
        attach,
    )
    assert page.status_code == 200
    in_panel = b"ORD-TEST-000910" in page.content
    in_timeline = b"Link added: order" in page.content
    assert in_panel == in_timeline, {"panel": in_panel, "timeline": in_timeline}
    again = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    assert b"ORD-TEST-000910" in again.content and b"Link added: order" in again.content


def test_staff_page_reads_the_account_in_its_own_reading(client, staff_ticket, agent):
    ticket = staff_ticket

    def remove_role():
        account = User.objects.get(pk=agent.pk)
        account.groups.remove(Group.objects.get(name="client_services"))

    page = _drawn_while(
        views,
        "reading_account",
        lambda: client.get(reverse("desk:request-detail", args=[ticket.pk])),
        remove_role,
    )
    assert page.status_code == 403
    assert ticket.subject.encode() not in page.content


def test_a_refused_staff_entry_is_drawn_from_one_reading(client, staff_ticket, clinic_portal):
    """A note refused for a changed ticket draws the page again from one reading: the other
    forms on it carry the revision of the timeline shown."""
    ticket = staff_ticket
    page = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    _client_reply(clinic_portal, ticket, "The courier left at noon.")
    late = "The courier is back with ORD-TEST-000711."
    refused = _drawn_while(
        views,
        "with_files",
        lambda: _staff_note(client, ticket, page, "Before the reply."),
        lambda: _client_reply(clinic_portal, ticket, late),
    )
    assert refused.status_code == 400
    ticket.refresh_from_db()
    assert b"The courier left at noon." in refused.content
    assert late.encode() not in refused.content
    assert int(refused.context["req"].revision) == ticket.revision - 1


# ------------------------------------------------------- what drawing writes: after the reading


def test_a_first_staff_page_inside_its_reading_records_whats_new_seen(
    client, staff_ticket, make_user
):
    newcomer = make_user("neighbour_newcomer", "client_services")
    client.force_login(newcomer)
    assert not UserPreference.objects.filter(user=newcomer).exists()
    page = client.get(reverse("desk:request-detail", args=[staff_ticket.pk]))
    assert page.status_code == 200
    stored = UserPreference.objects.get(user=newcomer, key=preferences.WHATS_NEW_KEY)
    assert stored.value == changelog.seen_marker(changelog.entries())


def test_after_reading_runs_once_the_pages_own_reading_has_ended():
    calls = []
    with concurrency.one_reading():
        concurrency.after_reading(lambda: calls.append("inside"))
        assert calls == []
    assert calls == ["inside"]
    concurrency.after_reading(lambda: calls.append("outside"))
    assert calls == ["inside", "outside"]
    with transaction.atomic():  # a transaction the reading did not open: at once, as before
        with concurrency.one_reading():
            concurrency.after_reading(lambda: calls.append("nested"))
            assert calls[-1] == "nested"
    with pytest.raises(PermissionDenied), concurrency.one_reading():
        concurrency.after_reading(lambda: calls.append("refused"))
        raise PermissionDenied()
    assert "refused" not in calls
    concurrency.after_reading(lambda: calls.append("later"))
    assert calls[-1] == "later"  # the reading's mark did not outlive it


def test_portal_page_for_a_client_writes_nothing(client, confirm_two_portal, clinic_portal):
    ticket, _url, _initial = confirm_two_portal
    before = UserPreference.objects.filter(user=clinic_portal).count()
    assert client.get(reverse("portal:request-detail", args=[ticket.number])).status_code == 200
    assert UserPreference.objects.filter(user=clinic_portal).count() == before


def test_staff_page_from_another_session_is_unaffected(staff_ticket, make_user):
    """Two people drawing the same ticket at once each get a whole page."""
    other = make_user("neighbour_reader", "client_services")
    first, second = HttpClient(), HttpClient()
    first.force_login(other)
    second.force_login(other)
    url = reverse("desk:request-detail", args=[staff_ticket.pk])
    pages = [Session(lambda c=c: c.get(url)).start() for c in (first, second)]
    for page in pages:
        page.join()
        assert page.outcome == "done" and page.result.status_code == 200, page.error


# ------------------------------------- review round 1: the cases next to its findings (1-9)


def _revision(ticket):
    return ServiceRequest.objects.values_list("revision", flat=True).get(pk=ticket.pk)


@pytest.fixture
def linked_ticket(staff_ticket, agent):
    lims = ExternalSystem.objects.create(key="lims", name="Example LIMS", kind="lims")
    attachment, _ = add_link(
        actor=agent,
        system=lims,
        record_type="order",
        identifier="ORD-TEST-000920",
        request_id=staff_ticket.pk,
    )
    staff_ticket.refresh_from_db()
    return staff_ticket, lims, attachment


def test_each_change_of_a_tickets_links_advances_its_revision_once(linked_ticket, agent):
    """Finding 2: attaching, showing, hiding and removing a ticket's link are changes to the
    ticket; a refused change advances nothing."""
    ticket, lims, attachment = linked_ticket
    start = _revision(ticket)
    set_shown_to_client(actor=agent, attachment_id=attachment.pk, shown=True)
    assert _revision(ticket) == start + 1
    set_shown_to_client(actor=agent, attachment_id=attachment.pk, shown=False)
    assert _revision(ticket) == start + 2
    with pytest.raises(ValidationError):
        set_shown_to_client(actor=agent, attachment_id=attachment.pk, shown=False)
    assert _revision(ticket) == start + 2
    remove_link(actor=agent, attachment_id=attachment.pk, reason="Entered on the wrong ticket")
    assert _revision(ticket) == start + 3
    with pytest.raises(ValidationError):
        remove_link(actor=agent, attachment_id=attachment.pk, reason="Entered twice")
    assert _revision(ticket) == start + 3
    add_link(
        actor=agent,
        system=lims,
        record_type="sample",
        identifier="ORD-TEST-000921",
        request_id=ticket.pk,
    )
    assert _revision(ticket) == start + 4


def test_an_organizations_link_and_a_links_own_edit_change_no_ticket(
    linked_ticket, agent, clinic, make_user
):
    """Only a change to the ticket's own links advances it: an organization's link, or a
    correction of a link's label (the link's own record, with its own revision), does not."""
    ticket, lims, attachment = linked_ticket
    start = _revision(ticket)
    add_link(
        actor=agent,
        system=lims,
        record_type="project",
        identifier="ORD-TEST-000922",
        client_id=clinic.pk,
    )
    link = attachment.link
    link.refresh_from_db()
    update_link(
        actor=agent,
        link_id=link.pk,
        expected_revision=link.revision,
        display_label="Order ORD-TEST-000920",
        reason="Clearer label",
    )
    assert _revision(ticket) == start


def test_staff_note_from_a_page_drawn_before_a_link_change_is_refused(client, linked_ticket, agent):
    ticket, lims, _attachment = linked_ticket
    page = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    add_link(
        actor=agent,
        system=lims,
        record_type="report",
        identifier="ORD-TEST-000923",
        request_id=ticket.pk,
    )
    refused = _staff_note(client, ticket, page, "Checked the order.")
    assert refused.status_code == 400
    assert not RequestEntry.objects.filter(request=ticket, body="Checked the order.").exists()
    again = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    assert b"ORD-TEST-000923" in again.content
    assert _staff_note(client, ticket, again, "Checked the order.").status_code == 302


def test_portal_reply_from_a_page_drawn_before_a_reference_is_hidden_is_refused(
    client, linked_ticket, agent, clinic_portal
):
    ticket, _lims, attachment = linked_ticket
    set_shown_to_client(actor=agent, attachment_id=attachment.pk, shown=True)
    client.force_login(clinic_portal)
    url = reverse("portal:request-detail", args=[ticket.number])
    page = client.get(url)
    assert b"ORD-TEST-000920" in page.content
    set_shown_to_client(actor=agent, attachment_id=attachment.pk, shown=False)
    text = "Is ORD-TEST-000920 the right order?"
    refused = _confirm_two_fresh_send(client, page, text)
    assert refused.status_code == 200 and refused.context["reply_form"].non_field_errors()
    assert not RequestEntry.objects.filter(request=ticket, body=text).exists()
    fresh = client.get(url)
    assert b"ORD-TEST-000920" not in fresh.content
    assert _confirm_two_fresh_send(client, fresh, text).status_code == 302


def _shown_form_revision(page, attachment):
    action = reverse("link_records:attachment-shown", args=[attachment.pk])
    html = page.content.decode()
    form = html[html.index(f'action="{action}"') :]
    form = form[: form.index("</form>")]
    return re.search(r'name="expected_revision" value="(\d+)"', form).group(1)


def test_show_to_client_from_the_current_page_works_and_advances_the_revision(
    client, linked_ticket
):
    """Finding 7: the Show/Hide form carries the ticket's revision; a current one is taken."""
    ticket, _lims, attachment = linked_ticket
    page = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    revision = _shown_form_revision(page, attachment)
    assert int(revision) == _revision(ticket)
    action = reverse("link_records:attachment-shown", args=[attachment.pk])
    posted = client.post(action, {"shown": "1", "expected_revision": revision}, follow=True)
    attachment.refresh_from_db()
    assert attachment.shown_to_client and _revision(ticket) == int(revision) + 1
    assert b"Shown to the client." in posted.content


@pytest.mark.parametrize("sent", [None, "0", "not-a-number"], ids=["missing", "zero", "text"])
def test_show_to_client_without_the_pages_revision_is_refused(client, linked_ticket, sent):
    """A form drawn before it carried the revision, or a made-up value, changes nothing."""
    ticket, _lims, attachment = linked_ticket
    start = _revision(ticket)
    fields = {"shown": "1"}
    if sent is not None:
        fields["expected_revision"] = sent
    action = reverse("link_records:attachment-shown", args=[attachment.pk])
    posted = client.post(action, fields, follow=True)
    attachment.refresh_from_db()
    assert not attachment.shown_to_client and _revision(ticket) == start
    assert b"This ticket changed" in posted.content


@pytest.mark.parametrize("audience", ["staff", "portal"])
def test_writes_from_current_pages_still_save_with_request_transactions_on(
    client, staff_ticket, clinic_portal, audience, monkeypatch
):
    """Finding 1: the reply and entry posts run without the request transaction, so a refused
    one draws its own reading; one that is accepted is still saved, once."""
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)
    ticket = staff_ticket
    text = "Saved with request transactions on: ORD-TEST-000924."
    if audience == "portal":
        client.force_login(clinic_portal)
        page = client.get(reverse("portal:request-detail", args=[ticket.number]))
        sent = _confirm_two_fresh_send(client, page, text)
    else:
        page = client.get(reverse("desk:request-detail", args=[ticket.pk]))
        sent = _staff_note(client, ticket, page, text)
    assert sent.status_code == 302
    assert RequestEntry.objects.filter(request=ticket, body=text).count() == 1


@pytest.mark.parametrize("audience", ["staff", "portal"])
def test_a_refused_post_with_request_transactions_on_stores_nothing(
    client, staff_ticket, clinic_portal, agent, audience, monkeypatch
):
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)
    ticket = staff_ticket
    if audience == "portal":
        client.force_login(clinic_portal)
        page = client.get(reverse("portal:request-detail", args=[ticket.number]))
    else:
        page = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    _confirm_two_staff_update(agent, ticket, "The courier has the package.")
    text = "Refused with request transactions on: ORD-TEST-000925."
    if audience == "portal":
        refused = _confirm_two_fresh_send(client, page, text)
        assert refused.status_code == 200
    else:
        refused = _staff_note(client, ticket, page, text)
        assert refused.status_code == 400
    assert b"The courier has the package." in refused.content
    assert not RequestEntry.objects.filter(request=ticket, body=text).exists()


def test_a_refused_entry_on_an_open_ticket_still_offers_every_kind(client, staff_ticket):
    """Finding 6's neighbour: only a ticket closed meanwhile narrows the composer to notes."""
    ticket = staff_ticket
    page = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    refused = client.post(
        reverse("desk:request-entry", args=[ticket.pk]),
        {
            "kind": EntryKind.REPLY,
            "body": "",
            "expected_revision": page.context["composer"]["expected_revision"].value(),
        },
    )
    assert refused.status_code == 400
    kinds = [value for value, _label in refused.context["composer"].fields["kind"].choices]
    assert set(kinds) == {EntryKind.REPLY, EntryKind.NOTE, EntryKind.CONTACT}


def test_a_refused_entry_on_a_ticket_closed_meanwhile_offers_notes_only(
    client, staff_ticket, agent
):
    ticket = staff_ticket
    page = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    set_status(ticket, Status.CLOSED)  # closed meanwhile (the revision is left as it was)
    refused = client.post(
        reverse("desk:request-entry", args=[ticket.pk]),
        {
            "kind": EntryKind.REPLY,
            "body": "After the close.",
            "expected_revision": page.context["composer"]["expected_revision"].value(),
        },
    )
    assert refused.status_code == 400
    assert not RequestEntry.objects.filter(request=ticket, body="After the close.").exists()
    kinds = [value for value, _label in refused.context["composer"].fields["kind"].choices]
    assert kinds == [EntryKind.NOTE]
    assert refused.context["is_closed"]


def _preference_request(user):
    from django.test import RequestFactory

    request = RequestFactory().get("/")
    request.user = user
    return request


def test_save_if_absent_creates_keeps_and_repairs(make_user):
    """Finding 8's neighbours: a first mark is created; a readable one is never replaced; an
    unreadable one counts as none."""
    person = make_user("neighbour_marks", "client_services")
    marks = UserPreference.objects.filter(user=person, key=preferences.WHATS_NEW_KEY)
    newest = changelog.seen_marker(changelog.entries())
    preferences.save_if_absent(_preference_request(person), preferences.WHATS_NEW_KEY, newest)
    assert marks.get().value == newest
    older = changelog.seen_marker(changelog.entries()[1:])
    assert older and older != newest
    kept = marks.values_list("value", "updated_at").get()
    preferences.save_if_absent(_preference_request(person), preferences.WHATS_NEW_KEY, older)
    assert marks.values_list("value", "updated_at").get() == kept
    marks.update(value="not a marker")
    preferences.save_if_absent(_preference_request(person), preferences.WHATS_NEW_KEY, older)
    assert marks.get().value == older


def test_opening_whats_new_still_replaces_the_first_mark(client, staff_ticket, make_user):
    newcomer = make_user("neighbour_reader_two", "client_services")
    client.force_login(newcomer)
    client.get(reverse("desk:request-detail", args=[staff_ticket.pk]))
    mark = UserPreference.objects.get(user=newcomer, key=preferences.WHATS_NEW_KEY)
    mark.value = changelog.seen_marker(changelog.entries()[1:])
    mark.save(update_fields=["value"])
    client.logout()
    client.force_login(newcomer)
    assert client.get(reverse("whats-new")).status_code == 200
    mark.refresh_from_db()
    assert mark.value == changelog.seen_marker(changelog.entries())


def test_after_reading_inside_a_callback_of_a_finished_reading_runs_at_once():
    """Finding 9: once the reading has committed, its callbacks are outside it."""
    calls = []

    def callback():
        with transaction.atomic():
            concurrency.after_reading(lambda: calls.append("at once"))
            calls.append("inside the callback's transaction")

    with concurrency.one_reading():
        concurrency.after_reading(callback)
    assert calls == ["at once", "inside the callback's transaction"]


def test_a_person_saving_the_drawn_name_itself_is_waited_for_once(monkeypatch):
    """Findings 3 and 4 (documented bound): only a person saving the very value just drawn,
    which needs it known in advance, meets the job. An exact match is waited for and then
    passed over with a new draw; the job's account is created once."""
    JobSettings.objects.create()
    suffixes = iter(["aabbccdd", "bbccddee"])
    monkeypatch.setattr(job_actor, "secrets", SimpleNamespace(token_hex=lambda n: next(suffixes)))
    drawn = f"{job_actor.USERNAME}-aabbccdd"
    person = Held(lambda: User.objects.create_user(drawn, email="neighbour-guess@example.invalid"))
    state, creation = _while_change_is_open(person, job_actor.system_account)
    assert state == "waiting" and creation.outcome == "done", (state, creation.error)
    assert creation.result.username == f"{job_actor.USERNAME}-bbccddee"
    assert User.objects.get(username=drawn).is_active
    assert AuditEvent.objects.filter(action="jobs.system_account_created").count() == 1


def test_add_link_page_drawn_before_a_ticket_change_is_refused_and_keeps_input(
    client, linked_ticket, agent
):
    """Confirmation C1/C8's neighbour: the Add link page sends the ticket's revision; a page
    opened before a change is refused with what was typed kept, and a page opened now saves."""
    ticket, lims, _attachment = linked_ticket
    url = reverse("link_records:request-link-add", args=[ticket.pk])
    page = client.get(url)
    sent = re.search(r'name="expected_revision" value="(\d+)"', page.content.decode()).group(1)
    assert int(sent) == _revision(ticket)
    _confirm_two_staff_update(agent, ticket, "The courier is on the way.")
    fields = {
        "system": lims.pk,
        "record_type": "order",
        "identifier": "ORD-TEST-000930",
        "role": "related",
        "expected_revision": sent,
    }
    refused = client.post(url, fields)
    assert refused.status_code == 200 and b"This ticket changed" in refused.content
    assert b'value="ORD-TEST-000930"' in refused.content
    assert not ticket.link_attachments.filter(link__identifier="ORD-TEST-000930").exists()
    fresh = client.get(url)
    fields["expected_revision"] = re.search(
        r'name="expected_revision" value="(\d+)"', fresh.content.decode()
    ).group(1)
    assert client.post(url, fields).status_code == 302
    assert ticket.link_attachments.filter(link__identifier="ORD-TEST-000930").count() == 1


def test_an_organizations_link_pages_carry_no_ticket_revision(client, staff_ticket, clinic, agent):
    lims = ExternalSystem.objects.create(key="lims", name="Example LIMS", kind="lims")
    url = reverse("link_records:client-link-add", args=[clinic.pk])
    page = client.get(url)
    assert b'name="expected_revision"' not in page.content
    posted = client.post(
        url,
        {
            "system": lims.pk,
            "record_type": "project",
            "identifier": "ORD-TEST-000931",
            "role": "related",
        },
    )
    assert posted.status_code == 302
    attachment = clinic.link_attachments.get(link__identifier="ORD-TEST-000931")
    removal = client.get(reverse("link_records:attachment-remove", args=[attachment.pk]))
    assert b'name="expected_revision"' not in removal.content


def test_every_revision_on_the_ticket_page_is_sent_as_a_plain_number(
    client, linked_ticket, settings
):
    """Second confirmation E1's neighbours: with thousands separators on, the composer, Take and
    Show/Hide forms send the revision the server compares, not "1,000"."""
    settings.USE_THOUSAND_SEPARATOR = True
    ticket, _lims, _attachment = linked_ticket
    ServiceRequest.objects.filter(pk=ticket.pk).update(revision=1000, assignee=None)
    page = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    sent = re.findall(r'name="expected_revision" value="([^"]*)"', page.content.decode())
    assert len(sent) >= 2 and set(sent) == {"1000"}, sent
