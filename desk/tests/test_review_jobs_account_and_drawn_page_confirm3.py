"""Third confirmation. Exercise the submitted controls and the form returned to the reader."""

from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse

from accounts import roles
from accounts.models import User
from core.page_docs import documentation_for_route
from desk import saved_views
from desk import services as desk
from desk.models import SavedView, ServiceRequest, Status
from links import services, views
from links.forms import LinkForm, RemoveLinkForm
from links.models import ExternalSystem

from .test_auto_close import resolve
from .test_contract import new_request, seen
from .test_review_assignment_lock_order_verify3 import paused
from .test_review_jobs_account_and_drawn_page import committed
from .test_review_jobs_account_and_drawn_page_confirm import (
    FormMarkup,
    attach,
    messages,
    reason,
    remove,
    remove_url,
    show_url,
    visibility,
)
from .test_saved_views import make_view

pytestmark = pytest.mark.django_db(transaction=True)
ORDER = "ORD-TEST-000981"
DIALOG = {"X-VR-Form": "1"}
SURFACES = pytest.mark.parametrize("headers", [{}, DIALOG], ids=["page", "dialog"])
GROUPING = pytest.mark.parametrize("grouping", [False, True], ids=["plain", "grouped"])


class PostedForm(HTMLParser):
    """Select the form by its action, including small POST buttons on the ticket page."""

    def __init__(self, response, url):
        super().__init__()
        self.url = url
        self.inside = False
        self.count = 0
        self.controls = {}
        self.feed(response.content.decode())
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            action = attrs.get("action") or self.url
            self.inside = action == self.url and attrs.get("method", "").lower() == "post"
            self.count += self.inside
        if self.inside and tag == "input" and attrs.get("type") == "hidden":
            self.controls[attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False


@pytest.fixture(autouse=True)
def local_mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


@pytest.fixture
def ticket(client, agent, category, clinic):
    client.force_login(agent)
    return new_request(agent, category, client=clinic)


@pytest.fixture
def system():
    return ExternalSystem.objects.create(name="Delivery", key="delivery", kind="other")


def add_page(ticket, system):
    return reverse("link_records:request-link-add", args=[ticket.pk]), {
        "identifier": ORDER,
        "record_type": "order",
        "system": system.pk,
        "role": "related",
    }


def revision_form(client, url, headers):
    response = client.get(url, headers=headers)
    form = FormMarkup(response)
    assert response.status_code == 200 and form.count == 1
    return form.hidden


def set_revision(ticket):
    ServiceRequest.objects.filter(pk=ticket.pk).update(revision=1000)


@GROUPING
@pytest.mark.parametrize("action", ["take", "show", "hide"])
def test_ticket_buttons_send_and_apply_the_revision(
    client, agent, ticket, system, join, settings, grouping, action
):
    settings.USE_THOUSAND_SEPARATOR = grouping
    join(agent, ticket.team)
    if action == "take":
        url = reverse("desk:request-take", args=[ticket.pk])
    else:
        item = attach(agent, system, ticket, shown=action == "hide")
        url = show_url(item)
    set_revision(ticket)
    page = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    form = PostedForm(page, url)
    assert form.count == 1 and form.controls["expected_revision"] == "1000"
    assert client.post(url, form.controls).status_code == 302
    ticket.refresh_from_db()
    assert ticket.revision == 1001
    if action == "take":
        assert ticket.assignee_id == agent.pk
    else:
        item.refresh_from_db()
        assert item.shown_to_client == (action == "show")


@GROUPING
@SURFACES
@pytest.mark.parametrize("action", ["add", "remove"])
def test_link_revision_after_field_error_saves_in_the_same_form(
    client, agent, ticket, system, headers, settings, grouping, action
):
    settings.USE_THOUSAND_SEPARATOR = grouping
    if action == "add":
        url, fields = add_page(ticket, system)
        required = "identifier"
    else:
        item = attach(agent, system, ticket)
        url, fields, required = remove_url(item), reason(item), "reason"
    set_revision(ticket)
    sent = revision_form(client, url, headers)
    assert sent["expected_revision"] == "1000"
    invalid = client.post(url, {**sent, **fields, required: ""}, headers=headers)
    assert invalid.status_code == 200 and required in invalid.context["form"].errors
    sent_again = FormMarkup(invalid).hidden
    assert sent_again["expected_revision"] == "1000" and seen(ticket) == 1000
    assert client.post(url, {**sent_again, **fields}, headers=headers).status_code == (
        204 if headers else 302
    )
    assert seen(ticket) == 1001


@GROUPING
@pytest.mark.parametrize("action", ["edit", "delete"])
def test_saved_view_controls_send_the_revision(client, agent, settings, grouping, action):
    settings.USE_THOUSAND_SEPARATOR = grouping
    client.force_login(agent)
    view = make_view(agent)
    SavedView.objects.filter(pk=view.pk).update(revision=1000)
    edit = reverse("desk:view-edit", args=[view.pk])
    if action == "edit":
        url, page_url = edit, edit
        fields = {"name": "Morning collections", "shared_with": "private"}
    else:
        url, page_url, fields = (
            reverse("desk:view-delete", args=[view.pk]),
            edit + "?confirm=delete",
            {},
        )
    form = PostedForm(client.get(page_url), url)
    assert form.count == 1 and form.controls["expected_revision"] == "1000"
    assert client.post(url, {**form.controls, **fields}).status_code == 302
    if action == "delete":
        assert not SavedView.objects.filter(pk=view.pk).exists()
    else:
        view.refresh_from_db()
        assert (view.name, view.revision) == ("Morning collections", 1001)


@pytest.mark.parametrize("action", ["take", "edit"])
def test_page_opened_before_number_format_change_has_a_recoverable_response(
    agent, category, join, action
):
    # These are the controls the previous templates emitted in a grouping deployment.
    client = Client(raise_request_exception=False)
    client.force_login(agent)
    if action == "take":
        ticket = new_request(agent, category)
        join(agent, ticket.team)
        set_revision(ticket)
        url, fields = reverse("desk:request-take", args=[ticket.pk]), {}
    else:
        view = make_view(agent)
        SavedView.objects.filter(pk=view.pk).update(revision=1000)
        url = reverse("desk:view-edit", args=[view.pk])
        fields = {"name": "Morning collections", "shared_with": "private"}
    response = client.post(url, {**fields, "expected_revision": "1,000"})
    assert response.status_code in (200, 302, 400), "An already-open form returned a server error."


@pytest.mark.parametrize("stage", ["field", "stale"])
def test_saved_view_redraw_keeps_the_revision_that_was_opened(client, make_user, stage):
    owner = make_user("collector", "supervisor")
    client.force_login(owner)
    view = make_view(owner)
    url = reverse("desk:view-edit", args=[view.pk])
    opened = revision_form(client, url, {})
    committed(
        lambda: saved_views.update_view(
            actor=owner,
            view=view,
            expected_revision=view.revision,
            name=view.name,
            shared_with="everyone",
        )
    )
    fields = {"name": "" if stage == "field" else "Morning collections", "shared_with": "private"}
    refused = client.post(url, {**opened, **fields})
    assert refused.status_code == 200 and refused.context["form"].errors
    view.refresh_from_db()
    assert (
        view.shared_with == "everyone"
        and refused.context["form"]["shared_with"].value() == "private"
    )
    returned = FormMarkup(refused).hidden
    retried = client.post(
        url, {**returned, "name": "Morning collections", "shared_with": "private"}
    )
    view.refresh_from_db()
    assert (retried.status_code, view.shared_with, returned["expected_revision"]) == (
        200,
        "everyone",
        opened["expected_revision"],
    )


def during_refusal(client, url, fields, headers, form_type, stage, change):
    opened = revision_form(client, url, headers)
    data = {**opened, **fields}
    if stage == "field":
        required = "identifier" if form_type is LinkForm else "reason"
        data[required] = ""
        module, name = form_type, "is_valid"
    else:
        # Pause immediately before the view hands the valid form to its real service.
        module, name = views, "add_link" if form_type is LinkForm else "remove_link"
    with paused(module, name, lambda: committed(change)):
        reply = client.post(url, data, headers=headers)
    return opened, reply


@SURFACES
@pytest.mark.parametrize("stage", ["field", "service"])
@pytest.mark.parametrize("action", ["add", "remove"])
def test_redraw_of_closed_ticket_returns_to_the_ticket(
    client, agent, ticket, system, headers, stage, action
):
    ticket = resolve(agent, ticket, days_ago=1)
    if action == "add":
        url, fields = add_page(ticket, system)
        form_type = LinkForm
    else:
        item = attach(agent, system, ticket)
        url, fields, form_type = remove_url(item), reason(item), RemoveLinkForm
    _, reply = during_refusal(
        client,
        url,
        fields,
        headers,
        form_type,
        stage,
        lambda: desk.change_status(
            actor=agent, request=ticket, expected_revision=seen(ticket), status=Status.CLOSED
        ),
    )
    assert reply.status_code == (204 if headers else 302)
    assert reply["HX-Redirect" if headers else "Location"] == reverse(
        "desk:request-detail", args=[ticket.pk]
    )
    assert services.CLOSED_MESSAGE in messages(reply)


@SURFACES
@pytest.mark.parametrize("stage", ["field", "service"])
@pytest.mark.parametrize("change", ["show", "hide", "remove"])
def test_remove_redraw_reads_the_committed_link_state(
    client, agent, ticket, system, headers, stage, change
):
    item = attach(agent, system, ticket, shown=change == "hide")

    def update():
        return (
            remove(agent, item) if change == "remove" else visibility(agent, item, change == "show")
        )

    opened, reply = during_refusal(
        client, remove_url(item), reason(item), headers, RemoveLinkForm, stage, update
    )
    assert reply.status_code == 200
    markup = FormMarkup(reply)
    if change == "remove":
        assert markup.count == 0 and b"Already removed" in reply.content
    else:
        label = "Shown to the client" if change == "show" else "Not shown to the client"
        assert (
            label in " ".join(markup.words)
            and markup.hidden["expected_revision"] == opened["expected_revision"]
        )
        assert str(seen(ticket)) != opened["expected_revision"]


@SURFACES
@pytest.mark.parametrize("action", ["add", "remove"])
def test_future_revision_stays_unusable_when_ticket_advances_during_validation(
    client, agent, ticket, system, headers, action
):
    if action == "add":
        url, fields = add_page(ticket, system)
        form_type, required = LinkForm, "identifier"
    else:
        item = attach(agent, system, ticket)
        url, fields, form_type, required = remove_url(item), reason(item), RemoveLinkForm, "reason"
    opened = revision_form(client, url, headers)
    future = str(int(opened["expected_revision"]) + 1)
    with paused(
        form_type,
        "is_valid",
        lambda: committed(
            lambda: (
                attach(agent, system, ticket, shown=True)
                if action == "add"
                else visibility(agent, item, True)
            )
        ),
    ):
        reply = client.post(
            url, {**opened, **fields, required: "", "expected_revision": future}, headers=headers
        )
    assert str(seen(ticket)) == future
    assert reply.status_code == 200 and FormMarkup(reply).hidden["expected_revision"] == "0"


@SURFACES
@pytest.mark.parametrize("stage", ["field", "service"])
def test_add_choices_follow_current_person_permission(
    client, agent, ticket, system, headers, stage
):
    # The account and ticket remain in scope. Only a new positive grant changes the form choices.
    grant = Permission.objects.get(codename="view_patient_information")
    url, fields = add_page(ticket, system)

    def change():
        agent.user_permissions.add(grant)
        if stage == "service":
            attach(agent, system, ticket)

    _, reply = during_refusal(client, url, fields, headers, LinkForm, stage, change)
    fresh = client.get(url, headers=headers)
    assert reply.status_code == fresh.status_code == 200 and reply.context["form"].errors
    marker = b'<option value="patient"'
    assert marker in fresh.content
    assert marker in reply.content, (
        "The returned form offers different record types from a fresh form."
    )


@pytest.mark.parametrize("action", ["add", "remove"])
@pytest.mark.parametrize("scope", ["member", "outside", "no-role"])
@pytest.mark.parametrize("stage", ["field", "service"])
@SURFACES
def test_ticket_redraw_obeys_its_scope_decision(
    client, agent, ticket, system, join, action, scope, stage, headers
):
    # Exercise the view's response to the selector contract. The real reader remains authorized.
    join(agent, ticket.team)
    ticket.category.is_restricted = True
    ticket.category.save(update_fields=["is_restricted"])
    if action == "add":
        url, fields = add_page(ticket, system)
        form_type, required = LinkForm, "identifier"
    else:
        item = attach(agent, system, ticket)
        url, fields, form_type, required = remove_url(item), reason(item), RemoveLinkForm, "reason"
    opened = revision_form(client, url, headers)
    if stage == "field":
        fields[required] = ""
    else:
        # An intervening note makes the otherwise valid form reach a real service refusal.
        committed(
            lambda: desk.add_entry(
                actor=agent,
                request=ticket,
                expected_revision=seen(ticket),
                kind="note",
                body="Collection confirmed",
            )
        )
    validating = []
    select = views.staff_requests

    def scope_for(user):
        rows = select(user)
        if validating and scope == "outside":
            return rows.none()
        if validating and scope == "no-role":
            raise PermissionDenied
        return rows

    with (
        patch.object(views, "staff_requests", scope_for),
        paused(form_type, "is_valid", lambda: validating.append(True)),
    ):
        reply = client.post(url, {**opened, **fields}, headers=headers)
    assert reply.status_code == {"member": 200, "outside": 404, "no-role": 403}[scope]


def test_organization_removal_checks_staff_role_before_redrawing(client, agent, ticket, system):
    # Observe the staff gate for an authorized account after the form detects its field error.
    item = attach(agent, system, ticket, target="organization")
    url = remove_url(item)
    opened = revision_form(client, url, DIALOG)
    gate = roles.require_role
    calls = []

    def checked(user, *allowed):
        calls.append("role")
        return gate(user, *allowed)

    with (
        patch.object(roles, "require_role", checked),
        paused(RemoveLinkForm, "is_valid", lambda: calls.append("validation")),
    ):
        reply = client.post(url, {**opened, "reason": ""}, headers=DIALOG)
    assert reply.status_code == 200 and "reason" in reply.context["form"].errors
    assert "role" in calls[calls.index("validation") + 1 :], calls


def test_ticket_help_and_roadmap_state_the_form_details_exception():
    tips = documentation_for_route("desk:request-detail")["good_to_know"]
    assert any(
        "stale" in tip and "Edit form details checks only its own fields" in tip for tip in tips
    )
    roadmap = (Path(__file__).resolve().parents[2] / "docs/roadmap.md").read_text()
    row = next(line for line in roadmap.splitlines() if line.startswith("| Links |"))
    assert "a correction of form details checks the fields it changes" in row


@SURFACES
@pytest.mark.parametrize("action", ["add", "remove"])
def test_field_error_also_explains_when_correcting_it_cannot_save(
    client, agent, ticket, system, headers, action
):
    if action == "add":
        url, fields = add_page(ticket, system)
        form_type, required = LinkForm, "identifier"

        def change():
            return attach(agent, system, ticket)
    else:
        item = attach(agent, system, ticket)
        url, fields, form_type, required = remove_url(item), reason(item), RemoveLinkForm, "reason"

        def change():
            return visibility(agent, item, True)

    opened, first = during_refusal(client, url, fields, headers, form_type, "field", change)
    assert first.status_code == 200 and required in first.context["form"].errors
    returned = FormMarkup(first).hidden
    assert returned["expected_revision"] == opened["expected_revision"] != str(seen(ticket))
    second = client.post(url, {**returned, **fields}, headers=headers)
    assert second.status_code == 200 and second.context["form"].non_field_errors()
    assert "ticket" in " ".join(first.context["form"].non_field_errors()).lower(), (
        "The form requests a field correction without saying the ticket already changed."
    )


@pytest.mark.parametrize("action", ["add", "remove"])
def test_redraw_checks_the_account_as_it_stands(client, agent, ticket, system, action):
    # A harmless account rename distinguishes a fresh read from the original account.
    if action == "add":
        url, fields = add_page(ticket, system)
        form_type = LinkForm
    else:
        item = attach(agent, system, ticket)
        url, fields, form_type = remove_url(item), reason(item), RemoveLinkForm
    names_at_scope_check = []
    select = views.staff_requests

    def checked(user):
        names_at_scope_check.append(user.first_name)
        return select(user)

    with patch.object(views, "staff_requests", checked):
        _, reply = during_refusal(
            client,
            url,
            fields,
            {},
            form_type,
            "field",
            lambda: User.objects.filter(pk=agent.pk).update(first_name="Morgan"),
        )
    assert reply.status_code == 200 and names_at_scope_check
    assert names_at_scope_check[-1] == "Morgan", names_at_scope_check


@SURFACES
def test_person_removal_summary_and_capability_are_current_on_redraw(
    client, agent, ticket, system, headers
):
    agent.user_permissions.add(Permission.objects.get(codename="view_patient_information"))
    item = attach(agent, system, ticket, target="person")
    url = remove_url(item)
    sent = revision_form(client, url, headers)
    order = []
    capability = views.can_see_person_links

    def observe(user):
        result = capability(user)
        order.append("permitted" if result else "denied")
        return result

    with (
        patch.object(views, "can_see_person_links", observe),
        paused(RemoveLinkForm, "is_valid", lambda: order.append("validation")),
    ):
        reply = client.post(url, {**sent, "reason_code": ""}, headers=headers)
    words = " ".join(FormMarkup(reply).words)
    assert reply.status_code == 200 and "reason_code" in reply.context["form"].errors
    assert "permitted" in order[order.index("validation") + 1 :]
    assert "Never shown to the client" in words and item.link.identifier not in words
    assert "no-store" in reply["Cache-Control"]


def test_restricted_selector_tracks_committed_membership_and_role_changes(agent, ticket, join):
    # Inspect the actual policy decisions independently of the handler's response contract.
    membership = join(agent, ticket.team)
    ticket.category.is_restricted = True
    ticket.category.save(update_fields=["is_restricted"])
    assert views.staff_requests(agent).filter(pk=ticket.pk).exists()
    committed(lambda: membership.delete())
    assert not views.staff_requests(agent).filter(pk=ticket.pk).exists()
    committed(lambda: agent.groups.clear())
    with pytest.raises(PermissionDenied):
        views.staff_requests(agent)
