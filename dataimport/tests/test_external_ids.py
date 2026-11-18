"""External identifiers file: links on organizations through
links.services, all rows or none; person-level types never imported. Synthetic data only."""

import pytest
from django.core.exceptions import ValidationError

from clients.models import Client
from core.models import AuditEvent
from dataimport.importing import EXTERNAL_IDS, ORGANIZATIONS, commit_file, preview_file
from links import services as link_services
from links.models import ExternalRecordLink, ExternalSystem, LinkAttachment

from .test_import import ORGS, counts, run

pytestmark = pytest.mark.django_db

HEADER = "account_code,system,record_type,identifier_scheme,identifier,role,display_label,url\r\n"


@pytest.fixture
def loaded(manager):
    run(ORGANIZATIONS, ORGS, manager)
    return Client.objects.get(account_number="EXGEN")


def preview(manager, text):
    return preview_file(EXTERNAL_IDS, actor=manager, data=text.encode(), filename="ids.csv")


def test_identifiers_become_organization_links_and_reimport_changes_nothing(manager, loaded):
    text = HEADER + "EXGEN,LIS,other,client_code,TEST-CLIENT-0042,primary,LIS client 42,\r\n"
    run(EXTERNAL_IDS, text, manager)
    attachment = LinkAttachment.objects.get(client=loaded)
    link = attachment.link
    assert (link.system.key, link.record_type, link.identifier_scheme, link.identifier) == (
        "lis",
        "other",
        "client_code",
        "TEST-CLIENT-0042",
    )
    assert attachment.role == "primary" and link.display_label == "LIS client 42"
    assert not link.is_person and not attachment.shown_to_client
    assert AuditEvent.objects.filter(action="external_link.created").count() == 1
    assert AuditEvent.objects.filter(action="external_link.attached").count() == 1
    summary = AuditEvent.objects.filter(action="dataimport.file_imported").latest("occurred_at")
    assert summary.after["record_type"] == "external-ids"
    before = counts()
    done = run(EXTERNAL_IDS, text, manager)
    assert done.counts["unchanged"] == 1 and counts() == before


@pytest.mark.parametrize("record_type", ["patient", "participant", "donor", "Patient"])
def test_person_level_identifiers_are_never_imported(manager, loaded, record_type):
    result = preview(manager, HEADER + f"EXGEN,lis,{record_type},,PT-TEST-00004821,,,\r\n")
    assert not result.ok and "person-level" in result.rows[0].errors[0]
    assert "PT-TEST" not in result.rows[0].errors[0]  # the key is not echoed
    assert not ExternalRecordLink.objects.exists()


def test_unknown_inactive_system_and_bad_values_are_row_errors(manager, loaded):
    ExternalSystem.objects.create(key="old-billing", name="Example Old Billing", is_active=False)
    text = (
        HEADER
        + "EXGEN,nosuch,order,,ORD-TEST-1,,,\r\n"
        + "EXGEN,old-billing,invoice,,INV-TEST-1,,,\r\n"
        + "EXGEN,lis,spaceship,,X-1,,,\r\n"
        + "EXGEN,lis,order,,ORD-TEST-2,boss,,\r\n"
        + "EXGEN,lis,order,,ORD-TEST-3,,,http://lis.example.invalid/3\r\n"
        + "EXNONE,lis,order,,ORD-TEST-4,,,\r\n"
    )
    result = preview(manager, text)
    errors = [row.errors[0] for row in result.rows]
    assert errors[0].startswith("system: no external system")
    assert "inactive" in errors[1]
    assert errors[2].startswith("record_type:")
    assert errors[3].startswith("role:")
    assert errors[4].startswith("url:")  # the links service's own https rule
    assert "Import the organizations first" in errors[5]
    with pytest.raises(ValidationError):
        commit_file(
            EXTERNAL_IDS,
            actor=manager,
            data=text.encode(),
            filename="ids.csv",
            reason="",
            state_token=result.state_token,
        )
    assert not ExternalRecordLink.objects.exists()


def test_an_existing_link_is_attached_and_never_changed(manager, loaded):
    """An import never changes a shared external record."""
    other = Client.objects.get(account_number="EXHS")
    system = ExternalSystem.objects.get(key="lis")
    link_services.add_link(
        actor=manager,
        system=system,
        record_type="other",
        identifier="TEST-SHARED-7",
        client_id=other.pk,
        display_label="Shared code",
    )
    text = HEADER + "EXGEN,lis,other,,TEST-SHARED-7,,Shared LIS code,\r\n"
    result = preview(manager, text)
    row = result.rows[0]
    assert row.action == "create" and row.changes == []
    assert "Attaches the link already recorded" in row.notes[0]
    assert "keeps 'Shared code'" in row.not_applied[0]
    assert result.not_applied_count == 1
    run(EXTERNAL_IDS, text, manager)
    link = ExternalRecordLink.objects.get()
    assert link.display_label == "Shared code"  # unchanged
    assert set(link.attachments.values_list("client__account_number", flat=True)) == {
        "EXGEN",
        "EXHS",
    }
    assert not AuditEvent.objects.filter(action="external_link.updated").exists()
    # The same row again: the attachment exists, nothing changes, the difference is still shown.
    again = preview(manager, text)
    assert again.rows[0].action == "unchanged" and again.rows[0].not_applied
    before = counts()
    run(EXTERNAL_IDS, text, manager)
    assert counts() == before


def test_a_link_changed_after_the_preview_is_refused(manager, loaded):
    text = HEADER + "EXGEN,lis,other,,TEST-9,,First label,\r\n"
    run(EXTERNAL_IDS, text, manager)
    other = HEADER + "EXHS,lis,other,,TEST-9,,,\r\n"  # attaches the same link elsewhere
    result = preview(manager, other)
    link = ExternalRecordLink.objects.get()
    link_services.update_link(
        actor=manager,
        link_id=link.pk,
        expected_revision=link.revision,
        reason="Someone else",
        display_label="Their label",
        url="",
    )
    with pytest.raises(ValidationError, match="different from the preview"):
        commit_file(
            EXTERNAL_IDS,
            actor=manager,
            data=other.encode(),
            filename="ids.csv",
            reason="",
            state_token=result.state_token,
        )
    assert LinkAttachment.objects.filter(client__account_number="EXHS").count() == 0


def test_rows_never_move_a_link_between_organizations(manager, loaded):
    text = HEADER + "EXGEN,lis,other,,TEST-10,,,\r\nEXHS,lis,other,,TEST-10,,,\r\n"
    run(EXTERNAL_IDS, text, manager)
    link = ExternalRecordLink.objects.get()
    assert sorted(link.attachments.values_list("client__account_number", flat=True)) == [
        "EXGEN",
        "EXHS",
    ]
    duplicate = preview(
        manager, HEADER + "EXGEN,lis,other,,TEST-11,,,\r\nexgen,LIS,other,,TEST-11,,,\r\n"
    )
    assert duplicate.rows[1].errors[0].startswith("Duplicate of line 2")


def test_control_characters_in_identifiers_are_refused(manager, loaded):
    result = preview(manager, HEADER + 'EXGEN,lis,other,,"TEST​12",,,\r\n')
    assert result.rows[0].errors[0].startswith("identifier:")


def test_an_address_needs_a_system_with_a_base_address(manager, loaded):
    system = ExternalSystem.objects.get(key="lis")
    assert system.base_url == ""
    text = HEADER + "EXGEN,lis,order,,ORD-TEST-20,,,https://lis.example.invalid/20\r\n"
    result = preview(manager, text)
    assert not result.ok and result.rows[0].errors[0].startswith("url:")
