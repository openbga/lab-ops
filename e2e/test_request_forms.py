"""Ticket forms (F1 to F5): a client raises a pickup from the forms menu, staff correct its form
details with a reason, and an administrator adds a field. Run in every `view` (desktop 1366 and
phone 390, light and dark); the client's raise form also at 360 px."""

import re

import pytest
from conftest import View
from helpers import field, sign_in
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

P360 = {"width": 360, "height": 780}


def standard_forms():
    """The demo seed loads the category presets, which bring the standard ticket forms."""
    from request_forms.models import RequestTemplate

    assert RequestTemplate.objects.filter(key="pickup", featured=True).exists()


def no_sideways_scroll(page, width):
    assert page.evaluate("document.documentElement.scrollWidth") <= width


def raise_pickup(page, demo, width, phone):
    standard_forms()
    sign_in(page, "demo_client", demo.password)
    expect(page).to_have_url(re.compile(r"/portal/$"))
    expect(page.get_by_role("heading", name="How can we help?")).to_be_visible()
    no_sideways_scroll(page, width)
    tile = page.get_by_role("link", name=re.compile("^Request a pickup"))
    if phone:
        assert tile.bounding_box()["height"] >= 44
    page.get_by_role("link", name="New ticket").first.click()
    expect(page.get_by_role("heading", level=1, name="New ticket")).to_be_visible()
    expect(page.get_by_role("link", name=re.compile("^Something else"))).to_be_visible()
    page.go_back()
    tile.click()
    expect(page.get_by_role("heading", level=1, name="Request a pickup")).to_be_visible()
    expect(page.get_by_text("Courier or route")).to_have_count(0)  # staff only

    # Sending without the required details keeps the input and says what is missing.
    field(page, "Your reference").fill("ORD-TEST-000051")
    page.get_by_role("button", name="Send ticket").click()
    expect(page.get_by_text("Check the highlighted fields below")).to_be_visible()
    expect(field(page, "Your reference")).to_have_value("ORD-TEST-000051")

    field(page, "Ready for pickup at").fill("2026-11-20 10:00")
    count = field(page, "Number of samples, boxes or coolers")
    assert count.get_attribute("inputmode") == "numeric"
    count.fill("3")
    page.get_by_label("Refrigerated").check()
    field(page, "Person and phone at the pickup point").fill("Front desk, 555-0110")
    from helpers import pick

    pick(field(page, "Site"), label="Main office (MAIN)")
    no_sideways_scroll(page, width)
    send = page.get_by_role("button", name="Send ticket")
    if phone:
        assert send.bounding_box()["height"] >= 44
    send.click()
    expect(page.get_by_text(re.compile(r"Ticket REQ-\d{6} sent"))).to_be_visible()
    return re.search(r"REQ-\d{6}", page.url).group(0)


def test_client_raises_a_pickup_from_the_forms_menu(page: Page, demo, view: View):
    number = raise_pickup(page, demo, view.viewport["width"], view.phone)
    from request_forms.models import FieldValue

    values = dict(
        FieldValue.objects.filter(filled_form__request__number=number).values_list("key", "value")
    )
    assert values == {
        "ready-at": "2026-11-20 10:00",
        "container-count": "3",
        "handling": ["Refrigerated"],
        "site-contact": "Front desk, 555-0110",
    }


def test_client_raise_form_at_360(page: Page, demo):
    page.set_viewport_size(P360)
    raise_pickup(page, demo, P360["width"], phone=True)


def test_staff_correct_form_details_with_a_reason(page: Page, demo, view: View):
    standard_forms()
    from accounts.models import User
    from request_forms import definition
    from request_forms.models import RequestTemplate
    from request_forms.services import create_request_with_form

    pickup = RequestTemplate.objects.get(key="pickup")
    request = create_request_with_form(
        actor=User.objects.get(username="demo_client_services"),
        template=pickup,
        expected_definition=definition.token(pickup),
        values={"container-count": 2},
        details_not_known=True,
        first_entry_kind="note",
        channel="phone",
    )
    sign_in(page, "demo_client_services", demo.password)
    page.goto(f"/request-forms/{request.number}/edit/")
    expect(page.get_by_role("heading", level=1)).to_contain_text(request.number)
    expect(page.get_by_text("required missing")).to_be_visible()
    field(page, "Number of samples, boxes or coolers").fill("4")
    field(page, "Courier or route").fill("Route 3")
    page.get_by_role("button", name="Save form details").click()
    expect(page.get_by_text("A reason is required to change or clear a value.")).to_be_visible()
    expect(field(page, "Courier or route")).to_have_value("Route 3")
    field(page, "Reason for change").fill("The caller counted again.")
    page.get_by_role("button", name="Save form details").click()
    expect(page.get_by_text(f"2 values recorded on {request.number}.")).to_be_visible()
    expect(page.locator(".vr-form-details")).to_contain_text("Route 3")  # the ticket page
    page.goto(f"/request-forms/{request.number}/edit/")
    expect(page.get_by_text("Number of samples, boxes or coolers: 2")).to_be_visible()
    expect(page.get_by_text("The caller counted again.")).to_be_visible()
    no_sideways_scroll(page, view.viewport["width"])


def test_admin_adds_a_field(page: Page, demo, view: View):
    standard_forms()
    sign_in(page, "demo_admin", demo.password)
    page.goto("/setup/request-forms/")
    expect(page.get_by_role("heading", level=1, name="Ticket forms")).to_be_visible()
    no_sideways_scroll(page, view.viewport["width"])
    page.get_by_role("link", name="Pickup", exact=True).click()
    expect(page.get_by_role("heading", level=1, name="Pickup")).to_be_visible()
    no_sideways_scroll(page, view.viewport["width"])
    from request_forms.models import RequestTemplate

    template = RequestTemplate.objects.get(key="pickup")
    page.goto(f"/setup/request-forms/{template.pk}/fields/new/")
    expect(page.get_by_text("This field holds patient information")).to_be_visible()
    field(page, "Label").fill("Gate code")
    page.get_by_role("button", name="Add field").click()
    expect(page.get_by_text("Field Gate code added.")).to_be_visible()
    expect(page.get_by_role("link", name="Gate code")).to_be_visible()
    no_sideways_scroll(page, view.viewport["width"])


def test_client_requests_a_pickup_again(page: Page, demo, view: View):
    number = raise_pickup(page, demo, view.viewport["width"], view.phone)
    page.goto(f"/portal/forms/pickup/?again={number}")
    expect(page.get_by_text(f"Answers copied from {number}")).to_be_visible()
    expect(field(page, "Number of samples, boxes or coolers")).to_have_value("3")
    expect(field(page, "Person and phone at the pickup point")).to_have_value(
        "Front desk, 555-0110"
    )
    expect(page.get_by_label("Refrigerated")).to_be_checked()
    expect(field(page, "Ready for pickup at")).to_have_value("")  # one occasion: not copied
    field(page, "Ready for pickup at").fill("2026-11-21 10:00")
    no_sideways_scroll(page, view.viewport["width"])
    page.get_by_role("button", name="Send ticket").click()
    expect(page.get_by_text(re.compile(r"Ticket REQ-\d{6} sent"))).to_be_visible()
    assert re.search(r"REQ-\d{6}", page.url).group(0) != number


def test_staff_log_a_pickup_with_its_form(page: Page, demo, view: View):
    from helpers import pick

    from desk.models import ServiceRequest

    standard_forms()
    sign_in(page, "demo_client_services", demo.password)
    page.goto("/requests/new/")
    pick(field(page, "Category"), label="Pickup or missed pickup")
    section = page.locator("#category-section")
    expect(section).to_contain_text("Ticket form")
    pick(field(page, "Ticket form"), label="Pickup")
    expect(section.get_by_text("Number of samples, boxes or coolers")).to_be_visible()
    expect(section.get_by_text("Courier or route")).to_be_visible()  # staff-only, for staff
    field(page, "Subject").fill("E2E courier pickup")
    field(page, "What the client asked").fill("Caller needs a pickup this afternoon.")
    field(page, "Courier or route").fill("Route 7")
    page.get_by_role("button", name="Log ticket").click()
    expect(page.get_by_text("Check the highlighted form details below")).to_be_visible()
    expect(field(page, "Courier or route")).to_have_value("Route 7")
    page.get_by_label("Some required details are not known yet").check()
    no_sideways_scroll(page, view.viewport["width"])
    page.get_by_role("button", name="Log ticket").click()
    heading = page.get_by_role("heading", level=1)
    expect(heading).to_contain_text("E2E courier pickup")
    details = page.locator(".vr-form-details")
    expect(details).to_contain_text("Route 7")
    expect(details.get_by_text("Still needed")).to_be_visible()
    no_sideways_scroll(page, view.viewport["width"])
    created = ServiceRequest.objects.get(subject="E2E courier pickup")
    assert created.filled_form.template.key == "pickup"


def test_staff_reveal_a_patient_value(page: Page, demo, view: View):
    from django.contrib.auth.models import Permission

    from accounts.models import User
    from core.models import AuditEvent
    from request_forms.models import FieldValue

    row = (
        FieldValue.objects.filter(key="patient-identifier", superseded_at__isnull=True)
        .select_related("filled_form__request")
        .first()
    )
    assert row is not None, "the demo seed holds one synthetic patient identifier"
    supervisor = User.objects.get(username="demo_supervisor")
    supervisor.user_permissions.add(
        Permission.objects.get(
            codename="view_patient_information", content_type__app_label="accounts"
        )
    )
    sign_in(page, "demo_supervisor", demo.password)
    page.goto(f"/requests/{row.filled_form.request.pk}/")
    details = page.locator(".vr-form-details")
    expect(details).to_contain_text("Patient information recorded.")
    expect(details.get_by_text(row.value)).to_have_count(0)
    details.get_by_role("button", name="Reveal").click()
    expect(details.get_by_text(row.value)).to_be_visible()
    expect(details.get_by_text("Reveal recorded.")).to_be_visible()
    assert AuditEvent.objects.filter(action="request_form.value_revealed").exists()
    no_sideways_scroll(page, view.viewport["width"])


def test_client_form_changed_while_open_is_refused(page: Page, demo, view: View):
    """Reviews 5 and 6: the laboratory changes the form while a client has it open. Sending it is
    refused with a message and nothing is sent; the form is drawn fresh, answers empty, and
    filled again it goes through."""
    standard_forms()
    from accounts.models import User
    from desk.models import ServiceRequest
    from request_forms.models import RequestTemplate
    from request_forms.services import update_field

    sign_in(page, "demo_client", demo.password)
    page.goto("/portal/forms/pickup/")
    expect(page.get_by_role("heading", level=1, name="Request a pickup")).to_be_visible()
    field(page, "Ready for pickup at").fill("2026-11-20 10:00")
    field(page, "Number of samples, boxes or coolers").fill("3")
    page.get_by_label("Refrigerated").check()
    field(page, "Person and phone at the pickup point").fill("Front desk, 555-0110")
    from helpers import pick

    pick(field(page, "Site"), label="Main office (MAIN)")
    count = RequestTemplate.objects.get(key="pickup").fields.get(key="container-count")
    update_field(
        actor=User.objects.get(username="demo_admin"),
        field_id=count.pk,
        expected_revision=count.revision,
        reason="E2E: the laboratory narrows the limit",
        max_value=50,
    )
    before = ServiceRequest.objects.count()
    page.get_by_role("button", name="Send ticket").click()
    expect(page.get_by_text("This form changed after the page was opened")).to_be_visible()
    assert ServiceRequest.objects.count() == before
    # Drawn fresh from the form as it is now, with none of the answers sent.
    expect(field(page, "Number of samples, boxes or coolers")).to_have_value("")
    no_sideways_scroll(page, view.viewport["width"])
    field(page, "Ready for pickup at").fill("2026-11-20 10:00")
    field(page, "Number of samples, boxes or coolers").fill("3")
    page.get_by_label("Refrigerated").check()
    field(page, "Person and phone at the pickup point").fill("Front desk, 555-0110")
    page.get_by_role("button", name="Send ticket").click()
    expect(page.get_by_text(re.compile(r"Ticket REQ-\d{6} sent"))).to_be_visible()
