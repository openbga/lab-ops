"""Monthly counts: laboratory months, sources and areas, and nothing outside the period."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from nonconformance import summary
from nonconformance.models import Nonconformance, Source


def make(raiser, raised_at, source="internal", area=""):
    nc = Nonconformance.objects.create(
        title="Synthetic",
        description="Synthetic",
        source=source,
        severity="minor",
        area=area,
        raised_by=raiser,
    )
    Nonconformance.objects.filter(pk=nc.pk).update(raised_at=raised_at)
    return nc


@pytest.mark.django_db
def test_counts_group_by_laboratory_month_source_and_area(settings, agent):
    settings.TIME_ZONE = "America/New_York"
    zone = ZoneInfo("America/New_York")
    with timezone.override(zone):
        # 23:30 local on 31 October is already November in UTC: it counts in October.
        make(agent, datetime(2026, 10, 31, 23, 30, tzinfo=zone), "complaint", "Reporting")
        make(agent, datetime(2026, 11, 2, 9, 0, tzinfo=zone), "complaint", " reporting ")
        make(agent, datetime(2026, 11, 3, 9, 0, tzinfo=zone), "equipment", "")
        make(agent, datetime(2025, 11, 30, 9, 0, tzinfo=zone), "audit", "Old")  # before period
        result = summary.counts(Nonconformance.objects.all(), today=date(2026, 11, 18))
    assert result["first"] == date(2025, 12, 1)
    assert len(result["months"]) == 12
    november, october = result["months"][0], result["months"][1]
    assert november["month"] == date(2026, 11, 1) and october["month"] == date(2026, 10, 1)
    sources = [value for value, _ in result["sources"]]
    assert november["by_source"][sources.index(Source.COMPLAINT)] == 1
    assert november["by_source"][sources.index(Source.EQUIPMENT)] == 1
    assert october["by_source"][sources.index(Source.COMPLAINT)] == 1
    assert result["total"] == 3
    # Areas match without regard to case or spaces; the label is one of the spellings used.
    areas = [(row["area"].casefold(), row["count"]) for row in result["areas"]]
    assert areas == [("reporting", 2), ("", 1)]
