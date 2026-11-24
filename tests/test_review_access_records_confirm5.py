"""Check the portal help when Request again restores typing but copies nothing.

Adopted from the review's fifth confirmation (the reviewer's original is kept outside the
repository)."""

# Pytest resolves the imported request-form fixtures by name.
# ruff: noqa: F811
import pytest
from django.apps import apps
from django.urls import reverse

from desk.tests.test_kept_typing import end_open, given_back, marker
from request_forms.tests.conftest import (  # noqa: F401
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


@pytest.mark.django_db
def test_nothing_copied_help_accounts_for_typing_put_back(client, portal_user, pickup):
    pickup.fields.filter(key="count").update(copy_on_repeat=False)
    source = raised(portal_user, pickup, {"count": 72})
    assert source.site_id is None
    url = reverse("portal_forms:raise", args=[pickup.key], query={"again": source.number})
    client.force_login(portal_user)
    first = client.get(url)
    assert first.status_code == 200 and Inputs(first).values["field-count"] == ""
    token = marker(first)[0]
    end_open(portal_user)
    kept = client.post(
        reverse("account-session-keep"),
        {"forms": [{"token": token, "values": {"field-count": "83"}}]},
        content_type="application/json",
    )
    assert kept.status_code == 200 and kept.json()["kept"] == 1
    client.force_login(portal_user)
    restored = client.get(url)
    assert restored.status_code == 200
    assert Inputs(restored).values["field-count"] == given_back(restored)["field-count"] == "83"
    if "accessrecord" in apps.get_app_config("core").models:
        record = apps.get_model("core", "AccessRecord")
        assert not record.objects.filter(request_ref=restored["X-Request-ID"]).exists()

    help_page = restored.context["page_documentation"]
    section = next(s for s in help_page["sections"] if s["title"] == "Who can open it")
    assert "when nothing can be copied, the form opens empty and nothing is recorded." not in (
        " ".join(section["paragraphs"]).lower()
    ), "The form shows the user's saved 83 while its help says it opens empty."
