"""Staff form intake enforces the category protection shown when the page opened."""

import re

import pytest
from django.urls import reverse
from django.utils.html import escape

from desk.models import ServiceRequest
from desk.selectors import staff_requests
from request_forms.models import FilledForm
from taxonomy import terms
from taxonomy.services import update_category

from .test_definition_changes import drawn_token, intake

pytestmark = pytest.mark.django_db


def category_change(admin, category, **values):
    category.refresh_from_db()
    return update_category(
        actor=admin,
        category_id=category.pk,
        expected_revision=category.revision,
        reason="Synthetic visibility change",
        **values,
    )


def drawn_terms(page):
    match = re.search(r'name="category_terms" value="([^"]+)"', page.content.decode())
    assert match, "the page carries no category terms"
    return match.group(1)


def test_restriction_change_invalidates_an_open_form(client, capable, agent, admin, clinic, pickup):
    category_change(admin, pickup.category, is_restricted=True)
    client.force_login(capable)
    page = client.get(
        reverse("desk:category-guidance"),
        {"category": pickup.category_id, "client": clinic.pk, "request_form": pickup.pk},
    )
    assert b"members and supervisors see these tickets" in page.content
    token, carried = drawn_token(page), drawn_terms(page)
    category_change(admin, pickup.category, is_restricted=False)
    response = intake(
        client,
        pickup.category,
        client=clinic.pk,
        request_form=pickup.pk,
        form_definition=token,
        category_terms=carried,
        **{"field-count": "2"},
    )
    if response.status_code == 302:
        row = FilledForm.objects.get()
        assert not staff_requests(agent).filter(pk=row.request_id).exists(), (
            "Previously restricted intake accepted for all staff"
        )
    assert not FilledForm.objects.exists()
    assert not ServiceRequest.objects.exists()
    assert escape(terms.CHANGED) in response.content.decode()
    assert b"members and supervisors see these tickets" not in response.content
