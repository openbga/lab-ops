"""Route coverage is a release gate; help stays inside each authorized page response."""

from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import URLResolver, get_resolver, resolve

from core.page_docs import (
    FRIENDLY_FIELDS,
    document_for_route,
    documentation_for_request,
    read_documents,
)

# Each exclusion is deliberately named: a newly mounted page must acquire help.
# Actions/downloads are explained on their invoking page, not a second endpoint.
NON_PAGE_ROUTES = {
    "attachments:download": "Binary file download from a ticket entry's file list",
    "audit_review:changes-export": "CSV download of the audit review's Changes list",
    "audit_review:access-export": "CSV download of the audit review's Access records list",
    "audit_review:sign-ins-export": "CSV download of the audit review's Sign-ins list",
    "audit_review:uses-export": "CSV download of the audit review's Workspace use list",
    "health": "JSON process health probe",
    "ready": "JSON database readiness probe",
    "lookup": "JSON option lookup for remote selects (core/lookups.py)",
    "preference": "POST-only per-user presentation setting (core/preferences.py)",
    "account-session": "JSON time left in the session, asked before the sign-out warning",
    "account-session-continue": "POST-only Stay signed in from the sign-out warning (JSON)",
    "account-session-keep": "POST-only keeping of typing after a session limit (JSON)",
    "account-session-sign-out": "POST-only Sign out of one session on Where you are signed in",
    "account-sessions-sign-out-others": "POST-only Sign out of all other sessions on Where you are "
    "signed in",
    "mfa_download_recovery_codes": "Private text download from recovery-code help",
    "admin:login": "Redirect to the application sign-in page",
    "admin:autocomplete": "JSON related-object lookup",
    "admin:jsi18n": "JavaScript translation asset",
    "admin:view_on_site": "Redirect to the object's application URL",
    "admin:accounts_user_delete": "Deletion disabled by AccountAdmin",
    "admin:mfa_authenticator_add": "Creation disabled by AuthenticatorAdmin",
    "admin:account_emailaddress_add": "Creation disabled by EmailAddressAdmin",
    "admin:account_emailaddress_delete": "Deletion disabled by EmailAddressAdmin",
    "admin:auth_group_delete": "Deletion disabled by RoleGroupAdmin",
    "admin:core_lab_add": "Creation disabled by LabAdmin",
    "admin:core_lab_delete": "Deletion disabled by LabAdmin",
    "announcement-withdraw": "POST action on the announcement list",
    "lab-logo-remove": "POST action on the laboratory profile",
    "lab-logo-image": "Logo image previewed on the laboratory profile",
    "dataimport:template": "CSV template download, documented on the spreadsheet import page",
    "lab-certificate-remove": "POST action on the laboratory profile",
    "desk:request-bulk": "POST action on the ticket list (bulk assign, status, close)",
    "desk:request-entry": "POST action on the ticket page (composer: reply, note, contact)",
    "desk:request-take": "POST action on the ticket page (Take it)",
    "desk:category-guidance": "HTMX partial: the intake guidance shown on the new-ticket form",
    "desk:view-delete": "POST action on the saved view's edit page (Delete view)",
    "desk:view-move": "POST action on the saved view's edit page (Move up, Move down)",
    "teams:member-remove": "POST action on the team page (Remove, after confirmation)",
    "teams:member-lead": "POST action on the team page (Make lead, Not lead)",
    "portal:request-reply": "POST-only reply on the portal ticket page",
    "taxonomy:category-move": "POST Move up/down on Ticket categories",
    "link_records:attachment-shown": "POST Show to client / Hide from client on a ticket's Links",
    "link_records:attachment-reveal": "POST Reveal key (HTMX fragment) on a ticket's Links",
    # Ticket forms
    "request_forms:template-move": "POST-only Move up/Move down on the ticket forms list",
    "request_forms:field-move": "POST-only Move up/Move down on the ticket form page",
    "request_forms:item-move": "POST-only Move up/Move down on the catalog page",
    "request_details:reveal": "POST Reveal (HTMX fragment) of a patient-information form value",
}


def named_routes(patterns, namespace=""):
    routes = set()
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            prefix = namespace + (pattern.namespace + ":" if pattern.namespace else "")
            routes |= named_routes(pattern.url_patterns, prefix)
        elif pattern.name:
            routes.add(namespace + pattern.name)
    return routes


def test_every_page_route_has_documentation_and_exclusions_are_current():
    routes = named_routes(get_resolver().url_patterns)
    covered = {route for doc in read_documents() for route in doc["routes"]}
    assert not routes - covered - NON_PAGE_ROUTES.keys(), "Add page help for these routes"
    assert not NON_PAGE_ROUTES.keys() - routes, "Remove stale non-page exclusions"
    assert not covered - routes - {"documentation:shared", "documentation:unavailable"}


def test_content_has_valid_sources_and_no_ambiguous_variant_matches():
    root = Path(__file__).resolve().parent.parent
    docs = read_documents()  # Also validates shape, nonempty entries and duplicate mappings.
    assert docs
    for doc in docs:
        for source in doc["sources"]:
            assert (root / source).is_file(), (doc["id"], source)
        # Two equally-specific predicates for one route must not overlap.
        for other in docs:
            if doc["id"] >= other["id"] or not set(doc["routes"]) & set(other["routes"]):
                continue
            left, right = doc.get("match", {}), other.get("match", {})
            if len(left) == len(right):
                assert any(k in right and right[k] != v for k, v in left.items()), (
                    doc["id"],
                    other["id"],
                )


def test_shared_form_routes_resolve_to_one_guide_for_create_and_edit():
    for create, edit in (
        ("clients:client-create", "clients:client-edit"),
        ("clients:contact-create", "clients:contact-edit"),
        ("lab-certificate-create", "lab-certificate-edit"),
        ("announcement-create", "announcement-edit"),
    ):
        guide = document_for_route(create, {})
        assert guide is not None, create
        assert guide == document_for_route(edit, {"pk": 1}), (create, edit)


def test_documentation_is_escaped_and_evidence_is_not_exposed():
    content = {
        "id": "safe",
        "title": "<script>bad()</script>",
        "purpose": "<img src=x>",
        "sources": ["private-evidence-path"],
        "sections": [
            {
                "title": "<b>Heading</b>",
                "paragraphs": ["<iframe>"],
                "items": [{"label": "<i>Field</i>", "description": "<svg onload=x>"}],
            }
        ],
    }
    html = render_to_string("documentation/content.html", {"page_documentation": content})
    # The drawer's own icons are <svg class="vr-icon">; the injected one must stay text.
    for unsafe in ("<script>", "<img", "<iframe>", "<svg onload", "private-evidence-path"):
        assert unsafe not in html
    assert "&lt;script&gt;" in html


def test_paragraph_only_sections_do_not_render_empty_dictionary_items_tables():
    paragraph_only = next(
        section
        for doc in read_documents()
        for section in doc["sections"]
        if section["paragraphs"] and not section["items"]
    )
    html = render_to_string(
        "documentation/content.html",
        {"page_documentation": {"title": "Notes", "sections": [paragraph_only]}},
    )
    assert "<table" not in html
    assert paragraph_only["title"] in html


@pytest.mark.django_db
def test_error_pages_replace_resolved_page_help_and_500_needs_no_database(
    django_assert_num_queries,
):
    from django.contrib.auth.models import AnonymousUser
    from django.views.defaults import page_not_found, permission_denied, server_error

    request = RequestFactory().get("/clients/123/")
    request.user = AnonymousUser()
    request.resolver_match = resolve(request.path)
    for view in (permission_denied, page_not_found):
        response = view(request, Exception("Synthetic error"))
        assert b'data-vr-documentation-id="core-unavailable"' in response.content
        assert b'data-vr-documentation-id="clients-client-detail"' not in response.content
    with django_assert_num_queries(0):
        response = server_error(request)
    assert b"data-vr-documentation-toggle" in response.content
    assert b'data-vr-documentation-id="core-unavailable"' in response.content


def test_request_help_uses_route_not_query_or_record_data():
    request = RequestFactory().get("/clients/?documentation=admin:accounts_user_change")
    request.resolver_match = resolve("/clients/")
    data = documentation_for_request(request)
    assert data["id"] == document_for_route("clients:client-list")["id"]
    assert set(data) <= {"id", "title", "purpose", "sections", *FRIENDLY_FIELDS}
    request.resolver_match = None
    assert documentation_for_request(request)["id"] == "core-unavailable"


@pytest.mark.django_db
def test_help_follows_authorized_page_and_form_fragments(client, staff):
    user = staff("docs-staff", "account_manager")
    client.force_login(user)
    for url, route in (
        ("/clients/", "clients:client-list"),
        ("/clients/new/", "clients:client-create"),
    ):
        response = client.get(url)
        assert response.status_code == 200
        assert response.context["page_documentation"]["id"] == document_for_route(route)["id"]
    fragment = client.get("/clients/new/", HTTP_X_VR_FORM="1")
    assert b"data-vr-documentation-source" in fragment.content
    from accounts.models import User
    from clients.models import Client

    customer = Client.objects.create(name="Synthetic docs client")
    portal_user = User.objects.create_user("docs-client", client=customer)
    client.force_login(portal_user)
    assert client.get("/clients/").status_code == 403
    portal = client.get("/portal/")
    assert portal.status_code == 200
    assert portal.context["page_documentation"]["id"] == document_for_route("portal:home")["id"]


@pytest.mark.django_db
def test_account_and_administration_screens_include_help(client, staff):
    response = client.get("/accounts/login/")
    assert response.context["page_documentation"]["id"] == "accounts-login"
    user = staff("docs-owner", "admin")
    user.is_staff = user.is_superuser = True
    user.save()
    client.force_login(user)
    response = client.get("/admin/accounts/user/")
    assert response.status_code == 200
    assert response.context["page_documentation"]["id"] == "admin-users"
    assert b"data-vr-documentation-toggle" in response.content
    assert b'id="vr-documentation"' in response.content


# ---------------------------------------------------------------- the friendly layer

FRIENDLY = {
    "summary": "Find an organization and see its contacts.",
    "tasks": [
        {"title": "Find an organization", "steps": ["Type a name in [[Search]].", "Select it."]},
        {"title": "Add an organization", "steps": ["Select [[New organization]]."]},
    ],
    "good_to_know": ["One.", "Two.", "Three."],
}


def friendly_document(**changes):
    document = {
        "id": "friendly",
        "title": "Organizations",
        "purpose": "Organizations.",
        "routes": ["friendly"],
        "sources": ["synthetic"],
        "sections": [{"title": "Fields", "items": [{"label": "Search", "description": "Finds."}]}],
        **FRIENDLY,
    }
    document.update(changes)
    return document


def check(document):
    from core.page_docs import check_friendly

    check_friendly("synthetic.json", document)


def test_friendly_layer_is_optional_but_all_or_none():
    check(friendly_document())
    plain = friendly_document()
    for key in FRIENDLY:
        del plain[key]
    check(plain)  # no layer at all is fine
    for key in FRIENDLY:
        partial = friendly_document()
        del partial[key]
        with pytest.raises(ImproperlyConfigured, match="come together"):
            check(partial)


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"summary": "x" * 241}, "summary"),
        ({"summary": "Select [[Apply]]."}, "summary is plain text"),
        ({"tasks": FRIENDLY["tasks"][:1]}, "tasks needs 2-5"),
        ({"tasks": FRIENDLY["tasks"] * 3}, "tasks needs 2-5"),
        ({"tasks": [{"title": "x" * 81, "steps": ["a"]}, FRIENDLY["tasks"][1]]}, "title"),
        ({"tasks": [{"title": "Do", "steps": []}, FRIENDLY["tasks"][1]]}, "1-4 steps"),
        ({"tasks": [{"title": "Do", "steps": ["a"] * 5}, FRIENDLY["tasks"][1]]}, "1-4 steps"),
        ({"tasks": [{"title": "Do", "steps": ["x" * 201]}, FRIENDLY["tasks"][1]]}, "over 200"),
        ({"tasks": [{"title": "Do", "steps": [" "]}, FRIENDLY["tasks"][1]]}, "non-blank"),
        ({"good_to_know": ["One.", "Two."]}, "good_to_know needs 3-5"),
        ({"good_to_know": ["a"] * 6}, "good_to_know needs 3-5"),
        ({"good_to_know": ["One.", "Two.", "x" * 201]}, "over 200"),
        ({"good_to_know": ["Open [[Columns.", "Two.", "Three."]}, "closing"),
        ({"good_to_know": ["[[a [[b]] c]]", "Two.", "Three."]}, "closing"),
        ({"good_to_know": ["Stray ]] here.", "Two.", "Three."]}, "closing"),
    ],
)
def test_friendly_layer_limits_and_labels_are_enforced(changes, message):
    with pytest.raises(ImproperlyConfigured, match=message):
        check(friendly_document(**changes))


# The appended workspace sections are not a page of their own.
FRIENDLY_EXEMPT = {"core-shared"}


def test_every_page_has_the_friendly_layer():
    # check_friendly (in read_documents) enforces the limits and labels; this enforces presence,
    # so no page falls back to the plain purpose and an open reference.
    missing = sorted(
        document["id"]
        for document in read_documents()
        if document["id"] not in FRIENDLY_EXEMPT and "tasks" not in document
    )
    assert missing == []


def test_the_drawer_receives_the_friendly_layer_but_not_evidence():
    from core.page_docs import documentation_for_route

    help_ = documentation_for_route("clients:client-list")
    assert help_["summary"] and help_["tasks"] and help_["good_to_know"]
    assert "sources" not in help_ and "routes" not in help_


def test_labels_render_as_chips_after_escaping():
    from core.templatetags.documentation import help_text

    assert help_text("Select [[New organization]].") == (
        'Select <span class="vr-help-ui">New organization</span>.'
    )
    assert help_text("<b>[[x]]</b>") == '&lt;b&gt;<span class="vr-help-ui">x</span>&lt;/b&gt;'
    assert help_text("Use [[<i>]].") == 'Use <span class="vr-help-ui">&lt;i&gt;</span>.'
    assert help_text("no markers") == "no markers"


def test_friendly_page_renders_summary_tasks_tips_and_collapsed_reference():
    html = render_to_string(
        "documentation/content.html", {"page_documentation": friendly_document()}
    )
    assert "Find an organization and see its contacts." in html
    assert 'href="#vr-help-tasks"' in html and 'id="vr-help-know"' in html
    assert html.count('<span class="vr-help-ui">') == 2
    assert '<details class="vr-help-ref" data-vr-help-section>' in html  # closed
    assert 'id="vr-help-filter"' in html


def test_page_without_the_layer_shows_purpose_and_open_reference():
    document = friendly_document()
    for key in FRIENDLY:
        del document[key]
    html = render_to_string("documentation/content.html", {"page_documentation": document})
    assert '<p class="vr-help-summary">Organizations.</p>' in html
    assert "vr-help-jump" not in html and "vr-help-tasks" not in html
    assert '<details class="vr-help-ref" open data-vr-help-section>' in html
    assert "<dt>Search</dt>" in html
