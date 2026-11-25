"""The Reviews tab of the audit review workspace (audit_review.reviews): the list of review records
with where reviews stand, a review's page, recording and signing a review, adding a note, and the
review interval. For holders of "Can review access and audit records" (audit_review.permissions).

Pages are drawn in one reading (core.concurrency.one_reading) with the account read in it. Every
page of the list, and its export, is drawn as of the moment its first page was (`upto`, the newest
review id then). Each page of the list, each review page shown and each export is recorded
(WorkspaceUse) with the answer it is about (core.access.keep), as the other tabs are. A form opened
with GET is drawn in one reading; a form sent is decided by the service, which holds the acting
account and checks again that it may review.
"""

import csv
import io
from functools import wraps
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.models import Count
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.views.decorators.http import require_GET

from core import access
from core.concurrency import one_reading, reading_account
from core.ids import whole_number

from . import reviews
from .models import EXAMINED, Review, Source, UseKind, WorkspaceUse
from .permissions import may_review, require_reviewer
from .review_forms import IntervalForm, NoteForm, ReviewForm
from .sources import EXPORT_LIMIT, PAGE_SIZE
from .views import ROUTES, _cell

LIST_TITLE = "Audit review: reviews"


def _list_url():
    return reverse("audit_review:reviews")


def _review_url(review):
    return reverse("audit_review:review", args=[review.pk])


def _use(request, kind, rows, **filters):
    account = access.request_account(request)
    return WorkspaceUse(
        account_id=account.pk,
        kind=kind,
        source=Source.REVIEWS,
        filters={name: str(value) for name, value in filters.items() if value is not None},
        rows=rows,
        request_ref=getattr(request, "request_id", "") or "",
    )


def _upto(request):
    """What the list is drawn as of: `upto` from the address, never beyond the newest review now."""
    now = Review.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
    upto = whole_number(request.GET.get("upto", ""), max_digits=19)
    return now if upto is None else min(upto, now)


def _records(upto):
    return Review.objects.filter(pk__lte=upto).annotate(note_count=Count("notes")).order_by("-pk")


@transaction.non_atomic_requests  # one_reading opens its own transaction (ATOMIC_REQUESTS)
@login_required
@require_GET
def reviews_page(request):
    """The Reviews tab: where reviews stand, and the review records, newest first."""
    with one_reading():
        account = reading_account(request)
        require_reviewer(account)
        upto = _upto(request)
        state = reviews.status()
        page_obj = Paginator(_records(upto), PAGE_SIZE).get_page(request.GET.get("page"))
        total = page_obj.paginator.count
        query = request.GET.copy()
        query["upto"] = str(upto)
        request.GET = query  # the pager's links carry the reading's moment
        response = render(
            request,
            "audit_review/reviews.html",
            {
                "page_title": LIST_TITLE,
                "state": state,
                "page_obj": page_obj,
                "total": total,
                "export_url": f"{reverse('audit_review:reviews-export')}?"
                f"{urlencode({'upto': upto})}",
                "export_limit": EXPORT_LIMIT,
                "exportable": 0 < total <= EXPORT_LIMIT,
                "review_create_url": reverse("audit_review:review-create"),
            },
        )
    access.keep(request, _use(request, UseKind.VIEWED, total, page=page_obj.number))
    return response


@transaction.non_atomic_requests
@login_required
@require_GET
def reviews_export(request):
    """Every review as of the reading's moment, as a CSV file."""
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
        content = _csv(records[:EXPORT_LIMIT])
    response = HttpResponse(content, content_type="text/csv; charset=utf-8")
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    response["Content-Disposition"] = f'attachment; filename="audit-review-reviews-{stamp}.csv"'
    access.keep(request, _use(request, UseKind.EXPORTED, total))
    return response


EXPORT_HEADER = (
    "Review",
    "From",
    "To",
    "Lists examined",
    "Other records examined",
    "Outcome",
    "Findings",
    "Follow-up",
    "Signed by",
    "Username",
    "Roles",
    "Meaning",
    "Signed at",
    "SHA-256 checksum",
    "Notes",
)


def _csv(records):
    out = io.StringIO()
    out.write("\ufeff")  # spreadsheets then read the file as UTF-8
    writer = csv.writer(out)
    writer.writerow(EXPORT_HEADER)
    for review in records:
        line = [
            review.number,
            review.period_from.isoformat(),
            review.period_to.isoformat(),
            "; ".join(review.examined_labels()),
            review.examined_other,
            review.get_outcome_display(),
            review.findings,
            review.follow_up,
            review.signer_name,
            review.signer_username,
            review.signer_roles,
            review.get_meaning_display(),
            timezone.localtime(review.signed_at).isoformat(timespec="seconds"),
            review.checksum,
            review.note_count,
        ]
        writer.writerow([_cell(value) for value in line])
    return out.getvalue()


def _examined_links(review):
    """Each list the review examined, opened on the review's period."""
    period = urlencode(
        {"date_from": review.period_from.isoformat(), "date_to": review.period_to.isoformat()}
    )
    links = []
    for source in EXAMINED:
        if source.value in review.examined:
            route = "audit_review:people" if source == Source.PEOPLE else ROUTES[source]
            links.append((source.label, f"{reverse(route)}?{period}"))
    return links


@transaction.non_atomic_requests
@login_required
@require_GET
def review_page(request, pk):
    """One review: what was examined and found, the sign-off and its checksum, and the notes."""
    with one_reading():
        account = reading_account(request)
        require_reviewer(account)
        review = Review.objects.filter(pk=pk).first()
        if review is None:
            raise Http404("No such review.")
        notes = list(review.notes.order_by("pk"))
        response = render(
            request,
            "audit_review/review_detail.html",
            {
                "page_title": f"Review {review.number}",
                "review": review,
                "notes": notes,
                "intact": reviews.intact(review),
                "examined_links": _examined_links(review),
                "list_url": _list_url(),
            },
        )
    access.keep(request, _use(request, UseKind.VIEWED, 1, review=review.pk))
    return response


# ------------------------------------------------------------------------------ forms


def _form_reading(view):
    """A form page opened with GET is drawn in one reading with the account read in it (and its
    access checked there); a form sent runs in the request's transaction where the deployment asks
    for one (ATOMIC_REQUESTS), and its service decides with the account as it holds it."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.method == "POST":
            if connection.settings_dict.get("ATOMIC_REQUESTS"):
                with transaction.atomic():
                    return view(request, *args, **kwargs)
            return view(request, *args, **kwargs)
        with one_reading():
            reading_account(request)
            return view(request, *args, **kwargs)

    return transaction.non_atomic_requests(wrapped)


def _errors(form, exc):
    if hasattr(exc, "error_dict"):
        for name, errors in exc.error_dict.items():
            target = name if name in form.fields else None
            for error in errors:
                form.add_error(target, error)
    else:
        for message in exc.messages:
            form.add_error(None, message)


def _interval_reading():
    """The interval and its history (reviews.interval_history) read at one moment: in a page's
    own reading (one_reading), or, for a form sent and drawn again, holding the interval's lock
    shared across both readings, so that no save of the interval commits between them. Only after
    the service has run: this request then asks for the lock no more."""
    with transaction.atomic(), connection.cursor() as cursor:
        # A transaction of its own where the request has none (no ATOMIC_REQUESTS): the lock is
        # held across both readings either way.
        cursor.execute("SELECT pg_advisory_xact_lock_shared(%s, %s)", list(reviews.SCHEDULE_LOCK))
        return reviews.schedule(), list(reviews.interval_history())


REDRAWN = "This form is shown again as {what} now stands. Check it, then send it again."


def _carry_revision(form, revision, what):
    """A refused form drawn again posts `revision`, read with what the page now shows. A revision
    that was missing or not a number is repaired so, and no field on the page can be corrected for
    it: when nothing else on the form is refused, it is said in words (`what` the form shows);
    otherwise the visible fields' errors guide, and the error summary counts only them."""
    form.data = form.data.copy()
    form.data["expected_revision"] = str(revision)
    if form.errors.pop("expected_revision", None) and not form.errors:
        form.add_error(None, REDRAWN.format(what=what))


def _refused(request, exc):
    """A service refused the account: it may no longer review. 403, as for a page opened now."""
    if not may_review(request.user):
        raise exc
    return str(exc)


def _render(request, template, context):
    response = render(request, template, context)
    add_never_cache_headers(response)  # a refused page shows what was typed again
    return response


def _crumbs(title, review=None):
    crumbs = [("Reviews", _list_url())]
    if review is not None:
        crumbs.append((review.number, _review_url(review)))
    crumbs.append((title, None))
    return crumbs


@login_required
@_form_reading
def review_create(request):
    """Record a review and sign it."""
    require_reviewer(request.user)
    state = reviews.status()
    if request.method == "POST":
        form = ReviewForm(request.POST, request=request)
    else:
        start, end = reviews.suggested_period(state)
        form = ReviewForm(initial={"period_from": start, "period_to": end}, request=request)
    if request.method == "POST" and form.check_password():
        data = form.cleaned_data
        try:
            review = reviews.sign_review(
                actor=request.user,
                period_from=data["period_from"],
                period_to=data["period_to"],
                examined=data["examined"],
                examined_other=data["examined_other"],
                outcome=data["outcome"],
                findings=data["findings"],
                follow_up=data["follow_up"],
                request_ref=getattr(request, "request_id", "") or "",
                password_hash=form.checked_hash,
            )
        except ValidationError as exc:
            _errors(form, exc)
        except PermissionDenied as exc:
            form.add_error(None, _refused(request, exc))
        else:
            messages.success(request, f"Review {review.number} signed.")
            return redirect(_review_url(review))
    title = "Record a review"
    return _render(
        request,
        "audit_review/review_form.html",
        {
            "page_title": title,
            "form": form,
            "state": state,
            "signer": request.user,
            "submit_label": "Sign review",
            "cancel_url": _list_url(),
            "crumbs": _crumbs(title),
        },
    )


@login_required
@_form_reading
def review_note(request, pk):
    """Add a note to a signed review."""
    require_reviewer(request.user)
    review = Review.objects.filter(pk=pk).first()
    if review is None:
        raise Http404("No such review.")
    # The notes the form shows and the revision it posts, from one statement.
    notes = list(review.notes.order_by("pk"))
    sent = request.POST if request.method == "POST" else None  # an empty one is checked too
    form = NoteForm(sent, initial={"expected_revision": 1 + len(notes)})
    if request.method == "POST" and form.is_valid():
        try:
            reviews.add_note(
                actor=request.user,
                review=review,
                text=form.cleaned_data["text"],
                expected_revision=form.cleaned_data["expected_revision"],
                request_ref=getattr(request, "request_id", "") or "",
            )
        except ValidationError as exc:
            _errors(form, exc)
            if reviews.STALE_NOTE in exc.messages:
                # Drawn again with the notes as they stand now, so that the note can be sent again
                # once they are read.
                notes = list(review.notes.order_by("pk"))
        except PermissionDenied as exc:
            form.add_error(None, _refused(request, exc))
        else:
            messages.success(request, f"Note added to review {review.number}.")
            return redirect(_review_url(review))
    if request.method == "POST":
        # A form sent and drawn again posts the revision the notes it shows make, whatever it
        # was refused for (an empty field, a note added meanwhile).
        _carry_revision(form, 1 + len(notes), "the review")
    title = "Add a note"
    return _render(
        request,
        "audit_review/review_note_form.html",
        {
            "page_title": title,
            "form": form,
            "review": review,
            "notes": notes,
            "submit_label": "Add note",
            "cancel_url": _review_url(review),
            "crumbs": _crumbs(title, review),
        },
    )


@login_required
@_form_reading
def review_interval(request):
    """Set how often reviews are due."""
    require_reviewer(request.user)
    if request.method == "POST":
        # Decided by the service, which takes the interval's lock; read for the page only if it
        # is drawn again (never before the service: the lock would be taken shared, then asked
        # for exclusively, and two saves at once would wait for each other).
        current = history = None
        form = IntervalForm(request.POST)  # an empty one is checked too
    else:
        current, history = _interval_reading()  # in the page's own reading
        form = IntervalForm(
            initial={"interval_days": current.interval_days, "expected_revision": current.revision}
        )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            _stored, changed = reviews.set_interval(
                actor=request.user,
                interval_days=data["interval_days"],
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
                "Review interval saved." if changed else "The review interval is unchanged.",
            )
            return redirect(_list_url())
    if current is None:
        # A form sent and refused, whatever for: drawn again with the interval and its history as
        # they stand now, read together, and their revision, so that it can be sent again once
        # compared.
        current, history = _interval_reading()
        _carry_revision(form, current.revision, "the interval")
    title = "Review interval"
    return _render(
        request,
        "audit_review/review_interval_form.html",
        {
            "page_title": title,
            "form": form,
            "current": current,
            "history": history,
            "submit_label": "Save interval",
            "cancel_url": _list_url(),
            "crumbs": _crumbs(title),
        },
    )
