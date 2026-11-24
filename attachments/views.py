"""Download a file, and withdraw one (supervisors).

The download is served as an attachment with a generated name, the type decided at upload, no
sniffing, no caching and a sandboxing content policy, so a browser never renders it in the
application's origin. Authorization is `services.readable_attachment`: out of scope is a 404 for
every audience, including a portal account guessing the id of a file on an internal note. Each
download is recorded as an access record (core.access), never with the file's name or content.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from accounts.roles import require_staff
from core import access
from core.models import AccessKind
from desk.permissions import is_supervisor
from desk.selectors import staff_requests

from .forms import WithdrawForm
from .models import Attachment
from .services import file_bytes, readable_attachment, withdraw_attachment


def request_page(service_request):
    from django.urls import reverse

    return reverse("desk:request-detail", args=[service_request.pk])


@never_cache
@login_required
@require_GET
def download(request, pk):
    reader = request.user  # the account the file is sent to (core.access)
    try:
        attachment = readable_attachment(reader, pk)
        data = file_bytes(attachment)
    except ObjectDoesNotExist as error:
        raise Http404 from error
    access.note(
        request, AccessKind.FILE, ticket_id=attachment.request_id, target=attachment, account=reader
    )
    response = HttpResponse(bytes(data), content_type=attachment.content_type)
    response["Content-Disposition"] = f'attachment; filename="{attachment.download_name}"'
    response["Content-Length"] = str(attachment.size)
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response["Cache-Control"] = "private, no-store"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    return response


@login_required
def withdraw(request, pk):
    require_staff(request.user)
    if not is_supervisor(request.user):
        raise Http404
    try:
        attachment = Attachment.objects.select_related("request", "entry").get(
            pk=pk, request__in=staff_requests(request.user)
        )
    except Attachment.DoesNotExist as error:
        raise Http404 from error
    back = request_page(attachment.request)
    form = WithdrawForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            withdraw_attachment(
                actor=request.user, attachment_id=attachment.pk, **form.cleaned_data
            )
        except ObjectDoesNotExist as error:  # left the actor's scope while the change waited
            raise Http404 from error
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, "File withdrawn. Clients no longer reach it.")
            return redirect(back)
    return render(
        request,
        "attachments/withdraw.html",
        {
            "page_title": "Withdraw file",
            "form": form,
            "attachment": attachment,
            "crumbs": [(attachment.request.number, back), ("Withdraw file", None)],
            "cancel_url": back,
        },
    )
