"""Changes to a form's definition and organization offers during capture."""

import re

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.urls import reverse
from django.utils.html import escape

from desk import services as desk
from desk.models import EntryKind, ServiceRequest
from request_forms import definition, selectors, services
from request_forms.forms import CorrectionForm, ValuesForm
from request_forms.models import (
    Audience,
    Catalog,
    CatalogItem,
    FieldKind,
    FieldValue,
    FilledForm,
    RequestTemplate,
    TemplateField,
)
from request_forms.services import FIELD_FIELDS, FORM_CHANGED, NOT_OFFERED
from taxonomy.terms import drawn

from .conftest import PASSWORD, definition_of
from .test_values import current, portal_raise

pytestmark = pytest.mark.django_db
CHANGED = "changed after the page was opened"
MARK = "SYNTHETIC-MARK"


def login(client, user):
    assert client.login(username=user.username, password=PASSWORD)


def drawn_token(page):
    """The definition token a drawn page carries."""
    match = re.search(r'name="form_definition" value="([^"]+)"', page.content.decode())
    assert match, "the page carries no definition token"
    return match.group(1)


def field(template, key):
    return template.fields.get(key=key)


def edit_field(admin, template, key, **data):
    target = field(template, key)
    services.update_field(
        actor=admin,
        field_id=target.pk,
        expected_revision=target.revision,
        reason="Synthetic definition change",
        **data,
    )


def unmark_patient(admin, template):
    edit_field(admin, template, "patient", holds_patient_information=False)


# One rule for every capture: any definition change refuses the write


def _retire_catalog(admin, template, catalog, clinic):
    services.update_catalog(
        actor=admin,
        catalog_id=catalog.pk,
        expected_revision=catalog.revision,
        reason="Retired",
        is_active=False,
    )


def _retire_item(admin, template, catalog, clinic):
    item = catalog.items.get(code="TEST-BAG")
    services.update_item(
        actor=admin,
        item_id=item.pk,
        expected_revision=item.revision,
        reason="Retired",
        is_active=False,
    )


def _limit_item(admin, template, catalog, clinic):
    item = catalog.items.get(code="TEST-BAG")
    services.update_item(
        actor=admin,
        item_id=item.pk,
        expected_revision=item.revision,
        reason="Fewer per request",
        max_quantity=3,
    )


def _add_item(admin, template, catalog, clinic):
    services.create_item(actor=admin, catalog_id=catalog.pk, name="Sample tube rack")


def _switch_catalog(admin, template, catalog, clinic):
    other = services.create_catalog(actor=admin, key="kits", name="Kits")
    edit_field(admin, template, "items", catalog=other)


def _add_field(admin, template, catalog, clinic):
    services.create_field(
        actor=admin, template_id=template.pk, key="seal", label="Seal", kind=FieldKind.SHORT_TEXT
    )


def _offer_to_clinic(admin, template, catalog, clinic):
    services.update_template(
        actor=admin,
        template_id=template.pk,
        expected_revision=template.revision,
        reason="Only the clinic",
        clients=[clinic],
    )


def _raw_unmark(admin, template, catalog, clinic):
    # Outside the services, without a revision: the token reads the settings themselves.
    TemplateField.objects.filter(template=template, key="patient").update(
        holds_patient_information=False
    )


DEFINITION_CHANGES = {
    "patient flag cleared": lambda a, t, c, o: unmark_patient(a, t),
    "patient flag set": lambda a, t, c, o: edit_field(
        a, t, "ready-at", holds_patient_information=True
    ),
    "audience": lambda a, t, c, o: edit_field(a, t, "tracking", audience=Audience.STAFF),
    "required": lambda a, t, c, o: edit_field(a, t, "ready-at", required=True),
    "kind": lambda a, t, c, o: edit_field(a, t, "courier", kind=FieldKind.LONG_TEXT),
    "choices": lambda a, t, c, o: edit_field(
        a, t, "handling", choices=["Room temperature", "Frozen", "Chilled"]
    ),
    "number limit": lambda a, t, c, o: edit_field(a, t, "count", max_value=9),
    "label": lambda a, t, c, o: edit_field(a, t, "count", label="Coolers or boxes"),
    "field retired": lambda a, t, c, o: edit_field(a, t, "handling", is_active=False),
    "field added": _add_field,
    "catalog retired": _retire_catalog,
    "item retired": _retire_item,
    "item limit": _limit_item,
    "item added": _add_item,
    "field's catalog switched": _switch_catalog,
    "offer narrowed": _offer_to_clinic,
    "patient flag cleared outside the services": _raw_unmark,
    # The category's protection, and meaning changed outside the services.
    "category restricted": lambda a, t, c, o: _category(a, t, is_restricted=True),
    "category made a complaint category": lambda a, t, c, o: _category(a, t, is_complaint=True),
    "category withdrawn from clients": lambda a, t, c, o: _category(a, t, client_may_raise=False),
    "category guidance": lambda a, t, c, o: _category(a, t, guidance="Synthetic new guidance"),
    "label outside the services": lambda a, t, c, o: t.fields.filter(key="count").update(
        label="Number of specimens"
    ),
    "help text outside the services": lambda a, t, c, o: t.fields.filter(key="count").update(
        help_text="Count the specimens"
    ),
    "item renamed outside the services": lambda a, t, c, o: c.items.filter(code="TEST-BAG").update(
        name="Specimen bags"
    ),
    "offer changed outside the services": lambda a, t, c, o: t.clients.add(o),
}


def _category(admin, template, **values):
    from taxonomy.services import update_category

    category = template.category
    category.refresh_from_db()
    update_category(
        actor=admin,
        category_id=category.pk,
        expected_revision=category.revision,
        reason="Synthetic category change",
        **values,
    )


def _portal(drawn, *, portal_user, capable, clinic, pickup, record):
    return services.create_request_with_form(
        actor=portal_user,
        template=pickup,
        expected_definition=drawn,
        values={"count": 1},
        first_entry_kind=EntryKind.REPLY,
        channel="portal",
    )


def _staff(drawn, *, portal_user, capable, clinic, pickup, record):
    return services.create_request_with_form(
        actor=capable,
        template=pickup,
        expected_definition=drawn,
        values={"count": 1},
        first_entry_kind=EntryKind.NOTE,
        channel="phone",
        client=clinic,
    )


def _intake(drawn, *, portal_user, capable, clinic, pickup, record):
    return desk.create_request(
        actor=capable,
        category=pickup.category,
        subject="Synthetic call",
        first_entry_kind=EntryKind.NOTE,
        first_entry_body="Synthetic call",
        channel="phone",
        client=clinic,
        on_created=lambda request: services.attach_values(
            actor=capable,
            request=request,
            template=pickup,
            expected_definition=drawn,
            values={"count": 1},
        ),
    )


def _correction(drawn, *, portal_user, capable, clinic, pickup, record):
    return services.correct_values(
        actor=capable,
        request=record,
        expected_definition=drawn,
        values={"count": 2},
        reason="Recount",
        expected={"count": current(record)["count"].pk},
    )


CAPTURES = {
    "portal raise": _portal,
    "staff intake": _staff,
    "intake on_created": _intake,
    "correction": _correction,
}


@pytest.mark.parametrize("capture", CAPTURES)
@pytest.mark.parametrize("change", DEFINITION_CHANGES)
def test_any_definition_change_refuses_every_capture(
    change, capture, admin, portal_user, capable, clinic, pickup, catalog
):
    record = portal_raise(portal_user, pickup, {"count": 1})
    drawn = definition_of(pickup)
    DEFINITION_CHANGES[change](admin, RequestTemplate.objects.get(pk=pickup.pk), catalog, clinic)
    assert definition_of(pickup) != drawn, "the change did not advance the token"
    requests = ServiceRequest.objects.count()
    values = FieldValue.objects.count()
    with pytest.raises(ValidationError) as refused:
        CAPTURES[capture](
            drawn,
            portal_user=portal_user,
            capable=capable,
            clinic=clinic,
            pickup=RequestTemplate.objects.get(pk=pickup.pk),
            record=record,
        )
    assert FORM_CHANGED in refused.value.messages
    assert ServiceRequest.objects.count() == requests and FieldValue.objects.count() == values


@pytest.mark.parametrize("capture", CAPTURES)
def test_an_unchanged_definition_is_accepted(capture, portal_user, capable, clinic, pickup):
    record = portal_raise(portal_user, pickup, {"count": 1})
    CAPTURES[capture](
        definition_of(pickup),
        portal_user=portal_user,
        capable=capable,
        clinic=clinic,
        pickup=pickup,
        record=record,
    )


@pytest.mark.parametrize("capture", CAPTURES)
@pytest.mark.parametrize("token", ["", "not-a-token", "²", "x" * 5000, None])
def test_a_missing_or_forged_token_is_refused(capture, token, portal_user, capable, clinic, pickup):
    record = portal_raise(portal_user, pickup, {"count": 1})
    with pytest.raises(ValidationError, match=CHANGED):
        CAPTURES[capture](
            token,
            portal_user=portal_user,
            capable=capable,
            clinic=clinic,
            pickup=pickup,
            record=record,
        )


def test_every_field_setting_is_part_of_the_definition():
    """A setting added to fields must be decided on: it is either in the token's settings or
    deliberately left to the field revision (wording only)."""
    wording = {"label", "help_text"}
    settings = {name.removesuffix("_id") for name in definition.FIELD_SETTINGS}
    assert set(FIELD_FIELDS) - wording <= settings
    assert "revision" in settings  # wording changes move the revision, which is covered


def test_reordering_fields_or_items_keeps_open_pages_valid(admin, pickup, catalog):
    drawn = definition_of(pickup)
    services.move_field(actor=admin, field_id=field(pickup, "handling").pk, direction="up")
    services.move_item(actor=admin, item_id=catalog.items.get(code="TEST-BAG").pk, direction="down")
    assert definition_of(pickup) == drawn


# Capture pages


def test_open_correction_keeps_sensitivity_of_first_fill(
    client, capable, admin, pickup, portal_user, agent
):
    """A blank patient field declassified while the correction page is open."""
    record = portal_raise(portal_user, pickup, {"count": 1})
    url = reverse("request_details:edit", args=[record.number])
    login(client, capable)
    page = client.get(url)
    assert page.status_code == 200 and b"field-patient" in page.content
    drawn = drawn_token(page)
    unmark_patient(admin, pickup)
    response = client.post(
        url,
        {
            "form_definition": drawn,
            "field-patient": MARK,
            "seen_patient": "",
            "field-count": "1",
            "seen_count": current(record)["count"].pk,
        },
    )
    body = response.content.decode()
    assert response.status_code == 200 and FORM_CHANGED in body
    assert MARK not in body
    assert "no-store" in response["Cache-Control"]
    assert "patient" not in current(record)
    lines = [line for row in selectors.staff_values(record, agent).rows for line in row.lines]
    assert MARK not in lines
    # The service refuses the drawn token whatever the page does.
    with pytest.raises(ValidationError, match=CHANGED):
        services.correct_values(
            actor=capable,
            request=record,
            expected_definition=drawn,
            values={"patient": MARK},
            reason="",
            expected={"patient": None},
        )


def intake(http, category, **data):
    fields = {
        "category": category.pk,
        "category_terms": drawn([category]),
        "channel": "phone",
        "first_entry_kind": "note",
        "subject": "Synthetic intake",
        "body": "Ordinary fixture",
        **data,
    }
    if "form_definition" not in fields and fields.get("request_form"):
        fields["form_definition"] = definition_of(
            RequestTemplate.objects.get(pk=fields["request_form"])
        )
    return http.post(reverse("desk:request-create"), fields)


def test_stale_intake_patient_input_keeps_cache_protection(client, capable, admin, pickup, clinic):
    """The refused page never caches, nor offers back, input typed as patient information."""
    login(client, capable)
    page = client.get(
        reverse("desk:category-guidance"),
        {"category": pickup.category_id, "request_form": pickup.pk},
    )
    assert b"Patient identifier" in page.content
    drawn = drawn_token(page)
    unmark_patient(admin, pickup)
    response = intake(
        client,
        pickup.category,
        client=clinic.pk,
        request_form=pickup.pk,
        form_definition=drawn,
        **{"field-count": "1", "field-patient": MARK},
    )
    body = response.content.decode()
    assert response.status_code == 200 and not FilledForm.objects.exists()
    assert "no-store" in response["Cache-Control"]
    assert MARK not in body and FORM_CHANGED in body
    # Typed again into the form as it is now, it is ordinary text, knowingly.
    again = intake(
        client,
        pickup.category,
        client=clinic.pk,
        request_form=pickup.pk,
        **{"field-count": "1", "field-patient": "TEST-ORDINARY"},
    )
    assert again.status_code == 302


def test_portal_page_drops_patient_input_typed_before_a_declassification(
    client, portal_user, admin, pickup
):
    login(client, portal_user)
    url = reverse("portal_forms:raise", args=[pickup.key])
    drawn = drawn_token(client.get(url))
    unmark_patient(admin, pickup)
    response = client.post(
        url, {"form_definition": drawn, "field-count": "1", "field-patient": MARK}
    )
    body = response.content.decode()
    assert response.status_code == 200 and not ServiceRequest.objects.exists()
    assert MARK not in body and FORM_CHANGED in body
    assert "no-store" in response["Cache-Control"]
    # A fresh form from the current definition, with none of the answers sent.
    assert 'name="field-count"' in body and 'value="1"' not in body


def test_an_unreadable_token_keeps_no_input(client, portal_user, pickup):
    login(client, portal_user)
    url = reverse("portal_forms:raise", args=[pickup.key])
    response = client.post(
        url, {"form_definition": "forged", "field-count": "7", "field-patient": MARK}
    )
    body = response.content.decode()
    assert response.status_code == 200 and not ServiceRequest.objects.exists()
    assert MARK not in body and FORM_CHANGED in body and 'value="7"' not in body


def test_open_portal_order_handles_catalog_retirement(client, pickup, catalog, admin, portal_user):
    """A catalog retired while a portal order is open."""
    item = catalog.items.get(code="TEST-BAG")
    login(client, portal_user)
    url = reverse("portal_forms:raise", args=[pickup.key])
    old_form = client.get(url)
    line = f"field-items_{item.pk}"
    assert line.encode() in old_form.content
    drawn = drawn_token(old_form)
    _retire_catalog(admin, pickup, catalog, None)
    response = client.post(url, {"form_definition": drawn, "field-count": "1", line: "2"})
    assert response.status_code == 200 and FORM_CHANGED in response.content.decode()
    assert not ServiceRequest.objects.exists() and not FilledForm.objects.exists()


def test_request_again_is_refused_after_an_item_change(client, pickup, catalog, admin, portal_user):
    item = catalog.items.get(code="TEST-BAG")
    record = portal_raise(portal_user, pickup, {"count": 1, "items": {item.pk: 2}})
    login(client, portal_user)
    url = reverse("portal_forms:raise", args=[pickup.key])
    page = client.get(url, {"again": record.number})
    assert page.status_code == 200
    drawn = drawn_token(page)
    _limit_item(admin, pickup, catalog, None)
    response = client.post(
        url, {"form_definition": drawn, "field-count": "1", f"field-items_{item.pk}": "2"}
    )
    assert response.status_code == 200 and FORM_CHANGED in response.content.decode()
    assert ServiceRequest.objects.count() == 1


# Staff intake observes the organization offer


def test_staff_intake_observes_organization_offer(client, agent, clinic, other_org, pickup):
    pickup.clients.set([clinic])
    login(client, agent)
    response = intake(
        client, pickup.category, request_form=pickup.pk, client=other_org.pk, **{"field-count": "2"}
    )
    assert response.status_code == 200 and escape(NOT_OFFERED) in response.content.decode()
    assert not ServiceRequest.objects.exists()
    assert not FilledForm.objects.filter(template=pickup, request__client=other_org).exists()
    # No organization: only forms offered to everyone.
    response = intake(client, pickup.category, request_form=pickup.pk, **{"field-count": "2"})
    assert response.status_code == 200 and not ServiceRequest.objects.exists()
    # One of its organizations: saved.
    response = intake(
        client, pickup.category, request_form=pickup.pk, client=clinic.pk, **{"field-count": "2"}
    )
    assert response.status_code == 302
    assert FilledForm.objects.get().request.client == clinic


def test_staff_intake_offer_follows_the_site_organization(
    client, agent, clinic, clinic_site, pickup
):
    """No organization chosen, a site of an organization the form is offered to: the request
    takes the site's organization, and the form."""
    pickup.clients.set([clinic])
    login(client, agent)
    response = intake(
        client,
        pickup.category,
        request_form=pickup.pk,
        site=clinic_site.pk,
        **{"field-count": "2"},
    )
    assert response.status_code == 302
    assert FilledForm.objects.get().request.client == clinic


def test_staff_intake_service_observes_organization_offer(agent, clinic, other_org, pickup):
    pickup.clients.set([clinic])
    for organization in (other_org, None):
        with pytest.raises(ValidationError, match="offered only to selected organizations"):
            services.create_request_with_form(
                actor=agent,
                template=pickup,
                expected_definition=definition_of(pickup),
                values={"count": 1},
                first_entry_kind=EntryKind.NOTE,
                channel="phone",
                client=organization,
            )
    with pytest.raises(ValidationError, match="offered only to selected organizations"):
        desk.create_request(
            actor=agent,
            category=pickup.category,
            subject="Synthetic call",
            first_entry_kind=EntryKind.NOTE,
            first_entry_body="Synthetic call",
            channel="phone",
            client=other_org,
            on_created=lambda request: services.attach_values(
                actor=agent,
                request=request,
                template=pickup,
                expected_definition=definition_of(pickup),
                values={"count": 1},
            ),
        )
    assert not ServiceRequest.objects.exists()


def test_picker_lists_the_forms_offered_to_the_chosen_organization(
    client, agent, clinic, other_org, pickup
):
    pickup.clients.set([clinic])
    login(client, agent)
    url = reverse("desk:category-guidance")
    for_clinic = client.get(url, {"category": pickup.category_id, "client": clinic.pk})
    assert "Pickup (selected organizations only)" in for_clinic.content.decode()
    for_other = client.get(url, {"category": pickup.category_id, "client": other_org.pk})
    assert "Pickup" not in for_other.content.decode()
    assert "Ticket form" not in for_other.content.decode()  # nothing to choose
    # Not chosen yet: listed, marked, and checked when saved.
    unknown = client.get(url, {"category": pickup.category_id, "client": "²"})
    assert unknown.status_code == 200 and "selected organizations only" in unknown.content.decode()


def test_portal_offer_all_entry_points(client, portal_user, outsider, pickup, clinic, other_org):
    """Portal offers apply to the menu, chooser, form, repeat action and submission."""
    pickup.clients.set([clinic])
    assert pickup in selectors.portal_menu(portal_user)
    assert pickup not in selectors.portal_menu(outsider)
    record = portal_raise(portal_user, pickup, {"count": 1})
    client.force_login(outsider)
    chooser = client.get(reverse("portal_forms:choose"))
    url = reverse("portal_forms:raise", args=[pickup.key])
    assert chooser.status_code == 200 and url.encode() not in chooser.content
    assert client.get(url).status_code == 404
    assert client.get(url, {"again": record.number}).status_code == 404
    response = client.post(url, {"form_definition": definition_of(pickup), "field-count": "2"})
    assert response.status_code == 302 and ServiceRequest.objects.count() == 1
    pickup.clients.set([other_org])
    client.force_login(portal_user)
    page = client.get(reverse("portal:request-detail", args=[record.number]))
    assert (
        page.status_code == 200
        and f'href="{url}?again={record.number}"'.encode() not in page.content
    )
    assert client.get(url, {"again": record.number}).status_code == 404


# The demo seed never widens an offer


@pytest.fixture
def demo_env(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "Synthetic-shared-fixture")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "Synthetic-private-fixture")


def test_seed_preserves_restriction_when_client_code_changes(demo_env):
    from accounts.models import User
    from clients.models import Client
    from clients.services import update_client
    from request_forms.seed import demo_offers

    call_command("seed_demo", verbosity=0)
    code = "EXRES"
    key = next(key for key, codes in demo_offers().items() if codes == {code})
    template = RequestTemplate.objects.get(key=key)
    account = User.objects.get(username="demo_client")
    assert template not in selectors.portal_templates(account)
    offered = set(template.clients.values_list("pk", flat=True))
    org = Client.objects.get(account_number=code)
    update_client(
        actor=User.objects.get(username="demo_admin"),
        client_id=org.pk,
        expected_revision=org.revision,
        reason="Synthetic renumbering",
        account_number="TEST-RENUMBERED",
    )
    # Seeding again creates no organizations, so it leaves every offer as it is.
    call_command("seed_demo", verbosity=0)
    assert set(template.clients.values_list("pk", flat=True)) == offered
    assert template not in selectors.portal_templates(account)


# The stray exception: a posted number in another script


@pytest.mark.parametrize("raw", ["²", "٣", "１", "-1", "1.5", ""])
def test_unusual_digits_are_ordinary_form_errors(raw, pickup, agent, portal_user, capable):
    form = ValuesForm(pickup, {"form_definition": raw, "field-count": "1"}, user=agent)
    assert not form.is_valid() and FORM_CHANGED in form.non_field_errors()
    record = portal_raise(portal_user, pickup, {"count": 1})
    correction = CorrectionForm(
        record.filled_form,
        {"form_definition": definition_of(record), "field-count": "2", "seen_count": raw},
        user=capable,
    )
    assert correction.is_valid() and correction.expected()["count"] is None


@pytest.mark.parametrize("raw", ["²", "٣"])
def test_unusual_digits_in_desk_parameters_are_not_errors(raw, client, agent, pickup):
    login(client, agent)
    assert client.get(reverse("desk:category-guidance"), {"category": raw}).status_code == 200
    created = client.post(
        reverse("desk:request-create"),
        {"category": raw, "channel": "phone", "first_entry_kind": "note", "subject": "S"},
    )
    assert created.status_code == 200 and not ServiceRequest.objects.exists()
    # The list answers a bad parameter as it answers any (never a server error).
    assert client.get(reverse("desk:request-list"), {"per_page": raw}).status_code < 500


def test_catalog_and_item_changes_lock_the_catalog_first(admin, catalog):
    """Item changes take the catalog's lock (definition.lock relies on it); a sanity check that
    the service still works with the extra lock."""
    item = CatalogItem.objects.get(code="TEST-BAG")
    services.update_item(
        actor=admin,
        item_id=item.pk,
        expected_revision=item.revision,
        reason="Renamed",
        name="Transport bags (large)",
    )
    assert Catalog.objects.get(pk=catalog.pk).revision == catalog.revision


def test_staff_service_checks_the_offer_against_the_request_as_created(
    agent, clinic, clinic_site, pickup
):
    """The service checks the organization the created request holds, not an argument."""
    pickup.clients.set([clinic])
    request = services.create_request_with_form(
        actor=agent,
        template=pickup,
        expected_definition=definition_of(pickup),
        values={"count": 1},
        first_entry_kind=EntryKind.NOTE,
        channel="phone",
        client=clinic,
        site=clinic_site,
    )
    assert request.client == clinic and FilledForm.objects.get().request == request
