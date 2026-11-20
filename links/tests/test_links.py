"""External systems and non-person links (L1, L2, L4, L5)."""

import pytest
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.template import Context, Template
from django.urls import reverse

from core.models import AuditEvent
from desk.models import RequestEntry
from links.models import ExternalRecordLink, ExternalSystem, LinkAttachment
from links.services import (
    add_link,
    create_system,
    remove_link,
    request_ids_for_identifier,
    set_shown_to_client,
    update_system,
)

from .conftest import PASSWORD


def render_panel(service_request, user):
    return Template("{% load record_links %}{% request_links r u %}").render(
        Context({"r": service_request, "u": user})
    )


# L1 ----------------------------------------------------------------------------------------


def test_starter_system_exists_after_migrate(db):
    system = ExternalSystem.objects.get(key="lis")
    assert system.name == "Laboratory information system" and system.is_active


@pytest.mark.parametrize(
    "base_url", ["http://lis.example.invalid/", "ftp://x.invalid", "lis.example.invalid"]
)
def test_base_url_is_https_only(admin, base_url):
    with pytest.raises(ValidationError):
        create_system(actor=admin, key="other", name="Other", kind="other", base_url=base_url)


def test_systems_are_admin_only_and_key_never_changes(admin, agent, lis):
    with pytest.raises(PermissionDenied):
        create_system(actor=agent, key="x", name="X", kind="other")
    with pytest.raises(ValidationError, match="key never changes"):
        update_system(
            actor=admin, system_id=lis.pk, expected_revision=lis.revision, reason="r", key="new"
        )
    update_system(
        actor=admin,
        system_id=lis.pk,
        expected_revision=lis.revision,
        reason="Our LIS",
        name="Example LIS",
    )
    assert AuditEvent.objects.filter(action="external_system.updated", reason="Our LIS").exists()


def test_inactive_system_takes_no_new_links(admin, agent, lis, service_request):
    update_system(
        actor=admin,
        system_id=lis.pk,
        expected_revision=lis.revision,
        reason="Retired",
        is_active=False,
    )
    lis.refresh_from_db()
    with pytest.raises(ValidationError, match="inactive"):
        add_link(
            actor=agent,
            request_id=service_request.pk,
            system=lis,
            record_type="order",
            identifier="ORD-TEST-1",
        )


# L2 ----------------------------------------------------------------------------------------


def test_natural_key_is_reused_across_requests(agent, lis, make_request):
    first, second = make_request(), make_request()
    a1, created1 = add_link(
        actor=agent,
        request_id=first.pk,
        system=lis,
        record_type="order",
        identifier=" ORD-TEST-000042 ",
    )
    a2, created2 = add_link(
        actor=agent,
        request_id=second.pk,
        system=lis,
        record_type="order",
        identifier="ORD-TEST-000042",
        display_label="A different label",
    )
    assert created1 and not created2
    assert a1.link_id == a2.link_id
    link = ExternalRecordLink.objects.get()
    assert link.identifier == "ORD-TEST-000042" and link.display_label == "ORD-TEST-000042"
    # A different scheme or type is another record.
    a3, created3 = add_link(
        actor=agent,
        request_id=first.pk,
        system=lis,
        record_type="accession",
        identifier="ORD-TEST-000042",
    )
    assert created3 and a3.link_id != a1.link_id


def test_same_link_twice_in_one_role_is_refused(agent, lis, service_request):
    add_link(
        actor=agent,
        request_id=service_request.pk,
        system=lis,
        record_type="order",
        identifier="ORD-TEST-1",
    )
    with pytest.raises(ValidationError, match="already carries"):
        add_link(
            actor=agent,
            request_id=service_request.pk,
            system=lis,
            record_type="order",
            identifier="ORD-TEST-1",
        )
    add_link(
        actor=agent,
        request_id=service_request.pk,
        system=lis,
        record_type="order",
        identifier="ORD-TEST-1",
        role="original",
    )


def test_url_must_be_https_on_the_system_host(agent, lims, service_request):
    with pytest.raises(ValidationError) as error:
        add_link(
            actor=agent,
            request_id=service_request.pk,
            system=lims,
            record_type="sample",
            identifier="SMP-TEST-1",
            url="https://elsewhere.example.invalid/SMP-TEST-1",
        )
    assert "url" in error.value.message_dict
    attachment, _ = add_link(
        actor=agent,
        request_id=service_request.pk,
        system=lims,
        record_type="sample",
        identifier="SMP-TEST-1",
        url="https://lims.example.invalid/app/samples/SMP-TEST-1",
    )
    assert attachment.link.url.startswith("https://lims.example.invalid/")


def test_removal_keeps_history(agent, lis, service_request):
    attachment, _ = add_link(
        actor=agent,
        request_id=service_request.pk,
        system=lis,
        record_type="report",
        identifier="RPT-TEST-000310",
    )
    with pytest.raises(ValidationError):
        remove_link(actor=agent, attachment_id=attachment.pk, reason=" ")
    remove_link(actor=agent, attachment_id=attachment.pk, reason="Linked to the wrong request")
    attachment.refresh_from_db()
    assert attachment.removed_at and attachment.removed_by == agent
    assert LinkAttachment.objects.filter(pk=attachment.pk).exists()
    with pytest.raises(ValidationError, match="already removed"):
        remove_link(actor=agent, attachment_id=attachment.pk, reason="Again")
    event = AuditEvent.objects.get(action="external_link.detached")
    assert event.reason == "Linked to the wrong request"
    actions = list(
        RequestEntry.objects.filter(request=service_request, kind="event").values_list(
            "event_action", flat=True
        )
    )
    assert "link.attached" in actions and "link.removed" in actions
    html = render_panel(service_request, agent)
    assert "Removed links (1)" in html and "Linked to the wrong request" in html
    # The link can be attached again in the same role after removal.
    add_link(
        actor=agent,
        request_id=service_request.pk,
        system=lis,
        record_type="report",
        identifier="RPT-TEST-000310",
    )


def test_show_to_client(agent, lis, service_request):
    attachment, _ = add_link(
        actor=agent,
        request_id=service_request.pk,
        system=lis,
        record_type="order",
        identifier="ORD-TEST-1",
        shown_to_client=True,
    )
    assert attachment.shown_to_client
    from links.access import client_shown_links

    assert [s.label for s in client_shown_links(service_request)] == ["ORD-TEST-1"]
    set_shown_to_client(actor=agent, attachment_id=attachment.pk, shown=False)
    assert client_shown_links(service_request) == []
    assert AuditEvent.objects.filter(action="external_link.attachment_updated").exists()


def test_organization_links(agent, lis, client_org, client):
    attachment, _ = add_link(
        actor=agent,
        client_id=client_org.pk,
        system=lis,
        record_type="project",
        identifier="PROJ-TEST-7",
    )
    assert attachment.client == client_org and attachment.request is None
    assert not attachment.shown_to_client
    with pytest.raises(PermissionDenied):
        add_link(
            actor=agent,
            client_id=client_org.pk,
            system=lis,
            record_type="patient",
            identifier="PT-TEST-1",
        )
    client.login(username=agent.username, password=PASSWORD)
    response = client.post(
        reverse("link_records:client-link-add", args=[client_org.pk]),
        {
            "system": lis.pk,
            "record_type": "study",
            "identifier": "STUDY-TEST-12",
            "role": "related",
        },
    )
    assert response.status_code == 302
    response = client.post(
        reverse("link_records:attachment-remove", args=[attachment.pk]),
        {"reason": "Closed project"},
    )
    assert response.status_code == 302
    assert LinkAttachment.objects.filter(client=client_org, removed_at__isnull=True).count() == 1


def test_link_page_lists_requests_and_history(agent, lis, make_request, client):
    first, second = make_request(), make_request()
    for request in (first, second):
        add_link(
            actor=agent,
            request_id=request.pk,
            system=lis,
            record_type="order",
            identifier="ORD-TEST-9",
        )
    link = ExternalRecordLink.objects.get()
    client.login(username=agent.username, password=PASSWORD)
    page = client.get(reverse("link_records:link-detail", args=[link.pk])).content.decode()
    assert first.number in page and second.number in page and "ORD-TEST-9" in page


# L4 ----------------------------------------------------------------------------------------


def test_client_reference_prefills_and_stays(agent, lis, make_request, client):
    service_request = make_request(client_reference="COC-TEST-000021")
    html = render_panel(service_request, agent)
    assert "COC-TEST-000021" in html and "Make a link" in html
    client.login(username=agent.username, password=PASSWORD)
    url = reverse("link_records:request-link-add", args=[service_request.pk])
    form_page = client.get(url + "?from=reference").content.decode()
    assert 'value="COC-TEST-000021"' in form_page
    service_request.refresh_from_db()
    assert f'name="expected_revision" value="{service_request.revision}"' in form_page
    client.post(
        url,
        {
            "system": lis.pk,
            "record_type": "custody_form",
            "identifier": "COC-TEST-000021",
            "role": "primary",
            "expected_revision": service_request.revision,  # the page's (a link page carries it)
        },
    )
    service_request.refresh_from_db()
    assert service_request.client_reference == "COC-TEST-000021"
    assert "Make a link" not in render_panel(service_request, agent)


# L5 ----------------------------------------------------------------------------------------


def test_identifier_search_is_case_insensitive_and_skips_removed(agent, lis, make_request):
    first, second, third = make_request(), make_request(), make_request()
    add_link(
        actor=agent,
        request_id=first.pk,
        system=lis,
        record_type="sample",
        identifier="SMP-TEST-ab12",
    )
    add_link(
        actor=agent,
        request_id=second.pk,
        system=lis,
        record_type="order",
        identifier="smp-test-AB12",
    )
    removed, _ = add_link(
        actor=agent,
        request_id=third.pk,
        system=lis,
        record_type="sample",
        identifier="SMP-TEST-AB12",
    )
    remove_link(actor=agent, attachment_id=removed.pk, reason="Wrong request")
    assert sorted(request_ids_for_identifier(agent, "  SMP-test-AB12 ")) == sorted(
        [first.pk, second.pk]
    )
    assert request_ids_for_identifier(agent, "SMP-TEST") == []  # whole identifier, not a prefix
    assert request_ids_for_identifier(agent, "") == []


def test_identifier_search_honours_restricted_categories(
    agent, quality_member, lis, restricted, make_request
):
    hidden = make_request(category_=restricted)
    add_link(
        actor=quality_member,
        request_id=hidden.pk,
        system=lis,
        record_type="order",
        identifier="ORD-TEST-77",
    )
    assert request_ids_for_identifier(quality_member, "ORD-TEST-77") == [hidden.pk]
    assert request_ids_for_identifier(agent, "ORD-TEST-77") == []
    with pytest.raises(ObjectDoesNotExist):
        add_link(
            actor=agent, request_id=hidden.pk, system=lis, record_type="order", identifier="X-1"
        )


def test_identifier_search_never_matches_person_keys(holder, lis, service_request):
    add_link(
        actor=holder,
        request_id=service_request.pk,
        system=lis,
        record_type="patient",
        identifier="PT-TEST-00009999",
    )
    assert request_ids_for_identifier(holder, "PT-TEST-00009999") == []


def test_link_pages_for_another_readers_restricted_request_are_404(
    agent, quality_member, lis, restricted, make_request, client
):
    hidden = make_request(category_=restricted)
    attachment, _ = add_link(
        actor=quality_member,
        request_id=hidden.pk,
        system=lis,
        record_type="order",
        identifier="ORD-TEST-78",
    )
    client.login(username=agent.username, password=PASSWORD)
    for url in (
        reverse("link_records:request-link-add", args=[hidden.pk]),
        reverse("link_records:attachment-remove", args=[attachment.pk]),
    ):
        assert client.get(url).status_code == 404
    assert (
        client.post(
            reverse("link_records:attachment-shown", args=[attachment.pk]), {"shown": "1"}
        ).status_code
        == 404
    )
    page = client.get(
        reverse("link_records:link-detail", args=[attachment.link_id])
    ).content.decode()
    assert hidden.number not in page


def test_portal_account_is_refused(make_user, client_org, service_request, client):
    portal = make_user("portal", client=client_org)
    client.login(username=portal.username, password=PASSWORD)
    assert (
        client.get(reverse("link_records:request-link-add", args=[service_request.pk])).status_code
        == 403
    )


def test_seed_brings_back_the_starter_system_after_a_reset(make_user):
    """reset_demo wipes rows data migrations made; the seed recreates the starter system."""
    from core.seeding import SeedContext
    from links.seed import seed_setup

    make_user("demo_admin", "admin")
    LinkAttachment.objects.all().delete()
    ExternalRecordLink.objects.all().delete()
    ExternalSystem.objects.all().delete()
    seed_setup(SeedContext(password="unused"))
    seed_setup(SeedContext(password="unused"))  # idempotent
    assert ExternalSystem.objects.get(key="lis").name == "Example LIS"
    assert ExternalSystem.objects.count() == 3
