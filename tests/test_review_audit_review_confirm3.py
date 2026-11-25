"""Third confirmation of the audit date boundaries, snapshot wording and shared filter controls.

Adopted from the third confirmation (.local/review-history/audit-review-confirm3.md).
"""

import csv
import io
from datetime import datetime, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from test_review_audit_review_confirm import _write_calendar_rows

from audit_review.tests.conftest import agent as agent
from audit_review.tests.conftest import make_user as make_user
from audit_review.tests.conftest import reviewer as reviewer


@pytest.mark.parametrize("source", ["changes", "access", "sign-ins", "uses"])
def test_slow_clock_exception_in_the_footer_matches_the_offered_export(
    client, reviewer, agent, monkeypatch, source
):
    before = (timezone.now() - timedelta(minutes=5)).replace(microsecond=0)
    later_record_time = before + timedelta(minutes=1)
    _write_calendar_rows(source, agent, [before], monkeypatch)
    client.force_login(reviewer)
    page = client.get(reverse(f"audit_review:{source}"), {"person": agent.pk})
    assert page.status_code == 200 and page.context["total"] == 1
    original_export = page.context["export_url"]

    # The writer starts after the page is drawn, with a recorded time before its cutoff.
    _write_calendar_rows(source, agent, [later_record_time], monkeypatch)
    response = client.get(original_export)
    assert response.status_code == 200
    rows = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")))
    expected = [later_record_time, before] if source == "changes" else [before]
    assert [datetime.fromisoformat(row["When"]) for row in rows] == expected

    text = page.content.decode()
    footer = text.split("Rows are listed as they stood", 1)[1].split("</p>", 1)[0]
    if source == "changes":
        assert "clock" in footer and "behind" in footer
    elif "clock" in footer:
        assert "Changes" in footer


@pytest.mark.parametrize("source", ["changes", "access", "sign-ins", "uses"])
def test_forward_midnight_jump_keeps_the_boundary_microsecond_in_the_new_day(
    client, reviewer, agent, monkeypatch, source
):
    import csv
    import io
    from datetime import UTC, datetime, timedelta

    from django.urls import reverse
    from django.utils import timezone
    from test_review_audit_review_confirm import _write_calendar_rows

    # Toronto jumped from March 30 at 23:29:59.999999 to March 31 at 00:30:00.
    first = datetime(1919, 3, 31, 4, 30, tzinfo=UTC)
    next_day = datetime(1919, 4, 1, 4, tzinfo=UTC)
    tick = timedelta(microseconds=1)
    times = [first - tick, first, first + tick, next_day - tick, next_day]
    _write_calendar_rows(source, agent, times, monkeypatch)
    client.force_login(reviewer)
    with timezone.override("America/Toronto"):
        for day, expected in [("1919-03-30", times[:1]), ("1919-03-31", times[1:4])]:
            page = client.get(
                reverse(f"audit_review:{source}"),
                {"person": agent.pk, "date_from": day, "date_to": day},
            )
            assert page.status_code == 200 and page.context["filter_form"].is_valid()
            shown = [row.occurred_at for row in page.context["page_obj"]]
            exported = client.get(page.context["export_url"])
            assert exported.status_code == 200
            rows = csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig")))
            exported_times = [datetime.fromisoformat(row["When"]) for row in rows]
            assert shown == expected[::-1]
            assert exported_times == [t.replace(microsecond=0) for t in expected[::-1]]


@pytest.mark.parametrize(
    ("zone", "day", "first_utc"),
    [
        ("Africa/Monrovia", "1972-01-07", "1972-01-07T00:44:30+00:00"),
        ("Pacific/Kiritimati", "1994-12-31", "1994-12-31T10:00:00+00:00"),
        ("Pacific/Kiritimati", "1995-01-01", "1994-12-31T10:00:00+00:00"),
        ("America/Havana", "2024-11-03", "2024-11-03T04:00:00+00:00"),
        ("America/St_Johns", "2009-11-01", "2009-11-01T02:30:00+00:00"),
    ],
)
def test_calendar_start_at_skipped_repeated_and_subminute_midnights(zone, day, first_utc):
    from datetime import UTC, date, datetime, timedelta
    from zoneinfo import ZoneInfo

    from django.utils import timezone

    from audit_review.sources import _day_start

    local = ZoneInfo(zone)
    first = datetime.fromisoformat(first_utc)
    requested = date.fromisoformat(day)
    assert (first - timedelta(microseconds=1)).astimezone(local).date() < requested
    assert first.astimezone(local).date() >= requested
    with timezone.override(local):
        assert _day_start(requested).astimezone(UTC) == first


@pytest.mark.parametrize(
    ("zone", "day", "moment"),
    [
        ("Etc/GMT-14", "0001-01-01", "0001-01-01T01:00:00+00:00"),
        ("Etc/GMT+12", "9999-12-31", "9999-12-31T18:00:00+00:00"),
    ],
)
def test_first_and_last_local_dates_are_usable_as_closed_filters(
    client, reviewer, agent, zone, day, moment
):
    import csv
    import io
    from datetime import datetime

    from django.urls import reverse
    from django.utils import timezone

    from accounts.models import SignInEvent

    stamp = datetime.fromisoformat(moment)
    SignInEvent.objects.create(user=agent, occurred_at=stamp, kind="failed", factor="password")
    client.force_login(reviewer)
    with timezone.override(zone):
        page = client.get(
            reverse("audit_review:sign-ins"),
            {"person": agent.pk, "date_from": day, "date_to": day},
        )
        assert page.status_code == 200 and page.context["filter_form"].is_valid()
        assert [row.occurred_at for row in page.context["page_obj"]] == [stamp]
        exported = client.get(page.context["export_url"])
        assert exported.status_code == 200
        rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
        assert len(rows) == 1
        when = datetime.fromisoformat(rows[0]["When"])
        assert when == stamp and when.date().isoformat() == day


@pytest.mark.parametrize(
    ("path", "refused"),
    [
        ("/requests/", {"created_from": "2026-03-14", "created_to": "2026-03-01"}),
        ("/clients/", {"state": "former-choice"}),
    ],
)
def test_a_refused_list_does_not_add_an_active_desktop_chip_for_literal_all(
    client, staff, path, refused
):
    client.force_login(staff("confirm-chip-reader", "technical"))
    page = client.get(path, {"q": "all", **refused})
    assert page.status_code == 200
    form = page.context["filter_form"]
    assert form.errors and "q" not in form.errors
    assert not list(page.context["page_obj"])
    html = page.content.decode()
    assert "These filters cannot be applied" in html
    assert 'aria-label="Remove filter Search: all"' not in html


def test_grid_toolbar_counts_a_legacy_query_chip_with_its_own_label_once(rf):
    """The published chip format has a label, a value and the URL that removes the filter."""
    from django import forms
    from django.template import Context, Template

    class TicketSearch(forms.Form):
        q = forms.CharField(label="Ticket text", required=False)

    request = rf.get("/requests/", {"q": "courier"})
    form = TicketSearch(request.GET)
    assert form.is_valid()
    # This link is for the q field: following it removes the page's only query parameter.
    chip = {"label": form["q"].label, "value": form.cleaned_data["q"], "remove_url": "?"}
    html = Template("{% load ui %}{% grid_toolbar label='Filter tickets' %}").render(
        Context({"request": request, "filter_form": form, "active_filters": [chip], "q": "courier"})
    )
    assert 'aria-label="Remove filter Ticket text: courier"' in html
    import re

    count = re.search(r'<span class="vr-filter-count">(\d+)<span', html)
    summary = re.search(r"<span data-vr-filter-state>([^<]*)</span>", html)
    assert count and summary
    assert count[1] == "1" and summary[1].count("courier") == 1, summary[1]


def test_same_text_search_and_choice_have_independent_removal_links(client, staff):
    from urllib.parse import parse_qs, urlsplit

    client.force_login(staff("confirm-separate-chips", "technical"))
    page = client.get("/clients/", {"q": "Active", "active": "yes", "page": 2})
    assert page.status_code == 200 and page.context["filter_form"].is_valid()
    chips = page.context["active_filters"]
    by_label = {chip["label"]: chip for chip in chips}
    assert set(by_label) == {"Search", "Status"}
    assert {chip["value"] for chip in chips} == {"Active"}
    assert parse_qs(urlsplit(by_label["Search"]["remove_url"]).query) == {"active": ["yes"]}
    assert parse_qs(urlsplit(by_label["Status"]["remove_url"]).query) == {"q": ["Active"]}
    for remaining, removed in [("Search", "Status"), ("Status", "Search")]:
        after = client.get("/clients/" + by_label[removed]["remove_url"])
        assert after.status_code == 200
        assert [chip["label"] for chip in after.context["active_filters"]] == [remaining]
        assert f"<span data-vr-filter-state>{remaining}: Active</span>" in after.content.decode()
