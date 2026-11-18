from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import DisallowedHost
from django.templatetags.static import static

from accounts.roles import effective_roles
from clients.permissions import MANAGE_ROLES as CLIENT_ROLES

from .form_surfaces import form_route_patterns, surface_for_request
from .models import Lab
from .page_docs import documentation_for_request


def form_presentation(request):
    surface = surface_for_request(request)
    form_dialog = bool(surface and request.headers.get("X-VR-Form") == "1")
    return {
        "form_surface": surface,
        "form_route_patterns": form_route_patterns(),
        # True when this response fills the shared form dialog (layouts/form_fragment.html).
        "form_dialog": form_dialog,
        "form_surface_layout": (
            "layouts/form_fragment.html" if form_dialog else "layouts/document.html"
        ),
    }


def link_preview_image(request):
    """Absolute URL of the link-preview image: from PUBLIC_URL when set, else the request's own
    host. None when the host is not allowed (the 400 page for that host must still render)."""
    path = static("img/link-preview.png")
    if settings.PUBLIC_URL:
        return settings.PUBLIC_URL + path
    try:
        return request.build_absolute_uri(path)
    except DisallowedHost:
        return None


def workspace(request):
    roles = effective_roles(getattr(request, "user", AnonymousUser()))
    return {
        "outbound_email_enabled": settings.EMAIL_BACKEND
        != "django.core.mail.backends.dummy.EmailBackend",
        "link_preview_image": link_preview_image(request),
        "lab": Lab.objects.first(),
        "user_roles": roles,
        "can_manage_clients": bool(roles & set(CLIENT_ROLES)),
        "page_documentation": documentation_for_request(request),
    }
