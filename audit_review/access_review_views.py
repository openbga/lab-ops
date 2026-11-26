"""The Reviews tab of Access review (audit_review.access_reviews): the list of signed access
reviews, one review's page and its decisions as a CSV file, the list's export, and recording and
signing a review. For holders of "Can review access and audit records" (audit_review.permissions).

Pages are drawn in one reading (core.concurrency.one_reading) with the account read in it. Every
page of the list, and its export, is drawn as of the moment its first page was (`upto`, the newest
access review id then). Each page of the list, each review page, each export and each drawing of
the form, which lists every grant in its scope, is recorded (WorkspaceUse) with the answer it is
about (core.access.keep). A form sent is decided by the service, which holds the acting account,
checks again that it may review and reads the grants again as they stand.
"""

import csv
import io
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from core import access
from core.concurrency import one_reading, reading_account
from core.ids import whole_number

from . import access_reviews
from .access_review_forms import AccessReviewForm, ScopeForm, shown
from .models import AccessReview, Decision, Source, UseKind, WorkspaceUse
from .permissions import require_reviewer
from .review_views import _errors, _form_reading, _refused, _render
from .sources import EXPORT_LIMIT, PAGE_SIZE
from .views import _cell

LIST_TITLE = "Access review: reviews"
CHOICE_REFUSED = (
    "The accounts this form was sent for were not accepted, so it now lists the staff accounts. "
    "Check the decisions below, then sign again."
)


def _list_url():
    return reverse("access_review:reviews")


def _review_url(review):
    return reverse("access_review:review", args=[review.pk])


def _use(request, kind, rows, **filters):
    account = access.request_account(request)
    return WorkspaceUse(
        account_id=account.pk,
        kind=kind,
        source=Source.ACCESS_REVIEWS,
        filters={name: str(value) for name, value in filters.items() if value is not None},
        rows=rows,
        request_ref=getattr(request, "request_id", "") or "",
    )


def _upto(request):
    """What the list is drawn as of: `upto` from the address, never beyond the newest review now."""
    now = AccessReview.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
    upto = whole_number(request.GET.get("upto", ""), max_digits=19)
    return now if upto is None else min(upto, now)


def _records(upto):
    return AccessReview.objects.filter(pk__lte=upto).order_by("-pk")


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


def _when(moment):
    return timezone.localtime(moment).isoformat(timespec="seconds")


# ------------------------------------------------------------------------------ the list


@transaction.non_atomic_requests  # one_reading opens its own transaction (ATOMIC_REQUESTS)
@login_required
@require_GET
def reviews_page(request):
    """The Reviews tab: the signed access reviews, newest first."""
    with one_reading():
        account = reading_account(request)
        require_reviewer(account)
        upto = _upto(request)
        page_obj = Paginator(_records(upto), PAGE_SIZE).get_page(request.GET.get("page"))
        total = page_obj.paginator.count
        query = request.GET.copy()
        query["upto"] = str(upto)
        request.GET = query  # the pager's links carry the reading's moment
        response = render(
            request,
            "audit_review/access_reviews.html",
            {
                "page_title": LIST_TITLE,
                "page_obj": page_obj,
                "total": total,
                "last": access_reviews.latest(),
                "export_url": f"{reverse('access_review:reviews-export')}?"
                f"{urlencode({'upto': upto})}",
                "export_limit": EXPORT_LIMIT,
                "exportable": 0 < total <= EXPORT_LIMIT,
                "create_url": reverse("access_review:review-create"),
            },
        )
    access.keep(request, _use(request, UseKind.VIEWED, total, page=page_obj.number))
    return response


LIST_HEADER = (
    "Access review",
    "Accounts reviewed",
    "Accounts",
    "Grants",
    "Decided to remove",
    "Findings",
    "Follow-up",
    "Signed by",
    "Username",
    "Roles",
    "Meaning",
    "Signed at",
    "SHA-256 checksum",
)


@transaction.non_atomic_requests
@login_required
@require_GET
def reviews_export(request):
    """Every access review as of the reading's moment, as a CSV file."""
    with one_reading():
        account = reading_account(request)
        require_reviewer(account)
        upto = _upto(request)
        records = _records(upto)
        total = records.count()
        if total > EXPORT_LIMIT:
            messages.error(
                request,
                f"An export holds at most {EXPORT_LIMIT:,} rows and this list now has {total:,}.",
            )
            return redirect(f"{_list_url()}?{urlencode({'upto': upto})}")
        out, writer = _writer()
        writer.writerow(LIST_HEADER)
        for review in records[:EXPORT_LIMIT]:
            line = [
                review.number,
                review.scope_label,
                review.accounts,
                review.grants,
                review.removals,
                review.findings,
                review.follow_up,
                review.signer_name,
                review.signer_username,
                review.signer_roles,
                review.get_meaning_display(),
                _when(review.signed_at),
                review.checksum,
            ]
            writer.writerow([_cell(value) for value in line])
    response = _csv_response(out.getvalue(), "access-review-reviews")
    access.keep(request, _use(request, UseKind.EXPORTED, total))
    return response


# ------------------------------------------------------------------------------ one review


def _review(pk):
    review = AccessReview.objects.filter(pk=pk).first()
    if review is None:
        raise Http404("No such access review.")
    return review


@transaction.non_atomic_requests
@login_required
@require_GET
def review_page(request, pk):
    """One access review: every grant reviewed with its decision, whether each grant decided for
    removal is still held, the findings and follow-up, and the sign-off with its checksum."""
    with one_reading():
        account = reading_account(request)
        require_reviewer(account)
        review = _review(pk)
        items = list(review.items.order_by("position"))
        held = access_reviews.still_held(item for item in items if item.decision == Decision.REMOVE)
        groups = access_reviews.by_account(items)
        for group in groups:
            group.rows = [(item, held.get(item.pk)) for item in group.grants]
        removals = [(item, held[item.pk]) for item in items if item.decision == Decision.REMOVE]
        response = render(
            request,
            "audit_review/access_review_detail.html",
            {
                "page_title": f"Access review {review.number}",
                "review": review,
                "accounts": groups,
                "removals": removals,
                "still_held": sum(1 for _item, is_held in removals if is_held),
                "intact": access_reviews.intact(review, items),
                "list_url": _list_url(),
                "export_url": reverse("access_review:review-export", args=[review.pk]),
            },
        )
    access.keep(request, _use(request, UseKind.VIEWED, len(items), access_review=review.pk))
    return response


ITEM_HEADER = (
    "Access review",
    "Person",
    "Username",
    "Client organization",
    "Grant",
    "Detail",
    "Decision",
    "Held now",
    "Signed at",
)


@transaction.non_atomic_requests
@login_required
@require_GET
def review_export(request, pk):
    """One access review's decisions, one row a grant, as a CSV file, with whether each grant
    decided for removal is still held as the file is made."""
    with one_reading():
        account = reading_account(request)
        require_reviewer(account)
        review = _review(pk)
        items = list(review.items.order_by("position"))
        held = access_reviews.still_held(item for item in items if item.decision == Decision.REMOVE)
        out, writer = _writer()
        writer.writerow(ITEM_HEADER)
        for item in items:
            if item.decision == Decision.REMOVE:
                now = "Yes" if held.get(item.pk) else "No"
            else:
                now = ""
            line = [
                review.number,
                item.account_name,
                item.account_username,
                item.organization_name,
                item.label,
                item.detail,
                item.get_decision_display(),
                now,
                _when(review.signed_at),
            ]
            writer.writerow([_cell(value) for value in line])
    response = _csv_response(out.getvalue(), f"access-review-{review.number}")
    access.keep(request, _use(request, UseKind.EXPORTED, len(items), access_review=review.pk))
    return response


# ------------------------------------------------------------------------------ the form


@login_required
@_form_reading
def review_create(request):
    """Record an access review and sign it: the grants of the accounts chosen, a decision for
    each, and the reviewer's signature."""
    require_reviewer(request.user)
    if request.method == "POST":
        form = None
        choice = ScopeForm(
            {
                "scope": request.POST.get("scope", ""),
                "organization": request.POST.get("organization", ""),
            }
        )
    else:
        choice = ScopeForm(request.GET or None)
    scope, organization = choice.chosen()
    current = access_reviews.reading(scope, organization)
    if request.method == "POST":
        form = AccessReviewForm(request.POST, grants=current.grants, request=request)
        if not choice.is_valid():
            form = form.redraw(
                current.grants, current.fingerprint, CHOICE_REFUSED, scope, organization
            )
        elif request.POST.get("expected") != current.fingerprint:
            # Changed since the form was drawn: drawn again as the grants stand, before the
            # password is checked (a form sent again costs no attempt).
            form = form.redraw(
                current.grants, current.fingerprint, access_reviews.STALE, scope, organization
            )
        elif form.check_password():
            data = form.cleaned_data
            try:
                review = access_reviews.sign_access_review(
                    actor=request.user,
                    scope=scope,
                    organization=organization,
                    decisions=form.decisions(),
                    expected=data["expected"],
                    findings=data["findings"],
                    follow_up=data["follow_up"],
                    request_ref=getattr(request, "request_id", "") or "",
                    password_hash=form.checked_hash,
                )
            except ValidationError as exc:
                if access_reviews.STALE in exc.messages:
                    current = access_reviews.reading(scope, organization)
                    form = form.redraw(
                        current.grants,
                        current.fingerprint,
                        access_reviews.STALE,
                        scope,
                        organization,
                    )
                else:
                    _errors(form, exc)
            except PermissionDenied as exc:
                form.add_error(None, _refused(request, exc))
            else:
                messages.success(request, f"Access review {review.number} signed.")
                return redirect(_review_url(review))
    else:
        form = AccessReviewForm(
            initial={
                "scope": scope,
                "organization": organization.pk if organization is not None else None,
                "expected": current.fingerprint,
                "shown": shown(current.grants),
            },
            grants=current.grants,
            request=request,
        )
    title = "Record an access review"
    response = _render(
        request,
        "audit_review/access_review_form.html",
        {
            "page_title": title,
            "form": form,
            "choice": choice,
            "scope": scope,
            "organization": organization,
            "reading": current,
            "accounts": form.accounts(),
            "undecided": form.undecided(),
            "signer": request.user,
            "submit_label": "Sign access review",
            "cancel_url": _list_url(),
            "most_accounts": access_reviews.MOST_ACCOUNTS,
            "most_grants": access_reviews.MOST_GRANTS,
            "crumbs": [("Access review", None), ("Reviews", _list_url()), (title, None)],
        },
    )
    access.keep(
        request,
        _use(
            request,
            UseKind.VIEWED,
            len(current.grants),
            form="new",
            scope=scope,
            organization=organization.pk if organization is not None else None,
        ),
    )
    return response
