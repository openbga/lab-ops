"""Confirmation of the audit workspace fixes on synthetic laboratory records.

Adopted from the confirmation round (.local/review-history/audit-review-confirm.md).
"""

import csv
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from django.db import connections, transaction
from django.urls import reverse

from accounts.models import SignInEvent
from audit_review.tests.conftest import agent as agent
from audit_review.tests.conftest import clinic as clinic
from audit_review.tests.conftest import make_user as make_user
from audit_review.tests.conftest import reviewer as reviewer
from audit_review.tests.conftest import water as water


def _write_calendar_rows(source, actor, moments, monkeypatch):
    from django.utils import timezone

    from accounts.models import SignInEvent
    from audit_review.models import WorkspaceUse
    from core.models import AccessRecord, AuditEvent

    for moment in moments:
        if source == "changes":
            with monkeypatch.context() as fixed:
                fixed.setattr(timezone, "now", lambda stamp=moment: stamp)
                AuditEvent.objects.create(
                    actor=actor,
                    object_type="accounts.user",
                    object_id=str(actor.pk),
                    action="calendar.confirm",
                )
        elif source == "access":
            AccessRecord.objects.create(
                account=actor,
                ticket_id=42,
                capacity="laboratory",
                kind="ticket",
                occurred_at=moment,
            )
        elif source == "sign-ins":
            SignInEvent.objects.create(
                user=actor, kind="failed", factor="password", occurred_at=moment
            )
        else:
            WorkspaceUse.objects.create(
                account=actor, source="changes", kind="exported", rows=1, occurred_at=moment
            )


@pytest.mark.parametrize("source", ["changes", "access", "sign-ins", "uses"])
@pytest.mark.parametrize(
    ("zone", "day", "pivot"),
    [
        pytest.param(
            "America/St_Johns",
            "2009-10-31",
            "2009-11-01T02:31",
            # Adopted with the documented limit: a day runs from its first local midnight to the
            # next, so the hour repeated across midnight belongs to the later day.
            marks=pytest.mark.xfail(strict=True, reason="Documented limit: help, From and To."),
        ),
        ("Pacific/Apia", "2011-12-30", "2011-12-30T10:00"),
        ("Australia/Lord_Howe", "2024-04-07", "2024-04-06T15:00"),
        ("CET", "2025-07-02", "2025-07-01T22:30"),
        ("EET", "2025-07-02", "2025-07-01T21:30"),
        ("Etc/GMT+10", "2025-05-01", "2025-05-01T00:00"),
        ("Etc/GMT-14", "2025-05-01", "2025-05-01T00:00"),
    ],
)
def test_date_bounds_match_the_displayed_local_day_on_each_list(
    client, reviewer, agent, monkeypatch, source, zone, day, pivot
):
    """Compare single-day and open bounds with the dates shown on the unfiltered page and CSV."""
    import csv
    import io
    import re
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    # Adapted: CET and EET are links some time zone data no longer carries (CI's has neither).
    try:
        ZoneInfo(zone)
    except ZoneInfoNotFoundError:
        pytest.skip(f"This system's time zone data has no {zone}.")

    from django.urls import reverse
    from django.utils import timezone

    center = datetime.fromisoformat(pivot).replace(tzinfo=UTC)
    moments = [
        center + timedelta(seconds=seconds)
        for seconds in (-129600, -43200, -3600, -60, -1, 0, 1, 3600, 43200, 129600)
    ]
    _write_calendar_rows(source, agent, moments, monkeypatch)
    client.force_login(reviewer)
    path = reverse(f"audit_review:{source}")

    def reading(**bounds):
        response = client.get(path, {"person": agent.pk, **bounds})
        assert response.status_code == 200 and response.context["filter_form"].is_valid()
        # Use the When column actually drawn, including its UTC offset in the time element.
        html = response.content.decode()
        shown = (
            re.findall(
                r'<time datetime="([^"]+)">[^<]+</time>',
                html.split("<tbody>", 1)[-1].split("</tbody>", 1)[0],
            )
            if "<tbody>" in html
            else []
        )
        exported = client.get(response.context["export_url"])
        assert exported.status_code == 200
        csv_rows = csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig")))
        return [datetime.fromisoformat(s) for s in shown], [
            datetime.fromisoformat(row["When"]) for row in csv_rows
        ]

    with timezone.override(zone):
        unfiltered, original_export = reading()
        assert unfiltered == original_export == moments[::-1]
        differences = []
        for start, end in [(day, day), (day, None), (None, day)]:
            query = {
                name: value for name, value in (("date_from", start), ("date_to", end)) if value
            }
            expected = [
                stamp
                for stamp in unfiltered
                if (start is None or stamp.date().isoformat() >= start)
                and (end is None or stamp.date().isoformat() <= end)
            ]
            shown, exported = reading(**query)
            if shown != expected or exported != expected:
                differences.append(
                    (
                        query,
                        [s.isoformat() for s in expected],
                        [s.isoformat() for s in shown],
                        [s.isoformat() for s in exported],
                    )
                )
    assert not differences, differences


@pytest.mark.django_db(transaction=True)
def test_each_option_directory_handles_the_first_last_and_missing_page(client):
    from django.contrib.auth.models import Group, Permission
    from django.urls import reverse

    from accounts.models import User
    from clients.models import Client as Organization
    from clients.models import ClientContact, ClientSite
    from core.lookups import LOOKUPS

    lab = Organization.objects.create(name="Laboratory directory")
    reader = User.objects.create_user("directory-reviewer", email="reader@example.invalid")
    role, _ = Group.objects.get_or_create(name="technical")
    reader.groups.add(role)
    reader.user_permissions.add(
        Permission.objects.get(
            codename="review_audit_records",
            content_type__app_label="accounts",
            content_type__model="user",
        )
    )
    portal = User.objects.create_user(
        "directory-portal", email="portal@example.invalid", client=lab
    )
    prefix = "Confirmation"
    labels = [f"{prefix} {n:02}" for n in range(21)]
    organizations = Organization.objects.bulk_create([Organization(name=label) for label in labels])
    sites = ClientSite.objects.bulk_create([ClientSite(client=lab, name=label) for label in labels])
    contacts = ClientContact.objects.bulk_create(
        [ClientContact(client=lab, name=label, phone="555-0199") for label in labels]
    )
    people = User.objects.bulk_create(
        [
            User(username=f"confirm-{n:02}", email=f"confirm-{n}@example.invalid", first_name=label)
            for n, label in enumerate(labels)
        ]
    )
    role.user_set.add(*people)
    directories = {
        "clients": organizations,
        "active-clients": organizations,
        "desk-contacts": contacts,
        "portal-contacts": contacts,
        "desk-sites": sites,
        "role-holders": people,
        "staff": people,
        "review-accounts": people,
    }

    def options(address, page_number):
        response = client.get(address, {"q": prefix, "page": page_number})
        assert response.status_code == 200, address
        return response.json()

    assert set(directories) == set(LOOKUPS)
    for name, choices in directories.items():
        client.force_login(portal if name == "portal-contacts" else reader)
        address = reverse("lookup", args=[name])

        first = options(address, "1")
        assert len(first["results"]) == 20 and first["more"], name
        assert options(address, "first-page") == first, name
        last = options(address, "2")
        assert not last["more"], name
        assert [r["value"] for r in first["results"] + last["results"]] == [
            str(row.pk) for row in choices
        ], name
        for missing in ("0", "-1", "3"):
            assert options(address, missing) == {"results": [], "more": False}, (name, missing)


@pytest.mark.django_db
def test_request_template_keeps_distinct_readable_choices_when_another_value_is_refused():
    from html.parser import HTMLParser

    from clients.models import Client as Organization
    from request_forms.forms import TemplateForm

    class SelectedOptions(HTMLParser):
        def __init__(self):
            super().__init__()
            self.values = []

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag == "option" and "selected" in attributes:
                self.values.append(attributes["value"])

    choices = [Organization.objects.create(name=label) for label in ("East", "West")]
    left, right = [str(organization.pk) for organization in choices]
    # The field still rejects the submission. The user can correct it without reselecting
    # their two valid organizations; a repeated option must only appear once.
    form = TemplateForm(
        data={"clients": [right, "renamed choice", "", left, right, "9" * 40, "-3"]}
    )
    assert form.has_error("clients")
    selected = SelectedOptions()
    selected.feed(form["clients"].as_widget())
    assert sorted(selected.values) == sorted([left, right])


@pytest.mark.django_db(transaction=True)
def test_option_label_and_readers_name_agree_while_a_rename_commits(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace

    from django.contrib.auth.models import Group
    from django.db import connections
    from django.urls import reverse

    from accounts.models import User
    from core.lookups import LOOKUPS

    reader = User.objects.create_user("reader-name", first_name="Morning")
    reader.groups.add(Group.objects.get_or_create(name="technical")[0])
    client.force_login(reader)
    directory = LOOKUPS["staff"]
    observed = []

    def rename():
        try:
            User.objects.filter(pk=reader.pk).update(first_name="Afternoon")
        finally:
            connections.close_all()

    def options(request):
        rows = directory.queryset(request)
        with ThreadPoolExecutor(max_workers=1) as worker:
            worker.submit(rename).result(timeout=10)
        observed.append(request.user.first_name)
        return rows

    address = reverse("lookup", args=["staff"])
    with monkeypatch.context() as hooks:
        hooks.setitem(LOOKUPS, "staff", replace(directory, queryset=options))
        current = client.get(address, {"q": reader.username}).json()
    following = client.get(address, {"q": reader.username}).json()
    assert observed == ["Morning"]
    assert current["results"] == [{"value": str(reader.pk), "text": "Morning (reader-name)"}]
    assert following["results"] == [{"value": str(reader.pk), "text": "Afternoon (reader-name)"}]


@pytest.mark.django_db(transaction=True)
def test_snapshot_descriptions_allow_an_in_flight_event_without_an_organization_filter(
    client, reviewer, agent, settings
):
    client.force_login(reviewer)
    started = Event()
    release = Event()

    def save_pending():
        try:
            with transaction.atomic():
                SignInEvent.objects.create(user_id=agent.pk, kind="failed", factor="password")
                started.set()
                assert release.wait(15)
        finally:
            connections.close_all()

    # The lower id is allocated before the page opens and commits after a later id has committed.
    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(save_pending)
        try:
            assert started.wait(10)
            SignInEvent.objects.create(user=agent, kind="failed", factor="password")
            page = client.get(reverse("audit_review:sign-ins"), {"action": "failed"})
            assert page.status_code == 200 and page.context["total"] == 1
        finally:
            release.set()
        writer.result(timeout=10)
    exported = client.get(page.context["export_url"])
    assert exported.status_code == 200
    assert len(list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))) == 2

    product = (settings.BASE_DIR / "docs/product.md").read_text()
    product = " ".join(product.split()).partition("The Audit review workspace brings")[2]
    product = product.partition("Export CSV gives")[0]
    footer = (
        page.content.decode().partition("Rows are listed as they stood")[2].partition("</p>")[0]
    )
    pages = next(
        item["description"]
        for section in page.context["page_documentation"]["sections"]
        for item in section.get("items", [])
        if item["label"] == "Pages"
    )
    assert "still being saved" in pages
    # Both versions make a complete snapshot promise. The newer wording names only membership.
    start = "as the list stood when its first page opened"
    promises = {
        "product guide": product.rstrip().endswith(
            (
                start + ".",
                start + ", except that which accounts and tickets belong to a "
                "filtered organization is read again for each page and the export.",
            )
        ),
        "workspace footer": footer.startswith(
            (
                " when the first page opened; open the list again to see newer ones.",
                " when the first page opened (an organization's accounts and tickets as they "
                "stand now); open the list again to see newer ones.",
            )
        ),
    }
    assert not any(promises.values()), [place for place, promised in promises.items() if promised]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("round_trip", [False, True], ids=["initial", "reapplied"])
def test_demo_seed_identifies_its_model_with_an_existing_local_permission(
    monkeypatch, settings, tmp_path, round_trip
):
    from django.contrib.auth.models import Permission
    from django.contrib.contenttypes.models import ContentType
    from django.core.management import call_command

    from accounts.models import AccountSession, User

    settings.MEDIA_ROOT = tmp_path
    monkeypatch.setenv("DEMO_PASSWORD", "Synthetic-shared-fixture")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "Synthetic-private-fixture")
    codename = "review_audit_records"
    unrelated = Permission.objects.create(
        content_type=ContentType.objects.get_for_model(AccountSession),
        codename=codename,
        name="Synthetic local reporting",
    )
    if round_trip:
        try:
            call_command(
                "migrate", "accounts", "0009_sign_in_history", verbosity=0, interactive=False
            )
        finally:
            call_command("migrate", verbosity=0, interactive=False)
        assert Permission.objects.filter(pk=unrelated.pk).exists()
    call_command("seed_demo", verbosity=0, stdout=io.StringIO())
    supervisor = User.objects.get(username="demo_supervisor")
    app_permission = Permission.objects.get(
        codename=codename, content_type=ContentType.objects.get_for_model(User)
    )
    assert supervisor.user_permissions.filter(pk=app_permission.pk).exists()
    assert not unrelated.user_set.exists() and not unrelated.group_set.exists()


def test_filtered_sign_in_export_survives_normal_deactivation_and_refuses_external_deletion(
    client, reviewer, make_user
):
    from django.test import Client as Browser

    from audit_review.tests.conftest import PASSWORD
    from desk.tests.test_account_move import admin_change

    subject = make_user("confirm-former-account", "technical")
    SignInEvent.objects.create(user=subject, kind="failed", factor="password")
    client.force_login(reviewer)
    page = client.get(reverse("audit_review:sign-ins"), {"person": subject.pk, "action": "failed"})
    assert page.status_code == 200 and page.context["total"] == 1
    export_url = page.context["export_url"]
    lookup = reverse("lookup", args=["review-accounts"])
    administrator = Browser()
    administrator.force_login(
        make_user("confirm-administrator", "admin", is_staff=True, is_superuser=True)
    )
    change = admin_change(administrator, subject, is_active="", confirm_password=PASSWORD)
    assert change.status_code == 302, change.context["adminform"].form.errors
    subject.refresh_from_db()
    assert not subject.is_active
    assert client.get(lookup, {"q": subject.username}).json()["results"][0]["value"] == str(
        subject.pk
    )
    body = client.get(export_url)
    assert body.status_code == 200
    assert [
        row["Username"] for row in csv.DictReader(io.StringIO(body.content.decode("utf-8-sig")))
    ] == [subject.username]

    subject.delete()
    assert client.get(lookup, {"q": subject.username}).json()["results"] == []
    response = client.get(export_url, follow=True)
    assert len(response.redirect_chain) == 1 and response.context["total"] == 0
    assert "person" in response.context["filter_form"].errors


def test_organization_owned_link_is_in_the_documented_changes_filter(
    client, reviewer, agent, clinic, water
):
    from links.models import ExternalSystem
    from links.services import add_link

    system = ExternalSystem.objects.create(name="Example dispatch register")
    link, _ = add_link(
        actor=agent,
        client_id=clinic.pk,
        system=system,
        record_type="shipment",
        identifier="SHIP-C3-01",
    )
    add_link(
        actor=agent,
        client_id=water.pk,
        system=system,
        record_type="shipment",
        identifier="SHIP-C3-02",
    )
    client.force_login(reviewer)
    page = client.get(
        reverse("audit_review:changes"),
        {"action": "external_link.attached", "organization": clinic.pk},
    )
    assert page.status_code == 200 and page.context["total"] == 1
    [row] = page.context["page_obj"]
    assert row.cells[1].text.endswith(f" {link.pk}") and row.cells[1].note == clinic.name
    body = client.get(page.context["export_url"])
    assert body.status_code == 200
    [row] = csv.DictReader(io.StringIO(body.content.decode("utf-8-sig")))
    assert row["Client organization"] == clinic.name and row["Record"].endswith(f" {link.pk}")
    descriptions = [
        item["description"]
        for section in page.context["page_documentation"]["sections"]
        for item in section.get("items", [])
        if item["label"] == "Client organization"
    ]
    assert any("links" in entry for entry in descriptions)


def test_container_context_includes_the_workspace_and_omits_its_tests(tmp_path, settings):
    """Exercise the production COPY with the project's actual .dockerignore in a small context."""
    import shutil
    import subprocess

    docker = shutil.which("docker")
    if (
        not docker
        or subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=15
        ).returncode
    ):
        pytest.skip("The container-context check needs a Docker daemon")

    source = settings.BASE_DIR
    context = tmp_path / "context"
    output = tmp_path / "copied"
    context.mkdir()
    shutil.copy(source / ".dockerignore", context / ".dockerignore")
    shutil.copytree(
        source / "audit_review",
        context / "audit_review",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    subprocess.run(
        [
            docker,
            "build",
            "--network=none",
            "--file",
            "-",
            "--output",
            f"type=local,dest={output}",
            str(context),
        ],
        input="FROM scratch\nCOPY . .\n",
        text=True,
        check=True,
        capture_output=True,
        timeout=90,
    )
    app = output / "audit_review"
    required = {
        item.relative_to(source / "audit_review")
        for item in (source / "audit_review").rglob("*")
        if item.suffix in {".py", ".html"} and "tests" not in item.relative_to(source).parts
    }
    assert required and all((app / item).is_file() for item in required)
    assert not list((app / "tests").rglob("*.py")), "The image includes development-only test files"
