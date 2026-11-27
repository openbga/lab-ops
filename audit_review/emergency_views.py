"""The Emergency access tab of Access review (audit_review.emergency): every use of emergency
access, newest first, and one use's page with what it revealed and its review. For holders of "Can
review access and audit records" (audit_review.permissions).

Pages are drawn in one reading (core.concurrency.one_reading) with the account read in it. Every
page of the list is drawn as of the newest use when its first page was (`upto`), and its status
filter is applied in each page's reading. Each page of the list and each use's page shown is
recorded (WorkspaceUse) with the answer it is about (core.access.keep). A review sent is decided by
audit_review.emergency.record_review, which holds the acting account, locks the use and reads both
again as they stand; a refusal draws the page again from a new reading, with what was typed.
"""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from core import access
from core.concurrency import one_reading, reading_account
from core.ids import whole_number
from desk.models import EmergencyAccess, EmergencyOutcome

from . import emergency
from .models import Source, UseKind, WorkspaceUse
from .permissions import require_reviewer
from .sources import PAGE_SIZE

LIST_TITLE = "Access review: emergency access"


class StatusForm(forms.Form):
    """Which uses the list shows."""

    status = forms.ChoiceField(
        label="Status",
        choices=emergency.STATUS_CHOICES,
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class ReviewForm(forms.Form):
    outcome = forms.ChoiceField(
        label="Outcome",
        choices=EmergencyOutcome.choices,
        widget=forms.RadioSelect(attrs={"class": "form-check-input"}),
        error_messages={"required": emergency.OUTCOME_REQUIRED},
    )
    note = forms.CharField(
        label="What you checked and found",
        max_length=emergency.NOTE_LIMIT,
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
        help_text=(
            "For example whom you asked and whether the reason held. Leave out patient "
            "information. Kept with the review; it cannot be changed afterwards."
        ),
        error_messages={"required": emergency.NOTE_REQUIRED},
    )


def _use(request, kind, rows, **filters):
    account = access.request_account(request)
    return WorkspaceUse(
        account_id=account.pk,
        kind=kind,
        source=Source.EMERGENCY,
        filters={name: str(value) for name, value in filters.items() if value not in (None, "")},
        rows=rows,
        request_ref=getattr(request, "request_id", "") or "",
    )


def _upto(request):
    """What the list is drawn as of: `upto` from the address, never beyond the newest use now."""
    now = EmergencyAccess.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
    upto = whole_number(request.GET.get("upto", ""), max_digits=19)
    return now if upto is None else min(upto, now)


@transaction.non_atomic_requests  # one_reading opens its own transaction (ATOMIC_REQUESTS)
@login_required
@require_GET
def uses_page(request):
    """The Emergency access tab: every use, newest first, narrowed by status."""
    form = StatusForm(request.GET or None)
    status = form.cleaned_data["status"] if form.is_valid() else ""
    with one_reading():
        account = reading_account(request)
        require_reviewer(account)
        upto = _upto(request)
        rows = emergency.uses(status).filter(pk__lte=upto)
        page_obj = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page"))
        for use in page_obj:
            use.status = emergency.status_of(use)
            use.status_label = emergency.STATUS_LABELS[use.status]
        total = page_obj.paginator.count
        query = request.GET.copy()
        query["upto"] = str(upto)
        request.GET = query  # the pager's links carry the reading's moment
        response = render(
            request,
            "audit_review/access_emergency.html",
            {
                "page_title": LIST_TITLE,
                "form": form if form.is_bound else StatusForm(),
                "status": status,
                "page_obj": page_obj,
                "total": total,
                "waiting": emergency.waiting_count(),
            },
        )
    use = _use(request, UseKind.VIEWED, total, emergency_status=status, page=page_obj.number)
    access.keep(request, use)
    return response


def _draw_use(request, pk, form):
    from desk.selectors import staff_requests

    with one_reading():
        account = reading_account(request)
        require_reviewer(account)
        use = emergency.uses().filter(pk=pk).first()
        if use is None:
            raise Http404("No such use of emergency access.")
        status = emergency.status_of(use)
        own = use.user_id == account.pk
        reveals = emergency.reveals(use)
        drawable = status == emergency.Status.WAITING and not own
        # A review refused because the use changed since the page was opened, drawn where the form
        # is no longer offered.
        refusal = list(form.non_field_errors()) if form.is_bound and not drawable else []
        response = render(
            request,
            "audit_review/access_emergency_use.html",
            {
                "page_title": f"Emergency access {use.number}",
                "use": use,
                "status": status,
                "status_label": emergency.STATUS_LABELS[status],
                "review": use.review if status == emergency.Status.REVIEWED else None,
                "own": own,
                "form": form if drawable else None,
                "refusal": refusal,
                "reveals": reveals,
                "ticket_readable": staff_requests(account).filter(pk=use.ticket_id).exists(),
                "list_url": reverse("access_review:emergency"),
            },
        )
    access.keep(request, _use(request, UseKind.VIEWED, len(reveals), emergency_use=use.pk))
    return response


@transaction.non_atomic_requests  # one_reading and the service open their own transactions
@login_required
@require_http_methods(["GET", "HEAD", "POST"])
def use_page(request, pk):
    """One use: who, when, why, what it revealed, and its review or the form to record one."""
    form = ReviewForm(request.POST if request.method == "POST" else None)
    if request.method == "POST":
        require_reviewer(request.user)
        if form.is_valid():
            try:
                emergency.record_review(
                    actor=request.user,
                    use_id=pk,
                    outcome=form.cleaned_data["outcome"],
                    note=form.cleaned_data["note"],
                )
            except EmergencyAccess.DoesNotExist:
                raise Http404("No such use of emergency access.") from None
            except ValidationError as exc:
                if hasattr(exc, "message_dict"):
                    for name, errors in exc.message_dict.items():
                        form.add_error(name if name in form.fields else None, errors)
                else:
                    form.add_error(None, exc)
            else:
                messages.success(request, "Review recorded.")
                return redirect("access_review:emergency-use", pk=pk)
    return _draw_use(request, pk, form)
