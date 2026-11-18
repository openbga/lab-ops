"""Browser policies: local assets only, never framed, no device access."""

from django.conf import settings

DEFAULT_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)
DEFAULT_PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"


class SecurityHeadersMiddleware:
    """Wrap even early refusals and WhiteNoise responses; never read the request body."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        policy = getattr(settings, "CONTENT_SECURITY_POLICY", DEFAULT_CONTENT_SECURITY_POLICY)
        response.setdefault("Content-Security-Policy", policy)
        response.setdefault("Permissions-Policy", DEFAULT_PERMISSIONS_POLICY)
        return response
