"""Reject oversized declared bodies before CSRF or views parse uploaded data.

This is a Content-Length guard, not a streaming limit. The HTTP server/proxy must
also bound bodies with missing or inaccurate lengths before passing them to Django.
A non-empty length containing anything but ASCII digits is refused with HTTP 400.
"""

from django.conf import settings
from django.http import HttpResponse
from django.utils.cache import add_never_cache_headers

# The largest supported upload is 10 MiB; leave room for multipart/form fields.
DEFAULT_MAX_REQUEST_BODY_BYTES = 12 * 1024 * 1024


class RequestBodyLimitMiddleware:
    """Place after request logging and before any middleware that can read the body."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.limit = str(
            getattr(settings, "MAX_REQUEST_BODY_BYTES", DEFAULT_MAX_REQUEST_BODY_BYTES)
        )

    def __call__(self, request):
        length = request.META.get("CONTENT_LENGTH", "")
        if length and not (length.isascii() and length.isdigit()):
            return _refusal("The request length is invalid. Reload the page and try again.\n", 400)
        length = length.lstrip("0")
        # Compare decimal text so even a declared length with thousands of digits
        # can be rejected without Python's integer-string conversion limit.
        if len(length) > len(self.limit) or (
            len(length) == len(self.limit) and length > self.limit
        ):
            return _refusal(
                "Request body is too large. Reduce the upload or form data and try again.\n", 413
            )
        return self.get_response(request)


def _refusal(text, status):
    """A refusal that precedes every other middleware: never stored (a module address's
    responses never are, and this one is decided before the address is known)."""
    response = HttpResponse(text, status=status, content_type="text/plain; charset=utf-8")
    add_never_cache_headers(response)
    return response
