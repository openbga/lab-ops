"""Second confirmation of the audit workspace changes, using synthetic records.

Adopted from the second confirmation (.local/review-history/audit-review-confirm2.md).
"""

import csv
import io
import re
from datetime import timedelta
from html import unescape
from urllib.parse import urljoin

import pytest
from django.urls import reverse
from django.utils import timezone

from audit_review.tests.conftest import agent as agent
from audit_review.tests.conftest import category as category
from audit_review.tests.conftest import clinic as clinic
from audit_review.tests.conftest import make_ticket as make_ticket
from audit_review.tests.conftest import make_user as make_user
from audit_review.tests.conftest import reviewer as reviewer


@pytest.mark.parametrize("source", ["changes", "access", "sign-ins", "uses"])
@pytest.mark.parametrize(
    ("zone", "day", "first_utc", "next_utc"),
    [
        # Toronto advanced from 23:29:59 to 00:30:00. This day starts at its first actual time.
        ("America/Toronto", "1919-03-31", "1919-03-31T04:30", "1919-04-01T04:00"),
        # St. John's first midnight begins the day, including the cross-midnight repeated hour.
        ("America/St_Johns", "2009-11-01", "2009-11-01T02:30", "2009-11-02T03:30"),
        # In Santiago the clock jumped at midnight to 01:00 at the start of this day.
        ("America/Santiago", "2019-09-08", "2019-09-08T04:00", "2019-09-09T03:00"),
    ],
)
def test_calendar_exports_use_the_first_instant_of_the_day(
    client, reviewer, agent, monkeypatch, source, zone, day, first_utc, next_utc
):
    import csv
    import io
    from datetime import UTC, date, datetime, timedelta
    from zoneinfo import ZoneInfo

    from django.urls import reverse
    from django.utils import timezone
    from test_review_audit_review_confirm import _write_calendar_rows

    local = ZoneInfo(zone)
    start = datetime.fromisoformat(first_utc).replace(tzinfo=UTC)
    end = datetime.fromisoformat(next_utc).replace(tzinfo=UTC)
    second = timedelta(seconds=1)
    assert (start - second).astimezone(local).date().isoformat() < day
    assert start.astimezone(local).date().isoformat() == day
    moments = [start - second, start, start + timedelta(minutes=15), end - second, end]
    _write_calendar_rows(source, agent, moments, monkeypatch)
    client.force_login(reviewer)
    address = reverse(f"audit_review:{source}")
    differences = {}
    with timezone.override(local):
        for lower, upper, expected in [
            (day, day, moments[1:4]),
            (day, "", moments[1:]),
            ("", (date.fromisoformat(day) - timedelta(days=1)).isoformat(), moments[:1]),
            ("", day, moments[:4]),
            (date.min.isoformat(), date.max.isoformat(), moments),
        ]:
            page = client.get(address, {"person": agent.pk, "date_from": lower, "date_to": upper})
            assert page.status_code == 200 and page.context["filter_form"].is_valid()
            shown = [row.occurred_at for row in page.context["page_obj"]]
            exported = client.get(page.context["export_url"])
            assert exported.status_code == 200
            rows = csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig")))
            csv_times = [datetime.fromisoformat(row["When"]) for row in rows]
            assert shown == csv_times
            if shown != expected[::-1]:
                differences[lower, upper] = [t.astimezone(local).isoformat() for t in shown]
    assert not differences, differences


def test_changes_footer_explains_a_later_page_gaining_a_change_from_a_slow_clock(
    client, reviewer, monkeypatch, settings
):
    from audit_review.sources import PAGE_SIZE
    from core.audit import record_event
    from core.models import AuditEvent

    action = "account.updated"
    when = timezone.now()
    with monkeypatch.context() as original_writer:
        original_writer.setattr(timezone, "now", lambda: when - timedelta(minutes=1))
        AuditEvent.objects.bulk_create(
            [
                AuditEvent(
                    actor=reviewer,
                    object_type=reviewer._meta.label_lower,
                    object_id=str(reviewer.pk),
                    action=action,
                    request_ref=f"initial-{i}",
                )
                for i in range(PAGE_SIZE + 1)
            ]
        )
    client.force_login(reviewer)
    address = reverse("audit_review:changes")
    page = client.get(address, {"action": action})
    assert page.status_code == 200 and page.context["total"] == PAGE_SIZE + 1
    link = re.search(r'href="([^"]+)" rel="next"', page.content.decode())
    assert link
    second_page = urljoin(address, unescape(link[1]))
    before = client.get(second_page)
    assert before.status_code == 200 and len(before.context["page_obj"]) == 1

    # Both pages are finished before this writer starts. There are no organization changes.
    with monkeypatch.context() as slow_writer:
        slow_writer.setattr(timezone, "now", lambda: when - timedelta(minutes=2))
        later = record_event(actor=reviewer, obj=reviewer, action=action)

    second = client.get(second_page)
    assert second.context["total"] == PAGE_SIZE + 2
    assert second.context["page_obj"][-1].occurred_at == later.occurred_at
    exported = client.get(page.context["export_url"])
    assert exported.status_code == 200
    rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
    assert len(rows) == PAGE_SIZE + 2
    assert rows[-1]["When"] == timezone.localtime(later.occurred_at).isoformat(timespec="seconds")

    pages_help = next(
        item["description"]
        for section in page.context["page_documentation"]["sections"]
        for item in section.get("items", [])
        if item["label"] == "Pages"
    )
    assert "clock runs behind" in pages_help
    guide = " ".join((settings.BASE_DIR / "docs/product.md").read_text().split())
    workspace = guide.partition("The Audit review workspace brings")[2]
    guide = workspace.partition("Every page, and the export,")[2].partition("Export CSV gives")[0]
    footer = re.search(r"<p[^>]*>(Rows are listed as they stood[^<]*)</p>", page.content.decode())
    places = {"product guide": guide, "visible Changes footer": footer[1] if footer else ""}
    unqualified = [
        name
        for name, claim in places.items()
        if "stood when" in claim
        and not any(word in claim.casefold() for word in ("clock", "skew", "backdated", "see help"))
    ]
    assert not unqualified, (
        f"An event saved after the cutoff appears on page two and in CSV: {unqualified}"
    )


@pytest.mark.django_db(transaction=True)
def test_repeated_demo_seed_grants_both_user_permissions_only_once_with_model_namesakes(
    settings, tmp_path, monkeypatch
):
    from django.contrib.auth.models import Group, Permission
    from django.contrib.contenttypes.models import ContentType
    from django.core.management import call_command

    from accounts.models import AccountSession, User
    from core.models import Announcement, AuditEvent

    settings.MEDIA_ROOT = tmp_path
    monkeypatch.setenv("DEMO_PASSWORD", "Synthetic-shared-fixture")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "Synthetic-private-fixture")
    codenames = ("review_audit_records", "view_patient_information")
    extra = [
        Permission.objects.create(
            content_type=ContentType.objects.get_for_model(model),
            codename=code,
            name="Synthetic local reporting",
        )
        for model in (AccountSession, Group)
        for code in codenames
    ]

    for _ in range(2):
        call_command("seed_demo", announce_accounts=True, verbosity=0, stdout=io.StringIO())
    supervisor = User.objects.get(username="demo_supervisor")
    expected = Permission.objects.filter(
        content_type=ContentType.objects.get_for_model(User), codename__in=codenames
    )
    assert set(supervisor.user_permissions.values_list("pk", flat=True)) == set(
        expected.values_list("pk", flat=True)
    )
    assert len(expected) == 2
    assert all(not custom.user_set.exists() and not custom.group_set.exists() for custom in extra)
    audit = AuditEvent.objects.filter(
        action="account.capability_granted", object_id=str(supervisor.pk)
    )
    assert sorted(grant["capability"] for grant in audit.values_list("after", flat=True)) == sorted(
        codenames
    )
    assert Announcement.objects.filter(title="Demo accounts", show_on_sign_in=True).count() == 1


def test_find_refuses_long_text_before_looking_for_matches(client, agent, clinic, make_ticket):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from django.urls import reverse

    client.force_login(agent)
    ticket = make_ticket(clinic, subject="Synthetic search boundary")
    address = reverse("desk:request-find")
    ticket_url = reverse("desk:request-detail", args=[ticket.pk])

    def padded_number(length):
        return "REQ-" + str(ticket.pk).zfill(length - 4)

    accepted = client.get(address, {"q": padded_number(200)})
    assert accepted.status_code == 302 and accepted.url == ticket_url

    for query in (padded_number(201), "é" * 201):
        with CaptureQueriesContext(connection) as queries:
            refused = client.get(address, {"q": query})
        assert refused.status_code == 200
        assert refused.context["errors"] and refused.context["q"] == query
        assert not any(
            query in recorded["sql"]
            for recorded in queries.captured_queries
            if "SELECT " in recorded["sql"]
        )
        assert not refused.context["results"] and not refused.context["organizations"]

    matched = client.get(address, {"q": "Synthetic search boundary"})
    assert [match.pk for match in matched.context["results"]] == [ticket.pk]


@pytest.fixture
def toolbar_readers(settings, monkeypatch, tmp_path, db):
    from django.core.management import call_command

    from accounts.models import User

    monkeypatch.setenv("DEMO_PASSWORD", "Synthetic-confirmation-pass")
    settings.MEDIA_ROOT = tmp_path
    call_command("seed_demo", verbosity=0)
    return {
        name: User.objects.get(username=f"demo_{name}")
        for name in ("admin", "client", "supervisor")
    }


def toolbar_summary(page):
    import re
    from html import unescape

    html = page.content.decode()
    summary = re.search(r"<span data-vr-filter-state>(.*?)</span>", html)
    count = re.search(r'<span class="vr-filter-count">(\d+)<span', html)
    assert summary, page.wsgi_request.path
    return int(count[1]) if count else 0, unescape(summary[1])


def test_refused_filter_combinations_only_summarize_choices_applied_by_the_list(
    client, toolbar_readers
):
    cases = {
        "admin": {
            "/lab/announcements/": "state",
            "/setup/service-levels/": "active",
            "/setup/service-levels/calendars/": "active",
            "/setup/systems/": "active",
            "/clients/": "state",
            "/quality/nonconformances/": "source",
            "/quality/actions/": "status",
            "/requests/": "priority",
        },
        "client": {"/portal/requests/": "status"},
    }
    incorrect = []
    for reader, paths in cases.items():
        client.force_login(toolbar_readers[reader])
        for path, field in paths.items():
            query = {"q": "Clinic", field: "obsolete-choice"}
            page = client.get(path, query)
            assert page.status_code == 200
            form = page.context["filter_form"]
            assert set(form.errors) == {field}
            assert "These filters cannot be applied" in page.content.decode()
            assert not list(page.context["page_obj"]), path
            count, summary = toolbar_summary(page)
            if "Search: Clinic" in summary:
                incorrect.append((path, count, summary))
    assert not incorrect, "\n".join(
        f"{path}: {count} active ({summary})" for path, count, summary in incorrect
    )


@pytest.mark.xfail(
    strict=True,
    reason="Predates the workspace, a follow-up: a search for the text all has no chip; "
    "widening the chip rule showed chips on refused lists (third confirmation), so it was "
    "withdrawn.",
)
def test_applied_literal_search_has_a_removable_chip_even_when_it_is_also_a_choice_sentinel(
    client, staff
):
    from clients.models import Client

    organization = Client.objects.create(name="Synthetic Ballard Laboratory")
    other = Client.objects.create(name="Synthetic Shore Laboratory")
    client.force_login(staff("literal-search-reader", "technical"))
    page = client.get("/clients/", {"q": "all", "active": "yes"})
    assert page.status_code == 200
    pks = {row.pk for row in page.context["page_obj"]}
    assert organization.pk in pks and other.pk not in pks
    count, summary = toolbar_summary(page)
    assert count == 2 and "Search: all" in summary and "Status: Active" in summary
    assert 'aria-label="Remove filter Search: all"' in page.content.decode()


def test_search_summaries_with_duplicates_whitespace_and_refusals_on_each_surfaced_list(
    client, toolbar_readers
):
    from clients.models import Client

    cases = {
        "admin": [
            "/lab/announcements/",
            "/setup/service-levels/",
            "/setup/service-levels/calendars/",
            "/setup/systems/",
            "/clients/",
            "/quality/nonconformances/",
            "/quality/actions/",
            "/requests/",
        ],
        "client": ["/portal/requests/"],
    }
    for reader, paths in cases.items():
        client.force_login(toolbar_readers[reader])
        for path in paths:
            for term in ("  Clinic  ", "007", "   ", "z" * 201):
                page = client.get(path, {"q": term})
                assert page.status_code == 200, path
                form = page.context["filter_form"]
                count, summary = toolbar_summary(page)
                if len(term) > 200 or not term.strip():
                    assert (count, summary) == (0, "No filters"), (path, summary)
                else:
                    assert form.is_valid()
                    assert (count, summary) == (1, "Search: " + term.strip()), (path, summary)
    client.force_login(toolbar_readers["admin"])
    same = client.get("/clients/", {"q": "Active", "active": "yes"})
    assert same.context["filter_form"].is_valid()
    count, summary = toolbar_summary(same)
    assert count == 2 and set(summary.split(" · ")) == {"Search: Active", "Status: Active"}

    organization = Client.objects.get(account_number="EXCLIN")
    for reader, paths in [
        ("admin", [f"/clients/{organization.pk}/timeline/"]),
        (
            "supervisor",
            [f"/audit-review/{source}/" for source in ("changes", "access", "sign-ins", "uses")],
        ),
    ]:
        client.force_login(toolbar_readers[reader])
        for path in paths:
            page = client.get(path, {"q": "unused text"})
            assert page.status_code == 200 and "q" not in page.context["filter_form"].fields
            assert toolbar_summary(page) == (0, "No filters")


def test_matching_free_text_and_an_independent_team_each_count_on_the_ticket_queue(client, staff):
    from teams.models import Team

    team = Team.objects.create(name="all", slug="confirm-all")
    client.force_login(staff("team-search-reader", "technical"))
    page = client.get("/requests/", {"q": "all", "team": team.pk, "view": "all"})
    assert page.status_code == 200 and page.context["filter_form"].is_valid()
    count, summary = toolbar_summary(page)
    assert "Team: all" in summary
    assert "Search: all" in summary and count == 2, (count, summary)
