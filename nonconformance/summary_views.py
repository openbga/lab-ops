"""The nonconformance summary: records raised per month by source and by area over the last
twelve months. Every staff role reads it while the module is on (selectors)."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.urls import reverse

from . import selectors, summary


@login_required
def summary_page(request):
    records = selectors.staff_nonconformances(request.user)
    counts = summary.counts(records)
    return render(
        request,
        "nonconformance/summary.html",
        {
            "page_title": "Nonconformance summary",
            "counts": counts,
            # (source value, label, twelve-month count) for the totals row and its links.
            "source_totals": [
                (value, label, n)
                for (value, label), n in zip(
                    counts["sources"], counts["source_totals"], strict=True
                )
            ],
            "list_url": reverse("nonconformance:list"),
        },
    )
