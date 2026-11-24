"""The audit review workspace: who may open it, what each list shows and leaves out, its filters,
its pager's one reading, its exports and the record of each use. All data is synthetic."""

import csv
import io
from datetime import datetime, timedelta

import pytest
from django.contrib.auth.models import Group
from django.db import DatabaseError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import SignInEvent
from audit_review import sources
from audit_review.models import Source, UseKind, WorkspaceUse
from core.models import AccessKind, AccessRecord, AuditEvent

from .conftest import review_permission

LISTS = ("changes", "access", "sign-ins", "uses")
SOURCES = {
    "changes": Source.CHANGES,
    "access": Source.ACCESS,
    "sign-ins": Source.SIGN_INS,
    "uses": Source.USES,
}
SNAPSHOT_TEXT = "Synthetic snapshot words that must stay on the record"
REASON_TEXT = "Synthetic reason words that must stay on the record"


def url(name, **query):
    from urllib.parse import urlencode

    address = reverse(f"audit_review:{name}")
    return f"{address}?{urlencode(query)}" if query else address


def uses(**filters):
    return list(WorkspaceUse.objects.filter(**filters).order_by("id"))


def event(actor, obj, action="request.updated", **extra):
    return AuditEvent.objects.create(
        actor=actor,
        action=action,
        object_type=obj._meta.label_lower,
        object_id=str(obj.pk),
        before={"subject": SNAPSHOT_TEXT},
        after={"subject": SNAPSHOT_TEXT},
        reason=REASON_TEXT,
        **extra,
    )


def read(account, ticket, kind=AccessKind.TICKET, **extra):
    return AccessRecord.objects.create(
        account_id=account.pk,
        capacity="client" if account.client_id else "laboratory",
        kind=kind,
        ticket_id=ticket.pk,
        **extra,
    )


def rows_of(response):
    return [row for row in response.context["page_obj"]]


def export_rows(response):
    text = response.content.decode("utf-8")
    assert text.startswith("\ufeff")
    return list(csv.reader(io.StringIO(text[1:])))


# ------------------------------------------------------------------------------ who may open it


@pytest.mark.parametrize("name", LISTS)
def test_a_reviewer_opens_every_list_and_its_use_is_recorded(client, reviewer, name):
    client.force_login(reviewer)
    response = client.get(url(name))
    assert response.status_code == 200
    [use] = uses()
    assert use.account_id == reviewer.pk
    assert use.kind == UseKind.VIEWED and use.source == SOURCES[name]
    assert use.filters == {"page": "1"}
    assert use.request_ref == response["X-Request-ID"]


@pytest.mark.parametrize("name", [*LISTS, *(f"{name}-export" for name in LISTS)])
@pytest.mark.parametrize(
    "who", ["supervisor", "admin", "superuser", "roleless", "portal", "inactive_role"]
)
def test_everyone_without_the_capability_and_a_role_is_refused_and_nothing_is_recorded(
    client, make_user, clinic, name, who
):
    if who == "supervisor":
        account = make_user("sup", "supervisor")
    elif who == "admin":
        account = make_user("adm", "admin")
    elif who == "superuser":
        account = make_user("root", is_staff=True, is_superuser=True)
    elif who == "roleless":
        account = make_user("nobody")
        account.user_permissions.add(review_permission())  # a grant without a role opens nothing
    elif who == "portal":
        account = make_user("portal", client=clinic)
        account.user_permissions.add(review_permission())  # never for a portal account
    else:
        account = make_user("former", "supervisor")
        account.user_permissions.add(review_permission())
        Group.objects.get(name="supervisor").user_set.remove(account)
    client.force_login(account)
    assert client.get(url(name)).status_code == 403
    assert not WorkspaceUse.objects.exists()


def test_a_group_grant_opens_the_workspace_and_its_removal_closes_it(client, make_user):
    account = make_user("grouped", "technical")
    group = Group.objects.get(name="technical")
    group.permissions.add(review_permission())
    client.force_login(account)
    assert client.get(url("changes")).status_code == 200
    group.permissions.remove(review_permission())
    assert client.get(url("changes")).status_code == 403


def test_anonymous_requests_are_sent_to_sign_in_and_record_nothing(client, db):
    for name in (*LISTS, "changes-export"):
        response = client.get(url(name))
        assert response.status_code == 302 and "/accounts/login/" in response["Location"]
    assert not WorkspaceUse.objects.exists()


@pytest.mark.parametrize("method", ["head", "post", "put", "delete", "options"])
def test_other_methods_are_refused_and_record_nothing(client, reviewer, method):
    client.force_login(reviewer)
    for name in ("changes", "changes-export"):
        assert getattr(client, method)(url(name)).status_code == 405
    assert not WorkspaceUse.objects.exists()


def test_the_person_lookup_is_for_reviewers_only(client, reviewer, supervisor, clinic, make_user):
    make_user("portal-person", client=clinic, first_name="Paula")
    lookup = reverse("lookup", args=["review-accounts"])
    client.force_login(supervisor)
    assert client.get(lookup).status_code == 403
    client.force_login(reviewer)
    found = client.get(lookup, {"q": "Paula"}).json()["results"]
    assert [row["text"] for row in found] == ["Paula (portal-person) · Example Clinic"]


# ------------------------------------------------------------------------------ what rows show


def test_changes_list_who_and_which_record_but_never_the_snapshots_or_reason(
    client, reviewer, agent, clinic, make_ticket
):
    ticket = make_ticket(clinic)
    event(agent, ticket, request_ref="a" * 32)
    event(agent, clinic, action="client.updated")
    client.force_login(reviewer)
    response = client.get(url("changes"))
    body = response.content.decode()
    assert SNAPSHOT_TEXT not in body and REASON_TEXT not in body
    assert ticket.subject not in body
    texts = [[cell.text for cell in row.cells] for row in rows_of(response)]
    assert ["Client updated", f"Client organization {clinic.pk}", "Not recorded"] in texts
    assert ["Ticket updated", ticket.number, "a" * 32] in texts
    export = client.get(url("changes-export"))
    content = export.content.decode()
    assert SNAPSHOT_TEXT not in content and REASON_TEXT not in content
    assert ticket.subject not in content


def test_access_records_name_the_file_by_its_download_name_and_never_another_tickets(
    client, reviewer, agent, clinic, make_ticket
):
    from attachments.models import Attachment

    ticket, other = make_ticket(clinic), make_ticket(clinic, "Other subject")
    read(agent, ticket)
    read(agent, ticket, AccessKind.FILE, object_type="attachments.attachment", object_id="999")
    client.force_login(reviewer)
    response = client.get(url("access"))
    cells = [[(cell.text, cell.note) for cell in row.cells] for row in rows_of(response)]
    assert cells[0][0] == ("Downloaded a file", "File 999")
    assert cells[0][2] == (ticket.number, "Example Clinic")
    assert cells[1][0] == ("Opened the ticket", "")
    assert other.number not in response.content.decode()
    assert not Attachment.objects.exists()


def test_sign_ins_show_browser_and_address_except_for_shared_demonstration_accounts(
    client, reviewer, make_user
):
    plain = make_user("plain", "technical")
    shared = make_user("shared", "technical", credentials_locked=True)
    agent_text = "Mozilla/5.0 (Macintosh) Firefox/140.0"
    for account in (plain, shared):
        SignInEvent.objects.create(
            user=account,
            kind="failed",
            factor="password",
            user_agent=agent_text,
            address="192.0.2.10",
        )
    client.force_login(reviewer)
    response = client.get(url("sign-ins", action="failed"))
    by_person = {row.username: [cell.text for cell in row.cells] for row in rows_of(response)}
    assert by_person["plain"][2] == "192.0.2.10"
    assert by_person["shared"][1:3] == [sources.NOT_SHOWN, sources.NOT_SHOWN]
    content = client.get(url("sign-ins-export", action="failed")).content.decode()
    assert content.count("192.0.2.10") == 1


def test_a_changed_account_is_named_by_its_username(client, reviewer, agent):
    event(agent, reviewer, action="account.updated")
    client.force_login(reviewer)
    [row] = rows_of(client.get(url("changes", action="account.updated")))
    assert row.cells[1].text == "Account reviewer"


def test_a_record_of_an_account_that_no_longer_exists_shows_its_number(
    client, reviewer, clinic, make_ticket
):
    ticket = make_ticket(clinic)
    AccessRecord.objects.create(
        account_id=987654, capacity="laboratory", kind="ticket", ticket_id=ticket.pk
    )
    client.force_login(reviewer)
    [row] = rows_of(client.get(url("access")))
    assert (row.person, row.username) == ("Account 987654", "no longer exists")


# ------------------------------------------------------------------------------ filters


def test_filters_by_person_ticket_organization_action_and_dates(
    client, reviewer, agent, supervisor, clinic, water, make_ticket
):
    clinic_ticket, water_ticket = make_ticket(clinic), make_ticket(water)
    read(agent, clinic_ticket)
    read(supervisor, water_ticket, AccessKind.HISTORY)
    client.force_login(reviewer)

    def tickets(**query):
        return [row.cells[2].text for row in rows_of(client.get(url("access", **query)))]

    assert tickets(person=agent.pk) == [clinic_ticket.number]
    assert tickets(organization=water.pk) == [water_ticket.number]
    assert tickets(ticket=water_ticket.number) == [water_ticket.number]
    assert tickets(ticket=str(clinic_ticket.pk)) == [clinic_ticket.number]
    assert tickets(action="history") == [water_ticket.number]
    today = timezone.localdate()
    assert len(tickets(date_from=today, date_to=today)) == 2
    assert tickets(date_to=today - timedelta(days=1)) == []
    [*_, last] = uses()
    assert last.filters == {"date_to": str(today - timedelta(days=1)), "page": "1"}


def test_a_ticket_filter_on_changes_takes_its_parts_and_no_other_ticket(
    client, reviewer, agent, clinic, make_ticket
):
    ticket, other = make_ticket(clinic), make_ticket(clinic, "Other")
    entry = ticket.entries.first()
    event(agent, entry, action="entry.added")
    event(agent, other)
    client.force_login(reviewer)
    rows = rows_of(client.get(url("changes", ticket=ticket.number)))
    records = {row.cells[1].text for row in rows}
    assert records == {ticket.number, f"Ticket entry on {ticket.number}"}


def test_an_organization_filter_on_changes_takes_the_organization_and_its_tickets(
    client, reviewer, agent, clinic, water, make_ticket
):
    make_ticket(clinic)
    make_ticket(water)
    event(agent, clinic, action="client.updated")
    event(agent, water, action="client.updated")
    client.force_login(reviewer)
    rows = rows_of(client.get(url("changes", organization=clinic.pk)))
    assert {row.cells[1].note for row in rows} == {"Example Clinic"}
    assert len(rows) >= 2  # the ticket's creation and the organization's change


def test_sign_ins_filter_by_a_portal_organization(client, reviewer, make_user, clinic, water):
    one, two = make_user("p1", client=clinic), make_user("p2", client=water)
    for account in (one, two):
        SignInEvent.objects.create(user=account, kind="failed", factor="password")
    client.force_login(reviewer)
    rows = rows_of(client.get(url("sign-ins", organization=clinic.pk, action="failed")))
    assert [row.username for row in rows] == ["p1"]
    assert rows[0].cells[3].text == "Example Clinic"


@pytest.mark.parametrize(
    "query",
    [
        {"ticket": "Jane Doe 1970-01-01"},
        {"ticket": "REQ-999999"},
        {"person": "999999"},
        {"date_from": "2026-11-20", "date_to": "2026-11-01"},
        {"action": "no_such_action"},
    ],
)
def test_an_invalid_filter_lists_nothing_and_records_no_typed_text(client, reviewer, query):
    client.force_login(reviewer)
    response = client.get(url("access", **query))
    assert response.status_code == 200
    assert response.context["page_obj"] == []
    [use] = uses()
    assert use.filters == {"invalid": True} and use.rows == 0
    assert "Jane" not in str(use.filters)


def test_a_filter_a_list_does_not_have_is_ignored(client, reviewer):
    client.force_login(reviewer)
    assert client.get(url("uses", ticket="anything", organization="1")).status_code == 200
    [use] = uses()
    assert use.filters == {"page": "1"}


def test_dates_are_days_in_the_deployment_time_zone(client, reviewer, agent, clinic, make_ticket):
    ticket = make_ticket(clinic)
    record = read(agent, ticket)
    local = timezone.localtime(record.occurred_at)
    client.force_login(reviewer)
    day = local.date()
    assert len(rows_of(client.get(url("access", date_from=day, date_to=day)))) == 1
    assert rows_of(client.get(url("access", date_from=day + timedelta(days=1)))) == []


# ------------------------------------------------------------------------------ one reading


def test_later_records_never_move_a_row_to_the_next_page(
    client, reviewer, agent, clinic, make_ticket
):
    ticket = make_ticket(clinic)
    for _ in range(sources.PAGE_SIZE + 5):
        read(agent, ticket)
    client.force_login(reviewer)
    first = client.get(url("access"))
    upto = first.context["request"].GET["upto"]
    second_before = [
        row.occurred_at for row in rows_of(client.get(url("access", page=2, upto=upto)))
    ]
    for _ in range(3):
        read(agent, ticket)
    second = client.get(url("access", page=2, upto=upto))
    assert [row.occurred_at for row in rows_of(second)] == second_before
    assert len(second_before) == 5
    assert f"upto={upto}" in second.content.decode()


def test_an_upto_in_the_future_or_malformed_is_read_as_now(
    client, reviewer, agent, clinic, make_ticket
):
    ticket = make_ticket(clinic)
    read(agent, ticket)
    client.force_login(reviewer)
    for upto in ("9" * 18, "not-a-number", "-5", "1" * 40):
        assert len(rows_of(client.get(url("access", upto=upto)))) == 1, upto
    assert rows_of(client.get(url("access", upto=0))) == []
    old = sources.to_marker(timezone.now() - timedelta(days=1))
    event(agent, ticket)
    assert rows_of(client.get(url("changes", upto=old))) == []


def test_the_export_uses_the_reading_of_the_page_it_was_offered_on(
    client, reviewer, agent, clinic, make_ticket
):
    ticket = make_ticket(clinic)
    read(agent, ticket)
    client.force_login(reviewer)
    page = client.get(url("access"))
    export_url = page.context["export_url"]
    read(agent, ticket, AccessKind.HISTORY)
    rows = export_rows(client.get(export_url))
    assert len(rows) == 2  # the header and the one record of the page's reading


# ------------------------------------------------------------------------------ exports


def test_an_export_holds_every_matching_row_and_is_recorded(
    client, reviewer, agent, clinic, make_ticket
):
    ticket = make_ticket(clinic)
    for _ in range(sources.PAGE_SIZE + 2):
        read(agent, ticket)
    client.force_login(reviewer)
    response = client.get(url("access-export", person=agent.pk))
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"].startswith('attachment; filename="audit-review-access-')
    assert "no-store" in response["Cache-Control"]
    rows = export_rows(response)
    assert rows[0] == [
        "When",
        "Person",
        "Username",
        "What",
        "Detail",
        "Capacity",
        "Ticket",
        "Client organization",
        "Request ID",
    ]
    assert len(rows) == sources.PAGE_SIZE + 3
    [use] = uses()
    assert use.kind == UseKind.EXPORTED and use.source == Source.ACCESS
    assert use.rows == sources.PAGE_SIZE + 2
    assert use.filters == {"person": str(agent.pk)}


def test_cells_a_spreadsheet_would_run_as_formulas_are_written_as_text(
    client, reviewer, make_user, clinic, make_ticket
):
    sly = make_user("-sly", "technical", first_name='=HYPERLINK("http://example.invalid")')
    read(sly, make_ticket(clinic))
    client.force_login(reviewer)
    [_, row] = export_rows(client.get(url("access-export")))
    assert row[1] == '\'=HYPERLINK("http://example.invalid")'
    assert row[2] == "'-sly"


@pytest.mark.parametrize("text", [" =1+1", "＝1+1", "+1", "@SUM(A1)", "\t=1", "plain"])
def test_the_formula_guard(text):
    from audit_review.views import _cell

    assert _cell(text) == (text if text == "plain" else "'" + text)


def test_a_result_larger_than_an_export_holds_is_refused_and_records_nothing(
    client, reviewer, agent, clinic, make_ticket, monkeypatch
):
    from audit_review import views

    monkeypatch.setattr(views, "EXPORT_LIMIT", 2)
    ticket = make_ticket(clinic)
    for _ in range(3):
        read(agent, ticket)
    client.force_login(reviewer)
    response = client.get(url("access-export"))
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("audit_review:access") + "?")
    assert not WorkspaceUse.objects.exists()
    page = client.get(response["Location"])
    assert "An export holds at most 2 rows" in page.content.decode()


def test_an_invalid_filter_export_returns_to_the_list_and_records_nothing(client, reviewer):
    client.force_login(reviewer)
    response = client.get(url("access-export", ticket="REQ-0"))
    assert response.status_code == 302
    assert not WorkspaceUse.objects.exists()


# ------------------------------------------------------------------------------ the record of use


def test_a_page_whose_use_cannot_be_recorded_is_not_sent(client, reviewer, monkeypatch):
    def refuse(*args, **kwargs):
        raise DatabaseError("synthetic write failure")

    monkeypatch.setattr(WorkspaceUse.objects, "bulk_create", refuse)
    client.raise_request_exception = False
    client.force_login(reviewer)
    for name in ("changes", "changes-export"):
        response = client.get(url(name))
        assert response.status_code == 500
        assert b"Audit review" not in response.content and b"When," not in response.content


def test_the_uses_list_describes_filters_by_name(client, reviewer, agent, clinic, make_ticket):
    ticket = make_ticket(clinic)
    client.force_login(reviewer)
    client.get(url("access", person=agent.pk, ticket=ticket.number, organization=clinic.pk))
    client.get(url("access-export", action="file"))
    rows = rows_of(client.get(url("uses")))
    described = [(row.cells[0].text, row.cells[0].note, row.cells[1].text) for row in rows]
    assert described == [
        ("Exported", "Access records", "Action: Downloaded a file"),
        (
            "Viewed a page",
            "Access records",
            f"Person: Ada Agent; Organization: Example Clinic; Ticket: {ticket.number}",
        ),
    ]


def test_uses_are_append_only(reviewer):
    use = WorkspaceUse.objects.create(
        account_id=reviewer.pk, kind="viewed", source="changes", rows=0
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        WorkspaceUse.objects.filter(pk=use.pk).update(rows=5)
    with pytest.raises(DatabaseError), transaction.atomic():
        WorkspaceUse.objects.filter(pk=use.pk).delete()
    assert WorkspaceUse.objects.get(pk=use.pk).rows == 0


def test_the_database_refuses_an_unknown_kind_or_list(reviewer):
    for values in ({"kind": "erased", "source": "changes"}, {"kind": "viewed", "source": "x"}):
        with pytest.raises(DatabaseError), transaction.atomic():
            WorkspaceUse.objects.create(account_id=reviewer.pk, rows=0, **values)


# ------------------------------------------------------------------------------ cost


@pytest.mark.parametrize("name", LISTS)
def test_a_page_of_more_rows_than_fit_keeps_a_query_budget(
    client, reviewer, agent, supervisor, clinic, make_ticket, name
):
    tickets = [make_ticket(clinic, f"Subject {n}") for n in range(4)]
    for n in range(sources.PAGE_SIZE + 10):
        ticket = tickets[n % 4]
        read(agent if n % 2 else supervisor, ticket)
        event(agent, ticket)
        SignInEvent.objects.create(user=agent, kind="failed", factor="password")
        WorkspaceUse.objects.create(
            account_id=reviewer.pk,
            kind="viewed",
            source="access",
            rows=n,
            filters={"person": str(agent.pk), "ticket": str(ticket.pk)},
        )
    client.force_login(reviewer)
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url(name))
    assert response.status_code == 200
    assert len(rows_of(response)) == sources.PAGE_SIZE
    assert len(queries) <= 40, [query["sql"][:120] for query in queries]


def test_a_sign_out_written_later_with_an_earlier_time_stays_out_of_an_earlier_reading(
    client, reviewer, make_user
):
    from datetime import timedelta as delta

    person = make_user("limited", "technical")
    SignInEvent.objects.create(user=person, kind="failed", factor="password")
    client.force_login(reviewer)
    first = client.get(url("sign-ins", person=person.pk))
    upto = first.context["request"].GET["upto"]
    SignInEvent.objects.create(
        user=person,
        kind="signed_out",
        ending="inactivity",
        occurred_at=timezone.now() - delta(hours=1),
    )
    again = client.get(url("sign-ins", person=person.pk, upto=upto))
    assert [row.cells[0].text for row in rows_of(again)] == ["Sign-in failed"]
    export = export_rows(client.get(first.context["export_url"]))
    assert [line[3] for line in export[1:]] == ["Sign-in failed"]


def test_markers_round_trip():
    moment = datetime(2026, 11, 24, 12, 30, 1, 123456, tzinfo=timezone.get_current_timezone())
    assert sources.from_marker(sources.to_marker(moment)) == moment
