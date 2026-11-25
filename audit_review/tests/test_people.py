"""The People tab of the audit review workspace: who may open it, what it counts for each person
over a period, business hours, the lists its counts open, its one reading, its export and the
record of each use; and the filters the lists gained for it. All data is synthetic."""

import csv
import io
from datetime import date, datetime, time, timedelta
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import SignInEvent
from audit_review import people
from audit_review.models import Source, UseKind, WorkspaceUse
from core.models import AccessKind, AccessRecord, AuditEvent
from service_levels.models import BusinessCalendar, Holiday

from .conftest import review_permission

WEEKDAYS_9_TO_5 = {day: [["09:00", "17:00"]] for day in ("mon", "tue", "wed", "thu", "fri")}


def url(name="people", **query):
    address = reverse(f"audit_review:{name}")
    return f"{address}?{urlencode(query)}" if query else address


def at(day, hour, minute=0):
    """A moment of `day` by the deployment's clock."""
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


def a_monday(weeks_ago=1):
    today = timezone.localdate()
    return today - timedelta(days=today.weekday() + 7 * weeks_ago)


def sign_in(account, moment, kind=SignInEvent.Kind.SIGNED_IN):
    return SignInEvent.objects.create(user_id=account.pk, kind=kind, occurred_at=moment)


def read(account, ticket, moment, kind=AccessKind.TICKET):
    targets = {
        AccessKind.FILE: ("attachments.attachment", "1"),
        AccessKind.PATIENT_ANSWER: ("request_forms.fieldvalue", "1"),
        AccessKind.PERSON_KEY: ("links.linkattachment", "1"),
    }
    object_type, object_id = targets.get(kind, ("", ""))
    return AccessRecord.objects.create(
        account_id=account.pk,
        capacity="client" if account.client_id else "laboratory",
        kind=kind,
        ticket_id=ticket.pk,
        object_type=object_type,
        object_id=object_id,
        occurred_at=moment,
    )


def change(actor, obj, moment):
    with mock.patch("django.utils.timezone.now", return_value=moment):
        return AuditEvent.objects.create(
            actor=actor,
            action="request.updated",
            object_type=obj._meta.label_lower,
            object_id=str(obj.pk),
        )


def person_of(response, account):
    for person in response.context["page_obj"]:
        if person.account_id == account.pk:
            return person
    return None


def counts(response, account):
    person = person_of(response, account)
    return None if person is None else dict(person.counts)


def export_rows(response):
    text = response.content.decode("utf-8")
    assert text.startswith("﻿")
    return list(csv.reader(io.StringIO(text[1:])))


@pytest.fixture
def calendar(db):
    return BusinessCalendar.objects.create(name="Weekday hours", hours=WEEKDAYS_9_TO_5)


@pytest.fixture
def portal_user(make_user, clinic):
    return make_user("clinic_portal", client=clinic, first_name="Pat", last_name="Portal")


# ------------------------------------------------------------------------------ who may open it


def test_a_reviewer_opens_the_tab_and_its_use_records_the_period_applied(
    client, reviewer, calendar
):
    client.force_login(reviewer)
    response = client.get(url())
    assert response.status_code == 200
    assert response.context["page_title"] == "Audit review: people"
    [use] = WorkspaceUse.objects.all()
    today = timezone.localdate()
    assert (use.kind, use.source, use.account_id) == (UseKind.VIEWED, Source.PEOPLE, reviewer.pk)
    assert use.filters == {
        "date_from": (today - timedelta(days=29)).isoformat(),
        "date_to": today.isoformat(),
        "calendar": str(calendar.pk),
        "page": "1",
    }


@pytest.mark.parametrize("name", ["people", "people-export"])
def test_everyone_without_the_capability_and_a_role_is_refused_and_nothing_is_recorded(
    client, make_user, clinic, name
):
    granted_without_role = make_user("granted_only")
    granted_without_role.user_permissions.add(review_permission())
    portal_with_grant = make_user("portal_grant", client=clinic)
    portal_with_grant.user_permissions.add(review_permission())
    admin = make_user("admin_role", "admin", is_superuser=True, is_staff=True)
    for account in (
        make_user("plain_supervisor", "supervisor"),
        granted_without_role,
        portal_with_grant,
        admin,
    ):
        client.force_login(account)
        assert client.get(url(name)).status_code == 403, account.username
    assert not WorkspaceUse.objects.exists()


def test_anonymous_requests_are_sent_to_sign_in_and_record_nothing(client, db):
    for name in ("people", "people-export"):
        response = client.get(url(name))
        assert response.status_code == 302 and "/accounts/login/" in response["Location"]
    assert not WorkspaceUse.objects.exists()


def test_a_grant_removed_from_the_group_closes_the_tab(client, make_user):
    holder = make_user("group_holder", "supervisor")
    group = Group.objects.create(name="Audit reviewers")
    group.permissions.add(review_permission())
    holder.groups.add(group)
    client.force_login(holder)
    assert client.get(url()).status_code == 200
    group.permissions.clear()
    assert client.get(url()).status_code == 403


def test_other_methods_are_refused_and_record_nothing(client, reviewer):
    client.force_login(reviewer)
    assert client.post(url()).status_code == 405
    assert client.head(url()).status_code == 405
    assert client.post(url("people-export")).status_code == 405
    assert not WorkspaceUse.objects.exists()


# ------------------------------------------------------------------------------ what it counts


def test_each_count_holds_its_kinds_and_leaves_out_the_rest(
    client, reviewer, agent, portal_user, clinic, make_ticket, calendar
):
    ticket = make_ticket(clinic)
    day = timezone.localdate() - timedelta(days=2)
    moment = at(day, 12)
    for kind in (
        SignInEvent.Kind.SIGNED_IN,
        SignInEvent.Kind.SIGNED_IN,
        SignInEvent.Kind.FAILED,
        SignInEvent.Kind.LOCKED_OUT,
        SignInEvent.Kind.REFUSED,
        SignInEvent.Kind.REAUTHENTICATION_FAILED,
        SignInEvent.Kind.SIGNED_OUT,  # not counted
    ):
        sign_in(agent, moment, kind)
    for kind in (
        AccessKind.TICKET,
        AccessKind.TICKET,
        AccessKind.FILE,
        AccessKind.PATIENT_ANSWER,
        AccessKind.PERSON_KEY,
        AccessKind.HISTORY,  # not counted
        AccessKind.TIMELINE,  # not counted
    ):
        read(agent, ticket, moment, kind)
    change(agent, ticket, moment)
    read(portal_user, ticket, moment)
    client.force_login(reviewer)
    response = client.get(url(calendar="none"))
    assert counts(response, agent) == {
        "sign_ins": 2,
        "failed": 4,
        "tickets": 2,
        "downloads": 1,
        "shown": 2,
        # this change and those the agent made logging the ticket (make_ticket)
        "changes": AuditEvent.objects.filter(actor=agent).count(),
    }
    assert counts(response, portal_user)["tickets"] == 1
    assert person_of(response, portal_user).organization == "Example Clinic"
    assert not response.context["summary"].judged
    assert person_of(response, agent).outside is None


def test_someone_with_nothing_counted_in_the_period_is_not_listed(
    client, reviewer, agent, supervisor, clinic, make_ticket
):
    ticket = make_ticket(clinic)
    read(supervisor, ticket, at(timezone.localdate() - timedelta(days=1), 10), AccessKind.HISTORY)
    client.force_login(reviewer)
    response = client.get(url())
    assert person_of(response, supervisor) is None
    assert person_of(response, agent) is not None  # created the ticket


def test_the_period_defaults_to_the_last_30_days_and_its_days_are_local(
    client, reviewer, agent, clinic, make_ticket
):
    ticket = make_ticket(clinic)
    today = timezone.localdate()
    read(agent, ticket, at(today - timedelta(days=29), 0, 0))  # first moment of the period
    read(agent, ticket, at(today - timedelta(days=30), 23, 59))  # the day before
    client.force_login(reviewer)
    response = client.get(url(calendar="none"))
    assert counts(response, agent)["tickets"] == 1
    period = response.context["period"]
    assert (period["date_from"], period["date_to"]) == (today - timedelta(days=29), today)
    assert response.context["active_filters"] == []  # defaults make no chip
    day = today - timedelta(days=30)
    response = client.get(url(date_from=day, date_to=day, calendar="none"))
    assert counts(response, agent)["tickets"] == 1


def test_empty_to_is_today_and_empty_from_is_30_days_before_to(client, reviewer):
    client.force_login(reviewer)
    today = timezone.localdate()
    response = client.get(url(date_to=date(2026, 3, 31)))
    period = response.context["period"]
    assert (period["date_from"], period["date_to"]) == (date(2026, 3, 2), date(2026, 3, 31))
    response = client.get(url(date_from=today - timedelta(days=3)))
    period = response.context["period"]
    assert (period["date_from"], period["date_to"]) == (today - timedelta(days=3), today)
    future = today + timedelta(days=10)
    response = client.get(url(date_from=future))
    assert response.context["period"]["date_to"] == future


@pytest.mark.parametrize(
    "query",
    [
        {"date_from": "2026-05-02", "date_to": "2026-05-01"},
        {"date_from": "2025-01-01", "date_to": "2026-01-02"},  # 367 days
        {"date_from": "not a date"},
        {"calendar": "999999"},
        {"activity": "outside", "calendar": "none"},
        {"person": "999999"},
    ],
)
def test_an_invalid_filter_counts_nothing_and_records_no_typed_text(
    client, reviewer, calendar, query
):
    client.force_login(reviewer)
    response = client.get(url(**query))
    assert response.status_code == 200
    assert response.context["filter_form"].errors
    assert response.context["page_obj"] == []
    assert WorkspaceUse.objects.get().filters == {"invalid": True}
    export = client.get(url("people-export", **query))
    assert export.status_code == 302
    assert WorkspaceUse.objects.count() == 1


def test_a_full_year_is_accepted(client, reviewer):
    client.force_login(reviewer)
    response = client.get(url(date_from="2024-01-01", date_to="2024-12-31"))  # 366 days
    assert not response.context["filter_form"].errors


def test_an_organization_filter_takes_its_portal_accounts_only(
    client, reviewer, agent, portal_user, make_user, water, clinic, make_ticket
):
    other = make_user("water_portal", client=water)
    ticket = make_ticket(clinic)
    moment = at(timezone.localdate(), 0, 1)
    read(portal_user, ticket, moment)
    read(other, ticket, moment)
    client.force_login(reviewer)
    response = client.get(url(organization=clinic.pk))
    listed = [person.account_id for person in response.context["page_obj"]]
    assert listed == [portal_user.pk]


def test_a_person_filter_takes_that_account_only(
    client, reviewer, agent, supervisor, clinic, make_ticket
):
    ticket = make_ticket(clinic)
    read(supervisor, ticket, at(timezone.localdate(), 0, 1))
    client.force_login(reviewer)
    response = client.get(url(person=supervisor.pk))
    assert [person.account_id for person in response.context["page_obj"]] == [supervisor.pk]


def test_an_account_that_no_longer_exists_is_counted_by_its_number_and_opens_nothing(
    client, reviewer
):
    sign_in_moment = at(timezone.localdate(), 0, 1)
    SignInEvent.objects.create(user_id=987654, kind="failed", occurred_at=sign_in_moment)
    client.force_login(reviewer)
    response = client.get(url())
    [gone] = [person for person in response.context["page_obj"] if person.account_id == 987654]
    assert (gone.name, gone.username, gone.exists) == ("Account 987654", "no longer exists", False)
    assert gone.counts["failed"] == 1
    assert all(not cell.url for cell in gone.cells)


# ------------------------------------------------------------------------------ business hours


@pytest.fixture
def week(agent, clinic, make_ticket, calendar):
    """Reads by the agent around a weekday calendar of 09:00-17:00, a holiday on Tuesday."""
    monday = a_monday()
    Holiday.objects.create(calendar=calendar, date=monday + timedelta(days=1), name="Holiday")
    ticket = make_ticket(clinic)
    inside = [at(monday, 9, 0), at(monday, 16, 59), at(monday + timedelta(days=2), 12)]
    outside = [
        at(monday, 8, 59),
        at(monday, 17, 0),  # closing time is outside
        at(monday + timedelta(days=1), 12),  # the holiday
        at(monday + timedelta(days=5), 12),  # Saturday
    ]
    for moment in inside + outside:
        read(agent, ticket, moment)
    return {"monday": monday, "ticket": ticket, "inside": inside, "outside": outside}


def test_activity_outside_a_calendars_hours_counts_weekends_holidays_and_closing_time(
    client, reviewer, agent, week
):
    monday = week["monday"]
    client.force_login(reviewer)
    query = {"date_from": monday, "date_to": monday + timedelta(days=6)}
    response = client.get(url(**query))
    person = person_of(response, agent)
    assert response.context["summary"].judged
    assert person.counts["tickets"] == 7
    assert person.outside == 4
    response = client.get(url(activity="outside", **query))
    person = person_of(response, agent)
    assert person.counts["tickets"] == 4
    assert person.outside == 4
    assert response.context["period"]["outside_only"]


def test_the_outside_count_opens_the_persons_activity_outside_the_hours(
    client, reviewer, agent, week
):
    monday = week["monday"]
    client.force_login(reviewer)
    response = client.get(url(date_from=monday, date_to=monday + timedelta(days=6)))
    cell = person_of(response, agent).cells[-1]
    assert cell.label == people.OUTSIDE_LABEL and cell.value == 4
    query = parse_qs(urlsplit(cell.url).query)
    assert query["activity"] == ["outside"] and query["person"] == [str(agent.pk)]
    followed = client.get(cell.url)
    assert counts(followed, agent)["tickets"] == 4
    assert [person.account_id for person in followed.context["page_obj"]] == [agent.pk]


def test_every_count_opens_its_list_with_exactly_the_rows_it_counted(client, reviewer, agent, week):
    monday = week["monday"]
    sign_in(agent, at(monday, 7))
    sign_in(agent, at(monday, 10), SignInEvent.Kind.FAILED)
    client.force_login(reviewer)
    for activity in ("", "outside"):
        response = client.get(
            url(date_from=monday, date_to=monday + timedelta(days=6), activity=activity)
        )
        person = person_of(response, agent)
        for cell in person.cells[: len(people.MEASURES)]:
            if not cell.value:
                assert not cell.url
                continue
            listed = client.get(cell.url)
            assert listed.status_code == 200, cell.url
            assert not listed.context["filter_form"].errors, cell.url
            assert listed.context["total"] == cell.value, (activity, cell.label)


def test_records_written_after_the_first_page_change_no_count_its_links_or_export(
    client, reviewer, agent, week
):
    monday = week["monday"]
    client.force_login(reviewer)
    first = client.get(url(date_from=monday, date_to=monday + timedelta(days=6)))
    tickets = person_of(first, agent).cells[2]
    read(agent, week["ticket"], at(monday, 10))
    sign_in(agent, at(monday, 10))
    again = client.get(first.context["export_url"].replace("/export/", "/"))
    assert counts(again, agent) == counts(first, agent)
    assert client.get(tickets.url).context["total"] == tickets.value
    exported = export_rows(client.get(first.context["export_url"]))
    header, *lines = exported
    [line] = [line for line in lines if line[3] == agent.username]
    assert line[header.index("Tickets opened")] == str(tickets.value)
    fresh = client.get(url(date_from=monday, date_to=monday + timedelta(days=6)))
    assert counts(fresh, agent)["tickets"] == tickets.value + 1


def test_not_judged_and_no_active_calendar_leave_the_outside_count_out(
    client, reviewer, agent, week, calendar
):
    monday = week["monday"]
    client.force_login(reviewer)
    query = {"date_from": monday, "date_to": monday + timedelta(days=6)}
    response = client.get(url(calendar="none", **query))
    assert not response.context["summary"].judged
    assert b"Outside business hours" not in response.content.split(b"<tbody>")[0]
    calendar.is_active = False
    calendar.save()
    response = client.get(url(**query))
    form = response.context["filter_form"]
    assert "calendar" not in form.fields and "activity" not in form.fields
    assert not response.context["summary"].judged
    assert WorkspaceUse.objects.order_by("id").last().filters["calendar"] == "none"


def test_another_calendar_can_be_chosen_and_the_first_one_is_the_default(
    client, reviewer, agent, week, calendar
):
    always = BusinessCalendar.objects.create(
        name="Z around the clock",
        hours={
            day: [["00:00", "24:00"]] for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        },
    )
    monday = week["monday"]
    client.force_login(reviewer)
    query = {"date_from": monday, "date_to": monday + timedelta(days=6)}
    response = client.get(url(calendar=always.pk, **query))
    assert person_of(response, agent).outside == 0  # open around the clock, with no holiday
    [chip] = [chip for chip in response.context["active_filters"] if chip["name"] == "calendar"]
    assert chip["value"] == "Z around the clock"
    response = client.get(url(calendar=calendar.pk, **query))
    [chip] = [chip for chip in response.context["active_filters"] if chip["name"] == "calendar"]
    assert chip["value"] == "Weekday hours"
    response = client.get(url(**query))
    assert response.context["period"]["calendar"] == "Weekday hours"


# ------------------------------------------------------------------------------ sorting and pages


def test_sorting_by_a_count_puts_the_most_first_and_ties_by_name(
    client, reviewer, make_user, clinic, make_ticket
):
    ticket = make_ticket(clinic)
    moment = at(timezone.localdate(), 0, 1)
    accounts = {}
    for name, opened in (("bea", 1), ("abe", 3), ("cal", 3)):
        accounts[name] = make_user(name, "client_services")
        for _ in range(opened):
            read(accounts[name], ticket, moment)
    client.force_login(reviewer)
    response = client.get(url(sort="-tickets", calendar="none"))
    names = [person.username for person in response.context["page_obj"]]
    assert names[:3] == ["abe", "cal", "bea"]
    assert WorkspaceUse.objects.get().filters["sort"] == "-tickets"
    response = client.get(url(sort="-person", calendar="none"))
    names = [person.username for person in response.context["page_obj"]]
    assert names.index("cal") < names.index("bea") < names.index("abe")
    response = client.get(url(sort="nonsense", calendar="none"))
    assert response.context["sort"] == people.DEFAULT_SORT


def test_pages_hold_fifty_people_and_a_page_keeps_a_query_budget(
    client, reviewer, make_user, clinic, make_ticket, calendar
):
    ticket = make_ticket(clinic)
    moment = at(timezone.localdate(), 0, 1)
    for number in range(60):
        account = make_user(f"person{number:02d}", "client_services")
        read(account, ticket, moment)
        sign_in(account, moment)
    client.force_login(reviewer)
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url())
    assert len(response.context["page_obj"].object_list) == 50
    assert len(queries) <= 40, [query["sql"][:120] for query in queries]
    second = client.get(url(page=2))
    assert len(second.context["page_obj"].object_list) >= 10


# ------------------------------------------------------------------------------ export


def test_the_export_holds_every_person_and_is_recorded(client, reviewer, agent, make_user, week):
    formula = make_user("formula", "client_services", first_name="=HYPERLINK(1)")
    read(formula, week["ticket"], at(week["monday"], 12))
    monday = week["monday"]
    client.force_login(reviewer)
    page = client.get(url(date_from=monday, date_to=monday + timedelta(days=6)))
    response = client.get(page.context["export_url"])
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    disposition = response["Content-Disposition"]
    assert (
        f"audit-review-people-{monday:%Y%m%d}-{monday + timedelta(days=6):%Y%m%d}-" in disposition
    )
    header, *lines = export_rows(response)
    assert header == [
        "From",
        "To",
        "Person",
        "Username",
        "Client organization",
        "Sign-ins",
        "Failed or refused sign-ins",
        "Tickets opened",
        "Files downloaded",
        "Patient information shown",
        "Changes made",
        "Outside the hours of Weekday hours",
    ]
    [line] = [line for line in lines if line[3] == agent.username]
    assert line[:2] == [monday.isoformat(), (monday + timedelta(days=6)).isoformat()]
    assert line[7] == "7" and line[-1] == "4"
    [guarded] = [line for line in lines if line[3] == "formula"]
    assert guarded[2].startswith("'=")
    use = WorkspaceUse.objects.filter(kind=UseKind.EXPORTED).get()
    assert use.source == Source.PEOPLE and use.rows == len(lines)
    assert "page" not in use.filters


def test_an_export_of_activity_outside_the_hours_says_so_in_its_header(
    client, reviewer, agent, week
):
    monday = week["monday"]
    client.force_login(reviewer)
    query = {"date_from": monday, "date_to": monday + timedelta(days=6), "activity": "outside"}
    header, *lines = export_rows(client.get(url("people-export", **query)))
    assert header[5] == "Sign-ins (outside business hours)"
    [line] = [line for line in lines if line[3] == agent.username]
    assert line[7] == "4"


# ------------------------------------------------------------------------------ the lists


def test_sign_ins_and_access_records_filter_by_a_group_of_kinds(
    client, reviewer, agent, clinic, make_ticket
):
    ticket = make_ticket(clinic)
    moment = at(timezone.localdate(), 0, 1)
    for kind in ("failed", "locked_out", "refused", "reauth_failed", "signed_in", "signed_out"):
        sign_in(agent, moment, kind)
    for kind in (AccessKind.PATIENT_ANSWER, AccessKind.PERSON_KEY, AccessKind.FILE):
        read(agent, ticket, moment, kind)
    client.force_login(reviewer)
    response = client.get(url("sign-ins", person=agent.pk, action="failed_or_refused"))
    assert response.context["total"] == 4
    response = client.get(url("access", person=agent.pk, action="patient_information"))
    assert response.context["total"] == 2
    uses = client.get(url("uses"))
    described = [row.cells[1].text for row in uses.context["page_obj"]]
    assert any("Action: Any patient information shown" in text for text in described)
    assert any("Action: Any failed or refused attempt" in text for text in described)


def test_the_lists_filter_outside_a_calendars_hours_with_both_dates(
    client, reviewer, agent, week, calendar
):
    monday = week["monday"]
    client.force_login(reviewer)
    query = {"person": agent.pk, "outside_hours": calendar.pk}
    response = client.get(
        url("access", date_from=monday, date_to=monday + timedelta(days=6), **query)
    )
    assert response.context["total"] == 4
    listed = {row.occurred_at for row in response.context["page_obj"]}
    assert listed == set(week["outside"])
    for partial in ({"date_from": monday}, {"date_to": monday}, {}):
        response = client.get(url("access", **partial, **query))
        assert "outside_hours" in response.context["filter_form"].errors
        assert response.context["page_obj"] == []
    response = client.get(url("access", date_from="2024-01-01", date_to="2025-01-01", **query))
    assert "date_to" in response.context["filter_form"].errors
    uses = client.get(url("uses"))
    described = [row.cells[1].text for row in uses.context["page_obj"]]
    assert any("Outside the hours of: Weekday hours" in text for text in described)


def test_the_uses_list_describes_a_people_use_by_name(client, reviewer, agent, calendar):
    client.force_login(reviewer)
    client.get(url(person=agent.pk, activity="outside", sort="-outside", date_from="2026-01-05"))
    response = client.get(url("uses"))
    [row] = response.context["page_obj"]
    assert row.cells[0].note == "People"
    text = row.cells[1].text
    for part in (
        "Person: Ada Agent",
        "From 2026-01-05",
        f"To {timezone.localdate().isoformat()}",
        "Business hours: Weekday hours",
        "Only activity outside business hours",
        "Sorted by outside business hours, most first",
    ):
        assert part in text, (part, text)


def test_the_database_accepts_a_use_of_the_people_tab(reviewer):
    WorkspaceUse.objects.create(
        account_id=reviewer.pk, kind=UseKind.VIEWED, source=Source.PEOPLE, rows=0
    )
