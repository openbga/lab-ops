"""Round-one confirmation. The browser-form cases submit only inputs present in the HTML.

Separate handler checks supply the form object's initial state to exercise the checks after
form validation, even on a page that fails to render a hidden control.
"""

import re
from html.parser import HTMLParser
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.urls import reverse

from accounts.models import User
from core.models import AuditEvent
from core.page_docs import documentation_for_route
from desk import services as desk
from desk.models import EntryKind, Status
from jobs import actor as jobs_actor
from links import services, views
from links.models import ExternalSystem

from .test_auto_close import resolve
from .test_contract import new_request, seen
from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by, paused
from .test_review_jobs_account_and_drawn_page import committed, while_held

pytestmark = pytest.mark.django_db(transaction=True)

REASON = "Collection completed"
NOTE = "Green delivery basket"
LABEL = "Morning collection"
IDENTIFIER = "ORD-TEST-000961"
# Adapted (confirmation, C1-C3, C7): a ticket's link pages now send its revision instead of an
# original-state token, so a change since is refused with the desk's stale message.
CONFLICT = "This ticket changed. Reload it and review the latest entries."
CHOOSE = "Choose Show to client or Hide from client."


class FormMarkup(HTMLParser):
    """Successful hidden controls and visible text of the editable form only."""

    def __init__(self, response):
        super().__init__()
        self.hidden = {}
        self.words = []
        self.inside = False
        self.count = 0
        self.feed(response.content.decode())
        self.close()

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "form":
            self.inside = "vr-form" in fields.get("class", "").split()
            self.count += int(self.inside)
        if self.inside and tag == "input" and fields.get("type") == "hidden":
            self.hidden[fields["name"]] = fields.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False

    def handle_data(self, value):
        if self.inside and value.strip():
            self.words.append(" ".join(value.split()))


@pytest.fixture(autouse=True)
def mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


@pytest.fixture
def ticket(agent, category, clinic):
    return new_request(agent, category, client=clinic)


@pytest.fixture
def system():
    return ExternalSystem.objects.create(key="collections", name="Collections", kind="other")


@pytest.fixture
def capable(agent):
    agent.user_permissions.add(Permission.objects.get(codename="view_patient_information"))
    return agent


def attach(user, system, ticket, *, target="ticket", shown=False):
    destination = (
        {"client_id": ticket.client_id} if target == "organization" else {"request_id": ticket.pk}
    )
    item, _ = services.add_link(
        actor=user,
        system=system,
        identifier=IDENTIFIER,
        record_type="patient" if target == "person" else "order",
        display_label="Collection record" if target == "person" else LABEL,
        shown_to_client=shown,
        **destination,
    )
    return item


def remove_url(item):
    return reverse("link_records:attachment-remove", args=[item.pk])


def show_url(item):
    return reverse("link_records:attachment-shown", args=[item.pk])


def visibility(actor, item, shown):
    return services.set_shown_to_client(actor=actor, attachment_id=item.pk, shown=shown)


def remove(actor, item):
    return services.remove_link(actor=actor, attachment_id=item.pk, **reason(item))


def reason(item):
    return (
        {"reason_code": "no_longer_needed", "note": NOTE} if item.is_person else {"reason": REASON}
    )


def service_input(page, item):
    # Deliberately separate from FormMarkup: isolate the view's service call after validation.
    # Adapted: the ticket's revision the page sends is not a form field; read it from the page.
    data = reason(item)
    sent = re.search(r'name="expected_revision" value="(\d+)"', page.content.decode())
    if sent:
        data["expected_revision"] = sent.group(1)
    return data


def events(item):
    return AuditEvent.objects.filter(action="external_link.detached", object_id=str(item.pk))


def messages(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


@pytest.mark.parametrize("dialog", [False, True], ids=["page", "dialog"])
@pytest.mark.parametrize("target", ["ticket", "organization", "person"])
def test_current_rendered_removal_form_saves_once(client, capable, system, ticket, target, dialog):
    item = attach(capable, system, ticket, target=target)
    client.force_login(capable)
    before = seen(ticket)
    url = remove_url(item)
    headers = {"X-VR-Form": "1"} if dialog else {}
    page = client.get(url, headers=headers)
    assert page.status_code == 200
    assert (b"<!doctype html>" not in page.content.lower()) == dialog
    form = FormMarkup(page)
    assert form.count == 1 and "csrfmiddlewaretoken" in form.hidden
    response = client.post(url, {**form.hidden, **reason(item)}, headers=headers)
    item.refresh_from_db()
    assert item.removed_by_id == capable.pk, (
        sorted(form.hidden),
        response.context["form"].errors if response.context else response.status_code,
    )
    assert events(item).count() == 1
    assert seen(ticket) == before + (target != "organization")
    assert response.status_code == (204 if dialog else 302)
    assert response["HX-Redirect" if dialog else "Location"] == (
        reverse("clients:client-detail", args=[ticket.client_id])
        if target == "organization"
        else reverse("desk:request-detail", args=[ticket.pk])
    )


@pytest.mark.parametrize("target", ["ticket", "organization"])
def test_removal_rechecks_the_displayed_label(client, agent, ticket, system, target):
    item = attach(agent, system, ticket, target=target)
    client.force_login(agent)
    page = client.get(remove_url(item))
    assert LABEL in " ".join(FormMarkup(page).words)
    data = service_input(page, item)
    committed(
        lambda: services.update_link(
            actor=agent,
            link_id=item.link_id,
            expected_revision=item.link.revision,
            display_label="Evening collection",
            reason="Correct collection",
        )
    )
    fresh = client.get(remove_url(item))
    assert "Evening collection" in " ".join(FormMarkup(fresh).words)
    before = seen(ticket)
    response = client.post(remove_url(item), data)
    item.refresh_from_db()
    # Adapted (C2): a correction of the link's label is the link's own change, not the ticket's
    # (docs/architecture.md, attachment-remove.json "Remove link"), so the removal saves once.
    assert response.status_code == 302 and item.removed_at is not None
    assert events(item).count() == 1
    assert seen(ticket) == (before + 1 if target == "ticket" else before)


@pytest.mark.parametrize(
    "initial,changes",
    [(False, [True]), (True, [False]), (False, [True, False]), (True, [False, True])],
    ids=["show", "hide", "show-then-hide", "hide-then-show"],
)
def test_removal_after_committed_visibility_changes(
    client, agent, ticket, system, initial, changes
):
    item = attach(agent, system, ticket, shown=initial)
    client.force_login(agent)
    url = remove_url(item)
    page = client.get(url)
    data = service_input(page, item)
    for shown in changes:
        committed(lambda shown=shown: visibility(agent, item, shown))
    before = seen(ticket)
    response = client.post(url, data)
    item.refresh_from_db()
    assert item.removed_at is None and seen(ticket) == before and not events(item).exists()
    assert response.status_code == 200 and CONFLICT in " ".join(FormMarkup(response).words)
    assert REASON.encode() in response.content
    # Adapted: the page drawn again keeps the revision it was opened with (so sending it again is
    # refused the same way); a page opened now carries the current one, for any number of changes.
    assert (
        f'name="expected_revision" value="{data["expected_revision"]}"' in response.content.decode()
    )
    fresh = client.get(url)
    assert service_input(fresh, item)["expected_revision"] == str(seen(ticket))
    assert service_input(fresh, item)["expected_revision"] != data["expected_revision"]


def test_reopened_removal_form_shows_the_changed_visibility(client, agent, ticket, system):
    item = attach(agent, system, ticket)
    client.force_login(agent)
    url = remove_url(item)
    hidden = FormMarkup(client.get(url))
    committed(lambda: visibility(agent, item, True))
    shown = FormMarkup(client.get(url))
    assert LABEL in " ".join(hidden.words) and LABEL in " ".join(shown.words)
    assert hidden.words != shown.words, (
        "The two removal forms give the operator identical visible information."
    )


@pytest.mark.parametrize("when", ["before-post", "during-post"])
def test_refused_removal_draws_the_current_already_removed_page(
    client, agent, ticket, system, when
):
    item = attach(agent, system, ticket)
    client.force_login(agent)
    url = remove_url(item)
    data = service_input(client.get(url), item)

    def other_removal():
        committed(
            lambda: services.remove_link(
                actor=agent, attachment_id=item.pk, reason="Morning delivery finished"
            )
        )

    if when == "before-post":
        other_removal()
        response = client.post(url, data)
    else:
        with paused(views, "remove_link", other_removal):
            response = client.post(url, data)
    item.refresh_from_db()
    assert events(item).count() == 1 and item.removal_reason == "Morning delivery finished"
    assert (
        response.status_code == 200
        and b"Already removed" in response.content
        and FormMarkup(response).count == 0
    )


@pytest.mark.parametrize("when", ["before-post", "during-post"])
def test_removal_after_closure_explains_closure_without_offering_remove(
    client, agent, ticket, system, when
):
    ticket = resolve(agent, ticket, days_ago=1)
    item = attach(agent, system, ticket)
    client.force_login(agent)
    url = remove_url(item)
    data = service_input(client.get(url), item)

    def close():
        committed(
            lambda: desk.change_status(
                actor=agent, request=ticket, expected_revision=seen(ticket), status=Status.CLOSED
            )
        )

    if when == "before-post":
        close()
        response = client.post(url, data)
    else:
        with paused(views, "remove_link", close):
            response = client.post(url, data)
    item.refresh_from_db()
    assert item.removed_at is None and not events(item).exists()
    assert (
        services.CLOSED_MESSAGE in messages(response)
        or services.CLOSED_MESSAGE.encode() in response.content
    )
    assert FormMarkup(response).count == 0, (
        "The refusal still offers to submit Remove link on a closed ticket."
    )


@pytest.mark.parametrize("dialog", [False, True], ids=["page", "dialog"])
def test_an_older_form_without_state_is_refused_with_recovery_instructions(
    client, agent, ticket, system, dialog
):
    item = attach(agent, system, ticket)
    client.force_login(agent)
    headers = {"X-VR-Form": "1"} if dialog else {}
    url = remove_url(item)
    fields = FormMarkup(client.get(url, headers=headers)).hidden
    # Adapted (C7): a page drawn before this release sends no ticket revision.
    fields.pop("expected_revision", None)
    response = client.post(url, {**fields, "reason": REASON}, headers=headers)
    item.refresh_from_db()
    assert item.removed_at is None and not events(item).exists()
    text = " ".join(FormMarkup(response).words).lower()
    assert REASON.encode() in response.content
    assert any(
        word in text for word in ("reload", "reopen", "open it again", "open the form again")
    ), text


def test_person_removal_keeps_note_and_key_out_of_history_and_digest(
    client, capable, ticket, system, caplog
):
    item = attach(capable, system, ticket, target="person")
    client.force_login(capable)
    url = remove_url(item)
    page = client.get(url)
    assert b"Collection record" not in page.content and IDENTIFIER.encode() not in page.content
    assert "no-store" in page["Cache-Control"]
    # Adapted: the removal page carries the ticket's revision, no digest of the attachment.
    assert b"original_state" not in page.content
    response = client.post(url, service_input(page, item))
    assert response.status_code == 302
    item.refresh_from_db()
    assert item.removal_note == NOTE and item.removal_reason_code == "no_longer_needed"
    assert events(item).get().reason == "no_longer_needed"
    already = client.get(url)
    assert b"Already removed" in already.content
    public = (
        str(list(events(item).values_list("before", "after", "reason")))
        + str(list(ticket.entries.values_list("body", "event_data")))
        + caplog.text
        + str(messages(response))
        + already.content.decode()
    )
    assert NOTE not in public and IDENTIFIER not in public


@pytest.mark.parametrize("action", ["remove", "show"])
def test_person_authorization_is_checked_before_a_stale_decision(
    client, capable, ticket, system, action
):
    """Observe the capability check on a fully authorized request with an outdated form."""
    item = attach(capable, system, ticket, target="person")
    client.force_login(capable)
    if action == "remove":
        url = remove_url(item)
        data = service_input(client.get(url), item)
    else:
        url = show_url(item)
        data = {"shown": "1", "expected_revision": seen(ticket)}
    committed(lambda: remove(capable, item))
    with patch.object(services, "require_capability", wraps=services.require_capability) as checked:
        response = client.post(url, data)
    assert response.status_code in (200, 302) and events(item).count() == 1
    assert checked.call_count >= 1, (
        "The locked service answered before checking this person's link capability."
    )


@pytest.mark.parametrize("choice", ["0", "1", None, "other"])
def test_visibility_action_from_current_page(client, agent, ticket, system, choice):
    initial = choice != "1"
    item = attach(agent, system, ticket, shown=initial)
    client.force_login(agent)
    before = seen(ticket)
    data = {"expected_revision": before}
    if choice is not None:
        data["shown"] = choice
    response = client.post(show_url(item), data)
    item.refresh_from_db()
    valid = choice in ("0", "1")
    assert response.status_code == 302 and seen(ticket) == before + valid
    assert item.shown_to_client == (choice == "1" if valid else initial)
    wanted = {"1": "Shown to the client.", "0": "Hidden from the client."}.get(choice, CHOOSE)
    assert messages(response) == [wanted]


@pytest.mark.parametrize("change", ["removed", "closed"])
@pytest.mark.parametrize("current", [False, True], ids=["old-revision", "current-revision"])
def test_visibility_checks_revision_before_closed_or_removed(
    client, agent, ticket, system, change, current
):
    ticket = resolve(agent, ticket, days_ago=1)
    item = attach(agent, system, ticket)
    client.force_login(agent)
    before = seen(ticket)
    if change == "closed":
        committed(
            lambda: desk.change_status(
                actor=agent, request=ticket, expected_revision=before, status=Status.CLOSED
            )
        )
        refusal = services.CLOSED_MESSAGE
    else:
        committed(lambda: remove(agent, item))
        refusal = "Only a current link on a ticket can be shown to the client."
    revision = seen(ticket)
    response = client.post(
        show_url(item), {"shown": "1", "expected_revision": revision if current else before}
    )
    item.refresh_from_db()
    assert not item.shown_to_client and seen(ticket) == revision
    assert messages(response) == [refusal if current else desk.STALE]


def test_waiting_removal_checks_the_committed_visibility(client, agent, ticket, system):
    item = attach(agent, system, ticket)
    client.force_login(agent)
    url = remove_url(item)
    data = service_input(client.get(url), item)
    before = seen(ticket)
    writer = Held(lambda: visibility(agent, item, True))
    submission = Session(lambda: client.post(url, data)).start()
    try:
        assert submission.waiting_or_ended() == "waiting"
        assert _blocked_by(submission.pid, writer.session.pid)
    finally:
        writer.release()
        submission.join()
    assert writer.session.outcome == submission.outcome == "done", (
        writer.session.error,
        submission.error,
    )
    item.refresh_from_db()
    assert item.shown_to_client and item.removed_at is None and not events(item).exists()
    assert seen(ticket) == before + 1
    response = submission.result
    assert CONFLICT in " ".join(FormMarkup(response).words)
    assert REASON.encode() in response.content


@pytest.mark.parametrize("action", ["remove", "show", "invalid-choice"])
def test_person_link_forms_require_the_current_capability(client, capable, ticket, system, action):
    item = attach(capable, system, ticket, target="person")
    client.force_login(capable)
    if action == "remove":
        url = remove_url(item)
        data = service_input(client.get(url), item)
    else:
        url = show_url(item)
        data = {"shown": "1" if action == "show" else "other", "expected_revision": seen(ticket)}
    committed(
        lambda: capable.user_permissions.remove(
            Permission.objects.get(codename="view_patient_information")
        )
    )
    response = client.post(url, data)
    item.refresh_from_db()
    assert response.status_code == 404 and not events(item).exists() and item.removed_at is None
    assert not item.shown_to_client
    assert IDENTIFIER.encode() not in response.content and NOTE.encode() not in response.content
    assert CONFLICT not in messages(response) and CHOOSE not in messages(response)


def test_desk_help_summary_of_an_earlier_ticket_page_on_the_add_form(client, agent, ticket, system):
    """The ticket help summary promises refusal after a change. Its detailed Links section
    and Add form deliberately permit this independent edit."""
    client.force_login(agent)
    url = reverse("link_records:request-link-add", args=[ticket.pk])
    assert url.encode() in client.get(reverse("desk:request-detail", args=[ticket.pk])).content
    page = client.get(url)
    assert page.status_code == 200
    earlier = seen(ticket)
    fields = {
        **FormMarkup(page).hidden,
        "system": system.pk,
        "record_type": "order",
        "role": "related",
        "identifier": IDENTIFIER,
    }
    committed(
        lambda: desk.add_entry(
            actor=agent,
            request=ticket,
            expected_revision=earlier,
            kind=EntryKind.NOTE,
            body="Collection arranged",
        )
    )
    response = client.post(url, fields)
    # Adapted (C8): the Add link page sends the ticket's revision and is refused after any change
    # to the ticket, so the ticket help's tip holds for it too.
    assert response.status_code == 200 and ticket.link_attachments.count() == 0
    assert CONFLICT in " ".join(FormMarkup(response).words)
    help_page = documentation_for_route("desk:request-detail")
    tip = (
        "If someone changed the ticket after you opened it, your change is refused as stale. "
        "Reload, read the latest entries and try again."
    )
    assert tip in help_page["good_to_know"]


def test_another_person_link_is_a_conflict_even_for_a_different_identifier(
    client, capable, ticket, system
):
    client.force_login(capable)
    url = reverse("link_records:request-link-add", args=[ticket.pk])
    page = client.get(url)
    fields = {
        **FormMarkup(page).hidden,
        "system": system.pk,
        "record_type": "patient",
        "role": "related",
        "identifier": "ORD-TEST-000962",
        "display_label": "Delivery alias",
    }
    committed(lambda: attach(capable, system, ticket, target="person"))
    response = client.post(url, fields)
    # Adapted (C8): the other person-level link changed the ticket, so the page is refused as
    # stale before the one-person-link rule is reached; either way nothing is added.
    assert response.status_code == 200 and CONFLICT.encode() in response.content
    assert ticket.link_attachments.count() == 1
    help_page = documentation_for_route("link_records:request-link-add")
    descriptions = [
        item["description"]
        for section in help_page["sections"]
        for item in section.get("items", [])
        if item["label"] == "History"
    ]
    assert not any(
        description.endswith(
            "Adding checks only for the same link in the same role, "
            "so it still works after other changes to the ticket."
        )
        for description in descriptions
    )


@pytest.mark.parametrize("capitals", [False, True], ids=["exact", "capitals"])
def test_equal_job_draw_remains_an_exception_to_the_old_name_guarantee(capitals):
    chosen = "scheduled-jobs-aabbccdd"
    spelling = chosen.upper() if capitals else chosen
    with patch.object(jobs_actor.secrets, "token_hex", side_effect=["aabbccdd", "bbccddee"]):
        state, job = while_held(
            lambda: User.objects.create_user(spelling, "delivery@example.invalid"),
            jobs_actor.system_account,
        )
    assert job.pk != User.objects.get(username=spelling).pk and not job.is_active
    assert AuditEvent.objects.get(action="jobs.system_account_created").actor_id == job.pk
    assert (state, job.username) == (
        ("ended", chosen) if capitals else ("waiting", "scheduled-jobs-bbccddee")
    )
