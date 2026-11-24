"""Confirmation of the access-record fixes using synthetic ticket data.

Adopted from the review's confirmation round (the reviewer's original is kept outside the
repository). Accepted limits are strict xfails naming the document that states them. No browser
import: CI's unit job installs no Playwright."""

# Fixtures imported from the request-form suite retain their pytest names.
# ruff: noqa: F811
import logging
from html import unescape
from urllib.parse import parse_qs, urljoin, urlsplit

import pytest
from django.apps import apps
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.template import engines
from django.template.response import TemplateResponse
from django.urls import reverse
from django.views.debug import ExceptionReporter

from clients.models import ClientSite
from request_forms.tests.conftest import (  # noqa: F401
    PATIENT_SENTINEL,
    admin,
    agent,
    capable,
    catalog,
    clinic,
    clinic_site,
    group,
    logistics,
    make_user,
    pickup,
    pickup_category,
    portal_user,
)
from request_forms.tests.test_capture_consistency import Inputs, other_connection
from request_forms.tests.test_pages import raised

pytestmark = pytest.mark.django_db


@pytest.fixture
def ticket(portal_user, pickup):
    return raised(portal_user, pickup, {"count": 3, "patient": PATIENT_SENTINEL})


def rows_for(response):
    records = apps.get_model("core", "AccessRecord")
    return list(
        records.objects.filter(request_ref=response["X-Request-ID"])
        .order_by("kind", "object_id")
        .values_list("kind", "ticket_id", "account_id", "object_id")
    )


class TemplateHook:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_template_response(self, request, response):
        return response


@pytest.mark.parametrize(
    "change",
    [
        "preserve",
        pytest.param(
            "distinct-replacement",
            marks=pytest.mark.xfail(
                strict=True,
                reason="Accepted limit: a deployment's own template-response hook or post-render "
                "callback rewriting a deferred page (docs/architecture.md, Access records).",
            ),
        ),
        pytest.param(
            "hide-in-place",
            marks=pytest.mark.xfail(
                strict=True,
                reason="Accepted limit: a deployment's own template-response hook or post-render "
                "callback rewriting a deferred page (docs/architecture.md, Access records).",
            ),
        ),
        pytest.param(
            "after-render",
            marks=pytest.mark.xfail(
                strict=True,
                reason="Accepted limit: a deployment's own template-response hook or post-render "
                "callback rewriting a deferred page (docs/architecture.md, Access records).",
            ),
        ),
        "wrap-rendered",
        "render-refused",
        "two-renderings",
    ],
)
def test_template_records_match_the_delivered_content(
    client, portal_user, ticket, pickup, settings, monkeypatch, change
):
    from desk import portal_views
    from request_forms.models import FieldValue

    engine = engines["django"]
    details = "{% load request_forms %}{% request_form_details service_request %}"
    other = raised(portal_user, pickup, {"count": 4, "patient": "OTHER-SYNTHETIC-ANSWER"})
    value = FieldValue.objects.get(filled_form__request=ticket, key="patient")
    other_value = FieldValue.objects.get(filled_form__request=other, key="patient")
    replacement = "Synthetic replacement document"

    def deferred(request, template, context):
        # In the two-renderings case both displayed fragments use the project's real inclusion tag.
        if change == "two-renderings":
            context["intro"] = engine.from_string(details).render(context, request)
        return TemplateResponse(
            request,
            engine.from_string("{{ intro }}{{ banner }}{{ service_request.number }}" + details),
            context,
        )

    def hook(self, request, response):
        if change == "distinct-replacement":
            return TemplateResponse(
                request, engine.from_string(details), {"service_request": other}
            )
        if change == "hide-in-place":
            response.template_name = engine.from_string(replacement)
        elif change == "after-render":
            response.add_post_render_callback(lambda rendered: HttpResponse(replacement))
        elif change == "render-refused":

            def refuse(rendered):
                assert PATIENT_SENTINEL.encode() in rendered.content
                raise PermissionDenied("Synthetic refusal")

            response.add_post_render_callback(refuse)
        else:
            response.context_data["banner"] = "Synthetic banner"
            if change == "wrap-rendered":
                response.add_post_render_callback(lambda rendered: HttpResponse(rendered.content))
        return response

    middleware = list(settings.MIDDLEWARE)
    middleware.insert(
        middleware.index("core.access.AccessBindMiddleware"), f"{__name__}.TemplateHook"
    )
    settings.MIDDLEWARE = middleware
    monkeypatch.setattr(TemplateHook, "process_template_response", hook)
    monkeypatch.setattr(portal_views, "render", deferred)
    client.force_login(portal_user)
    response = client.get(reverse("portal:request-detail", args=[ticket.number]))
    if change == "render-refused":
        assert response.status_code == 403 and PATIENT_SENTINEL.encode() not in response.content
        assert rows_for(response) == []
        return
    assert response.status_code == 200
    body = response.content.decode()
    if change in ("hide-in-place", "after-render"):
        assert body == replacement
        expected = []
    elif change == "distinct-replacement":
        assert "OTHER-SYNTHETIC-ANSWER" in body and PATIENT_SENTINEL not in body
        expected = [("patient_answer", other.pk, portal_user.pk, str(other_value.pk))]
    else:
        assert ticket.number in body and "Synthetic banner" in body
        assert body.count(PATIENT_SENTINEL) == (2 if change == "two-renderings" else 1)
        expected = [
            ("patient_answer", ticket.pk, portal_user.pk, str(value.pk)),
            ("ticket", ticket.pk, portal_user.pk, ""),
        ]
    assert rows_for(response) == expected


@pytest.mark.parametrize(
    "raw", [None, "", "1.5", "-1", " 3", "²", "９", "3" * 5000, "9" * 19, "historic"]
)
def test_pager_normalizes_the_cutoff_in_the_links_it_serves(client, capable, ticket, raw):
    from tests.test_review_access_records_verify import PagerLink, history_rows

    rows = history_rows(ticket, capable, 32)
    client.force_login(capable)
    url = reverse("desk:request-access", args=[ticket.pk])
    limit = rows[28].pk if raw == "historic" else rows[-1].pk
    supplied = str(limit).zfill(12) if raw == "historic" else raw
    first = client.get(url, {} if supplied is None else {"upto": supplied})
    assert first.status_code == 200
    expected = [r.request_ref for r in reversed(rows) if r.pk <= limit]
    assert [r.request_ref for r in first.context["page_obj"]] == expected[:25]
    link = PagerLink("next")
    link.feed(first.content.decode())
    assert link.href and parse_qs(urlsplit(link.href).query)["upto"] == [str(limit)]
    history_rows(ticket, capable, 1)
    second = client.get(urljoin(url, link.href))
    assert second.status_code == 200
    assert [r.request_ref for r in second.context["page_obj"]] == expected[25:]


def repeat_url(pickup, source):
    return reverse("portal_forms:raise", args=[pickup.key], query={"again": source.number})


def test_only_a_site_copied_is_shown_and_recorded(client, portal_user, pickup, clinic_site):
    pickup.fields.filter(key="count").update(copy_on_repeat=False)
    source = raised(
        portal_user, pickup, {"count": 741, "patient": PATIENT_SENTINEL}, site=clinic_site
    )
    client.force_login(portal_user)
    url = repeat_url(pickup, source)
    page = client.get(url)
    form = page.context["form"]
    assert page.status_code == 200 and PATIENT_SENTINEL.encode() not in page.content
    assert form["field-count"].value() in (None, "")
    assert str(form["site"].value()) == str(clinic_site.pk)
    assert f'value="{clinic_site.pk}" selected' in str(form["site"])
    assert f"Answers copied from {source.number}".encode() in page.content
    assert rows_for(page) == [("repeat", source.pk, portal_user.pk, "")]
    head = client.head(url)
    assert head.status_code == 200 and not head.content and rows_for(head) == []


def test_stale_repeat_submission_does_not_call_the_submitted_site_a_copy(
    client, portal_user, pickup, clinic_site
):
    # The source belongs to the organization as a whole. The portal user can select a site.
    source = raised(portal_user, pickup, {"count": 741})
    assert source.site_id is None
    client.force_login(portal_user)
    url = repeat_url(pickup, source)
    opened = client.get(url)
    assert opened.status_code == 200 and b"Answers copied from" in opened.content
    token = Inputs(opened).values["form_definition"]
    pickup.fields.filter(key="count").update(label="Revised synthetic count")
    response = client.post(
        url, {"form_definition": token, "site": str(clinic_site.pk), "field-count": "83"}
    )
    assert response.status_code == 200
    assert b"Revised synthetic count" in response.content
    form = response.context["form"]
    assert str(form["site"].value()) == str(clinic_site.pk)
    assert form["field-count"].value() in (None, "")
    if "accessrecord" in apps.get_app_config("core").models:
        assert rows_for(response) == []
    assert f"Answers copied from {source.number}".encode() not in response.content


@pytest.mark.xfail(
    strict=True,
    reason="Accepted limit: a site deactivated while Request again's form is drawn "
    "(docs/architecture.md, Access records).",
)
@pytest.mark.django_db(transaction=True)
def test_repeat_only_records_a_site_that_reaches_the_form(
    client, portal_user, pickup, clinic_site, monkeypatch
):
    from desk import portal_forms

    pickup.fields.filter(key="count").update(copy_on_repeat=False)
    source = raised(
        portal_user, pickup, {"count": 741, "patient": PATIENT_SENTINEL}, site=clinic_site
    )
    original = portal_forms.portal_sites
    changes = []

    def sites_when_form_is_built(user):
        if not changes:
            changes.append(
                other_connection(
                    lambda: ClientSite.objects.filter(pk=clinic_site.pk).update(is_active=False)
                )
            )
        return original(user)

    monkeypatch.setattr(portal_forms, "portal_sites", sites_when_form_is_built)
    client.force_login(portal_user)
    response = client.get(repeat_url(pickup, source))
    assert response.status_code == 200 and changes == [1]
    form = response.context["form"]
    assert "site" not in form.fields and form["field-count"].value() in ("", None)
    assert (
        PATIENT_SENTINEL.encode() not in response.content
        and clinic_site.name.encode() not in response.content
    )
    notice = f"Answers copied from {source.number}".encode() in response.content
    rows = rows_for(response) if "accessrecord" in apps.get_app_config("core").models else []
    assert (notice, rows) == (False, [])


@pytest.mark.parametrize(
    "middleware_change", ["shipped", "without-writer", "without-binder", "reordered"]
)
def test_security_settings_reports_the_effective_middleware(
    client, admin, settings, middleware_change
):
    from core import security_settings

    client.force_login(admin)
    writer = "core.access.AccessRecordMiddleware"
    binder = "core.access.AccessBindMiddleware"
    stack = list(settings.MIDDLEWARE)
    if middleware_change == "without-writer":
        stack.remove(writer)
    elif middleware_change == "without-binder":
        stack.remove(binder)
    elif middleware_change == "reordered":
        stack.remove(binder)
        stack.insert(stack.index(writer), binder)
    settings.MIDDLEWARE = stack
    response = client.get(reverse("security-settings"))
    assert response.status_code == 200
    facts = {f.key: f for group in response.context["groups"] for f in group.facts}
    for key in ("end-sessions", "page-storage"):
        fact = facts[key]
        if middleware_change == "shipped":
            assert fact.value != security_settings.CHANGED
        else:
            assert fact.value == security_settings.CHANGED and "MIDDLEWARE" in fact.note
        assert fact.value in unescape(response.content.decode())
    assert rows_for(response) == []


def test_production_500_delivers_no_report_with_admins_configured(
    client, agent, ticket, settings, monkeypatch
):
    from django.core import mail

    from desk import views

    settings.DEBUG = False
    settings.ADMINS = [("Test recipient", "review@example.invalid")]
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    reports = []
    init = ExceptionReporter.__init__

    def observed(self, *args, **kwargs):
        reports.append(True)
        return init(self, *args, **kwargs)

    def unavailable(**kwargs):
        raise RuntimeError("Synthetic service interruption")

    monkeypatch.setattr(ExceptionReporter, "__init__", observed)
    monkeypatch.setattr(views, "add_entry_with_files", unavailable)
    client.force_login(agent)
    client.raise_request_exception = False
    response = client.post(
        reverse("desk:request-entry", args=[ticket.pk]),
        {"kind": "note", "body": "Synthetic-submitted-note", "expected_revision": ticket.revision},
        secure=True,
    )
    assert response.status_code == 500 and isinstance(response.exc_info[1], RuntimeError)
    assert (
        b"Something went wrong" in response.content
        and b"Synthetic-submitted-note" not in response.content
    )
    assert reports == [] and mail.outbox == []
    assert logging.getLogger("django.request").hasHandlers() and response["X-Request-ID"]
