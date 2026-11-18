"""Confirmation binds each imported record to its preview."""

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from clients.models import Client, ClientSite
from clients.services import update_client
from core.models import AuditEvent
from dataimport import importing
from links.models import ExternalRecordLink, ExternalSystem

from .test_import import run

pytestmark = pytest.mark.django_db
MARK = "PT-TEST-SENTINEL-7731"


@pytest.fixture
def clinic(db):
    return Client.objects.create(name="Example Clinic", account_number="EXCLIN")


@pytest.fixture
def other_org(db):
    return Client.objects.create(name="Example Other", account_number="EXOTHER")


def test_preview_token_binds_destination_organization(manager, clinic, other_org):
    data = f"account_code,site_code,name\n{clinic.account_number},R3,Round three\n".encode()
    options = {"actor": manager, "data": data, "filename": "sites.csv"}
    approved = importing.preview_file(importing.SITES, **options)
    assert approved.ok
    code = clinic.account_number
    for org, new_code in [(clinic, "OLD-CLIENT"), (other_org, code)]:
        org.refresh_from_db()
        update_client(
            actor=manager,
            client_id=org.pk,
            expected_revision=org.revision,
            reason="Renumber organization",
            account_number=new_code,
        )
    new = importing.preview_file(importing.SITES, **options)
    assert approved.rows[0].new_fields["client_id"] != new.rows[0].new_fields["client_id"]
    assert approved.state_token != new.state_token
    with pytest.raises(ValidationError, match="different from the preview"):
        importing.commit_file(
            importing.SITES, reason="", state_token=approved.state_token, **options
        )
    assert not ClientSite.objects.exists()


def test_parent_resolution_is_bound_too(manager, clinic, other_org):
    data = b"account_code,name,parent_account_code\nEXCHILD,Example Child,EXCLIN\n"
    options = {"actor": manager, "data": data, "filename": "orgs.csv"}
    approved = importing.preview_file(importing.ORGANIZATIONS, **options)
    for org, new_code in [(clinic, "OLD-CLIENT"), (other_org, "EXCLIN")]:
        org.refresh_from_db()
        update_client(
            actor=manager,
            client_id=org.pk,
            expected_revision=org.revision,
            reason="Renumber organization",
            account_number=new_code,
        )
    with pytest.raises(ValidationError, match="different from the preview"):
        importing.commit_file(
            importing.ORGANIZATIONS, reason="", state_token=approved.state_token, **options
        )


def test_person_import_error_does_not_display_identifier(client, manager, clinic):
    client.force_login(manager)
    data = (
        "account_code,system,record_type,identifier_scheme,identifier,role\n"
        f"{clinic.account_number},lis,patient,,{MARK},related\n"
    )
    page = client.post(
        reverse("dataimport:upload", args=["external-ids"]),
        {"step": "preview", "file": SimpleUploadedFile("identifiers.csv", data.encode())},
    )
    assert page.status_code == 200 and b"person-level" in page.content
    assert MARK.encode() not in page.content
    assert b"identifier not shown" in page.content


def test_reused_link_keeps_its_label_across_organizations(manager, clinic, other_org):
    system, _ = ExternalSystem.objects.get_or_create(
        key="test-lis", defaults={"name": "Example LIS", "kind": "lis"}
    )
    header = "account_code,system,record_type,identifier_scheme,identifier,role,display_label\n"
    run(
        importing.EXTERNAL_IDS,
        header + f"{clinic.account_number},{system.key},order,,TEST-ORDER,related,Old label\n",
        manager,
    )
    new_file = (
        header + f"{other_org.account_number},{system.key},order,,TEST-ORDER,related,New label\n"
    )
    done = run(importing.EXTERNAL_IDS, new_file, manager, reason="")
    assert done.counts["create"] == 1 and done.rows[0].not_applied
    link = ExternalRecordLink.objects.get(system=system, identifier="TEST-ORDER")
    assert link.display_label == "Old label" and link.attachments.count() == 2


def test_import_source_filename_matches_preview(manager):
    data = b"account_code,name\nR3,Example round three\n"
    original = importing.preview_file(
        importing.ORGANIZATIONS, actor=manager, data=data, filename="checked.csv"
    )
    assert original.ok
    with pytest.raises(ValidationError, match="different from the preview"):
        importing.commit_file(
            importing.ORGANIZATIONS,
            actor=manager,
            data=data,
            filename="other.csv",
            reason="",
            state_token=original.state_token,
        )
    assert not AuditEvent.objects.filter(action=importing.SUMMARY_ACTION).exists()
    importing.commit_file(
        importing.ORGANIZATIONS,
        actor=manager,
        data=data,
        filename="checked.csv",
        reason="",
        state_token=original.state_token,
    )
    summary = AuditEvent.objects.get(action=importing.SUMMARY_ACTION)
    assert summary.after["filename"] == "checked.csv"


def test_import_resolves_the_parent_once_under_lock(manager, clinic, monkeypatch):
    """The write uses the parent resolved and locked when the file was read;
    no second, unlocked lookup of the parent's code happens while the rows are written."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    content = (
        f"account_code,name,parent_account_code\nCHILD,Example Child,{clinic.account_number}\n"
    )
    args = {"actor": manager, "data": content.encode(), "filename": "example.csv"}
    preview = importing.preview_file(importing.ORGANIZATIONS, **args)
    apply_rows = importing._apply
    lookups = []

    def observed_apply(*a, **kw):
        with CaptureQueriesContext(connection) as queries:
            result = apply_rows(*a, **kw)
        lookups.extend(q["sql"] for q in queries)
        return result

    monkeypatch.setattr(importing, "_apply", observed_apply)
    with CaptureQueriesContext(connection) as everything:
        importing.commit_file(
            importing.ORGANIZATIONS, state_token=preview.state_token, reason="", **args
        )
    assert Client.objects.get(account_number="CHILD").parent_id == clinic.pk
    assert not any(
        "SELECT" in sql and "UPPER(" in sql and clinic.account_number in sql for sql in lookups
    )
    assert any(
        "UPPER(" in q["sql"] and clinic.account_number in q["sql"] and "FOR UPDATE" in q["sql"]
        for q in everything
    )
