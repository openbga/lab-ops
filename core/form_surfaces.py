"""Editing surface metadata for progressive enhancement, never authorization policy."""

import re
from dataclasses import dataclass
from functools import cache

from django.urls import URLResolver, get_resolver

# Dedicated editing pages only. Record pages with inline actions remain record pages.
FORM_SURFACES = {
    "clients:client-create": "lg",
    "clients:client-edit": "lg",
    "clients:contact-create": "lg",
    "clients:contact-edit": "lg",
    "clients:site-create": "lg",
    "clients:site-edit": "lg",
    "clients:portal-access-edit": "lg",
    "announcement-create": "lg",
    "announcement-edit": "lg",
    "lab-profile-edit": "lg",
    "lab-logo-edit": "lg",
    "lab-certificate-create": "lg",
    "lab-certificate-edit": "lg",
    "jobs:settings-edit": "lg",
    "lab-contact-edit": "lg",
    "session-limits-edit": "lg",
    "second-factor-edit": "lg",
    "reveal-confirmation-edit": "lg",
    # Request desk
    "desk:request-create": "lg",
    "desk:request-assign": "lg",
    "desk:request-status": "lg",
    "desk:request-properties": "lg",
    "desk:complaint-mark": "lg",
    "desk:complaint-decision": "lg",
    "desk:complaint-record": "lg",
    "desk:complaint-review": "lg",
    "desk:request-emergency": "lg",
    "desk:entry-withdraw": "lg",
    "desk:view-create": "lg",
    "desk:view-edit": "lg",
    "teams:team-create": "lg",
    "teams:team-edit": "lg",
    # Setup and links
    "taxonomy:category-create": "lg",
    "taxonomy:group-create": "lg",
    "taxonomy:category-edit": "lg",
    "service_levels:target-create": "lg",
    "service_levels:target-edit": "lg",
    "service_levels:calendar-create": "lg",
    "service_levels:calendar-edit": "lg",
    "service_levels:holiday-create": "lg",
    "service_levels:holiday-remove": "lg",
    "links:system-create": "lg",
    "links:system-edit": "lg",
    "link_records:request-link-add": "lg",
    "link_records:client-link-add": "lg",
    "link_records:attachment-remove": "lg",
    "link_records:link-edit": "lg",
    # Attachments (M2-B)
    "attachments:withdraw": "lg",
    # Optional modules
    "lab-module": "lg",
    "nonconformance:create": "lg",
    "nonconformance:from-ticket": "lg",
    "nonconformance:edit": "lg",
    "nonconformance:owner": "lg",
    "nonconformance:status": "lg",
    "nonconformance:investigation": "lg",
    "nonconformance:impact": "lg",
    "nonconformance:effectiveness": "lg",
    "nonconformance:close": "lg",
    "nonconformance:reopen": "lg",
    "nonconformance:action-add": "lg",
    "nonconformance:action-edit": "lg",
    "nonconformance:action-done": "lg",
    "nonconformance:action-cancel": "lg",
    "nonconformance:ticket-link": "lg",
    "nonconformance:ticket-unlink": "lg",
    # Audit review records
    "audit_review:review-create": "lg",
    "audit_review:review-note": "lg",
    "audit_review:review-interval": "lg",
    "access_review:unused-period": "lg",
}


@dataclass(frozen=True)
class FormSurface:
    size: str


def surface_for_request(request):
    """Return presentation metadata for an already resolved request."""
    match = getattr(request, "resolver_match", None)
    size = FORM_SURFACES.get(match.view_name) if match is not None else None
    return FormSurface(size) if size else None


@cache
def form_route_patterns():
    """Return JavaScript-compatible path regexes from Django's canonical routes.

    The current application's converters use regex syntax shared by Python and JS.
    Remove Python's named-group spelling and end anchor before exposing the patterns.
    Callers match URL.pathname, without its query string or fragment.
    """
    result = []

    def visit(resolver, prefix="", namespaces=()):
        for route in resolver.url_patterns:
            regex = route.pattern.regex.pattern.removeprefix("^")
            if isinstance(route, URLResolver):
                child_namespaces = namespaces + ((route.namespace,) if route.namespace else ())
                visit(route, prefix + regex, child_namespaces)
                continue
            name = ":".join((*namespaces, route.name)) if route.name else ""
            if name not in FORM_SURFACES:
                continue
            pattern = re.sub(r"\(\?P<[^>]+>", "(?:", prefix + regex)
            pattern = pattern.replace(r"\Z", "$")
            if not pattern.endswith("$"):
                pattern += "$"
            result.append({"pattern": "^/" + pattern, "size": FORM_SURFACES[name]})

    visit(get_resolver())
    return result
