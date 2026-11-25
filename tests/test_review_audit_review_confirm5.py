"""Check the chip contract at the toolbar and at its shipped call sites.

Adopted from the fifth confirmation (.local/review-history/audit-review-confirm5.md), which
found nothing.
"""

from urllib.parse import parse_qs, urlsplit

import pytest
from django.template import Context, Template
from test_review_audit_review_confirm2 import toolbar_readers as toolbar_readers

from clients.models import Client
from core.forms import SearchFilter


@pytest.mark.parametrize(
    ("name", "value", "expected_count", "expected_summary"),
    [
        ("q", "ticket text", 1, "Custom: ticket text"),
        ("team", "courier", 2, "Custom: courier · Search: courier"),
        (None, "courier", 1, "Custom: courier"),
        (None, "Courier", 2, "Custom: Courier · Search: courier"),
    ],
)
def test_toolbar_identifies_the_search_chip(rf, name, value, expected_count, expected_summary):
    request = rf.get("/requests/", {"q": "courier"})
    form = SearchFilter(request.GET)
    assert form.is_valid()
    chip = {"label": "Custom", "value": value, "remove_url": "?"}
    if name is not None:
        chip["name"] = name
    toolbar = Template("{% load ui %}{% grid_toolbar label='Filter tickets' chips=chips q=q %}")
    html = toolbar.render(
        Context(
            {"request": request, "filter_form": form, "chips": [chip], "q": form.cleaned_data["q"]}
        )
    )
    assert f'<span class="vr-filter-count">{expected_count}<span' in html
    assert f"<span data-vr-filter-state>{expected_summary}</span>" in html


def test_shipped_toolbars_pass_form_field_names(client, toolbar_readers):
    organization = Client.objects.get(account_number="EXCLIN")
    cases = {
        "admin": [
            ("/lab/announcements/", "q", "courier"),
            ("/setup/service-levels/", "q", "courier"),
            ("/setup/service-levels/calendars/", "q", "courier"),
            ("/setup/systems/", "q", "courier"),
            ("/quality/nonconformances/", "q", "courier"),
            ("/quality/actions/", "q", "courier"),
            ("/requests/", "q", "courier"),
            ("/clients/", "q", "courier"),
            (f"/clients/{organization.pk}/timeline/", "kind", "opened"),
        ],
        "client": [("/portal/requests/", "q", "courier")],
        "supervisor": [
            (f"/audit-review/{source}/", "date_from", "2026-03-01")
            for source in ("changes", "access", "sign-ins", "uses")
        ],
    }
    for role, pages in cases.items():
        client.force_login(toolbar_readers[role])
        for path, field, value in pages:
            for query in ({}, {field: value}):
                page = client.get(path, query)
                assert page.status_code == 200, path
                (toolbar,) = [
                    context
                    for template, context in zip(page.templates, page.context, strict=True)
                    if template.name == "includes/grid_toolbar.html"
                ]
                form = toolbar["filter_form"]
                assert form.is_valid(), (path, form.errors)
                chips = toolbar["chips"]
                assert len(chips) == len(query), (path, chips)
                for chip in chips:
                    assert chip["name"] == field and field in form.fields, (path, chip)
                    assert field not in parse_qs(urlsplit(chip["remove_url"]).query), (path, chip)
                assert toolbar["filter_count"] == len(query), path
