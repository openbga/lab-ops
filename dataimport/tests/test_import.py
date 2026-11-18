"""Spreadsheet import (M2-A): organizations, sites and contacts through the clients services,
all rows or none. Synthetic data only."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from accounts.models import User
from clients.models import Client, ClientContact, ClientSite, PortalAccess
from clients.services import update_client
from core.models import AuditEvent
from dataimport import importing
from dataimport.importing import CONTACTS, ORGANIZATIONS, SITES, commit_file, preview_file

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db

ORGS = (
    "account_code,name,organization_type,parent_account_code,email,phone,city,state\r\n"
    "EXHS,Example Health System,healthcare,,hs@example.invalid,555-0130,Exampleville,ny\r\n"
    "EXGEN,Example General Hospital,healthcare,EXHS,,555-0131,Exampleville,NY\r\n"
)
SITES_CSV = (
    "account_code,site_code,name,kind,phone\r\n"
    "EXGEN,ED,Emergency department,facility,555-0132\r\n"
    "EXGEN,DRAW,Outpatient draw station,collection_site,\r\n"
)
CONTACTS_CSV = (
    "account_code,email,name,role,phone,responsibilities,site_codes\r\n"
    "EXGEN,liaison@hospital.example.invalid,Example Liaison,Liaison,555-0134,"
    "results; Quality,\r\n"
    "EXGEN,,Example Courier,Courier,555-0135,Supplies and pickups,DRAW\r\n"
)


def run(record_type, text, actor, reason="", encoding="utf-8"):
    data = text.encode(encoding) if isinstance(text, str) else text
    preview = preview_file(record_type, actor=actor, data=data, filename="file.csv")
    assert preview.ok, (preview.file_errors, [r.errors for r in preview.rows])
    return commit_file(
        record_type,
        actor=actor,
        data=data,
        filename="file.csv",
        reason=reason,
        state_token=preview.state_token,
    )


def counts():
    return (
        Client.objects.count(),
        ClientSite.objects.count(),
        ClientContact.objects.count(),
        AuditEvent.objects.count(),
        User.objects.count(),
        PortalAccess.objects.count(),
    )


def load_all(actor):
    run(ORGANIZATIONS, ORGS, actor)
    run(SITES, SITES_CSV, actor)
    run(CONTACTS, CONTACTS_CSV, actor)


# Roles ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["technical", "supervisor"])
def test_other_roles_and_portal_accounts_are_refused(client, make_user, role):
    targets = [
        reverse("dataimport:index"),
        reverse("dataimport:upload", args=["organizations"]),
        reverse("dataimport:template", args=["contacts"]),
    ]
    portal = make_user("portal", client=Client.objects.create(name="Example Org"))
    for user in (make_user(f"r-{role}", role), portal):
        client.force_login(user)
        for url in targets:
            assert client.get(url).status_code == 403
        with pytest.raises(PermissionDenied):
            preview_file(ORGANIZATIONS, actor=user, data=ORGS.encode(), filename="f.csv")
        with pytest.raises(PermissionDenied):
            commit_file(
                ORGANIZATIONS,
                actor=user,
                data=ORGS.encode(),
                filename="f.csv",
                reason="",
                state_token="x",
            )
    client.logout()
    assert client.get(targets[0]).status_code == 302  # to sign-in


@pytest.mark.parametrize("role", ["account_manager", "client_services", "admin"])
def test_client_managers_open_every_page(client, make_user, role):
    client.force_login(make_user(f"m-{role}", role))
    assert client.get(reverse("dataimport:index")).status_code == 200
    for record_type in importing.TYPES:
        assert client.get(reverse("dataimport:upload", args=[record_type.slug])).status_code == 200
    assert client.get(reverse("dataimport:upload", args=["patients"])).status_code == 404


# Templates -----------------------------------------------------------------------------------


@pytest.mark.parametrize("record_type", importing.TYPES, ids=lambda t: t.slug)
def test_template_has_every_column_and_one_synthetic_row(client, manager, record_type):
    client.force_login(manager)
    response = client.get(reverse("dataimport:template", args=[record_type.slug]))
    assert response["Content-Disposition"] == (
        f'attachment; filename="{record_type.slug}-template.csv"'
    )
    assert response["X-Content-Type-Options"] == "nosniff"
    body = response.content
    assert body.startswith("﻿".encode())
    lines = body.decode("utf-8-sig").splitlines()
    assert lines[0].split(",") == [c.name for c in record_type.columns]
    assert len(lines) == 2
    assert "Example" in lines[1] or "EXAMPLE" in lines[1]  # synthetic
    assert not any(importing.formula_warning("x", cell) for cell in lines[1].split(","))


def test_templates_import_in_order(manager):
    for record_type in importing.TYPES:
        done = run(record_type, importing.template_bytes(record_type), manager)
        assert done.counts["create"] == 1
    contact = ClientContact.objects.get()
    assert [site.code for site in contact.sites.all()] == ["MAIN"]


# Outcome, audit and idempotence ---------------------------------------------------------------


def test_files_create_records_through_the_services_with_one_summary_each(manager):
    users_before = User.objects.count()
    load_all(manager)
    hospital = Client.objects.get(account_number="EXGEN")
    assert hospital.parent.account_number == "EXHS"
    assert Client.objects.get(account_number="EXHS").state == "NY"  # normalised
    assert set(hospital.sites.values_list("code", flat=True)) == {"ED", "DRAW"}
    liaison = ClientContact.objects.get(email="liaison@hospital.example.invalid")
    assert liaison.responsibilities == ["results", "quality"]
    courier = ClientContact.objects.get(name="Example Courier")
    assert courier.email == "" and [s.code for s in courier.sites.all()] == ["DRAW"]
    summaries = AuditEvent.objects.filter(action=importing.SUMMARY_ACTION)
    assert [e.after["record_type"] for e in summaries.order_by("occurred_at")] == [
        "organizations",
        "sites",
        "contacts",
    ]
    assert all(e.actor == manager for e in summaries)
    assert AuditEvent.objects.filter(action="client.created").count() == 2
    assert AuditEvent.objects.filter(action="site.created").count() == 2
    assert AuditEvent.objects.filter(action="client_contact.added").count() == 2
    created = AuditEvent.objects.filter(action="client.created").first()
    assert "file.csv" in created.reason and "SHA-256" in created.reason
    # Never sign-in accounts or portal access.
    assert User.objects.count() == users_before and not PortalAccess.objects.exists()


def test_reimporting_the_same_files_changes_nothing(manager):
    load_all(manager)
    before = counts()
    for record_type, text in ((ORGANIZATIONS, ORGS), (SITES, SITES_CSV), (CONTACTS, CONTACTS_CSV)):
        done = run(record_type, text, manager)
        assert done.counts == {"create": 0, "update": 0, "unchanged": 2, "error": 0}
    assert counts() == before  # no records, no audit events, not even a summary


def test_updates_need_a_reason_kept_in_each_history(manager):
    load_all(manager)
    changed = ORGS.replace("555-0131", "555-0139")
    data = changed.encode()
    preview = preview_file(ORGANIZATIONS, actor=manager, data=data, filename="f.csv")
    assert preview.counts == {"create": 0, "update": 1, "unchanged": 1, "error": 0}
    row = [r for r in preview.rows if r.action == "update"][0]
    assert row.changes == [("phone", "555-0131", "555-0139")]
    with pytest.raises(ValidationError) as exc:
        commit_file(
            ORGANIZATIONS,
            actor=manager,
            data=data,
            filename="f.csv",
            reason="",
            state_token=preview.state_token,
        )
    assert "reason" in exc.value.message_dict
    commit_file(
        ORGANIZATIONS,
        actor=manager,
        data=data,
        filename="f.csv",
        reason="New switchboard",
        state_token=preview.state_token,
    )
    event = AuditEvent.objects.get(action="client.updated")
    assert event.reason.startswith("New switchboard (import of f.csv")
    assert Client.objects.get(account_number="EXGEN").phone == "555-0139"


def test_absent_column_is_left_alone_and_blank_clears_optional(manager):
    load_all(manager)
    run(ORGANIZATIONS, "account_code,city\r\nEXGEN,\r\n", manager, reason="Moved")
    hospital = Client.objects.get(account_number="EXGEN")
    assert hospital.city == "" and hospital.phone == "555-0131"


# Risks: no trace, partial import, stale preview -----------------------------------------------


def test_preview_leaves_no_trace(manager):
    load_all(manager)
    before = counts()
    changed = ORGS.replace("555-0131", "555-0139") + "EXNEW,Example New Org,other,,,,,\r\n"
    preview = preview_file(ORGANIZATIONS, actor=manager, data=changed.encode(), filename="f.csv")
    assert preview.counts["create"] == 1 and preview.counts["update"] == 1
    assert counts() == before
    assert Client.objects.get(account_number="EXGEN").phone == "555-0131"


def test_a_late_service_error_writes_nothing(manager):
    # The last row passes the file checks but the service refuses it (neither email nor phone).
    text = (
        "account_code,email,name,phone\r\n"
        "EXGEN,a@hospital.example.invalid,Example A,\r\n"
        "EXGEN,,Example Unreachable,\r\n"
    )
    run(ORGANIZATIONS, ORGS, manager)
    before = counts()
    preview = preview_file(CONTACTS, actor=manager, data=text.encode(), filename="c.csv")
    assert not preview.ok and preview.rows[1].errors
    with pytest.raises(ValidationError):
        commit_file(
            CONTACTS,
            actor=manager,
            data=text.encode(),
            filename="c.csv",
            reason="",
            state_token=preview.state_token,
        )
    assert counts() == before


def test_a_record_changed_after_the_preview_is_refused(manager):
    run(ORGANIZATIONS, ORGS, manager)
    data = ORGS.replace("555-0131", "555-0139").encode()
    preview = preview_file(ORGANIZATIONS, actor=manager, data=data, filename="f.csv")
    hospital = Client.objects.get(account_number="EXGEN")
    update_client(
        actor=manager,
        client_id=hospital.pk,
        expected_revision=hospital.revision,
        reason="Someone else",
        city="Elsewhere",
    )
    with pytest.raises(ValidationError, match="different from the preview"):
        commit_file(
            ORGANIZATIONS,
            actor=manager,
            data=data,
            filename="f.csv",
            reason="x",
            state_token=preview.state_token,
        )
    assert Client.objects.get(pk=hospital.pk).phone == "555-0131"


def test_a_file_edited_between_preview_and_import_is_refused(manager):
    data = ORGS.encode()
    preview = preview_file(ORGANIZATIONS, actor=manager, data=data, filename="f.csv")
    edited = ORGS.replace("Example General Hospital", "Example Other Hospital").encode()
    with pytest.raises(ValidationError, match="different from the preview"):
        commit_file(
            ORGANIZATIONS,
            actor=manager,
            data=edited,
            filename="f.csv",
            reason="",
            state_token=preview.state_token,
        )
    assert not Client.objects.exists()


def test_page_flow_and_tampered_commit(client, manager):
    client.force_login(manager)
    url = reverse("dataimport:upload", args=["organizations"])
    response = client.post(
        url, {"step": "preview", "file": SimpleUploadedFile("orgs.csv", ORGS.encode())}
    )
    assert response.status_code == 200 and response.context["preview"].ok
    assert not Client.objects.exists()
    form = response.context["commit_form"]
    fields = {name: form[name].value() for name in ("step", "filename", "payload", "state_token")}
    tampered = {
        **fields,
        "payload": importing.encode_payload(ORGS.replace("EXGEN", "EXX").encode()),
    }
    response = client.post(url, tampered)
    assert response.status_code == 200 and not Client.objects.exists()
    assert "different from the preview" in response.content.decode()
    response = client.post(url, fields)
    assert response.status_code == 302 and Client.objects.count() == 2
    assert "Imported orgs.csv: 2 created" in str(list(response.wsgi_request._messages))


# Risks: cells, encoding, patients --------------------------------------------------------------


def test_formula_looking_cells_are_flagged_and_kept_verbatim(manager):
    text = (
        "account_code,name,notes,phone\r\n"
        'EXF,Example Formula Org,"=HYPERLINK(""http://x.invalid"")",+1 555 0100\r\n'
        "EXG,Example Plain Org,＠sum,555-0101\r\n"
        "EXH,Example Quiet Org,Ordinary note,555-0102\r\n"
    )
    preview = preview_file(ORGANIZATIONS, actor=manager, data=text.encode(), filename="f.csv")
    assert preview.ok and preview.warning_count == 3
    assert [len(r.warnings) for r in preview.rows] == [2, 1, 0]
    assert preview.ordered_rows[-1].label == "EXH"
    run(ORGANIZATIONS, text, manager)
    assert Client.objects.get(account_number="EXF").notes == '=HYPERLINK("http://x.invalid")'
    summary = AuditEvent.objects.get(action=importing.SUMMARY_ACTION)
    assert summary.after["formula_warnings"] == 3


def test_utf8_with_byte_order_mark_and_mixed_case_headers(manager):
    text = "﻿Account_Code,NAME,City\r\nEXC,Example Café Clinic,Exampleville\r\n"
    run(ORGANIZATIONS, text, manager)
    assert Client.objects.get(account_number="EXC").name == "Example Café Clinic"


def test_other_encodings_are_refused_not_guessed(manager):
    data = "account_code,name\r\nEXC,Example Café Clinic\r\n".encode("latin-1")
    preview = preview_file(ORGANIZATIONS, actor=manager, data=data, filename="f.csv")
    assert not preview.ok and "not UTF-8" in preview.file_errors[0]


def test_patient_columns_are_refused(manager):
    data = b"account_code,name,patient_name,dob\r\nEXC,Example Clinic,Jane Example,2000-01-01\r\n"
    preview = preview_file(ORGANIZATIONS, actor=manager, data=data, filename="f.csv")
    assert not preview.ok and "patient data" in preview.file_errors[0]


def test_header_and_row_errors_name_line_and_column(manager):
    preview = preview_file(
        ORGANIZATIONS, actor=manager, data=b"name,colour\r\nX,red\r\n", filename="f.csv"
    )
    assert any("unknown column(s) colour" in e for e in preview.file_errors)
    assert any("missing key column(s) account_code" in e for e in preview.file_errors)
    text = (
        "account_code,name,organization_type,state,account_owner,active\r\n"
        "EXA,Example A,spaceship,ZZ,nobody,maybe\r\n"
        "EXA,Example A again,,,,\r\n"
        "EXB,,,,,\r\n"
    )
    preview = preview_file(ORGANIZATIONS, actor=manager, data=text.encode(), filename="f.csv")
    first, second, third = preview.rows
    assert first.line == 2
    assert {e.split(":")[0] for e in first.errors} == {
        "organization_type",
        "state",
        "account_owner",
        "active",
    }
    assert "active: cannot be blank." in second.errors
    assert "name: cannot be blank." in third.errors


def test_duplicates_and_twin_names_in_one_file(manager):
    text = "account_code,name\r\nEXA,Example A\r\nexa,Example A spelled again\r\nEXB,Example A\r\n"
    preview = preview_file(ORGANIZATIONS, actor=manager, data=text.encode(), filename="f.csv")
    assert preview.rows[1].errors == ["Duplicate of line 2: one row per record."]
    assert "earlier row of this file" in preview.rows[2].errors[0]


def test_parents_come_first_and_cycles_are_refused(manager):
    late = "account_code,name,parent_account_code\r\nEXC,Example Child,EXP\r\nEXP,Example P,\r\n"
    preview = preview_file(ORGANIZATIONS, actor=manager, data=late.encode(), filename="f.csv")
    assert "Put parents first" in preview.rows[0].errors[0]
    run(
        ORGANIZATIONS,
        "account_code,name,parent_account_code\r\nEXP,Example P,\r\nEXC,Example Child,exp\r\n",
        manager,
    )
    cycle = "account_code,parent_account_code\r\nEXP,EXC\r\n"
    preview = preview_file(ORGANIZATIONS, actor=manager, data=cycle.encode(), filename="f.csv")
    assert not preview.ok and "parent_account_code" in preview.rows[0].errors[0]


# Risks: nothing moves across organizations -----------------------------------------------------


def test_site_and_contact_rows_never_move_records_between_organizations(manager):
    load_all(manager)
    run(ORGANIZATIONS, "account_code,name\r\nEXOTHER,Example Other Org\r\n", manager)
    ed = ClientSite.objects.get(code="ED")
    liaison = ClientContact.objects.get(email="liaison@hospital.example.invalid")
    # The same site code and the same email under another organization are other records.
    run(SITES, "account_code,site_code,name\r\nEXOTHER,ED,Example Other ED\r\n", manager)
    run(
        CONTACTS,
        "account_code,email,name\r\nEXOTHER,liaison@hospital.example.invalid,Example Liaison\r\n",
        manager,
    )
    ed.refresh_from_db()
    liaison.refresh_from_db()
    assert ed.client.account_number == "EXGEN" and liaison.client.account_number == "EXGEN"
    assert ClientSite.objects.filter(code="ED").count() == 2
    # A contact cannot serve another organization's site.
    text = (
        "account_code,email,name,site_codes\r\nEXOTHER,new@other.example.invalid,Example N,DRAW\r\n"
    )
    preview = preview_file(CONTACTS, actor=manager, data=text.encode(), filename="f.csv")
    assert "no site DRAW at EXOTHER" in preview.rows[0].errors[0]


def test_a_contact_known_by_name_gains_its_email(manager):
    load_all(manager)
    text = "account_code,email,name\r\nEXGEN,courier@hospital.example.invalid,Example Courier\r\n"
    preview = preview_file(CONTACTS, actor=manager, data=text.encode(), filename="f.csv")
    assert preview.rows[0].action == "update"
    assert preview.rows[0].changes == [("email", "", "courier@hospital.example.invalid")]
    run(CONTACTS, text, manager, reason="Email on file now")
    assert ClientContact.objects.get(name="Example Courier").email == (
        "courier@hospital.example.invalid"
    )
    assert ClientContact.objects.count() == 2


def test_unknown_organization_is_a_row_error(manager):
    preview = preview_file(SITES, actor=manager, data=SITES_CSV.encode(), filename="s.csv")
    assert all("Import the organizations first" in r.errors[0] for r in preview.rows)


def test_signed_in_upload_page_has_help_and_columns(client, manager):
    assert client.login(username=manager.username, password=PASSWORD)
    html = client.get(reverse("dataimport:upload", args=["contacts"])).content.decode()
    assert "site_codes" in html and "contacts-template.csv" in html
