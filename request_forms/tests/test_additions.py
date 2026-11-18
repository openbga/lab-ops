"""Section headings, forms for selected organizations, copying a form, and "Request again"."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from core.models import AuditEvent
from desk.models import EntryKind
from request_forms.forms import PortalRaiseForm, ValuesForm
from request_forms.models import FieldKind, RequestTemplate
from request_forms.selectors import portal_templates, repeat_values, staff_templates
from request_forms.services import (
    clone_template,
    create_field,
    create_request_with_form,
    update_template,
)

from .conftest import PASSWORD, PATIENT_SENTINEL, definition_of

pytestmark = pytest.mark.django_db


def portal_raise(user, template, values, **extra):
    return create_request_with_form(
        actor=user,
        template=template,
        expected_definition=definition_of(template),
        values=values,
        first_entry_kind=EntryKind.REPLY,
        channel="portal",
        **extra,
    )


# Section headings -----------------------------------------------------------------------


def test_heading_asks_nothing_and_groups_the_fields_after_it(admin, portal_user, pickup):
    heading = create_field(
        actor=admin,
        template_id=pickup.pk,
        key="about",
        label="About the pickup",
        kind=FieldKind.HEADING,
        required=True,
        help_text="Tell us what to collect.",
    )
    assert heading.required is False and heading.copy_on_repeat is False
    pickup.fields.filter(pk=heading.pk).update(position=1)
    layout = ValuesForm(pickup, user=portal_user).layout()
    assert layout[0] == ("heading", heading)
    assert all(kind == "field" for kind, _ in layout[1:])
    with pytest.raises(PermissionDenied):  # a heading takes no value from anyone
        portal_raise(portal_user, pickup, {"count": 1, "about": "anything"})


def test_heading_without_fields_for_this_person_is_left_out(admin, portal_user, pickup):
    create_field(
        actor=admin,
        template_id=pickup.pk,
        key="lab-only",
        label="Laboratory use",
        kind=FieldKind.HEADING,
    )
    create_field(
        actor=admin,
        template_id=pickup.pk,
        key="dock",
        label="Dock",
        kind=FieldKind.SHORT_TEXT,
        audience="staff",
    )
    portal_layout = ValuesForm(pickup, user=portal_user).layout()
    assert portal_layout[-1][0] == "field"
    assert all(item.label != "Laboratory use" for kind, item in portal_layout if kind == "heading")


# Selected organizations -----------------------------------------------------------------------


def test_form_for_selected_organizations(
    admin, client, portal_user, outsider, agent, pickup, other_org
):
    update_template(
        actor=admin,
        template_id=pickup.pk,
        expected_revision=pickup.revision,
        reason="Only the water authority orders this way",
        clients=[other_org],
    )
    event = AuditEvent.objects.get(action="request_form.updated")
    assert (event.before["clients"], event.after["clients"]) == ([], [other_org.pk])
    assert pickup not in portal_templates(portal_user)
    assert pickup in portal_templates(outsider)
    assert pickup in staff_templates()
    with pytest.raises(PermissionDenied):
        portal_raise(portal_user, pickup, {"count": 1})
    assert client.login(username=portal_user.username, password=PASSWORD)
    assert client.get(reverse("portal_forms:raise", args=["pickup"])).status_code == 404


# Copy a form -----------------------------------------------------------------------


def test_copy_keeps_fields_and_leaves_the_menu(admin, agent, pickup):
    with pytest.raises(PermissionDenied):
        clone_template(actor=agent, template_id=pickup.pk, key="x", name="X")
    copy = clone_template(actor=admin, template_id=pickup.pk, key="stat-pickup", name="STAT")
    assert copy.featured is False and copy.offered_in_portal is True
    assert list(copy.fields.values_list("key", flat=True)) == list(
        pickup.fields.values_list("key", flat=True)
    )
    assert pickup.fields.count() == copy.fields.count()  # the original keeps its own
    assert AuditEvent.objects.filter(action="request_form_field.created").count() == (
        copy.fields.count()
    )
    with pytest.raises(ValidationError):
        clone_template(actor=admin, template_id=pickup.pk, key="stat-pickup", name="Again")


# Request again -----------------------------------------------------------------------


@pytest.fixture
def earlier(portal_user, pickup, catalog, clinic_site):
    pickup.fields.create(
        key="order-reference",
        label="Order number",
        kind=FieldKind.SHORT_TEXT,
        copy_on_repeat=False,
        position=500,
    )
    pickup.fields.create(
        key="labels", label="Labeled", kind=FieldKind.CONFIRMATION, required=True, position=510
    )
    bags = catalog.items.get(code="TEST-BAG")
    box = catalog.items.get(name="Shipping box")
    from django.utils import timezone

    return portal_raise(
        portal_user,
        pickup,
        {
            "count": 3,
            "ready-at": timezone.now(),
            "handling": "Frozen",
            "patient": PATIENT_SENTINEL,
            "items": {bags.pk: 2, box.pk: 1},
            "order-reference": "ORD-TEST-9",
            "labels": True,
        },
        site=clinic_site,
    )


def test_request_again_copies_safe_answers_only(portal_user, pickup, catalog, earlier, clinic_site):
    box = catalog.items.get(name="Shipping box")
    box.is_active = False
    box.save()
    values, site = repeat_values(pickup, portal_user, earlier.number)
    bags = catalog.items.get(code="TEST-BAG")
    assert values == {"count": "3", "handling": "Frozen", "items": {bags.pk: "2"}}
    assert site == clinic_site
    form = PortalRaiseForm(pickup, user=portal_user, initial_values=values)
    assert form["field-count"].value() == "3"


def test_request_again_only_from_the_accounts_own_requests(
    portal_user, outsider, pickup, staff_template, earlier
):
    assert repeat_values(pickup, outsider, earlier.number) is None
    assert repeat_values(staff_template, portal_user, earlier.number) is None


def test_request_again_page(client, portal_user, outsider, pickup, earlier):
    url = reverse("portal_forms:raise", args=["pickup"]) + f"?again={earlier.number}"
    assert client.login(username=portal_user.username, password=PASSWORD)
    page = client.get(url).content.decode()
    assert f"Answers copied from {earlier.number}" in page
    assert "ORD-TEST-9" not in page and PATIENT_SENTINEL not in page
    client.logout()
    assert client.login(username=outsider.username, password=PASSWORD)
    assert client.get(url).status_code == 404


def test_request_again_link_on_the_details(portal_user, pickup, earlier):
    from django.template import Context, Template
    from django.test import RequestFactory

    http = RequestFactory().get("/")
    http.user = portal_user
    html = Template("{% load request_forms %}{% request_form_details r %}").render(
        Context({"request": http, "r": earlier})
    )
    assert "Request again" in html and f"?again={earlier.number}" in html
    RequestTemplate.objects.filter(pk=pickup.pk).update(offered_in_portal=False)
    html = Template("{% load request_forms %}{% request_form_details r %}").render(
        Context({"request": http, "r": earlier})
    )
    assert "Request again" not in html
