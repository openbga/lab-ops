"""Confirm it is you before revealing patient information (core.reveal_confirmation): the answer a
reveal gives when it needs a confirmation first, and the page that takes it.

A reveal asked to wait sends the person to Confirm it is you (HTMX's HX-Redirect, or a redirect
without script), with the page they were on as the page to return to. Confirmed, the page records
it for the session and returns them there; the reveal itself is chosen again, so nothing is
revealed by a request the person did not send after confirming.
"""

from urllib.parse import urlencode, urlsplit

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from accounts import confirmation
from accounts.roles import require_staff

from . import reveal_confirmation
from .lab_profile_forms import ConfirmBeforeRevealForm


def _on_this_site(request, target):
    """`target` as a path and query on this site, or None."""
    if not isinstance(target, str) or not target:
        return None
    if not url_has_allowed_host_and_scheme(
        target, {request.get_host()}, require_https=request.is_secure()
    ):
        return None
    parts = urlsplit(target)
    if not parts.path.startswith("/") or parts.path.startswith("//"):
        return None
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _page_on_screen(request):
    """The page the reveal was sent from (HTMX's current address, else the referrer), on this
    site, or None."""
    for header in ("HX-Current-URL", "Referer"):
        page = _on_this_site(request, request.headers.get(header, ""))
        if page:
            return page
    return None


def ask(request):
    """The answer to a reveal that needs a confirmation first: go to Confirm it is you, which
    returns to the page the reveal was sent from. Not cached, like every reveal's answer."""
    target = reverse("account-confirm-reveal")
    back = _page_on_screen(request)
    if back:
        target += "?" + urlencode({"next": back})
    if request.headers.get("HX-Request"):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = target
    else:
        response = redirect(target)
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response


@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
@login_required
def confirm_before_reveal(request):
    """Confirm it is you before revealing patient information: the password, and the second factor
    where the account has one. Staff only. Once this session has confirmed, or when the
    laboratory does not ask, it goes straight back."""
    require_staff(request.user)
    posted = request.method == "POST"
    back = _on_this_site(request, (request.POST if posted else request.GET).get("next", ""))
    back = back or reverse("dashboard")
    if not reveal_confirmation.asks(request):
        return redirect(back)
    if confirmation.current(request):
        # Confirmed for another change within the window, here or in another tab: it counts, as
        # it does for a reveal, so there is nothing to ask.
        reveal_confirmation.remember(request)
        return redirect(back)
    form = ConfirmBeforeRevealForm(request.POST if posted else None, initial={"next": back})
    if posted and form.is_valid():
        reveal_confirmation.remember(request)
        messages.success(
            request, "Confirmed. You can reveal patient information until this session ends."
        )
        return redirect(back)
    return render(
        request,
        "core/confirm_before_reveal.html",
        {"page_title": "Confirm it is you", "form": form, "cancel_url": back},
    )
