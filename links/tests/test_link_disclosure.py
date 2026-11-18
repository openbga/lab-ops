"""The information each reader can access about external record links."""

import json

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from core.models import AuditEvent
from desk.models import RequestEntry
from desk.selectors import staff_requests
from links.forms import LinkEditForm, LinkForm
from links.models import ExternalRecordLink, LinkAttachment
from links.services import add_link, remove_link, update_link, update_system
from teams.models import TeamMembership

from .conftest import PATIENT_KEY
from .test_person_links import panel

DONOR_KEY = "SIM-DONOR-11223344"


# history on a shared link page


def test_shared_link_page_shows_only_readable_carriers_and_their_history(
    client, agent, lis, make_request, quality_member, restricted
):
    visible = make_request()
    hidden_request = make_request(category_=restricted)
    assert not staff_requests(agent).filter(pk=hidden_request.pk).exists()
    data = {"system": lis, "record_type": "order", "identifier": "REVIEW-SHARED"}
    shared, _ = add_link(actor=agent, request_id=visible.pk, **data)
    hidden, _ = add_link(actor=quality_member, request_id=hidden_request.pk, **data)
    reason = "Synthetic quality investigation at restricted intake"
    remove_link(actor=quality_member, attachment_id=hidden.pk, reason=reason)
    client.force_login(agent)
    page = client.get(reverse("link_records:link-detail", args=[shared.link_id]))
    assert [a.pk for a in page.context["attachments"]] == [shared.pk]
    assert reason.encode() not in page.content
    assert hidden_request.number.encode() not in page.content
    history_objects = {(e.object_type, e.object_id) for e in page.context["history"]}
    assert ("links.linkattachment", str(hidden.pk)) not in history_objects
    # The team member sees both carriers and both histories.
    client.force_login(quality_member)
    page = client.get(reverse("link_records:link-detail", args=[shared.link_id]))
    assert len(page.context["attachments"]) == 2 and reason.encode() in page.content


# person link page needs a readable carrier now


@pytest.fixture
def donor(quality_member, lims, restricted, make_request):
    record = make_request(category_=restricted)
    attachment, _ = add_link(
        actor=quality_member,
        request_id=record.pk,
        system=lims,
        record_type="donor",
        identifier=DONOR_KEY,
    )
    return attachment


def test_person_link_page_closes_after_leaving_the_team(client, quality_member, quality, donor):
    detail = reverse("link_records:link-detail", args=[donor.link_id])
    edit = reverse("link_records:link-edit", args=[donor.link_id])
    client.force_login(quality_member)
    assert client.get(detail).status_code == 200
    TeamMembership.objects.filter(user=quality_member, team=quality).delete()
    for url in (detail, edit):
        response = client.get(url)
        assert response.status_code == 404
        assert donor.link.display_label.encode() not in response.content
    with pytest.raises(ExternalRecordLink.DoesNotExist):
        update_link(
            actor=quality_member,
            link_id=donor.link_id,
            expected_revision=1,
            display_label="Donor alias Heron",
            reason_code="entered_in_error",
        )


def test_capability_alone_opens_no_person_link(client, holder, donor):
    # holder has the capability but cannot read the restricted carrier request.
    response = client.get(reverse("link_records:link-detail", args=[donor.link_id]))
    assert response.status_code == 302  # signed out: sign-in first
    client.force_login(holder)
    assert client.get(reverse("link_records:link-detail", args=[donor.link_id])).status_code == 404


@pytest.mark.parametrize("role", ["admin", "supervisor"])
def test_roles_without_the_capability_read_no_person_link(client, role, make_user, person):
    from django.contrib import admin as django_admin

    reader = make_user("review_" + role, role, is_superuser=True, is_staff=True)
    assert not django_admin.site.is_registered(ExternalRecordLink)
    assert not django_admin.site.is_registered(LinkAttachment)
    client.force_login(reader)
    detail = client.get(reverse("link_records:link-detail", args=[person.link_id]))
    assert detail.status_code == 404
    assert "Patient linked" in panel(person.request, reader)
    assert PATIENT_KEY not in panel(person.request, reader)


@pytest.mark.parametrize("headers", [{}, {"X-VR-Form": "1"}], ids=["page", "dialog"])
def test_person_edit_is_not_a_key_reveal(client, holder, person, headers):
    client.force_login(holder)
    response = client.get(reverse("link_records:link-edit", args=[person.link_id]), headers=headers)
    assert response.status_code == 200
    assert b'name="display_label"' in response.content  # the visible fields render
    assert b'name="url"' not in response.content
    assert not AuditEvent.objects.filter(action="external_link.person_revealed").exists()
    assert PATIENT_KEY.encode() not in response.content


# a removed person row without the capability


def test_removed_person_placeholder_is_the_whole_row(agent, holder, service_request, person):
    note = "Synthetic: matched the wrong clinic patient " + PATIENT_KEY
    remove_link(actor=holder, attachment_id=person.pk, reason_code="wrong_person", note=note)
    html = panel(service_request, agent)
    assert "Removed links (1)" in html and "Patient linked" in html
    for private in (note, "Wrong person", holder.get_username(), "removed by"):
        assert private not in html
    holder_html = panel(service_request, holder)
    assert "Wrong person" in holder_html and note in holder_html


# no key in removal or change reasons


def test_person_reasons_are_codes_and_the_note_stays_on_the_row(holder, person):
    with pytest.raises(ValidationError):
        remove_link(actor=holder, attachment_id=person.pk, reason="Correcting " + PATIENT_KEY)
    assert not AuditEvent.objects.filter(action="external_link.detached").exists()
    note = "Correcting source key " + PATIENT_KEY
    remove_link(actor=holder, attachment_id=person.pk, reason_code="entered_in_error", note=note)
    event = AuditEvent.objects.get(action="external_link.detached")
    assert event.reason == "entered_in_error"
    assert PATIENT_KEY not in json.dumps([event.before, event.after, event.reason])
    timeline = RequestEntry.objects.get(event_action="link.removed")
    assert timeline.event_data["reason_code"] == "entered_in_error"
    assert PATIENT_KEY not in json.dumps(timeline.event_data)
    person.refresh_from_db()
    assert person.removal_note == note and person.removal_reason == "Entered in error"


def test_person_link_change_reason_is_a_code(holder, person):
    update_link(
        actor=holder,
        link_id=person.link_id,
        expected_revision=1,
        display_label="Patient alias Heron",
        reason="Wrong key " + PATIENT_KEY,  # ignored: person links take a code
        reason_code="entered_in_error",
    )
    event = AuditEvent.objects.get(action="external_link.updated")
    assert event.reason == "entered_in_error" and PATIENT_KEY not in event.reason
    with pytest.raises(ValidationError):
        update_link(
            actor=holder,
            link_id=person.link_id,
            expected_revision=2,
            display_label="Patient alias Wren",
            reason="Free text only",
        )


def test_ordinary_links_keep_free_text_reasons(agent, lis, service_request):
    attachment, _ = add_link(
        actor=agent,
        request_id=service_request.pk,
        system=lis,
        record_type="order",
        identifier="O-1",
    )
    remove_link(actor=agent, attachment_id=attachment.pk, reason="Linked to the wrong request")
    assert AuditEvent.objects.get(action="external_link.detached").reason == (
        "Linked to the wrong request"
    )


# no web address on a person link


def test_person_link_has_no_address(holder, lims, service_request, person, client):
    with pytest.raises(ValidationError) as error:
        add_link(
            actor=holder,
            request_id=service_request.pk,
            system=lims,
            record_type="participant",
            identifier="PARTICIPANT-TEST-0007",
            url="https://lims.example.invalid/app/participants/PARTICIPANT-TEST-0007",
        )
    assert "url" in error.value.message_dict
    with pytest.raises(IntegrityError), transaction.atomic():
        ExternalRecordLink.objects.filter(pk=person.link_id).update(
            url=f"https://lis.example.invalid/patients/{PATIENT_KEY}"
        )
    form = LinkForm(
        {
            "system": lims.pk,
            "record_type": "patient",
            "identifier": PATIENT_KEY,
            "role": "related",
            "url": "https://lims.example.invalid/app/x",
        },
        allow_person=True,
    )
    assert not form.is_valid() and "url" in form.errors
    assert "url" not in LinkEditForm(instance=person.link).fields
    client.force_login(holder)
    fragment = client.post(
        reverse("link_records:attachment-reveal", args=[person.pk]), headers={"HX-Request": "true"}
    )
    assert PATIENT_KEY.encode() in fragment.content and b"<a " not in fragment.content


# no reveal after removal


def test_removed_person_link_cannot_be_revealed(client, holder, person):
    remove_link(actor=holder, attachment_id=person.pk, reason_code="wrong_person")
    client.force_login(holder)
    response = client.post(
        reverse("link_records:attachment-reveal", args=[person.pk]), headers={"HX-Request": "true"}
    )
    assert response.status_code == 404
    assert PATIENT_KEY.encode() not in response.content
    assert not AuditEvent.objects.filter(action="external_link.person_revealed").exists()


# addresses only on the system's configured host


def test_address_needs_a_system_base_address(agent, lis, service_request):
    assert not lis.base_url  # starter system
    with pytest.raises(ValidationError, match="no base address"):
        add_link(
            actor=agent,
            system=lis,
            request_id=service_request.pk,
            record_type="order",
            identifier="TEST-ORDER",
            url="https://unconfigured.example.invalid/record",
        )
    assert not ExternalRecordLink.objects.exclude(url="").exists()


def test_address_off_the_current_host_is_not_a_link(client, admin, agent, lims, client_org):
    old_url = "https://lims.example.invalid/app/record"
    attachment, _ = add_link(
        actor=agent,
        system=lims,
        client_id=client_org.pk,
        record_type="project",
        identifier="TEST-PROJECT",
        url=old_url,
    )
    client.force_login(agent)
    page = client.get(reverse("clients:client-detail", args=[client_org.pk])).content.decode()
    assert f'href="{old_url}"' in page
    update_system(
        actor=admin,
        system_id=lims.pk,
        expected_revision=lims.revision,
        reason="Move to the replacement host",
        base_url="https://new.example.invalid/",
    )
    page = client.get(reverse("clients:client-detail", args=[client_org.pk]))
    assert old_url.encode() not in page.content
    assert b"address not on the system" in page.content
    detail = client.get(reverse("link_records:link-detail", args=[attachment.link_id]))
    assert f'href="{old_url}"'.encode() not in detail.content
    assert b"not on the system" in detail.content


# the searched key is never written back


@pytest.mark.parametrize("key", [PATIENT_KEY + "​", PATIENT_KEY, "x" * 201])
def test_person_search_never_reflects_the_key(client, holder, key):
    client.force_login(holder)
    result = client.post(reverse("link_records:person-search"), {"key": key})
    assert "no-store" in result["Cache-Control"]
    assert key.encode() not in result.content
    assert PATIENT_KEY.encode() not in result.content
