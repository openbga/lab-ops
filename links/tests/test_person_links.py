"""Person-level links (L3): capability on every path, masked labels, one per request, audited
reveal and search, and the key kept out of URLs, titles, list HTML, logs and audit payloads."""

import json
import logging

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.template import Context, Template
from django.urls import reverse

from core.models import AuditEvent
from desk.models import RequestEntry
from links.masking import key_digest
from links.models import ExternalRecordLink, LinkAttachment
from links.services import add_link, remove_link, reveal_person_key, search_person_key

from .conftest import PASSWORD, PATIENT_KEY

LABEL = "Patient ····4821"


def panel(service_request, user):
    return Template("{% load record_links %}{% request_links r u %}").render(
        Context({"r": service_request, "u": user})
    )


# Capability on every path ------------------------------------------------------------------


def test_add_needs_the_capability_and_no_role_implies_it(agent, admin, lis, service_request):
    for user in (agent, admin):
        with pytest.raises(PermissionDenied):
            add_link(
                actor=user,
                request_id=service_request.pk,
                system=lis,
                record_type="patient",
                identifier=PATIENT_KEY,
            )
    assert not ExternalRecordLink.objects.exists()


def test_add_form_offers_person_types_only_to_holders(agent, holder, service_request, client):
    url = reverse("link_records:request-link-add", args=[service_request.pk])
    client.login(username=agent.username, password=PASSWORD)
    assert 'value="patient"' not in client.get(url).content.decode()
    response = client.post(url, {"system": 1, "record_type": "patient", "identifier": PATIENT_KEY})
    assert response.status_code == 200 and not ExternalRecordLink.objects.exists()
    client.login(username=holder.username, password=PASSWORD)
    page = client.get(url)
    assert 'value="patient"' in page.content.decode()
    assert "no-store" in page["Cache-Control"]


def test_masked_label_proposed_and_stored(person):
    assert person.link.display_label == LABEL
    assert person.is_person and person.link.is_person


@pytest.mark.parametrize(
    "label", [f"Patient {PATIENT_KEY}", "Jane Doe jane.doe@example.invalid", "Patient 000048210"]
)
def test_refused_labels(holder, lis, service_request, label):
    with pytest.raises(ValidationError) as error:
        add_link(
            actor=holder,
            request_id=service_request.pk,
            system=lis,
            record_type="patient",
            identifier=PATIENT_KEY,
            display_label=label,
        )
    assert "display_label" in error.value.message_dict
    assert PATIENT_KEY not in str(error.value)


def test_short_key_needs_an_alias(holder, lis, service_request):
    with pytest.raises(ValidationError, match="alias"):
        add_link(
            actor=holder,
            request_id=service_request.pk,
            system=lis,
            record_type="donor",
            identifier="D-77",
        )
    attachment, _ = add_link(
        actor=holder,
        request_id=service_request.pk,
        system=lis,
        record_type="donor",
        identifier="D-77",
        display_label="Donor alias Kestrel",
    )
    assert attachment.link.display_label == "Donor alias Kestrel"


def test_person_link_never_shown_to_client(holder, lis, service_request):
    with pytest.raises(ValidationError) as error:
        add_link(
            actor=holder,
            request_id=service_request.pk,
            system=lis,
            record_type="participant",
            identifier="PARTICIPANT-TEST-0007",
            shown_to_client=True,
        )
    assert "shown_to_client" in error.value.message_dict
    with pytest.raises(IntegrityError), transaction.atomic():
        link = ExternalRecordLink.objects.create(
            system=lis,
            record_type="patient",
            identifier="PT-TEST-1",
            display_label="x",
            is_person=True,
            created_by=holder,
        )
        LinkAttachment.objects.create(
            link=link,
            request=service_request,
            is_person=True,
            shown_to_client=True,
            added_by=holder,
        )


def test_one_person_link_per_request(holder, lis, service_request, person):
    with pytest.raises(ValidationError, match="already has a person-level link"):
        add_link(
            actor=holder,
            request_id=service_request.pk,
            system=lis,
            record_type="patient",
            identifier="PT-TEST-00001111",
        )
    other = ExternalRecordLink.objects.create(
        system=lis,
        record_type="patient",
        identifier="PT-TEST-2",
        display_label="Patient alias",
        is_person=True,
        created_by=holder,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        LinkAttachment.objects.create(
            link=other, request=service_request, is_person=True, added_by=holder
        )
    # After removal another may be added.
    remove_link(actor=holder, attachment_id=person.pk, reason_code="wrong_person")
    add_link(
        actor=holder,
        request_id=service_request.pk,
        system=lis,
        record_type="patient",
        identifier="PT-TEST-00001111",
    )


def test_is_person_follows_the_record_type_in_the_database(holder, lis):
    with pytest.raises(IntegrityError), transaction.atomic():
        ExternalRecordLink.objects.create(
            system=lis,
            record_type="order",
            identifier="ORD-1",
            display_label="x",
            is_person=True,
            created_by=holder,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        ExternalRecordLink.objects.create(
            system=lis,
            record_type="patient",
            identifier="PT-1",
            display_label="x",
            is_person=False,
            created_by=holder,
        )


def test_reader_without_capability_sees_a_placeholder_only(agent, holder, service_request, person):
    html = panel(service_request, agent)
    assert "Patient linked" in html
    for secret in (
        PATIENT_KEY,
        "4821",
        LABEL,
        str(person.link_id),
        "Reveal key",
        "lis.example.invalid",
    ):
        assert secret not in html
    holder_html = panel(service_request, holder)
    assert LABEL in holder_html and "Reveal key" in holder_html
    assert PATIENT_KEY not in holder_html  # the key comes only from the reveal


def test_detach_needs_the_capability(agent, holder, person, client):
    with pytest.raises(PermissionDenied):
        remove_link(actor=agent, attachment_id=person.pk, reason_code="other")
    client.login(username=agent.username, password=PASSWORD)
    url = reverse("link_records:attachment-remove", args=[person.pk])
    assert client.get(url).status_code == 404
    assert client.post(url, {"reason_code": "other"}).status_code == 404
    remove_link(actor=holder, attachment_id=person.pk, reason_code="wrong_person")
    event = AuditEvent.objects.get(action="external_link.detached")
    assert PATIENT_KEY not in json.dumps([event.before, event.after])


def test_link_page_of_a_person_link(agent, holder, person, client):
    url = reverse("link_records:link-detail", args=[person.link_id])
    client.login(username=agent.username, password=PASSWORD)
    assert client.get(url).status_code == 404
    assert client.get(reverse("link_records:link-edit", args=[person.link_id])).status_code == 404
    client.login(username=holder.username, password=PASSWORD)
    response = client.get(url)
    html = response.content.decode()
    title = html.split("<title>")[1].split("</title>")[0]
    assert "Patient link" in title and "4821" not in title
    assert PATIENT_KEY not in html
    assert "no-store" in response["Cache-Control"]


# Reveal ------------------------------------------------------------------------------------


def test_reveal_is_post_only_audited_and_not_cached(agent, holder, person, service_request, client):
    url = reverse("link_records:attachment-reveal", args=[person.pk])
    assert PATIENT_KEY not in url
    client.login(username=agent.username, password=PASSWORD)
    assert client.post(url).status_code == 404
    with pytest.raises(PermissionDenied):
        reveal_person_key(actor=agent, attachment_id=person.pk)
    client.login(username=holder.username, password=PASSWORD)
    assert client.get(url).status_code == 405
    response = client.post(url, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    assert PATIENT_KEY in response.content.decode()
    assert "no-store" in response["Cache-Control"]
    event = AuditEvent.objects.get(action="external_link.person_revealed")
    assert event.actor == holder and event.object_id == str(person.link_id)
    assert event.after == {"request": service_request.pk, "attachment": person.pk}
    assert PATIENT_KEY not in json.dumps([event.before, event.after, event.reason])
    page = client.post(url)  # without JavaScript: a page, the key not in its title
    title = page.content.decode().split("<title>")[1].split("</title>")[0]
    assert PATIENT_KEY not in title


def test_reveal_on_a_restricted_request_needs_read_access(
    holder, quality_member, lis, restricted, make_request, client
):
    hidden = make_request(category_=restricted)
    attachment, _ = add_link(
        actor=quality_member,
        request_id=hidden.pk,
        system=lis,
        record_type="patient",
        identifier=PATIENT_KEY,
    )
    client.login(username=holder.username, password=PASSWORD)
    assert (
        client.post(reverse("link_records:attachment-reveal", args=[attachment.pk])).status_code
        == 404
    )
    assert not AuditEvent.objects.filter(action="external_link.person_revealed").exists()


# Search ------------------------------------------------------------------------------------


def test_person_search_is_exact_post_only_and_audited(
    agent, holder, person, service_request, client, settings
):
    url = reverse("link_records:person-search")
    client.login(username=agent.username, password=PASSWORD)
    page = client.get(url)
    assert page.status_code == 200 and "Permission needed" in page.content.decode()
    assert 'name="key"' not in page.content.decode()
    assert client.post(url, {"key": PATIENT_KEY}).status_code == 403
    with pytest.raises(PermissionDenied):
        search_person_key(actor=agent, key=PATIENT_KEY)
    client.login(username=holder.username, password=PASSWORD)
    get = client.get(url, {"key": PATIENT_KEY})  # a GET never searches
    assert service_request.number not in get.content.decode()
    assert not AuditEvent.objects.filter(action="external_link.person_searched").exists()
    response = client.post(url, {"key": PATIENT_KEY})
    html = response.content.decode()
    assert service_request.number in html
    assert PATIENT_KEY not in html  # not echoed back into the form or the results
    assert "no-store" in response["Cache-Control"]
    event = AuditEvent.objects.get(action="external_link.person_searched")
    assert event.object_type == "accounts.user" and event.object_id == str(holder.pk)
    assert event.after["key_digest"] == key_digest(PATIENT_KEY, settings.SECRET_KEY)
    assert event.after["links"] == [str(person.link_id)]
    assert PATIENT_KEY not in json.dumps(event.after)
    assert search_person_key(actor=holder, key=PATIENT_KEY.lower()) == []
    assert search_person_key(actor=holder, key=PATIENT_KEY[:-1]) == []


def test_search_ignores_removed_links_and_unreadable_requests(
    holder, quality_member, lis, restricted, make_request, person
):
    hidden = make_request(category_=restricted)
    add_link(
        actor=quality_member,
        request_id=hidden.pk,
        system=lis,
        record_type="patient",
        identifier=PATIENT_KEY,
    )
    assert search_person_key(actor=holder, key=PATIENT_KEY) == [person.request_id]
    assert sorted(search_person_key(actor=quality_member, key=PATIENT_KEY)) == sorted(
        [person.request_id, hidden.pk]
    )
    remove_link(actor=holder, attachment_id=person.pk, reason_code="wrong_person")
    assert search_person_key(actor=holder, key=PATIENT_KEY) == []


# The key stays out of everything else ------------------------------------------------------


def test_key_absent_from_audit_timeline_urls_and_logs(holder, lis, service_request, client, caplog):
    caplog.set_level(logging.DEBUG)
    client.login(username=holder.username, password=PASSWORD)
    add_url = reverse("link_records:request-link-add", args=[service_request.pk])
    response = client.post(
        add_url,
        {
            "system": lis.pk,
            "record_type": "patient",
            "identifier": PATIENT_KEY,
            "role": "primary",
        },
    )
    assert response.status_code == 302
    assert PATIENT_KEY not in response["Location"]
    attachment = LinkAttachment.objects.get()
    client.post(
        reverse("link_records:link-edit", args=[attachment.link_id]),
        {
            "display_label": "Patient alias Heron",
            "reason_code": "entered_in_error",
            "expected_revision": attachment.link.revision,
        },
    )
    client.post(reverse("link_records:attachment-reveal", args=[attachment.pk]))
    client.post(reverse("link_records:person-search"), {"key": PATIENT_KEY})
    client.post(
        reverse("link_records:attachment-remove", args=[attachment.pk]),
        {"reason_code": "no_longer_needed", "note": "Duplicate of the clinic's copy"},
    )
    audit = json.dumps(
        list(
            AuditEvent.objects.values(
                "action", "object_type", "object_id", "before", "after", "reason"
            )
        )
    )
    assert "external_link.created" in audit and "external_link.updated" in audit
    assert PATIENT_KEY not in audit and LABEL not in audit and "Heron" not in audit
    timeline = json.dumps(list(RequestEntry.objects.values("body", "event_action", "event_data")))
    assert PATIENT_KEY not in timeline and "Heron" not in timeline
    assert PATIENT_KEY not in caplog.text
    assert str(attachment.link) == "Patient link"
