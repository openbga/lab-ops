"""Counts of nonconformances for the summary page: records raised in each calendar month by
source, and by area, over the same months. Months are laboratory dates (settings.TIME_ZONE).
Counts only: no score, rate or target. The caller passes records it has authorized
(selectors.staff_nonconformances)."""

from datetime import date, datetime, time

from django.db.models import Count, Min
from django.db.models.functions import Lower, Trim, TruncMonth
from django.utils import timezone

from .models import Source

MONTHS = 12


def _month_start(day, back):
    """The first day of the month `back` months before `day`'s month."""
    index = day.year * 12 + day.month - 1 - back
    return date(index // 12, index % 12 + 1, 1)


def period(today=None, months=MONTHS):
    """(first day, list of month starts oldest first) of the last `months` months, this one
    included."""
    today = today or timezone.localdate()
    starts = [_month_start(today, back) for back in range(months - 1, -1, -1)]
    return starts[0], starts


def counts(records, today=None, months=MONTHS):
    """{"months": [{"month", "by_source": [n per Source], "total"}] newest first,
    "sources": [(value, label)], "source_totals": [n per Source], "total",
    "areas": [{"area", "count"}] most first, "first": first day}."""
    first, starts = period(today, months)
    zone = timezone.get_current_timezone()
    since = timezone.make_aware(datetime.combine(first, time.min), zone)
    in_period = records.filter(raised_at__gte=since)
    rows = (
        in_period.annotate(month=TruncMonth("raised_at", tzinfo=zone))
        .values("month", "source")
        .annotate(n=Count("pk"))
    )
    table = {}
    for row in rows:
        month = (
            timezone.localtime(row["month"], zone).date()
            if isinstance(row["month"], datetime)
            else row["month"]
        )
        table.setdefault(month, {})[row["source"]] = row["n"]
    sources = list(Source.choices)
    months_out = []
    for start in reversed(starts):
        by_source = [table.get(start, {}).get(value, 0) for value, _ in sources]
        months_out.append({"month": start, "by_source": by_source, "total": sum(by_source)})
    source_totals = [sum(m["by_source"][i] for m in months_out) for i in range(len(sources))]
    areas = (
        in_period.annotate(key=Lower(Trim("area")))
        .values("key")
        .annotate(count=Count("pk"), label=Min(Trim("area")))
        .order_by("-count", "key")
    )
    return {
        "first": first,
        "months": months_out,
        "sources": sources,
        "source_totals": source_totals,
        "total": sum(source_totals),
        "areas": [{"area": row["label"] or "", "count": row["count"]} for row in areas],
    }
