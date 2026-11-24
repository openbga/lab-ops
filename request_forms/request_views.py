"""Staff fill or correct the values of a request's form (F4). Any staff role, on an open request it
may read (desk.selectors.staff_requests, checked again before drawing; anything else answers
404; a closed request's values are frozen). Patient-information fields are
offered only with `view_patient_information`; request_forms.services checks it all again."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from accounts.acting import hold_actor
from accounts.roles import require_staff
from core import reveal_confirmation, reveal_confirmation_views
from desk.models import ServiceRequest, Status
from desk.selectors import staff_requests

from . import kinds
from .forms import CorrectionForm
from .models import FieldValue, FilledForm
from .selectors import may_see_patient_information, staff_values
from .services import CLOSED_MESSAGE, FORM_CHANGED, correct_values, is_form_changed
from .services import reveal_value as services_reveal_value


def request_url(service_request):
    """The staff request page, at its form details."""
    return f"{reverse('desk:request-detail', args=[service_request.pk])}#form-details-heading"


def _earlier_values(filled, user):
    """Superseded values, newest first, as this staff account may see them. A patient-information
    row never shows its value; without the capability it is a bare placeholder (no person, time
    or reason), with it the reason code and note."""
    sees_patient = may_see_patient_information(user)
    rows = []
    for row in (
        filled.values.filter(superseded_at__isnull=False)
        .select_related("field", "entered_by", "replaced_by", "replaced_by__entered_by")
        .order_by("-superseded_at", "-pk")
    ):
        patient = row.holds_patient_information or row.field.holds_patient_information
        replacement = getattr(row, "replaced_by", None)
        if patient and not sees_patient:
            rows.append(
                {
                    "title": f"{row.label}: patient information (not shown)",
                    "detail": None,
                    "replaced_at": None,
                    "replaced_by": None,
                    "reason": None,
                }
            )
            continue
        if patient:
            shown = "patient information (not shown)"
        else:
            shown = "; ".join(kinds.display(row.kind, row.value)) or "blank"
        reason = replacement.reason if replacement else ""
        if replacement and replacement.reason_code:
            label = replacement.get_reason_code_display()
            reason = f"{label}. {reason}" if reason and reason != label else label
        who = row.entered_by.get_full_name() or row.entered_by.get_username()
        entered = timezone.localtime(row.entered_at)
        rows.append(
            {
                "title": f"{row.label}: {shown}",
                "detail": f"Entered by {who} on {entered:%Y-%m-%d %H:%M}",
                "replaced_at": row.superseded_at,
                "replaced_by": replacement.entered_by if replacement else None,
                "reason": reason,
            }
        )
    return rows


@never_cache  # a failed submission re-renders typed values, patient information included
@login_required
def edit_values(request, number):
    require_staff(request.user)
    service_request = get_object_or_404(staff_requests(request.user), number=number)
    filled = FilledForm.objects.filter(request=service_request).select_related("template").first()
    if filled is None:
        raise Http404("This ticket was not raised with a form.")
    back = request_url(service_request)
    if service_request.status == Status.CLOSED:
        messages.info(request, CLOSED_MESSAGE)
        return redirect(back)
    form = CorrectionForm(filled, request.POST or None, user=request.user)
    changed = request.method == "POST" and form.definition_changed
    if request.method == "POST" and not changed and form.is_valid():
        try:
            changed = correct_values(
                actor=request.user,
                request=service_request,
                expected_definition=form.posted_definition,
                values=form.changed_values(),
                reason=form.cleaned_data.get("reason", ""),
                reason_code=form.cleaned_data.get("reason_code", ""),
                patient_note=form.cleaned_data.get("patient_note", ""),
                expected=form.expected(),
                kept_rows=form.kept_rows,
            )
        except ServiceRequest.DoesNotExist:
            raise Http404("No such ticket.") from None
        except ValidationError as exc:
            if is_form_changed(exc):
                changed = True
            else:
                form.add_value_errors(exc)
        else:
            messages.success(
                request,
                f"{len(changed)} value{'s' * (len(changed) != 1)} recorded on {number}.",
            )
            return redirect(back)
    if changed:
        # The form changed since the page was drawn, whichever layer noticed: a fresh page from
        # the current definition and the current values, with nothing that was typed.
        messages.error(request, FORM_CHANGED)
        form = CorrectionForm(filled, user=request.user)
    # Scope again before anything of the request is drawn: a request that left this person's
    # scope while they worked answers 404, refused submission included. What is
    # missing comes from the form's own reading, as its controls do.
    details = staff_values(
        service_request, request.user, defn=form.definition, current=form.current
    )
    if details is None:
        raise Http404("No such ticket.")
    return render(
        request,
        "request_forms/edit_values.html",
        {
            "page_title": f"Form details of {number}",
            "service_request": service_request,
            "filled": filled,
            "form": form,
            "details": details,
            "earlier": _earlier_values(filled, request.user),
            # From the form's own definition read, as its controls are.
            "hidden_fields": any(
                f.holds_patient_information for f in form.definition.value_fields()
            )
            and not may_see_patient_information(request.user),
            "back_url": back,
            "crumbs": [(number, back), ("Form details", None)],
        },
    )


@never_cache
@login_required
@require_POST
def reveal_value(request, pk):
    """One patient-information value: an HTMX fragment, or a small page without JavaScript.
    Staff holding view_patient_information, on a request they may read; audited; not cached.
    Where the laboratory asks staff to confirm it is them before the first reveal in a session
    (core.reveal_confirmation), a session that has not is sent to confirm first, decided in the
    reveal's transaction before anything is read."""
    require_staff(request.user)
    try:
        with transaction.atomic():
            hold_actor(request.user)  # before the rule's lock (accounts.acting)
            if not reveal_confirmation.may_reveal(request):
                return reveal_confirmation_views.ask(request)
            row = services_reveal_value(actor=request.user, value_id=pk)
    except FieldValue.DoesNotExist:
        raise Http404("No such value.") from None
    service_request = row.filled_form.request
    context = {
        "row": row,
        "lines": kinds.display(row.kind, row.value),
        "back": request_url(service_request),
    }
    if request.headers.get("HX-Request"):
        response = render(request, "request_forms/_revealed_value.html", context)
    else:
        context |= {
            "page_title": row.label,
            "crumbs": [(service_request.number, context["back"]), (row.label, None)],
        }
        response = render(request, "request_forms/revealed_value.html", context)
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response
