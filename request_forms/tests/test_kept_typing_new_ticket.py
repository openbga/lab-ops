"""What a staff member was typing in a new ticket, kept when a session limit signs them out
(accounts.kept, desk.views.KEEP_NEW_TICKET): the ticket's own fields with the organization, site
and contact by id, the ticket form chosen and that form's fields, kept as one copy and given back
at the next sign-in in the same browser, drawn in a page whose category section and ticket form
come from the copy. Never files or patient information. All data is synthetic."""

import html
import json
import re
from datetime import timedelta

import pytest
from django.db.models.functions import Now
from django.test import Client as BrowserClient
from django.urls import reverse

from accounts import kept
from accounts.models import AccountSession, KeptDraft
from clients.models import ClientContact
from desk.models import ServiceRequest

from .conftest import PATIENT_SENTINEL

pytestmark = pytest.mark.django_db

PAGE = reverse("desk:request-create")
SECTION = reverse("desk:category-guidance")
KEY = "staff-ticket"
TICKET_FIELDS = [
    "category",
    "priority",
    "client",
    "site",
    "contact",
    "reported_by",
    "channel",
    "client_reference",
    "subject",
    "first_entry_kind",
    "body",
    "assign_to",
]


def browser_for(user):
    browser = BrowserClient()
    browser.force_login(user)
    return browser


def end_open(user, ending=AccountSession.Ending.INACTIVITY):
    """End the account's open session as a limit would have."""
    row = AccountSession.objects.get(user=user, ended_at__isnull=True)
    AccountSession.objects.filter(pk=row.pk).update(ended_at=Now(), ending=ending)
    return row


def marker(response):
    """(token, names, attributes) of the page's or section's keep marker, or None."""
    text = response.content.decode()
    found = re.search(r'<input type="hidden" name="_vr_kept" value="([^"]+)"([^>]*)>', text)
    if found is None:
        return None
    attrs = found.group(2)
    names = re.search(r'data-vr-kept-fields="([^"]*)"', attrs)
    return found.group(1), json.loads(html.unescape(names.group(1))), attrs


def section_marker(browser, category, **query):
    """The marker of the category section drawn for `category` (as htmx asks for it)."""
    response = browser.get(SECTION, {"category": category.pk, **query}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    return marker(response)


def send(browser, token, values):
    response = browser.post(
        reverse("account-session-keep"),
        data=json.dumps({"forms": [{"token": token, "values": values}]}),
        content_type="application/json",
    )
    assert response.status_code == 200
    return response.json()["kept"]


def typed(pickup, clinic, clinic_site, **extra):
    """What a person typed in a new ticket with the pickup form, as the page sends it."""
    values = {
        "category": str(pickup.category_id),
        "priority": "",
        "client": str(clinic.pk),
        "site": str(clinic_site.pk),
        "contact": "",
        "reported_by": "",
        "channel": "email",
        "client_reference": "ORD-1001",
        "subject": "Cooler pickup",
        "first_entry_kind": "reply",
        "body": "Two coolers at the north door.",
        "assign_to": "",
        "request_form": str(pickup.pk),
        "field-count": "3",
        "field-courier": "Route 4",
        "field-handling": "Frozen",
        "field-patient": PATIENT_SENTINEL,
    }
    values.update(extra)
    return values


def kept_then_signed_in(user, pickup, clinic, clinic_site, **extra):
    """The pickup section's marker, a limit ending its session, the typing kept; then the same
    browser signed in again."""
    browser = browser_for(user)
    browser.get(PAGE)
    token, _names, _attrs = section_marker(browser, pickup.category)
    end_open(user)
    assert send(browser, token, typed(pickup, clinic, clinic_site, **extra)) == 1
    browser.force_login(user)
    return browser


def value(response, name):
    """The value a control of the page is drawn with: the ticket's own fields, the picker, or the
    ticket form's fields."""
    form = response.context["form"]
    if name in form.fields:
        return form[name].value()
    picker, values_form = response.context["intake_drawn"]
    for part in (picker, values_form):
        if part is not None and name in part.fields:
            return part[name].value()
    raise KeyError(name)


def missed_hidden(response):
    found = re.search(r"<p[^>]*data-vr-kept-missed[^>]*>", response.content.decode())
    assert found is not None
    return " hidden" in found.group(0)


# the marker


def test_the_page_names_the_tickets_fields_with_the_organization_by_id(agent):
    browser = browser_for(agent)
    token, names, attrs = marker(browser.get(PAGE))
    # No category yet: the ticket's own fields, never the files or the category's hidden terms.
    assert names == TICKET_FIELDS and "data-vr-kept-bound" not in attrs
    data = kept._read(token)
    assert data["k"] == KEY and data["r"] is None
    assert data["t"] == ["reported_by", "client_reference", "subject", "body"]
    assert {data["w"][name] for name in ("client", "site", "contact")} == {"choice"}


def test_the_section_names_the_chosen_ticket_forms_fields_but_never_patient_information(
    capable, pickup
):
    browser = browser_for(capable)
    token, names, _attrs = section_marker(browser, pickup.category)
    section = browser.get(SECTION, {"category": pickup.category_id}).content.decode()
    assert 'name="field-patient"' in section  # drawn for a holder of the capability...
    assert "field-patient" not in names  # ...and never kept
    assert names[: len(TICKET_FIELDS)] == TICKET_FIELDS
    assert {"request_form", "field-count", "field-courier", "field-handling"} <= set(names)
    assert "details_not_known" in names
    assert not {"files", "form_definition", "category_terms"} & set(names)
    # The fields belong to the form the picker chose.
    assert kept._read(token)["r"] == ["request_form", pickup.pk, "request_form"]


def test_no_form_chosen_names_the_picker_and_no_form_fields(agent, pickup):
    _token, names, _attrs = section_marker(browser_for(agent), pickup.category, request_form="")
    assert "request_form" in names
    assert not [name for name in names if name.startswith("field-")]


# kept and given back


def test_a_new_ticket_comes_back_with_its_category_organization_and_ticket_form(
    agent, pickup, clinic, clinic_site
):
    browser = kept_then_signed_in(agent, pickup, clinic, clinic_site)
    stored = KeptDraft.objects.get()
    assert PATIENT_SENTINEL not in json.dumps(stored.values) and stored.form == KEY

    page = browser.get(PAGE)
    text = page.content.decode()
    assert page.status_code == 200
    assert "is back in the form" in text and missed_hidden(page)
    assert value(page, "category") == str(pickup.category_id)
    assert value(page, "client") == str(clinic.pk) and value(page, "site") == str(clinic_site.pk)
    assert re.search(rf'<option value="{clinic.pk}" selected\s*>Example Clinic', text)
    assert value(page, "subject") == "Cooler pickup" and value(page, "channel") == "email"
    assert value(page, "first_entry_kind") == "reply"
    # The category section is drawn for the category given back, with its ticket form.
    assert "Number of coolers" in text
    assert value(page, "request_form") == str(pickup.pk)
    assert value(page, "field-count") == "3" and value(page, "field-courier") == "Route 4"
    assert value(page, "field-handling") == "Frozen"
    assert PATIENT_SENTINEL not in text
    _token, _names, attrs = marker(page)
    assert "data-vr-kept-restored" in attrs and "data-vr-kept-bound" in attrs
    assert KeptDraft.objects.get().taken_at is not None
    # Given back once: Discard (the page again) draws it empty.
    again = browser.get(PAGE)
    assert value(again, "subject") is None and "is back in the form" not in again.content.decode()


def test_a_copy_saying_another_form_was_chosen_keeps_none_of_the_drawn_forms_fields(
    agent, pickup, clinic, clinic_site
):
    # The picker was changed to No form and the session ended before the section was drawn again.
    browser = kept_then_signed_in(agent, pickup, clinic, clinic_site, request_form="")
    stored = KeptDraft.objects.get()
    assert not [name for name in stored.values if name.startswith("field-")] and stored.dropped
    page = browser.get(PAGE)
    assert value(page, "subject") == "Cooler pickup"
    assert value(page, "request_form") == "" and "Number of coolers" not in page.content.decode()
    assert not missed_hidden(page)  # some of it was not put back


def test_an_organization_or_site_deactivated_since_is_not_given_back(
    agent, pickup, clinic, clinic_site
):
    browser = kept_then_signed_in(agent, pickup, clinic, clinic_site)
    type(clinic_site).objects.filter(pk=clinic_site.pk).update(is_active=False)
    page = browser.get(PAGE)
    assert value(page, "site") in (None, "") and value(page, "client") == str(clinic.pk)
    assert value(page, "subject") == "Cooler pickup" and not missed_hidden(page)
    assert "Main office" not in page.content.decode()


def test_a_contact_of_the_organization_comes_back(agent, pickup, clinic, clinic_site):
    contact = ClientContact.objects.create(
        client=clinic, name="Robin Example", email="robin@example.invalid"
    )
    browser = kept_then_signed_in(agent, pickup, clinic, clinic_site, contact=str(contact.pk))
    page = browser.get(PAGE)
    assert value(page, "contact") == str(contact.pk) and "Robin Example" in page.content.decode()


def test_a_ticket_form_retired_since_is_not_drawn_and_its_fields_are_not_put_back(
    agent, pickup, clinic, clinic_site
):
    browser = kept_then_signed_in(agent, pickup, clinic, clinic_site)
    type(pickup).objects.filter(pk=pickup.pk).update(is_active=False)
    page = browser.get(PAGE)
    text = page.content.decode()
    assert value(page, "category") == str(pickup.category_id)
    assert "Number of coolers" not in text and "Route 4" not in text
    assert value(page, "subject") == "Cooler pickup" and not missed_hidden(page)


def test_a_field_marked_as_patient_information_since_is_not_given_back(
    capable, pickup, clinic, clinic_site
):
    browser = kept_then_signed_in(capable, pickup, clinic, clinic_site)
    pickup.fields.filter(key="courier").update(holds_patient_information=True)
    page = browser.get(PAGE)
    assert value(page, "field-courier") in (None, "") and "Route 4" not in page.content.decode()
    assert value(page, "field-count") == "3" and not missed_hidden(page)


def test_a_field_marked_as_patient_information_before_the_keep_is_never_stored(
    capable, pickup, clinic, clinic_site
):
    browser = browser_for(capable)
    token, _names, _attrs = section_marker(browser, pickup.category)
    pickup.fields.filter(key="courier").update(holds_patient_information=True)
    end_open(capable)
    assert send(browser, token, typed(pickup, clinic, clinic_site)) == 1
    assert "field-courier" not in KeptDraft.objects.get().values


def test_the_section_never_gives_a_copy_back(agent, pickup, clinic, clinic_site):
    browser = kept_then_signed_in(agent, pickup, clinic, clinic_site)
    section = browser.get(SECTION, {"category": pickup.category_id}, HTTP_HX_REQUEST="true")
    assert "data-vr-kept-restored" not in marker(section)[2]
    assert KeptDraft.objects.get().taken_at is None
    assert value(browser.get(PAGE), "subject") == "Cooler pickup"


def test_another_staff_account_gets_nothing_back(agent, capable, pickup, clinic, clinic_site):
    browser = kept_then_signed_in(agent, pickup, clinic, clinic_site)
    browser.force_login(capable)
    page = browser.get(PAGE)
    assert value(page, "subject") is None and KeptDraft.objects.get().taken_at is None


def test_a_refused_save_draws_what_was_posted_and_leaves_the_copy(
    agent, pickup, clinic, clinic_site
):
    browser = kept_then_signed_in(agent, pickup, clinic, clinic_site)
    refused = browser.post(PAGE, {"category": pickup.category_id, "subject": "", "body": "Hi"})
    assert refused.status_code == 200 and not ServiceRequest.objects.exists()
    _token, _names, attrs = marker(refused)
    assert "data-vr-kept-bound" in attrs and "data-vr-kept-restored" not in attrs
    assert "Cooler pickup" not in refused.content.decode()
    assert KeptDraft.objects.get().taken_at is None


def test_a_new_ticket_sent_after_the_session_ended_is_kept_from_what_it_posted(
    agent, pickup, clinic, clinic_site
):
    browser = browser_for(agent)
    token, _names, _attrs = section_marker(browser, pickup.category)
    AccountSession.objects.filter(user=agent, ended_at__isnull=True).update(
        last_active_at=Now() - timedelta(minutes=31)
    )
    posted = typed(pickup, clinic, clinic_site)
    response = browser.post(PAGE, {kept.FIELD: token, **posted})
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]
    assert not ServiceRequest.objects.exists()
    values = KeptDraft.objects.get().values
    assert values["subject"] == "Cooler pickup" and values["field-count"] == "3"
    assert values["client"] == str(clinic.pk) and "field-patient" not in values
    browser.force_login(agent)
    page = browser.get(PAGE)
    assert value(page, "field-courier") == "Route 4" and value(page, "site") == str(clinic_site.pk)


# returning to the form after signing in again


def test_a_dialog_form_sent_after_the_session_ended_returns_to_its_own_page(
    agent, pickup, clinic, clinic_site
):
    browser = browser_for(agent)
    token, _names, _attrs = section_marker(browser, pickup.category)
    AccountSession.objects.filter(user=agent, ended_at__isnull=True).update(
        last_active_at=Now() - timedelta(minutes=31)
    )
    list_page = "http://testserver/requests/?view=all"
    response = browser.post(
        PAGE,
        {kept.FIELD: token, **typed(pickup, clinic, clinic_site)},
        HTTP_X_VR_FORM="1",
        HTTP_HX_REQUEST="true",
        HTTP_HX_CURRENT_URL=list_page,
    )
    assert response.status_code == 401
    # Kept: signing in again opens the form's own page, where it is given back.
    assert response["HX-Redirect"].endswith("?next=/requests/new/")
    assert KeptDraft.objects.get().values["subject"] == "Cooler pickup"


def test_a_dialog_form_with_nothing_kept_returns_to_the_page_on_screen(agent):
    browser = browser_for(agent)
    browser.get(PAGE)
    AccountSession.objects.filter(user=agent, ended_at__isnull=True).update(
        last_active_at=Now() - timedelta(minutes=31)
    )
    response = browser.post(
        PAGE,
        {"subject": "No token"},
        HTTP_X_VR_FORM="1",
        HTTP_HX_REQUEST="true",
        HTTP_HX_CURRENT_URL="http://testserver/requests/?view=all",
    )
    assert response.status_code == 401
    assert response["HX-Redirect"].endswith("?next=/requests/%3Fview%3Dall")
    assert not KeptDraft.objects.exists()
