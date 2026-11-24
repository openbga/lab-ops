"""The audit review workspace: four read-only lists (audit_review.sources), each filtered, paged and
exported, for holders of the review capability (audit_review.permissions).

Each page is drawn in one reading (core.concurrency.one_reading): the account, its role and
capability, the filters' choices and the rows come from one moment. Every page of one reading, and
its export, is drawn as of the moment its first page was (`upto`, carried in the pager's and the
export's links), so records written since, this workspace's own use included, never move a row to
the next page. Each page shown and each export is recorded (WorkspaceUse) with the answer it is
about (core.access.keep): a refused request, a redirect or an answer of 500 or more records
nothing, and an answer whose record cannot be written is not sent.
"""

import csv
import io

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from core import access
from core.concurrency import one_reading, reading_account
from core.grids import active_filters
from core.ids import whole_number
from core.lists import toolbar_context

from .forms import ReviewFilter
from .models import Source, UseKind, WorkspaceUse
from .permissions import require_reviewer
from .sources import (
    COLUMNS,
    EXPORT_LIMIT,
    PAGE_SIZE,
    describe,
    newest,
    rows_for,
)

ROUTES = {
    Source.CHANGES: "audit_review:changes",
    Source.ACCESS: "audit_review:access",
    Source.SIGN_INS: "audit_review:sign-ins",
    Source.USES: "audit_review:uses",
}
EXPORTS = {
    Source.CHANGES: "audit_review:changes-export",
    Source.ACCESS: "audit_review:access-export",
    Source.SIGN_INS: "audit_review:sign-ins-export",
    Source.USES: "audit_review:uses-export",
}
TITLES = {
    Source.CHANGES: "Audit review",
    Source.ACCESS: "Audit review: access records",
    Source.SIGN_INS: "Audit review: sign-ins",
    Source.USES: "Audit review: workspace use",
}
NOUNS = {
    Source.CHANGES: "changes",
    Source.ACCESS: "access records",
    Source.SIGN_INS: "sign-in events",
    Source.USES: "uses",
}
INTROS = {
    Source.CHANGES: "Every change recorded in the change history: who made it, to which record "
    "and when. What changed is on the record's own history.",
    Source.ACCESS: "Every sensitive read: a ticket, its history or form details opened, a file "
    "downloaded, a patient-marked answer or a person-level key shown.",
    Source.SIGN_INS: "Every account's sign-ins, sign-outs and failed or refused attempts.",
    Source.USES: "Each page of this workspace shown and each export, with its filters.",
}
# Parameters that are not filters: they never make a chip or a use's filters.
NOT_FILTERS = ("page", "upto")


def _upto(request, source):
    """What the reading is drawn as of (audit_review.sources.newest): `upto` from the address,
    never beyond what is newest now."""
    now = newest(source)
    upto = whole_number(request.GET.get("upto", ""), max_digits=19)
    return now if upto is None else min(upto, now)


def _chips(request, form):
    """The active filters as removable chips; removing one starts a new reading."""
    original = request.GET
    request.GET = original.copy()
    for name in NOT_FILTERS:
        request.GET.pop(name, None)
    try:
        return active_filters(request, form) if form.is_valid() else []
    finally:
        request.GET = original


def _use(request, kind, source, form, rows, page=None):
    """The record of this use, written with the answer (core.access.keep)."""
    filters = form.recorded() if form.is_valid() else {"invalid": True}
    if page is not None:
        filters["page"] = str(page)
    account = access.request_account(request)
    return WorkspaceUse(
        account_id=account.pk,
        kind=kind,
        source=source,
        filters=filters,
        rows=rows,
        request_ref=getattr(request, "request_id", "") or "",
    )


@transaction.non_atomic_requests  # one_reading opens its own transaction (ATOMIC_REQUESTS)
@login_required
@require_GET
def workspace(request, source):
    source = Source(source)
    with one_reading():
        account = reading_account(request)
        require_reviewer(account)
        form = ReviewFilter(source, request.GET)
        upto = _upto(request, source)
        chips = _chips(request, form)
        if form.is_valid():
            records = rows_for(source, form.cleaned_data, upto)
            page_obj = Paginator(records, PAGE_SIZE).get_page(request.GET.get("page"))
            page_obj.object_list = describe(source, page_obj.object_list)
            total = page_obj.paginator.count
        else:
            page_obj, total = [], 0
        query = request.GET.copy()
        query["upto"] = str(upto)
        request.GET = query  # the pager's links carry the reading's moment
        export = query.copy()
        export.pop("page", None)
        response = render(
            request,
            "audit_review/workspace.html",
            {
                "page_title": TITLES[source],
                "source": source,
                "noun": NOUNS[source],
                "intro": INTROS[source],
                "columns": COLUMNS[source],
                "page_obj": page_obj,
                "filter_form": form,
                "active_filters": chips,
                "export_url": f"{reverse(EXPORTS[source])}?{export.urlencode()}",
                "export_limit": EXPORT_LIMIT,
                "exportable": 0 < total <= EXPORT_LIMIT,
                "total": total,
                **toolbar_context(form, chips, inline=form.inline(), more=form.more()),
            },
        )
    page = page_obj.number if form.is_valid() else None
    access.keep(request, _use(request, UseKind.VIEWED, source, form, total, page))
    return response


@transaction.non_atomic_requests
@login_required
@require_GET
def export(request, source):
    """The list as filtered, every row as of the reading's moment, as a CSV file."""
    source = Source(source)
    with one_reading():
        account = reading_account(request)
        require_reviewer(account)
        form = ReviewFilter(source, request.GET)
        upto = _upto(request, source)
        back = request.GET.copy()
        back["upto"] = str(upto)
        back_url = f"{reverse(ROUTES[source])}?{back.urlencode()}"
        if not form.is_valid():
            return redirect(back_url)
        records = rows_for(source, form.cleaned_data, upto)
        total = records.count()
        if total > EXPORT_LIMIT:
            messages.error(
                request,
                f"An export holds at most {EXPORT_LIMIT:,} rows and this list now has "
                f"{total:,}. Narrow the filters, then export again.",
            )
            return redirect(back_url)
        content = _csv(source, describe(source, records[:EXPORT_LIMIT]))
    response = HttpResponse(content, content_type="text/csv; charset=utf-8")
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    name = f"audit-review-{source.value.replace('_', '-')}-{stamp}.csv"
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    access.keep(request, _use(request, UseKind.EXPORTED, source, form, total))
    return response


# A cell a spreadsheet would read as a formula starts with one of these, possibly after spaces
# (and the full-width forms of the signs): it is written with an apostrophe in front, which
# spreadsheets show as text (OWASP, CSV injection).
FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠")


def _cell(value):
    text = str(value)
    return "'" + text if text.lstrip(" ").startswith(FORMULA_START) else text


def _csv(source, rows):
    header = ["When", "Person", "Username"]
    for column in COLUMNS[source]:
        header.append(column.label)
        if column.note:
            header.append(column.note)
    out = io.StringIO()
    out.write("\ufeff")  # spreadsheets then read the file as UTF-8
    writer = csv.writer(out)
    writer.writerow(header)
    for row in rows:
        line = [
            timezone.localtime(row.occurred_at).isoformat(timespec="seconds"),
            row.person,
            row.username,
        ]
        for column, cell in zip(COLUMNS[source], row.cells, strict=True):
            line.append(cell.text)
            if column.note:
                line.append(cell.note)
        writer.writerow([_cell(value) for value in line])
    return out.getvalue()
