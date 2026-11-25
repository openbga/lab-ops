from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import DisallowedHost
from django.templatetags.static import static

from accounts.roles import roles_and_capabilities
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
    # The roles and the capabilities in one statement: the sidebar shows items for both.
    roles, capabilities = roles_and_capabilities(getattr(request, "user", AnonymousUser()))
    second_factor_pending = _second_factor_pending(request)
    password_change_pending = not second_factor_pending and _password_change_pending(request)
    return {
        "outbound_email_enabled": settings.EMAIL_BACKEND
        != "django.core.mail.backends.dummy.EmailBackend",
        "link_preview_image": link_preview_image(request),
        "lab": Lab.objects.first(),
        "user_roles": roles,
        "user_capabilities": capabilities,
        "can_manage_clients": bool(roles & set(CLIENT_ROLES)),
        "page_documentation": documentation_for_request(request),
        # The signed-in session as this request found it (accounts.sessions), for the warning
        # before sign-out (includes/session_warning.html); None when not signed in.
        "account_session": getattr(request, "account_session", None),
        # The session must add a second sign-in factor before anything else (accounts.enrolment):
        # the shell leaves out the navigation it cannot use yet.
        "second_factor_pending": second_factor_pending,
        # The session must choose a new password before anything else (accounts.compromised).
        "password_change_pending": password_change_pending,
        # Either: the shell leaves out its navigation.
        "session_held": second_factor_pending or password_change_pending,
        # The page that sign-in was opening is still kept for Continue after the factor is added.
        "second_factor_continue": _second_factor_continue(request),
    }


def _second_factor_pending(request):
    """As accounts.enrolment decided when the request reached its view; for a page drawn without
    one (Not found), decided here."""
    decided = getattr(request, "vr_second_factor_pending", False)
    if decided is None:
        from accounts.enrolment import holds

        # Both holds in one reading, as the middleware reads them (accounts.enrolment.holds).
        decided, request.vr_password_marked = holds(getattr(request, "account_session", None))
        request.vr_second_factor_pending = decided
    return decided


def _password_change_pending(request):
    """As accounts.compromised decided when the request reached its view; for a page drawn
    without one (Not found), decided here."""
    decided = getattr(request, "vr_password_change_pending", False)
    if decided is None:
        marked = getattr(request, "vr_password_marked", None)
        if marked is None:
            from accounts.compromised import pending

            marked = pending(request)
        decided = request.vr_password_change_pending = marked
    return decided


def _second_factor_continue(request):
    from accounts.enrolment import NEXT_KEY

    user = getattr(request, "user", None)
    session = getattr(request, "session", None)
    return bool(
        user is not None and user.is_authenticated and session is not None and NEXT_KEY in session
    )
