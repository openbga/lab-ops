"""Confirmation of unchanged defaults when a session-limited copy cannot be drawn."""

import json
from pathlib import Path

import pytest
from django import forms
from django.urls import reverse
from django.utils import timezone

from accounts import kept
from accounts.models import KeptDraft
from desk import services as tickets
from desk import views as desk
from desk.models import ServiceRequest
from desk.tests import test_kept_typing as composer
from nonconformance import services
from nonconformance.tests import test_review_kept_typing_nonconformance as prior
from request_forms.intake import slot
from request_forms.tests import conftest as ticket_forms
from request_forms.tests import test_kept_typing_new_ticket as new_ticket
from tests.test_review_kept_typing_new_ticket import hidden, selected

pytestmark = pytest.mark.django_db
catalog = ticket_forms.catalog
logistics = ticket_forms.logistics
pickup_category = ticket_forms.pickup_category
pickup = ticket_forms.pickup
portal_user = ticket_forms.portal_user
owned = prior.owned
decided = prior.decided


@pytest.mark.parametrize("fits", [False, True], ids=["retired-answer-only", "body-still-fits"])
def test_new_ticket_from_organization_restores_only_announced_edits(
    agent, clinic, plain_category, monkeypatch, fits
):
    # Model a legacy ticket layout with a dispatch box and no channel or entry-kind controls.
    # Staff could clear the organization, write in the since-retired box and time out.
    class EarlierTicket(desk.RequestCreateForm):
        channel = None
        first_entry_kind = None
        dispatch_note = forms.CharField(required=False)
        groups = (*desk.RequestCreateForm.groups, ("Dispatch", (("dispatch_note", "col-12"),)))

    address = reverse("desk:request-create") + f"?client={clinic.pk}"
    browser = composer.browser_for(agent)
    with monkeypatch.context() as earlier:
        earlier.setattr(desk, "RequestCreateForm", EarlierTicket)
        first = browser.get(address)
        assert selected(first, "client") == str(clinic.pk)
        assert 'name="dispatch_note"' in first.text
        changes = {"client": "", "dispatch_note": "Call on arrival."}
        if fits:
            changes["body"] = "Collect at the north door."
        prior.keep_page(browser, agent, first, changes)
    returned = browser.get(address)
    fresh = browser.get(address)
    assert selected(fresh, "client") == str(clinic.pk)
    assert ("none of it fits" in returned.text) is (not fits)
    assert "Call on arrival." not in returned.text
    if fits:
        assert new_ticket.value(returned, "body") == changes["body"]
        assert selected(returned, "client") == ""
    else:
        # Complete the required fields without changing the organization. Get the terms
        # from the same category fragment that an ordinary category choice would request.
        fields = prior.PostedControls(returned).values
        section = browser.get(
            new_ticket.SECTION, {"category": plain_category.pk, "client": fields["client"]}
        )
        posted = {
            **fields,
            "category": str(plain_category.pk),
            "category_terms": hidden(section, "category_terms"),
            "subject": "Check the pickup time",
            "body": "The cartons are ready.",
        }
        logged = browser.post(address, posted)
        ticket = ServiceRequest.objects.get(subject=posted["subject"])
        assert logged.status_code == 302
        assert (fields["client"], ticket.client_id) == (str(clinic.pk), clinic.pk), (
            "Not put back cleared the organization. The ticket was logged without a client."
        )


@pytest.mark.parametrize("fits", [False, True], ids=["nothing-drawable", "intake-text-drawable"])
def test_prepare_preserves_initial_values_in_each_part_of_the_ticket(agent, clinic, pickup, fits):
    # Exercise the actual ticket, picker and request-form controls together with distinct
    # defaults. The page-level test above covers the preliminary draw that selects these parts.
    response = composer.browser_for(agent).get(reverse("desk:request-create"))
    ticket = desk.RequestCreateForm(initial={"reported_by": "Courier", "client": clinic.pk})
    picker, intake = slot(pickup.category, agent, query={"request_form": str(pickup.pk)})
    intake.initial["field-courier"] = "Route A"
    parts = kept.Parts(ticket, picker, intake)
    original = [part.initial.copy() for part in parts.forms]
    values = {"reported_by": "", "request_form": "", "field-courier": "", "priority": "retired"}
    if fits:
        values["field-courier"] = "Route B"
    given = kept.Taken(values, timezone.now(), kept.controls_of(parts), False, None)
    notice = kept.prepare(response.wsgi_request, parts, "staff-ticket", taken=given)
    assert notice["nothing"] is (not fits)
    if fits:
        assert [
            ticket["reported_by"].value(),
            picker["request_form"].value(),
            intake["field-courier"].value(),
        ] == ["", "", "Route B"]
    else:
        assert [part.initial for part in parts.forms] == original
        assert str(picker["request_form"].value()) == str(pickup.pk)


@pytest.mark.parametrize(
    "surface", ["composer", "portal-reply", "portal-new", "portal-request", "investigation"]
)
def test_no_fit_on_a_single_form_preserves_the_controls_drawn_on_a_fresh_visit(
    agent, portal_user, plain_ticket, pickup, owned, surface
):
    pages = {
        "composer": (
            agent,
            reverse("desk:request-detail", args=[plain_ticket.pk]),
            "composer",
            "body",
        ),
        "portal-reply": (
            portal_user,
            reverse("portal:request-detail", args=[plain_ticket.number]),
            "reply_form",
            "body",
        ),
        "portal-new": (portal_user, reverse("portal:request-create"), "form", "description"),
        "portal-request": (
            portal_user,
            reverse("portal_forms:raise", args=[pickup.key]),
            "form",
            "description",
        ),
        "investigation": (agent, prior.url("investigation", owned.pk), "form", "similar_cases"),
    }
    user, address, context, field = pages[surface]
    if surface == "investigation":
        prior.investigation(owned, agent, "The pickup log was complete.")
    browser = composer.browser_for(user)
    page = browser.get(address)
    assert page.status_code == 200
    token, names, _ = composer.marker(page)
    composer.end_open(user)
    assert prior.receipt(browser, token, {field: "Verify the manifest."})["held"] == [True]
    # The old layout had a checklist in place of today's text field and an additional note.
    # The remaining controls were blank. The checklist's two answers cannot fit one text field.
    values = {name: "" for name in names}
    values.update({field: ["am", "pm"], "old_note": "Verify the manifest."})
    draft = KeptDraft.objects.get()
    draft.values = values
    draft.controls.update({field: "choices", "old_note": "text"})
    draft.save(update_fields=["values", "controls"])
    browser.force_login(user)
    returned = browser.get(address)
    assert "none of it fits" in returned.text
    fresh = browser.get(address)
    assert "data-vr-kept-notice" not in fresh.text
    for name in returned.context[context].fields:
        if name in names:
            assert returned.context[context][name].value() == fresh.context[context][name].value()


@pytest.mark.parametrize(
    "stored,sent,same",
    [
        (None, 0, True),
        (0, None, True),
        (0, 0, True),
        (None, None, True),
        (None, 1, False),
        (1, 0, False),
        (0, 1, False),
        (2, 1, False),
        (2_147_483_647, 2_147_483_647, True),
    ],
)
def test_revision_equivalence_keeps_distinct_answers_and_controls_apart(stored, sent, same):
    first = ({"body": "Two cartons"}, {"body": "text"}, False, stored)
    for values, controls, dropped, equal in [
        ({"body": "Two cartons"}, {"body": "text"}, False, True),
        ({"body": "Three cartons"}, {"body": "text"}, False, False),
        ({"body": "Two cartons"}, {"body": "choice"}, False, False),
        ({"body": "Two cartons"}, {"body": "text"}, True, False),
        (
            {"body": "Two cartons", "request_form": ""},
            {"body": "text", "request_form": "choice"},
            False,
            False,
        ),
    ]:
        assert kept._same_copy(first, (values, controls, dropped, sent)) is (same and equal)


@pytest.mark.parametrize("route,parent", [("reopen", "record"), ("from-ticket", "ticket")])
def test_help_after_refusal_leads_to_a_parent_that_can_still_be_opened(
    route, parent, owned, decided, agent, lead
):
    if route == "reopen":
        record = prior.ready_to_close(owned, agent)
        record = services.close(
            actor=lead, nc=record, expected_revision=record.revision, note="Review complete."
        )
        address = prior.url(route, record.pk)
        destination = prior.url("detail", record.pk)
        browser, _ = prior.pending(lead, address, {"reason": prior.DRAFT})
        services.reopen(
            actor=lead, nc=record, expected_revision=record.revision, reason="Follow-up required."
        )
    else:
        address = prior.url(route, decided.pk)
        destination = reverse("desk:request-detail", args=[decided.pk])
        browser, _ = prior.pending(
            agent,
            address,
            {
                "title": prior.DRAFT,
                "severity": "minor",
                "description": "Review the courier log.",
            },
        )
        tickets.review_complaint(
            actor=lead, request=decided, expected_revision=decided.revision, note="Checked."
        )
    restored = browser.get(address)
    refused = browser.post(address, prior.PostedControls(restored).values)
    assert refused.status_code == 200 and refused.context["form"].non_field_errors()
    assert f'href="{destination}">Cancel</a>' in refused.text
    page = browser.get(destination)
    assert page.status_code == 200 and f'href="{address}"' not in page.text

    help_file = Path(__file__).resolve().parents[2] / "docs/pages/nonconformance" / f"{route}.json"
    help_data = json.loads(help_file.read_text())
    advice = next(
        item["description"]
        for section in help_data["sections"]
        for item in section.get("items", [])
        if item["label"] == "Signed out by a time limit"
    )
    assert f"after a refused save go back to the {parent}" in advice
