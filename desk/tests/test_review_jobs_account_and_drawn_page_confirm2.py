"""Second confirmation of the ticket revision sent from Add link and Remove link."""

from html.parser import HTMLParser
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.urls import reverse

from core.models import AuditEvent
from core.page_docs import documentation_for_route
from desk import services as desk
from desk.models import EntryKind, ServiceRequest, Status
from links import services as links
from links import views
from links.forms import LinkForm, RemoveLinkForm
from links.models import ExternalSystem, LinkAttachment

# Adapted (second confirmation, E6): the link pages say how to get a fresh revision.
from links.views import LINK_PAGE_STALE  # noqa: E402
from request_forms.models import FieldKind, RequestTemplate
from request_forms.tests.test_values import current, portal_raise

from .test_auto_close import resolve
from .test_contract import new_request, seen
from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by, paused
from .test_review_jobs_account_and_drawn_page import committed

pytestmark = pytest.mark.django_db(transaction=True)
ORDER = "ORD-TEST-000971"
REASON = "Collection completed"
LABEL = "Morning delivery"
DIALOG = {"X-VR-Form": "1"}


class Markup(HTMLParser):
    """Inspect the submitted form itself, including hidden inputs and visible notices."""

    def __init__(self, response):
        super().__init__()
        self.inside = False
        self.forms = 0
        self.hidden = {}
        self.revisions = []
        self.inputs = {}
        self.words = []
        self.links = []
        self.feed(response.content.decode())
        self.close()

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "a" and "href" in fields:
            self.links.append(fields["href"])
        if tag == "form":
            self.inside = "vr-form" in fields.get("class", "").split()
            self.forms += int(self.inside)
        if self.inside and tag == "input" and fields.get("name"):
            name = fields["name"]
            self.inputs[name] = fields.get("value", "")
            if fields.get("type") == "hidden":
                self.hidden[name] = self.inputs[name]
                if name == "expected_revision":
                    self.revisions.append(self.inputs[name])

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False

    def handle_data(self, data):
        if self.inside and data.strip():
            self.words.append(" ".join(data.split()))

    @property
    def visible(self):
        return " ".join(self.words)


@pytest.fixture(autouse=True)
def mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


@pytest.fixture
def ticket(agent, category, clinic):
    return new_request(agent, category, client=clinic, client_reference=ORDER)


@pytest.fixture
def system():
    return ExternalSystem.objects.create(key="delivery", name="Delivery", kind="other")


def attach(agent, ticket, system, *, person=False, organization=False):
    target = {"client_id": ticket.client_id} if organization else {"request_id": ticket.pk}
    item, _ = links.add_link(
        actor=agent,
        system=system,
        record_type="patient" if person else "order",
        identifier=ORDER,
        display_label=LABEL,
        **target,
    )
    return item


def link_page(action, ticket, system, agent, *, organization=False, person=False):
    if person:
        agent.user_permissions.add(Permission.objects.get(codename="view_patient_information"))
    if action == "remove":
        item = attach(agent, ticket, system, person=person, organization=organization)
        fields = (
            {"reason_code": "no_longer_needed", "note": REASON} if person else {"reason": REASON}
        )
        return reverse("link_records:attachment-remove", args=[item.pk]), fields
    fields = {
        "identifier": ORDER,
        "display_label": LABEL,
        "system": system.pk,
        "record_type": "patient" if person else "order",
        "role": "related",
    }
    if organization:
        return reverse("link_records:client-link-add", args=[ticket.client_id]), fields
    return reverse("link_records:request-link-add", args=[ticket.pk]), fields


def note(agent, ticket):
    return desk.add_entry(
        actor=agent,
        request=ticket,
        expected_revision=seen(ticket),
        kind=EntryKind.NOTE,
        body="Delivery confirmed",
    )


def close_ticket(agent, ticket):
    return desk.change_status(
        actor=agent, request=ticket, expected_revision=seen(ticket), status=Status.CLOSED
    )


def mutation_state(ticket):
    return (
        seen(ticket),
        LinkAttachment.objects.count(),
        LinkAttachment.objects.filter(removed_at__isnull=False).count(),
        AuditEvent.objects.filter(
            action__in=["external_link.attached", "external_link.detached"]
        ).count(),
    )


def messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


def submit(response, client, url, fields, headers=None):
    return client.post(url, {**Markup(response).hidden, **fields}, headers=headers or {})


@pytest.mark.parametrize("dialog", [False, True], ids=["page", "dialog"])
@pytest.mark.parametrize("action", ["add", "remove"])
@pytest.mark.parametrize("target", ["ticket", "organization"])
def test_rendered_scope_and_save(client, agent, ticket, system, target, action, dialog):
    url, fields = link_page(action, ticket, system, agent, organization=target == "organization")
    client.force_login(agent)
    headers = DIALOG if dialog else {}
    page = client.get(url, headers=headers)
    markup = Markup(page)
    before = mutation_state(ticket)
    assert page.status_code == 200 and markup.forms == 1 and "csrfmiddlewaretoken" in markup.hidden
    assert (b"<!doctype html>" not in page.content.lower()) == dialog
    assert markup.revisions == ([str(before[0])] if target == "ticket" else [])
    assert "original_state" not in markup.hidden
    response = submit(page, client, url, fields, headers)
    assert response.status_code == (204 if dialog else 302)
    assert mutation_state(ticket) == (
        before[0] + (target == "ticket"),
        before[1] + (action == "add"),
        before[2] + (action == "remove"),
        before[3] + 1,
    )


@pytest.mark.parametrize("action", ["add", "remove"])
@pytest.mark.parametrize("sent", [None, "other", "0", "future"])
def test_missing_or_incorrect_revision_refuses_an_otherwise_valid_form(
    client, agent, ticket, system, action, sent
):
    url, fields = link_page(action, ticket, system, agent)
    client.force_login(agent)
    before = mutation_state(ticket)
    data = {**Markup(client.get(url)).hidden, **fields}
    data.pop("expected_revision", None)
    if sent is not None:
        data["expected_revision"] = str(before[0] + 1) if sent == "future" else sent
    response = client.post(url, data)
    assert mutation_state(ticket) == before
    assert response.status_code == 200 and LINK_PAGE_STALE in Markup(response).visible
    assert (LABEL if action == "add" else REASON).encode() in response.content


@pytest.mark.parametrize("action", ["add", "remove"])
def test_invalid_form_then_corrected_submission_keeps_its_original_revision(
    client, agent, ticket, system, action
):
    url, fields = link_page(action, ticket, system, agent)
    client.force_login(agent)
    page = client.get(url, headers=DIALOG)
    opened = seen(ticket)
    committed(lambda: note(agent, ticket))
    before = mutation_state(ticket)
    required = "identifier" if action == "add" else "reason"
    invalid = submit(page, client, url, {**fields, required: ""}, DIALOG)
    assert invalid.status_code == 200 and required in invalid.context["form"].errors
    assert Markup(invalid).revisions == [str(opened)]
    refused = submit(invalid, client, url, fields, DIALOG)
    assert mutation_state(ticket) == before and LINK_PAGE_STALE in Markup(refused).visible
    assert Markup(refused).revisions == [str(opened)]
    fresh = client.get(url, headers=DIALOG)
    assert Markup(fresh).revisions == [str(before[0])]
    assert submit(fresh, client, url, fields, DIALOG).status_code == 204


@pytest.mark.parametrize("dialog", [False, True], ids=["page", "dialog"])
def test_make_a_link_uses_the_prefilled_identifier_and_opened_revision(
    client, agent, ticket, system, dialog
):
    url, fields = link_page("add", ticket, system, agent)
    client.force_login(agent)
    detail = reverse("desk:request-detail", args=[ticket.pk])
    reference_url = url + "?from=reference"
    assert reference_url in Markup(client.get(detail)).links
    headers = DIALOG if dialog else {}
    page = client.get(reference_url, headers=headers)
    form = Markup(page)
    assert form.inputs["identifier"] == ticket.client_reference == ORDER
    assert form.revisions == [str(seen(ticket))]
    fields["identifier"] = form.inputs["identifier"]
    committed(lambda: note(agent, ticket))
    refused = submit(page, client, reference_url, fields, headers)
    assert LINK_PAGE_STALE in Markup(refused).visible and not ticket.link_attachments.exists()
    assert Markup(refused).inputs["identifier"] == ORDER
    fresh = client.get(reference_url, headers=headers)
    saved = submit(fresh, client, reference_url, fields, headers)
    assert saved.status_code == (204 if dialog else 302)
    assert ticket.link_attachments.get().link.identifier == ORDER
    assert reference_url not in Markup(client.get(detail)).links


@pytest.mark.parametrize("stage", ["field-validation", "service"])
@pytest.mark.parametrize("action", ["add", "remove"])
def test_form_answers_closure_committed_during_post(client, agent, ticket, system, action, stage):
    ticket = resolve(agent, ticket, days_ago=1)
    url, fields = link_page(action, ticket, system, agent)
    client.force_login(agent)
    page = client.get(url)
    data = {**Markup(page).hidden, **fields}
    # Isolate the old removal handler as well when comparing an in-service refusal.
    if stage == "service" and "original_state" in page.context["form"].fields:
        data["original_state"] = page.context["form"]["original_state"].value()
    if stage == "field-validation":
        data["identifier" if action == "add" else "reason"] = ""
        module, name = (LinkForm if action == "add" else RemoveLinkForm), "is_valid"
    else:
        module, name = views, ("add_link" if action == "add" else "remove_link")
    before = mutation_state(ticket)
    with paused(module, name, lambda: committed(lambda: close_ticket(agent, ticket))):
        response = client.post(url, data)
    ticket.refresh_from_db()
    assert ticket.status == Status.CLOSED and mutation_state(ticket)[1:] == before[1:]
    assert Markup(response).forms == 0, "The closed ticket still offers to save the link change."
    assert (
        links.CLOSED_MESSAGE in messages(response)
        or links.CLOSED_MESSAGE.encode() in response.content
    )


@pytest.mark.parametrize("change", ["show", "remove"])
def test_invalid_removal_redraw_reads_current_attachment(client, agent, ticket, system, change):
    url, _fields = link_page("remove", ticket, system, agent)
    item = ticket.link_attachments.get()
    client.force_login(agent)
    page = client.get(url)

    def update():
        if change == "show":
            return links.set_shown_to_client(actor=agent, attachment_id=item.pk, shown=True)
        return links.remove_link(actor=agent, attachment_id=item.pk, reason="Delivery replaced")

    with paused(RemoveLinkForm, "is_valid", lambda: committed(update)):
        response = submit(page, client, url, {"reason": ""}, DIALOG)
    assert response.status_code == 200
    fresh = client.get(url, headers=DIALOG)
    if change == "show":
        assert b"Shown to the client" in fresh.content
        assert b"Shown to the client" in response.content
        assert Markup(response).revisions == Markup(page).revisions
    else:
        assert b"Already removed" in fresh.content
        assert b"Already removed" in response.content and Markup(response).forms == 0


@pytest.mark.parametrize("action", ["add", "remove"])
def test_grouped_display_numbers_do_not_change_the_wire_revision(
    client, agent, ticket, system, action, settings
):
    url, fields = link_page(action, ticket, system, agent)
    # A long-lived ticket in a deployment that groups thousands in displayed numbers.
    ServiceRequest.objects.filter(pk=ticket.pk).update(revision=1000)
    settings.USE_THOUSAND_SEPARATOR = True
    client.force_login(agent)
    page = client.get(url)
    wire = Markup(page).revisions
    # Compare formatting independently of the earlier release's omitted removal token.
    if "original_state" in page.context["form"].fields:
        fields["original_state"] = page.context["form"]["original_state"].value()
    submitted = submit(page, client, url, fields)
    assert submitted.status_code == 302, (wire, submitted.status_code)
    assert seen(ticket) == 1001 and all(value == "1000" for value in wire)


def test_person_removal_summary_states_whether_client_sees_link(client, agent, ticket, system):
    url, _fields = link_page("remove", ticket, system, agent, person=True)
    client.force_login(agent)
    response = client.get(url, headers=DIALOG)
    summary = Markup(response).visible.lower()
    assert "patient link" in summary and ORDER not in summary and LABEL.lower() not in summary
    assert any(
        word in summary
        for word in (
            "never shown",
            "not shown to the client",
            "never visible",
            "not visible to the client",
        )
    ), summary


@pytest.mark.parametrize("action", ["add", "remove"])
def test_a_person_link_rechecks_capability_before_refusing_stale_work(
    client, agent, ticket, system, action
):
    url, fields = link_page(action, ticket, system, agent, person=True)
    client.force_login(agent)
    page = client.get(url)
    committed(lambda: note(agent, ticket))
    order = []
    real_read, real_capability = links._reread, links.require_capability

    def reread(*args, **kwargs):
        user = real_read(*args, **kwargs)
        order.append(("reading", id(user)))
        return user

    def capability(user, code):
        real_capability(user, code)
        order.append(("capability", id(user)))

    before = mutation_state(ticket)
    with (
        patch.object(links, "_reread", reread),
        patch.object(links, "require_capability", capability),
    ):
        refused = submit(page, client, url, fields)
    assert before == mutation_state(ticket) and LINK_PAGE_STALE in Markup(refused).visible
    assert (
        len(order) >= 2 and order[-2][0] == "reading" and order[-1] == ("capability", order[-2][1])
    )


def test_add_rechecks_the_revision_after_waiting_for_a_ticket_writer(client, agent, ticket, system):
    url, fields = link_page("add", ticket, system, agent)
    client.force_login(agent)
    page = client.get(url)
    writer = Held(lambda: note(agent, ticket))
    worker = Session(lambda: submit(page, client, url, fields)).start()
    try:
        assert worker.waiting_or_ended() == "waiting" and _blocked_by(
            worker.pid, writer.session.pid
        )
    finally:
        writer.release()
        worker.join()
    assert writer.session.outcome == worker.outcome == "done", (writer.session.error, worker.error)
    assert not ticket.link_attachments.exists() and LINK_PAGE_STALE in Markup(worker.result).visible


@pytest.mark.parametrize("action", ["add", "remove"])
def test_refusal_never_draws_a_form_for_a_ticket_revision_that_does_not_exist(
    client, agent, ticket, system, action
):
    url, fields = link_page(action, ticket, system, agent)
    client.force_login(agent)
    page = client.get(url)
    before = mutation_state(ticket)
    fields["expected_revision"] = str(before[0] + 1)
    refused = submit(page, client, url, fields)
    assert refused.status_code == 200 and mutation_state(ticket) == before
    wire = Markup(refused).revisions
    assert all(int(number) <= before[0] for number in wire), (
        "The refused form now advertises a revision the ticket has never reached."
    )


@pytest.mark.parametrize("action", ["add", "remove"])
def test_stale_native_post_explains_how_to_get_a_fresh_revision(
    client, agent, ticket, system, action
):
    url, fields = link_page(action, ticket, system, agent)
    client.force_login(agent)
    page = client.get(url)
    payload = {**Markup(page).hidden, **fields}
    committed(lambda: note(agent, ticket))
    before = mutation_state(ticket)
    first = client.post(url, payload)
    # Reload of a direct POST response repeats the same method and body.
    reload_response = client.post(url, payload)
    assert first.status_code == reload_response.status_code == 200 and before == mutation_state(
        ticket
    )
    assert Markup(first).revisions == Markup(reload_response).revisions
    fresh = client.get(url)
    assert submit(fresh, client, url, fields).status_code == 302
    error = " ".join(str(message) for message in first.context["form"].non_field_errors()).lower()
    assert error and (
        "reload" not in error or "from the ticket" in error or "open the ticket" in error
    ), error


def test_summary_accounts_for_field_level_corrections_after_a_ticket_link_changes(
    client, agent, category, clinic_portal, system
):
    template = RequestTemplate.objects.create(key="delivery", name="Delivery", category=category)
    template.fields.create(key="route", label="Route", kind=FieldKind.SHORT_TEXT)
    ticket = portal_raise(clinic_portal, template, {"route": "North"})
    client.force_login(agent)
    url = reverse("request_details:edit", args=[ticket.number])
    page = client.get(url)
    committed(lambda: attach(agent, ticket, system))
    reply = submit(page, client, url, {"field-route": "West", "reason": "Collection moved"})
    assert reply.status_code == 302 and current(ticket)["route"].value == "West"
    tip = (
        "If someone changed the ticket after you opened it, your change is refused as stale. "
        "Reload, read the latest entries and try again."
    )
    assert tip not in documentation_for_route("desk:request-detail")["good_to_know"], (
        "The ticket help summary omits the exception documented in its Links section."
    )
