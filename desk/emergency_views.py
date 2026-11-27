"""Starting emergency access from a ticket (desk.emergency): the page that says what it gives and
asks why, with Confirm it is you.

The page is drawn in one reading (core.concurrency.one_reading) with the account read in it: a
ticket the account may not read answers 404, an account that may not use emergency access 403.
An account that can see the patient information already, or has a use in force on the ticket, is
told so and offered no form, as is one for which the ticket holds no patient information hidden.
A form sent is checked for access (the ticket, the capability) before its password, then decided
by desk.emergency.start, which holds the account and reads it and the ticket again as they stand;
a refusal draws the page again from a new reading, with what was typed.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.views.decorators.http import require_http_methods

from accounts.roles import require_staff
from core.concurrency import one_reading, reading_account

from . import emergency
from .forms import EmergencyAccessForm
from .models import ServiceRequest
from .views import _back, _errors, _queue_url, _record_url, _request_or_404


def _local(moment):
    return timezone.localtime(moment).strftime("%H:%M")


def _draw(request, pk, form):
    with one_reading():
        account = reading_account(request)
        require_staff(account)
        ticket = _request_or_404(account, pk)
        if not emergency.is_eligible(account):
            raise PermissionDenied(emergency.NOT_ELIGIBLE)
        grant = emergency.patient_grant(account, ticket.pk)
        hidden = grant is None and emergency.has_hidden_patient_information(ticket)
        record_url = _record_url(ticket, _back(request))
        context = {
            "page_title": f"Emergency access: {ticket.number}",
            "req": ticket,
            "form": form if hidden else None,
            "nothing_hidden": grant is None and not hidden,
            "holds": grant == emergency.BY_CAPABILITY,
            "use": grant if grant not in (None, emergency.BY_CAPABILITY) else None,
            "minutes": emergency.MINUTES,
            "cancel_url": record_url,
            "list_query": _back(request),
            "crumbs": [
                ("Tickets", _queue_url(_back(request))),
                (ticket.number, record_url),
                ("Emergency access", None),
            ],
        }
        response = render(request, "desk/emergency_access.html", context)
    add_never_cache_headers(response)  # a refused form shows what was typed again
    return response


@login_required
@require_http_methods(["GET", "HEAD", "POST"])
def request_emergency(request, pk):
    require_staff(request.user)
    if request.method == "POST":
        # Access is decided before the form checks a password (accounts.confirmation): a request
        # refused here checks none.
        _request_or_404(request.user, pk)
        if not emergency.is_eligible(request.user):
            raise PermissionDenied(emergency.NOT_ELIGIBLE)
    form = EmergencyAccessForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            use = emergency.start(
                actor=request.user,
                ticket_id=pk,
                reason=form.cleaned_data["reason"],
                explanation=form.cleaned_data["explanation"],
            )
        except ServiceRequest.DoesNotExist:
            raise Http404("No such ticket.") from None
        except emergency.AlreadyInForce as exc:
            messages.info(
                request,
                f"Your emergency access to this ticket is already in force until "
                f"{_local(exc.use.ends_at)}.",
            )
            return redirect(_record_url(exc.use.ticket, _back(request)))
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(
                request,
                f"Emergency access started. You can reveal the patient information on "
                f"{use.ticket.number} until {_local(use.ends_at)}.",
            )
            return redirect(_record_url(use.ticket, _back(request)))
    return _draw(request, pk, form)
