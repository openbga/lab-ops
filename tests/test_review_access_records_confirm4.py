"""Check the portal help's description of the source-ticket record.

Adopted from the review's fourth confirmation (the reviewer's original is kept outside the
repository)."""

# Pytest resolves the imported request-form fixtures by name.
# ruff: noqa: F811
import pytest
from django.apps import apps
from django.urls import reverse

from request_forms.models import Audience, FieldKind
from request_forms.tests.conftest import (  # noqa: F401
    capable,
    catalog,
    clinic,
    group,
    logistics,
    make_user,
    pickup,
    pickup_category,
    portal_user,
)
from request_forms.tests.test_capture_consistency import Inputs
from request_forms.tests.test_pages import raised

pytestmark = pytest.mark.django_db


def open_again(client, portal_user, template, source):
    client.force_login(portal_user)
    response = client.get(
        reverse("portal_forms:raise", args=[template.key], query={"again": source.number})
    )
    assert response.status_code == 200
    return response


def test_help_accounts_for_a_repeat_form_with_nothing_to_copy(client, portal_user, pickup):
    pickup.fields.filter(key="count").update(copy_on_repeat=False)
    source = raised(portal_user, pickup, {"count": 510687})
    assert source.site_id is None
    response = open_again(client, portal_user, pickup, source)
    assert Inputs(response).values["field-count"] == ""
    if "accessrecord" in apps.get_app_config("core").models:
        record = apps.get_model("core", "AccessRecord")
        assert not record.objects.filter(request_ref=response["X-Request-ID"]).exists()
    help_page = response.context["page_documentation"]
    assert help_page["id"] == "portal-forms-raise"
    section = next(s for s in help_page["sections"] if s["title"] == "Who can open it")
    for paragraph in section["paragraphs"]:
        if paragraph.startswith(
            "Opened with Request again from one of your tickets, the form is filled"
        ):
            assert any(
                word in paragraph.lower()
                for word in ("if ", "when ", "only ", "unless ", "nothing", "no record")
            ), (
                "The help says opening Request again fills the form and creates a record, "
                "even when the source supplied nothing"
            )


def test_supervisor_can_read_the_source_account_and_time_without_the_answer(
    client, portal_user, pickup, capable
):
    record = apps.get_model("core", "AccessRecord")
    pickup.fields.create(
        key="delivery-note",
        label="Delivery note",
        kind=FieldKind.SHORT_TEXT,
        audience=Audience.CLIENT,
        position=180,
    )
    answer = "Synthetic collection at the east desk"
    source = raised(portal_user, pickup, {"count": 3, "delivery-note": answer})
    response = open_again(client, portal_user, pickup, source)
    assert Inputs(response).values["field-delivery-note"] == answer
    receipt = record.objects.get(request_ref=response["X-Request-ID"])
    assert (receipt.ticket_id, receipt.account_id, receipt.capacity, receipt.kind) == (
        source.pk,
        portal_user.pk,
        "client",
        "repeat",
    )
    assert receipt.occurred_at is not None
    assert receipt.object_type == receipt.object_id == ""
    client.force_login(capable)
    page = client.get(reverse("desk:request-access", args=[source.pk]))
    assert page.status_code == 200
    (row,) = page.context["page_obj"]
    assert row.request_ref == receipt.request_ref
    assert (row.username, row.occurred_at) == (portal_user.username, receipt.occurred_at)
    assert row.what == receipt.get_kind_display() and row.detail == ""
    assert answer.encode() not in page.content
