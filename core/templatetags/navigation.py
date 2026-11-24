"""Application navigation: which sidebar or portal item each page marks, and the section tab sets.

This module is the one declaration of the route-to-item mapping (docs/ui-standards.md section 3,
rule 4). Every page beneath a list marks that list's item, so each item names the routes it owns
besides its own list. A route appears under one item at most; routes reached from the user menu
(account pages) mark none. tests/test_navigation.py checks that every HTML route is declared here
or in its own exemption lists, and that each item's label equals its list page's title.

Templates use:
  {% load navigation %}
  {% nav_item "clients" %}            one sidebar item (includes/nav_link.html)
  {% portal_nav_item "portal-home" %}  one client-portal item (same include, portal markup)
  {% section_tabs "<name>" %}          a declared tab set (includes/section_tabs.html)

Staff menu visibility follows work capabilities below; views still enforce record access.
"""

from dataclasses import dataclass

from django import template
from django.core.exceptions import ImproperlyConfigured
from django.urls import NoReverseMatch, reverse

from accounts.roles import ROLES, effective_roles
from core.modules import enabled_for

register = template.Library()


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    url_name: str  # the list or overview the item opens; it marks the item too
    icon: str = ""
    routes: tuple[str, ...] = ()  # further view names (record, form, action pages) it marks


@dataclass(frozen=True)
class Tab:
    label: str
    url_name: str


@dataclass(frozen=True)
class TabSet:
    label: str  # accessible name of the <nav>
    tabs: tuple[Tab, ...]


STAFF_NAV = (
    NavItem("dashboard", "Dashboard", "dashboard", "house"),
    # Desk
    NavItem(
        "requests",
        "Tickets",
        "desk:request-list",
        "inbox",
        (
            "desk:request-create",
            "desk:request-find",
            "desk:request-detail",
            "desk:request-assign",
            "desk:request-status",
            "desk:request-properties",
            "desk:complaint-mark",
            "desk:complaint-decision",
            "desk:complaint-record",
            "desk:complaint-review",
            "desk:entry-withdraw",
            "desk:request-history",
            "desk:view-create",
            "desk:view-edit",
            # A complaint's nonconformance, opened from the ticket (optional module).
            "nonconformance:from-ticket",
            # Links to external records, opened from a ticket.
            "link_records:request-link-add",
            "link_records:attachment-remove",
            "link_records:link-detail",
            "link_records:link-edit",
            "link_records:person-search",
            # A ticket's form details.
            "request_details:edit",
            # Files on ticket entries (attachments).
            "attachments:withdraw",
        ),
    ),
    NavItem(
        "teams",
        "Teams",
        "teams:team-list",
        "people",
        ("teams:team-create", "teams:team-detail", "teams:team-edit"),
    ),
    # Clients
    NavItem(
        "clients",
        "Client organizations",
        "clients:client-list",
        "building",
        (
            "clients:client-create",
            "clients:client-detail",
            "clients:client-edit",
            "clients:contact-create",
            "clients:contact-edit",
            "link_records:client-link-add",  # opened from the organization page
            "clients:portal-access-edit",
            "clients:site-create",
            "clients:site-edit",
            "clients:client-timeline",
        ),
    ),
    # Import (milestone 2, M2-A): spreadsheet import of organizations, sites and contacts
    NavItem(
        "import",
        "Spreadsheet import",
        "dataimport:index",
        "file-earmark-arrow-up",
        ("dataimport:upload",),
    ),
    # Quality (optional module "nonconformance", core.modules)
    NavItem(
        "nonconformances",
        "Nonconformances",
        "nonconformance:list",
        "exclamation-triangle",
        (
            "nonconformance:action-list",
            "nonconformance:summary",
            "nonconformance:create",
            "nonconformance:detail",
            "nonconformance:edit",
            "nonconformance:owner",
            "nonconformance:status",
            "nonconformance:investigation",
            "nonconformance:impact",
            "nonconformance:effectiveness",
            "nonconformance:close",
            "nonconformance:reopen",
            "nonconformance:action-add",
            "nonconformance:action-edit",
            "nonconformance:action-done",
            "nonconformance:action-cancel",
            "nonconformance:ticket-link",
            "nonconformance:ticket-unlink",
        ),
    ),
    # Setup
    NavItem(
        "request-categories",
        "Ticket categories",
        "taxonomy:category-list",
        "tag",
        (
            "taxonomy:category-create",
            "taxonomy:group-create",
            "taxonomy:category-edit",
            "taxonomy:preset-load",
        ),
    ),
    NavItem(
        "service-targets",
        "Service targets",
        "service_levels:target-list",
        "clock-history",
        ("service_levels:target-create", "service_levels:target-edit"),
    ),
    NavItem(
        "business-calendars",
        "Business calendars",
        "service_levels:calendar-list",
        "calendar3",
        (
            "service_levels:calendar-create",
            "service_levels:calendar-detail",
            "service_levels:calendar-edit",
            "service_levels:holiday-create",
            "service_levels:holiday-remove",
        ),
    ),
    NavItem(
        "external-systems",
        "External systems",
        "links:system-list",
        "tools",
        (
            "links:system-create",
            "links:system-edit",
        ),
    ),
    # Ticket forms
    NavItem(
        "request-forms",
        "Ticket forms",
        "request_forms:template-list",
        "list-check",
        (
            "request_forms:template-create",
            "request_forms:template-detail",
            "request_forms:template-edit",
            "request_forms:template-preview",
            "request_forms:template-copy",
            "request_forms:field-create",
            "request_forms:field-edit",
            "request_forms:preset-load",
            "request_forms:catalog-list",
            "request_forms:catalog-create",
            "request_forms:catalog-detail",
            "request_forms:catalog-edit",
            "request_forms:item-create",
            "request_forms:item-edit",
        ),
    ),
    # Setup: laboratory profile and the frame's own items
    NavItem(
        "lab-profile",
        "Laboratory profile",
        "lab-profile",
        "sliders",
        (
            "lab-profile-edit",
            "lab-logo-edit",
            "lab-certificate-create",
            "lab-certificate-edit",
            "lab-module",
            "lab-contact-edit",
        ),
    ),
    NavItem(
        "security-settings",
        "Security settings",
        "security-settings",
        "shield-lock",
        ("session-limits-edit", "second-factor-edit", "reveal-confirmation-edit"),
    ),
    NavItem("record-retention", "Record retention", "record-retention", "archive"),
    NavItem(
        "scheduled-jobs",
        "Scheduled jobs",
        "jobs:overview",
        "arrow-counterclockwise",
        ("jobs:settings-edit",),
    ),
    NavItem(
        "announcements",
        "Announcements",
        "announcement-list",
        "megaphone",
        ("announcement-create", "announcement-edit"),
    ),
    # Django admin renders its own chrome, so no shell page marks this item.
    NavItem("accounts-roles", "Accounts & roles", "admin:index", "people"),
)

PORTAL_NAV = (
    NavItem("portal-home", "Home", "portal:home"),
    # Portal
    NavItem(
        "portal-requests",
        "Tickets",
        "portal:request-list",
        "",
        (
            "portal:request-create",
            "portal:request-detail",
            "portal:request-reply",
            # Ticket forms
            "portal_forms:choose",
            "portal_forms:raise",
        ),
    ),
)

# Work destinations, not a second authorization policy. Staff may still follow related-record
# links to readable pages outside their menu. Admin's operational roles are expanded by
# effective_roles. A section key is also its accent line (--vr-line-<key> in static/css/app.css).
STAFF_SECTIONS = (
    ("home", "", ("dashboard",)),
    ("desk", "Service desk", ("requests", "teams")),
    ("clients", "Clients", ("clients", "import")),
    ("quality", "Quality", ("nonconformances",)),
    (
        "setup",
        "Setup",
        (
            "request-categories",
            "service-targets",
            "business-calendars",
            "external-systems",
            "request-forms",
            "lab-profile",
            "security-settings",
            "record-retention",
            "scheduled-jobs",
            "announcements",
            "accounts-roles",
        ),
    ),
)
STAFF_MENU_ROLES = {
    "dashboard": ROLES,
    # Desk items: every staff role works tickets and reads teams
    "requests": ROLES,
    "teams": ROLES,
    "clients": ROLES,  # every staff role reads client organizations; CLIENT_ROLES change them
    "import": ("account_manager", "client_services"),  # clients.permissions.MANAGE_ROLES
    # Quality: every staff role raises and reads nonconformances, while the module is on
    "nonconformances": ROLES,
    # Setup items: the admin role sets up the taxonomy, clocks and systems
    "request-categories": ("admin",),
    "service-targets": ("admin",),
    "business-calendars": ("admin",),
    "external-systems": ("admin",),
    "request-forms": ("admin",),
    "lab-profile": ROLES,  # every staff role reads it; the admin role changes it
    "security-settings": ("admin",),  # core.security_settings.SECURITY_ROLES
    "record-retention": ("admin",),
    "scheduled-jobs": ("admin",),
    "announcements": ("admin",),
    "accounts-roles": (),  # deployment-owner access is separate from lab roles
}


# Items of an optional module (core.modules key): shown only while the module is on.
MODULE_ITEMS = {"nonconformances": "nonconformance"}


def visible_staff_items(user, roles=None, request=None):
    """Keys of the staff menu items this account's current work capabilities show, without the
    items of an optional module that is off (in `request`'s one reading of the switches, so the
    menu agrees with the page; core.modules.states_for)."""
    if roles is None:
        roles = effective_roles(user)
    visible = {key for key, allowed in STAFF_MENU_ROLES.items() if roles.intersection(allowed)}
    for key, module in MODULE_ITEMS.items():
        if key in visible and not enabled_for(request, module):
            visible.discard(key)
    if (
        user.is_authenticated
        and user.is_active
        and user.client_id is None
        and user.is_staff
        and user.is_superuser
    ):
        visible.add("accounts-roles")
    return visible


@register.simple_tag(takes_context=True)
def staff_nav_sections(context):
    """Return nonempty menu sections for this account's current work capabilities."""
    request = context["request"]
    visible = visible_staff_items(request.user, context.get("user_roles"), request)
    return [
        {"key": key, "label": label, "items": items}
        for key, label, keys in STAFF_SECTIONS
        if (items := [item for item in keys if item in visible])
    ]


# Pages reached from the user menu or the top bar (What's new): they render inside the user's
# shell and mark no item.
ACCOUNT_ROUTES = frozenset(
    {
        "whats-new",
        "email-notices",
        "account-sessions",
        "account-sign-ins",
        "account-second-factor",
        "account-new-password",
        "account-confirm-reveal",
        "account_change_password",
        "account_set_password",
        "account_email",
        "account_logout",
        "mfa_index",
        "mfa_activate_totp",
        "mfa_deactivate_totp",
        "mfa_view_recovery_codes",
        "mfa_generate_recovery_codes",
    }
)

# Section tabs: one sidebar item whose content is two to six sibling lists (standard section 3,
# rule 2). Every list in a set marks the same sidebar item.
SECTION_TABS = {
    "quality": TabSet(
        "Quality lists",
        (
            Tab("Nonconformances", "nonconformance:list"),
            Tab("Corrective actions", "nonconformance:action-list"),
            Tab("Summary", "nonconformance:summary"),
        ),
    ),
}

# Routes whose page belongs to another item depending on the record it shows. The route is
# declared under its usual item above; its view may name one of the alternatives listed here as
# "nav_item" in the template context (a contact page that belongs to another item, say).
# Any other value is ignored.
SHARED_ROUTES = {}

ITEMS = {item.key: item for item in STAFF_NAV + PORTAL_NAV}


def _route_items():
    mapping = {}
    for item in STAFF_NAV + PORTAL_NAV:
        for route in (item.url_name, *item.routes):
            if route in mapping:
                raise ImproperlyConfigured(
                    f"Route {route!r} marks both {mapping[route]!r} and {item.key!r}."
                )
            mapping[route] = item.key
    if len(ITEMS) != len(STAFF_NAV) + len(PORTAL_NAV):
        raise ImproperlyConfigured("Navigation item keys must be unique.")
    return mapping


ROUTE_ITEMS = _route_items()


def current_item_key(request, chosen=None):
    """The key of the item the request's page marks, or None (account, error, unmapped).

    `chosen` is the view's "nav_item" context value; it counts only for a declared shared route.
    """
    match = getattr(request, "resolver_match", None)
    if not match:
        return None
    if chosen in SHARED_ROUTES.get(match.view_name, ()):
        return chosen
    return ROUTE_ITEMS.get(match.view_name)


def _url(url_name):
    try:
        return reverse(url_name)
    except NoReverseMatch:  # not mounted: the item renders as "Planned"
        return ""


def _item_context(context, key, portal):
    item = ITEMS[key]
    request = context.get("request")
    return {
        "url": _url(item.url_name),
        "label": item.label,
        "icon": item.icon,
        "current": request is not None
        and current_item_key(request, context.get("nav_item")) == key,
        "portal": portal,
    }


@register.inclusion_tag("includes/nav_link.html", takes_context=True)
def nav_item(context, key):
    """One staff sidebar item, marked current when the page belongs to it."""
    return _item_context(context, key, portal=False)


@register.inclusion_tag("includes/nav_link.html", takes_context=True)
def portal_nav_item(context, key):
    """One client-portal top-bar item, marked current when the page belongs to it."""
    return _item_context(context, key, portal=True)


@register.inclusion_tag("includes/section_tabs.html", takes_context=True)
def section_tabs(context, name):
    """A declared tab set rendered through includes/section_tabs.html."""
    tab_set = SECTION_TABS[name]
    match = getattr(context.get("request"), "resolver_match", None)
    view_name = match.view_name if match else None
    return {
        "label": tab_set.label,
        "tabs": [
            {"label": tab.label, "url": _url(tab.url_name), "current": tab.url_name == view_name}
            for tab in tab_set.tabs
        ],
    }
