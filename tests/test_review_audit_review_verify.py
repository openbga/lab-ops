"""Independent verification of the round-one audit workspace fixes, using synthetic data.

Adopted from the verification round (.local/review-history/audit-review-verify.md). The case of an
account deleted outside the application is a documented limit (strict xfail; page help, Person)."""

import csv
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from html import unescape
from importlib.util import find_spec
from threading import Event
from urllib.parse import urljoin

import pytest
from django.contrib.auth.models import Group, Permission
from django.db import connections, transaction
from django.test import Client as Browser
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from accounts.models import SignInEvent
from core.models import AccessRecord, AuditEvent

if find_spec("audit_review"):
    from audit_review.tests.conftest import agent as agent
    from audit_review.tests.conftest import category as category
    from audit_review.tests.conftest import clinic as clinic
    from audit_review.tests.conftest import make_ticket as make_ticket
    from audit_review.tests.conftest import make_user as make_user
    from audit_review.tests.conftest import reviewer as reviewer
    from audit_review.tests.conftest import water as water


def test_product_history_visibility_matches_the_release(client, settings, staff):
    """Compare the product's privacy assurance to a real, explicitly authorized reader."""
    account = staff("verify-subject", "technical")
    historical = timezone.now() - timedelta(days=95)
    SignInEvent.objects.create(
        user=account, kind="failed", factor="password", occurred_at=historical
    )
    client.force_login(account)
    own_page = client.get(reverse("account-sign-ins"))
    assert own_page.status_code == 200
    assert all(entry.occurred_at != historical for entry in own_page.context["entries"])

    try:
        review_url = reverse("audit_review:sign-ins")
    except NoReverseMatch:  # the preceding release only has the personal page
        review_rows = []
    else:
        reader = staff("verify-reader", "technical")
        permission = Permission.objects.get(
            content_type__app_label="accounts",
            content_type__model="user",
            codename="review_audit_records",
        )
        reader.user_permissions.add(permission)
        client.force_login(reader)
        review = client.get(review_url, {"person": account.pk, "action": "failed"})
        assert review.status_code == 200
        review_rows = list(review.context["page_obj"])
        assert [(row.username, row.occurred_at) for row in review_rows] == [
            (account.username, historical)
        ]
    guide = " ".join((settings.BASE_DIR / "docs/product.md").read_text().split())
    assert not (review_rows and "nobody reads another person's history in this release" in guide)


def test_product_and_footer_qualify_the_snapshot_when_an_organization_moves(
    client, settings, reviewer, make_user, clinic, water
):
    """The advertised export follows today's organization membership, as detailed Help says."""
    from desk.tests.test_account_move import admin_change

    person = make_user("verify-portal-move", client=clinic)
    SignInEvent.objects.create(user=person, kind="failed", factor="password")
    administration = Browser()
    administration.force_login(
        make_user("verify-dir-admin", "admin", is_superuser=True, is_staff=True)
    )
    client.force_login(reviewer)
    page = client.get(
        reverse("audit_review:sign-ins"), {"action": "failed", "organization": clinic.pk}
    )
    assert page.status_code == 200 and page.context["total"] == 1
    export_url = page.context["export_url"]
    first = client.get(export_url)
    assert first.status_code == 200
    assert len(list(csv.DictReader(io.StringIO(first.content.decode("utf-8-sig"))))) == 1
    assert admin_change(administration, person, client=water.pk).status_code == 302
    exported = client.get(export_url)
    assert exported.status_code == 200
    assert list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig")))) == []

    guide = " ".join((settings.BASE_DIR / "docs/product.md").read_text().split())
    workspace_section = guide.split("The Audit review workspace brings", 1)[-1].split(
        "The [safeguards reference]", 1
    )[0]
    footer = page.content.decode().split("Rows are listed as they stood", 1)
    statement = "Every page, and the export, is drawn as the list stood when its first page opened."
    claims = {
        "product guide": statement in workspace_section and "moved" not in workspace_section,
        "workspace footer": len(footer) == 2
        and "when the first page opened; open the list again to see newer ones."
        in footer[1].split("</p>", 1)[0],
    }
    assert not any(claims.values()), [place for place, absolute in claims.items() if absolute]


def test_workspace_help_includes_clocks_among_ticket_labelled_records(
    client, reviewer, clinic, make_ticket
):
    from core.audit import record_event
    from service_levels.models import RequestClock

    ticket = make_ticket(clinic)
    clock, _ = RequestClock.objects.get_or_create(request=ticket)
    record_event(actor=reviewer, action="clock.breach_noticed", obj=clock)
    client.force_login(reviewer)
    page = client.get(
        reverse("audit_review:changes"), {"action": "clock.breach_noticed", "ticket": ticket.number}
    )
    assert page.status_code == 200
    [row] = page.context["page_obj"]
    assert row.cells[1].text.endswith(ticket.number)
    help_content = page.context["page_documentation"]
    definition = next(
        field["description"]
        for section in help_content["sections"]
        for field in section.get("items", [])
        if field["label"] == "Record"
    )
    if "anything else by its kind and number" in definition:
        assert "clock" in definition.casefold(), definition


def test_security_settings_count_and_help_follow_role_group_and_deactivation(
    client, staff, make_user
):
    admin = staff("verify-settings", "admin")
    client.force_login(admin)
    group = Group.objects.create(name="verify-review-holders")
    perm = Permission.objects.get(
        content_type__app_label="accounts",
        content_type__model="user",
        codename="review_audit_records",
    )
    group.permissions.add(perm)
    first = make_user("verify-both-grants", "technical")
    second = make_user("verify-group-grant", "technical")
    group.user_set.add(first, second)
    first.user_permissions.add(perm)

    def displayed():
        page = client.get(reverse("security-settings"))
        assert page.status_code == 200
        facts = [fact for category in page.context["groups"] for fact in category.facts]
        [fact] = [fact for fact in facts if fact.key == "review-capability"]
        assert fact.label in json.dumps(page.context["page_documentation"])
        return fact.value

    assert displayed() == "2 active staff accounts"
    first.groups.remove(Group.objects.get(name="technical"))
    assert displayed() == "1 active staff account"
    second.is_active = False
    second.save(update_fields=["is_active"])
    assert displayed() == "0 active staff accounts"


def test_every_list_applies_only_its_documented_filters(client, reviewer, clinic):
    from audit_review.models import WorkspaceUse

    client.force_login(reviewer)
    common = {"action", "date_from", "date_to", "person"}
    extra = {
        # adapted: Changes, Access records and Sign-ins gained "Outside the hours of" (People tab)
        "changes": {"organization", "ticket", "outside_hours"},
        "access": {"organization", "ticket", "outside_hours"},
        "sign-ins": {"organization", "outside_hours"},
        "uses": set(),
    }
    for route, fields in extra.items():
        page = client.get(reverse(f"audit_review:{route}"), {"organization": clinic.pk})
        assert page.status_code == 200
        assert set(page.context["filter_form"].fields) == common | fields
        use = WorkspaceUse.objects.get(request_ref=page["X-Request-ID"])
        assert use.filters == (
            {"organization": str(clinic.pk), "page": "1"}
            if "organization" in fields
            else {"page": "1"}
        )


# Snapshot boundaries and retained records


def _verify_history_row(route, account_id, when):
    from audit_review.models import WorkspaceUse

    if route == "access":
        return AccessRecord(
            account_id=account_id,
            ticket_id=42,
            capacity="laboratory",
            kind="ticket",
            occurred_at=when,
        )
    if route == "uses":
        return WorkspaceUse(
            account_id=account_id,
            source="changes",
            kind="exported",
            rows=1,
            occurred_at=when,
        )
    return SignInEvent(user_id=account_id, kind="failed", factor="password", occurred_at=when)


def _verify_export_times(response):
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    lines = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")))
    return [datetime.fromisoformat(line["When"]) for line in lines]


@pytest.mark.parametrize("route", ["access", "sign-ins", "uses"])
def test_id_held_pages_and_offered_export_survive_crossing_the_limit(
    client, reviewer, agent, route
):
    client.force_login(reviewer)
    when = datetime(2024, 3, 22, tzinfo=UTC)
    initial = [when + timedelta(minutes=n) for n in range(53)]
    records = [_verify_history_row(route, agent.pk, stamp) for stamp in initial]
    model = type(records[0])
    model.objects.bulk_create(records)
    address = reverse(f"audit_review:{route}")
    first = client.get(address, {"person": agent.pk})
    assert first.status_code == 200
    assert first.context["total"] == len(records)
    assert int(first.context["request"].GET["upto"]) == records[-1].pk
    export = first.context["export_url"]
    next_href = re.search(r'href="([^"]+)" rel="next"', first.content.decode())
    assert next_href is not None
    second_url = urljoin(address, unescape(next_href[1]))

    # Later writes alternate between earlier and later occurrence times.
    added = [
        _verify_history_row(route, agent.pk, when + timedelta(days=(-1) ** n))
        for n in range(10_001 - len(records))
    ]
    model.objects.bulk_create(added, batch_size=500)
    second = client.get(second_url)
    assert second.status_code == 200 and second.context["total"] == len(records)
    rendered = [row.occurred_at for page in (first, second) for row in page.context["page_obj"]]
    assert rendered == initial[::-1]
    assert _verify_export_times(client.get(export)) == rendered
    fresh = client.get(address, {"person": agent.pk})
    assert fresh.context["total"] == 10_001 and not fresh.context["exportable"]
    assert (
        client.get(reverse(f"audit_review:{route}-export"), {"person": agent.pk}).status_code == 302
    )


@pytest.mark.parametrize("route", ["access", "sign-ins", "uses"])
def test_id_cutoff_is_inclusive_and_clamped_on_each_list(client, reviewer, agent, route):
    client.force_login(reviewer)
    when = datetime(2024, 4, 5, tzinfo=UTC)
    # The oldest timestamp deliberately gets the largest id.
    records = [_verify_history_row(route, agent.pk, when - timedelta(days=i)) for i in range(3)]
    model = type(records[0])
    model.objects.bulk_create(records)
    newest = records[-1].pk
    for supplied in (0, newest - 1, newest, newest + 1, -1, "9" * 19, "9" * 40):
        before = model.objects.latest("pk").pk
        expected = min(supplied, before) if isinstance(supplied, int) and supplied >= 0 else before
        query = {"person": agent.pk, "upto": str(supplied)}
        page = client.get(reverse(f"audit_review:{route}"), query)
        assert page.status_code == 200 and int(page.context["request"].GET["upto"]) == expected
        rows = [row.occurred_at for row in page.context["page_obj"]]
        assert rows == [record.occurred_at for record in records if record.pk <= expected]
        assert _verify_export_times(client.get(page.context["export_url"])) == rows


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("route", ["access", "sign-ins", "uses"])
@pytest.mark.parametrize("commit_a_higher_id", [False, True])
def test_id_allocated_before_first_page_obeys_the_documented_in_flight_exception(
    client, reviewer, agent, route, commit_a_higher_id
):
    client.force_login(reviewer)
    when = datetime(2024, 4, 5, tzinfo=UTC)
    initial = _verify_history_row(route, agent.pk, when)
    initial.save()
    allocated, may_commit = Event(), Event()

    def pending_write():
        try:
            with transaction.atomic():
                pending = _verify_history_row(route, agent.pk, when + timedelta(seconds=1))
                pending.save()
                allocated.set()
                assert may_commit.wait(20)
            return pending.pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as writer:
        eventual = writer.submit(pending_write)
        try:
            assert allocated.wait(10)
            visible = initial
            if commit_a_higher_id:
                visible = _verify_history_row(route, agent.pk, when + timedelta(seconds=2))
                visible.save()
            page = client.get(reverse(f"audit_review:{route}"), {"person": agent.pk})
            assert page.status_code == 200
            marker = int(page.context["request"].GET["upto"])
            assert marker == visible.pk
            assert page.context["total"] == (2 if commit_a_higher_id else 1)
        finally:
            may_commit.set()
        pending_id = eventual.result(timeout=10)
    result = _verify_export_times(client.get(page.context["export_url"]))
    assert (pending_id <= marker) == commit_a_higher_id
    expected = (
        [when + timedelta(seconds=2), when + timedelta(seconds=1), when]
        if commit_a_higher_id
        else [when]
    )
    assert result == expected


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("route", ["changes", "changes-export"])
def test_change_committed_after_clock_read_but_visible_in_snapshot_waits_for_new_reading(
    client, reviewer, monkeypatch, route
):
    from audit_review import views

    client.force_login(reviewer)

    def record(reference):
        return AuditEvent.objects.create(
            actor_id=reviewer.pk,
            action="account.updated",
            object_type="accounts.user",
            object_id=str(reviewer.pk),
            request_ref=reference,
        )

    record("before-start")

    def committed_change():
        try:
            record("after-start")
        finally:
            connections.close_all()

    original = views.reading_account

    def pin_snapshot_after_commit(request):
        with ThreadPoolExecutor(max_workers=1) as writer:
            writer.submit(committed_change).result(timeout=10)
        assert AuditEvent.objects.filter(request_ref="after-start").exists()
        return original(request)

    with monkeypatch.context() as patch:
        patch.setattr(views, "reading_account", pin_snapshot_after_commit)
        response = client.get(reverse(f"audit_review:{route}"), {"action": "account.updated"})
    assert response.status_code == 200
    assert b"before-start" in response.content and b"after-start" not in response.content
    if route == "changes":
        frozen = client.get(response.context["export_url"])
        assert b"before-start" in frozen.content and b"after-start" not in frozen.content
    fresh = client.get(reverse(f"audit_review:{route}"), {"action": "account.updated"})
    assert fresh.status_code == 200 and b"after-start" in fresh.content


@pytest.mark.xfail(
    strict=True,
    reason="Documented limit: an account deleted outside the application can no longer be chosen "
    "in Person, so a page or export filtered by it is refused (docs/pages/audit_review).",
)
@pytest.mark.parametrize("route", ["access", "sign-ins", "uses"])
def test_person_filtered_export_keeps_retained_history_when_the_person_is_removed(
    client, reviewer, make_user, route
):
    subject = make_user("retained-account")
    when = datetime(2024, 2, 3, tzinfo=UTC)
    row = _verify_history_row(route, subject.pk, when)
    row.save()
    client.force_login(reviewer)
    page = client.get(reverse(f"audit_review:{route}"), {"person": subject.pk})
    assert page.status_code == 200 and page.context["total"] == 1
    assert _verify_export_times(client.get(page.context["export_url"])) == [when]
    # The documented out-of-band account removal leaves the historical row in place.
    subject.delete()
    assert type(row).objects.filter(pk=row.pk).exists()
    assert _verify_export_times(client.get(page.context["export_url"])) == [when]


@pytest.mark.parametrize("route", ["changes", "sign-ins"])
def test_frozen_organization_export_can_gain_an_older_member_event_only(
    client, reviewer, make_user, clinic, water, route
):
    from django.test import Client

    from desk.tests.test_account_move import admin_change

    resident = make_user("stable-client", client=clinic)
    moving = make_user("moving-client", client=water)
    admin = make_user("change-organization", "admin", is_superuser=True, is_staff=True)
    administration = Client()
    administration.force_login(admin)
    client.force_login(reviewer)
    action = "failed" if route == "sign-ins" else "review.fixture"

    def record(account, reference):
        if route == "sign-ins":
            return SignInEvent.objects.create(user=account, kind=action, factor="password")
        return AuditEvent.objects.create(
            actor=account,
            action=action,
            object_type="accounts.user",
            object_id=str(account.pk),
            request_ref=reference,
        )

    record(resident, "resident")
    record(moving, "moving-before-first-page")
    query = {"organization": clinic.pk, "action": action}
    page = client.get(reverse(f"audit_review:{route}"), query)
    assert page.status_code == 200 and page.context["total"] == 1

    record(moving, "moving-after-first-page")
    assert admin_change(administration, moving, client=clinic.pk).status_code == 302
    moving.refresh_from_db()
    assert moving.client_id == clinic.pk
    frozen = client.get(page.context["export_url"])
    assert frozen.status_code == 200
    rows = list(csv.DictReader(io.StringIO(frozen.content.decode("utf-8-sig"))))
    assert [row["Username"] for row in rows] == [moving.username, resident.username]
    assert client.get(reverse(f"audit_review:{route}"), query).context["total"] == 3


def test_changes_cutoff_includes_the_matching_microsecond_on_html_and_csv(
    client, reviewer, agent, monkeypatch
):
    from audit_review.sources import to_marker

    client.force_login(reviewer)
    pivot = datetime(2024, 8, 1, 12, 0, 0, 999998, tzinfo=UTC)
    stamps = [pivot + timedelta(microseconds=n) for n in (-1, 0, 1)]
    for index, stamp in enumerate(stamps):
        with monkeypatch.context() as fixed:
            fixed.setattr(timezone, "now", lambda moment=stamp: moment)
            AuditEvent.objects.create(
                actor=agent,
                object_type="accounts.user",
                object_id=str(agent.pk),
                action="review.clock",
                request_ref=f"change-{index}",
            )
    cutoff = to_marker(pivot)
    for supplied, count in [
        (cutoff - 1, 1),
        (cutoff, 2),
        (cutoff + 1, 3),
        (0, 0),
        (-1, 3),
        ("9" * 19, 3),
        ("9" * 40, 3),
    ]:
        page = client.get(
            reverse("audit_review:changes"), {"action": "review.clock", "upto": supplied}
        )
        assert page.status_code == 200
        expected = [f"change-{index}" for index in reversed(range(count))]
        assert [row.cells[-1].text for row in page.context["page_obj"]] == expected
        exported = client.get(page.context["export_url"])
        rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
        assert [row["Request ID"] for row in rows] == expected


# Row labels, CSV and local calendar dates


def _cell_exports(client, page):
    import csv
    import io

    assert page.status_code == 200
    result = client.get(page.context["export_url"])
    assert result.status_code == 200
    return list(csv.DictReader(io.StringIO(result.content.decode("utf-8-sig"), newline="")))


def test_internal_ticket_parts_keep_their_ticket_number_and_empty_organization(
    client, reviewer, agent, clinic, make_ticket
):
    from django.urls import reverse

    from attachments.inspection import inspect_bytes
    from attachments.services import add_entry_with_files, withdraw_attachment
    from core.audit import record_event
    from core.models import AccessRecord
    from desk.models import EntryKind
    from service_levels.models import RequestClock

    # Give the clock and its ticket different ids so a join on the clock's pk would be caught.
    external = make_ticket(clinic)
    internal = make_ticket(None)
    internal_clock, _ = RequestClock.objects.get_or_create(request=internal)
    external_clock, _ = RequestClock.objects.get_or_create(request=external)
    for clock in (internal_clock, external_clock):
        record_event(actor=agent, action="clock.breach_noticed", obj=clock)

    file_entry = add_entry_with_files(
        actor=agent,
        request=internal,
        kind=EntryKind.NOTE,
        expected_revision=internal.revision,
        body="Synthetic internal note",
        files=[inspect_bytes("fixture-original.txt", b"Example")],
    )
    attachment = file_entry.attachments.get()
    withdraw_attachment(actor=reviewer, attachment_id=attachment.pk, reason="Fixture withdrawal")
    AccessRecord.objects.create(
        account=agent,
        capacity="laboratory",
        kind="file",
        ticket_id=internal.pk,
        object_type=attachment._meta.label_lower,
        object_id=str(attachment.pk),
    )

    client.force_login(reviewer)
    changes = reverse("audit_review:changes")
    for ticket, expected_org in ((internal, ""), (external, clinic.name)):
        page = client.get(changes, {"action": "clock.breach_noticed", "ticket": ticket.number})
        rows = _cell_exports(client, page)
        assert [(row["Record"], row["Client organization"]) for row in rows] == [
            (f"Ticket clock on {ticket.number}", expected_org)
        ]
        [display] = page.context["page_obj"]
        assert (display.cells[1].text, display.cells[1].note) == (
            f"Ticket clock on {ticket.number}",
            expected_org,
        )
    organization = client.get(
        changes, {"action": "clock.breach_noticed", "organization": clinic.pk}
    )
    assert [row["Record"] for row in _cell_exports(client, organization)] == [
        f"Ticket clock on {external.number}"
    ]
    access = client.get(
        reverse("audit_review:access"), {"action": "file", "ticket": internal.number}
    )
    [file_row] = _cell_exports(client, access)
    assert (file_row["Ticket"], file_row["Client organization"], file_row["Detail"]) == (
        internal.number,
        "",
        attachment.download_name,
    )
    assert attachment.download_name.encode() in access.content
    assert attachment.original_name.encode() not in access.content
    assert attachment.original_name not in str(file_row)


def test_retained_sign_in_actor_cells_on_html_and_csv(client, reviewer, make_user):
    """Exercise a saved reading across a change in the availability of its account."""
    from django.urls import reverse

    from accounts.models import SignInEvent

    person = make_user("former-review-fixture", "technical", credentials_locked=True)
    SignInEvent.objects.create(user=person, kind="failed", factor="password")
    client.force_login(reviewer)
    address = reverse("audit_review:sign-ins")
    page = client.get(address, {"action": "failed"})
    [initial] = _cell_exports(client, page)
    assert initial["Browser"] == initial["Address"] == "Not shown: shared demonstration account"
    ident = person.pk
    person.delete()
    [after] = _cell_exports(client, page)
    assert after["Person"] == f"Account {ident}"
    assert after["Username"] == "no longer exists"
    assert after["Client organization"] == ""
    withheld = "Not shown: the account no longer exists"
    assert after["Address"] == after["Browser"] == withheld
    fresh = client.get(address, {"action": "failed"})
    [shown] = fresh.context["page_obj"]
    assert [cell.text for cell in shown.cells[1:4]] == [withheld, withheld, ""]
    table = fresh.content.decode().split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert table.count(withheld) == 2
    assert f"Account {ident}" in table and person.username not in table


def test_csv_preserves_layout_and_prefixes_inert_punctuation_after_whitespace(
    client, reviewer, agent, make_ticket
):
    from django.urls import reverse

    from core.models import AccessRecord

    ticket = make_ticket(None)
    refs = ["  ＠", "\u2003＋", "\t", "\r", "  office", 'north,"east"', "  Équipe"]
    AccessRecord.objects.bulk_create(
        [
            AccessRecord(
                account=agent,
                ticket_id=ticket.pk,
                kind="ticket",
                capacity="laboratory",
                request_ref=ref,
            )
            for ref in refs
        ]
    )
    client.force_login(reviewer)
    page = client.get(reverse("audit_review:access"), {"person": agent.pk})
    assert sorted(row.cells[-1].text for row in page.context["page_obj"]) == sorted(refs)
    assert sorted(row["Request ID"] for row in _cell_exports(client, page)) == sorted(
        ["'" + ref for ref in refs[:4]] + refs[4:]
    )


def test_open_date_bounds_and_last_day_reach_page_csv_and_use_history(client, reviewer, agent):
    from datetime import UTC, date, datetime

    from django.urls import reverse
    from django.utils import timezone

    from accounts.models import SignInEvent
    from audit_review.models import WorkspaceUse

    # Noon stays inside Python's datetime domain even after conversion to UTC+14.
    for day in (30, 31):
        SignInEvent.objects.create(
            user=agent,
            kind="failed",
            factor="password",
            occurred_at=datetime(9999, 12, day, 5, tzinfo=UTC),
        )
    client.force_login(reviewer)
    address = reverse("audit_review:sign-ins")
    with timezone.override("Pacific/Kiritimati"):
        for lower, upper, days in (
            (date.min, date.max, [31, 30]),
            (date.max, date.max, [31]),
            (date.min, date(9999, 12, 30), [30]),
        ):
            filters = {
                "person": str(agent.pk),
                "action": "failed",
                "date_from": str(lower),
                "date_to": str(upper),
            }
            page = client.get(address, filters)
            assert page.status_code == 200 and page.context["total"] == len(days)
            exported = _cell_exports(client, page)
            assert [datetime.fromisoformat(row["When"]).day for row in exported] == days
            use = WorkspaceUse.objects.get(request_ref=page["X-Request-ID"])
            assert use.filters == {**filters, "page": "1"}


def test_help_for_sign_in_time_matches_the_deadline_shown_on_a_late_expiry(client, reviewer, agent):
    from datetime import datetime, timedelta

    from django.urls import reverse
    from django.utils import timezone

    from accounts.models import AccountSession
    from accounts.sessions import close_expired

    now = timezone.now()
    earlier = now - timedelta(days=7)
    session = AccountSession.objects.create(user=agent, started_at=earlier, last_active_at=earlier)
    assert close_expired(agent) == 1
    session.refresh_from_db()
    assert session.ended_at < now - timedelta(days=5)
    client.force_login(reviewer)
    page = client.get(
        reverse("audit_review:sign-ins"), {"person": agent.pk, "action": "signed_out"}
    )
    [csv_row] = _cell_exports(client, page)
    [row] = page.context["page_obj"]
    assert row.occurred_at == session.ended_at
    assert datetime.fromisoformat(csv_row["When"]) == session.ended_at.replace(microsecond=0)
    document = page.context["page_documentation"]
    when = next(
        item["description"]
        for section in document["sections"]
        for item in section["items"]
        if item["label"] == "When"
    )
    incorrect = (
        "a sign-in event and a workspace use, by the database's clock when the record was written"
    )
    assert incorrect not in when


@pytest.mark.xfail(
    strict=True,
    reason="Documented limit (confirmation round): a day runs from its first local midnight to "
    "the next, so a repeated hour across midnight belongs to the later day (help, From and To).",
)
@pytest.mark.parametrize("day", ["2009-10-31", "2009-11-01"])
def test_a_day_filter_matches_the_dates_shown_across_a_midnight_rollback(
    client, reviewer, agent, day
):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from django.urls import reverse
    from django.utils import timezone

    from accounts.models import SignInEvent

    # St John's clock went from Nov 1 00:00:59 to Oct 31 23:01 before a second midnight.
    moments = [
        datetime.fromisoformat(s)
        for s in (
            "2009-11-01T02:29:00+00:00",
            "2009-11-01T02:30:30+00:00",
            "2009-11-01T02:31:00+00:00",
            "2009-11-01T03:30:00+00:00",
        )
    ]
    for moment in moments:
        SignInEvent.objects.create(user=agent, kind="failed", factor="password", occurred_at=moment)
    zone = ZoneInfo("America/St_Johns")
    expected = [t for t in reversed(moments) if t.astimezone(zone).date().isoformat() == day]
    client.force_login(reviewer)
    with timezone.override(zone):
        page = client.get(
            reverse("audit_review:sign-ins"),
            {"person": agent.pk, "action": "failed", "date_from": day, "date_to": day},
        )
        actual = [datetime.fromisoformat(row["When"]) for row in _cell_exports(client, page)]
        shown = [row.occurred_at for row in page.context["page_obj"]]
    assert (shown, actual) == (expected, expected)


# Remote selects and the option directory


@pytest.mark.django_db
@pytest.mark.parametrize("refused", ["unknown-number", "999999999", "-1"])
def test_rejected_client_scope_keeps_other_selected_organizations(refused):
    from clients.models import Client as Organization
    from request_forms.forms import TemplateForm

    existing = [
        Organization.objects.create(name="Northern Laboratory"),
        Organization.objects.create(name="Western Laboratory"),
    ]
    form = TemplateForm(data={"clients": [str(row.pk) for row in existing] + [refused]})
    assert form.has_error("clients")
    html = form["clients"].as_widget()
    assert 'aria-invalid="true"' in html
    for row in existing:
        assert f'value="{row.pk}" selected' in html
    assert f'value="{refused}"' not in html


@pytest.mark.django_db
@pytest.mark.parametrize("many", [False, True])
def test_remote_selection_preserves_text_keys_and_the_fields_own_choices(many):
    from django import forms

    from clients.models import Client as Organization
    from core.lookups import RemoteSelect, RemoteSelectMultiple

    supplier = Organization.objects.create(name="North", account_number="LAB-07", is_active=False)
    other = Organization.objects.create(name="South", account_number="LAB-08")
    field_type = forms.ModelMultipleChoiceField if many else forms.ModelChoiceField
    widget_type = RemoteSelectMultiple if many else RemoteSelect

    class Choice(forms.Form):
        account = field_type(
            queryset=Organization.objects.filter(pk=supplier.pk),
            to_field_name="account_number",
            widget=widget_type("active-clients"),
        )

    for item in (supplier, other):
        value = [item.account_number] if many else item.account_number
        choice = Choice(data={"account": value})
        assert choice.is_valid() == (item == supplier)
        html = choice["account"].as_widget()
        assert (f'value="{supplier.account_number}" selected' in html) == (item == supplier)
        assert other.account_number not in html


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("lookup_name", ["clients", "portal-contacts"])
def test_lookup_reads_requesting_account_in_the_options_snapshot(client, lookup_name):
    """Observe which reading supplied the account. All memberships stay fixed."""
    from django.contrib.auth.models import Group
    from django.db import connection
    from django.urls import reverse

    from accounts.models import User
    from clients.models import Client as Organization

    organization = Organization.objects.create(name="Example Directory Observer")
    account = User.objects.create_user(
        username="lookup-observer",
        email="observer@example.invalid",
        client=organization if lookup_name == "portal-contacts" else None,
    )
    if lookup_name == "clients":
        account.groups.add(Group.objects.get_or_create(name="technical")[0])
    client.force_login(account)
    before_snapshot = []
    in_snapshot = []
    reading = False

    def trace(execute, sql, params, many, context):
        nonlocal reading
        if "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ" in sql.upper():
            reading = True
        # No directory choice in either lookup selects any accounts. These SELECTs
        # load the active status and organization of the requesting account itself.
        if all(
            term in sql
            for term in (
                '"accounts_user"."is_active"',
                '"accounts_user"."client_id"',
                'FROM "accounts_user"',
            )
        ):
            (in_snapshot if reading and connection.in_atomic_block else before_snapshot).append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(trace):
        response = client.get(reverse("lookup", args=[lookup_name]), {"q": "absent suggestion"})
    assert response.status_code == 200
    assert response.json() == {"results": [], "more": False}
    assert in_snapshot, (
        "Requesting account status and organization came from outside the lookup snapshot "
        f"({len(before_snapshot)} earlier account reads)"
    )


@pytest.mark.django_db(transaction=True)
def test_staff_directory_check_and_option_observe_the_same_account_reading(rf, monkeypatch):
    import json
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace

    from django.contrib.auth.models import Group
    from django.db import connections
    from django.urls import reverse

    from accounts.models import User
    from core.lookups import LOOKUPS, lookup

    actor = User.objects.create_user("directory-reader", first_name="Earlier name")
    actor.groups.add(Group.objects.get_or_create(name="technical")[0])
    request = rf.get(reverse("lookup", args=["staff"]), {"q": actor.username})
    request.user = User.objects.get(pk=actor.pk)  # the account captured at authentication

    def rename():
        try:
            User.objects.filter(pk=actor.pk).update(first_name="Current name")
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as worker:
        worker.submit(rename).result(timeout=10)

    observed = []
    entry = LOOKUPS["staff"]

    def observe_reader(request):
        options = entry.queryset(request)
        observed.append(request.user.first_name)
        return options

    monkeypatch.setitem(LOOKUPS, "staff", replace(entry, queryset=observe_reader))
    response = lookup(request, "staff")
    [option] = json.loads(response.content)["results"]
    assert option["text"] == f"Current name ({actor.username})"
    assert observed == ["Current name"], "The option builder received the pre-reading account"


@pytest.mark.django_db
def test_lookup_handles_a_noninteger_page_as_its_first_page(client):
    from django.contrib.auth.models import Group
    from django.urls import reverse

    from accounts.models import User
    from clients.models import Client as Organization

    requester = User.objects.create_user("directory-pages")
    requester.groups.add(Group.objects.get_or_create(name="technical")[0])
    Organization.objects.create(name="Directory suggestion")
    client.force_login(requester)
    url = reverse("lookup", args=["clients"])
    ordinary = client.get(url, {"q": "Directory suggestion", "page": 1})
    assert ordinary.status_code == 200
    response = client.get(url, {"q": "Directory suggestion", "page": "first"})
    assert response.status_code == 200
    assert response.json() == ordinary.json()


@pytest.mark.django_db
def test_remote_fields_in_application_forms_redraw_a_refused_selection():
    from importlib import import_module

    from django import forms

    from core.lookups import RemoteSelect

    modules = [
        "clients.forms",
        "desk.forms",
        "desk.lists",
        "teams.forms",
        "nonconformance.forms",
        "request_forms.forms",
        "audit_review.forms",
    ]
    covered = set()
    for name in modules:
        try:
            module = import_module(name)
        except ModuleNotFoundError:
            if name == "audit_review.forms":
                continue  # the pre-workspace revision
            raise
        for form in vars(module).values():
            if (
                not isinstance(form, type)
                or form.__module__ != name
                or not issubclass(form, forms.BaseForm)
            ):
                continue
            for field_name, original in form.base_fields.items():
                if not isinstance(original.widget, RemoteSelect):
                    continue
                # Preserve the real validator, widget, lookup name, labels and queryset
                # while isolating each field from unrelated required inputs.
                holder = type("RemoteFieldForm", (forms.Form,), {field_name: original})
                for invalid in ("retired", "9999999999999999999999", "-1"):
                    many = original.widget.allow_multiple_selected
                    bound = holder({field_name: [invalid] if many else invalid})
                    assert bound.has_error(field_name), (name, form.__name__, field_name)
                    html = bound[field_name].as_widget()
                    assert 'aria-invalid="true"' in html
                    assert f'value="{invalid}"' not in html
                covered.add(original.widget.lookup)
    assert {
        "active-clients",
        "clients",
        "desk-sites",
        "desk-contacts",
        "role-holders",
        "staff",
    } <= covered


# Directory membership and pagination


@pytest.mark.django_db(transaction=True)
def test_portal_choices_keep_count_and_names_from_one_directory_reading(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from django.core.paginator import Paginator
    from django.db import connections
    from django.urls import reverse

    from accounts.models import User
    from clients.models import Client as Organization
    from clients.models import ClientContact

    client_org = Organization.objects.create(name="Synthetic contacts")
    portal_user = User.objects.create_user("directory-portal", client=client_org)
    contacts = [
        ClientContact.objects.create(
            client=client_org, name=f"Example {i:02}", phone=f"555-{i:04d}"
        )
        for i in range(20)
    ]
    client.force_login(portal_user)
    original = Paginator.page

    def record_contact():
        try:
            ClientContact.objects.create(
                client_id=client_org.pk, name="Example 00-A", phone="555-0020"
            )
        finally:
            connections.close_all()

    def page_during_update(paginator, number):
        page = original(paginator, number)
        with ThreadPoolExecutor(max_workers=1) as worker:
            worker.submit(record_contact).result(timeout=10)
        return page

    address = reverse("lookup", args=["portal-contacts"])
    with monkeypatch.context() as injection:
        injection.setattr(Paginator, "page", page_during_update)
        response = client.get(address, {"q": "Example"})
    assert response.status_code == 200
    assert response.json() == {
        "results": [{"value": str(c.pk), "text": c.name} for c in contacts],
        "more": False,
    }
    later = client.get(address, {"q": "Example"}).json()
    assert later["more"]
    assert later["results"][1]["text"] == "Example 00-A"


@pytest.mark.django_db
def test_each_registered_lookup_returns_its_scoped_option(client):
    from django.contrib.auth.models import Group, Permission
    from django.urls import reverse

    from accounts.models import User
    from clients.models import Client as Organization
    from clients.models import ClientContact, ClientSite
    from core.lookups import LOOKUPS

    org = Organization.objects.create(name="Verification Laboratory", account_number="VERIFY")
    site = ClientSite.objects.create(client=org, name="Verification Site")
    contact = ClientContact.objects.create(
        client=org, name="Verification Contact", phone="555-0123"
    )
    staff = User.objects.create_user("verificationstaff", email="staff@example.invalid")
    staff.groups.add(Group.objects.get_or_create(name="technical")[0])
    portal = User.objects.create_user(
        "verificationportal", email="portal@example.invalid", client=org
    )
    cases = {
        "clients": (org, org.name, org.name),
        "active-clients": (org, org.name, org.name),
        "desk-contacts": (contact, contact.name, f"{contact.name} · {org.name}"),
        "desk-sites": (site, site.name, f"{site.name} · {org.name}"),
        "role-holders": (staff, staff.username, staff.username),
        "staff": (staff, staff.username, staff.username),
        "portal-contacts": (contact, contact.name, contact.name),
    }
    if "review-accounts" in LOOKUPS:
        staff.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="accounts", codename="review_audit_records"
            )
        )
        cases["review-accounts"] = (portal, portal.username, f"{portal.username} · {org.name}")
    assert set(cases) == set(LOOKUPS)
    for route, (obj, query, label) in cases.items():
        client.force_login(portal if route == "portal-contacts" else staff)
        response = client.get(reverse("lookup", args=[route]), {"q": query})
        assert response.status_code == 200, route
        assert response.json() == {
            "results": [{"value": str(obj.pk), "text": label}],
            "more": False,
        }, route


# Reversing and reapplying the migration


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "legacy_model",
    [("auth", "group"), ("accounts", "accountsession")],
    ids=["different-app", "same-app-different-model"],
)
def test_rollback_and_reapply_retire_only_the_review_permission(legacy_model):
    from django.contrib.auth.models import Group, Permission
    from django.contrib.contenttypes.models import ContentType
    from django.core.management import call_command

    from accounts.models import User

    def migrate(*target):
        call_command("migrate", *target, verbosity=0, interactive=False)

    codename = "review_audit_records"
    review = Permission.objects.filter(
        content_type__app_label="accounts", content_type__model="user", codename=codename
    )
    account = User.objects.create_user("rollback-synthetic")
    groups = [Group.objects.create(name=name) for name in ("Review fixture A", "Review fixture B")]
    account.groups.add(*groups)
    holders = [account.user_permissions, *(group.permissions for group in groups)]
    previous = Permission.objects.get(
        content_type__app_label="accounts",
        content_type__model="user",
        codename="view_patient_information",
    )
    try:
        migrate("accounts", "0009_sign_in_history")
        # A deployment may already use this codename on a different model.
        legacy = Permission.objects.create(
            content_type=ContentType.objects.get_by_natural_key(*legacy_model),
            codename=codename,
            name="Synthetic legacy reporting permission",
        )
        keep = {previous.pk, legacy.pk}
        for holder in holders:
            holder.add(previous, legacy)

        migrate("accounts", "0010_review_capability")
        original = review.get()
        assert not original.user_set.exists() and not original.group_set.exists()
        for holder in holders:
            holder.add(original)

        migrate("accounts", "0009_sign_in_history")
        expected_removed = not review.exists()
        legacy_survived = Permission.objects.filter(pk=legacy.pk).exists()
        remaining_grants = [set(holder.values_list("pk", flat=True)) for holder in holders]
    finally:
        migrate()

    restored = review.get()
    assert expected_removed
    assert not restored.user_set.exists() and not restored.group_set.exists()
    assert set(account.groups.values_list("pk", flat=True)) == {group.pk for group in groups}
    assert legacy_survived, f"Rollback removed a permission on {'.'.join(legacy_model)}"
    assert remaining_grants == [keep] * len(holders)
    assert all(set(holder.values_list("pk", flat=True)) == keep for holder in holders)
