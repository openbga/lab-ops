"""The Access review section (audit_review.access_report): the Accounts and Unused accounts tabs,
their exports, and the unused account period's form. For holders of "Can review access and audit
records" (audit_review.permissions).

Each page and export is drawn in one reading (core.concurrency.one_reading): the account, its role
and capability, the filters' choices, the accounts and what they hold come from one moment, which
is the moment the page is drawn (the accounts as they stand, not as of an earlier page). Each page
of the two lists shown and each export is recorded (WorkspaceUse) with the answer it is about
(core.access.keep), as in the audit review workspace; the period's form is not, as the Reviews tab's
forms are not (it shows the period and its changes, which Changes lists too). The period's form is
opened in one reading and decided by the service (access_report.set_period), which holds the acting
account and checks again that it may review.
"""

import csv
import io
from types import SimpleNamespace

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from core import access
from core.concurrency import one_reading, reading_account
from core.grids import chosen_query, shown
from core.lists import toolbar_context

from . import access_report
from .access_forms import AccountFilter, PeriodForm, UnusedFilter
from .models import Source, UseKind, WorkspaceUse
from .permissions import require_reviewer
from .review_views import _carry_revision, _errors, _form_reading, _refused, _render
from .sources import EXPORT_LIMIT, PAGE_SIZE
from .views import _cell, _chips

ACCOUNTS_TITLE = "Access review"
UNUSED_TITLE = "Access review: unused accounts"
NOT_FILTERS = ("page", "sort")


def _use(request, kind, source, form, rows, page=None, sort=None):
    """The record of this use, written with the answer (core.access.keep)."""
    filters = form.recorded() if form.is_valid() else {"invalid": True}
    if page is not None:
        filters["page"] = str(page)
    if sort is not None and sort != access_report.DEFAULT_SORT and form.is_valid():
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


def _sort(value):
    return value if value in access_report.SORTS else access_report.DEFAULT_SORT


def _stamp():
    return timezone.localtime().strftime("%Y%m%d-%H%M%S")


def _csv_response(content, name):
    response = HttpResponse(content, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}-{_stamp()}.csv"'
    return response


def _writer():
    out = io.StringIO()
    out.write("\ufeff")  # spreadsheets then read the file as UTF-8
    return out, csv.writer(out)


def _items(items):
    return "; ".join(f"{item.text} ({item.note})" if item.note else item.text for item in items)


def _when(moment):
    return timezone.localtime(moment).isoformat(timespec="seconds") if moment else ""


# ------------------------------------------------------------------------------ Accounts


@transaction.non_atomic_requests  # one_reading opens its own transaction (ATOMIC_REQUESTS)
@login_required
@require_GET
def accounts_page(request):
    """The Accounts tab: every account and what it holds (access_report.accounts)."""
    with one_reading():
        account = reading_account(request)  # the reading's first query: its snapshot
        require_reviewer(account)
        form = AccountFilter(request.GET)
        chips = _chips(request, form, NOT_FILTERS)
        sort = _sort(request.GET.get("sort"))
        if form.is_valid():
            records = access_report.accounts(form.cleaned_data, sort)
            page_obj = Paginator(records, PAGE_SIZE).get_page(request.GET.get("page"))
            page_obj.object_list = access_report.describe(list(page_obj.object_list))
            total = page_obj.paginator.count
            # Its sort may be saved, once the record of this page is (core.preferences).
            shown(request, SimpleNamespace(per_page=None, sort=sort))
        else:
            page_obj, total = [], 0
        export = chosen_query(request)  # never a sort added from the saved settings
        export.pop("page", None)
        if sort != access_report.DEFAULT_SORT:
            export["sort"] = sort
        response = render(
            request,
            "audit_review/access_accounts.html",
            {
                "page_title": ACCOUNTS_TITLE,
                "grid": "access_review.accounts",
                "sort": sort,
                "page_obj": page_obj,
                "filter_form": form,
                "active_filters": chips,
                "export_url": f"{reverse('access_review:accounts-export')}?{export.urlencode()}",
                "export_limit": EXPORT_LIMIT,
                "exportable": 0 < total <= EXPORT_LIMIT,
                "total": total,
                "period": access_report.period(),
                **toolbar_context(form, chips, inline=form.inline(), more=form.more()),
            },
        )
    page = page_obj.number if form.is_valid() else None
    access.keep(request, _use(request, UseKind.VIEWED, Source.ACCOUNTS, form, total, page, sort))
    return response


ACCOUNT_HEADER = [
    "Person",
    "Username",
    "Kind",
    "Client organization",
    "Status",
    "Deactivated",
    "Admin console",
    "Roles",
    "Capabilities",
    "Teams",
    "Portal access",
    "Second factor",
    "Required by the rule",
    "Last sign-in",
    "Created",
]


@transaction.non_atomic_requests
@login_required
@require_GET
def accounts_export(request):
    """The Accounts tab as filtered and sorted, every account, as a CSV file."""
    with one_reading():
        account = reading_account(request)
        require_reviewer(account)
        form = AccountFilter(request.GET)
        sort = _sort(request.GET.get("sort"))
        back = chosen_query(request)
        back_url = f"{reverse('access_review:accounts')}?{back.urlencode()}"
        if not form.is_valid():
            return redirect(back_url)
        records = access_report.accounts(form.cleaned_data, sort)
        total = records.count()
        if total > EXPORT_LIMIT:
            messages.error(
                request,
                f"An export holds at most {EXPORT_LIMIT:,} rows and this list now has "
                f"{total:,}. Narrow the filters, then export again.",
            )
            return redirect(back_url)
        rows = access_report.describe(list(records[:EXPORT_LIMIT]))
        out, writer = _writer()
        writer.writerow(ACCOUNT_HEADER)
        for row in rows:
            line = [
                row.name,
                row.username,
                row.kind,
                row.organization,
                row.status,
                row.deactivated.plain() if row.deactivated else "",
                _items(row.console),
                _items(row.roles),
                _items(row.capabilities),
                _items(row.teams),
                row.portal,
                "Set up" if row.second_factor else "Not set up",
                "Yes" if row.required else "No",
                _when(row.last_sign_in),
                _when(row.joined),
            ]
            writer.writerow([_cell(value) for value in line])
    response = _csv_response(out.getvalue(), "access-review-accounts")
    access.keep(request, _use(request, UseKind.EXPORTED, Source.ACCOUNTS, form, total, sort=sort))
    return response


# ------------------------------------------------------------------------------ Unused accounts


def _unused_rows(days, form, now):
    return access_report.unused_accounts(days, form.cleaned_data, now)


@transaction.non_atomic_requests
@login_required
@require_GET
def unused_page(request):
    """The Unused accounts tab: active accounts with no sign-in for the laboratory's period."""
    with one_reading():
        account = reading_account(request)
        require_reviewer(account)
        form = UnusedFilter(request.GET)
        chips = _chips(request, form, NOT_FILTERS)
        period = access_report.period()
        now = timezone.now()
        if form.is_valid() and period.days:
            records = _unused_rows(period.days, form, now)
            page_obj = Paginator(records, PAGE_SIZE).get_page(request.GET.get("page"))
            page_obj.object_list = access_report.describe(list(page_obj.object_list), history=False)
            total = page_obj.paginator.count
        else:
            page_obj, total = [], 0
        export = chosen_query(request)
        export.pop("page", None)
        response = render(
            request,
            "audit_review/access_unused.html",
            {
                "page_title": UNUSED_TITLE,
                "grid": "access_review.unused",
                "page_obj": page_obj,
                "filter_form": form,
                "active_filters": chips,
                "period": period,
                "since": access_report.cutoff(period.days, now) if period.days else None,
                "now": now,
                "export_url": f"{reverse('access_review:unused-export')}?{export.urlencode()}",
                "export_limit": EXPORT_LIMIT,
                "exportable": 0 < total <= EXPORT_LIMIT,
                "total": total,
                **toolbar_context(form, chips, inline=form.inline(), more=form.more()),
            },
        )
    page = page_obj.number if form.is_valid() and period.days else None
    use = _use(request, UseKind.VIEWED, Source.UNUSED, form, total, page)
    # The period the page showed, whether or not its filters were accepted.
    use.filters["days"] = str(period.days) if period.days else "none"
    access.keep(request, use)
    return response


UNUSED_HEADER = [
    "Person",
    "Username",
    "Kind",
    "Client organization",
    "Roles",
    "Capabilities",
    "Last sign-in",
    "Created",
    "Days without a sign-in",
    "Unused after (days)",
]


def days_unused(row, now):
    """Whole days since the last sign-in, or since the account was created when it never signed
    in."""
    since = row.last_sign_in or row.joined
    return (now - since).days if since else None


@transaction.non_atomic_requests
@login_required
@require_GET
def unused_export(request):
    """The Unused accounts tab as filtered, every account, as a CSV file."""
    with one_reading():
        account = reading_account(request)
        require_reviewer(account)
        form = UnusedFilter(request.GET)
        back_url = f"{reverse('access_review:unused')}?{chosen_query(request).urlencode()}"
        period = access_report.period()
        if not form.is_valid() or not period.days:
            return redirect(back_url)
        now = timezone.now()
        records = _unused_rows(period.days, form, now)
        total = records.count()
        if total > EXPORT_LIMIT:
            messages.error(
                request,
                f"An export holds at most {EXPORT_LIMIT:,} rows and this list now has "
                f"{total:,}. Narrow the filters, then export again.",
            )
            return redirect(back_url)
        rows = access_report.describe(list(records[:EXPORT_LIMIT]), history=False)
        out, writer = _writer()
        writer.writerow(UNUSED_HEADER)
        for row in rows:
            line = [
                row.name,
                row.username,
                row.kind,
                row.organization,
                _items(row.roles),
                _items(row.capabilities),
                _when(row.last_sign_in) or "Never",
                _when(row.joined),
                days_unused(row, now),
                period.days,
            ]
            writer.writerow([_cell(value) for value in line])
    response = _csv_response(out.getvalue(), "access-review-unused-accounts")
    use = _use(request, UseKind.EXPORTED, Source.UNUSED, form, total)
    use.filters["days"] = str(period.days)
    access.keep(request, use)
    return response


# ------------------------------------------------------------------------------ the period


def _period_reading():
    """The period and its history (access_report.period_history) read at one moment: in a page's
    own reading (one_reading), or, for a form sent and drawn again, holding the period's lock
    shared across both readings, so that no save of the period commits between them. Only after
    the service has run: this request then asks for the lock no more."""
    with transaction.atomic(), connection.cursor() as cursor:
        # A transaction of its own where the request has none (no ATOMIC_REQUESTS): the lock is
        # held across both readings either way.
        cursor.execute(
            "SELECT pg_advisory_xact_lock_shared(%s, %s)", list(access_report.PERIOD_LOCK)
        )
        return access_report.period(), list(access_report.period_history())


@login_required
@_form_reading
def unused_period(request):
    """Set after how many days without a sign-in an active account is unused."""
    require_reviewer(request.user)
    if request.method == "POST":
        # Decided by the service, which takes the period's lock; read for the page only if it is
        # drawn again (never before the service: the lock would be taken shared, then asked for
        # exclusively, and two saves at once would wait for each other).
        current = history = None
        form = PeriodForm(request.POST)  # an empty one is checked too
    else:
        current, history = _period_reading()  # in the page's own reading
        form = PeriodForm(initial={"days": current.days, "expected_revision": current.revision})
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            _stored, changed = access_report.set_period(
                actor=request.user,
                days=data["days"],
                expected_revision=data["expected_revision"],
                reason=data["reason"],
            )
        except ValidationError as exc:
            _errors(form, exc)
        except PermissionDenied as exc:
            form.add_error(None, _refused(request, exc))
        else:
            messages.success(
                request,
                "Unused account period saved." if changed else "The period is unchanged.",
            )
            return redirect(reverse("access_review:unused"))
    if current is None:
        # A form sent and refused, whatever for: drawn again with the period and its history as
        # they stand now, read together, and their revision, so that it can be sent again once
        # compared.
        current, history = _period_reading()
        _carry_revision(form, current.revision, "the period")
    title = "Unused account period"
    return _render(
        request,
        "audit_review/access_period_form.html",
        {
            "page_title": title,
            "form": form,
            "current": current,
            "history": history,
            "submit_label": "Save period",
            "cancel_url": reverse("access_review:unused"),
            "crumbs": [
                ("Access review", None),
                ("Unused accounts", reverse("access_review:unused")),
                (title, None),
            ],
        },
    )
