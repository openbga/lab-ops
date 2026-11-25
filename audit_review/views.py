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
from types import SimpleNamespace

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
from core.grids import active_filters, chosen_query, shown
from core.ids import whole_number
from core.lists import toolbar_context
from dataimport.importing import FORMULA_STARTS

from . import people
from .forms import READING_PARAMS, PeopleFilter, ReviewFilter
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


def _before_reading(source):
    """For Changes, the moment now, taken before the reading's snapshot (audit_review.sources.
    newest): a change saved after it is left out of this reading's pages and export, even one
    committed before the snapshot. None for the other lists, whose newest id is read in it."""
    return newest(source) if source == Source.CHANGES else None


def _upto(request, source, before):
    """What the reading is drawn as of: `upto` from the address, never beyond what is newest now
    (`before`, or the newest id in the reading)."""
    now = before if before is not None else newest(source)
    upto = whole_number(request.GET.get("upto", ""), max_digits=19)
    return now if upto is None else min(upto, now)


def _chips(request, form, not_filters=NOT_FILTERS):
    """The active filters as removable chips; removing one starts a new reading."""
    original = request.GET
    request.GET = original.copy()
    for name in not_filters:
        request.GET.pop(name, None)
    try:
        return active_filters(request, form) if form.is_valid() else []
    finally:
        request.GET = original


def _use(request, kind, source, form, rows, page=None, sort=None):
    """The record of this use, written with the answer (core.access.keep)."""
    filters = form.recorded() if form.is_valid() else {"invalid": True}
    if page is not None:
        filters["page"] = str(page)
    if sort is not None and sort != people.DEFAULT_SORT and form.is_valid():
        filters["sort"] = sort
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
    before = _before_reading(source)
    with one_reading():
        account = reading_account(request)
        require_reviewer(account)
        form = ReviewFilter(source, request.GET)
        upto = _upto(request, source, before)
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
                "grid": f"audit_review.{source.value}",
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
    before = _before_reading(source)
    with one_reading():
        account = reading_account(request)
        require_reviewer(account)
        form = ReviewFilter(source, request.GET)
        upto = _upto(request, source, before)
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


def _cell(value):
    """A cell a spreadsheet would read as a formula, by the rule the directory import warns of
    (dataimport.importing.FORMULA_STARTS, also after leading spaces), is written with an
    apostrophe in front, which spreadsheets show as text (OWASP, CSV injection)."""
    text = str(value)
    if text.startswith(FORMULA_STARTS) or text.strip().startswith(FORMULA_STARTS):
        return "'" + text
    return text


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


# ------------------------------------------------------------------------------ People

PEOPLE_NOT_FILTERS = (
    "page",
    "sort",
    *people.MARKER_PARAMS.values(),
    *READING_PARAMS.values(),
    people.READING_SORT,
)


def _people_marks(request):
    """What each list is counted as of (people.summarize): the marker its own name in the address
    carries (people.MARKER_PARAMS), never beyond what is newest in this reading. Read inside the
    reading, after its first query: for Changes, the time of the newest change the reading's
    snapshot holds, and never later than now (people.newest_change), so the three lists agree on
    what the snapshot holds and a change begun after it is never counted later."""
    marks = {}
    for source, name in people.MARKER_PARAMS.items():
        now = people.newest_change() if source == Source.CHANGES else newest(source)
        given = whole_number(request.GET.get(name, ""), max_digits=19)
        marks[source] = now if given is None else min(given, now)
    return marks


def _people_period(form, effective):
    calendar = effective["calendar"]
    return {
        # No date was given, as a filter or carried from a first page: the last 30 days.
        "default": not any(
            (
                form.cleaned_data.get("date_from"),
                form.cleaned_data.get("date_to"),
                form.reading.get("date_from"),
                form.reading.get("date_to"),
            )
        ),
        "date_from": effective["date_from"],
        "date_to": effective["date_to"],
        "calendar": calendar.name if calendar else "",
        "outside_only": effective["outside_only"],
    }


@transaction.non_atomic_requests  # one_reading opens its own transaction (ATOMIC_REQUESTS)
@login_required
@require_GET
def people_page(request):
    """The People tab: counts by person over a period (audit_review.people)."""
    with one_reading():
        account = reading_account(request)  # the reading's first query: its snapshot
        require_reviewer(account)
        marks = _people_marks(request)
        form = PeopleFilter(request.GET, reading=PeopleFilter.reading_from(request.GET))
        chips = _chips(request, form, PEOPLE_NOT_FILTERS)
        for chip in chips:
            if chip["name"] == "calendar":
                chip["value"] = form.chip_value(form.cleaned_data["calendar"])
        period = summary = None
        sort = people.DEFAULT_SORT
        if form.is_valid():
            effective = form.effective()
            sort = people.sort_value(request.GET.get("sort"), judged=effective["calendar"])
            period = _people_period(form, effective)
            summary = people.summarize(effective, marks)
            rows = people.ordered(summary.people, sort)
            page_obj = Paginator(rows, people.PAGE_SIZE).get_page(request.GET.get("page"))
            people.link(page_obj.object_list, form, marks, request.path)
            total = page_obj.paginator.count
            # Its sort may be saved, once the record of this page is (core.preferences).
            shown(request, SimpleNamespace(per_page=None, sort=sort))
        else:
            page_obj, total = [], 0
        query = request.GET.copy()
        for name, value in people.marker_params(marks).items():
            query[name] = value
        if form.is_valid():
            # The period and calendar applied, so that a later page, another sort or the export
            # keeps them past midnight or a change of the calendars; not as filters.
            for name, value in form.applied().items():
                query[name] = value
        request.GET = query  # the pager's and the sort links carry the reading
        # The export keeps this page's order under a name that is not the list's sort, so neither
        # a sort added from the reviewer's saved settings nor a refused export's way back turns
        # it into a choice that would be saved.
        export = chosen_query(request)
        export.pop("page", None)
        export.pop("sort", None)
        export[people.READING_SORT] = sort
        response = render(
            request,
            "audit_review/people.html",
            {
                "page_title": "Audit review: people",
                # One layout for both views: the count outside the hours is only in one of them,
                # and keeps its saved choice in the other (static/js/grid.js, save).
                "grid": "audit_review.people",
                "measures": people.MEASURES,
                "outside_label": people.OUTSIDE_LABEL,
                "summary": summary,
                "period": period,
                "sort": sort,
                "page_obj": page_obj,
                "filter_form": form,
                "active_filters": chips,
                "export_url": f"{reverse('audit_review:people-export')}?{export.urlencode()}",
                "total": total,
                **toolbar_context(form, chips, inline=form.inline(), more=form.more()),
            },
        )
    page = page_obj.number if form.is_valid() else None
    access.keep(request, _use(request, UseKind.VIEWED, Source.PEOPLE, form, total, page, sort))
    return response


@transaction.non_atomic_requests
@login_required
@require_GET
def people_export(request):
    """The People tab as filtered and sorted, every person, as a CSV file."""
    with one_reading():
        account = reading_account(request)  # the reading's first query: its snapshot
        require_reviewer(account)
        marks = _people_marks(request)
        form = PeopleFilter(request.GET, reading=PeopleFilter.reading_from(request.GET))
        back = chosen_query(request)  # never a sort added from the saved settings
        for name, value in people.marker_params(marks).items():
            back[name] = value
        back_url = f"{reverse('audit_review:people')}?{back.urlencode()}"
        if not form.is_valid():
            return redirect(back_url)
        effective = form.effective()
        sort = people.sort_value(
            request.GET.get(people.READING_SORT) or request.GET.get("sort"),
            judged=effective["calendar"],
        )
        summary = people.summarize(effective, marks)
        rows = people.ordered(summary.people, sort)
        content = _people_csv(effective, summary, rows)
    response = HttpResponse(content, content_type="text/csv; charset=utf-8")
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    name = (
        f"audit-review-people-{effective['date_from']:%Y%m%d}-{effective['date_to']:%Y%m%d}-"
        f"{stamp}.csv"
    )
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    access.keep(request, _use(request, UseKind.EXPORTED, Source.PEOPLE, form, len(rows), sort=sort))
    return response


def _people_csv(effective, summary, rows):
    """One line per person: the period, the person, then each count; with "Only outside business
    hours", each count's header says so."""
    calendar = effective["calendar"]
    suffix = " (outside business hours)" if effective["outside_only"] else ""
    header = ["From", "To", "Person", "Username", "Client organization"]
    header += [f"{measure.label}{suffix}" for measure in people.MEASURES]
    if summary.judged:
        header.append(f"Outside the hours of {calendar.name}")
    out = io.StringIO()
    out.write("\ufeff")  # spreadsheets then read the file as UTF-8
    writer = csv.writer(out)
    writer.writerow([_cell(value) for value in header])
    for person in rows:
        line = [
            effective["date_from"].isoformat(),
            effective["date_to"].isoformat(),
            person.name,
            person.username,
            person.organization,
        ]
        line += [person.counts[measure.key] for measure in people.MEASURES]
        if summary.judged:
            line.append(person.outside)
        writer.writerow([_cell(value) for value in line])
    return out.getvalue()
