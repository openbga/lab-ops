"""Where the module meets the desk and the summary: the complaint record form keeps the shape it
was drawn in when the module is switched meanwhile, the complaint review shows the record opened
from the complaint, the summary page's access, and cleared values in a record's history. Synthetic
data only."""

import pytest
from django.urls import reverse

from core.models import LabModule
from desk import services as desk_services
from desk.models import ComplaintReview
from nonconformance import services

pytestmark = pytest.mark.django_db


def fresh(obj):
    return type(obj).objects.get(pk=obj.pk)


@pytest.fixture
def decided(complaint, agent):
    desk_services.decide_complaint(
        actor=agent,
        request=complaint,
        expected_revision=complaint.revision,
        decision="investigate",
        rationale="Repeated delay for one client.",
    )
    ticket = fresh(complaint)
    desk_services.update_complaint(
        actor=agent,
        request=ticket,
        expected_revision=ticket.revision,
        corrective_action_reference="CA-TEST-0042",
    )
    return fresh(ticket)


def record_form(client, ticket):
    return reverse("desk:complaint-record", args=[ticket.pk])


def post_record(client, ticket, shape, **values):
    data = {
        "expected_revision": ticket.revision,
        "findings": "Courier pickup moved.",
        "outcome_communicated_how": "",
        "form_shape": shape,
        **values,
    }
    return client.post(record_form(client, ticket), data)


def test_with_the_module_on_the_record_form_has_no_free_text_reference(
    client, module_on, agent, decided
):
    client.force_login(agent)
    page = client.get(record_form(client, decided)).content.decode()
    assert 'name="corrective_action_reference"' not in page
    assert 'name="form_shape" value="nonconformance"' in page
    response = post_record(client, decided, "nonconformance")
    assert response.status_code == 302
    review = ComplaintReview.objects.get(request=decided)
    assert review.findings == "Courier pickup moved."
    assert review.corrective_action_reference == "CA-TEST-0042"  # kept as it was


def test_a_module_switched_off_after_the_form_opened_does_not_clear_the_stored_reference(
    client, module_on, agent, decided
):
    client.force_login(agent)
    LabModule.objects.filter(key="nonconformance").update(is_enabled=False)
    # Drawn with the module on (no reference field), saved with it off.
    response = post_record(client, decided, "nonconformance")
    assert response.status_code == 200
    page = response.content.decode()
    assert "switched on or off after you opened this form" in page
    assert 'name="corrective_action_reference"' in page  # shown again in the current shape
    assert 'name="form_shape" value="reference"' in page
    assert "Courier pickup moved." in page  # the other answers are kept
    review = ComplaintReview.objects.get(request=decided)
    assert review.corrective_action_reference == "CA-TEST-0042"
    assert review.findings == ""
    assert fresh(decided).revision == decided.revision


def test_a_module_switched_on_after_the_form_opened_does_not_drop_a_typed_reference(
    client, agent, decided
):
    client.force_login(agent)
    LabModule.objects.create(key="nonconformance", is_enabled=True, revision=1)
    response = post_record(client, decided, "reference", corrective_action_reference="CA-TEST-0099")
    assert response.status_code == 200
    assert "switched on or off after you opened this form" in response.content.decode()
    review = ComplaintReview.objects.get(request=decided)
    assert review.corrective_action_reference == "CA-TEST-0042" and review.findings == ""


def test_with_the_module_off_the_record_form_still_saves_a_reference(client, agent, decided):
    client.force_login(agent)
    response = post_record(client, decided, "reference", corrective_action_reference="CA-TEST-0100")
    assert response.status_code == 302
    assert ComplaintReview.objects.get(request=decided).corrective_action_reference == (
        "CA-TEST-0100"
    )


def test_the_complaint_review_shows_the_record_opened_from_the_complaint(
    client, module_on, agent, lead2, decided
):
    nc = services.open_from_complaint(
        actor=agent,
        request=decided,
        expected_revision=decided.revision,
        title="Late results",
        description="Late three times.",
        severity="major",
    )
    client.force_login(lead2)
    page = client.get(reverse("desk:complaint-review", args=[decided.pk])).content.decode()
    assert nc.number in page and "Reference recorded earlier" in page
    LabModule.objects.filter(key="nonconformance").update(is_enabled=False)
    page = client.get(reverse("desk:complaint-review", args=[decided.pk])).content.decode()
    main = page[page.index("<main") : page.index("</main>")]
    assert nc.number not in main and "CA-TEST-0042" in main


class TestSummary:
    def test_staff_read_it_with_the_module_on(self, client, module_on, tech):
        client.force_login(tech)
        response = client.get(reverse("nonconformance:summary"))
        assert response.status_code == 200
        assert "By month and source" in response.content.decode()

    @pytest.mark.parametrize("who", ["no_role", "portal"])
    def test_accounts_without_a_staff_role_are_refused(self, client, module_on, request, who):
        client.force_login(request.getfixturevalue(who))
        assert client.get(reverse("nonconformance:summary")).status_code == 403

    def test_it_does_not_exist_while_the_module_is_off(self, client, tech):
        client.force_login(tech)
        assert client.get(reverse("nonconformance:summary")).status_code == 404

    def test_anonymous_visitors_sign_in_first(self, client, module_on):
        response = client.get(reverse("nonconformance:summary"))
        assert response.status_code == 302 and "login" in response["Location"]


def test_a_cleared_effectiveness_check_reads_not_recorded_in_the_history(
    client, module_on, agent, lead
):
    nc = services.raise_nonconformance(
        actor=lead,
        title="Synthetic",
        description="Synthetic.",
        source="internal",
        severity="minor",
        owner=agent,
    )
    nc = services.change_status(
        actor=agent, nc=nc, expected_revision=nc.revision, status="verification"
    )
    nc = services.record_effectiveness(
        actor=agent, nc=nc, expected_revision=nc.revision, result="no_action_needed", note="n/a"
    )
    services.change_status(
        actor=agent, nc=nc, expected_revision=nc.revision, status="investigating"
    )
    client.force_login(agent)
    page = client.get(reverse("nonconformance:detail", args=[nc.pk])).content.decode()
    assert (
        '<strong>Effectiveness:</strong> <del class="vr-prior">No corrective action needed</del>'
        ' <span aria-hidden="true">→</span><span class="visually-hidden">changed to</span>'
        " Not recorded"
    ) in page
